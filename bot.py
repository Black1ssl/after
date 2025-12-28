import os
import sqlite3
import time
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 7186582328  # ID pemilik grup
TAGS = ["#pria", "#wanita"]
CHANNEL_ID = -1003595038397  # ganti dengan ID channel kamu
# atau pakai username channel:
# CHANNEL_ID = "@namachannel"

# 🔧 Tambahan Konfigurasi (WAJIB)
LOG_CHANNEL_ID = -1003439614621  # channel log/admin (private)

# database
db = sqlite3.connect("users.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL;")
db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    gender TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS welcomed_users (
    user_id INTEGER,
    chat_id INTEGER,
    PRIMARY KEY (user_id, chat_id)
)
""")
db.commit()

# 🧠 Fungsi LOG ADMIN (BARU)
async def send_to_log_channel(context: ContextTypes.DEFAULT_TYPE, msg: Update.message.__class__, gender: str):
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
                parse_mode=ParseMode.HTML
            )
        elif getattr(msg, "video", None):
            await context.bot.send_video(
                chat_id=LOG_CHANNEL_ID,
                video=msg.video.file_id,
                caption=log_caption,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=log_caption,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        # jangan crash bot kalau log gagal
        print("Gagal mengirim log:", e)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.from_user.is_bot:
        return

    text = (msg.text or msg.caption or "").lower()

    # deteksi gender
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
    cur = db.cursor()

    cur.execute("SELECT gender FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()

    if row and row[0] != gender:
        await msg.reply_text(
            f"❌ Post ditolak.\nGender akun kamu sudah tercatat sebagai #{row[0]}."
        )
        return

    if not row:
        cur.execute(
            "INSERT INTO users (user_id, username, gender) VALUES (?,?,?)",
            (user_id, username, gender)
        )
        db.commit()

    caption = msg.text or msg.caption or ""

    # ======================
    # KIRIM KE CHANNEL PUBLIK
    # ======================
    try:
        if getattr(msg, "photo", None):
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=msg.photo[-1].file_id,
                caption=caption
            )
        elif getattr(msg, "video", None):
            await context.bot.send_video(
                chat_id=CHANNEL_ID,
                video=msg.video.file_id,
                caption=caption
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption
            )
    except Exception as e:
        # jika pengiriman ke publik gagal, beri tahu pengirim
        await msg.reply_text(f"❌ Gagal mengirim ke channel publik: {e}")
        return

    # ======================
    # 🔐 LOG ADMIN
    # ======================
    await send_to_log_channel(context, msg, gender)

    await msg.reply_text("✅ Post berhasil dikirim.")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat.id

    # hapus pesan "user joined"
    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=msg.message_id
        )
    except:
        pass

    cur = db.cursor()

    for user in msg.new_chat_members:
        if user.is_bot:
            continue

        user_id = user.id

        # cek apakah sudah pernah di-welcome
        cur.execute(
            "SELECT 1 FROM welcomed_users WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )

        if cur.fetchone():
            continue  # sudah pernah, jangan nyepam

        # simpan sebagai sudah di-welcome
        cur.execute(
            "INSERT INTO welcomed_users (user_id, chat_id) VALUES (?, ?)",
            (user_id, chat_id)
        )
        db.commit()

        # kirim welcome
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
            parse_mode=ParseMode.HTML
        )

async def anti_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    chat = msg.chat

    if user.is_bot:
        return

    # 🔐 cek status user (admin atau bukan)
    member = await context.bot.get_chat_member(chat.id, user.id)

    if member.status in ("administrator", "creator"):
        return  # admin kebal

    # 🗑️ hapus pesan link
    try:
        await msg.delete()
    except BadRequest:
        pass

    # ⏱️ ban 1 jam
    until_date = int(time.time()) + 3600

    try:
        await context.bot.ban_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            until_date=until_date
        )

        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"🚫 <b>{user.first_name}</b> diblokir 1 jam\n"
                "Alasan: Mengirim link"
            ),
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        print("Ban gagal:", e)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    chat = msg.chat

    # Cek jika di grup
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Perintah ini hanya untuk grup.")
        return

    # Cek status admin
    member = await context.bot.get_chat_member(chat.id, user.id)
    if user.id != OWNER_ID and member.status not in ("administrator", "creator"):
        await msg.reply_text("❌ Hanya pemilik grup atau admin yang bisa menggunakan perintah ini.")
        return

    # Parse argumen
    args = context.args
    if not args:
        await msg.reply_text("❌ Gunakan: /unban <user_id>")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await msg.reply_text("❌ User ID harus berupa angka.")
        return

    # Unban user
    try:
        await context.bot.unban_chat_member(
            chat_id=chat.id,
            user_id=target_user_id
        )
        await msg.reply_text(f"✅ User {target_user_id} telah di-unban.")
    except Exception as e:
        await msg.reply_text(f"❌ Gagal unban: {str(e)}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.Entity("url"),
            anti_link
        )
    )

    app.add_handler(CommandHandler("unban", unban_user))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
