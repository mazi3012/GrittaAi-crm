"""Shared data layer for Gretta AI.

Used by BOTH bot.py and dashboard.py so the schema lives in exactly one
place.

Engines:
- DATABASE_URL set -> Neon/Postgres (shared cloud DB: bot on Render +
  dashboard on Vercel see the same leads).
- DATABASE_URL unset -> local SQLite file (offline dev fallback), WAL mode
  so the bot can write while the dashboard reads without locking errors.

Both drivers expose the same cursor surface used here (execute /
fetchone / fetchall / rowcount / commit); only the placeholder style
differs (%s vs ?), handled by _PH.
"""

import os
import sqlite3
import threading

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

DB_NAME = os.getenv("DB_PATH", "crm.db")  # SQLite-only (ignored on Postgres)

_PH = "%s" if USE_PG else "?"  # query placeholder per engine

if USE_PG:
    import psycopg  # noqa: F401  (import early so missing driver fails fast)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    username TEXT PRIMARY KEY,
    claimed_by TEXT,
    status TEXT DEFAULT 'New',
    lead_score TEXT DEFAULT 'UNKNOWN',
    platform TEXT DEFAULT 'Instagram',
    next_steps TEXT DEFAULT 'Review lead details',
    conversation_summary TEXT,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_write_lock = threading.Lock()
_initialized = False


def _connect():
    if USE_PG:
        return psycopg.connect(DATABASE_URL, connect_timeout=15)
    db_path = os.getenv("DB_PATH", DB_NAME)
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _with_ts(row):
    """Normalize last_updated (index 7) to 'YYYY-MM-DD HH:MM:SS' strings.

    Postgres returns datetime objects; SQLite returns strings. The SPA
    sorts/displays via Date.parse(), so keep the wire format identical.
    """
    if row is not None and hasattr(row[7], "strftime"):
        row = row[:7] + (row[7].strftime("%Y-%m-%d %H:%M:%S"),)
    return row


def init_db():
    """Create the schema once per process (cheap no-op afterwards)."""
    global _initialized
    if _initialized:
        return
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def normalize_username(username: str) -> str:
    """Trim, force a leading @, and lowercase for deterministic storage."""
    username = (username or "").strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


def get_lead(username):
    """Return the raw lead row tuple, or None if the lead is unknown."""
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT username, claimed_by, status, lead_score, platform, "
            "next_steps, conversation_summary, last_updated "
            f"FROM leads WHERE LOWER(username) = {_PH}",
            (normalize_username(username),),
        )
        return _with_ts(cur.fetchone())
    finally:
        conn.close()


