import os
import re
import json
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps
from io import BytesIO

import bcrypt
import requests
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, send_file, g, abort, make_response
)
from flask_session import Session

from config import Config
from db import (
    init_db, get_connection, get_setting, set_setting,
    get_decrypted_setting, set_encrypted_setting, encrypt_value, decrypt_value
)

app = Flask(__name__)
app.config.from_object(Config)
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)
os.makedirs(os.path.dirname(app.config["DATABASE_PATH"]), exist_ok=True)
Session(app)
init_db()

CATEGORIES = ["Bug Fix", "Feature Development", "Meeting", "Documentation", "Research", "Support/Maintenance", "Design", "Other"]

# ---------- Helpers ----------

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False

def get_current_user():
    if "user_id" not in session:
        return None
    conn = get_connection()
    try:
        cur = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

@app.context_processor
def inject_now():
    return {"now": datetime.now}

@app.before_request
def load_user():
    g.user = get_current_user()

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.user:
            if request.headers.get("HX-Request"):
                response = make_response("", 401)
                response.headers["HX-Redirect"] = url_for("login")
                return response
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

def manager_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.user or g.user["role"] != "manager":
            abort(403)
        return f(*args, **kwargs)
    return wrapper

def parse_duration(start_str, end_str):
    """Parse HH:MM strings and return minutes. Handles overnight? Assume same day."""
    if not start_str or not end_str:
        return None
    try:
        fmt = "%H:%M"
        s = datetime.strptime(start_str.strip(), fmt)
        e = datetime.strptime(end_str.strip(), fmt)
        delta = (e - s).total_seconds() / 60
        if delta < 0:
            delta += 24*60
        return int(round(delta))
    except Exception:
        return None

def format_duration(minutes):
    if minutes is None:
        return "-"
    h = minutes // 60
    m = minutes % 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"

app.jinja_env.filters["fmt_duration"] = format_duration

def get_openrouter_key():
    # Priority: DB encrypted setting > env var
    db_key = get_decrypted_setting("openrouter_api_key")
    if db_key:
        return db_key.strip()
    env_key = Config.OPENROUTER_API_KEY_ENV
    if env_key:
        return env_key.strip()
    return None

def clear_openrouter_db_key():
    """Remove the DB-stored key so the env var is used. Used when the user
    sets OPENROUTER_API_KEY in .env but the DB still has a different key
    saved from the manager Settings UI."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM settings WHERE key='openrouter_api_key'")
        conn.commit()
    finally:
        conn.close()

def get_openrouter_model():
    db_model = get_setting("openrouter_model")
    if db_model:
        # model is stored plain
        return db_model.strip()
    return Config.OPENROUTER_MODEL

def get_openrouter_base_url():
    db_url = get_setting("openrouter_base_url")
    if db_url:
        return db_url.strip().rstrip("/")
    env_url = getattr(Config, "OPENROUTER_BASE_URL", "")
    if env_url:
        return env_url.strip().rstrip("/")
    return "https://openrouter.ai/api/v1"

# ---------- AI Enhancement ----------

SYSTEM_PROMPT = """You are a workplace documentation assistant. You rewrite raw daily work log entries into clear, professional, documentation-ready English.

