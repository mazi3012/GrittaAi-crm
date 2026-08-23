"""Gretta AI — CRM Command Center backend (FastAPI).

Serves:
  GET  /                    → static/index.html  (the SPA)
  GET  /static/*            → styles.css / app.js
  GET  /api/leads           → leads + KPI stats (JSON)
  POST /api/lead/stage      → {username, stage}      move a lead
  POST /api/lead/owner      → {username, owner}      reassign owner
  POST /api/lead/note       → {username, note}       append a note to the summary
  POST /api/lead/next_steps → {username, next_steps} set the next step

All writes go through db.py so the Telegram bot and this dashboard stay
in sync on the same SQLite file. Inputs are length-capped and stage
values validated against VALID_STAGES.

Run with either:
    python dashboard.py
    uvicorn dashboard:app --host 0.0.0.0 --port 8000
"""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import (
    VALID_SCORES,
    VALID_STAGES,
    all_leads,
    assign_owner,
    dashboard_stats,
    delete_lead,
    get_lead,
    init_db,
    normalize_username,
    save_lead,
    set_lead_stage,
    update_lead,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MAX_TEXT = 4000  # sanity cap for note payloads

app = FastAPI(title="Gretta CRM")


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
    if payload.stage not in VALID_STAGES:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"Invalid stage '{payload.stage}'"})
    ok = set_lead_stage(payload.username, payload.stage)
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
    if get_lead(uname) is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Lead not found"})

    kwargs = {}
    if payload.status is not None:
        if payload.status not in VALID_STAGES:
            return JSONResponse(status_code=400, content={"ok": False, "error": f"Invalid stage '{payload.status}'"})
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


# ---- static SPA -------------------------------------------------------
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
