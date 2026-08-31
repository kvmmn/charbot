# Architecture

## Runtime

```
Telegram  --HTTPS POST /telegram/webhook-->  Fly (fra)  --SQL-->  Neon Postgres
                 |                                |
                 |                                +-- GET /health
                 +-- secret header WEBHOOK_SECRET
```

- App: `chaharsotoon-charbot` (`fly.toml`), `BOT_MODE=webhook`, `min_machines_running = 1`, `auto_stop_machines = false`.
- Entry: `charbot.main` (FastAPI + python-telegram-bot). Startup registers the webhook; it must **not** drop pending updates.
- Polling (`python -m charbot.main --mode polling`) is local/dev only. Never run it against the production bot token while the webhook is active.

## Understanding path

`charbot/understand.py` extracts a task from colloquial Persian:

- Strip filler
- Title / description / assignee / due
- `confidence` + optional `ask` when required fields are missing
- Optional LLM if `CHARBOT_LLM_BASE_URL` and `CHARBOT_LLM_API_KEY` (or `OPENAI_API_KEY`) are set

`charbot/nlp.py` still parses dates and commands; create-task titles go through `extract_task`. `charbot/bot.py` uses the previous human message when the current line is “did you get that?” / “save this”.

## Data (Neon)

Schemas (see `schema.sql`):

| Schema | Contents |
|---|---|
| `identity` | organizations, groups, people, roles, memories, events |
| `work` | projects (SHEY), tasks, assignees, task events |
| `comms` | messages, message_media, lessons |
| `ops` | settings, migrations |

Public views keep older names working. `search_path`: `identity, work, comms, ops, public`.

A task list never prints description. Formatter: `charbot/formatting.py` (Telegram HTML `<blockquote>` cards).

## Voice

`charbot/voice.py`: download → transcribe → store on the person + message row → answer from transcript. The Fly image **does not** install `faster-whisper` (too large; it also blocked health checks). Live ASR uses an OpenAI-compatible HTTP endpoint (`CHARBOT_LLM_BASE_URL` + `CHARBOT_LLM_API_KEY` or `OPENAI_API_KEY`). If ASR is unavailable the group gets «نتونستم صدا را بنویسم.» Logs redact Telegram bot tokens.

## Backup (Grok)

Weekday routines may restart a dead process and drain **Neon unprocessed** rows. They must **never** call Telegram `getUpdates` (that steals the webhook/polling stream).

## Secrets

| Name | Where |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Fly secret / local env file, never git |
| `DATABASE_URL` | Fly secret / local `.env` |
| `WEBHOOK_SECRET` | Fly secret |
| `FLY_API_TOKEN` | Operator machine / CI, never git |

`.dockerignore` excludes `.venv`, `.git`, `.env`, and `data/`.
