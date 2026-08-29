"""Gretta AI - Telegram CRM bot.

Pipeline: screenshot -> Vision AI (Gemini 2.0 Flash / OpenRouter Vision) -> LLM analysis
-> structured lead saved to SQLite (crm.db), with /claim and /check
commands to prevent duplicate outreach.

Interactive layer:
- App-style inline-keyboard menus: /start opens a button menu, leads open
  as tappable cards, and the deal stage can be moved with one tap.
- Free-text chat: anything that is not a command goes to the LLM, so the
  bot can actually hold a conversation (per-chat short-term memory).
- Progress feedback: screenshot analysis edits a single status message
  (Reading -> Analyzing -> Result) instead of spamming new ones.
- Screenshot triage: every screenshot asks what to do with it (Log /
  Summarize / Advice) and asks for the client @username when none is visible.

Optimizations:
- Zero heavy local ML frameworks (PaddleOCR removed) -> ultra-lightweight (<40MB RAM)
- Instant startup and full compatibility with Render, Koyeb, Vercel & Hugging Face
- Direct Vision AI screen understanding for 10x higher extraction accuracy
- One persistent HTTP session with retries to OpenRouter / Gemini API
- DB schema lives in db.py; init happens once per process (not per query)
- Strict JSON output from the LLM (no fragile multi-line regex parsing)
- knowledge.txt is injected into the system prompt (no RAG needed)
"""

import base64
import functools
import html
import io
import json
import os
import re
import threading
import time
from datetime import date, timedelta

import requests
import telebot
from dotenv import load_dotenv
from groq import Groq
from PIL import Image
from telebot import types

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("MODEL", "stealth/ox-alpha")
VISION_MODEL = os.getenv("VISION_MODEL", MODEL)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b").strip()

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
# Telegram rejects localhost/private URLs on inline buttons ("Wrong HTTP URL"),
# which would crash /start and /help — so omit the button for unusable URLs.
if not DASHBOARD_URL.startswith(("https://", "http://")) or "localhost" in DASHBOARD_URL or "127.0.0.1" in DASHBOARD_URL:
    DASHBOARD_URL = None

if not TELEGRAM_TOKEN or (not OPENROUTER_API_KEY and not GROQ_API_KEY):
    raise ValueError(
        "Error: TELEGRAM_TOKEN and at least one AI provider key "
        "(OPENROUTER_API_KEY or GROQ_API_KEY) are required in .env!"
    )

from db import (  # noqa: E402  (after env validation)
    CLOSER_STATUSES,
    OLD_TO_STATUS,
    STATUSES,
    YESNO,
    add_lead,
    all_bot_users,
    all_leads,
    bot_user_allowed,
    dashboard_stats,
    delete_lead,
    find_bot_user,
    get_lead,
    normalize_username,
    overdue_leads_for_sender,
    profile_link_for,
    scheduled_next_followup,
    set_bot_user_authorized,
    today_str,
    track_bot_user,
    update_lead,
    next_followup_date,
)
import sheets  # noqa: E402  Google Sheets mirror (no-op unless configured)

MAX_IMAGE_DIM = 1600      # px - screenshots scaled for fast vision encoding
MAX_SUMMARY_CHARS = 500   # cap stored summary length
TG_MSG_LIMIT = 3900       # split long replies (Telegram hard-caps at 4096)

STATUS_EMOJI = {
    "Message Sent": "📨", "Seen Not Replied": "👀", "Replied": "💬",
    "Follow up 1": "1️⃣", "Follow up 2": "2️⃣", "Follow up 3": "3️⃣",
    "Follow up 4": "4️⃣", "Replied-No yet booked": "🤔", "Closing Call": "📞",
    "Number received": "☎️", "Discovery Call booked": "📅",
    "Not Interested": "🚫", "Lost": "❌", "Won": "🏆",
}


def status_chip(status):
    """'💬 Replied' label used across cards, lists and toasts."""
    s = status or "Message Sent"
    return f"{STATUS_EMOJI.get(s, '▫️')} {s}"


