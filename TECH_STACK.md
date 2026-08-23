# Gretta AI — Technical Stack & Architecture

> Internal Telegram CRM bot: teammates forward customer-chat screenshots to
> **@GrittaAi_bot**, the bot OCRs them, an LLM extracts structured lead data,
> and everything lands in a shared SQLite CRM with a live web dashboard.
>
> Stack verified live on 2026-08-22 · Python 3.12 · Linux · venv 1.4 GB

---

## 1. Architecture at a glance

```
 Telegram (@GrittaAi_bot)
    │  photo + optional caption          /claim  /check  /start
    ▼
 bot.py ── pyTelegramBotAPI long-polling (auto-reconnect loop)
    │  1. download photo via Bot API
    │  2. Pillow: decode → RGB flatten → downscale ≤ 1600 px (LANCZOS)
    │  3. PaddleOCR PP-OCRv6 pipeline (lazy-loaded, lock-serialized)
    │  4. extracted text capped at 4 000 chars
    ▼
 OpenRouter · chat/completions
    │  model: nvidia/nemotron-3-ultra-550b-a55b:free
    │  strict-JSON contract: lead / score / platform / stage /
    │                        next_steps / summary
    ▼
 db.py ── shared, thread-safe data layer (single schema source)
    │  merge-aware upsert into crm.db (SQLite, WAL mode)
    ▼
 dashboard.py ── FastAPI + Uvicorn on :8000
    └─ dark Tailwind table, all values HTML-escaped (XSS-safe)
```

## 2. Core stack

| Layer | Technology | Pinned version | Role |
|---|---|---|---|
| Language | Python | 3.12.x | runtime |
| Bot framework | pyTelegramBotAPI (`telebot`) | 4.36.1 | Telegram Bot API, long polling |
| OCR engine | PaddleOCR | 3.7.0 | screenshot → text (PP-OCRv6 det+rec, textline orientation) |
| OCR runtime | PaddlePaddle | 3.3.1 | tensor engine under PaddleOCR (CPU) |
| OCR suite | PaddleX | 3.7.2 | model pipelines PaddleOCR 3.x is built on |
| LLM gateway | OpenRouter REST API | — | lead analysis, strict JSON out |
| LLM model | `nvidia/nemotron-3-ultra-550b-a55b:free` | configurable via `.env` | reasoning + extraction |
| Web framework | FastAPI | 0.141.1 | dashboard HTTP API |
| ASGI server | Uvicorn | 0.52.4 | serves the dashboard |
| Database | SQLite (WAL mode) | stdlib `sqlite3` | `crm.db` lead store |
| HTTP client | requests + urllib3 Retry | 2.34.2 | OpenRouter calls, persistent session |
| Imaging | Pillow | 12.3.0 | decode / downscale screenshots |
| Numerics | NumPy | 2.3.5 | image arrays for OCR |
| Config | python-dotenv | 1.2.3 | `.env` secret loading |
| Dashboard UI | Tailwind CSS (browser CDN v4) | — | styling only, no build step |

## 3. Project files

| File | Lines | Purpose |
|---|---|---|
| `bot.py` | 361 | Telegram handlers, OCR pipeline, LLM analysis |
| `db.py` | 175 | schema + `get_lead` / `save_lead` / `all_leads`, WAL, write lock |
| `dashboard.py` | 133 | FastAPI dashboard, escaped HTML rendering |
| `knowledge.txt` | — | team knowledge injected into the system prompt |
| `crm.db` | — | the CRM data (WAL journal) |
| `.env` / `.env.example` | — | `TELEGRAM_TOKEN`, `OPENROUTER_API_KEY`, optional `MODEL` |
| `requirements.txt` | 12 | pinned direct dependencies |

## 4. Data model (`crm.db` → `leads`)

| Column | Type | Notes |
|---|---|---|
| `username` | TEXT PK | normalized: trimmed, lowercased, leading `@` |
| `claimed_by` | TEXT | first owner kept forever (no lead stealing) |
| `status` | TEXT | New / Contacted / Meeting Booked / Converted / Lost |
| `lead_score` | TEXT | HIGH / MEDIUM / LOW / UNKNOWN (LLM-assigned) |
| `platform` | TEXT | Instagram / WhatsApp / IndiaMART |
| `next_steps` | TEXT | actionable instruction from the LLM |
| `conversation_summary` | TEXT | appended per analysis, capped 500 chars |
| `last_updated` | TIMESTAMP | auto on every write |

## 5. Key engineering decisions

