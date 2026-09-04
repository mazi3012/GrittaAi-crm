"""Gretta AI — CRM Command Center backend (FastAPI).

Serves:
  GET  /                    → static/index.html  (the SPA)
  GET  /static/*            → styles.css / app.js
  GET  /api/leads           → leads + KPI stats (JSON)
  POST /api/lead/stage      → {username, stage}      move a lead
  POST /api/lead/owner      → {username, owner}      reassign owner
  POST /api/lead/note       → {username, note}       append a note to the summary
  POST /api/lead/next_steps → {username, next_steps} set the next step

Neon Auth (enabled when NEON_AUTH_BASE_URL is set):
  GET  /api/auth/status     → {auth_required, authenticated}
  POST /api/auth/login      → {email, password}      proxies Neon Auth login
  POST /api/auth/logout     → clears the session cookie
Every other /api/* route returns 401 until a valid session cookie is present.
 Sessions are owned and checked by Neon Auth; the app only stores an opaque,
 HttpOnly same-origin cookie.

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

import json
import os
import time
from typing import Optional
import urllib.error
import urllib.request
from http.cookies import SimpleCookie

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from starlette.concurrency import run_in_threadpool

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
    set_bot_user_authorized,
    today_str,
    update_lead,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MAX_TEXT = 4000  # sanity cap for note payloads
app = FastAPI(title="Gritta CRM")

# --------------------------------------------------------------------- auth
# Neon Auth is the identity/session authority. The app cookie contains only
# Neon’s opaque session token; it is never decoded or trusted locally.
NEON_AUTH_BASE_URL = os.getenv("NEON_AUTH_BASE_URL", "").strip().rstrip("/")
INVITED_EMAILS = {
    email.strip().lower()
    for email in os.getenv("GRITTA_AUTH_INVITED_EMAILS", "").split(",")
    if email.strip()
}
AUTH_COOKIE = "__Host-gretta_auth"
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() == "true"
AUTH_ENABLED = bool(NEON_AUTH_BASE_URL)


def _auth_url(path: str) -> str:
    return f"{NEON_AUTH_BASE_URL}/{path.lstrip('/')}"


def _neon_request(path: str, method: str = "GET", body=None, session_token=None):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if session_token:
        clean_token = str(session_token).strip()
        headers["Cookie"] = (
            f"__Secure-neon-auth.session_token={clean_token}; "
            f"__Secure-neonauth.session_token={clean_token}; "
            f"better-auth.session_token={clean_token}"
        )
        headers["Authorization"] = f"Bearer {clean_token.split('.')[0]}"
    request = urllib.request.Request(
        _auth_url(path),
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as upstream:
            raw_cookie = upstream.headers.get("Set-Cookie", "")
            return upstream.status, json.loads(upstream.read() or b"{}"), raw_cookie
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except (ValueError, TypeError):
            detail = {}
        return exc.code, detail, ""
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 503, {}, ""


def _extract_session_token(set_cookie: str, data: dict = None) -> Optional[str]:
    if set_cookie:
        cookie = SimpleCookie()
        try:
            cookie.load(set_cookie)
            for key in ("__Secure-neon-auth.session_token", "__Secure-neonauth.session_token", "better-auth.session_token"):
                if key in cookie:
                    return cookie[key].value
        except Exception:
            pass
    if isinstance(data, dict):
        if data.get("token"):
            return str(data["token"])
        if data.get("data", {}).get("token"):
            return str(data["data"]["token"])
    return None


def _session_from_request(request: Request):
    return request.cookies.get(AUTH_COOKIE)


def _valid_session(session_token):
    if not AUTH_ENABLED or not session_token:
        return None
    status, data, _ = _neon_request("get-session", session_token=session_token)
    if status != 200:
        return None
    session = data.get("session") or data.get("data", {}).get("session")
    user = data.get("user") or data.get("data", {}).get("user")
    if not session or not user:
        return None
    email = str(user.get("email", "")).strip().lower()
    if INVITED_EMAILS and email not in INVITED_EMAILS:
        return None
    return {"session": session, "user": user}


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # non-browser clients do not send Origin
    return origin == str(request.base_url).rstrip("/")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Gate every API route behind a live Neon Auth session."""
    path = request.url.path
    if AUTH_ENABLED and path.startswith("/api"):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
            return JSONResponse(status_code=403, content={"ok": False, "error": "Invalid origin"})
        if path.startswith("/api/auth"):
            return await call_next(request)
        if not await run_in_threadpool(_valid_session, _session_from_request(request)):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Not authenticated"},
            )
    return await call_next(request)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=200)


class RequestResetIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=8, max_length=200)


class SignUpIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)
    name: Optional[str] = Field(default=None, max_length=100)


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    session = await run_in_threadpool(_valid_session, _session_from_request(request))
    return {"auth_required": AUTH_ENABLED, "authenticated": bool(session),
            "user": session["user"] if session else None}