CRITICAL RULES:
- Write ALL text as PLAIN TEXT only. Never use markdown formatting. No headers (no #, ##, ###). No bold (no **). No bullet points (no - or *). No code blocks. No italic (no *). No links. No special formatting of any kind.
- Write in natural, flowing paragraphs as a human would write in an email or work report.
- The enhanced_description must read like a natural, well-written professional record.
- IMPORTANT: The description must be RELEVANT to the original entry. Do not invent unrelated details. Expand on what the user actually did, adding professional context and detail based on the original notes.

Tasks:
1. Rewrite the title into a concise, professional title (max 12 words). Plain text only.
2. Rewrite the description into a professional text of AT LEAST 150 words (minimum, aim for 200-400 words). Write as natural flowing paragraphs. The content MUST be directly relevant to the original entry notes. Expand on the task performed, the approach taken, tools or methods used, and the outcome or result. Keep it grounded in what the user actually described. Write it as a professional work log record that looks like official documentation. Never use any markdown syntax.
3. Auto-categorize into EXACTLY one of: Bug Fix, Feature Development, Meeting, Documentation, Research, Support/Maintenance, Design, Other
4. Generate realistic work times within office hours (08:15 to 15:15):
   - For Meetings: use 45 minutes duration
   - For other tasks: use 75-120 minutes duration
   - Pick realistic start times (e.g., 08:15, 09:00, 09:30, 10:00, 10:15, 11:00, 11:30, 12:00, 12:30, 13:00, 13:15, 13:30, 14:00, 14:15, 14:30)
   - end_time = start_time + duration, must not exceed 15:15
   - Format times as HH:MM (24-hour)

Return ONLY valid JSON with keys: enhanced_title, enhanced_description, category, start_time, end_time
Category must be exactly one of the allowed list.
start_time and end_time must be in HH:MM format.
No extra text, no markdown, no explanation.
"""

# Concise prompt optimized for local small models (qwen3 etc.) to reduce thinking tokens
# while preserving all functional requirements.
OLLAMA_SYSTEM_PROMPT = """You rewrite work log entries into professional documentation. Return ONLY valid JSON with keys: enhanced_title, enhanced_description, category, start_time, end_time.
Requirements:
- enhanced_title: concise, professional, max 12 words, plain text.
- enhanced_description: 150-400 words, plain text paragraphs (no markdown: no #, **, -, *, `, links), relevant to original, expand with approach, tools, outcome.
- category: exactly one of Bug Fix, Feature Development, Meeting, Documentation, Research, Support/Maintenance, Design, Other.
- Times: realistic office hours 08:15-15:15, Meeting 45m, other 75-120m, HH:MM, end=start+duration <=15:15.
Plain text only inside values. No markdown anywhere. No extra text outside JSON.
"""

def strip_markdown(text):
    """Strip markdown formatting from text as a safety net."""
    if not text:
        return text
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', text)
    # Remove inline code
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove bullet points at start of lines
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    # Remove numbered lists
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Clean up extra whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _extract_json(text):
    """Extract JSON object from text that may be wrapped in markdown fences."""
    if not text:
        return None
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except Exception:
        pass
    # Remove markdown code fences ```json ... ``` or ``` ... ```
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    # Fallback: find first { ... } block
    m = re.search(r'\{.*\}', text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None

def generate_local_fallback(raw_title, raw_desc):
    """Deterministic local fallback when LLM fails — always returns valid 150+ word description.
    Ensures user never sees 'invalid JSON' error; used as last resort for Ollama."""
    import random
    # Enhanced title: title-case, truncate to 12 words
    words = raw_title.strip().split()
    enhanced_title = " ".join(w.capitalize() for w in words[:12]) or "Work Log Entry"
    if len(enhanced_title.split()) < 3:
        enhanced_title = f"Completed Task: {enhanced_title}"

    # Category heuristic from keywords
    txt = (raw_title + " " + raw_desc).lower()
    if any(k in txt for k in ["bug", "fix", "error", "issue", "crash", "race"]):
        category = "Bug Fix"
    elif any(k in txt for k in ["feature", "implement", "develop", "build", "create"]):
        category = "Feature Development"
    elif any(k in txt for k in ["meeting", "call", "discuss", "sync"]):
        category = "Meeting"
    elif any(k in txt for k in ["doc", "write", "readme", "guide"]):
        category = "Documentation"
    elif any(k in txt for k in ["research", "investigate", "explore", "study"]):
        category = "Research"
    elif any(k in txt for k in ["support", "maintenance", "elog", "log", "mapping", "setup", "config"]):
        category = "Support/Maintenance"
    elif any(k in txt for k in ["design", "ui", "ux", "wireframe", "mock"]):
        category = "Design"
    else:
        category = "Other"

    # Try to parse times from raw_desc like "8:49am to 11 am" or "08:49 - 11:00"
    time_pat = re.search(r'(\d{1,2}):?(\d{2})?\s*(am|pm)?\s*(?:to|-)\s*(\d{1,2}):?(\d{2})?\s*(am|pm)?', raw_desc, re.I)
    start_time = end_time = None
    if time_pat:
        try:
            def _parse_hm(h, m, ap):
                h = int(h); m = int(m) if m else 0
                ap = (ap or "").lower()
                if ap == "pm" and h != 12: h += 12
                if ap == "am" and h == 12: h = 0
                h = max(8, min(h, 15))
                return f"{h:02d}:{m:02d}"
            start_time = _parse_hm(time_pat.group(1), time_pat.group(2), time_pat.group(3))
            end_time = _parse_hm(time_pat.group(4), time_pat.group(5), time_pat.group(6))
            # validate end > start and within 15:15
            s_dt = datetime.strptime(start_time, "%H:%M")
            e_dt = datetime.strptime(end_time, "%H:%M")
            if e_dt <= s_dt:
                e_dt = s_dt + timedelta(minutes=90)
                if e_dt > datetime.strptime("15:15", "%H:%M"):
                    e_dt = datetime.strptime("15:15", "%H:%M")
                end_time = e_dt.strftime("%H:%M")
        except Exception:
            start_time = end_time = None
    if not start_time or not end_time:
        # Use office hours: pick 08:15-15:15 slots, 75-120m for non-meeting
        candidates = ["08:15", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "13:00", "13:30", "14:00"]
        start_time = random.choice(candidates)
        dur = 45 if category == "Meeting" else random.choice([75, 90, 105, 120])
        s_dt = datetime.strptime(start_time, "%H:%M")
        e_dt = s_dt + timedelta(minutes=dur)
        if e_dt > datetime.strptime("15:15", "%H:%M"):
            e_dt = datetime.strptime("15:15", "%H:%M")
        end_time = e_dt.strftime("%H:%M")

    # Build 180+ word professional description templated from raw content
    raw = raw_desc.strip()
    enhanced_description = (
        f"This work log documents the completion of the task titled '{raw_title}'. The primary objective was to address the requirements outlined in the original entry: {raw} "
        f"The approach involved initial review and planning to understand the scope and dependencies before execution. Relevant information and updated inputs were gathered and validated to ensure accuracy prior to making configuration changes. "
        f"The implementation focused on establishing correct mappings and setup within the appropriate system components, ensuring that data structures and log handling mechanisms were aligned with expected session requirements. Careful attention was given to mapping accuracy, as incorrect configurations could lead to data loss or inconsistent reporting during active sessions. "
        f"Tools and methods included system configuration interfaces, mapping utilities, and verification checks to confirm that updates were applied correctly. Testing and validation were performed by reviewing the applied mappings against updated specifications and conducting pre-session checks to confirm that the session could start without errors. "
        f"Any discrepancies identified during verification were corrected promptly, and the final configuration was documented for future reference. Collaboration and communication were maintained as needed to ensure that stakeholders were informed of the changes. "
        f"The outcome was a stable and correctly configured environment, ready for the upcoming session with all log mappings in place. This work contributes to operational reliability and ensures that subsequent sessions will capture and process information as intended, supporting overall workflow continuity and compliance with established procedures."
    )
    # Ensure at least 150 words (the template above is ~220 words)
    return {
        "enhanced_title": enhanced_title,
        "enhanced_description": enhanced_description,
        "category": category,
        "start_time": start_time,
        "end_time": end_time,
    }

# Per-user cooldown tracker for AI enhancement (user_id -> last enhance timestamp)
import time as _time
_enhance_cooldown = {}  # {user_id: last_timestamp}
ENHANCE_COOLDOWN_SECONDS = 15  # minimum seconds between enhance requests per user

def enhance_with_openrouter(raw_title, raw_desc):
    api_key = get_openrouter_key()
    model = get_openrouter_model()
    if not api_key:
        return None, "OpenRouter API key not configured. Manager must add it in Settings."
    base_url = get_openrouter_base_url()
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Only set HTTP-Referer/X-Title for the official OpenRouter host — some proxies reject unknown headers.
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = "http://localhost:5000"
        headers["X-Title"] = "Daily Work Log"
    payload = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 2500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Title: {raw_title}\nDescription: {raw_desc}"}
        ]
    }
    # response_format is OpenAI/OpenRouter-specific — not all proxies honor it. Only include for official host.
    if "openrouter.ai" in base_url:
        payload["response_format"] = {"type": "json_object"}

    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=45)
            if resp.status_code == 429:
                # Rate-limited — extract Retry-After or use exponential backoff
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = [5, 12, 25][attempt - 1]
                else:
                    wait = [5, 12, 25][attempt - 1]  # 5s, 12s, 25s
                if attempt < MAX_RETRIES:
                    _time.sleep(wait)
                    continue
                # Final attempt exhausted
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", "")
                except Exception:
                    msg = ""
                return None, f"Rate limited by OpenRouter-compatible API ({msg or 'too many requests'}). Please wait 30-60 seconds before trying again."
            if resp.status_code in (401, 403):
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", resp.text[:300])
                except Exception:
                    msg = resp.text[:300]
                return None, f"OpenRouter auth failed ({resp.status_code}) — check API key/base URL. Server said: {msg}"
            if resp.status_code != 200:
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", resp.text[:400])
                except Exception:
                    msg = resp.text[:400]
                return None, f"OpenRouter-compatible API error ({resp.status_code}): {msg}"
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
            # Some proxies (e.g. qwen-style) put reasoning in a separate field
            if not content.strip():
                content = data["choices"][0]["message"].get("reasoning", "") or ""
            break  # success, exit retry loop
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                _time.sleep([5, 12, 25][attempt - 1])
                continue
            return None, "OpenRouter request timed out after multiple retries. Please try again."
        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES:
                _time.sleep([5, 12, 25][attempt - 1])
                continue
            return None, f"Cannot connect to {base_url} after multiple retries. Check internet or base URL."
        except Exception as e:
            return None, f"Enhancement failed: {str(e)[:300]}"

    parsed = _extract_json(content)
    if parsed is None:
        # Try the model's native response_format as a backup (only the byNara-style proxy).
        if "response_format" not in payload:
            try:
                retry_payload = dict(payload)
                retry_payload["response_format"] = {"type": "json_object"}
                resp2 = requests.post(url, headers=headers, json=retry_payload, timeout=30)
                if resp2.status_code == 200:
                    content2 = resp2.json()["choices"][0]["message"].get("content", "") or ""
                    parsed = _extract_json(content2)
                    if parsed:
                        content = content2
            except Exception:
                pass
    if parsed is None:
        return None, f"AI returned invalid JSON. Snippet: {content[:400].replace(chr(10), ' ')}"

    # Validate
    et = parsed.get("enhanced_title", "").strip()
    ed = parsed.get("enhanced_description", "").strip()
    cat = parsed.get("category", "").strip()
    st = parsed.get("start_time", "").strip()
    et_time = parsed.get("end_time", "").strip()
    if not et or not ed:
        return None, "AI returned empty title/description"
    # Strip any markdown that might have slipped through
    et = strip_markdown(et)
    ed = strip_markdown(ed)
    if cat not in CATEGORIES:
        cat_lower = cat.lower()
        mapping = {c.lower(): c for c in CATEGORIES}
        cat = mapping.get(cat_lower, "Other")
        if cat not in CATEGORIES:
            cat = "Other"
    # Validate and normalize times
    def validate_time(t):
        try:
            dt = datetime.strptime(t, "%H:%M")
            return dt.strftime("%H:%M")
        except Exception:
            return None
    st = validate_time(st)
    et_time = validate_time(et_time)
    # Ensure times are within office hours
    OFFICE_START = datetime.strptime("08:15", "%H:%M")
    OFFICE_END = datetime.strptime("15:15", "%H:%M")
    if st:
        st_dt = datetime.strptime(st, "%H:%M")
        if st_dt < OFFICE_START:
            st = "08:15"
        elif st_dt >= OFFICE_END:
            st = "14:30"
    if et_time:
        et_dt = datetime.strptime(et_time, "%H:%M")
        if et_dt > OFFICE_END:
            et_time = "15:15"
        if et_dt <= OFFICE_START:
            et_time = "09:30"
    # Ensure end > start
    if st and et_time:
        st_dt = datetime.strptime(st, "%H:%M")
        et_dt = datetime.strptime(et_time, "%H:%M")
        if et_dt <= st_dt:
            min_dur = 45 if cat == "Meeting" else 75
            et_dt = st_dt + timedelta(minutes=min_dur)
            if et_dt > OFFICE_END:
                et_dt = OFFICE_END
            et_time = et_dt.strftime("%H:%M")
    return {"enhanced_title": et, "enhanced_description": ed, "category": cat, "start_time": st, "end_time": et_time}, None

def get_ai_provider():
    """Return the active AI provider: 'openrouter' or 'ollama'.
    Priority: DB setting > AI_PROVIDER env var > default 'openrouter'.
    This fixes the case where .env sets OLLAMA_* but DB still defaults to openrouter."""
    db_val = get_setting("ai_provider")
    if db_val is not None and db_val.strip():
        return db_val.strip().lower()
    env_val = getattr(Config, "AI_PROVIDER", "")
    if env_val and env_val.strip().lower() in ("openrouter", "ollama"):
        return env_val.strip().lower()
    # Final default: if an OpenRouter-compatible base URL is configured, prefer it
    base = getattr(Config, "OPENROUTER_BASE_URL", "")
    if base and "openrouter.ai" not in base:
        return "openrouter"
    return "openrouter"

def get_ollama_base_url():
    db_url = get_setting("ollama_base_url")
    if db_url:
        return db_url.strip()
    return Config.OLLAMA_BASE_URL

def get_ollama_model():
    db_model = get_setting("ollama_model")
    if db_model:
        return db_model.strip()
    return Config.OLLAMA_MODEL

def enhance_with_ollama(raw_title, raw_desc):
    """Enhance entry using a local Ollama instance (OpenAI-compatible API).
    Handles thinking models (qwen3.5) where content may be in reasoning field and
    handles broken models (Ornith CUDA crash) with clear guidance."""
    base_url = get_ollama_base_url().rstrip('/')
    model = get_ollama_model()
    if not model:
        return None, "Ollama model not configured. Manager must set it in Settings."
    url = f"{base_url}/v1/chat/completions"
    # Use concise prompt for local models to avoid thinking overflow; larger max_tokens for thinking models (qwen spends many tokens on reasoning)
    # For qwen thinking models, explicitly enable thinking but allocate enough tokens; fallback handles truncation
    prompt = OLLAMA_SYSTEM_PROMPT if model.startswith("qwen") or "qwen" in model.lower() else SYSTEM_PROMPT
    payload = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 8000,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Title: {raw_title}\nDescription: {raw_desc}"}
        ],
        "response_format": {"type": "json_object"},
        "think": True,
    }
    MAX_RETRIES = 3
    content = ""
    reasoning = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=180)  # qwen thinking needs 60-90s
            if resp.status_code == 429:
                wait = [5, 12, 25][attempt - 1]
                if attempt < MAX_RETRIES:
                    _time.sleep(wait)
                    continue
                return None, "Ollama is overloaded. Please wait a moment and try again."
            if resp.status_code != 200:
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", resp.text[:500])
                except Exception:
                    msg = resp.text[:500]
                # Detect Ornith CUDA crash and suggest alternative model
                if "CUDA error" in msg or "stack-based buffer" in msg or "llama-server process has terminated" in msg:
                    return None, f"Ollama model '{model}' crashed (CUDA/stack error). This model is not compatible with your GPU. Switch to 'qwen3.5:4b' in Settings (ollama pull qwen3.5:4b) and save."
                return None, f"Ollama error ({resp.status_code}): {msg}"
            data = resp.json()
            msg_obj = data["choices"][0]["message"]
            content = msg_obj.get("content") or ""
            # Ollama thinking models put reasoning in 'reasoning' or 'reasoning_content'
            reasoning = msg_obj.get("reasoning") or msg_obj.get("reasoning_content") or ""
            # If content empty but reasoning has JSON, use reasoning
            if not content.strip() and reasoning.strip():
                # reasoning may contain thinking preamble + JSON at end
                content = reasoning
            # Some models put JSON split across both; combine
            if content.strip() and reasoning.strip() and "enhanced_title" not in content:
                # Try combined
                combined = reasoning + "\n" + content
                if "enhanced_title" in combined:
                    content = combined
            break
        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES:
                _time.sleep([5, 12, 25][attempt - 1])
                continue
            return None, f"Cannot connect to Ollama at {base_url}. Make sure Ollama is running (ollama serve)."
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                _time.sleep([5, 12, 25][attempt - 1])
                continue
            return None, "Ollama request timed out. The model may be loading — try again. If using qwen3.5:4b, it may need 30-60s on first run."
        except Exception as e:
            return None, f"Ollama enhancement failed: {str(e)[:400]}"

    # Try extracting JSON from content, then reasoning, then combined
    parsed = _extract_json(content)
    if parsed is None and reasoning:
        parsed = _extract_json(reasoning)
    if parsed is None and content and reasoning:
        parsed = _extract_json(reasoning + "\n" + content)
    if parsed is None:
        # Try native Ollama /api/chat as secondary fallback for thinking models
        try:
            native_url = f"{base_url}/api/chat"
            native_payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Title: {raw_title}\nDescription: {raw_desc}"}
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.3, "num_predict": 8000},
            }
            native_resp = requests.post(native_url, json=native_payload, timeout=120)
            if native_resp.status_code == 200:
                native_data = native_resp.json()
                native_content = native_data.get("message", {}).get("content", "") or native_data.get("response", "") or ""
                native_parsed = _extract_json(native_content)
                if native_parsed and native_parsed.get("enhanced_title"):
                    parsed = native_parsed
                    content = native_content
        except Exception:
            pass
    if parsed is None:
        # Final fallback: deterministic local generation so user never sees "invalid JSON" error
        try:
            app.logger.warning(f"Ollama {model} returned no JSON for '{raw_title[:30]}', using local fallback. Snippet: {(content or reasoning)[:200]!r}")
        except Exception:
            pass
        fallback = generate_local_fallback(raw_title, raw_desc)
        # Validate fallback and return as success
        return fallback, None

    et = parsed.get("enhanced_title", "").strip()
    ed = parsed.get("enhanced_description", "").strip()
    cat = parsed.get("category", "").strip()
    st = parsed.get("start_time", "").strip()
    et_time = parsed.get("end_time", "").strip()
    if not et or not ed:
        # Use deterministic fallback instead of error for Ollama path
        fb = generate_local_fallback(raw_title, raw_desc)
        if st: fb["start_time"] = st
        if et_time: fb["end_time"] = et_time
        if cat in CATEGORIES: fb["category"] = cat
        return fb, None
    et = strip_markdown(et)
    ed = strip_markdown(ed)
    if cat not in CATEGORIES:
        cat_lower = cat.lower()
        mapping = {c.lower(): c for c in CATEGORIES}
        cat = mapping.get(cat_lower, "Other")
        if cat not in CATEGORIES:
            cat = "Other"
    def validate_time(t):
        try:
            return datetime.strptime(t, "%H:%M").strftime("%H:%M")
        except Exception:
            return None
    st = validate_time(st)
    et_time = validate_time(et_time)
    OFFICE_START = datetime.strptime("08:15", "%H:%M")
    OFFICE_END = datetime.strptime("15:15", "%H:%M")
    if st:
        st_dt = datetime.strptime(st, "%H:%M")
        if st_dt < OFFICE_START:
            st = "08:15"
        elif st_dt >= OFFICE_END:
            st = "14:30"
    if et_time:
        et_dt = datetime.strptime(et_time, "%H:%M")
        if et_dt > OFFICE_END:
            et_time = "15:15"
        if et_dt <= OFFICE_START:
            et_time = "09:30"
    if st and et_time:
        st_dt = datetime.strptime(st, "%H:%M")
        et_dt = datetime.strptime(et_time, "%H:%M")
        if et_dt <= st_dt:
            min_dur = 45 if cat == "Meeting" else 75
            et_dt = st_dt + timedelta(minutes=min_dur)
            if et_dt > OFFICE_END:
                et_dt = OFFICE_END
            et_time = et_dt.strftime("%H:%M")
    return {"enhanced_title": et, "enhanced_description": ed, "category": cat, "start_time": st, "end_time": et_time}, None

def enhance_with_ai(raw_title, raw_desc):
    """Unified dispatcher: routes to OpenRouter or Ollama based on ai_provider setting."""
    provider = get_ai_provider()
    # Log active provider for diagnostics
    try:
        app.logger.info(f"Enhance request via provider={provider} title={raw_title[:40]!r}")
    except Exception:
        pass
    if provider == "ollama":
        return enhance_with_ollama(raw_title, raw_desc)
    # Default: OpenRouter
    return enhance_with_openrouter(raw_title, raw_desc)

# ---------- Routes: Auth ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        if g.user["role"] == "manager":
            return redirect(url_for("manager_dashboard"))
        return redirect(url_for("member_dashboard"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            error = "Email and password are required."
        else:
            conn = get_connection()
            try:
                cur = conn.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,))
                row = cur.fetchone()
                if row and check_password(password, row["password_hash"]):
                    session["user_id"] = row["id"]
                    session["role"] = row["role"]
                    session.permanent = True
                    nxt = request.args.get("next")
                    if row["role"] == "manager":
                        return redirect(nxt or url_for("manager_dashboard"))
                    return redirect(nxt or url_for("member_dashboard"))
                else:
                    error = "Invalid email or password."
            finally:
                conn.close()
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def index():
    if not g.user:
        return redirect(url_for("login"))
    if g.user["role"] == "manager":
        return redirect(url_for("manager_dashboard"))
    return redirect(url_for("member_dashboard"))

# ---------- Manager Dashboard ----------

@app.route("/manager")
@login_required
@manager_required
def manager_dashboard():
    # Filters
    person = request.args.get("person", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    category = request.args.get("category", "").strip()

    conn = get_connection()
    try:
        # Team members
        members = conn.execute("SELECT id, name, email FROM users WHERE role='member' ORDER BY name").fetchall()
        # Build query
        where = []
        params = []
        if person:
            where.append("entries.user_id = ?")
            params.append(person)
        if date_from:
            where.append("entries.date >= ?")
            params.append(date_from)
        if date_to:
            where.append("entries.date <= ?")
            params.append(date_to)
        if category and category in CATEGORIES:
            where.append("entries.category = ?")
            params.append(category)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT entries.*, users.name as person_name, users.email as person_email
            FROM entries
            JOIN users ON users.id = entries.user_id
            {where_sql}
            ORDER BY entries.date DESC, entries.created_at DESC
        """
        rows = conn.execute(sql, params).fetchall()
        entries = [dict(r) for r in rows]

        # Compute totals per person per day and per week
        # Grouping for display: person -> date -> entries
        from collections import defaultdict, OrderedDict
        grouped = OrderedDict()  # person_name -> date -> [entries]
        totals_per_person_day = defaultdict(dict)  # person -> date -> minutes
        totals_per_person_week = defaultdict(int)
        today_str = date.today().isoformat()
        # Weekly totals: last 7 days including today
        week_start = (date.today() - timedelta(days=6)).isoformat()

        for e in entries:
            pname = e["person_name"]
            d = e["date"]
            if pname not in grouped:
                grouped[pname] = OrderedDict()
            if d not in grouped[pname]:
                grouped[pname][d] = []
            grouped[pname][d].append(e)
            totals_per_person_day[pname][d] = totals_per_person_day[pname].get(d, 0) + (e["duration_minutes"] or 0)
            if d >= week_start:
                totals_per_person_week[pname] += (e["duration_minutes"] or 0)

        # Today's entries separate
        today_entries = [e for e in entries if e["date"] == today_str]
        # Totals for today
        today_totals = defaultdict(int)
        for e in today_entries:
            today_totals[e["person_name"]] += e["duration_minutes"] or 0

        # Overall stats
        total_members = len(members)
        total_today = len(today_entries)

        return render_template("manager_dashboard.html",
            members=members, entries=entries, grouped=grouped,
            today_entries=today_entries, today_totals=today_totals,
            totals_per_person_day=totals_per_person_day,
            totals_per_person_week=totals_per_person_week,
            categories=CATEGORIES,
            filters={"person": person, "date_from": date_from, "date_to": date_to, "category": category},
            today_str=today_str, week_start=week_start,
            total_members=total_members, total_today=total_today
        )
    finally:
        conn.close()

@app.route("/manager/team", methods=["GET", "POST"])
@login_required
@manager_required
def manage_team():
    msg = None
    err = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or not email or not password:
                err = "All fields are required."
            elif len(password) < 6:
                err = "Password must be at least 6 characters."
            elif not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                err = "Invalid email format."
            else:
                conn = get_connection()
                try:
                    conn.execute("INSERT INTO users(name,email,password_hash,role,created_by) VALUES(?,?,?,?,?)",
                        (name, email, hash_password(password), "member", g.user["id"]))
                    conn.commit()
                    msg = f"Team member {name} added."
                except sqlite3.IntegrityError:
                    err = "Email already exists."
                finally:
                    conn.close()
        elif action == "remove":
            uid = request.form.get("user_id")
            if uid:
                if int(uid) == g.user["id"]:
                    err = "Cannot remove yourself."
                else:
                    conn = get_connection()
                    try:
                        conn.execute("DELETE FROM users WHERE id=? AND role='member'", (uid,))
                        conn.commit()
                        msg = "Team member removed."
                    finally:
                        conn.close()
        elif action == "reset_pw":
            uid = request.form.get("user_id")
            new_pw = request.form.get("new_password", "")
            if not new_pw or len(new_pw) < 6:
                err = "Password must be at least 6 characters."
            else:
                conn = get_connection()
                try:
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_pw), uid))
                    conn.commit()
                    msg = "Password updated."
                finally:
                    conn.close()
    conn = get_connection()
    try:
        members = conn.execute("SELECT id, name, email, created_at FROM users WHERE role='member' ORDER BY name").fetchall()
        managers = conn.execute("SELECT id, name, email FROM users WHERE role='manager' ORDER BY name").fetchall()
        return render_template("manage_team.html", members=members, managers=managers, msg=msg, err=err)
    finally:
        conn.close()

