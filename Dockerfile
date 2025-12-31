# Use a slim Python base
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    DB_PATH=/app/data/users.db

# Install ffmpeg + build deps required by some packages/yt-dlp postprocessors
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      gcc \
      build-essential \
      libffi-dev \
      libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . /app

# Prepare data dir
RUN mkdir -p /app/data && chown -R root:root /app

# Create a non-root user for running the bot
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

# Run the bot
CMD ["python", "bot.py"]
