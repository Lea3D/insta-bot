FROM ghcr.io/actions/python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir instaloader python-telegram-bot
COPY bot.py .
CMD ["python", "bot.py"]