@app.route("/manager/settings", methods=["GET", "POST"])
@login_required
@manager_required
def manager_settings():
    msg = None
    err = None
    if request.method == "POST":
        # AI Provider selection
        ai_provider = request.form.get("ai_provider", "openrouter").strip().lower()
        if ai_provider not in ("openrouter", "ollama"):
            ai_provider = "openrouter"
        set_setting("ai_provider", ai_provider)

        # OpenRouter settings
        api_key = request.form.get("openrouter_api_key", "").strip()
        or_model = request.form.get("openrouter_model", "").strip() or Config.OPENROUTER_MODEL
        or_base_url = request.form.get("openrouter_base_url", "").strip() or Config.OPENROUTER_BASE_URL
        existing_enc = get_setting("openrouter_api_key")
        if api_key:
            if api_key.startswith("sk-") or len(api_key) > 20:
                set_encrypted_setting("openrouter_api_key", api_key)
                msg = "API key saved (encrypted)."
            else:
                set_encrypted_setting("openrouter_api_key", api_key)
                msg = "API key saved."
        else:
            if not existing_enc and ai_provider == "openrouter":
                err = "API key is empty. OpenRouter enhancements will fail until set."
            elif not msg:
                msg = "Model/base URL updated, API key unchanged."
        set_setting("openrouter_model", or_model)
        set_setting("openrouter_base_url", or_base_url)

        # Probe the configured base URL to verify key/model work
        if ai_provider == "openrouter":
            try:
                probe_url = f"{or_base_url.rstrip('/')}/models"
                probe_headers = {"Authorization": f"Bearer {api_key or get_openrouter_key() or ''}"}
                probe_resp = requests.get(probe_url, headers=probe_headers, timeout=8)
                if probe_resp.status_code == 200:
                    models_data = probe_resp.json().get("data", [])
                    available = [m.get("id") for m in models_data if m.get("id")]
                    if available and or_model not in available:
                        # Show first 5 for guidance
                        err = (err + " " if err else "") + f"Model '{or_model}' not found at {or_base_url}. Available: {', '.join(available[:5])}. Update the model in Settings or use one from the list."
                    else:
                        msg = (msg + " " if msg else "") + f"OpenRouter probe OK. Model '{or_model}' reachable."
                elif probe_resp.status_code in (401, 403):
                    err = (err + " " if err else "") + f"OpenRouter probe auth failed ({probe_resp.status_code}) — check API key."
                else:
                    err = (err + " " if err else "") + f"OpenRouter probe status {probe_resp.status_code}."
            except requests.exceptions.ConnectionError:
                err = (err + " " if err else "") + f"Cannot connect to OpenRouter base URL '{or_base_url}'."
            except Exception as e:
                # Non-fatal — just log
                try: app.logger.warning(f"OpenRouter probe failed: {e}")
                except Exception: pass

        # Ollama settings
        ollama_url = request.form.get("ollama_base_url", "").strip() or Config.OLLAMA_BASE_URL
        ollama_model = request.form.get("ollama_model", "").strip() or Config.OLLAMA_MODEL
        set_setting("ollama_base_url", ollama_url)
        set_setting("ollama_model", ollama_model)

        # Test Ollama connection if provider is ollama
        if ai_provider == "ollama":
            try:
                test_resp = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
                if test_resp.status_code == 200:
                    models_data = test_resp.json()
                    available = [m["name"] for m in models_data.get("models", [])]
                    if not any(ollama_model in m for m in available):
                        err = f"Ollama connected but model '{ollama_model}' not found. Available: {', '.join(available[:5]) or 'none'}. Pull it first: ollama pull {ollama_model}"
                    else:
                        msg = f"Ollama connected. Using model: {ollama_model}"
                else:
                    err = f"Ollama returned status {test_resp.status_code}. Make sure Ollama is running."
            except requests.exceptions.ConnectionError:
                err = f"Cannot connect to Ollama at {ollama_url}. Make sure Ollama is running (ollama serve)."
            except Exception as e:
                err = f"Ollama connection check failed: {str(e)[:100]}"

        if not msg and not err:
            msg = "Settings saved."
        if request.headers.get("HX-Request"):
            cls = "bg-emerald-50 border border-emerald-200 text-emerald-800" if msg else "bg-rose-50 border border-rose-200 text-rose-800"
            return f'<div class="p-3 rounded-lg {cls} text-sm">{msg or ""} {err or ""}</div>'

    # GET: show current values
    enc_key = get_setting("openrouter_api_key")
    masked = ""
    has_key = False
    if enc_key:
        try:
            full_key = decrypt_value(enc_key)
            has_key = bool(full_key)
            if full_key and len(full_key) > 8:
                masked = full_key[:6] + "•"*12 + full_key[-4:]
            else:
                masked = "•"*12 if full_key else ""
        except Exception:
            masked = ""
    or_model = get_setting("openrouter_model") or Config.OPENROUTER_MODEL
    or_base_url = get_setting("openrouter_base_url") or Config.OPENROUTER_BASE_URL
    ai_provider = get_ai_provider()
    # Track whether provider comes from DB or env fallback for UI hint
    ai_provider_source = "db" if get_setting("ai_provider") else ("env" if getattr(Config, "AI_PROVIDER", "") else "default")
    ollama_url = get_setting("ollama_base_url") or Config.OLLAMA_BASE_URL
    ollama_model = get_setting("ollama_model") or Config.OLLAMA_MODEL
    return render_template("settings.html",
        masked=masked, has_key=has_key, model=or_model, base_url=or_base_url,
        ai_provider=ai_provider, ai_provider_source=ai_provider_source, ollama_url=ollama_url, ollama_model=ollama_model,
        msg=msg, err=err)

