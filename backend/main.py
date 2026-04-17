"""
main.py - FastAPI backend for DailyPodClips.
SSE log streaming, subprocess management, persistent settings.
"""
import asyncio
import json
import os
import queue as thread_queue
import re
import shutil
import subprocess
import threading
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
    DATA_DIR, DOWNLOADS_DIR, PROCESSED_DIR, TRANSCRIPTS_DIR, TEMP_DIR,
    AUTH_DIR, COOKIES_DIR, GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE,
    GOOGLE_SCOPES, WHISPER_MODEL, WHISPER_COMPUTE_TYPE, WHISPER_BEAM_SIZE,
    WHISPER_DEVICE, FFMPEG_VIDEO_CODEC, FFMPEG_VIDEO_PRESET, FFMPEG_VIDEO_CRF,
    FFMPEG_VIDEO_PROFILE, FFMPEG_VIDEO_LEVEL, FFMPEG_PIXEL_FORMAT,
    FFMPEG_AUDIO_CODEC, FFMPEG_AUDIO_BITRATE, FFMPEG_AUDIO_SAMPLE_RATE,
    FACE_TRACK_FPS, FACE_TRACK_SMOOTHING, FACE_TRACK_MIN_CONFIDENCE,
)

# -- Persistent settings --
SETTINGS_FILE = DATA_DIR / "settings.json"

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))

# -- App Setup --
app = FastAPI(title="DailyPodClips", version="1.0.0")
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# -- Global State --
active_processes: Dict[str, subprocess.Popen] = {}
log_subscribers: Dict[str, list] = {}
current_video: Optional[str] = None
current_transcript: Optional[str] = None

# -- Pydantic Models --
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

class SettingsUpdate(BaseModel):
    cookies: Optional[str] = None
    gdrive_folder_transcripts: Optional[str] = None
    gdrive_folder_clips: Optional[str] = None

# -- SSE Helpers --
async def broadcast_log(block_id: str, message: str):
    if block_id not in log_subscribers:
        log_subscribers[block_id] = []
    for queue in log_subscribers[block_id]:
        await queue.put(message)

async def run_subprocess_with_logs(block_id: str, cmd: list, cwd: str = None, env: dict = None) -> int:
    await broadcast_log(block_id, f">> {' '.join(cmd[:6])}...")
    merged_env = {**os.environ, **(env or {})}
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=merged_env,
    )
    active_processes[block_id] = process

    async def stream_pipe(pipe, prefix=""):
        async for line in pipe:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                await broadcast_log(block_id, f"{prefix}{text}")

    await asyncio.gather(
        stream_pipe(process.stdout),
        stream_pipe(process.stderr, "[stderr] "),
    )
    await process.wait()
    active_processes.pop(block_id, None)
    status = "DONE" if process.returncode == 0 else f"FAILED (code {process.returncode})"
    await broadcast_log(block_id, status)
    return process.returncode

# -- SSE Endpoint --
@app.get("/api/logs/{block_id}")
async def stream_logs(block_id: str, request: Request):
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

# -- SETTINGS --
@app.get("/api/settings")
async def get_settings():
    return load_settings()

@app.post("/api/settings")
async def update_settings(req: SettingsUpdate):
    settings = load_settings()
    if req.cookies is not None:
        settings["cookies"] = req.cookies
        (COOKIES_DIR / "cookies.txt").write_text(req.cookies)
    if req.gdrive_folder_transcripts is not None:
        settings["gdrive_folder_transcripts"] = req.gdrive_folder_transcripts
    if req.gdrive_folder_clips is not None:
        settings["gdrive_folder_clips"] = req.gdrive_folder_clips
    save_settings(settings)
    return {"status": "ok", "settings": settings}

