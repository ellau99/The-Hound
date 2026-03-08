"""
The Hound 🐕 — Telegram Activity Tracker Bot v2
"""

import os
import re
import json
import logging
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TIMEZONE = os.environ.get("TZ", "Europe/London")
DATA_DIR = Path(__file__).parent / "data"
GROUPS_FILE = DATA_DIR / "groups.json"

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
BOT_USERNAME = "luminahound_bot"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def _now() -> datetime:
    """Get current time in configured timezone."""
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)

# ---------------------------------------------------------------------------
# Group registry — tracks which chats + threads the bot is active in
# ---------------------------------------------------------------------------
def load_groups() -> dict:
    """Load registry of active groups. {chat_id: {"thread_id": N or null}}"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if GROUPS_FILE.exists():
        with open(GROUPS_FILE) as f:
            return json.load(f)
    return {}

def save_groups(groups: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(GROUPS_FILE, "w") as f:
        json.dump(groups, f, indent=2)

def register_group(chat_id: int, thread_id: int = None):
    """Register a group chat so scheduled messages go to it."""
    groups = load_groups()
    cid = str(chat_id)
    if cid not in groups:
        groups[cid] = {"thread_id": thread_id}
        save_groups(groups)
        logger.info("Registered new group: %s (thread: %s)", chat_id, thread_id)
    elif thread_id and groups[cid].get("thread_id") != thread_id:
        groups[cid]["thread_id"] = thread_id
        save_groups(groups)

# ---------------------------------------------------------------------------
# Per-chat data helpers
# ---------------------------------------------------------------------------
def _data_file(chat_id) -> Path:
    return DATA_DIR / f"chat_{chat_id}.json"

def load_data(chat_id) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = _data_file(chat_id)
    if f.exists():
        with open(f) as fh:
            return json.load(fh)
    return {"week_of": _monday(), "topic": "", "members": {}, "team_tasks": [], "context_notes": []}

def save_data(data: dict, chat_id):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_data_file(chat_id), "w") as f:
        json.dump(data, f, indent=2, default=str)

def _monday() -> str:
    today = _now()
    return (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")

def ensure_week(data: dict, chat_id) -> dict:
    if data.get("week_of") != _monday():
        data = {"week_of": _monday(), "topic": "", "members": {}, "team_tasks": [], "context_notes": []}
        save_data(data, chat_id)
    return data

def get_member(data: dict, user) -> dict:
    uid = str(user.id)
    if uid not in data["members"]:
        data["members"][uid] = {
            "name": user.first_name or user.username or "Unknown",
            "username": user.username or "",
            "tasks": [], "completed": [], "updates": [],
        }
    else:
        data["members"][uid]["name"] = user.first_name or user.username or "Unknown"
        if user.username:
            data["members"][uid]["username"] = user.username
    return data["members"][uid]

def find_member_by_name(data: dict, query: str):
    """Find a member by name or username (partial, case-insensitive)."""
    q = query.lower().lstrip("@")
    for uid, info in data.get("members", {}).items():
        if q in info.get("name", "").lower() or q in info.get("username", "").lower():
            return uid, info
    return None, None

def add_context_note(data: dict, note: str):
    """Store context from @ mentions for the hourly check-in."""
    if "context_notes" not in data:
        data["context_notes"] = []
    data["context_notes"].append({
        "time": _now().isoformat(),
        "note": note,
    })
    # Keep last 20 notes max
    data["context_notes"] = data["context_notes"][-20:]

# ---------------------------------------------------------------------------
# Deadline parser
# ---------------------------------------------------------------------------
def parse_deadline(text: str):
    if not text:
        return None
    text = text.strip().lower()
    now = _now()

    # Relative: 10mins, 2hours, 1h30m, in 10 mins
    m = re.match(r'^(?:in\s+)?(\d+)\s*(?:h|hr|hrs|hour|hours)\s*(?:(\d+)\s*(?:m|min|mins|minutes?))?\s*$', text)
    if m:
        return now + timedelta(hours=int(m.group(1)), minutes=int(m.group(2) or 0))
    m = re.match(r'^(?:in\s+)?(\d+)\s*(?:m|min|mins|minutes?)\s*$', text)
    if m:
        return now + timedelta(minutes=int(m.group(1)))

    # Time component
    time_part = None
    tokens = text.split()
    remaining = []
    for tok in tokens:
        tm = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', tok)
        if tm and time_part is None:
            h, mi = int(tm.group(1)), int(tm.group(2) or 0)
            ap = tm.group(3)
            if ap == "pm" and h != 12: h += 12
            elif ap == "am" and h == 12: h = 0
            time_part = (h, mi)
        else:
            remaining.append(tok)

    if time_part is None:
        time_part = (17, 0)

    date_text = " ".join(remaining).strip()
    days_map = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
                "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
                "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}

    if not date_text or date_text == "today":
        dp = now.date()
    elif date_text in ("tonight", "this evening", "end of day", "eod"):
        dp = now.date()
        if time_part == (17, 0):  # Override default 5pm with 11pm for "tonight"
            time_part = (23, 0)
    elif date_text == "tomorrow":
        dp = (now + timedelta(days=1)).date()
    elif date_text in days_map:
        ahead = days_map[date_text] - now.weekday()
        if ahead < 0: ahead += 7  # Changed from <= 0 to < 0, so same day = today
        dp = (now + timedelta(days=ahead)).date()
    else:
        dp = None
        for fmt in ("%Y-%m-%d", "%d/%m", "%d/%m/%Y", "%d-%m", "%d-%m-%Y"):
            try:
                p = datetime.strptime(date_text, fmt)
                if "%Y" not in fmt:
                    p = p.replace(year=now.year)
                    if p.date() < now.date(): p = p.replace(year=now.year + 1)
                dp = p.date()
                break
            except ValueError:
                continue

    if dp is None:
        return None
    return datetime(dp.year, dp.month, dp.day, time_part[0], time_part[1])

# ---------------------------------------------------------------------------
# Snapshot builder for Claude
# ---------------------------------------------------------------------------
def build_snapshot(data: dict) -> str:
    lines = [f"WEEK OF: {data['week_of']}"]
    if data.get("topic"):
        lines.append(f"FOCUS: {data['topic']}")
    now = _now()
    lines.append(f"NOW: {now.strftime('%A %d %B, %H:%M')}")

    # Context notes from @ mentions
    notes = data.get("context_notes", [])
    if notes:
        lines.append("\nCONTEXT FROM TEAM (recent @ messages to The Hound):")
        for n in notes[-10:]:
            lines.append(f"  [{n['time'][:16]}] {n['note']}")

    lines.append("")

    members = data.get("members", {})
    for uid, info in members.items():
        name = info["name"]
        uname = info.get("username", "")
        tag = f"@{uname}" if uname else name
        tasks = info.get("tasks", [])
        completed = info.get("completed", [])

        lines.append(f"MEMBER: {name} (telegram: {tag})")
        if tasks:
            for i, t in enumerate(tasks, 1):
                dl = ""
                if t.get("deadline"):
                    try:
                        d = datetime.fromisoformat(t["deadline"])
                        diff = (d - now).total_seconds()
                        if diff < 0: dl = f" OVERDUE (was due {d.strftime('%a %d %b %H:%M')})"
                        elif diff < 3600: dl = f" DUE IN {int(diff/60)} MINS"
                        else: dl = f" (due {d.strftime('%a %d %b %H:%M')})"
                    except: pass
                lines.append(f"  {i}. {t['text']}{dl}")
        else:
            lines.append("  No tasks.")
        if completed:
            lines.append(f"  Completed: {', '.join(c['text'] for c in completed)}")
        lines.append("")

    # Team tasks
    tt = data.get("team_tasks", [])
    if tt:
        lines.append("TEAM TASKS (unassigned):")
        for i, t in enumerate(tt, 1):
            dl = ""
            if t.get("deadline"):
                try:
                    d = datetime.fromisoformat(t["deadline"])
                    if (d - now).total_seconds() < 0: dl = " OVERDUE"
                    else: dl = f" (due {d.strftime('%a %d %b %H:%M')})"
                except: pass
            lines.append(f"  {i}. {t['text']}{dl}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Claude API helper
# ---------------------------------------------------------------------------
async def ask_claude(system: str, prompt: str, max_tokens: int = 500) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(CLAUDE_API_URL, headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            }, json={
                "model": CLAUDE_MODEL, "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            })
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    except Exception as e:
        logger.error("Claude API error: %s", e)
        return ""


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
CHECKIN_PROMPT = """You are "The Hound" 🐕 — a Telegram accountability bot. Female. Sharp, witty, relentless.