@app.post("/api/auth/login")
def api_auth_login(payload: LoginIn, response: Response):
    """Proxy email/password login to Neon Auth without exposing its cookie domain."""
    if not AUTH_ENABLED:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Authentication is not configured"})
    email_clean = payload.email.strip().lower()
    if INVITED_EMAILS and email_clean not in INVITED_EMAILS:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Access restricted to invited team members"})
    status, data, set_cookie = _neon_request(
        "sign-in/email", "POST", {"email": email_clean, "password": payload.password}
    )
    if status != 200:
        time.sleep(0.25)
        err_msg = data.get("message") if isinstance(data, dict) else "Invalid email or password"
        return JSONResponse(status_code=401, content={"ok": False, "error": err_msg or "Invalid email or password"})
    token = _extract_session_token(set_cookie, data)
    if not token:
        return JSONResponse(status_code=502, content={"ok": False, "error": "Authentication session could not be established"})
    response.set_cookie(AUTH_COOKIE, token, httponly=True, secure=AUTH_COOKIE_SECURE,
                        samesite="lax", path="/")
    user = data.get("user") or data.get("data", {}).get("user")
    return {"ok": True, "auth_required": True, "user": user}


@app.post("/api/auth/request-reset")
def api_auth_request_reset(payload: RequestResetIn, request: Request):
    """Request a password reset link from Neon Auth."""
    if not AUTH_ENABLED:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Authentication is not configured"})
    email_clean = payload.email.strip().lower()
    if INVITED_EMAILS and email_clean not in INVITED_EMAILS:
        return {"ok": True, "message": "If an invited account exists, a reset link has been dispatched."}
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    status, data, _ = _neon_request(
        "request-password-reset",
        "POST",
        {"email": email_clean, "redirectTo": f"{origin}/"}
    )
    if status not in (200, 204):
        err_msg = data.get("message") if isinstance(data, dict) else "Failed to send reset link"
        return JSONResponse(status_code=status if status in (400, 429) else 500,
                            content={"ok": False, "error": err_msg or "Failed to request password reset"})
    return {"ok": True, "message": "Password reset instructions have been sent to your email."}


@app.post("/api/auth/reset-password")
def api_auth_reset_password(payload: ResetPasswordIn, response: Response):
    """Complete a password reset with token."""
    if not AUTH_ENABLED:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Authentication is not configured"})
    status, data, set_cookie = _neon_request(
        "reset-password",
        "POST",
        {"token": payload.token.strip(), "newPassword": payload.new_password}
    )
    if status != 200:
        err_msg = data.get("message") if isinstance(data, dict) else "Password reset failed"
        return JSONResponse(status_code=status if status in (400, 422) else 500,
                            content={"ok": False, "error": err_msg or "Invalid or expired reset token"})
    token = _extract_session_token(set_cookie, data)
    if token:
        response.set_cookie(AUTH_COOKIE, token, httponly=True, secure=AUTH_COOKIE_SECURE,
                            samesite="lax", path="/")
    user = data.get("user") or data.get("data", {}).get("user")
    return {"ok": True, "user": user, "message": "Password successfully updated!"}


@app.post("/api/auth/signup")
def api_auth_signup(payload: SignUpIn, response: Response):
    """Set up initial password / account for invited team members."""
    if not AUTH_ENABLED:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Authentication is not configured"})
    email_clean = payload.email.strip().lower()
    if INVITED_EMAILS and email_clean not in INVITED_EMAILS:
        return JSONResponse(status_code=403, content={"ok": False, "error": "Registration is invitation-only. Contact your admin."})
    name = (payload.name or email_clean.split("@")[0]).strip()
    status, data, set_cookie = _neon_request(
        "sign-up/email",
        "POST",
        {"email": email_clean, "password": payload.password, "name": name}
    )
    if status != 200:
        err_msg = data.get("message") if isinstance(data, dict) else "Signup failed"
        return JSONResponse(status_code=status if status in (400, 409, 422) else 500,
                            content={"ok": False, "error": err_msg or "Unable to complete account registration"})
    token = _extract_session_token(set_cookie, data)
    if not token:
        return JSONResponse(status_code=502, content={"ok": False, "error": "Authentication session could not be established"})
    response.set_cookie(AUTH_COOKIE, token, httponly=True, secure=AUTH_COOKIE_SECURE,
                        samesite="lax", path="/")
    user = data.get("user") or data.get("data", {}).get("user")
    return {"ok": True, "user": user, "message": "Account created successfully"}


@app.post("/api/auth/logout")
def api_auth_logout(request: Request, response: Response):
    token = _session_from_request(request)
    if token:
        _neon_request("sign-out", "POST", session_token=token)
    response.delete_cookie(AUTH_COOKIE, path="/")
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
