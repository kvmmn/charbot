FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_MODE=webhook \
    PORT=8081

COPY pyproject.toml README.md ./
COPY charbot ./charbot
COPY schema.sql ./schema.sql

# Skip faster-whisper on Fly (too heavy for a small always-on machine).
RUN pip install --no-cache-dir \
      "python-telegram-bot[job-queue]>=21.6,<22" \
      "fastapi>=0.115.0" \
      "uvicorn[standard]>=0.32.0" \
      "pydantic-settings>=2.6.0" \
      "python-dateutil>=2.9.0" \
      "psycopg[binary]>=3.2.0" \
      setuptools \
    && pip install --no-cache-dir --no-deps .

EXPOSE 8081

CMD ["python", "-m", "charbot.main", "--mode", "webhook"]
