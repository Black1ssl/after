import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
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
MAX_DAILY = 2  # max downloads per user per day (requested 2x/day)
DAILY_SECONDS = 24 * 60 * 60

# user -> {"count": int, "first_ts": float}
USER_DAILY_STATS: dict[int, dict] = {}

# prevent same user from running concurrent downloads
USER_ACTIVE_DOWNLOAD: set[int] = set()

# global semaphore to limit concurrent downloads overall
download_lock = asyncio.Semaphore(1)

# ======================
# DATABASE (safe path + fallback)
# ======================
DB_PATH = os.getenv("DB_PATH", "/app/data/users.db")
db_dir = os.path.dirname(DB_PATH)
try:
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
except Exception as e:
    logger.exception("Gagal membuat direktori database %s: %s", db_dir, e)
    DB_PATH = ":memory:"
    logger.warning("Menggunakan SQLite in-memory fallback (tidak persistent).")

try:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL;")
except sqlite3.OperationalError as e:
    logger.exception("Gagal membuka database %s: %s", DB_PATH, e)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL;")
    logger.warning("Fallback ke in-memory SQLite database (data tidak disimpan).")

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


def is_image_url(url: str) -> bool:
    url = url.lower().split("?")[0]
    return any(url.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))


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

        # Welcome message includes links to the bot and the channel as requested
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"👋 Selamat datang <b>{user.first_name}</b>!\n\n"
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
# DOWNLOAD FLOW (yt_dlp + image support)
# ======================


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    url = extract_first_url(msg)
    if not url:
        await msg.reply_text("❌ Tidak menemukan URL di pesan.")
        return

    # If it's an image URL, download immediately (no quality selection)
    if is_image_url(url):
        user_id = msg.from_user.id
        allowed, remaining = is_user_allowed(user_id)
        if not allowed:
            await msg.reply_text(
                "😅 Kuota download hari ini sudah habis\n\n"
                f"⏳ Reset dalam {human_time(remaining)}\n"
                f"📅 Limit: {MAX_DAILY} download / hari"
            )
            return

        await msg.reply_text("⏳ Mengunduh foto...")
        tmpf = None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content_length = resp.headers.get("Content-Length")
                    if content_length and int(content_length) > 50 * 1024 * 1024:
                        await msg.reply_text("❌ Foto lebih besar dari 50MB, tidak dapat dikirim.")
                        return
                    data = await resp.read()
                    if len(data) > 50 * 1024 * 1024:
                        await msg.reply_text("❌ Foto lebih besar dari 50MB, tidak dapat dikirim.")
                        return
                    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=Path(url).suffix or ".jpg")
                    tmpf.write(data)
                    tmpf.flush()
                    tmpf.close()

            # mark usage and send
            increment_user_count(user_id)
            try:
                await context.bot.send_photo(chat_id=user_id, photo=open(tmpf.name, "rb"))
            except Exception:
                await context.bot.send_document(chat_id=user_id, document=open(tmpf.name, "rb"))
            await msg.reply_text("✅ Foto berhasil dikirim.")
        except Exception as e:
            decrement_user_count_on_failure(user_id)
            logger.exception("Gagal mengunduh foto: %s", e)
            await msg.reply_text(f"❌ Gagal mengunduh foto: {e}")
        finally:
            try:
                if tmpf:
                    os.unlink(tmpf.name)
            except Exception:
                pass

        return

    # otherwise proceed to quality selection flow (for video/audio)
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

            # detect ffmpeg availability and adjust options accordingly
            ffmpeg_available = shutil.which("ffmpeg") is not None

            if data == "q_mp3":
                if not ffmpeg_available:
                    await query.edit_message_text(
                        "⚠️ Konversi ke MP3 memerlukan ffmpeg yang tidak tersedia di server.\n"
                        "Silakan pilih format video (360p/720p) atau jalankan bot di environment dengan ffmpeg (gunakan Docker)."
                    )
                    decrement_user_count_on_failure(user_id)
                    return
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
                if ffmpeg_available:
                    fmt = f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]"
                    ydl_opts = {
                        "format": fmt,
                        "outtmpl": out_template,
                        "merge_output_format": "mp4",
                        "quiet": True,
                        "no_warnings": True,
                        "noplaylist": True,
                    }
                else:
                    # fallback to single-file progressive format if no ffmpeg
                    fmt = "best"
                    ydl_opts = {
                        "format": fmt,
                        "outtmpl": out_template,
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
                decrement_user_count_on_failure(user_id)
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
# ADMIN TAG COMMAND (only admin and owner)
# ======================


async def tag_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /tag <user_id> <message> OR reply to a user with /tag <message>
    Only admins/owner can use this in groups.
    """
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user

    # This command intended for group contexts
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah /tag hanya untuk grup.")
        return

    # check admin
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None

    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang dapat menandai member.")
        return

    # determine target
    parts = context.args or []
    text_to_send = ""
    target_id = None
    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_id = target.id
        text_to_send = " ".join(parts) if parts else "(ditandai oleh admin)"
    else:
        if not parts:
            await msg.reply_text("Gunakan: /tag <user_id|@username> <pesan>  atau reply ke pesan user dengan /tag <pesan>")
            return
        # first arg could be @username or id
        first = parts[0]
        rest = parts[1:]
        text_to_send = " ".join(rest) if rest else "(ditandai oleh admin)"
        if first.startswith("@"):
            # try resolve username via get_chat_member? we can try get_chat_member with username not supported; try search via chat
            # fallback: instruct to reply or use numeric id
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


# ======================
# HELP COMMAND (show features & rights)
# ======================


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat = msg.chat
    user = msg.from_user

    # default member help (private or group)
    member_help = (
        "📚 Fitur untuk Member:\n"
        "- Mengirim menfess via private ke bot: sertakan tag #pria atau #wanita\n"
        "- Download video/audio dari link (YouTube/TikTok/IG) hingga 50MB\n"
        "- Download foto dari direct image URL (<=50MB)\n"
        "- Limit download: 2x per hari\n"
        "- Gunakan perintah /help untuk melihat bantuan ini\n"
    )

    admin_help = (
        "🔑 Tambahan hak untuk Admin:\n"
        "- Bisa menggunakan /unban <user_id>\n"
        "- Bisa menandai member dengan /tag (reply ke pesan user atau /tag <user_id> <pesan>)\n"
        "- Admin kebal dari anti-link\n"
    )

    if chat.type in ("group", "supergroup"):
        # check admin status
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
        except Exception:
            member = None
        if member and member.status in ("administrator", "creator") or user.id == OWNER_ID:
            await msg.reply_text("Halo Admin!\n\n" + member_help + "\n" + admin_help)
        else:
            await msg.reply_text("Halo Member!\n\n" + member_help)
    else:
        # private chat -> show both (member view) and note about admin features
        await msg.reply_text("Halo!\n\n" + member_help + "\n" + "Catatan: Fitur admin hanya untuk admin di grup.\n" + admin_help)


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
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.Entity("url") | filters.Entity("text_link")),
            anti_link,
        )
    )

    # Unban command
    app.add_handler(CommandHandler("unban", unban_user))

    # Download handlers: URL in private triggers quality keyboard or direct image download
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.Entity("url") | filters.Entity("text_link")), download_video))

    # CallbackQuery handler for quality selection
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))

    # Tag command (admin only in group)
    app.add_handler(CommandHandler("tag", tag_member))

    # Help command
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
