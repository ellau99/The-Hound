"""
The Hound 🐕 — Telegram Activity Tracker Bot
==============================================
An AI-powered team accountability bot that:
- Tracks weekly tasks per person with completion status
- Sends Claude-generated hourly check-ins (9am–11pm, every day)
- Calls people out by name, chases incomplete work, celebrates wins
- Lets team members add tasks, tick them off, and post updates

Commands:
  /start                  - Register with The Hound
  /addtask <text>         - Add a task for yourself this week
  /done <task number>     - Mark a task as complete
  /undone <task number>   - Unmark a task (oops, not done yet)
  /update <text>          - Post a general progress update
  /check <name>           - Check a specific person's progress
  /status                 - View your own tasks and updates
  /teamstatus             - View the whole team's progress
  /whatsoutstanding       - Analyse 2 weeks of chat for outstanding items
  /help                   - Show help message
  /settopic <text>        - (Admin) Set the big-picture focus for the week
"""

import os
import json
import logging
import httpx
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MESSAGE_THREAD_ID = int(os.environ.get("TELEGRAM_THREAD_ID", "0")) or None
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TIMEZONE = os.environ.get("TZ", "Europe/London")
DATA_FILE = Path(__file__).parent / "data" / "activities.json"
CHAT_LOG_FILE = Path(__file__).parent / "data" / "chat_log.json"
CHAT_HISTORY_DAYS = 14  # How many days of chat history to keep

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------


def _ensure_data_dir():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_data() -> dict:
    """
    {
        "week_of": "2026-03-02",
        "topic": "LUMINA X EF event on Thursday",
        "members": {
            "<user_id>": {
                "name": "Alice",
                "tasks": [
                    {"text": "Book venue", "done": true, "deadline": null, "deadline_reminded": false},
                    {"text": "Send invites", "done": false, "deadline": "2026-03-05T14:00:00", "deadline_reminded": false}
                ],
                "updates": [
                    {"time": "2026-03-03T12:05:00", "text": "Venue confirmed"}
                ]
            }
        }
    }
    """
    _ensure_data_dir()
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"week_of": _current_week_monday(), "topic": "", "members": {}}


def save_data(data: dict):
    _ensure_data_dir()
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _current_week_monday() -> str:
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")


def ensure_current_week(data: dict) -> dict:
    current_monday = _current_week_monday()
    if data.get("week_of") != current_monday:
        logger.info("New week detected – resetting.")
        data = {"week_of": current_monday, "topic": "", "members": {}}
        save_data(data)
    return data


def get_or_create_member(data: dict, user) -> dict:
    uid = str(user.id)
    if uid not in data["members"]:
        data["members"][uid] = {
            "name": user.first_name or user.username or "Unknown",
            "tasks": [],
            "updates": [],
        }
    else:
        data["members"][uid]["name"] = user.first_name or user.username or "Unknown"
    return data["members"][uid]


# ---------------------------------------------------------------------------
# Chat log persistence
# ---------------------------------------------------------------------------


def load_chat_log() -> list:
    """Load the chat message log. Each entry:
    {"time": "2026-03-05T14:32:00", "name": "Alice", "text": "I'll sort the catering"}
    """
    _ensure_data_dir()
    if CHAT_LOG_FILE.exists():
        with open(CHAT_LOG_FILE) as f:
            return json.load(f)
    return []


