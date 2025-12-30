FROM python:3.11-slim

# Install ffmpeg and required packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app files
COPY . /app

# Create non-root user, create data dir and set ownership (so bot can write)
RUN useradd -m botuser || true \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

ENV PYTHONUNBUFFERED=1

# Pastikan menjalankan file yang sesuai (ubah ke signup_bot.py jika itu yang kamu pakai)
CMD ["python", "bot.py"]
