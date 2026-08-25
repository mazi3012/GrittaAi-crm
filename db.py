"""Shared data layer for Gretta AI — Instagram outreach CRM.

Used by BOTH bot.py and dashboard.py so the schema lives in exactly one
place. The `leads` table mirrors the team's Google Sheet "CRM - Instagram
Data" COLUMN-FOR-COLUMN (one row here == one row in a setter's tab), so
the database and the sheet are 1:1 clones of each other:

  Lead Number | Full Name (Lead) | User name (Lead) | Profile Link |
  Followers Count | Sender Name | Sender Profile | First Touchpoint (Date) |
  Note | Status | Last Touchpoint (Date) | Next Touchpoint (Date) |
  Replied | Number Received | Number | Follow up 1..4 (+ Date) |
  Discovery Call | Discovery Date | Closing Call Status | Closed (Won/Lost)

Engines:
- DATABASE_URL set -> Neon/Postgres (shared cloud DB: bot on Render +
  dashboard on Vercel see the same leads).
- DATABASE_URL unset -> local SQLite file (WAL mode) fallback.

A legacy screenshot-triage schema (username/lead_score/stages) is detected
on startup and auto-migrated into this one; the original rows are preserved
untouched in `leads_legacy` so nothing is ever lost.
"""

import os
import sqlite3
import threading
from datetime import date

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

DB_NAME = os.getenv("DB_PATH", "crm.db")  # SQLite-only (ignored on Postgres)

_PH = "%s" if USE_PG else "?"  # query placeholder per engine

if USE_PG:
    import psycopg  # noqa: F401  (import early so missing driver fails fast)

# --------------------------------------------------------------- vocabulary
# The 14 Status values of the sheet's dropdown, in pipeline order.
STATUSES = (
    "Message Sent", "Seen Not Replied", "Replied",
    "Follow up 1", "Follow up 2", "Follow up 3", "Follow up 4",
    "Replied-No yet booked", "Closing Call", "Number received",
    "Discovery Call booked", "Not Interested", "Lost", "Won",
)

# The Yes/No dropdown columns (empty = not answered yet).
YESNO = ("Yes", "No", "")

# Warm leads a CLOSER should pick up -> mirrored to the sheet's Closer tab.
CLOSER_STATUSES = ("Replied", "Replied-No yet booked", "Number received",
                   "Closing Call", "Discovery Call booked", "Won")

# Old screenshot-triage stages -> new outreach statuses (legacy migration).
OLD_TO_STATUS = {
    "New": "Message Sent", "Contacted": "Message Sent",
    "Meeting Booked": "Discovery Call booked", "Converted": "Won",
    "Cancelled": "Not Interested", "Lost": "Lost",
}

# The 27 sheet columns in exact tab order -> field names used everywhere.
LEAD_FIELDS = (
    "lead_number", "full_name", "user_name", "profile_link",
    "followers_count", "sender_name", "sender_profile",
    "first_touchpoint", "note", "status", "last_touchpoint",
    "next_touchpoint", "replied", "number_received", "number",
    "follow_up_1", "follow_up_1_date", "follow_up_2", "follow_up_2_date",
    "follow_up_3", "follow_up_3_date", "follow_up_4", "follow_up_4_date",
    "discovery_call", "discovery_date", "closing_call_status",
    "closed_result",
)

# Dropdown values for the sheet's "Closing Call Status" column.
CLOSING_CALL_STATUSES = (
    "", "Interested", "Not Interested", "No Response", "Scheduled",
    "Completed", "Rescheduled", "No Show",
)

# Fields callers may write via update_lead()/upsert_lead().
UPDATABLE_FIELDS = tuple(f for f in LEAD_FIELDS if f != "user_name")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS leads (
        lead_number INTEGER DEFAULT 0,
        full_name TEXT DEFAULT '',
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
    # Audit log of every Telegram account that ever talks to the bot (see
    # the access gate in bot.py and the Bot Access tab in dashboard.py).
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


def _table_columns(conn, table):
    """Column-name set for a table, engine-agnostic."""
    if USE_PG:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def today_str():
    """Today as an ISO 'YYYY-MM-DD' string (canonical date storage format)."""
    return date.today().strftime("%Y-%m-%d")


def normalize_username(username):
    """Trim, force a leading @, and lowercase for deterministic storage."""
    username = (username or "").strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


def profile_link_for(username):
    """Build the Instagram profile URL from a @handle."""
    handle = (username or "").strip().lstrip("@")
    return f"https://www.instagram.com/{handle}/" if handle else ""


def _row_to_dict(row):
    lead = dict(zip(LEAD_FIELDS, row))
    if len(row) > len(LEAD_FIELDS):          # queries that add updated_at
        lead["updated_at"] = row[-1]
    return lead


def init_db():
    """Create the schema once per process; migrate legacy data if found."""
    global _initialized
    if _initialized:
        return
    with _write_lock:
        conn = _connect()
        try:
            legacy = _fetch_legacy_rows(conn)
            if legacy is not None:
                conn.execute("ALTER TABLE leads RENAME TO leads_legacy")
                conn.commit()
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.commit()
            if legacy:
                _import_legacy_rows(conn, legacy)
                conn.commit()
        finally:
            conn.close()
        _initialized = True


def _fetch_legacy_rows(conn):
    """Legacy rows (old schema) or None when the table is already new."""
    cols = _table_columns(conn, "leads")
    if not cols or "lead_score" not in cols:
        return None
    cur = conn.execute(
        "SELECT username, claimed_by, status, conversation_summary, "
        "last_updated FROM leads ORDER BY last_updated"
    )
    return cur.fetchall()


def _import_legacy_rows(conn, legacy_rows):
    """Best-effort mapping of old screenshot-triage rows into this schema."""
    for username, claimed_by, status, summary, last_updated in legacy_rows:
        uname = normalize_username(username)
        if not uname:
            continue
        sender = ("@" + claimed_by.strip().lstrip("@").lower()) if claimed_by else ""
        sender = sender or "@imported"
        new_status = OLD_TO_STATUS.get((status or "").strip(), "Message Sent")
        day = ""
        if last_updated is not None:
            day = (last_updated.strftime("%Y-%m-%d")
                   if hasattr(last_updated, "strftime") else str(last_updated)[:10])
        num = _next_lead_number(conn, sender)
        conn.execute(
            f"INSERT INTO leads (lead_number, user_name, profile_link, "
            f"sender_name, note, status, first_touchpoint, last_touchpoint) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (num, uname, profile_link_for(uname), sender,
             (summary or "").strip(), new_status, day, day),
        )


