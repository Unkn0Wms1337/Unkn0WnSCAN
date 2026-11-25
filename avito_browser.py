"""Загрузка страниц Авито через настоящий Chromium (Playwright).

curl-cffi не исполняет JavaScript, поэтому не проходит антибот-проверку
Авито и получает HTTP 439. Настоящий браузер решает challenge сам.

Чтобы не нагружать систему, браузер запускается один раз, страница
переиспользуется, а картинки/шрифты/медиа не скачиваются.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

AVITO_HOME = "https://www.avito.ru/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Скрываем типичные признаки автоматизации.
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

SEARCH_ITEM_SELECTOR = '[data-marker="item"]'


def playwright_proxy(url: str | None) -> dict | None:
    """Превращает прокси-URL в формат Playwright.

    Поддерживаются http://, https:// и socks5://, в том числе с логином:
    ``socks5://user:pass@1.2.3.4:1080``.
    """
    value = (url or "").strip()
    if not value:
        return None
    parts = urlsplit(value if "://" in value else "http://" + value)
    if not parts.hostname:
        return None
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy: dict = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


def mask_proxy(url: str | None) -> str:
    """Прокси без логина/пароля — чтобы не светить их в логах."""
    value = (url or "").strip()
    if not value:
        return "нет (напрямую)"
    parts = urlsplit(value if "://" in value else "http://" + value)
    if not parts.hostname:
        return "некорректный"
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{host}"

EXTRACT_SEARCH_ITEMS_JS = """
() => {
    const items = [...document.querySelectorAll('[data-marker="item"]')];
    const textOf = (el) => el ? (el.innerText || el.textContent || '').trim() : '';

    // В JSON-LD Авито кладёт дату публикации каждого объявления (validFrom).
    const offerDates = {};
    try {
        for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
            let data;
            try { data = JSON.parse(script.textContent || ''); } catch (e) { continue; }
            const graphs = Array.isArray(data) ? data : (data['@graph'] || [data]);
            for (const g of graphs) {
                if (!g || !g.offers) continue;
                const raw = g.offers.offers || (Array.isArray(g.offers) ? g.offers : [g.offers]);
                for (const offer of raw) {
                    if (!offer || !offer.url || !offer.validFrom) continue;
                    const m = String(offer.url).match(/(\\d{6,})(?:\\?|$)/);
                    if (m) offerDates[m[1]] = offer.validFrom;
                }
            }
        }
    } catch (e) {}

    return items.map((el) => {
        const link = el.querySelector('a[itemprop="url"]');
        const nameEl = el.querySelector('[itemprop="name"]') || el.querySelector('[data-marker="item-title"]');
        const priceMeta = el.querySelector('meta[itemprop="price"]');
        const priceEl = el.querySelector('[data-marker="item-price"]');
        const dateEl = el.querySelector('[data-marker="item-date"]');
        const img = el.querySelector('img[itemprop="image"]') ||
                    el.querySelector('[data-marker="item-image"] img');
        // У невидимых (ленивых) карточек <img> без src, но ссылка на фото
        // всегда лежит в data-marker="slider-image/image-<URL>".
        let image = img
            ? (img.getAttribute('src') || img.getAttribute('data-src') ||
               img.getAttribute('srcset') || img.getAttribute('data-srcset'))
            : null;
        if (!image) {
            const slide = el.querySelector('[data-marker^="slider-image/image-"]');
            const marker = slide ? (slide.getAttribute('data-marker') || '') : '';
            if (marker.indexOf('slider-image/image-') === 0) {
                image = marker.slice('slider-image/image-'.length);
            }
        }
        if (image) {
            image = String(image).trim().split(/\\s+/)[0];
            if (image.indexOf('//') === 0) image = 'https:' + image;
            if (!/^https?:/i.test(image)) image = null;
        }
        const itemId = el.getAttribute('data-item-id');
        return {
            id: itemId,
            href: link ? link.getAttribute('href') : null,
            title: textOf(nameEl),
            price: priceMeta ? priceMeta.getAttribute('content') : null,
            price_text: textOf(priceEl),
            image: image,
            location: textOf(el.querySelector('[data-marker="item-location"]')),
            date: textOf(dateEl),
            published: offerDates[itemId] || null,
        };
    });
}
"""

EXTRACT_LISTING_JS = """
() => {
    const textOf = (sel) => {
        const el = document.querySelector(sel);
        return el ? (el.innerText || el.textContent || '').trim() : '';
    };
    const attrOf = (sel, attr) => {
        const el = document.querySelector(sel);
        return el ? el.getAttribute(attr) : null;
    };
    const images = [];
    const og = attrOf('meta[property="og:image"]', 'content');
    if (og) images.push(og);
    for (const img of document.querySelectorAll(
        '[data-marker="image-preview/item"] img, [data-marker="image-frame/image-wrapper"] img'
    )) {
        const src = img.getAttribute('src') || img.getAttribute('data-src');
        if (src && !images.includes(src)) images.push(src);
        if (images.length >= 10) break;
    }
    return {
        title: textOf('h1') || textOf('[data-marker="item-view/title-info"]'),
        price_text: textOf('[data-marker="item-view/item-price"]'),
        price: attrOf('meta[itemprop="price"]', 'content'),
        description: textOf('[data-marker="item-view/item-description"]'),
        seller: textOf('[data-marker="seller-info/name"]'),
        location: textOf('[data-marker="item-view/item-location"]') ||
                  textOf('[data-marker="item-view/item-address"]'),
        date: textOf('[data-marker="item-view/item-date"]'),
        images: images,
    };
}
"""

BLOCK_MARKERS = (
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

BLOCKED_RESOURCES = {"image", "media", "font"}


class BrowserFetchError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AvitoBrowser:
    """Держит один Chromium и одну переиспользуемую страницу для Авито."""

    def __init__(
        self,
        *,
        headless: bool = True,
        profile_dir: str | Path | None = None,
        proxy: str | None = None,
        timeout_ms: int = 45000,
        block_images: bool = False,
    ) -> None:
        self._headless = headless
        self._profile_dir = str(profile_dir) if profile_dir else None
        self._proxy = proxy or None
        self._timeout_ms = max(15000, int(timeout_ms))
        self._block_images = block_images
        self._pw = None
        self._context = None
        self._page = None
        self._detail_page = None
        self._warmed = False
        self._start_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._context is not None:
            return
        async with self._start_lock:
            if self._context is not None:
                return
            await self._start_locked()

    async def _start_locked(self) -> None:
        self._pw = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": self._headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--mute-audio",
                "--disable-background-networking",
                "--disable-sync",
                "--lang=ru-RU",
            ],
        }
        if not self._headless:
            launch_kwargs["args"].append("--start-maximized")
        proxy = playwright_proxy(self._proxy)
        if proxy:
            launch_kwargs["proxy"] = proxy
        viewport = None if not self._headless else {"width": 1366, "height": 768}
        if self._profile_dir:
            Path(self._profile_dir).mkdir(parents=True, exist_ok=True)
            self._context = await self._pw.chromium.launch_persistent_context(
                self._profile_dir,
                user_agent=USER_AGENT,
                locale="ru-RU",
                viewport=viewport,
                **launch_kwargs,
            )
        else:
            browser = await self._pw.chromium.launch(**launch_kwargs)
            self._context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="ru-RU",
                viewport=viewport,
            )
        await self._context.add_init_script(STEALTH_SCRIPT)
        self._context.set_default_timeout(self._timeout_ms)
        logger.info(
            "Avito: запущен браузер (headless=%s, профиль=%s, прокси=%s)",
            self._headless,
            self._profile_dir or "временный",
            mask_proxy(self._proxy),
        )

    def set_proxy(self, proxy: str | None) -> None:
        """Меняет прокси. Применяется только после close()/перезапуска."""
        self._proxy = (proxy or "").strip() or None

    async def _new_page(self):
        page = await self._context.new_page()
        if self._block_images:
            async def _route(route):
                try:
                    if route.request.resource_type in BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:  # noqa: BLE001
                    pass

            await page.route("**/*", _route)
        return page

    async def _ready_page(self):
        """Страница под уже взятым _page_lock."""
        await self.start()
        if self._page is None or self._page.is_closed():
            self._page = await self._new_page()
            self._warmed = False
        if not self._warmed:
            await self._warmup(self._page)
        return self._page

    async def _warmup(self, page) -> None:
        """Один раз открываем главную, чтобы антибот успел выдать куки."""
        try:
            await page.goto(
                AVITO_HOME, wait_until="domcontentloaded", timeout=self._timeout_ms
            )
            await self._wait_ready(page)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Avito: прогрев главной не удался: %s", exc)
        self._warmed = True

    async def _wait_ready(self, page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            # networkidle не всегда наступает — не критично.
            pass

    async def _extract_items(self, page) -> list[dict]:
        try:
            items = await page.evaluate(EXTRACT_SEARCH_ITEMS_JS)
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    async def _body_text(self, page) -> str:
        try:
            return str(
                await page.evaluate(
                    "() => (document.body ? document.body.innerText : '')"
                )
                or ""
            )
        except Exception:
            return ""

    @staticmethod
    def _is_challenge(body: str) -> bool:
        lowered = (body or "").casefold()
        return any(marker in lowered for marker in BLOCK_MARKERS)

    async def watch_items(
        self,
        url: str,
        *,
        reload: bool,
        force: bool = False,
        reload_wait_ms: int = 12000,
    ) -> list[dict]:
        """Прочитать выдачу с уже открытой страницы.

        Важно для ручной проверки: если на странице висит капча/проверка IP,
        мы НЕ перезагружаем её и не уходим на другой адрес, чтобы человек мог
        спокойно пройти проверку. Пока проверка не пройдена, читаем только DOM.
        """
        async with self._page_lock:
            page = await self._ready_page()
            try:
                target = url.split("#")[0].rstrip("/")
                current = (page.url or "").split("#")[0].rstrip("/")
                current_lower = current.casefold()
                on_captcha_page = "firewall" in current_lower or "captcha" in current_lower

                items = await self._extract_items(page)
                if items and not reload and not force:
                    return items

                body = "" if items else await self._body_text(page)
                if (
                    not force
                    and not items
                    and (on_captcha_page or self._is_challenge(body))
                ):
                    # Не трогаем страницу — пусть человек пройдёт проверку.
                    raise BrowserFetchError(
                        "Avito показывает проверку IP",
                        status=429,
                    )

                if current != target or force:
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=self._timeout_ms
                    )
                    if (
                        response is not None
                        and response.status >= 400
                        and response.status not in {403, 429, 439}
                    ):
                        raise BrowserFetchError(
                            f"Avito вернул HTTP {response.status}",
                            status=response.status,
                        )
                    try:
                        await page.wait_for_selector(SEARCH_ITEM_SELECTOR, timeout=20000)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                elif reload:
                    logger.info("Avito: авто-обновление страницы поиска")
                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=self._timeout_ms,
                        )
                    except Exception:
                        pass
                    try:
                        await page.wait_for_selector(
                            SEARCH_ITEM_SELECTOR, timeout=reload_wait_ms
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)

                await asyncio.sleep(0.3)
                items = await self._extract_items(page)
                if items:
                    return items
                body = await self._body_text(page)
                if self._is_challenge(body):
                    raise BrowserFetchError(
                        "Avito показывает проверку IP",
                        status=429,
                    )
                return []
            except BrowserFetchError:
                # Страницу НЕ закрываем: она нужна человеку для прохождения проверки.
                raise
            except Exception as exc:  # noqa: BLE001
                await self._drop_page()
                raise BrowserFetchError(f"Ошибка браузера: {exc}") from exc

    async def fetch_search_items(
        self, url: str, *, wait_ms: int = 20000
    ) -> list[dict]:
        """Открывает страницу поиска и вытаскивает карточки объявлений из DOM.

        Серверный HTML Авито больше не содержит данные выдачи — они
        дорисовываются JavaScript. Поэтому парсим уже отрендеренный DOM.
        """
        async with self._page_lock:
            page = await self._ready_page()
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self._timeout_ms
                )
                if response is None:
                    raise BrowserFetchError("Авито не ответил")
                status = response.status
                if status >= 400 and status not in {403, 429, 439}:
                    raise BrowserFetchError(f"Avito вернул HTTP {status}", status=status)
                try:
                    await page.wait_for_selector(SEARCH_ITEM_SELECTOR, timeout=wait_ms)
                except Exception:
                    # Пустая выдача — тоже валидный результат, разберём ниже.
                    pass
                await self._wait_ready(page)
                await asyncio.sleep(0.6)
                items = await page.evaluate(EXTRACT_SEARCH_ITEMS_JS)
                if items:
                    return [item for item in items if isinstance(item, dict)]
                body = await page.evaluate(
                    "() => (document.body ? document.body.innerText : '')"
                )
                lowered = str(body or "").casefold()
                if any(marker in lowered for marker in BLOCK_MARKERS):
                    raise BrowserFetchError(
                        "Avito показал проверку вместо результатов поиска",
                        status=status if status in {403, 429, 439} else 429,
                    )
                if status in {403, 429, 439}:
                    raise BrowserFetchError(
                        f"Avito вернул HTTP {status} вместо выдачи", status=status
                    )
                return []
            except BrowserFetchError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._drop_page()
                raise BrowserFetchError(f"Ошибка браузера: {exc}") from exc

    async def fetch_listing_detail(self, url: str) -> dict | None:
        """Открывает карточку объявления и вытаскивает данные из DOM.

        Авито может вернуть HTTP 439 на сам документ, но страница при этом
        отрисовывается — поэтому статус не считаем приговором, смотрим DOM.
        """
        async with self._page_lock:
            page = await self._ready_page()
            try:
                try:
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=self._timeout_ms
                    )
                except Exception:
                    # Даже если навигация «упала», DOM мог успеть отрисоваться.
                    pass
                try:
                    await page.wait_for_selector(
                        'h1, [data-marker="item-view/item-price"]', timeout=15000
                    )
                except Exception:
                    pass
                await self._wait_ready(page)
                await asyncio.sleep(0.4)
                data = await page.evaluate(EXTRACT_LISTING_JS)
                if not isinstance(data, dict):
                    return None
                title = str(data.get("title") or "")
                if "не существует" in title.casefold():
                    return None
                if not title and not data.get("price_text"):
                    return None
                return data
            except Exception as exc:  # noqa: BLE001
                await self._drop_page()
                raise BrowserFetchError(f"Ошибка браузера: {exc}") from exc

    async def fetch_html(self, url: str) -> str:
        async with self._page_lock:
            page = await self._ready_page()
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self._timeout_ms
                )
                status = response.status if response is not None else None
                if status is not None and status >= 400:
                    raise BrowserFetchError(f"Avito вернул HTTP {status}", status=status)
                raw = ""
                if response is not None:
                    try:
                        raw = await response.text()
                    except Exception:
                        raw = ""
                await self._wait_ready(page)
                if raw:
                    return raw
                return await page.content()
            except BrowserFetchError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._drop_page()
                raise BrowserFetchError(f"Ошибка браузера: {exc}") from exc

    async def fetch_bytes(self, url: str, timeout_ms: int = 15000) -> bytes | None:
        """Скачать файл (например, фото) через сессию браузера."""
        await self.start()
        context = self._context
        if context is None:
            return None
        try:
            response = await context.request.get(
                url,
                timeout=timeout_ms,
                headers={
                    "User-Agent": USER_AGENT,
                    # JPEG/PNG вместо webp: webp Telegram превращает в стикер
                    # и не показывает подпись объявления.
                    "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Referer": AVITO_HOME,
                },
            )
            if response.ok:
                body = await response.body()
                if body:
                    return body
        except Exception:
            logger.debug("Avito: не удалось скачать %s", url, exc_info=True)
        return None

    async def _detail_page_handle(self):
        """Отдельная вкладка для карточек: страница поиска не трогается."""
        await self.start()
        if self._detail_page is None or self._detail_page.is_closed():
            self._detail_page = await self._new_page()
        return self._detail_page

    async def fetch_item_page(self, url: str) -> dict | None:
        """Открывает карточку в отдельной вкладке и вытаскивает данные."""
        page = await self._detail_page_handle()
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self._timeout_ms
            )
            try:
                await page.wait_for_selector(
                    '[data-marker="item-view/item-date"], h1', timeout=12000
                )
            except Exception:
                pass
            data = await page.evaluate(
                """() => {
                    const marker = (sel) => {
                        const el = document.querySelector('[data-marker="' + sel + '"]');
                        return el ? (el.innerText || el.textContent || '').trim() : null;
                    };
                    const link = document.querySelector('[data-marker="seller-link/link"]');
                    const sellerBlock = document.querySelector('[data-marker="sellerInfo"]')
                        || document.querySelector('[data-marker="item-view/seller-info"]');
                    return {
                        h1: (document.querySelector('h1')?.innerText || '').trim() || null,
                        date: marker('item-view/item-date'),
                        description: marker('item-view/item-description'),
                        seller_name: marker('seller-info/name'),
                        seller_label: marker('seller-info/label'),
                        reviews_text: marker('rating-caption/rating'),
                        seller_href: link ? link.getAttribute('href') : null,
                        seller_block: sellerBlock
                            ? (sellerBlock.innerText || '').trim().slice(0, 600)
                            : null,
                    };
                }"""
            )
            if not isinstance(data, dict):
                return None
            h1 = (data.get("h1") or "").lower()
            if "проверка" in h1 or "доступ" in h1:
                return None
            return data
        except Exception as exc:  # noqa: BLE001
            logger.debug("Avito: не удалось открыть карточку %s: %s", url, exc)
            return None

    async def fetch_item_date_text(self, url: str) -> str | None:
        """Текст времени публикации из карточки (fetch внутри страницы)."""
        async with self._page_lock:
            page = await self._ready_page()
            try:
                result = await page.evaluate(
                    """async (url) => {
                        try {
                            const r = await fetch(url, {credentials: 'include'});
                            const t = await r.text();
                            const doc = new DOMParser().parseFromString(t, 'text/html');
                            const el = doc.querySelector('[data-marker="item-view/item-date"]');
                            return {status: r.status, date: el ? el.textContent : null};
                        } catch (e) {
                            return {status: 0, date: null, error: String(e)};
                        }
                    }""",
                    url,
                )
            except Exception:
                logger.debug("Avito: не удалось получить дату %s", url, exc_info=True)
                return None
        date = result.get("date") if isinstance(result, dict) else None
        if not date:
            return None
        return str(date)

    async def fetch_html_via_request(
        self, url: str, timeout_ms: int = 15000
    ) -> str | None:
        """Быстро получить HTML страницы через сессию браузера (без навигации).

        Нужно для чтения точного времени публикации в карточке, не уводя
        открытую страницу поиска.
        """
        await self.start()
        context = self._context
        if context is None:
            return None
        try:
            response = await context.request.get(
                url,
                timeout=timeout_ms,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Referer": AVITO_HOME,
                },
            )
            if response.ok:
                return await response.text()
        except Exception:
            logger.debug("Avito: не удалось получить HTML %s", url, exc_info=True)
        return None

    async def fetch_json(self, url: str) -> tuple[int, str]:
        """Запрос к JSON API через fetch внутри страницы (с куками браузера)."""
        async with self._page_lock:
            page = await self._ready_page()
            try:
                await asyncio.sleep(1.0)
                result = await page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {
                            headers: {'Accept': 'application/json, text/plain, */*'},
                            credentials: 'include',
                        });
                        const t = await r.text();
                        return {status: r.status, text: t};
                    }""",
                    url,
                )
                return int(result["status"]), str(result["text"])
            except Exception as exc:  # noqa: BLE001
                await self._drop_page()
                raise BrowserFetchError(f"Ошибка браузера: {exc}") from exc

    async def _drop_page(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None
        self._warmed = False

    async def close(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                logger.debug("Avito: ошибка закрытия страницы", exc_info=True)
            self._page = None
        if self._detail_page is not None:
            try:
                await self._detail_page.close()
            except Exception:
                pass
            self._detail_page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                logger.debug("Avito: ошибка закрытия браузера", exc_info=True)
            self._context = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                logger.debug("Avito: ошибка остановки Playwright", exc_info=True)
            self._pw = None
        self._warmed = False
