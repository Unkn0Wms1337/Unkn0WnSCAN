from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from datetime import datetime
from html import escape

from telethon import TelegramClient, events
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.tl import functions, types
from telethon.tl.custom import Button

from avito import (
    AvitoBlockedError,
    AvitoClient,
    AvitoError,
    InvalidAvitoUrl,
    NotSearchUrl,
    build_search_url,
    extract_first_url,
    is_avito_url,
    normalize_avito_url,
    search_title,
    to_all_russia,
)
from config import Config, load_config
from monitor import Monitor
from storage import Storage, Subscription

logger = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS_PER_CHAT = 20


def _html(value: str) -> str:
    return escape(value, quote=False)


def _html_attr(value: str) -> str:
    return escape(value, quote=True)


def _short_title(subscription: Subscription) -> str:
    title = subscription.title.strip() or f"Поиск #{subscription.id}"
    if len(title) > 36:
        title = title[:35].rstrip() + "…"
    return title


def _status_line(subscription: Subscription) -> str:
    if not subscription.initialized:
        return "⏳ первичная проверка"
    if subscription.last_error:
        return "⚠️ последняя проверка не удалась"
    if subscription.last_checked:
        moment = datetime.fromtimestamp(subscription.last_checked).strftime("%d.%m %H:%M")
        return f"✅ проверено {moment}"
    return "✅ активен"


def _parse_command(text: str) -> tuple[str | None, str]:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None, stripped
    pieces = stripped.split(maxsplit=1)
    command = pieces[0].split("@", 1)[0].lower()
    args = pieces[1].strip() if len(pieces) > 1 else ""
    return command, args


async def set_commands(client: TelegramClient) -> None:
    await client(
        functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=[
                types.BotCommand(command="start", description="Как пользоваться ботом"),
                types.BotCommand(command="help", description="Помощь"),
                types.BotCommand(command="list", description="Мои поиски"),
                types.BotCommand(command="add", description="Добавить ссылку на поиск"),
                types.BotCommand(command="search", description="Найти прямо сейчас по слову"),
                types.BotCommand(command="del", description="Удалить поиск"),
                types.BotCommand(command="check", description="Проверить сейчас"),
                types.BotCommand(command="retry", description="Сбросить паузу Avito и проверить"),
            ],
        )
    )


