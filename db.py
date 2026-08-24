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

_SCHEMA = (
    """
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
    """,
    # Audit log of every Telegram account that ever talks to the bot, so the
    # team can see exactly who is using it and whitelist genuine teammates
    # (see the access gate in bot.py and the Bot Access tab in dashboard.py).
    """
    CREATE TABLE IF NOT EXISTS bot_users (
        telegram_id TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        is_authorized INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
)

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
            for statement in _SCHEMA:
                conn.execute(statement)
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
                    f"""
                    INSERT INTO leads
                        (username, claimed_by, status, lead_score, platform,
                         next_steps, conversation_summary, last_updated)
                    VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH},
                            CURRENT_TIMESTAMP)
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

                # Active Clients / Cancelled are LOCKED: a re-run of screenshot
                # analysis must never silently reopen or re-close the deal.
                if ex_status in CLOSED_STAGES:
                    new_status = ex_status

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
                    f"""
                    UPDATE leads
                       SET claimed_by = {_PH},
                           status = {_PH},
                           lead_score = {_PH},
                           platform = {_PH},
                           next_steps = {_PH},
                           conversation_summary = {_PH},
                           last_updated = CURRENT_TIMESTAMP
                     WHERE LOWER(username) = {_PH}
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


VALID_STAGES = ("New", "Contacted", "Meeting Booked", "Converted",
                "Cancelled", "Lost")

# Stages that end the prospecting pipeline. A CONVERTED deal becomes the
# client's ACTIVE service and is locked against accidental regression;
# CANCELLED marks a client who explicitly quit the service.
CLOSED_STAGES = ("Converted", "Cancelled")

# Allowed stage moves. Anything not listed is rejected, so neither the
# Telegram buttons nor the dashboard can drag an Active Client back into
# prospecting by accident. Re-activation after a cancellation IS allowed
# (the client came back), and a lost deal can be revived by contacting again.
STAGE_TRANSITIONS = {
    "New": ("Contacted", "Meeting Booked", "Converted", "Lost"),
    "Contacted": ("New", "Meeting Booked", "Converted", "Lost"),
    "Meeting Booked": ("New", "Contacted", "Converted", "Lost"),
    "Converted": ("Cancelled",),           # only exit: cancel the deal
    "Cancelled": ("Converted",),           # only exit: re-activate the client
    "Lost": ("New", "Contacted", "Meeting Booked", "Converted"),
}


def allowed_transitions(status):
    """Stages a lead may legally move to from its current stage."""
    return STAGE_TRANSITIONS.get(status or "New", ())


def can_move_stage(current, target):
    """True when moving current -> target respects the pipeline guardrails."""
    if target not in VALID_STAGES:
        return False
    if target == current:
        return True
    return target in allowed_transitions(current)


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
                   SUM(CASE WHEN status = 'Meeting Booked' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'Cancelled' THEN 1 ELSE 0 END)
              FROM leads
            """
        )
        total, unclaimed, hot, converted, meetings, cancelled = cur.fetchone()
        return {
            "total": total or 0,
            "unclaimed": unclaimed or 0,
            "hot": hot or 0,
            "converted": converted or 0,
            "meetings": meetings or 0,
            "cancelled": cancelled or 0,
        }
    finally:
        conn.close()


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

    # Status changes must respect the pipeline guardrails: an ACTIVE CLIENT
    # (Converted) can only be cancelled, and a Cancelled client can only be
    # re-activated — never silently dragged back to New/Contacted.
    if status is not None:
        if status not in VALID_STAGES:
            return False
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT status FROM leads WHERE LOWER(username) = " + _PH,
                (username,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        current_status = row[0] if row else None
        if current_status is None:
            return False
        if not can_move_stage(current_status, status):
            return False
        if status == current_status:
            status = None  # no-op move; skip the column entirely

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

    Unknown stages are rejected so button callbacks can never corrupt data,
    and STAGE_TRANSITIONS protects won/churned clients: a Converted lead is
    the ACTIVE CLIENT (locked) and can only be moved to Cancelled via an
    explicit cancel-deal; a Cancelled client can only be re-activated.
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
                "SELECT status FROM leads WHERE LOWER(username) = " + _PH,
                (username,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            current = row[0] or "New"
            if stage == current:
                return True
            if stage not in allowed_transitions(current):
                return False
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


# ------------------------------------------------------------- bot access
# Every Telegram account that ever talks to the bot is recorded here so the
# team can audit who is using it and lock strangers out (see the access gate
# in bot.py and the Bot Access tab in dashboard.py).

def _norm_handle(value):
    """Lowercase a @handle and guarantee the leading @ ('' when empty)."""
    value = (value or "").strip().lower()
    return f"@{value.lstrip('@')}" if value else ""


def track_bot_user(telegram_id, username=None, first_name=None):
    """Upsert a Telegram user's presence: bump msg_count + last_seen."""
    tid = str(telegram_id or "").strip()
    if not tid:
        return
    init_db()
    handle = _norm_handle(username)
    conn = _connect()
    try:
        with _write_lock:
            if USE_PG:
                conn.execute(
                    """
                    INSERT INTO bot_users
                        (telegram_id, username, first_name, msg_count,
                         first_seen, last_seen)
                    VALUES (%s, %s, %s, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        msg_count = bot_users.msg_count + 1,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (tid, handle or None, (first_name or "").strip() or None),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO bot_users
                        (telegram_id, username, first_name, msg_count,
                         first_seen, last_seen)
                    VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        username = excluded.username,
                        first_name = excluded.first_name,
                        msg_count = msg_count + 1,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (tid, handle or None, (first_name or "").strip() or None),
                )
            conn.commit()
    finally:
        conn.close()


def all_bot_users():
    """Every known Telegram user: authorized first, then most-recent activity.

    Rows are tuples of (telegram_id, username, first_name, is_authorized,
    msg_count, first_seen, last_seen) with timestamps normalized to strings.
    """
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT telegram_id, username, first_name, is_authorized, "
            "msg_count, first_seen, last_seen FROM bot_users "
            "ORDER BY is_authorized DESC, last_seen DESC"
        )

        def _ts(value):
            if value is not None and hasattr(value, "strftime"):
                return value.strftime("%Y-%m-%d %H:%M:%S")
            return value

        return [
            (r[0], r[1], r[2], bool(r[3]), r[4] or 0, _ts(r[5]), _ts(r[6]))
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def set_bot_user_authorized(telegram_id, authorized):
    """Whitelist (or revoke) a Telegram ID; creates a stub row when unknown.

    Works even for people who never messaged the bot yet — handy for
    pre-provisioning teammates from their numeric Telegram ID.
    """
    tid = str(telegram_id or "").strip().lstrip("@")
    if not tid:
        return False
    init_db()
    flag = 1 if authorized else 0
    conn = _connect()
    try:
        with _write_lock:
            conn.execute(
                f"INSERT INTO bot_users (telegram_id, is_authorized) "
                f"VALUES ({_PH}, {flag}) ON CONFLICT (telegram_id) DO NOTHING",
                (tid,),
            )
            conn.execute(
                f"UPDATE bot_users SET is_authorized = {flag} "
                f"WHERE telegram_id = {_PH}",
                (tid,),
            )
            conn.commit()
            return True
    finally:
        conn.close()


def bot_user_allowed(telegram_id, username=None):
    """True when the DB whitelist flags this Telegram user as authorized."""
    tid = str(telegram_id or "").strip()
    handle = _norm_handle(username)
    if not tid and not handle:
        return False
    init_db()
    conn = _connect()
    try:
        if tid:
            cur = conn.execute(
                f"SELECT is_authorized FROM bot_users WHERE telegram_id = {_PH}",
                (tid,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return True
        if handle:
            cur = conn.execute(
                f"SELECT is_authorized FROM bot_users "
                f"WHERE LOWER(username) = {_PH}",
                (handle,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return True
        return False
    finally:
        conn.close()


def find_bot_user(id_or_username):
    """Resolve '/allow <id|@handle>' style input to a stored bot_users tuple."""
    key = (id_or_username or "").strip().lower()
    if not key:
        return None
    handle = None if key.isdigit() else _norm_handle(key)
    for row in all_bot_users():
        if key.isdigit() and row[0] == key.lstrip("@"):
            return row
        if handle and _norm_handle(row[1]) == handle:
            return row
    return None
