#!/usr/bin/env python3
"""
Telegram menfess / downloader bot (final, merged & fixed)

Perbaikan utama di versi ini:
- Lengkapi download_video() dan quality_callback() sehingga handler terdaftar aman
- Gunakan is_user_allowed / increment/decrement usage di flow download
- Admin (OWNER_ID / ADMIN_IDS) dibypass untuk limit
- anti_link: cek status member dulu, jangan delete sebelum pengecekan admin
- Safe caption truncation, logging, dan fallback
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
from typing import Dict, Optional, Tuple

import aiohttp
import requests
from html import escape as escape_html
from yt_dlp import YoutubeDL

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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

_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set()
if _ADMIN_IDS_RAW:
    try:
        ADMIN_IDS = set(int(i.strip()) for i in _ADMIN_IDS_RAW.split(",") if i.strip())
    except Exception:
        ADMIN_IDS = set()

TAGS = ["#pria", "#wanita"]
DEFAULT_MALE_IMAGE = os.getenv("DEFAULT_MALE_IMAGE", "").strip()
DEFAULT_FEMALE_IMAGE = os.getenv("DEFAULT_FEMALE_IMAGE", "").strip()

# Limits / constants
MAX_DAILY = int(os.getenv("LIMIT_DOWNLOAD", "2"))
MAX_PHOTO_VIDEO_PER_DAY = int(os.getenv("LIMIT_MENFESS_MEDIA", "10"))
MAX_TEXT_PER_DAY = int(os.getenv("LIMIT_MENFESS_TEXT", "5"))
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
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL;")
except Exception:
    logger.exception("Failed open DB, switching to memory")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL;")

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

# Auto-detect default images in DATA_DIR if not set
if not DEFAULT_MALE_IMAGE:
    candidate = os.path.join(DATA_DIR, "default_male.jpg")
    if os.path.exists(candidate):
        DEFAULT_MALE_IMAGE = candidate
if not DEFAULT_FEMALE_IMAGE:
    candidate = os.path.join(DATA_DIR, "default_female.jpg")
    if os.path.exists(candidate):
        DEFAULT_FEMALE_IMAGE = candidate

# In-memory usage state
USER_ACTIVE_DOWNLOAD: set[int] = set()
download_lock = asyncio.Semaphore(1)
USER_POST_STATS: Dict[int, Dict[str, int or float]] = {}
USER_DAILY_STATS: Dict[int, Dict[str, int or float]] = {}

# ---------------------------
# Utilities
# ---------------------------
URL_RE = re.compile(r"https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+|telegram\.me/[^\s]+", flags=re.IGNORECASE)

def extract_first_url(msg: Message) -> Optional[str]:
    if not msg:
        return None
    entities = msg.entities or []
    for ent in entities:
        try:
            if ent.type == "text_link" and ent.url:
                return ent.url
            if ent.type == "url" and msg.text:
                return msg.text[ent.offset : ent.offset + ent.length]
        except Exception:
            continue
    entities = msg.caption_entities or []
    for ent in entities:
        try:
            if ent.type == "text_link" and ent.url:
                return ent.url
            if ent.type == "url" and msg.caption:
                return msg.caption[ent.offset : ent.offset + ent.length]
        except Exception:
            continue
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
    return user_id == OWNER_ID or user_id in ADMIN_IDS

# ---------------------------
# Post limits (with admin bypass)
# ---------------------------
def _reset_post_stats_if_needed(stats: Dict[str, int or float]) -> Dict[str, int or float]:
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

def decrement_post_count_on_failure(user_id: int, kind: str):
    stats = USER_POST_STATS.get(user_id)
    if not stats:
        return
    key = "photos_vids" if kind == "media" else "texts"
    if stats.get(key, 0) <= 1:
        stats[key] = 0
    else:
        stats[key] -= 1

# ---------------------------
# Download limits (with admin bypass)
# ---------------------------
def is_user_allowed(user_id: int, max_daily: int = MAX_DAILY) -> Tuple[bool, int]:
    if is_admin_id(user_id):
        return True, 0
    now = time.time()
    stats = USER_DAILY_STATS.get(user_id)
    if not stats:
        return True, 0
    first_ts = stats["first_ts"]
    count = stats["count"]
    elapsed = now - first_ts
    if elapsed >= DAILY_SECONDS:
        return True, 0
    if count < max_daily:
        return True, 0
    remaining = int(DAILY_SECONDS - elapsed)
    return False, remaining

def increment_user_count(user_id: int):
    if is_admin_id(user_id):
        return
    now = time.time()
    stats = USER_DAILY_STATS.get(user_id)
    if not stats:
        USER_DAILY_STATS[user_id] = {"count": 1, "first_ts": now}
    else:
        first_ts = stats["first_ts"]
        if now - first_ts >= DAILY_SECONDS:
            USER_DAILY_STATS[user_id] = {"count": 1, "first_ts": now}
        else:
            stats["count"] += 1

def decrement_user_count_on_failure(user_id: int):
    if is_admin_id(user_id):
        return
    stats = USER_DAILY_STATS.get(user_id)
    if not stats:
        return
    if stats["count"] <= 1:
        USER_DAILY_STATS.pop(user_id, None)
    else:
        stats["count"] -= 1

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
# HANDLERS
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

    default_photo = None
    if not is_media:
        if gender == "pria" and DEFAULT_MALE_IMAGE:
            default_photo = DEFAULT_MALE_IMAGE
        elif gender == "wanita" and DEFAULT_FEMALE_IMAGE:
            default_photo = DEFAULT_FEMALE_IMAGE

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
            if default_photo:
                if default_photo.startswith(("http://", "https://")):
                    await context.bot.send_photo(chat_id=CHANNEL_ID, photo=default_photo, caption=caption_for_media)
                elif os.path.exists(default_photo):
                    with open(default_photo, "rb") as fh:
                        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=fh, caption=caption_for_media)
                else:
                    await context.bot.send_message(chat_id=CHANNEL_ID, text=caption_for_text, disable_web_page_preview=True)
                increment_post_count(user_id, "text")
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

# ---------------------------
# DOWNLOAD FLOW: implementasi lengkap
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
        allowed, remaining = is_user_allowed(user_id)
        if not allowed:
            await msg.reply_text(
                f"😅 Kuota download hari ini sudah habis\n⏳ Reset dalam {human_time(remaining)}\n📅 Limit: {MAX_DAILY} download / hari"
            )
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

            increment_user_count(user_id)
            try:
                with open(tmpf_name, "rb") as fh:
                    try:
                        await context.bot.send_photo(chat_id=user_id, photo=fh)
                    except Exception:
                        fh.seek(0)
                        await context.bot.send_document(chat_id=user_id, document=fh)
                await msg.reply_text("✅ Foto berhasil dikirim.")
            except Exception:
                decrement_user_count_on_failure(user_id)
                logger.exception("Failed sending photo to user")
                await msg.reply_text("❌ Gagal mengirim foto.")
        except Exception:
            decrement_user_count_on_failure(user_id)
            logger.exception("Gagal mengunduh foto")
            await msg.reply_text("❌ Gagal mengunduh foto.")
        finally:
            try:
                if tmpf_name and os.path.exists(tmpf_name):
                    os.unlink(tmpf_name)
            except Exception:
                pass
        return

    # video/audio: offer quality options
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
    allowed, remaining = is_user_allowed(user_id)
    if not allowed:
        await query.edit_message_text(
            f"😅 Kuota download hari ini sudah habis\n⏳ Reset dalam {human_time(remaining)}\n📅 Limit: {MAX_DAILY} download / hari"
        )
        return

    await query.edit_message_text("⏳ Mengunduh, mohon tunggu...")
    tmpdir = None
    try:
        async with download_lock:
            USER_ACTIVE_DOWNLOAD.add(user_id)
            increment_user_count(user_id)
            tmpdir = tempfile.mkdtemp(prefix="yt-dl-")
            out_template = str(Path(tmpdir) / "output.%(ext)s")
            ffmpeg_available = shutil.which("ffmpeg") is not None

            if data == "q_mp3":
                if not ffmpeg_available:
                    decrement_user_count_on_failure(user_id)
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
                    ydl_opts = {"format": "best", "outtmpl": out_template, "quiet": True, "no_warnings": True, "noplaylist": True}

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
                decrement_user_count_on_failure(user_id)
                await query.edit_message_text("❌ File lebih besar dari 50MB sehingga tidak dapat dikirim melalui Bot Telegram.")
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
            except Exception:
                decrement_user_count_on_failure(user_id)
                logger.exception("Failed sending downloaded file")
                await query.edit_message_text("❌ Gagal mengirim file ke kamu.")
                return

            await query.edit_message_text("✅ Download selesai. File telah dikirim ke chat pribadi.")
    except Exception:
        decrement_user_count_on_failure(user_id)
        logger.exception("Error during download")
        try:
            await query.edit_message_text("❌ Gagal mengunduh.")
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
# ANTI-LINK (improved: check admin before deletion)
# ---------------------------
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
    # If sender is admin/creator/owner, ignore
    if member and member.status in ("administrator", "creator"):
        return
    # Not admin -> delete and ban temporarily
    try:
        await msg.delete()
    except Exception:
        pass
    until_date = int(time.time()) + 3600
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id, until_date=until_date)
        await context.bot.send_message(chat_id=chat.id, text=(f"🚫 <b>{escape_html(user.first_name or '')}</b> diblokir 1 jam\nAlasan: Mengirim link"), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Ban gagal")

# ---------------------------
# Tagging / moderation / help (unchanged, registered below)
# ---------------------------
async def tag_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implementation (same as previous)...
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
    with db:
        cur = db.cursor()
        cur.execute("SELECT user_id FROM welcomed_users WHERE chat_id=?", (chat.id,))
        rows = cur.fetchall()
    user_ids = [r["user_id"] for r in rows if r and isinstance(r["user_id"], int)]
    if not user_ids:
        await msg.reply_text("Tidak ada user yang tersimpan untuk ditandai.")
        return
    seen = set()
    user_ids = [uid for uid in user_ids if not (uid in seen or seen.add(uid))]
    MAX_TOTAL = 1000
    if len(user_ids) > MAX_TOTAL:
        await msg.reply_text(f"⚠️ Terdapat {len(user_ids)} user, terlalu banyak untuk ditag sekaligus.")
        return
    batch_size = 20
    sent_batches = 0
    try:
        for i in range(0, len(user_ids), batch_size):
            batch = user_ids[i : i + batch_size]
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
    await msg.reply_text(f"✅ Selesai mengirim tag kepada {len(user_ids)} user dalam {sent_batches} batch.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    all_features = (
        "📚 Fitur Bot (singkat):\n\n"
        "- Menfess via private: kirim teks/foto/video dengan tag #pria atau #wanita\n"
        "- Download file dari link (YouTube/TikTok/IG/...) pilih 360p/720p/MP3\n"
        "- Download foto dari direct image URL\n"
        "- Limit download & posting per hari\n"
        "- Admin commands: /tag /tagall /ban /kick /unban\n"
    )
    await msg.reply_text(all_features)

# ---------------------------
# MAIN: register handlers & start
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

    # Private menfess handler: exclude messages that contain url/text_link and commands
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.Entity("url") & ~filters.Entity("text_link") & ~filters.COMMAND, handle_message))
    # Welcome
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    # Anti-link in groups (improved)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.Entity("url") | filters.Entity("text_link")), anti_link))
    # Moderation
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("kick", kick_user))
    # Tagging
    app.add_handler(CommandHandler("tag", tag_member))
    app.add_handler(CommandHandler("tagall", tag_all))
    # Download handlers
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.Entity("url") | filters.Entity("text_link")), download_video))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))
    # Help
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
