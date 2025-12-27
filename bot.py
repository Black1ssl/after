import os
import sqlite3
import time
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
TAGS = ["#pria", "#wanita"]
CHANNEL_ID = -1003595038397  # ganti dengan ID channel kamu
# atau pakai username channel:
# CHANNEL_ID = "@namachannel"

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.from_user.is_bot:
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

    # kirim ke channel
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=msg.text or msg.caption
    )

    # balasan aman (tanpa ID)
    await msg.reply_text(
        f"✅ Post berhasil dikirim."
    )

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

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
