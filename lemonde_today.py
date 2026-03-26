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
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import sqlite3

# Selenium imports (optional, used when available)
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False


BASE_URL = "https://www.lemonde.fr/actualite-en-continu/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; lemonde-scraper/1.0; +https://example.org)"
}

TODAY = date.today()
TODAY_STR = TODAY.strftime("%Y/%m/%d")  # e.g. 2026/03/26

APOSTROPHE_VARIANTS = ["aujourd'hui", "aujourd’hui"]
PUB_REGEX = re.compile(r"Publié\s+(?:aujourd['’]hui|le)\b", re.IGNORECASE)

SUBSCRIBER_KEYWORDS = [
    "Article réservé",
    "Article réservé à nos abonnés",
    "Article réservé aux abonnés",
    "Réservé aux abonnés",
    "Réservé à nos abonnés",
    "Article réservé aux abonnés",
    "Article réservé aux abonnés",
]


def is_subscriber_only(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    for kw in SUBSCRIBER_KEYWORDS:
        if kw.lower() in lower:
            return True
    return False


def find_pub_text(ancestor) -> str:
    """Return any nearby publication text (e.g. 'Publié aujourd'hui à 12h35...') if present."""
    if not ancestor:
        return ""
    txt = ancestor.get_text(separator=" ", strip=True)
    # Look for 'Publié ...' or 'Publié aujourd'hui' variants
    m = re.search(r"Publié[^•\n\r]{0,120}", txt, flags=re.IGNORECASE)
    if m:
        return m.group(0)
    # Fallback: look specifically for today's mention
    for v in APOSTROPHE_VARIANTS:
        if v.lower() in txt.lower():
            return txt
    return ""


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
        published_at = _extract_time_from_pub_text(pub_text)

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

        # If published_at empty, try to extract from <time>
        if not published_at:
            try:
                t = soup.find("time")
                if t:
                    # prefer datetime attribute
                    dt = t.get("datetime") or t.get_text(" ", strip=True)
                    if dt and "T" in dt:
                        # ISO datetime: 2026-03-26T12:35:00+01:00
                        m = re.search(r"T(\d{2}:\d{2})", dt)
                        if m:
                            published_at = m.group(1)
                    else:
                        # fallback parse hh'h'mm
                        published_at = _extract_time_from_pub_text(dt)
            except Exception:
                pass

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
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


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
    args = parser.parse_args()

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
        # Initialize DB and insert new articles
        init_db(args.db_path)
        for a in articles_data:
            print(f"- {a['title']} ({a['published_at']}) [{len(a['text'])} chars]")
            if article_exists(a.get("title"), a.get("url"), db_path=args.db_path):
                print(f"  -> Skipping (already in DB): {a.get('url')}")
                continue
            ok = insert_article(a, db_path=args.db_path)
            if ok:
                print(f"  -> Inserted into DB: {a.get('url')}")
            else:
                print(f"  -> Failed to insert (duplicate?): {a.get('url')}")


if __name__ == "__main__":
    main()
