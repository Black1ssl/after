#!/usr/bin/env python3
"""
Telegram menfess (trimmed) — with startup channel check and channel-send fallback.
"""
import atexit
import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from html import escape as escape_html
import requests

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ---------------------------
# CONFIG / LOCK
# ---------------------------
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
LOCK_FILE = os.path.join(DATA_DIR, "bot.lock")
if os.path.exists(LOCK_FILE):
    print("❌ Bot already running (lock file detected). Exiting.")
    raise SystemExit(0)
with open(LOCK_FILE, "w") as f:
    f.write(str(os.getpid()))
def cleanup_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass
atexit.register(cleanup_lock)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set")
    raise SystemExit(1)

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
# MUST set CHANNEL_ID and LOG_CHANNEL_ID correctly (use -100... for channels)
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

TAGS = ["#pria", "#wanita"]
MAX_PHOTO_VIDEO_PER_DAY = int(os.getenv("LIMIT_MENFESS_MEDIA", "10"))
MAX_TEXT_PER_DAY = int(os.getenv("LIMIT_MENFESS_TEXT", "5"))
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
DAILY_SECONDS = 24 * 3600

# ---------------------------
# DB init (sqlite)
# ---------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "users.db"))
db_dir = os.path.dirname(DB_PATH)
try:
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
except Exception:
    DB_PATH = ":memory:"
try:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL;")
except Exception:
    db = sqlite3.connect(":memory:", check_same_thread=False)
