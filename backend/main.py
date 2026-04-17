"""
main.py — FastAPI backend for DailyPodClips.
Handles: Download, Transcribe, Clip Processing, Face Tracking, GDrive upload.
Uses SSE for real-time cross-device log streaming.
"""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

import aiofiles
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel

from backend.config import (
    DOWNLOADS_DIR, PROCESSED_DIR, TRANSCRIPTS_DIR, TEMP_DIR,
    AUTH_DIR, COOKIES_DIR, GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE,
    GOOGLE_SCOPES, WHISPER_MODEL, WHISPER_COMPUTE_TYPE, WHISPER_BEAM_SIZE,
    WHISPER_DEVICE, FFMPEG_VIDEO_CODEC, FFMPEG_VIDEO_PRESET, FFMPEG_VIDEO_CRF,
    FFMPEG_VIDEO_PROFILE, FFMPEG_VIDEO_LEVEL, FFMPEG_PIXEL_FORMAT,
    FFMPEG_AUDIO_CODEC, FFMPEG_AUDIO_BITRATE, FFMPEG_AUDIO_SAMPLE_RATE,
    FACE_TRACK_FPS, FACE_TRACK_SMOOTHING, FACE_TRACK_MIN_CONFIDENCE,
)

# ── App Setup ─────────────────────────────────────────────────
app = FastAPI(title="DailyPodClips", version="1.0.0")

# Serve frontend static files
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# ── Global State ──────────────────────────────────────────────
# Track active subprocesses so we can kill them
active_processes: Dict[str, subprocess.Popen] = {}
# SSE log queues per block (block_id -> list of subscriber queues)
log_subscribers: Dict[str, list] = {}
# Current video filename (set after download)
current_video: Optional[str] = None
# Current transcript filename
current_transcript: Optional[str] = None


# ── Pydantic Models ───────────────────────────────────────────
class DownloadRequest(BaseModel):
    url: str
    cookies: Optional[str] = None

class AuthCodeRequest(BaseModel):
    code: str

class GDriveFolderRequest(BaseModel):
    folder_id: str

class ClipJSON(BaseModel):
    json_data: str

class ReframeRequest(BaseModel):
    clip_filename: str


# ── SSE Helpers ───────────────────────────────────────────────
async def broadcast_log(block_id: str, message: str):
    """Send a log message to all SSE subscribers of a block."""
    if block_id not in log_subscribers:
        log_subscribers[block_id] = []
    for queue in log_subscribers[block_id]:
        await queue.put(message)

async def run_subprocess_with_logs(block_id: str, cmd: list, cwd: str = None, env: dict = None) -> int:
    """
    Run a subprocess, stream stdout/stderr to SSE subscribers line-by-line.
    Returns the process return code.
    """
    await broadcast_log(block_id, f"▶ Running: {' '.join(cmd[:5])}...")

    merged_env = {**os.environ, **(env or {})}
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=merged_env,
    )
    # Store so we can kill it later
    active_processes[block_id] = process

    async def stream_pipe(pipe, prefix=""):
        async for line in pipe:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                await broadcast_log(block_id, f"{prefix}{text}")

    # Stream both stdout and stderr concurrently
    await asyncio.gather(
        stream_pipe(process.stdout),
        stream_pipe(process.stderr, "⚠ "),
    )
    await process.wait()

    # Clean up
    active_processes.pop(block_id, None)
    status = "✅ Done" if process.returncode == 0 else f"❌ Failed (code {process.returncode})"
    await broadcast_log(block_id, status)
    return process.returncode


# ── SSE Endpoint ──────────────────────────────────────────────
@app.get("/api/logs/{block_id}")
async def stream_logs(block_id: str, request: Request):
    """SSE endpoint — frontend subscribes to receive real-time logs for a block."""
    queue = asyncio.Queue()
    if block_id not in log_subscribers:
        log_subscribers[block_id] = []
    log_subscribers[block_id].append(queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {"event": "log", "data": message}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "keepalive"}
        finally:
            log_subscribers[block_id].remove(queue)

    return EventSourceResponse(event_generator())


