#!/usr/bin/env python3
"""
Telegram menfess / downloader bot (trimmed)

Perubahan utama:
- Hapus semua fitur yang diminta:
  - DEFAULT_MALE_IMAGE / DEFAULT_FEMALE_IMAGE (default images)
  - ADMIN_IDS parsing and usage (only OWNER_ID remains as admin)
  - URL detection & download flow (download_video, quality_callback, yt-dlp, aiohttp)
  - /tagall command and implementation
  - MP3-related logic and FFmpeg references
- Menjaga fitur menfess (forward), welcome, anti-link, moderation, tag (single), help, logging.
- Menjaga database (users, welcomed_users) dan limits per-post.
"""
import atexit
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from html import escape as escape_html
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# NOTE: ADMIN_IDS removed as requested. Only OWNER_ID is considered admin.
TAGS = ["#pria", "#wanita"]

# Limits
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
# In-memory counters / locks (for posting limits)
# ---------------------------
USER_POST_STATS: Dict[int, Dict[str, Union[int, float]]] = {}

# ---------------------------
# Utilities
# ---------------------------
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
    # Only OWNER_ID considered admin now
    return user_id == OWNER_ID

# ---------------------------
# Post limits helpers
# ---------------------------
def _reset_post_stats_if_needed(stats: Dict[str, Union[int, float]]) -> Dict[str, Union[int, float]]:
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
# Logging to LOG_CHANNEL
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
        if getattr(msg, "photo", None):
            await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=msg.photo[-1].file_id, caption=log_caption, parse_mode=ParseMode.HTML)
        elif getattr(msg, "video", None):
            await context.bot.send_video(chat_id=LOG_CHANNEL_ID, video=msg.video.file_id, caption=log_caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_caption, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Gagal mengirim log")

# ---------------------------
# Handlers
# ---------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return

    text_lower = (msg.text or msg.caption or "").lower()
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
            f"📅 Batas: {MAX_PHOTO_VIDEO_PER_DAY if kind=='media' else MAX_TEXT_PER_DAY} per hari"
        )
        return

    # persist gender immutably
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

    try:
        if is_media:
            if getattr(msg, "photo", None):
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=msg.photo[-1].file_id, caption=caption_for_media)
            elif getattr(msg, "video", None):
                await context.bot.send_video(chat_id=CHANNEL_ID, video=msg.video.file_id, caption=caption_for_media)
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=caption_for_text, disable_web_page_preview=True)
            increment_post_count(user_id, "media")
        else:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=caption_for_text, disable_web_page_preview=True)
            increment_post_count(user_id, "text")
    except Exception:
        logger.exception("Failed to send menfess to channel")
        await msg.reply_text("❌ Gagal mengirim menfess. Silakan coba lagi.")
        return

    try:
        await send_to_log_channel(context, msg, gender)
    except Exception:
        logger.exception("Failed to send log after menfess")

    await msg.reply_text("✅ Post berhasil dikirim.")

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

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"👋 Selamat datang <b>{escape_html(user.first_name or '')}</b>!\n\n"
                "📌 <b>Peraturan Grup:</b>\n"
                "• No rasis 🚫\n"
                "• Jangan spam 🚫\n"
                "• Post menfess via bot\n\n"
                "🔗 Bot menfess: @sixafter_bot\n"
                "🔗 Channel menfess: https://t.me/sixafter0\n\n"
                "Semoga betah ya 😊"
            ),
            parse_mode=ParseMode.HTML,
        )

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
    except BadRequest:
        pass
    except Exception:
        pass
    until_date = int(time.time()) + 3600
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id, until_date=until_date)
        await context.bot.send_message(chat_id=chat.id, text=(f"🚫 <b>{escape_html(user.first_name or '')}</b> diblokir 1 jam\nAlasan: Mengirim link"), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Ban gagal")

# Moderation commands (unban/ban/kick/tag)
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    user = msg.from_user
    chat = msg.chat
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah ini hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya pemilik grup atau admin yang bisa menggunakan perintah ini.")
        return
    args = context.args
    if not args:
        await msg.reply_text("❌ Gunakan: /unban <user_id>")
        return
    try:
        target_user_id = int(args[0])
    except ValueError:
        await msg.reply_text("❌ User ID harus berupa angka.")
        return
    try:
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_user_id)
        await msg.reply_text(f"✅ User {target_user_id} telah di-unban.")
    except Exception as e:
        await msg.reply_text(f"❌ Gagal unban: {str(e)}")

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah /ban hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang dapat menggunakan /ban.")
        return
    if not context.args:
        await msg.reply_text("Gunakan: /ban <user_id> [hours]")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await msg.reply_text("User ID harus berupa angka.")
        return
    hours = None
    if len(context.args) >= 2:
        try:
            hours = float(context.args[1])
        except ValueError:
            hours = None
    until_date = int(time.time() + hours * 3600) if hours else None
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_user_id, until_date=until_date)
        if until_date:
            await msg.reply_text(f"✅ User {target_user_id} diban selama {hours} jam.")
        else:
            await msg.reply_text(f"✅ User {target_user_id} diban permanen (sampai di-unban).")
    except Exception as e:
        logger.exception("Gagal ban: %s", e)
        await msg.reply_text(f"❌ Gagal ban: {e}")

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah /kick hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang dapat menggunakan /kick.")
        return
    target_id = None
    if msg.reply_to_message:
        target_id = msg.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await msg.reply_text("User ID harus berupa angka atau gunakan reply ke pesan user.")
            return
    else:
        await msg.reply_text("Gunakan: reply ke pesan user + /kick atau /kick <user_id>")
        return
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id, until_date=int(time.time() + 30))
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_id)
        await msg.reply_text(f"✅ User {target_id} telah dikick (di-remove).")
    except Exception as e:
        logger.exception("Gagal kick: %s", e)
        await msg.reply_text(f"❌ Gagal kick: {e}")

async def tag_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah /tag hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang dapat menandai member.")
        return
    parts = context.args or []
    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_id = target.id
        text_to_send = " ".join(parts) if parts else "(ditandai oleh admin)"
    else:
        if not parts:
            await msg.reply_text("Gunakan: /tag <user_id> <pesan>  atau reply + /tag <pesan>")
            return
        first = parts[0]
        rest = parts[1:]
        text_to_send = " ".join(rest) if rest else "(ditandai oleh admin)"
        if first.startswith("@"):
            await msg.reply_text("Gunakan reply atau user_id. Mention by @username tidak didukung, gunakan reply atau user id.")
            return
        try:
            target_id = int(first)
        except ValueError:
            await msg.reply_text("User ID tidak valid.")
            return
    try:
        mention = f'<a href="tg://user?id={target_id}">disini</a>'
        await context.bot.send_message(chat_id=chat.id, text=f"🔔 {mention}\n\n{text_to_send}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Gagal menandai member: %s", e)
        await msg.reply_text(f"❌ Gagal menandai member: {e}")

# ---------------------------
# HELP
# ---------------------------
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
# MAIN (register handlers)
# ---------------------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set.")
        return

    # try delete webhook to avoid conflicts
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=5)
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()

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
