import os, glob, tempfile
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    url = None
    for word in text.split():
        if any(domain in word for domain in ["instagram.com", "tiktok.com", "youtube.com", "youtu.be", "twitter.com", "x.com"]):
            url = word
            break

    if not url:
        return

    await update.message.reply_text("⏳ Lade herunter...")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            'outtmpl': f'{tmpdir}/%(title)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'writeinfojson': False,
            'writethumbnail': False,
            # Telegram-Limit: 50MB — Qualität begrenzen
            'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best',
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                import subprocess
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {e}")
            return

        media_files = sorted(glob.glob(os.path.join(tmpdir, "*")))
        
        # Nur größte MP4 behalten (gemergede Version)
        mp4_files = [f for f in media_files if f.endswith((".mp4", ".mov", ".webm", ".mkv"))]
        if mp4_files:
            mp4_files = [max(mp4_files, key=os.path.getsize)]
        
        img_files = [f for f in media_files if f.endswith((".jpg", ".jpeg", ".png"))]
        
        final_files = img_files + mp4_files
        
        sent = 0
        first = True
        for f in final_files:
            if f.endswith((".jpg", ".jpeg", ".png")):
                await update.message.reply_photo(
                    photo=open(f, "rb"),
                    caption=caption if first else None
                )
                first = False
                sent += 1
            elif f.endswith((".mp4", ".mov", ".webm", ".mkv")):
                size_mb = os.path.getsize(f) / (1024 * 1024)
                if size_mb > 50:
                    await update.message.reply_text(f"⚠️ Datei zu groß ({size_mb:.1f} MB), Telegram-Limit ist 50 MB.")
                    continue
                await update.message.reply_video(
                    video=open(f, "rb"),
                    caption=caption if first else None,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                )
                first = False
                sent += 1

        if sent == 0:
            await update.message.reply_text("⚠️ Keine Mediendateien gefunden.")

# Erhöhter Timeout für den Bot selbst
request = HTTPXRequest(read_timeout=120, write_timeout=120, connect_timeout=60)
app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
app.run_polling()