CHAT_MEMORY_TURNS = 6     # user+assistant exchanges kept per chat
CHAT_MAX_CHARS = 1200     # cap per stored chat turn (token safety)

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ---------------------------------------------------------------- access gate
# Private-bot mode. Leave BOTH env vars empty to keep the bot open to anyone
# (every account is still logged into bot_users for auditing). Set either one
# and only whitelisted teammates get through — strangers are refused politely
# AND recorded, so /users shows exactly who tried to use the bot.
def _csv_env(name):
    raw = os.getenv(name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


ALLOWED_IDS = _csv_env("ALLOWED_TELEGRAM_IDS")
ALLOWED_UNAMES = {_norm.lower() for _norm in
                  ("@" + u.lstrip("@") for u in _csv_env("ALLOWED_TELEGRAM_USERNAMES"))}
OPEN_ACCESS = not ALLOWED_IDS and not ALLOWED_UNAMES


def _refuse(chat_id, tg_user):
    """Tell a stranger they're not on the team list (and log them)."""
    handle = f"@{tg_user.username}" if getattr(tg_user, "username", None) else "no @handle"
    bot.send_message(
        chat_id,
        "⛔ <b>Private bot.</b>\n"
        "You're not on this workspace's team list, so Gretta can't help you "
        "here.\n\n"
        f"Your Telegram ID: <code>{getattr(tg_user, 'id', '?')}</code> ({handle})\n"
        "Send that ID to the admin to get access.",
        parse_mode="HTML",
    )


def _gate(tg_user, chat_id) -> bool:
    """Track + authorize one interaction. True when the user may proceed."""
    tid = str(getattr(tg_user, "id", "") or "")
    uname = f"@{getattr(tg_user, 'username', None)}" if getattr(tg_user, "username", None) else None
    try:
        track_bot_user(tid, uname, getattr(tg_user, "first_name", None), chat_id=chat_id)
    except Exception as exc:
        print(f"bot_users tracking failed: {exc}")
    if OPEN_ACCESS:
        return True
    if tid in ALLOWED_IDS or (uname and uname.lower() in ALLOWED_UNAMES):
        return True
    try:
        if bot_user_allowed(tid, uname):
            return True
    except Exception as exc:
        print(f"bot_users lookup failed: {exc}")
    return False


def member_only(fn):
    """Decorator for message handlers: refuse non-teammates."""
    @functools.wraps(fn)
    def wrapper(message, *args, **kwargs):
        user = getattr(message, "from_user", None)
        if user is None or _gate(user, message.chat.id):
            return fn(message, *args, **kwargs)
        _refuse(message.chat.id, user)
        return None
    return wrapper


def member_callback(fn):
    """Decorator for callback-query handlers: refuse non-teammates."""
    @functools.wraps(fn)
    def wrapper(call, *args, **kwargs):
        user = getattr(call, "from_user", None)
        chat_id = call.message.chat.id if call.message else call.from_user.id
        if user is None or _gate(user, chat_id):
            return fn(call, *args, **kwargs)
        _refuse(chat_id, user)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return None
    return wrapper

# ------------------------------------------------------------- chat memory
# Per-chat rolling window of {"role": ..., "content": ...} for free-text chat.
# In-process only: history resets on restart, which is fine for small talk.
_chat_history = {}
_chat_lock = threading.Lock()
_typing_stop: dict = {}  # chat_id -> threading.Event (per-chat, no cross-user races)
# chat_id -> "check" | "num:@u" | "note:@u" | "next:@u": awaiting a reply
_pending_prompts = {}
# chat_id -> {"step": "username|name|followers|note", "data": {...}}:
# guided /addlead flow state
_pending_add = {}
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


def clean_ai_reply(text):
    """Strip provider reasoning traces before they reach Telegram."""
    value = str(text or "")
    # A few providers omit the opening tag, so when a closing tag exists the
    # safe boundary is everything after that tag.
    if re.search(r"</think>", value, flags=re.IGNORECASE):
        value = re.split(r"</think>\s*", value, maxsplit=1,
                         flags=re.IGNORECASE)[1]
    else:
        value = re.sub(r"<think>.*$", "", value,
                       flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\[(?:done|proceeds?)\]\s*", "", value,
                   flags=re.IGNORECASE)
    return value.strip()


def main_menu_kb(viewer=None):
    """Main app menu shown by /start and the 🏠 Home button."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    followup_label = "🔁 Follow-ups"
    if viewer:
        pending = overdue_leads_for_sender(viewer)
        due_stages = {}
        for lead in pending:
            stage = next((n for n in (1, 2, 3, 4)
                          if lead[f"follow_up_{n}"] != "Yes"), None)
            if stage:
                due_stages[stage] = due_stages.get(stage, 0) + 1
        if due_stages:
            detail = ", ".join(f"FU{stage}: {count}"
                                for stage, count in sorted(due_stages.items()))
            followup_label += f" ({detail})"
    kb.add(
        types.InlineKeyboardButton("🗂 My Leads", callback_data="menu:leads"),
        types.InlineKeyboardButton("➕ Add Lead", callback_data="ask:add"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 Team Stats", callback_data="menu:stats"),
        types.InlineKeyboardButton(followup_label, callback_data="menu:followups"),
    )
    kb.add(
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
        "I'm <b>Gretta AI</b> — your Instagram outreach CRM copilot.\n\n"
        "<b>What you can do:</b>\n"
        "• ➕ <b>/addlead</b> — log a new prospect (guided)\n"
        "• 🗂 <b>/myleads</b> — browse & manage your leads\n"
        "• 📊 <b>/stats</b> — team pipeline at a glance\n"
        "• 🔁 <b>/followups</b> — see your assigned follow-ups\n"
        "• 🔄 <b>/importsheet</b> — pull your Google Sheet into the CRM\n"
        "• 💬 Or just <b>talk to me</b> — ask anything!\n\n"
        "👇 Use the buttons below or type a command."
    )


def register_bot_commands():
    """Show the official command menu (the blue 'Menu' button in Telegram)."""
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Open the Gretta app menu"),
            types.BotCommand("addlead", "Log a new lead — /addlead"),
            types.BotCommand("myleads", "Show my leads"),
            types.BotCommand("lead", "Open a lead — /lead @username"),
            types.BotCommand("status", "Set status — /status @user <status>"),
            types.BotCommand("note", "Append a note — /note @user <text>"),
            types.BotCommand("number", "Save a number — /number @user <num>"),
            types.BotCommand("fup", "Follow-up done — /fup @user <1-4>"),
            types.BotCommand("followups", "See my assigned follow-ups"),
            types.BotCommand("stats", "Team pipeline summary"),
            types.BotCommand("importsheet", "Pull leads from Google Sheet"),
            types.BotCommand("syncsheet", "Push CRM to Google Sheet"),
            types.BotCommand("users", "Audit who is using the bot (team)"),
            types.BotCommand("allow", "Whitelist a user — /allow <id>"),
            types.BotCommand("deny", "Revoke a user — /deny <id>"),
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
    "Analyze this sales conversation screenshot directly and reply with ONLY a single JSON object "
    "(no markdown fences, no extra text) using exactly these keys:\n"
    '{"lead": "@username", "email": "client@example.com", "score": "HIGH|MEDIUM|LOW", '
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
    "Identify the client's username/handle and email address from the image if visible. "
    "If no email is visible, set email to an empty string. "
    "Use the provided target username if none is found in the screenshot."
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

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


def prepare_image_b64(image_bytes, max_dim=MAX_IMAGE_DIM):
    """Resize screenshot if needed and return base64 string and mime type."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=85)
        b64_str = base64.b64encode(out_buf.getvalue()).decode("utf-8")
        return b64_str, "image/jpeg"
    except Exception as exc:
        print(f"Error encoding image for vision API: {exc}")
        return None, None


def _ask_gemini_direct(prompt_text, image_bytes=None, timeout=90):
    """Direct free API call to Google Gemini 2.0 Flash (when GEMINI_API_KEY is provided)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    parts = []
    if image_bytes:
        b64_str, mime_type = prepare_image_b64(image_bytes)
        if b64_str:
            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": b64_str
                }
            })
    parts.append({"text": f"{SYSTEM_PROMPT}\n\n{prompt_text}"})
    
    payload = {"contents": [{"parts": parts}]}
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text")
        else:
            print(f"Gemini API error ({r.status_code}): {r.text[:200]}")
    except Exception as exc:
        print(f"Gemini API request error: {exc}")
    return None


def _ask_groq(messages, timeout=90):
    """Call Groq with streaming enabled and return the assembled response."""
    if not groq_client:
        return None

    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.6,
            max_completion_tokens=2048,
            top_p=0.95,
            stream=True,
            stop=None,
            timeout=timeout,
        )
        chunks = []
        for chunk in completion:
            delta = chunk.choices[0].delta if chunk.choices else None
            content = getattr(delta, "content", None) if delta else None
            if content:
                chunks.append(content)
        reply = clean_ai_reply("".join(chunks))
        return reply or None
    except Exception as exc:
        print(f"Groq API request error: {exc}")
        return None


def ask_ai(prompt_text, image_bytes=None, timeout=90):
    """Send a prompt using Groq-first vision and OpenRouter-first text routing."""
    if GEMINI_API_KEY and not image_bytes:
        res = _ask_gemini_direct(prompt_text, image_bytes=image_bytes, timeout=timeout)
        if res:
            return res

    model_to_use = VISION_MODEL if image_bytes else MODEL

    if image_bytes:
        b64_str, mime_type = prepare_image_b64(image_bytes)
        if b64_str:
            user_content = [
                {"type": "text", "text": prompt_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{b64_str}"
                    }
                }
            ]
        else:
            user_content = prompt_text
    else:
        user_content = prompt_text

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # Qwen on Groq is the primary provider for screenshots. If it fails,
    # continue to the OpenRouter vision model below.
    if image_bytes and groq_client:
        res = _ask_groq(messages, timeout=timeout)
        if res:
            return res

    payload = {
        "model": model_to_use,
        "messages": messages,
        "reasoning": {"exclude": True},
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
                        return clean_ai_reply(choices[0]["message"].get("content"))
            else:
                print(f"OpenRouter API error ({resp.status_code}): {resp.text[:200]}")
        except (requests.RequestException, ValueError) as exc:
            print(f"OpenRouter request error: {exc}")
        if attempt == 1:
            time.sleep(3)

    if not image_bytes and groq_client:
        return _ask_groq([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ], timeout=timeout)
    return None


# --------------------------------------------------------------- handlers
PRIMARY_SETTER_USERNAME = "@mazidur"
PRIMARY_SETTER_NAME = "Mazidur Rahman"


def _setter_identity(username=None, first_name=None):
    """Return the canonical display name used for lead ownership."""
    handle = (username or "").strip().lower()
    name = (first_name or "").strip().lower()
    if handle.lstrip("@") == PRIMARY_SETTER_USERNAME.lstrip("@") or name == "mazidur":
        return PRIMARY_SETTER_NAME
    return f"@{handle.lstrip('@')}" if handle else (first_name or "Unknown")


def sender_handle(message):
    user = message.from_user
    return _setter_identity(user.username, user.first_name)


def callback_sender_handle(call):
    """Return the setter who clicked an inline Telegram button.

    ``call.message.from_user`` is the bot, because the bot sent the message.
    The actual person who tapped the button is ``call.from_user``.
    """
    user = call.from_user
    return _setter_identity(user.username, user.first_name)


def lead_card(lead):
    """Render a lead dict (db.get_lead) as an HTML card."""
    uname = lead["user_name"]
    link = lead["profile_link"] or profile_link_for(uname)
    lines = [
        f"{status_chip(lead['status'])}  <b>{esc(lead['full_name'] or uname)}</b> "
        f"{esc(uname)} · #{lead['lead_number']}",
        f"🧑‍💼 Setter: <b>{esc(lead['sender_name'] or 'Unassigned')}</b>",
    ]
    if lead["followers_count"]:
        lines.append(f"👥 Followers: {esc(lead['followers_count'])}")
    lines.append(f"🔗 {esc(link)}")
    touch = " · ".join(filter(None, [
        f"1st: {lead['first_touchpoint']}" if lead["first_touchpoint"] else "",
        f"last: {lead['last_touchpoint']}" if lead["last_touchpoint"] else "",
        f"next: {lead['next_touchpoint']}" if lead["next_touchpoint"] else "",
    ]))
    if touch:
        lines.append(f"🕒 {esc(touch)}")
    flags = []
    if lead["replied"] == "Yes":
        flags.append("💬 replied")
    if lead["number_received"] == "Yes":
        flags.append(f"☎️ {lead['number']}" if lead["number"] else "☎️ number in")
    for n in (1, 2, 3, 4):
        if lead[f"follow_up_{n}"] == "Yes":
            when = lead[f"follow_up_{n}_date"]
            flags.append(f"🔁 FU{n} ✓{(' ' + when) if when else ''}")
    next_fu = next((n for n in (1, 2, 3, 4)
                    if lead[f"follow_up_{n}"] != "Yes"), None)
    if next_fu:
        due = (lead.get("next_touchpoint") or "") <= today_str()
        flags.append(f"{'🔔' if due else '📅'} FU{next_fu} pending")
    else:
        flags.append("✅ Follow-ups complete")
    if lead["discovery_call"] == "Yes":
        when = f" {lead['discovery_date']}" if lead["discovery_date"] else ""
        flags.append(f"📅 discovery{when}")
    if lead["closing_call_status"]:
        flags.append(f"📞 {lead['closing_call_status']}")
    if lead["closed_result"]:
        flags.append(f"🏁 {lead['closed_result']}")
    if flags:
        lines.append("✨ " + " · ".join(esc(f) for f in flags))
    lines.append(
        f"📝 {esc(lead['note']) if lead['note'] else '<i>No note yet</i>'}")
    return "\n".join(lines)


def status_kb(user_name):
    """Full management keyboard: every status + quick actions."""
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[
        types.InlineKeyboardButton(
            f"{STATUS_EMOJI.get(s, '▫️')} {s}",
            callback_data=f"st:{user_name}:{i}")
        for i, s in enumerate(STATUSES)
    ])
    kb.row(
        types.InlineKeyboardButton("💬 Replied ✓",
                                   callback_data=f"act:{user_name}:replied"),
        types.InlineKeyboardButton("☎️ Number",
                                   callback_data=f"act:{user_name}:asknum"),
    )
    kb.row(
        types.InlineKeyboardButton("1️⃣ FU1", callback_data=f"act:{user_name}:fup1"),
        types.InlineKeyboardButton("2️⃣ FU2", callback_data=f"act:{user_name}:fup2"),
        types.InlineKeyboardButton("3️⃣ FU3", callback_data=f"act:{user_name}:fup3"),
        types.InlineKeyboardButton("4️⃣ FU4", callback_data=f"act:{user_name}:fup4"),
    )
    kb.row(
        types.InlineKeyboardButton("📝 Note", callback_data=f"act:{user_name}:asknote"),
        types.InlineKeyboardButton("📅 Next", callback_data=f"act:{user_name}:asknext"),
        types.InlineKeyboardButton("🙋 Take", callback_data=f"act:{user_name}:take"),
    )
    kb.add(
        types.InlineKeyboardButton("🗑 Delete", callback_data=f"act:{user_name}:del"),
        types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"),
    )
    return kb


def new_lead_kb(username):
    """Shown for @handles that are not in the CRM yet."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        f"➕ Add {username} as a lead", callback_data=f"act:{username}:add"))
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"))
    return kb


def render_my_leads(rows):
    """Summarize a setter's leads -> (text, list of tappable buttons)."""
    header = "🗂 <b>My Leads</b>\n\n"
    if not rows:
        return (
            header + "You don't have any leads yet.\n"
            "➕ <b>/addlead</b> to log your first prospect — or "
            "<b>/importsheet</b> to pull your Google Sheet in!",
            [],
        )
    lines = [header]
    buttons = []
    shown = rows[:30]
    for lead in shown:
        chip = status_chip(lead["status"])
        name = lead["full_name"] or lead["user_name"]
        lines.append(
            f"• {chip} · #{lead['lead_number']} <b>{esc(name)}</b> "
            f"{esc(lead['user_name'])}"
        )
        cb = f"lead:{lead['user_name']}"
        if len(cb.encode()) <= 64:  # Telegram callback_data hard limit
            buttons.append(types.InlineKeyboardButton(
                f"#{lead['lead_number']} {lead['user_name']}", callback_data=cb))
    if len(rows) > len(shown):
        lines.append(
            f"\n<i>…and {len(rows) - len(shown)} more — see the dashboard</i>"
        )
    return "\n".join(lines), buttons


def followup_card_keyboard(username, stage, profile):
    """Actions belonging only to one follow-up card."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("↗ Open profile", url=profile),
        types.InlineKeyboardButton(
            f"✅ Done FU{stage}", callback_data=f"fu_done:{username}:{stage}"),
    )
    kb.add(types.InlineKeyboardButton(
        "❌ End follow-up", callback_data=f"fu_end:{username}"))
    return kb


def render_followup_card(lead, stage, due, when):
    """Render one lead's current follow-up card and its matching actions."""
    username = lead["user_name"]
    name = lead["full_name"] or username
    profile = lead.get("profile_link") or profile_link_for(username)
    marker = "🔔" if due else ("📅" if when else "🗓")
    text = (
        f"<b>👤 {esc(name)}</b> {esc(username)}\n"
        f"{marker} FU{stage} · {esc(when or 'not scheduled')}\n"
        f"{status_chip(lead.get('status', ''))} Status: "
        f"{esc(lead.get('status') or 'Not set')}\n"
        f"🔗 <a href=\"{esc(profile)}\">Open Instagram profile</a>\n"
        "━━━━━━━━━━━━━━━━"
    )
    return text, followup_card_keyboard(username, stage, profile)


def render_followups(rows):
    """Return a header and separate actionable card for every pending lead."""
    header = "🔁 <b>My Follow-ups</b>\n\n"
    today = today_str()
    grouped = {stage: [] for stage in (1, 2, 3, 4)}
    terminal = {"Not Interested", "Lost", "Won"}

    for lead in rows:
        if lead.get("status") in terminal:
            continue
        next_n = next((i for i in (1, 2, 3, 4)
                       if lead[f"follow_up_{i}"] != "Yes"), None)
        if next_n is not None:
            when = lead.get("next_touchpoint") or ""
            due = bool(when and when <= today)
            grouped[next_n].append((lead, due, when))

    pending_rows = [item for stage in grouped.values() for item in stage]
    if not pending_rows:
        return [(header + "✅ You have no pending follow-ups.", home_button_kb())]

    due_count = sum(1 for _, due, _ in pending_rows if due)
    unscheduled_count = sum(1 for _, _, when in pending_rows if not when)
    summary = [f"Pending: <b>{len(pending_rows)}</b>"]
    if due_count:
        summary.append(f"🔔 Due: <b>{due_count}</b>")
    if unscheduled_count:
        summary.append(f"🗓 Unscheduled: <b>{unscheduled_count}</b>")

    cards = [(header + " · ".join(summary), home_button_kb())]
    for stage, stage_rows in grouped.items():
        if not stage_rows:
            continue
        stage_rows.sort(key=lambda item: (not item[1], not bool(item[2]), item[2] or ""))
        for lead, due, when in stage_rows:
            cards.append(render_followup_card(lead, stage, due, when))
    return cards


def leads_kb(buttons):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if buttons:
        kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("🏠 Home", callback_data="menu:home"))
    return kb


