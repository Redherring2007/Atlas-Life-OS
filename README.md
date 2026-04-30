# Atlas Life OS

Atlas Life OS is a Telegram-based personal operating system for capturing tasks, reminders, payment follow-ups, lead follow-ups, contract actions, parking location, and personal reminders from text messages or voice notes.

It uses Telegram long polling, a Telegram Mini App, Neon Postgres for storage, `dateparser` for local parsing, `faster-whisper` for local voice transcription, and optionally OpenAI for stricter task extraction when `OPENAI_API_KEY` is present.

## Features

- Text message to parsed task
- Voice note download, ffmpeg conversion, local Whisper transcription, and parsed task
- Telegram Mini App dashboard at `/app`
- Per-user local timezone detection from shared Telegram location or Mini App parking location
- Clean task cards with local due times
- App-style Telegram buttons for current tasks, due today, mark done, and remind again in 20 minutes
- Parking location with bay number and directions link
- Neon Postgres task, parking, timezone, and access-control storage
- Automatic database schema setup on app startup
- One-time reminders using `reminder_sent`
- Commands: `/start`, `/home`, `/help`, `/tasks`, `/today`, `/overdue`, `/done <n>`, `/delete <n>`

## Environment Variables

Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=123456:replace-with-your-token
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
OPENAI_API_KEY=
WHISPER_MODEL_SIZE=small
WHISPER_LANGUAGE=en
REMINDER_CHECK_SECONDS=60
LOCAL_TIMEZONE=Asia/Dubai
MINI_APP_URL=https://your-railway-domain.up.railway.app
PORT=8000
```

Required:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`

Optional:

- `OPENAI_API_KEY`: enables OpenAI JSON parsing. If missing, rate-limited, or failing, Atlas Life OS uses the local fallback parser.
- `WHISPER_MODEL_SIZE`: defaults to `small`.
- `WHISPER_LANGUAGE`: defaults to `en`.
- `REMINDER_CHECK_SECONDS`: defaults to `60`.
- `LOCAL_TIMEZONE`: fallback timezone before a user shares location. Defaults to `Asia/Dubai`.
- `MINI_APP_URL`: public HTTPS URL for this service. Set it to the Railway public domain to enable the Telegram Mini App button.
- `PORT`: web server port. Railway sets this automatically.

## Create a Telegram Bot

1. Open Telegram and message `@BotFather`.
2. Send `/newbot`.
3. Follow the prompts for name and username.
4. Copy the bot token into `TELEGRAM_BOT_TOKEN`.

## Mini App Setup

1. Deploy this service to Railway and generate a public domain.
2. Set `MINI_APP_URL` to that public domain, for example `https://atlas-life-os.up.railway.app`.
3. Redeploy.
4. Open `/home` in Telegram. Atlas shows an `Open Atlas App` button when `MINI_APP_URL` is set.

The Mini App is served at `/app`. API requests validate Telegram Mini App signed init data before reading or writing user data.

## Neon Setup

1. Create a free Neon project.
2. Copy the pooled or direct connection string into `DATABASE_URL`.
3. Make sure the connection string includes `sslmode=require`.

Atlas Life OS creates the required tables automatically on startup. This includes `tasks`, `user_settings`, `user_access`, and `parking_locations`.

Your database URL contains credentials. Keep it private and do not expose it in frontend code.

## Railway Deployment

This project includes:

- `runtime.txt`
- `nixpacks.toml`
- start command: `python app.py`

Set the same environment variables in Railway. No webhook domain is required because the bot uses long polling. The same service also serves the Mini App over HTTPS.

On startup, Atlas Life OS ensures the database schema exists, sets the Telegram command menu, optionally sets the Telegram Mini App menu button, and then starts reminders.

## Access Control

Users are auto-added to `user_access` the first time they interact with Atlas. Access defaults to on.

View users:

```sql
select *
from user_access
order by last_seen_at desc;
```

Disable a user:

```sql
update user_access
set access_enabled = false
where telegram_user_id = 'USER_ID_HERE';
```

Enable a user:

```sql
update user_access
set access_enabled = true
where telegram_user_id = 'USER_ID_HERE';
```

Disabled users cannot use commands, text, voice, location, buttons, Mini App APIs, or reminders.

## Task App Flow

Open `/home` to see the main Atlas Life OS screen with counts and buttons. You do not need a button for new tasks: speak or type into Telegram and Atlas will capture it.

Telegram does not expose timezone silently. When a user shares location once from the `Update local time` button, Atlas detects and stores that user's timezone in Neon. After that, parsing, task lists, due-today queries, and reminders use that user's local time automatically.

Saved tasks are read back as a clean card with the tidied title and local due time. Categories and priorities are kept internally but not shown under the task.

When a reminder is due, Atlas sends a reminder card with buttons to mark it done or remind again in 20 minutes.

## Voice Transcription

Telegram voice notes arrive as OGG/Opus files. Atlas Life OS downloads the file, converts it to 16 kHz mono WAV with `ffmpeg`, and transcribes it locally with `faster-whisper`.

Temporary voice files are deleted in a `finally` block after each transcription attempt.

## Parser Behavior

`parse_task` is a single async parser function. If `OPENAI_API_KEY` exists, the app asks OpenAI for strict JSON with:

- `title`
- `due_at`
- `category`
- `priority`

If OpenAI is missing, rate-limited, or fails, the fallback parser still saves the task. It uses `dateparser` for due dates and keyword rules for category and priority.

Tasks without due dates are saved with `due_at = null`.

## Task Numbering

`/tasks` displays numbered pending tasks ordered by:

```text
due_at ASC NULLS LAST, created_at DESC
```

`/done <n>` and `/delete <n>` use that same ordering and never expose database IDs.

## Known Limitations

- Telegram requires the user to share location once before Atlas can know that user's timezone.
- The Mini App must be opened from Telegram so the API can validate signed init data.
- The fallback parser is keyword-based and intentionally simple.
- Local Whisper transcription can be slow on small CPUs.
- Neon free tier limits apply.
- Telegram long polling works well for an MVP but should run as a single bot process.
