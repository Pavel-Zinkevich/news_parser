#!/usr/bin/env python3
"""send_pending_news.py

Send pending paraphrases from paraphrased_translation.db to an admin Telegram chat for approval.

Behavior:
- Loads TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, TELEGRAM_CHANNEL_ID from .env
- Finds paraphrases where sent_for_approval = 0
- Sends them concurrently to the admin chat with inline ✅/❌ buttons
- Marks sent_for_approval = 1 after successful initial send
- Waits for admin callbacks (approve/reject)
- On approve: posts to channel and marks approved = 1
- On reject: does not repost, leaves approved = 0
- Exits automatically after all items are processed

Requires: python-telegram-bot v20+, python-dotenv
"""

import os
import sqlite3
import asyncio
import logging
import time
from typing import List, Dict

from dotenv import load_dotenv

# load env
load_dotenv(dotenv_path=".env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
DB_PATH = os.getenv("PARAPHRASE_DB_PATH", "paraphrased_translation.db")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID or not TELEGRAM_CHANNEL_ID:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID and TELEGRAM_CHANNEL_ID in .env")

try:
    TELEGRAM_ADMIN_ID = int(TELEGRAM_ADMIN_ID)
    TELEGRAM_CHANNEL_ID = int(TELEGRAM_CHANNEL_ID)
except Exception:
    raise SystemExit("TELEGRAM_ADMIN_ID and TELEGRAM_CHANNEL_ID must be integers")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.error import TimedOut, RetryAfter
import html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


def _split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT):
    if not text:
        return [""]
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + limit)
        if end == n:
            parts.append(text[start:end])
            break
        split_at = text.rfind('\n', start, end)
        if split_at == -1:
            split_at = text.rfind(' ', start, end)
        if split_at == -1 or split_at <= start:
            split_at = end
        parts.append(text[start:split_at].rstrip())
        start = split_at
        while start < n and text[start] in ('\n', ' ', '\t'):
            start += 1
    return parts