# -- BLOCK 1: DOWNLOADER --
@app.post("/api/download")
async def download_video(req: DownloadRequest):
    global current_video
    block_id = "downloader"
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL is required")

    await broadcast_log(block_id, f"Starting download: {url}")

    # Use saved cookies or from request
    settings = load_settings()
    cookie_path = str(COOKIES_DIR / "cookies.txt")
    if req.cookies and req.cookies.strip():
        async with aiofiles.open(cookie_path, "w") as f:
            await f.write(req.cookies)
        settings["cookies"] = req.cookies
        save_settings(settings)
        await broadcast_log(block_id, "Cookies saved (persistent)")
    elif (COOKIES_DIR / "cookies.txt").exists():
        await broadcast_log(block_id, "Using saved cookies")
    else:
        cookie_path = None

    is_direct_link = url.lower().endswith(('.mp4', '.mkv', '.webm', '.mov'))

    if is_direct_link:
        filename = url.split("/")[-1].split("?")[0]
        cmd = ["aria2c", "-x", "16", "-s", "16", "--max-connection-per-server=16",
               "-d", str(DOWNLOADS_DIR), "-o", filename, url]
    else:
        cmd = ["yt-dlp",
               "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv+ba/b",
               "--merge-output-format", "mp4", "--remux-video", "mp4",
               "-o", str(DOWNLOADS_DIR / "%(title)s.%(ext)s"),
               "--no-playlist", "--progress", "--newline",
               "--retries", "3", "--fragment-retries", "3"]
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(url)

    returncode = await run_subprocess_with_logs(block_id, cmd)
    if returncode != 0:
        raise HTTPException(500, "Download failed")

    mp4_files = sorted(DOWNLOADS_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True)
    if not mp4_files:
        raise HTTPException(500, "No MP4 file found after download")

    current_video = mp4_files[0].name
    await broadcast_log(block_id, f"Saved: {current_video}")
    return {"status": "ok", "filename": current_video}

@app.post("/api/stop/{block_id}")
async def stop_process(block_id: str):
    process = active_processes.get(block_id)
    if process and process.returncode is None:
        try:
            process.kill()
            await broadcast_log(block_id, "Process killed by user")
        except Exception as e:
            await broadcast_log(block_id, f"Kill error: {e}")
        active_processes.pop(block_id, None)
        return {"status": "stopped"}
    return {"status": "no_active_process"}

# -- BLOCK 2: TRANSCRIBER (with per-segment progress) --
@app.post("/api/transcribe")
async def transcribe_video():
    global current_transcript
    block_id = "transcriber"

    if not current_video:
        raise HTTPException(400, "No video downloaded yet")
    video_path = DOWNLOADS_DIR / current_video
    if not video_path.exists():
        raise HTTPException(404, f"Video not found: {current_video}")

    await broadcast_log(block_id, f"Transcribing: {current_video}")
    await broadcast_log(block_id, f"Model: {WHISPER_MODEL} | Compute: {WHISPER_COMPUTE_TYPE}")
    await broadcast_log(block_id, "Loading model...")

    progress_q = thread_queue.Queue()

    def _transcribe_with_progress():
        try:
            from faster_whisper import WhisperModel
            progress_q.put(("log", "Model loaded, starting transcription..."))
            model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
            segments_gen, info = model.transcribe(str(video_path), beam_size=WHISPER_BEAM_SIZE, word_timestamps=False)
            progress_q.put(("info", info))
            all_segments = []
            for seg in segments_gen:
                all_segments.append(seg)
                pct = min(100, int((seg.end / max(info.duration, 1)) * 100))
                progress_q.put(("segment", seg, pct, len(all_segments)))
            progress_q.put(("done", all_segments))
        except Exception as e:
            progress_q.put(("error", str(e)))

    whisper_thread = threading.Thread(target=_transcribe_with_progress, daemon=True)
    whisper_thread.start()

    transcript_lines = []
    all_segments = []

    while True:
        try:
            msg = await asyncio.get_event_loop().run_in_executor(
                None, lambda: progress_q.get(timeout=120)
            )
        except Exception:
            await broadcast_log(block_id, "Transcription timed out")
            raise HTTPException(500, "Transcription timed out")

        if msg[0] == "log":
            await broadcast_log(block_id, msg[1])
        elif msg[0] == "info":
            info = msg[1]
            await broadcast_log(block_id, f"Language: {info.language} ({info.language_probability:.2f})")
            await broadcast_log(block_id, f"Duration: {info.duration:.1f}s")
        elif msg[0] == "segment":
            seg, pct, count = msg[1], msg[2], msg[3]
            start_s, end_s = int(seg.start), int(seg.end)
            line = f"[{start_s:05d}s -> {end_s:05d}s] {seg.text.strip()}"
            transcript_lines.append(line)
            await broadcast_log(block_id, f"{pct}% | Seg {count}: {line[:90]}")
        elif msg[0] == "done":
            all_segments = msg[1]
            await broadcast_log(block_id, f"Transcription complete - {len(all_segments)} segments")
            break
        elif msg[0] == "error":
            await broadcast_log(block_id, f"ERROR: {msg[1]}")
            raise HTTPException(500, f"Transcription failed: {msg[1]}")

    transcript_text = "\n".join(transcript_lines)
    video_stem = Path(current_video).stem
    transcript_name = f"{video_stem}_transcription.txt"
    transcript_path = TRANSCRIPTS_DIR / transcript_name
    async with aiofiles.open(transcript_path, "w", encoding="utf-8") as f:
        await f.write(transcript_text)

    current_transcript = transcript_name
    await broadcast_log(block_id, f"Saved: {transcript_name}")
    return {"status": "ok", "filename": transcript_name, "transcript": transcript_text}