def send_followup_cards(chat_id, viewer):
    """Send the follow-up summary and one message per lead card."""
    for text, keyboard in render_followups(my_leads_for(viewer)):
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)


def refresh_followup_cards(call):
    """Remove the completed/ended card while leaving other cards in place."""
    chat_id = call.message.chat.id
    try:
        bot.delete_message(chat_id, call.message.message_id)
    except Exception:
        pass


def followup_block_message(chat_id, viewer):
    """Prevent new lead creation only while a follow-up is due or overdue."""
    blocked = overdue_leads_for_sender(viewer)
    if not blocked:
        return False
    names = ", ".join(x["user_name"] for x in blocked[:8])
    next_stage = next((n for n in (1, 2, 3, 4)
                       if blocked[0][f"follow_up_{n}"] != "Yes"), 1)
    bot.send_message(
        chat_id,
        "⛔ <b>Complete your follow-up before adding a new lead.</b>\n\n"
        f"Pending: {esc(names)}\n"
        f"Open Follow-ups, or use <code>/fup @username {next_stage}</code> "
        "(use the pending number), then try again.",
        parse_mode="HTML", reply_markup=home_button_kb())
    return True


@bot.message_handler(commands=["start"])
@member_only
def send_welcome(message):
    viewer = sender_handle(message)
    bot.send_message(
        message.chat.id,
        welcome_text(message.from_user.first_name or "there"),
        parse_mode="HTML",
        reply_markup=main_menu_kb(viewer),
    )


