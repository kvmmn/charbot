# Deploy

This guide assumes Fly.io + Neon Postgres. Other hosts work if they can run the Docker image, expose HTTPS, and hold secrets.

## Prerequisites

- A Telegram bot token from BotFather
- The numeric id of your Telegram group (`TELEGRAM_GROUP_ID`)
- A Neon (or Postgres) connection string
- `fly` CLI authenticated for your org

Do **not** commit secrets. Set them on the host.

## App name

Tracked `fly.toml` uses the placeholder:

```toml
app = "your-charbot-app"
```

Deploy with an explicit app name:

```bash
fly deploy --remote-only --app <your-real-app> --ha=false
```

See `fly.toml.example` for documented placeholders.

## Secrets (production parity)

After a public-default sanitization, **existing** Neon data keeps working only if production sets the same org/group/project identifiers the DB already has, for example:

```bash
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  DATABASE_URL=... \
  TELEGRAM_GROUP_ID=... \
  WEBHOOK_URL=https://<your-app>.fly.dev \
  WEBHOOK_SECRET=... \
  CHARBOT_ORG_SLUG=<existing-org-slug> \
  CHARBOT_ORG_NAME=<existing-org-name> \
  CHARBOT_GROUP_TITLE=<existing-group-title> \
  CHARBOT_GROUP_CHAT_ID=<existing-chat-id> \
  CHARBOT_PROJECT_SLUG=<existing-project-slug> \
  CHARBOT_PROJECT_NAME=<existing-project-name> \
  CHARBOT_TG_USERNAMES=kawe:<handle>,hamed:<handle>,saman:<handle>,mohammadreza:<handle> \
  CHARBOT_ROLE_KAWE=<existing-seed-or-leave-default> \
  CHARBOT_ROLE_HAMED=<existing-seed-or-leave-default> \
  CHARBOT_NOTE_GHAZAL=<existing-seed-or-leave-default> \
  --app <your-real-app>
```

Optional ASR keys: `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `CHARBOT_ASR_MODEL`.

Changing `CHARBOT_ORG_SLUG` without matching Neon creates a **new** org row; it does not wipe old rows, but the app will look at the new slug. Keep production slug aligned with existing data.

## Webhook mode

`BOT_MODE=webhook` (set in `fly.toml` `[env]`). On boot, the process registers the webhook and must not drop pending updates.

Health check: `GET /health` → JSON ok.

## Local vs production

| | Local | Production |
|---|---|---|
| Mode | `polling` | `webhook` |
| DB | SQLite or Neon | Neon |
| Secrets | `.env` (gitignored) | Fly secrets |
| Polling + webhook | Never both on one token | Webhook only |

## Jobs

External schedulers invoke package entrypoints (same env as the web process), for example:

```bash
python -m charbot.jobs.standup
python -m charbot.jobs.urgency --dry-run
python -m charbot.jobs.followup
python -m charbot.jobs.inbox
python -m charbot.jobs.weekly_report
```

Jobs require `TELEGRAM_GROUP_ID` (or `TELEGRAM_ALLOWED_GROUP_IDS`). There is no hard-coded chat-id fallback.

## Checklist

1. Secrets set on Fly (not in git)
2. `fly deploy --app <real>`
3. `/health` returns ok
4. Send a message in the allowed group; confirm a reply
5. Leave laptop pollers off
