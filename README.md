lemonde_today.py

A small script to fetch Le Monde's "actualité en continu" page and extract today's article headings and links. It can also fetch full text for free (non-subscriber) articles and store them in a local SQLite database.

Features

- Scrapes https://www.lemonde.fr/actualite-en-continu/ for articles published today.
- Detects whether an article is subscriber-only using nearby labels like "Article réservé".
- Optionally fetches the full text of free articles and stores them in an SQLite database (`articles.db` by default).
- Uses Selenium (headless Chrome) when available to bypass cookie consent/paywall walls; falls back to requests when not.

Files

- `lemonde_today.py` - main script
- `requirements.txt` - Python dependencies (requests, beautifulsoup4, lxml, selenium, webdriver-manager)
- `.gitignore` - ignores `*.db`, virtualenv folders and caches

Installation

1) (Optional) Create and activate a virtual environment:

   python3 -m venv .venv
   source .venv/bin/activate

2) Install dependencies:

   python3 -m pip install -r requirements.txt

Usage

Basic: fetch and print today's article index (headlines, URLs, whether free):

   python3 lemonde_today.py

Fetch full text of free articles and store them in SQLite (default `articles.db`):

   python3 lemonde_today.py --fetch

Specify a custom database file:

   python3 lemonde_today.py --fetch --db-path my_articles.db

If the site blocks requests with a paywall/consent (HTTP 402) you can pass your browser Cookie header (copied from Developer Tools) via the `--cookie` flag or the `LEMONDE_COOKIE` environment variable:

   python3 lemonde_today.py --cookie 'name=value; name2=value2' --fetch

or

   export LEMONDE_COOKIE='name=value; name2=value2'
   python3 lemonde_today.py --fetch

Notes on Selenium

- Selenium + webdriver-manager are recommended because Le Monde may show a cookiewall that blocks non-browser requests.
- Selenium requires a browser (Chrome/Chromium) to be installed on your machine. webdriver-manager will download a compatible chromedriver automatically, but the Chrome binary must be present.
- If Selenium cannot start, the script falls back to requests and will likely receive HTTP 402 unless cookies are provided.

SQLite schema

The script creates an `articles` table with this schema:

- id INTEGER PRIMARY KEY AUTOINCREMENT
- title TEXT NOT NULL
- url TEXT NOT NULL UNIQUE
- published_at TEXT (HH:MM)
- text TEXT

Behavior

- When run with `--fetch`, the script will fetch all free articles, extract and clean text, then check the DB for duplicates (by URL or title). New articles are inserted; duplicates are skipped.
- Cleaning is heuristic: the script removes scripts/styles, tries to remove obvious ad/newsletter/related blocks, and extracts paragraphs from the article body. If you see artifacts or missing text, I can tune selectors for Le Monde's structure.

Troubleshooting

- If you get HTTP 402 (Payment Required): use Selenium or provide the Cookie header from your browser.
- If Selenium fails to start: ensure Chrome/Chromium is installed and up-to-date. You can also run with `--fetch` but set `LEMONDE_COOKIE` to a valid cookie value.
- If inserts to SQLite fail with IntegrityError: the script already checks for duplicates; duplicates are skipped.

Options for improvement

- Add `--no-headless` to see the real browser and manually accept cookie prompts.
- Persist DB connection across inserts for speed when inserting many articles.
- Export articles to JSON/CSV.

License

Use this script for personal or educational purposes as you like.
