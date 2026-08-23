"""Gretta AI - Telegram CRM bot.

Pipeline: screenshot -> OCR (PaddleOCR) -> LLM analysis (OpenRouter)
-> structured lead saved to SQLite (crm.db), with /claim and /check
commands to prevent duplicate outreach.

Interactive layer (this build):
- App-style inline-keyboard menus: /start opens a button menu, leads open
  as tappable cards, and the deal stage can be moved with one tap.
- Free-text chat: anything that is not a command goes to the LLM, so the
  bot can actually hold a conversation (per-chat short-term memory).
- Progress feedback: screenshot analysis edits a single status message
  (Reading -> Analyzing -> Result) instead of spamming new ones.
- Screenshot triage: every screenshot asks what to do with it (Log /
  Summarize / Advice) and asks for the client @username when none is visible.

Optimizations kept from the previous version:
- OCR model is loaded lazily on the first photo (faster startup)
- Screenshots are downscaled before OCR (much faster, same accuracy)
- One persistent HTTP session with retries to OpenRouter
- DB schema lives in db.py; init happens once per process (not per query)
- Strict JSON output from the LLM (no fragile multi-line regex parsing)
- OCR text is capped before it hits the LLM (token/cost safety)
- knowledge.txt is injected into the system prompt (no RAG needed)
"""

import html
import io
import json
import os
import re
import threading
import time

import numpy as np
import requests
import telebot
from dotenv import load_dotenv
from PIL import Image
from telebot import types

os.environ.setdefault("GLOG_minloglevel", "2")
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
# Telegram rejects localhost/private URLs on inline buttons ("Wrong HTTP URL"),
# which would crash /start and /help — so omit the button for unusable URLs.
if not DASHBOARD_URL.startswith(("https://", "http://")) or "localhost" in DASHBOARD_URL or "127.0.0.1" in DASHBOARD_URL:
    DASHBOARD_URL = None

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("Error: TELEGRAM_TOKEN or OPENROUTER_API_KEY missing from .env!")

from db import (  # noqa: E402  (after env validation)
    all_leads,
    get_lead,
    leads_for_owner,
    normalize_username,
    save_lead,
    set_lead_stage,
)

MAX_IMAGE_DIM = 1600      # px - screenshots OCR fine well below this
MAX_OCR_CHARS = 4000      # cap text sent to the LLM
MAX_SUMMARY_CHARS = 500   # cap stored summary length
TG_MSG_LIMIT = 3900       # split long replies (Telegram hard-caps at 4096)
OCR_LOCK = threading.Lock()  # PaddleOCR predictor is not thread-safe

STAGES = ["New", "Contacted", "Meeting Booked", "Converted", "Lost"]
SCORE_EMOJI = {"HIGH": "🔥", "MEDIUM": "🟡", "LOW": "🧊"}
STAGE_EMOJI = {
    "New": "🆕", "Contacted": "📨", "Meeting Booked": "📅",
    "Converted": "🏆", "Lost": "❌",
}
CHAT_MEMORY_TURNS = 6     # user+assistant exchanges kept per chat
CHAT_MAX_CHARS = 1200     # cap per stored chat turn (token safety)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
_ocr = None

# ------------------------------------------------------------- chat memory
# Per-chat rolling window of {"role": ..., "content": ...} for free-text chat.
# In-process only: history resets on restart, which is fine for small talk.
_chat_history = {}
_chat_lock = threading.Lock()
_typing_stop = threading.Event()
# chat_id -> "check" | "claim": set when we asked the user for a @username
_pending_prompts = {}
# chat_id -> {"photo_message_id", "text", "target"}: screenshots waiting for
# the user to pick Log/Summarize/Advice (and/or supply the client username).
_pending_shots = {}


def remember(chat_id, role, content):
    """Append a turn to the chat's LLM memory (bounded)."""
    with _chat_lock:
        hist = _chat_history.setdefault(chat_id, [])
        hist.append({"role": role, "content": content[:CHAT_MAX_CHARS]})
        while len(hist) > CHAT_MEMORY_TURNS * 2:
            hist.pop(0)


def history_for(chat_id):
    with _chat_lock:
        return list(_chat_history.get(chat_id, []))


# ------------------------------------------------------------------ ui kit
def esc(text):
    """Escape text for HTML parse_mode."""
    return html.escape(str(text), quote=False)


