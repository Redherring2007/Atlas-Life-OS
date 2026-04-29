# Atlas Life OS

Atlas Life OS is a Telegram-based personal operating system for capturing tasks, reminders, payment follow-ups, lead follow-ups, contract actions, and personal reminders from text messages or voice notes.

It uses Telegram long polling, Neon Postgres for storage, `dateparser` for local parsing, `faster-whisper` for local voice transcription, and optionally OpenAI for stricter task extraction when `OPENAI_API_KEY` is present.

## Features

- Text message to parsed task
- Voice note download, ffmpeg conversion, local Whisper transcription, and parsed task
- Neon Postgres task storage
- Automatic database schema setup on app startup
- One-time reminders using `reminder_sent`
- Commands: `/start`, `/help`, `/tasks`, `/today`, `/overdue`, `/done <n>`, `/delete <n>`
- No webhook, domain, or paid service required for the default MVP path

## Environment Variables

Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=123456:replace-with-your-token
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
OPENAI_API_KEY=
WHISPER_MODEL_SIZE=tiny
REMINDER_CHECK_SECONDS=60
```

Required:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`

Optional:

- `OPENAI_API_KEY`: enables OpenAI JSON parsing. If missing, rate-limited, or failing, Atlas Life OS uses the local fallback parser.
- `WHISPER_MODEL_SIZE`: defaults to `tiny`.
- `REMINDER_CHECK_SECONDS`: defaults to `60`.

## Create a Telegram Bot

1. Open Telegram and message `@BotFather`.
2. Send `/newbot`.
3. Follow the prompts for name and username.
4. Copy the bot token into `TELEGRAM_BOT_TOKEN`.

## Neon Setup

1. Create a free Neon project.
2. Copy the pooled or direct connection string into `DATABASE_URL`.
3. Make sure the connection string includes `sslmode=require`.

Atlas Life OS creates the required `tasks` table and indexes automatically on startup. You can also run `schema.sql` manually in the Neon SQL editor if you want to verify or repair the schema yourself.

Your database URL contains credentials. Keep it private and do not expose it in frontend code.

## Install ffmpeg Locally

Voice notes require `ffmpeg`.

Linux:

```sh
sudo apt-get update
sudo apt-get install ffmpeg
```

macOS:

```sh
brew install ffmpeg
```

Windows:

```powershell
winget install Gyan.FFmpeg
```

After installing on Windows, restart the terminal so `ffmpeg` is available on `PATH`.

## Local Python Run

Create and activate a virtual environment, then install dependencies:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

You can also use:

```sh
sh start.sh
```

or on Windows:

```bat
start.bat
```

## Docker Run

Create `.env`, then run:

```sh
docker-compose up --build
```

The Docker image installs `ffmpeg` and runs `python bot.py`.

## Railway Deployment

Railway deployment is optional. This project includes:

- `runtime.txt`
- `nixpacks.toml`
- start command: `python bot.py`

Set the same environment variables in Railway. No webhook domain is required because the bot uses long polling.

On startup, Atlas Life OS ensures the database schema exists before it begins processing Telegram updates. If voice notes transcribe but fail to save, check that `DATABASE_URL` points to the intended Neon database and that the latest deployment includes the startup schema fix.

## Voice Transcription

Telegram voice notes arrive as OGG/Opus files. Atlas Life OS downloads the file, converts it to 16 kHz mono WAV with `ffmpeg`, and transcribes it locally with `faster-whisper`.

The default model is `tiny` to keep CPU and memory use low. Larger models can improve accuracy but need more resources.

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

- Reminder times are stored and displayed in UTC.
- The fallback parser is keyword-based and intentionally simple.
- Local Whisper transcription can be slow on small CPUs.
- Neon free tier limits apply.
- Telegram long polling works well for an MVP but should run as a single bot process.
