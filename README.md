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
   - `MODEL` = `nvidia/nemotron-3-ultra-550b-a55b:free` (optional)
   - `DASHBOARD_URL` = `https://your-vercel-dashboard.vercel.app` (optional)
5. Hugging Face will automatically build the container using the provided `Dockerfile` and start the bot with a built-in health check on port `7860`.

### 2. Host FastAPI Dashboard on Vercel (Free)

1. Import your repository (`mazi3012/GrittaAi-crm.git`) into [Vercel](https://vercel.com/new).
2. Vercel automatically detects `vercel.json` and configures the `@vercel/python` builder for `dashboard.py`.
3. In Project Settings -> **Environment Variables**, optionally set:
   - `DB_PATH` = `/tmp/crm.db` (for serverless environments)
4. Deploy! Your CRM dashboard will be live at `https://your-project.vercel.app`.

---

## 📁 Project Layout

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot: OCR → LLM analysis → interactive inline menus + HF health server |
| `dashboard.py` | FastAPI web dashboard serving static SPA and REST endpoints |
| `db.py` | Shared SQLite data layer with WAL mode support |
| `static/` | Next-Gen SPA dashboard (HTML, CSS, JS with Obsidian/Slate theme support) |
| `Dockerfile` | Docker configuration for Hugging Face Spaces deployment |
| `vercel.json` | Serverless configuration for Vercel deployment |
| `knowledge.txt` | Gretta's sales persona and prompt rules |

---

## 🔐 Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather (**Required**) | — |
| `OPENROUTER_API_KEY` | OpenRouter API Key (**Required**) | — |
| `MODEL` | OpenRouter Model ID | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `DASHBOARD_URL` | Public HTTPS URL of the Vercel dashboard | — |
| `DB_PATH` | Path to SQLite database | `crm.db` |
| `PORT` | Health server / web port | `7860` (HF) / `8000` (Local) |

> ⚠️ Never commit `.env` to version control. Keep secrets in your environment settings on Hugging Face and Vercel.