Write a scheduled check-in message. You're given task data and context notes from the team.

FORMAT — use this exact structure:
1. Start with "🐕 [TIME] CHECK-IN"
2. A scannable list of ALL tasks from ALL members and team tasks, one per line:
   - ⬜ @username — task name (pending)
   - ❌ @username — task name — OVERDUE (overdue)
   - ✅ @username — task name (completed this week, briefly)
   - ⬜ TEAM — unassigned task name
3. After the list, 3-5 lines of commentary. This is where your personality lives:
   - Call out overdue tasks HARD
   - Celebrate completions briefly
   - Reference any context notes (e.g. "I've been told Charlie is offline — that doesn't mean her tasks disappear")
   - Get more intense later in the day
   - Dog puns welcome
   - End with something that demands action

Use Telegram @usernames when tagging people. Single asterisks for *bold*. NO double asterisks."""

GRILL_PROMPT = """You are "The Hound" 🐕 delivering a SAVAGE roast.

Write EXACTLY 4 lines. Not 5. Not 6. FOUR. Each separated by a blank line.

Line 1: "🐕🔥 THE GRILL — [NAME]"
Line 2: Their stats as a brutal insult. 2-3 sentences. Make it devastating and specific.
Line 3: Their worst tasks demolished. 2-3 sentences. Name the tasks, predict and destroy excuses.
Line 4: The challenge — a number, a time, a dare. End with something that stings.

RULES:
- EXACTLY 4 lines separated by blank lines. NEVER more than 4.
- Use their @username. Use *bold* with SINGLE asterisks only. NO double asterisks.
- Be Gordon Ramsay mean. Reference SPECIFIC tasks from the data."""

OUTSTANDING_PROMPT = """You are "The Hound" 🐕. Analyse the task data and find what's outstanding, overdue, or forgotten.

Be thorough. Names, tasks, deadlines. Group by severity: 🚨 critical, ⚠️ stale, ✅ handled.
Start with "🐕 OUTSTANDING ITEMS". Up to 20 lines. Use @usernames.
Single asterisks for *bold*. NO double asterisks."""

MENTION_PROMPT = """You are "The Hound" 🐕 — a Telegram accountability bot. Female. Someone just @mentioned you.

