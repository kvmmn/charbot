# Security

charbot is a Telegram bot with access to a group chat and a database. Treat tokens and connection strings as production secrets.

## Never commit

- `.env` and any file with real tokens
- `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`
- Fly / CI deploy tokens
- Telegram group invite links (private `t.me` invite URLs)
- Screenshots or logs that embed secrets

`.gitignore` already excludes `.env`, `data/`, preview artifacts, and `fly.toml.local`.

## Runtime gating

- Restrict inbound traffic with `TELEGRAM_GROUP_ID` (and optional `TELEGRAM_ALLOWED_GROUP_IDS`).
- In webhook mode, set `WEBHOOK_SECRET` and verify Telegram’s secret-token header.
- Outbound jobs (`charbot.jobs.*`) refuse to send if no allowed groups are configured — there is no hard-coded production chat id fallback.

## Org / identity overrides

Public defaults (`example-org`, `Example Board`, empty `CHARBOT_TG_USERNAMES`) are generic on purpose. Production must set org/group/project env vars so Neon upserts keep matching existing rows. Telegram `@handles` belong in `CHARBOT_TG_USERNAMES`, not in source.

## Logging

- Application startup redacts Bot API token patterns from logs.
- Do not `print` or log `.env` contents, Fly secrets, or ASR/LLM API keys.
- Prefer structured errors without request bodies that may contain PII beyond what you already store intentionally.

## Data

- Prefer Neon (or another hosted Postgres) with least-privilege DB roles.
- SQLite (`DATABASE_PATH`) is for local/dev only.
- Voice transcripts and person facts are stored because the product needs them; do not mirror them into public issue trackers.

## Deploy surface

- One live Telegram delivery path: webhook **or** polling, never both on the same token.
- Keep `min_machines_running = 1` (or equivalent) so the webhook host is always up.
- Rotate BotFather tokens and webhook secrets if they leak; update Fly secrets without writing them into git.
