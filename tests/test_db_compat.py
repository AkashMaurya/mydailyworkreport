"""DB backend compatibility: SQLite->PostgreSQL translation, unique-violation
detection, bootstrap-manager safety, and manager-only seeding."""
import os
import sqlite3

import bcrypt

import db
from app import ensure_bootstrap_manager
from db import is_unique_violation, to_pg
from tests.conftest import count_rows, create_user, db_snapshot


# ---------- to_pg translation ----------

def test_to_pg_collate_nocase_login():
    src = "SELECT * FROM users WHERE email=? COLLATE NOCASE"
    out = to_pg(src)
    assert out == "SELECT * FROM users WHERE LOWER(email) = LOWER(%s)"
    assert "COLLATE" not in out.upper()


def test_to_pg_datetime_now_plain():
    out = to_pg("UPDATE entries SET updated_at=datetime('now') WHERE id=?")
    assert out == "UPDATE entries SET updated_at=now() WHERE id=%s"
    assert "datetime(" not in out


def test_to_pg_datetime_now_modifier():
    out = to_pg("SELECT * FROM t WHERE ts > datetime('now', '-15 minutes')")
    assert out == "SELECT * FROM t WHERE ts > now() - INTERVAL '15 minutes'"


def test_to_pg_settings_upsert():
    src = ("INSERT INTO settings(key, value, updated_at) VALUES(?,?,datetime('now')) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')")
    out = to_pg(src)
    assert out.count("%s") == 2
    assert "now()" in out
    assert "ON CONFLICT(key)" in out  # works on Postgres as-is


def test_to_pg_placeholder_count_matches():
    src = ("INSERT INTO entries(user_id, title_raw, description_raw, category, date, "
           "start_time, end_time, duration_minutes) VALUES(?,?,?,?,?,?,?,?)")
    assert to_pg(src).count("%s") == 8
    assert to_pg(src).count("?") == 0


def test_to_pg_leaves_plain_select_untouched():
    src = "SELECT id, name, email, created_at FROM users WHERE role='member' ORDER BY name"
    assert to_pg(src) == src


# ---------- unique violation detection (both backends) ----------

def test_is_unique_violation_sqlite():
    exc = sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
    assert is_unique_violation(exc) is True
    assert is_unique_violation(sqlite3.IntegrityError("FOREIGN KEY constraint failed")) is False
    assert is_unique_violation(ValueError("boom")) is False


def test_is_unique_violation_postgres_sqlstate():
    class FakePgError(Exception):
        pgcode = "23505"
    class FakeOtherPg(Exception):
        pgcode = "23503"
    assert is_unique_violation(FakePgError("duplicate key")) is True
    assert is_unique_violation(FakeOtherPg("fk")) is False


# ---------- backend selection ----------

def test_pg_disabled_by_default():
    # Local/test runs must never silently jump to Postgres even though a
    # DATABASE_URL may exist in .env.
    assert db.pg_enabled() is False


def test_pg_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(db.Config, "DB_BACKEND", "postgres")
    monkeypatch.setattr(db.Config, "DATABASE_URL", "")
    assert db.pg_enabled() is False  # no URL -> no postgres
    monkeypatch.setattr(db.Config, "DATABASE_URL", "postgresql://x/y")
    assert db.pg_enabled() is True
    monkeypatch.setattr(db.Config, "DB_BACKEND", "sqlite")
    assert db.pg_enabled() is False  # opt-in withdrawn


# ---------- bootstrap manager ----------

def test_bootstrap_creates_manager_only_on_empty_db():
    result = ensure_bootstrap_manager()  # temp DB is empty after clean_state
    assert result == "manager@worklog.local"
    assert count_rows("users") == 1
    assert count_rows("entries") == 0
    from db import get_connection
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", ("manager@worklog.local",)).fetchone()
    finally:
        conn.close()
    assert row["role"] == "manager"
    assert row["password_hash"].startswith("$2")
    assert bcrypt.checkpw(b"Manager123!", row["password_hash"].encode())


def test_bootstrap_never_touches_non_empty_db():
    member = create_user("Existing Member", "exist@x.com", password="secret123")
    before = db_snapshot()
    assert ensure_bootstrap_manager() is None
    assert db_snapshot() == before
    assert count_rows("users") == 1


def test_bootstrap_does_not_remove_seed_or_real_users(manager, member, member2):
    # Even if seed-looking users exist, bootstrap neither adds nor deletes.
    before = db_snapshot()
    ensure_bootstrap_manager()
    assert db_snapshot() == before
    assert count_rows("users") == 3


# ---------- seed.py: manager-only by default ----------

def test_seed_default_creates_manager_only(monkeypatch):
    import seed
    monkeypatch.delenv("SEED_SAMPLE_DATA", raising=False)
    seed.main()
    assert count_rows("users") == 1
    assert count_rows("entries") == 0
    # idempotent re-run
    seed.main()
    assert count_rows("users") == 1


def test_seed_sample_data_opt_in(monkeypatch):
    import seed
    monkeypatch.setenv("SEED_SAMPLE_DATA", "1")
    seed.main()
    assert count_rows("users") == 3      # manager + Alex + Samir
    assert count_rows("entries") == 10   # 5 sample entries each
