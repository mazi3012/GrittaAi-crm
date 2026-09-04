"""Gretta AI — Database connection pool and utilities.

Provides connection pooling for PostgreSQL and optimized SQLite handling
with proper transaction management.
"""
import os
import sqlite3
import threading
import atexit
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from datetime import date, timedelta

from config import (
    DATABASE_URL, USE_POSTGRES, DB_PATH, DB_CONNECT_TIMEOUT,
    LOG_LEVEL
)
from logger import get_logger

log = get_logger(__name__)

# Query placeholder based on database engine
PH = "%s" if USE_POSTGRES else "?"

# Thread-local storage for connections
_local = threading.local()
_write_lock = threading.Lock()
_initialized = False

# Connection pool for PostgreSQL
_pool: List[Any] = []
_pool_lock = threading.Lock()
_pool_size = 0
_max_pool_size = 10


def _create_connection():
    """Create a new database connection."""
    if USE_POSTGRES:
        import psycopg
        return psycopg.connect(DATABASE_URL, connect_timeout=DB_CONNECT_TIMEOUT)
    
    db_path = os.getenv("DB_PATH", DB_PATH)
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    return conn


def get_connection():
    """Get a database connection from pool or create new one."""
    if USE_POSTGRES:
        with _pool_lock:
            if _pool:
                conn = _pool.pop()
                try:
                    # Test connection is alive
                    conn.execute("SELECT 1")
                    return conn
                except Exception:
                    # Connection is dead, create new one
                    pass
            return _create_connection()
    return _create_connection()


def release_connection(conn):
    """Return connection to pool or close it."""
    if USE_POSTGRES:
        with _pool_lock:
            global _pool_size
            if _pool_size < _max_pool_size:
                try:
                    conn.rollback()  # Clear any pending transaction
                    _pool.append(conn)
                    _pool_size += 1
                    return
                except Exception:
                    pass
    try:
        conn.close()
    except Exception:
        pass


@contextmanager
def db_transaction():
    """Context manager for database transactions with automatic commit/rollback."""
    conn = get_connection()
    try:
        with _write_lock:
            yield conn
            conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"Transaction failed: {e}")
        raise
    finally:
        release_connection(conn)


@contextmanager
def db_query():
    """Context manager for read-only queries."""
    conn = get_connection()
    try:
        yield conn
    finally:
        release_connection(conn)


# ==============================================================================
# SCHEMA DEFINITIONS
# ==============================================================================

STATUSES = (
    "Message Sent", "Seen Not Replied", "Replied",
    "Follow up 1", "Follow up 2", "Follow up 3", "Follow up 4",
    "Replied-No yet booked", "Closing Call", "Number received",
    "Discovery Call booked", "Not Interested", "Lost", "Won",
)

YESNO = ("Yes", "No", "")

CLOSER_STATUSES = (
    "Replied", "Replied-No yet booked", "Number received",
    "Closing Call", "Discovery Call booked", "Won"
)

LEAD_FIELDS = (
    "lead_number", "full_name", "email", "user_name", "profile_link",
    "followers_count", "sender_name", "sender_profile",
    "first_touchpoint", "note", "status", "last_touchpoint",
    "next_touchpoint", "replied", "number_received", "number",
    "follow_up_1", "follow_up_1_date", "follow_up_2", "follow_up_2_date",
    "follow_up_3", "follow_up_3_date", "follow_up_4", "follow_up_4_date",
    "discovery_call", "discovery_date", "closing_call_status",
    "closed_result",
)

CLOSING_CALL_STATUSES = (
    "", "Interested", "Not Interested", "No Response", "Scheduled",
    "Completed", "Rescheduled", "No Show",
)

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS leads (
        lead_number INTEGER DEFAULT 0,
        full_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        user_name TEXT PRIMARY KEY,
        profile_link TEXT DEFAULT '',
        followers_count TEXT DEFAULT '',
        sender_name TEXT DEFAULT '',
        sender_profile TEXT DEFAULT '',
        first_touchpoint TEXT DEFAULT '',
        note TEXT DEFAULT '',
        status TEXT DEFAULT 'Message Sent',
        last_touchpoint TEXT DEFAULT '',
        next_touchpoint TEXT DEFAULT '',
        replied TEXT DEFAULT '',
        number_received TEXT DEFAULT '',
        number TEXT DEFAULT '',
        follow_up_1 TEXT DEFAULT '',
        follow_up_1_date TEXT DEFAULT '',
        follow_up_2 TEXT DEFAULT '',
        follow_up_2_date TEXT DEFAULT '',
        follow_up_3 TEXT DEFAULT '',
        follow_up_3_date TEXT DEFAULT '',
        follow_up_4 TEXT DEFAULT '',
        follow_up_4_date TEXT DEFAULT '',
        discovery_call TEXT DEFAULT '',
        discovery_date TEXT DEFAULT '',
        closing_call_status TEXT DEFAULT '',
        closed_result TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_users (
        telegram_id TEXT PRIMARY KEY,
        chat_id TEXT,
        username TEXT,
        first_name TEXT,
        is_authorized INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

# Indexes for common queries
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_leads_sender ON leads(LOWER(sender_name))",
    "CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)",
    "CREATE INDEX IF NOT EXISTS idx_leads_next_touch ON leads(next_touchpoint)",
    "CREATE INDEX IF NOT EXISTS idx_bot_users_auth ON bot_users(is_authorized)",
]


def init_db():
    """Initialize database schema and create indexes."""
    global _initialized
    if _initialized:
        return
    
    with _write_lock:
        if _initialized:
            return
        
        with db_transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            for index in _INDEXES:
                try:
                    conn.execute(index)
                except Exception as e:
                    log.warning(f"Index creation warning: {e}")
        
        _initialized = True
        log.info("Database initialized with indexes")


def today_str() -> str:
    """Today as ISO date string."""
    return date.today().strftime("%Y-%m-%d")


def normalize_username(username: str) -> str:
    """Normalize username to lowercase with @ prefix."""
    username = (username or "").strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username