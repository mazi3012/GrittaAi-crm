"""Gretta AI — Optimized database operations.

High-performance database operations with connection pooling,
caching, and prepared statements.
"""
from typing import Optional, List, Dict, Any, Tuple

from database import (
    PH, init_db, get_connection, release_connection, db_transaction, db_query,
    STATUSES, YESNO, CLOSER_STATUSES, LEAD_FIELDS, CLOSING_CALL_STATUSES
)
from cache import cache
from logger import get_logger

log = get_logger(__name__)


def _row_to_lead(row: Tuple, include_updated: bool = False) -> Optional[Dict]:
    """Convert database row to lead dictionary."""
    if not row:
        return None
    
    fields = list(LEAD_FIELDS)
    if include_updated:
        fields.append("updated_at")
    
    lead = dict(zip(fields, row))
    if len(row) > len(LEAD_FIELDS):
        lead["updated_at"] = row[len(LEAD_FIELDS)]
    return lead


def get_lead_cached(username: str) -> Optional[Dict]:
    """Get a single lead with caching."""
    uname = username.strip().lower().lstrip("@")
    cache_key = f"lead:{uname}"
    
    cached = cache.leads.get(cache_key)
    if cached is not None:
        return cached
    
    with db_query() as conn:
        cols = ", ".join(LEAD_FIELDS)
        cur = conn.execute(
            f"SELECT {cols} FROM leads WHERE LOWER(user_name) = {PH}",
            (f"@{uname}",),
        )
        row = cur.fetchone()
        lead = _row_to_lead(row) if row else None
        
        if lead:
            cache.leads.set(cache_key, lead)
        return lead


def get_lead(username: str) -> Optional[Dict]:
    """Get a single lead (alias for compatibility)."""
    return get_lead_cached(username)


def get_all_leads_cached(sender_name: Optional[str] = None) -> List[Dict]:
    """Get all leads with caching."""
    cache_key = f"leads:all:{sender_name or 'none'}"
    
    cached = cache.leads.get(cache_key)
    if cached is not None:
        return cached
    
    with db_query() as conn:
        cols = ", ".join(LEAD_FIELDS) + ", updated_at"
        
        if sender_name:
            cur = conn.execute(
                f"SELECT {cols} FROM leads WHERE LOWER(sender_name) = LOWER({PH}) ORDER BY lead_number",
                (sender_name.strip(),),
            )
        else:
            cur = conn.execute(
                f"SELECT {cols} FROM leads ORDER BY sender_name, lead_number"
            )
        
        leads = [_row_to_lead(r, include_updated=True) for r in cur.fetchall()]
        cache.leads.set(cache_key, leads, ttl=30)
        return leads


def all_leads(sender_name: Optional[str] = None) -> List[Dict]:
    """Get all leads (alias for compatibility)."""
    return get_all_leads_cached(sender_name)


def upsert_lead(fields: Dict) -> Tuple[Dict, bool]:
    """Insert or update a lead."""
    from database import normalize_username, today_str
    
    data = {k: v for k, v in fields.items() if k in LEAD_FIELDS}
    uname = normalize_username(data.get("user_name", ""))
    
    if not uname:
        raise ValueError("A lead @username is required")
    
    data["user_name"] = uname
    data.setdefault("profile_link", f"https://www.instagram.com/{uname.lstrip('@')}/")
    data.setdefault("status", "Message Sent")
    data.setdefault("first_touchpoint", today_str())
    data.setdefault("last_touchpoint", today_str())
    
    with db_transaction() as conn:
        cur = conn.execute(f"SELECT 1 FROM leads WHERE LOWER(user_name) = {PH}", (uname,))
        exists = cur.fetchone() is not None
        
        if exists:
            cols = [k for k in data.keys() if k != "user_name"]
            if cols:
                assignments = ", ".join(f"{k} = {PH}" for k in cols)
                conn.execute(
                    f"UPDATE leads SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE LOWER(user_name) = {PH}",
                    tuple(data[k] for k in cols) + (uname,),
                )
        else:
            cols = list(LEAD_FIELDS)
            values = []
            for c in cols:
                if c == "lead_number":
                    sender = data.get("sender_name", "Unassigned")
                    cur = conn.execute(
                        f"SELECT MAX(lead_number) FROM leads WHERE LOWER(sender_name) = LOWER({PH})",
                        (sender,),
                    )
                    next_num = (cur.fetchone()[0] or 0) + 1
                    values.append(next_num)
                else:
                    values.append(data.get(c, ""))
            conn.execute(
                f"INSERT INTO leads ({', '.join(cols)}) VALUES ({', '.join(PH for _ in cols)})",
                tuple(values),
            )
    
    cache.invalidate_leads()
    return get_lead(uname), not exists