# ── BLOCK 1: DOWNLOADER ──────────────────────────────────────
@app.post("/api/download")
async def download_video(req: DownloadRequest):
    """Download video using yt-dlp or aria2c. Streams progress via SSE."""
    global current_video
    block_id = "downloader"
    url = req.url.strip()

    if not url:
        raise HTTPException(400, "URL is required")

    await broadcast_log(block_id, f"📥 Starting download: {url}")

    # Save cookies file if provided
    cookie_path = None
    if req.cookies and req.cookies.strip():
        cookie_path = str(COOKIES_DIR / "cookies.txt")
        async with aiofiles.open(cookie_path, "w") as f:
            await f.write(req.cookies)
        await broadcast_log(block_id, "🍪 Cookies file saved")

    # Determine download method
    is_direct_link = url.lower().endswith(('.mp4', '.mkv', '.webm', '.mov'))

    if is_direct_link:
        # Use aria2c for direct links (faster, supports resume)
        filename = url.split("/")[-1].split("?")[0]
        cmd = [
            "aria2c", "-x", "16", "-s", "16", "--max-connection-per-server=16",
            "-d", str(DOWNLOADS_DIR), "-o", filename, url
        ]
    else:
        # Use yt-dlp for YouTube/platform links
        # Format: try best 1080p, broad fallbacks so it never fails on format
        cmd = [
            "yt-dlp",
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv+ba/b",
            "--merge-output-format", "mp4",
            "--remux-video", "mp4",
            "-o", str(DOWNLOADS_DIR / "%(title)s.%(ext)s"),
            "--no-playlist",
            "--progress",
            "--newline",
            # Retry and resilience flags
            "--retries", "3",
            "--fragment-retries", "3",
        ]
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(url)

    returncode = await run_subprocess_with_logs(block_id, cmd)

    if returncode != 0:
        raise HTTPException(500, "Download failed — check logs")

    # Find the downloaded file (most recent mp4)
    mp4_files = sorted(DOWNLOADS_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True)
    if not mp4_files:
        raise HTTPException(500, "No MP4 file found after download")

    current_video = mp4_files[0].name
    await broadcast_log(block_id, f"✅ Saved: {current_video}")
    return {"status": "ok", "filename": current_video}


# ── BLOCK 1: STOP ────────────────────────────────────────────
@app.post("/api/stop/{block_id}")
async def stop_process(block_id: str):
    """Kill the subprocess running in a specific block."""
    process = active_processes.get(block_id)
    if process and process.returncode is None:
        try:
            process.kill()
            await broadcast_log(block_id, "🛑 Process killed by user")
        except Exception as e:
            await broadcast_log(block_id, f"⚠ Kill error: {e}")
        active_processes.pop(block_id, None)
        return {"status": "stopped"}
    return {"status": "no_active_process"}


# ── BLOCK 2: TRANSCRIBER ─────────────────────────────────────
@app.post("/api/transcribe")
async def transcribe_video():
    """Transcribe the downloaded video using faster-whisper (CPU, base model)."""
    global current_transcript
    block_id = "transcriber"

    if not current_video:
        raise HTTPException(400, "No video downloaded yet")

    video_path = DOWNLOADS_DIR / current_video
    if not video_path.exists():
        raise HTTPException(404, f"Video not found: {current_video}")

    await broadcast_log(block_id, f"🎙 Transcribing: {current_video}")
    await broadcast_log(block_id, f"  Model: {WHISPER_MODEL} | Compute: {WHISPER_COMPUTE_TYPE}")

    # Run transcription in a thread to not block the event loop
    def _transcribe():
        from faster_whisper import WhisperModel
        model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        segments, info = model.transcribe(
            str(video_path),
            beam_size=WHISPER_BEAM_SIZE,
            word_timestamps=False,
        )
        return list(segments), info

    loop = asyncio.get_event_loop()
    segments, info = await loop.run_in_executor(None, _transcribe)

    await broadcast_log(block_id, f"  Language: {info.language} (prob: {info.language_probability:.2f})")
    await broadcast_log(block_id, f"  Duration: {info.duration:.1f}s")

    # Format transcript in the required format: [00000s → 00002s] Text
    transcript_lines = []
    for seg in segments:
        start_s = int(seg.start)
        end_s = int(seg.end)
        # Pad to 5 digits for consistency
        line = f"[{start_s:05d}s → {end_s:05d}s] {seg.text.strip()}"
        transcript_lines.append(line)

    transcript_text = "\n".join(transcript_lines)

    # Save transcript file
    video_stem = Path(current_video).stem
    transcript_name = f"{video_stem}_transcription.txt"
    transcript_path = TRANSCRIPTS_DIR / transcript_name
    async with aiofiles.open(transcript_path, "w", encoding="utf-8") as f:
        await f.write(transcript_text)

    current_transcript = transcript_name
    await broadcast_log(block_id, f"✅ Transcript saved: {transcript_name}")
    await broadcast_log(block_id, f"  Total segments: {len(segments)}")

    return {
        "status": "ok",
        "filename": transcript_name,
        "transcript": transcript_text,
    }