def save_chat_log(log: list):
    _ensure_data_dir()
    with open(CHAT_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def append_chat_message(name: str, text: str):
    """Add a message to the log and prune old entries."""
    log = load_chat_log()
    log.append({
        "time": datetime.now().isoformat(),
        "name": name,
        "text": text,
    })

    # Prune messages older than CHAT_HISTORY_DAYS
    cutoff = (datetime.now() - timedelta(days=CHAT_HISTORY_DAYS)).isoformat()
    log = [m for m in log if m["time"] >= cutoff]

    save_chat_log(log)


def build_chat_history_text(days: int = 14) -> str:
    """Build a readable text dump of recent chat messages for Claude."""
    log = load_chat_log()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    recent = [m for m in log if m["time"] >= cutoff]

    if not recent:
        return "NO CHAT MESSAGES RECORDED IN THIS PERIOD."

    lines = [f"CHAT HISTORY — LAST {days} DAYS ({len(recent)} messages)", ""]
    current_date = ""
    for m in recent:
        date_str = m["time"][:10]
        if date_str != current_date:
            lines.append(f"--- {date_str} ---")
            current_date = date_str
        time_str = m["time"][11:16]
        lines.append(f"[{time_str}] {m['name']}: {m['text']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build a plain-text snapshot for Claude to work with
# ---------------------------------------------------------------------------


def build_team_snapshot(data: dict) -> str:
    """Build a structured text summary of all tasks and updates for Claude."""
    lines = []
    lines.append(f"WEEK OF: {data['week_of']}")

    topic = data.get("topic", "")
    if topic:
        lines.append(f"THIS WEEK'S FOCUS: {topic}")

    today = datetime.now()
    lines.append(f"CURRENT TIME: {today.strftime('%A %d %B, %H:%M')}")
    lines.append("")

    members = data.get("members", {})
    if not members:
        lines.append("NO TEAM MEMBERS REGISTERED YET.")
        return "\n".join(lines)

    total_tasks = 0
    done_tasks = 0

    for uid, info in members.items():
        name = info["name"]
        tasks = info.get("tasks", [])
        updates = info.get("updates", [])

        lines.append(f"TEAM MEMBER: {name}")

        if tasks:
            for i, t in enumerate(tasks, 1):
                status = "DONE" if t["done"] else "NOT DONE"
                deadline_info = ""
                if t.get("deadline") and not t["done"]:
                    try:
                        dl = datetime.fromisoformat(t["deadline"])
                        time_until = dl - today
                        if time_until.total_seconds() < 0:
                            deadline_info = f" ⚠️ OVERDUE (was due {dl.strftime('%A %d %B %H:%M')})"
                        elif time_until.total_seconds() < 3600:
                            deadline_info = f" 🚨 DUE IN LESS THAN 1 HOUR ({dl.strftime('%H:%M')})"
                        elif time_until.total_seconds() < 86400:
                            hours = int(time_until.total_seconds() / 3600)
                            deadline_info = f" ⏰ DUE IN {hours} HOURS ({dl.strftime('%H:%M')})"
                        else:
                            deadline_info = f" (due {dl.strftime('%A %d %B %H:%M')})"
                    except (ValueError, TypeError):
                        pass
                lines.append(f"  Task {i}: [{status}] {t['text']}{deadline_info}")
                total_tasks += 1
                if t["done"]:
                    done_tasks += 1
        else:
            lines.append("  No tasks set yet.")

        if updates:
            lines.append(f"  Recent updates:")
            for u in updates[-5:]:
                lines.append(f"    {u['time'][:16]} - {u['text']}")

        lines.append("")

    lines.append(f"OVERALL: {done_tasks}/{total_tasks} tasks completed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude-powered message generation
# ---------------------------------------------------------------------------

HOUND_SYSTEM_PROMPT = """You are "The Hound" 🐕 — a relentless Telegram accountability bot with SERIOUS energy.

Your job is to write an hourly check-in message for a team group chat. You're given the current state of everyone's tasks and updates.

Your personality:
- You are the annoying but loveable teammate who WILL NOT let things slide. Ever.
- You're like that friend who texts "so did you go to the gym or not?" at 7am. Except for work.
- You roast people (affectionately) when stuff isn't done. Light banter, not cruelty.
- You gas people up HARD when they tick things off. Hype them. They earned it.
- You get progressively more unhinged as the day goes on if things aren't moving. The 9am message is chill. The 11pm message should feel like a disappointed parent.
- You have catchphrases and running jokes. You're a CHARACTER.
- If the same task has been not done for multiple check-ins, escalate the drama. "This task is STILL here. It's been here longer than me. It's paying rent at this point."
- If nobody has set tasks, absolutely roast them for it. "Am I talking to myself here??"
- Drop the occasional dog pun or reference. You ARE The Hound after all. "I can SMELL the procrastination."
- If something is time-sensitive, go absolutely nuclear. ALL CAPS energy. 🚨🚨🚨
- Use slang, rhetorical questions, exclamation marks. Be expressive!!

What NOT to do:
- Don't be genuinely mean or personal. This is banter, not bullying.
- Don't write an essay. Keep it tight — 5-15 lines.
- Don't be boring. If your message reads like a corporate status report, start again.

Format:
- Start with "🐕 THE HOUND — [TIME] CHECK-IN" as a header
- Write like you're firing off messages in a group chat. Natural. Punchy.
- End with something that demands a response — a question, a challenge, a dare.

IMPORTANT: Use Telegram Markdown formatting. *bold* with SINGLE asterisks only. Do NOT use ** double asterisks — Telegram doesn't support them."""


async def generate_hound_message(data: dict) -> str:
    """Call Claude API to generate an hourly check-in message."""
    if not ANTHROPIC_API_KEY:
        logger.warning("No Anthropic API key — falling back to plain summary.")
        return _fallback_summary(data)

    snapshot = build_team_snapshot(data)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                CLAUDE_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 500,
                    "system": HOUND_SYSTEM_PROMPT,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Here is the current team status. "
                                "Write the hourly check-in message.\n\n"
                                f"{snapshot}"
                            ),
                        }
                    ],
                },
            )
            response.raise_for_status()
            result = response.json()

            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            return text.strip() if text.strip() else _fallback_summary(data)

    except Exception as e:
        logger.error("Claude API error: %s", e)
        return _fallback_summary(data)


