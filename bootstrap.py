"""Первичная настройка: чтобы на новом ПК хватило `python bot.py`.

Скрипт сам:
1. создаёт локальное окружение `.venv` (если его нет);
2. доустанавливает зависимости из `requirements.txt`;
3. скачивает Chromium для Playwright;
4. создаёт `.env` из `.env.example` и спрашивает BOT_TOKEN, если он не задан;
5. перезапускает `bot.py` уже внутри `.venv`.

Модуль использует только стандартную библиотеку, поэтому его можно
импортировать до установки зависимостей.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"
ENV_FILE = PROJECT_DIR / ".env"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"
DATA_DIR = PROJECT_DIR / "data"
BOT_FILE = PROJECT_DIR / "bot.py"

_REQUIRED_IMPORTS = (
    "telethon",
    "aiohttp",
    "curl_cffi",
    "playwright",
    "dotenv",
    "PIL",
    "socks",
)
_ENV_MARKER = "UNKN0WN_BOOTSTRAPPED"
_TOKEN_PLACEHOLDER = "replace_with_your_botfather_token"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _say(message: str) -> None:
    print(f"\n[setup] {message}", flush=True)


def _run(command: list[str], *, cwd: Path | None = None, env: dict | None = None) -> int:
    try:
        return subprocess.call(
            command,
            cwd=str(cwd or PROJECT_DIR),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt" and command and "pip" in " ".join(command)
            else 0,
        )
    except FileNotFoundError:
        return 127


def _ensure_venv() -> Path:
    python = _venv_python()
    if python.exists():
        return python
    _say("Первый запуск: создаю локальное окружение .venv…")
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    code = _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    if code != 0 or not python.exists():
        _say(
            "Не удалось создать .venv. Установите Python 3.10+ и модуль venv "
            "(на Linux: sudo apt install python3-venv), затем запустите снова."
        )
        raise SystemExit(1)
    return python


def _packages_ok(python: Path) -> bool:
    check = (
        "import importlib, sys\n"
        "mods = %r\n"
        "missing = [m for m in mods if importlib.util.find_spec(m) is None]\n"
        "sys.exit(1 if missing else 0)\n" % (_REQUIRED_IMPORTS,)
    )
    try:
        return (
            subprocess.call(
                [str(python), "-c", check],
                cwd=str(PROJECT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        )
    except OSError:
        return False


def _install_requirements(python: Path) -> None:
    _say("Устанавливаю зависимости (один раз, нужен интернет)…")
    subprocess.call(
        [str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        cwd=str(PROJECT_DIR),
    )
    code = subprocess.call(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(REQUIREMENTS),
        ],
        cwd=str(PROJECT_DIR),
    )
    if code != 0:
        _say(
            "Не удалось установить зависимости. Проверьте интернет и запустите "
            "вручную:\n  "
            f"{python} -m pip install -r requirements.txt"
        )
        raise SystemExit(1)


def _playwright_browsers_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _chromium_installed() -> bool:
    root = _playwright_browsers_dir()
    if not root.exists():
        return False
    patterns = (
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    )
    return any(root.glob(pattern) for pattern in patterns)


def _ensure_chromium(python: Path) -> None:
    if _chromium_installed():
        return
    _say("Скачиваю Chromium для Playwright (один раз, ~150 МБ)…")
    code = subprocess.call(
        [str(python), "-m", "playwright", "install", "chromium"],
        cwd=str(PROJECT_DIR),
    )
    if code != 0:
        _say(
            "Chromium не установился. Позже выполните вручную:\n  "
            f"{python} -m playwright install chromium"
        )


def _env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lstrip("\ufeff")] = value.strip()
    return values


def _set_env_value(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(lambda _: replacement, text, count=1)
    return text.rstrip() + "\n" + replacement + "\n"


def _configure_env() -> None:
    if not ENV_FILE.exists():
        if not ENV_EXAMPLE.exists():
            return
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        _say("Создан .env из .env.example.")
    try:
        raw = ENV_FILE.read_bytes()
    except OSError:
        return
    if raw.startswith(b"\xef\xbb\xbf"):
        # Убираем BOM: иначе первая переменная (обычно BOT_TOKEN) не читается.
        text = raw.decode("utf-8-sig")
        ENV_FILE.write_text(text, encoding="utf-8")
    else:
        text = raw.decode("utf-8", errors="replace")
    values = _env_values(text)
    token = values.get("BOT_TOKEN", "").strip()
    if token and _TOKEN_PLACEHOLDER not in token and ":" in token:
        return
    if not sys.stdin or not sys.stdin.isatty():
        _say(
            "BOT_TOKEN не задан. Откройте .env и впишите токен от @BotFather, "
            "затем запустите бота снова."
        )
        raise SystemExit(1)
    print(
        "\n[setup] Нужен токен Telegram-бота.\n"
        "         Получите его у @BotFather командой /newbot и вставьте сюда.\n"
    )
    try:
        token = input("BOT_TOKEN: ").strip()
    except (EOFError, KeyboardInterrupt):
        token = ""
    if not token or ":" not in token:
        _say("Токен не задан — запуск отменён.")
        raise SystemExit(1)
    text = _set_env_value(text, "BOT_TOKEN", token)
    m = re.search(r"^TELEGRAM_API_ID=(.*)$", text, re.MULTILINE)
    if not m or not m.group(1).strip():
        text = _set_env_value(text, "TELEGRAM_API_ID", "2040")
        text = _set_env_value(
            text,
            "TELEGRAM_API_HASH",
            "b18441a1ff607e10a989891a5462e627",
        )
    ENV_FILE.write_text(text, encoding="utf-8")
    _say("Токен сохранён в .env.")
    if not values.get("TELEGRAM_PROXY_URL") and not values.get(
        "TELEGRAM_MT_PROXY"
    ):
        print(
            "[setup] Подсказка: если Telegram не подключается напрямую, "
            "впишите TELEGRAM_MT_PROXY или TELEGRAM_PROXY_URL в .env.\n"
        )


def ensure_environment() -> None:
    """Готовит окружение и перезапускает bot.py внутри .venv."""
    if os.environ.get(_ENV_MARKER) == "1":
        return
    if not BOT_FILE.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    python = _ensure_venv()
    if not _packages_ok(python):
        _install_requirements(python)
    _ensure_chromium(python)
    _configure_env()

    current = Path(sys.executable).resolve()
    target = python.resolve()
    if current == target:
        os.environ[_ENV_MARKER] = "1"
        return

    _say("Запускаю бота в локальном окружении…")
    env = os.environ.copy()
    env[_ENV_MARKER] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        code = subprocess.call([str(python), str(BOT_FILE)], env=env)
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    ensure_environment()
