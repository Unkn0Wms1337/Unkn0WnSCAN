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

import asyncio
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
DEFAULT_INSTANT_PROXY = (
    os.getenv("AVITO_WARP_INSTANT_PROXY", "").strip()
    or DEFAULT_USQUE_PROXY
)
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
    global _last_hop_at
    warp_bin = find_warp_cli()
    if not warp_bin:
        return False, "warp-cli не найден"
    if not proxy_alive(proxy):
        return False, "WARP-прокси не отвечает"
    if not _hop_lock.acquire(blocking=False):
        return False, "смена IP уже выполняется"
    try:
        if time.time() - _last_hop_at < 3:
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
        if time.time() - _last_hop_at < 3:
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
# Мгновенный обход: пул готовых WARP-тоннелей + локальный SOCKS5-фасад
#
# Браузер всегда ходит на один и тот же адрес (фасад). Внутри живут два
# заранее зарегистрированных usque-тоннеля с разными IPv6 /64. При блокировке
# фасад переключается на резервный тоннель за миллисекунды, а старый тоннель
# перерегистрируется в фоне. Перезапуск браузера не нужен.
# --------------------------------------------------------------------------

POOL_BACKEND_PORTS = (25350, 25351)
_ports_env = (os.getenv("AVITO_WARP_POOL_PORTS") or "").strip()
if _ports_env:
    try:
        _parsed = tuple(
            int(part) for part in _ports_env.replace(";", ",").split(",") if part
        )
        if len(_parsed) >= 2:
            POOL_BACKEND_PORTS = _parsed[:2]
    except ValueError:
        pass


def _pool_backend_proxy(port: int) -> str:
    return f"socks5://127.0.0.1:{port}"


_pool: dict[int, dict] = {}
_pool_active_port: int | None = None
_pool_front_port: int | None = None
_pool_thread: threading.Thread | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None
_pool_server: asyncio.AbstractServer | None = None
_pool_ready = threading.Event()


def _pool_ensure_exe(download: bool) -> Path | None:
    exe = _usque_default_path()
    if not exe.is_absolute():
        exe = Path.cwd() / exe
    if not exe.exists() and (not download or not _download_usque(exe)):
        return None
    return exe


def _pool_stop_tunnel(port: int) -> None:
    info = _pool.pop(port, None)
    process = info.get("process") if info else None
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    _kill_port_owner(_pool_backend_proxy(port))
    time.sleep(0.5)


