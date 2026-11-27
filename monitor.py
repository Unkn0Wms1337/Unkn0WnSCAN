from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import replace

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ForbiddenError,
    InputUserDeactivatedError,
    UserIsBlockedError,
)

from avito import (
    AvitoBlockedError,
    AvitoClient,
    AvitoError,
    Listing,
    robust_market_price,
    seller_token,
)
from notifier import send_listing
from storage import Storage, Subscription

logger = logging.getLogger(__name__)

FORBIDDEN_ERRORS = (ForbiddenError, UserIsBlockedError, InputUserDeactivatedError)
BLOCK_RETRIES = 3
BLOCK_RETRY_DELAY = 1.5


class Monitor:
    def __init__(
        self,
        client: TelegramClient,
        storage: Storage,
        avito_client: AvitoClient,
        *,
        interval: int,
        initial_pages: int,
        check_pages: int,
        max_new_per_check: int,
        fetch_details: bool,
        fresh_hours: float = 24.0,
    ) -> None:
        self.client = client
        self.storage = storage
        self.avito = avito_client
        self.interval = interval
        self.initial_pages = initial_pages
        self.check_pages = check_pages
        self.max_new_per_check = max_new_per_check
        self.fetch_details = fetch_details
        self.fresh_window = max(1800.0, float(fresh_hours) * 3600.0)
        self._locks: dict[int, asyncio.Lock] = {}
        self._stop = asyncio.Event()

    def _lock(self, subscription_id: int) -> asyncio.Lock:
        lock = self._locks.get(subscription_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[subscription_id] = lock
        return lock

    async def _send_text(self, chat_id: int, text: str) -> bool:
        try:
            await self.client.send_message(
                chat_id,
                text,
                parse_mode="html",
                link_preview=False,
            )
            return True
        except FloodWaitError as exc:
            logger.warning("Telegram flood control: жду %s сек.", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            try:
                await self.client.send_message(
                    chat_id,
                    text,
                    parse_mode="html",
                    link_preview=False,
                )
                return True
            except Exception:
                logger.exception("Повторная отправка сообщения в чат %s не удалась", chat_id)
                return False
        except FORBIDDEN_ERRORS:
            logger.warning("Бот не может писать в чат %s, удаляю подписки", chat_id)
            self.storage.delete_chat(chat_id)
            return False
        except Exception:
            logger.exception("Не удалось отправить сообщение в чат %s", chat_id)
            return False

    @staticmethod
    def _error_text(error: Exception) -> str:
        if isinstance(error, AvitoBlockedError):
            # Текст намеренно без минут: иначе он меняется от проверки к проверке
            # и бот присылал бы одно и то же уведомление каждые пару минут.
            return (
                "Avito временно ограничил запросы с этого IP. "
                "Обхожу ограничение автоматически: бот сам проходит проверку, "
                "меняет IP/прокси и продолжает без вашего участия."
            )
        if isinstance(error, AvitoError):
            return str(error)
        return "неизвестная ошибка при запросе к Avito"

    async def ensure_initialized(self, subscription_id: int, notify: bool = True) -> None:
        lock = self._lock(subscription_id)
        async with lock:
            subscription = self.storage.get_subscription(subscription_id)
            if subscription is None or subscription.initialized:
                return
            await self._initial_scan_locked(subscription, notify=notify)

    async def _initial_scan_locked(self, subscription: Subscription, notify: bool) -> None:
        if self.avito.is_blocked():
            logger.debug(
                "Первичная проверка #%s отложена: Avito на паузе ещё %s сек.",
                subscription.id,
                self.avito.blocked_seconds_left(),
            )
            return
        try:
            listings = await self._fetch_search_resilient(
                subscription.url, self.initial_pages
            )
        except Exception as exc:
            logger.warning("Первичная проверка %s не удалась: %s", subscription.url, exc)
            self.storage.set_error(subscription.id, self._error_text(exc))
            if notify:
                await self._send_text(
                    subscription.chat_id,
                    "❌ <b>Не удалось прочитать поиск Avito</b>\n\n"
                    f"🔗 {_html_title(subscription.title)}\n"
                    f"Причина: {_html_text(self._error_text(exc))}\n\n"
                    "Подписка сохранена, попробую ещё раз автоматически.",
                )
            return

        median = robust_market_price(item.price for item in listings)
        self.storage.mark_seen(subscription.id, [item.id for item in listings])
        self.storage.set_initialized(subscription.id, median)

        if notify:
            count_text = (
                f"Сейчас в поиске: <b>{len(listings)}</b> "
                f"{_plural(len(listings), 'объявление', 'объявления', 'объявлений')}."
                if listings
                else "Сейчас в поиске нет объявлений."
            )
            interval_minutes = max(1, self.interval // 60)
            await self._send_text(
                subscription.chat_id,
                "✅ <b>Мониторинг запущен</b>\n\n"
                f"🔗 <a href=\"{_html_attr(subscription.url)}\">{_html_title(subscription.title)}</a>\n"
                f"{count_text}\n\n"
                "Пришлю только новые объявления, которые появятся после запуска.\n"
                f"Проверяю каждые {interval_minutes} мин.",
            )

    async def check_subscription(self, subscription_id: int) -> None:
        lock = self._lock(subscription_id)
        async with lock:
            subscription = self.storage.get_subscription(subscription_id)
            if subscription is None:
                return
            if not subscription.initialized:
                await self._initial_scan_locked(subscription, notify=True)
                return
            await self._check_locked(subscription)

    async def _check_locked(self, subscription: Subscription) -> None:
        # После блокировки не дёргаем Avito каждую секунду — это только
        # продлевает блокировку и засоряет лог.
        if self.avito.is_blocked():
            logger.debug(
                "Проверка #%s отложена: Avito на паузе ещё %s сек.",
                subscription.id,
                self.avito.blocked_seconds_left(),
            )
            return
        try:
            listings = await self._fetch_search_resilient(
                subscription.url, self.check_pages
            )
        except Exception as exc:
            message = self._error_text(exc)
            should_notify = self.storage.set_error(subscription.id, message)
            logger.warning("Проверка #%s не удалась: %s", subscription.id, exc)
            if should_notify:
                await self._send_text(
                    subscription.chat_id,
                    "⚠️ <b>Не удалось проверить поиск</b>\n\n"
                    f"🔗 {_html_title(subscription.title)}\n"
                    f"Причина: {_html_text(message)}\n\n"
                    "Продолжаю проверять автоматически.",
                )
            return

        fresh_market = robust_market_price(item.price for item in listings)
        market = fresh_market or subscription.market_median

        seen = self.storage.seen_ids(subscription.id)
        blocked = self.storage.blocked_sellers(subscription.chat_id)
        new_items = [item for item in listings if item.id not in seen]
        if not new_items:
            self.storage.mark_checked(subscription.id, fresh_market)
            return

        # Окно свежести: шлём объявления, опубликованные за последние N часов,
        # даже если бот увидел их не сразу после публикации.
        cutoff = time.time() - self.fresh_window

        accepted: list[Listing] = []
        checked = 0
        for item in new_items:
            published = item.published_ts
            precise = item.published_precise
            logger.debug(
                "Кандидат %s: время=%s (точное=%s)",
                item.id,
                _format_ts(published) if published else "нет",
                precise,
            )

            # Дешёвый отсев по дате из выдачи: если даже она старше окна,
            # карточку не открываем вовсе.
            if (
                published is not None
                and not precise
                and published < cutoff - 86400
            ):
                self.storage.mark_seen(subscription.id, [item.id])
                continue

            # Точное время публикации («1 час назад», «сегодня в 21:14»)
            # читаем из карточки, не уходя со страницы поиска. Оттуда же
            # берём описание и данные продавца для подписи к фото.
            if checked:
                await asyncio.sleep(0.3)
            exact, exact_precise, detail = await self.avito.fetch_item_info(item.url)
            checked += 1
            if detail:
                item = replace(
                    item,
                    description=item.description or detail.get("description", ""),
                    seller_name=item.seller_name or detail.get("seller_name"),
                    seller_id=item.seller_id or detail.get("seller_id"),
                    seller_is_private=(
                        item.seller_is_private
                        if item.seller_is_private is not None
                        else detail.get("seller_is_private")
                    ),
                    seller_rating=(
                        item.seller_rating
                        if item.seller_rating is not None
                        else detail.get("seller_rating")
                    ),
                    seller_reviews=(
                        item.seller_reviews
                        if item.seller_reviews is not None
                        else detail.get("seller_reviews")
                    ),
                    seller_completed=(
                        item.seller_completed
                        if item.seller_completed is not None
                        else detail.get("seller_completed")
                    ),
                )
            if exact is not None:
                item = replace(
                    item,
                    published_ts=exact,
                    published_precise=exact_precise,
                )
                published = exact
                precise = exact_precise

            item_seller_token = seller_token(item.seller_id)
            if item.seller_id and (
                item.seller_id in blocked or item_seller_token in blocked
            ):
                self.storage.mark_seen(subscription.id, [item.id])
                logger.info(
                    "Пропущено объявление %s: продавец заблокирован", item.id
                )
                continue

            if published is not None and published < cutoff:
                self.storage.mark_seen(subscription.id, [item.id])
                logger.info(
                    "Пропущено объявление %s: опубликовано %s (старше %s ч.)",
                    item.id,
                    _format_ts(published),
                    round(self.fresh_window / 3600, 1),
                )
                continue

            if published is None:
                logger.info(
                    "Объявление %s: время публикации не определилось, отправляю",
                    item.id,
                )
            accepted.append(item)

        accepted.sort(
            key=lambda listing: listing.published_ts or 0, reverse=True
        )

        to_send = accepted[: self.max_new_per_check]
        for index, item in enumerate(to_send):
            enriched = item
            if self.fetch_details:
                try:
                    detail = await self.avito.fetch_listing(item.url, item.id)
                    if detail:
                        enriched = _merge(item, detail)
                except Exception as exc:
                    logger.warning(
                        "Не удалось получить карточку %s, отправляю данные из поиска: %s",
                        item.id,
                        exc,
                    )
            try:
                await send_listing(
                    self.client,
                    subscription.chat_id,
                    enriched,
                    market,
                    photo_fetcher=self.avito.fetch_image_bytes,
                    proxy_url=self.avito.current_proxy(),
                )
            except FORBIDDEN_ERRORS:
                logger.warning("Бот заблокирован в чате %s, удаляю подписки", subscription.chat_id)
                self.storage.delete_chat(subscription.chat_id)
                return
            except Exception:
                logger.exception("Не удалось отправить объявление %s", item.id)
                break
            self.storage.mark_seen(subscription.id, [item.id])
            if index < len(to_send) - 1:
                await asyncio.sleep(0.7)

        self.storage.mark_checked(subscription.id, fresh_market)
        if to_send:
            logger.info(
                "Подписка #%s: отправлено %s из %s свежих",
                subscription.id,
                len(to_send),
                len(accepted),
            )

    async def _fetch_search_resilient(self, url: str, pages: int) -> list[Listing]:
        """Retry the search after the Avito client automatically changes IP."""
        for attempt in range(BLOCK_RETRIES):
            try:
                return await self.avito.fetch_search(url, pages)
            except AvitoBlockedError:
                if attempt + 1 >= BLOCK_RETRIES:
                    raise
                await asyncio.sleep(BLOCK_RETRY_DELAY)
                logger.info(
                    "Повторяю проверку Avito после смены IP (%s/%s)",
                    attempt + 2,
                    BLOCK_RETRIES,
                )
        return []

    async def search_now(self, chat_id: int, url: str, limit: int = 8) -> int:
        """Run a one-off search and send the freshest listings to the chat."""
        listings = await self._fetch_search_resilient(url, 1)
        if not listings:
            await self._send_text(
                chat_id, "🔎 По этому запросу сейчас ничего не нашлось."
            )
            return 0

        market = robust_market_price(item.price for item in listings)
        to_send = listings[: max(1, limit)]
        sent = 0
        for index, item in enumerate(to_send):
            try:
                await send_listing(
                    self.client,
                    chat_id,
                    item,
                    market,
                    photo_fetcher=self.avito.fetch_image_bytes,
                    proxy_url=self.avito.current_proxy(),
                )
            except FORBIDDEN_ERRORS:
                logger.warning("Бот заблокирован в чате %s, прекращаю поиск", chat_id)
                return sent
            except Exception:
                logger.exception("Не удалось отправить объявление %s", item.id)
                break
            sent += 1
            if index < len(to_send) - 1:
                await asyncio.sleep(0.7)

        if len(listings) > sent:
            await self._send_text(
                chat_id,
                f"Показал <b>{sent}</b> из <b>{len(listings)}</b> подходящих. "
                "Добавьте поиск в отслеживание, чтобы получать новые автоматически.",
            )
        return sent

    async def run_forever(self) -> None:
        logger.info("Мониторинг запущен, интервал %s сек.", self.interval)
        while not self._stop.is_set():
            subscriptions = self.storage.list_subscriptions()
            # Не проверяем подписки последовательно: медленный поиск или
            # отправка нескольких объявлений в одном чате не должен отодвигать
            # такой же поиск в другом чате. Все подписки текущего раунда
            # стартуют одновременно, а внутренние блокировки защищают только
            # конкретную подписку.
            async def check_one(subscription: Subscription) -> None:
                try:
                    if subscription.initialized:
                        await self.check_subscription(subscription.id)
                    else:
                        await self.ensure_initialized(subscription.id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Ошибка при проверке подписки #%s", subscription.id)

            if subscriptions:
                await asyncio.gather(*(check_one(item) for item in subscriptions))
            try:
                # Небольшой разброс, чтобы запросы не шли строго по секундомеру.
                jitter = random.uniform(0, min(12.0, max(0.4, self.interval * 0.2)))
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval + jitter
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def _merge(search_item: Listing, detail: Listing) -> Listing:
    return replace(
        search_item,
        price=detail.price if detail.price is not None else search_item.price,
        price_text=detail.price_text if detail.price is not None else search_item.price_text,
        description=detail.description or search_item.description,
        image_urls=list(dict.fromkeys(detail.image_urls + search_item.image_urls)),
        seller_name=detail.seller_name or search_item.seller_name,
        seller_is_private=(
            detail.seller_is_private
            if detail.seller_is_private is not None
            else search_item.seller_is_private
        ),
        seller_rating=(
            detail.seller_rating
            if detail.seller_rating is not None
            else search_item.seller_rating
        ),
        seller_reviews=(
            detail.seller_reviews
            if detail.seller_reviews is not None
            else search_item.seller_reviews
        ),
        location=detail.location or search_item.location,
        published_ts=detail.published_ts or search_item.published_ts,
        published_precise=(
            detail.published_precise
            if detail.published_ts
            else search_item.published_precise
        ),
    )


def _html_text(value: str) -> str:
    from html import escape

    return escape(value)


def _html_attr(value: str) -> str:
    from html import escape

    return escape(value, quote=True)


def _html_title(value: str) -> str:
    return _html_text(value)


def _format_ts(ts: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def _plural(number: int, one: str, few: str, many: str) -> str:
    number = abs(number) % 100
    if 11 <= number <= 19:
        return many
    number %= 10
    if number == 1:
        return one
    if 2 <= number <= 4:
        return few
    return many