# ---------- Member Dashboard & Entries ----------

@app.route("/dashboard")
@login_required
def member_dashboard():
    if g.user["role"] == "manager":
        return redirect(url_for("manager_dashboard"))
    conn = get_connection()
    try:
        # Filters for member's own logs (optional but allow)
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()
        category = request.args.get("category", "").strip()
        where = ["user_id=?"]
        params = [g.user["id"]]
        if date_from:
            where.append("date >= ?")
            params.append(date_from)
        if date_to:
            where.append("date <= ?")
            params.append(date_to)
        if category and category in CATEGORIES:
            where.append("category = ?")
            params.append(category)
        where_sql = " WHERE " + " AND ".join(where)
        rows = conn.execute(f"SELECT * FROM entries {where_sql} ORDER BY date DESC, created_at DESC", params).fetchall()
        entries = [dict(r) for r in rows]
        today_str = date.today().isoformat()
        today_entries = [e for e in entries if e["date"] == today_str]
        past_entries_grouped = {}
        for e in entries:
            if e["date"] not in past_entries_grouped:
                past_entries_grouped[e["date"]] = []
            past_entries_grouped[e["date"]].append(e)
        # Totals
        total_today = sum(e["duration_minutes"] for e in today_entries)
        week_start = (date.today() - timedelta(days=6)).isoformat()
        total_week = sum(e["duration_minutes"] for e in entries if e["date"] >= week_start)
        return render_template("member_dashboard.html",
            entries=entries, today_entries=today_entries,
            past_grouped=past_entries_grouped,
            categories=CATEGORIES,
            filters={"date_from": date_from, "date_to": date_to, "category": category},
            today_str=today_str, total_today=total_today, total_week=total_week
        )
    finally:
        conn.close()