def register_handlers(
    client: TelegramClient,
    storage: Storage,
    monitor: Monitor,
    all_russia: bool = False,
) -> None:
    background_tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            background_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error:
                logger.error("Фоновая задача завершилась с ошибкой", exc_info=error)

        task.add_done_callback(done)

    async def reply(event, text: str, buttons=None) -> None:
        await event.respond(
            text,
            parse_mode="html",
            link_preview=False,
            buttons=buttons,
        )

    async def add_search(event, raw_url: str) -> None:
        chat_id = event.chat_id
        try:
            url = normalize_avito_url(raw_url)
        except NotSearchUrl as exc:
            await reply(event, f"⚠️ {_html(str(exc))}")
            return
        except InvalidAvitoUrl as exc:
            await reply(
                event,
                f"⚠️ {_html(str(exc))}\n\n"
                "Нужна ссылка на <b>результаты поиска</b> Авито, например:\n"
                "<code>https://www.avito.ru/belgorod?q=xbox</code>",
            )
            return

        if all_russia:
            url = to_all_russia(url)

        existing = storage.list_subscriptions(chat_id)
        if len(existing) >= MAX_SUBSCRIPTIONS_PER_CHAT and all(
            subscription.url != url for subscription in existing
        ):
            await reply(
                event,
                f"⚠️ Достигнут лимит: {MAX_SUBSCRIPTIONS_PER_CHAT} поисков. "
                "Удалите ненужный через /list и /del.",
            )
            return

        title = search_title(url)
        subscription, created = storage.add_subscription(chat_id, url, title)
        if not created:
            if not subscription.initialized:
                spawn(monitor.ensure_initialized(subscription.id))
                await reply(event, "⏳ Этот поиск уже добавлен, завершаю первичную проверку.")
            else:
                await reply(
                    event,
                    f"ℹ️ Этот поиск уже отслеживается: <b>{_html(subscription.title)}</b>\n"
                    "Новые объявления придут автоматически.",
                )
            return

        await reply(
            event,
            "✅ <b>Добавил поиск</b>\n\n"
            f"🔗 {_html(title)}\n\n"
            "Сейчас посмотрю текущие объявления, чтобы запомнить их. "
            "Дальше пришлю только новые.",
        )
        spawn(monitor.ensure_initialized(subscription.id))

    async def show_start(event) -> None:
        await reply(
            event,
            "👋 <b>Привет! Я слежу за новыми объявлениями на Авито.</b>\n\n"
            "Пришлите мне ссылку на <b>результаты поиска</b> (не на отдельное объявление) "
            "с нужными фильтрами — я запомню текущие объявления и буду присылать только новые.\n\n"
            "Как взять ссылку: откройте Авито → задайте фильтры → скопируйте адрес из строки браузера.\n\n"
            "<b>Команды:</b>\n"
            "/search <i>запрос</i> — найти прямо сейчас по слову\n"
            "/list — мои поиски\n"
            "/add <i>ссылка</i> — добавить поиск\n"
            "/del <i>номер</i> — удалить поиск\n"
            "/check <i>номер</i> — проверить сейчас\n\n"
            "Можно просто отправить ссылку или написать запрос текстом.",
        )

    async def show_help(event) -> None:
        await reply(
            event,
            "<b>Что я умею</b>\n\n"
            "• Ищу объявления по слову прямо сейчас: <code>/search iphone 13</code>.\n"
            "• Слежу за новыми объявлениями по ссылке на поиск Авито.\n"
            "• Присылаю карточку с ценой, оценкой рынка, продавцом, рейтингом и фото.\n"
            "• Первый раз запоминаю уже существующие объявления, чтобы не спамить старыми.\n"
            "• Фото временно скачиваются, отправляются и сразу удаляются с диска.\n\n"
            "<b>Команды:</b>\n"
            "/search <i>запрос</i> — найти сейчас\n"
            "/add <i>ссылка</i> — новый поиск\n"
            "/list — список поисков и кнопки удаления\n"
            "/del <i>номер</i> — удалить\n"
            "/check <i>номер</i> — проверить без ожидания\n"
            "/retry — сбросить паузу Авито и проверить сейчас (после смены IP)\n\n"
            "Если Авито временно ограничил запросы с вашего IP, бот подождёт и повторит автоматически.",
        )

    async def show_list(event) -> None:
        subscriptions = storage.list_subscriptions(event.chat_id)
        if not subscriptions:
            await reply(
                event,
                "Пока нет активных поисков. Пришлите ссылку на результаты поиска Авито.",
            )
            return

        lines = ["📋 <b>Ваши поиски Avito</b>", ""]
        buttons: list[list[Button]] = []
        for subscription in subscriptions[:20]:
            lines.append(
                f"<b>#{subscription.id}</b> {_html(subscription.title)}\n"
                f"   {_status_line(subscription)} · было объявлений: {storage.seen_count(subscription.id)}"
            )
            buttons.append(
                [
                    Button.inline(
                        f"🗑 #{subscription.id} {_short_title(subscription)}",
                        data=f"del:{subscription.id}".encode(),
                    )
                ]
            )
        if len(subscriptions) > 20:
            lines.append(f"\n…и ещё {len(subscriptions) - 20}")
        lines.append("\nУдалить можно кнопкой ниже или командой <code>/del номер</code>.")
        await reply(event, "\n".join(lines), buttons=buttons)

    async def delete_search(event, args: str) -> None:
        match = re.search(r"\d+", (args or "").strip().lstrip("#"))
        if not match:
            await reply(event, "Укажите номер поиска: <code>/del 1</code>")
            return
        subscription_id = int(match.group(0))
        if storage.delete_subscription(subscription_id, event.chat_id):
            await reply(event, f"🗑 Поиск #{subscription_id} удалён.")
        else:
            await reply(event, "Не нашёл такой поиск в этом чате. Посмотрите /list.")

    async def check_search(event, args: str) -> None:
        subscriptions = storage.list_subscriptions(event.chat_id)
        if not subscriptions:
            await reply(event, "Нет активных поисков. Пришлите ссылку на поиск Авито.")
            return
        match = re.search(r"\d+", (args or "").strip().lstrip("#"))
        if match:
            subscription_id = int(match.group(0))
            subscription = next(
                (item for item in subscriptions if item.id == subscription_id),
                None,
            )
        elif len(subscriptions) == 1:
            subscription = subscriptions[0]
        else:
            await reply(
                event,
                "Укажите номер поиска: <code>/check 1</code>. Номера есть в /list.",
            )
            return
        if subscription is None:
            await reply(event, "Не нашёл такой поиск в этом чате.")
            return
        await reply(event, f"🔄 Проверяю поиск #{subscription.id}…")
        spawn(monitor.check_subscription(subscription.id))

    async def retry_all(event) -> None:
        monitor.avito.clear_block()
        subscriptions = storage.list_subscriptions(event.chat_id)
        if not subscriptions:
            await reply(
                event,
                "Нет сохранённых поисков. Пришлите ссылку или напишите запрос.",
            )
            return
        await reply(event, "🔄 Сбросил паузу и проверяю поиски…")
        for subscription in subscriptions:
            spawn(monitor.check_subscription(subscription.id))

    async def run_instant_search(event, query: str) -> None:
        query = (query or "").strip()
        if len(query) < 2:
            await reply(
                event,
                "Напишите, что искать, например:\n<code>/search iphone 13</code>",
            )
            return
        url = build_search_url(query)
        chat_id = event.chat_id
        await reply(event, f"🔎 Ищу «{_html(query)}» по всей России…")

        async def worker() -> None:
            try:
                await monitor.search_now(chat_id, url, limit=8)
            except AvitoBlockedError as exc:
                await reply(event, f"⏳ {_html(str(exc))}")
            except AvitoError as exc:
                await reply(event, f"⚠️ Не удалось выполнить поиск: {_html(str(exc))}")
            except Exception:
                logger.exception("Ошибка при поиске «%s»", query)
                await reply(event, "⚠️ Ошибка при поиске. Попробуйте позже.")

        spawn(worker())

    @client.on(events.NewMessage())
    async def on_message(event: events.NewMessage.Event) -> None:
        text = (event.raw_text or "").strip()
        command, args = _parse_command(text)

        if command in {"/start", "/help"}:
            await (show_start(event) if command == "/start" else show_help(event))
            return
        if command == "/add":
            raw = extract_first_url(args) or extract_first_url(text)
            if not raw:
                await reply(
                    event,
                    "Пришлите ссылку вместе с командой, например:\n"
                    "<code>/add https://www.avito.ru/belgorod?q=xbox</code>",
                )
                return
            await add_search(event, raw)
            return
        if command == "/list":
            await show_list(event)
            return
        if command == "/del":
            await delete_search(event, args)
            return
        if command == "/check":
            await check_search(event, args)
            return
        if command == "/search":
            await run_instant_search(event, args)
            return
        if command == "/retry":
            await retry_all(event)
            return
        if command:
            await reply(event, "Не знаю такую команду. Посмотрите /help.")
            return

        raw = extract_first_url(text)
        if raw:
            if is_avito_url(raw):
                await add_search(event, raw)
            else:
                await reply(
                    event,
                    "Нужна ссылка именно на Авито. Откройте результаты поиска и скопируйте адрес.",
                )
            return
        # Обычный текст без ссылки считаем поисковым запросом.
        if text:
            await run_instant_search(event, text)
            return
        await reply(event, "Пришлите ссылку на результаты поиска Авито или используйте /help.")

    @client.on(events.CallbackQuery())
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        data = (event.data or b"").decode("utf-8", "replace")
        if data.startswith("block:"):
            seller_id = data.split(":", 1)[1]
            storage.block_seller(event.chat_id, seller_id)
            await event.answer("Продавец заблокирован")
            with contextlib.suppress(Exception):
                await event.delete()
            return
        if not data.startswith("del:"):
            await event.answer()
            return
        try:
            subscription_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await event.answer("Некорректная кнопка", alert=True)
            return
        deleted = storage.delete_subscription(subscription_id, event.chat_id)
        await event.answer("Удалено" if deleted else "Уже удалено")
        if deleted:
            with contextlib.suppress(Exception):
                await event.edit(
                    f"🗑 Поиск #{subscription_id} удалён.\n\n"
                    "Список обновлён. Посмотреть остальные: /list",
                    parse_mode="html",
                )


