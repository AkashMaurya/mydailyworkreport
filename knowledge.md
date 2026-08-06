# Project Knowledge

## What This Is
A **Daily Work Log** web app — internal productivity tool for managers to track team members' daily work and generate compliance-ready reports. Flask + HTMX + Tailwind + Alpine.js, SQLite, session auth, OpenRouter AI enhancement.

## Key Code Locations
- `app.py` — Flask app, all routes, AI enhancement, export logic
- `config.py` — Config (env vars, session, DB path)
- `db.py` — SQLite schema (`SCHEMA`), connection, Fernet encryption helpers
- `seed.py` — Seeds first manager + sample members + sample entries
- `templates/` — Jinja2 HTML templates (base, dashboards, entry cards, etc.)
- `instance/` — SQLite DB and Flask sessions (auto-created)

## Commands
```bash
# Install
pip install -r requirements.txt

# Setup (first time)
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # FLASK_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FERNET_KEY
# Edit .env with generated values
python seed.py

# Run
python app.py          # → http://localhost:5000 (debug, auto-reload)
# OR
flask --app app run --host 0.0.0.0 --port 5000 --debug
```

## Stack & Libraries
- **Flask 3.1.3** + Flask-Session 0.8.0 (filesystem sessions)
- **HTMX** for dynamic enhance/delete
- **Tailwind CDN** (loaded via CDN in templates, no build step)
- **Alpine.js** for timer, toggles, collapsibles
- **bcrypt 5.0.0** for password hashing
- **cryptography 50.0.0** (Fernet) for API key encryption
- **openpyxl** (Excel), **python-docx** (Word), **pdfkit** + **reportlab** (PDF fallback)
- **requests** for OpenRouter API calls
- **python-dotenv** for .env loading
- No package.json, no build tools — pure Python + CDN frontend

## Conventions
- **Single-file Flask app** (`app.py`) — all routes in one file
- **Raw vs Enhanced** entries: `title_raw`/`description_raw` are immutable originals; `title_enhanced`/`description_enhanced` are AI-rewritten and stored in separate columns
- **Categories**: `Bug Fix`, `Feature Development`, `Meeting`, `Documentation`, `Research`, `Support/Maintenance`, `Design`, `Other` — enforced by CHECK constraint in DB and `CATEGORIES` list in `app.py`
- **Roles**: `manager` (can see all, manage team, manage settings) and `member` (sees only own entries)
- **HTMX patterns**: `hx-post`, `hx-delete`, `hx-swap="outerHTML"` for dynamic updates; server returns HTML snippets
- **Alpine.js**: `x-data`, `x-show`, `x-on:click` for client-side interactivity (timer, toggle raw/enhanced)
- **Tailwind**: utility-first, custom palette `slate + indigo + emerald`; fonts Inter + Fraunces
- **Jinja2 filters**: `fmt_duration` registered for duration formatting
- **Flash messages**: flashed with `error`/`success` category

## Gotchas
- **No test suite** — no automated tests exist
- **No linter/formatter** — no flake8, ruff, or black configured
- **PDF export** requires `wkhtmltopdf` system binary for best results; falls back to ReportLab automatically
- **`.env` required** for `FLASK_SECRET_KEY` and `FERNET_KEY` — without them encryption falls back to a derived key from SECRET_KEY
- **Session TTL**: 8 hours (configurable via `PERMANENT_SESSION_LIFETIME` in `config.py`)
- **OpenRouter API key** is optional — app works fully offline; only the "Enhance" button needs it
- **SQLite WAL mode** enabled — good for concurrent reads but single writer
- **No CSRF protection** on forms beyond session signing
- **`app.run(debug=True)`** — only for local dev; use gunicorn for production
- **Database file** at `instance/worklog.db` — auto-created on first run
- **Seed is idempotent** — safe to re-run, skips existing emails
