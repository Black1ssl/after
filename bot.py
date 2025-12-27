import os
import sqlite3
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
TAGS = ["#pria", "#wanita"]

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
    # cari tag
    gender = None
    for tag in TAGS:
        if tag in text:
            gender = tag.replace("#", "")
            break
    if not gender:
        await msg.reply_text(
            "❌ Post ditolak.\nWajib pakai #pria atau #wanita"
        )
        return
    user_id = msg.from_user.id
    username = msg.from_user.username
    cur = db.cursor()
    cur.execute("SELECT gender FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        if row[0] != gender:
            await msg.reply_text(
                f"❌ Post ditolak.\n"
                f"Gender akun kamu sudah tercatat sebagai #{row[0]}.\n"
                f"Tidak boleh berubah."
            )
            return
    else:
        cur.execute(
            "INSERT INTO users (user_id, username, gender) VALUES (?,?,?)",
            (user_id, username, gender)
        )
        db.commit()
    await msg.reply_text(
        f"✅ Post diterima sebagai #{gender}.\n"
        f"ID: {user_id}"
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

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
