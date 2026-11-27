"""Автоматический поиск и проверка прокси для обхода блокировки Avito.

Идея: если Авито ограничил IP, бот сам скачивает свежие списки бесплатных
прокси, параллельно проверяет их на реальном запросе к Avito и оставляет
только те, которые действительно отдают выдачу. Найденные рабочие прокси
кэшируются в data/proxies_working.txt, чтобы использовать их повторно
без нового сканирования.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from curl_cffi import requests

CHECK_URL = (
    "https://www.avito.ru/web/1/main/items?q=iphone&page=1&limit=5"
)
CHECK_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.avito.ru/",
}

HTTP_LISTS = (
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/"
    "protocols/http/data.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&protocol=http&proxy_format=ipport&format=text",
)
SOCKS_LISTS = (
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/"
    "socks5.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/"
    "protocols/socks5/data.txt",
)
GEONODE_URL = (
    "https://proxylist.geonode.com/api/proxy-list?limit=300&page=1"
    "&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps"
)

_IP_PORT = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}")
_DISCOVERY_LOCK = threading.Lock()


def _normalize(value: str, default_scheme: str) -> str | None:
    value = (value or "").strip()
    if not value or value.startswith("#"):
        return None
    if value.startswith(("http://", "https://", "socks5://", "socks4://")):
        return value
    if _IP_PORT.fullmatch(value):
        return f"{default_scheme}://{value}"
    return None


def _parse_list(text: str, default_scheme: str) -> list[str]:
    result: list[str] = []
    for line in (text or "").splitlines():
        candidate = _normalize(line, default_scheme)
        if candidate:
            result.append(candidate)
    return result


def collect_candidates(limit: int = 450) -> list[str]:
    """Скачивает публичные списки прокси (HTTP и SOCKS5)."""
    found: dict[str, None] = {}

    def add(values: Iterable[str]) -> None:
        for value in values:
            found.setdefault(value, None)

    for url in HTTP_LISTS:
        try:
            response = requests.get(url, timeout=25, impersonate="chrome")
            add(_parse_list(response.text, "http"))
        except Exception:
            continue
    for url in SOCKS_LISTS:
        try:
            response = requests.get(url, timeout=25, impersonate="chrome")
            add(_parse_list(response.text, "socks5"))
        except Exception:
            continue
    try:
        response = requests.get(GEONODE_URL, timeout=25, impersonate="chrome")
        payload = json.loads(response.text or "{}")
        for item in payload.get("data") or []:
            protocols = item.get("protocols") or ["http"]
            for protocol in protocols:
                if protocol not in {"http", "https", "socks4", "socks5"}:
                    continue
                found.setdefault(
                    f"{protocol}://{item['ip']}:{item['port']}", None
                )
    except Exception:
        pass
    return list(found)[: max(1, limit)]


def validate_proxy(proxy: str, timeout: float = 12.0) -> bool:
    """Проверяет, что через прокси Авито отдаёт реальную выдачу."""
    try:
        response = requests.get(
            CHECK_URL,
            impersonate="chrome",
            timeout=timeout,
            headers=CHECK_HEADERS,
            proxies={"http": proxy, "https": proxy},
        )
    except Exception:
        return False
    if response.status_code != 200:
        return False
    text = response.text or ""
    return '"items"' in text and '"count"' in text


def discover(
    limit: int = 450,
    workers: int = 100,
    max_working: int = 12,
    on_found: Callable[[str], None] | None = None,
) -> list[str]:
    """Возвращает рабочие прокси, проверяя кандидатов параллельно."""
    candidates = collect_candidates(limit=limit)
    working: list[str] = []
    if not candidates:
        return working
    checked = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(validate_proxy, proxy): proxy for proxy in candidates}
        for future in cf.as_completed(futures):
            checked += 1
            proxy = futures[future]
            try:
                ok = future.result()
            except Exception:
                ok = False
            if ok and proxy not in working:
                working.append(proxy)
                if on_found is not None:
                    try:
                        on_found(proxy)
                    except Exception:
                        pass
            if len(working) >= max_working:
                for pending in futures:
                    pending.cancel()
                break
            if checked >= len(candidates):
                break
    return working


def load_cached(path: str | Path) -> list[str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def save_cached(path: str | Path, proxies: Iterable[str]) -> None:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(proxies) + "\n", encoding="utf-8")
    except Exception:
        pass


class ProxyDiscovery:
    """Фоновый поиск прокси: не блокирует цикл мониторинга."""

    def __init__(
        self,
        *,
        cache_path: str | Path,
        limit: int = 450,
        workers: int = 100,
        cooldown: float = 900.0,
        max_working: int = 12,
    ) -> None:
        self._cache_path = Path(cache_path)
        self._limit = max(50, int(limit))
        self._workers = max(10, int(workers))
        self._cooldown = max(60.0, float(cooldown))
        self._max_working = max(1, int(max_working))
        self._last_run = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def ready(self) -> bool:
        return (
            not self.running
            and (time.time() - self._last_run) >= self._cooldown
        )

    def start(
        self,
        on_found: Callable[[str], None] | None = None,
        on_done: Callable[[list[str]], None] | None = None,
    ) -> bool:
        """Запускает поиск в фоне. Возвращает False, если уже идёт/рано."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if (time.time() - self._last_run) < self._cooldown:
                return False
            self._last_run = time.time()

            def run() -> None:
                cached = [
                    proxy
                    for proxy in load_cached(self._cache_path)
                    if validate_proxy(proxy)
                ]
                if cached:
                    save_cached(self._cache_path, cached)
                    if on_done is not None:
                        on_done(cached)
                    return
                found = discover(
                    limit=self._limit,
                    workers=self._workers,
                    max_working=self._max_working,
                    on_found=on_found,
                )
                if found:
                    save_cached(self._cache_path, found)
                if on_done is not None:
                    on_done(found)

            thread = threading.Thread(
                target=run,
                name="avito-proxy-discovery",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True