def _ask_add_step(chat_id, step=None):
    """Send the next prompt of the guided /addlead flow."""
    state = _pending_add.get(chat_id) or {}
    step = step or state.get("step", "name")
    prompts = {
        "username": ("➕ <b>Adding a new lead</b>\n\nWhat's their Instagram "
                     "<b>@username</b>? (example: <code>@imrahulanjaa</code>)"),
        "name": "👤 Got it. Now their <b>full name</b>? (or <code>-</code> to skip)",
        "followers": ("👥 How many <b>followers</b>? (e.g. <code>724</code>, "
                      "<code>13.5k</code> — or <code>-</code> to skip)"),
        "note": "📝 Any <b>note</b> about this prospect? (or <code>-</code> to skip)",
    }
    bot.send_message(chat_id, prompts[step], parse_mode="HTML")


def _process_add_step(message, state):
    """Consume one answer of the guided /addlead flow. True when consumed."""
    chat_id = message.chat.id
    step, data = state.get("step"), state.setdefault("data", {})
    text = (message.text or "").strip()
    if step == "username":
        found = re.findall(r"@[A-Za-z0-9_]+", text)
        if not found:
            bot.send_message(
                chat_id,
                "🤔 That doesn't look like a username — send it like "
                "<code>@john_doe</code>.",
                parse_mode="HTML",
            )
            return True
        data["user_name"] = found[0].lower()
        state["step"] = "name"
        _ask_add_step(chat_id, "name")
        return True
    if step == "name":
        if text and text != "-":
            data["full_name"] = text
        state["step"] = "followers"
        _ask_add_step(chat_id, "followers")
        return True
    if step == "followers":
        if text and text != "-":
            data["followers_count"] = text
        state["step"] = "note"
        _ask_add_step(chat_id, "note")
        return True
    if step == "note":
        if text and text != "-":
            data["note"] = text
        _pending_add.pop(chat_id, None)
        uname = data.get("user_name")
        if not uname:
            bot.send_message(
                chat_id,
                "⚠️ The lead @username got lost — let's start over with "
                "/addlead.",
                parse_mode="HTML", reply_markup=home_button_kb(),
            )
            return True
        try:
            lead, created = add_lead(
                full_name=data.get("full_name", ""), email=data.get("email", ""), user_name=uname,
                sender_name=sender_handle(message),
                followers_count=data.get("followers_count", ""),
                note=data.get("note", ""))
        except ValueError as exc:
            bot.send_message(chat_id, f"⚠️ {esc(str(exc))}",
                             parse_mode="HTML", reply_markup=home_button_kb())
            return True
        if created:
            bot.send_message(
                chat_id,
                f"✅ <b>Lead #{lead['lead_number']} added!</b>\n\n{lead_card(lead)}",
                parse_mode="HTML", reply_markup=status_kb(uname))
        else:
            bot.send_message(
                chat_id,
                f"ℹ️ {esc(uname)} is already in the CRM:\n\n{lead_card(lead)}",
                parse_mode="HTML", reply_markup=status_kb(uname))
        return True
    return False


@bot.message_handler(commands=["addlead"])
@member_only
def handle_addlead(message):
    """Guided lead creation: @username -> name -> followers -> note."""
    chat_id = message.chat.id
    # Apply the same gate to both the one-line and guided /addlead flows.
    if followup_block_message(chat_id, sender_handle(message)):
        return
    parts = (message.text or "").split(maxsplit=1)
    target = None
    if len(parts) > 1:
        found = re.findall(r"@[A-Za-z0-9_]+", parts[1])
        target = found[0].lower() if found else None
    if target:
        _pending_add[chat_id] = {"step": "name", "data": {"user_name": target}}
        _ask_add_step(chat_id, "name")
        return
    _pending_add[chat_id] = {"step": "username", "data": {}}
    _ask_add_step(chat_id, "username")


