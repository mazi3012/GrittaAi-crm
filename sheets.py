"""Google Sheets mirror for Gretta AI — Instagram outreach CRM.

Two-way bridge between the `leads` table and the team's Google Sheet
"CRM - Instagram Data" (tabs: one per setter + Closer + Tracker):

PUSH (automatic): every db mutation queues a debounced background push of
the FULL snapshot. Leads are grouped per setter into their own tab —
"Guthal Setter", "Archit Setter", "Mazidur Setter" (first word of the
sender name + " Setter") — warm leads land in "Closer", and a per-setter
summary is written to "Tracker".

PULL (/importsheet): reads every data tab back and upserts it into the DB,
so leads you already track in the sheet seed the CRM in one command.

Setup: paste google-apps-script.gs into Extensions -> Apps Script, deploy
as Web App (Execute as: Me, Who has access: Anyone), put the /exec URL in
GOOGLE_SHEET_WEBAPP_URL and the same secret in GOOGLE_SHEET_SECRET.

Design notes:
- Full-snapshot replace: statuses/notes/follow-ups get rewritten all the
  time — mirroring whole tabs can never drift out of sync.
- Bursts coalesce: rapid successive saves collapse into ONE Google request
  after COALESCE_SECONDS, keeping well below Apps Script quotas.
- Never raises into callers: a Sheets outage must never break the CRM.
"""

import os
import threading
import time
from datetime import date, datetime

WEBAPP_URL = os.getenv("GOOGLE_SHEET_WEBAPP_URL", "").strip()
SHEET_SECRET = os.getenv("GOOGLE_SHEET_SECRET", "").strip()

COALESCE_SECONDS = 8.0  # quiet window before a queued push actually fires

# The 28 columns of every setter tab, in the sheet's exact order.
HEADERS = (
    "Lead Number", "Full Name (Lead)", "Email", "User name (Lead)", "Profile Link",
    "Followers Count", "Sender Name", "Sender Profile",
    "First Touchpoint (Date)", "Note", "Status", "Last Touchpoint (Date)",
    "Next Touchpoint (Date)", "Replied", "Number Received", "Number",
    "Follow up 1", "Follow up 1 (Date)", "Follow up 2", "Follow up 2 (Date)",
    "Follow up 3", "Follow up 3 (Date)", "Follow up 4", "Follow up 4 (Date)",
    "Discovery Call", "Discovery Date", "Closing Call Status",
    "Closed (Won/Lost)",
)

HEADER_TO_FIELD = {
    "Lead Number": "lead_number",
    "Full Name (Lead)": "full_name",
    "Email": "email",
    "User name (Lead)": "user_name",
    "Profile Link": "profile_link",
    "Followers Count": "followers_count",
    "Sender Name": "sender_name",
    "Sender Profile": "sender_profile",
    "First Touchpoint (Date)": "first_touchpoint",
    "Note": "note",
    "Status": "status",
    "Last Touchpoint (Date)": "last_touchpoint",
    "Next Touchpoint (Date)": "next_touchpoint",
    "Replied": "replied",
    "Number Received": "number_received",
    "Number": "number",
    "Follow up 1": "follow_up_1",
    "Follow up 1 (Date)": "follow_up_1_date",
    "Follow up 2": "follow_up_2",
    "Follow up 2 (Date)": "follow_up_2_date",
    "Follow up 3": "follow_up_3",
    "Follow up 3 (Date)": "follow_up_3_date",
    "Follow up 4": "follow_up_4",
    "Follow up 4 (Date)": "follow_up_4_date",
    "Discovery Call": "discovery_call",
    "Discovery Date": "discovery_date",
    "Closing Call Status": "closing_call_status",
    "Closed (Won/Lost)": "closed_result",
}

DATE_FIELDS = {
    "first_touchpoint", "last_touchpoint", "next_touchpoint",
    "follow_up_1_date", "follow_up_2_date", "follow_up_3_date",
    "follow_up_4_date", "discovery_date",
}

