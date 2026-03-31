lemonde_today.py

A small script to fetch Le Monde's "actualité en continu" page and extract today's article headings and links. It can also fetch full text for free (non-subscriber) articles and store them in a local SQLite database.

Features

- Scrapes https://www.lemonde.fr/actualite-en-continu/ for articles published today.
- Detects whether an article is subscriber-only using nearby labels like "Article réservé".
- Optionally fetches the full text of free articles and stores them in an SQLite database (`articles.db` by default).
- Uses Selenium (headless Chrome) when available to bypass cookie consent/paywall walls; falls back to requests when not.

Files

```markdown
# News parser and Telegram approval helper

This workspace contains a small scraper and a lightweight Telegram approval workflow used to extract articles from Le Monde's "actualité en continu", translate/paraphrase them, and publish approved items to a Telegram channel after human review.

There are two main CLI scripts:

- `lemonde_today.py` — scraper + translation/paraphrase pipeline. Extracts today's articles, optionally fetches full text, translates and generates paraphrases, and (optionally) schedules Telegram approval messages.
- `send_last_paraphrase.py` — helper that sends the most recently created paraphrase to the configured admin for approval and runs a short-lived bot until approval is handled.
- `send_pending_news.py` — batch helper that finds all paraphrases that haven't been sent for approval (`sent_for_approval = 0`) and sends them concurrently to the admin chat; waits for approve/reject callbacks and posts approved items to the configured channel.

This README documents how to install, configure, and run these scripts, plus notes about the SQLite schema and troubleshooting tips.

## Requirements

- Python 3.10+ (the code has been developed against modern Python; features like `|` type unions are used in some helpers)
- Dependencies listed in `requirements.txt` (requests, beautifulsoup4, lxml, python-telegram-bot>=20, python-dotenv, optionally transformers/torch/sacremoses and selenium)

Installation (recommended virtualenv):

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Environment

Create a `.env` file in the project root (or export these variables in your environment). The Telegram helpers require:

- TELEGRAM_BOT_TOKEN — your bot token (from BotFather)
- TELEGRAM_ADMIN_ID — your Telegram user id (numeric)
- TELEGRAM_CHANNEL_ID — target channel id (numeric, may be negative for private channels)

Optional variables:
- PARAPHRASE_DB_PATH — path to the paraphrases DB (defaults to `paraphrased_translation.db`)
- LEMONDE_COOKIE — cookie string to bypass cookie/paywall walls when scraping

Example `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_ADMIN_ID=123456789
TELEGRAM_CHANNEL_ID=-1001234567890
PARAPHRASE_DB_PATH=paraphrased_translation.db
LEMONDE_COOKIE="name=value; name2=value2"
```

## Databases and schema

This project uses three SQLite databases by default:

- `articles.db` — article index and raw fetched text (created by `lemonde_today.py` when `--fetch` is used)
- `translation.db` — English translations of articles
- `paraphrased_translation.db` — paraphrased English text and approval state

Important tables (in `paraphrased_translation.db`):

- `paraphrases` — stores paraphrases with these relevant columns (existing + added columns):
  - id INTEGER PRIMARY KEY
  - translation_id INTEGER
  - title TEXT
  - url TEXT
  - paraphrased_text TEXT
  - approved INTEGER DEFAULT 0
  - sent_for_approval BOOLEAN DEFAULT 0

The code contains a safe migration that will add `approved` and `sent_for_approval` columns if they don't exist.

## Scripts

### 1) `lemonde_today.py`

Primary scraper/translation/paraphrase pipeline. Major behaviors:

- Scrapes Le Monde live page for today's articles and detects subscriber-only items.
- Optionally fetches article HTML and extracts clean text (`--fetch`).
- Translates articles with Helsinki-NLP `opus-mt-fr-en` (if transformers available).
- Paraphrases translations using `Vamsi/T5_Paraphrase_Paws` (if available).
- Inserts articles/translations/paraphrases into the respective SQLite DBs.
- Optionally schedules Telegram approval messages for new paraphrases (if `--telegram` passed or env vars present).

Usage examples:

```bash
# print today's index
python3 lemonde_today.py