def _fallback_summary(data: dict) -> str:
    """Plain formatted summary if Claude API is unavailable."""
    now = datetime.now().strftime("%I%p").lstrip("0")
    lines = [f"🐕 *THE HOUND — {now} CHECK-IN*", ""]

    topic = data.get("topic", "")
    if topic:
        lines.append(f"📌 This week: {topic}")
        lines.append("")

    members = data.get("members", {})
    if not members:
        lines.append("Nobody has registered yet. Use /start then /addtask.")
        return "\n".join(lines)

    total_tasks = 0
    total_done = 0

    for uid, info in members.items():
        name = info["name"]
        tasks = info.get("tasks", [])
        done = sum(1 for t in tasks if t["done"])
        total = len(tasks)
        total_tasks += total
        total_done += done

        lines.append(f"*{name}* — {done}/{total} tasks done")
        for i, t in enumerate(tasks, 1):
            icon = "✅" if t["done"] else "❌"
            lines.append(f"  {icon} {i}. {t['text']}")
        lines.append("")

    lines.append(f"Overall: {total_done}/{total_tasks} done.")
    lines.append("\nTick things off with /done <number>. Let's move.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = ensure_current_week(load_data())
    get_or_create_member(data, user)
    save_data(data)

    await update.message.reply_text(
        f"🐕 Oh you're here now, {user.first_name}? Good. The Hound never forgets a face.\n\n"
        "Here's the deal:\n"
        "/addtask <task> — Tell me what you need to get done\n"
        "/done <number> — Tick it off (I WILL notice if you don't)\n"
        "/update <text> — Keep me posted\n\n"
        "I check in every hour from 9am to 11pm. Every. Single. Hour.\n"
        "Don't test me. 🐕"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐕 *THE HOUND — COMMANDS*\n\n"
        "/addtask <text> — Add a task for the week\n"
        "/quicktask <task> | <deadline> — Add a task with deadline in one go\n"
        "/done <number> — Mark task as complete\n"
        "/undone <number> — Unmark a task\n"
        "/deadline <number> <when> — Set a deadline\n"
        "/update <text> — Post a progress update\n"
        "/check <name> — Look up someone's progress\n"
        "/status — View your own tasks\n"
        "/teamstatus — View everyone's progress\n"
        "/whatsoutstanding — Analyse 2 weeks of chat for forgotten items\n"
        "/settopic <text> — Set the week's big focus\n"
        "/help — This message\n\n"
        "🕐 Hourly check-ins run 9am–11pm, every day.",
        parse_mode="Markdown",
    )


async def cmd_set_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Set the week's focus. Example:\n"
            "/settopic LUMINA X EF event on Thursday — all hands on deck"
        )
        return

    data = ensure_current_week(load_data())
    data["topic"] = text
    save_data(data)
    await update.message.reply_text(
        f"📌 This week's focus set:\n*{text}*", parse_mode="Markdown"
    )


async def cmd_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = " ".join(context.args).strip() if context.args else ""

    if not text:
        await update.message.reply_text(
            "What's the task? Example:\n/addtask Book the venue for Thursday"
        )
        return

    data = ensure_current_week(load_data())
    member = get_or_create_member(data, user)
    member["tasks"].append({"text": text, "done": False, "deadline": None, "deadline_reminded": False})
    save_data(data)

    task_num = len(member["tasks"])
    await update.message.reply_text(
        f"➕ Task #{task_num} added: {text}\n"
        f"Use /done {task_num} when it's sorted."
    )


