#!/usr/bin/env python3
"""send_last_paraphrase.py

Send the most recent paraphrase to your admin Telegram chat for approval.
This script starts a local Application (python-telegram-bot v20+) in the main thread
and schedules sending the approval message after the bot starts. Keep this process
running while you approve/reject messages.
"""

import os
import sqlite3
import asyncio
from dotenv import load_dotenv
import json
import socket

# Scoped IPv4 preference: prefer IPv4 only while creating network clients or
# performing the quick pre-check. This avoids patching the resolver for the
# entire process lifetime.
class _PreferIPv4:
    def __init__(self):
        self._orig = socket.getaddrinfo

    def __enter__(self):
        orig = self._orig

        def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
            infos = orig(host, port, family, type, proto, flags)
            ipv4 = [i for i in infos if i[0] == socket.AF_INET]
            return ipv4 or infos

        socket.getaddrinfo = _getaddrinfo_ipv4

    def __exit__(self, exc_type, exc, tb):
        socket.getaddrinfo = self._orig

# import network libraries after applying IPv4 preference so they use it
import requests
import httpx

# load .env explicitly to avoid find_dotenv stack issues
load_dotenv(dotenv_path=".env")

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes
except Exception as e:
    print("python-telegram-bot is required. Install with: pip install python-telegram-bot>=20.0")
    raise

from lemonde_today import get_paraphrase, mark_paraphrase_approved, mark_paraphrase_sent_for_approval


TELEGRAM_MESSAGE_LIMIT = 4096