# ── BLOCK 2: GDRIVE AUTH ─────────────────────────────────────
@app.get("/api/gdrive/auth-url")
async def get_gdrive_auth_url():
    """Generate Google OAuth2 URL for user to authorize in browser."""
    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise HTTPException(400, "Upload credentials.json to /app/data/auth/ first")

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        str(GOOGLE_CREDENTIALS_FILE), GOOGLE_SCOPES
    )
    # Use OOB-like redirect so user copies the code
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return {"auth_url": auth_url}


@app.post("/api/gdrive/auth-code")
async def submit_gdrive_auth_code(req: AuthCodeRequest):
    """Exchange the authorization code for tokens and save."""
    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise HTTPException(400, "Upload credentials.json first")

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        str(GOOGLE_CREDENTIALS_FILE), GOOGLE_SCOPES
    )
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    flow.fetch_token(code=req.code)

    # Save the token
    creds = flow.credentials
    async with aiofiles.open(str(GOOGLE_TOKEN_FILE), "w") as f:
        await f.write(creds.to_json())

    return {"status": "ok", "message": "Google Drive authenticated successfully"}


@app.post("/api/gdrive/upload")
async def upload_to_gdrive(req: GDriveFolderRequest):
    """Upload transcript or processed clips to Google Drive folder."""
    block_id = "transcriber"
    folder_id = req.folder_id.strip()

    if not GOOGLE_TOKEN_FILE.exists():
        raise HTTPException(400, "Not authenticated — complete Google auth first")

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_FILE), GOOGLE_SCOPES)
    service = build("drive", "v3", credentials=creds)

    uploaded = []

    # Upload transcript if exists
    if current_transcript:
        t_path = TRANSCRIPTS_DIR / current_transcript
        if t_path.exists():
            await broadcast_log(block_id, f"☁ Uploading transcript: {current_transcript}")
            media = MediaFileUpload(str(t_path), resumable=True)
            file_meta = {"name": current_transcript, "parents": [folder_id]}
            result = service.files().create(body=file_meta, media_body=media, fields="id").execute()
            uploaded.append({"name": current_transcript, "id": result["id"]})
            await broadcast_log(block_id, f"✅ Uploaded: {current_transcript}")

    # Upload processed clips
    for clip_path in PROCESSED_DIR.glob("*.mp4"):
        await broadcast_log(block_id, f"☁ Uploading clip: {clip_path.name}")
        media = MediaFileUpload(str(clip_path), resumable=True)
        file_meta = {"name": clip_path.name, "parents": [folder_id]}
        result = service.files().create(body=file_meta, media_body=media, fields="id").execute()
        uploaded.append({"name": clip_path.name, "id": result["id"]})
        await broadcast_log(block_id, f"✅ Uploaded: {clip_path.name}")

    return {"status": "ok", "uploaded": uploaded}


# ── BLOCK 3: VALIDATE JSON ───────────────────────────────────
@app.post("/api/validate-json")
async def validate_clip_json(req: ClipJSON):
    """Validate the AI-generated clip JSON structure."""
    try:
        data = json.loads(req.json_data)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")

    if not isinstance(data, list):
        raise HTTPException(400, "JSON must be an array")

    for i, clip in enumerate(data):
        if "clip_number" not in clip:
            raise HTTPException(400, f"Clip {i}: missing 'clip_number'")
        if "segments_to_keep" not in clip:
            raise HTTPException(400, f"Clip {i}: missing 'segments_to_keep'")
        if not isinstance(clip["segments_to_keep"], list):
            raise HTTPException(400, f"Clip {i}: 'segments_to_keep' must be an array")
        for j, seg in enumerate(clip["segments_to_keep"]):
            if "start_timestamp" not in seg or "end_timestamp" not in seg:
                raise HTTPException(400, f"Clip {i} segment {j}: missing timestamps")

    return {"status": "valid", "clip_count": len(data)}


# ── BLOCK 3: PROCESS CLIPS ───────────────────────────────────
def parse_timestamp(ts: str) -> float:
    """Convert HH:MM:SS or SS to seconds."""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return float(parts[0])


