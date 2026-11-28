from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Публичные ключи Telegram Desktop. Для постоянной работы лучше получить
# собственные на https://my.telegram.org и указать в .env.
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    items: list[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        value = chunk.strip()
        if value and value not in items:
            items.append(value)
    return items


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _parse_mt_proxy(raw: str) -> tuple[str, int, str] | None:
    """Разбирает tg://proxy?server=...&port=...&secret=... (или t.me/proxy)."""
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value and not value.startswith("tg:"):
        value = "tg://" + value
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    query = parse_qs(parts.query)
    server = (query.get("server") or [""])[0].strip()
    port_raw = (query.get("port") or [""])[0].strip()
    secret = (query.get("secret") or [""])[0].strip()
    if not server or not port_raw or not secret:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        return None
    return server, port, secret


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    telegram_proxy_url: str | None
    mt_proxy_server: str | None
    mt_proxy_port: int | None
    mt_proxy_secret: str | None
    check_interval: int
    initial_pages: int
    check_pages: int
    request_delay: float
    max_new_per_check: int
    fetch_details: bool
    proxy_url: str | None
    avito_proxy_urls: tuple[str, ...]
    avito_browser_proxy: str | None
    avito_rotate_on_block: bool
    avito_block_pause_minutes: int
    avito_auto_proxy: bool
    avito_proxy_cache: Path
    avito_proxy_scan_limit: int
    avito_proxy_scan_workers: int
    avito_warp_hop: bool
    avito_warp_primary: bool
    avito_warp_proxy: str
    avito_warp_max_hops: int
    avito_warp_engine: str
    avito_usque_path: str | None
    avito_use_browser: bool
    avito_browser_headless: bool
    avito_browser_profile: Path
    avito_all_russia: bool
    avito_state_path: Path
    avito_reload_seconds: float
    avito_fresh_hours: float
    avito_cookie: str | None
    avito_timeout: float
    avito_fallback_direct: bool
    avito_location_id: str | None
    db_path: Path
    session_path: Path
    log_level: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен от @BotFather."
        )

    proxy_server = os.getenv("TELEGRAM_MT_PROXY_SERVER", "").strip() or None
    proxy_port_raw = os.getenv("TELEGRAM_MT_PROXY_PORT", "").strip()
    proxy_secret = os.getenv("TELEGRAM_MT_PROXY_SECRET", "").strip() or None
    proxy_port: int | None = None
    if proxy_server and proxy_port_raw:
        try:
            proxy_port = int(proxy_port_raw)
        except ValueError:
            proxy_port = None

    # Ссылку tg://proxy?... можно указать одной строкой — она важнее полей.
    mt_link = (
        os.getenv("TELEGRAM_MT_PROXY", "").strip()
        or os.getenv("TELEGRAM_MT_PROXY_URL", "").strip()
    )
    parsed_proxy = _parse_mt_proxy(mt_link)
    if parsed_proxy:
        proxy_server, proxy_port, proxy_secret = parsed_proxy

    avito_proxy_urls = _env_list("AVITO_PROXY_URLS")
    single_avito_proxy = os.getenv("AVITO_PROXY_URL", "").strip()
    if single_avito_proxy and single_avito_proxy not in avito_proxy_urls:
        avito_proxy_urls.insert(0, single_avito_proxy)

    # Список прокси из файла (по одному на строку, # — комментарий).
    proxy_file = os.getenv("AVITO_PROXY_FILE", "").strip()
    if proxy_file:
        proxy_path = Path(proxy_file)
        if not proxy_path.is_absolute():
            proxy_path = BASE_DIR / proxy_path
        try:
            for line in proxy_path.read_text(encoding="utf-8-sig").splitlines():
                value = line.strip()
                if value and not value.startswith("#") and value not in avito_proxy_urls:
                    avito_proxy_urls.append(value)
        except OSError:
            pass

    return Config(
        bot_token=token,
        api_id=_env_int("TELEGRAM_API_ID", DEFAULT_API_ID),
        api_hash=os.getenv("TELEGRAM_API_HASH", "").strip() or DEFAULT_API_HASH,
        telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
        mt_proxy_server=proxy_server,
        mt_proxy_port=proxy_port,
        mt_proxy_secret=proxy_secret,
        check_interval=_env_int("CHECK_INTERVAL_SECONDS", 60, 1),
        initial_pages=_env_int("INITIAL_PAGES", 2, 1),
        check_pages=_env_int("CHECK_PAGES", 1, 1),
        request_delay=_env_float("REQUEST_DELAY_SECONDS", 2.0, 0.5),
        max_new_per_check=_env_int("MAX_NEW_PER_CHECK", 20, 1),
        fetch_details=_env_bool("FETCH_DETAILS", True),
        proxy_url=single_avito_proxy or None,
        avito_proxy_urls=tuple(avito_proxy_urls),
        avito_browser_proxy=os.getenv("AVITO_BROWSER_PROXY", "").strip() or None,
        avito_rotate_on_block=_env_bool("AVITO_ROTATE_ON_BLOCK", True),
        avito_block_pause_minutes=_env_int(
            "AVITO_BLOCK_PAUSE_MINUTES", 10, 1
        ),
        avito_auto_proxy=_env_bool("AVITO_AUTO_PROXY", True),
        avito_proxy_cache=_path_from_env(
            "AVITO_PROXY_CACHE", BASE_DIR / "data" / "proxies_working.txt"
        ),
        avito_proxy_scan_limit=_env_int("AVITO_PROXY_SCAN_LIMIT", 450, 50),
        avito_proxy_scan_workers=_env_int("AVITO_PROXY_SCAN_WORKERS", 100, 10),
        avito_warp_hop=_env_bool("AVITO_WARP_HOP", True),
        avito_warp_primary=_env_bool("AVITO_WARP_PRIMARY", True),
        avito_warp_proxy=os.getenv(
            "AVITO_WARP_PROXY", "socks5://127.0.0.1:40000"
        ).strip()
        or "socks5://127.0.0.1:40000",
        avito_warp_max_hops=_env_int("AVITO_WARP_MAX_HOPS", 4, 1),
        avito_warp_engine=(
            os.getenv("AVITO_WARP_ENGINE", "auto").strip().lower() or "auto"
        ),
        avito_usque_path=os.getenv("AVITO_USQUE_PATH", "").strip() or None,
        avito_use_browser=_env_bool("AVITO_USE_BROWSER", True),
        avito_browser_headless=_env_bool("AVITO_BROWSER_HEADLESS", False),
        avito_browser_profile=_path_from_env(
            "AVITO_BROWSER_PROFILE", BASE_DIR / "data" / "browser_profile"
        ),
        avito_all_russia=_env_bool("AVITO_ALL_RUSSIA", True),
        avito_state_path=_path_from_env(
            "AVITO_STATE_PATH", BASE_DIR / "data" / "avito_state.json"
        ),
        avito_reload_seconds=_env_float("AVITO_RELOAD_SECONDS", 20.0, 8.0),
        avito_fresh_hours=_env_float("AVITO_FRESH_HOURS", 24.0, 0.5),
        avito_cookie=os.getenv("AVITO_COOKIE", "").strip() or None,
        avito_timeout=_env_float("AVITO_TIMEOUT_SECONDS", 45.0, 20.0),
        avito_fallback_direct=_env_bool("AVITO_PROXY_FALLBACK_DIRECT", True),
        avito_location_id=os.getenv("AVITO_LOCATION_ID", "").strip() or None,
        db_path=_path_from_env("DB_PATH", BASE_DIR / "data" / "bot.db"),
        session_path=_path_from_env("SESSION_PATH", BASE_DIR / "data" / "bot"),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
