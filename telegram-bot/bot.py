from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

BOT_TOKEN = "8571822830:AAH0PRPvBEDEzOa3AjZbBFSMGnsM9UDs3uQ"

TARGET_CHANNEL_ID = -1003595038397 # ID CHANNEL

REQUIRED_TAG = "#pria" "#wanita"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

   

    text = msg.text or msg.caption or ""

    # Validasi tag
    if REQUIRED_TAG not in text:
        await msg.reply_text(
            f"❌ ditolak.\nWajib menyertakan tag {REQUIRED_TAG}"
        )
        return

    # Kirim ke channel
    if msg.photo:
        await context.bot.send_photo(
            chat_id=TARGET_CHANNEL_ID,
            photo=msg.photo[-1].file_id,
            caption=msg.caption
        )
    else:
        await context.bot.send_message(
            chat_id=TARGET_CHANNEL_ID,
            text=msg.text
        )

    await msg.reply_text("✅  berhasil dikirim.")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.ALL, handle_message))
app.run_polling()