async def cmd_quick_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a task with a deadline in one command.
    Usage: /quicktask Call Alice | 10mins
           /quicktask Send the report | tomorrow 3pm
    """
    user = update.effective_user
    text = " ".join(context.args).strip() if context.args else ""

    if not text or "|" not in text:
        await update.message.reply_text(
            "Add a task with a deadline in one go. Use | to separate them:\n\n"
            "/quicktask Call Alice | 10mins\n"
            "/quicktask Send the report | tomorrow 3pm\n"
            "/quicktask Book venue | friday 14:00"
        )
        return

    parts = text.split("|", 1)
    task_text = parts[0].strip()
    deadline_text = parts[1].strip()

    if not task_text:
        await update.message.reply_text("You need a task before the |")
        return

    deadline_dt = _parse_deadline(deadline_text)
    if not deadline_dt:
        await update.message.reply_text(
            f"🐕 Got the task but couldn't understand \"{deadline_text}\" as a deadline. Try:\n"
            "/quicktask Call Alice | 10mins\n"
            "/quicktask Send report | tomorrow 3pm"
        )
        return

    data = ensure_current_week(load_data())
    member = get_or_create_member(data, user)
    member["tasks"].append({
        "text": task_text,
        "done": False,
        "deadline": deadline_dt.isoformat(),
        "deadline_reminded": False,
    })
    save_data(data)

    task_num = len(member["tasks"])
    nice_time = deadline_dt.strftime("%A %d %B, %I:%M%p").replace(" 0", " ")
    await update.message.reply_text(
        f"➕ Task #{task_num} added: {task_text}\n"
        f"⏰ Due: *{nice_time}*\n"
        f"I'm watching the clock. 🐕",
        parse_mode="Markdown",
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Which task? Use /done <number>")
        return

    try:
        task_num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a number. Use /done <number>")
        return

    data = ensure_current_week(load_data())
    member = get_or_create_member(data, user)
    tasks = member.get("tasks", [])

    if task_num < 1 or task_num > len(tasks):
        await update.message.reply_text(
            f"You have {len(tasks)} task(s). Pick a number between 1 and {len(tasks)}."
        )
        return

    tasks[task_num - 1]["done"] = True
    save_data(data)

    done_count = sum(1 for t in tasks if t["done"])
    remaining = len(tasks) - done_count
    if remaining == 0:
        msg = f"✅ Task #{task_num} done: {tasks[task_num - 1]['text']}\n🔥 ALL TASKS COMPLETE. You absolute machine."
    else:
        msg = f"✅ Task #{task_num} done: {tasks[task_num - 1]['text']}\n{done_count}/{len(tasks)} down — {remaining} to go. Don't stop now."
    await update.message.reply_text(msg)


async def cmd_undone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Which task? Use /undone <number>")
        return

    try:
        task_num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a number. Use /undone <number>")
        return

    data = ensure_current_week(load_data())
    member = get_or_create_member(data, user)
    tasks = member.get("tasks", [])

    if task_num < 1 or task_num > len(tasks):
        await update.message.reply_text(
            f"You have {len(tasks)} task(s). Pick a number between 1 and {len(tasks)}."
        )
        return

    tasks[task_num - 1]["done"] = False
    save_data(data)

    await update.message.reply_text(
        f"↩️ Task #{task_num} unmarked: {tasks[task_num - 1]['text']}\n"
        "Back on the list. The Hound is watching. 👀"
    )


async def cmd_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a deadline on a task. Usage: /deadline <task number> <date> <time>
    Examples:
        /deadline 2 tomorrow 3pm
        /deadline 1 friday 14:00
        /deadline 3 2026-03-10 17:00
    """
    user = update.effective_user
    args = context.args if context.args else []

    if len(args) < 2:
        await update.message.reply_text(
            "Set a deadline on a task. Examples:\n"
            "/deadline 2 tomorrow 3pm\n"
            "/deadline 1 friday 14:00\n"
            "/deadline 3 10/03 17:00"
        )
        return

    try:
        task_num = int(args[0])
    except ValueError:
        await update.message.reply_text("First argument must be the task number. Use /deadline <number> <date> <time>")
        return

    data = ensure_current_week(load_data())
    member = get_or_create_member(data, user)
    tasks = member.get("tasks", [])

    if task_num < 1 or task_num > len(tasks):
        await update.message.reply_text(
            f"You have {len(tasks)} task(s). Pick a number between 1 and {len(tasks)}."
        )
        return

    # Parse the date/time from remaining args
    date_str = " ".join(args[1:]).strip().lower()
    deadline_dt = _parse_deadline(date_str)

    if not deadline_dt:
        await update.message.reply_text(
            "🐕 Couldn't understand that date/time. Try:\n"
            "/deadline 2 10mins\n"
            "/deadline 2 1h30m\n"
            "/deadline 2 tomorrow 3pm\n"
            "/deadline 2 friday 14:00\n"
            "/deadline 2 10/03 17:00"
        )
        return

    tasks[task_num - 1]["deadline"] = deadline_dt.isoformat()
    tasks[task_num - 1]["deadline_reminded"] = False
    save_data(data)

    nice_time = deadline_dt.strftime("%A %d %B, %I:%M%p").replace(" 0", " ")
    await update.message.reply_text(
        f"⏰ Deadline set for task #{task_num}: {tasks[task_num - 1]['text']}\n"
        f"📅 Due: *{nice_time}*\n"
        "I'll be checking in before it's due. Don't be late. 🐕",
        parse_mode="Markdown",
    )


