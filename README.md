# charbot

Telegram coordinator bot for **Chaharsotoon (4S)** — task board, assignments, and gentle follow-ups in the private group **X-Chaharsotoon**.

Bot: [@TheCharBot](https://t.me/TheCharBot) (display name: charbot)

## What it does

- Create, assign, set due dates, mark done, list open/overdue tasks
- `/standup` summary for the board (Kawe, Hamed, Saman, Mohammadreza)
- Natural-language task phrases in **English and Persian**
- Maps Telegram users to board members (`/whoami`, `/map`)
- Daily follow-up nudge for overdue/unowned work (not every message)
- SQLite persistence (survives restarts when `data/` or `/data` volume is mounted)

## Quick start (local polling)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN (from BotFather, never commit it)

python -m charbot.main --mode polling
```

## Add charbot to X-Chaharsotoon

1. In Telegram, open group **X-Chaharsotoon** → Add members → search **@TheCharBot** → add.
2. In [@BotFather](https://t.me/BotFather):
   - `/setprivacy` → choose **@TheCharBot** → **Disable** (bot must read group messages for NL task parsing).
3. Optional: make charbot a **group admin** if you want cleaner visibility of all messages (not strictly required with privacy disabled).
4. Send `/start` or `/help` in the group.
5. Each board member runs `/map Kawe` (or their name) once, or an admin replies to their message: `/map Hamed`.

### Get the group id

After adding the bot, send any message in the group and check logs, or forward a group message to [@userinfobot](https://t.me/userinfobot). Supergroups look like `-100xxxxxxxxxx`.

Set in env:

```bash
TELEGRAM_GROUP_ID=-100xxxxxxxxxx
```

When set, charbot refuses other chats.

## Commands

| Command | Example |
|---------|---------|
| `/task` | `/task Ship Q3 invoice template` |
| `/assign` | `/assign 3 Kawe` |
| `/due` | `/due 3 tomorrow` or `/due 3 2026-03-15` |
| `/done` | `/done 3` |
| `/open` | List open tasks |
| `/overdue` | List overdue tasks |
| `/standup` | Daily board summary |
| `/whoami` | Your Telegram → member mapping |
| `/map` | `/map Saman` or reply + `/map Mohammadreza` |
| `/help` | Full help |

Natural language examples: `task: Prepare contract`, `assign 2 Hamed`, `done 1`, `open tasks`, `تسک: ارسال فاکتور`.

## Environment variables

See [.env.example](.env.example). Required in production:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From BotFather — **never commit** |
| `TELEGRAM_GROUP_ID` | X-Chaharsotoon chat id |
| `BOT_MODE` | `polling` (dev) or `webhook` (prod) |
| `WEBHOOK_URL` | Public HTTPS base URL (webhook mode) |
| `DATABASE_PATH` | SQLite file path (use a mounted volume) |

Optional: `WEBHOOK_SECRET`, `TELEGRAM_ALLOWED_GROUP_IDS`, `FOLLOWUP_INTERVAL_HOURS`.

## Production (webhook)

### Docker

```bash
docker build -t charbot .
docker run -p 8081:8081 \
  -e TELEGRAM_BOT_TOKEN=... \
  -e TELEGRAM_GROUP_ID=... \
  -e BOT_MODE=webhook \
  -e WEBHOOK_URL=https://your-host.example \
  -v charbot-data:/data \
  charbot
```

Health: `GET /health` → `{"status":"ok","service":"charbot"}`  
Webhook: `POST /telegram/webhook`

### Fly.io

```bash
fly launch --no-deploy
fly volumes create charbot_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_GROUP_ID=... WEBHOOK_URL=https://<app>.fly.dev
fly deploy
```

After deploy, Telegram sends updates to `https://<app>.fly.dev/telegram/webhook`.

### Render

Use the Dockerfile or `Procfile` (`web: python -m charbot.main --mode webhook`), set env vars in the dashboard, add a persistent disk at `/data`, and point `WEBHOOK_URL` to your Render URL.

## Tests & CI

```bash
pip install -e ".[dev]"
ruff check charbot tests
pytest
```

CI runs on push/PR (lint + tests). Tests do not call Telegram.

## Database migration path

v1 uses SQLite at `DATABASE_PATH`. For a hosted DB later, replace `charbot/store.py` with a Postgres-backed implementation keeping the same method signatures — handlers stay unchanged.

## Security

- Do not commit `.env`, tokens, or private group invite links.
- Set `TELEGRAM_GROUP_ID` in production.
- Use `WEBHOOK_SECRET` so only Telegram can POST to your webhook.