def update_lead(username: str, **fields) -> Optional[Dict]:
    """Update a lead's fields."""
    from database import normalize_username, today_str
    
    uname = normalize_username(username)
    if not uname:
        raise ValueError("A lead @username is required")
    
    clean = {k: v for k, v in fields.items() if k in LEAD_FIELDS and v is not None}
    
    if not clean:
        return get_lead(uname)
    
    for n in (1, 2, 3, 4):
        if clean.get(f"follow_up_{n}") == "Yes" and not clean.get(f"follow_up_{n}_date"):
            clean[f"follow_up_{n}_date"] = today_str()
    
    if clean.get("number"):
        clean.setdefault("number_received", "Yes")
    
    clean.setdefault("last_touchpoint", today_str())
    
    with db_transaction() as conn:
        if clean:
            assignments = ", ".join(f"{k} = {PH}" for k in clean)
            conn.execute(
                f"UPDATE leads SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE LOWER(user_name) = {PH}",
                tuple(clean.values()) + (uname,),
            )
    
    cache.leads.delete(f"lead:{uname.lstrip('@')}")
    cache.invalidate_leads()
    
    return get_lead(uname)


def delete_lead(username: str) -> bool:
    """Delete a lead."""
    uname = normalize_username(username)
    if not uname:
        return False
    
    with db_transaction() as conn:
        cur = conn.execute(f"DELETE FROM leads WHERE LOWER(user_name) = {PH}", (uname,))
        deleted = cur.rowcount > 0
    
    if deleted:
        cache.leads.delete(f"lead:{uname.lstrip('@')}")
        cache.invalidate_leads()
    
    return deleted


def dashboard_stats() -> Dict[str, Any]:
    """Get dashboard statistics with caching."""
    cached = cache.stats.get("stats:all")
    if cached is not None:
        return cached
    
    leads = get_all_leads_cached()
    
    by_status = {s: 0 for s in STATUSES}
    setters = {}
    
    for lead in leads:
        status = lead.get("status") or "Message Sent"
        by_status[status] = by_status.get(status, 0) + 1
        
        sender = lead.get("sender_name") or "Unassigned"
        if sender not in setters:
            setters[sender] = {"total": 0, "by_status": {s: 0 for s in STATUSES}}
        
        setters[sender]["total"] += 1
        setters[sender]["by_status"][status] = setters[sender]["by_status"].get(status, 0) + 1
    
    warm = sum(by_status.get(s, 0) for s in CLOSER_STATUSES)
    
    stats = {
        "total": len(leads),
        "by_status": by_status,
        "setters": setters,
        "warm": warm,
        "won": by_status.get("Won", 0),
        "lost": by_status.get("Lost", 0) + by_status.get("Not Interested", 0),
    }
    
    cache.stats.set("stats:all", stats, ttl=30)
    return stats


def track_bot_user(telegram_id: str, username: Optional[str] = None, 
                   first_name: Optional[str] = None, chat_id: Optional[str] = None):
    """Track or update a bot user."""
    tid = str(telegram_id or "").strip()
    if not tid:
        return
    
    handle = f"@{username.strip().lower().lstrip('@')}" if username else ""
    
    init_db()
    
    with db_transaction() as conn:
        conn.execute("""
            INSERT INTO bot_users (telegram_id, chat_id, username, first_name, msg_count, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (telegram_id) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                msg_count = bot_users.msg_count + 1,
                last_seen = CURRENT_TIMESTAMP
        """, (tid, str(chat_id or tid), handle or None, (first_name or "").strip() or None))
    
    cache.users.clear()


def get_all_bot_users() -> List[Tuple]:
    """Get all bot users."""
    init_db()
    with db_query() as conn:
        cur = conn.execute("""
            SELECT telegram_id, username, first_name, is_authorized, msg_count, first_seen, last_seen 
            FROM bot_users ORDER BY is_authorized DESC, last_seen DESC
        """)
        return list(cur.fetchall())


def set_bot_user_authorized(telegram_id: str, authorized: bool) -> bool:
    """Set bot user authorization status."""
    tid = str(telegram_id or "").strip().lstrip("@")
    if not tid:
        return False
    
    init_db()
    
    with db_transaction() as conn:
        conn.execute(
            f"INSERT INTO bot_users (telegram_id, is_authorized) VALUES ({PH}, %s) "
            f"ON CONFLICT (telegram_id) DO UPDATE SET is_authorized = %s",
            (tid, 1 if authorized else 0, 1 if authorized else 0),
        )
    
    cache.users.clear()
    return True


def bot_user_allowed(telegram_id: str, username: Optional[str] = None) -> bool:
    """Check if a bot user is allowed."""
    init_db()
    
    tid = str(telegram_id or "").strip()
    handle = f"@{username.strip().lower().lstrip('@')}" if username else ""
    
    with db_query() as conn:
        if tid:
            cur = conn.execute(
                f"SELECT is_authorized FROM bot_users WHERE telegram_id = {PH}",
                (tid,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return True
        
        if handle:
            cur = conn.execute(
                f"SELECT is_authorized FROM bot_users WHERE LOWER(username) = {PH}",
                (handle.lower(),),
            )
            row = cur.fetchone()
            if row and row[0]:
                return True
    
    return False