@app.post("/api/process-clips")
async def process_clips(req: ClipJSON):
    """
    Process clips from JSON: lossless cut → silence removal → final render.
    Face tracking is done separately via /api/reframe.
    """
    block_id = "clipprocessor"

    if not current_video:
        raise HTTPException(400, "No video downloaded yet")

    video_path = DOWNLOADS_DIR / current_video
    if not video_path.exists():
        raise HTTPException(404, f"Video not found: {current_video}")

    try:
        clips = json.loads(req.json_data)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")

    await broadcast_log(block_id, f"🎬 Processing {len(clips)} clips from: {current_video}")

    results = []

    for clip in clips:
        clip_num = clip["clip_number"]
        segments = clip["segments_to_keep"]
        hook = clip.get("hook", f"clip_{clip_num}")
        # Sanitize filename
        safe_hook = re.sub(r'[^\w\s-]', '', hook)[:80].strip().replace(' ', '_')
        if not safe_hook:
            safe_hook = f"clip_{clip_num}"

        await broadcast_log(block_id, f"\n── Clip {clip_num}: {safe_hook} ──")

        # STEP 1: Lossless cut each segment
        segment_files = []
        for i, seg in enumerate(segments):
            start = parse_timestamp(seg["start_timestamp"])
            end = parse_timestamp(seg["end_timestamp"])
            duration = end - start

            seg_file = TEMP_DIR / f"clip{clip_num}_seg{i}.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(seg_file),
            ]
            await broadcast_log(block_id, f"  ✂ Segment {i+1}: {seg['start_timestamp']} → {seg['end_timestamp']}")
            rc = await run_subprocess_with_logs(block_id, cmd)
            if rc != 0:
                await broadcast_log(block_id, f"  ❌ Segment {i+1} cut failed")
                continue
            segment_files.append(seg_file)

        if not segment_files:
            await broadcast_log(block_id, f"  ❌ No segments extracted for clip {clip_num}")
            continue

        # STEP 2: Concat segments using FFmpeg concat demuxer
        concat_file = TEMP_DIR / f"clip{clip_num}_concat.txt"
        async with aiofiles.open(concat_file, "w") as f:
            for sf in segment_files:
                await f.write(f"file '{sf}'\n")

        joined_file = TEMP_DIR / f"clip{clip_num}_joined.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(joined_file),
        ]
        await broadcast_log(block_id, "  🔗 Joining segments...")
        rc = await run_subprocess_with_logs(block_id, cmd)
        if rc != 0:
            await broadcast_log(block_id, "  ❌ Concat failed")
            continue

        # STEP 3: Silence removal with auto-editor
        silence_removed = TEMP_DIR / f"clip{clip_num}_nosilence.mp4"
        cmd = [
            "auto-editor", str(joined_file),
            "--margin", "0.15sec",
            "--output", str(silence_removed),
            "--no-open",
        ]
        await broadcast_log(block_id, "  🔇 Removing silence...")
        rc = await run_subprocess_with_logs(block_id, cmd)
        if rc != 0:
            await broadcast_log(block_id, "  ⚠ Silence removal failed, using joined file")
            silence_removed = joined_file

        # STEP 4: Final render — 16:9 HD export (face reframing done separately)
        final_file = PROCESSED_DIR / f"{safe_hook}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(silence_removed),
            "-c:v", FFMPEG_VIDEO_CODEC,
            "-preset", FFMPEG_VIDEO_PRESET,
            "-crf", FFMPEG_VIDEO_CRF,
            "-profile:v", FFMPEG_VIDEO_PROFILE,
            "-level", FFMPEG_VIDEO_LEVEL,
            "-pix_fmt", FFMPEG_PIXEL_FORMAT,
            "-c:a", FFMPEG_AUDIO_CODEC,
            "-b:a", FFMPEG_AUDIO_BITRATE,
            "-ar", FFMPEG_AUDIO_SAMPLE_RATE,
            "-movflags", "+faststart",
            str(final_file),
        ]
        await broadcast_log(block_id, "  🎬 Final render (16:9 HD)...")
        rc = await run_subprocess_with_logs(block_id, cmd)
        if rc != 0:
            await broadcast_log(block_id, "  ❌ Final render failed")
            continue

        await broadcast_log(block_id, f"  ✅ Clip saved: {safe_hook}.mp4")
        results.append({"clip_number": clip_num, "filename": f"{safe_hook}.mp4"})

    # Clean up temp files
    for f in TEMP_DIR.glob("*"):
        f.unlink(missing_ok=True)

    await broadcast_log(block_id, f"\n🎉 Processed {len(results)}/{len(clips)} clips successfully")
    return {"status": "ok", "clips": results}


