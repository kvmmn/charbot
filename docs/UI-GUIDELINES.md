# Telegram UI guidelines

This is the design contract for charbot's Telegram surface: the group chat
**X-Chaharsotoon**. It exists so a new contributor can add or change a
message without reading `charbot/bot.py` line by line. If code and this
document disagree, that's a bug — fix one to match the other in the same
change.

> **Governing rule: lists explain; cards ask; edits close the loop.**
>
> A read-only list never has buttons. A card with a keyboard asks about
> exactly one thing. Tapping a card's button edits that same message into a
> closed record — it never spawns a second "thanks" message.

This replaced an earlier design (one message with every overdue task's
buttons all stacked underneath it — 18 buttons under 5 tasks, nobody could
tell which button belonged to which task). Don't reintroduce it.

## 1. The medium's constraints

Telegram, not us, sets these limits. Design inside them, don't fight them:

- **Parse mode is HTML, and it is a small subset.** Only `<b>`, `<i>`,
  `<u>`, `<s>`, `<span class="tg-spoiler">`, `<a href>`, `<code>`, `<pre>`,
  and `<blockquote>` / `<blockquote expandable>` are recognized. Anything
  else prints literally. Always `html.escape()` user-generated text (task
  titles, transcript text) before interpolating it — a stray `<` or `&` in
  a title breaks the whole message's formatting, not just that field.
- **`<blockquote>` and `<blockquote expandable>` cannot nest**, in
  themselves or in each other. You cannot put a quoted card inside a quoted
  overflow section. That is why a card's own title/metadata is never
  quoted — blockquote is reserved for one flat block of overflow text
  (digest tail, weekly per-person detail), never wrapped per-item.
- **One keyboard per message, full stop.** `reply_markup` attaches to the
  message you send it with. There is no such thing as "the keyboard for
  task 3 out of the 5 tasks in this message" — Telegram has no concept of
  a button belonging to one paragraph of a longer text. If several items
  are each answerable, they need separate messages, each with its own
  keyboard directly underneath it.
- **`callback_data` must be ≤ 64 bytes.** Ours is `kind:choice:id`
  (e.g. `fu:done:101`) — comfortably short. Keep it that way; don't start
  embedding titles or free text in it.
- **Button labels should stay short.** `charbot/buttons.py` clips every
  label to ~32 characters (`_clip`). A label that wraps to two lines on a
  phone reads as clutter.
- **Rate limits are real.** Telegram throttles rapid `sendMessage` calls to
  the same chat; a burst without pacing gets you a `429`. Multi-message
  sends (full lists and the follow-up job) sleep `LIST_SEND_DELAY` /
  `FOLLOWUP_SEND_DELAY` seconds between sends, and only ever burst 1–3
  *active cards* before pacing kicks in.

## 2. The interaction model: digest → one active card → resolved edit

A shared group must never see a wall of simultaneous questions.

1. **Digest.** A read-only intro (type label + summary) plus **one message
   per person** orients everyone: what's outstanding, grouped by owner.
   No keyboard on any of these. Never stack every person under one long
   bubble — the eye loses the list when it grows.
2. **Active card.** Only the *next* item that genuinely needs an answer is
   sent as its own message with its own keyboard — never several tasks'
   keyboards merged into one message.
3. **Resolved edit.** When someone taps a button, that same message is
   *edited* — keyboard removed, replaced with who answered and what they
   chose. Nothing new is appended under it.
4. **Advance.** Right after the edit, if more items are waiting, the next
   one is sent as a fresh active card. This chains one-at-a-time until the
   batch is empty (`charbot.bot._advance_followup_queue`).

**Burst limits.** Sending more than one active card in the same breath is
only ever acceptable when each targets a *different named person* and
there are at most 3 of them — three different people each seeing their own
one-line question is fine; the same person seeing three questions at once,
or a pile of 4+, is not.

