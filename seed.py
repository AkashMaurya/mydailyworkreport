#!/usr/bin/env python3
"""
Seed script: creates the first manager account and sample data.
Run once on initial setup:
    python seed.py
Idempotent: will not duplicate if manager email exists.
"""
import os
import sys
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
load_dotenv()

# Ensure app context
from db import init_db, get_connection, get_setting, set_setting
from config import Config
import bcrypt

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def ensure_user(name, email, password, role, created_by=None):
    conn = get_connection()
    try:
        cur = conn.execute("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,))
        if cur.fetchone():
            print(f"  • {email} already exists, skipping")
            return None
        cur = conn.execute(
            "INSERT INTO users(name, email, password_hash, role, created_by) VALUES(?,?,?,?,?)",
            (name, email.lower(), hash_password(password), role, created_by)
        )
        conn.commit()
        uid = cur.lastrowid
        print(f"  ✓ Created {role}: {name} <{email}> (id={uid})")
        return uid
    finally:
        conn.close()

def seed_entries(user_id):
    conn = get_connection()
    try:
        cur = conn.execute("SELECT COUNT(*) as c FROM entries WHERE user_id=?", (user_id,))
        if cur.fetchone()["c"] > 0:
            print("  • Sample entries already exist for user", user_id)
            return
        today = date.today()
        samples = [
            ("Fixed checkout race condition", "fixed bug where orders got duplicated when user double-clicked checkout button, added idempotency key", "Bug Fix", today.isoformat(), "09:00", "11:30", 150),
            ("Planned Q3 roadmap with design", "had meeting with design and product to discuss Q3 roadmap, aligned on priorities and timelines", "Meeting", today.isoformat(), "11:30", "12:30", 60),
            ("API documentation for payments", "wrote docs for payments API endpoints, added examples and error codes", "Documentation", (today - timedelta(days=1)).isoformat(), "14:00", "16:00", 120),
            ("Researched caching strategies", "looked into redis vs memcached for session store, compared latency and persistence", "Research", (today - timedelta(days=1)).isoformat(), "09:30", "11:00", 90),
            ("Implemented dark mode toggle", "built dark mode toggle with tailwind and persisted in localStorage, added system preference detection", "Feature Development", (today - timedelta(days=2)).isoformat(), "10:00", "13:00", 180),
        ]
        for title, desc, cat, d, st, et, dur in samples:
            # Create both raw and one enhanced example to showcase toggle
            conn.execute("""
                INSERT INTO entries(user_id, title_raw, description_raw, title_enhanced, description_enhanced, category, date, start_time, end_time, duration_minutes, enhanced_at)
                VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now'))
            """, (user_id, title, desc, f"[Enhanced] {title}", desc.capitalize() + " — rewritten in professional documentation style for compliance reporting.", cat, d, st, et, dur))
        conn.commit()
        print(f"  ✓ Seeded {len(samples)} sample entries for user {user_id}")
    finally:
        conn.close()

def main():
    print("Initializing database...")
    init_db()
    print(f"DB path: {Config.DATABASE_PATH}")
    print("\nSeeding users...")
    manager_id = ensure_user("Priya Sharma", "manager@worklog.local", "Manager123!", "manager")
    if manager_id is None:
        # get existing manager id
        conn = get_connection()
        try:
            cur = conn.execute("SELECT id FROM users WHERE email='manager@worklog.local' COLLATE NOCASE")
            row = cur.fetchone()
            manager_id = row["id"] if row else None
        finally:
            conn.close()
    # Sample members
    m1 = ensure_user("Alex Rivera", "alex@worklog.local", "Member123!", "member", created_by=manager_id)
    m2 = ensure_user("Samir Patel", "samir@worklog.local", "Member123!", "member", created_by=manager_id)
    # If already existed, fetch their ids for seeding check
    conn = get_connection()
    try:
        for email in ["alex@worklog.local", "samir@worklog.local"]:
            cur = conn.execute("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,))
            row = cur.fetchone()
            if row:
                seed_entries(row["id"])
    finally:
        conn.close()

    print("\nDone.")
    print("\nCredentials:")
    print("  Manager → manager@worklog.local / Manager123!")
    print("  Member  → alex@worklog.local / Member123!")
    print("  Member  → samir@worklog.local / Member123!")
    print("\nNext:")
    print("  1. cp .env.example .env  (then edit FLASK_SECRET_KEY & FERNET_KEY)")
    print("  2. python app.py")
    print("  3. Open http://localhost:5000 and log in")
    print("  4. As manager, go to Settings → add your OpenRouter API key to enable AI Enhance")

if __name__ == "__main__":
    main()
