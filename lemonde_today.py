#!/usr/bin/env python3
"""
Fetch https://www.lemonde.fr/actualite-en-continu/ and list all article headings and links
published today, plus whether each article is subscriber-only.

Usage: python3 lemonde_today.py
"""

import re
import sys
import os
import argparse
import sqlite3
import datetime
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import time
import threading
import asyncio
import json

# telegram imports (optional)
try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes
    TELEGRAM_AVAILABLE = True
except Exception:
    TELEGRAM_AVAILABLE = False

# Globals for telegram app
TELEGRAM_APP = None
TELEGRAM_LOOP = None
TELEGRAM_THREAD = None
TELEGRAM_ADMIN_ID = None
TELEGRAM_CHANNEL_ID = None

# Optional selenium support
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

# Optional transformers (for translation)
try:
    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
    TRANSFORMERS_AVAILABLE = True
except Exception:
    TRANSFORMERS_AVAILABLE = False

# Optional torch (transformers backend) and sacremoses for faster tokenization
try:
    import torch
    TORCH_AVAILABLE = True
except Exception:
    TORCH_AVAILABLE = False

try:
    from sacremoses import MosesTokenizer
    SACREMOSES_AVAILABLE = True
except Exception:
    SACREMOSES_AVAILABLE = False

# Constants
BASE_URL = "https://www.lemonde.fr/actualite-en-continu/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

TODAY = date.today()
TODAY_STR = TODAY.strftime("%Y/%m/%d")

APOSTROPHE_VARIANTS = ["aujourd'hui", "aujourd’hui", "aujourd’", "aujourd"]

SUBSCRIBER_KEYWORDS = [
    "abonné", "abonnés", "réservé aux abonnés", "pour les abonnés", "payant", "premium"
]


def find_pub_text(tag) -> str:
    """Find a publication snippet (e.g. 'Publié aujourd'hui à 12h35') inside the given tag."""
    if tag is None:
        return ""
    try:
        txt = tag.get_text(" ", strip=True)
    except Exception:
        txt = str(tag)
    m = re.search(r"Publié\s+[^\n]+", txt, re.IGNORECASE)
    if m:
        return m.group(0)
    for v in APOSTROPHE_VARIANTS:
        if v.lower() in txt.lower():
            return txt
    return ""


def is_subscriber_only(txt: str) -> bool:
    if not txt:
        return False
    t = txt.lower()
    return any(k in t for k in SUBSCRIBER_KEYWORDS)


def gather_articles(html: str, base: str = BASE_URL):
    soup = BeautifulSoup(html, "lxml")

    results = []
    seen = set()

    # Find candidate links: most article links are <a href="...">Title</a>
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # Skip anchors and javascript
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        # Normalize absolute URL
        url = urljoin(base, href)

        # Basic heuristics: accept links that look like article pages
        if not re.search(r"/\d{4}/\d{2}/\d{2}/|/article/|/live/", url):
            # skip links that very likely aren't articles
            continue

        title = a.get_text(separator=" ", strip=True)
        if not title:
            # maybe the link has an <h3> or <strong> inside
            continue

        # find a nearby ancestor that contains publication info
        pub_text = ""
        parent = a
        for _ in range(6):
            parent = parent.parent
            if parent is None:
                break
            candidate = find_pub_text(parent)
            if candidate:
                pub_text = candidate
                break

        # If pub_text empty, try to find a sibling or next text node
        if not pub_text:
            # search within the next 3 siblings
            sib = a
            for _ in range(6):
                sib = sib.next_sibling
                if sib is None:
                    break
                try:
                    raw = getattr(sib, "get_text", lambda **kw: str(sib))(separator=" ", strip=True)
                except Exception:
                    raw = str(sib)
                if raw and "Publié" in raw:
                    pub_text = raw
                    break

        # Decide if the article is from today
        from_today = False
        # 1) URL contains today's YYYY/MM/DD
        if TODAY_STR in url:
            from_today = True
        # 2) pub_text contains "aujourd" (aujourd'hui variants)
        if pub_text:
            if any(v.lower() in pub_text.lower() for v in APOSTROPHE_VARIANTS) or re.search(r"Publié\s+le\s+\d{1,2}\s+[A-Za-zéû-]+\s+\d{4}", pub_text, re.IGNORECASE):
                # If it explicitly says aujourd'hui or a full date, keep
                if "aujourd" in pub_text.lower() or TODAY_STR in url:
                    from_today = True
                else:
                    # If it says a date, try to parse a year-month-day in the string
                    if re.search(r"\b\d{4}/\d{2}/\d{2}\b", pub_text):
                        if TODAY_STR in pub_text:
                            from_today = True
        # If neither method matched, skip
        if not from_today:
            continue

        # subscription-only detection
        # check the link text + nearby text
        nearby_text = " ".join(filter(None, [title, pub_text]))
        subscriber = is_subscriber_only(nearby_text)

        key = (title, url)
        if key in seen:
            continue
        seen.add(key)

        results.append({"title": title, "url": url, "pub_text": pub_text, "subscriber_only": subscriber})

    return results


