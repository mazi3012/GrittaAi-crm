---
title: Gretta AI CRM Bot
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🤖 Gretta AI — Telegram CRM Bot & Live Command Center

A Telegram bot that turns chat screenshots into structured CRM leads, paired with a live, modern web dashboard. Built for sales teams that DM prospects on Instagram, WhatsApp, or IndiaMART and need to prevent duplicate outreach.

## 🚀 Key Features

1. **Screenshot Triage**: Send a conversation screenshot; PaddleOCR extracts text and OpenRouter AI scores lead quality, intent, and recommended next steps.
2. **Sheet-shaped CRM & Duplicate Protection**: leads are logged per-setter (`/addlead @user`, `/lead @user`) with the same 27 columns as the team Google Sheet — duplicates merge into one row, never stolen.
3. **Interactive Telegram Menu**: Full app-style inline keyboard UI for pipeline management directly inside Telegram.
4. **Next-Gen CRM Dashboard**: FastAPI SPA with live lead status toggling, dual theme (Obsidian Dark / Slate Light), search/filters, and real-time statistics.
5. **Google Sheets Backup Mirror**: The whole `leads` table auto-syncs to a public team Google Sheet after every change (`/syncsheet` forces it on demand) — zero extra dependencies.

---

## 🛠️ Local Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # Fill in your real tokens
```

Get a Telegram token from [@BotFather](https://t.me/BotFather) and an API key from [OpenRouter](https://openrouter.ai/keys).

### Running Locally

```bash
# Terminal 1 — Run Telegram Bot
python bot.py

# Terminal 2 — Run CRM Dashboard
uvicorn dashboard:app --host 0.0.0.0 --port 8000
```

---

## 📊 Google Sheets Backup (live mirror for the team)

The bot can clone your entire `leads` table into a public Google Sheet so teammates can browse/report without touching Telegram or the dashboard. Every lead mutation (from the **bot or the dashboard**) triggers a debounced background push of the full snapshot, so the sheet can never drift out of sync.

### One-time setup (~3 minutes)

1. Open your team Google Sheet → **Extensions → Apps Script**.
2. Paste the contents of [`google-apps-script.gs`](google-apps-script.gs), and set the `SECRET` constant to any long random string.
3. **Deploy → New deployment → Web app** with *Execute as: Me* and *Who has access: Anyone* (the shared secret is what actually blocks strangers). Authorize when prompted.
4. Copy the Web app URL ending in `/exec`.
5. Add to your environment (local `.env`, Render/HF Spaces secrets):

   ```env
   GOOGLE_SHEET_WEBAPP_URL=https://script.google.com/macros/s/AKfy.../exec
   GOOGLE_SHEET_SECRET=your-long-random-string
   ```

6. Run `/syncsheet` in Telegram to verify — the `Leads` tab fills instantly, and every sync is logged in a `Sync Log` tab.

> 💡 The sheet is a **mirror**: the CRM database stays the source of truth, and each sync overwrites manual sheet edits. If a dashboard-side change happens while Vercel's serverless worker is frozen, the next bot-side change (or `/syncsheet`) catches it up. Leave `GOOGLE_SHEET_WEBAPP_URL` empty to disable the feature entirely — everything then behaves exactly as before.

---

## ☁️ Deployment Guide

### 1. Host Telegram Bot on Hugging Face Spaces (Free)

1. Create a new Space on [Hugging Face](https://huggingface.co/new-space).
2. Choose **Docker** as the Space SDK.
3. Push or connect this Git repository (`mazi3012/GrittaAi-crm.git`).
4. In Space Settings -> **Variables and secrets**, add:
   - `TELEGRAM_TOKEN` = `your_telegram_bot_token`
   - `OPENROUTER_API_KEY` = `your_openrouter_api_key`
   - `MODEL` = `stealth/ox-alpha` (optional)
   - `DASHBOARD_URL` = `https://your-vercel-dashboard.vercel.app` (optional)
5. Hugging Face will automatically build the container using the provided `Dockerfile` and start the bot with a built-in health check on port `7860`.

### 2. Host Telegram Bot on Render (Alternative to HF Spaces)

