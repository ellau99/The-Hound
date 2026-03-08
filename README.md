# The Hound 🐕

An AI-powered Telegram bot that keeps teams accountable. It tracks tasks, sends hourly check-ins written by Claude, chases deadlines, and reads through chat history to find anything that's been forgotten.

## What It Does

- **Hourly check-ins (9am–11pm)** — Claude-generated messages that summarise progress, call people out by name, and get more intense as the day goes on
- **Task tracking** — team members add tasks, set deadlines, and tick them off
- **Deadline alerts** — fires an alert when a task is within an hour of its deadline and still not done
- **Chat monitoring** — silently logs group messages and can analyse 2 weeks of history to find dropped commitments
- **Natural language Q&A** — @ the bot with any question and it answers using task data and chat history
- **Weekly reset** — everything clears on Monday for a fresh start

## Files

```
bot.py              — The bot (this is the only code file)
requirements.txt    — Python dependencies
data/               — Created automatically at runtime
  activities.json   — Task tracker data
  chat_log.json     — Chat message history
```

## Setup

### Prerequisites

- Python 3.10+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An Anthropic API key (from [console.anthropic.com](https://console.anthropic.com))
- A server that stays on 24/7 (e.g. PythonAnywhere Hacker plan at $5/month)

### 1. Create the Telegram Bot

Message [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts. Copy the API token.

Then send `/setprivacy` to BotFather, select your bot, and choose **Disable**. This allows the bot to read all group messages (required for chat logging and `/whatsoutstanding`).

### 2. Add the Bot to Your Group

Add the bot to your Telegram group. If your group has topics/forums, send a message in the specific topic you want the bot to post in.

### 3. Get Your Chat ID and Thread ID

Send a message in the group (or specific topic), then visit:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Find `"chat":{"id":-100XXXXXXXXXX}` — that's your chat ID.

If your group has topics, also find `"message_thread_id":N` — that's your thread ID.

### 4. Get an Anthropic API Key

Sign up at [console.anthropic.com](https://console.anthropic.com), add credit ($5 is plenty), and create an API key.

### 5. Install and Run

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="-100XXXXXXXXXX"
export TELEGRAM_THREAD_ID="4"              # omit if no topics
export ANTHROPIC_API_KEY="sk-ant-..."
export TZ="Europe/London"

python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Register with The Hound |
| `/addtask <text>` | Add a task |
| `/quicktask <task> \| <deadline>` | Add a task with a deadline in one go |
| `/done <number>` | Mark a task as complete |
| `/undone <number>` | Unmark a task |
| `/deadline <number> <when>` | Set a deadline on an existing task |
| `/update <text>` | Post a progress update |
| `/status` | View your own tasks and deadlines |
| `/check <name>` | Look up someone else's progress |
| `/teamstatus` | View everyone's tasks |
| `/whatsoutstanding` | Analyse 2 weeks of chat for forgotten items |
| `/settopic <text>` | Set the week's big-picture focus |
| `/help` | Show commands |
| `@botusername <question>` | Ask The Hound anything in plain English |

### Deadline Formats

All of these work: `10mins`, `2hours`, `1h30m`, `in 20 mins`, `tomorrow 3pm`, `friday 14:00`, `10/03 17:00`. If you skip the time, it defaults to 5pm.

## Hosting on PythonAnywhere

1. Sign up for the **Hacker plan** ($5/month) at [pythonanywhere.com](https://www.pythonanywhere.com)
2. Upload `bot.py` and `requirements.txt` to a directory (e.g. `~/Hound/`)
3. Open a Bash console and install dependencies: `pip install --user -r requirements.txt`
4. Create a run script:

```bash
cat > ~/Hound/run.sh << 'EOF'
#!/bin/bash
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="-100XXXXXXXXXX"
export TELEGRAM_THREAD_ID="4"
export ANTHROPIC_API_KEY="sk-ant-..."
export TZ="Europe/London"
cd ~/Hound
python3 bot.py
EOF
chmod +x ~/Hound/run.sh
```

5. Go to the **Tasks** tab and add an Always-On Task: `/home/YOURUSERNAME/Hound/run.sh`

## Hosting with systemd (Linux VPS)

Create `/etc/systemd/system/hound.service`:

```ini
[Unit]
Description=The Hound Telegram Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/hound
Environment=TELEGRAM_BOT_TOKEN=your-token
Environment=TELEGRAM_CHAT_ID=-100XXXXXXXXXX
Environment=TELEGRAM_THREAD_ID=4
Environment=ANTHROPIC_API_KEY=sk-ant-...
Environment=TZ=Europe/London
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable hound
sudo systemctl start hound
```

## Cost

Claude API usage is minimal. Each hourly message costs roughly $0.003–0.01. With 15 messages a day plus occasional `/whatsoutstanding` and @ mentions, expect around $0.10–0.30 per day.

## Customisation

- **Schedule hours** — edit the `CronTrigger(hour="9-23")` in `bot.py`
- **Timezone** — set the `TZ` environment variable
- **Personality** — edit `HOUND_SYSTEM_PROMPT` and `OUTSTANDING_SYSTEM_PROMPT` in `bot.py`
- **Chat history window** — change `CHAT_HISTORY_DAYS` (default 14)
- **Deadline check frequency** — edit the `IntervalTrigger(minutes=2)` in `bot.py`

## Security Note

Your `run.sh` contains API keys. If hosting on GitHub, **do not commit `run.sh`**. Add it to `.gitignore`:

```
run.sh
data/
```

Use environment variables or a `.env` file that stays out of version control.