# DB helpers
def get_pending_paraphrases(db_path: str = DB_PATH) -> List[Dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, translation_id, title, url, paraphrased_text FROM paraphrases WHERE sent_for_approval = 0")
        rows = cur.fetchall()
        return [{"id": r[0], "translation_id": r[1], "title": r[2], "url": r[3], "paraphrased_text": r[4]} for r in rows]
    finally:
        conn.close()


def get_translation_url(translation_id: int, db_path: str = "translation.db") -> str | None:
    """Return the URL for the given translation id from translation.db, or None."""
    if not translation_id:
        return None
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("SELECT url FROM translations WHERE id = ? LIMIT 1", (translation_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None
    finally:
        conn.close()


def mark_sent_for_approval(paraphrase_id: int, db_path: str = DB_PATH) -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE paraphrases SET sent_for_approval = 1 WHERE id = ?", (paraphrase_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.exception("Failed to mark sent_for_approval for %s: %s", paraphrase_id, e)
        return False
    finally:
        conn.close()


def mark_approved(paraphrase_id: int, db_path: str = DB_PATH) -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE paraphrases SET approved = 1 WHERE id = ?", (paraphrase_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.exception("Failed to mark approved for %s: %s", paraphrase_id, e)
        return False
    finally:
        conn.close()


async def safe_send(bot, **kwargs):
    """Send with retries for transient errors."""
    max_attempts = 4
    backoff = 0.5
    for attempt in range(1, max_attempts + 1):
        try:
            return await bot.send_message(**kwargs)
        except RetryAfter as e:
            wait = getattr(e, "retry_after", 1)
            logger.warning("RetryAfter from Telegram, waiting %s s", wait)
            await asyncio.sleep(wait)
        except (TimedOut, ConnectionError) as e:
            logger.warning("Transient error on send (attempt %s/%s): %s", attempt, max_attempts, e)
            await asyncio.sleep(backoff * attempt)
        except Exception as e:
            logger.exception("Failed to send message: %s", e)
            # non-transient, break
            break
    return None


async def send_paraphrase_and_wait(bot, paraphrase: Dict, events: Dict[int, asyncio.Event], semaphore: asyncio.Semaphore):
    """Send initial approval message and the remaining chunks, mark sent, then wait for admin decision event to be set."""
    pid = paraphrase["id"]
    title = paraphrase.get("title") or ""
    body = paraphrase.get("paraphrased_text") or ""
    # try to get canonical url from translations DB
    url = None
    try:
        url = get_translation_url(paraphrase.get("translation_id"))
    except Exception:
        url = paraphrase.get("url")
    if not url:
        url = paraphrase.get("url")

    # build HTML formatted message: bold title, body, separator, clickable link
    esc_title = html.escape(title)
    esc_body = html.escape(body)
    esc_url = html.escape(url or "", quote=True)
    link_html = f'<a href="{esc_url}">🔗 Link to article</a>' if esc_url else ''

    full = f"<b>{esc_title}</b>\n\n{esc_body}\n\n---\n\n{link_html}".strip()
    parts = _split_text(full, TELEGRAM_MESSAGE_LIMIT)

    kb = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{pid}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{pid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{pid}")
    ]]
    reply_markup = InlineKeyboardMarkup(kb)

    async with semaphore:
        # send first part with buttons
        try:
            if parts:
                # first part - include reply_markup and parse_mode=HTML
                msg = await safe_send(bot, chat_id=TELEGRAM_ADMIN_ID, text=parts[0], reply_markup=reply_markup, parse_mode="HTML")
                # send remaining parts without buttons; ensure parse_mode=HTML
                for part in parts[1:]:
                    try:
                        await safe_send(bot, chat_id=TELEGRAM_ADMIN_ID, text=part, reply_to_message_id=(msg.message_id if msg else None), parse_mode="HTML")
                    except Exception:
                        # keep going
                        pass
            else:
                msg = await safe_send(bot, chat_id=TELEGRAM_ADMIN_ID, text="(empty)", reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logger.exception("Failed to send approval message for %s: %s", pid, e)
            return

    # mark as sent so other runs won't re-send
    marked = False
    try:
        marked = mark_sent_for_approval(pid)
    except Exception:
        pass
    if not marked:
        logger.warning("Could not mark paraphrase %s as sent_for_approval", pid)

    # create an event if not present and wait until callback handler sets it
    ev = events.setdefault(pid, asyncio.Event())
    logger.info("Waiting for admin decision on paraphrase %s", pid)
    await ev.wait()
    logger.info("Decision received for paraphrase %s", pid)


async def main():
    pending = get_pending_paraphrases()
    if not pending:
        logger.info("No pending paraphrases to send. Exiting.")
        return

    # build application
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # events to wait for admin decisions
    events: Dict[int, asyncio.Event] = {}
    # pending edit requests: maps admin user id -> paraphrase id they're editing
    pending_edits: Dict[int, int] = {}

    # callback handler nested so it closes over events and db functions
    async def _callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        data = update.callback_query.data or ""
        try:
            action, sid = data.split(":", 1)
            pid = int(sid)
        except Exception:
            await update.callback_query.answer("Invalid callback data")
            return

        # avoid double-processing: if event already set, just ack
        ev = events.setdefault(pid, asyncio.Event())
        if ev.is_set():
            try:
                await update.callback_query.answer("Already processed")
            except Exception:
                pass
            return

        if action == "approve":
            # fetch paraphrase fresh to get text/title
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            try:
                # fetch title, paraphrased_text, translation_id and stored url (if any)
                cur.execute("SELECT title, paraphrased_text, translation_id, url FROM paraphrases WHERE id = ? LIMIT 1", (pid,))
                row = cur.fetchone()
            finally:
                conn.close()

            if not row:
                try:
                    await update.callback_query.answer("Paraphrase not found")
                except Exception:
                    pass
                ev.set()
                return

            title, paraphrased_text, translation_id, stored_url = row[0] or "", row[1] or "", row[2] if len(row) > 2 else None, row[3] if len(row) > 3 else None

            # If there's an edited version, prefer the latest edited_title/edited_text
            try:
                conn2 = sqlite3.connect(DB_PATH)
                cur2 = conn2.cursor()
                cur2.execute("SELECT edited_title, edited_text FROM edited_paraphrases WHERE original_id = ? ORDER BY id DESC LIMIT 1", (pid,))
                er = cur2.fetchone()
                conn2.close()
                if er and (er[0] or er[1]):
                    title = er[0] or title
                    paraphrased_text = er[1] or paraphrased_text
            except Exception:
                # ignore and fallback to original paraphrase
                pass

            # determine canonical url (prefer translation.db lookup)
            url = None
            try:
                if translation_id:
                    url = get_translation_url(translation_id)
            except Exception:
                url = None
            if not url:
                url = stored_url

            # build HTML formatted message for channel: bold title, body, separator, clickable link
            esc_title = html.escape(title)
            esc_body = html.escape(paraphrased_text)
            esc_url = html.escape(url or "", quote=True)
            link_html = f'<a href="{esc_url}">🔗 Link to article</a>' if esc_url else ''
            full_html = f"<b>{esc_title}</b>\n\n{esc_body}\n\n---\n\n{link_html}".strip()
            parts = _split_text(full_html, TELEGRAM_MESSAGE_LIMIT)

            # send to channel; require all parts to be sent (use HTML parse mode)
            all_sent = True
            for part in parts:
                try:
                    sent = await safe_send(context.bot, chat_id=TELEGRAM_CHANNEL_ID, text=part, parse_mode="HTML")
                    if not sent:
                        all_sent = False
                        logger.error("Failed to send part to channel for paraphrase %s", pid)
                        break
                except Exception as e:
                    logger.exception("Error sending part to channel for %s: %s", pid, e)
                    all_sent = False
                    break

            if all_sent:
                # mark approved in DB
                try:
                    marked = mark_approved(pid)
                    if not marked:
                        logger.warning("Failed to mark paraphrase %s as approved", pid)
                except Exception:
                    logger.exception("Exception while marking approved for %s", pid)

                try:
                    await update.callback_query.answer("Approved and posted")
                except Exception:
                    pass

                try:
                    await update.callback_query.message.edit_reply_markup(None)
                except Exception:
                    pass
            else:
                try:
                    await update.callback_query.answer("Failed to post to channel; see logs")
                except Exception:
                    pass

            ev.set()

        elif action == "reject":
            # don't repost; just acknowledge
            try:
                await update.callback_query.answer("Rejected")
            except Exception:
                pass
            try:
                await update.callback_query.message.edit_reply_markup(None)
            except Exception:
                pass
            ev.set()

        elif action == "edit":
            # Store paraphrase id in memory for the admin and prompt for edited text
            admin_id = update.effective_user.id if update.effective_user else None
            if admin_id is None:
                try:
                    await update.callback_query.answer("Unable to identify you")
                except Exception:
                    pass
                return

            pending_edits[admin_id] = pid
            try:
                await update.callback_query.answer("Send the edited text now")
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=("Please send the edited version now.\n\n"
                          "- The first paragraph will be used as the heading.\n"
                          "- Separate paragraphs with Shift+Enter.\n"
                          "- Do NOT include the article link or a '---' divider — these will be added automatically when posting."),
                )
            except Exception:
                pass
            return

    app.add_handler(CallbackQueryHandler(_callback_router))

    # Message handler to capture edited text from admin after they pressed ✏️ Edit
    async def _message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return
        user = update.message.from_user
        if not user:
            return
        uid = user.id
        # only accept from the configured admin
        if uid != TELEGRAM_ADMIN_ID:
            return

        # check if this admin has a pending edit request
        pid = pending_edits.get(uid)
        if not pid:
            return

        text = update.message.text or ""
        if not text.strip():
            await update.message.reply_text("Empty message — please send the edited text, with the first paragraph as heading.")
            return

        import re
        # split paragraphs by blank line; fallback to single-newline splits
        paragraphs = re.split(r"\n\s*\n", text.strip())
        if len(paragraphs) == 1:
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]

        if not paragraphs:
            await update.message.reply_text("Could not parse paragraphs — please separate paragraphs with Shift+Enter (blank line).")
            return

        edited_title = paragraphs[0].strip()
        edited_body_parts = paragraphs[1:]

        # remove any lines that look like links or dividers from the edited body
        cleaned_parts = []
        for part in edited_body_parts:
            lines = [ln for ln in part.splitlines() if ln.strip() and 'http' not in ln and '🔗' not in ln and '---' not in ln]
            if lines:
                cleaned_parts.append('\n'.join(lines))

        edited_text = '\n\n'.join(cleaned_parts).strip()

        # Insert into DB
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("INSERT INTO edited_paraphrases (original_id, edited_title, edited_text) VALUES (?, ?, ?)", (pid, edited_title, edited_text))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.exception("Failed to save edited_paraphrase for %s: %s", pid, e)
            try:
                await update.message.reply_text("Failed to save edited version — see logs.")
            except Exception:
                pass
            pending_edits.pop(uid, None)
            return

        # acknowledge and clear pending state
        try:
            await update.message.reply_text("Edited version saved. Press ✅ Approve to post the edited version, or ✏️ Edit again to submit another revision.")
        except Exception:
            pass
        pending_edits.pop(uid, None)

    app.add_handler(MessageHandler(filters.User(TELEGRAM_ADMIN_ID) & (~filters.COMMAND), _message_router))

    # start the app (non-blocking) and start polling so callbacks are received
    await app.initialize()
    await app.start()
    # start polling to receive updates (CallbackQueryHandler requires polling or webhook)
    try:
        await app.updater.start_polling()
    except Exception:
        # some PTB setups expose start_polling differently; log and continue
        logger.exception("Failed to start polling via app.updater.start_polling()")
    logger.info("Telegram app started and polling; sending %s pending items...", len(pending))

    sem = asyncio.Semaphore(8)
    tasks = [asyncio.create_task(send_paraphrase_and_wait(app.bot, p, events, sem)) for p in pending]

    # Wait for all tasks to complete (i.e. sent and then event set by callback)
    try:
        await asyncio.gather(*tasks)
    except Exception:
        logger.exception("Error while processing pending paraphrases")

    # give small grace time for any final outgoing requests
    await asyncio.sleep(0.5)

    # stop polling and shutdown the application cleanly
    try:
        await app.updater.stop_polling()
    except Exception:
        pass
    try:
        await app.stop()
    except Exception:
        pass
    try:
        await app.shutdown()
    except Exception:
        pass

    logger.info("All pending paraphrases processed; exiting.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
