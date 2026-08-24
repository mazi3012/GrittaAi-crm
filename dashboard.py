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
import os
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import (
    VALID_SCORES,
    VALID_STAGES,
    all_bot_users,
    all_leads,
    assign_owner,
    can_move_stage,
    dashboard_stats,
    delete_lead,
    get_lead,
    init_db,
    normalize_username,
    save_lead,
    set_bot_user_authorized,
    set_lead_stage,
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
    leads = []
    for row in all_leads():
        leads.append({
            "username": row[0],
            "claimed_by": row[1] or "",
            "status": row[2] or "New",
            "score": row[3] or "UNKNOWN",
            "platform": row[4] or "Instagram",
            "next_steps": row[5] or "Review lead details",
            "summary": row[6] or "",
            "updated": row[7] or "",
        })
    return leads


@app.get("/api/leads")
def api_leads():
    init_db()
    return {"leads": _lead_dicts(), "stats": dashboard_stats()}


class StageIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    stage: str = Field(min_length=1, max_length=40)


@app.post("/api/lead/stage")
def api_set_stage(payload: StageIn):
    uname = normalize_username(payload.username)
    lead = get_lead(uname)
    if lead is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": "Lead not found"})
    current = lead[2] or "New"
    if payload.stage == current:
        return {"ok": True}  # no-op move, nothing to do
    if payload.stage not in VALID_STAGES:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": f"Invalid stage '{payload.stage}'"})
    if not can_move_stage(current, payload.stage):
        if current == "Converted":
            reason = ("🔒 Active Client is locked — the only allowed change "
                      "is 🚫 Cancel Deal")
        elif current == "Cancelled":
            reason = "🔒 Cancelled client — use ♻️ Re-activate to bring them back"
        else:
            reason = f"🔒 Cannot move a lead from '{current}' to '{payload.stage}'"
        return JSONResponse(status_code=409, content={"ok": False, "error": reason})
    ok = set_lead_stage(uname, payload.stage)
    return {"ok": ok}


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
    ok = assign_owner(uname, payload.owner)  # explicit dashboard override
    return {"ok": bool(ok), "owner": (payload.owner or "").strip() or None}


class NoteIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=1, max_length=MAX_TEXT)


@app.post("/api/lead/note")
def api_add_note(payload: NoteIn):
    uname = normalize_username(payload.username)
    if get_lead(uname) is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})
    # save_lead appends summaries and never steals the first owner.
    save_lead(uname, summary=payload.note.strip())
    return {"ok": True}


class NextStepsIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    next_steps: str = Field(max_length=500, default="")


@app.post("/api/lead/next_steps")
def api_set_next_steps(payload: NextStepsIn):
    uname = normalize_username(payload.username)
    if get_lead(uname) is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})
    save_lead(uname, next_steps=(payload.next_steps.strip() or "Review lead details"))
    return {"ok": True}


class UpdateIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    score: str | None = Field(default=None, max_length=20)
    platform: str | None = Field(default=None, max_length=60)
    owner: str | None = Field(default=None, max_length=120)
    next_steps: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=20000)


@app.post("/api/lead/update")
def api_update_lead(payload: UpdateIn):
    """Partial edit from the drawer form: only sent fields are changed."""
    uname = normalize_username(payload.username)
    lead = get_lead(uname)
    if lead is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})

    kwargs = {}
    if payload.status is not None:
        if payload.status not in VALID_STAGES:
            return JSONResponse(status_code=400, content={"ok": False, "error": f"Invalid stage '{payload.status}'"})
        if payload.status != (lead[2] or "New") and not can_move_stage(lead[2] or "New", payload.status):
            return JSONResponse(status_code=409, content={"ok": False, "error": (
                "🔒 Active Client is locked — only 🚫 Cancel Deal is allowed"
                if (lead[2] or "New") == "Converted"
                else f"🔒 Cannot move from '{lead[2]}' to '{payload.status}'"
            )})
        kwargs["status"] = payload.status
    if payload.score is not None:
        if payload.score not in VALID_SCORES:
            return JSONResponse(status_code=400, content={"ok": False, "error": f"Invalid score '{payload.score}'"})
        kwargs["lead_score"] = payload.score
    if payload.platform is not None:
        if not payload.platform.strip():
            return JSONResponse(status_code=400, content={"ok": False, "error": "Platform cannot be empty"})
        kwargs["platform"] = payload.platform.strip()
    if payload.owner is not None:
        kwargs["claimed_by"] = payload.owner  # "" -> unclaim
    if payload.next_steps is not None:
        kwargs["next_steps"] = payload.next_steps
    if payload.summary is not None:
        kwargs["summary"] = payload.summary  # full overwrite (form warns)

    update_lead(uname, **kwargs)
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
