```python name=bot.py
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

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

# optional OpenAI
try:
    import openai
except Exception:
    openai = None

# ======================
# CONFIG
# ======================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional
if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

OWNER_ID = int(os.getenv("OWNER_ID", "7186582328"))
TAGS = ["#pria", "#wanita"]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003595038397"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003439614621"))

# ======================
# LIMITS / QUEUE / STATE
# ======================
MAX_DAILY = 2  # max downloads per user per day
DAILY_SECONDS = 24 * 60 * 60

USER_DAILY_STATS: dict[int, dict] = {}  # user_id -> {"count": int, "first_ts": float}
USER_ACTIVE_DOWNLOAD: set[int] = set()
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

# tables
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
db.execute(
    """
CREATE TABLE IF NOT EXISTS group_settings (
    chat_id INTEGER PRIMARY KEY,
    rude_mode INTEGER DEFAULT 0
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


URL_RE = re.compile(
    r"https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+|telegram\.me/[^\s]+", flags=re.IGNORECASE
)


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
    url = url.lower().split("?")[0]
    return any(url.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))


def is_rude_enabled(chat_id: int) -> bool:
    cur = db.cursor()
    cur.execute("SELECT rude_mode FROM group_settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    return bool(row and row[0] == 1)


def set_rude_mode(chat_id: int, enabled: bool):
    with db:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO group_settings (chat_id, rude_mode) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET rude_mode=excluded.rude_mode",
            (chat_id, 1 if enabled else 0),
        )


# ======================
# Rude/ChatGPT helpers
# ======================
# simple in-memory rate limit per-user for rude replies
LAST_REPLY: dict[int, float] = {}
CHAT_COOLDOWN = 5  # seconds between replies per user

# blacklist patterns (extend as needed)
PROTECTED_SLURS = []
PROTECTED_PATTERNS = [re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE) for w in PROTECTED_SLURS]


def sanitize_output(text: str) -> str:
    out = text
    for pat in PROTECTED_PATTERNS:
        out = pat.sub("***", out)
    out = re.sub(r"(tg://user\?id=\d+)", "", out)
    return out.strip()


async def generate_rude_reply(prompt: str) -> str:
    if not openai or not OPENAI_API_KEY:
        canned = [
            "Lah, repot amat sih? Bentar, urusanku juga.",
            "Mau gimana coba? Kalau gampang kamu pasti udah bisa.",
            "Santai, ngga usah heboh. Aku urus belakangan.",
        ]
        return canned[int(time.time()) % len(canned)]

    system_prompt = (
        "Kamu asisten berbahasa Indonesia dengan nada sarkastik/kasar ringan. Balas singkat (maks 2 kalimat). "
        "JANGAN mengandung ancaman, ujaran kebencian, slurs terhadap protected groups, konten seksual eksplisit, atau ajakan kekerasan. "
        "Jika permintaan berbahaya, tolak dengan nada pedas tapi sopan."
    )

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.8,
            top_p=0.9,
        )
        text = resp.choices[0].message.content.strip()
        text = sanitize_output(text)
        if not text:
            return "Wah, aku nggak bisa jawab itu, bro."
        return text
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return "Maaf, server pelit. Coba lagi nanti."


# ======================
# CORE HANDLERS
# ======================


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
            await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=msg.photo[-1].file_id, caption=log_caption, parse_mode=ParseMode.HTML)
        elif getattr(msg, "video", None):
            await context.bot.send_video(chat_id=LOG_CHANNEL_ID, video=msg.video.file_id, caption=log_caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_caption, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Gagal mengirim log")


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
        await context.bot.send_message(chat_id=chat.id, text=(f"🚫 <b>{user.first_name}</b> diblokir 1 jam\nAlasan: Mengirim link"), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Ban gagal")


# ======================
# Moderation: ban/kick/unban
# ======================


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

    # image direct URL
    if is_image_url(url):
        user_id = msg.from_user.id
        allowed, remaining = is_user_allowed(user_id)
        if not allowed:
            await msg.reply_text("😅 Kuota download hari ini sudah habis\n\n" f"⏳ Reset dalam {human_time(remaining)}\n" f"📅 Limit: {MAX_DAILY} download / hari")
            return

        await msg.reply_text("⏳ Mengunduh foto...")
        tmpf = None
        try:
            import aiohttp

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

    # otherwise video/audio flow
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
        await query.edit_message_text("😅 Kuota download hari ini sudah habis\n\n" f"⏳ Reset dalam {human_time(remaining)}\n" f"📅 Limit: {MAX_DAILY} download / hari")
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
                    await query.edit_message_text("⚠️ Konversi ke MP3 memerlukan ffmpeg yang tidak tersedia di server. Pilih video atau gunakan Docker.")
                    decrement_user_count_on_failure(user_id)
                    return
                ydl_opts = {"format": "bestaudio/best", "outtmpl": out_template, "quiet": True, "no_warnings": True, "noplaylist": True, "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],}
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

            TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
            if size_bytes > TELEGRAM_MAX_BYTES:
                await query.edit_message_text("❌ File lebih besar dari 50MB sehingga tidak dapat dikirim melalui Bot Telegram.\nSilakan unduh langsung dari sumber (link) atau gunakan metode lain.")
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
# TAG COMMANDS
# ======================


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
            await msg.reply_text("Gunakan: /tag <user_id> <pesan>  atau reply ke pesan user dengan /tag <pesan>")
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

    with db:
        cur = db.cursor()
        cur.execute("SELECT user_id FROM welcomed_users WHERE chat_id=?", (chat.id,))
        rows = cur.fetchall()
    user_ids = [r[0] for r in rows if r and isinstance(r[0], int)]
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


# ======================
# HELP COMMAND
# ======================


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user

    all_features = (
        "📚 Fitur Bot (lengkap):\n\n"
        "Member:\n"
        "- Menfess via private: kirim teks/foto/video dengan tag #pria atau #wanita\n"
        "- Download video/audio dari hampir semua link (YouTube/TikTok/IG/...) pilih 360p/720p/MP3\n"
        "- Download foto dari direct image URL\n"
        "- Batas file dikirim oleh bot: 50 MB\n"
        f"- Limit download: {MAX_DAILY}x per hari per user\n\n"
        "Admin (harus admin/owner di grup):\n"
        "- /tag <user_id> <pesan> atau reply + /tag : menandai 1 member\n"
        "- /tagall [pesan] : menandai semua member yang tersimpan (batched)\n"
        "- /ban <user_id> [hours] : ban user (optional durasi dalam jam)\n"
        "- /kick <user_id> atau reply + /kick : kick user dari grup\n"
        "- /unban <user_id> : unban user\n"
        "- /rude_on : minta konfirmasi untuk aktifkan mode rudeness (admin-only)\n"
        "- /rude_off : matikan mode rude\n"
        "- /rude_status : cek status mode rude\n\n"
        "Lainnya:\n"
        "- Auto welcome untuk member baru (welcome berisi link bot & channel)\n"
        "- Anti-link di grup (hapus + ban sementara)\n"
        "- Gunakan /help untuk melihat bantuan ini\n"
    )
    if chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
        except Exception:
            member = None
        if member and member.status in ("administrator", "creator") or user.id == OWNER_ID:
            await msg.reply_text("Halo Admin!\n\n" + all_features)
        else:
            await msg.reply_text("Halo Member!\n\n" + all_features)
    else:
        await msg.reply_text(all_features)


# ======================
# Rude mode: opt-in confirmation + handlers
# ======================
PENDING_RUDE_CONFIRMATIONS: Dict[int, Dict[str, float]] = {}
RUD_MODE_CONFIRM_TIMEOUT = 30  # seconds for confirmation


async def rude_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah ini hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang bisa mengaktifkan mode rude.")
        return
    if is_rude_enabled(chat.id):
        await msg.reply_text("Mode 'rude' sudah aktif di grup ini.")
        return
    expires = time.time() + RUD_MODE_CONFIRM_TIMEOUT
    PENDING_RUDE_CONFIRMATIONS[chat.id] = {"initiator": user.id, "expires_at": expires}
    await msg.reply_text(
        "⚠️ Kamu akan mengaktifkan mode 'rude' (bot akan membalas saat ditag dengan nada sarkastik/kasar ringan).\n"
        "Untuk konfirmasi, ketik `I AGREE` (case-insensitive) di obrolan ini dalam 30 detik.\n"
        "Jika kamu berubah pikiran, abaikan pesan ini.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def rude_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah ini hanya untuk grup.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        member = None
    if user.id != OWNER_ID and (not member or member.status not in ("administrator", "creator")):
        await msg.reply_text("❌ Hanya admin atau pemilik grup yang bisa menonaktifkan mode rude.")
        return
    if not is_rude_enabled(chat.id):
        await msg.reply_text("Mode 'rude' saat ini sudah NON-AKTIF.")
        return
    set_rude_mode(chat.id, False)
    PENDING_RUDE_CONFIRMATIONS.pop(chat.id, None)
    await msg.reply_text("✅ Mode 'rude' dimatikan untuk grup ini.")


async def rude_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    enabled = is_rude_enabled(chat.id)
    status_text = "ON" if enabled else "OFF"
    await msg.reply_text(f"🟢 Mode 'rude' saat ini: {status_text}")


async def handle_rude_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat = msg.chat
    user = msg.from_user
    if chat.type not in ("group", "supergroup"):
        return
    pend = PENDING_RUDE_CONFIRMATIONS.get(chat.id)
    if not pend:
        return
    if time.time() > pend["expires_at"]:
        PENDING_RUDE_CONFIRMATIONS.pop(chat.id, None)
        return
    # only initiator can confirm (change if you want admin-anyone)
    if user.id != pend["initiator"]:
        return
    if msg.text.strip().upper() == "I AGREE":
        set_rude_mode(chat.id, True)
        PENDING_RUDE_CONFIRMATIONS.pop(chat.id, None)
        await msg.reply_text("✅ Konfirmasi diterima. Mode 'rude' sekarang AKTIF untuk grup ini.")


async def mention_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    chat = msg.chat
    if chat.type not in ("group", "supergroup"):
        return
    if not is_rude_enabled(chat.id):
        return

    bot_user = await context.bot.get_me()
    bot_username = getattr(bot_user, "username", None)
    text = (msg.text or msg.caption or "") or ""
    mentioned = False

    # reply to bot
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_user.id:
        mentioned = True

    # check entities
    ents = msg.entities or []
    for ent in ents:
        if ent.type == "text_mention" and getattr(ent, "user", None):
            if ent.user.id == bot_user.id:
                mentioned = True
                break
        if ent.type == "mention" and bot_username:
            snippet = text[ent.offset : ent.offset + ent.length]
            if snippet.lstrip("@").lower() == bot_username.lower():
                mentioned = True
                break

    if bot_username and f"@{bot_username.lower()}" in text.lower():
        mentioned = True

    if not mentioned:
        return

    now = time.time()
    last = LAST_REPLY.get(msg.from_user.id, 0)
    if now - last < CHAT_COOLDOWN:
        return
    LAST_REPLY[msg.from_user.id] = now

    user_display = msg.from_user.first_name or "User"
    prompt = f"User said: {text}\n\nRespond in Indonesian with a short rude/sarcastic reply addressed to the user ({user_display}). Keep it short and avoid threats, slurs toward protected groups, sexual explicit content, or doxxing."

    reply_text = await generate_rude_reply(prompt)
    for pat in PROTECTED_PATTERNS:
        if pat.search(reply_text):
            reply_text = pat.sub("***", reply_text)

    try:
        await msg.reply_text(reply_text)
    except Exception:
        await context.bot.send_message(chat_id=chat.id, text=reply_text)


# ======================
# MAIN
# ======================


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # PRIVATE menfess handler: exclude messages that contain url/text_link
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.Entity("url") & ~filters.Entity("text_link") & ~filters.COMMAND, handle_message)
    )

    # Welcome new members
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # Anti-link in groups
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.Entity("url") | filters.Entity("text_link")), anti_link))

    # Moderation
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("kick", kick_user))

    # Download handlers
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.Entity("url") | filters.Entity("text_link")), download_video))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))

    # Tag commands
    app.add_handler(CommandHandler("tag", tag_member))
    app.add_handler(CommandHandler("tagall", tag_all))

    # Rude mode commands & confirmation
    app.add_handler(CommandHandler("rude_on", rude_on))
    app.add_handler(CommandHandler("rude_off", rude_off))
    app.add_handler(CommandHandler("rude_status", rude_status))
    # confirmation handler must be before mention handler so "I AGREE" is captured
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, handle_rude_confirmation))

    # Mention handler (respond when bot is mentioned and rude_mode ON)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL & ~filters.COMMAND, mention_handler))

    # HELP
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
```
