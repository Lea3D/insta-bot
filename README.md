# insta-bot

Telegram-Bot: Link aus Instagram/TikTok/YouTube/Twitter/X in den Chat werfen,
der Bot lädt das Medium und schickt es zurück (Limit 50 MB).

- Videos/Reels: yt-dlp
- Bildposts/Karussells: automatischer Fallback auf gallery-dl, mehrere Bilder als Album
- Optional `COOKIES_FILE` für Inhalte, die Login verlangen

## Betrieb

    cp .env.example .env   # BOT_TOKEN eintragen
    docker compose up -d --build

Update des fertigen Images: `docker compose pull && docker compose up -d`

## CI

- `check-updates.yml`: täglich yt-dlp/gallery-dl auf PyPI prüfen, `requirements.txt` bumpen, Build anstoßen
- `docker.yml`: baut und pusht `ghcr.io/lea3d/insta-bot:latest`
