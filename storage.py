from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ERROR_NOTIFY_INTERVAL = 30 * 60


@dataclass(frozen=True)
class Subscription:
    id: int
    chat_id: int
    url: str
    title: str
    initialized: bool
    market_median: int | None
    last_checked: float | None
    last_error: str | None
    error_notified_at: float | None
    created_at: float


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    market_median INTEGER,
                    last_checked REAL,
                    last_error TEXT,
                    error_notified_at REAL,
                    created_at REAL NOT NULL,
                    UNIQUE(chat_id, url)
                );

                CREATE TABLE IF NOT EXISTS seen_listings (
                    subscription_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY (subscription_id, item_id),
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS blocked_sellers (
                    chat_id INTEGER NOT NULL,
                    seller_id TEXT NOT NULL,
                    blocked_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, seller_id)
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_chat
                    ON subscriptions(chat_id);
                CREATE INDEX IF NOT EXISTS idx_seen_subscription
                    ON seen_listings(subscription_id);
                """
            )

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            url=str(row["url"]),
            title=str(row["title"]),
            initialized=bool(row["initialized"]),
            market_median=int(row["market_median"]) if row["market_median"] is not None else None,
            last_checked=float(row["last_checked"]) if row["last_checked"] is not None else None,
            last_error=row["last_error"],
            error_notified_at=(
                float(row["error_notified_at"]) if row["error_notified_at"] is not None else None
            ),
            created_at=float(row["created_at"]),
        )

    def add_subscription(self, chat_id: int, url: str, title: str) -> tuple[Subscription, bool]:
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO subscriptions (chat_id, url, title, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, url, title, now),
            )
            created = cur.rowcount > 0
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE chat_id = ? AND url = ?",
                (chat_id, url),
            ).fetchone()
        if row is None:
            raise RuntimeError("Не удалось сохранить подписку")
        return self._row_to_subscription(row), created

    def get_subscription(self, subscription_id: int) -> Subscription | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
        return self._row_to_subscription(row) if row else None

    def update_subscription_url(
        self, subscription_id: int, url: str, title: str
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscriptions SET url = ?, title = ? WHERE id = ?",
                (url, title, subscription_id),
            )

    def list_subscriptions(self, chat_id: int | None = None) -> list[Subscription]:
        with self._lock:
            if chat_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM subscriptions ORDER BY id ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id ASC",
                    (chat_id,),
                ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def delete_subscription(self, subscription_id: int, chat_id: int | None = None) -> bool:
        with self._lock, self._conn:
            if chat_id is None:
                cur = self._conn.execute(
                    "DELETE FROM subscriptions WHERE id = ?",
                    (subscription_id,),
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM subscriptions WHERE id = ? AND chat_id = ?",
                    (subscription_id, chat_id),
                )
            self._conn.execute(
                "DELETE FROM seen_listings WHERE subscription_id = ?",
                (subscription_id,),
            )
        return cur.rowcount > 0

    def delete_chat(self, chat_id: int) -> None:
        with self._lock, self._conn:
            ids = [
                int(row[0])
                for row in self._conn.execute(
                    "SELECT id FROM subscriptions WHERE chat_id = ?",
                    (chat_id,),
                ).fetchall()
            ]
            self._conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"DELETE FROM seen_listings WHERE subscription_id IN ({placeholders})",
                    ids,
                )

    def seen_ids(self, subscription_id: int) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id FROM seen_listings WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def seen_count(self, subscription_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM seen_listings WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def max_seen_item_id(self, subscription_id: int) -> int:
        """Самый большой числовой id среди уже известных объявлений.

        Id объявлений Авито растут со временем. Всё, что меньше этого
        значения, — старые объявления, которые Авито просто переставил
        в выдаче. Их нельзя присылать как «новые».
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MAX(CAST(item_id AS INTEGER)) FROM seen_listings
                WHERE subscription_id = ? AND item_id GLOB '[0-9]*'
                """,
                (subscription_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] else 0

    def mark_seen(self, subscription_id: int, item_ids: list[str] | set[str]) -> None:
        ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not ids:
            return
        now = time.time()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_listings (subscription_id, item_id, seen_at) VALUES (?, ?, ?)",
                ((subscription_id, item_id, now) for item_id in ids),
            )

    def block_seller(self, chat_id: int, seller_id: str) -> None:
        seller_id = (seller_id or "").strip()
        if not seller_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO blocked_sellers (chat_id, seller_id, blocked_at) VALUES (?, ?, ?)",
                (chat_id, seller_id, time.time()),
            )

    def blocked_sellers(self, chat_id: int) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seller_id FROM blocked_sellers WHERE chat_id = ?", (chat_id,)
            ).fetchall()
        return {str(row["seller_id"]) for row in rows}

    def set_initialized(
        self,
        subscription_id: int,
        market_median: int | None,
        title: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock, self._conn:
            if title:
                self._conn.execute(
                    """
                    UPDATE subscriptions
                    SET initialized = 1, market_median = ?, last_checked = ?,
                        last_error = NULL, error_notified_at = NULL, title = ?
                    WHERE id = ?
                    """,
                    (market_median, now, title, subscription_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE subscriptions
                    SET initialized = 1, market_median = ?, last_checked = ?,
                        last_error = NULL, error_notified_at = NULL
                    WHERE id = ?
                    """,
                    (market_median, now, subscription_id),
                )

    def update_market(self, subscription_id: int, market_median: int | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscriptions SET market_median = ? WHERE id = ?",
                (market_median, subscription_id),
            )

    def mark_checked(self, subscription_id: int, market_median: int | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE subscriptions
                SET last_checked = ?, market_median = COALESCE(?, market_median),
                    last_error = NULL, error_notified_at = NULL
                WHERE id = ?
                """,
                (time.time(), market_median, subscription_id),
            )

    def set_error(self, subscription_id: int, message: str) -> bool:
        """Save an error. Return True when the user should be notified about it."""
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT last_error, error_notified_at FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            if row is None:
                return False
            # ВАЖНО: не шлём уведомление на каждую новую формулировку ошибки.
            # Иначе при проверке раз в секунду Telegram заваливает сообщениями.
            # Пишем не чаще раза в ERROR_NOTIFY_INTERVAL, даже если текст
            # ошибки каждый раз разный.
            should_notify = (
                row["error_notified_at"] is None
                or now - float(row["error_notified_at"]) >= ERROR_NOTIFY_INTERVAL
            )
            self._conn.execute(
                """
                UPDATE subscriptions
                SET last_error = ?,
                    error_notified_at = CASE WHEN ? THEN ? ELSE error_notified_at END
                WHERE id = ?
                """,
                (message, int(should_notify), now, subscription_id),
            )
        return should_notify

    def close(self) -> None:
        with self._lock:
            self._conn.close()