def main_menu_kb():
    """Main app menu shown by /start and the 🏠 Home button."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🗂 My Leads", callback_data="menu:leads"),
        types.InlineKeyboardButton("🔎 Check Lead", callback_data="ask:check"),
    )
    kb.add(
        types.InlineKeyboardButton("🤝 Claim Lead", callback_data="ask:claim"),
        types.InlineKeyboardButton("💬 Talk to Gretta", callback_data="ask:chat"),
    )
    if DASHBOARD_URL:
        kb.add(types.InlineKeyboardButton("🌐 Open Dashboard", url=DASHBOARD_URL))
    return kb


def home_button_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"))
    return kb


def welcome_text(name):
    return (
        f"👋 <b>Welcome, {esc(name)}!</b>\n\n"
        "I'm <b>Gretta AI</b> — your CRM copilot.\n\n"
        "📸 <b>Send a screenshot</b> of any sales chat and I'll extract the "
        "lead, score it and log it automatically.\n\n"
        "<b>What you can do:</b>\n"
        "• 🗂 Browse / manage <b>your</b> leads with tap buttons\n"
        "• 🔎 <b>/check @user</b> — see who owns a lead\n"
        "• 🤝 <b>/claim @user</b> — claim a lead\n"
        "• 💬 Or just <b>talk to me</b> — ask anything!\n\n"
        "👇 Use the buttons below or type a command."
    )


def register_bot_commands():
    """Show the official command menu (the blue 'Menu' button in Telegram)."""
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Open the Gretta app menu"),
            types.BotCommand("leads", "Show my leads"),
            types.BotCommand("check", "Check a lead — /check @username"),
            types.BotCommand("claim", "Claim a lead — /claim @username"),
            types.BotCommand("stats", "Team pipeline summary"),
            types.BotCommand("help", "How to use Gretta"),
        ])
    except Exception as exc:
        print(f"Could not set bot commands: {exc}")


def send_long(chat_id, text, reply_to=None, kb=None):
    """Send text split into Telegram-safe chunks."""
    chunks = [text[i:i + TG_MSG_LIMIT] for i in range(0, len(text), TG_MSG_LIMIT)]
    sent = None
    for idx, chunk in enumerate(chunks):
        markup = kb if idx == len(chunks) - 1 else None
        if idx == 0 and reply_to:
            sent = bot.send_message(
                chat_id, chunk, reply_to_message_id=reply_to,
                parse_mode="HTML", reply_markup=markup,
            )
        else:
            sent = bot.send_message(chat_id, chunk, parse_mode="HTML", reply_markup=markup)
    return sent


def edit_status(msg, text, reply_markup=None):
    """Edit a progress message in place; fall back silently if it vanished."""
    try:
        bot.edit_message_text(
            text, msg.chat.id, msg.message_id,
            parse_mode="HTML", reply_markup=reply_markup,
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print(f"edit_message_text failed: {exc}")


def get_ocr():
    """Load PaddleOCR on first use instead of at startup.

    NOTE: enable_mkldnn must stay OFF — paddlepaddle 3.3.1 crashes at
    predict time with NotImplementedError (PIR/OneDNN bug), verified live.
    """
    global _ocr
    if _ocr is None:
        with OCR_LOCK:
            if _ocr is None:
                from paddleocr import PaddleOCR
                _ocr = PaddleOCR(
                    use_textline_orientation=True, lang="en", enable_mkldnn=False
                )
    return _ocr


def load_knowledge():
    try:
        with open("knowledge.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return (
            "Gretta AI is the internal CRM and lead management assistant. "
            "Prevent duplicate outreach. Score leads HIGH, MEDIUM or LOW "
            "based on readiness to act. Be concise and human-friendly."
        )


KNOWLEDGE = load_knowledge()

SYSTEM_PROMPT = (
    "You are Gretta AI, a sharp enterprise sales CRM assistant. Keep outputs "
    "direct, professional, and structured.\n\n"
    "TEAM KNOWLEDGE BASE:\n" + KNOWLEDGE
)

ANALYSIS_INSTRUCTIONS = (
    "Analyze this sales conversation and reply with ONLY a single JSON object "
    "(no markdown fences, no extra text) using exactly these keys:\n"
    '{"lead": "@username", "score": "HIGH|MEDIUM|LOW", '
    '"platform": "Instagram|WhatsApp|IndiaMART", '
    '"stage": "New|Contacted|Meeting Booked|Converted|Lost", '
    '"next_steps": "short actionable instruction", '
    '"summary": "one clean professional sentence"}\n\n'
    "CONVERSATION LAYOUT (critical):\n"
    "- This is a phone screenshot of a chat app. Messages bubble from BOTH "
    "sides: bubbles on the LEFT edge belong to THE CLIENT (the prospect); "
    "bubbles on the RIGHT edge belong to US — the Gretta team member.\n"
    '- "lead"/"next_steps"/"summary" must describe the CLIENT\'s intent '
    "(left side), never our own replies (right side).\n"
    "- A client asking about price or service = interest = HIGH/MEDIUM "
    "score. Do NOT score based on what WE promised.\n\n"
    "Use the provided target username unless the screenshot clearly names "
    "a different prospect."
)

# ------------------------------------------------------------------- http
session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "HTTP-Referer": "http://localhost",
    "X-Title": "GrettaAI",
    "Content-Type": "application/json",
})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry = Retry(total=2, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
except Exception:
    pass


def ask_ai(prompt_text, timeout=90):
    """Send a prompt to OpenRouter and return the reply text, or None.

    OpenRouter sometimes reports upstream outages INSIDE an HTTP 200 body
    (e.g. {"error": {...}} when the model provider is overloaded), so both
    cases are handled and retried once.
    """
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
    }
    for attempt in (1, 2):
        try:
            resp = session.post(OPENROUTER_URL, json=payload, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data.get("error"), dict):
                    print(f"OpenRouter upstream error: {str(data['error'])[:200]}")
                else:
                    choices = data.get("choices")
                    if choices:
                        return choices[0]["message"]["content"]
            else:
                print(f"OpenRouter API error ({resp.status_code}): {resp.text[:200]}")
        except (requests.RequestException, ValueError) as exc:
            print(f"OpenRouter request error: {exc}")
        if attempt == 1:
            time.sleep(3)
    return None


# -------------------------------------------------------------------- ocr
def extract_text(ocr_result):
    """Flatten any PaddleOCR output shape into plain text."""
    extracted = []

    def search_for_text(item):
        if isinstance(item, dict):
            for key in ("text", "rec_text", "rec_texts", "words"):
                if key in item and item[key]:
                    val = item[key]
                    if isinstance(val, str):
                        extracted.append(val)
                    elif isinstance(val, list):
                        extracted.extend(v for v in val if isinstance(v, str))
            for v in item.values():
                search_for_text(v)
        elif isinstance(item, (list, tuple)):
            if (len(item) >= 2 and isinstance(item[1], (list, tuple))
                    and item[1] and isinstance(item[1][0], str)):
                extracted.append(item[1][0])
            elif (len(item) == 2 and isinstance(item[0], str)
                    and isinstance(item[1], (float, int))):
                extracted.append(item[0])
            else:
                for sub in item:
                    search_for_text(sub)
        elif hasattr(item, "__dict__"):
            if isinstance(getattr(item, "rec_text", None), str):
                extracted.append(item.rec_text)
            if isinstance(getattr(item, "text", None), str):
                extracted.append(item.text)
            search_for_text(item.__dict__)

    if hasattr(ocr_result, "__iter__") and not isinstance(
            ocr_result, (dict, list, tuple, str)):
        try:
            ocr_result = list(ocr_result)
        except Exception:
            pass

    search_for_text(ocr_result)

    clean, seen = [], set()
    for t in extracted:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            clean.append(t)
    return "\n".join(clean)


def preprocess_image(image_bytes):
    """Decode, flatten to RGB, and downscale large screenshots for fast OCR."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = image.size
    if max(w, h) > MAX_IMAGE_DIM:
        scale = MAX_IMAGE_DIM / max(w, h)
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return np.array(image)


