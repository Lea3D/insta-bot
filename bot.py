import os, glob, tempfile
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import instaloader

BOT_TOKEN = os.environ["BOT_TOKEN"]

L = instaloader.Instaloader(
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    post_metadata_txt_pattern=""
)

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    shortcode = None
    for segment in ["/p/", "/reel/"]:
        if segment in url:
            shortcode = url.split(segment)[1].strip("/").split("/")[0]
            break

    if not shortcode:
        await update.message.reply_text("❌ Keine gültige Instagram-URL erkannt.")
        return

    await update.message.reply_text("⏳ Lade herunter...")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.dirname_pattern = tmpdir
            L.download_post(post, target=tmpdir)
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {e}")
            return

        media_files = sorted(glob.glob(os.path.join(tmpdir, "**", "*"), recursive=True))
        sent = 0
        for f in media_files:
            if f.endswith((".jpg", ".jpeg", ".png")):
                await update.message.reply_photo(photo=open(f, "rb"))
                sent += 1
            elif f.endswith((".mp4", ".mov")):
                await update.message.reply_video(video=open(f, "rb"))
                sent += 1

        if sent == 0:
            await update.message.reply_text("⚠️ Keine Mediendateien gefunden.")
        else:
            await update.message.reply_text(f"✅ {sent} Datei(en) gesendet.")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
app.run_polling()