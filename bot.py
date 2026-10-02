import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import InputMediaPhoto, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("insta-bot")

# httpx/httpcore loggen bei INFO jede Request-URL im Klartext, inkl. Bot-Token
# (".../bot<TOKEN>/sendMessage"). Deshalb separat anheben.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------- Config ---

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Optional: Netscape-Cookiefile (z. B. für Instagram-Bildposts, die Login brauchen).
COOKIES_FILE = os.environ.get("COOKIES_FILE") or None
# Wie viele Downloads gleichzeitig laufen dürfen.
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))

if COOKIES_FILE and not os.path.isfile(COOKIES_FILE):
    logger.warning("COOKIES_FILE=%s existiert nicht – wird ignoriert.", COOKIES_FILE)
    COOKIES_FILE = None

TELEGRAM_LIMIT_MB = 50
# Puffer, weil yt-dlp's filesize teils nur eine Schätzung ist.
SAFE_LIMIT_MB = TELEGRAM_LIMIT_MB - 2
MAX_ITEMS = 10  # Telegram: max. 10 Medien pro Album

SUPPORTED_HOSTS = {"instagram.com", "tiktok.com", "youtube.com", "youtu.be", "twitter.com", "x.com"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}

# Progressive (bereits gemergte) Formate bevorzugen, alle unter dem Telegram-Limit.
# Merge aus getrennten Streams nur, wenn nichts Passendes existiert. "worst" ist der
# letzte Notnagel für Quellen ohne filesize-Angabe (meist kurze Clips).
FORMAT_SELECTOR = (
    f"best[height<=720][filesize<{SAFE_LIMIT_MB}M]/"
    f"best[height<=480][filesize<{SAFE_LIMIT_MB}M]/"
    f"best[height<=360][filesize<{SAFE_LIMIT_MB}M]/"
    f"bestvideo[height<=720][filesize<{SAFE_LIMIT_MB - 10}M]+bestaudio[filesize<8M]/"
    f"worst"
)

# yt-dlp meldet das bei reinen Bildposts (Instagram-Fotos/Karussells, Tweets mit Bildern).
NO_VIDEO_RE = re.compile(r"no video formats|no video could be found|there is no video")

ERROR_PATTERNS = [
    (r"login|private|restricted video|authrequired|unauthorized|\b401\b|cookies",
     "🔒 Privater Inhalt oder Login erforderlich – kann nicht heruntergeladen werden."),
    (r"\b429\b|too many requests|rate[- ]?limit",
     "⏳ Zu viele Anfragen gerade (Rate-Limit). Bitte in ein paar Minuten nochmal versuchen."),
    (r"\b403\b|forbidden",
     "🔒 Zugriff verweigert (HTTP 403). Das kann an einer veralteten yt-dlp-Version liegen."),
    (r"unsupported url|unable to extract|no media found",
     "❓ Dieser Link wird nicht unterstützt oder enthält kein erkennbares Medium."),
    (r"video unavailable|content isn't available|has been removed|\b404\b|not found",
     "🚫 Inhalt nicht verfügbar (gelöscht, privat oder eingeschränkt)."),
    (r"not available in your country|geo[- ]?restricted",
     "🌍 Inhalt ist in dieser Region nicht verfügbar."),
    (r"timed out|timeout|connection|network|temporary failure",
     "📡 Verbindungsproblem beim Download. Bitte nochmal versuchen."),
]

DOWNLOAD_SLOTS = asyncio.Semaphore(MAX_PARALLEL)


# --------------------------------------------------------------- Helpers ---

def friendly_error(exc: Exception) -> str:
    """Mappt gängige yt-dlp-/gallery-dl-Fehler auf eine kurze, verständliche Meldung."""
    msg = str(exc).lower()
    for pattern, friendly in ERROR_PATTERNS:
        if re.search(pattern, msg):
            return friendly
    return "❌ Download fehlgeschlagen. Der Link scheint gerade nicht zu funktionieren."