# -- BLOCK 2: GDRIVE AUTH --
@app.get("/api/gdrive/auth-url")
async def get_gdrive_auth_url():
    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise HTTPException(400, "Upload credentials.json to /app/data/auth/ first")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS_FILE), GOOGLE_SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return {"auth_url": auth_url}

@app.post("/api/gdrive/auth-code")
async def submit_gdrive_auth_code(req: AuthCodeRequest):
    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise HTTPException(400, "Upload credentials.json first")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS_FILE), GOOGLE_SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    flow.fetch_token(code=req.code)
    creds = flow.credentials
    async with aiofiles.open(str(GOOGLE_TOKEN_FILE), "w") as f:
        await f.write(creds.to_json())
    return {"status": "ok", "message": "Google Drive authenticated successfully"}

@app.post("/api/gdrive/upload")
async def upload_to_gdrive(req: GDriveFolderRequest):
    block_id = "transcriber"
    folder_id = req.folder_id.strip()
    if not GOOGLE_TOKEN_FILE.exists():
        raise HTTPException(400, "Not authenticated")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_FILE), GOOGLE_SCOPES)
    service = build("drive", "v3", credentials=creds)
    uploaded = []
    if current_transcript:
        t_path = TRANSCRIPTS_DIR / current_transcript
        if t_path.exists():
            await broadcast_log(block_id, f"Uploading: {current_transcript}")
            media = MediaFileUpload(str(t_path), resumable=True)
            file_meta = {"name": current_transcript, "parents": [folder_id]}
            result = service.files().create(body=file_meta, media_body=media, fields="id").execute()
            uploaded.append({"name": current_transcript, "id": result["id"]})
    for clip_path in PROCESSED_DIR.glob("*.mp4"):
        await broadcast_log(block_id, f"Uploading: {clip_path.name}")
        media = MediaFileUpload(str(clip_path), resumable=True)
        file_meta = {"name": clip_path.name, "parents": [folder_id]}
        result = service.files().create(body=file_meta, media_body=media, fields="id").execute()
        uploaded.append({"name": clip_path.name, "id": result["id"]})
    return {"status": "ok", "uploaded": uploaded}

