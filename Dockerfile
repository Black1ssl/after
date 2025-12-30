FROM python:3.11-slim

# ======================
# SYSTEM DEPENDENCIES
# ======================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ======================
# WORKDIR
# ======================
WORKDIR /app

# ======================
# INSTALL PYTHON DEPS
# ======================
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ======================
# COPY APP
# ======================
COPY . .

# ======================
# SECURITY: NON-ROOT USER
# ======================
RUN useradd -m botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

USER botuser

# ======================
# ENV
# ======================
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ======================
# START BOT
# ======================
CMD ["python", "bot.py"]