def _next_lead_number(conn, sender_name):
    """Next per-setter Lead Number (mirrors how each tab counts 1, 2, 3…)."""
    cur = conn.execute(
        f"SELECT MAX(lead_number) FROM leads WHERE LOWER(sender_name) = LOWER({_PH})",
        (sender_name or "",),
    )
    row = cur.fetchone()
    return (row[0] or 0) + 1


def _notify_sheet(reason):
    """Fire-and-forget Google Sheet mirror push (no-op unless configured)."""
    try:
        import sheets
        sheets.request_sync(reason)
    except Exception:
        pass


def add_lead(user_name, full_name="", sender_name="", followers_count="",
             note="", status="Message Sent", sender_profile="",
             number=""):
    """Create a lead for a setter; returns (lead_dict, created).

    Lead Number auto-increments PER SETTER (like each sheet tab), the
    profile link is built from the handle, and both touchpoints are
    stamped with today's date.
    """
    init_db()
    uname = normalize_username(user_name)
    if not uname:
        raise ValueError("A lead @username is required")
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'")
    existing = get_lead(uname)
    if existing is not None:
        return existing, False
    sender = (sender_name or "").strip() or "Unassigned"
    number_received = "Yes" if (number or "").strip() else ""
    today = today_str()
    conn = _connect()
    try:
        with _write_lock:
            num = _next_lead_number(conn, sender)
            conn.execute(
                f"INSERT INTO leads (lead_number, full_name, user_name, "
                f"profile_link, followers_count, sender_name, sender_profile, "
                f"first_touchpoint, note, status, last_touchpoint, number, "
                f"number_received) "
                f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, "
                f"{_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
                (num, (full_name or "").strip(), uname,
                 profile_link_for(uname), (followers_count or "").strip(),
                 sender, (sender_profile or "").strip(), today,
                 (note or "").strip(), status, today,
                 (number or "").strip(), number_received),
            )
            conn.commit()
    finally:
        conn.close()
    _notify_sheet(f"add_lead:{uname}")
    return get_lead(uname), True


