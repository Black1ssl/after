import os
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

REQUIRED_TAGS = ["#pria", "#wanita"]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or msg.caption or ""

    # ubah ke lowercase biar fleksibel
    text_lower = text.lower()

    # cek tag
    if not any(tag in text_lower for tag in REQUIRED_TAGS):
        await msg.reply_text(
            "❌ Post ditolak.\n\n"
            "Wajib menggunakan salah satu tag:\n"
            "#pria atau #wanita"
        )
        return

    # lolos validasi
    await msg.reply_text("✅ Post diterima, sedang diproses…")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