def _extract_time_from_pub_text(pub_text: str) -> str:
    """Extract HH:MM (24h) from a publication snippet like 'Publié aujourd'hui à 12h35'"""
    if not pub_text:
        return ""
    m = re.search(r"(\d{1,2}h\d{2}|\d{1,2}h)", pub_text)
    if not m:
        return ""
    t = m.group(1)
    t = t.replace("h", ":")
    if ":" not in t:
        t = f"{t}:00"
    # normalize to HH:MM
    parts = t.split(":")
    hh = parts[0].zfill(2)
    mm = parts[1].zfill(2) if len(parts) > 1 else "00"
    return f"{hh}:{mm}"


def extract_date_from_url(url: str) -> str:
    """Extract YYYY-MM-DD from a URL containing /YYYY/MM/DD/.

    Returns empty string if not found.
    """
    if not url:
        return ""
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"


def _clean_text(soup: BeautifulSoup) -> str:
    # remove scripts, styles and common ad/subscribe blocks
    for sel in soup(["script", "style", "noscript", "iframe"]):
        try:
            sel.decompose()
        except Exception:
            pass

    # remove elements likely to be ads, newsletters, related, share buttons
    patterns = ["advert", "publi", "newsletter", "related", "share", "social", "cookie", "consent", "aside", "comments", "subscribe"]
    for tag in soup.find_all(True):
        # Access attributes safely; some Tag objects may have .attrs == None in edge cases
        attrs = getattr(tag, "attrs", {}) or {}
        classes = attrs.get("class") or []
        cl = " ".join(classes).lower() if classes else ""
        idv = (attrs.get("id") or "").lower()
        if any(p in cl for p in patterns) or any(p in idv for p in patterns):
            try:
                tag.decompose()
            except Exception:
                pass

    # Prefer articleBody or main article tags
    body = None
    # itemprop="articleBody"
    body = soup.find(attrs={"itemprop": "articleBody"})
    if body is None:
        body = soup.find("article")
    if body is None:
        # look for common classes
        candidates = soup.find_all(["div", "section"], class_=re.compile(r"(article__content|article-body|article-content|content|contenu|corps|body)", re.I))
        if candidates:
            # pick the largest candidate
            body = max(candidates, key=lambda x: len(x.get_text(" ", strip=True)))
    if body is None:
        # fallback to entire page
        body = soup

    paragraphs = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    if not paragraphs:
        # fall back to text of body
        txt = body.get_text(" ", strip=True)
    else:
        txt = "\n\n".join(paragraphs)

    # collapse multiple newlines and spaces
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    txt = txt.strip()
    return txt