# -- BLOCK 3: VALIDATE JSON --
@app.post("/api/validate-json")
async def validate_clip_json(req: ClipJSON):
    try:
        data = json.loads(req.json_data)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    if not isinstance(data, list):
        raise HTTPException(400, "JSON must be an array")
    for i, clip in enumerate(data):
        if "clip_number" not in clip:
            raise HTTPException(400, f"Clip {i}: missing clip_number")
        if "segments_to_keep" not in clip:
            raise HTTPException(400, f"Clip {i}: missing segments_to_keep")
        for j, seg in enumerate(clip["segments_to_keep"]):
            if "start_timestamp" not in seg or "end_timestamp" not in seg:
                raise HTTPException(400, f"Clip {i} seg {j}: missing timestamps")
    return {"status": "valid", "clip_count": len(data)}

# -- BLOCK 3: PROCESS CLIPS --
def parse_timestamp(ts: str) -> float:
    parts = ts.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])

@app.post("/api/process-clips")
async def process_clips(req: ClipJSON):
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

    await broadcast_log(block_id, f"Processing {len(clips)} clips from: {current_video}")
    results = []

    for clip in clips:
        clip_num = clip["clip_number"]
        segments = clip["segments_to_keep"]
        hook = clip.get("hook", f"clip_{clip_num}")
        safe_hook = re.sub(r'[^\w\s-]', '', hook)[:80].strip().replace(' ', '_')
        if not safe_hook:
            safe_hook = f"clip_{clip_num}"

        await broadcast_log(block_id, f"\n-- Clip {clip_num}: {safe_hook} --")
        segment_files = []
        for i, seg in enumerate(segments):
            start = parse_timestamp(seg["start_timestamp"])
            end = parse_timestamp(seg["end_timestamp"])
            duration = end - start
            seg_file = TEMP_DIR / f"clip{clip_num}_seg{i}.mp4"
            cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
                   "-t", str(duration), "-c", "copy", "-avoid_negative_ts", "make_zero", str(seg_file)]
            await broadcast_log(block_id, f"  Cut seg {i+1}: {seg['start_timestamp']} -> {seg['end_timestamp']}")
            rc = await run_subprocess_with_logs(block_id, cmd)
            if rc != 0:
                continue
            segment_files.append(seg_file)

        if not segment_files:
            await broadcast_log(block_id, f"  No segments for clip {clip_num}")
            continue

        concat_file = TEMP_DIR / f"clip{clip_num}_concat.txt"
        async with aiofiles.open(concat_file, "w") as f:
            for sf in segment_files:
                await f.write(f"file '{sf}'\n")

        joined_file = TEMP_DIR / f"clip{clip_num}_joined.mp4"
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined_file)]
        await broadcast_log(block_id, "  Joining segments...")
        rc = await run_subprocess_with_logs(block_id, cmd)
        if rc != 0:
            continue

        silence_removed = TEMP_DIR / f"clip{clip_num}_nosilence.mp4"
        cmd = ["auto-editor", str(joined_file), "--margin", "0.15sec", "--output", str(silence_removed), "--no-open"]
        await broadcast_log(block_id, "  Removing silence...")
        rc = await run_subprocess_with_logs(block_id, cmd)
        if rc != 0:
            await broadcast_log(block_id, "  Silence removal failed, using joined file")
            silence_removed = joined_file

        final_file = PROCESSED_DIR / f"{safe_hook}.mp4"
        cmd = ["ffmpeg", "-y", "-i", str(silence_removed),
               "-c:v", FFMPEG_VIDEO_CODEC, "-preset", FFMPEG_VIDEO_PRESET,
               "-crf", FFMPEG_VIDEO_CRF, "-profile:v", FFMPEG_VIDEO_PROFILE,
               "-level", FFMPEG_VIDEO_LEVEL, "-pix_fmt", FFMPEG_PIXEL_FORMAT,
               "-c:a", FFMPEG_AUDIO_CODEC, "-b:a", FFMPEG_AUDIO_BITRATE,
               "-ar", FFMPEG_AUDIO_SAMPLE_RATE, "-movflags", "+faststart", str(final_file)]
        await broadcast_log(block_id, "  Final render (16:9 HD)...")
        rc = await run_subprocess_with_logs(block_id, cmd)
        if rc != 0:
            continue
        await broadcast_log(block_id, f"  Clip saved: {safe_hook}.mp4")
        results.append({"clip_number": clip_num, "filename": f"{safe_hook}.mp4"})

    for f in TEMP_DIR.glob("*"):
        f.unlink(missing_ok=True)
    await broadcast_log(block_id, f"\nProcessed {len(results)}/{len(clips)} clips")
    return {"status": "ok", "clips": results}