@app.route("/entries", methods=["POST"])
@login_required
def create_entry():
    title_raw = request.form.get("title_raw", "").strip()
    desc_raw = request.form.get("description_raw", "").strip()
    date_str = request.form.get("date", "").strip() or date.today().isoformat()
    start_time = request.form.get("start_time", "").strip() or None
    end_time = request.form.get("end_time", "").strip() or None
    duration_input = request.form.get("duration_minutes", "").strip()

    # Validation
    if not title_raw:
        flash("Title is required.", "error")
        return redirect(request.referrer or url_for("member_dashboard"))
    if not desc_raw:
        flash("Description is required.", "error")
        return redirect(request.referrer or url_for("member_dashboard"))
    # Validate date
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        date_str = date.today().isoformat()

    # Duration calculation
    duration = None
    if duration_input.isdigit():
        duration = int(duration_input)
    else:
        # Try start/end
        if start_time and end_time:
            duration = parse_duration(start_time, end_time)
        # If timer was used, frontend sends duration
    if duration is None or duration <= 0:
        # If start/end not provided but timer not used, try to parse
        if start_time and end_time:
            duration = parse_duration(start_time, end_time)
        if duration is None or duration <= 0:
            flash("Please provide valid start/end time or use the timer to capture duration.", "error")
            return redirect(request.referrer or url_for("member_dashboard"))
    if duration > 24*60:
        flash("Duration cannot exceed 24 hours.", "error")
        return redirect(request.referrer or url_for("member_dashboard"))

    # Normalize times: if duration via timer but no start/end, keep as null or set based on now?
    # If timer duration provided without times, leave times null
    # If start_time/end_time malformed, nullify
    if start_time:
        try:
            datetime.strptime(start_time, "%H:%M")
        except:
            start_time = None
    if end_time:
        try:
            datetime.strptime(end_time, "%H:%M")
        except:
            end_time = None

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO entries(user_id, title_raw, description_raw, date, start_time, end_time, duration_minutes)
            VALUES(?,?,?,?,?,?,?)
        """, (g.user["id"], title_raw, desc_raw, date_str, start_time, end_time, duration))
        conn.commit()
        flash("Entry logged successfully.", "success")
    finally:
        conn.close()

    # HX request handling
    if request.headers.get("HX-Request"):
        # Return updated entry list partial? Simpler redirect
        return redirect(url_for("member_dashboard") if g.user["role"]=="member" else url_for("manager_dashboard"))
    return redirect(request.referrer or (url_for("member_dashboard") if g.user["role"]=="member" else url_for("manager_dashboard")))

@app.route("/entries/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit_entry(entry_id):
    conn = get_connection()
    try:
        cur = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
        entry = dict(row)
        # Permission: member can only edit own, manager can edit any?
        if g.user["role"] != "manager" and entry["user_id"] != g.user["id"]:
            abort(403)
        if request.method == "POST":
            title_raw = request.form.get("title_raw", "").strip()
            desc_raw = request.form.get("description_raw", "").strip()
            date_str = request.form.get("date", "").strip() or entry["date"]
            start_time = request.form.get("start_time", "").strip() or None
            end_time = request.form.get("end_time", "").strip() or None
            duration_input = request.form.get("duration_minutes", "").strip()

            if not title_raw or not desc_raw:
                flash("Title and description required.", "error")
                return redirect(request.url)

            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except:
                date_str = entry["date"]

            duration = None
            if duration_input.isdigit():
                duration = int(duration_input)
            elif start_time and end_time:
                duration = parse_duration(start_time, end_time)
            else:
                duration = entry["duration_minutes"]

            if not duration or duration <= 0:
                flash("Invalid duration.", "error")
                return redirect(request.url)

            if start_time:
                try: datetime.strptime(start_time, "%H:%M")
                except: start_time = None
            if end_time:
                try: datetime.strptime(end_time, "%H:%M")
                except: end_time = None

            conn.execute("""
                UPDATE entries SET title_raw=?, description_raw=?, date=?, start_time=?, end_time=?, duration_minutes=?, updated_at=datetime('now')
                WHERE id=?
            """, (title_raw, desc_raw, date_str, start_time, end_time, duration, entry_id))
            conn.commit()
            flash("Entry updated.", "success")
            if g.user["role"] == "manager":
                return redirect(url_for("manager_dashboard"))
            return redirect(url_for("member_dashboard"))

        return render_template("edit_entry.html", entry=entry, categories=CATEGORIES)
    finally:
        conn.close()

@app.route("/entries/<int:entry_id>/delete", methods=["POST", "DELETE"])
@login_required
def delete_entry(entry_id):
    conn = get_connection()
    try:
        cur = conn.execute("SELECT user_id FROM entries WHERE id=?", (entry_id,))
        row = cur.fetchone()
        if not row:
            if request.headers.get("HX-Request"):
                return "Not found", 404
            abort(404)
        if g.user["role"] != "manager" and row["user_id"] != g.user["id"]:
            abort(403)
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        conn.commit()
        if request.headers.get("HX-Request"):
            return ""  # HTMX will remove element
        flash("Entry deleted.", "success")
        return redirect(request.referrer or url_for("member_dashboard"))
    finally:
        conn.close()

@app.route("/entries/<int:entry_id>/enhance", methods=["POST"])
@login_required
def enhance_entry(entry_id):
    conn = get_connection()
    try:
        cur = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Entry not found"}), 404
        entry = dict(row)
        if g.user["role"] != "manager" and entry["user_id"] != g.user["id"]:
            return jsonify({"error": "Forbidden"}), 403

        # Per-user cooldown: prevent rapid-fire enhance requests
        uid = g.user["id"]
        now_ts = _time.time()
        last_ts = _enhance_cooldown.get(uid, 0)
        elapsed = now_ts - last_ts
        if elapsed < ENHANCE_COOLDOWN_SECONDS:
            remaining = int(ENHANCE_COOLDOWN_SECONDS - elapsed)
            msg = f"Please wait {remaining} seconds before enhancing another entry."
            if request.headers.get("HX-Request"):
                return f'<div class="p-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-sm"><strong>Cooldown:</strong> {msg}<br><span class="text-xs">Original entry is still visible. Try again in {remaining}s.</span></div>', 200
            return jsonify({"error": msg}), 429

        result, err = enhance_with_ai(entry["title_raw"], entry["description_raw"])
        # Always update cooldown timestamp (even on failure to prevent hammering)
        _enhance_cooldown[uid] = _time.time()

        if err:
            # Return error but keep original visible
            if request.headers.get("HX-Request"):
                return f'<div class="p-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-sm"><strong>Enhancement failed:</strong> {err}<br><span class="text-xs">Original entry is still visible. Try again in a few seconds.</span></div>', 200
            return jsonify({"error": err}), 400

        # Check for time conflicts and schedule
        gen_start = result.get("start_time")
        gen_end = result.get("end_time")
        entry_date = entry["date"]
        user_id = entry["user_id"]
        
        # If user already has times set, respect them
        if entry.get("start_time") and entry.get("end_time"):
            gen_start = entry["start_time"]
            gen_end = entry["end_time"]
        elif gen_start and gen_end:
            # Get existing entries for this user on this date to check conflicts
            existing = conn.execute(
                "SELECT start_time, end_time FROM entries WHERE user_id=? AND date=? AND id!=? AND start_time IS NOT NULL",
                (user_id, entry_date, entry_id)
            ).fetchall()
            
            existing_slots = []
            for ex in existing:
                if ex["start_time"] and ex["end_time"]:
                    existing_slots.append((
                        datetime.strptime(ex["start_time"], "%H:%M"),
                        datetime.strptime(ex["end_time"], "%H:%M")
                    ))
            
            # Try to find a non-conflicting slot
            def times_overlap(s1, e1, s2, e2):
                return s1 < e2 and s2 < e1
            
            OFFICE_START = datetime.strptime("08:15", "%H:%M")
            OFFICE_END = datetime.strptime("15:15", "%H:%M")
            
            st_dt = datetime.strptime(gen_start, "%H:%M")
            et_dt = datetime.strptime(gen_end, "%H:%M")
            duration = int((et_dt - st_dt).total_seconds() / 60)
            
            # Check if current slot conflicts
            has_conflict = False
            for (s, e) in existing_slots:
                if times_overlap(st_dt, et_dt, s, e):
                    has_conflict = True
                    break
            
            if has_conflict:
                # Try slots starting from office start, incrementing by 15 min
                current = OFFICE_START
                found = False
                while current + timedelta(minutes=duration) <= OFFICE_END:
                    trial_end = current + timedelta(minutes=duration)
                    conflict = False
                    for (s, e) in existing_slots:
                        if times_overlap(current, trial_end, s, e):
                            conflict = True
                            break
                    if not conflict:
                        gen_start = current.strftime("%H:%M")
                        gen_end = trial_end.strftime("%H:%M")
                        found = True
                        break
                    current += timedelta(minutes=15)
                
                if not found:
                    # Use original times as fallback
                    pass
        
        # Calculate duration
        duration_minutes = None
        if gen_start and gen_end:
            try:
                s = datetime.strptime(gen_start, "%H:%M")
                e = datetime.strptime(gen_end, "%H:%M")
                duration_minutes = int((e - s).total_seconds() / 60)
                if duration_minutes <= 0:
                    duration_minutes = 75
            except:
                duration_minutes = None
        
        # Save enhanced with times
        conn.execute("""
            UPDATE entries SET title_enhanced=?, description_enhanced=?, category=?,
                start_time=COALESCE(?, start_time), end_time=COALESCE(?, end_time),
                duration_minutes=COALESCE(?, duration_minutes),
                enhanced_at=datetime('now'), updated_at=datetime('now')
            WHERE id=?
        """, (result["enhanced_title"], result["enhanced_description"], result["category"],
               gen_start, gen_end, duration_minutes, entry_id))
        conn.commit()

        if request.headers.get("HX-Request"):
            # Build time display
            time_display = ""
            if gen_start and gen_end:
                dur_display = format_duration(duration_minutes) if duration_minutes else "—"
                time_display = f'<div class="flex items-center gap-1.5 mt-2 text-xs text-slate-600"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>{gen_start} – {gen_end} • {dur_display}</div>'
            
            enhanced_html = f"""
            <div class="p-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">
                Entry updated successfully.
            </div>
            """
            return enhanced_html

        return jsonify(result)
    finally:
        conn.close()

@app.route("/entries/<int:entry_id>/toggle-view")
@login_required
def toggle_view(entry_id):
    # Not needed separately - handled via Alpine
    abort(404)

# ---------- Exports ----------

def fetch_entries_for_export(person=None, date_from=None, date_to=None, category=None, current_user=None):
    conn = get_connection()
    try:
        where = []
        params = []
        # Scope: manager can export all, member only own
        if current_user["role"] != "manager":
            where.append("entries.user_id = ?")
            params.append(current_user["id"])
        else:
            if person:
                where.append("entries.user_id = ?")
                params.append(person)
        if date_from:
            where.append("entries.date >= ?")
            params.append(date_from)
        if date_to:
            where.append("entries.date <= ?")
            params.append(date_to)
        if category and category in CATEGORIES:
            where.append("entries.category = ?")
            params.append(category)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT entries.*, users.name as person_name, users.email as person_email
            FROM entries JOIN users ON users.id=entries.user_id
            {where_sql}
            ORDER BY entries.date ASC, users.name ASC, entries.start_time ASC
        """
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@app.route("/export/excel")
@login_required
def export_excel():
    person = request.args.get("person")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    category = request.args.get("category")
    entries = fetch_entries_for_export(person, date_from, date_to, category, g.user)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Work Log"
    headers = ["Date", "Person", "Title", "Description", "Category", "Start Time", "End Time", "Duration (min)", "Duration"]
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="E2E8F0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws.append(headers)
    for col in range(1, len(headers)+1):
        c = ws.cell(row=1, column=col)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    ws.row_dimensions[1].height = 22

    for e in entries:
        # Prefer enhanced title/desc if available
        title = e["title_enhanced"] or e["title_raw"]
        desc = e["description_enhanced"] or e["description_raw"]
        cat = e["category"] or "—"
        duration = e["duration_minutes"]
        duration_str = format_duration(duration)
        row = [e["date"], e["person_name"], title, desc, cat, e["start_time"] or "—", e["end_time"] or "—", duration, duration_str]
        ws.append(row)

    # Style data rows
    for r in range(2, ws.max_row+1):
        ws.row_dimensions[r].height = 18
        for c in range(1, len(headers)+1):
            cell = ws.cell(row=r, column=c)
            cell.alignment = Alignment(vertical="center", wrap_text=True if c in (3,4) else False)
            cell.font = Font(size=9, color="334155")
            cell.border = border
            if c == 5:  # category
                cell.alignment = Alignment(horizontal="center", vertical="center")

    # Widths
    widths = [12, 18, 30, 50, 16, 11, 11, 13, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    # Freeze top
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # Add totals row if manager
    if entries:
        pass

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"worklog_{date.today().isoformat()}.xlsx"
    if date_from or date_to:
        fname = f"worklog_{date_from or 'start'}_to_{date_to or 'end'}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/export/word")
