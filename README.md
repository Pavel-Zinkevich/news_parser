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
# news_parser

Small toolkit to scrape Le Monde's "actualité en continu", translate/paraphrase free articles, and provide a Telegram-based approval workflow that posts approved paraphrases to a channel.

This README explains the main scripts, configuration, databases, and common troubleshooting steps.

## What this repository contains

- `lemonde_today.py` — main scraper + translation + paraphrase pipeline. Can: list today's articles, fetch full text (`--fetch`), translate to English, generate paraphrases, and optionally schedule Telegram approval messages.
- `send_pending_news.py` — batch sender: finds paraphrases that haven't been sent for approval and sends them to the configured admin with inline Approve/Edit/Reject buttons; waits for admin decisions and posts approved items to the channel.
- `send_last_paraphrase.py` — convenience script to send the most recent paraphrase for manual approval.
- SQLite DBs created/used at runtime: `articles.db`, `translation.db`, `paraphrased_translation.db` (default paths).

## Requirements

- Python 3.10+
- See `requirements.txt` for the Python dependencies. Optional extras:
  - `transformers` (+ `torch`) to run translation/paraphrase models locally
  - `selenium` + `webdriver-manager` to use headless Chrome for fetching pages that require JS

Install in a virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Configuration / Environment

Create a `.env` file in the project root (or export environment variables). The Telegram scripts expect these variables when used:

- `TELEGRAM_BOT_TOKEN` — bot token from BotFather
- `TELEGRAM_ADMIN_ID` — numeric Telegram user id of the approver
- `TELEGRAM_CHANNEL_ID` — numeric id of the channel to post approved items (negative for private channels)

Optional environment variables:

- `PARAPHRASE_DB_PATH` — path to paraphrase DB (defaults to `paraphrased_translation.db`)
- `LEMONDE_COOKIE` — cookie header string to bypass cookie/paywall walls when scraping

Example `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_ADMIN_ID=123456789
TELEGRAM_CHANNEL_ID=-1001234567890
PARAPHRASE_DB_PATH=paraphrased_translation.db
LEMONDE_COOKIE="name=value; name2=value2"
```

Security note: do not commit `.env` to version control. If you accidentally pushed it to a remote, remove it from history and rotate secrets.

## Databases (default)

By default the scripts use three SQLite files in the project root:

- `articles.db` — article index and raw fetched text (created when running `lemonde_today.py --fetch`)
- `translation.db` — English translations of articles
- `paraphrased_translation.db` — paraphrased English text, edited versions, and approval flags

Important tables/columns (summary):

- `translations` (in `translation.db`): `id`, `article_id`, `title`, `url`, `translated_text`
- `paraphrases` (in `paraphrased_translation.db`): `id`, `translation_id`, `title`, `url`, `paraphrased_text`, `approved` (0/1), `sent_for_approval` (0/1)
- `edited_paraphrases` (in `paraphrased_translation.db`): stores admin edits with `original_id`, `edited_title`, `edited_text`

The code includes migration logic (safe ALTERs / CREATE TABLE IF NOT EXISTS) to add missing columns/tables where practical.

## Common usage

1) List today's index (no fetching):

```bash
python3 lemonde_today.py
```

2) Fetch full text for free (non-subscriber) articles, translate & paraphrase:

```bash
python3 lemonde_today.py --fetch
```

3) Translate only: translate articles that are present in `articles.db` but missing in `translation.db`:

```bash
python3 lemonde_today.py --translate
```

4) Paraphrase-only flow (process existing translations):

```bash
python3 lemonde_today.py --paraphrase-only
```

5) Send pending paraphrases to admin for approval (batch):

```bash
python3 send_pending_news.py
```

6) Send the most recent paraphrase for manual approval:

```bash
python3 send_last_paraphrase.py
```

Telegram options: you can pass Telegram values via CLI flags on `lemonde_today.py` (e.g. `--telegram --telegram-token ... --telegram-admin-id ...`) or set the corresponding env vars.

## Title/subtitle handling

Translations now treat an article heading and subtitle separately. When present the code stores the translated title and the translated subtitle on a separate line (title ends with a period). This prevents title/subtitle from being merged into a single sentence during translation.

If you prefer a separate `subtitle` column instead of a newline in the `title` field, I can add a migration to introduce that column.

## Troubleshooting

- Payment wall / 402 responses: some articles are subscriber-only and the site may respond with HTTP 402. To fetch paywalled content you must supply an authenticated session cookie. Use `--cookie 'name=value; ...'` or set `LEMONDE_COOKIE`.
- Selenium errors: if headless Chrome fails to start, the scripts fall back to `requests` HTML fetch. Ensure `selenium` and `webdriver-manager` are installed and Chrome is available if you need JS rendering.
- Telegram callback issues: make sure your bot isn't configured with a webhook and that you run the scripts on a machine with network access. The scripts use polling (python-telegram-bot v20+ lifecycle) to receive callback updates.
- Database schema errors: migration code is defensive; if you see errors, open the DB with `sqlite3` and inspect the tables. Back up DBs before manual schema changes.

## Developer notes and next steps

- Consider adding unit tests for the title/subtitle splitter and small DB migration tests.
- Consider adding a `--dry-run` mode to `send_pending_news.py` to preview posted content without sending it.

## Removing a committed `.env` (if you accidentally committed secrets)

If you committed `.env` to your repo, remove it from the index and rotate secrets:

```bash
git rm --cached .env
git commit -m "Remove .env from index"
git push
# Rotate any secrets (Telegram token) that may have been exposed
```

If the file was pushed previously and you need to remove it from history, use `git filter-repo` or `git filter-branch` (these rewrite history and require force-push).

## License

This code is provided as-is for personal and educational use. No warranty.