def _split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT):
    """Split text into chunks no longer than limit. Try to split on newlines/spaces."""
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
        # try to find a newline before end
        split_at = text.rfind('\n', start, end)
        if split_at == -1:
            # try space
            split_at = text.rfind(' ', start, end)
        if split_at == -1 or split_at <= start:
            # forced split
            split_at = end
        parts.append(text[start:split_at].rstrip())
        start = split_at
        # skip any whitespace at the new start
        while start < n and text[start] in ('\n', ' ', '\t'):
            start += 1

    return parts


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin = os.getenv("TELEGRAM_ADMIN_ID")
    channel = os.getenv("TELEGRAM_CHANNEL_ID")
    if not token or not admin or not channel:
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_ID / TELEGRAM_CHANNEL_ID in .env")
        return

    try:
        admin_id = int(admin)
        channel_id = int(channel)
    except Exception:
        print("TELEGRAM_ADMIN_ID and TELEGRAM_CHANNEL_ID must be numeric (e.g. -3825586306)")
        return

    # find most recent paraphrase id
    db = "paraphrased_translation.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("SELECT id FROM paraphrases ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()

    if not row:
        print("No paraphrases found in", db)
        return

    pid = row[0]

    async def _send_approval_coro(a):
        # fetch paraphrase fresh
        p = get_paraphrase(pid)
        if not p:
            print("Paraphrase not found for id", pid)
            return

        title = p.get("title") or ""
        body = p.get("paraphrased_text") or ""
        full = (title + "\n\n" + body).strip()
        parts = _split_text(full, TELEGRAM_MESSAGE_LIMIT)

        # Inline buttons only on the first message
        kb = [[InlineKeyboardButton("✅ Yes", callback_data=f"approve:{pid}"), InlineKeyboardButton("❌ No", callback_data=f"reject:{pid}")]]
        reply_markup = InlineKeyboardMarkup(kb)

        sent_ok = False
        try:
            if parts:
                # send first part with buttons
                await a.bot.send_message(chat_id=admin_id, text=parts[0], reply_markup=reply_markup)
                # send remaining parts without buttons
                for part in parts[1:]:
                    await a.bot.send_message(chat_id=admin_id, text=part)
            else:
                await a.bot.send_message(chat_id=admin_id, text="(empty)", reply_markup=reply_markup)
            sent_ok = True
        except Exception as e:
            print("Failed to send approval message:", e)

        # mark as sent_for_approval if initial send succeeded
        if sent_ok:
            try:
                mark_paraphrase_sent_for_approval(pid)
            except Exception:
                pass

    # callback handler
    async def _callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        data = update.callback_query.data or ""
        try:
            action, sid = data.split(":", 1)
            tid = int(sid)
        except Exception:
            await update.callback_query.answer("Invalid callback data")
            return

        if action == "approve":
            row = get_paraphrase(tid)
            if not row:
                await update.callback_query.answer("Paraphrase not found")
                return

            full_text = (row.get("title") or "") + "\n\n" + (row.get("paraphrased_text") or "")
            parts = _split_text(full_text, TELEGRAM_MESSAGE_LIMIT)

            # send with retries to avoid transient httpx connect timeouts
            from telegram.error import TimedOut
            import httpx

            all_sent = True
            for part in parts:
                sent = False
                for attempt in range(3):
                    try:
                        await context.bot.send_message(chat_id=channel_id, text=part)
                        sent = True
                        break
                    except (TimedOut, httpx.ConnectTimeout):
                        # wait and retry with small backoff
                        await asyncio.sleep(0.5 * (attempt + 1))
                    except Exception as e:
                        # other errors, log and break
                        print("Failed to send approved message part:", e)
                        break
                if not sent:
                    all_sent = False
                    print("Failed to send a part after retries; aborting approval")
                    break

            if all_sent:
                try:
                    mark_paraphrase_approved(tid)
                except Exception as e:
                    print("Failed to mark paraphrase approved:", e)

                try:
                    await update.callback_query.answer("Approved and posted")
                except Exception:
                    pass

                try:
                    await update.callback_query.message.edit_reply_markup(None)
                except Exception:
                    pass

                # allow a short moment for any outgoing requests to schedule
                try:
                    await asyncio.sleep(0.25)
                except Exception:
                    pass

                # Stop the Application gracefully by awaiting Application.stop()
                try:
                    app_obj = getattr(context, "application", None)
                    if app_obj is None:
                        # If Application is not on context (unlikely), try to retrieve from bot
                        app_obj = getattr(context.bot, "application", None)

                    if app_obj is not None:
                        # Application.stop() is a coroutine in PTB v20+: await it to shutdown cleanly
                        try:
                            await app_obj.stop()
                        except Exception as e:
                            print("Error while awaiting Application.stop():", e)
                    else:
                        print("Unable to locate Application instance to stop; please stop the process manually.")
                except Exception as e:
                    print("Failed to stop application gracefully:", e)
            else:
                try:
                    await update.callback_query.answer("Failed to post to channel. See logs.")
                except Exception:
                    pass
        elif action == "reject":
            await update.callback_query.answer("Rejected")
            try:
                await update.callback_query.message.edit_reply_markup(None)
            except Exception:
                pass

    # use a custom Request with slightly larger timeouts to reduce spurious connect timeouts
    # quick synchronous pre-check: ensure token and network can reach Telegram API
    def _check_token_connectivity(tok: str, timeout: float = 5.0) -> bool:
        try:
            # prefer IPv4 for the pre-check (scoped)
            with _PreferIPv4():
                resp = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=timeout)
            j = resp.json()
            return bool(j.get("ok"))
        except Exception as e:
            print("Pre-check failed contacting Telegram API:", e)
            return False

    if not _check_token_connectivity(token, timeout=8.0):
        print("Failed to contact Telegram API with provided token. Aborting startup.")
        print("Tips: check TELEGRAM_BOT_TOKEN, internet connectivity, and proxy settings.")
        return

    try:
        from telegram.request import Request
        # build an httpx.AsyncClient that disables HTTP/2 and respects environment proxies
        # create the httpx client while preferring IPv4 to reduce IPv6-related hangs
        with _PreferIPv4():
            httpx_client = httpx.AsyncClient(http2=False, trust_env=True, timeout=httpx.Timeout(30.0, connect=15.0))
        request = Request(connect_timeout=15.0, read_timeout=30.0, pool_timeout=10.0, httpx_client=httpx_client)
        app = ApplicationBuilder().token(token).request(request).post_init(_send_approval_coro).build()
    except Exception:
        # fallback to default request if import fails for some reason
        app = ApplicationBuilder().token(token).post_init(_send_approval_coro).build()

    app.add_handler(CallbackQueryHandler(_callback_router))

    # (previously used a thread to wait for app._loop; replaced with post_init above)

    # start polling (blocking) to handle callbacks; keep running until interrupted
    try:
        app.run_polling()
    finally:
        # close custom httpx client if created
        try:
            if 'httpx_client' in locals() and httpx_client is not None:
                try:
                    asyncio.run(httpx_client.aclose())
                except Exception:
                    pass
        except Exception:
            pass


if __name__ == '__main__':
    main()