def _pool_start_tunnel(
    exe: Path, port: int, timeout: float = 90.0
) -> bool:
    """Поднимает один usque и подтверждает реальный внешний IPv6."""
    proxy = _pool_backend_proxy(port)
    if proxy_alive(proxy):
        current = egress_ip(proxy, timeout=15.0)
        if current:
            _pool[port] = {
                "process": None,
                "egress": current,
                "ready": True,
                "exe": exe,
            }
            return True
        _pool_stop_tunnel(port)
    config = exe.parent / f"usque-{port}.json"
    if not config.exists() and not _usque_register(exe, config):
        return False
    try:
        process = subprocess.Popen(
            [
                str(exe),
                "-c",
                str(config),
                "socks",
                "-b",
                "127.0.0.1",
                "-p",
                str(port),
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("WARP: не удалось запустить пул: %s", str(exc)[:140])
        return False
    deadline = time.time() + max(15.0, timeout)
    while time.time() < deadline:
        if proxy_alive(proxy):
            current = egress_ip(proxy, timeout=25.0)
            if current:
                _pool[port] = {
                    "process": process,
                    "egress": current,
                    "ready": True,
                    "exe": exe,
                }
                return True
        if process.poll() is not None:
            break
        time.sleep(1.0)
    _pool_stop_tunnel(port)
    return False


def _pool_refill(port: int) -> None:
    """В фоне перерегистрирует отработавший тоннель на новое устройство."""
    try:
        with _state_lock:
            if _pool_front_port is None:
                return
            info = _pool.get(_pool_active_port or -1)
            exe = (info or {}).get("exe")
        _pool_stop_tunnel(port)
        if exe is None:
            exe = _pool_ensure_exe(download=False)
        if exe is None:
            return
        config = exe.parent / f"usque-{port}.json"
        config.unlink(missing_ok=True)
        if _pool_start_tunnel(exe, port, timeout=90.0):
            _logo(f"резервный WARP-тоннель :{port} снова готов")
    except Exception:  # noqa: BLE001
        logger.debug("WARP: не удалось пополнить пул", exc_info=True)


def _pool_candidates() -> list[int]:
    with _state_lock:
        active = _pool_active_port
        ports = [p for p in POOL_BACKEND_PORTS if p != active]
        if active in _pool:
            ports.insert(0, active)
        return [p for p in ports if p in _pool and _pool[p].get("ready")]


async def _socks_read_reply(reader: asyncio.StreamReader) -> int:
    header = await reader.readexactly(4)
    if header[0] != 5:
        raise OSError("bad socks reply")
    atyp = header[3]
    if atyp == 1:
        await reader.readexactly(4)
    elif atyp == 3:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length)
    elif atyp == 4:
        await reader.readexactly(16)
    await reader.readexactly(2)
    return header[1]


def _socks_addr(host: str) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raw = host.encode("idna")
        return b"\x03" + bytes([len(raw)]) + raw
    if ip.version == 4:
        return b"\x01" + ip.packed
    return b"\x04" + ip.packed


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _front_handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        greeting = await asyncio.wait_for(client_reader.readexactly(2), 10)
        if greeting[0] != 5:
            return
        await client_reader.readexactly(greeting[1])
        client_writer.write(b"\x05\x00")
        await client_writer.drain()

        header = await asyncio.wait_for(client_reader.readexactly(4), 15)
        if header[0] != 5 or header[1] != 1:
            client_writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await client_writer.drain()
            return
        atyp = header[3]
        if atyp == 1:
            host = socket.inet_ntoa(await client_reader.readexactly(4))
        elif atyp == 3:
            length = (await client_reader.readexactly(1))[0]
            host = (await client_reader.readexactly(length)).decode("idna")
        elif atyp == 4:
            host = socket.inet_ntop(
                socket.AF_INET6, await client_reader.readexactly(16)
            )
        else:
            return
        port = int.from_bytes(await client_reader.readexactly(2), "big")

        for backend in _pool_candidates():
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", backend), 4
                )
                upstream_writer = up_writer
                up_writer.write(b"\x05\x01\x00")
                await up_writer.drain()
                if await asyncio.wait_for(up_reader.readexactly(2), 6) != b"\x05\x00":
                    raise OSError("socks auth failed")
                up_writer.write(
                    b"\x05\x01\x00" + _socks_addr(host) + port.to_bytes(2, "big")
                )
                await up_writer.drain()
                reply = await asyncio.wait_for(_socks_read_reply(up_reader), 12)
                if reply != 0:
                    raise OSError(f"socks connect failed: {reply}")
                client_writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await client_writer.drain()
                await asyncio.gather(
                    _pipe(client_reader, up_writer),
                    _pipe(up_reader, client_writer),
                )
                return
            except Exception:  # noqa: BLE001
                if upstream_writer is not None:
                    try:
                        upstream_writer.close()
                    except Exception:  # noqa: BLE001
                        pass
                    upstream_writer = None

        client_writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
        await client_writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        for writer in (upstream_writer, client_writer):
            if writer is not None:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass


def _front_thread_main(port: int) -> None:
    global _pool_loop, _pool_server
    loop = asyncio.new_event_loop()
    _pool_loop = loop
    asyncio.set_event_loop(loop)

    async def serve() -> asyncio.AbstractServer:
        return await asyncio.start_server(
            _front_handle, "127.0.0.1", port, backlog=256
        )

    try:
        server = loop.run_until_complete(serve())
        _pool_server = server
        _pool_ready.set()
        loop.run_forever()
    except Exception:  # noqa: BLE001
        logger.warning("WARP: SOCKS-фасад остановился", exc_info=True)
    finally:
        _pool_ready.set()
        try:
            if _pool_server is not None:
                _pool_server.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def _pool_start_front(proxy: str) -> bool:
    global _pool_front_port, _pool_thread
    target = proxy_host_port(proxy)
    if target is None:
        return False
    _, port = target
    if _pool_thread is not None and _pool_thread.is_alive() and proxy_alive(proxy):
        _pool_front_port = port
        return True
    _kill_port_owner(proxy)
    _pool_ready.clear()
    _pool_thread = threading.Thread(
        target=_front_thread_main,
        args=(port,),
        daemon=True,
        name="warp-socks-front",
    )
    _pool_thread.start()
    _pool_ready.wait(timeout=8.0)
    if not proxy_alive(proxy):
        return False
    _pool_front_port = port
    _logo(
        f"мгновенный WARP готов: фасад 127.0.0.1:{port}, "
        f"тоннели {POOL_BACKEND_PORTS[0]}/{POOL_BACKEND_PORTS[1]}"
    )
    return True