# ── BLOCK 3: REFRAME (1:1 Face Track) ────────────────────────
@app.post("/api/reframe")
async def reframe_clip(req: ReframeRequest):
    """Apply 1:1 face-centered reframing to a processed clip."""
    block_id = "clipprocessor"
    clip_path = PROCESSED_DIR / req.clip_filename

    if not clip_path.exists():
        raise HTTPException(404, f"Clip not found: {req.clip_filename}")

    await broadcast_log(block_id, f"🎯 Reframing: {req.clip_filename}")

    # Import face tracker and run
    from backend.face_tracker import generate_crop_filter, apply_crop_filter

    await broadcast_log(block_id, "  👁 Detecting faces (sampling 2fps)...")

    loop = asyncio.get_event_loop()
    filter_path = await loop.run_in_executor(
        None, generate_crop_filter, str(clip_path), str(TEMP_DIR)
    )

    if not filter_path:
        await broadcast_log(block_id, "  ⚠ No faces detected — using center crop")
        # Fallback: center crop to 1:1
        output_path = PROCESSED_DIR / f"reframed_{req.clip_filename}"
        cmd = [
            "ffmpeg", "-y", "-i", str(clip_path),
            "-vf", "crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2",
            "-c:v", FFMPEG_VIDEO_CODEC, "-preset", FFMPEG_VIDEO_PRESET,
            "-crf", FFMPEG_VIDEO_CRF, "-pix_fmt", FFMPEG_PIXEL_FORMAT,
            "-c:a", FFMPEG_AUDIO_CODEC, "-b:a", FFMPEG_AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(output_path),
        ]
        rc = await run_subprocess_with_logs(block_id, cmd)
    else:
        await broadcast_log(block_id, "  🎯 Applying dynamic face crop...")
        output_path = PROCESSED_DIR / f"reframed_{req.clip_filename}"
        rc = await loop.run_in_executor(
            None, apply_crop_filter, str(clip_path), filter_path, str(output_path)
        )

    # Replace original with reframed version
    if output_path.exists():
        clip_path.unlink()
        output_path.rename(clip_path)
        await broadcast_log(block_id, f"  ✅ Reframed: {req.clip_filename}")
    else:
        await broadcast_log(block_id, f"  ❌ Reframe failed for {req.clip_filename}")

    return {"status": "ok", "filename": req.clip_filename}


@app.post("/api/reframe-all")
async def reframe_all_clips():
    """Reframe all processed clips to 1:1 with face tracking."""
    block_id = "clipprocessor"
    clips = list(PROCESSED_DIR.glob("*.mp4"))
    # Skip already reframed files
    clips = [c for c in clips if not c.name.startswith("reframed_")]

    if not clips:
        raise HTTPException(404, "No clips to reframe")

    await broadcast_log(block_id, f"🎯 Reframing {len(clips)} clips...")
    results = []
    for clip in clips:
        r = ReframeRequest(clip_filename=clip.name)
        result = await reframe_clip(r)
        results.append(result)

    return {"status": "ok", "reframed": len(results)}


# ── BLOCK 4: GALLERY ─────────────────────────────────────────
@app.get("/api/clips")
async def list_clips():
    """List all processed clips for the gallery preview."""
    clips = []
    for f in sorted(PROCESSED_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True):
        clips.append({
            "filename": f.name,
            "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
            "url": f"/api/clips/serve/{f.name}",
        })
    return {"clips": clips}


@app.get("/api/clips/serve/{filename}")
async def serve_clip(filename: str):
    """Serve a processed clip file for preview/download."""
    path = PROCESSED_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)


# ── GLOBAL: CACHE CLEAR ──────────────────────────────────────
@app.post("/api/clear-all")
async def clear_all_data():
    """Delete all downloaded, processed, and temp files."""
    global current_video, current_transcript
    count = 0
    for directory in [DOWNLOADS_DIR, PROCESSED_DIR, TRANSCRIPTS_DIR, TEMP_DIR]:
        for f in directory.glob("*"):
            if f.is_file():
                f.unlink()
                count += 1
            elif f.is_dir():
                shutil.rmtree(f)
                count += 1
    current_video = None
    current_transcript = None
    return {"status": "ok", "deleted": count}


# ── STATUS ────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status():
    """Return current pipeline state."""
    return {
        "current_video": current_video,
        "current_transcript": current_transcript,
        "active_processes": list(active_processes.keys()),
        "processed_clips": [f.name for f in PROCESSED_DIR.glob("*.mp4")],
        "gdrive_authenticated": GOOGLE_TOKEN_FILE.exists(),
    }


# ── FRONTEND ──────────────────────────────────────────────────
@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/index.html")
