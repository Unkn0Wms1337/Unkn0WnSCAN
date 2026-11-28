from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import tempfile
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from PIL import Image
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.custom import Button

from avito import Listing
from formatter import format_listing_caption

logger = logging.getLogger(__name__)

# Telegram ограничивает длину URL в inline-кнопке.
MAX_BUTTON_URL = 1900


def listing_buttons(listing: Listing):
    """Кнопка перехода к объявлению на Авито."""
    url = (listing.url or "").strip()
    rows = []
    if url and len(url) <= MAX_BUTTON_URL:
        rows.append([Button.url("✈️ Открыть на Авито", url)])
    return rows or None

MAX_PHOTO_BYTES = 15 * 1024 * 1024
PHOTO_TIMEOUT = aiohttp.ClientTimeout(total=35, connect=12)
PHOTO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    # WebP/AVIF не просим: Telegram сохраняет webp как стикер и теряет подпись.
    "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.avito.ru/",
}


def _safe_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def _file_suffix(content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, ".jpg")


def _looks_like_image(content: bytes, content_type: str) -> bool:
    if content_type.lower().startswith("image/"):
        return True
    return content.startswith(
        (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF")
    )


def _to_jpeg(content: bytes) -> bytes | None:
    """Переводит любую картинку в JPEG.

    Telegram сохраняет WebP как стикер (`DocumentAttributeSticker`), а у стикера
    подпись в клиентах не показывается — сообщение выглядит как «фото без текста».
    Поэтому webp/avif/gif всегда перекодируем в обычный JPEG.
    """
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.mode not in ("RGB", "L"):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=90, optimize=True)
            return buffer.getvalue()
    except Exception as exc:
        logger.warning("Не удалось перекодировать фото в JPEG: %s", exc)
        return None