def find_url(text: str) -> str | None:
    """Erste URL im Text, deren Host zu einer unterstützten Plattform gehört."""
    for word in text.split():
        candidate = word.strip("<>()[]\"',;")
        if not candidate:
            continue
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        host = (parsed.hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in SUPPORTED_HOSTS):
            return candidate if "://" in candidate else f"https://{candidate}"
    return None


def collect_media(directory: str) -> tuple[list[Path], list[Path]]:
    files = sorted(p for p in Path(directory).iterdir() if p.is_file())
    images = [p for p in files if p.suffix.lower() in IMAGE_EXT]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    return images, videos


def prepare_cookies(tmpdir: str) -> str | None:
    """Kopiert das Cookiefile pro Job ins Temp-Verzeichnis, damit yt-dlp/gallery-dl
    es nicht im (evtl. read-only gemounteten) Original verändern wollen."""
    if not COOKIES_FILE:
        return None
    target = os.path.join(tmpdir, "cookies.txt")
    shutil.copyfile(COOKIES_FILE, target)
    return target


async def safe_delete(message):
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError as e:
        logger.info("Konnte Statusnachricht nicht löschen: %s", e)


async def send_with_retry(coro_factory, *, retries=1):
    """Führt eine Telegram-Sendeaktion aus und versucht bei Timeout-Fehlern einmal
    automatisch erneut. Große Uploads nahe am 50-MB-Limit können je nach Upload-
    Bandbreite länger brauchen als ein einzelner Versuch."""
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


# -------------------------------------------------------------- Download ---