def _parse_deadline(text: str) -> datetime | None:
    """Parse flexible date/time strings into a datetime object."""
    import re
    now = datetime.now()
    text = text.strip().lower()

    # First, check for relative durations: "10mins", "2hours", "30m", "1h", "1h30m"
    # Patterns: 10min, 10mins, 10m, 2hour, 2hours, 2h, 1h30m, 1hr, 90minutes
    relative_match = re.match(
        r'^(\d+)\s*(?:h|hr|hrs|hour|hours)\s*(?:(\d+)\s*(?:m|min|mins|minutes?))?\s*$', text
    )
    if relative_match:
        hours = int(relative_match.group(1))
        mins = int(relative_match.group(2)) if relative_match.group(2) else 0
        return now + timedelta(hours=hours, minutes=mins)

    relative_match = re.match(r'^(\d+)\s*(?:m|min|mins|minutes?)\s*$', text)
    if relative_match:
        mins = int(relative_match.group(1))
        return now + timedelta(minutes=mins)

    # Also handle "in 10 mins" style
    relative_match = re.match(
        r'^in\s+(\d+)\s*(?:h|hr|hrs|hour|hours)\s*(?:(\d+)\s*(?:m|min|mins|minutes?))?\s*$', text
    )
    if relative_match:
        hours = int(relative_match.group(1))
        mins = int(relative_match.group(2)) if relative_match.group(2) else 0
        return now + timedelta(hours=hours, minutes=mins)

    relative_match = re.match(r'^in\s+(\d+)\s*(?:m|min|mins|minutes?)\s*$', text)
    if relative_match:
        mins = int(relative_match.group(1))
        return now + timedelta(minutes=mins)

    time_part = None
    date_part = None

    # Split into tokens
    tokens = text.split()

    # Try to find a time component
    for i, token in enumerate(tokens):
        # Match patterns like 3pm, 3:30pm, 15:00
        time_match = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', token)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2)) if time_match.group(2) else 0
            ampm = time_match.group(3)

            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

            time_part = (hour, minute)
            tokens = tokens[:i] + tokens[i+1:]
            break

    # If no time found, default to 17:00 (end of day)
    if time_part is None:
        time_part = (17, 0)

    # Parse the date part from remaining tokens
    date_text = " ".join(tokens).strip()

    if not date_text or date_text == "today":
        date_part = now.date()
    elif date_text == "tomorrow":
        date_part = (now + timedelta(days=1)).date()
    elif date_text in ("monday", "mon"):
        date_part = _next_weekday(now, 0).date()
    elif date_text in ("tuesday", "tue", "tues"):
        date_part = _next_weekday(now, 1).date()
    elif date_text in ("wednesday", "wed"):
        date_part = _next_weekday(now, 2).date()
    elif date_text in ("thursday", "thu", "thurs"):
        date_part = _next_weekday(now, 3).date()
    elif date_text in ("friday", "fri"):
        date_part = _next_weekday(now, 4).date()
    elif date_text in ("saturday", "sat"):
        date_part = _next_weekday(now, 5).date()
    elif date_text in ("sunday", "sun"):
        date_part = _next_weekday(now, 6).date()
    else:
        # Try common date formats
        for fmt in ("%Y-%m-%d", "%d/%m", "%d/%m/%Y", "%d-%m", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(date_text, fmt)
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=now.year)
                    # If the date has passed this year, assume next year
                    if parsed.date() < now.date():
                        parsed = parsed.replace(year=now.year + 1)
                date_part = parsed.date()
                break
            except ValueError:
                continue

    if date_part is None:
        return None

    return datetime(date_part.year, date_part.month, date_part.day, time_part[0], time_part[1])