def process_free_articles(articles, use_selenium=False):
    """Given the list returned by gather_articles, fetch full text for non-subscriber articles.

    Returns articles_data list of dicts:
    {
        "title": ..., "url": ..., "published_at": "HH:MM", "text": ...
    }
    """
    articles_data = []

    free_articles = [a for a in articles if not a.get("subscriber_only")]
    if not free_articles:
        return articles_data

    driver = None
    if use_selenium and SELENIUM_AVAILABLE:
        try:
            options = Options()
            options.headless = True
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument(f"--user-agent={HEADERS.get('User-Agent')}")
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(30)
        except Exception as e:
            print("Failed to start Selenium driver for article fetch:", e, file=sys.stderr)
            driver = None

    for a in free_articles:
        url = a.get("url")
        title = a.get("title")
        pub_text = a.get("pub_text", "")

        # Extract date from URL and time from pub_text, then combine
        date_str = extract_date_from_url(url)
        time_str = _extract_time_from_pub_text(pub_text)
        published_at = ""
        if date_str and time_str:
            published_at = f"{date_str} {time_str}"
        elif date_str:
            # keep date; may fill time from <time> tag later
            published_at = date_str
        else:
            # fallback to old behavior (time-only)
            published_at = time_str

        html = None
        if driver:
            try:
                driver.get(url)
                # small wait for dynamic content
                WebDriverWait(driver, 3)
                html = driver.page_source
            except Exception:
                html = None
        if html is None:
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                r.raise_for_status()
                html = r.text
            except Exception as e:
                print(f"Failed to fetch article {url}: {e}", file=sys.stderr)
                continue

        soup = BeautifulSoup(html, "lxml")

        # If published_at does not include a date/time, try to extract from <time>
        # Prefer filling missing time when we have a date from URL
        try_time_fill = True
        if published_at and re.match(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", str(published_at)):
            try_time_fill = False
        if try_time_fill:
            try:
                t = soup.find("time")
                if t:
                    # prefer datetime attribute
                    dt = t.get("datetime") or t.get_text(" ", strip=True)
                    if dt and "T" in dt:
                        # ISO datetime: 2026-03-26T12:35:00+01:00
                        m = re.search(r"T(\d{2}:\d{2})", dt)
                        if m:
                            time_from_time_tag = m.group(1)
                            if date_str:
                                published_at = f"{date_str} {time_from_time_tag}"
                            else:
                                published_at = time_from_time_tag
                    else:
                        # fallback parse hh'h'mm
                        time_from_dt = _extract_time_from_pub_text(dt)
                        if time_from_dt:
                            if date_str:
                                published_at = f"{date_str} {time_from_dt}"
                            else:
                                published_at = time_from_dt
            except Exception:
                pass

        # If published_at currently equals date only (YYYY-MM-DD), normalize to YYYY-MM-DD 00:00
        if published_at and re.match(r"^\d{4}-\d{2}-\d{2}$", str(published_at)):
            published_at = f"{published_at} 00:00"

        text = _clean_text(soup)

        articles_data.append({
            "title": title,
            "url": url,
            "published_at": published_at,
            "text": text,
        })

    if driver:
        try:
            driver.quit()
        except Exception:
            pass

    return articles_data


###############################################################################
# Translation helpers
###############################################################################


def chunk_text_by_tokens(text: str, tokenizer, max_tokens: int = 500, use_sacremoses: bool = False):
    """Split `text` into chunks where each chunk has <= max_tokens tokens (according to `tokenizer`).

    Strategy:
    - Split text into sentence-like fragments using punctuation.
    - Accumulate fragments until token count would exceed max_tokens.
    - If a single fragment is longer than max_tokens, fall back to word-based splitting.
    """
    if not text:
        return []

    # simple sentence splitter (keeps punctuation)
    frags = re.split(r'(?<=[\.\!\?…])\s+', text)

    chunks = []
    cur = []
    cur_count = 0

    # helper to count tokens for a fragment
    def token_count_for(s: str) -> int:
        if not s:
            return 0
        if use_sacremoses and SACREMOSES_AVAILABLE:
            # sacremoses provides a fast word tokenizer; approximate token count
            mt = MosesTokenizer()
            toks = mt.tokenize(s, return_str=False)
            return len(toks)
        # fallback: use tokenizer.encode (exclude special tokens)
        try:
            ids = tokenizer.encode(s, add_special_tokens=False)
            return len(ids)
        except Exception:
            # worst-case fallback: approximate by word count
            return len(s.split())

    for frag in frags:
        cnt = token_count_for(frag)
        if cnt > max_tokens:
            # fragment itself too large: split by words
            words = frag.split()
            sub = []
            sub_count = 0
            for w in words:
                w_cnt = token_count_for(w)
                if sub_count + w_cnt <= max_tokens:
                    sub.append(w)
                    sub_count += w_cnt
                else:
                    if sub:
                        chunks.append(" ".join(sub))
                    sub = [w]
                    sub_count = w_cnt
            if sub:
                # append remainder
                if cur_count + sub_count <= max_tokens and cur:
                    cur.append(" ".join(sub))
                    cur_count += sub_count
                else:
                    if cur:
                        chunks.append(" ".join(cur))
                    chunks.append(" ".join(sub))
                    cur = []
                    cur_count = 0
            continue

        # normal fragment
        if cur_count + cnt <= max_tokens:
            cur.append(frag)
            cur_count += cnt
        else:
            # flush current
            if cur:
                chunks.append(" ".join(cur))
            cur = [frag]
            cur_count = cnt

    if cur:
        chunks.append(" ".join(cur))

    return chunks


def translate_in_chunks(text: str, tokenizer, model, max_tokens: int = 500, device: str = "cpu", use_sacremoses: bool = False) -> str:
    """Translate `text` by splitting into token-sized chunks and translating each.

    Returns the concatenated translation string.
    """
    if not text:
        return ""

    # Prepare device
    use_cuda = TORCH_AVAILABLE and torch.cuda.is_available()
    dev = torch.device("cuda") if use_cuda and device == "cuda" else torch.device("cpu")
    try:
        model.to(dev)
    except Exception:
        pass

    chunks = chunk_text_by_tokens(text, tokenizer, max_tokens=max_tokens, use_sacremoses=use_sacremoses)
    translations = []

    for i, chunk in enumerate(chunks):
        try:
            # tokenizers will add special tokens and handle truncation
            inputs = tokenizer(chunk, return_tensors="pt", truncation=True)
            if TORCH_AVAILABLE:
                inputs = {k: v.to(dev) for k, v in inputs.items()}
            outputs = model.generate(**inputs, max_length= inputs['input_ids'].shape[1] * 4 + 50)
            translated = tokenizer.decode(outputs[0], skip_special_tokens=True)
            translations.append(translated)
        except Exception as e:
            # If a chunk fails, try a safer fallback: character-based smaller chunks
            safe_parts = [chunk[i:i+1000] for i in range(0, len(chunk), 1000)]
            for sp in safe_parts:
                try:
                    sin = tokenizer(sp, return_tensors="pt", truncation=True)
                    if TORCH_AVAILABLE:
                        sin = {k: v.to(dev) for k, v in sin.items()}
                    out = model.generate(**sin, max_length= sin['input_ids'].shape[1] * 4 + 50)
                    translations.append(tokenizer.decode(out[0], skip_special_tokens=True))
                except Exception as e2:
                    # Last resort: append empty string and continue
                    translations.append("")
    # join translated parts with double newlines to preserve paragraph breaks
    return "\n\n".join(filter(None, translations))


def init_db(db_path: str = "articles.db"):
    """Create SQLite DB and articles table if not exists."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            published_at TEXT,
            text TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def init_translation_db(db_path: str = "translation.db"):
    """Create translation DB and translations table if not exists."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # enable foreign keys
    cur.execute("PRAGMA foreign_keys = ON;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER,
            title TEXT,
            url TEXT,
            translated_text TEXT,
            FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


def get_translation_id_by_article(article_id: int, url: str, db_path: str = "translation.db") -> int | None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id FROM translations WHERE article_id = ? OR url = ? LIMIT 1", (article_id, url))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_translation_text(translation_id: int, db_path: str = "translation.db") -> dict | None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, title, url, translated_text FROM translations WHERE id = ? LIMIT 1", (translation_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "title": row[1], "url": row[2], "translated_text": row[3]}


def list_translations(db_path: str = "translation.db"):
    """Yield all translations as dicts from the translations DB."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, article_id, title, url, translated_text FROM translations")
    rows = cur.fetchall()
    conn.close()
    for row in rows:
        yield {"id": row[0], "article_id": row[1], "title": row[2], "url": row[3], "translated_text": row[4]}


def paraphrase_only_flow(args):
    """Process all translations in translation.db and create paraphrases for missing ones."""
    print("Running paraphrase-only mode: scanning translations and generating paraphrases...")
    init_paraphrase_db()

    if not TRANSFORMERS_AVAILABLE:
        print("Transformers not available; cannot paraphrase. Install 'transformers' and restart.")
        return

    # load paraphrase model once
    paraphrase_model_name = "Vamsi/T5_Paraphrase_Paws"
    try:
        print(f"Loading paraphrase model {paraphrase_model_name}...")
        paraphrase_tokenizer = AutoTokenizer.from_pretrained(paraphrase_model_name)
        paraphrase_model = AutoModelForSeq2SeqLM.from_pretrained(paraphrase_model_name)
        if TORCH_AVAILABLE and torch.cuda.is_available():
            paraphrase_model.to(torch.device("cuda"))
    except Exception as e:
        print(f"Failed to load paraphrase model: {e}")
        return

    for t in list_translations():
        t_id = t.get("id")
        url = t.get("url")
        print(f"Processing translation id={t_id} url={url}")
        if paraphrase_exists(t_id, url):
            print(f"  -> Paraphrase exists for translation id={t_id}; skipping")
            continue
        text = t.get("translated_text") or ""
        if not text:
            print(f"  -> No translated text for id={t_id}; skipping")
            continue
        try:
            paraphrased = translate_in_chunks(
                text,
                paraphrase_tokenizer,
                paraphrase_model,
                max_tokens=200,
                device=("cuda" if TORCH_AVAILABLE and torch.cuda.is_available() else "cpu"),
                use_sacremoses=args.use_sacremoses,
            )
        except Exception as e:
            print(f"  -> Paraphrasing failed for translation id={t_id}: {e}")
            paraphrased = ""

        if paraphrased:
            p_id = insert_paraphrase(t_id, t.get("title"), url, paraphrased)
            if p_id:
                print(f"  -> Inserted paraphrase for translation id={t_id} (paraphrase_id={p_id})")
                # schedule telegram approval message if requested
                if getattr(args, "telegram", False) and TELEGRAM_AVAILABLE:
                    token = getattr(args, "telegram_token", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
                    try:
                        admin_id = int(getattr(args, "telegram_admin_id", os.environ.get("TELEGRAM_ADMIN_ID") or 0) or 0)
                    except Exception:
                        admin_id = 0
                    try:
                        channel_id = int(getattr(args, "telegram_channel_id", os.environ.get("TELEGRAM_CHANNEL_ID") or 0) or 0)
                    except Exception:
                        channel_id = 0
                    if token and admin_id and channel_id:
                        start_telegram_app(token, admin_id, channel_id)
                        if TELEGRAM_LOOP:
                            try:
                                # avoid re-sending if previously scheduled
                                p_row = get_paraphrase(p_id)
                                if not p_row or not p_row.get("sent_for_approval"):
                                    # mark as sent to avoid duplicate scheduling on restarts
                                    try:
                                        mark_paraphrase_sent_for_approval(p_id)
                                    except Exception:
                                        pass
                                    asyncio.run_coroutine_threadsafe(_send_approval_message(p_id, t.get("title"), paraphrased), TELEGRAM_LOOP)
                                else:
                                    print(f"Paraphrase id={p_id} already sent for approval; skipping scheduling")
                            except Exception as e:
                                print("Failed to schedule telegram approval message:", e)
            else:
                print(f"  -> Failed to insert paraphrase for translation id={t_id}")


def translation_exists(article_id: int, url: str, db_path: str = "translation.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM translations WHERE article_id = ? OR url = ? LIMIT 1", (article_id, url))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def insert_translation(article_id: int, title: str, url: str, translated_text: str, db_path: str = "translation.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO translations (article_id, title, url, translated_text) VALUES (?, ?, ?, ?)",
            (article_id, title, url, translated_text),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def init_paraphrase_db(db_path: str = "paraphrased_translation.db"):
    """Create paraphrased translations DB and paraphrases table if not exists."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paraphrases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            translation_id INTEGER,
            title TEXT,
            url TEXT,
            paraphrased_text TEXT,
            FOREIGN KEY(translation_id) REFERENCES translations(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()
    # ensure approved column exists for workflow
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("ALTER TABLE paraphrases ADD COLUMN approved INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        # likely column already exists
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # ensure sent_for_approval column exists (tracks whether we've sent the paraphrase to admin)
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(paraphrases)")
        cols = [r[1] for r in cur.fetchall()]
        if 'sent_for_approval' not in cols:
            cur.execute("ALTER TABLE paraphrases ADD COLUMN sent_for_approval BOOLEAN DEFAULT 0")
            conn.commit()
            print(f"Added column 'sent_for_approval' to {db_path}")
        else:
            print(f"Column 'sent_for_approval' already exists in {db_path}")
    except Exception as e:
        print(f"Failed to ensure 'sent_for_approval' column: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # create table to store edited/paraphrased versions (admin-edits) if not exists
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS edited_paraphrases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id INTEGER,
                edited_title TEXT,
                edited_text TEXT,
                FOREIGN KEY(original_id) REFERENCES paraphrases(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    except Exception as e:
        print(f"Failed to create 'edited_paraphrases' table: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def paraphrase_exists(translation_id: int, url: str, db_path: str = "paraphrased_translation.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM paraphrases WHERE translation_id = ? OR url = ? LIMIT 1", (translation_id, url))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def insert_paraphrase(translation_id: int, title: str, url: str, paraphrased_text: str, db_path: str = "paraphrased_translation.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO paraphrases (translation_id, title, url, paraphrased_text) VALUES (?, ?, ?, ?)",
            (translation_id, title, url, paraphrased_text),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # try to return existing id
        cur.execute("SELECT id FROM paraphrases WHERE translation_id = ? OR url = ? LIMIT 1", (translation_id, url))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def mark_paraphrase_approved(paraphrase_id: int, db_path: str = "paraphrased_translation.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE paraphrases SET approved = 1 WHERE id = ?", (paraphrase_id,))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def mark_paraphrase_sent_for_approval(paraphrase_id: int, db_path: str = "paraphrased_translation.db") -> bool:
    """Mark the paraphrase as having been sent for approval (sent_for_approval = 1)."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE paraphrases SET sent_for_approval = 1 WHERE id = ?", (paraphrase_id,))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def get_paraphrase(paraphrase_id: int, db_path: str = "paraphrased_translation.db") -> dict | None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, translation_id, title, url, paraphrased_text, approved, sent_for_approval FROM paraphrases WHERE id = ? LIMIT 1", (paraphrase_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "translation_id": row[1], "title": row[2], "url": row[3], "paraphrased_text": row[4], "approved": row[5], "sent_for_approval": row[6]}


def insert_edited_paraphrase(original_id: int, edited_title: str, edited_text: str, db_path: str = "paraphrased_translation.db") -> int | None:
    """Insert an edited version of a paraphrase into edited_paraphrases.

    Returns the new row id on success, or None on failure.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO edited_paraphrases (original_id, edited_title, edited_text) VALUES (?, ?, ?)",
            (original_id, edited_title, edited_text),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        # attempt to fail gracefully
        return None
    finally:
        conn.close()


def get_edited_for(original_id: int, db_path: str = "paraphrased_translation.db") -> list:
    """Return a list of edited_paraphrases rows for the given original_id, newest first.

    Each item is a dict: {id, original_id, edited_title, edited_text}
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, original_id, edited_title, edited_text FROM edited_paraphrases WHERE original_id = ? ORDER BY id DESC",
            (original_id,),
        )
        rows = cur.fetchall()
        return [{"id": r[0], "original_id": r[1], "edited_title": r[2], "edited_text": r[3]} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _chunk_text_by_chars(text: str, min_size: int = 200, max_size: int = 500):
    """Split text into chunks of roughly min_size..max_size characters, preferring paragraph/sentence breaks."""
    if not text:
        return []
    # split by paragraphs first
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    cur = ""
    for p in parts:
        if not cur:
            cur = p
        else:
            if len(cur) + 2 + len(p) <= max_size:
                cur = cur + "\n\n" + p
            else:
                if len(cur) >= min_size:
                    chunks.append(cur)
                    cur = p
                else:
                    # cur too small; try to append until at least min_size
                    cur = cur + "\n\n" + p
                    if len(cur) >= min_size:
                        chunks.append(cur)
                        cur = ""
    if cur:
        # may still be long; break by sentences
        if len(cur) > max_size:
            sents = re.split(r'(?<=[\.\!\?…])\s+', cur)
            cur2 = ""
            for s in sents:
                if not cur2:
                    cur2 = s
                elif len(cur2) + 1 + len(s) <= max_size:
                    cur2 = cur2 + " " + s
                else:
                    chunks.append(cur2)
                    cur2 = s
            if cur2:
                chunks.append(cur2)
        else:
            chunks.append(cur)
    return chunks


async def _send_approval_message(paraphrase_id: int, title: str, paraphrased_text: str):
    """Coroutine that sends the approval message (title + chunked paraphrase) to admin chat with inline buttons."""
    global TELEGRAM_APP, TELEGRAM_ADMIN_ID
    if not TELEGRAM_APP or TELEGRAM_ADMIN_ID is None:
        return
    kb = [
        [InlineKeyboardButton("✅ Yes", callback_data=f"approve:{paraphrase_id}"), InlineKeyboardButton("❌ No", callback_data=f"reject:{paraphrase_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(kb)
    try:
        init_msg = await TELEGRAM_APP.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=f"Review article: {title}", reply_markup=reply_markup)
    except Exception as e:
        print("Failed to send approval message:", e)
        return

    # send paraphrased text in chunks
    chunks = _chunk_text_by_chars(paraphrased_text, min_size=200, max_size=500)
    if not chunks:
        return
    for c in chunks:
        try:
            await TELEGRAM_APP.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=c, reply_to_message_id=init_msg.message_id)
            await asyncio.sleep(0.8)
        except Exception:
            # continue on failures
            pass


def start_telegram_app(token: str, admin_chat_id: int, channel_id: int):
    """Start the telegram Application in a background thread. Stores globals for scheduling coroutines."""
    global TELEGRAM_APP, TELEGRAM_LOOP, TELEGRAM_THREAD, TELEGRAM_ADMIN_ID, TELEGRAM_CHANNEL_ID
    if not TELEGRAM_AVAILABLE:
        print("python-telegram-bot not available; install it to enable Telegram workflow")
        return False

    if TELEGRAM_APP:
        return True

    TELEGRAM_ADMIN_ID = admin_chat_id
    TELEGRAM_CHANNEL_ID = channel_id

    app = ApplicationBuilder().token(token).build()

    async def _callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # expects callback_data like 'approve:123' or 'reject:123'
        data = update.callback_query.data or ""
        try:
            action, sid = data.split(":", 1)
            pid = int(sid)
        except Exception:
            await update.callback_query.answer("Invalid callback data")
            return

        if action == "approve":
            p = get_paraphrase(pid)
            if not p:
                await update.callback_query.answer("Paraphrase not found")
                return
            text = (p.get("title") or "") + "\n\n" + (p.get("paraphrased_text") or "")
            parts = [text[i:i+3800] for i in range(0, len(text), 3800)]
            for part in parts:
                await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=part)
            mark_paraphrase_approved(pid)
            await update.callback_query.answer("Approved and posted")
            try:
                await update.callback_query.message.edit_reply_markup(None)
            except Exception:
                pass
        elif action == "reject":
            await update.callback_query.answer("Rejected")
            try:
                await update.callback_query.message.edit_reply_markup(None)
            except Exception:
                pass

    app.add_handler(CallbackQueryHandler(_callback_router))

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        nonlocal_app = app
        # store loop and app for scheduling
        globals()['TELEGRAM_LOOP'] = loop
        globals()['TELEGRAM_APP'] = nonlocal_app
        try:
            nonlocal_app.run_polling()
        except Exception as e:
            print("Telegram app polling stopped:", e)

    t = threading.Thread(target=_run, daemon=True)
    TELEGRAM_THREAD = t
    t.start()
    time.sleep(1)
    return True


def stop_telegram_app():
    global TELEGRAM_APP
    if TELEGRAM_APP:
        try:
            TELEGRAM_APP.stop()
        except Exception:
            pass


def article_exists(title: str, url: str, db_path: str = "articles.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM articles WHERE url = ? OR title = ? LIMIT 1", (url, title))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def insert_article(article: dict, db_path: str = "articles.db") -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO articles (title, url, published_at, text) VALUES (?, ?, ?, ?)",
            (article.get("title"), article.get("url"), article.get("published_at"), article.get("text")),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # return existing id if present
        cur.execute("SELECT id FROM articles WHERE url = ?", (article.get("url"),))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_article_id(url: str, db_path: str = "articles.db") -> int | None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id FROM articles WHERE url = ? LIMIT 1", (url,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def main():
    parser = argparse.ArgumentParser(description="Fetch Le Monde live page and list today's articles")
    parser.add_argument("--cookie", help="Cookie header string (or set env LEMONDE_COOKIE)")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch full text of free articles after scraping the index"
    )
    parser.add_argument(
        "--db-path",
        default="articles.db",
        help="SQLite database path (default: articles.db)"
    )
    parser.add_argument(
        "--use-sacremoses",
        action="store_true",
        help="Use sacremoses for faster/approximate token counting when chunking (optional)"
    )
    parser.add_argument(
        "--paraphrase-only",
        action="store_true",
        help="Only run the paraphrase pipeline on existing translations in translation.db and exit"
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Enable Telegram approval workflow (requires telegram token/admin/channel)"
    )
    parser.add_argument(
        "--telegram-token",
        help="Telegram bot token (or set TELEGRAM_BOT_TOKEN env)"
    )
    parser.add_argument(
        "--telegram-admin-id",
        help="Telegram admin chat id (your user id) or set TELEGRAM_ADMIN_ID env",
    )
    parser.add_argument(
        "--telegram-channel-id",
        help="Target Telegram channel id where approved posts are sent (or set TELEGRAM_CHANNEL_ID env)",
    )
    args = parser.parse_args()

    # If paraphrase-only mode requested, run paraphrase-only flow and exit
    if getattr(args, "paraphrase_only", False):
        paraphrase_only_flow(args)
        return

    # Allow cookie to be passed via --cookie or environment variable LEMONDE_COOKIE
    cookie_val = args.cookie or os.environ.get("LEMONDE_COOKIE")
    if cookie_val:
        HEADERS["Cookie"] = cookie_val

    use_selenium = SELENIUM_AVAILABLE
    if not SELENIUM_AVAILABLE:
        print("Selenium or webdriver-manager not available; falling back to requests.\n"
              "Install with: python3 -m pip install -r requirements.txt")
        use_selenium = False

    html = None
    if use_selenium:
        try:
            print("Fetching with Selenium (headless Chrome)...")
            options = Options()
            options.headless = True
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument(f"--user-agent={HEADERS.get('User-Agent')}")
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(30)
            driver.get(BASE_URL)
            html = driver.page_source
            driver.quit()
        except Exception as e:
            print("Selenium fetch failed:", e, file=sys.stderr)
            print("Falling back to requests...")
            use_selenium = False

    if not use_selenium:
        print(f"Fetching {BASE_URL} with requests ...")
        try:
            r = requests.get(BASE_URL, headers=HEADERS, timeout=20)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            print("Failed to fetch page:", e, file=sys.stderr)
            sys.exit(1)

    articles = gather_articles(html, base=BASE_URL)

    if not articles:
        print("No articles found for today.")
        return

    # Print index
    print(f"Found {len(articles)} article(s) published today ({TODAY.isoformat()}):\n")
    for i, a in enumerate(sorted(articles, key=lambda x: x.get("pub_text", ""), reverse=True), start=1):
        sub = "(subscriber-only)" if a["subscriber_only"] else "(free)"
        print(f"{i}. {a['title']}\n   {a['url']}\n   {sub}")
        if a.get("pub_text"):
            print(f"   {a['pub_text']}")
        print()

    # If --fetch is provided, fetch full texts for free articles
    if args.fetch:
        print("Fetching full text for free articles...")
        articles_data = process_free_articles(articles, use_selenium=use_selenium)
        print(f"\nFetched {len(articles_data)} free articles with text:")
        # Initialize DBs
        init_db(args.db_path)
        init_translation_db()

        # initialize translation model lazily (only if transformers available)
        model = None
        tokenizer = None
        if TRANSFORMERS_AVAILABLE:
            try:
                model_name = "Helsinki-NLP/opus-mt-fr-en"
                print(f"Loading translation model {model_name}...")
                tokenizer = AutoTokenizer.from_pretrained(model_name)
                model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
            except Exception as e:
                print("Failed to load translation model:", e, file=sys.stderr)
                model = None
                tokenizer = None

        for a in articles_data:
            print(f"- {a['title']} ({a['published_at']}) [{len(a['text'])} chars]")
            # determine article id (insert if new)
            if article_exists(a.get("title"), a.get("url"), db_path=args.db_path):
                art_id = get_article_id(a.get("url"), db_path=args.db_path)
                print(f"  -> Skipping insert (already in DB): {a.get('url')} (id={art_id})")
            else:
                art_id = insert_article(a, db_path=args.db_path)
                if art_id:
                    print(f"  -> Inserted into DB: {a.get('url')} (id={art_id})")
                else:
                    print(f"  -> Failed to insert (duplicate?): {a.get('url')}")

            if art_id is None:
                continue

            # Skip if translation already exists
            if translation_exists(art_id, a.get("url")):
                print(f"  -> Translation already exists for article id={art_id}")
                continue

            # If transformers not available or model failed to load, skip translation
            if not TRANSFORMERS_AVAILABLE or model is None or tokenizer is None:
                print("  -> Transformers not available or model failed to load; skipping translation.")
                continue

            # perform translation (chunking long texts)
            try:
                text = a.get("text") or ""
                translated_text = translate_in_chunks(
                    text,
                    tokenizer,
                    model,
                    max_tokens=500,
                    device=("cuda" if TORCH_AVAILABLE and torch.cuda.is_available() else "cpu"),
                    use_sacremoses=args.use_sacremoses,
                )
                # translate title
                if a.get('title'):
                    t_inputs = tokenizer(a.get('title'), return_tensors='pt', truncation=True)
                    t_out = model.generate(**t_inputs, max_length=128)
                    translated_title = tokenizer.decode(t_out[0], skip_special_tokens=True)
                else:
                    translated_title = ""

                ok = insert_translation(art_id, translated_title, a.get("url"), translated_text)
                if ok:
                    print(f"  -> Inserted translation for article id={art_id}")
                else:
                    print(f"  -> Failed to insert translation for article id={art_id}")
            except Exception as e:
                print(f"  -> Translation failed for article id={art_id}: {e}")

            # After translation inserted (or existing), attempt paraphrase
            try:
                # get translation id (either existing or newly inserted)
                trans_id = get_translation_id_by_article(art_id, a.get("url"))
                if trans_id is None:
                    print(f"  -> Cannot find translation id for article id={art_id}; skipping paraphrase")
                    continue

                # ensure paraphrase DB exists
                init_paraphrase_db()

                if paraphrase_exists(trans_id, a.get("url")):
                    print(f"  -> Paraphrase already exists for translation id={trans_id}")
                    continue

                # fetch translation text
                trans_row = get_translation_text(trans_id)
                if not trans_row or not trans_row.get("translated_text"):
                    print(f"  -> No translated text found for translation id={trans_id}; skipping paraphrase")
                    continue

                # initialize paraphrase model lazily
                paraphrase_model = None
                paraphrase_tokenizer = None
                paraphrase_model_name = "Vamsi/T5_Paraphrase_Paws"
                if TRANSFORMERS_AVAILABLE:
                    try:
                        print(f"  -> Loading paraphrase model {paraphrase_model_name}...")
                        paraphrase_tokenizer = AutoTokenizer.from_pretrained(paraphrase_model_name)
                        paraphrase_model = AutoModelForSeq2SeqLM.from_pretrained(paraphrase_model_name)
                        # move to GPU if available
                        if TORCH_AVAILABLE and torch.cuda.is_available():
                            paraphrase_model.to(torch.device("cuda"))
                    except Exception as e:
                        print(f"  -> Failed to load paraphrase model: {e}")
                        paraphrase_model = None
                        paraphrase_tokenizer = None

                if paraphrase_model is None or paraphrase_tokenizer is None:
                    print("  -> Paraphrase model unavailable; skipping paraphrase.")
                    continue

                # paraphrase the translated text in chunks
                paraphrased = ""
                try:
                    paraphrased = translate_in_chunks(
                        trans_row.get("translated_text"),
                        paraphrase_tokenizer,
                        paraphrase_model,
                        max_tokens=200,
                        device=("cuda" if TORCH_AVAILABLE and torch.cuda.is_available() else "cpu"),
                        use_sacremoses=args.use_sacremoses,
                    )
                except Exception as e:
                    print(f"  -> Paraphrasing failed for translation id={trans_id}: {e}")
                    paraphrased = ""

                if paraphrased:
                    p_id = insert_paraphrase(trans_id, trans_row.get("title"), trans_row.get("url"), paraphrased)
                    if p_id:
                        print(f"  -> Inserted paraphrase for translation id={trans_id} (paraphrase_id={p_id})")
                        # send approval request via Telegram if enabled
                        if getattr(args, "telegram", False) and TELEGRAM_AVAILABLE:
                            token = getattr(args, "telegram_token", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
                            try:
                                admin_id = int(getattr(args, "telegram_admin_id", os.environ.get("TELEGRAM_ADMIN_ID") or 0) or 0)
                            except Exception:
                                admin_id = 0
                            try:
                                channel_id = int(getattr(args, "telegram_channel_id", os.environ.get("TELEGRAM_CHANNEL_ID") or 0) or 0)
                            except Exception:
                                channel_id = 0
                            if token and admin_id and channel_id:
                                start_telegram_app(token, admin_id, channel_id)
                                if TELEGRAM_LOOP:
                                    try:
                                        p_row = get_paraphrase(p_id)
                                        if not p_row or not p_row.get("sent_for_approval"):
                                            try:
                                                mark_paraphrase_sent_for_approval(p_id)
                                            except Exception:
                                                pass
                                            asyncio.run_coroutine_threadsafe(_send_approval_message(p_id, trans_row.get("title"), paraphrased), TELEGRAM_LOOP)
                                        else:
                                            print(f"Paraphrase id={p_id} already sent for approval; skipping scheduling")
                                    except Exception as e:
                                        print("Failed to schedule telegram approval message:", e)
                    else:
                        print(f"  -> Failed to insert paraphrase for translation id={trans_id}")
            except Exception as e:
                print(f"  -> Paraphrase pipeline error for article id={art_id}: {e}")


if __name__ == "__main__":
    main()