| Decision | Rationale |
|---|---|
| **Lazy OCR load** | PaddleOCR + 5 models take seconds to init; deferring to the first photo keeps bot startup instant. Double-checked locking (`OCR_LOCK`) prevents double init. |
| **`enable_mkldnn=False`** | paddlepaddle 3.3.1 crashes at *predict* time with `NotImplementedError` (PIR/OneDNN executor bug) when MKL-DNN is on. Caught only by a live test — a construct-time try/except could never have caught it. Documented in `bot.py`. |
| **Downscale to ≤ 1600 px** | OCR accuracy is unaffected for screenshots; runtime drops sharply on large images. |
| **OCR text cap (4 000 chars)** | Protects LLM token budget/cost from pathological screenshots. |
| **Persistent `requests.Session` + Retry** | Connection reuse + automatic retry on 429/5xx with backoff for OpenRouter. |
| **Strict JSON LLM contract** | The model must return exactly one JSON object (`lead/score/platform/stage/next_steps/summary`); parsed with a `\{.*\}` DOTALL regex + `json.loads`, then every field sanitized (enum checks, `@` prefix, 500-char summary cap). |
| **Python-side merge upsert** | SQL `ON CONFLICT ... excluded.*` returns post-COALESCE values, making in-SQL merging unreliable. `db.save_lead()` implements the rules readably: first owner kept forever, New→Contacted auto-advance on first touch, empty fields never clobber, summaries append, higher-quality score wins. |
| **WAL journal + `busy_timeout`** | Bot (writer) and dashboard (reader) run as separate processes without lock errors. |
| **`html.escape` everywhere** | LLM/OCR text is untrusted input; every dynamic value in the dashboard is escaped (attribute values with `quote=True`), closing the stored-XSS hole. |
| **Prompt-injected knowledge** | `knowledge.txt` is pasted into the system prompt — replaces the deleted RAG/embeddings pipeline (torch, sentence-transformers, `knowledge.db`) at zero quality cost for a knowledge base this small. |
| **Single schema source** | `db.py` is the only place that knows the schema; both processes call `init_db()` (once per process, idempotent). |

## 6. Concurrency & reliability model

- **`OCR_LOCK`** — the Paddle predictor is not thread-safe; photo handling is serialized.
- **`db._write_lock`** — one process-wide write lock; WAL handles cross-process read/write.
- **`infinity_polling(timeout=60, skip_pending=True)`** inside `while True` — Telegram disconnects auto-reconnect after 5 s.
- **Graceful degradation** — OCR empty → friendly reply; LLM down → "try again"; malformed JSON → sanitized defaults; every handler wrapped in try/except.
- **200-wrapped upstream errors** — OpenRouter can return HTTP 200 whose body is `{"error": …}` when the model provider (e.g. Nvidia) is overloaded; `ask_ai` detects this, retries once, and returns `None` cleanly.

## 7. Performance profile (measured on this machine)

| Stage | Cold | Warm |
|---|---|---|
| `import bot` | ~0.36 s | — |
| OCR model init (first photo only) | 3–7 s | — |
| OCR predict (900×420 screenshot) | 4.8 s | 4.7 s |
| LLM analysis (OpenRouter, free tier) | ~5–30 s | — |
| DB upsert + read | < 5 ms | < 5 ms |

## 8. Removed / rejected (and why)

| Removed | Reason |
|---|---|
| `sentence-transformers`, `torch`, `transformers`, full CUDA stack (~25 pkgs) | Only used by deleted `build_db.py`; shrank venv **6.3 GB → 1.4 GB** |
| `build_db.py` + RAG/embeddings pipeline | `knowledge.db` never existed in the flow; knowledge is prompt-injected instead |
| `setup_crm.py` | Stale schema would have broken fresh installs (`CREATE TABLE IF NOT EXISTS` never upgrades) |
| SQL upsert merging | `excluded.*` post-COALESCE trap → replaced with explicit Python merge |
| MKL-DNN acceleration | Crashes Paddle 3.3.1 at predict time (see §5) |

## 9. Running it

```bash
# one-time setup
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in real tokens

# run (two processes)
venv/bin/python bot.py                       # Telegram bot (long polling)
venv/bin/python dashboard.py                 # CRM dashboard on :8000

# verify
curl http://localhost:8000/                  # dashboard HTML
venv/bin/python -c "import bot"              # env + syntax check
```

Secrets live only in `.env` (git-ignored). Rotate the Telegram token via
@BotFather `/revoke` and the OpenRouter key at openrouter.ai/keys if it was
ever committed or shared.

