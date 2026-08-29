"""Gretta AI — CRM Command Center backend (FastAPI).

Serves:
  GET  /                    → static/index.html  (the SPA)
  GET  /static/*            → styles.css / app.js
  GET  /api/leads           → leads + KPI stats (JSON)
  POST /api/lead/stage      → {username, stage}      move a lead
  POST /api/lead/owner      → {username, owner}      reassign owner
  POST /api/lead/note       → {username, note}       append a note to the summary
  POST /api/lead/next_steps → {username, next_steps} set the next step

Admin auth (enabled automatically when ADMIN_PASSWORD is set):
  GET  /api/auth/status     → {auth_required, authenticated}
  POST /api/auth/login      → {password}             sets a signed session cookie
  POST /api/auth/logout     → clears the session cookie
Every other /api/* route returns 401 until a valid session cookie is present.
Sessions are stateless: HMAC-signed expiry timestamps, so they survive
Vercel's serverless cold starts without any storage.

Bot access control:
  GET  /api/users           → every Telegram account that talked to the bot
  POST /api/user/access     → {user_id, authorized}  whitelist / revoke

All writes go through db.py so the Telegram bot and this dashboard stay
in sync on the same database. Stage moves are validated against both
VALID_STAGES and the STAGE_TRANSITIONS guardrails (an Active Client can
only be cancelled; a Cancelled client can only be re-activated).

Run with either:
    python dashboard.py
    uvicorn dashboard:app --host 0.0.0.0 --port 8000
"""

import hmac
import json
import os
import time
import urllib.error
import urllib.request

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from db import (  # noqa: E402
    CLOSING_CALL_STATUSES,
    STATUSES,
    YESNO,
    all_bot_users,
    all_leads,
    dashboard_stats,
    delete_lead,
    get_lead,
    init_db,
    normalize_username,
    today_str,
    update_lead,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MAX_TEXT = 4000  # sanity cap for note payloads
app = FastAPI(title="Gretta CRM")

# --------------------------------------------------------------------- auth
# Set ADMIN_PASSWORD in the environment (local .env or Vercel/Render env vars)
# to lock the dashboard down. Without it the API stays open for local dev —
# but NEVER deploy publicly without setting it.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
AUTH_ENABLED = bool(ADMIN_PASSWORD)
SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "").strip() or ADMIN_PASSWORD
COOKIE_NAME = "gretta_session"
SESSION_TTL = 7 * 24 * 3600  # one week per sign-in


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(),
                    "sha256").hexdigest()


def _make_token() -> str:
    expires = int(time.time()) + SESSION_TTL
    return f"{expires}.{_sign(str(expires))}"