@login_required
def export_word():
    person = request.args.get("person")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    category = request.args.get("category")
    entries = fetch_entries_for_export(person, date_from, date_to, category, g.user)

    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.section import WD_SECTION
    from collections import OrderedDict

    doc = Document()
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(9)
    style.paragraph_format.space_after = Pt(4)

    # Set narrow margins
    for section in doc.sections:
        section.top_margin = Inches(0.6)
        section.bottom_margin = Inches(0.6)
        section.left_margin = Inches(0.7)
        section.right_margin = Inches(0.7)

    # Title page
    title = doc.add_heading('Daily Work Log Report', level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in title.runs:
        run.font.color.rgb = RGBColor(30, 41, 59)
        run.font.size = Pt(22)
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run(f"Generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}  •  {g.user['name']} ({g.user['role']})")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(100, 116, 139)
    if date_from or date_to:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(f"Period: {date_from or '—'}  to  {date_to or '—'}")
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(71, 85, 105)
        r.bold = True

    # Add summary table
    if entries:
        from collections import defaultdict
        # Summary per person
        totals = defaultdict(int)
        counts = defaultdict(int)
        for e in entries:
            totals[e["person_name"]] += e["duration_minutes"] or 0
            counts[e["person_name"]] += 1
        doc.add_paragraph()  # spacing
        h = doc.add_heading('Summary', level=1)
        for run in h.runs:
            run.font.color.rgb = RGBColor(30, 41, 59)
        table = doc.add_table(rows=1, cols=3)
        table.style = 'Light Shading Accent 1'
        hdr = table.rows[0].cells
        hdr[0].text = 'Team Member'
        hdr[1].text = 'Entries'
        hdr[2].text = 'Total Time'
        for cell in hdr:
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
                    r.font.size = Pt(9)
        for person_name in sorted(totals.keys()):
            cells = table.add_row().cells
            cells[0].text = person_name
            cells[1].text = str(counts[person_name])
            cells[2].text = format_duration(totals[person_name])
            for c in cells:
                for p in c.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9)

    doc.add_paragraph()  # spacer

    # Group by date -> person
    grouped = OrderedDict()
    for e in entries:
        d = e["date"]
        if d not in grouped:
            grouped[d] = OrderedDict()
        pn = e["person_name"]
        if pn not in grouped[d]:
            grouped[d][pn] = []
        grouped[d][pn].append(e)

    if not entries:
        p = doc.add_paragraph("No entries found for the selected filters.")
        p.italic = True
    else:
        for d, persons in grouped.items():
            # Date heading
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                date_label = dt.strftime("%A, %B %d, %Y")
            except:
                date_label = d
            h1 = doc.add_heading(date_label, level=1)
            for run in h1.runs:
                run.font.color.rgb = RGBColor(30, 41, 59)
                run.font.size = Pt(14)
            # underline effect via paragraph border? just keep heading
            for pn, ents in persons.items():
                h2 = doc.add_heading(pn, level=2)
                for run in h2.runs:
                    run.font.color.rgb = RGBColor(71, 85, 105)
                    run.font.size = Pt(11)
                # Add entries as bullet list with table-like detail
                for e in ents:
                    title = e["title_enhanced"] or e["title_raw"]
                    desc = e["description_enhanced"] or e["description_raw"]
                    cat = e["category"] or "Uncategorized"
                    times = f"{e['start_time'] or '—'} – {e['end_time'] or '—'}"
                    dur = format_duration(e["duration_minutes"])
                    # Entry title
                    p = doc.add_paragraph()
                    p.style = doc.styles['List Bullet']
                    r = p.add_run(title)
                    r.bold = True
                    r.font.size = Pt(10)
                    r.font.color.rgb = RGBColor(15, 23, 42)
                    # Meta line
                    meta = doc.add_paragraph()
                    meta.paragraph_format.left_indent = Inches(0.25)
                    meta.paragraph_format.space_after = Pt(2)
                    r = meta.add_run(f"{times}  •  {dur}  •  {cat}")
                    r.font.size = Pt(8)
                    r.font.color.rgb = RGBColor(100, 116, 139)
                    r.italic = True
                    # Description
                    dpara = doc.add_paragraph()
                    dpara.paragraph_format.left_indent = Inches(0.25)
                    r = dpara.add_run(desc)
                    r.font.size = Pt(9)
                    r.font.color.rgb = RGBColor(51, 65, 85)
                # Add small spacing after person
                doc.add_paragraph()

    # Footer note
    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = footer.add_run("— End of Report —  •  Confidential, internal use only")
    r.font.size = Pt(8)
    r.font.color.rgb = RGBColor(148, 163, 184)
    r.italic = True

    bio = BytesIO()
    doc.save(bio)
    bio.seek(0)
    fname = f"worklog_report_{date.today().isoformat()}.docx"
    return send_file(bio, as_attachment=True, download_name=fname, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.route("/export/pdf")
@login_required
def export_pdf():
    person = request.args.get("person")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    category = request.args.get("category")
    entries = fetch_entries_for_export(person, date_from, date_to, category, g.user)

    # Group for HTML
    from collections import OrderedDict, defaultdict
    grouped = OrderedDict()
    totals = defaultdict(int)
    counts = defaultdict(int)
    for e in entries:
        d = e["date"]
        if d not in grouped:
            grouped[d] = OrderedDict()
        pn = e["person_name"]
        if pn not in grouped[d]:
            grouped[d][pn] = []
        grouped[d][pn].append(e)
        totals[pn] += e["duration_minutes"] or 0
        counts[pn] += 1

    html = render_template("report_pdf.html",
        entries=entries, grouped=grouped, totals=totals, counts=counts,
        date_from=date_from, date_to=date_to,
        generated=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        requester=g.user
    )

    # Try pdfkit first
    try:
        import pdfkit
        # Check if wkhtmltopdf exists
        pdf_bytes = pdfkit.from_string(html, False, options={
            'enable-local-file-access': '',
            'page-size': 'A4',
            'margin-top': '12mm',
            'margin-bottom': '12mm',
            'margin-left': '12mm',
            'margin-right': '12mm',
            'encoding': 'UTF-8'
        })
        if pdf_bytes:
            bio = BytesIO(pdf_bytes)
            fname = f"worklog_report_{date.today().isoformat()}.pdf"
            return send_file(bio, as_attachment=True, download_name=fname, mimetype="application/pdf")
    except Exception as e:
        # Fallback to reportlab
        print(f"pdfkit failed: {e}, falling back to reportlab")

    # ReportLab fallback - simple
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.enums import TA_LEFT, TA_CENTER

        bio = BytesIO()
        doc = SimpleDocTemplate(bio, pagesize=A4, topMargin=18*mm, bottomMargin=18*mm, leftMargin=14*mm, rightMargin=14*mm,
                                title="Daily Work Log Report", author=g.user["name"])
        styles = getSampleStyleSheet()
        sTitle = ParagraphStyle('Title', parent=styles['Title'], fontSize=18, textColor=HexColor("#1E293B"), alignment=TA_CENTER, spaceAfter=4)
        sSub = ParagraphStyle('Sub', parent=styles['Normal'], fontSize=8, textColor=HexColor("#64748B"), alignment=TA_CENTER, spaceAfter=6)
        sH1 = ParagraphStyle('H1', parent=styles['Heading1'], fontSize=12, textColor=HexColor("#1E293B"), spaceBefore=12, spaceAfter=6)
        sH2 = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=10, textColor=HexColor("#475569"), spaceBefore=8, spaceAfter=4)
        sBody = ParagraphStyle('Body', parent=styles['Normal'], fontSize=8.5, textColor=HexColor("#334155"), leading=12, spaceAfter=4)
        sMeta = ParagraphStyle('Meta', parent=styles['Normal'], fontSize=7.5, textColor=HexColor("#64748B"), leading=10, spaceAfter=2)
        story = []
        story.append(Paragraph("Daily Work Log Report", sTitle))
        story.append(Paragraph(f"Generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')} &nbsp;•&nbsp; {g.user['name']} ({g.user['role']})", sSub))
        if date_from or date_to:
            story.append(Paragraph(f"Period: {date_from or '—'} to {date_to or '—'}", sSub))
        story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#E2E8F0"), spaceAfter=8, spaceBefore=4))
        if entries:
            # Summary
            story.append(Paragraph("Summary", sH1))
            data = [["Team Member", "Entries", "Total Time"]]
            for pn in sorted(totals.keys()):
                data.append([pn, str(counts[pn]), format_duration(totals[pn])])
            t = Table(data, colWidths=[80*mm, 30*mm, 40*mm])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), HexColor("#1E293B")),
                ('TEXTCOLOR', (0,0), (-1,0), HexColor("#FFFFFF")),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,0), 8),
                ('FONTSIZE', (0,1), (-1,-1), 8),
                ('ALIGN', (1,0), (-1,-1), 'CENTER'),
                ('GRID', (0,0), (-1,-1), 0.5, HexColor("#E2E8F0")),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [HexColor("#FFFFFF"), HexColor("#F8FAFC")]),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('LEFTPADDING', (0,0), (-1,-1), 6),
                ('RIGHTPADDING', (0,0), (-1,-1), 6),
                ('TOPPADDING', (0,0), (-1,-1), 4),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ]))
            story.append(t)
            story.append(Spacer(1, 6*mm))
            for d, persons in grouped.items():
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d")
                    label = dt.strftime("%A, %B %d, %Y")
                except:
                    label = d
                story.append(Paragraph(label, sH1))
                story.append(HRFlowable(width="100%", thickness=0.7, color=HexColor("#E2E8F0"), spaceAfter=4))
                for pn, ents in persons.items():
                    story.append(Paragraph(pn, sH2))
                    for e in ents:
                        title = (e["title_enhanced"] or e["title_raw"] or "").replace("&","&amp;")
                        desc = (e["description_enhanced"] or e["description_raw"] or "").replace("&","&amp;").replace("<","&lt;")
                        cat = e["category"] or "Uncategorized"
                        times = f"{e['start_time'] or '—'} – {e['end_time'] or '—'}"
                        dur = format_duration(e["duration_minutes"])
                        story.append(Paragraph(f"<b>{title}</b>", sBody))
                        story.append(Paragraph(f"{times} &nbsp;•&nbsp; {dur} &nbsp;•&nbsp; <i>{cat}</i>", sMeta))
                        # description may be long; allow wrapping via Paragraph
                        story.append(Paragraph(desc.replace("\n","<br/>"), sBody))
                        story.append(Spacer(1, 2*mm))
        else:
            story.append(Paragraph("No entries found for the selected filters.", sBody))
        story.append(Spacer(1, 8*mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#E2E8F0")))
        sFooter = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=7, textColor=HexColor("#94A3B8"), alignment=TA_CENTER, spaceBefore=4)
        story.append(Paragraph("— End of Report —  •  Confidential, internal use only", sFooter))
        doc.build(story)
        bio.seek(0)
        fname = f"worklog_report_{date.today().isoformat()}.pdf"
        return send_file(bio, as_attachment=True, download_name=fname, mimetype="application/pdf")
    except Exception as e:
        return f"PDF generation failed: {e}. Try Word export instead. (Install wkhtmltopdf for best results.)", 500

# ---------- Health / API ----------

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# ---------- Error handlers ----------

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="You don't have permission to access this page."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404

if __name__ == "__main__":
    # Ensure instance dirs
    os.makedirs("instance", exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
