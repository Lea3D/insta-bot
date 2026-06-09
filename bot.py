import os, glob, tempfile
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # URL aus Text extrahieren
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
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                caption = info.get("description", "")[:1024] if info else ""
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {e}")
            return

        media_files = sorted(glob.glob(os.path.join(tmpdir, "*")))
        sent = 0
        first = True
        for f in media_files:
            if f.endswith((".jpg", ".jpeg", ".png")):
                await update.message.reply_photo(
                    photo=open(f, "rb"),
                    caption=caption if first else None
                )
                first = False
                sent += 1
            elif f.endswith((".mp4", ".mov", ".webm", ".mkv")):
                await update.message.reply_video(
                    video=open(f, "rb"),
                    caption=caption if first else None
                )
                first = False
                sent += 1

        if sent == 0:
            await update.message.reply_text("⚠️ Keine Mediendateien gefunden.")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
app.run_polling()