def _telegram_proxy_dict(url: str) -> dict:
    """Convert socks5://user:pass@host:port (or http://) to a Telethon proxy dict."""
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url if "://" in url else f"socks5://{url}")
    scheme = parts.scheme.lower()
    if scheme not in {"socks5", "socks5h", "socks4", "http"}:
        raise ValueError(f"Неподдерживаемый прокси для Telegram: {scheme}")
    if not parts.hostname:
        raise ValueError(f"В TELEGRAM_PROXY_URL нет хоста: {url}")
    proxy: dict = {
        "proxy_type": "socks5" if scheme.startswith("socks5") else scheme,
        "addr": parts.hostname,
        "port": parts.port or 1080,
        "rdns": True,
    }
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


def build_telegram_client(config: Config) -> TelegramClient:
    kwargs: dict = {
        "device_model": "Avito Monitor",
        "system_version": "Windows 10",
        "app_version": "1.0",
        "request_retries": 5,
        "connection_retries": 5,
    }
    if config.telegram_proxy_url:
        # SOCKS5/HTTP-прокси: используется для всех соединений Telethon.
        kwargs["proxy"] = _telegram_proxy_dict(config.telegram_proxy_url)
    elif (
        config.mt_proxy_server
        and config.mt_proxy_port
        and config.mt_proxy_secret
    ):
        kwargs["connection"] = ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (
            config.mt_proxy_server,
            config.mt_proxy_port,
            config.mt_proxy_secret,
        )
    return TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        **kwargs,
    )