# -- BLOCK 3: REFRAME --
@app.post("/api/reframe")
async def reframe_clip(req: ReframeRequest):
    block_id = "clipprocessor"
    clip_path = PROCESSED_DIR / req.clip_filename
    if not clip_path.exists():
        raise HTTPException(404, f"Clip not found: {req.clip_filename}")

    await broadcast_log(block_id, f"Reframing: {req.clip_filename}")
    from backend.face_tracker import generate_crop_filter, apply_crop_filter

    await broadcast_log(block_id, "  Detecting faces (2fps)...")
    loop = asyncio.get_event_loop()
    filter_path = await loop.run_in_executor(None, generate_crop_filter, str(clip_path), str(TEMP_DIR))

    output_path = PROCESSED_DIR / f"reframed_{req.clip_filename}"
    if not filter_path:
        await broadcast_log(block_id, "  No faces detected, center crop")
        cmd = ["ffmpeg", "-y", "-i", str(clip_path),
               "-vf", "crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2",
               "-c:v", FFMPEG_VIDEO_CODEC, "-preset", FFMPEG_VIDEO_PRESET,
               "-crf", FFMPEG_VIDEO_CRF, "-pix_fmt", FFMPEG_PIXEL_FORMAT,
               "-c:a", FFMPEG_AUDIO_CODEC, "-b:a", FFMPEG_AUDIO_BITRATE,
               "-movflags", "+faststart", str(output_path)]
        await run_subprocess_with_logs(block_id, cmd)
    else:
        await broadcast_log(block_id, "  Applying dynamic face crop...")
        await loop.run_in_executor(None, apply_crop_filter, str(clip_path), filter_path, str(output_path))

    if output_path.exists():
        clip_path.unlink()
        output_path.rename(clip_path)
        await broadcast_log(block_id, f"  Reframed: {req.clip_filename}")
    else:
        await broadcast_log(block_id, f"  Reframe failed for {req.clip_filename}")
    return {"status": "ok", "filename": req.clip_filename}

@app.post("/api/reframe-all")
async def reframe_all_clips():
    block_id = "clipprocessor"
    clips = [c for c in PROCESSED_DIR.glob("*.mp4") if not c.name.startswith("reframed_")]
    if not clips:
        raise HTTPException(404, "No clips to reframe")
    await broadcast_log(block_id, f"Reframing {len(clips)} clips...")
    results = []
    for clip in clips:
        result = await reframe_clip(ReframeRequest(clip_filename=clip.name))
        results.append(result)
    return {"status": "ok", "reframed": len(results)}

# -- BLOCK 4: GALLERY --
@app.get("/api/clips")
async def list_clips():
    clips = []
    for f in sorted(PROCESSED_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True):
        clips.append({"filename": f.name, "size_mb": round(f.stat().st_size / (1024*1024), 2),
                       "url": f"/api/clips/serve/{f.name}"})
    return {"clips": clips}

@app.get("/api/clips/serve/{filename}")
async def serve_clip(filename: str):
    path = PROCESSED_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)

# -- GLOBAL: CACHE CLEAR --
@app.post("/api/clear-all")
async def clear_all_data():
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

# -- STATUS --
@app.get("/api/status")
async def get_status():
    return {
        "current_video": current_video,
        "current_transcript": current_transcript,
        "active_processes": list(active_processes.keys()),
        "processed_clips": [f.name for f in PROCESSED_DIR.glob("*.mp4")],
        "gdrive_authenticated": GOOGLE_TOKEN_FILE.exists(),
    }

# -- FRONTEND --
@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/index.html")