def _token_ok(token) -> bool:
    """Constant-time check of 'expiry.hexsig' session tokens."""
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit():
        return False
    try:
        valid = hmac.compare_digest(sig, _sign(expires))
    except Exception:
        return False
    return valid and int(expires) >= int(time.time())


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Gate every /api/* route behind the admin session cookie.

    /api/auth/* stays public (login/status/logout need to be reachable) and
    everything else — leads, mutations, user lists — requires a valid cookie
    whenever ADMIN_PASSWORD is configured.
    """
    path = request.url.path
    if (AUTH_ENABLED and path.startswith("/api")
            and not path.startswith("/api/auth")):
        if not _token_ok(request.cookies.get(COOKIE_NAME)):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Not authenticated"},
            )
    return await call_next(request)


class LoginIn(BaseModel):
    username: str = Field(default="", max_length=120)
    password: str = Field(min_length=1, max_length=200)


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    """SPA boot check: is a password configured, and do we hold a session?"""
    authenticated = (not AUTH_ENABLED) or _token_ok(request.cookies.get(COOKIE_NAME))
    return {"auth_required": AUTH_ENABLED, "authenticated": authenticated}


@app.post("/api/auth/login")
def api_auth_login(payload: LoginIn, response: Response):
    """Verify credentials and set a signed, HttpOnly session cookie."""
    if not AUTH_ENABLED:
        return {"ok": True, "auth_required": False}
    user_ok = hmac.compare_digest(payload.username.strip().lower().encode(),
                                  ADMIN_USERNAME.lower().encode())
    pass_ok = hmac.compare_digest(payload.password.encode(),
                                  ADMIN_PASSWORD.encode())
    if not (user_ok and pass_ok):
        time.sleep(0.4)  # cheap brute-force damper
        return JSONResponse(status_code=401,
                            content={"ok": False,
                                     "error": "Wrong username or password"})
    response.set_cookie(
        COOKIE_NAME, _make_token(), max_age=SESSION_TTL,
        httponly=True, samesite="lax",
    )
    return {"ok": True, "auth_required": True}


@app.post("/api/auth/logout")
def api_auth_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


def _lead_dicts():
    """Full sheet-shaped rows for the SPA (all 28 columns + timestamps)."""
    leads = []
    for row in all_leads():
        lead = dict(row)
        lead["username"] = lead["user_name"]       # legacy alias used by app.js
        lead["updated"] = lead.get("updated_at") or ""
        leads.append(lead)
    return leads


@app.get("/api/leads")
def api_leads():
    init_db()
    return {"leads": _lead_dicts(), "stats": dashboard_stats()}


class StageIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    stage: str = Field(min_length=1, max_length=40)   # a status name

    @property
    def status(self) -> str:
        return self.stage.strip()


@app.post("/api/lead/stage")
def api_set_stage(payload: StageIn):
    uname = normalize_username(payload.username)
    if get_lead(uname) is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": "Lead not found"})
    if payload.status == (get_lead(uname) or {}).get("status"):
        return {"ok": True}
    try:
        update_lead(uname, status=payload.status)
    except ValueError as exc:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": str(exc)})
    return {"ok": True}


class OwnerIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    owner: str = Field(max_length=120, default="")


@app.post("/api/lead/owner")
def api_set_owner(payload: OwnerIn):
    uname = normalize_username(payload.username)
    if not uname:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Missing username"})
    if get_lead(uname) is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})
    owner = (payload.owner or "").strip() or "Unassigned"
    update_lead(uname, sender_name=owner)
    return {"ok": True, "owner": owner}


class NoteIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=1, max_length=MAX_TEXT)


@app.post("/api/lead/note")
def api_add_note(payload: NoteIn):
    uname = normalize_username(payload.username)
    lead = get_lead(uname)
    if lead is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})
    merged = f"{lead['note']} | {payload.note.strip()}" if lead["note"] \
        else payload.note.strip()
    update_lead(uname, note=merged[:4000])
    return {"ok": True}


class UpdateIn(BaseModel):
    """Partial edit from the drawer form: only sent fields are changed."""
    username: str = Field(min_length=1, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    full_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    followers_count: str | None = Field(default=None, max_length=60)
    number: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=4000)
    next_touchpoint: str | None = Field(default=None, max_length=20)
    replied: str | None = Field(default=None, max_length=4)
    number_received: str | None = Field(default=None, max_length=4)
    follow_up_1: str | None = Field(default=None, max_length=4)
    follow_up_2: str | None = Field(default=None, max_length=4)
    follow_up_3: str | None = Field(default=None, max_length=4)
    follow_up_4: str | None = Field(default=None, max_length=4)
    discovery_call: str | None = Field(default=None, max_length=4)
    discovery_date: str | None = Field(default=None, max_length=20)
    closing_call_status: str | None = Field(default=None, max_length=40)
    closed_result: str | None = Field(default=None, max_length=20)


# Fields the drawer may write -> passed straight through to update_lead.
EDITABLE = set(UpdateIn.model_fields) - {"username"}


@app.post("/api/lead/update")
def api_update_lead(payload: UpdateIn):
    uname = normalize_username(payload.username)
    if get_lead(uname) is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})
    fields = {k: v for k, v in payload.model_dump().items()
              if k in EDITABLE and v is not None}
    for flag in ("replied", "number_received", "follow_up_1", "follow_up_2",
                 "follow_up_3", "follow_up_4", "discovery_call"):
        if flag in fields and fields[flag] not in YESNO:
            fields[flag] = "Yes" if fields[flag] else ""
    try:
        update_lead(uname, **fields)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return {"ok": True}


class DeleteIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)


@app.post("/api/lead/delete")
def api_delete_lead(payload: DeleteIn):
    ok = delete_lead(payload.username)
    if not ok:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})
    return {"ok": True}


# ------------------------------------------------------- bot access control
@app.get("/api/users")
def api_users():
    """Every Telegram account that ever messaged the bot (audit + whitelist)."""
    init_db()
    users = [
        {
            "telegram_id": row[0],
            "username": row[1] or "",
            "first_name": row[2] or "",
            "authorized": bool(row[3]),
            "msg_count": row[4],
            "first_seen": row[5] or "",
            "last_seen": row[6] or "",
        }
        for row in all_bot_users()
    ]
    return {"users": users}


class AccessIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=32)
    authorized: bool


@app.post("/api/user/access")
def api_user_access(payload: AccessIn):
    """Whitelist or revoke a Telegram user from the Bot Access tab."""
    ok = set_bot_user_authorized(payload.user_id, payload.authorized)
    if not ok:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Missing user_id"})
    return {"ok": True, "authorized": payload.authorized}


# ---- static SPA -------------------------------------------------------
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
