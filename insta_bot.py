import os, glob, tempfile, logging, time
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("insta-bot")

# httpx loggt bei INFO jede Request-URL im Klartext, inkl. Bot-Token
# (".../bot<TOKEN>/sendMessage"). Level hier separat anheben, damit der
# Token nicht in den Logs landet, während der Rest des Loggings unverändert
# auf INFO bleibt.
logging.getLogger("httpx").setLevel(logging.WARNING)

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
        (("403", "forbidden"),
         "🔒 Zugriff verweigert (HTTP 403). Das kann an einer veralteten yt-dlp-Version liegen."),
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


async def send_with_retry(coro_factory, *, retries=1):
    """Führt eine Telegram-Sendeaktion aus und versucht bei Timeout-Fehlern
    einmal automatisch erneut, bevor der Fehler nach oben gereicht wird.
    Große Video-Uploads (nahe am 50MB-Limit) können je nach Upload-
    Bandbreite des Containers länger brauchen als ein einzelner Versuch."""
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except TelegramError as e:
            attempt += 1
            is_timeout = "timed out" in str(e).lower() or "timeout" in str(e).lower()
            if is_timeout and attempt <= retries:
                logger.info("Sendeversuch %s ist getimeoutet, versuche erneut: %s", attempt, e)
                continue
            raise


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

    chat_id = update.effective_chat.id if update.effective_chat else "?"
    user = update.effective_user.username or update.effective_user.id if update.effective_user else "?"
    job_start = time.monotonic()
    logger.info("Neuer Job von %s (chat %s): %s", user, chat_id, url)

    status_msg = await update.message.reply_text("⏳ Lade herunter...")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            'outtmpl': f'{tmpdir}/%(title)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'writeinfojson': False,
            'writethumbnail': False,
            'format': FORMAT_SELECTOR,
            'merge_output_format': 'mp4',
            # Telegram zeigt WebM/VP9 (typisch bei YouTube Shorts, wenn kein
            # mp4-Format verfügbar ist) oft nicht als abspielbares Video an,
            # sondern liefert es als generische Datei aus. Deshalb hier immer
            # nach mp4 konvertieren – wird übersprungen, wenn die Datei
            # bereits mp4 ist, kostet also im Normalfall nichts.
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
        }

        download_start = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                caption = info.get("description", "")[:1024] if info else ""
        except Exception as e:
            logger.warning("Download fehlgeschlagen für %s: %s", url, e)
            await safe_delete(status_msg)
            await update.message.reply_text(friendly_error(e))
            return

        download_seconds = time.monotonic() - download_start
        logger.info(
            "Download fertig für %s: title=%r duration=%ss in %.1fs",
            url, info.get("title") if info else None, info.get("duration") if info else None, download_seconds,
        )

        media_files = sorted(glob.glob(os.path.join(tmpdir, "*")))
        logger.info("Gefundene Dateien nach Download: %s", [os.path.basename(f) for f in media_files])

        mp4_files = [f for f in media_files if f.endswith((".mp4", ".mov", ".webm", ".mkv"))]
        if mp4_files:
            mp4_files = [max(mp4_files, key=os.path.getsize)]

        img_files = [f for f in media_files if f.endswith((".jpg", ".jpeg", ".png"))]
        final_files = img_files + mp4_files

        sent = 0
        first = True
        send_failed = False
        oversized = False

        for f in final_files:
            try:
                if f.endswith((".jpg", ".jpeg", ".png")):
                    await send_with_retry(lambda f=f: update.message.reply_photo(
                        photo=open(f, "rb"),
                        caption=caption if first else None
                    ))
                    first = False
                    sent += 1
                    logger.info("Foto gesendet: %s", os.path.basename(f))
                elif f.endswith((".mp4", ".mov", ".webm", ".mkv")):
                    size_mb = os.path.getsize(f) / (1024 * 1024)
                    if size_mb > TELEGRAM_LIMIT_MB:
                        # Rare: yt-dlp's size estimate was off. Nothing was sent, so
                        # there's nothing to clean up beyond the status message below.
                        # Flag it explicitly instead of relying on sent==0, so the
                        # generic "keine Mediendateien gefunden" fallback below
                        # doesn't also fire and produce a confusing double message.
                        oversized = True
                        logger.warning(
                            "Video zu groß für Telegram: %s (%.1f MB > %s MB Limit)",
                            os.path.basename(f), size_mb, TELEGRAM_LIMIT_MB,
                        )
                        await update.message.reply_text(
                            f"⚠️ Video ist selbst in kleinster verfügbarer Qualität zu groß für Telegram "
                            f"({size_mb:.0f} MB, Limit {TELEGRAM_LIMIT_MB} MB)."
                        )
                        continue
                    await send_with_retry(lambda f=f: update.message.reply_video(
                        video=open(f, "rb"),
                        caption=caption if first else None,
                        read_timeout=280,
                        write_timeout=280,
                        connect_timeout=60,
                    ))
                    first = False
                    sent += 1
                    logger.info("Video gesendet: %s (%.1f MB)", os.path.basename(f), size_mb)
            except TelegramError as e:
                logger.warning("Senden an Telegram fehlgeschlagen für %s: %s", f, e)
                send_failed = True

        await safe_delete(status_msg)

        total_seconds = time.monotonic() - job_start
        if sent == 0 and not send_failed and not oversized:
            logger.warning("Job für %s abgeschlossen ohne Ergebnis (keine Mediendateien) in %.1fs", url, total_seconds)
            await update.message.reply_text("⚠️ Keine Mediendateien gefunden.")
        elif send_failed:
            logger.warning("Job für %s teilweise fehlgeschlagen (sent=%s) in %.1fs", url, sent, total_seconds)
            await update.message.reply_text("⚠️ Download war ok, aber der Versand an Telegram ist fehlgeschlagen. Bitte nochmal versuchen.")
        else:
            logger.info("Job für %s abgeschlossen: %s Datei(en) gesendet in %.1fs", url, sent, total_seconds)


request = HTTPXRequest(read_timeout=280, write_timeout=280, connect_timeout=60)
app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
app.run_polling()
