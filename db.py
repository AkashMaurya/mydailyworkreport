import sqlite3
import os
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

from config import Config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','member')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title_raw TEXT NOT NULL,
    description_raw TEXT NOT NULL,
    title_enhanced TEXT,
    description_enhanced TEXT,
    category TEXT CHECK(category IN ('Bug Fix','Feature Development','Meeting','Documentation','Research','Support/Maintenance','Design','Other') OR category IS NULL),
    date TEXT NOT NULL, -- YYYY-MM-DD
    start_time TEXT, -- HH:MM
    end_time TEXT,   -- HH:MM
    duration_minutes INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    enhanced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, date);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
CREATE INDEX IF NOT EXISTS idx_entries_category ON entries(category);
"""

def get_db_path():
    return Config.DATABASE_PATH

def get_connection():
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

def _derive_fernet_key():
    """Derive a Fernet key from SECRET_KEY if FERNET_KEY not provided.
    This ensures encryption still works out-of-the-box."""
    if Config.FERNET_KEY:
        key_str = Config.FERNET_KEY.strip()
        # Validate
        try:
            Fernet(key_str.encode() if isinstance(key_str, str) else key_str)
            return key_str.encode() if isinstance(key_str, str) else key_str
        except Exception:
            pass
    # Derive deterministic 32-byte key from SECRET_KEY via SHA256 and base64
    digest = hashlib.sha256(Config.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)

def get_fernet():
    key = _derive_fernet_key()
    return Fernet(key)

def encrypt_value(plain: str) -> str:
    if not plain:
        return ""
    f = get_fernet()
    return f.encrypt(plain.encode()).decode()

def decrypt_value(token: str) -> str:
    if not token:
        return ""
    f = get_fernet()
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        # Fallback: maybe stored as plain (legacy) -> return as-is
        return token

def get_setting(key: str):
    conn = get_connection()
    try:
        cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        if row:
            return row["value"]
        return None
    finally:
        conn.close()

def set_setting(key: str, value: str):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')", (key, value))
        conn.commit()
    finally:
        conn.close()

def get_decrypted_setting(key: str):
    val = get_setting(key)
    if val is None:
        return None
    try:
        return decrypt_value(val)
    except Exception:
        return None

def set_encrypted_setting(key: str, plain: str):
    enc = encrypt_value(plain)
    set_setting(key, enc)
