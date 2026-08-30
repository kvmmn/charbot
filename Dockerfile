FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_MODE=webhook \
    PORT=8081 \
    DATABASE_PATH=/data/charbot.db

COPY pyproject.toml README.md ./
COPY charbot ./charbot
COPY schema.sql ./schema.sql

RUN pip install --no-cache-dir . && mkdir -p /data

EXPOSE 8081

CMD ["python", "-m", "charbot.main", "--mode", "webhook"]
