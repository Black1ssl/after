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

    caption = msg.caption or msg.text or ""

    # ======================
    # KIRIM KE CHANNEL
    # ======================

    # 📷 FOTO
    if msg.photo:
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=msg.photo[-1].file_id,
            caption=caption
        )

    # 🎥 VIDEO
    elif msg.video:
        await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=msg.video.file_id,
            caption=caption
        )

    # 📝 TEKS SAJA → KIRIM GAMBAR DEFAULT
    else:
        image_path = "pria.jpg" if gender == "pria" else "wanita.jpg"

        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=open(image_path, "rb"),
            caption=caption
        )

    # balasan aman
    await msg.reply_text("✅ Post berhasil dikirim.")