def download_with_ytdlp(url: str, tmpdir: str, cookies: str | None) -> str:
    """Blockierend – wird per asyncio.to_thread ausgeführt. Gibt die Caption zurück."""
    ydl_opts = {
        "outtmpl": f"{tmpdir}/%(id)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlistend": MAX_ITEMS,
        "writeinfojson": False,
        "writethumbnail": False,
        "format": FORMAT_SELECTOR,
        "merge_output_format": "mp4",
        # Telegram zeigt WebM/VP9 oft nicht als abspielbares Video an. Daher immer
        # nach mp4 konvertieren (wird übersprungen, wenn die Datei schon mp4 ist).
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    description = (info or {}).get("description") or ""
    logger.info("yt-dlp fertig: title=%r duration=%ss", (info or {}).get("title"), (info or {}).get("duration"))
    return description[:1024]


def download_with_gallery_dl(url: str, tmpdir: str, cookies: str | None) -> str:
    """Fallback für Bildposts/Karussells. Blockierend – per asyncio.to_thread."""
    cmd = ["gallery-dl", "--no-mtime", "-D", tmpdir]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    images, videos = collect_media(tmpdir)
    if not images and not videos:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(detail or "gallery-dl hat nichts heruntergeladen")
    return ""


async def fetch_media(url: str, tmpdir: str) -> str:
    """yt-dlp zuerst; bei reinen Bildposts Fallback auf gallery-dl."""
    cookies = prepare_cookies(tmpdir)
    async with DOWNLOAD_SLOTS:
        try:
            return await asyncio.to_thread(download_with_ytdlp, url, tmpdir, cookies)
        except Exception as e:
            if not NO_VIDEO_RE.search(str(e).lower()):
                raise
            logger.info("Kein Video in %s – versuche gallery-dl", url)
        return await asyncio.to_thread(download_with_gallery_dl, url, tmpdir, cookies)


# ---------------------------------------------------------------- Upload ---

async def send_images(message, chunk: list[Path], caption: str | None):
    try:
        if len(chunk) == 1:
            async def _single():
                return await message.reply_photo(photo=chunk[0].read_bytes(), caption=caption)
            await send_with_retry(_single)
        else:
            async def _group():
                media = [
                    InputMediaPhoto(p.read_bytes(), caption=caption if i == 0 else None)
                    for i, p in enumerate(chunk)
                ]
                return await message.reply_media_group(media=media)
            await send_with_retry(_group)
    except BadRequest as e:
        # z. B. ungültige Foto-Dimensionen: als Datei schicken statt aufzugeben.
        logger.info("Foto-Versand abgelehnt (%s) – sende als Dokument", e)
        for i, p in enumerate(chunk):
            async def _doc(p=p, i=i):
                with open(p, "rb") as fh:
                    return await message.reply_document(document=fh, caption=caption if i == 0 else None)
            await send_with_retry(_doc)


async def send_video(message, path: Path, caption: str | None):
    async def _send():
        with open(path, "rb") as fh:
            return await message.reply_video(
                video=fh,
                caption=caption,
                supports_streaming=True,
                read_timeout=280,
                write_timeout=280,
                connect_timeout=60,
            )
    return await send_with_retry(_send)


async def deliver(message, images: list[Path], videos: list[Path], caption: str):
    """Schickt alle Medien. Gibt (gesendet, send_failed, oversized) zurück."""
    sent = 0
    send_failed = False
    oversized = False
    pending_caption = caption or None

    images = images[:MAX_ITEMS]
    if images:
        try:
            await send_images(message, images, pending_caption)
            sent += len(images)
            pending_caption = None
            logger.info("%s Bild(er) gesendet", len(images))
        except TelegramError as e:
            logger.warning("Bild-Versand fehlgeschlagen: %s", e)
            send_failed = True

    for path in videos[:MAX_ITEMS]:
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > TELEGRAM_LIMIT_MB:
            # Selten: yt-dlp's Größenschätzung lag daneben.
            oversized = True
            logger.warning("Video zu groß: %s (%.1f MB > %s MB)", path.name, size_mb, TELEGRAM_LIMIT_MB)
            await message.reply_text(
                f"⚠️ Video ist selbst in kleinster verfügbarer Qualität zu groß für Telegram "
                f"({size_mb:.0f} MB, Limit {TELEGRAM_LIMIT_MB} MB)."
            )
            continue

        logger.info("Starte Video-Upload: %s (%.1f MB)", path.name, size_mb)
        start = time.monotonic()
        try:
            await send_video(message, path, pending_caption)
        except TelegramError as e:
            logger.warning("Video-Versand fehlgeschlagen für %s nach %.1fs: %s", path.name, time.monotonic() - start, e)
            send_failed = True
            continue
        pending_caption = None
        sent += 1
        seconds = time.monotonic() - start
        logger.info("Video gesendet: %s (%.1f MB) in %.1fs -> %.2f MB/s",
                    path.name, size_mb, seconds, size_mb / seconds if seconds > 0 else 0)

    return sent, send_failed, oversized


# ---------------------------------------------------------------- Handler ---

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    url = find_url(message.text)
    if not url:
        return

    chat_id = update.effective_chat.id if update.effective_chat else "?"
    user = (update.effective_user.username or update.effective_user.id) if update.effective_user else "?"
    job_start = time.monotonic()
    logger.info("Neuer Job von %s (chat %s): %s", user, chat_id, url)

    status_msg = await message.reply_text("⏳ Lade herunter...")
    sent = 0
    send_failed = oversized = False

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                caption = await fetch_media(url, tmpdir)
            except Exception as e:
                logger.warning("Download fehlgeschlagen für %s: %s", url, e)
                await message.reply_text(friendly_error(e))
                return

            images, videos = collect_media(tmpdir)
            logger.info("Gefundene Medien: %s Bild(er), %s Video(s)", len(images), len(videos))
            sent, send_failed, oversized = await deliver(message, images, videos, caption)
    finally:
        await safe_delete(status_msg)

    total = time.monotonic() - job_start
    if sent == 0 and not send_failed and not oversized:
        logger.warning("Job für %s ohne Ergebnis in %.1fs", url, total)
        await message.reply_text("⚠️ Keine Mediendateien gefunden.")
    elif send_failed:
        logger.warning("Job für %s teilweise fehlgeschlagen (sent=%s) in %.1fs", url, sent, total)
        await message.reply_text("⚠️ Download war ok, aber der Versand an Telegram ist fehlgeschlagen. Bitte nochmal versuchen.")
    else:
        logger.info("Job für %s abgeschlossen: %s Datei(en) in %.1fs", url, sent, total)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unbehandelte Exception im Handler", exc_info=context.error)


def main():
    request = HTTPXRequest(read_timeout=280, write_timeout=280, connect_timeout=60)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)  # ein langer Download blockiert nicht die anderen Chats
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