db.row_factory = sqlite3.Row
_db_lock = asyncio.Lock()
with db:
    db.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        gender TEXT
    )
    """
    )
    db.execute(
        """
    CREATE TABLE IF NOT EXISTS welcomed_users (
        user_id INTEGER,
        chat_id INTEGER,
        PRIMARY KEY (user_id, chat_id)
    )
    """
    )

# ---------------------------
# In-memory counters / helpers
# ---------------------------
USER_POST_STATS: Dict[int, Dict[str, Union[int, float]]] = {}

def human_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h} jam {m} menit"
    if m:
        return f"{m} menit"
    return "beberapa detik"

def safe_caption(text: Optional[str], limit: int = 1024) -> Optional[str]:
    if not text:
        return None
    txt = str(text).replace("\x00", "")
    return txt[:limit] if len(txt) > limit else txt

def safe_text_message(text: Optional[str], limit: int = 4096) -> str:
    if not text:
        return ""
    txt = str(text).replace("\x00", "")
    return txt[:limit] if len(txt) > limit else txt

def is_admin_id(user_id: int) -> bool:
    return user_id == OWNER_ID

def _reset_post_stats_if_needed(stats):
    now = time.time()
    first_ts = stats.get("first_ts", 0)
    if now - first_ts >= DAILY_SECONDS:
        return {"first_ts": now, "photos_vids": 0, "texts": 0}
    return stats

def is_post_allowed(user_id: int, kind: str) -> Tuple[bool, int]:
    if is_admin_id(user_id):
        return True, 0
    now = time.time()
    stats = USER_POST_STATS.get(user_id)
    if not stats:
        remaining = MAX_PHOTO_VIDEO_PER_DAY if kind == "media" else MAX_TEXT_PER_DAY
        return True, remaining
    stats = _reset_post_stats_if_needed(stats)
    USER_POST_STATS[user_id] = stats
    if kind == "media":
        used = stats.get("photos_vids", 0)
        if used >= MAX_PHOTO_VIDEO_PER_DAY:
            remaining_seconds = int(DAILY_SECONDS - (now - stats["first_ts"]))
            return False, remaining_seconds
        return True, MAX_PHOTO_VIDEO_PER_DAY - used
    else:
        used = stats.get("texts", 0)
        if used >= MAX_TEXT_PER_DAY:
            remaining_seconds = int(DAILY_SECONDS - (now - stats["first_ts"]))
            return False, remaining_seconds
        return True, MAX_TEXT_PER_DAY - used

def increment_post_count(user_id: int, kind: str):
    now = time.time()
    stats = USER_POST_STATS.get(user_id)
    if not stats:
        stats = {"first_ts": now, "photos_vids": 0, "texts": 0}
        USER_POST_STATS[user_id] = stats
    else:
        stats = _reset_post_stats_if_needed(stats)
        USER_POST_STATS[user_id] = stats
    if kind == "media":
        stats["photos_vids"] = stats.get("photos_vids", 0) + 1
    else:
        stats["texts"] = stats.get("texts", 0) + 1

# ---------------------------
# Channel availability flags (set at startup)
# ---------------------------
CHANNEL_OK = False
LOG_CHANNEL_OK = False

async def validate_channels(bot):
    """Check that CHANNEL_ID and LOG_CHANNEL_ID are valid and bot can access them."""
    global CHANNEL_OK, LOG_CHANNEL_OK
    CHANNEL_OK = False
    LOG_CHANNEL_OK = False
    # check channel
    if CHANNEL_ID:
        try:
            await bot.get_chat(CHANNEL_ID)
            CHANNEL_OK = True
            logger.info("CHANNEL_ID reachable")
        except Exception as e:
            CHANNEL_OK = False
            logger.warning("CHANNEL_ID not reachable at startup: %s", e)
    else:
        logger.warning("CHANNEL_ID is not set or zero")
    # check log channel
    if LOG_CHANNEL_ID:
        try:
            await bot.get_chat(LOG_CHANNEL_ID)
            LOG_CHANNEL_OK = True
            logger.info("LOG_CHANNEL_ID reachable")
        except Exception as e:
            LOG_CHANNEL_OK = False
            logger.warning("LOG_CHANNEL_ID not reachable at startup: %s", e)
    else:
        logger.warning("LOG_CHANNEL_ID is not set or zero")

# ---------------------------
# Logging function (uses LOG_CHANNEL_OK)
# ---------------------------
async def send_to_log_channel(context: ContextTypes.DEFAULT_TYPE, msg: Message, gender: str):
    user = msg.from_user
    username = f"@{user.username}" if user.username else "(no username)"
    name = user.first_name or "-"
    user_text = escape_html((msg.caption or msg.text or ""))
    log_caption = (
        f"👤 <b>Nama:</b> {escape_html(name)}\n"
        f"🔗 <b>Username:</b> {escape_html(username)}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"⚧ <b>Gender:</b> #{escape_html(gender)}\n\n"
        f"{user_text}"
    )
    try:
        if LOG_CHANNEL_OK:
            if getattr(msg, "photo", None):
                await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=msg.photo[-1].file_id, caption=log_caption, parse_mode=ParseMode.HTML)
            elif getattr(msg, "video", None):
                await context.bot.send_video(chat_id=LOG_CHANNEL_ID, video=msg.video.file_id, caption=log_caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_caption, parse_mode=ParseMode.HTML)
        else:
            # fallback: DM owner
            await context.bot.send_message(chat_id=OWNER_ID, text=f"[LOG] Bot could not reach LOG_CHANNEL_ID. User post:\n\n{log_caption}", parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Failed to send log (and fallback)")

# ---------------------------
# Handlers
# ---------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return

    text_lower = (msg.text or msg.caption or "") .lower()
    gender = None
    for tag in TAGS:
        if tag in text_lower:
            gender = tag.replace("#", "")
            break
    if not gender:
        await msg.reply_text("❌ Post ditolak.\nWajib pakai #pria atau #wanita")
        return

    user_id = msg.from_user.id
    username = msg.from_user.username
    is_media = bool(getattr(msg, "photo", None) or getattr(msg, "video", None))
    kind = "media" if is_media else "text"

    allowed, rem = is_post_allowed(user_id, kind)
    if not allowed:
        await msg.reply_text(
            f"😅 Kuota kirim { 'foto/video' if kind=='media' else 'teks' } hari ini sudah habis.\n"
            f"⏳ Reset dalam {human_time(rem)}\n"
        )
        return

    # persist gender
    async with _db_lock:
        cur = db.cursor()
        cur.execute("SELECT gender FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row and row["gender"] != gender:
            await msg.reply_text(f"❌ Post ditolak.\nGender akun kamu sudah tercatat sebagai #{row['gender']}.")
            return
        if not row:
            cur.execute("INSERT INTO users (user_id, username, gender) VALUES (?, ?, ?)", (user_id, username, gender))
            db.commit()

    caption_raw = msg.caption if getattr(msg, "caption", None) else (msg.text or "")
    caption_for_media = safe_caption(caption_raw, limit=1024)
    caption_for_text = safe_text_message(caption_raw, limit=4096)

    # Attempt send to channel; on failure fallback to owner DM
    try:
        if CHANNEL_OK:
            if is_media and getattr(msg, "photo", None):
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=msg.photo[-1].file_id, caption=caption_for_media)
            elif is_media and getattr(msg, "video", None):
                await context.bot.send_video(chat_id=CHANNEL_ID, video=msg.video.file_id, caption=caption_for_media)
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=caption_for_text, disable_web_page_preview=True)
            # increment counters
            increment_post_count(user_id, kind)
        else:
            raise BadRequest("CHANNEL_UNAVAILABLE")
    except BadRequest as e:
        logger.exception("Failed to send menfess to channel: %s", e)
        # Fallback: send DM to owner with content + info
        try:
            owner_text = (
                f"[FALLBACK] Failed to post to CHANNEL_ID ({CHANNEL_ID}).\n"
                f"User: @{username} (id: {user_id})\n"
                f"Gender: #{gender}\n\n"
                f"Content:\n{caption_for_text if not is_media else '(media attached)'}"
            )
            await context.bot.send_message(chat_id=OWNER_ID, text=owner_text, disable_web_page_preview=True)
        except Exception:
            logger.exception("Failed to notify owner about failed post")
        await msg.reply_text("⚠️ Posting ke channel gagal; admin telah diberitahu.")
        return
    except Exception:
        logger.exception("Failed to send menfess to channel (unexpected)")
        await msg.reply_text("❌ Gagal mengirim menfess. Silakan coba lagi.")
        return

    # send log (or fallback)
    try:
        await send_to_log_channel(context, msg, gender)
    except Exception:
        logger.exception("Failed to send log after menfess")

    await msg.reply_text("✅ Post berhasil dikirim.")

# welcome_new_member, anti_link, moderation, tag handlers (unchanged — omitted here for brevity)
# For completeness we re-use simpler versions:

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception:
        pass
    for user in msg.new_chat_members:
        if user.is_bot:
            continue
        user_id = user.id
        with db:
            cur = db.cursor()
            cur.execute("SELECT 1 FROM welcomed_users WHERE user_id=? AND chat_id=?", (user_id, chat_id))
            if cur.fetchone():
                continue
            cur.execute("INSERT INTO welcomed_users (user_id, chat_id) VALUES (?, ?)", (user_id, chat_id))
        await context.bot.send_message(chat_id=chat_id, text=f"👋 Selamat datang {escape_html(user.first_name or '')}!", parse_mode=ParseMode.HTML)

async def anti_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    user = msg.from_user
    chat = msg.chat
    if user.is_bot:
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if member and member.status in ("administrator", "creator"):
        return
    try:
        await msg.delete()
    except Exception:
        pass
    until_date = int(time.time()) + 3600
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id, until_date=until_date)
        await context.bot.send_message(chat_id=chat.id, text=(f"🚫 {escape_html(user.first_name or '')} diblokir 1 jam\nAlasan: Mengirim link"), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Ban gagal")

# Other moderation handlers (unban_user, ban_user, kick_user, tag_member) omitted for brevity
# Use your previous implementations here unchanged (they are compatible).

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (reuse previous function body)
    await update.message.reply_text("Unban placeholder (implement as before).")

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ban placeholder (implement as before).")

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Kick placeholder (implement as before).")

async def tag_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tag placeholder (implement as before).")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    all_features = (
        "📚 Fitur Bot (singkat):\n\n"
        "- Menfess via private: kirim teks/foto/video dengan tag #pria atau #wanita\n"
        "- Limit menfess per hari: foto/video dan teks\n"
        "- Moderation: /tag, /ban, /kick, /unban\n"
    )
    await msg.reply_text(all_features)

# ---------------------------
# MAIN (register handlers + validate channels)
# ---------------------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set.")
        return

    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=5)
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()

    # validate channels before registering handlers (so we know CHANNEL_OK/LOG_CHANNEL_OK)
    try:
        # run async validate on the bot object
        asyncio.run(validate_channels(app.bot))
    except Exception as e:
        logger.exception("Channel validation failed at startup: %s", e)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.Entity("url") & ~filters.Entity("text_link") & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.Entity("url") | filters.Entity("text_link")), anti_link))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("kick", kick_user))
    app.add_handler(CommandHandler("tag", tag_member))
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