YESNO_FIELDS = {
    "replied", "number_received", "follow_up_1", "follow_up_2",
    "follow_up_3", "follow_up_4", "discovery_call",
}

# Tabs managed by the Apps Script that are NOT raw lead data on pull.
_DERIVED_TABS = {"Sync Log", "Tracker", "Closer"}

_lock = threading.Lock()
_pending_reasons = []
_worker_alive = False


def configured():
    """True when GOOGLE_SHEET_WEBAPP_URL is present — otherwise all no-ops."""
    return bool(WEBAPP_URL)


def request_sync(reason=""):
    """Queue a debounced full-snapshot push on a background thread."""
    global _worker_alive
    if not WEBAPP_URL:
        return False
    try:
        with _lock:
            _pending_reasons.append((reason or "update")[:80])
            if not _worker_alive:
                _worker_alive = True
                threading.Thread(target=_worker, daemon=True).start()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[sheets] schedule error: {exc}")
        return False


def _worker():
    """Coalesce queued triggers, then push once per burst until queue drains."""
    global _worker_alive
    try:
        time.sleep(COALESCE_SECONDS)
        while True:
            with _lock:
                reasons = list(_pending_reasons)
                _pending_reasons.clear()
                if not reasons:
                    _worker_alive = False
                    return
            ok, detail = push_now()
            tag = ", ".join(dict.fromkeys(reasons))[:140]
            print(f"[sheets] {'synced' if ok else 'sync FAILED'} ({tag}): {detail}")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[sheets] worker error: {exc}")
    finally:
        with _lock:
            _worker_alive = False


def tab_for(sender_name):
    """'Guthal Basumatary' -> 'Guthal Setter' (matches the real tabs)."""
    words = (sender_name or "").strip().lstrip("@").split()
    return f"{words[0].capitalize()} Setter" if words else "Unassigned Setter"


def _fmt(value, field):
    """Cell value for the sheet: ISO dates -> '23 Aug', None -> ''."""
    if value is None:
        return ""
    if (field in DATE_FIELDS and isinstance(value, str)
            and len(value) >= 10 and value[4] == "-"):
        try:
            d = datetime.strptime(value[:10], "%Y-%m-%d").date()
            return d.strftime("%d %b").lstrip("0")  # '23 Aug'
        except ValueError:
            return value
    return value


def _lead_row(lead):
    return [_fmt(lead.get(f), f) for f in HEADER_TO_FIELD.values()]


def _tracker_payload(leads):
    """Per-setter summary written to the sheet's Tracker tab."""
    import db
    statuses = list(db.STATUSES)
    headers = (["Setter"] + statuses
               + ["Total", "Warm", "Won", "Lost/NI"])
    stats = db.dashboard_stats()
    rows = []
    for name, bucket in stats["setters"].items():
        rows.append(
            [name] + [bucket["by_status"].get(st, 0) for st in statuses]
            + [bucket["total"],
               sum(bucket["by_status"].get(x, 0) for x in db.CLOSER_STATUSES),
               bucket["by_status"].get("Won", 0),
               bucket["by_status"].get("Lost", 0)
               + bucket["by_status"].get("Not Interested", 0)]
        )
    rows.append(["TOTAL"] + [stats["by_status"].get(st, 0) for st in statuses]
                + [stats["total"], stats["warm"], stats["won"], stats["lost"]])
    return {"headers": headers, "rows": rows}