def _http_proxy(url: str | None) -> str | None:
    """aiohttp понимает только HTTP(S)-прокси; socks5 оставляем браузеру."""
    value = (url or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    return None


async def _download_photo(
    session: aiohttp.ClientSession,
    url: str,
    directory: Path,
    index: int,
    semaphore: asyncio.Semaphore,
    fetcher=None,
    proxy: str | None = None,
) -> Path | None:
    content: bytes | None = None
    content_type = ""
    if _safe_http_url(url):
        async with semaphore:
            try:
                async with session.get(
                    url, headers=PHOTO_HEADERS, proxy=_http_proxy(proxy)
                ) as response:
                    if response.status == 200:
                        content_type = response.headers.get("Content-Type", "")
                        content = await response.read()
                    else:
                        logger.debug("Фото %s вернуло HTTP %s", url, response.status)
            except Exception as exc:
                logger.debug("Не удалось скачать фото %s напрямую: %s", url, exc)

    # Авито отдаёт картинки только «своим» — через сессию браузера надёжнее.
    if not content and fetcher is not None:
        try:
            content = await fetcher(url)
        except Exception as exc:
            logger.debug("Не удалось скачать фото %s через браузер: %s", url, exc)
        if content:
            content_type = "image/jpeg"

    if not content or len(content) > MAX_PHOTO_BYTES or not _looks_like_image(content, content_type):
        return None

    suffix = _file_suffix(content_type)
    is_webp = content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    is_avif = content[4:12] == b"ftypavif"
    if suffix not in {".jpg", ".png"} or is_webp or is_avif:
        # WebP/AVIF/GIF -> JPEG, иначе Telegram сделает из картинки стикер
        # и не покажет подпись.
        converted = await asyncio.to_thread(_to_jpeg, content)
        if not converted:
            return None
        content = converted
        suffix = ".jpg"

    path = directory / f"{index:02d}{suffix}"
    await asyncio.to_thread(path.write_bytes, content)
    return path


async def _download_photos(
    urls: list[str], directory: Path, fetcher=None, proxy: str | None = None
) -> list[Path]:
    if not urls:
        return []
    semaphore = asyncio.Semaphore(4)
    connector = aiohttp.TCPConnector(limit=6, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        timeout=PHOTO_TIMEOUT,
        connector=connector,
        headers=PHOTO_HEADERS,
    ) as session:
        results = await asyncio.gather(
            *(
                _download_photo(
                    session, url, directory, index, semaphore, fetcher, proxy
                )
                for index, url in enumerate(urls[:10])
            ),
            return_exceptions=True,
        )
    files: list[Path] = []
    for result in results:
        if isinstance(result, Path):
            files.append(result)
    return files


async def _send_with_flood_retry(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except FloodWaitError as exc:
        logger.warning("Telegram flood control: жду %s сек.", exc.seconds)
        await asyncio.sleep(exc.seconds + 1)
        return await func(*args, **kwargs)


async def send_listing(
    client: TelegramClient,
    chat_id: int,
    listing: Listing,
    market_median: int | None,
    photo_fetcher=None,
    proxy_url: str | None = None,
) -> None:
    """Download photos into a temp folder, send them, then delete them immediately."""
    caption = format_listing_caption(listing, market_median)
    buttons = listing_buttons(listing)

    with tempfile.TemporaryDirectory(prefix="avito-photo-") as tmp:
        directory = Path(tmp)
        try:
            # Одно сообщение = одно объявление: первое фото + подпись с данными
            # + кнопка «Открыть объявление». Альбом не используем, иначе
            # Telegram дублирует кнопку под каждой картинкой.
            files = await _download_photos(
                listing.image_urls[:1],
                directory,
                fetcher=photo_fetcher,
                proxy=proxy_url,
            )
            if not files:
                logger.warning(
                    "Фото для %s не скачалось, отправляю только текст (%s)",
                    listing.id,
                    (listing.image_urls[:1] or ["нет url"]),
                )
            if files:
                try:
                    message = await _send_with_flood_retry(
                        client.send_file,
                        chat_id,
                        files[0],
                        caption=caption,
                        parse_mode="html",
                        buttons=buttons,
                    )
                    logger.info(
                        "Фото с подписью %s: подпись=%s символов, в сообщении=%s",
                        listing.id,
                        len(caption),
                        len(getattr(message, "message", "") or ""),
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        "Не удалось отправить фото с подписью для %s (%s). "
                        "Пробую с короткой подписью.",
                        listing.id,
                        exc,
                    )
                    # Подпись к фото в Telegram ограничена 1024 символами.
                    # Если основная не влезла — отправляем фото с укороченной,
                    # но всё равно ОДНИМ сообщением (фото + текст + кнопка).
                    try:
                        plain = re.sub(r"<[^>]+>", "", caption)
                        plain = unescape(plain).strip()
                        if len(plain) > 800:
                            plain = plain[:797].rstrip() + "…"
                        await _send_with_flood_retry(
                            client.send_file,
                            chat_id,
                            files[0],
                            caption=plain,
                            parse_mode=None,
                            buttons=buttons,
                        )
                        return
                    except Exception as exc2:
                        logger.warning(
                            "Фото с короткой подписью тоже не ушло (%s). "
                            "Отправляю текстом.",
                            exc2,
                        )
            try:
                await _send_with_flood_retry(
                    client.send_message,
                    chat_id,
                    caption,
                    parse_mode="html",
                    link_preview=False,
                    buttons=buttons,
                )
            except Exception:
                # Если кнопка почему-то не принята, всё равно отправим текст.
                if buttons:
                    await _send_with_flood_retry(
                        client.send_message,
                        chat_id,
                        caption,
                        parse_mode="html",
                        link_preview=False,
                    )
                else:
                    raise
        finally:
            # TemporaryDirectory удалит все скачанные фото сразу после отправки
            # (или после ошибки), на диске ничего не остаётся.
            logger.debug("Удаляю временные фото объявления %s", listing.id)
