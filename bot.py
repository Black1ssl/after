#!/usr/bin/env python3
"""
Telegram menfess / downloader bot (merged + fixed)

Features:
- Single-instance lockfile
- SQLite persistence (tables: users, welcomed_users, usage_log, last_actions)
- Daily usage limits (download, menfess_text, menfess_media) stored in usage_log (per date)
- Persisted cooldowns stored in last_actions (survive restart)
- Admins (ADMIN_IDS) bypass limits and cooldowns; admin actions do NOT increment usage
- Menfess flow: requires #pria or #wanita in message (text or media). Forwards to CHANNEL_ID and logs to LOG_CHANNEL_ID
  - For text-only menfess, the bot will attach a default photo based on gender (male/female) when configured.
- Download flow: supports direct image URL download and yt-dlp for video/audio (choice via inline keyboard)
- Safe DB access via asyncio.Lock
- Uses python-telegram-bot v20+ async API
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
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from html import escape as escape_html
from yt_dlp import YoutubeDL

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ---------------------------
# SINGLE INSTANCE LOCK
# ---------------------------
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
LOCK_FILE = os.path.join(DATA_DIR, "bot.lock")

if os.path.exists(LOCK_FILE):
    print("❌ Bot already running (lock file detected). Exiting.")
    raise SystemExit(0)

with open(LOCK_FILE, "w") as f:
    f.write(str(os.getpid()))

print("✅ Lock acquired, bot starting...")

def cleanup_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            print("🧹 Lock file removed, bot stopped cleanly.")
    except Exception:
        pass

atexit.register(cleanup_lock)

# ---------------------------
# LOGGING & CONFIG
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set")
    raise SystemExit(1)

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Admins: comma-separated environment variable
_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set()
if _ADMIN_IDS_RAW:
    try:
        ADMIN_IDS = set(int(i.strip()) for i in _ADMIN_IDS_RAW.split(",") if i.strip())
    except Exception:
        ADMIN_IDS = set()

TAGS = ["#pria", "#wanita"]

# Default images (for text-only menfess)
# These can be local file paths (absolute or relative) or HTTP(S) URLs.
DEFAULT_MALE_IMAGE = os.getenv("DEFAULT_MALE_IMAGE", "")   # e.g. /app/data/default_male.jpg or https://...
DEFAULT_FEMALE_IMAGE = os.getenv("DEFAULT_FEMALE_IMAGE", "") # e.g. /app/data/default_female.jpg or https://...

# ---------------------------
# LIMITS / COOLDOWNS
# ---------------------------
LIMITS = {
    "download": int(os.getenv("LIMIT_DOWNLOAD", "2")),
    "menfess_text": int(os.getenv("LIMIT_MENFESS_TEXT", "5")),
    "menfess_media": int(os.getenv("LIMIT_MENFESS_MEDIA", "10")),
}

COOLDOWNS = {
    "download": int(os.getenv("CD_DOWNLOAD", "5")),
    "menfess_text": int(os.getenv("CD_MENFESS_TEXT", "3")),
    "menfess_media": int(os.getenv("CD_MENFESS_MEDIA", "5")),
}

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
DAILY_SECONDS = 24 * 3600

# ---------------------------
# DB (SQLite) init
# ---------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "users.db"))
db_dir = os.path.dirname(DB_PATH)
try:
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
except Exception as e:
    logger.exception("Failed making DB dir %s: %s", db_dir, e)
    DB_PATH = ":memory:"

try:
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL;")
except Exception:
    logger.exception("Failed open DB, switching to memory")
    _conn = sqlite3.connect(":memory:", check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL;")

_conn.row_factory = sqlite3.Row
_db_lock = asyncio.Lock()

def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        gender TEXT
    )
    """
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS welcomed_users (
        user_id INTEGER,
        chat_id INTEGER,
        PRIMARY KEY (user_id, chat_id)
    )
    """
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS usage_log (
        user_id INTEGER,
        usage_type TEXT,
        count INTEGER DEFAULT 0,
        date TEXT,
        PRIMARY KEY (user_id, usage_type, date)
    )
    """
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS last_actions (
        user_id INTEGER,
        usage_type TEXT,
        last_ts REAL,
        PRIMARY KEY (user_id, usage_type)
    )
    """
    )
    conn.commit()

init_db(_conn)

def get_db_conn() -> sqlite3.Connection:
    return _conn

# ---------------------------
# Utilities: URL / image detection
# ---------------------------
URL_RE = re.compile(r"https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+|telegram\.me/[^\s]+", flags=re.IGNORECASE)

def extract_first_url(msg: Message) -> Optional[str]:
    if not msg:
        return None
    entities = msg.entities or []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            return ent.url
        if ent.type == "url" and msg.text:
            return msg.text[ent.offset : ent.offset + ent.length]
    entities = msg.caption_entities or []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            return ent.url
        if ent.type == "url" and msg.caption:
            return msg.caption[ent.offset : ent.offset + ent.length]
    hay = (msg.text or "") + " " + (msg.caption or "")
    m = URL_RE.search(hay)
    return m.group(0) if m else None

def is_image_url(url: str) -> bool:
    if not url:
        return False
    url = url.lower().split("?")[0]
    return any(url.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))

def human_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h} jam {m} menit"
    if m:
        return f"{m} menit"
    return "beberapa detik"

# ---------------------------
# Admin helper
# ---------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id == OWNER_ID

# ---------------------------
# Usage and cooldown (DB-backed)
# ---------------------------
async def check_limit(user_id: int, usage_type: str) -> bool:
    """Return True if allowed (used < limit) or no limit set. Admins bypass."""
    if is_admin(user_id):
        return True
    limit = LIMITS.get(usage_type)
    if limit is None:
        return True
    today = date.today().isoformat()
    conn = get_db_conn()
    async with _db_lock:
        cur = conn.cursor()
        cur.execute("SELECT count FROM usage_log WHERE user_id=? AND usage_type=? AND date=?", (user_id, usage_type, today))
        row = cur.fetchone()
        used = row["count"] if row else 0
    return used < limit

async def increment_usage(user_id: int, usage_type: str):
    """Increment today's usage. Admins do NOT increment."""
    if is_admin(user_id):
        return
    today = date.today().isoformat()
    conn = get_db_conn()
    async with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
        INSERT INTO usage_log (user_id, usage_type, count, date)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(user_id, usage_type, date)
        DO UPDATE SET count = count + 1
        """,
            (user_id, usage_type, today),
        )
        conn.commit()

async def get_usage_today(user_id: int, usage_type: Optional[str] = None) -> Tuple[int, Optional[int]]:
    """Return (used, limit) for a usage_type or total used (limit None)."""
    conn = get_db_conn()
    today = date.today().isoformat()
    async with _db_lock:
        cur = conn.cursor()
        if usage_type:
            cur.execute("SELECT count FROM usage_log WHERE user_id=? AND usage_type=? AND date=?", (user_id, usage_type, today))
            row = cur.fetchone()
            used = row["count"] if row else 0
            limit = LIMITS.get(usage_type)
            return used, limit
        else:
            cur.execute("SELECT SUM(count) as s FROM usage_log WHERE user_id=? AND date=?", (user_id, today))
            row = cur.fetchone()
            used = int(row["s"]) if row and row["s"] is not None else 0
            return used, None

# Persisted cooldowns
async def get_last_action_db(user_id: int, usage_type: str) -> Optional[float]:
    conn = get_db_conn()
    async with _db_lock:
        cur = conn.cursor()
        cur.execute("SELECT last_ts FROM last_actions WHERE user_id=? AND usage_type=?", (user_id, usage_type))
        row = cur.fetchone()
        return float(row["last_ts"]) if row and row["last_ts"] is not None else None

async def set_last_action_db(user_id: int, usage_type: str, ts: Optional[float] = None):
    if ts is None:
        ts = time.time()
    conn = get_db_conn()
    async with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
        INSERT INTO last_actions (user_id, usage_type, last_ts)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, usage_type) DO UPDATE SET last_ts=excluded.last_ts
        """,
            (user_id, usage_type, ts),
        )
        conn.commit()