1. Push this repository to GitHub (`mazi3012/GrittaAi-crm`).
2. On [Render](https://dashboard.render.com), click **New + → Blueprint** and select the repo — Render detects `render.yaml`.
3. When prompted, fill in the secret variables:
   - `TELEGRAM_TOKEN` = your telegram bot token
   - `OPENROUTER_API_KEY` = your OpenRouter API key
   - `DASHBOARD_URL` = your live Vercel dashboard URL (e.g. `https://your-app.vercel.app`)
4. Deploy! The bot runs as a Docker web service; the health endpoint (`/`) responds on Render's assigned `$PORT`.

> ⚠️ **Render free tier caveat:** free web services sleep after ~15 minutes without inbound HTTP traffic. A long-polling bot receives no inbound traffic, so keep it awake with a free uptime pinger (e.g. [UptimeRobot](https://uptimerobot.com)) hitting the Render URL every 10 minutes, or upgrade to the paid Starter plan. While asleep, Telegram queues updates and delivers them once the bot wakes up.

> 💡 **Shared database:** set `DATABASE_URL` (Neon Postgres) in **both** the Render bot service and the Vercel dashboard project so leads created in Telegram appear live on the dashboard. Without it, each service silently falls back to its own throwaway SQLite file (`crm.db` locally, ephemeral on Render/Vercel).

---

### 3. Host FastAPI Dashboard on Vercel (Free)

1. Import your repository (`mazi3012/GrittaAi-crm.git`) into [Vercel](https://vercel.com/new).
2. Vercel automatically detects the root-level `index.py` FastAPI entrypoint.
3. In Project Settings -> **Environment Variables**, set `DATABASE_URL` to the same Neon Postgres connection string used by the Telegram bot.
4. For a temporary SQLite-only deployment, set `DB_PATH` to `/tmp/crm.db`; Vercel storage is otherwise ephemeral and should not be used as the shared CRM database.
5. Deploy! Your CRM dashboard will be live at `https://your-project.vercel.app`.

---

## 📁 Project Layout

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot: Vision AI analysis → interactive inline menus + HF health server |
| `dashboard.py` | FastAPI web dashboard serving static SPA and REST endpoints |
| `db.py` | Shared SQLite data layer with WAL mode support |
| `sheets.py` | Google Sheets backup mirror — debounced full-snapshot pushes |
| `google-apps-script.gs` | Paste into your Google Sheet's Apps Script editor (one-time setup) |
| `static/` | Next-Gen SPA dashboard (HTML, CSS, JS with Obsidian/Slate theme support) |
| `Dockerfile` | Docker configuration for Hugging Face Spaces / Render deployment |
| `render.yaml` | Render Blueprint for one-click Telegram bot deployment |
| `index.py` | Root FastAPI entrypoint discovered automatically by Vercel |
| `knowledge.txt` | Gretta's sales persona and prompt rules |

---

## 🔐 Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather (**Required**) | — |
| `OPENROUTER_API_KEY` | OpenRouter API Key (**Required**) | — |
| `MODEL` | OpenRouter Model ID | `stealth/ox-alpha` |
| `VISION_MODEL` | OpenRouter Vision Model ID | `stealth/ox-alpha` |
| `DASHBOARD_URL` | Public HTTPS URL of the Vercel dashboard | — |
| `DATABASE_URL` | Neon Postgres connection string — shared DB for Render + Vercel; omit for local SQLite | — |
| `DB_PATH` | Path to SQLite database (used only when `DATABASE_URL` is unset) | `crm.db` |
| `PORT` | Health server / web port | `7860` (HF) / `8000` (Local) |
| `ADMIN_USERNAME` | Dashboard admin username | `admin` |
| `ADMIN_PASSWORD` | **Set it in prod!** Enables the dashboard sign-in gate (`/api/*` locked behind a signed HttpOnly session cookie). Empty = open API (local dev only). | — |
| `ADMIN_SESSION_SECRET` | Optional cookie-signing secret; rotating signs out all sessions | falls back to `ADMIN_PASSWORD` |
| `ALLOWED_TELEGRAM_IDS` | Comma-separated numeric Telegram IDs allowed to use the bot. Empty both lists = bot open to everyone (still fully logged). | — |
| `ALLOWED_TELEGRAM_USERNAMES` | Comma-separated @handles allowed to use the bot | — |
| `GOOGLE_SHEET_WEBAPP_URL` | Apps Script Web App `/exec` URL — enables the live Google Sheets backup mirror ([setup guide](#-google-sheets-backup-live-mirror-for-the-team)) | — |
| `GOOGLE_SHEET_SECRET` | Shared secret; must match the `SECRET` constant in the Apps Script | — |

> ⚠️ Never commit `.env` to version control. Keep secrets in your environment settings on Hugging Face and Vercel.

---

## 🔒 Pipeline Rules & Access Control

**Lead lifecycle (mirrors the Google Sheet Status column):**
`Message Sent → Seen Not Replied → Replied → Follow up 1-4 → Replied-No yet booked → Number received → Closing Call → Discovery Call booked → Won`, plus `Not Interested` / `Lost`.

- Every write stamps **Last Touchpoint**; marking a follow-up auto-dates it and saving a number flips **Number Received ✓** — same rules the sheet's dropdowns imply.
- The dashboard board/table, the Telegram cards and the Google Sheet mirror all share these exact values, so nothing drifts between them.
- Guardrails live once in `db.STAGE_TRANSITIONS` and are enforced by the DB layer itself — the UI merely mirrors them.

**Dashboard auth:** set `ADMIN_PASSWORD` (e.g. in Vercel env vars) and the whole API requires signing in at the glass login screen. Sessions are stateless signed cookies (7 days) — no database needed, works across serverless cold starts. Sign out anytime from the sidebar.

**Bot access control:** every Telegram account that messages the bot is recorded in the `bot_users` table (message counts, first/last seen, whitelist flag) — visible in the dashboard's **🛡 Bot Access** tab with Allow/Deny buttons, or via `/users` in Telegram. To hard-lock the bot, set `ALLOWED_TELEGRAM_IDS` / `ALLOWED_TELEGRAM_USERNAMES`; strangers then get a polite refusal showing their ID so you can `/allow <id>` them.
