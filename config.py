import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-production-please-use-env")
    FERNET_KEY = os.getenv("FERNET_KEY", None)
    DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "instance", "worklog.db"))
    SESSION_TYPE = "filesystem"
    SESSION_FILE_DIR = os.path.join(BASE_DIR, "instance", "flask_session")
    SESSION_PERMANENT = True
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_USE_SIGNER = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    OPENROUTER_API_KEY_ENV = os.getenv("OPENROUTER_API_KEY", "")