You can do TWO things:
1. ANSWER questions about tasks/progress — keep it SHORT. 1-3 lines max. No essays.
2. EXECUTE actions if they're asking you to do something.

CRITICAL — INTENT PARSING:
- "I need to X", "I have to X", "I should X", "remind me to X" → add_task for the person speaking
- "We need to X", "someone needs to X" → team_task (collective, unassigned — for when ONE person needs to pick it up)
- "set everyone a task to X", "everyone needs to X", "assign everyone X" → assign_all (gives EVERY member the task individually)
- "X needs to Y", "assign X to Y", "give X the task of Y" → assign to that person
- "I did X", "X is done", "finished X" → mark as done
- "push X back", "postpone X", "move X to later" → postpone
- "delete X", "remove X", "cancel X" → delete
- "change X to Y", "edit X" → edit
- "add a deadline to the team task X" → edit_team_task
- "X is offline/sick/away/behind/ahead" → context note
- "what are my tasks", "what do I have", "what's coming up", "my status", "show my tasks" → status for the person speaking
- "what does X have", "what's on X's plate", "X's tasks", "what's left for X", "check X" → status for that person

When extracting deadlines from natural language:
- "in five minutes" = "5mins"
- "in an hour" = "1h"  
- "by 3pm" = "today 3pm"
- "by tomorrow" = "tomorrow"
- If someone says "I need to call my mom in five minutes" the task is "Call mom" and the deadline is "5mins"

Respond with a JSON block wrapped in ```json``` fences for actions:

{"action": "add_task", "user": "me", "text": "Call mom", "deadline": "5mins"}
{"action": "team_task", "text": "Sort welcome packs", "deadline": "friday"}
{"action": "assign_all", "text": "Read the report", "deadline": "tomorrow 8am"}
{"action": "done", "user": "me", "task_num": 1}
{"action": "assign", "user": "charlie", "text": "Sort welcome packs", "deadline": "friday"}
{"action": "delete", "user": "me", "task_num": 2}
{"action": "postpone", "user": "me", "task_num": 1, "deadline": "1h"}
{"action": "edit", "user": "me", "task_num": 1, "text": "New text", "deadline": "tomorrow"}
{"action": "edit_team_task", "text": "Sort welcome packs", "deadline": "friday"}
{"action": "grill", "user": "charlie"}
{"action": "context", "note": "Charlie is offline today"}
{"action": "status", "user": "me"}
{"action": "status", "user": "ella"}
{"action": "status", "user": "everyone"}

Rules:
- "user" should be the person's first name (lowercase) or "me" for the person asking
- Include a brief natural language response BEFORE the JSON block(s) (1 line max)
- If multiple actions are needed (e.g. "delete task 1 and task 3"), return MULTIPLE separate ```json``` blocks, one per action
- If it's just a question, NO JSON needed — just answer in 1-3 lines
- Be The Hound — witty, sharp, brief. Not verbose.