def push_now():
    """Push the complete CRM snapshot to the sheet right now (blocking).

    Returns (ok, detail) — safe to show directly in Telegram via /syncsheet.
    """
    if not WEBAPP_URL:
        return False, "GOOGLE_SHEET_WEBAPP_URL is not configured"
    try:
        import db
        leads = db.all_leads()
        groups, closer = {}, []
        for lead in leads:
            row = _lead_row(lead)
            groups.setdefault(tab_for(lead["sender_name"]), []).append(row)
            if lead["status"] in db.CLOSER_STATUSES:
                closer.append(row)
        payload = {
            "secret": SHEET_SECRET,
            "action": "replace_all",
            "headers": list(HEADERS),
            "groups": groups,
            "closer": {"headers": list(HEADERS), "rows": closer},
            "tracker": _tracker_payload(leads),
            "syncedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        import requests  # already a core dependency of this project

        # requests follows the Apps Script 302 redirect chain by default,
        # which is exactly how doPost responses arrive.
        resp = requests.post(WEBAPP_URL, json=payload, timeout=30)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:180]}"
        try:
            data = resp.json()
        except ValueError:
            return False, ("unexpected non-JSON reply — check that the web "
                           "app is deployed with access 'Anyone' and you "
                           "copied the /exec URL")
        if isinstance(data, dict) and data.get("ok"):
            return True, data.get("detail") or f"{len(leads)} leads mirrored"
        return False, str((data or {}).get("error", "unknown Apps Script error"))
    except Exception as exc:
        return False, str(exc)[:300]


def _parse_date(value):
    """'23 Aug' / '23 Aug 2026' / ISO -> ISO 'YYYY-MM-DD' (else original)."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) >= 10 and v[4] == "-":
        return v[:10]
    for fmt in ("%d %b %Y", "%d %b"):
        try:
            d = datetime.strptime(v, fmt)
            if fmt == "%d %b":
                d = d.replace(year=date.today().year)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v


def _norm_yesno(value):
    v = (value or "").strip().lower()
    if v in ("yes", "y", "true", "1"):
        return "Yes"
    if v in ("no", "n", "false", "0"):
        return "No"
    return ""


def pull_now():
    """Pull every data tab from the sheet and upsert it into the CRM.

    Returns (ok, detail) for the /importsheet Telegram reply.
    """
    if not WEBAPP_URL:
        return False, "GOOGLE_SHEET_WEBAPP_URL is not configured"
    try:
        import db
        import requests
        resp = requests.get(WEBAPP_URL, params={"secret": SHEET_SECRET},
                            timeout=30)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:180]}"
        data = resp.json()
        if not (isinstance(data, dict) and data.get("ok")):
            return False, str(
                (data or {}).get("error", "unexpected pull response"))[:200]
        tabs = data.get("tabs") or {}
        created = updated = 0
        for tab, blob in tabs.items():
            if tab in _DERIVED_TABS or not blob.get("rows"):
                continue
            headers = [str(h).strip() for h in blob.get("headers") or []]
            fields = [HEADER_TO_FIELD.get(h) for h in headers]
            default_sender = (tab[:-len(" Setter")].strip()
                              if tab.endswith(" Setter") else tab.strip())
            for raw in blob.get("rows") or []:
                lead = {}
                for i, field in enumerate(fields):
                    if not field or i >= len(raw):
                        continue
                    value = raw[i]
                    if field in DATE_FIELDS:
                        value = _parse_date(value)
                    elif field in YESNO_FIELDS:
                        value = _norm_yesno(value)
                    elif field == "lead_number":
                        try:
                            value = int(float(str(value).strip() or 0))
                        except (ValueError, TypeError):
                            value = 0
                    lead[field] = value
                uname = db.normalize_username(lead.get("user_name"))
                if not uname:
                    continue  # row without a handle cannot be keyed
                lead["user_name"] = uname
                if not (lead.get("sender_name") or "").strip():
                    lead["sender_name"] = default_sender
                if lead.get("status") not in db.STATUSES:
                    lead["status"] = "Message Sent"
                _, was_created = db.upsert_lead(lead)
                if was_created:
                    created += 1
                else:
                    updated += 1
        return True, (f"imported {created} new + updated {updated} leads "
                      f"from {len([t for t in tabs if t not in _DERIVED_TABS])} tabs")
    except Exception as exc:
        return False, str(exc)[:300]