async def run(config: Config) -> None:
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(config.db_path)
    avito_client = AvitoClient(
        proxy_url=config.proxy_url,
        proxy_urls=config.avito_proxy_urls,
        cookie=config.avito_cookie,
        request_delay=config.request_delay,
        timeout=config.avito_timeout,
        fallback_direct=config.avito_fallback_direct,
        location_id=config.avito_location_id,
        use_browser=config.avito_use_browser,
        browser_headless=config.avito_browser_headless,
        browser_profile=str(config.avito_browser_profile),
        all_russia=config.avito_all_russia,
        state_path=str(config.avito_state_path),
        watch_reload=config.avito_reload_seconds,
        browser_proxy=config.avito_browser_proxy,
        rotate_on_block=config.avito_rotate_on_block,
        block_pause_minutes=config.avito_block_pause_minutes,
        auto_proxy=config.avito_auto_proxy,
        proxy_cache_path=str(config.avito_proxy_cache),
        proxy_scan_limit=config.avito_proxy_scan_limit,
        proxy_scan_workers=config.avito_proxy_scan_workers,
        warp_hop=config.avito_warp_hop,
        warp_primary=config.avito_warp_primary,
        warp_proxy=config.avito_warp_proxy,
        warp_max_hops=config.avito_warp_max_hops,
    )
    client = build_telegram_client(config)
    monitor: Monitor | None = None
    monitor_task: asyncio.Task | None = None
    try:
        await client.start(bot_token=config.bot_token)
        me = await client.get_me()
        logger.info("Бот @%s подключён", me.username or me.id)

        # Переводим старые подписки на поиск по всей России.
        if config.avito_all_russia:
            for subscription in storage.list_subscriptions():
                new_url = to_all_russia(subscription.url)
                if new_url != subscription.url:
                    storage.update_subscription_url(
                        subscription.id, new_url, search_title(new_url)
                    )
                    logger.info(
                        "Подписка #%s переведена на поиск по всей России",
                        subscription.id,
                    )

        monitor = Monitor(
            client,
            storage,
            avito_client,
            interval=config.check_interval,
            initial_pages=config.initial_pages,
            check_pages=config.check_pages,
            max_new_per_check=config.max_new_per_check,
            fetch_details=config.fetch_details,
            fresh_hours=config.avito_fresh_hours,
        )
        register_handlers(client, storage, monitor, all_russia=config.avito_all_russia)
        with contextlib.suppress(Exception):
            await set_commands(client)

        # Если есть кэш рабочих прокси с прошлой блокировки — включаем его
        # до первой проверки, чтобы не тратить время на заблокированный IP.
        with contextlib.suppress(Exception):
            await avito_client.preload_cached_proxies()

        monitor_task = asyncio.create_task(monitor.run_forever())
        logger.info("Подписок в базе: %s", len(storage.list_subscriptions()))
        await client.run_until_disconnected()
    finally:
        if monitor:
            monitor.stop()
        if monitor_task:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
        await avito_client.close()
        storage.close()
        await client.disconnect()


def _acquire_instance_lock(path):
    """Не даёт запустить второй экземпляр: иначе Telethon-сессия ломается.

    Возвращает открытый файл (держит блокировку), False — если уже занято,
    или None — если платформа не поддерживает msvcrt/fcntl.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - Linux/macOS
        try:
            import fcntl
        except ImportError:
            return None
        handle = open(path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    handle = open(path, "a+")  # noqa: SIM115
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> None:
    import time

    config = load_config()
    log_path = config.db_path.parent / "bot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    # Если прокси/сеть временно пропали, Telethon завершает работу.
    # Не выходим, а перезапускаемся с паузой.
    instance_lock = _acquire_instance_lock(config.db_path.parent / "bot.lock")
    if instance_lock is False:
        logger.error(
            "Бот уже запущен (data/bot.lock). Закройте второй экземпляр — "
            "иначе Telegram отзовёт ключ сессии."
        )
        return
    while True:
        try:
            asyncio.run(run(config))
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception:
            logger.exception("Бот остановился с ошибкой, перезапуск через 30 сек.")
            time.sleep(30)
            continue
        logger.warning("Соединение закрыто, перезапуск через 10 сек.")
        time.sleep(10)


if __name__ == "__main__":
    main()
