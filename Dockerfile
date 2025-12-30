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

# Verify requests is installed (fail early if not)
RUN pip show requests || (echo "ERROR: requests not installed" && exit 1)

# Copy app files
COPY . /app

# Optional: non-root user
RUN useradd -m botuser || true
USER botuser

ENV PYTHONUNBUFFERED=1

# Pastikan nama file yang dijalankan sesuai (bot.py / signup_bot.py)
CMD ["python", "bot.py"]