def _apply_rules(fields):
    """Sheet-consistent side effects, applied to every write path."""
    for n in (1, 2, 3, 4):
        key = f"follow_up_{n}"
        if fields.get(key) == "Yes" and not fields.get(f"{key}_date"):
            fields[f"{key}_date"] = today_str()
    if str(fields.get("number") or "").strip():
        fields.setdefault("number_received", "Yes")
    for key in ("replied", "number_received", "discovery_call"):
        if key in fields and fields[key] not in YESNO:
            raise ValueError(f"{key} must be 'Yes', 'No' or empty")
    if "closing_call_status" in fields and \
            fields["closing_call_status"] not in CLOSING_CALL_STATUSES:
        raise ValueError(
            f"Invalid closing call status '{fields['closing_call_status']}' "
            f"— valid: {', '.join(s for s in CLOSING_CALL_STATUSES if s)}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"Invalid status '{fields['status']}'")


def get_lead(user_name):
    """Return one lead as a dict, or None when unknown."""
    init_db()
    uname = normalize_username(user_name)
    if not uname:
        return None
    conn = _connect()
    try:
        cols = ", ".join(LEAD_FIELDS)
        cur = conn.execute(
            f"SELECT {cols} FROM leads WHERE LOWER(user_name) = {_PH}",
            (uname,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_lead(user_name, **fields):
    """Merge validated fields into a lead; returns the fresh lead dict.

    Any write bumps Last Touchpoint to today (that is what the column
    means in the sheet) unless the caller sets it explicitly.
    """
    init_db()
    uname = normalize_username(user_name)
    if not uname:
        raise ValueError("A lead @username is required")
    clean = {k: v for k, v in fields.items() if k in UPDATABLE_FIELDS}
    if not clean:
        return get_lead(uname)
    _apply_rules(clean)
    clean.setdefault("last_touchpoint", today_str())
    assignments = ", ".join(f"{k} = {_PH}" for k in clean)
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                f"UPDATE leads SET {assignments}, "
                f"updated_at = CURRENT_TIMESTAMP "
                f"WHERE LOWER(user_name) = {_PH}",
                tuple(clean.values()) + (uname,),
            )
            if cur.rowcount == 0:
                return None
            conn.commit()
    finally:
        conn.close()
    _notify_sheet(f"update:{uname}")
    return get_lead(uname)


def upsert_lead(fields):
    """Insert-or-update a full lead (the Google Sheet import path).

    Sheet values win; missing fields keep their defaults. Returns
    (lead_dict, created).
    """
    init_db()
    data = {k: v for k, v in (fields or {}).items() if k in LEAD_FIELDS}
    uname = normalize_username(data.get("user_name"))
    if not uname:
        raise ValueError("A lead @username is required")
    data["user_name"] = uname
    _apply_rules(data)
    if get_lead(uname) is None:
        sender = (data.get("sender_name") or "").strip() or "Unassigned"
        today = today_str()
        data.setdefault("profile_link", profile_link_for(uname))
        data.setdefault("status", "Message Sent")
        data.setdefault("first_touchpoint", today)
        data.setdefault("last_touchpoint", today)
        conn = _connect()
        try:
            with _write_lock:
                if not data.get("lead_number"):
                    data["lead_number"] = _next_lead_number(conn, sender)
                cols = list(LEAD_FIELDS)
                conn.execute(
                    f"INSERT INTO leads ({', '.join(cols)}) "
                    f"VALUES ({', '.join(_PH for _ in cols)})",
                    tuple((data.get(c) if data.get(c) is not None else "")
                          for c in cols),
                )
                conn.commit()
        finally:
            conn.close()
        _notify_sheet(f"import:{uname}")
        return get_lead(uname), True
    fresh = update_lead(uname, **{k: v for k, v in data.items() if k != "user_name"})
    return fresh, False


def all_leads(sender_name=None):
    """Every lead (or one setter's), sheet order: setter then Lead Number."""
    init_db()
    cols = ", ".join(LEAD_FIELDS) + ", updated_at"
    conn = _connect()
    try:
        if sender_name:
            cur = conn.execute(
                f"SELECT {cols} FROM leads "
                f"WHERE LOWER(sender_name) = LOWER({_PH}) ORDER BY lead_number",
                ((sender_name or "").strip(),),
            )
        else:
            cur = conn.execute(
                f"SELECT {cols} FROM leads ORDER BY sender_name, lead_number")
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def leads_for_setter(sender_name):
    """Convenience alias: one setter's leads in tab order."""
    return all_leads(sender_name)


def delete_lead(user_name):
    init_db()
    uname = normalize_username(user_name)
    if not uname:
        return False
    conn = _connect()
    try:
        with _write_lock:
            cur = conn.execute(
                f"DELETE FROM leads WHERE LOWER(user_name) = {_PH}", (uname,))
            conn.commit()
            deleted = cur.rowcount > 0
    finally:
        conn.close()
    if deleted:
        _notify_sheet(f"delete:{uname}")
    return deleted


def setter_names():
    """Every distinct Sender Name (setter), alphabetical."""
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT DISTINCT sender_name FROM leads "
            "WHERE sender_name <> '' ORDER BY sender_name"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def dashboard_stats():
    """Aggregates for /stats and the dashboard: totals per status/setter."""
    rows = all_leads()
    by_status = {s: 0 for s in STATUSES}
    setters = {}
    for lead in rows:
        status = lead["status"] or "Message Sent"
        by_status[status] = by_status.get(status, 0) + 1
        bucket = setters.setdefault(
            lead["sender_name"] or "Unassigned",
            {"total": 0, "by_status": {s: 0 for s in STATUSES}})
        bucket["total"] += 1
        bucket["by_status"][status] = bucket["by_status"].get(status, 0) + 1
    warm = sum(by_status.get(s, 0) for s in CLOSER_STATUSES)
    return {
        "total": len(rows),
        "by_status": by_status,
        "setters": setters,
        "warm": warm,
        "won": by_status.get("Won", 0),
        "lost": by_status.get("Lost", 0) + by_status.get("Not Interested", 0),
    }


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
                    f"INSERT INTO bot_users (telegram_id, username, first_name,"
                    f" msg_count, first_seen, last_seen) "
                    f"VALUES ({_PH}, {_PH}, {_PH}, 1, CURRENT_TIMESTAMP, "
                    f"CURRENT_TIMESTAMP) "
                    f"ON CONFLICT(telegram_id) DO UPDATE SET "
                    f"username = excluded.username, "
                    f"first_name = excluded.first_name, "
                    f"msg_count = msg_count + 1, "
                    f"last_seen = CURRENT_TIMESTAMP",
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
