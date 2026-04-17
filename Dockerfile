# ──────────────────────────────────────────────────────────────
# DailyPodClips — ARM64-native Dockerfile for OCL Free Tier
# Base: Ubuntu 22.04 ARM64 (guaranteed ARM wheel compat)
# ──────────────────────────────────────────────────────────────
FROM ubuntu:22.04

# Prevent interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data

# ── System dependencies ──────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev python3-venv \
    ffmpeg \
    aria2 \
    wget curl git ca-certificates gnupg \
    # MediaPipe / OpenCV native deps
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender-dev \
    # Build tools for any wheels that need compiling
    build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

# ── Install Deno (required by yt-dlp for YouTube JS challenge solver) ──
RUN apt-get update && apt-get install -y unzip && \
    curl -fsSL https://deno.land/install.sh | sh && \
    mv /root/.deno/bin/deno /usr/local/bin/deno && \
    deno --version

WORKDIR /app

# ── Python dependencies ──────────────────────────────────────
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ── Copy application code ────────────────────────────────────
COPY . .

# ── Create data directories ──────────────────────────────────
RUN mkdir -p /app/data/downloads /app/data/processed /app/data/transcripts \
    /app/data/temp /app/data/auth /app/data/cookies

EXPOSE 8000

# ── Start FastAPI via uvicorn ─────────────────────────────────
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
