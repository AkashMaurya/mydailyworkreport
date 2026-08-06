# Daily Work Log — Internal Productivity Tool

A production-grade, local-first **Daily Work Log** web app for managers to track team members' daily work and generate compliance-ready reports. Built with **Flask + HTMX + Tailwind + Alpine.js**, SQLite, session auth, and OpenRouter AI enhancement.

![Stack](https://img.shields.io/badge/Stack-Flask%20%7C%20HTMX%20%7C%20Tailwind%20%7C%20Alpine-1e293b) ![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB) ![License](https://img.shields.io/badge/License-Internal-lightgrey)

---

## Features

### Core: Work Log Entries
- **Title + description** stored **as-is** (raw). Never overwritten by AI.
- **Two time-input modes**: manual start/end **OR** live timer (Start → Stop, duration auto-calculated).
- **Date** defaults to today.
- **Enhanced** title/description/category stored separately; UI shows enhanced by default with toggle to raw.
- Responsive, mobile-friendly logging.

### Auth & Roles
- **Manager**: email+password login, add/remove members (name+email+password), view all logs, export, manage OpenRouter key.
- **Team Member**: credentials created by manager (no self-signup), sees/manages only own entries.
- **bcrypt** hashing, **Flask-Session** filesystem sessions (8h TTL, httpOnly, SameSite=Lax).

### AI Enhancement (OpenRouter)
- Manager enters API key in **Settings** → stored **encrypted (Fernet)** in SQLite, never hardcoded or exposed to frontend.
- Per-entry **Enhance** button sends `title_raw + description_raw` to OpenRouter with a strict system prompt:
  1. Rewrite title/description into clear professional English
  2. Auto-categorize into exactly one of: `Bug Fix, Feature Development, Meeting, Documentation, Research, Support/Maintenance, Design, Other`
  3. Return JSON `{enhanced_title, enhanced_description, category}`
- Graceful degradation: clear error, original stays visible if API missing/fails.
- Configurable model (default `openai/gpt-4o-mini`).

### Manager Dashboard
- Single view, grouped **person → date → entries**
- **Filterable** by person, date range, category
- **Today’s entries prominent** (dark hero card); past days **collapsible**
- **Totals**: per-person per-day and per-week (last 7 days, calculated from `duration_minutes`)

### Exports (filtered, downloadable)
- **Excel (.xlsx)**: one row per entry — Date, Person, Title, Description, Category, Start, End, Duration
- **Word (.docx)**: per-day report grouped by person — heading=date, subheading=person, formatted official document
- **PDF**: same structure as Word (pdfkit → wkhtmltopdf, with ReportLab fallback if binary missing)

### Design & UX
- Calm, minimal, trustworthy aesthetic — **slate + indigo + emerald**, custom palette (not generic cream/terracotta or dark/neon)
- **Tailwind CDN**, **HTMX** for dynamic enhance/delete, **Alpine.js** for timer/toggles/collapsibles
- Clean hierarchy, good spacing, readable typography (Inter + Fraunces)

---

## Quick Start (Local Dev)

### 1. Requirements
- Python 3.10+
- No external DB/services (SQLite file-based). Only external call is OpenRouter on Enhance.

### 2. Clone & Install
```bash
git clone <repo> && cd daily-work-log
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Environment
```bash
cp .env.example .env
# Edit .env:
# - Generate secrets:
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Fill `FLASK_SECRET_KEY` and `FERNET_KEY` in `.env`. Optionally set `OPENROUTER_API_KEY` there (or via Manager Settings UI, which encrypts in DB).

`.env.example`:
```
FLASK_SECRET_KEY=change-me-to-a-random-64-char-string
FERNET_KEY=your-fernet-key-here-44chars-base64==
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openai/gpt-4o-mini
DATABASE_PATH=instance/worklog.db
```

### 4. Seed (first manager + sample data)
```bash
python seed.py
```
Creates:
- **Manager**: `manager@worklog.local` / `Manager123!`
- **Members**: `alex@worklog.local` / `Member123!`, `samir@worklog.local` / `Member123!`
- Sample entries (5 across dates) with one enhanced example

Idempotent — safe to re-run; skips existing emails.

### 5. Run
```bash
python app.py
# → http://localhost:5000  (debug, auto-reload)
```
Or:
```bash
flask --app app run --host 0.0.0.0 --port 5000 --debug
```

### 6. Login & Configure AI
1. Log in as **manager@worklog.local / Manager123!**
2. Go to **Settings** → paste your [OpenRouter API key](https://openrouter.ai/keys) → Save (encrypted).
3. As a member, log entries → click **Enhance** → see professional rewrite + category + toggle.

> Without a key, the app still works fully offline for entry creation; Enhance shows a clear error.

---

## Project Structure
```
.
├── app.py                 # Flask app, routes, AI, exports
├── config.py              # Config (env, session, DB path)
├── db.py                  # SQLite schema, connection, Fernet encryption helpers
├── seed.py                # One-time seed: manager + members + samples
├── requirements.txt
├── .env.example
├── instance/
│   ├── worklog.db         # SQLite (created on first run)
│   └── flask_session/     # Filesystem sessions
└── templates/
    ├── base.html
    ├── login.html
    ├── member_dashboard.html
    ├── manager_dashboard.html
    ├── _entry_card.html          # member entry card (with HTMX enhance)
    ├── _entry_card_manager.html  # manager variant
    ├── manage_team.html
    ├── settings.html
    ├── edit_entry.html
    ├── report_pdf.html     # HTML for pdfkit
    └── error.html
```

## Database Schema
```sql
-- users
id PK, name TEXT, email UNIQUE (NOCASE), password_hash TEXT (bcrypt),
role CHECK('manager','member'), created_at DATETIME, created_by FK

-- settings
key PK, value TEXT (encrypted for openrouter_api_key), updated_at

-- entries
id PK, user_id FK CASCADE,
title_raw TEXT, description_raw TEXT,
title_enhanced TEXT, description_enhanced TEXT,
category CHECK(8 allowed or NULL),
date TEXT (YYYY-MM-DD), start_time TEXT (HH:MM), end_time TEXT (HH:MM),
duration_minutes INTEGER NOT NULL,
created_at, updated_at, enhanced_at
Indexes: (user_id, date), (date), (category)
```
See `db.py:SCHEMA` for authoritative definition. WAL + FK enabled.

## API & Routes
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET/POST | `/login` | — | Session login |
| GET | `/logout` | — | Clear session |
| GET | `/manager` | manager | Dashboard (filters: person, date_from, date_to, category) |
| GET/POST | `/manager/team` | manager | Add/remove/reset PW |
| GET/POST | `/manager/settings` | manager | Encrypted OpenRouter key + model |
| GET | `/dashboard` | member | Own logs (same filters) |
| POST | `/entries` | any | Create (manual or timer) |
| GET/POST | `/entries/<id>/edit` | owner/manager | Edit raw + date/time |
| POST/DELETE | `/entries/<id>/delete` | owner/manager | HTMX-aware delete |
| POST | `/entries/<id>/enhance` | owner/manager | Calls OpenRouter, stores enhanced |
| GET | `/export/excel` | any (scoped) | Filtered .xlsx |
| GET | `/export/word` | any (scoped) | Filtered .docx |
| GET | `/export/pdf` | any (scoped) | Filtered .pdf (pdfkit → ReportLab fallback) |
| GET | `/api/health` | — | Health check |

---

## Security & Compliance

- **Passwords**: bcrypt hashed before storage; never logged.
- **API Key**: Fernet-encrypted in `settings` table; key derived from `FLASK_SECRET_KEY` or `FERNET_KEY` env.  Never in frontend, git, or `.env` committed. Masked in UI.
- **Sessions**: Filesystem, signed, httpOnly, 8h TTL, SameSite Lax.
- **Originals**: Never overwritten. `title_raw/description_raw` immutable unless user edits; `title_enhanced/*` separate columns.
- **Offline**: Entry creation works without internet; only Enhance requires OpenRouter.
- **Exports**: Browser-downloadable, no temp files left.

## Error Handling
- Missing fields → flash + redirect
- Invalid times → 400 with message
- Enhance failures (no key, timeout, 4xx/5xx, bad JSON) → inline amber error, original visible, retryable
- PDF: tries pdfkit/wkhtmltopdf first, falls back to ReportLab with same data; clear message if both fail

## Customization
- **Change model**: Manager Settings → `openai/gpt-4o-mini` or `anthropic/claude-3-haiku` etc.
- **TTL**: Edit `PERMANENT_SESSION_LIFETIME` in `config.py`.
- **Categories**: `CATEGORIES` list in `app.py` + CHECK constraint in `db.py`.

## Production Notes
- Set `FLASK_SECRET_KEY` and `FERNET_KEY` to strong random values in `.env` (never commit).
- `app.run(debug=True)` is for local dev; for prod use `gunicorn app:app`.
- Install `wkhtmltopdf` for best PDF fidelity: `sudo apt-get install wkhtmltopdf` (or brew). Without it, ReportLab fallback is used automatically.
- Backup `instance/worklog.db` regularly; WAL mode enabled.
- Rotate Fernet key with care (existing encrypted values need re-encryption).

## Troubleshooting
- **No Enhance** → Check Settings has valid `sk-or-v1-...` key; check OpenRouter balance; try model `openai/gpt-4o-mini`.
- **PDF looks plain** → Install wkhtmltopdf; fallback is functional but simpler.
- **Session expires quickly** → Increase `PERMANENT_SESSION_LIFETIME`.
- **Port in use** → `lsof -i :5000` or change `--port`.

## License
Internal tool — not for public distribution.
