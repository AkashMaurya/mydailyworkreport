import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-production-please-use-env")
    FERNET_KEY = os.getenv("FERNET_KEY", None)
    DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "instance", "worklog.db"))
    # Database backend: "sqlite" (default; local dev + tests) or "postgres" (production).
    # Postgres is only used when DB_BACKEND=postgres AND DATABASE_URL is set,
    # so a DATABASE_URL sitting in a local .env never hijacks local development.
    DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").strip().lower()
    DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
    # Bootstrap manager created only when the users table is completely empty
    # (first boot on a fresh production database). Never overwrites existing data.
    BOOTSTRAP_MANAGER_NAME = os.getenv("MANAGER_NAME", "Manager").strip() or "Manager"
    BOOTSTRAP_MANAGER_EMAIL = os.getenv("MANAGER_EMAIL", "manager@worklog.local").strip().lower()
    BOOTSTRAP_MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "Manager123!")
    SESSION_TYPE = "filesystem"
    SESSION_FILE_DIR = os.path.join(BASE_DIR, "instance", "flask_session")
    SESSION_PERMANENT = True
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_USE_SIGNER = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    OPENROUTER_API_KEY_ENV = os.getenv("OPENROUTER_API_KEY", "")
    # Optional: override the OpenRouter base URL (e.g. self-hosted proxy, byNara, OpenAI-compatible gateways).
    # Defaults to the official https://openrouter.ai/api/v1 endpoint.
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    # AI provider selection: 'openrouter' (cloud) or 'ollama' (local). Env overrides DB default.
    AI_PROVIDER = os.getenv("AI_PROVIDER", "").strip().lower()
    # Ollama (local LLM) settings
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
