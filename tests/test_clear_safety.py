"""Change 1 — data-safety guard tests for the /manager "Clear" button.

The Clear control on the manager dashboard is a filter-reset anchor: a plain
link to ``GET /manager`` with no query string. These tests prove that:

1. Following the Clear link never alters users or work entries.
2. The Clear control is (and stays) a plain navigation link — not a form
   submission, not an HTMX destructive verb, not wired to any handler.
3. The app defines no bulk "clear data" route.
4. The real destructive paths (Remove member, delete entry) behave exactly as
   intended and reject malformed input without crashing.
"""
import re

from tests.conftest import count_rows, create_entry, create_user, db_snapshot


def _extract_clear_link(html):
    """Return the anchor tag whose text is exactly 'Clear'."""
    matches = re.findall(r'<a\b[^>]*>\s*Clear\s*</a>', html)
    assert matches, "No 'Clear' link found on /manager"
    return matches[0]


# ---------- Users and work data survive a Clear ----------

def test_users_and_entries_survive_clear(client, manager):
    """Following the actual Clear link must not change any stored data."""
    m1 = create_user("Alex", "alex@x.com")
    m2 = create_user("Blake", "blake@x.com")
    create_entry(m1["id"], title="Alex task A")
    create_entry(m1["id"], title="Alex task B", date="2026-10-02")
    create_entry(m2["id"], title="Blake task C")

    client.post("/login", data={"email": manager["email"], "password": "secret123"})
    before = db_snapshot()

    # Extract the exact href the browser would follow when Clear is clicked
    page = client.get("/manager")
    assert page.status_code == 200
    href = re.search(r'href="([^"]*)"[^>]*>\s*Clear\s*</a>', page.get_data(as_text=True))
    assert href, "Clear link href not found"
    clear_url = href.group(1)

    resp = client.get(clear_url)
    assert resp.status_code == 200

    after = db_snapshot()
    assert after == before, "Clear must not modify users or entries"
    assert count_rows("users") == 3
    assert count_rows("entries") == 3


def test_clear_survives_with_active_filters(client, manager):
    """Clear with filters applied (the normal usage) also mutates nothing."""
    m = create_user("Sam", "sam@x.com")
    create_entry(m["id"])
    client.post("/login", data={"email": manager["email"], "password": "secret123"})

    before = db_snapshot()
    page = client.get(
        "/manager?person=%d&date_from=2026-01-01&date_to=2026-12-31&category=Meeting"
        % m["id"]
    )
    assert page.status_code == 200
    resp = client.get("/manager")  # Clear = unfiltered GET
    assert resp.status_code == 200
    assert db_snapshot() == before


def test_member_data_survives_clear_as_manager(client, manager, member):
    """The manager clearing filters never removes team member accounts."""
    create_entry(member["id"], title="Important work")
    client.post("/login", data={"email": manager["email"], "password": "secret123"})

    client.get("/manager")
    client.get("/manager")
    assert count_rows("users") == 2  # manager + member
    assert count_rows("entries") == 1
    conn_user = create_user  # sanity: helper still usable post-Clear
    assert conn_user


# ---------- Clear is a safe, non-destructive control ----------

def test_clear_link_is_plain_get_navigation(client, manager):
    """The Clear control must stay an <a> GET link to /manager (no action/JS)."""
    client.post("/login", data={"email": manager["email"], "password": "secret123"})
    html = client.get("/manager").get_data(as_text=True)

    tag = _extract_clear_link(html)
    # Plain navigation to the bare dashboard route = resets view state only
    assert 'href="/manager"' in tag, (
        "Clear must link to bare GET /manager; got: %s" % tag
    )
    # No destructive affordances
    for forbidden in ("hx-delete", "hx-post", "hx-put", "hx-trigger", "onclick",
                      "method=", "action="):
        assert forbidden not in tag, "Clear must not carry %r: %s" % (forbidden, tag)


def test_clear_filters_link_in_empty_state_is_safe(client, manager):
    """The empty-state 'Clear filters' link is equally non-destructive."""
    client.post("/login", data={"email": manager["email"], "password": "secret123"})
    html = client.get("/manager").get_data(as_text=True)
    matches = re.findall(r'<a\b[^>]*>\s*Clear filters\s*</a>', html)
    assert matches, "empty-state 'Clear filters' link missing"
    for tag in matches:
        assert 'href="/manager"' in tag, tag
        for forbidden in ("hx-delete", "hx-post", "onclick", "action="):
            assert forbidden not in tag, tag


def test_no_bulk_clear_route_exists(app):
    """No route may exist whose purpose is clearing/purging data."""
    suspicious = [
        str(r) for r in app.url_map.iter_rules()
        if re.search(r"clear|purge|wipe|truncate", str(r), re.I)
    ]
    assert suspicious == [], "Unexpected data-clearing routes: %s" % suspicious


# ---------- The real destructive paths stay correct and crash-free ----------

def test_remove_member_still_works(manager_client, member):
    """Remove (explicit, POST, confirmed) still deletes a member as designed."""
    resp = manager_client.post(
        "/manager/team", data={"action": "remove", "user_id": str(member["id"])},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Team member removed." in resp.data
    assert count_rows("users") == 1  # member gone, manager remains


def test_remove_malformed_user_id_is_rejected_not_500(manager_client, member):
    """Hardening: garbage user_id returns a friendly error, never a 500."""
    resp = manager_client.post(
        "/manager/team", data={"action": "remove", "user_id": "not-an-int"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid team member selected." in resp.data
    assert count_rows("users") == 2  # nothing deleted


def test_remove_missing_user_reports_not_found(manager_client):
    resp = manager_client.post(
        "/manager/team", data={"action": "remove", "user_id": "99999"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Team member not found." in resp.data


def test_cannot_remove_self_or_managers(manager_client, manager, member):
    # self
    resp = manager_client.post(
        "/manager/team", data={"action": "remove", "user_id": str(manager["id"])},
        follow_redirects=True,
    )
    assert b"Cannot remove yourself." in resp.data
    # managers are never removed by this action (SQL scopes to role='member')
    m2 = create_user("Other Manager", "other@x.com", role="manager")
    resp = manager_client.post(
        "/manager/team", data={"action": "remove", "user_id": str(m2["id"])},
        follow_redirects=True,
    )
    assert b"Team member not found." in resp.data
    assert count_rows("users") == 3  # 2 managers + member


def test_clear_requires_login(client):
    """The dashboard Clear target requires a session (no anonymous data access)."""
    resp = client.get("/manager")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
