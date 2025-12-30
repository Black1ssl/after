import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

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

# yt_dlp Python API
from yt_dlp import YoutubeDL

# ======================
# CONFIG
# ======================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "7186582328"))
TAGS = ["#pria", "#wanita"]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003595038397"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003439614621"))

# ======================
# LIMITS / QUEUE / STATE
# ======================
MAX_DAILY = 1  # max downloads per user per day
DAILY_SECONDS = 24 * 60 * 60

# user -> {"count": int, "first_ts": float}
USER_DAILY_STATS: dict[int, dict] = {}

# prevent same user from running concurrent downloads
USER_ACTIVE_DOWNLOAD: set[int] = set()

# global semaphore to limit concurrent downloads overall
download_lock = asyncio.Semaphore(1)

# ======================
# DATABASE
# ======================
db = sqlite3.connect("users.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL;")
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
db.commit()

# ======================
# HELPERS
# ======================


def is_user_allowed(user_id: int, max_daily: int = MAX_DAILY) -> Tuple[bool, int]:
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
    stats = USER_DAILY_STATS.get(user_id)
    if not stats:
        return
    if stats["count"] <= 1:
        USER_DAILY_STATS.pop(user_id, None)
    else:
        stats["count"] -= 1


def human_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h} jam {m} menit"
    if m:
        return f"{m} menit"
    return "beberapa detik"


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
    return None


async def send_to_log_channel(context: ContextTypes.DEFAULT_TYPE, msg: Message, gender: str):
    user = msg.from_user
    username = f"@{user.username}" if user.username else "(no username)"
    name = user.first_name or "-"
    log_caption = (
        f"👤 <b>Nama:</b> {name}\n"
        f"🔗 <b>Username:</b> {username}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"⚧ <b>Gender:</b> #{gender}\n\n"
        f"{msg.caption or msg.text or ''}"
    )
    try:
        if getattr(msg, "photo", None):
            await context.bot.send_photo(
                chat_id=LOG_CHANNEL_ID,
                photo=msg.photo[-1].file_id,
                caption=log_caption,
                parse_mode=ParseMode.HTML,
            )
        elif getattr(msg, "video", None):
            await context.bot.send_video(
                chat_id=LOG_CHANNEL_ID,
                video=msg.video.file_id,
                caption=log_caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_caption, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Gagal mengirim log")


# ======================
# HANDLERS
# ======================


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return

    text = (msg.text or msg.caption or "").lower()

    gender = None
    for tag in TAGS:
        if tag in text:
            gender = tag.replace("#", "")
            break

    if not gender:
        await msg.reply_text("❌ Post ditolak.\nWajib pakai #pria atau #wanita")
        return

    user_id = msg.from_user.id
    username = msg.from_user.username

    with db:
        cur = db.cursor()
        cur.execute("SELECT gender FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row and row[0] != gender:
            await msg.reply_text(f"❌ Post ditolak.\nGender akun kamu sudah tercatat sebagai #{row[0]}.")
            return
        if not row:
            cur.execute("INSERT INTO users (user_id, username, gender) VALUES (?,?,?)", (user_id, username, gender))

    caption = msg.text or msg.caption or ""
    try:
        if getattr(msg, "photo", None):
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=msg.photo[-1].file_id, caption=caption)
        elif getattr(msg, "video", None):
            await context.bot.send_video(chat_id=CHANNEL_ID, video=msg.video.file_id, caption=caption)
        else:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=caption)
    except Exception as e:
        await msg.reply_text(f"❌ Gagal mengirim ke channel publik: {e}")
        return

    await send_to_log_channel(context, msg, gender)
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
                f"👋 Selamat datang <b>{user.first_name}</b>!\n\n"
                "📌 <b>Peraturan Grup:</b>\n"
                "• No rasis 🚫\n"
                "• Jangan spam 🚫\n"
                "• Post menfess via bot\n\n"
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
        await context.bot.send_message(
            chat_id=chat.id,
            text=(f"🚫 <b>{user.first_name}</b> diblokir 1 jam\nAlasan: Mengirim link"),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Ban gagal")


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


# ======================
# DOWNLOAD FLOW (yt_dlp)
# ======================


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    url = extract_first_url(msg)
    if not url:
        await msg.reply_text("❌ Tidak menemukan URL di pesan.")
        return

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
            "😅 Kuota download hari ini sudah habis\n\n"
            f"⏳ Reset dalam {human_time(remaining)}\n"
            f"📅 Limit: {MAX_DAILY} download / hari"
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

            if data == "q_mp3":
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": out_template,
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "postprocessors": [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                    ],
                }
            else:
                max_h = 360 if data == "q_360" else 720
                fmt = f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]"
                ydl_opts = {
                    "format": fmt,
                    "outtmpl": out_template,
                    "merge_output_format": "mp4",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                }

            def run_ydl():
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            # run blocking yt_dlp in thread
            await asyncio.to_thread(run_ydl)

            files = list(Path(tmpdir).iterdir())
            if not files:
                raise RuntimeError("Download gagal — tidak ada file output dari yt-dlp.")
            files_sorted = sorted(files, key=lambda p: p.stat().st_size, reverse=True)
            output_file = files_sorted[0]
            size_bytes = output_file.stat().st_size
            logger.info("Downloaded file: %s (%d bytes)", output_file, size_bytes)

            TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
            if size_bytes > TELEGRAM_MAX_BYTES:
                await query.edit_message_text(
                    "❌ File lebih besar dari 50MB sehingga tidak dapat dikirim melalui Bot Telegram.\n"
                    "Silakan unduh langsung dari sumber (link) atau gunakan metode lain."
                )
                return

            suffix = output_file.suffix.lower()
            try:
                if suffix in (".mp4", ".mkv", ".webm", ".mov"):
                    await context.bot.send_video(chat_id=user_id, video=str(output_file))
                elif suffix in (".mp3", ".m4a", ".aac", ".opus"):
                    await context.bot.send_audio(chat_id=user_id, audio=str(output_file))
                else:
                    await context.bot.send_document(chat_id=user_id, document=str(output_file))
            except Exception:
                try:
                    await context.bot.send_document(chat_id=user_id, document=str(output_file))
                except Exception as e:
                    raise RuntimeError(f"Gagal mengirim file ke pengguna: {e}")

            await query.edit_message_text("✅ Download selesai. File telah dikirim ke chat pribadi.")
    except Exception as exc:
        decrement_user_count_on_failure(user_id)
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


# ======================
# MAIN SETUP
# ======================


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # PRIVATE menfess handler: exclude messages that contain url/text_link
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & ~filters.Entity("url")
            & ~filters.Entity("text_link")
            & ~filters.COMMAND,
            handle_message,
        )
    )

    # Welcome new members
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # Anti-link in groups: catch url and text_link
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.Entity("url") | filters.Entity("text_link")), anti_link))

    # Unban command
    app.add_handler(CommandHandler("unban", unban_user))

    # Download handlers: URL in private triggers quality keyboard
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.Entity("url") | filters.Entity("text_link")), download_video))

    # CallbackQuery handler for quality selection
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
