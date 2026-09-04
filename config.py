"""Gretta AI — Centralized configuration management.

All configuration values are loaded from environment variables with
type coercion, validation, and sensible defaults.
"""
import os
import logging
from typing import Optional, Set
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_env_str(key: str, default: str = "", required: bool = True) -> str:
    """Get string environment variable.
    
    Args:
        key: Environment variable name
        default: Default value if not set
        required: If True and no value provided, log warning
    """
    value = os.getenv(key, default).strip()
    if not value and not default and required:
        logger.warning(f"Environment variable {key} is not set")
    return value


def _get_env_bool(key: str, default: bool = False) -> bool:
    """Get boolean environment variable."""
    return _get_env_str(key, str(default)).lower() in ("true", "1", "yes")


def _get_env_int(key: str, default: int) -> int:
    """Get integer environment variable."""
    try:
        return int(_get_env_str(key, str(default)))
    except ValueError:
        logger.warning(f"Invalid integer for {key}, using default: {default}")
        return default


def _get_env_set(key: str) -> Set[str]:
    """Get comma-separated values as a set."""
    return {item.strip() for item in _get_env_str(key).split(",") if item.strip()}


# ==============================================================================
# REQUIRED CREDENTIALS (Bot requires these, dashboard can run without)
# ==============================================================================

# Use None as default when not required
_telegram_token_raw = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_TOKEN = _telegram_token_raw.strip() if _telegram_token_raw else None

_openrouter_key_raw = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_KEY = _openrouter_key_raw.strip() if _openrouter_key_raw else None

_groq_key_raw = os.getenv("GROQ_API_KEY")
GROQ_API_KEY = _groq_key_raw.strip() if _groq_key_raw else None

# Only validate bot credentials when running as bot (not dashboard-only)
_RUNNING_AS_BOT = bool(TELEGRAM_TOKEN)

if _RUNNING_AS_BOT:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is required")
    if not OPENROUTER_API_KEY and not GROQ_API_KEY:
        raise ValueError("At least one of OPENROUTER_API_KEY or GROQ_API_KEY is required")

# ==============================================================================
# AI MODELS
# ==============================================================================

MODEL: str = _get_env_str("MODEL", "stealth/ox-alpha")
VISION_MODEL: str = _get_env_str("VISION_MODEL", MODEL)
GROQ_MODEL: str = _get_env_str("GROQ_MODEL", "qwen/qwen3.6-27b")
OPENROUTER_URL: str = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_KEY: Optional[str] = _get_env_str("GEMINI_API_KEY", required=False)

# ==============================================================================
# DATABASE
# ==============================================================================

DATABASE_URL: str = _get_env_str("DATABASE_URL")
USE_POSTGRES: bool = bool(DATABASE_URL)
DB_PATH: str = _get_env_str("DB_PATH", "crm.db")
DB_CONNECT_TIMEOUT: int = _get_env_int("DB_CONNECT_TIMEOUT", 15)

# ==============================================================================
# DASHBOARD & AUTH
# ==============================================================================

DASHBOARD_URL: Optional[str] = None
raw_url = _get_env_str("DASHBOARD_URL")
if raw_url.startswith(("https://", "http://")) and "localhost" not in raw_url:
    DASHBOARD_URL = raw_url

PORT: int = _get_env_int("PORT", 8000)
NEON_AUTH_BASE_URL: str = _get_env_str("NEON_AUTH_BASE_URL").rstrip("/")
AUTH_ENABLED: bool = bool(NEON_AUTH_BASE_URL)
AUTH_COOKIE_SECURE: bool = _get_env_bool("AUTH_COOKIE_SECURE", True)
INVITED_EMAILS: Set[str] = _get_env_set("GRITTA_AUTH_INVITED_EMAILS")

# ==============================================================================
# BOT ACCESS
# ==============================================================================

ALLOWED_TELEGRAM_IDS: Set[str] = _get_env_set("ALLOWED_TELEGRAM_IDS")
ALLOWED_TELEGRAM_USERNAMES: Set[str] = {
    f"@{u.lstrip('@')}" for u in _get_env_set("ALLOWED_TELEGRAM_USERNAMES")
}
OPEN_ACCESS: bool = not ALLOWED_TELEGRAM_IDS and not ALLOWED_TELEGRAM_USERNAMES

# ==============================================================================
# GOOGLE SHEETS
# ==============================================================================

GOOGLE_SHEET_WEBAPP_URL: str = _get_env_str("GOOGLE_SHEET_WEBAPP_URL")
GOOGLE_SHEET_SECRET: str = _get_env_str("GOOGLE_SHEET_SECRET")
GOOGLE_SHEETS_ENABLED: bool = bool(GOOGLE_SHEET_WEBAPP_URL and GOOGLE_SHEET_SECRET)

# ==============================================================================
# RATE LIMITING
# ==============================================================================

RATE_LIMIT_ENABLED: bool = _get_env_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_REQUESTS: int = _get_env_int("RATE_LIMIT_REQUESTS", 100)
RATE_LIMIT_WINDOW: int = _get_env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
AUTH_RATE_LIMIT: int = _get_env_int("AUTH_RATE_LIMIT_REQUESTS", 5)

# ==============================================================================
# APP SETTINGS
# ==============================================================================

MAX_IMAGE_DIM: int = 1600
TG_MSG_LIMIT: int = 3900
CHAT_MEMORY_TURNS: int = 6
CHAT_MAX_CHARS: int = 1200
AI_TIMEOUT: int = _get_env_int("AI_REQUEST_TIMEOUT", 120)

LOG_LEVEL: str = _get_env_str("LOG_LEVEL", "INFO")
BASE_DIR: Path = Path(__file__).parent
STATIC_DIR: Path = BASE_DIR / "static"