# fetch full texts and run translation/paraphrase pipeline
python3 lemonde_today.py --fetch

# paraphrase-only (operate on existing translations)
python3 lemonde_today.py --paraphrase-only

# with Telegram approval (requires TELEGRAM_* env vars or flags)
python3 lemonde_today.py --paraphrase-only --telegram --telegram-token "$TELEGRAM_BOT_TOKEN" --telegram-admin-id "$TELEGRAM_ADMIN_ID" --telegram-channel-id "$TELEGRAM_CHANNEL_ID"
```

Notes:
- If Le Monde shows a cookie/paywall wall, pass `--cookie 'name=value; ...'` or set `LEMONDE_COOKIE`.
- The script is intentionally defensive: it checks for existing rows, handles DB integrity errors, and falls back to requests if Selenium is unavailable.

### 2) `send_last_paraphrase.py`

Helper to send the most recent paraphrase to the admin for approval and wait until an approval decision is made.

Behavior:

- Starts a short-lived python-telegram-bot Application in the main thread.
- Sends the most recent paraphrase (first chunk with inline ✅/❌ buttons, remaining chunks without buttons).
- Uses a robust httpx client configuration and attempts to prefer IPv4 on problematic networks.
- On approval, sends the paraphrase to the configured channel and marks the paraphrase as approved in the DB. Then stops the bot and exits.

Usage:

```bash
python3 send_last_paraphrase.py
```

This is useful for manual one-off approvals.

### 3) `send_pending_news.py`

Batch helper to send all paraphrases that have not yet been sent for approval. Key behaviors:

- Reads paraphrases where `sent_for_approval = 0` from `paraphrased_translation.db`.
- Sends them concurrently (async) to the admin chat with inline ✅/❌ buttons.
- Marks `sent_for_approval = 1` after a successful initial send to avoid duplicates on restarts.
- Waits for admin callbacks; on approve posts to the configured channel and marks `approved = 1`.
- On reject, no repost is made. Each item unblocks its waiting task after the admin action.
- Shuts down the bot and exits once all pending items are processed.

Usage:

```bash
python3 send_pending_news.py
```

Implementation notes:
- Messages are formatted in HTML: title is bold, the paraphrased text follows, then a separator `---` and a clickable link "🔗 Link to article". `translation.db` is consulted to find the canonical URL where possible.
- The script starts the Application properly (initialize/start + start_polling) so CallbackQueryHandler receives updates.
- The sending is concurrent (asyncio tasks + semaphore) and uses retries for transient Telegram errors.

## Troubleshooting & common issues

- If callbacks (Approve/Reject) don't trigger:
  - Ensure the bot is not configured with a webhook elsewhere (a webhook prevents polling from receiving updates).
  - The script must start polling (it does); if you see no callback activity, check that the bot token and chat IDs are correct and that the admin is interacting with the message in a private chat (or that bot privacy allows interaction in groups).

- If you see httpx/connect timeouts on macOS or in flaky networks:
  - The helper scripts include a scoped IPv4 preference and an httpx.AsyncClient configured with http2 disabled and trust_env=True to work better with some proxy/VPN setups.
  - Consider increasing connect/read timeouts in the code if you observe intermittent ConnectTimeouts.

- If the DB schema is missing the `sent_for_approval` column:
  - `init_paraphrase_db()` in `lemonde_today.py` will add the column if missing. You can also run the script once to apply the migration.

## Development notes and next steps

- You can add a `--dry-run` to `send_pending_news.py` to preview messages without sending.
- Consider moving `sent_for_approval` marking to occur only after a confirmed send if you need stronger guarantees (currently the code marks after successful send, but other helper code also marks before scheduling in some flows).
- Tests: adding a small test suite that runs against a temporary SQLite file would help avoid regressions in DB migrations.

## License

This code is provided without warranty. Use for personal and educational purposes.

```
