# DailyPodClips 🎙✂️

> Self-hosted Opus Clips alternative — a viral podcast clip factory running on Oracle Cloud Free Tier.

## Architecture

```
Browser (phone/laptop) ←→ Cloudflare Tunnel ←→ Docker (port 8000)
                                                  ├── FastAPI + SSE
                                                  ├── yt-dlp / aria2c
                                                  ├── faster-whisper (base, int8)
                                                  ├── auto-editor (silence removal)
                                                  ├── MediaPipe (face tracking)
                                                  └── FFmpeg (cut/join/reframe/export)
```

## Pipeline

| Block | Function |
|-------|----------|
| **01 — Downloader** | YouTube/direct link → MP4 via yt-dlp or aria2c |
| **02 — Transcriber** | Whisper base model → `[00000s → 00002s] text` format → GDrive upload |
| **03 — Clip Processor** | Paste AI JSON → lossless cut → silence removal → HD render → 1:1 face reframe |
| **04 — Gallery** | Preview processed clips with HTML5 video players |

## Quick Start

```bash
# Clone
git clone https://github.com/anziltz000/DailyPodClips.git
cd DailyPodClips

# Build and run
docker compose up --build -d

# Access at http://localhost:port
```

## Google Drive Setup

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the Google Drive API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download `credentials.json` → place in `data/auth/`
5. In the UI: click "Get Auth URL" → authorize → paste code back

## OCL Free Tier Specs

- **Instance**: Ampere A1 ARM64, 4 OCPUs, 24GB RAM
- **GPU**: None — all processing is CPU-optimized
- **Whisper**: `base` model, `int8` compute type
- **Face tracking**: 2fps sampling to minimize CPU load

## License

MIT