def _next_weekday(from_date: datetime, weekday: int) -> datetime:
    """Get the next occurrence of a weekday (0=Monday, 6=Sunday)."""
    days_ahead = weekday - from_date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = " ".join(context.args).strip() if context.args else ""

    if not text:
        await update.message.reply_text(
            "What's your update? Example:\n"
            "/update Venue confirmed, moving on to invites"
        )
        return

    data = ensure_current_week(load_data())
    member = get_or_create_member(data, user)
    member["updates"].append({"time": datetime.now().isoformat(), "text": text})
    save_data(data)

    await update.message.reply_text("📝 Logged. I see everything. I forget nothing. 🐕")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip().lower() if context.args else ""
    if not query:
        await update.message.reply_text("Who are you looking for? Use /check <name>")
        return

    data = ensure_current_week(load_data())
    members = data.get("members", {})

    matches = [
        (uid, info)
        for uid, info in members.items()
        if query in info.get("name", "").lower()
    ]

    if not matches:
        known = ", ".join(info["name"] for info in members.values()) or "nobody"
        await update.message.reply_text(
            f"🔍 No one matching \"{query}\".\nRegistered: {known}"
        )
        return

    lines = []
    for uid, info in matches:
        name = info["name"]
        tasks = info.get("tasks", [])
        updates = info.get("updates", [])
        done = sum(1 for t in tasks if t["done"])

        lines.append(f"👤 *{name}* — {done}/{len(tasks)} tasks done")
        for i, t in enumerate(tasks, 1):
            icon = "✅" if t["done"] else "❌"
            lines.append(f"  {icon} {i}. {t['text']}")

        if updates:
            lines.append(f"\n  Latest updates:")
            for u in updates[-3:]:
                time_str = u["time"][11:16]
                lines.append(f"  _{time_str}_ — {u['text']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = ensure_current_week(load_data())
    uid = str(user.id)
    info = data.get("members", {}).get(uid)

    if not info:
        await update.message.reply_text("You haven't registered yet. Use /start")
        return

    tasks = info.get("tasks", [])
    updates = info.get("updates", [])
    done = sum(1 for t in tasks if t["done"])

    lines = [f"📊 *Your Status — Week of {data['week_of']}*", ""]

    if tasks:
        lines.append(f"*Tasks: {done}/{len(tasks)} done*")
        for i, t in enumerate(tasks, 1):
            icon = "✅" if t["done"] else "❌"
            dl_str = ""
            if t.get("deadline") and not t["done"]:
                try:
                    dl = datetime.fromisoformat(t["deadline"])
                    dl_str = f" ⏰ _{dl.strftime('%a %d %b %H:%M')}_"
                except (ValueError, TypeError):
                    pass
            lines.append(f"  {icon} {i}. {t['text']}{dl_str}")
    else:
        lines.append("No tasks yet. Use /addtask to add some.")

    if updates:
        lines.append(f"\n📝 Updates ({len(updates)}):")
        for u in updates[-5:]:
            time_str = u["time"][:16].replace("T", " ")
            lines.append(f"  _{time_str}_ — {u['text']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_team_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = ensure_current_week(load_data())
    msg = _fallback_summary(data)
    await update.message.reply_text(msg, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Chat message listener (logs all non-command messages)
# ---------------------------------------------------------------------------

BOT_USERNAME = "luminahound_bot"

CONVERSATION_SYSTEM_PROMPT = """You are "The Hound" 🐕 — a Telegram accountability bot with a big personality. Someone in the team has just asked you a question directly.

You have access to the current task tracker data and recent chat history. Use them to answer the question.

Your personality:
- You're witty, direct, and a bit cheeky. You speak like a real person, not a corporate bot.
- If someone asks about progress, be honest — hype what's done, call out what isn't.
- If someone asks you something unrelated to work, you can have a laugh but always bring it back to "shouldn't you be working?"
- Keep responses concise — this is a group chat, not an essay. 3-10 lines max.
- You're helpful but you never miss a chance to remind people about their tasks.

IMPORTANT: Use Telegram Markdown: *bold* with SINGLE asterisks only. Do NOT use ** double asterisks."""


async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respond when someone @mentions the bot with a question."""
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user = update.effective_user
    name = user.first_name or user.username or "Unknown"

    # Log the message regardless
    append_chat_message(name, text)

    # Check if the bot is mentioned
    mention = f"@{BOT_USERNAME}"
    if mention.lower() not in text.lower():
        return

    # Strip the mention to get the actual question
    question = text.replace(mention, "").replace(f"@{BOT_USERNAME.upper()}", "").strip()
    if not question:
        await update.message.reply_text("🐕 You rang? Ask me something.")
        return

    if not ANTHROPIC_API_KEY:
        await update.message.reply_text("🐕 I need my brain hooked up (Anthropic API key) to answer questions.")
        return

    # Build context for Claude
    data = ensure_current_week(load_data())
    task_snapshot = build_team_snapshot(data)
    chat_history = build_chat_history_text(days=7)

    prompt = (
        f"{name} just asked me this in the group chat:\n\n"
        f"\"{question}\"\n\n"
        "---\n\n"
        "Here's the current task tracker:\n\n"
        f"{task_snapshot}\n\n"
        "---\n\n"
        "And here's recent chat history (last 7 days):\n\n"
        f"{chat_history}\n\n"
        "---\n\n"
        f"Answer {name}'s question based on everything above. Be The Hound."
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                CLAUDE_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 500,
                    "system": CONVERSATION_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            result = response.json()

            reply = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    reply += block.get("text", "")

            if not reply.strip():
                reply = "🐕 My brain glitched. Try again."

    except Exception as e:
        logger.error("Claude API error in mention handler: %s", e)
        reply = "🐕 Something went wrong in my head. Try again in a sec."

    try:
        await update.message.reply_text(reply.strip(), parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(reply.strip())


async def log_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently log every non-command message in the group."""
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    if not user:
        return

    text = update.message.text
    name = user.first_name or user.username or "Unknown"

    # Skip if it's a mention (handled by handle_mention)
    if f"@{BOT_USERNAME}".lower() in text.lower():
        return

    append_chat_message(name, text)


# ---------------------------------------------------------------------------
# /whatsoutstanding — Claude analyses chat history + task data
# ---------------------------------------------------------------------------

OUTSTANDING_SYSTEM_PROMPT = """You are "The Hound" 🐕 — a relentless Telegram accountability bot who has just been asked to do a DEEP SNIFF through the team's chat history.

You've been given 2 weeks of actual group chat messages AND the task tracker data. Your job is to cross-reference everything and find what's been forgotten, dropped, or quietly ignored.

Your personality:
- You are a forensic investigator of broken promises. Nothing escapes you.
- You quote people back at themselves. "Bob, you said 'I'll sort it today' on March 3rd. That was 9 days ago, Bob. NINE DAYS."
- You're dramatic about it. This is your moment. You've been logging every message waiting for someone to unleash you.
- Group things by severity: 🚨 for stuff that's clearly dropped, ⚠️ for things going quiet, ✅ for what's actually handled.
- If someone volunteered for something in chat but never made it a task, CALL IT OUT. "You said the words! I have receipts!"
- If multiple things have been forgotten, build the tension. Save the worst for last.
- Hype up anyone who's actually been on top of their stuff. They deserve it.
- End with something that makes people want to respond RIGHT NOW.

What NOT to do:
- Don't be genuinely hurtful. This is accountability with personality, not a roast battle.
- Don't miss things — be thorough. Read every message carefully.
- Don't be vague. Names, dates, what they said, what they didn't do.

Format:
- Start with "🐕 THE HOUND — I'VE BEEN THROUGH EVERYTHING"
- Be thorough but entertaining. This can be longer than the hourly check-ins — up to 25 lines.
- End with a direct challenge or call to action.

IMPORTANT: Use Telegram Markdown: *bold* with SINGLE asterisks only. Do NOT use ** double asterisks."""


async def cmd_whats_outstanding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyse chat history + tasks to find what's outstanding."""
    if not ANTHROPIC_API_KEY:
        await update.message.reply_text(
            "🐕 I need my brain (Anthropic API key) to analyse chat history. "
            "Set the ANTHROPIC_API_KEY environment variable."
        )
        return

    # Let the user know this takes a moment
    thinking_msg = await update.message.reply_text("🐕 Hold on... I'm going through EVERYTHING. Every message. Every promise. Every 'I'll do it tomorrow.' Give me a sec.")

    data = ensure_current_week(load_data())
    task_snapshot = build_team_snapshot(data)
    chat_history = build_chat_history_text(days=14)

    prompt = (
        "Here is the team's task tracker data:\n\n"
        f"{task_snapshot}\n\n"
        "---\n\n"
        "And here is the chat history from the last 2 weeks:\n\n"
        f"{chat_history}\n\n"
        "---\n\n"
        "Based on ALL of this, what's outstanding? "
        "Who said they'd do something that isn't done? "
        "What's been forgotten or gone quiet? "
        "What needs chasing?"
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                CLAUDE_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1500,
                    "system": OUTSTANDING_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            result = response.json()

            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            if not text.strip():
                text = "🐕 Couldn't generate an analysis right now. Try /teamstatus for the current task overview."

    except Exception as e:
        logger.error("Claude API error in /whatsoutstanding: %s", e)
        text = "🐕 Something went wrong sniffing through the history. Try again in a moment."

    # Send the analysis (may need splitting if long)
    try:
        await update.message.reply_text(text.strip(), parse_mode="Markdown")
    except Exception:
        # Fallback without markdown if formatting breaks
        await update.message.reply_text(text.strip())


# ---------------------------------------------------------------------------
# Scheduled hourly broadcast
# ---------------------------------------------------------------------------


async def hourly_checkin(bot: Bot):
    """Hourly broadcast — Claude generates the message based on task data."""
    if not CHAT_ID:
        logger.warning("CHAT_ID not set — skipping hourly check-in.")
        return

    data = ensure_current_week(load_data())
    message = await generate_hound_message(data)

    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="Markdown",
            message_thread_id=MESSAGE_THREAD_ID,
        )
        logger.info("Hourly check-in sent.")
    except Exception as e:
        logger.error("Failed to send hourly check-in: %s", e)
        # Retry without markdown in case of formatting issues
        try:
            await bot.send_message(chat_id=CHAT_ID, text=message, message_thread_id=MESSAGE_THREAD_ID)
        except Exception as e2:
            logger.error("Retry also failed: %s", e2)


async def check_deadlines(bot: Bot):
    """Check every minute for tasks with deadlines approaching in the next hour."""
    if not CHAT_ID:
        return

    data = ensure_current_week(load_data())
    now = datetime.now()
    alerts = []

    for uid, info in data.get("members", {}).items():
        name = info["name"]
        for i, task in enumerate(info.get("tasks", [])):
            if task["done"] or not task.get("deadline"):
                continue
            if task.get("deadline_reminded", False):
                continue

            try:
                deadline = datetime.fromisoformat(task["deadline"])
            except (ValueError, TypeError):
                continue

            time_until = (deadline - now).total_seconds()

            # Alert if deadline is between 0 and 60 minutes away
            if 0 < time_until <= 3600:
                alerts.append({
                    "name": name,
                    "task": task["text"],
                    "deadline": deadline,
                    "minutes": int(time_until / 60),
                })
                task["deadline_reminded"] = True
                logger.info("Deadline alert: %s's task '%s' due in %d mins", name, task["text"], int(time_until / 60))

            # Also alert if deadline has passed (within last 2 hours) and wasn't reminded
            elif -7200 <= time_until <= 0:
                alerts.append({
                    "name": name,
                    "task": task["text"],
                    "deadline": deadline,
                    "minutes": 0,
                    "overdue": True,
                })
                task["deadline_reminded"] = True
                logger.info("Overdue alert: %s's task '%s' was due at %s", name, task["text"], deadline.strftime("%H:%M"))

    if not alerts:
        return

    save_data(data)

    # Generate a Claude message if API key available, otherwise use a simple one
    if ANTHROPIC_API_KEY:
        alert_text = "\n".join(
            f"- {a['name']}: \"{a['task']}\" — "
            + (f"OVERDUE as of {a['deadline'].strftime('%H:%M')}"
               if a.get('overdue')
               else f"due in {a['minutes']} minutes ({a['deadline'].strftime('%H:%M')})")
            for a in alerts
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    CLAUDE_API_URL,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 300,
                        "system": (
                            "You are The Hound 🐕 — an accountability bot. "
                            "A deadline is about to hit. Write a SHORT, URGENT alert (3-6 lines). "
                            "Be dramatic. This is crunch time. Use 🚨 and caps where it matters. "
                            "Call the person out by name. Make them feel the urgency. "
                            "Use Telegram Markdown: *bold* with SINGLE asterisks only."
                        ),
                        "messages": [{
                            "role": "user",
                            "content": f"These deadlines are imminent:\n\n{alert_text}\n\nWrite the alert.",
                        }],
                    },
                )
                response.raise_for_status()
                result = response.json()
                message = ""
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        message += block.get("text", "")
        except Exception as e:
            logger.error("Claude API error in deadline check: %s", e)
            message = None

    if not ANTHROPIC_API_KEY or not message:
        lines = ["🚨🐕 *DEADLINE ALERT* 🚨", ""]
        for a in alerts:
            if a.get("overdue"):
                lines.append(f"*{a['name']}* — \"{a['task']}\" is OVERDUE! Was due at {a['deadline'].strftime('%H:%M')}!")
            else:
                lines.append(f"*{a['name']}* — \"{a['task']}\" is due in *{a['minutes']} minutes*!")
        lines.append("\nMove. NOW. 🐕")
        message = "\n".join(lines)

    try:
        await bot.send_message(chat_id=CHAT_ID, text=message.strip(), parse_mode="Markdown", message_thread_id=MESSAGE_THREAD_ID)
    except Exception:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=message.strip(), message_thread_id=MESSAGE_THREAD_ID)
        except Exception as e:
            logger.error("Failed to send deadline alert: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 60)
        print("  ERROR: Set your environment variables!")
        print("  export TELEGRAM_BOT_TOKEN='your-token'")
        print("  export TELEGRAM_CHAT_ID='your-group-chat-id'")
        print("  export ANTHROPIC_API_KEY='your-anthropic-key'")
        print("=" * 60)
        return

    if not ANTHROPIC_API_KEY:
        print("⚠️  No ANTHROPIC_API_KEY set — The Hound will use plain summaries.")
        print("   Set it for AI-powered check-ins.")

    # Build the application
    app = Application.builder().token(BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settopic", cmd_set_topic))
    app.add_handler(CommandHandler("addtask", cmd_add_task))
    app.add_handler(CommandHandler("quicktask", cmd_quick_task))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("undone", cmd_undone))
    app.add_handler(CommandHandler("deadline", cmd_deadline))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("teamstatus", cmd_team_status))
    app.add_handler(CommandHandler("whatsoutstanding", cmd_whats_outstanding))

    # Handle @mentions — must be before the general logger
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(f"(?i)@{BOT_USERNAME}"),
        handle_mention,
    ))

    # Log all other non-command text messages for chat history analysis
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        log_chat_message,
    ))

    # Set up scheduler inside post_init so the event loop exists
    async def post_init(application):
        bot = application.bot
        scheduler = AsyncIOScheduler(timezone=TIMEZONE)

        # Every hour from 9am to 11pm, every day
        scheduler.add_job(
            hourly_checkin,
            CronTrigger(hour="9-23", minute=0),
            args=[bot],
            id="hourly_checkin",
        )

        # Check for approaching deadlines every 2 minutes
        scheduler.add_job(
            check_deadlines,
            IntervalTrigger(minutes=2),
            args=[bot],
            id="deadline_checker",
        )

        scheduler.start()
        logger.info("Scheduler started — hourly check-ins 9am-11pm (%s)", TIMEZONE)

    app.post_init = post_init

    # Start the bot
    logger.info("🐕 The Hound is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