Use Telegram Markdown: *bold* with SINGLE asterisks. NO double asterisks."""

# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------
def format_status(info: dict, week: str) -> str:
    tasks = info.get("tasks", [])
    completed = info.get("completed", [])
    now = _now()
    lines = [f"📊 Week of {week}", ""]

    for i, t in enumerate(tasks, 1):
        dl_str = ""
        overdue = False
        if t.get("deadline"):
            try:
                dl = datetime.fromisoformat(t["deadline"])
                if (dl - now).total_seconds() < 0:
                    overdue = True
                    dl_str = f" — OVERDUE {dl.strftime('%a %H:%M')}"
                else:
                    dl_str = f" — {dl.strftime('%a %H:%M')}"
            except: pass
        icon = "❌" if overdue else "⬜"
        lines.append(f"{i}. {icon} {t['text']}{dl_str}")

    if not tasks:
        lines.append("No outstanding tasks.")

    if completed:
        lines.append(f"\n✅ {len(completed)} completed")

    return "\n".join(lines)

def format_check(info: dict) -> str:
    tasks = info.get("tasks", [])
    completed = info.get("completed", [])
    now = _now()
    name = info["name"]
    lines = [f"👤 *{name}*", ""]

    for i, t in enumerate(tasks, 1):
        dl_str = ""
        overdue = False
        if t.get("deadline"):
            try:
                dl = datetime.fromisoformat(t["deadline"])
                if (dl - now).total_seconds() < 0:
                    overdue = True
                    dl_str = f" — OVERDUE {dl.strftime('%a %H:%M')}"
                else:
                    dl_str = f" — {dl.strftime('%a %H:%M')}"
            except: pass
        icon = "❌" if overdue else "⬜"
        lines.append(f"{i}. {icon} {t['text']}{dl_str}")

    if not tasks:
        lines.append("No outstanding tasks.")
    if completed:
        lines.append(f"\n✅ {len(completed)} completed")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    get_member(data, user)
    save_data(data, cid)
    await update.message.reply_text(
        f"🐕 Woof! {user.first_name} is here. I already know who you are.\n"
        "/addtask to add work, /done to tick it off.\n"
        "Or just @ me and talk normally. I check in 4x daily. 🦴"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐕 *THE HOUND*\n\n"
        "/addtask, /tasks, /quicktask — add tasks\n"
        "/done, /undone, /edit, /delete — manage tasks\n"
        "/deadline, /postpone — manage deadlines\n"
        "/assign — give someone a task\n"
        "/teamtask — add an unassigned team task\n"
        "/grill — roast someone's progress\n"
        "/status, /check, /teamstatus — view progress\n"
        "/whatsoutstanding — find forgotten items\n"
        "/settopic — set weekly focus\n"
        "@luminahound_bot — ask or tell me anything\n\n"
        "Check-ins at 9am, 1pm, 6pm, 11pm.",
        parse_mode="Markdown",
    )

async def cmd_settopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        return await update.message.reply_text("Usage: /settopic EF event Thursday")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    data["topic"] = text
    save_data(data, cid)
    await update.message.reply_text(f"🐕 📌 This week's mission: *{text}* — let's get it 🔥", parse_mode="Markdown")

async def cmd_addtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        return await update.message.reply_text("Usage: /addtask Book the venue")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    m["tasks"].append({"text": text, "deadline": None, "deadline_reminded": False})
    save_data(data, cid)
    n = len(m["tasks"])
    await update.message.reply_text(f"🐕 ➕ #{n}: {text} — on the board! Don't let it collect dust. 👀")

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    full = update.message.text or ""
    after = full.split(None, 1)[1] if len(full.split(None, 1)) > 1 else ""
    lines = [l.strip() for l in after.strip().split("\n") if l.strip()]
    if not lines:
        return await update.message.reply_text("/tasks\nCall Alice | 10mins\nSend report | tomorrow 3pm\nBook venue")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    added = []
    for line in lines:
        if "|" in line:
            parts = line.split("|", 1)
            txt, dl = parts[0].strip(), parse_deadline(parts[1].strip())
        else:
            txt, dl = line, None
        if not txt: continue
        m["tasks"].append({"text": txt, "deadline": dl.isoformat() if dl else None, "deadline_reminded": False})
        n = len(m["tasks"])
        dl_str = f" ⏰ {dl.strftime('%a %H:%M')}" if dl else ""
        added.append(f"  {n}. {txt}{dl_str}")
    save_data(data, cid)
    await update.message.reply_text(f"➕ {len(added)} added:\n" + "\n".join(added))

async def cmd_quicktask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text or "|" not in text:
        return await update.message.reply_text("Usage: /quicktask Call Alice | 10mins")
    parts = text.split("|", 1)
    txt, dl = parts[0].strip(), parse_deadline(parts[1].strip())
    if not dl:
        return await update.message.reply_text(f"🐕 Couldn't parse \"{parts[1].strip()}\" as a deadline.")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    m["tasks"].append({"text": txt, "deadline": dl.isoformat(), "deadline_reminded": False})
    save_data(data, cid)
    n = len(m["tasks"])
    await update.message.reply_text(f"🐕 ➕ #{n}: {txt} ⏰ {dl.strftime('%a %d %b %H:%M')} — clock's ticking! 🔥")

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /done 1")
    try: num = int(ctx.args[0])
    except: return await update.message.reply_text("That's not a number.")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    tasks = m.get("tasks", [])
    if num < 1 or num > len(tasks):
        return await update.message.reply_text(f"Pick 1–{len(tasks)}.")
    done = tasks.pop(num - 1)
    if "completed" not in m: m["completed"] = []
    m["completed"].append({"text": done["text"], "at": _now().isoformat()})
    save_data(data, cid)
    left = len(tasks)
    msg = f"🐕 ✅ {done['text']}" + (" — all clear! You absolute legend 🔥👑" if left == 0 else f" — smashed it! {left} left, keep hunting 🦴")
    await update.message.reply_text(msg)

async def cmd_undone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    completed = m.get("completed", [])
    if not completed:
        return await update.message.reply_text("🐕 Nothing to undo — your conscience is clean... for now 👀")
    restored = completed.pop()
    m["tasks"].append({"text": restored["text"], "deadline": None, "deadline_reminded": False})
    save_data(data, cid)
    await update.message.reply_text(f"🐕 ↩️ Back from the dead: {restored['text']} — The Hound never forgets 👀")

async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if len(args) < 2:
        return await update.message.reply_text("Usage: /edit 2 New text\nor /edit 2 New text | friday 3pm\nor /edit 2 | friday 3pm")
    try: num = int(args[0])
    except: return await update.message.reply_text("First arg must be task number.")
    rest = " ".join(args[1:]).strip()
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    tasks = m.get("tasks", [])
    if num < 1 or num > len(tasks):
        return await update.message.reply_text(f"Pick 1–{len(tasks)}.")

    if "|" in rest:
        parts = rest.split("|", 1)
        new_text = parts[0].strip()
        new_dl = parse_deadline(parts[1].strip())
        if new_text:
            tasks[num-1]["text"] = new_text
        if new_dl:
            tasks[num-1]["deadline"] = new_dl.isoformat()
            tasks[num-1]["deadline_reminded"] = False
    else:
        tasks[num-1]["text"] = rest

    save_data(data, cid)
    t = tasks[num-1]
    dl_str = ""
    if t.get("deadline"):
        try: dl_str = f" ⏰ {datetime.fromisoformat(t['deadline']).strftime('%a %d %b %H:%M')}"
        except: pass
    await update.message.reply_text(f"🐕 ✏️ #{num} updated: {t['text']}{dl_str} — noted! 📋")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /delete 2")
    try: num = int(ctx.args[0])
    except: return await update.message.reply_text("That's not a number.")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    tasks = m.get("tasks", [])
    if num < 1 or num > len(tasks):
        return await update.message.reply_text(f"Pick 1–{len(tasks)}.")
    removed = tasks.pop(num - 1)
    save_data(data, cid)
    await update.message.reply_text(f"🐕 🗑️ Gone: {removed['text']} — buried in the garden 🦴")

async def cmd_deadline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if len(args) < 2:
        return await update.message.reply_text("Usage: /deadline 1 tomorrow 3pm")
    try: num = int(args[0])
    except: return await update.message.reply_text("First arg must be task number.")
    dl = parse_deadline(" ".join(args[1:]))
    if not dl:
        return await update.message.reply_text("Couldn't parse that deadline.")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    tasks = m.get("tasks", [])
    if num < 1 or num > len(tasks):
        return await update.message.reply_text(f"Pick 1–{len(tasks)}.")
    tasks[num-1]["deadline"] = dl.isoformat()
    tasks[num-1]["deadline_reminded"] = False
    save_data(data, cid)
    await update.message.reply_text(f"🐕 ⏰ #{num} due {dl.strftime('%a %d %b %H:%M')} — I'll be watching 👀")

async def cmd_postpone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if len(args) < 2:
        return await update.message.reply_text("Usage: /postpone 1 friday 3pm")
    try: num = int(args[0])
    except: return await update.message.reply_text("First arg must be task number.")
    dl = parse_deadline(" ".join(args[1:]))
    if not dl:
        return await update.message.reply_text("Couldn't parse that deadline.")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    tasks = m.get("tasks", [])
    if num < 1 or num > len(tasks):
        return await update.message.reply_text(f"Pick 1–{len(tasks)}.")
    tasks[num-1]["deadline"] = dl.isoformat()
    tasks[num-1]["deadline_reminded"] = False
    save_data(data, cid)
    await update.message.reply_text(f"🐕 📅 #{num} pushed to {dl.strftime('%a %d %b %H:%M')} — you're not off the hook though 🐾")

async def cmd_assign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        return await update.message.reply_text("Usage: /assign @charlie Book the venue\nor /assign @charlie Book venue | friday")
    # First token should be the person
    tokens = text.split(None, 1)
    if len(tokens) < 2:
        return await update.message.reply_text("Need a person and a task.")
    person_query = tokens[0].lstrip("@")
    task_text = tokens[1]

    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    uid, info = find_member_by_name(data, person_query)
    if not info:
        # Create placeholder member
        placeholder_uid = f"placeholder_{person_query.lower()}"
        data["members"][placeholder_uid] = {
            "name": person_query.capitalize(),
            "username": person_query.lower(),
            "tasks": [], "completed": [], "updates": [],
        }
        info = data["members"][placeholder_uid]

    dl = None
    txt = task_text
    if "|" in task_text:
        parts = task_text.split("|", 1)
        txt = parts[0].strip()
        dl = parse_deadline(parts[1].strip())

    info["tasks"].append({"text": txt, "deadline": dl.isoformat() if dl else None, "deadline_reminded": False})
    save_data(data, cid)
    n = len(info["tasks"])
    dl_str = f" ⏰ {dl.strftime('%a %d %b %H:%M')}" if dl else ""
    uname = info.get("username", "")
    tag = f"@{uname}" if uname else info["name"]
    await update.message.reply_text(f"🐕 ➕ Thrown to {tag}: {txt}{dl_str} — good luck 🫡")

async def cmd_teamtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        return await update.message.reply_text("Usage: /teamtask Sort the welcome packs\nor /teamtask Sort packs | friday")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    if "team_tasks" not in data: data["team_tasks"] = []
    dl = None
    txt = text
    if "|" in text:
        parts = text.split("|", 1)
        txt = parts[0].strip()
        dl = parse_deadline(parts[1].strip())
    data["team_tasks"].append({"text": txt, "deadline": dl.isoformat() if dl else None})
    save_data(data, cid)
    n = len(data["team_tasks"])
    dl_str = f" ⏰ {dl.strftime('%a %d %b %H:%M')}" if dl else ""
    await update.message.reply_text(f"🐕 📌 Team task #{n}: {txt}{dl_str} — someone claim this before I start barking 🐾")

async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        return await update.message.reply_text("Usage: /update Spoke to client, confirmed")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    m["updates"].append({"time": _now().isoformat(), "text": text})
    save_data(data, cid)
    await update.message.reply_text("🐕 📝 Logged! The Hound sees all, forgets nothing 👁️")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    m = get_member(data, update.effective_user)
    save_data(data, cid)
    await update.message.reply_text(format_status(m, data["week_of"]), parse_mode="Markdown")

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip() if ctx.args else ""
    if not query:
        return await update.message.reply_text("Usage: /check Alice")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    uid, info = find_member_by_name(data, query)
    if not info:
        known = ", ".join(i["name"] for i in data["members"].values())
        return await update.message.reply_text(f"Can't find \"{query}\". Registered: {known}")
    await update.message.reply_text(format_check(info), parse_mode="Markdown")

async def cmd_teamstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    lines = [f"📋 Week of {data['week_of']}", ""]
    for uid, info in data.get("members", {}).items():
        name = info["name"]
        tasks = info.get("tasks", [])
        completed = info.get("completed", [])
        now = _now()
        overdue = sum(1 for t in tasks if t.get("deadline") and (datetime.fromisoformat(t["deadline"]) - now).total_seconds() < 0)
        line = f"{name}: {len(tasks)} open"
        if overdue: line += f" · {overdue} overdue"
        line += f" · {len(completed)} done"
        lines.append(line)
    tt = data.get("team_tasks", [])
    if tt:
        lines.append(f"\n📌 {len(tt)} team task(s) unassigned")
    await update.message.reply_text("\n".join(lines))

async def cmd_whatsoutstanding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    snapshot = build_snapshot(data)
    reply = await ask_claude(OUTSTANDING_PROMPT, snapshot, 1000)
    if not reply:
        return await update.message.reply_text("🐕 Brain offline. Try /teamstatus.")
    try:
        await update.message.reply_text(reply, parse_mode="Markdown")
    except:
        await update.message.reply_text(reply)

async def cmd_grill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip() if ctx.args else ""
    if not query:
        return await update.message.reply_text("Usage: /grill Charlie")
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    uid, info = find_member_by_name(data, query)
    if not info:
        return await update.message.reply_text(f"Can't find \"{query}\".")
    snapshot = build_snapshot(data)
    uname = info.get("username", "")
    tag = f"@{uname}" if uname else info["name"]
    prompt = f"Grill {info['name']} (telegram: {tag}). Here's the data:\n\n{snapshot}"
    reply = await ask_claude(GRILL_PROMPT, prompt, 550)
    if not reply:
        return await update.message.reply_text("🐕 Brain offline.")
    # Send each paragraph as a separate message
    import asyncio
    lines = [l.strip() for l in reply.split("\n\n") if l.strip()]
    # If Claude didn't use blank lines, fall back to single newlines
    if len(lines) <= 2:
        lines = [l.strip() for l in reply.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        try:
            await update.message.reply_text(line, parse_mode="Markdown")
        except:
            await update.message.reply_text(line)
        if i < len(lines) - 1:
            await asyncio.sleep(2)

# ---------------------------------------------------------------------------
# @ mention handler — natural language + action execution
# ---------------------------------------------------------------------------
async def handle_mention(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    mention = f"@{BOT_USERNAME}"
    if mention.lower() not in text.lower():
        return

    question = text.replace(mention, "").replace(f"@{BOT_USERNAME.upper()}", "").strip()
    if not question:
        return await update.message.reply_text("🐕 You rang?")

    user = update.effective_user
    name = user.first_name or user.username or "Unknown"
    cid = update.effective_chat.id
    register_group(cid, getattr(update.message, "message_thread_id", None))
    data = ensure_week(load_data(cid), cid)
    get_member(data, user)  # Auto-register
    save_data(data, cid)
    snapshot = build_snapshot(data)

    prompt = f"{name} said: \"{question}\"\n\nTask data:\n{snapshot}"
    reply = await ask_claude(MENTION_PROMPT, prompt, 400)

    if not reply:
        return await update.message.reply_text("🐕 Brain glitch. Try again.")

    # Check for JSON action blocks (can be multiple)
    json_matches = re.findall(r'```json\s*(\{.*?\})\s*```', reply, re.DOTALL)
    if json_matches:
        results = []
        has_grill = False
        for json_str in json_matches:
            try:
                action = json.loads(json_str)

                # Handle grill action specially
                if action.get("action") == "grill":
                    has_grill = True
                    target_name = action.get("user", "")
                    uid_t, info_t = find_member_by_name(data, target_name)
                    if info_t:
                        snapshot = build_snapshot(data)
                        uname = info_t.get("username", "")
                        tag = f"@{uname}" if uname else info_t["name"]
                        grill_prompt = f"Grill {info_t['name']} (telegram: {tag}). Here's the data:\n\n{snapshot}"
                        grill_reply = await ask_claude(GRILL_PROMPT, grill_prompt, 550)
                        if grill_reply:
                            import asyncio
                            lines_g = [l.strip() for l in grill_reply.split("\n\n") if l.strip()]
                            if len(lines_g) <= 2:
                                lines_g = [l.strip() for l in grill_reply.split("\n") if l.strip()]
                            for i, line in enumerate(lines_g):
                                try:
                                    await update.message.reply_text(line, parse_mode="Markdown")
                                except:
                                    await update.message.reply_text(line)
                                if i < len(lines_g) - 1:
                                    await asyncio.sleep(2)
                            reply = ""
                    else:
                        results.append(f"Can't find \"{target_name}\".")

                elif action.get("action") == "context":
                    add_context_note(data, f"{name}: {action.get('note', question)}")
                    save_data(data, cid)

                else:
                    # Re-load data each time since previous action may have changed task indices
                    data = load_data(cid)
                    data = ensure_week(data, cid)
                    result = await execute_action(action, data, user, cid)
                    if result:
                        results.append(result)
            except Exception as e:
                logger.error("Action execution error: %s", e)

        if not has_grill:
            clean_reply = re.sub(r'```json\s*\{.*?\}\s*```', '', reply, flags=re.DOTALL).strip()
            if results:
                results_str = "\n".join(results)
                reply = f"{clean_reply}\n{results_str}" if clean_reply else results_str
            else:
                reply = clean_reply
    else:
        if any(kw in question.lower() for kw in ["offline", "away", "sick", "finished", "done", "completed", "hasn't", "didn't", "won't", "can't", "late", "behind", "ahead", "grill", "roast"]):
            add_context_note(data, f"{name} said: {question}")
            save_data(data, cid)

    if reply.strip():
        try:
            await update.message.reply_text(reply.strip(), parse_mode="Markdown")
        except:
            await update.message.reply_text(reply.strip())


async def execute_action(action: dict, data: dict, requesting_user, chat_id) -> str:
    """Execute an action parsed from Claude's response."""
    act = action.get("action", "")
    target = action.get("user", "me")

    # Handle "everyone" status before member resolution
    if act == "status" and target in ("everyone", "all", "team"):
        week = data.get("week_of", "")
        lines = [f"📋 Week of {week}", ""]
        for uid, inf in data.get("members", {}).items():
            lines.append(format_check(inf))
            lines.append("")
        tt = data.get("team_tasks", [])
        if tt:
            lines.append("📌 *Team tasks:*")
            for i, t in enumerate(tt, 1):
                lines.append(f"  {i}. ⬜ {t['text']}")
        return "\n".join(lines) if len(lines) > 2 else "No one has any tasks yet."

    # Resolve target member
    if target == "me":
        uid = str(requesting_user.id)
        info = data.get("members", {}).get(uid)
        if not info:
            info = get_member(data, requesting_user)
    else:
        uid, info = find_member_by_name(data, target)
        if not info:
            # Create placeholder member from username/name
            placeholder_name = target.lstrip("@").capitalize()
            placeholder_username = target.lstrip("@").lower()
            placeholder_uid = f"placeholder_{placeholder_username}"
            data["members"][placeholder_uid] = {
                "name": placeholder_name,
                "username": placeholder_username,
                "tasks": [], "completed": [], "updates": [],
            }
            info = data["members"][placeholder_uid]
            save_data(data, chat_id)

    if act == "add_task" or act == "assign":
        txt = action.get("text", "")
        if not txt: return ""
        dl = parse_deadline(action.get("deadline", "")) if action.get("deadline") else None
        info["tasks"].append({"text": txt, "deadline": dl.isoformat() if dl else None, "deadline_reminded": False})
        save_data(data, chat_id)
        dl_str = f" ⏰ {dl.strftime('%a %H:%M')}" if dl else ""
        return f"➕ Added for {info['name']}: {txt}{dl_str}"

    elif act == "team_task":
        txt = action.get("text", "")
        if not txt: return ""
        dl = parse_deadline(action.get("deadline", "")) if action.get("deadline") else None
        if "team_tasks" not in data: data["team_tasks"] = []
        data["team_tasks"].append({"text": txt, "deadline": dl.isoformat() if dl else None})
        save_data(data, chat_id)
        dl_str = f" ⏰ {dl.strftime('%a %H:%M')}" if dl else ""
        return f"📌 Team task added: {txt}{dl_str}"

    elif act == "assign_all":
        txt = action.get("text", "")
        if not txt: return ""
        dl = parse_deadline(action.get("deadline", "")) if action.get("deadline") else None
        members = data.get("members", {})
        if not members:
            return "No one's registered yet — nobody to assign to."
        names = []
        for uid, m in members.items():
            m["tasks"].append({"text": txt, "deadline": dl.isoformat() if dl else None, "deadline_reminded": False})
            names.append(m["name"])
        save_data(data, chat_id)
        dl_str = f" ⏰ {dl.strftime('%a %H:%M')}" if dl else ""
        return f"➕ Assigned to everyone ({', '.join(names)}): {txt}{dl_str}"

    elif act == "edit_team_task":
        txt = action.get("text", "")
        dl_str_input = action.get("deadline", "")
        tt = data.get("team_tasks", [])
        if not tt:
            return "No team tasks to edit."
        # Find matching team task by partial text match
        matched = None
        for t in tt:
            if txt.lower() in t.get("text", "").lower():
                matched = t
                break
        if not matched:
            matched = tt[-1]  # Fall back to most recent
        if dl_str_input:
            dl = parse_deadline(dl_str_input)
            if dl:
                matched["deadline"] = dl.isoformat()
        if txt and txt.lower() not in matched.get("text", "").lower():
            matched["text"] = txt
        save_data(data, chat_id)
        dl_str = ""
        if matched.get("deadline"):
            try: dl_str = f" ⏰ {datetime.fromisoformat(matched['deadline']).strftime('%a %d %b %H:%M')}"
            except: pass
        return f"📌 Team task updated: {matched['text']}{dl_str}"

    elif act == "done":
        num = action.get("task_num", 0)
        tasks = info.get("tasks", [])
        if num < 1 or num > len(tasks): return f"Invalid task number."
        done = tasks.pop(num - 1)
        if "completed" not in info: info["completed"] = []
        info["completed"].append({"text": done["text"], "at": _now().isoformat()})
        save_data(data, chat_id)
        return f"✅ {done['text']}"

    elif act == "delete":
        num = action.get("task_num", 0)
        tasks = info.get("tasks", [])
        if num < 1 or num > len(tasks): return "Invalid task number."
        removed = tasks.pop(num - 1)
        save_data(data, chat_id)
        return f"🗑️ Deleted: {removed['text']}"

    elif act == "postpone":
        num = action.get("task_num", 0)
        dl = parse_deadline(action.get("deadline", ""))
        tasks = info.get("tasks", [])
        if num < 1 or num > len(tasks): return "Invalid task number."
        if not dl: return "Couldn't parse deadline."
        tasks[num-1]["deadline"] = dl.isoformat()
        tasks[num-1]["deadline_reminded"] = False
        save_data(data, chat_id)
        return f"📅 Postponed to {dl.strftime('%a %d %b %H:%M')}"

    elif act == "edit":
        num = action.get("task_num", 0)
        tasks = info.get("tasks", [])
        if num < 1 or num > len(tasks): return "Invalid task number."
        if action.get("text"): tasks[num-1]["text"] = action["text"]
        if action.get("deadline"):
            dl = parse_deadline(action["deadline"])
            if dl:
                tasks[num-1]["deadline"] = dl.isoformat()
                tasks[num-1]["deadline_reminded"] = False
        save_data(data, chat_id)
        return f"✏️ Updated: {tasks[num-1]['text']}"

    elif act == "grill":
        return ""  # Grill handled separately

    elif act == "status":
        return format_check(info) if info else "Can't find that person."

    elif act == "context":
        add_context_note(data, action.get("note", ""))
        save_data(data, chat_id)
        return ""

    return ""