async def is_on_cooldown(user_id: int, usage_type: str) -> Tuple[bool, int]:
    """Return (on_cooldown, seconds_left). Admins bypass."""
    if is_admin(user_id):
        return False, 0
    now = time.time()
    last = await get_last_action_db(user_id, usage_type)
    cd = COOLDOWNS.get(usage_type, 0)
    if last is None:
        return False, 0
    left = int(cd - (now - last))
    if left > 0:
        return True, left
    return False, 0

# ---------------------------
# Menfess helpers (validate and record user gender)
# ---------------------------
async def ensure_user_gender(user_id: int, username: Optional[str], gender: str) -> Tuple[bool, Optional[str]]:
    """
    Ensure gender consistency. Returns (ok, existing_gender_or_none).
    Gender is immutable once set (cannot be changed) — persistence in 'users' table.
    """
    conn = get_db_conn()
    async with _db_lock:
        cur = conn.cursor()
        cur.execute("SELECT gender FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            existing = row["gender"]
            if existing != gender:
                return False, existing
            return True, existing
        else:
            cur.execute("INSERT INTO users (user_id, username, gender) VALUES (?, ?, ?)", (user_id, username, gender))
            conn.commit()
            return True, None

async def send_to_log_channel(context: ContextTypes.DEFAULT_TYPE, msg: Message, gender: str, default_photo: Optional[str] = None):
    """
    Send a log copy to LOG_CHANNEL_ID. If default_photo provided (path or URL), send photo + caption similar to public channel.
    """
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
        if default_photo:
            # default_photo may be URL or local path
            if default_photo.startswith("http://") or default_photo.startswith("https://"):
                await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=default_photo, caption=log_caption, parse_mode=ParseMode.HTML)
            elif os.path.exists(default_photo):
                with open(default_photo, "rb") as fh:
                    await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=fh, caption=log_caption, parse_mode=ParseMode.HTML)
            else:
                # fallback to text log
                await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_caption, parse_mode=ParseMode.HTML)
        else:
            # if original msg has media, use it; otherwise send text log
            if getattr(msg, "photo", None):
                await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=msg.photo[-1].file_id, caption=log_caption, parse_mode=ParseMode.HTML)
            elif getattr(msg, "video", None):
                await context.bot.send_video(chat_id=LOG_CHANNEL_ID, video=msg.video.file_id, caption=log_caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_caption, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Failed to send log")

# ---------------------------
# HANDLERS
# ---------------------------

USER_ACTIVE_DOWNLOAD: set[int] = set()
download_lock = asyncio.Semaphore(1)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Private menfess handler: accepts text or media (photo/video) as menfess.
    Requires #pria or #wanita in message/caption.
    For text-only menfess, attach a default photo for the corresponding gender if configured.
    Gender is immutable once set in DB (cannot be changed).
    """
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

    ok, existing = await ensure_user_gender(user_id, username, gender)
    if not ok:
        await msg.reply_text(f"❌ Post ditolak.\nGender akun kamu sudah tercatat sebagai #{existing}.")
        return

    is_media = bool(getattr(msg, "photo", None) or getattr(msg, "video", None))
    usage_type = "menfess_media" if is_media else "menfess_text"

    # cooldown
    on_cd, left = await is_on_cooldown(user_id, usage_type)
    if on_cd:
        await msg.reply_text(f"⏳ Tunggu {left}s sebelum mengirim { 'foto/video' if is_media else 'teks' } lagi.")
        return

    # limit
    allowed = await check_limit(user_id, usage_type)
    if not allowed:
        limit = LIMITS.get(usage_type)
        used, _ = await get_usage_today(user_id, usage_type)
        await msg.reply_text(
            f"😅 Kuota kirim { 'foto/video' if is_media else 'teks' } hari ini sudah habis.\n"
            f"📅 Batas: {limit} per hari\n"
            f"📌 Penggunaan hari ini: {used}/{limit}\n"
            f"⏳ Coba lagi besok"
        )
        return

    caption = msg.caption or msg.text or ""

    # Decide default photo for text-only menfess
    default_photo = None
    if not is_media:
        if gender == "pria" and DEFAULT_MALE_IMAGE:
            default_photo = DEFAULT_MALE_IMAGE
        elif gender == "wanita" and DEFAULT_FEMALE_IMAGE:
            default_photo = DEFAULT_FEMALE_IMAGE

    # attempt to forward to public channel, only increment count if success
    try:
        if is_media:
            if getattr(msg, "photo", None):
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=msg.photo[-1].file_id, caption=caption)
            elif getattr(msg, "video", None):
                await context.bot.send_video(chat_id=CHANNEL_ID, video=msg.video.file_id, caption=caption)
        else:
            if default_photo:
                # default_photo can be a URL or local file path
                if default_photo.startswith("http://") or default_photo.startswith("https://"):
                    await context.bot.send_photo(chat_id=CHANNEL_ID, photo=default_photo, caption=caption)
                elif os.path.exists(default_photo):
                    with open(default_photo, "rb") as fh:
                        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=fh, caption=caption)
                else:
                    # fallback to text message if file missing
                    await context.bot.send_message(chat_id=CHANNEL_ID, text=caption)
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=caption)
    except Exception as e:
        logger.exception("Failed to send menfess to channel: %s", e)
        await msg.reply_text(f"❌ Gagal mengirim ke channel publik: {e}")
        return

    # success -> log, set cooldown, increment usage (admins skipped)
    await send_to_log_channel(context, msg, gender, default_photo=default_photo)
    await set_last_action_db(user_id, usage_type)
    await increment_usage(user_id, usage_type)

    # reply
    if is_admin(user_id):
        await msg.reply_text("✅ Post berhasil dikirim (admin: unlimited).")
    else:
        used, limit = await get_usage_today(user_id, usage_type)
        await msg.reply_text(f"✅ Post berhasil dikirim ({used}/{limit}).")

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
        async with _db_lock:
            cur = get_db_conn().cursor()
            cur.execute("SELECT 1 FROM welcomed_users WHERE user_id=? AND chat_id=?", (user_id, chat_id))
            if cur.fetchone():
                continue
            cur.execute("INSERT INTO welcomed_users (user_id, chat_id) VALUES (?, ?)", (user_id, chat_id))
            get_db_conn().commit()

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

# ---------------------------
# Moderation commands (ban/kick/unban)
# ---------------------------
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
    until_date = None
    if hours:
        until_date = int(time.time() + hours * 3600)
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

# ---------------------------
# DOWNLOAD FLOW
# ---------------------------
async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    user_id = msg.from_user.id
    url = extract_first_url(msg)
    if not url:
        await msg.reply_text("❌ Tidak menemukan URL di pesan.")
        return

    # image direct URL handling
    if is_image_url(url):
        # cooldown and limit check for download
        on_cd, left = await is_on_cooldown(user_id, "download")
        if on_cd:
            await msg.reply_text(f"⏳ Tunggu {left}s sebelum melakukan download lagi.")
            return
        allowed = await check_limit(user_id, "download")
        if not allowed:
            used, limit = await get_usage_today(user_id, "download")
            await msg.reply_text("😅 Kuota download hari ini sudah habis\n\n" f"📅 Limit: {limit} download / hari\n" f"📌 Penggunaan: {used}/{limit}\n" f"⏳ Coba lagi besok")
            return

        await msg.reply_text("⏳ Mengunduh foto...")
        tmpf_name = None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content_length = resp.headers.get("Content-Length")
                    if content_length and int(content_length) > TELEGRAM_MAX_BYTES:
                        await msg.reply_text("❌ Foto lebih besar dari 50MB, tidak dapat dikirim.")
                        return
                    data = await resp.read()
                    if len(data) > TELEGRAM_MAX_BYTES:
                        await msg.reply_text("❌ Foto lebih besar dari 50MB, tidak dapat dikirim.")
                        return
                    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=Path(url).suffix or ".jpg")
                    tmpf.write(data)
                    tmpf.flush()
                    tmpf.close()
                    tmpf_name = tmpf.name

            # send to user (private)
            try:
                with open(tmpf_name, "rb") as fh:
                    try:
                        await context.bot.send_photo(chat_id=user_id, photo=fh)
                    except Exception:
                        fh.seek(0)
                        await context.bot.send_document(chat_id=user_id, document=fh)
                # success -> persist usage + cooldown
                await increment_usage(user_id, "download")
                await set_last_action_db(user_id, "download")
                if is_admin(user_id):
                    await msg.reply_text("✅ Foto berhasil dikirim (admin: unlimited).")
                else:
                    used, limit = await get_usage_today(user_id, "download")
                    await msg.reply_text(f"✅ Foto berhasil dikirim ({used}/{limit}).")
            except Exception as e:
                logger.exception("Failed send photo to user: %s", e)
                # no increment on failure (we increment only on success)
                await msg.reply_text(f"❌ Gagal mengirim foto: {e}")
        except Exception as e:
            logger.exception("Gagal mengunduh foto: %s", e)
            await msg.reply_text(f"❌ Gagal mengunduh foto: {e}")
        finally:
            try:
                if tmpf_name and os.path.exists(tmpf_name):
                    os.unlink(tmpf_name)
            except Exception:
                pass
        return

    # video/audio flow: prompt quality
    context.user_data["download_url"] = url
    keyboard = [
        [InlineKeyboardButton("360p", callback_data="q_360"), InlineKeyboardButton("720p", callback_data="q_720")],
        [InlineKeyboardButton("🎵 MP3", callback_data="q_mp3")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.reply_text("Pilih kualitas download:", reply_markup=reply_markup)

async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data
    url = context.user_data.get("download_url")
    if not url:
        await query.edit_message_text("❌ URL tidak ditemukan. Kirim ulang link.")
        return
    if user_id in USER_ACTIVE_DOWNLOAD:
        await query.answer("⏳ Download kamu masih berjalan", show_alert=True)
        return

    on_cd, left = await is_on_cooldown(user_id, "download")
    if on_cd:
        await query.edit_message_text(f"⏳ Tunggu {left}s sebelum coba lagi.")
        return
    allowed = await check_limit(user_id, "download")
    if not allowed:
        used, limit = await get_usage_today(user_id, "download")
        await query.edit_message_text("😅 Kuota download hari ini sudah habis\n\n" f"📅 Limit: {limit} download / hari\n" f"📌 Penggunaan: {used}/{limit}\n" f"⏳ Coba lagi besok")
        return

    await query.edit_message_text("⏳ Mengunduh, mohon tunggu...")
    tmpdir = None
    try:
        async with download_lock:
            USER_ACTIVE_DOWNLOAD.add(user_id)
            # we will increment usage AFTER successful send to user
            tmpdir = tempfile.mkdtemp(prefix="yt-dl-")
            out_template = str(Path(tmpdir) / "output.%(ext)s")
            ffmpeg_available = shutil.which("ffmpeg") is not None

            if data == "q_mp3":
                if not ffmpeg_available:
                    await query.edit_message_text("⚠️ Konversi ke MP3 memerlukan ffmpeg yang tidak tersedia di server.")
                    return
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": out_template,
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
                }
            else:
                max_h = 360 if data == "q_360" else 720
                if ffmpeg_available:
                    fmt = f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]"
                    ydl_opts = {"format": fmt, "outtmpl": out_template, "merge_output_format": "mp4", "quiet": True, "no_warnings": True, "noplaylist": True}
                else:
                    fmt = "best"
                    ydl_opts = {"format": fmt, "outtmpl": out_template, "quiet": True, "no_warnings": True, "noplaylist": True}

            def run_ydl():
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            await asyncio.to_thread(run_ydl)

            files = list(Path(tmpdir).iterdir())
            if not files:
                raise RuntimeError("Download gagal — tidak ada file output dari yt-dlp.")
            files_sorted = sorted(files, key=lambda p: p.stat().st_size, reverse=True)
            output_file = files_sorted[0]
            size_bytes = output_file.stat().st_size
            logger.info("Downloaded file: %s (%d bytes)", output_file, size_bytes)

            if size_bytes > TELEGRAM_MAX_BYTES:
                await query.edit_message_text("❌ File lebih besar dari 50MB sehingga tidak dapat dikirim melalui Bot Telegram.\nSilakan unduh langsung dari sumber (link) atau gunakan metode lain.")
                return

            suffix = output_file.suffix.lower()
            try:
                with open(output_file, "rb") as fh:
                    if suffix in (".mp4", ".mkv", ".webm", ".mov"):
                        await context.bot.send_video(chat_id=user_id, video=fh)
                    elif suffix in (".mp3", ".m4a", ".aac", ".opus"):
                        await context.bot.send_audio(chat_id=user_id, audio=fh)
                    else:
                        await context.bot.send_document(chat_id=user_id, document=fh)
            except Exception as e:
                logger.exception("Failed sending downloaded file: %s", e)
                raise RuntimeError(f"Gagal mengirim file ke pengguna: {e}")

            # success -> persist usage + cooldown
            await set_last_action_db(user_id, "download")
            await increment_usage(user_id, "download")

            if is_admin(user_id):
                await query.edit_message_text("✅ Download selesai. File telah dikirim (admin: unlimited).")
            else:
                used, limit = await get_usage_today(user_id, "download")
                await query.edit_message_text(f"✅ Download selesai. Penggunaan hari ini: {used}/{limit}")
    except Exception as exc:
        logger.exception("Error during download: %s", exc)
        try:
            await query.edit_message_text(f"❌ Gagal mengunduh: {exc}")
        except Exception:
            pass
    finally:
        USER_ACTIVE_DOWNLOAD.discard(user_id)
        try:
            if tmpdir and Path(tmpdir).exists():
                shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

# ---------------------------
# TAG / ADMIN utilities
# ---------------------------
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
    text_to_send = ""
    target_id = None
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
        else:
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

async def tag_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah /tagall hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang dapat menggunakan /tagall.")
        return

    custom_text = None
    if context.args:
        custom_text = " ".join(context.args)
    elif msg.reply_to_message and msg.reply_to_message.text:
        custom_text = msg.reply_to_message.text

    async with _db_lock:
        cur = get_db_conn().cursor()
        cur.execute("SELECT user_id FROM welcomed_users WHERE chat_id=?", (chat.id,))
        rows = cur.fetchall()
    user_ids = [r[0] for r in rows if r and isinstance(r[0], int)]
    if not user_ids:
        await msg.reply_text("Tidak ada user yang tersimpan untuk ditandai.")
        return

    seen = set()
    deduped = [uid for uid in user_ids if not (uid in seen or seen.add(uid))]
    MAX_TOTAL = 1000
    if len(deduped) > MAX_TOTAL:
        await msg.reply_text(f"⚠️ Terdapat {len(deduped)} user, terlalu banyak untuk ditag sekaligus.")
        return

    batch_size = 20
    sent_batches = 0
    try:
        for i in range(0, len(deduped), batch_size):
            batch = deduped[i : i + batch_size]
            mentions = " ".join(f'<a href="tg://user?id={uid}">.</a>' for uid in batch)
            body = custom_text or "Perhatian dari admin."
            text = f"🔔 Panggilan untuk semua:\n{mentions}\n\n{body}"
            await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.HTML)
            sent_batches += 1
            await asyncio.sleep(1)
    except Exception as e:
        logger.exception("Error saat mengirim tagall: %s", e)
        await msg.reply_text(f"❌ Gagal mengirim tagall: {e}")
        return

    await msg.reply_text(f"✅ Selesai mengirim tag kepada {len(deduped)} user dalam {sent_batches} batch.")

# ---------------------------
# HELP
# ---------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    all_features = (
        "📚 Fitur Bot (lengkap):\n\n"
        "- Menfess via private: kirim teks/foto/video dengan tag #pria atau #wanita\n"
        "- Untuk teks menfess, bot akan menambahkan foto default sesuai gender jika dikonfigurasi\n"
        "- Download video/audio dari link (YouTube/TikTok/IG/...) pilih 360p/720p/MP3\n"
        "- Download foto dari direct image URL\n"
        f"- Batas file dikirim oleh bot: {TELEGRAM_MAX_BYTES//(1024*1024)} MB\n"
        f"- Limit download: {LIMITS.get('download')}x per hari per user\n"
        f"- Limit menfess per hari: foto/video {LIMITS.get('menfess_media')}x, teks {LIMITS.get('menfess_text')}x\n\n"
        "Admin commands: /tag /tagall /ban /kick /unban\n"
    )
    await msg.reply_text(all_features)

# ---------------------------
# MAIN: register handlers & start
# ---------------------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set.")
        return
    if not CHANNEL_ID or not LOG_CHANNEL_ID:
        logger.warning("CHANNEL_ID or LOG_CHANNEL_ID not set; menfess or log may fail.")

    if ADMIN_IDS:
        logger.info("Admin IDs: %s", ADMIN_IDS)
    if OWNER_ID:
        logger.info("Owner ID: %s", OWNER_ID)
    if DEFAULT_MALE_IMAGE:
        logger.info("Default male image configured: %s", DEFAULT_MALE_IMAGE)
    if DEFAULT_FEMALE_IMAGE:
        logger.info("Default female image configured: %s", DEFAULT_FEMALE_IMAGE)

    # attempt to delete webhook to avoid conflicts
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=5)
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()

    # Private menfess handler: exclude messages that contain url/text_link and commands
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.Entity("url") & ~filters.Entity("text_link") & ~filters.COMMAND, handle_message)
    )
    # Welcome
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    # Anti-link in groups
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.Entity("url") | filters.Entity("text_link")), anti_link))
    # Moderation
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("kick", kick_user))
    # Download handlers (private messages with links)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.Entity("url") | filters.Entity("text_link")), download_video))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))
    # Tagging
    app.add_handler(CommandHandler("tag", tag_member))
    app.add_handler(CommandHandler("tagall", tag_all))
    # help
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