# --------------------------------------------------------------- handlers
def sender_handle(message):
    user = message.from_user
    return f"@{user.username}" if user.username else (user.first_name or "Unknown")


def lead_card(lead):
    """Render a lead row as an HTML card (row = db.get_lead tuple order)."""
    username, owner, status, score, platform = lead[0], lead[1], lead[2], lead[3], lead[4]
    next_steps, summary, updated = lead[5], lead[6], lead[7]
    score_tag = SCORE_EMOJI.get(score, "⚪️")
    stage_tag = STAGE_EMOJI.get(status, "•")
    return (
        f"👤 <b>{esc(username)}</b>  {score_tag} <i>{esc(score)}</i>\n"
        f"{stage_tag} Stage: <b>{esc(status or 'New')}</b>\n"
        f"📱 Platform: {esc(platform or 'Instagram')}\n"
        f"🧑‍💼 Owner: <b>{esc(owner) if owner else 'Unclaimed'}</b>\n"
        f"🎯 Next: {esc(next_steps or 'Review lead')}\n"
        f"🕒 Updated: {esc(updated or '-')}\n\n"
        f"💡 {esc(summary) if summary else '<i>No summary yet</i>'}"
    )


def stage_kb(username):
    """One-tap deal-stage mover for a lead card."""
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[
        types.InlineKeyboardButton(
            f"{STAGE_EMOJI[s]} {s}", callback_data=f"stage:{username}:{s}"
        )
        for s in STAGES
    ])
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"))
    return kb


def claim_kb(username):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"🤝 Claim {username}", callback_data=f"claim:{username}"))
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"))
    return kb


