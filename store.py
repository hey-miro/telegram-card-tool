import os
import sqlite3
import sys
import threading
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        data_root = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        data_root = os.getenv("APPDATA", os.path.expanduser("~"))
    else:
        data_root = os.getenv("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    DATA_DIR = os.path.join(data_root, "Telegram名片工具")
else:
    DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "tool.db")

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    session TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def set_setting(key, value):
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()


def get_setting(key):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()


def get_config():
    return {
        "api_id": get_setting("api_id"),
        "api_hash": get_setting("api_hash"),
        "proxy_enabled": get_setting("proxy_enabled") == "1",
        "proxy_type": get_setting("proxy_type") or "socks5",
        "proxy_host": get_setting("proxy_host") or "",
        "proxy_port": get_setting("proxy_port") or "",
    }


def set_config(
    api_id,
    api_hash,
    proxy_enabled=False,
    proxy_type="socks5",
    proxy_host="",
    proxy_port="",
):
    set_setting("api_id", api_id)
    set_setting("api_hash", api_hash)
    set_setting("proxy_enabled", "1" if proxy_enabled else "0")
    set_setting("proxy_type", proxy_type or "socks5")
    set_setting("proxy_host", proxy_host or "")
    set_setting("proxy_port", proxy_port or "")


def upsert_account(phone, session):
    now = int(time.time())
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO accounts(phone, session, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    session = excluded.session,
                    updated_at = excluded.updated_at
                """,
                (phone, session, now, now),
            )
            conn.commit()
        finally:
            conn.close()


def list_accounts():
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, phone, session, created_at, updated_at "
                "FROM accounts ORDER BY updated_at DESC"
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "phone": row["phone"],
                    "has_session": bool(row["session"]),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()


def get_account(account_id):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, phone, session FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_account_by_phone(phone):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, phone, session FROM accounts WHERE phone = ?",
                (phone,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def delete_account(account_id):
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
        finally:
            conn.close()
