# Telegram Menfess Bot (clean, no rude mode)

Ini versi bot Telegram yang sudah dibersihkan dari fitur "rude mode". Fitur utama yang tersisa:
- Menfess via private message (wajib #pria atau #wanita)
- Download video/audio (yt-dlp) + konversi MP3 (memerlukan ffmpeg di server)
- Download gambar dari direct image URL
- Auto welcome member baru (disimpan di DB)
- Anti-link di grup (menghapus dan ban sementara)
- Admin commands: /ban, /kick, /unban, /tag, /tagall
- Logging ke channel (LOG_CHANNEL_ID)

Cara pakai singkat:
1. Set environment variables:
   - BOT_TOKEN (required)
   - OWNER_ID (opsional, default di file)
   - CHANNEL_ID (tempat menfess dikirim)
   - LOG_CHANNEL_ID (tempat log dikirim)
   - DB_PATH (opsional, default `/app/data/users.db`)

2. Install dependencies:
   pip install -r requirements.txt

3. Jalankan:
   python bot.py

Catatan:
- Untuk konversi MP3, server mesti memiliki `ffmpeg` di PATH.
- Bot membatasi pengiriman file via Telegram maksimal 50MB (batas kode).