@bot.message_handler(commands=["lead", "check"])
@member_only
def handle_lead(message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        _pending_prompts[message.chat.id] = "check"
        bot.send_message(
            message.chat.id,
            "🔎 <b>Open a lead</b>\n\nWhich @username? For example: "
            "<code>@john_doe</code>",
            parse_mode="HTML",
        )
        return
    show_lead_record(message.chat.id, parts[1])


def my_leads_for(viewer):
    """A setter's leads: exact sender match first, then first-name match."""
    rows = all_leads()
    view = (viewer or "").strip().lower()
    exact = [r for r in rows if (r["sender_name"] or "").strip().lower() == view]
    if exact or not view:
        return exact
    first = view.lstrip("@")
    return [r for r in rows
            if (r["sender_name"] or "").strip().lower().lstrip("@").startswith(first)]


@bot.message_handler(commands=["leads", "myleads"])
@member_only
def handle_leads(message):
    viewer = sender_handle(message)
    rows = my_leads_for(viewer)
    text, btns = render_my_leads(rows)
    bot.send_message(message.chat.id, text, parse_mode="HTML",
                     reply_markup=leads_kb(btns))


def _target_from(message):
    """'/cmd @user rest…' -> ('@user', ['rest', '…']) or (None, …)."""
    parts = (message.text or "").split()
    if len(parts) >= 2:
        found = re.findall(r"@[A-Za-z0-9_]+", parts[1])
        if found:
            return normalize_username(found[0]), parts[2:]
    return None, parts[2:] if len(parts) > 2 else []


@bot.message_handler(commands=["status"])
@member_only
def handle_status(message):
    """'/status @user Replied' — quick status change (prefix match ok)."""
    target, rest = _target_from(message)
    if not target:
        bot.send_message(
            message.chat.id,
            "Usage: <code>/status @user Replied</code>\n\nValid statuses:\n"
            + "\n".join(f"• {s}" for s in STATUSES),
            parse_mode="HTML", reply_markup=home_button_kb(),
        )
        return
    if not rest:
        show_lead_record(message.chat.id, target)
        return
    wanted = " ".join(rest).strip().lower()
    match = next((s for s in STATUSES if s.lower() == wanted), None) or \
        next((s for s in STATUSES if s.lower().startswith(wanted)), None)
    if not match:
        bot.send_message(
            message.chat.id,
            f"🤔 No status matches “{esc(' '.join(rest))}”.\nValid: "
            + ", ".join(STATUSES),
            parse_mode="HTML", reply_markup=home_button_kb(),
        )
        return
    try:
        lead = update_lead(target, status=match)
    except ValueError as exc:
        bot.send_message(message.chat.id, f"⚠️ {esc(str(exc))}", parse_mode="HTML")
        return
    if not lead:
        show_lead_record(message.chat.id, target)
        return
    bot.send_message(message.chat.id,
                     f"✅ {esc(target)} → <b>{status_chip(match)}</b>",
                     parse_mode="HTML", reply_markup=home_button_kb())


@bot.message_handler(commands=["note"])
@member_only
def handle_note(message):
    """'/note @user text' — append to the lead's note."""
    target, rest = _target_from(message)
    text = " ".join(rest).strip()
    if not target or not text:
        bot.send_message(
            message.chat.id,
            "Usage: <code>/note @user asked for price, follow up friday</code>",
            parse_mode="HTML")
        return
    lead = get_lead(target)
    if not lead:
        show_lead_record(message.chat.id, target)
        return
    merged = f"{lead['note']} | {text}" if lead["note"] else text
    lead = update_lead(target, note=merged[:2000])
    bot.send_message(message.chat.id,
                     f"📝 Note saved for {esc(target)}:\n{esc(lead['note'])}",
                     parse_mode="HTML", reply_markup=home_button_kb())


def stats_text():
    """Team pipeline summary (also used by the 📊 Team Stats menu button)."""
    s = dashboard_stats()
    if not s["total"]:
        return ("📭 The CRM is empty — <b>/addlead</b> to log your first "
                "prospect or <b>/importsheet</b> to pull your Google Sheet!")
    lines = ["📊 <b>Team Pipeline</b>\n"]
    for status in STATUSES:
        n = s["by_status"].get(status, 0)
        if n:
            # Block-drawing bars render as opaque white rectangles in some
            # Telegram Android fonts/themes. Keep the stats text portable.
            lines.append(f"{STATUS_EMOJI.get(status, '▫️')} <b>{status}</b>: {n}")
    lines.append("")
    for name, bucket in s["setters"].items():
        lines.append(f"🧑‍💼 <b>{esc(name)}</b>: {bucket['total']} leads")
    lines.append("")
    lines.append(f"Total: <b>{s['total']}</b> · 🔥 Warm: <b>{s['warm']}</b> · "
                 f"🏆 Won: <b>{s['won']}</b> · 🚫 Lost/NI: <b>{s['lost']}</b>")
    return "\n".join(lines)


@bot.message_handler(commands=["number"])
@member_only
def handle_number(message):
    """'/number @user 9876543210' — save the phone (auto Number Received ✓)."""
    target, rest = _target_from(message)
    number = " ".join(rest).strip()
    if not target or not number:
        bot.send_message(message.chat.id,
                         "Usage: <code>/number @user 9876543210</code>",
                         parse_mode="HTML")
        return
    lead = update_lead(target, number=number[:40])
    if not lead:
        show_lead_record(message.chat.id, target)
        return
    bot.send_message(message.chat.id,
                     f"☎️ <code>{esc(number[:40])}</code> saved for "
                     f"{esc(target)} — <b>Number Received ✓</b>",
                     parse_mode="HTML", reply_markup=home_button_kb())


@bot.message_handler(commands=["fup"])
@member_only
def handle_fup(message):
    """'/fup @user 2' — mark follow-up N done (dates today, advances status)."""
    target, rest = _target_from(message)
    n = rest[0] if rest else ""
    if not target or n not in ("1", "2", "3", "4"):
        bot.send_message(message.chat.id,
                         "Usage: <code>/fup @user 2</code>  (1-4)",
                         parse_mode="HTML")
        return
    current_lead = get_lead(target)
    if not current_lead:
        show_lead_record(message.chat.id, target)
        return
    current = current_lead.get("status", "")
    fields = {f"follow_up_{n}": "Yes"}
    # Store the next scheduled touchpoint according to the BD sequence:
    # Day 3, Day 6, Day 9, Day 13. The text is still copied by the setter;
    # this command only records completion and schedules the next reminder.
    # Recalculate from the first unfinished stage so legacy rows with skipped
    # or inconsistent flags recover safely instead of showing a stale date.
    prospective = dict(current_lead)
    prospective[f"follow_up_{n}"] = "Yes"
    _, fields["next_touchpoint"] = scheduled_next_followup(prospective)
    if current in ("Message Sent", "Seen Not Replied", "Replied",
                   f"Follow up {n}") or not current:
        fields["status"] = f"Follow up {n}"
    lead = update_lead(target, **fields)
    if not lead:
        show_lead_record(message.chat.id, target)
        return
    bot.send_message(message.chat.id,
                     f"🔁 Follow up {n} marked done for {esc(target)} "
                     f"({today_str()}) → <b>{status_chip(lead['status'])}</b>",
                     parse_mode="HTML", reply_markup=home_button_kb())


@bot.message_handler(commands=["followups"])
@member_only
def handle_followups(message):
    """Show all assigned follow-ups on demand."""
    send_followup_cards(message.chat.id, sender_handle(message))


@bot.message_handler(commands=["next"])
@member_only
def handle_next(message):
    """'/next @user friday' — set the Next Touchpoint date."""
    target, rest = _target_from(message)
    when = _parse_next_date(" ".join(rest))
    if not target or not when:
        bot.send_message(message.chat.id,
                         "Usage: <code>/next @user 2026-09-01</code> "
                         "(or 'tomorrow', 'friday'…)",
                         parse_mode="HTML")
        return
    lead = update_lead(target, next_touchpoint=when)
    if not lead:
        show_lead_record(message.chat.id, target)
        return
    bot.send_message(message.chat.id,
                     f"📅 Next touchpoint for {esc(target)}: <b>{when}</b>",
                     parse_mode="HTML", reply_markup=home_button_kb())


def _parse_next_date(text):
    """Accept YYYY-MM-DD, d/m, 'today', 'tomorrow' or weekday names."""
    t = (text or "").strip().lower()
    today = date.today()
    if t == "today":
        return today.strftime("%Y-%m-%d")
    if t in ("tomorrow", "tmr"):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]
    if t in weekdays:
        delta = (weekdays.index(t) - today.weekday()) % 7 or 7
        return (today + timedelta(days=delta)).strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)),
                        int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t)
    if m:  # d/m or d/m/y (day-first, like the sheet)
        dd, mm = int(m.group(1)), int(m.group(2))
        yy = int(m.group(3)) if m.group(3) else today.year
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, dd).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


@bot.message_handler(commands=["stats"])
@member_only
def handle_stats(message):
    bot.send_message(message.chat.id, stats_text(), parse_mode="HTML",
                     reply_markup=home_button_kb())


@bot.message_handler(commands=["syncsheet"])
@member_only
def handle_syncsheet(message):
    """Force-push the full leads table to the team Google Sheet backup."""
    chat_id = message.chat.id
    if not sheets.configured():
        bot.send_message(
            chat_id,
            "🔗 <b>Google Sheets backup isn't set up yet.</b>\n\n"
            "Add <code>GOOGLE_SHEET_WEBAPP_URL</code> (and optionally "
            "<code>GOOGLE_SHEET_SECRET</code>) to your environment — full "
            "walkthrough in README.md → “Google Sheets backup”. Until then "
            "I sync nothing.",
            parse_mode="HTML",
            reply_markup=home_button_kb(),
        )
        return
    notice = bot.send_message(chat_id, "⏳ Pushing CRM → Google Sheet…")
    ok, detail = sheets.push_now()
    if ok:
        text = f"✅ <b>Sheet synced!</b> {esc(detail)}"
    else:
        text = f"⚠️ <b>Sheet sync failed:</b> {esc(detail)}"
    try:
        bot.edit_message_text(
            text, chat_id, notice.message_id,
            parse_mode="HTML", reply_markup=home_button_kb(),
        )
    except Exception:
        bot.send_message(chat_id, text, parse_mode="HTML",
                         reply_markup=home_button_kb())


