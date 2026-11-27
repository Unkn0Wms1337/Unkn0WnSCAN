"""Смена выходного IP через Cloudflare WARP — подход openhop.

Два взаимозаменяемых движка:

* **official** — установленный Cloudflare WARP (`warp-cli`) в режиме
  локального SOCKS5 (обычно 127.0.0.1:40000). Смена IP:
  `tunnel rotate-keys` + `tunnel endpoint reset` + reconnect.
* **usque** — собственный WARP-клиент (MASQUE), скачивается автоматически.
  Не требует прав администратора и установки WARP, поднимает SOCKS5 на
  127.0.0.1:25344. Смена IP — новая регистрация WARP-устройства.

Оба варианта дают новый IPv6 /64, который Авито видит как новый адрес.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests

logger = logging.getLogger(__name__)

DEFAULT_WARP_PROXY = "socks5://127.0.0.1:40000"
DEFAULT_USQUE_PROXY = "socks5://127.0.0.1:25344"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

USQUE_VERSION = "v4.2.1"
USQUE_URL = os.getenv("AVITO_USQUE_URL", "").strip()
USQUE_ASSETS = {
    ("Windows", "AMD64"): ("usque_4.2.1_windows_amd64.zip", "usque.exe"),
    ("Windows", "x86"): ("usque_4.2.1_windows_386.zip", "usque.exe"),
    ("Windows", "ARM64"): ("usque_4.2.1_windows_arm64.zip", "usque.exe"),
    ("Linux", "x86_64"): ("usque_4.2.1_linux_amd64.zip", "usque"),
    ("Linux", "aarch64"): ("usque_4.2.1_linux_arm64.zip", "usque"),
    ("Linux", "arm64"): ("usque_4.2.1_linux_arm64.zip", "usque"),
    ("Darwin", "x86_64"): ("usque_4.2.1_darwin_amd64.zip", "usque"),
    ("Darwin", "arm64"): ("usque_4.2.1_darwin_arm64.zip", "usque"),
}

_WARP_CANDIDATES = (
    Path(r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    Path(r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Cloudflare"
    / "Cloudflare WARP"
    / "warp-cli.exe",
    Path("/usr/bin/warp-cli"),
    Path("/usr/local/bin/warp-cli"),
)

_hop_lock = threading.RLock()
_state_lock = threading.RLock()
_last_hop_at = 0.0

_backend: str | None = None
_active_proxy: str | None = None
_usque_proxy = DEFAULT_USQUE_PROXY
_usque_process: subprocess.Popen | None = None
_usque_exe: Path | None = None


# --------------------------------------------------------------------------
# Общие помощники
# --------------------------------------------------------------------------


def find_warp_cli() -> str | None:
    """Путь к warp-cli.exe (официальный клиент Cloudflare WARP)."""
    env = (os.getenv("WARP_CLI") or "").strip()
    if env and Path(env).exists():
        return env
    on_path = shutil.which("warp-cli")
    if on_path:
        return on_path
    for candidate in _WARP_CANDIDATES:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def proxy_host_port(proxy: str) -> tuple[str, int] | None:
    value = (proxy or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if not parts.hostname:
        return None
    port = parts.port or (1080 if parts.scheme.startswith("socks") else 8080)
    return parts.hostname, port


def proxy_alive(proxy: str, timeout: float = 2.5) -> bool:
    """Быстрая проверка, что локальный прокси-порт слушается."""
    target = proxy_host_port(proxy)
    if target is None:
        return False
    host, port = target
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def egress_ip(proxy: str, timeout: float = 12.0) -> str | None:
    """Внешний IP, который видит интернет через прокси (IPv6 у WARP)."""
    try:
        response = requests.get(
            TRACE_URL,
            impersonate="chrome",
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    for line in (response.text or "").splitlines():
        if line.startswith("ip="):
            return line.split("=", 1)[1].strip() or None
    return None


def _prefix64(ip: str | None) -> str:
    """Первые 64 бита адреса: WARP выдаёт новую IPv6 /64 при каждом прыжке."""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if addr.version != 6:
        return str(addr)
    network = ipaddress.ip_network(f"{addr}/64", strict=False)
    return str(network.network_address)


def _logo(message: str) -> None:
    logger.info("WARP: %s", message)


# --------------------------------------------------------------------------
# Официальный Cloudflare WARP (warp-cli)
# --------------------------------------------------------------------------


def _run_warp(args: list[str], warp_bin: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [warp_bin, "--accept-tos", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return completed.returncode, output
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def warp_connected(warp_bin: str) -> bool:
    rc, output = _run_warp(["status"], warp_bin)
    if rc != 0:
        return False
    lowered = output.casefold()
    return "connected" in lowered and "disconnected" not in lowered


def _official_start(proxy: str, timeout: float = 45.0) -> bool:
    if proxy_alive(proxy):
        return True
    warp_bin = find_warp_cli()
    if not warp_bin:
        return False
    if not warp_connected(warp_bin):
        _run_warp(["connect"], warp_bin)
    deadline = time.time() + max(5.0, timeout)
    while time.time() < deadline:
        if proxy_alive(proxy):
            return True
        time.sleep(1.5)
    return proxy_alive(proxy)


def _official_hop(proxy: str, timeout: float = 90.0) -> tuple[bool, str]:
    warp_bin = find_warp_cli()
    if not warp_bin:
        return False, "warp-cli не найден"
    if not proxy_alive(proxy):
        return False, "WARP-прокси не отвечает"
    if not _hop_lock.acquire(blocking=False):
        return False, "смена IP уже выполняется"
    try:
        if time.time() - _last_hop_at < 30:
            return False, "слишком часто"
        before = egress_ip(proxy)
        _run_warp(["tunnel", "rotate-keys"], warp_bin)
        _run_warp(["tunnel", "endpoint", "reset"], warp_bin)
        _run_warp(["disconnect"], warp_bin)
        time.sleep(1.0)
        _run_warp(["connect"], warp_bin)
        deadline = time.time() + max(20.0, timeout)
        while time.time() < deadline:
            if not warp_connected(warp_bin):
                time.sleep(1.5)
                continue
            current = egress_ip(proxy)
            if current and _prefix64(current) != _prefix64(before):
                _last_hop_at = time.time()
                note = f"{_prefix64(before) or '?'} -> {_prefix64(current)}"
                _logo(f"новый адрес {note}")
                return True, note
            time.sleep(1.5)
        return False, "адрес не изменился"
    finally:
        _hop_lock.release()


# --------------------------------------------------------------------------
# usque (встроенный WARP без установки и без прав администратора)
# --------------------------------------------------------------------------


def _usque_default_path() -> Path:
    env = (os.getenv("AVITO_USQUE_PATH") or "").strip()
    if env:
        return Path(env)
    name = "usque.exe" if os.name == "nt" else "usque"
    return Path(__file__).resolve().parent / "data" / "warp" / name


def _usque_download_url() -> str | None:
    if USQUE_URL:
        return USQUE_URL
    key = (platform.system(), platform.machine())
    asset = USQUE_ASSETS.get(key)
    if asset is None:
        return None
    return (
        "https://github.com/Diniboy1123/usque/releases/download/"
        f"{USQUE_VERSION}/{asset[0]}"
    )


def _bundle_zip() -> Path:
    """Встроенный в репозиторий архив usque (работает без GitHub)."""
    return Path(__file__).resolve().parent / "vendor" / "usque.zip"


def _extract_usque(archive: Path, destination: Path) -> bool:
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = [name for name in bundle.namelist() if "/" not in name.strip("/")]
            target = next(
                (name for name in names if name.endswith(destination.name)),
                None,
            )
            if target is None:
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(target))
        if os.name != "nt":
            destination.chmod(0o755)
        return destination.exists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("WARP: не удалось распаковать usque: %s", str(exc)[:160])
        return False


def _download_usque(destination: Path, timeout: float = 120.0) -> bool:
    bundle = _bundle_zip()
    if bundle.exists():
        _logo("распаковываю встроенный WARP-клиент usque…")
        if _extract_usque(bundle, destination):
            return True
    url = _usque_download_url()
    if not url:
        logger.warning(
            "WARP: нет сборки usque для %s/%s",
            platform.system(),
            platform.machine(),
        )
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / "usque.zip"
    _logo("скачиваю встроенный WARP-клиент usque (~5 МБ, один раз)…")
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                impersonate="chrome",
                timeout=(15, max(60.0, timeout)),
                allow_redirects=True,
            )
            if response.status_code == 200 and len(response.content) > 1_000_000:
                archive.write_bytes(response.content)
                if _extract_usque(archive, destination):
                    archive.unlink(missing_ok=True)
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WARP: попытка %s/3 скачать usque не удалась: %s",
                attempt,
                str(exc)[:140],
            )
        time.sleep(2.0)
    archive.unlink(missing_ok=True)
    return False


def _usque_register(exe: Path, config: Path, timeout: float = 90.0) -> bool:
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(exe), "-c", str(config), "register", "--accept-tos"],
            cwd=str(config.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0 and config.exists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("WARP: регистрация usque не удалась: %s", str(exc)[:160])
        return False


def _kill_port_owner(proxy: str) -> None:
    """Освобождает порт, если остался процесс от прошлого запуска бота."""
    target = proxy_host_port(proxy)
    if target is None:
        return
    _, port = target
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}"):
                    if parts[3].upper() == "LISTENING":
                        subprocess.run(
                            ["taskkill", "/PID", parts[4], "/F"],
                            capture_output=True,
                            timeout=10,
                            creationflags=getattr(
                                subprocess, "CREATE_NO_WINDOW", 0
                            ),
                        )
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass


def _stop_usque() -> None:
    global _usque_process
    process = _usque_process
    _usque_process = None
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=6)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    if _usque_proxy:
        _kill_port_owner(_usque_proxy)
    time.sleep(0.8)


def _usque_start(
    proxy: str,
    path: Path | None = None,
    download: bool = True,
    timeout: float = 90.0,
) -> bool:
    global _usque_process, _usque_exe
    if proxy_alive(proxy):
        return True
    exe = Path(path) if path else _usque_default_path()
    if not exe.is_absolute():
        exe = Path.cwd() / exe
    if not exe.exists():
        if not download or not _download_usque(exe):
            return False
    config = exe.parent / "usque.json"
    if not config.exists() and not _usque_register(exe, config):
        return False
    target = proxy_host_port(proxy)
    if target is None:
        return False
    host, port = target
    try:
        _usque_process = subprocess.Popen(
            [
                str(exe),
                "-c",
                str(config),
                "socks",
                "-b",
                host,
                "-p",
                str(port),
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("WARP: не удалось запустить usque: %s", str(exc)[:160])
        return False
    deadline = time.time() + max(10.0, timeout)
    while time.time() < deadline:
        if proxy_alive(proxy):
            # usque поднимает SOCKS5 сразу, а MASQUE-туннель — лениво, при
            # первом запросе. Прогреваем его и убеждаемся, что трафик идёт.
            if egress_ip(proxy, timeout=min(40.0, max(15.0, timeout / 2))):
                _usque_exe = exe
                _logo("встроенный WARP (usque) запущен")
                return True
        if _usque_process.poll() is not None:
            break
        time.sleep(1.0)
    _stop_usque()
    return False


def _usque_hop(proxy: str, timeout: float = 90.0) -> tuple[bool, str]:
    global _last_hop_at
    if not _hop_lock.acquire(blocking=False):
        return False, "смена IP уже выполняется"
    try:
        if time.time() - _last_hop_at < 20:
            return False, "слишком часто"
        before = egress_ip(proxy, timeout=30.0)
        exe = _usque_exe or _usque_default_path()
        if not exe.is_absolute():
            exe = Path.cwd() / exe
        config = exe.parent / "usque.json"
        _stop_usque()
        config.unlink(missing_ok=True)
        if not _usque_register(exe, config):
            return False, "не удалось зарегистрировать новое устройство"
        if not _usque_start(proxy, exe, download=False, timeout=timeout):
            return False, "не удалось поднять туннель"
        deadline = time.time() + max(10.0, timeout)
        while time.time() < deadline:
            current = egress_ip(proxy, timeout=30.0)
            if current and _prefix64(current) != _prefix64(before):
                _last_hop_at = time.time()
                note = f"{_prefix64(before) or '?'} -> {_prefix64(current)}"
                _logo(f"новый адрес {note}")
                return True, note
            time.sleep(1.5)
        return False, "адрес не изменился"
    finally:
        _hop_lock.release()


# --------------------------------------------------------------------------
# Публичный интерфейс
# --------------------------------------------------------------------------


def ensure_ready(
    prefer: str = "auto",
    official_proxy: str = DEFAULT_WARP_PROXY,
    usque_proxy: str = DEFAULT_USQUE_PROXY,
    usque_path: str | None = None,
    download: bool = True,
    timeout: float = 120.0,
) -> str | None:
    """Поднимает доступный WARP и возвращает прокси, либо None.

    prefer: auto | official | usque | off.
    """
    global _backend, _active_proxy, _usque_proxy
    prefer = (prefer or "auto").strip().lower()
    if prefer == "off":
        return None
    with _state_lock:
        if prefer in {"auto", "official"} and proxy_alive(official_proxy):
            _backend = "official"
            _active_proxy = official_proxy
            return official_proxy
        if prefer in {"auto", "official"} and find_warp_cli():
            if _official_start(official_proxy, timeout=min(30.0, timeout)):
                _backend = "official"
                _active_proxy = official_proxy
                return official_proxy
        if prefer in {"auto", "usque"}:
            _usque_proxy = usque_proxy
            path = Path(usque_path) if usque_path else None
            if _usque_start(
                usque_proxy,
                path=path,
                download=download,
                timeout=timeout,
            ):
                _backend = "usque"
                _active_proxy = usque_proxy
                return usque_proxy
    return None


def active_proxy() -> str | None:
    return _active_proxy


def backend() -> str | None:
    return _backend


def hop(proxy: str | None = None, timeout: float = 90.0) -> tuple[bool, str]:
    """Один «прыжок»: новый IPv6 /64 у выбранного движка."""
    target = proxy or _active_proxy
    if not target:
        return False, "WARP не запущен"
    if _backend == "usque" or (
        _usque_proxy and target == _usque_proxy
    ):
        return _usque_hop(target, timeout)
    return _official_hop(target, timeout)


def ensure_warp_proxy(
    proxy: str = DEFAULT_WARP_PROXY, timeout: float = 45.0
) -> bool:
    """Совместимость: поднимает нужный движок для указанного прокси."""
    if proxy_alive(proxy):
        return True
    if _usque_proxy and proxy == _usque_proxy:
        return _usque_start(proxy, timeout=timeout)
    return _official_start(proxy, timeout=timeout)


def shutdown() -> None:
    """Останавливает только наш встроенный usque; официальный WARP не трогаем."""
    _stop_usque()


def status(proxy: str | None = None) -> dict:
    target = proxy or _active_proxy or DEFAULT_WARP_PROXY
    return {
        "backend": _backend,
        "proxy": target,
        "proxy_alive": proxy_alive(target),
        "warp_cli": find_warp_cli(),
        "usque": str(_usque_default_path()),
        "egress": egress_ip(target) if proxy_alive(target) else None,
    }