def render_my_leads(rows):
    """Summarize a teammate's leads -> (text, list of tappable buttons)."""
    header = "🗂 <b>My Leads</b>\n\n"
    if not rows:
        return (
            header + "You don't own any leads yet.\n"
            "📸 Send me a screenshot of a sales chat and I'll log your "
            "first lead!",
            [],
        )
    lines = [header]
    buttons = []
    shown = rows[:30]
    for row in shown:
        username, status, score = row[0], row[2], row[3]
        score_e = SCORE_EMOJI.get((score or "").upper(), "▫️")
        stage_e = STAGE_EMOJI.get(status or "New", "🆕")
        lines.append(
            f"• {score_e} <b>{esc(username)}</b> · {stage_e} {esc(status or 'New')}"
        )
        cb = f"lead:{username}"
        if len(cb.encode()) <= 64:  # Telegram callback_data hard limit
            buttons.append(
                types.InlineKeyboardButton(f"{score_e} {username}", callback_data=cb)
            )
    if len(rows) > len(shown):
        lines.append(
            f"\n<i>…and {len(rows) - len(shown)} more — see the dashboard</i>"
        )
    return "\n".join(lines), buttons


def leads_kb(buttons):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if buttons:
        kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"))
    return kb


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.send_message(
        message.chat.id,
        welcome_text(message.from_user.first_name or "there"),
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


def run_claim(username, claimer):
    """Shared claim logic for the command, the reply-flow and the button."""
    lead = get_lead(username)
    if lead and lead[1] and lead[1].lower() != claimer.lower():
        return False, (
            f"⚠️ Lead {esc(username)} is ALREADY claimed by <b>{esc(lead[1])}</b>!"
        )
    save_lead(username=username, claimed_by=claimer, status="Contacted")
    return True, (
        f"✅ Lead <b>{esc(normalize_username(username))}</b> claimed by "
        f"<b>{esc(claimer)}</b> — stage set to 📨 Contacted."
    )


@bot.message_handler(commands=["claim"])
def handle_claim(message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        _pending_prompts[message.chat.id] = "claim"
        bot.send_message(
            message.chat.id,
            "🤝 <b>Claim a lead</b>\n\nWhich @username? For example: <code>@john_doe</code>",
            parse_mode="HTML",
        )
        return
    ok, text = run_claim(parts[1], sender_handle(message))
    bot.send_message(message.chat.id, text, parse_mode="HTML",
                     reply_markup=home_button_kb())


@bot.message_handler(commands=["check"])
def handle_check(message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        _pending_prompts[message.chat.id] = "check"
        bot.send_message(
            message.chat.id,
            "🔎 <b>Check a lead</b>\n\nWhich @username? For example: <code>@john_doe</code>",
            parse_mode="HTML",
        )
        return
    show_lead_record(message.chat.id, parts[1])


@bot.message_handler(commands=["leads"])
def handle_leads(message):
    viewer = sender_handle(message)
    rows = leads_for_owner(viewer)
    text, btns = render_my_leads(rows)
    bot.send_message(message.chat.id, text, parse_mode="HTML",
                     reply_markup=leads_kb(btns))


@bot.message_handler(commands=["stats"])
def handle_stats(message):
    rows = all_leads()
    total = len(rows)
    if not total:
        bot.send_message(
            message.chat.id,
            "📭 The CRM is empty — send me a 📸 screenshot to log your "
            "first lead!",
            reply_markup=home_button_kb(),
        )
        return
    by_stage = {}
    for row in rows:
        stage = row[2] or "New"
        by_stage[stage] = by_stage.get(stage, 0) + 1
    unclaimed = sum(1 for row in rows if not row[1])
    hot = sum(1 for row in rows if (row[3] or "").upper() == "HIGH")
    lines = ["📊 <b>Team Pipeline</b>\n"]
    for stage in STAGES:
        n = by_stage.get(stage, 0)
        bar = "█" * n if n else ""
        lines.append(f"{STAGE_EMOJI.get(stage, '▫️')} <b>{stage}</b>: {n} {bar}")
    lines.append("")
    lines.append(f"Total leads: <b>{total}</b>")
    lines.append(f"🔥 Hot (HIGH score): <b>{hot}</b>")
    lines.append(f"🟢 Unclaimed: <b>{unclaimed}</b>")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML",
                     reply_markup=home_button_kb())


@bot.message_handler(commands=["help"])
def handle_help(message):
    bot.send_message(
        message.chat.id,
        "<b>How to use Gretta AI</b> 🤖\n\n"
        "📸 <b>Log a lead:</b> just send a screenshot of any sales chat "
        "(WhatsApp, Instagram, LinkedIn…). I read it, score the lead and "
        "save it — no typing needed.\n\n"
        "<b>Commands</b>\n"
        "/leads — browse <i>your</i> leads with tappable cards\n"
        "/check @user — see who owns a lead\n"
        "/claim @user — claim a lead for yourself\n"
        "/stats — team pipeline at a glance\n\n"
        "💬 <b>Talk to me:</b> type anything — negotiation tips, pipeline "
        "questions, follow-up ideas.\n"
        "⚡️ <b>Shortcuts:</b> tap a lead button to open its card, then tap "
        "a stage to move the deal.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


def show_lead_record(chat_id, username):
    lead = get_lead(username)
    if not lead:
        uname = normalize_username(username) or username
        bot.send_message(
            chat_id,
            f"🟢 <b>{esc(uname)}</b> is brand new — not in the CRM yet. "
            "Nobody has touched this lead!",
            parse_mode="HTML",
            reply_markup=claim_kb(uname),
        )
        return
    bot.send_message(chat_id, lead_card(lead), parse_mode="HTML",
                     reply_markup=stage_kb(lead[0]))


def triage_kb(has_target):
    """What-to-do buttons shown immediately after a screenshot arrives."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    if has_target:
        kb.add(types.InlineKeyboardButton(
            "✅ Log this lead", callback_data="shot:log"))
    else:
        kb.add(types.InlineKeyboardButton(
            "👤 Enter client @username", callback_data="shot:askid"))
    kb.add(types.InlineKeyboardButton(
        "📝 Summarize the chat", callback_data="shot:summarize"))
    kb.add(types.InlineKeyboardButton(
        "💬 What should I reply?", callback_data="shot:advice"))
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="shot:cancel"))
    return kb


def start_screenshot_flow(message, file_id):
    """Store the upload, then ask the user what to do with it.

    The client @username is picked up from the caption when present;
    otherwise Gretta asks for it immediately (per team workflow).
    """
    chat_id = message.chat.id
    caption = message.caption or ""
    users = re.findall(r"@[A-Za-z0-9_]+", caption)
    target = users[0].lower() if users else None
    _pending_shots[chat_id] = {
        "file_id": file_id,
        "caption": caption,
        "target": target,
        "owner": sender_handle(message),
        "message_id": message.message_id,
    }
    if target:
        triage_msg = bot.reply_to(
            message,
            f"📸 Screenshot received!\n\nWhat should I do with it? "
            f"I'll use <code>{esc(target)}</code> as the client unless you "
            f"type another @username.",
            parse_mode="HTML",
            reply_markup=triage_kb(True),
        )
    else:
        triage_msg = bot.reply_to(
            message,
            "📸 Screenshot received!\n\n"
            "❓ <b>Who is the client?</b> Type their @username "
            "(example: <code>@john_doe</code>) — or just tap an action below "
            "to continue without it.",
            parse_mode="HTML",
            reply_markup=triage_kb(False),
        )
    _pending_shots[chat_id]["triage_id"] = triage_msg.message_id


def run_shot_action(chat_id, action, status=None):
    """Run OCR + the chosen action for the chat's pending screenshot."""
    shot = _pending_shots.pop(chat_id, None)
    if not shot:
        bot.send_message(
            chat_id,
            "🤔 That screenshot request expired — send the screenshot again?",
            reply_markup=home_button_kb(),
        )
        return

    if status is None:
        status = bot.send_message(
            chat_id, "🔍 <b>Reading screenshot…</b> running OCR",
            parse_mode="HTML")

    # ------------------------------------------------------------ OCR
    try:
        file_info = bot.get_file(shot["file_id"])
        image_bytes = bot.download_file(file_info.file_path)
        img_np = preprocess_image(image_bytes)
        # NOTE: do NOT hold OCR_LOCK while calling get_ocr(): get_ocr()
        # acquires OCR_LOCK itself and threading.Lock is not reentrant,
        # so nesting here deadlocked the first screenshot of every run.
        ocr = get_ocr()
        with OCR_LOCK:
            raw = ocr.predict(img_np) if hasattr(ocr, "predict") else ocr.ocr(img_np)
        extracted_text = extract_text(raw)
    except Exception as exc:
        print(f"OCR failed for chat {chat_id}: {exc}")
        edit_status(status, "⚠️ Couldn't read that image (OCR failed). Try "
                            "sending it as a photo instead of a file?")
        return

    if not extracted_text.strip():
        edit_status(status, "🤷 No readable text found in that image. "
                            "Send a clearer screenshot?")
        return
    if len(extracted_text) > MAX_OCR_CHARS:
        extracted_text = extracted_text[:MAX_OCR_CHARS] + "\n[truncated]"

    caption = shot.get("caption") or ""
    cap_users = re.findall(r"@[A-Za-z0-9_]+", caption)
    ocr_users = re.findall(r"@[A-Za-z0-9_]+", extracted_text)
    all_users = list(dict.fromkeys(cap_users + ocr_users))
    auto_target = cap_users[0] if cap_users else (all_users[0] if all_users else None)
    # A username typed by the user always wins over OCR guesses
    target_user = shot.get("target") or auto_target or "@unknown_lead"

    if action == "log":
        analyze_and_reply(status, extracted_text, caption, target_user,
                          owner=shot.get("owner"))
    elif action == "summarize":
        summarize_screenshot(chat_id, status, extracted_text, target_user)
    elif action == "advice":
        advice_for_screenshot(chat_id, status, extracted_text, target_user)


def analyze_and_reply(status, extracted_text, user_caption, target_user,
                      owner=None):
    """LLM pipeline: score + persist the lead, then render its card."""

    # ---------------------------------------------------------------- llm
    edit_status(
        status,
        f"🧠 <b>Analyzing lead</b> {esc(target_user)}…\n"
        "<i>Gretta is scoring intent and extracting next steps</i>",
    )
    prompt = (
        f"User Caption: '{user_caption}'\n\n"
        f"Extracted Conversation Text:\n{extracted_text}\n\n"
        f"Target Lead Username: {target_user}\n\n"
        f"{ANALYSIS_INSTRUCTIONS}"
    )

    ai_reply = ask_ai(prompt)
    if not ai_reply:
        edit_status(status, "⚠️ AI analysis failed — the model may be busy. "
                            "Please try again.")
        return

    match = re.search(r"\{.*\}", ai_reply, re.DOTALL)
    if not match:
        edit_status(status, "⚠️ Could not parse AI response. Try again.")
        return
    try:
        info = json.loads(match.group(0))
    except json.JSONDecodeError:
        edit_status(status, "⚠️ AI returned malformed data. Try again.")
        return

    found_user = str(info.get("lead") or target_user).strip()
    if not found_user.startswith("@"):
        found_user = f"@{found_user}"
    found_score = str(info.get("score", "MEDIUM")).upper()
    if found_score not in ("HIGH", "MEDIUM", "LOW"):
        found_score = "MEDIUM"
    found_platform = str(info.get("platform", "Instagram")).strip()
    found_stage = str(info.get("stage", "Contacted")).strip()
    found_next = str(info.get("next_steps", "Follow up with client")).strip()
    found_summary = str(info.get("summary", "Screenshot analyzed")).strip()
    if len(found_summary) > MAX_SUMMARY_CHARS:
        found_summary = found_summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

    # Never steal a lead another teammate already owns
    existing = get_lead(found_user)
    owner = existing[1] if (existing and existing[1]) else (owner or "Unclaimed")

    save_lead(
        username=found_user,
        claimed_by=owner,
        lead_score=found_score,
        platform=found_platform,
        status=found_stage,
        next_steps=found_next,
        summary=found_summary,
    )

    lead = get_lead(found_user)
    card = lead_card(lead) if lead else f"👤 <b>{esc(found_user)}</b> saved."
    edit_status(status, f"✅ <b>Lead logged!</b>\n\n{card}", stage_kb(found_user))


LAYOUT_NOTE = (
    "CONVERSATION LAYOUT: bubbles on the LEFT edge are THE CLIENT "
    "(the prospect); bubbles on the RIGHT edge are US (the Gretta sales "
    "teammate). Base your answer on the CLIENT's left-side messages."
)


def summarize_screenshot(chat_id, status, text, target):
    edit_status(status, f"🧠 <b>Summarizing the chat with {esc(target)}…</b>")
    prompt = (
        f"{LAYOUT_NOTE}\n\nSummarize this sales chat in 3-5 short plain-text "
        f"bullet lines for our CRM notes: what the CLIENT wants, where the "
        f"deal stands, price discussed, and the single most important next "
        f"step. No markdown headings.\n\n"
        f"Lead: {target}\n\nConversation:\n{text}"
    )
    reply = ask_ai(prompt)
    if not reply:
        edit_status(status, "⚠️ AI summary failed — the model may be busy. "
                            "Please try again.")
        return
    body = esc(reply.strip())[:3500]
    edit_status(status,
                f"📝 <b>Chat summary — {esc(target)}</b>\n\n{body}",
                home_button_kb())


def advice_for_screenshot(chat_id, status, text, target):
    edit_status(status, f"🧠 <b>Crafting the perfect reply for {esc(target)}…</b>")
    prompt = (
        f"{LAYOUT_NOTE}\n\nSuggest ONE great next reply WE should send the "
        f"client, then 2 short alternative angles. Keep it human, friendly "
        f"and concise — no markdown headings.\n\n"
        f"Lead: {target}\n\nConversation:\n{text}"
    )
    reply = ask_ai(prompt)
    if not reply:
        edit_status(status, "⚠️ AI advice failed — the model may be busy. "
                            "Please try again.")
        return
    body = esc(reply.strip())[:3500]
    edit_status(status,
                f"💬 <b>Suggested reply — {esc(target)}</b>\n\n{body}",
                home_button_kb())


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    try:
        start_screenshot_flow(message, message.photo[-1].file_id)
    except Exception as exc:
        print(f"Photo processing error: {exc}")
        try:
            bot.reply_to(message, "⚠️ Couldn't process that image. Please try again.")
        except Exception:
            pass


@bot.message_handler(content_types=["document"])
def handle_document(message):
    """Users often forward screenshots as files (no compression) — support it."""
    mime = getattr(message.document, "mime_type", "") or ""
    if not mime.startswith("image/"):
        bot.reply_to(
            message,
            "📎 That's not an image. Send a chat <b>screenshot</b> and I'll "
            "analyze it for you!",
            parse_mode="HTML",
        )
        return
    try:
        start_screenshot_flow(message, message.document.file_id)
    except Exception as exc:
        print(f"Document processing error: {exc}")
        try:
            bot.reply_to(message, "⚠️ Couldn't process that image. Please try again.")
        except Exception:
            pass


# --------------------------------------------------------------- callbacks
@bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
def on_menu(call):
    chat_id = call.message.chat.id
    data = call.data
    if data == "menu:home":
        text = welcome_text(call.from_user.first_name or "there")
        kb = main_menu_kb()
    elif data == "menu:leads":
        viewer = sender_handle(call.message)
        rows = leads_for_owner(viewer)
        text, btns = render_my_leads(rows)
        kb = leads_kb(btns)
    elif data == "ask:check":
        _pending_prompts[chat_id] = "check"
        bot.send_message(chat_id, "🔎 Send me the @username to check:",
                         parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    elif data == "ask:claim":
        _pending_prompts[chat_id] = "claim"
        bot.send_message(chat_id, "🤝 Send me the @username to claim:",
                         parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    else:  # ask:chat
        bot.send_message(
            chat_id,
            "💬 <b>Chat mode!</b> Just type anything — ask about your "
            "pipeline, negotiation tips, or what to say next.",
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)
        return
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id,
                              parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("lead:"))
def on_lead_card(call):
    username = call.data[len("lead:"):]
    lead = get_lead(username)
    if not lead:
        bot.answer_callback_query(call.id, "Lead no longer exists.", show_alert=True)
        return
    try:
        bot.edit_message_text(lead_card(lead), call.message.chat.id,
                              call.message.message_id, parse_mode="HTML",
                              reply_markup=stage_kb(username))
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            bot.send_message(call.message.chat.id, lead_card(lead),
                             parse_mode="HTML", reply_markup=stage_kb(username))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("stage:"))
def on_stage_change(call):
    try:
        _, username, new_stage = call.data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid action.")
        return
    if new_stage not in STAGES:
        bot.answer_callback_query(call.id, "Unknown stage.")
        return
    set_lead_stage(username, new_stage)
    lead = get_lead(username)
    toast = f"{STAGE_EMOJI.get(new_stage, '✅')} {normalize_username(username)} → {new_stage}"
    try:
        bot.edit_message_text(lead_card(lead), call.message.chat.id,
                              call.message.message_id, parse_mode="HTML",
                              reply_markup=stage_kb(username))
    except Exception:
        pass
    bot.answer_callback_query(call.id, toast)


@bot.callback_query_handler(func=lambda c: c.data.startswith("claim:"))
def on_claim_button(call):
    username = call.data[len("claim:"):]
    claimer = sender_handle(call.message)
    ok, text = run_claim(username, claimer)
    bot.send_message(call.message.chat.id, text, parse_mode="HTML",
                     reply_markup=home_button_kb() if ok else None)
    if ok:
        lead = get_lead(username)
        if lead:
            bot.send_message(call.message.chat.id, lead_card(lead),
                             parse_mode="HTML", reply_markup=stage_kb(lead[0]))
    bot.answer_callback_query(call.id)


# ------------------------------------------------------- screenshot actions
@bot.callback_query_handler(func=lambda c: c.data.startswith("shot:"))
def on_shot_action(call):
    chat_id = call.message.chat.id
    action = call.data[len("shot:"):]

    if action == "cancel":
        _pending_shots.pop(chat_id, None)
        try:
            bot.edit_message_text("❌ Okay — discarded that screenshot.",
                                  chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    if action == "askid":
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "👤 Who is the client in this screenshot?\n"
            "Type their @username (example: <code>@john_doe</code>).",
            parse_mode="HTML",
        )
        return

    if not _pending_shots.get(chat_id):
        bot.answer_callback_query(
            call.id, "This screenshot expired — please send it again.",
            show_alert=True)
        return

    bot.answer_callback_query(call.id)
    status = None
    try:
        status = bot.edit_message_text(
            "🔍 <b>Reading screenshot…</b> running OCR",
            chat_id, call.message.message_id, parse_mode="HTML")
    except Exception:
        status = None
    threading.Thread(
        target=run_shot_action, args=(chat_id, action, status), daemon=True
    ).start()


# ------------------------------------------------------------ free-text AI
CHAT_SYSTEM_PROMPT = (
    "You are Gretta AI, a sharp, friendly enterprise-sales CRM assistant "
    "chatting inside Telegram with a sales teammate. Be concise, practical "
    "and human — short paragraphs, no markdown headings. You can reference "
    "their CRM knowledge below.\n\nTEAM KNOWLEDGE BASE:\n" + KNOWLEDGE
)


def ask_ai_chat(user_text, chat_id):
    """Free-form LLM chat with per-chat rolling memory."""
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    messages.extend(history_for(chat_id))
    messages.append({"role": "user", "content": user_text})
    payload = {"model": MODEL, "messages": messages}
    for attempt in (1, 2):
        try:
            resp = session.post(OPENROUTER_URL, json=payload, timeout=90)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data.get("error"), dict):
                    print(f"OpenRouter upstream error: {str(data['error'])[:200]}")
                else:
                    choices = data.get("choices")
                    if choices:
                        return choices[0]["message"]["content"]
            else:
                print(f"OpenRouter API error ({resp.status_code}): {resp.text[:200]}")
        except (requests.RequestException, ValueError) as exc:
            print(f"OpenRouter request error: {exc}")
        if attempt == 1:
            time.sleep(3)
    return None


@bot.message_handler(
    func=lambda m: m.content_type == "text" and not m.text.startswith("/"),
    content_types=["text"],
)
def handle_free_text(message):
    user_text = (message.text or "").strip()
    if not user_text:
        return

    # Waiting for the client @username for a pending screenshot?
    # Accept "@name" typed anywhere, or a bare name sent as a reply to the
    # triage prompt — so ordinary chat text is never mistaken for a username.
    shot = _pending_shots.get(message.chat.id)
    if shot and not shot.get("target"):
        replied_to_triage = bool(
            message.reply_to_message
            and shot.get("triage_id")
            and message.reply_to_message.message_id == shot["triage_id"]
        )
        m = re.fullmatch(r"@?[A-Za-z0-9_]{2,30}", user_text)
        if m and (user_text.startswith("@") or replied_to_triage):
            uname = normalize_username(m.group(0))
            shot["target"] = uname
            bot.send_message(
                message.chat.id,
                f"✅ Got it — client is <b>{esc(uname)}</b>. Now what should "
                f"I do with the screenshot?",
                parse_mode="HTML",
                reply_markup=triage_kb(True),
            )
            return

    # Waiting for a @username answer from the Check/Claim button flow?
    pending = _pending_prompts.pop(message.chat.id, None)
    if re.fullmatch(r"/[A-Za-z0-9_]+.*", user_text):
        _pending_prompts.pop(message.chat.id, None)  # command cancels prompt
    elif pending == "check":
        show_lead_record(message.chat.id, user_text)
        return
    elif pending == "claim":
        ok, text = run_claim(user_text, sender_handle(message))
        bot.send_message(message.chat.id, text, parse_mode="HTML",
                         reply_markup=home_button_kb() if ok else None)
        return

    # A bare @username typed alone behaves like /check (handy any time)
    if re.fullmatch(r"@[A-Za-z0-9_]{2,}", user_text):
        show_lead_record(message.chat.id, user_text)
        return

    remember(message.chat.id, "user", user_text)
    _typing_stop.clear()
    typing = threading.Thread(
        target=_keep_typing, args=(message.chat.id,), daemon=True
    )
    typing.start()
    try:
        reply = ask_ai_chat(user_text, message.chat.id)
    finally:
        _typing_stop.set()

    if not reply:
        reply = (
            "🤔 I couldn't reach my AI brain just now (the model provider may "
            "be busy). Try again in a moment!"
        )
    remember(message.chat.id, "assistant", reply)
    send_long(message.chat.id, esc(reply))


def _keep_typing(chat_id):
    """Refresh the 'typing…' indicator while the LLM thinks."""
    while not _typing_stop.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            break
        _typing_stop.wait(timeout=4.0)


@bot.message_handler(func=lambda m: True)
def fallback_everything_else(message):
    """Stickers, voice notes, video notes, anything unhandled."""
    bot.reply_to(
        message,
        "🤖 I work best with 📸 screenshots or plain text messages!\n\n"
        "Tap 🏠 Home to see everything I can do.",
        reply_markup=home_button_kb(),
    )


def _start_health_server():
    """Start a lightweight HTTP server on PORT (default 7860) for Hugging Face Spaces health checks."""
    port = int(os.getenv("PORT", "7860"))
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Gretta Bot is live!\n")

        def log_message(self, format, *args):
            pass  # Suppress health check access logs

    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        print(f"Health check server listening on 0.0.0.0:{port}")
        server.serve_forever()
    except Exception as exc:
        print(f"Health server skipped: {exc}")


if __name__ == "__main__":
    register_bot_commands()
    threading.Thread(target=_start_health_server, daemon=True).start()
    print("Gretta AI CRM Bot running (interactive build)...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as exc:
            print(f"Polling error: {exc}. Reconnecting in 5 seconds...")
            time.sleep(5)