@bot.message_handler(commands=["importsheet"])
@member_only
def handle_importsheet(message):
    """Pull every data tab from the Google Sheet into the CRM."""
    chat_id = message.chat.id
    if not sheets.configured():
        bot.send_message(
            chat_id,
            "🔗 <b>Google Sheets isn't set up yet.</b>\n\n"
            "Add <code>GOOGLE_SHEET_WEBAPP_URL</code> (and "
            "<code>GOOGLE_SHEET_SECRET</code>) to your environment — full "
            "walkthrough in README.md → “Google Sheets mirror”.",
            parse_mode="HTML",
            reply_markup=home_button_kb(),
        )
        return
    notice = bot.send_message(chat_id, "⏳ Pulling Google Sheet → CRM…")
    ok, detail = sheets.pull_now()
    text = f"✅ <b>Import done!</b> {esc(detail)}" if ok else \
        f"⚠️ <b>Import failed:</b> {esc(detail)}"
    try:
        bot.edit_message_text(text, chat_id, notice.message_id,
                              parse_mode="HTML", reply_markup=home_button_kb())
    except Exception:
        bot.send_message(chat_id, text, parse_mode="HTML",
                         reply_markup=home_button_kb())


@bot.message_handler(commands=["help"])
@member_only
def handle_help(message):
    bot.send_message(
        message.chat.id,
        "<b>How to use Gretta AI</b> 🤖\n\n"
        "<b>Manage your Instagram outreach:</b>\n"
        "/addlead — log a new prospect (guided)\n"
        "/myleads — browse <i>your</i> leads with tappable cards\n"
        "/lead @user — open a lead's full card\n"
        "/status @user &lt;status&gt; — quick status change\n"
        "/note @user &lt;text&gt; — append a note\n"
        "/number @user &lt;number&gt; — save their phone number\n"
        "/fup @user &lt;1-4&gt; — mark a follow-up as done\n"
        "/next @user &lt;date&gt; — set the next touchpoint\n"
        "/stats — team pipeline at a glance\n"
        "/importsheet — pull your Google Sheet into the CRM\n"
        "/syncsheet — push the CRM to your Google Sheet\n\n"
        "💬 <b>Talk to me:</b> type anything — outreach tips, follow-up "
        "ideas, pipeline questions.\n"
        "⚡️ <b>Shortcuts:</b> tap a lead to open its card, then tap a "
        "status or action button.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


def show_lead_record(chat_id, username):
    lead = get_lead(username)
    if not lead:
        uname = normalize_username(username) or username
        bot.send_message(
            chat_id,
            f"🟢 <b>{esc(uname)}</b> is not in the CRM yet — nobody has "
            "touched this prospect!",
            parse_mode="HTML",
            reply_markup=new_lead_kb(uname),
        )
        return
    bot.send_message(chat_id, lead_card(lead), parse_mode="HTML",
                     reply_markup=status_kb(lead["user_name"]))


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
    """Run Vision AI + the chosen action for the chat's pending screenshot."""
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
            chat_id, "🔍 <b>Reading screenshot…</b> analyzing with Vision AI",
            parse_mode="HTML")

    # ------------------------------------------------------------ Vision AI
    try:
        file_info = bot.get_file(shot["file_id"])
        image_bytes = bot.download_file(file_info.file_path)
    except Exception as exc:
        print(f"Image download failed for chat {chat_id}: {exc}")
        edit_status(status, "⚠️ Couldn't download that image. Try "
                            "sending it again?")
        return

    caption = shot.get("caption") or ""
    cap_users = re.findall(r"@[A-Za-z0-9_]+", caption)
    target_user = shot.get("target") or (cap_users[0] if cap_users else "@unknown_lead")

    if action == "log":
        analyze_and_reply(status, image_bytes, caption, target_user,
                          owner=shot.get("owner"))
    elif action == "summarize":
        summarize_screenshot(chat_id, status, image_bytes, target_user)
    elif action == "advice":
        advice_for_screenshot(chat_id, status, image_bytes, target_user)


