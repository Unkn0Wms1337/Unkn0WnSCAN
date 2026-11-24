from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import html
import json
import logging
import random
import re
import statistics
import time
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession

try:  # Playwright опционален: без него остаётся режим curl-cffi.
    from avito_browser import AvitoBrowser, BrowserFetchError
except Exception:  # pragma: no cover
    AvitoBrowser = None  # type: ignore[assignment]
    BrowserFetchError = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

AVITO_HOSTS = ("avito.ru", "www.avito.ru", "m.avito.ru")
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL)
ITEM_ID_RE = re.compile(r"[-_](\d{6,})(?:/)?$")
INITIAL_DATA_RE = re.compile(
    r"(?:window\.)?__(?:initialData|PRELOADED_STATE|INITIAL_STATE)__\s*=\s*",
    re.DOTALL,
)
TRACKING_QUERY_PARAMS = {"context", "p"}
PRICE_DIGITS_RE = re.compile(r"\d+")
ITEM_DATE_RE = re.compile(
    r'data-marker="item-view/item-date"[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\u00a0]+")

FIREWALL_MARKERS = (
    "firewall",
    "доступ ограничен",
    "проблем с ip",
    "проблема с ip",
    "очень много запросов",
    "страница не загрузилась",
    "пройти проверку",
    "подтвердите, что вы не робот",
    "проверка браузера",
    "слишком много запросов",
    "access denied",
)


class AvitoError(Exception):
    """Base error for Avito requests/parsing."""


class InvalidAvitoUrl(AvitoError):
    """The user supplied something that is not a public Avito search URL."""


class NotSearchUrl(InvalidAvitoUrl):
    """An item page was supplied instead of a search results page."""


class AvitoBlockedError(AvitoError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AvitoParseError(AvitoError):
    pass


@dataclass
class Listing:
    id: str
    title: str
    url: str
    price: int | None = None
    price_text: str = "Цена не указана"
    description: str = ""
    image_urls: list[str] = field(default_factory=list)
    seller_name: str | None = None
    seller_id: str | None = None
    seller_is_private: bool | None = None
    seller_rating: float | None = None
    seller_reviews: int | None = None
    seller_completed: int | None = None
    location: str | None = None
    published_ts: int | None = None
    published_precise: bool = False

    @property
    def has_price(self) -> bool:
        return self.price is not None and self.price > 0


def seller_token(seller_id: str | None) -> str | None:
    """Короткий стабильный ключ продавца для callback_data (лимит 64 байта).

    Telegram не даёт положить в inline-кнопку длинный seller_id
    (`shop:...`, `brand:...`, `name:...`), поэтому блокируем по хэшу.
    """
    if not seller_id:
        return None
    return hashlib.sha1(seller_id.encode("utf-8")).hexdigest()[:16]


@dataclass
class SearchPage:
    url: str
    items: list[Listing]


def is_avito_url(raw: str) -> bool:
    try:
        host = (urlsplit(raw.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in AVITO_HOSTS


def extract_first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>\"']+", text or "", re.IGNORECASE)
    return match.group(0).rstrip(").,;!?") if match else None


def normalize_avito_url(raw: str) -> str:
    """Validate and normalize an Avito search URL without tracking garbage."""
    value = html.unescape((raw or "").strip())
    if not value:
        raise InvalidAvitoUrl("Пустая ссылка.")
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise InvalidAvitoUrl("Не получилось разобрать ссылку.") from exc

    host = (parts.hostname or "").lower()
    if host not in AVITO_HOSTS:
        raise InvalidAvitoUrl("Нужна ссылка именно на сайт avito.ru.")

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        last_segment = path.rsplit("/", 1)[-1]
        if ITEM_ID_RE.search(last_segment):
            raise NotSearchUrl(
                "Это ссылка на одно объявление, а не на результаты поиска. "
                "Откройте поиск Авито с фильтрами и пришлите ссылку из адресной строки."
            )

    query: list[tuple[str, str]] = []
    for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in TRACKING_QUERY_PARAMS:
            continue
        query.append((key, item_value))
    query.sort(key=lambda pair: (pair[0], pair[1]))

    normalized = urlunsplit(
        (
            "https",
            "www.avito.ru",
            path,
            urlencode(query, doseq=True),
            "",
        )
    )
    if len(normalized) > 3500:
        raise InvalidAvitoUrl("Ссылка слишком длинная. Уберите лишние параметры из адресной строки.")
    return normalized


def to_all_russia(url: str) -> str:
    """Rewrite a city search URL to the all-Russia one (avito.ru/all/...)."""
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and segments[0].casefold() in {"all", "rossiya"}:
        new_segments = list(segments)
        new_segments[0] = "all"
    elif len(segments) >= 2:
        new_segments = ["all"] + segments[1:]
    else:
        new_segments = ["all"]
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"locationid", "location", "latitude", "longitude"}
    ]
    return urlunsplit(
        ("https", "www.avito.ru", "/" + "/".join(new_segments), urlencode(query), "")
    )


def build_search_url(query: str) -> str:
    """Build an all-Russia Avito search URL from a plain text query."""
    text = (query or "").strip()
    if not text:
        raise InvalidAvitoUrl("Пустой поисковый запрос.")
    # Сортировка по дате, чтобы первыми шли самые свежие объявления.
    return urlunsplit(
        ("https", "www.avito.ru", "/all", urlencode({"q": text, "s": "104"}), "")
    )


def search_title(url: str) -> str:
    """Short human-readable name for a subscription."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    city = ""
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments:
        city = unquote(segments[0]).replace("_", " ").replace("-", " ").strip().capitalize()
        if city.casefold() in {"all", "rossiya"}:
            city = "Все регионы"

    query_text = (query.get("q") or query.get("query") or "").strip()
    if query_text:
        name = f"{city} · {query_text}" if city else query_text
    elif segments:
        name = unquote(segments[-1]).replace("_", " ").replace("-", " ").strip().capitalize()
        if city and city.casefold() not in name.casefold():
            name = f"{city} · {name}"
    else:
        name = parts.netloc
    return name[:100]


def build_page_url(url: str, page: int) -> str:
    parts = urlsplit(url)
    query: list[tuple[str, str]] = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"p"}
    ]
    # 104 = «по дате», newest first. Otherwise a pinned old ad can hide fresh ones.
    query = [(key, value) for key, value in query if key.lower() != "s"]
    query.append(("s", "104"))
    if page > 1:
        query.append(("p", str(page)))
    return urlunsplit(
        (
            "https",
            "www.avito.ru",
            parts.path or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


def parse_price_text(text: str | None) -> int | None:
    if not text:
        return None
    digits = "".join(PRICE_DIGITS_RE.findall(text))
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if value > 0 else None


def format_price(value: int | None) -> str:
    if value is None or value <= 0:
        return "Цена не указана"
    return f"{value:,}".replace(",", " ") + " ₽"


def robust_market_price(prices: Iterable[int | None]) -> int | None:
    """Median price, trimmed at 10/90 percentiles to ignore parts and scam ads."""
    clean = sorted(int(price) for price in prices if price and price > 0)
    if len(clean) < 3:
        return None
    if len(clean) >= 8:
        low = int(len(clean) * 0.10)
        high = max(low + 1, int(len(clean) * 0.90))
        if high - low >= 5:
            clean = clean[low:high]
    median = int(round(statistics.median(clean)))
    return median if median > 0 else None


def _strip_html(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = html.unescape(TAG_RE.sub(" ", value))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(SPACE_RE.sub(" ", line).strip() for line in value.split("\n"))
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _first_string(mapping: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = "".join(PRICE_DIGITS_RE.findall(value))
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def _parse_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:[.,]\d+)?", value)
        if match:
            try:
                return float(match.group(0).replace(",", "."))
            except ValueError:
                return None
    return None


_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def _parse_publication(
    value: Any, now_ts: float | None = None
) -> tuple[int | None, bool]:
    """Разбирает время публикации Авито.

    Возвращает (timestamp, точное_время). Точное время бывает в карточке
    объявления («17 ноября в 16:28», «1 час назад»). В выдаче обычно есть
    только дата (JSON-LD `validFrom`, «2026-11-17») — тогда precise=False.
    """
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        stamp = _parse_timestamp(value)
        return (stamp, True) if stamp else (None, False)

    text = str(value).strip().lower().replace("ё", "е").lstrip("·,;: ").strip()
    if not text:
        return None, False

    now = int(now_ts if now_ts is not None else time.time())
    local = datetime.fromtimestamp(now)

    iso_date = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_date:
        try:
            dt = datetime(int(iso_date[1]), int(iso_date[2]), int(iso_date[3]))
        except ValueError:
            return None, False
        return int(dt.timestamp()), False

    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?(z|[+-]\d{2}:?\d{2})?",
        text,
    ):
        stamp = _parse_timestamp(text)
        return (stamp, True) if stamp else (None, False)

    clock = re.search(r"(\d{1,2}):(\d{2})", text)
    hour, minute = (int(clock[1]), int(clock[2])) if clock else (0, 0)

    if "сегодня" in text:
        dt = datetime(local.year, local.month, local.day, hour, minute)
        return int(dt.timestamp()), bool(clock)
    if "вчера" in text:
        base = local - timedelta(days=1)
        dt = datetime(base.year, base.month, base.day, hour, minute)
        return int(dt.timestamp()), bool(clock)

    relative = _parse_relative_date(text, now)
    if relative:
        return relative, True

    day_match = re.search(r"(\d{1,2})\s+([а-я]+)", text)
    if day_match:
        day = int(day_match[1])
        month = _MONTHS.get(day_match[2])
        if month:
            try:
                candidate = datetime(local.year, month, day, hour, minute)
            except ValueError:
                return None, False
            if candidate > local + timedelta(days=1):
                try:
                    candidate = datetime(local.year - 1, month, day, hour, minute)
                except ValueError:
                    return None, False
            return int(candidate.timestamp()), bool(clock)

    return None, False


def _format_debug_ts(ts: int | None) -> str:
    if not ts:
        return "нет"
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def _parse_relative_date(value: Any, now_ts: float | None = None) -> int | None:
    """Разбирает человекочитаемую дату Авито: «2 минуты назад», «вчера»."""
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace("ё", "е")
    if not text:
        return None
    now = int(now_ts if now_ts is not None else time.time())
    if "только что" in text or "сейчас" in text:
        return now
    match = re.search(r"(\d+)\s*(сек|мин|час|дн|день|дня|дней|недел|месяц)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("сек"):
            return now - amount
        if unit.startswith("мин"):
            return now - amount * 60
        if unit.startswith("час"):
            return now - amount * 3600
        if unit.startswith("дн"):
            return now - amount * 86400
        if unit.startswith("недел"):
            return now - amount * 7 * 86400
        if unit.startswith("месяц"):
            return now - amount * 30 * 86400
    if "вчера" in text:
        return now - 86400
    if "сегодня" in text:
        return now - 60
    return None


def _parse_timestamp(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        now = int(time.time())
        if 946684800 <= stamp <= now + 86400:
            return stamp
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return _parse_timestamp(int(stripped))
        try:
            dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        stamp = int(dt.timestamp())
        now = int(time.time())
        if 946684800 <= stamp <= now + 86400:
            return stamp
    return None


def _price_from_node(node: dict[str, Any]) -> tuple[int | None, str]:
    value: Any = None
    text: str | None = None

    for key in ("priceDetailed", "price", "priceValue"):
        raw = node.get(key)
        if isinstance(raw, dict):
            text = _first_string(raw, ("string", "title", "formatted", "text"))
            for sub_key in ("value", "price", "amount", "numeric"):
                if sub_key in raw:
                    value = raw.get(sub_key)
                    break
            if value is None and text:
                value = parse_price_text(text)
        elif isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
            value = raw
            if isinstance(raw, str):
                text = raw
        if value is not None or text:
            break

    if value is None:
        for key in ("priceFormatted", "priceString", "priceText"):
            raw = node.get(key)
            if isinstance(raw, str) and raw.strip():
                text = raw.strip()
                value = parse_price_text(text)
                break

    parsed = _parse_int(value)
    if parsed is None and text:
        parsed = parse_price_text(text)
    if text:
        text = _strip_html(text)
    return parsed, text or format_price(parsed)


def _extract_url(node: dict[str, Any]) -> str | None:
    raw = _first_string(node, ("urlPath", "url", "link", "href"))
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    absolute = urljoin("https://www.avito.ru/", raw)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").lower()
    if host not in AVITO_HOSTS:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_PARAMS
    ]
    return urlunsplit(("https", "www.avito.ru", parts.path, urlencode(query, doseq=True), ""))


def _id_from_url(url: str) -> str | None:
    path = urlsplit(url).path.rstrip("/")
    match = re.search(r"[-_](\d{6,})", path.rsplit("/", 1)[-1])
    return match.group(1) if match else None


def _url_rank(url: str) -> int:
    match = re.search(r"(\d{2,4})x(\d{2,4})", url)
    if match:
        return int(match.group(1)) * int(match.group(2))
    return 0


def _best_image_url(values: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for key, value in values.items():
        if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
            dimension_match = re.search(r"(\d{2,4})x(\d{2,4})", str(key))
            score = (
                int(dimension_match.group(1)) * int(dimension_match.group(2))
                if dimension_match
                else _url_rank(value)
            )
            candidates.append((score, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _image_urls_from(value: Any, limit: int = 30) -> list[str]:
    result: list[str] = []

    def add(url: str) -> None:
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("http://", "https://")):
            return
        if url not in result and len(result) < limit:
            result.append(url)

    def walk(node: Any) -> None:
        if len(result) >= limit:
            return
        if isinstance(node, str):
            if node.startswith(("http://", "https://", "//")):
                add(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return

        urls = node.get("urls")
        if isinstance(urls, dict):
            best = _best_image_url(urls)
            if best:
                add(best)
            return
        sizes = node.get("sizes")
        if isinstance(sizes, (dict, list)):
            walk(sizes)
            return

        for key in (
            "url",
            "src",
            "imageUrl",
            "image",
            "large",
            "original",
            "preview",
            "640x480",
            "1280x960",
        ):
            candidate = node.get(key)
            if isinstance(candidate, str) and candidate.startswith(
                ("http://", "https://", "//")
            ):
                add(candidate)
        for key, candidate in node.items():
            if key in {"url", "src", "imageUrl", "image"}:
                continue
            if isinstance(candidate, (dict, list)) and key not in {"avatar", "seller"}:
                walk(candidate)

    walk(value)
    return result


def _seller_from_node(node: dict[str, Any]) -> tuple[str | None, bool | None, float | None, int | None]:
    name = _first_string(node, ("name", "title", "sellerName", "displayName"))
    private: bool | None = None

    for key in ("isPrivate", "isPrivatePerson", "private", "isIndividual"):
        if isinstance(node.get(key), bool):
            private = bool(node[key])
            break
    if private is None:
        for key in ("sellerType", "type", "kind"):
            raw = node.get(key)
            if isinstance(raw, str):
                value = raw.strip().lower()
                if value in {"private", "person", "individual", "user"}:
                    private = True
                    break
                if value in {"company", "shop", "business", "brand", "store"}:
                    private = False
                    break
        if private is None:
            company = node.get("company")
            if isinstance(company, dict) and company.get("name"):
                name = name or _first_string(company, ("name", "title"))
                private = False

    rating_node: Any = node.get("rating")
    score: float | None = None
    reviews: int | None = None
    if isinstance(rating_node, dict):
        score = _parse_float(
            _first_string(rating_node, ("score", "value", "rating", "average", "ratingValue"))
        )
        reviews = _parse_int(
            _first_string(
                rating_node,
                ("reviewsCount", "reviewCount", "count", "reviews", "ratingCount", "total"),
            )
        )
    elif isinstance(rating_node, (int, float, str)) and not isinstance(rating_node, bool):
        score = _parse_float(rating_node)

    if score is None:
        score = _parse_float(
            _first_string(node, ("ratingValue", "sellerRating", "score", "rating"))
        )
    if reviews is None:
        reviews = _parse_int(
            _first_string(
                node,
                ("reviewsCount", "reviewCount", "sellerReviewsCount", "ratingCount", "reviews"),
            )
        )

    if score is not None and not (0 <= score <= 5):
        score = None
    if reviews is not None and reviews < 0:
        reviews = None
    return name, private, score, reviews


def extract_seller(item: dict[str, Any]) -> tuple[str | None, bool | None, float | None, int | None]:
    for key in ("sellerInfo", "seller", "sellerData", "advertiser", "owner"):
        node = item.get(key)
        if isinstance(node, dict):
            result = _seller_from_node(node)
            if any(value is not None for value in result):
                return result
        elif isinstance(node, str) and node.strip():
            name = node.strip()
            private = None
            if "частное лицо" in name.casefold():
                private = True
            elif "магазин" in name.casefold():
                private = False
            return name, private, None, None

    flat = {
        "name": item.get("sellerName") or item.get("seller_name"),
        "isPrivate": item.get("sellerIsPrivate"),
        "ratingValue": item.get("sellerRating"),
        "reviewsCount": item.get("sellerReviewsCount") or item.get("sellerRatingCount"),
    }
    if any(value is not None for value in flat.values()):
        return _seller_from_node({key: value for key, value in flat.items() if value is not None})
    return None, None, None, None


def _collect_dicts(node: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 40:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _collect_dicts(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _collect_dicts(value, depth + 1)


def _looks_like_listing(node: dict[str, Any]) -> bool:
    if not isinstance(node.get("id"), (int, str)):
        return False
    if not isinstance(node.get("title"), str) or not node["title"].strip():
        return False
    if not _extract_url(node):
        return False
    has_price = any(
        key in node for key in ("priceDetailed", "price", "priceValue", "priceFormatted")
    )
    return has_price


def _listing_from_dict(node: dict[str, Any]) -> Listing | None:
    url = _extract_url(node)
    if not url:
        return None

    raw_id = node.get("id") or node.get("itemId") or node.get("item_id")
    item_id = str(raw_id) if isinstance(raw_id, (int, str)) else None
    if not item_id:
        item_id = _id_from_url(url)
    if not item_id:
        return None

    title = _strip_html(_first_string(node, ("title", "name")) or "")
    if not title:
        return None

    price, price_text = _price_from_node(node)

    description = ""
    for key in ("description", "descriptionText", "descriptionHtml", "body"):
        raw_description = node.get(key)
        if isinstance(raw_description, str) and raw_description.strip():
            description = _strip_html(raw_description)
            if description:
                break

    images: list[str] = []
    for key in ("images", "photos", "imageUrls", "gallery", "image"):
        if key in node:
            images = _image_urls_from(node.get(key))
            if images:
                break

    seller_name, seller_private, seller_rating, seller_reviews = extract_seller(node)

    location = None
    location_node = node.get("location")
    if isinstance(location_node, dict):
        location = _first_string(location_node, ("name", "title", "city"))
    elif isinstance(location_node, str):
        location = location_node.strip() or None

    published_ts = None
    for key in ("sortTimeStamp", "publishedAt", "time", "date", "createdAt"):
        if key in node:
            published_ts = _parse_timestamp(node.get(key))
            if published_ts:
                break

    return Listing(
        id=item_id,
        title=title,
        url=url,
        price=price,
        price_text=price_text,
        description=description,
        image_urls=images,
        seller_name=seller_name,
        seller_is_private=seller_private,
        seller_rating=seller_rating,
        seller_reviews=seller_reviews,
        location=location,
        published_ts=published_ts,
        published_precise=bool(published_ts),
    )


def _merge_listings(first: Listing, second: Listing) -> Listing:
    images = list(dict.fromkeys(first.image_urls + second.image_urls))
    return replace(
        first,
        price=first.price if first.price is not None else second.price,
        price_text=(
            first.price_text
            if first.price is not None
            else (second.price_text or first.price_text)
        ),
        description=first.description or second.description,
        image_urls=images,
        seller_name=first.seller_name or second.seller_name,
        seller_id=first.seller_id or second.seller_id,
        seller_is_private=(
            first.seller_is_private
            if first.seller_is_private is not None
            else second.seller_is_private
        ),
        seller_rating=(
            first.seller_rating if first.seller_rating is not None else second.seller_rating
        ),
        seller_reviews=(
            first.seller_reviews if first.seller_reviews is not None else second.seller_reviews
        ),
        seller_completed=(
            first.seller_completed
            if first.seller_completed is not None
            else second.seller_completed
        ),
        location=first.location or second.location,
        published_ts=first.published_ts or second.published_ts,
        published_precise=(
            first.published_precise
            if first.published_ts
            else second.published_precise
        ),
    )


def _listings_from_blobs(blobs: Iterable[Any]) -> list[Listing]:
    by_id: dict[str, Listing] = {}
    for blob in blobs:
        for node in _collect_dicts(blob):
            if not _looks_like_listing(node):
                continue
            listing = _listing_from_dict(node)
            if not listing:
                continue
            existing = by_id.get(listing.id)
            by_id[listing.id] = _merge_listings(existing, listing) if existing else listing
    return list(by_id.values())


def _decode_state_string(raw: str) -> list[Any]:
    variants = [raw]
    for _ in range(2):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(decoded)

    result: list[Any] = []
    for variant in variants:
        stripped = variant.strip()
        if not stripped:
            continue
        payloads = [stripped]
        if stripped.startswith("H4sI") or stripped.startswith("eJ"):
            try:
                compressed = base64.b64decode(stripped + "=" * (-len(stripped) % 4))
                for decompressor in (gzip.decompress, zlib.decompress):
                    try:
                        payloads.append(decompressor(compressed).decode("utf-8", "replace"))
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        for payload in payloads:
            try:
                parsed = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            result.append(parsed)
            if isinstance(parsed, str):
                try:
                    result.append(json.loads(unquote(parsed)))
                except (json.JSONDecodeError, TypeError):
                    pass
    return result


def _balanced_json(text: str, start: int) -> str | None:
    opening = text[start : start + 1]
    if opening not in {"{", "["}:
        return None
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(text), start + 20_000_000)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _collect_initial_data(text: str, add_blob) -> None:
    for match in INITIAL_DATA_RE.finditer(text):
        tail = text[match.end() :]
        rest_stripped = tail.lstrip()
        if rest_stripped.startswith("JSON.parse("):
            rest_stripped = rest_stripped[len("JSON.parse(") :].lstrip()
        if rest_stripped.startswith('"'):
            string_match = re.match(r'"((?:\\.|[^"\\])*)"', rest_stripped, re.DOTALL)
            if string_match:
                literal = '"' + string_match.group(1) + '"'
                try:
                    raw_value = json.loads(literal)
                except (json.JSONDecodeError, TypeError):
                    raw_value = string_match.group(1)
                for decoded in _decode_state_string(raw_value):
                    add_blob(decoded)
        elif rest_stripped.startswith(("{", "[")):
            start = text.find(rest_stripped[0], match.end())
            if start != -1:
                raw_object = _balanced_json(text, start)
                if raw_object:
                    try:
                        add_blob(json.loads(raw_object))
                    except (json.JSONDecodeError, TypeError):
                        pass


def extract_json_blobs(html_text: str) -> list[Any]:
    """Pull JSON state out of a modern Avito HTML page.

    Script bodies are parsed as-is first: unescaping the whole document before
    JSON parsing would turn &quot; inside JSON strings into invalid quotes.
    """
    blobs: list[Any] = []

    def add_blob(value: Any) -> None:
        if isinstance(value, (dict, list)):
            blobs.append(value)
            if len(blobs) >= 120:
                raise _BlobLimitReached

    try:
        for match in SCRIPT_RE.finditer(html_text):
            attrs = match.group(1).lower()
            body = match.group(2).strip()
            if not body:
                continue
            if "ld+json" in attrs or body.startswith(("{", "[")):
                try:
                    add_blob(json.loads(body))
                except (json.JSONDecodeError, TypeError):
                    try:
                        add_blob(json.loads(html.unescape(body)))
                    except (json.JSONDecodeError, TypeError):
                        continue

        _collect_initial_data(html_text, add_blob)
        if not blobs:
            _collect_initial_data(html.unescape(html_text), add_blob)
    except _BlobLimitReached:
        pass

    return blobs


class _BlobLimitReached(Exception):
    pass


def detect_firewall(html_text: str) -> bool:
    lowered = html_text.casefold()
    return any(marker in lowered for marker in FIREWALL_MARKERS)


def parse_search_html(html_text: str) -> list[Listing]:
    if detect_firewall(html_text):
        raise AvitoBlockedError("Avito ограничил доступ к странице (firewall).")

    blobs = extract_json_blobs(html_text)
    listings = _listings_from_blobs(blobs)
    if listings:
        return listings

    lowered = html_text.casefold()
    empty_markers = (
        "ничего не найдено",
        "объявлений не найдено",
        "нет объявлений",
        "по вашему запросу ничего",
        "0 объявлений",
    )
    if any(marker in lowered for marker in empty_markers):
        return []
    if len(lowered) < 200_000 and (
        "captcha" in lowered or "проверка браузера" in lowered
    ):
        raise AvitoBlockedError("Avito показал капчу вместо результатов поиска.")
    raise AvitoParseError("Не удалось разобрать страницу Avito: данные объявлений не найдены.")


def _meta_content(html_text: str, property_name: str) -> str | None:
    pattern = re.compile(
        r"<meta\b[^>]*(?:property|name)=[\"']"
        + re.escape(property_name)
        + r"[\"'][^>]*content=[\"']([^\"']*)[\"']",
        re.IGNORECASE,
    )
    match = pattern.search(html_text)
    if not match:
        pattern = re.compile(
            r"<meta\b[^>]*content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']"
            + re.escape(property_name)
            + r"[\"']",
            re.IGNORECASE,
        )
        match = pattern.search(html_text)
    return html.unescape(match.group(1)).strip() if match else None


def _global_seller_from_blobs(blobs: Iterable[Any]) -> tuple[str | None, bool | None, float | None, int | None] | None:
    found: dict[str, tuple[str | None, bool | None, float | None, int | None]] = {}
    for blob in blobs:
        for node in _collect_dicts(blob):
            for key in ("sellerInfo", "seller", "sellerData", "advertiser"):
                value = node.get(key)
                if not isinstance(value, dict):
                    continue
                if not any(
                    marker in value
                    for marker in ("sellerId", "reviewsCount", "rating", "isPrivate", "name")
                ):
                    continue
                result = _seller_from_node(value)
                if not any(item is not None for item in result):
                    continue
                signature = str(
                    value.get("sellerId")
                    or value.get("id")
                    or (result[0], result[2], result[3])
                )
                found[signature] = result
    if len(found) == 1:
        return next(iter(found.values()))
    return None


def parse_listing_html(html_text: str, item_id: str) -> Listing | None:
    if detect_firewall(html_text):
        raise AvitoBlockedError("Avito ограничил доступ к странице (firewall).")

    blobs = extract_json_blobs(html_text)
    listings = _listings_from_blobs(blobs)
    listing = next((item for item in listings if item.id == item_id), None)
    if listing is None and len(listings) == 1:
        listing = listings[0]

    if listing is None:
        title = _meta_content(html_text, "og:title")
        description = _meta_content(html_text, "og:description")
        image = _meta_content(html_text, "og:image")
        price = _meta_content(html_text, "product:price:amount")
        if title:
            clean_title = re.sub(r"\s*[|—]\s*Avito.*$", "", title, flags=re.IGNORECASE).strip()
            listing = Listing(
                id=item_id,
                title=clean_title or title,
                url=f"https://www.avito.ru/{item_id}",
                price=parse_price_text(price),
                price_text=format_price(parse_price_text(price)),
                description=_strip_html(description or ""),
                image_urls=[image] if image else [],
            )
        else:
            return None

    if all(
        value is None
        for value in (
            listing.seller_name,
            listing.seller_rating,
            listing.seller_reviews,
            listing.seller_is_private,
        )
    ):
        seller = _global_seller_from_blobs(blobs)
        if seller:
            listing.seller_name, listing.seller_is_private, listing.seller_rating, listing.seller_reviews = seller
    return listing


def parse_api_items(data: Any) -> list[Listing]:
    """Parse JSON from Avito's /web/1/main/items endpoint."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    results: list[Listing] = []
    for node in items:
        if not isinstance(node, dict):
            continue
        item_id = node.get("id")
        if item_id is None:
            continue
        title = _strip_html(node.get("title") or node.get("imagesAlt") or "")
        url_path = str(node.get("urlPath") or "")
        if not url_path:
            continue
        url = url_path if url_path.startswith("http") else "https://www.avito.ru" + url_path
        price_info = node.get("priceDetailed") or {}
        price_text = _strip_html(price_info.get("string") or "")
        price = parse_price_text(price_text)
        if price is None:
            price = _parse_int(price_info.get("value"))
            if price is not None and price <= 0:
                price = None
        if not price_text:
            price_text = format_price(price)
        images: list[str] = []
        raw_images = node.get("images")
        if isinstance(raw_images, list):
            for image in raw_images:
                best = _best_image_url(image) if isinstance(image, dict) else None
                if best:
                    if best.startswith("//"):
                        best = "https:" + best
                    if best not in images:
                        images.append(best)
                if len(images) >= 10:
                    break
        location_info = node.get("location") or {}
        location = location_info.get("name") if isinstance(location_info, dict) else None
        published = _parse_timestamp(
            node.get("sortTimeStamp")
            or node.get("time")
            or node.get("publishedAt")
            or node.get("date")
        )
        results.append(
            Listing(
                id=str(item_id),
                title=title,
                url=url,
                price=price,
                price_text=price_text,
                image_urls=images,
                location=str(location) if location else None,
                published_ts=published,
            )
        )
    return results


class AvitoClient:
    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        proxy_urls: Sequence[str] | None = None,
        cookie: str | None = None,
        request_delay: float = 2.0,
        timeout: float = 70.0,
        fallback_direct: bool = True,
        location_id: str | None = None,
        use_browser: bool = False,
        browser_headless: bool = False,
        browser_profile: str | None = None,
        all_russia: bool = False,
        state_path: str | None = None,
        watch_reload: float = 20.0,
    ) -> None:
        urls: list[str] = []
        for candidate in list(proxy_urls or []) + ([proxy_url] if proxy_url else []):
            value = (candidate or "").strip()
            if value and value not in urls:
                urls.append(value)
        self._proxy_urls = urls
        self._proxy_url = urls[0] if urls else None
        self._proxy_index = 0
        self._cookie = cookie
        self._location_id = (location_id or "").strip() or None
        self._request_delay = max(0.5, request_delay)
        self._timeout = max(20.0, timeout)
        self._fallback_direct = fallback_direct
        self._all_russia = all_russia
        self._last_request_at = 0.0
        self._blocked_until = 0.0
        self._block_count = 0
        self._last_reload_at = 0.0
        self._watch_reload = max(15.0, float(watch_reload))
        self._force_reload = False
        self._state_path = Path(state_path) if state_path else None
        self._load_state()
        self._sessions: dict[str | None, AsyncSession] = {}
        self._session_lock = asyncio.Lock()
        self._throttle_lock = asyncio.Lock()
        self._headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.avito.ru/",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        if cookie:
            self._headers["Cookie"] = cookie

        self._browser = None
        if use_browser and AvitoBrowser is not None:
            self._browser = AvitoBrowser(
                headless=browser_headless,
                profile_dir=browser_profile,
                proxy=self._proxy_url,
                timeout_ms=int(self._timeout * 1000),
            )

    def _session_kwargs(self, proxy: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "impersonate": "chrome",
            "timeout": self._timeout,
            "max_clients": 3,
            "headers": self._headers,
            "allow_redirects": True,
        }
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    async def _get_session(self, proxy: str | None = None) -> AsyncSession:
        async with self._session_lock:
            session = self._sessions.get(proxy)
            if session is None:
                session = AsyncSession(**self._session_kwargs(proxy))
                self._sessions[proxy] = session
            return session

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            now = asyncio.get_running_loop().time()
            wait_for = self._request_delay - (now - self._last_request_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for + random.uniform(0, 0.4))
            self._last_request_at = asyncio.get_running_loop().time()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._blocked_until = float(data.get("blocked_until") or 0.0)
            self._block_count = int(data.get("block_count") or 0)
        except Exception:
            logger.debug("Не удалось прочитать состояние Avito", exc_info=True)
            return
        left = int(self._blocked_until - time.time())
        if left > 0:
            logger.info(
                "Avito: действует пауза после блокировки, ещё %s мин. (запросы не отправляются)",
                left // 60 + 1,
            )
        elif self._blocked_until:
            self._blocked_until = 0.0

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(
                    {
                        "blocked_until": self._blocked_until,
                        "block_count": self._block_count,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Не удалось сохранить состояние Avito", exc_info=True)

    def _reset_block(self) -> None:
        self._block_count = 0
        self._blocked_until = 0.0
        if self._state_path is not None and self._state_path.exists():
            try:
                self._state_path.unlink()
            except Exception:
                pass

    def clear_block(self) -> None:
        """Сбросить паузу вручную (например, после смены IP)."""
        if self._blocked_until > time.time():
            logger.info("Пауза Avito сброшена вручную")
        self._last_reload_at = 0.0
        self._force_reload = True
        self._reset_block()

    def _note_block(self, first_minutes: int = 15) -> None:
        self._block_count += 1
        # 15 мин, 30, 60, 120 — максимум 2 часа. Не долбим Avito, пока IP
        # заблокирован: иначе блокировка продлевается.
        minutes = min(
            120, first_minutes * (2 ** (self._block_count - 1))
        )
        self._blocked_until = time.time() + minutes * 60
        self._save_state()
        logger.warning(
            "Avito ограничил запросы, пауза %s мин. (попытка %s)",
            minutes,
            self._block_count,
        )

    async def _request_html(
        self, session: AsyncSession, url: str, attempts: int = 2
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            await self._throttle()
            try:
                response = await session.get(url, allow_redirects=True)
            except Exception as exc:
                last_error = exc
                logger.warning("Ошибка сети при запросе %s: %s", url, exc)
                if attempt + 1 < attempts:
                    await asyncio.sleep(2 + random.uniform(0, 1))
                    continue
                raise AvitoError(
                    f"Сетевая ошибка при запросе к Avito: {exc}"
                ) from exc

            status = int(response.status_code)
            if status == 200:
                raw = response.content or b""
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        text = raw.decode("cp1251")
                    except UnicodeDecodeError:
                        text = raw.decode("utf-8", "replace")
                if detect_firewall(text):
                    raise AvitoBlockedError(
                        "Avito ограничил доступ (firewall/anti-bot).",
                        status=status,
                    )
                return text

            if status in {403, 429, 439}:
                retry_after = response.headers.get("Retry-After") or response.headers.get(
                    "retry-after"
                )
                try:
                    delay = min(60.0, max(4.0, float(retry_after or 0)))
                except (TypeError, ValueError):
                    delay = 5.0 + attempt * 5.0
                if attempt + 1 < attempts:
                    logger.warning("Avito ответил HTTP %s, повтор через %.0f сек.", status, delay)
                    await asyncio.sleep(delay + random.uniform(0, 2))
                    continue
                raise AvitoBlockedError(
                    f"Avito ограничил доступ (HTTP {status}). Обычно помогает другой IP/прокси.",
                    status=status,
                )

            raise AvitoError(f"Avito вернул HTTP {status} для {url}")
        raise AvitoError(f"Не удалось загрузить страницу Avito: {last_error}")

    def _proxy_order(self) -> list[str | None]:
        """Прокси по кругу, начиная с текущего, плюс прямой запрос в конце."""
        if not self._proxy_urls:
            return [None]
        start = self._proxy_index % len(self._proxy_urls)
        ordered: list[str | None] = [
            *self._proxy_urls[start:],
            *self._proxy_urls[:start],
        ]
        if self._fallback_direct:
            ordered.append(None)
        return ordered

    def _advance_proxy(self, proxy: str | None) -> None:
        if proxy is None or not self._proxy_urls:
            return
        try:
            current = self._proxy_urls.index(proxy)
        except ValueError:
            return
        self._proxy_index = (current + 1) % len(self._proxy_urls)

    async def _fetch_html_browser(self, url: str) -> str:
        assert self._browser is not None
        try:
            text = await self._browser.fetch_html(url)
        except BrowserFetchError as exc:
            if exc.status in {403, 429, 439}:
                self._note_block()
                raise AvitoBlockedError(
                    f"Avito ограничил доступ (HTTP {exc.status}). Если браузер "
                    "детектируется, попробуйте AVITO_BROWSER_HEADLESS=false.",
                    status=exc.status,
                ) from exc
            raise AvitoError(f"Не удалось загрузить страницу Avito: {exc}") from exc
        if detect_firewall(text):
            self._note_block()
            raise AvitoBlockedError(
                "Avito ограничил доступ (firewall/anti-bot).", status=439
            )
        self._reset_block()
        return text

    async def _fetch_json_browser(self, url: str, params: dict[str, Any]) -> Any:
        assert self._browser is not None
        full_url = url
        if params:
            separator = "&" if "?" in url else "?"
            full_url = f"{url}{separator}{urlencode(params)}"
        try:
            status, body = await self._browser.fetch_json(full_url)
        except BrowserFetchError as exc:
            if exc.status in {403, 429, 439}:
                self._note_block()
                raise AvitoBlockedError(
                    f"Avito ограничил доступ (HTTP {exc.status}).", status=exc.status
                ) from exc
            raise AvitoError(
                f"Сетевая ошибка при запросе JSON к Avito: {exc}"
            ) from exc
        if status in {403, 429, 439}:
            self._note_block()
            raise AvitoBlockedError(
                f"Avito ограничил доступ (HTTP {status}).", status=status
            )
        if status != 200:
            raise AvitoError(f"Avito вернул HTTP {status} для {full_url}")
        if body.lstrip().startswith("<"):
            raise AvitoBlockedError(
                "Avito показал проверку вместо данных поиска (JSON API).",
                status=status,
            )
        try:
            data = json.loads(body)
        except Exception as exc:
            raise AvitoError(f"Avito вернул некорректный JSON: {exc}") from exc
        self._reset_block()
        return data

    async def _fetch_html(self, url: str) -> str:
        now = time.time()
        if now < self._blocked_until:
            left = int(self._blocked_until - now)
            raise AvitoBlockedError(
                f"Avito временно заблокировал IP, следующая попытка через {left // 60 + 1} мин."
            )

        if self._browser is not None:
            return await self._fetch_html_browser(url)

        order = self._proxy_order()
        last_error: AvitoError | None = None
        for proxy in order:
            session = await self._get_session(proxy)
            # Если есть запасной вариант, не ждём два таймаута на один адрес.
            attempts = 1 if (proxy is not None and len(order) > 1) else 2
            try:
                text = await self._request_html(session, url, attempts)
            except AvitoError as exc:
                last_error = exc
                if proxy is not None and len(order) > 1:
                    logger.warning(
                        "Прокси Avito не сработал (%s), пробую следующий",
                        str(exc)[:160],
                    )
                continue
            self._advance_proxy(proxy)
            self._reset_block()
            return text

        if isinstance(last_error, AvitoBlockedError):
            self._note_block()
        raise last_error or AvitoError("Не удалось загрузить страницу Avito")

    async def _request_json(
        self,
        session: AsyncSession,
        url: str,
        params: dict[str, Any],
        attempts: int = 2,
    ) -> Any:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            await self._throttle()
            try:
                response = await session.get(
                    url,
                    params=params,
                    headers=headers,
                    allow_redirects=True,
                )
            except Exception as exc:
                last_error = exc
                logger.warning("Ошибка сети при запросе JSON %s: %s", url, exc)
                if attempt + 1 < attempts:
                    await asyncio.sleep(2 + random.uniform(0, 1))
                    continue
                raise AvitoError(f"Сетевая ошибка при запросе к Avito: {exc}") from exc

            status = int(response.status_code)
            content_type = (response.headers.get("content-type") or "").lower()
            if status == 200 and "json" in content_type:
                try:
                    return response.json()
                except Exception as exc:
                    raise AvitoError(f"Avito вернул некорректный JSON: {exc}") from exc
            if status == 200 and "text/html" in content_type:
                raise AvitoBlockedError(
                    "Avito показал проверку вместо данных поиска (JSON API).",
                    status=status,
                )
            if status in {403, 429, 439}:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(60.0, max(4.0, float(retry_after or 0)))
                except (TypeError, ValueError):
                    delay = 5.0 + attempt * 5.0
                if attempt + 1 < attempts:
                    logger.warning("JSON API Avito: HTTP %s, повтор через %.0f сек.", status, delay)
                    await asyncio.sleep(delay + random.uniform(0, 2))
                    continue
                raise AvitoBlockedError(
                    f"Avito ограничил доступ (HTTP {status}). Обычно помогает другой IP/прокси.",
                    status=status,
                )
            raise AvitoError(f"Avito вернул HTTP {status} для {url}")
        raise AvitoError(f"Не удалось загрузить JSON Avito: {last_error}")

    async def _fetch_json(self, url: str, params: dict[str, Any]) -> Any:
        now = time.time()
        if now < self._blocked_until:
            left = int(self._blocked_until - now)
            raise AvitoBlockedError(
                f"Avito временно заблокировал IP, следующая попытка через {left // 60 + 1} мин."
            )

        if self._browser is not None:
            return await self._fetch_json_browser(url, params)

        order = self._proxy_order()
        last_error: AvitoError | None = None
        for proxy in order:
            session = await self._get_session(proxy)
            attempts = 1 if (proxy is not None and len(order) > 1) else 2
            try:
                data = await self._request_json(session, url, params, attempts)
            except AvitoError as exc:
                last_error = exc
                if proxy is not None and len(order) > 1:
                    logger.warning(
                        "Прокси Avito (JSON) не сработал (%s), пробую следующий",
                        str(exc)[:160],
                    )
                continue
            self._advance_proxy(proxy)
            self._reset_block()
            return data

        if isinstance(last_error, AvitoBlockedError):
            self._note_block()
        raise last_error or AvitoError("Не удалось загрузить JSON Avito")

    async def _fetch_search_api(self, url: str, pages: int) -> list[Listing]:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        q = (query.get("q") or "").strip()
        if not q:
            return []
        location_id = query.get("locationId") or self._location_id
        category_id = query.get("categoryId")
        api_url = "https://www.avito.ru/web/1/main/items"
        collected: list[Listing] = []
        for page in range(1, max(1, pages) + 1):
            params: dict[str, Any] = {"q": q, "page": page, "limit": 30}
            if location_id:
                params["locationId"] = location_id
            if category_id:
                params["categoryId"] = category_id
            data = await self._fetch_json(api_url, params)
            if not isinstance(data, dict):
                return []
            title = str(data.get("title") or "").lower()
            # Иногда вместо выдачи API отдаёт главную страницу с рекомендациями.
            # Такие объявления нельзя показывать как результаты поиска.
            if "рекомендац" in title:
                logger.warning("JSON API Avito вернул рекомендации вместо выдачи, перехожу на HTML")
                return []
            if location_id and data.get("locationId") and str(data["locationId"]) != str(location_id):
                logger.warning(
                    "JSON API Avito вернул другой город (locationId=%s, нужен %s)",
                    data.get("locationId"),
                    location_id,
                )
                return []
            items = parse_api_items(data)
            collected.extend(items)
            raw_count = len(data.get("items") or [])
            if raw_count < 30:
                break
        return collected

    async def _fetch_search_browser(self, url: str, pages: int) -> list[Listing]:
        assert self._browser is not None
        all_items: list[Listing] = []
        for page in range(1, max(1, pages) + 1):
            page_url = build_page_url(url, page)
            # Первую страницу читаем с уже открытой вкладки: перезагружаем
            # её не чаще, чем раз в _watch_reload секунд, а в остальное время
            # просто смотрим DOM. Так браузер остаётся открытым и не долбит Авито.
            if pages == 1:
                reload = (time.time() - self._last_reload_at) >= self._watch_reload
                force = self._force_reload
                self._force_reload = False
                if reload or force:
                    await self._throttle()
                    # Засекаем время ДО перезагрузки: иначе долгая загрузка
                    # сдвигает следующий цикл и новые лоты приходят позже.
                    self._last_reload_at = time.time()
                try:
                    raw_items = await self._browser.watch_items(
                        page_url, reload=reload, force=force
                    )
                except BrowserFetchError as exc:
                    if exc.status in {403, 429, 439}:
                        raise AvitoBlockedError(
                            "Авито показывает проверку IP. Пройдите её в открытом "
                            "браузере — бот продолжит автоматически.",
                            status=exc.status,
                        ) from exc
                    raise AvitoError(
                        f"Не удалось загрузить страницу поиска Avito: {exc}"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise AvitoError(
                        f"Не удалось загрузить страницу поиска Avito: {exc}"
                    ) from exc
            else:
                await self._throttle()
                try:
                    raw_items = await self._browser.fetch_search_items(page_url)
                except BrowserFetchError as exc:
                    if exc.status in {403, 429, 439}:
                        raise AvitoBlockedError(
                            "Авито показывает проверку IP. Пройдите её в открытом "
                            "браузере — бот продолжит автоматически.",
                            status=exc.status,
                        ) from exc
                    raise AvitoError(
                        f"Не удалось загрузить страницу поиска Avito: {exc}"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise AvitoError(
                        f"Не удалось загрузить страницу поиска Avito: {exc}"
                    ) from exc
            # Ошибки ловим снаружи, чтобы не потерять открытую страницу с капчей.
            page_items = [
                listing
                for listing in (self._listing_from_dom(node) for node in raw_items)
                if listing is not None
            ]
            all_items.extend(page_items)
            if len(page_items) < 10:
                break
            if page < pages:
                await asyncio.sleep(random.uniform(3.0, 6.0))

        self._reset_block()
        by_id: dict[str, Listing] = {}
        for item in all_items:
            previous = by_id.get(item.id)
            by_id[item.id] = _merge_listings(previous, item) if previous else item
        return list(by_id.values())

    @staticmethod
    def _listing_from_dom(node: dict[str, Any]) -> Listing | None:
        item_id = str(node.get("id") or "").strip()
        href = str(node.get("href") or "").strip()
        if not item_id or not href:
            return None
        if href.startswith("//"):
            href = "https:" + href
        elif not href.startswith("http"):
            href = "https://www.avito.ru" + href
        title = _strip_html(str(node.get("title") or "")).strip()
        price_text = str(node.get("price_text") or "").strip()
        price = parse_price_text(price_text)
        if price is None:
            price = parse_price_text(str(node.get("price") or ""))
        if not price_text:
            price_text = format_price(price)
        images: list[str] = []
        image = str(node.get("image") or "").strip()
        if image:
            if image.startswith("//"):
                image = "https:" + image
            images.append(image)
        location = str(node.get("location") or "").strip() or None
        published_ts, published_precise = _parse_publication(node.get("date"))
        if published_ts is None:
            published_ts, published_precise = _parse_publication(
                node.get("published")
            )
        return Listing(
            id=item_id,
            title=title or item_id,
            url=href,
            price=price,
            price_text=price_text,
            image_urls=images,
            location=location,
            published_ts=published_ts,
            published_precise=published_precise,
        )

    async def fetch_search(self, url: str, pages: int = 1) -> list[Listing]:
        pages = max(1, pages)
        if self._all_russia:
            url = to_all_russia(url)

        # Основной путь: настоящий браузер, который сам проходит JS-проверку.
        if self._browser is not None:
            return await self._fetch_search_browser(url, pages)

        # JSON API отдаёт выдачу даже когда HTML-страница закрыта капчей.
        try:
            api_items = await self._fetch_search_api(url, pages)
        except AvitoError as exc:
            logger.warning(
                "JSON API Avito не сработал (%s), пробую HTML",
                str(exc)[:160],
            )
            api_items = []
        if api_items:
            by_id: dict[str, Listing] = {}
            for item in api_items:
                previous = by_id.get(item.id)
                by_id[item.id] = _merge_listings(previous, item) if previous else item
            return list(by_id.values())

        all_items: list[Listing] = []
        for page in range(1, pages + 1):
            page_url = build_page_url(url, page)
            try:
                html_text = await self._fetch_html(page_url)
                items = parse_search_html(html_text)
            except AvitoError:
                if page == 1:
                    raise
                logger.warning("Не удалось прочитать страницу %s, оставляю уже полученные данные", page)
                break
            all_items.extend(items)
            if len(items) < 10:
                break
        by_id: dict[str, Listing] = {}
        for item in all_items:
            previous = by_id.get(item.id)
            by_id[item.id] = _merge_listings(previous, item) if previous else item
        return list(by_id.values())

    async def _fetch_listing_browser(
        self, url: str, item_id: str
    ) -> Listing | None:
        assert self._browser is not None
        await self._throttle()
        try:
            node = await self._browser.fetch_listing_detail(url)
        except BrowserFetchError as exc:
            raise AvitoError(f"Не удалось загрузить карточку Avito: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise AvitoError(f"Не удалось загрузить карточку Avito: {exc}") from exc
        if not node:
            return None
        title = _strip_html(str(node.get("title") or "")).strip()
        price_text = str(node.get("price_text") or "").strip()
        price = parse_price_text(price_text)
        if price is None:
            price = parse_price_text(str(node.get("price") or ""))
        if not price_text:
            price_text = format_price(price)
        description = _strip_html(str(node.get("description") or "")).strip()
        seller = str(node.get("seller") or "").strip() or None
        location = str(node.get("location") or "").strip() or None
        published_ts, published_precise = _parse_publication(node.get("date"))
        images: list[str] = []
        for raw in node.get("images") or []:
            if not isinstance(raw, str):
                continue
            image = raw.strip()
            if not image:
                continue
            if image.startswith("//"):
                image = "https:" + image
            if image not in images:
                images.append(image)
        return Listing(
            id=str(item_id),
            title=title or item_id,
            url=url,
            price=price,
            price_text=price_text,
            description=description,
            image_urls=images,
            seller_name=seller,
            location=location,
            published_ts=published_ts,
            published_precise=published_precise,
        )

    async def fetch_listing(self, url: str, item_id: str) -> Listing | None:
        if self._browser is not None:
            return await self._fetch_listing_browser(url, item_id)
        html_text = await self._fetch_html(url)
        return parse_listing_html(html_text, item_id)

    async def fetch_image_bytes(self, url: str) -> bytes | None:
        """Скачать фото через сессию браузера.

        У Авито картинки доступны только с теми же куками и User-Agent,
        что и страница. Обычный aiohttp-запрос часто получает заглушку или 403,
        поэтому пробуем через уже открытый браузер.
        """
        if self._browser is None:
            return None
        try:
            return await self._browser.fetch_bytes(url, timeout_ms=15000)
        except Exception:  # noqa: BLE001
            logger.debug("Avito: не удалось скачать фото %s", url, exc_info=True)
            return None

    async def fetch_item_info(
        self, url: str
    ) -> tuple[int | None, bool, dict[str, Any]]:
        """Время публикации и данные продавца из карточки объявления.

        Возвращает (timestamp, точное_время, данные_карточки). Данные нужны
        для подписи: описание, имя/тип продавца, рейтинг, число отзывов.
        """
        if self._browser is None:
            return None, False, {}
        try:
            data = await self._browser.fetch_item_page(url)
        except Exception:  # noqa: BLE001
            logger.debug("Avito: не удалось получить карточку %s", url, exc_info=True)
            return None, False, {}
        if not data:
            return None, False, {}

        detail: dict[str, Any] = {}
        description = (data.get("description") or "").strip()
        if description:
            detail["description"] = description
        seller_name = (data.get("seller_name") or "").strip()
        if seller_name:
            detail["seller_name"] = seller_name
        label = (data.get("seller_label") or "").strip()
        if label:
            detail["seller_is_private"] = "частное" in label.lower()
        href = data.get("seller_href") or ""
        if href:
            match = re.search(r"[?&]shopId=(\d+)", href)
            if match:
                detail["seller_id"] = "shop:" + match.group(1)
            else:
                match = re.search(r"/brands/([^/?#]+)", href)
                if match:
                    detail["seller_id"] = "brand:" + match.group(1)
        if not detail.get("seller_id") and seller_name:
            if seller_name.lower() not in {"пользователь", "не указан", "авито"}:
                detail["seller_id"] = "name:" + seller_name
        reviews_text = data.get("reviews_text") or ""
        match = re.search(r"([\d\s\u00a0]+)\s*отзыв", reviews_text, re.IGNORECASE)
        if match:
            count = int(re.sub(r"\D", "", match.group(1)) or 0)
            if count:
                detail["seller_reviews"] = count
        block_text = (data.get("seller_block") or "").replace("\u00a0", " ")
        match = re.search(r"(\d[.,]\d)", block_text)
        if match:
            detail["seller_rating"] = float(match.group(1).replace(",", "."))
        match = re.search(r"заверш[её]нных:?\s*(\d+)", block_text, re.IGNORECASE)
        if match:
            detail["seller_completed"] = int(match.group(1))

        raw = data.get("date")
        if not raw:
            logger.info("Avito: в карточке %s нет времени публикации", url[-32:])
            return None, False, detail
        raw = str(raw).replace("\u00a0", " ").strip(" ·\n\t")
        result = _parse_publication(raw)
        logger.info(
            "Avito: время публикации %s -> %r -> %s",
            url.rsplit("/", 1)[-1][-24:],
            raw,
            _format_debug_ts(result[0]),
        )
        return result[0], result[1], detail

    async def close(self) -> None:
        async with self._session_lock:
            for session in self._sessions.values():
                try:
                    await session.close()
                except Exception:
                    logger.debug("Не удалось закрыть сессию Avito", exc_info=True)
            self._sessions.clear()
        if self._browser is not None:
            await self._browser.close()
