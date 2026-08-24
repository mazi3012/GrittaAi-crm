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
2. **Auto-Claiming & Duplicate Protection**: Auto-claims leads based on the sender, preventing team collisions (`/claim @user` and `/check @user`).
3. **Interactive Telegram Menu**: Full app-style inline keyboard UI for pipeline management directly inside Telegram.
4. **Next-Gen CRM Dashboard**: FastAPI SPA with live lead status toggling, dual theme (Obsidian Dark / Slate Light), search/filters, and real-time statistics.

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

> ⚠️ Never commit `.env` to version control. Keep secrets in your environment settings on Hugging Face and Vercel.

---

## 🔒 Pipeline Rules & Access Control

**Lead lifecycle:** `New → Contacted → Meeting Booked → Active Client`, plus `Lost`.
- A **converted deal becomes an Active Client** (shown solid green everywhere). The stage is **locked**: nobody can drag it back to New/Contacted — from Telegram *or* the dashboard. The only exit is an explicit **🚫 Cancel Deal** when a client quits the service.
- **Cancelled clients** can be ♻️ re-activated (moved back to Active Client) if they return.
- Guardrails live once in `db.STAGE_TRANSITIONS` and are enforced by the DB layer itself — the UI merely mirrors them.

**Dashboard auth:** set `ADMIN_PASSWORD` (e.g. in Vercel env vars) and the whole API requires signing in at the glass login screen. Sessions are stateless signed cookies (7 days) — no database needed, works across serverless cold starts. Sign out anytime from the sidebar.

**Bot access control:** every Telegram account that messages the bot is recorded in the `bot_users` table (message counts, first/last seen, whitelist flag) — visible in the dashboard's **🛡 Bot Access** tab with Allow/Deny buttons, or via `/users` in Telegram. To hard-lock the bot, set `ALLOWED_TELEGRAM_IDS` / `ALLOWED_TELEGRAM_USERNAMES`; strangers then get a polite refusal showing their ID so you can `/allow <id>` them.
