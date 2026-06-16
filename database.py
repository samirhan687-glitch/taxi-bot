"""
Taxi Bot — Database layer (SQLite)
"""

import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "taxi.db"


class Database:
    """Async-compatible SQLite database wrapper."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language_code TEXT,
                referred_by INTEGER,
                registered_at INTEGER DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS drivers (
                user_id INTEGER PRIMARY KEY,
                phone TEXT,
                car_number TEXT,
                car_model TEXT DEFAULT '',
                available INTEGER DEFAULT 1,
                rating_avg REAL DEFAULT 0.0,
                total_rides INTEGER DEFAULT 0,
                registered_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                order_type TEXT NOT NULL CHECK(order_type IN ('passenger', 'driver')),
                from_location TEXT,
                to_location TEXT,
                seats INTEGER DEFAULT 4,
                price INTEGER DEFAULT 0,
                departure_time TEXT,
                message_id INTEGER,
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'closed', 'matched', 'completed', 'cancelled')),
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                closed_at INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0,
                passengers_count INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('credit', 'debit')),
                description TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                chat_title TEXT,
                base_location TEXT DEFAULT 'Qizilqosh',
                auto_delete INTEGER DEFAULT 1,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                passenger_id INTEGER NOT NULL,
                driver_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'completed')),
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (passenger_id) REFERENCES users(user_id),
                FOREIGN KEY (driver_id) REFERENCES users(user_id),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                aliases TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS active_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                location_message_id INTEGER,
                is_live INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS spam_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                rater_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment TEXT DEFAULT '',
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                bonus_given INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_chat ON orders(chat_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_spam_log_user_time ON spam_log(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_ratings_target ON ratings(target_id);
            CREATE INDEX IF NOT EXISTS idx_ratings_order ON ratings(order_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
        """)

        conn.commit()
        conn.close()

    # ─── Users ────────────────────────────────────────────────

    def upsert_user(self, user_id: int, username: str = None,
                    first_name: str = None, language_code: str = None,
                    referred_by: int = None):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO users (user_id, username, first_name, language_code, referred_by)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username = COALESCE(excluded.username, users.username),
                   first_name = COALESCE(excluded.first_name, users.first_name),
                   language_code = COALESCE(excluded.language_code, users.language_code)
            """,
            (user_id, username, first_name, language_code, referred_by),
        )
        conn.commit()
        conn.close()

    def get_user(self, user_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_all_users(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM users ORDER BY registered_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ─── Drivers ──────────────────────────────────────────────

    def register_driver(self, user_id: int, phone: str, car_number: str,
                        car_model: str = "", available: int = 1):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO drivers (user_id, phone, car_number, car_model, available)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   phone = excluded.phone,
                   car_number = excluded.car_number,
                   car_model = COALESCE(excluded.car_model, drivers.car_model)
            """,
            (user_id, phone, car_number, car_model, available),
        )
        conn.commit()
        conn.close()

    def update_driver(self, user_id: int, **kwargs):
        conn = self._get_conn()
        for key, value in kwargs.items():
            conn.execute(f"UPDATE drivers SET {key} = ? WHERE user_id = ?", (value, user_id))
        conn.commit()
        conn.close()

    def is_driver(self, user_id: int) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM drivers WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return row is not None

    def is_driver_available(self, user_id: int) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT available FROM drivers WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return bool(row["available"]) if row else False

    def set_driver_available(self, user_id: int, available: bool):
        conn = self._get_conn()
        conn.execute(
            "UPDATE drivers SET available = ? WHERE user_id = ?",
            (int(available), user_id),
        )
        conn.commit()
        conn.close()

    def get_driver(self, user_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM drivers WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_all_drivers(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT d.*, u.first_name, u.username FROM drivers d JOIN users u ON d.user_id = u.user_id ORDER BY d.rating_avg DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_available_drivers(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT d.*, u.first_name, u.username FROM drivers d JOIN users u ON d.user_id = u.user_id WHERE d.available = 1 ORDER BY d.rating_avg DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ─── Orders ───────────────────────────────────────────────

    def create_order(
        self,
        user_id: int,
        chat_id: int,
        order_type: str,
        from_location: str,
        to_location: str,
        seats: int = 4,
        price: int = 0,
        departure_time: str = None,
        message_id: int = None,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO orders
               (user_id, chat_id, order_type, from_location, to_location, seats, price, departure_time, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, chat_id, order_type, from_location, to_location, seats, price, departure_time, message_id),
        )
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return order_id

    def get_active_order(
        self, user_id: int, chat_id: int, order_type: str
    ) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM orders
               WHERE user_id = ? AND chat_id = ? AND order_type = ? AND status = 'active'
               ORDER BY id DESC LIMIT 1
            """,
            (user_id, chat_id, order_type),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_order(self, order_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_active_orders_by_chat(self, chat_id: int) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM orders
               WHERE chat_id = ? AND status = 'active'
               ORDER BY id DESC
            """,
            (chat_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_user_orders(self, user_id: int, limit: int = 10) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM orders
               WHERE user_id = ?
               ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_order(self, order_id: int, **kwargs):
        conn = self._get_conn()
        for key, value in kwargs.items():
            conn.execute(f"UPDATE orders SET {key} = ? WHERE id = ?", (value, order_id))
        conn.commit()
        conn.close()

    def close_order(self, order_id: int):
        conn = self._get_conn()
        conn.execute(
            """UPDATE orders SET status = 'closed', closed_at = strftime('%s', 'now')
               WHERE id = ?
            """,
            (order_id,),
        )
        conn.commit()
        conn.close()

    def cancel_order(self, order_id: int):
        conn = self._get_conn()
        conn.execute(
            """UPDATE orders SET status = 'cancelled', closed_at = strftime('%s', 'now')
               WHERE id = ?
            """,
            (order_id,),
        )
        conn.commit()
        conn.close()

    def complete_order(self, order_id: int):
        conn = self._get_conn()
        conn.execute(
            """UPDATE orders SET status = 'completed', closed_at = strftime('%s', 'now')
               WHERE id = ?
            """,
            (order_id,),
        )
        # Increment driver total_rides
        order = conn.execute("SELECT user_id, order_type FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order and order["order_type"] == "driver":
            conn.execute(
                "UPDATE drivers SET total_rides = total_rides + 1 WHERE user_id = ?",
                (order["user_id"],),
            )
        conn.commit()
        conn.close()

    def close_expired_orders(self, max_age_seconds: int = 3600) -> list[int]:
        """Close orders older than max_age_seconds. Returns closed order IDs."""
        cutoff = int(time.time()) - max_age_seconds
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id FROM orders
               WHERE status = 'active' AND created_at < ?
            """,
            (cutoff,),
        ).fetchall()
        order_ids = [r["id"] for r in rows]
        if order_ids:
            conn.execute(
                """UPDATE orders SET status = 'closed', closed_at = strftime('%s', 'now')
                   WHERE id IN ({})
                """.format(",".join("?" * len(order_ids))),
                order_ids,
            )
            conn.commit()
        conn.close()
        return order_ids

    def decrement_seats(self, order_id: int) -> int:
        """Decrement seats by 1. Returns new seats count."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE orders SET seats = seats - 1 WHERE id = ?", (order_id,)
        )
        row = conn.execute(
            "SELECT seats FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.commit()
        conn.close()
        return row["seats"] if row else 0

    # ─── Balances ─────────────────────────────────────────────

    def ensure_balance(self, user_id: int):
        conn = self._get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO balances (user_id, balance) VALUES (?, 0)""",
            (user_id,),
        )
        conn.commit()
        conn.close()

    def get_balance(self, user_id: int) -> dict:
        self.ensure_balance(user_id)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM balances WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else {"balance": 0, "total_earned": 0, "total_spent": 0, "passengers_count": 0}

    def add_balance(self, user_id: int, amount: int, description: str = "Admin to'ldirish"):
        self.ensure_balance(user_id)
        conn = self._get_conn()
        conn.execute(
            "UPDATE balances SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?",
            (amount, amount, user_id),
        )
        conn.execute(
            """INSERT INTO transactions (user_id, amount, type, description)
               VALUES (?, ?, 'credit', ?)
            """,
            (user_id, amount, description),
        )
        conn.commit()
        conn.close()

    def deduct_for_passenger(self, user_id: int, cost: int = 1000) -> bool:
        """Deduct balance for a passenger. Returns True if successful."""
        self.ensure_balance(user_id)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT balance FROM balances WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or row["balance"] < cost:
            conn.close()
            return False

        conn.execute(
            """UPDATE balances
               SET balance = balance - ?,
                   total_spent = total_spent + ?,
                   passengers_count = passengers_count + 1
               WHERE user_id = ?
            """,
            (cost, cost, user_id),
        )
        conn.execute(
            """INSERT INTO transactions (user_id, amount, type, description)
               VALUES (?, ?, 'debit', 'Yo\'lovchi uchun to\'lov')
            """,
            (user_id, cost),
        )
        conn.commit()
        conn.close()
        return True

    def get_transactions(self, user_id: int, limit: int = 10) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ─── Ratings ──────────────────────────────────────────────

    def add_rating(self, order_id: int, rater_id: int, target_id: int,
                   rating: int, comment: str = ""):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO ratings (order_id, rater_id, target_id, rating, comment)
               VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, rater_id, target_id, rating, comment),
        )
        # Update driver average rating
        avg = conn.execute(
            """SELECT AVG(rating) as avg, COUNT(*) as cnt FROM ratings WHERE target_id = ?""",
            (target_id,),
        ).fetchone()
        if avg:
            conn.execute(
                "UPDATE drivers SET rating_avg = ? WHERE user_id = ?",
                (round(avg["avg"], 1), target_id),
            )
        conn.commit()
        conn.close()

    def get_rating_for_order(self, order_id: int, rater_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM ratings WHERE order_id = ? AND rater_id = ?",
            (order_id, rater_id),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_avg_rating(self, user_id: int) -> float:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT AVG(rating) as avg FROM ratings WHERE target_id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
        return round(row["avg"], 1) if row and row["avg"] else 0.0

    def get_user_ratings(self, user_id: int, limit: int = 5) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT r.*, u.first_name as rater_name FROM ratings r
               JOIN users u ON r.rater_id = u.user_id
               WHERE r.target_id = ? ORDER BY r.id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ─── Referrals ────────────────────────────────────────────

    def add_referral(self, referrer_id: int, referred_id: int) -> bool:
        conn = self._get_conn()
        # Check if already referred
        existing = conn.execute(
            "SELECT 1 FROM referrals WHERE referred_id = ?",
            (referred_id,),
        ).fetchone()
        if existing:
            conn.close()
            return False

        conn.execute(
            """INSERT INTO referrals (referrer_id, referred_id)
               VALUES (?, ?)
            """,
            (referrer_id, referred_id),
        )

        # Give bonus to both
        REFERRAL_BONUS = 2000
        self.ensure_balance(referrer_id)
        self.ensure_balance(referred_id)
        conn.execute(
            "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
            (REFERRAL_BONUS, referrer_id),
        )
        conn.execute(
            """INSERT INTO transactions (user_id, amount, type, description)
               VALUES (?, ?, 'credit', 'Referal bonusi')
            """,
            (referrer_id, REFERRAL_BONUS),
        )
        conn.execute(
            "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
            (REFERRAL_BONUS, referred_id),
        )
        conn.execute(
            """INSERT INTO transactions (user_id, amount, type, description)
               VALUES (?, ?, 'credit', 'Referal bonusi')
            """,
            (referred_id, REFERRAL_BONUS),
        )
        conn.execute(
            "UPDATE referrals SET bonus_given = ? WHERE referrer_id = ? AND referred_id = ?",
            (REFERRAL_BONUS, referrer_id, referred_id),
        )
        conn.commit()
        conn.close()
        return True

    def get_referrals(self, user_id: int) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT r.*, u.first_name as referred_name FROM referrals r
               JOIN users u ON r.referred_id = u.user_id
               WHERE r.referrer_id = ?""",
            (user_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_referral_count(self, user_id: int) -> int:
        conn = self._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ?",
            (user_id,),
        ).fetchone()["cnt"]
        conn.close()
        return count

    # ─── Contacts ──────────────────────────────────────────────

    def add_contact(self, passenger_id: int, driver_id: int, order_id: int):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO contacts (passenger_id, driver_id, order_id)
               VALUES (?, ?, ?)
            """,
            (passenger_id, driver_id, order_id),
        )
        conn.commit()
        conn.close()

    def get_contacts_for_order(self, order_id: int) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM contacts WHERE order_id = ?", (order_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def complete_contact(self, order_id: int):
        conn = self._get_conn()
        conn.execute(
            "UPDATE contacts SET status = 'completed' WHERE order_id = ?",
            (order_id,),
        )
        conn.commit()
        conn.close()

    def save_connection(
        self, user_id: int, chat_id: int, location_message_id: int = None,
        is_live: bool = False
    ):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO active_connections (user_id, chat_id, location_message_id, is_live)
               VALUES (?, ?, ?, ?)
            """,
            (user_id, chat_id, location_message_id, int(is_live)),
        )
        conn.commit()
        conn.close()

    # ─── Group Settings ───────────────────────────────────────

    def get_group_settings(self, chat_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def save_group_settings(
        self, chat_id: int, chat_title: str, base_location: str
    ):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO group_settings (chat_id, chat_title, base_location)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   chat_title = excluded.chat_title,
                   base_location = excluded.base_location
            """,
            (chat_id, chat_title, base_location),
        )
        conn.commit()
        conn.close()

    # ─── Spam Log ─────────────────────────────────────────────

    def check_spam(self, user_id: int, chat_id: int, window_seconds: int = 120, max_count: int = 3) -> bool:
        """Returns True if spam detected (too many messages in window)."""
        cutoff = int(time.time()) - window_seconds
        conn = self._get_conn()
        count = conn.execute(
            """SELECT COUNT(*) as cnt FROM spam_log
               WHERE user_id = ? AND chat_id = ? AND created_at > ?
            """,
            (user_id, chat_id, cutoff),
        ).fetchone()["cnt"]
        conn.execute(
            """INSERT INTO spam_log (user_id, chat_id) VALUES (?, ?)""",
            (user_id, chat_id),
        )
        conn.commit()
        conn.close()
        return count >= max_count

    # ─── Stats ────────────────────────────────────────────────

    def get_stats(self) -> dict:
        conn = self._get_conn()
        users_count = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
        drivers_count = conn.execute("SELECT COUNT(*) as cnt FROM drivers").fetchone()["cnt"]
        active_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE status = 'active'"
        ).fetchone()["cnt"]
        completed_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE status = 'completed'"
        ).fetchone()["cnt"]
        total_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders"
        ).fetchone()["cnt"]
        total_revenue = conn.execute(
            "SELECT COALESCE(SUM(total_spent), 0) as total FROM balances"
        ).fetchone()["total"]
        today_start = int(time.time()) - 86400
        today_transactions = conn.execute(
            "SELECT COUNT(*) as cnt FROM transactions WHERE created_at > ?",
            (today_start,),
        ).fetchone()["cnt"]
        today_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE created_at > ?",
            (today_start,),
        ).fetchone()["cnt"]
        avg_rating = conn.execute(
            "SELECT COALESCE(AVG(rating), 0) as avg FROM ratings"
        ).fetchone()["avg"]
        conn.close()
        return {
            "users_count": users_count,
            "drivers_count": drivers_count,
            "active_orders": active_orders,
            "completed_orders": completed_orders,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "today_transactions": today_transactions,
            "today_orders": today_orders,
            "avg_rating": round(avg_rating, 1) if avg_rating else 0.0,
        }

    def get_user_stats(self, user_id: int) -> dict:
        """Get detailed stats for a single user."""
        conn = self._get_conn()
        total_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE user_id = ?",
            (user_id,),
        ).fetchone()["cnt"]
        active_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()["cnt"]
        completed_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        ).fetchone()["cnt"]
        balance = conn.execute(
            "SELECT * FROM balances WHERE user_id = ?", (user_id,)
        ).fetchone()
        rating_avg = conn.execute(
            "SELECT AVG(rating) as avg FROM ratings WHERE target_id = ?",
            (user_id,),
        ).fetchone()["avg"]
        referrals = conn.execute(
            "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ?",
            (user_id,),
        ).fetchone()["cnt"]
        conn.close()
        return {
            "total_orders": total_orders,
            "active_orders": active_orders,
            "completed_orders": completed_orders,
            "balance": dict(balance) if balance else {"balance": 0},
            "rating_avg": round(rating_avg, 1) if rating_avg else 0.0,
            "referrals": referrals,
        }

    # ─── Cleaning / purging methods ────────────────────────────

    def close_all_user_orders(self, user_id: int) -> int:
        """Close all active/matched orders for a specific user."""
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE orders SET status = 'closed', closed_at = strftime('%s', 'now')
               WHERE user_id = ? AND status IN ('active', 'matched')
            """,
            (user_id,),
        )
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

    def clean_spam_log(self, days_old: int = 7) -> int:
        """Delete spam_log entries older than N days."""
        conn = self._get_conn()
        cutoff = int(time.time()) - (days_old * 86400)
        cursor = conn.execute("DELETE FROM spam_log WHERE created_at < ?", (cutoff,))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

    def clean_old_orders(self, days_old: int = 30) -> int:
        """Delete completed/cancelled/closed orders older than N days."""
        conn = self._get_conn()
        cutoff = int(time.time()) - (days_old * 86400)
        cursor = conn.execute(
            """DELETE FROM orders
               WHERE status IN ('completed', 'cancelled', 'closed')
               AND created_at < ?
            """,
            (cutoff,),
        )
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

    def clean_old_contacts(self, days_old: int = 30) -> int:
        """Delete contacts older than N days."""
        conn = self._get_conn()
        cutoff = int(time.time()) - (days_old * 86400)
        cursor = conn.execute("DELETE FROM contacts WHERE created_at < ?", (cutoff,))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

    def purge_all_orders(self) -> dict:
        """Delete ALL orders and related data. Admin only — counts first, then delete."""
        conn = self._get_conn()
        orders_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        contacts_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        spam_count = conn.execute("SELECT COUNT(*) FROM spam_log").fetchone()[0]
        # Delete referencing tables FIRST to avoid FK constraint errors
        conn.execute("DELETE FROM contacts WHERE order_id IN (SELECT id FROM orders)")
        conn.execute("DELETE FROM active_connections")
        conn.execute("DELETE FROM spam_log")
        conn.execute("DELETE FROM orders")
        conn.commit()
        conn.close()
        return {
            "orders": orders_count,
            "contacts": contacts_count,
            "spam": spam_count,
        }