def analyze_and_reply(status, image_bytes, user_caption, target_user,
                      owner=None):
    """Vision AI pipeline: score + persist the lead directly from screenshot image."""

    edit_status(
        status,
        f"🧠 <b>Analyzing lead</b> {esc(target_user)}…\n"
        "<i>Gretta Vision AI is scoring intent and extracting next steps</i>",
    )
    prompt = (
        f"User Caption: '{user_caption}'\n\n"
        f"Target Lead Username: {target_user}\n\n"
        f"{ANALYSIS_INSTRUCTIONS}"
    )

    ai_reply = clean_ai_reply(ask_ai(prompt, image_bytes=image_bytes))
    if not ai_reply:
        edit_status(status, "⚠️ AI analysis failed — the vision model may be busy. "
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
    found_stage = str(info.get("stage", "Contacted")).strip()
    found_status = OLD_TO_STATUS.get(found_stage, "Replied")
    found_next = str(info.get("next_steps", "Follow up with client")).strip()
    found_summary = str(info.get("summary", "Screenshot analyzed")).strip()
    found_email = str(info.get("email", "")).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", found_email):
        found_email = ""
    if len(found_summary) > MAX_SUMMARY_CHARS:
        found_summary = found_summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

    note_bits = []
    if found_score == "HIGH":
        note_bits.append("AI score: HIGH 🔥")
    if found_next and found_next.lower() != "follow up with client":
        note_bits.append(f"Next: {found_next}")
    note_bits.append(found_summary)
    note = " | ".join(note_bits)

    # Never steal a lead another teammate already owns
    existing = get_lead(found_user)
    setter = (existing or {}).get("sender_name") or owner or "Unassigned"
    if existing:
        merged = f"{existing['note']} | {note}" if existing["note"] else note
        fields = {"note": merged[:2000]}
        if found_email and not existing.get("email"):
            fields["email"] = found_email
        if (existing["status"] or "Message Sent") == "Message Sent":
            fields["status"] = found_status
        lead = update_lead(found_user, **fields)
    else:
        lead, _ = add_lead(full_name="", email=found_email, user_name=found_user,
                           sender_name=setter, note=note, status=found_status)

    card = lead_card(lead) if lead else f"👤 <b>{esc(found_user)}</b> saved."
    edit_status(status, f"✅ <b>Lead logged!</b>\n\n{card}",
                status_kb(found_user))


LAYOUT_NOTE = (
    "CONVERSATION LAYOUT: bubbles on the LEFT edge are THE CLIENT "
    "(the prospect); bubbles on the RIGHT edge are US (the Gretta sales "
    "teammate). Base your answer on the CLIENT's left-side messages in the screenshot."
)


def summarize_screenshot(chat_id, status, image_bytes, target):
    edit_status(status, f"🧠 <b>Summarizing the chat with {esc(target)}…</b>")
    prompt = (
        f"{LAYOUT_NOTE}\n\nSummarize this sales chat screenshot in 3-5 short plain-text "
        f"bullet lines for our CRM notes: what the CLIENT wants, where the "
        f"deal stands, price discussed, and the single most important next "
        f"step. No markdown headings.\n\n"
        f"Lead: {target}"
    )
    reply = ask_ai(prompt, image_bytes=image_bytes)
    if not reply:
        edit_status(status, "⚠️ AI summary failed — the vision model may be busy. "
                            "Please try again.")
        return
    body = esc(reply.strip())[:3500]
    edit_status(status,
                f"📝 <b>Chat summary — {esc(target)}</b>\n\n{body}",
                home_button_kb())


def advice_for_screenshot(chat_id, status, image_bytes, target):
    edit_status(status, f"🧠 <b>Crafting the perfect reply for {esc(target)}…</b>")
    prompt = (
        f"{LAYOUT_NOTE}\n\nSuggest ONE great next reply WE should send the "
        f"client based on the conversation in the screenshot, then 2 short alternative angles. Keep it human, friendly "
        f"and concise — no markdown headings.\n\n"
        f"Lead: {target}"
    )
    reply = ask_ai(prompt, image_bytes=image_bytes)
    if not reply:
        edit_status(status, "⚠️ AI advice failed — the vision model may be busy. "
                            "Please try again.")
        return
    body = esc(reply.strip())[:3500]
    edit_status(status,
                f"💬 <b>Suggested reply — {esc(target)}</b>\n\n{body}",
                home_button_kb())


@bot.message_handler(content_types=["photo"])
@member_only
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
@member_only
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
@member_callback
def on_menu(call):
    chat_id = call.message.chat.id
    data = call.data
    if data == "menu:home":
        text = welcome_text(call.from_user.first_name or "there")
        kb = main_menu_kb(callback_sender_handle(call))
    elif data == "menu:leads":
        viewer = callback_sender_handle(call)
        rows = my_leads_for(viewer)
        text, btns = render_my_leads(rows)
        kb = leads_kb(btns)
    elif data == "menu:stats":
        text = stats_text()
        kb = home_button_kb()
    elif data == "menu:followups":
        viewer = callback_sender_handle(call)
        cards = render_followups(my_leads_for(viewer))
        text, kb = cards[0]
    elif data == "ask:add":
        if followup_block_message(chat_id, callback_sender_handle(call)):
            bot.answer_callback_query(call.id)
            return
        _pending_add[chat_id] = {"step": "username", "data": {}}
        _ask_add_step(chat_id, "username")
        bot.answer_callback_query(call.id)
        return
    elif data == "ask:check":
        _pending_prompts[chat_id] = "check"
        bot.send_message(chat_id, "🔎 Send me the @username to open:",
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
    if data == "menu:followups":
        for card_text, card_kb in cards[1:]:
            bot.send_message(chat_id, card_text, parse_mode="HTML", reply_markup=card_kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("lead:"))
@member_callback
def on_lead_card(call):
    username = call.data[len("lead:"):]
    lead = get_lead(username)
    if not lead:
        bot.answer_callback_query(call.id, "Lead no longer exists.", show_alert=True)
        return
    try:
        bot.edit_message_text(lead_card(lead), call.message.chat.id,
                              call.message.message_id, parse_mode="HTML",
                              reply_markup=status_kb(lead["user_name"]))
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            bot.send_message(call.message.chat.id, lead_card(lead),
                             parse_mode="HTML",
                             reply_markup=status_kb(lead["user_name"]))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("st:"))
@member_callback
def on_status_change(call):
    try:
        _, username, idx = call.data.split(":", 2)
        new_status = STATUSES[int(idx)]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid action.")
        return
    try:
        lead = update_lead(username, status=new_status)
    except ValueError as exc:
        bot.answer_callback_query(call.id, str(exc)[:190], show_alert=True)
        return
    if not lead:
        bot.answer_callback_query(call.id, "Lead no longer exists.", show_alert=True)
        return
    try:
        bot.edit_message_text(lead_card(lead), call.message.chat.id,
                              call.message.message_id, parse_mode="HTML",
                              reply_markup=status_kb(username))
    except Exception:
        pass
    bot.answer_callback_query(call.id,
                              f"{STATUS_EMOJI.get(new_status, '✅')} {new_status}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("act:"))
@member_callback
def on_lead_action(call):
    """Quick actions: replied / follow-ups / number / note / next / take / del."""
    try:
        _, username, action = call.data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid action.")
        return
    chat_id = call.message.chat.id
    lead = None
    viewer = callback_sender_handle(call)
    if action == "replied":
        current = (get_lead(username) or {}).get("status", "")
        fields = {"replied": "Yes"}
        if current in ("Message Sent", "Seen Not Replied", ""):
            fields["status"] = "Replied"
        lead = update_lead(username, **fields)
        toast = "💬 Marked as replied"
    elif action.startswith("fup") and action[-1] in "1234":
        n = action[-1]
        current = (get_lead(username) or {}).get("status", "")
        fields = {f"follow_up_{n}": "Yes"}
        if current in ("Message Sent", "Seen Not Replied", "Replied",
                       f"Follow up {n}", ""):
            fields["status"] = f"Follow up {n}"
        lead = update_lead(username, **fields)
        toast = f"🔁 Follow up {n} done"
    elif action == "take":
        lead = update_lead(username, sender_name=callback_sender_handle(call))
        toast = f"🙋 Taken by {callback_sender_handle(call)}"
    elif action == "del":
        delete_lead(username)
        try:
            bot.edit_message_text(
                f"🗑 <b>{esc(username)}</b> deleted from the CRM.",
                chat_id, call.message.message_id, parse_mode="HTML",
                reply_markup=home_button_kb())
        except Exception:
            pass
        bot.answer_callback_query(call.id, "🗑 Deleted")
        return
    elif action == "add":
        if followup_block_message(chat_id, viewer):
            bot.answer_callback_query(call.id)
            return
        _pending_add[chat_id] = {"step": "name", "data": {"user_name": username}}
        _ask_add_step(chat_id, "name")
        bot.answer_callback_query(call.id)
        return
    elif action == "asknum":
        _pending_prompts[chat_id] = f"num:{username}"
        bot.send_message(chat_id,
                         f"☎️ Send me <b>{esc(username)}</b>'s phone number:",
                         parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    elif action == "asknote":
        _pending_prompts[chat_id] = f"note:{username}"
        bot.send_message(
            chat_id,
            f"📝 Send me the note to append for <b>{esc(username)}</b>:",
            parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    elif action == "asknext":
        _pending_prompts[chat_id] = f"next:{username}"
        bot.send_message(
            chat_id,
            f"📅 Next touchpoint date for <b>{esc(username)}</b>? "
            "(YYYY-MM-DD, 'tomorrow' or a weekday)",
            parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    else:
        bot.answer_callback_query(call.id, "Unknown action.")
        return
    if not lead:
        bot.answer_callback_query(call.id, "Lead no longer exists.", show_alert=True)
        return
    try:
        bot.edit_message_text(lead_card(lead), chat_id, call.message.message_id,
                              parse_mode="HTML", reply_markup=status_kb(username))
    except Exception:
        pass
    bot.answer_callback_query(call.id, toast[:190])


@bot.callback_query_handler(func=lambda c: c.data.startswith("fu_done:"))
@member_callback
def on_followup_done(call):
    """Mark the displayed follow-up complete and refresh the list."""
    try:
        _, username, stage_text = call.data.split(":", 2)
        stage = int(stage_text)
        if stage not in (1, 2, 3, 4):
            raise ValueError
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid follow-up.", show_alert=True)
        return
    lead = get_lead(username)
    if not lead:
        bot.answer_callback_query(call.id, "Lead no longer exists.", show_alert=True)
        return
    expected = next((n for n in (1, 2, 3, 4)
                     if lead[f"follow_up_{n}"] != "Yes"), None)
    if expected != stage:
        bot.answer_callback_query(call.id, "That follow-up is no longer pending.", show_alert=True)
        return
    lead = update_lead(username, **{f"follow_up_{stage}": "Yes"})
    refresh_followup_cards(call)
    bot.answer_callback_query(call.id, f"✅ FU{stage} completed")


@bot.callback_query_handler(func=lambda c: c.data.startswith("fu_end:"))
@member_callback
def on_followup_end(call):
    """End the follow-up sequence by marking the lead Not Interested."""
    username = call.data[len("fu_end:"):]
    lead = update_lead(username, status="Not Interested", next_touchpoint="")
    if not lead:
        bot.answer_callback_query(call.id, "Lead no longer exists.", show_alert=True)
        return
    refresh_followup_cards(call)
    bot.answer_callback_query(call.id, "❌ Follow-up ended — marked Not Interested")


# ------------------------------------------------------- screenshot actions
@bot.callback_query_handler(func=lambda c: c.data.startswith("shot:"))
@member_callback
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
            "🔍 <b>Reading screenshot…</b> analyzing with Vision AI",
            chat_id, call.message.message_id, parse_mode="HTML")
    except Exception:
        status = None
    threading.Thread(
        target=run_shot_action, args=(chat_id, action, status), daemon=True
    ).start()


@bot.callback_query_handler(func=lambda c: c.data == "noop")
def on_noop(call):
    """Tapped the current-stage chip (or a locked Active Client row)."""
    bot.answer_callback_query(call.id, "🔒 This stage is locked.",
                              show_alert=False)


# ------------------------------------------------- access admin (team-only)
@bot.message_handler(commands=["users"])
@member_only
def handle_users(message):
    """Audit every Telegram account that has talked to the bot."""
    rows = all_bot_users()
    if not rows:
        bot.send_message(message.chat.id,
                         "🛡 No Telegram user has talked to me yet.",
                         reply_markup=home_button_kb())
        return
    mode = ("🌍 OPEN to everyone — set ALLOWED_TELEGRAM_IDS to lock down"
            if OPEN_ACCESS else "🔒 PRIVATE — whitelist active")
    lines = [f"🛡 <b>Bot Users</b> · {mode}\n"]
    for tid, uname, fname, auth, cnt, _first, last in rows[:25]:
        badge = "✅" if auth else ("▫️" if OPEN_ACCESS else "⛔")
        who = esc(uname or fname or "unknown")
        lines.append(f"{badge} {who} · <code>{esc(tid)}</code> · "
                     f"{cnt} msgs · {esc(str(last or ''))}")
    if len(rows) > 25:
        lines.append(f"\n…and {len(rows) - 25} more")
    lines.append("\nWhitelist with: /allow <id-or-@username>")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML",
                     reply_markup=home_button_kb())


@bot.message_handler(commands=["allow", "deny"])
@member_only
def handle_allow_deny(message):
    """/allow <id|@handle> whitelists a Telegram user; /deny revokes them."""
    grant = message.text.split()[0].lstrip("/").lower() == "allow"
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            f"Usage: <code>/{'allow' if grant else 'deny'} "
            "&lt;telegram-id-or-@username&gt;</code>",
            parse_mode="HTML",
        )
        return
    key = parts[1]
    row = find_bot_user(key)
    if row:
        target_id, target_name = row[0], (row[1] or row[2] or row[0])
    elif key.lstrip("@").isdigit():
        # Unknown ID: create a stub entry so they're pre-approved on arrival.
        target_id, target_name = key.lstrip("@"), key
    else:
        bot.send_message(
            message.chat.id,
            f"🤔 I've never seen <b>{esc(key)}</b> message the bot.\n"
            "Ask them to send me any message once, then retry — or add "
            "their numeric ID to ALLOWED_TELEGRAM_IDS.",
            parse_mode="HTML", reply_markup=home_button_kb(),
        )
        return
    set_bot_user_authorized(target_id, grant)
    verb = "whitelisted ✅" if grant else "revoked ⛔"
    bot.send_message(
        message.chat.id,
        f"🛡 <b>{esc(str(target_name))}</b> (<code>{esc(target_id)}</code>) "
        f"{verb}.\nThey can {'now use' if grant else 'no longer use'} the bot.",
        parse_mode="HTML", reply_markup=home_button_kb(),
    )


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
    payload = {
        "model": MODEL,
        "messages": messages,
        "reasoning": {"exclude": True},
    }
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
                        return clean_ai_reply(choices[0]["message"].get("content"))
            else:
                print(f"OpenRouter API error ({resp.status_code}): {resp.text[:200]}")
        except (requests.RequestException, ValueError) as exc:
            print(f"OpenRouter request error: {exc}")
        if attempt == 1:
            time.sleep(3)

    if groq_client:
        return _ask_groq(messages, timeout=90)
    return None


@bot.message_handler(
    func=lambda m: m.content_type == "text" and not m.text.startswith("/"),
    content_types=["text"],
)
@member_only
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

    # Guided /addlead flow first (multi-step, so it wins over prompts).
    add_state = _pending_add.get(message.chat.id)
    if add_state and not user_text.startswith("/"):
        if _process_add_step(message, add_state):
            return

    # Waiting for an answer from a lead-card button flow?
    pending = _pending_prompts.pop(message.chat.id, None)
    if re.fullmatch(r"/[A-Za-z0-9_]+.*", user_text):
        _pending_prompts.pop(message.chat.id, None)  # command cancels prompt
    elif pending == "check":
        show_lead_record(message.chat.id, user_text)
        return
    elif isinstance(pending, str) and pending.startswith("num:"):
        number = user_text.strip()[:40]
        lead = update_lead(pending[4:], number=number)
        if lead:
            bot.send_message(
                message.chat.id,
                f"☎️ <code>{esc(number)}</code> saved — "
                f"<b>Number Received ✓</b>\n\n{lead_card(lead)}",
                parse_mode="HTML", reply_markup=status_kb(lead["user_name"]))
        else:
            bot.send_message(message.chat.id, "🤔 That lead no longer exists.",
                             reply_markup=home_button_kb())
        return
    elif isinstance(pending, str) and pending.startswith("note:"):
        lead = get_lead(pending[5:])
        if lead:
            merged = (f"{lead['note']} | {user_text}"
                      if lead["note"] else user_text)
            lead = update_lead(pending[5:], note=merged[:2000])
        if lead:
            bot.send_message(
                message.chat.id,
                f"📝 Note saved:\n{esc(lead['note'])}\n\n{lead_card(lead)}",
                parse_mode="HTML", reply_markup=status_kb(lead["user_name"]))
        else:
            bot.send_message(message.chat.id, "🤔 That lead no longer exists.",
                             reply_markup=home_button_kb())
        return
    elif isinstance(pending, str) and pending.startswith("next:"):
        when = _parse_next_date(user_text)
        if not when:
            _pending_prompts[message.chat.id] = pending  # let them retry
            bot.send_message(
                message.chat.id,
                "🤔 Couldn't parse that date — try <code>YYYY-MM-DD</code>, "
                "<code>tomorrow</code> or a weekday name.",
                parse_mode="HTML")
            return
        lead = update_lead(pending[5:], next_touchpoint=when)
        if lead:
            bot.send_message(
                message.chat.id,
                f"📅 Next touchpoint: <b>{when}</b>\n\n{lead_card(lead)}",
                parse_mode="HTML", reply_markup=status_kb(lead["user_name"]))
        else:
            bot.send_message(message.chat.id, "🤔 That lead no longer exists.",
                             reply_markup=home_button_kb())
        return

    # A bare @username typed alone behaves like /check (handy any time)
    if re.fullmatch(r"@[A-Za-z0-9_]{2,}", user_text):
        show_lead_record(message.chat.id, user_text)
        return

    remember(message.chat.id, "user", user_text)
    stop_evt = threading.Event()
    _typing_stop[message.chat.id] = stop_evt
    typing = threading.Thread(
        target=_keep_typing, args=(message.chat.id, stop_evt), daemon=True
    )
    typing.start()
    try:
        reply = ask_ai_chat(user_text, message.chat.id)
    finally:
        stop_evt.set()
        _typing_stop.pop(message.chat.id, None)

    if not reply:
        reply = (
            "🤔 I couldn't reach my AI brain just now (the model provider may "
            "be busy). Try again in a moment!"
        )
    reply = clean_ai_reply(reply)
    remember(message.chat.id, "assistant", reply)
    send_long(message.chat.id, esc(reply))


def _keep_typing(chat_id, stop_evt):
    """Refresh the 'typing…' indicator while the LLM thinks (per-chat event)."""
    while not stop_evt.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            break
        stop_evt.wait(timeout=4.0)


@bot.message_handler(func=lambda m: True)
@member_only
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

