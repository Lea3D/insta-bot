import os, glob, tempfile, logging
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("insta-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

TELEGRAM_LIMIT_MB = 50
# Leave headroom because yt-dlp's filesize is sometimes just an estimate.
SAFE_LIMIT_MB = TELEGRAM_LIMIT_MB - 2

# Prefer progressive (already-merged) formats at decreasing quality, all capped
# below the Telegram limit. Only fall back to merging separate video+audio
# (with tighter per-stream caps, since the filesize filter can't sum them) if
# no small-enough combined format exists. "worst" is the last-resort catch-all
# for sources that don't report filesize at all (common for short-form clips,
# which are rarely anywhere near the limit anyway).
FORMAT_SELECTOR = (
    f"best[height<=720][filesize<{SAFE_LIMIT_MB}M]/"
    f"best[height<=480][filesize<{SAFE_LIMIT_MB}M]/"
    f"best[height<=360][filesize<{SAFE_LIMIT_MB}M]/"
    f"bestvideo[height<=720][filesize<{SAFE_LIMIT_MB - 10}M]+bestaudio[filesize<8M]/"
    f"worst"
)


def friendly_error(exc: Exception) -> str:
    """Map common yt-dlp / platform errors to a short, understandable message."""
    msg = str(exc).lower()

    patterns = [
        (("login required", "private", "restricted video"),
         "🔒 Privater Inhalt oder Login erforderlich – kann nicht heruntergeladen werden."),
        (("429", "too many requests", "rate-limit", "rate limit"),
         "⏳ Zu viele Anfragen gerade (Rate-Limit). Bitte in ein paar Minuten nochmal versuchen."),
        (("unsupported url", "no video formats", "unable to extract", "no media found"),
         "❓ Dieser Link wird nicht unterstützt oder enthält kein erkennbares Medium."),
        (("video unavailable", "content isn't available", "has been removed", "404", "not found"),
         "🚫 Inhalt nicht verfügbar (gelöscht, privat oder eingeschränkt)."),
        (("not available in your country", "geo-restricted", "geo restricted"),
         "🌍 Inhalt ist in dieser Region nicht verfügbar."),
        (("timed out", "timeout", "connection", "network", "temporary failure"),
         "📡 Verbindungsproblem beim Download. Bitte nochmal versuchen."),
    ]

    for keywords, friendly in patterns:
        if any(k in msg for k in keywords):
            return friendly

    return "❌ Download fehlgeschlagen. Der Link scheint gerade nicht zu funktionieren."


async def safe_delete(message):
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError as e:
        logger.info("Konnte Statusnachricht nicht löschen: %s", e)


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

    status_msg = await update.message.reply_text("⏳ Lade herunter...")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            'outtmpl': f'{tmpdir}/%(title)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'writeinfojson': False,
            'writethumbnail': False,
            'format': FORMAT_SELECTOR,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                caption = info.get("description", "")[:1024] if info else ""
        except Exception as e:
            logger.warning("Download fehlgeschlagen für %s: %s", url, e)
            await safe_delete(status_msg)
            await update.message.reply_text(friendly_error(e))
            return

        media_files = sorted(glob.glob(os.path.join(tmpdir, "*")))

        mp4_files = [f for f in media_files if f.endswith((".mp4", ".mov", ".webm", ".mkv"))]
        if mp4_files:
            mp4_files = [max(mp4_files, key=os.path.getsize)]

        img_files = [f for f in media_files if f.endswith((".jpg", ".jpeg", ".png"))]
        final_files = img_files + mp4_files

        sent = 0
        first = True
        send_failed = False

        for f in final_files:
            try:
                if f.endswith((".jpg", ".jpeg", ".png")):
                    await update.message.reply_photo(
                        photo=open(f, "rb"),
                        caption=caption if first else None
                    )
                    first = False
                    sent += 1
                elif f.endswith((".mp4", ".mov", ".webm", ".mkv")):
                    size_mb = os.path.getsize(f) / (1024 * 1024)
                    if size_mb > TELEGRAM_LIMIT_MB:
                        # Rare: yt-dlp's size estimate was off. Nothing was sent, so
                        # there's nothing to clean up beyond the status message below.
                        await update.message.reply_text(
                            f"⚠️ Video ist selbst in kleinster verfügbarer Qualität zu groß für Telegram "
                            f"({size_mb:.0f} MB, Limit {TELEGRAM_LIMIT_MB} MB)."
                        )
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
            except TelegramError as e:
                logger.warning("Senden an Telegram fehlgeschlagen für %s: %s", f, e)
                send_failed = True

        await safe_delete(status_msg)

        if sent == 0 and not send_failed:
            await update.message.reply_text("⚠️ Keine Mediendateien gefunden.")
        elif send_failed:
            await update.message.reply_text("⚠️ Download war ok, aber der Versand an Telegram ist fehlgeschlagen. Bitte nochmal versuchen.")


request = HTTPXRequest(read_timeout=120, write_timeout=120, connect_timeout=60)
app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
app.run_polling()