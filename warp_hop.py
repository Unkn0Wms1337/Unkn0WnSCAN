"""Смена выходного IP через Cloudflare WARP — подход openhop.

openhop (https://openhop.cfd) держит WARP в режиме локального SOCKS5-прокси
(127.0.0.1:40000), а при исчерпании лимита «прыгает» на новый адрес:
`warp-cli tunnel rotate-keys`, `warp-cli tunnel endpoint reset`,
disconnect/connect. Здесь тот же механизм, но для Авито: браузер ходит через
WARP, а при блокировке бот сам получает новый IPv6 /64.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests

logger = logging.getLogger(__name__)

DEFAULT_WARP_PROXY = "socks5://127.0.0.1:40000"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

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

_hop_lock = threading.Lock()
_last_hop_at = 0.0


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


def ensure_warp_proxy(proxy: str = DEFAULT_WARP_PROXY, timeout: float = 45.0) -> bool:
    """Проверяет локальный прокси WARP и при необходимости поднимает тоннель."""
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


def hop(proxy: str = DEFAULT_WARP_PROXY, timeout: float = 90.0) -> tuple[bool, str]:
    """Один «прыжок» WARP: новый ключ, новый endpoint, переподключение.

    Возвращает (успех, описание). Смена подтверждается по внешнему IP.
    """
    global _last_hop_at
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
        last = before
        while time.time() < deadline:
            if not warp_connected(warp_bin):
                time.sleep(1.5)
                continue
            last = egress_ip(proxy)
            if last and _prefix64(last) != _prefix64(before):
                _last_hop_at = time.time()
                logger.info(
                    "WARP: новый адрес %s -> %s",
                    _prefix64(before) or "нет",
                    _prefix64(last),
                )
                return True, f"{_prefix64(before) or '?'} -> {_prefix64(last)}"
            time.sleep(1.5)
        return False, "адрес не изменился"
    finally:
        _hop_lock.release()


def status(proxy: str = DEFAULT_WARP_PROXY) -> dict:
    warp_bin = find_warp_cli()
    return {
        "warp_cli": warp_bin,
        "proxy": proxy,
        "proxy_alive": proxy_alive(proxy),
        "connected": warp_connected(warp_bin) if warp_bin else False,
        "egress": egress_ip(proxy) if proxy_alive(proxy) else None,
    }