# ---------------------------------------------------------------------------
# Scheduled check-ins and deadline alerts
# ---------------------------------------------------------------------------
async def send_to_group(bot: Bot, chat_id: str, thread_id, text: str):
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown", message_thread_id=thread_id)
    except:
        try:
            await bot.send_message(chat_id=int(chat_id), text=text, message_thread_id=thread_id)
        except Exception as e:
            logger.error("Send to %s failed: %s", chat_id, e)

async def scheduled_checkin(bot: Bot):
    groups = load_groups()
    for chat_id, ginfo in groups.items():
        thread_id = ginfo.get("thread_id")
        data = ensure_week(load_data(chat_id), chat_id)
        if not data.get("members"): continue
        snapshot = build_snapshot(data)
        reply = await ask_claude(CHECKIN_PROMPT, f"Write the check-in.\n\n{snapshot}", 800)
        if not reply:
            reply = f"🐕 CHECK-IN\n\n" + "\n".join(
                f"{inf['name']}: {len(inf.get('tasks',[]))} open, {len(inf.get('completed',[]))} done"
                for inf in data.get("members", {}).values()
            )
        await send_to_group(bot, chat_id, thread_id, reply)

async def check_deadlines(bot: Bot):
    groups = load_groups()
    for chat_id, ginfo in groups.items():
        thread_id = ginfo.get("thread_id")
        data = ensure_week(load_data(chat_id), chat_id)
        now = _now()
        alerts = []
        for uid, info in data.get("members", {}).items():
            uname = info.get("username", "")
            tag = f"@{uname}" if uname else info["name"]
            for task in info.get("tasks", []):
                if not task.get("deadline") or task.get("deadline_reminded", False): continue
                try: dl = datetime.fromisoformat(task["deadline"])
                except: continue
                diff = (dl - now).total_seconds()
                if 0 < diff <= 3600:
                    alerts.append({"tag": tag, "task": task["text"], "mins": int(diff/60)})
                    task["deadline_reminded"] = True
                elif -7200 <= diff <= 0:
                    alerts.append({"tag": tag, "task": task["text"], "mins": 0, "overdue": True})
                    task["deadline_reminded"] = True
        if not alerts: continue
        save_data(data, chat_id)
        lines = ["🚨🐕 *DEADLINE ALERT*", ""]
        for a in alerts:
            if a.get("overdue"):
                lines.append(f"❌ {a['tag']} — \"{a['task']}\" is OVERDUE")
            else:
                lines.append(f"⏰ {a['tag']} — \"{a['task']}\" due in {a['mins']} mins")
        await send_to_group(bot, chat_id, thread_id, "\n".join(lines))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("Set TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    for cmd, fn in [
        ("start", cmd_start), ("help", cmd_help), ("settopic", cmd_settopic),
        ("addtask", cmd_addtask), ("tasks", cmd_tasks), ("quicktask", cmd_quicktask),
        ("done", cmd_done), ("undone", cmd_undone), ("edit", cmd_edit),
        ("delete", cmd_delete), ("deadline", cmd_deadline), ("postpone", cmd_postpone),
        ("assign", cmd_assign), ("teamtask", cmd_teamtask), ("update", cmd_update),
        ("status", cmd_status), ("check", cmd_check), ("teamstatus", cmd_teamstatus),
        ("whatsoutstanding", cmd_whatsoutstanding), ("grill", cmd_grill),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(f"(?i)@{BOT_USERNAME}"),
        handle_mention,
    ))

    async def post_init(application):
        bot = application.bot
        scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        # Check-ins at 9am, 1pm, 6pm, 11pm
        for h in [9, 13, 18, 23]:
            scheduler.add_job(scheduled_checkin, CronTrigger(hour=h, minute=0), args=[bot], id=f"checkin_{h}")
        scheduler.add_job(check_deadlines, IntervalTrigger(minutes=2), args=[bot], id="deadlines")
        scheduler.start()
        logger.info("Scheduler started — check-ins at 9am, 1pm, 6pm, 11pm (%s)", TIMEZONE)

    app.post_init = post_init
    logger.info("🐕 The Hound is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
