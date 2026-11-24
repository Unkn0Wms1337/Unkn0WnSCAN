from __future__ import annotations

import re
import time
from datetime import datetime
from html import escape

from avito import Listing, format_price

CAPTION_LIMIT = 900
TITLE_LIMIT = 120
DESCRIPTION_LIMIT = 1500


def _plural_ru(number: int, one: str, few: str, many: str) -> str:
    number = abs(number) % 100
    if 11 <= number <= 19:
        return many
    number %= 10
    if number == 1:
        return one
    if 2 <= number <= 4:
        return few
    return many


def market_diff(price: int | None, market: int | None) -> int | None:
    if not price or not market or price <= 0 or market <= 0:
        return None
    return int(round((price - market) / market * 100))


def _market_line(price: int | None, market: int | None) -> str:
    diff = market_diff(price, market)
    if diff is None:
        return "⚪️ Рынок: нет данных"
    if diff <= -3:
        icon = "🟢"
    elif diff >= 3:
        icon = "🔴"
    else:
        icon = "🟡"
    if diff == 0:
        percent = "0% к рынку"
    elif diff > 0:
        percent = f"+{diff}% к рынку"
    else:
        percent = f"{diff}% к рынку"
    return f"{icon} Рынок: {format_price(market)} ({percent})"


def _seller_line(listing: Listing) -> str:
    if listing.seller_is_private is True:
        seller = "частное лицо"
    elif listing.seller_name:
        seller = listing.seller_name
    elif listing.seller_is_private is False:
        seller = "компания"
    else:
        seller = "не указан"
    completed = listing.seller_completed
    if completed:
        seller = f"{seller} (завершённых: {completed})"
    return f"👤 Продавец: {escape(seller)}"


def _rating_line(listing: Listing) -> str:
    reviews = listing.seller_reviews
    rating = listing.seller_rating
    word = _plural_ru(reviews, "отзыв", "отзыва", "отзывов") if reviews else "отзывов"
    if rating and reviews:
        return f"⭐ Рейтинг: {rating:.1f} - {reviews} {word}"
    if rating:
        return f"⭐ Рейтинг: {rating:.1f}"
    if reviews:
        return f"⭐ Рейтинг: {reviews} {word}"
    return "⭐ Рейтинг: нет отзывов"


def _notes(listing: Listing, market: int | None) -> str:
    diff = market_diff(listing.price, market)
    if diff is None:
        return "нет данных для оценки рынка"
    if diff <= -15:
        return f"дешевле рынка на {abs(diff)}%"
    if diff < -3:
        return "немного дешевле рынка"
    if diff >= 15:
        return f"дороже рынка на {diff}%"
    if diff > 3:
        return "немного дороже рынка"
    return "цена на уровне рынка"


def _escape_limited(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    result: list[str] = []
    used = 0
    for char in text:
        piece = escape(char, quote=False)
        if used + len(piece) > limit:
            result.append("…")
            break
        result.append(piece)
        used += len(piece)
    return "".join(result)


def _published_line(listing: Listing) -> str | None:
    ts = listing.published_ts
    if not ts:
        return None
    diff = max(0, int(time.time()) - ts)
    if listing.published_precise:
        if diff < 60:
            return "🕒 Опубликовано: только что"
        if diff < 3600:
            return f"🕒 Опубликовано: {diff // 60} мин назад"
        if diff < 86400:
            return f"🕒 Опубликовано: {diff // 3600} ч назад"
        return "🕒 Опубликовано: " + datetime.fromtimestamp(ts).strftime("%d.%m в %H:%M")
    return "🕒 Опубликовано: " + datetime.fromtimestamp(ts).strftime("%d.%m")


def format_listing_caption(listing: Listing, market_median: int | None = None) -> str:
    title = listing.title.strip() or "Без названия"
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 1].rstrip() + "…"
    title_html = escape(title)
    url_html = escape(listing.url, quote=True)

    price = listing.price
    price_text = format_price(price) if price else (listing.price_text or "Цена не указана")

    header = "\n".join(
        [
            f'📦 <a href="{url_html}">{title_html}</a>',
            f"💳 Цена: {escape(price_text)}",
            _seller_line(listing),
            _rating_line(listing),
        ]
    )

    description = listing.description.strip()
    description = re.sub(r"[ \t]+\n", "\n", description)
    description = re.sub(r"\n{3,}", "\n\n", description)
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    if not description:
        description = "не указано"

    label = "\n📝 "
    # Описание оформляем цитатой — так же, как в референсе.
    start = "<blockquote>"
    end = "</blockquote>"
    budget = CAPTION_LIMIT - len(header) - len(label) - len(start) - len(end) - 2
    body = _escape_limited(description, max(0, budget))
    return f"{header}{label}{start}{body}{end}"