def _pool_ensure(
    front_proxy: str,
    path: str | None = None,
    download: bool = True,
    timeout: float = 120.0,
) -> bool:
    """Поднимает два готовых тоннеля и фасад. Возвращает True при успехе."""
    global _pool_active_port
    if (
        _pool_thread is not None
        and _pool_thread.is_alive()
        and _pool_front_port is not None
        and proxy_alive(front_proxy)
        and _pool_candidates()
    ):
        return True
    exe = Path(path) if path else _pool_ensure_exe(download)
    if exe is None:
        return False
    if not exe.is_absolute():
        exe = Path.cwd() / exe
    started = time.time()
    ready_ports: list[int] = []
    for port in POOL_BACKEND_PORTS:
        remaining = max(20.0, timeout - (time.time() - started))
        if _pool_start_tunnel(exe, port, timeout=remaining):
            ready_ports.append(port)
    if len(ready_ports) < 2:
        for port in ready_ports:
            _pool_stop_tunnel(port)
        return False
    with _state_lock:
        _pool_active_port = ready_ports[0]
    if not _pool_start_front(front_proxy):
        return False
    logger.info(
        "WARP: пул готов за %.1f с (%s / %s)",
        time.time() - started,
        _prefix64(_pool[ready_ports[0]].get("egress")),
        _prefix64(_pool[ready_ports[1]].get("egress")),
    )
    return True


def _pool_hop(proxy: str, timeout: float = 90.0) -> tuple[bool, str]:
    """Мгновенно переключает фасад на резервный тоннель."""
    global _pool_active_port, _last_hop_at
    with _state_lock:
        if _pool_front_port is None or proxy != (
            f"socks5://127.0.0.1:{_pool_front_port}"
        ):
            return False, "фасад не запущен"
        active = _pool_active_port
        standby = next(
            (p for p in POOL_BACKEND_PORTS if p != active and p in _pool),
            None,
        )
        if active is None or standby is None:
            return False, "резервный тоннель ещё готовится"
        before = _prefix64(_pool[active].get("egress"))
        after = _prefix64(_pool[standby].get("egress"))
        if not after or after == before:
            return False, "резервный адрес совпадает"
        _pool_active_port = standby
        _last_hop_at = time.time()
    _logo(f"мгновенная смена IP {before or '?'} -> {after}")
    threading.Thread(
        target=_pool_refill, args=(active,), daemon=True, name="warp-refill"
    ).start()
    return True, f"{before or '?'} -> {after}"


def needs_browser_restart(proxy: str | None = None) -> bool:
    """False для пула: браузер продолжает работать через тот же фасад."""
    target = proxy or _active_proxy
    return not (
        _backend == "instant"
        and _pool_front_port is not None
        and target == f"socks5://127.0.0.1:{_pool_front_port}"
    )


def _pool_shutdown() -> None:
    global _pool_thread, _pool_loop, _pool_server, _pool_front_port, _pool_active_port
    loop = _pool_loop
    server = _pool_server
    if loop is not None and server is not None:
        async def close_front() -> None:
            try:
                server.close()
                await server.wait_closed()
            finally:
                loop.stop()

        try:
            loop.call_soon_threadsafe(
                lambda: loop.create_task(close_front())
            )
        except Exception:  # noqa: BLE001
            pass
    if _pool_thread is not None:
        _pool_thread.join(timeout=3)
    _pool_thread = None
    _pool_loop = None
    _pool_server = None
    _pool_front_port = None
    _pool_active_port = None
    for port in list(_pool):
        _pool_stop_tunnel(port)


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

    prefer: auto | instant | official | usque | off.
    """
    global _backend, _active_proxy, _usque_proxy
    prefer = (prefer or "auto").strip().lower()
    if prefer == "off":
        return None
    with _state_lock:
        if prefer in {"auto", "instant"}:
            path = Path(usque_path) if usque_path else None
            # The pool's public address is deliberately independent from the
            # backend usque port.  Honor AVITO_WARP_INSTANT_PROXY here rather
            # than accidentally binding the facade to the legacy 25344 port.
            instant_proxy = DEFAULT_INSTANT_PROXY
            if _pool_ensure(
                instant_proxy,
                path=path,
                download=download,
                timeout=timeout,
            ):
                _backend = "instant"
                _active_proxy = instant_proxy
                return instant_proxy
            if prefer == "instant":
                return None
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
    if _backend == "instant":
        return _pool_hop(target, timeout)
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
    if _backend == "instant":
        return _pool_ensure(proxy, timeout=timeout)
    if _usque_proxy and proxy == _usque_proxy:
        return _usque_start(proxy, timeout=timeout)
    return _official_start(proxy, timeout=timeout)


def shutdown() -> None:
    """Останавливает только наш встроенный usque; официальный WARP не трогаем."""
    _pool_shutdown()
    _stop_usque()


def status(proxy: str | None = None) -> dict:
    target = proxy or _active_proxy or DEFAULT_WARP_PROXY
    with _state_lock:
        pool = {
            port: _prefix64(info.get("egress"))
            for port, info in _pool.items()
        }
        active = _pool_active_port
    return {
        "backend": _backend,
        "proxy": target,
        "proxy_alive": proxy_alive(target),
        "warp_cli": find_warp_cli(),
        "usque": str(_usque_default_path()),
        "pool": pool,
        "pool_active": active,
        "egress": egress_ip(target) if proxy_alive(target) else None,
    }
