#!/usr/bin/env python3
"""Sync the local SQLite database to Neon (Postgres) over HTTPS.

Why HTTPS: many local networks block Postgres port 5432. Neon exposes a
SQL-over-HTTPS endpoint (https://<endpoint>/sql) that works anywhere port 443
is reachable, so this script runs from laptops as well as from CI.

- Idempotent: rows are upserted by primary key; safe to re-run.
- Additive/update-only: it never DELETEs rows on Neon. Rows that exist only on
  Neon are reported at the end so you can decide what to do with them.
- Source of truth: the local (cleaned) SQLite database.

Usage:
    python migrate_to_neon.py           # sync SQLite -> Neon
    python migrate_to_neon.py --check   # read-only: report both sides
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = os.path.join("instance", "worklog.db")
PG_URL = os.environ.get("DATABASE_URL", "").strip()


def neon_host():
    if not PG_URL:
        print("Error: DATABASE_URL not found in .env file!")
        sys.exit(1)
    return urllib.parse.urlparse(PG_URL.split("?")[0]).hostname


def sql(query, params=None):
    """Run one statement on Neon via SQL-over-HTTPS."""
    body = {"query": query}
    if params is not None:
        body["params"] = list(params)
    req = urllib.request.Request(
        "https://%s/sql" % HOST,
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("neon-connection-string", PG_URL)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise RuntimeError("Neon HTTP %s: %s" % (e.code, detail))


def qrows(query, params=None):
    return sql(query, params).get("rows", [])


def upsert(table, columns, rows, pk="id"):
    """Upsert dict rows into a Neon table by primary key."""
    cols = [c for c in columns]
    col_sql = ", ".join(cols)
    n = len(cols)
    placeholders = ", ".join("$%d" % (i + 1) for i in range(n))
    updates = ", ".join("%s = EXCLUDED.%s" % (c, c) for c in cols if c != pk)
    stmt = (
        "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) DO UPDATE SET %s"
        % (table, col_sql, placeholders, pk, updates)
    )
    for row in rows:
        sql(stmt, [row.get(c) for c in cols])


def fetch_local():
    if not os.path.exists(SQLITE_PATH):
        print("Error: %s not found" % SQLITE_PATH)
        sys.exit(1)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    data = {}
    data["users"] = [dict(r) for r in conn.execute(
        "SELECT id, name, email, password_hash, role, created_at, created_by "
        "FROM users ORDER BY id")]
    data["settings"] = [dict(r) for r in conn.execute(
        "SELECT key, value, updated_at FROM settings ORDER BY key")]
    data["entries"] = [dict(r) for r in conn.execute(
        "SELECT id, user_id, title_raw, description_raw, title_enhanced, "
        "description_enhanced, category, date, start_time, end_time, "
        "duration_minutes, created_at, updated_at, enhanced_at "
        "FROM entries ORDER BY id")]
    conn.close()
    return data


def fix_sequences():
    for table in ("users", "entries"):
        qrows(
            "SELECT setval(pg_get_serial_sequence('%s', 'id'), "
            "COALESCE((SELECT MAX(id) FROM %s), 1))" % (table, table)
        )


def main():
    global HOST
    HOST = neon_host()
    check_only = "--check" in sys.argv

    print("Source: local SQLite  ->", SQLITE_PATH)
    print("Target: Neon (HTTPS)  ->", HOST)
    data = fetch_local()
    print("Local counts: users=%d settings=%d entries=%d"
          % (len(data["users"]), len(data["settings"]), len(data["entries"])))

    neon_users = {r["id"]: r for r in qrows("SELECT id, email, role FROM users")}
    neon_entries = {r["id"]: r for r in qrows("SELECT id, user_id FROM entries")}
    print("Neon counts: users=%d entries=%d" % (len(neon_users), len(neon_entries)))

    if check_only:
        local_ids = {u["id"] for u in data["users"]}
        print("Users only on Neon:", [neon_users[i] for i in sorted(set(neon_users) - local_ids)])
        print("Users only local:", [u["email"] for u in data["users"] if u["id"] not in neon_users])
        return

    # Neon returns values as strings; normalize ints for JSON params.
    for u in data["users"]:
        u["created_by"] = int(u["created_by"]) if u["created_by"] is not None else None
    for e in data["entries"]:
        for k in ("user_id", "duration_minutes"):
            e[k] = int(e[k]) if e[k] is not None else None

    print("Syncing schema (create-if-missing)...")
    import db as db_module  # reuse the app's PG schema statements
    for stmt in db_module.SCHEMA_PG_STATEMENTS:
        sql(stmt)

    print("Upserting users (%d)..." % len(data["users"]))
    upsert("users",
           ["id", "name", "email", "password_hash", "role", "created_at", "created_by"],
           data["users"])
    print("Upserting settings (%d)..." % len(data["settings"]))
    upsert("settings", ["key", "value", "updated_at"], data["settings"], pk="key")
    print("Upserting entries (%d)..." % len(data["entries"]))
    upsert("entries",
           ["id", "user_id", "title_raw", "description_raw", "title_enhanced",
            "description_enhanced", "category", "date", "start_time", "end_time",
            "duration_minutes", "created_at", "updated_at", "enhanced_at"],
           data["entries"])

    print("Fixing id sequences...")
    fix_sequences()

    # Verify
    after_users = qrows("SELECT id, name, email, role FROM users ORDER BY id")
    after_entries = qrows("SELECT COUNT(*) AS c FROM entries")[0]["c"]
    print("\nSync complete. Neon now has:")
    for u in after_users:
        print("  user %-3s %-18s %-28s %s" % (u["id"], u["role"], u["email"], u["name"]))
    print("  entries:", after_entries)

    local_user_ids = {u["id"] for u in data["users"]}
    extras = [neon_users[i] for i in sorted(set(neon_users) - local_user_ids)]
    if extras:
        print("\nNOTE: users present ONLY on Neon (not touched):", extras)


if __name__ == "__main__":
    main()
