# Official lightweight Python image
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable stdout/stderr flushing
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies (ffmpeg is required for MP3/video processing)
# Keep the image small by cleaning apt lists afterwards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create app directory and non-root user
RUN useradd -m -s /bin/bash appuser
WORKDIR /app
COPY --chown=appuser:appuser requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY --chown=appuser:appuser . /app

# Switch to non-root user
USER appuser

# Default environment variables (override at runtime)
ENV DB_PATH=/app/data/users.db

# Command to run the bot. Provide BOT_TOKEN etc. via environment at runtime.
CMD ["python", "bot.py"]