def save_lead(username, claimed_by=None, status=None, lead_score="UNKNOWN",
              platform="Instagram", next_steps="", summary=""):
    """Upsert a lead, merging fields instead of blindly overwriting.

    Business rules (enforced here so callers can't get them wrong):
    - the FIRST owner is kept forever -> no lead stealing
    - a fresh 'New' lead moves to 'Contacted' the moment it gets an owner
    - unpassed/empty fields never erase known ones
    - summaries are appended, a new non-default score always wins

    The merge is done in explicit Python (not SQL upsert gymnastics) so every
    rule is readable and testable.
    """
    init_db()
    username = normalize_username(username)
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                "SELECT claimed_by, status, lead_score, platform, next_steps,"
                " conversation_summary FROM leads WHERE LOWER(username) = " + _PH,
                (username,),
            )
            existing = cur.fetchone()

            if existing is None:
                # --- brand new lead -------------------------------------
                conn.execute(
                    """
                    INSERT INTO leads
                        (username, claimed_by, status, lead_score, platform,
                         next_steps, conversation_summary, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
                    (
                        username,
                        claimed_by,
                        status or ("Contacted" if claimed_by else "New"),
                        lead_score if lead_score != "UNKNOWN" else "UNKNOWN",
                        platform or "Instagram",
                        next_steps or "Review lead details",
                        summary or None,
                    ),
                )
            else:
                # --- merge into the existing row ------------------------
                ex_owner, ex_status, ex_score = existing[0], existing[1], existing[2]
                ex_platform, ex_next, ex_summary = existing[3], existing[4], existing[5]

                new_owner = ex_owner if ex_owner else claimed_by

                if ex_status in (None, "", "New") and (claimed_by or new_owner):
                    new_status = "Contacted"          # first touch auto-advances
                else:
                    new_status = status if status not in (None, "") else (ex_status or "New")

                new_score = lead_score if lead_score != "UNKNOWN" else (ex_score or "UNKNOWN")
                new_platform = platform if platform else (ex_platform or "Instagram")
                new_next = next_steps if next_steps else (ex_next or "Review lead details")

                if not ex_summary:
                    new_summary = summary or None
                elif summary:
                    new_summary = f"{ex_summary} | {summary}"
                else:
                    new_summary = ex_summary

                conn.execute(
                    """
                    UPDATE leads
                       SET claimed_by = %s,
                           status = %s,
                           lead_score = %s,
                           platform = %s,
                           next_steps = %s,
                           conversation_summary = %s,
                           last_updated = CURRENT_TIMESTAMP
                     WHERE LOWER(username) = %s
                    """,
                    (new_owner, new_status, new_score, new_platform,
                     new_next, new_summary, username),
                )
            conn.commit()
    finally:
        conn.close()


def all_leads():
    """Every lead, newest activity first (raw row tuples)."""
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT username, claimed_by, status, lead_score, platform, "
            "next_steps, conversation_summary, last_updated "
            "FROM leads ORDER BY last_updated DESC"
        )
        return [_with_ts(r) for r in cur.fetchall()]
    finally:
        conn.close()


def leads_for_owner(owner):
    """Leads owned by one teammate, newest activity first."""
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT username, claimed_by, status, lead_score, platform, "
            "next_steps, conversation_summary, last_updated "
            f"FROM leads WHERE LOWER(claimed_by) = LOWER({_PH}) "
            "ORDER BY last_updated DESC",
            ((owner or "").strip(),),
        )
        return [_with_ts(r) for r in cur.fetchall()]
    finally:
        conn.close()


VALID_STAGES = ("New", "Contacted", "Meeting Booked", "Converted", "Lost")


def assign_owner(username, owner):
    """Explicitly (re)assign a lead's owner from the dashboard.

    Unlike save_lead — whose first-owner-wins rule protects the bot flow —
    this is an intentional manual override from the dashboard, so it MAY
    replace an existing owner. Pass owner="" to unassign (stored as NULL).
    Returns True if a row was updated.
    """
    init_db()
    username = normalize_username(username)
    if not username:
        return False
    owner = (owner or "").strip() or None
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                "UPDATE leads SET claimed_by = " + _PH +
                ", last_updated = CURRENT_TIMESTAMP "
                "WHERE LOWER(username) = " + _PH,
                (owner, username),
            )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def dashboard_stats():
    """Aggregated counters for the dashboard KPI cards (single pass in SQL)."""
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN LOWER(claimed_by) IS NULL OR LOWER(claimed_by) = '' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN UPPER(lead_score) = 'HIGH' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'Converted' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'Meeting Booked' THEN 1 ELSE 0 END)
              FROM leads
            """
        )
        total, unclaimed, hot, converted, meetings = cur.fetchone()
        return {
            "total": total or 0,
            "unclaimed": unclaimed or 0,
            "hot": hot or 0,
            "converted": converted or 0,
            "meetings": meetings or 0,
        }
    finally:
        conn.close()


VALID_STAGES = ("New", "Contacted", "Meeting Booked", "Converted", "Lost")
VALID_SCORES = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")


def update_lead(username, claimed_by=None, status=None, lead_score=None,
                platform=None, next_steps=None, summary=None):
    """Explicit field edit from the dashboard (bypasses save_lead merge rules).

    Only the fields you pass are changed; pass None to leave a field alone.
    claimed_by="" unclaims, summary replaces the whole conversation history.
    Returns True if a row was updated.
    """
    init_db()
    username = normalize_username(username)
    if not username:
        return False
    sets, vals = [], []

    if claimed_by is not None:
        sets.append(f"claimed_by = {_PH}")
        vals.append(claimed_by.strip() or None)
    if status is not None:
        if status not in VALID_STAGES:
            return False
        sets.append(f"status = {_PH}")
        vals.append(status)
    if lead_score is not None:
        if lead_score not in VALID_SCORES:
            return False
        sets.append(f"lead_score = {_PH}")
        vals.append(lead_score)
    if platform is not None:
        sets.append(f"platform = {_PH}")
        vals.append(platform.strip() or "Instagram")
    if next_steps is not None:
        sets.append(f"next_steps = {_PH}")
        vals.append(next_steps.strip() or "Review lead details")
    if summary is not None:
        sets.append(f"conversation_summary = {_PH}")
        vals.append(summary.strip() or None)

    if not sets:
        return True
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                f"UPDATE leads SET {', '.join(sets)}, last_updated = CURRENT_TIMESTAMP "
                "WHERE LOWER(username) = " + _PH,
                (*vals, username),
            )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def delete_lead(username):
    """Remove a lead permanently (dashboard delete button)."""
    init_db()
    username = normalize_username(username)
    if not username:
        return False
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                "DELETE FROM leads WHERE LOWER(username) = " + _PH, (username,)
            )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def set_lead_stage(username, stage):
    """Move a lead to a pipeline stage; returns True if a row was updated.

    Unknown stages are rejected so button callbacks can never corrupt data.
    """
    if stage not in VALID_STAGES:
        return False
    init_db()
    username = normalize_username(username)
    if not username:
        return False
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                "UPDATE leads SET status = " + _PH +
                ", last_updated = CURRENT_TIMESTAMP "
                "WHERE LOWER(username) = " + _PH,
                (stage, username),
            )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()
