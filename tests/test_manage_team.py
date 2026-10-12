"""Change 3a — manager account creation and team member creation on /manager/team."""
import bcrypt

from tests.conftest import count_rows, create_user, login


def _get_user(email):
    from db import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------- Team member creation (pre-existing, kept working) ----------

def test_can_create_team_member(manager_client):
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "member",
        "name": "New Member", "email": "new@x.com", "password": "secret123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Team member New Member added." in resp.data
    user = _get_user("new@x.com")
    assert user is not None
    assert user["role"] == "member"
    assert user["password_hash"] != "secret123"  # never plaintext
    assert user["password_hash"].startswith("$2")
    assert bcrypt.checkpw(b"secret123", user["password_hash"].encode())


def test_member_creation_validations(manager_client):
    # duplicate email
    create_user("Dup", "dup@x.com")
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "member",
        "name": "Dup2", "email": "dup@x.com", "password": "secret123",
    }, follow_redirects=True)
    assert b"Email already exists." in resp.data

    # short password
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "member",
        "name": "Shorty", "email": "short@x.com", "password": "abc",
    }, follow_redirects=True)
    assert b"Password must be at least 6 characters." in resp.data

    # invalid email
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "member",
        "name": "Bad", "email": "not-an-email", "password": "secret123",
    }, follow_redirects=True)
    assert b"Invalid email format." in resp.data

    # missing fields
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "member", "name": "", "email": "", "password": "",
    }, follow_redirects=True)
    assert b"All fields are required." in resp.data
    assert count_rows("users") == 2  # only the manager + seeded dup user


# ---------- Manager account creation (new) ----------

def test_can_create_manager_account(manager_client, app):
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "manager",
        "name": "Second Manager", "email": "mgr2@x.com", "password": "manager456",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Manager Second Manager added." in resp.data

    user = _get_user("mgr2@x.com")
    assert user is not None
    assert user["role"] == "manager"
    # stored hashed, verifiable
    assert user["password_hash"] != "manager456"
    assert bcrypt.checkpw(b"manager456", user["password_hash"].encode())

    # the new manager can actually log in and reach /manager
    fresh = app.test_client()
    resp = login(fresh, "mgr2@x.com", "manager456")
    assert resp.status_code == 302
    assert "/manager" in resp.headers["Location"]
    assert fresh.get("/manager").status_code == 200


def test_manager_account_creation_form_present(manager_client):
    html = manager_client.get("/manager/team").get_data(as_text=True)
    assert 'name="role" value="manager"' in html, "Add-manager form missing"
    assert b"via seed script" not in manager_client.get("/manager/team").data


def test_role_whitelist_rejects_forged_role(manager_client):
    resp = manager_client.post("/manager/team", data={
        "action": "add", "role": "superadmin",
        "name": "Sneaky", "email": "sneaky@x.com", "password": "secret123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    user = _get_user("sneaky@x.com")
    assert user["role"] == "member"  # forged role falls back to member


def test_member_cannot_create_accounts(member_client):
    resp = member_client.post("/manager/team", data={
        "action": "add", "role": "manager",
        "name": "Evil", "email": "evil@x.com", "password": "secret123",
    })
    assert resp.status_code == 403
    assert _get_user("evil@x.com") is None
    assert count_rows("users") == 1


# ---------- Editing manager accounts ----------

def test_edit_form_present_on_manager_rows(manager_client):
    html = manager_client.get("/manager/team").get_data(as_text=True)
    assert 'name="action" value="edit_manager"' in html
    assert 'name="new_password"' in html


def test_can_edit_own_manager_account(manager_client, manager):
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": "Priya Sharma Renamed", "email": "priya@newdomain.com",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Manager account updated." in resp.data
    user = _get_user("priya@newdomain.com")
    assert user["name"] == "Priya Sharma Renamed"
    assert user["role"] == "manager"
    # password untouched when blank
    assert bcrypt.checkpw(b"secret123", user["password_hash"].encode())


def test_edit_manager_can_change_password(manager_client, manager):
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": manager["name"], "email": manager["email"],
        "new_password": "newpass99",
    }, follow_redirects=True)
    assert b"Manager account updated." in resp.data
    user = _get_user(manager["email"])
    assert bcrypt.checkpw(b"newpass99", user["password_hash"].encode())


def test_edit_manager_duplicate_email_rejected(manager_client, manager, member):
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": manager["name"], "email": member["email"],
    }, follow_redirects=True)
    assert b"Email already exists." in resp.data
    # original untouched
    assert _get_user(manager["email"])["name"] == manager["name"]


def test_edit_manager_validations(manager_client, manager, member):
    # invalid email
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": "X", "email": "not-an-email",
    }, follow_redirects=True)
    assert b"Invalid email format." in resp.data

    # short password
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": "X", "email": "x@y.com", "new_password": "abc",
    }, follow_redirects=True)
    assert b"Password must be at least 6 characters." in resp.data

    # missing fields
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": "", "email": "",
    }, follow_redirects=True)
    assert b"Name and email are required." in resp.data

    # member id can never be edited via this action
    resp = manager_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": member["id"],
        "name": "Hijacked", "email": "hijack@x.com",
    }, follow_redirects=True)
    assert b"Manager not found." in resp.data
    assert _get_user(member["email"])["name"] == member["name"]
    assert member["email"] == _get_user(member["email"])["email"]


def test_member_cannot_edit_managers(member_client, manager):
    resp = member_client.post("/manager/team", data={
        "action": "edit_manager", "user_id": manager["id"],
        "name": "Nope", "email": "nope@x.com",
    })
    assert resp.status_code == 403
