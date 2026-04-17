"""
config.py — Central configuration for DailyPodClips.
All paths, model settings, and FFmpeg presets live here.
"""
import os
from pathlib import Path

# ── Data directories ──────────────────────────────────────────
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DOWNLOADS_DIR = DATA_DIR / "downloads"
PROCESSED_DIR = DATA_DIR / "processed"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
TEMP_DIR = DATA_DIR / "temp"
AUTH_DIR = DATA_DIR / "auth"
COOKIES_DIR = DATA_DIR / "cookies"

# Ensure all dirs exist at import time
for d in [DOWNLOADS_DIR, PROCESSED_DIR, TRANSCRIPTS_DIR, TEMP_DIR, AUTH_DIR, COOKIES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── Whisper settings (OCL CPU-safe) ──────────────────────────
WHISPER_MODEL = "base"          # base.en for English-only, or "base" for multilingual
WHISPER_COMPUTE_TYPE = "int8"   # int8 is fastest on ARM CPU
WHISPER_BEAM_SIZE = 1           # Greedy decoding — fast on CPU
WHISPER_DEVICE = "cpu"

# ── FFmpeg export presets (social-media-optimized) ────────────
# High quality 1:1 square for TikTok/Reels/Shorts
FFMPEG_VIDEO_CODEC = "libx264"
FFMPEG_VIDEO_PRESET = "faster"   # Balance speed vs quality on ARM CPU
FFMPEG_VIDEO_CRF = "20"         # Near-visually-lossless
FFMPEG_VIDEO_PROFILE = "high"
FFMPEG_VIDEO_LEVEL = "4.1"
FFMPEG_PIXEL_FORMAT = "yuv420p"
FFMPEG_AUDIO_CODEC = "aac"
FFMPEG_AUDIO_BITRATE = "192k"
FFMPEG_AUDIO_SAMPLE_RATE = "48000"

# ── Face tracking settings ────────────────────────────────────
FACE_TRACK_FPS = 2              # Sample 2 frames/sec for face detection (saves CPU)
FACE_TRACK_SMOOTHING = 15       # Number of frames to smooth crop movement
FACE_TRACK_MIN_CONFIDENCE = 0.5 # Minimum face detection confidence

# ── Google Drive OAuth2 ───────────────────────────────────────
GOOGLE_CREDENTIALS_FILE = AUTH_DIR / "credentials.json"
GOOGLE_TOKEN_FILE = AUTH_DIR / "token.json"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# ── Server ────────────────────────────────────────────────────
SERVER_PORT = 8000
