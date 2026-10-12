"""Test harness for mydailyworkreport.

The app creates its database and session directory at import time (app.py:23-28),
so the environment must be redirected to temporary locations BEFORE `app` is
imported. ``load_dotenv()`` in config.py does not override existing environment
variables, so setting DATABASE_PATH here takes precedence over .env.
"""
import os
import sys
import tempfile

# --- Redirect all app state to temp locations before importing the app ---
_TEST_ROOT = tempfile.mkdtemp(prefix="worklog_tests_")
os.environ["DATABASE_PATH"] = os.path.join(_TEST_ROOT, "test_worklog.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.Config.SESSION_FILE_DIR = os.path.join(_TEST_ROOT, "flask_session")

import app as app_module  # noqa: E402
from db import get_connection, init_db  # noqa: E402
import bcrypt  # noqa: E402
import pytest  # noqa: E402


# ---------- State helpers ----------

def _reset_database():
    """Remove all rows so every test starts from a clean, known state."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM settings")
        conn.commit()
    finally:
        conn.close()


def create_user(name, email, password="secret123", role="member"):
    """Insert a user directly into the DB, returning its row as a dict."""
    conn = get_connection()
    try:
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cur = conn.execute(
            "INSERT INTO users(name, email, password_hash, role) VALUES(?,?,?,?)",
            (name, email.lower(), pw_hash, role),
        )
        conn.commit()
        return dict(
            conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        )
    finally:
        conn.close()


def create_entry(user_id, title="Task", description="Details", date="2026-10-01",
                 start_time="09:00", end_time="10:00", duration_minutes=60,
                 category="Feature Development"):
    """Insert a work entry directly into the DB, returning its row as a dict."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO entries
               (user_id, title_raw, description_raw, category, date,
                start_time, end_time, duration_minutes)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user_id, title, description, category, date,
             start_time, end_time, duration_minutes),
        )
        conn.commit()
        return dict(
            conn.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone()
        )
    finally:
        conn.close()


def count_rows(table):
    conn = get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        conn.close()


def db_snapshot():
    """Full picture of stored data — used to prove Clear doesn't mutate it."""
    conn = get_connection()
    try:
        users = [dict(r) for r in conn.execute(
            "SELECT id, name, email, password_hash, role FROM users ORDER BY id"
        ).fetchall()]
        entries = [dict(r) for r in conn.execute(
            "SELECT id, user_id, title_raw, description_raw, category, date, "
            "duration_minutes FROM entries ORDER BY id"
        ).fetchall()]
        return {"users": users, "entries": entries}
    finally:
        conn.close()


def login(client, email, password):
    """Log a user in through the real /login route; returns the response."""
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


# ---------- Fixtures ----------

@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts with an empty database."""
    _reset_database()
    yield


@pytest.fixture()
def app():
    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def manager(app):
    return create_user("Mara Manager", "manager@example.com", role="manager")


@pytest.fixture()
def member(app):
    return create_user("Alex Member", "alex@example.com", role="member")


@pytest.fixture()
def member2(app):
    return create_user("Blake Member", "blake@example.com", role="member")


@pytest.fixture()
def manager_client(client, manager):
    login(client, manager["email"], "secret123")
    return client


@pytest.fixture()
def member_client(client, member):
    login(client, member["email"], "secret123")
    return client
