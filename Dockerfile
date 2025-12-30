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

# Optional: non-root user
RUN useradd -m botuser || true
USER botuser

ENV PYTHONUNBUFFERED=1

CMD ["python", "signup_bot.py"]