| Answerable items this run | What happens |
|---|---|
| 0 | No card. (Digest only, if there's anything to show at all.) |
| 1–3, all different named people | Send all of them, one message each, paced with a short delay. |
| 1–3, but repeating the same person | Sequential: send the first, queue the rest. |
| 4+ | Sequential: send the first, queue the rest (`_save_followup_queue`), one card at a time as each resolves. |

**Routine lists are always read-only** — no exceptions for length or
urgency. **Urgent overdue items are never buried in an expandable
blockquote** — collapsing is for history and low-priority remainder only;
the digest orders groups by urgency (most overdue person first) precisely
so the important thing is never the thing that got hidden.

Code: `charbot/bot.py` — `_run_followup_for_group` (digest + burst/queue),
`_send_active_card` (one task → one card), `_advance_followup_queue`
(edit → next card), `handle_callback`'s `td`/`fu` branch (the edit itself).

## 3. Hierarchy: fixed position, not decoration

Every message that isn't a bare one-line reply follows this shape, top to
bottom. Skip a line only if there is nothing to put there — never reorder:

```
1. <b>message type</b>      — bold, invariant per type, this is what recognizes the message
2. (blank line)
3. subject / title           — the one thing this message is about
4. body or list               — detail, if any
5. metadata, one natural line — due date, who, counts — never a "field: value · field: value" row
6. keyboard                   — only on an active card; never on a list
```

## 4. Recognition: the bold first line, not icons

There is no per-message-type emoji or color code to memorize. The first
bold phrase does the recognizing; the shape underneath does the rest.
Persian digits everywhere; no other icon system.

| First line | Shape | Used by |
|---|---|---|
| `<b>برنامهٔ امروز</b>` | Intro + one read-only message per person, no keyboard | `format_person_list_messages`, `bot.cmd_standup` |
| `<b>کارهای عقب‌افتاده</b>` / `<b>کارهای باز</b>` | Multi-person: intro + one message per person. Single-person («کارهای سامان»): one flat list. No keyboard | `format_person_list_messages`, `format_task_list` |
| `<b>عقب‌افتاده</b>` / `<b>موعد امروز</b>` / `<b>موعد فردا</b>` | Urgency bands: intro + one read-only message per person per band. Never merge bands; never a fourth «در حال دیر شدن» label | `charbot.jobs.urgency`, `format_person_list_messages` |
| `<b>پاسخ لازم</b>` | One direct question + one keyboard (the active card) | `format_active_card`, `format_task_question` |
| `<b>عقب‌افتاده</b>` (single alert) | Single-item alert: owner + consequence + suggested next action | `format_overdue_alert` |
| `<b>ثبت شد</b>` | Short edited confirmation, keyboard removed | `format_resolved`, `format_task_confirmation` |
| `<b>گزارش {period}</b>` | Totals first, per-person detail in one expandable blockquote | `report.format_period_report` |

No per-task emoji. No decorative emoji in reports or lists. At most one
consistent glyph, and only for alerts (none of our current alert copy
needs one — don't add one just to fill space).

## 5. Identity: the name is primary

The written **name** carries identity. The colored ring
(`charbot.formatting.PERSON_MARK`) is a *secondary* cue only — it is tiny,
inaccessible to anyone who hasn't memorized the legend, and never a
substitute for the name. Rules:

- A ring never appears without the name it belongs to, right next to it:
  `person_label()` returns `"🟣 غزل"`, never `"🟣"` alone.
- In a person-grouped digest, the ring+name pair appears **once**, in that
  person's section header. Items underneath it are not re-tagged with the
  ring — they're already inside that person's section.
- Status is a **word** (`عقب‌افتاده` / `امروز` / `بدون موعد` / `پیش‌رو`),
  never a second colored circle competing with the person ring for
  attention.
- Ghazal is not in X-Chaharsotoon; chase via Hamed (follow-up questions, @mentions, active cards). `assignee_key` stays `ghazal`. Completion reports from the chase contact or from Kawe can close the assignee's task.

## 6. Persian digits, Jalali dates, no "·"

- Every number a human reads — due dates, task counts, `کار N` references,
  timestamps — uses Persian digits (`charbot.formatting.to_fa_digits`).
  Latin digits inside RTL Persian text read badly.
- Dates are written in the **Jalali (Shamsi) calendar** by month name —
  `۷ شهریور`, not `29/8`. `charbot.formatting.jalali_label` does the
  Gregorian→Jalali conversion (self-contained, no extra dependency).
- No `·` (middle dot) as a field separator. It's visually weak in RTL, and
  it invites cramming unrelated fields into one noisy line. Write one
  natural Persian sentence instead (`غزل، موعد ۸ شهریور، ۳ روز
  عقب‌افتاده`), or put unrelated facts on separate lines.
- No bare `#12` anchor. Where a human genuinely needs the reference (e.g.
  disambiguating same-sounding tasks in `task_pick_buttons`), spell it out:
  `کار ۱۲: بررسی قرارداد`.

## 7. Progressive disclosure

`<blockquote expandable>` exists for one purpose: keep a message short
without deleting information. Rules:

- Used past ~6 cards/lines in a *single* message (`LIST_INLINE_MAX`) — not
  before. A 3-task list is never collapsed. Multi-person lists do not
  collapse later people: each person is already their own message.
- Reserved for the actual overflow *payload* (extra tasks under one person,
  per-person detail in a weekly report) — never for a type label or a
  metadata line. If everything is quoted, nothing stands out.
- Never used to hide the most urgent item. Person messages are ordered by
  urgency (most overdue person first); later people are separate messages,
  not an expandable tail.

## 8. Rendered examples

Real output from `charbot/formatting.py`, using the actual overdue set from
the incident this design replaced (غزل logo execution 29 Aug; حامد board
list 30 Aug; غزل Instagram videographer 1 Sep; محمدرضا Tuesday meeting
1 Sep; سامان Mashhad flight 1 Sep — "today" = 3 Sep 2026).

### Daily plan / multi-person list (`format_person_list_messages`, `bot.cmd_standup`)

Live send is **five Telegram messages** (intro + one per person). Joined
here only so the example is readable in one place:

```
<b>برنامهٔ امروز</b>

۵ کار برای ۴ نفر
```

```
<b>🟣 غزل — ۲ کار</b>
پیگیری از حامد
۱. اجرای سه لوگو — موعد ۷ شهریور
۲. قیمت فیلم‌بردار اینستاگرام — موعد ۱۰ شهریور
```

```
<b>🟢 حامد — ۱ کار</b>
۱. صورتجلسه هیئت مدیره — موعد ۸ شهریور
```

```
<b>🟡 محمدرضا — ۱ کار</b>
۱. جلسه سه‌شنبه — موعد ۱۰ شهریور
```

```
<b>🟠 سامان — ۱ کار</b>
۱. بلیط پرواز مشهد — موعد ۱۰ شهریور
```
No keyboard. If exactly one item needed a decision, cmd_standup follows
this with one active card (see below). With five, the periodic follow-up
job hands them out one at a time instead (section 2).

### Digest (`format_person_list_messages`, `followup_job`)

Same shape as the daily plan — intro, then one message per person — with
the type label `<b>کارهای عقب‌افتاده</b>`. Ghazal's message leads because
she owns the oldest overdue item. No keyboard on any of these messages.
`format_task_digest` exists only as a joined preview for tests.

### Task list answer (`format_task_list`, e.g. «کارهای سامان چی؟»)

```
<b>کارهای سامان</b>

۱. بلیط پرواز مشهد — موعد ۱۰ شهریور — 🟠 سامان
```
Read-only, same as every list. If the person also wants role info, that is
a *separate* follow-up message with its own keyboard (see
`bot.handle_natural_language`'s `ASK_WHICH` branch) — never buttons bolted
onto this list.

### Active card (`format_task_question` + `buttons.question_buttons`)

```
<b>پاسخ لازم</b>

حامد، از غزل بپرس: اجرای سه لوگو چه شد؟
موعد ۷ شهریور، ۵ روز عقب‌افتاده
```
Chase-via lives in the **question**, not a fourth type label. Meta is
absolute by Berlin calendar day: overdue keeps «N روز عقب‌افتاده»; due
today is «موعد امروز» (after ~16:00 Berlin: «موعد امروز، هنوز مانده»);
tomorrow is «موعد فردا» — never «۰ روز». Buttons (one row, labels are
answers to *this* question): `فرستادم` | `هنوز نه` | `فردا می‌فرستم`

### Resolved edit (`format_resolved`) — what the card above becomes on tap

```
<b>ثبت شد</b>

حامد: فرستادم
پاسخ امروز، ۱۷:۲۰
```
Keyboard gone. Same message — this is an edit, not a reply.

### Overdue alert (`format_overdue_alert`) — a single badly-late item

```
<b>عقب‌افتاده</b>

🟣 غزل — اجرای سه لوگو
موعد ۷ شهریور، ۵ روز عقب‌افتاده؛ تحویل جمعه عقب می‌افتد
پیشنهاد: همین امروز بفرست
```

### Weekly report (`report.format_period_report`)

```
<b>گزارش این هفته</b>

بازه ۳۱ اوت ۲۰۲۶ تا ۶ سپتامبر ۲۰۲۶
۷ انجام‌شده، ۳ مانده، ۲ عقب‌افتاده

<blockquote expandable>کاوه — انجام‌شده ۰، به‌موقع ۰، دیر ۰، باز ۰، عقب‌افتاده ۰
حامد — انجام‌شده ۲، به‌موقع ۲، دیر ۰، باز ۱، عقب‌افتاده ۰
سامان — انجام‌شده ۳، به‌موقع ۲، دیر ۱، باز ۲، عقب‌افتاده ۱
محمدرضا — انجام‌شده ۰، به‌موقع ۰، دیر ۰، باز ۱، عقب‌افتاده ۱
غزل — انجام‌شده ۲، به‌موقع ۲، دیر ۰، باز ۰، عقب‌افتاده ۰</blockquote>
محمدرضا این بازه انجام‌شده‌ای ندارد و کار باز یا عقب‌افتاده دارد — کم‌کاری احتمالی.
```
Totals are the subject line; the per-person breakdown is one expandable
blockquote, not five separate quoted cards.

## 9. Urgency bands (`charbot.jobs.urgency`)

Three absolute labels by Europe/Berlin calendar day — never rename due-today
to overdue until the day rolls, and never invent «در حال دیر شدن» as a type.
Late-afternoon pressure is a **meta** line (`موعد امروز، هنوز مانده`) plus
cadence, not a new band.

| Label | Rule | List | Active card |
|---|---|---|---|
| عقب‌افتاده | `due_date < today` | intro + one message per person | yes (oldest first) |
| موعد امروز | `due_date == today` | intro + one message per person | yes (after overdue) |
| موعد فردا | `due_date == tomorrow` | intro + one message per person | no (heads-up only) |

Send order: overdue → due today → due tomorrow. Empty bands send nothing.
Unowned open tasks with a due in a band still appear under بدون مسئول.
No due date → not in this job.

**Cards:** prefer one person-scoped list per non-empty band, then at most
**one** active card in flight. Escalate by resolving then sending the next
queued card — do not stack identical digests or three cards at once. Within
a band, finish one person's items before jumping (`order_tasks_for_cards`).
Ghazal cards address Hamed in the question; the bold type line stays one of
the three labels above.

**Don't:** color-code urgency; urgency icons per line; bury overdue in an
expandable; merge bands into one «فوری» mega-digest.

**Cadence (routines, weekday Berlin working hours):** overdue+today at
~10:00, 13:30, 16:30, 18:30; tomorrow heads-up once with the 16:30 or 18:30
run. See [ARCHITECTURE.md](ARCHITECTURE.md) §10.

## 10. Where this lives — don't build a keyboard anywhere else

**`charbot/formatting.py`** owns every rendered text shape: cards, lists,
digests, active-card text, resolved-edit text, alerts, and the shared
primitives (Persian digits, Jalali dates, expandable wrapping). **`charbot/
buttons.py`** owns every keyboard: it turns a question's own content into
2–4 chips (`question_buttons`) or turns a task list into pick-buttons
(`task_pick_buttons`) — always bound to *that* message's content, never a
frozen global menu.

**No other module may construct its own `InlineKeyboardMarkup` or invent
its own text layout for a task/list/report.** `charbot/bot.py` and
`charbot/report.py` call into these two modules; they do not lay out text
or wire callback data themselves. If you're tempted to build a one-off
keyboard inline in a handler, that's the signal to add a function to
`buttons.py` instead.

## 11. Checklist: adding a new message type

1. **Name it.** Pick the bold first line. Does it fit an existing shape
   (section 4)? If not, is a new shape truly warranted, or is this a
   variant of an existing one (e.g. a new alert flavor is still
   `format_overdue_alert`-shaped)?
2. **Read-only or active?** If it lists more than one thing, it's
   read-only — no keyboard, full stop. If it asks about exactly one thing,
   it's a card — one keyboard, built from that thing's own content.
3. **Write the renderer in `formatting.py`.** Follow the hierarchy
   (section 3): bold type line, blank, subject, body, one natural metadata
   line. No `·`. Persian digits via `to_fa_digits`. Dates via
   `jalali_label`. Escape any user-generated text with `html.escape`.
4. **If it can be long,** cap the inline count (reuse `LIST_INLINE_MAX` /
   `DIGEST_INLINE_MAX` or a type-appropriate constant) and wrap the
   overflow in one `wrap_expandable(...)` call — never nest another
   blockquote inside it, and never collapse the most urgent items.
5. **If it needs buttons,** add a builder to `buttons.py` that derives
   labels from the actual content in front of the user, keep
   `callback_data` to `kind:choice:id` (≤ 64 bytes), and clip labels
   (`_clip`, ~32 chars).
6. **If it's part of a batch** (several of these might need sending
   back-to-back), follow the digest → active card → resolved edit model
   (section 2): don't stack keyboards, don't burst more than 3, and page
   the rest through a queue instead of dumping them.
7. **Test it** in `tests/test_formatting.py` (pure rendering) and, if it's
   wired into a handler, `tests/test_presentation.py` (the handler sends
   the right text/keyboard to the right message). Assert the negative too:
   a list-shaped test should assert it has *no* keyboard.
8. **Update this document** — table in section 4, and a rendered example
   in section 8 — in the same change.

Cross-references: [ARCHITECTURE.md](ARCHITECTURE.md) for the runtime and
data model this presentation layer sits on top of;
[FOR-MANAGERS.md](FOR-MANAGERS.md) for the non-technical picture.

## 12. Scheduled-message invariant

Morning, urgency, follow-up, inbox, and weekly-report entrypoints live under `charbot.jobs`. They use the same formatting/report/buttons modules as live replies; no scheduled path may invent a second text or keyboard layout. Urgency CLI defaults to `--dry-run`.
