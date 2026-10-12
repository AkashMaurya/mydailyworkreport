"""Change 3b — self-service password change (/account/password).

Covers: bcrypt hashing at rest, the current-password check, rejection of wrong
credentials, validation rules, access control, and post-change login behavior.
"""
import bcrypt

from tests.conftest import create_user, login


def _row(email):
    from db import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _change(client, current, new, confirm=None):
    return client.post("/account/password", data={
        "current_password": current,
        "new_password": new,
        "confirm_password": confirm if confirm is not None else new,
    }, follow_redirects=True)


# ---------- Hashing ----------

def test_password_stored_hashed_never_plaintext(member):
    row = _row(member["email"])
    assert row["password_hash"] != "secret123"
    assert row["password_hash"].startswith("$2")  # bcrypt
    # The stored hash must not contain the plaintext anywhere
    assert "secret123" not in row["password_hash"]
    assert bcrypt.checkpw(b"secret123", row["password_hash"].encode())


def test_change_stores_new_bcrypt_hash(member_client, member):
    before = _row(member["email"])["password_hash"]
    resp = _change(member_client, "secret123", "brandnew456")
    assert resp.status_code == 200
    assert b"Password updated successfully." in resp.data

    after = _row(member["email"])["password_hash"]
    assert after != before
    assert after.startswith("$2")
    assert after != "brandnew456"
    assert bcrypt.checkpw(b"brandnew456", after.encode())
    # bcrypt salts differ even for identical inputs
    assert bcrypt.hashpw(b"brandnew456", after.encode()) == after.encode()


# ---------- Current-password check ----------

def test_correct_current_password_allows_change(member_client, member, app):
    resp = _change(member_client, "secret123", "brandnew456")
    assert b"Password updated successfully." in resp.data

    # old password no longer authenticates
    fresh = app.test_client()
    resp = login(fresh, member["email"], "secret123")
    assert b"Invalid email or password." in resp.data

    # new password authenticates
    fresh2 = app.test_client()
    resp = login(fresh2, member["email"], "brandnew456")
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["Location"]


def test_wrong_current_password_rejected_no_write(member_client, member):
    before = _row(member["email"])["password_hash"]
    resp = _change(member_client, "WRONG-password", "brandnew456")
    assert resp.status_code == 200
    assert b"Current password is incorrect." in resp.data
    # nothing changed — hash identical, old password still valid
    assert _row(member["email"])["password_hash"] == before
    assert bcrypt.checkpw(b"secret123", before.encode())


def test_wrong_current_password_rejected_for_manager(manager_client, manager, app):
    before = _row(manager["email"])["password_hash"]
    resp = _change(manager_client, "nope", "brandnew456")
    assert b"Current password is incorrect." in resp.data
    assert _row(manager["email"])["password_hash"] == before

    fresh = app.test_client()
    resp = login(fresh, manager["email"], "secret123")
    assert resp.status_code == 302  # original password untouched


# ---------- Validation rules ----------

def test_validation_errors_do_not_write(member_client, member):
    before = _row(member["email"])["password_hash"]

    # mismatched confirmation
    resp = _change(member_client, "secret123", "brandnew456", confirm="different1")
    assert b"New passwords do not match." in resp.data

    # too short
    resp = _change(member_client, "secret123", "abc")
    assert b"New password must be at least 6 characters." in resp.data

    # same as current
    resp = _change(member_client, "secret123", "secret123")
    assert b"New password must be different from the current password." in resp.data

    # missing fields
    resp = member_client.post("/account/password", data={}, follow_redirects=True)
    assert b"All fields are required." in resp.data

    assert _row(member["email"])["password_hash"] == before  # never written


# ---------- Access control & UI ----------

def test_requires_login(client):
    resp = client.get("/account/password")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    resp = client.post("/account/password", data={
        "current_password": "x", "new_password": "yyyyyy", "confirm_password": "yyyyyy",
    })
    assert resp.status_code == 302


def test_available_to_members(member_client):
    resp = member_client.get("/account/password")
    assert resp.status_code == 200
    assert b"Current password" in resp.data
    # nav link visible on the member dashboard
    dash = member_client.get("/dashboard").get_data(as_text=True)
    assert 'href="/account/password"' in dash


def test_available_to_managers(manager_client):
    resp = manager_client.get("/account/password")
    assert resp.status_code == 200
    assert b"Current password" in resp.data
    dash = manager_client.get("/manager").get_data(as_text=True)
    assert 'href="/account/password"' in dash


def test_change_password_does_not_affect_other_users(member_client, member, manager):
    other_before = _row(manager["email"])["password_hash"]
    _change(member_client, "secret123", "brandnew456")
    assert _row(manager["email"])["password_hash"] == other_before
