# charbot for managers

This is the coordination bot for Chaharsotoon. It lives in the Telegram group **X-Chaharsotoon** as **@TheCharBot**. You do not need to know how servers work to use it.

## The idea in one sentence

Talk in the group as usual. The bot turns that talk into a clean work record (what, who, when) and answers in the same group.

## Picture of the service

![How charbot runs](charbot-workflow.png)

| Step | What you see | What actually happens |
|---|---|---|
| 1 | Someone speaks or writes in X-Chaharsotoon | Telegram sends the message to our bot |
| 2 | The bot is always on | A small Fly.io machine in Frankfurt receives it immediately (webhook), 24/7 |
| 3 | The bot understands messy speech | It removes filler (“I should…”, “save this as a task”), keeps the real title, owner, and deadline. If it is unsure, it asks — it should not invent |
| 4 | Nothing important is lost | People, roles, tasks, and messages sit in Neon (hosted Postgres), not on a laptop |
| 5 | A short card comes back | Lists show **title · owner · due** only. Extra detail is stored but not dumped into the group |

## How to talk to it

- Say the work in ordinary Persian. You do not have to use slash commands.
- If you describe work and then ask “did you get that?”, it should use the **previous** message.
- Voice notes are stored; transcription is part of the pipeline (best on a machine with speech models; production Fly is sized for chat, not large speech models).
- It will not keep asking Hamed or Saman for their titles. Those are already recorded.
- Company name is **چهارستون** (Chaharsotoon).

## What a task is

A task is four fields:

1. **Title** — the work, cleaned
2. **Owner** — who is responsible
3. **Deadline** — when it is due
4. **Description** — everything else (notify Hamed, Mashhad site, etc.)

If owner or deadline is missing, the bot should ask. It should not silently drop the work.

## What this is not

- Not a replacement for the board. It is the board’s memory in the group.
- Not running on anyone’s laptop in production. That was the old, fragile path.
- Not storing secrets in GitHub.

## When something looks “deaf”

Telegram delivers to **one** live process. Production is the Fly webhook. A laptop poller must stay off, or the two fight and messages go missing. Health check: [https://chaharsotoon-charbot.fly.dev/health](https://chaharsotoon-charbot.fly.dev/health) should return `ok`.
