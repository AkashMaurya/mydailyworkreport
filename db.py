import os
import re
import sqlite3
import base64
import hashlib
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from config import Config

# ---------------------------------------------------------------------------
# SQLite schema (local development + automated tests)
# ---------------------------------------------------------------------------

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
    category TEXT CHECK(category IN ('Bug Fix','Feature Development','Meeting','Documentation',
        'Research','Support/Maintenance','Design','Other') OR category IS NULL),
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

# ---------------------------------------------------------------------------
# PostgreSQL (Neon) schema — executed statement-by-statement. Kept compatible
# with the tables created by the original migrate_to_neon.py so re-running
# init_db() against an existing Neon database is a no-op.
# ---------------------------------------------------------------------------

SCHEMA_PG_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('manager','member')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entries (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title_raw TEXT NOT NULL,
        description_raw TEXT NOT NULL,
        title_enhanced TEXT,
        description_enhanced TEXT,
        category TEXT,
        date TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        duration_minutes INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        enhanced_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date)",
    "CREATE INDEX IF NOT EXISTS idx_entries_category ON entries(category)",
]


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def pg_enabled():
    """True only when the operator explicitly opts in to Postgres.

    DB_BACKEND=postgres AND DATABASE_URL must both be present. A DATABASE_URL
    left in a local .env therefore never hijacks local SQLite development.
    """
    return Config.DB_BACKEND == "postgres" and bool(Config.DATABASE_URL)


# ---------------------------------------------------------------------------
# SQLite -> PostgreSQL translation
# ---------------------------------------------------------------------------

def to_pg(sql):
    """Translate the app's SQLite-flavoured SQL to PostgreSQL.

    Handles every dialect difference used in this codebase:
    - ``email=? COLLATE NOCASE``  ->  ``LOWER(email)=LOWER(%s)``
    - ``datetime('now')``         ->  ``now()``
    - ``datetime('now', '-15 minutes')`` -> ``now() - INTERVAL '15 minutes'``
    - ``?`` placeholders          ->  ``%s``

    (No query in this codebase contains a literal ``?`` or ``%`` inside a
    string value, so plain replacement is safe.)
    """
    s = sql
    # Case-insensitive comparisons (must run before placeholder rewriting)
    s = re.sub(
        r"(?i)email\s*=\s*\?\s*COLLATE\s+NOCASE",
        "LOWER(email) = LOWER(%s)",
        s,
    )
    s = re.sub(
        r"(?i)=\s*\?\s*COLLATE\s+NOCASE",
        "= LOWER(%s)",
        s,
    )
    # datetime('now', 'modifier') -> now() +/- INTERVAL 'modifier'
    # (SQLite folds the sign into the modifier; Postgres wants it on the operator.)
    def _interval_repl(m):
        mod = m.group(1).strip()
        sign = "-"
        if mod[:1] in ("-", "+"):
            sign = mod[0]
            mod = mod[1:].strip()
        return "now() %s INTERVAL '%s'" % (sign, mod.replace("'", ""))
    s = re.sub(
        r"(?i)datetime\(\s*'now'\s*,\s*'([^']+)'\s*\)",
        _interval_repl,
        s,
    )
    # datetime('now') -> now()
    s = re.sub(r"(?i)datetime\(\s*'now'\s*\)", "now()", s)
    # Placeholders
    s = s.replace("?", "%s")
    return s


def is_unique_violation(exc):
    """Detect a UNIQUE-constraint violation on either backend."""
    if isinstance(exc, sqlite3.IntegrityError):
        return "UNIQUE" in str(exc).upper()
    # psycopg2: SQLSTATE 23505 = unique_violation
    return getattr(exc, "pgcode", None) == "23505"


# ---------------------------------------------------------------------------
# PostgreSQL connection wrapper (same surface as sqlite3.Connection)
# ---------------------------------------------------------------------------

def _convert_value(v):
    """Render PG values the same way SQLite would (datetime -> '... ' text)."""
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return v


def _convert_row(row):
    if row is None:
        return None
    out = dict(row)
    for k, v in out.items():
        out[k] = _convert_value(v)
    return out


class _PgCursorProxy:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return _convert_row(self._cursor.fetchone())

    def fetchall(self):
        return [_convert_row(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield _convert_row(row)


class PgConnection:
    """Mimics the sqlite3.Connection surface used across this app."""

    def __init__(self, connection):
        self._connection = connection
        self._cursor = connection.cursor()

    def execute(self, sql, params=()):
        self._cursor.execute(to_pg(sql), tuple(params))
        return _PgCursorProxy(self._cursor)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        try:
            self._cursor.close()
        except Exception:
            pass
        self._connection.close()


# ---------------------------------------------------------------------------
# Connections + init
# ---------------------------------------------------------------------------

def get_db_path():
    return Config.DATABASE_PATH


def get_connection():
    if pg_enabled():
        import psycopg2
        from psycopg2.extras import RealDictCursor
        raw = psycopg2.connect(
            Config.DATABASE_URL,
            connect_timeout=15,
            cursor_factory=RealDictCursor,
        )
        return PgConnection(raw)
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    conn = get_connection()
    try:
        if pg_enabled():
            for stmt in SCHEMA_PG_STATEMENTS:
                conn.execute(stmt)
        else:
            conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fernet encryption helpers (unchanged)
# ---------------------------------------------------------------------------

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
