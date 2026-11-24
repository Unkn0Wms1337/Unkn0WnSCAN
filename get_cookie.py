"""Собирает Cookie из авторизованной сессии Авито через Playwright.

Запуск:
    python get_cookie.py

Браузер открывается на https://www.avito.ru/, скрипт ждёт, пока ты
залогинишься вручную, затем сохраняет готовый Cookie-заголовок в файл
(по умолчанию data/avito_cookies.txt) и печатает его в stdout.

Формат для .env (AVITO_COOKIE=...) — одна строка пар "ключ=значение",
разделённых точкой с запятой, например:
    SID=...; UNIQUE=...; A=...

Переменные окружения:
    HEADLESS       — запускать браузер в headless-режиме (true/false, по умолчанию false);
    COOKIE_OUT     — путь к файлу для сохранения куки (по умолчанию data/avito_cookies.txt);
    COOKIE_TIMEOUT — максимум времени ожидания входа в секундах (по умолчанию 180).
"""

import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

AVITO_URL = "https://www.avito.ru/"
TIMEOUT_SECONDS = float(os.getenv("COOKIE_TIMEOUT", "180") or "180")


def build_cookie_header(cookies: list[dict]) -> str:
    pairs: list[str] = []
    for cookie in cookies:
        name = (cookie.get("name") or "").strip()
        value = (cookie.get("value") or "").strip()
        if not name or not value:
            continue
        if not (cookie.get("domain") or "").lower().startswith("avito"):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def main() -> int:
    headless = os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes", "on"}
    out_path = Path(os.getenv("COOKIE_OUT", "data/avito_cookies.txt"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--start-maximized"])
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        print(f"Открываем {AVITO_URL} ...")
        page.goto(AVITO_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("load", timeout=60000)
        except Exception:
            # "load" мог не наступить за отведённое время — не критично,
            # главное, что документ прогрузился достаточно для чтения куки.
            pass

        cookies: list[dict] = []
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            cookies = context.cookies()
            has_sid = any(
                (c.get("name") or "").upper() == "SID" and (c.get("value") or "").strip()
                for c in cookies
            )
            if has_sid:
                break
            page.wait_for_timeout(2000)

        context.close()
        browser.close()

    header = build_cookie_header(cookies)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header, encoding="utf-8")
    print(f"Cookie сохранены в {out_path}")
    print(header)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
