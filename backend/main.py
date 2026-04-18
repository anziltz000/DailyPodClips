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

import psutil
import aiofiles
from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel

from backend.config import (
    DATA_DIR, AUTH_DIR, COOKIES_DIR, GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE,
    GOOGLE_SCOPES, WHISPER_MODEL, WHISPER_COMPUTE_TYPE, WHISPER_BEAM_SIZE,
    WHISPER_DEVICE, FFMPEG_VIDEO_CODEC, FFMPEG_VIDEO_PRESET, FFMPEG_VIDEO_CRF,
    FFMPEG_VIDEO_PROFILE, FFMPEG_VIDEO_LEVEL, FFMPEG_PIXEL_FORMAT,
    FFMPEG_AUDIO_CODEC, FFMPEG_AUDIO_BITRATE, FFMPEG_AUDIO_SAMPLE_RATE,
    FACE_TRACK_FPS, FACE_TRACK_SMOOTHING, FACE_TRACK_MIN_CONFIDENCE,
)

from backend.projects import (
    init_projects, load_projects, create_project, rename_project,
    delete_project, cleanup_old_projects, get_project_dir
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
app = FastAPI(title="DailyPodClips", version="1.1.0")
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# -- Global State --
init_projects()
# project_id -> {"active_processes": {}, "log_subscribers": {}, "current_video": None, "current_transcript": None}
project_state = {}

def get_pstate(pid: str):
    if not pid:
        pid = "default"
    if pid not in project_state:
        project_state[pid] = {
            "active_processes": {},
            "log_subscribers": {},
            "current_video": None,
            "current_transcript": None,
        }
    return project_state[pid]

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

class ProjectCreate(BaseModel):
    name: str

class ProjectRename(BaseModel):
    name: str

# Dependency to extract project ID
def get_project_id(x_project_id: Optional[str] = Header(None)):
    if not x_project_id:
        return "default"
    return x_project_id

# -- SSE Helpers --
async def broadcast_log(project_id: str, block_id: str, message: str):
    state = get_pstate(project_id)
    if block_id not in state["log_subscribers"]:
        state["log_subscribers"][block_id] = []
    for queue in state["log_subscribers"][block_id]:
        await queue.put(message)
    
    # Save log to file
    log_dir = get_project_dir(project_id, "logs")
    async with aiofiles.open(log_dir / f"{block_id}.log", "a", encoding="utf-8") as f:
        await f.write(message + "\n")

async def run_subprocess_with_logs(project_id: str, block_id: str, cmd: list, cwd: str = None, env: dict = None) -> int:
    await broadcast_log(project_id, block_id, f">> {' '.join(cmd[:6])}...")
    merged_env = {**os.environ, **(env or {})}
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=merged_env,
    )
    get_pstate(project_id)["active_processes"][block_id] = process

    async def stream_pipe(pipe, prefix=""):
        async for line in pipe:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                await broadcast_log(project_id, block_id, f"{prefix}{text}")

    try:
        await asyncio.gather(
            stream_pipe(process.stdout),
            stream_pipe(process.stderr, "[stderr] "),
        )
        await process.wait()
    finally:
        get_pstate(project_id)["active_processes"].pop(block_id, None)
        status = "DONE" if process.returncode == 0 else f"FAILED (code {process.returncode})"
        await broadcast_log(project_id, block_id, status)
    return process.returncode

# -- PROJECTS API --
@app.get("/api/projects")
async def api_list_projects():
    cleanup_old_projects(15)
    return {"projects": load_projects()}

@app.post("/api/projects")
async def api_create_project(req: ProjectCreate):
    return create_project(req.name)

@app.put("/api/projects/{pid}")
async def api_rename_project(pid: str, req: ProjectRename):
    rename_project(pid, req.name)
    return {"status": "ok"}

@app.delete("/api/projects/{pid}")
async def api_delete_project(pid: str):
    delete_project(pid)
    # Stop active processes
    state = get_pstate(pid)
    for block_id, proc in state["active_processes"].items():
        if proc.returncode is None:
            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
            except Exception:
                pass
    project_state.pop(pid, None)
    return {"status": "ok"}

# -- SSE Endpoint --
@app.get("/api/logs/{project_id}/{block_id}")
async def stream_logs(project_id: str, block_id: str, request: Request):
    queue = asyncio.Queue()
    state = get_pstate(project_id)
    if block_id not in state["log_subscribers"]:
        state["log_subscribers"][block_id] = []
    state["log_subscribers"][block_id].append(queue)

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
            if queue in state["log_subscribers"].get(block_id, []):
                state["log_subscribers"][block_id].remove(queue)

    return EventSourceResponse(event_generator())

@app.get("/api/logs/{project_id}/{block_id}/history")
async def get_log_history(project_id: str, block_id: str):
    log_dir = get_project_dir(project_id, "logs")
    log_file = log_dir / f"{block_id}.log"
    if log_file.exists():
        return {"logs": log_file.read_text(encoding="utf-8", errors="replace")}
    return {"logs": ""}

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
async def download_video(req: DownloadRequest, project_id: str = Depends(get_project_id)):
    block_id = "downloader"
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL is required")

    downloads_dir = get_project_dir(project_id, "downloads")
    await broadcast_log(project_id, block_id, f"Starting download: {url}")

    settings = load_settings()
    cookie_path = str(COOKIES_DIR / "cookies.txt")
    if req.cookies and req.cookies.strip():
        async with aiofiles.open(cookie_path, "w") as f:
            await f.write(req.cookies)
        settings["cookies"] = req.cookies
        save_settings(settings)
        await broadcast_log(project_id, block_id, "Cookies saved")
    elif (COOKIES_DIR / "cookies.txt").exists():
        await broadcast_log(project_id, block_id, "Using saved cookies")
    else:
        cookie_path = None

    is_direct_link = url.lower().endswith(('.mp4', '.mkv', '.webm', '.mov'))

    if is_direct_link:
        filename = url.split("/")[-1].split("?")[0]
        cmd = ["wget", "-c", "-O", str(downloads_dir / filename), url]
    else:
        cmd = ["yt-dlp",
               "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv+ba/b",
               "--merge-output-format", "mp4", "--remux-video", "mp4",
               "-o", str(downloads_dir / "%(title)s.%(ext)s"),
               "--no-playlist", "--progress", "--newline",
               "--retries", "3", "--fragment-retries", "3"]
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(url)

    returncode = await run_subprocess_with_logs(project_id, block_id, cmd)
    if returncode != 0:
        raise HTTPException(500, "Download failed")

    mp4_files = sorted(downloads_dir.glob("*.mp4"), key=os.path.getmtime, reverse=True)
    if not mp4_files:
        raise HTTPException(500, "No MP4 file found after download")

    state = get_pstate(project_id)
    state["current_video"] = mp4_files[0].name
    await broadcast_log(project_id, block_id, f"Saved: {state['current_video']}")
    return {"status": "ok", "filename": state["current_video"]}

@app.post("/api/stop/{block_id}")
async def stop_process(block_id: str, project_id: str = Depends(get_project_id)):
    state = get_pstate(project_id)
    process = state["active_processes"].get(block_id)
    if process and process.returncode is None:
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
            await broadcast_log(project_id, block_id, "Process killed by user")
        except Exception as e:
            await broadcast_log(project_id, block_id, f"Kill error: {e}")
        state["active_processes"].pop(block_id, None)
        return {"status": "stopped"}
    return {"status": "no_active_process"}

# -- BLOCK 2: TRANSCRIBER --
@app.post("/api/transcribe")
async def transcribe_video(project_id: str = Depends(get_project_id)):
    block_id = "transcriber"
    state = get_pstate(project_id)
    downloads_dir = get_project_dir(project_id, "downloads")
    transcripts_dir = get_project_dir(project_id, "transcripts")

    if not state["current_video"]:
        raise HTTPException(400, "No video downloaded yet")
    video_path = downloads_dir / state["current_video"]
    if not video_path.exists():
        raise HTTPException(404, f"Video not found: {state['current_video']}")

    await broadcast_log(project_id, block_id, f"Transcribing: {state['current_video']}")
    await broadcast_log(project_id, block_id, "Loading model...")

    mock_proc = type("MockProc", (), {"returncode": None, "pid": 0})()
    state["active_processes"][block_id] = mock_proc
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
        finally:
            # Tell the main loop the thread is fully dead
            progress_q.put(("thread_exit", None))

    whisper_thread = threading.Thread(target=_transcribe_with_progress, daemon=True)
    whisper_thread.start()

    transcript_lines = []
    all_segments = []

    try:
        while True:
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: progress_q.get(timeout=120)
                )
            except Exception:
                await broadcast_log(project_id, block_id, "Transcription timed out")
                raise HTTPException(500, "Transcription timed out")

            if msg[0] == "log":
                await broadcast_log(project_id, block_id, msg[1])
            elif msg[0] == "info":
                info = msg[1]
                await broadcast_log(project_id, block_id, f"Duration: {info.duration:.1f}s")
            elif msg[0] == "segment":
                seg, pct, count = msg[1], msg[2], msg[3]
                start_s, end_s = int(seg.start), int(seg.end)
                line = f"[{start_s:05d}s -> {end_s:05d}s] {seg.text.strip()}"
                transcript_lines.append(line)
                await broadcast_log(project_id, block_id, f"{pct}% | Seg {count}: {line[:90]}")
            elif msg[0] == "done":
                all_segments = msg[1]
                await broadcast_log(project_id, block_id, f"Transcription complete")
            elif msg[0] == "error":
                await broadcast_log(project_id, block_id, f"ERROR: {msg[1]}")
                raise HTTPException(500, f"Transcription failed: {msg[1]}")
            elif msg[0] == "thread_exit":
                break

        transcript_text = "\n".join(transcript_lines)
        video_stem = Path(state["current_video"]).stem
        transcript_name = f"{video_stem}_transcription.txt"
        transcript_path = transcripts_dir / transcript_name
        async with aiofiles.open(transcript_path, "w", encoding="utf-8") as f:
            await f.write(transcript_text)

        state["current_transcript"] = transcript_name
        await broadcast_log(project_id, block_id, f"Saved: {transcript_name}")
        return {"status": "ok", "filename": transcript_name, "transcript": transcript_text}
    finally:
        state["active_processes"].pop(block_id, None)

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
    return {"status": "ok", "message": "Google Drive authenticated"}

@app.post("/api/gdrive/upload")
async def upload_to_gdrive(req: GDriveFolderRequest, project_id: str = Depends(get_project_id)):
    block_id = "transcriber"
    folder_id = req.folder_id.strip()
    if not GOOGLE_TOKEN_FILE.exists():
        raise HTTPException(400, "Not authenticated")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_FILE), GOOGLE_SCOPES)
    service = build("drive", "v3", credentials=creds)
    
    state = get_pstate(project_id)
    transcripts_dir = get_project_dir(project_id, "transcripts")
    processed_dir = get_project_dir(project_id, "processed")
    uploaded = []
    
    if state["current_transcript"]:
        t_path = transcripts_dir / state["current_transcript"]
        if t_path.exists():
            await broadcast_log(project_id, block_id, f"Uploading: {state['current_transcript']}")
            media = MediaFileUpload(str(t_path), resumable=True)
            file_meta = {"name": state["current_transcript"], "parents": [folder_id]}
            result = service.files().create(body=file_meta, media_body=media, fields="id").execute()
            uploaded.append({"name": state["current_transcript"], "id": result["id"]})
            
    for clip_path in processed_dir.glob("*.mp4"):
        await broadcast_log(project_id, block_id, f"Uploading: {clip_path.name}")
        media = MediaFileUpload(str(clip_path), resumable=True)
        file_meta = {"name": clip_path.name, "parents": [folder_id]}
        result = service.files().create(body=file_meta, media_body=media, fields="id").execute()
        uploaded.append({"name": clip_path.name, "id": result["id"]})
    return {"status": "ok", "uploaded": uploaded}

# -- BLOCK 3: PROCESS CLIPS --
def parse_timestamp(ts: str) -> float:
    parts = ts.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])

@app.post("/api/process-clips")
async def process_clips(req: ClipJSON, project_id: str = Depends(get_project_id)):
    block_id = "clipprocessor"
    state = get_pstate(project_id)
    downloads_dir = get_project_dir(project_id, "downloads")
    temp_dir = get_project_dir(project_id, "temp")
    processed_dir = get_project_dir(project_id, "processed")

    if not state["current_video"]:
        raise HTTPException(400, "No video downloaded yet")
    video_path = downloads_dir / state["current_video"]
    if not video_path.exists():
        raise HTTPException(404, f"Video not found: {state['current_video']}")
    try:
        clips = json.loads(req.json_data)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")

    await broadcast_log(project_id, block_id, f"Processing {len(clips)} clips")
    results = []

    for clip in clips:
        clip_num = clip.get("clip_number", 1)
        segments = clip.get("segments_to_keep", [])
        hook = clip.get("hook", f"clip_{clip_num}")
        safe_hook = re.sub(r'[^\w\s-]', '', hook)[:80].strip().replace(' ', '_')
        if not safe_hook:
            safe_hook = f"clip_{clip_num}"

        await broadcast_log(project_id, block_id, f"\n-- Clip {clip_num}: {safe_hook} --")
        segment_files = []
        for i, seg in enumerate(segments):
            start = parse_timestamp(seg["start_timestamp"])
            end = parse_timestamp(seg["end_timestamp"])
            duration = end - start
            seg_file = temp_dir / f"clip{clip_num}_seg{i}.mp4"
            cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
                   "-t", str(duration), "-c", "copy", "-avoid_negative_ts", "make_zero", str(seg_file)]
            await broadcast_log(project_id, block_id, f"  Cut seg {i+1}")
            rc = await run_subprocess_with_logs(project_id, block_id, cmd)
            if rc != 0: continue
            segment_files.append(seg_file)

        if not segment_files:
            continue

        concat_file = temp_dir / f"clip{clip_num}_concat.txt"
        async with aiofiles.open(concat_file, "w") as f:
            for sf in segment_files:
                await f.write(f"file '{sf}'\n")

        joined_file = temp_dir / f"clip{clip_num}_joined.mp4"
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined_file)]
        rc = await run_subprocess_with_logs(project_id, block_id, cmd)
        if rc != 0: continue

        silence_removed = temp_dir / f"clip{clip_num}_nosilence.mp4"
        cmd = ["python3", "-m", "auto_editor", str(joined_file), "--margin", "0.15sec", "--output", str(silence_removed), "--no-open"]
        rc = await run_subprocess_with_logs(project_id, block_id, cmd)
        if rc != 0:
            silence_removed = joined_file

        from backend.face_tracker import generate_crop_filter
        await broadcast_log(project_id, block_id, "  Detecting faces for 1:1 crop...")
        loop = asyncio.get_event_loop()
        filter_path = await loop.run_in_executor(None, generate_crop_filter, str(silence_removed), str(temp_dir))

        final_file = processed_dir / f"{safe_hook}.mp4"
        
        if not filter_path:
            await broadcast_log(project_id, block_id, "  Center crop (1:1)")
            cmd = ["ffmpeg", "-y", "-i", str(silence_removed),
                   "-vf", "crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2,scale=1080:1080:flags=lanczos",
                   "-c:v", FFMPEG_VIDEO_CODEC, "-preset", FFMPEG_VIDEO_PRESET,
                   "-crf", FFMPEG_VIDEO_CRF, "-pix_fmt", FFMPEG_PIXEL_FORMAT,
                   "-c:a", FFMPEG_AUDIO_CODEC, "-b:a", FFMPEG_AUDIO_BITRATE,
                   "-movflags", "+faststart", str(final_file)]
        else:
            await broadcast_log(project_id, block_id, "  Dynamic face crop (1:1)")
            from backend.face_tracker import get_video_info
            info = get_video_info(str(silence_removed))
            crop_size = min(info["width"], info["height"])
            cx, cy = (info["width"] - crop_size) // 2, (info["height"] - crop_size) // 2
            fc = f"sendcmd=f='{filter_path}',crop={crop_size}:{crop_size}:{cx}:{cy},scale=1080:1080:flags=lanczos"
            cmd = ["ffmpeg", "-y", "-i", str(silence_removed), "-vf", fc,
                   "-c:v", FFMPEG_VIDEO_CODEC, "-preset", FFMPEG_VIDEO_PRESET,
                   "-crf", FFMPEG_VIDEO_CRF, "-pix_fmt", FFMPEG_PIXEL_FORMAT,
                   "-c:a", FFMPEG_AUDIO_CODEC, "-b:a", FFMPEG_AUDIO_BITRATE,
                   "-movflags", "+faststart", str(final_file)]
            
        rc = await run_subprocess_with_logs(project_id, block_id, cmd)
        if rc != 0:
            await broadcast_log(project_id, block_id, "  ❌ Final render failed")
            continue
            
        meta_file = processed_dir / f"{safe_hook}.json"
        async with aiofiles.open(meta_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(clip, indent=2))
            
        await broadcast_log(project_id, block_id, f"  ✅ Saved: {safe_hook}.mp4")
        results.append({"clip_number": clip_num, "filename": f"{safe_hook}.mp4"})

    for f in temp_dir.glob("*"):
        f.unlink(missing_ok=True)
    return {"status": "ok", "clips": results}

# -- BLOCK 4: GALLERY --
@app.get("/api/clips")
async def list_clips(project_id: str = Depends(get_project_id)):
    clips = []
    processed_dir = get_project_dir(project_id, "processed")
    for f in sorted(processed_dir.glob("*.mp4"), key=os.path.getmtime, reverse=True):
        meta_path = f.with_suffix(".json")
        metadata = {}
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        clips.append({
            "filename": f.name, 
            "size_mb": round(f.stat().st_size / (1024*1024), 2),
            "url": f"/api/clips/serve/{f.name}?project_id={project_id}",
            "metadata": metadata
        })
    return {"clips": clips}

@app.get("/api/clips/serve/{filename}")
async def serve_clip(filename: str, project_id: str = "default"):
    path = get_project_dir(project_id, "processed") / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)

# -- GLOBAL: CACHE CLEAR --
@app.post("/api/clear-all")
async def clear_all_data(project_id: str = Depends(get_project_id)):
    state = get_pstate(project_id)
    count = 0
    for sub in ["downloads", "processed", "transcripts", "temp"]:
        directory = get_project_dir(project_id, sub)
        for f in directory.glob("*"):
            if f.is_file():
                f.unlink()
                count += 1
            elif f.is_dir():
                shutil.rmtree(f)
                count += 1
    state["current_video"] = None
    state["current_transcript"] = None
    return {"status": "ok", "deleted": count}

# -- STATUS --
@app.get("/api/status")
async def get_status(project_id: str = Depends(get_project_id)):
    state = get_pstate(project_id)
    processed_dir = get_project_dir(project_id, "processed")
    # if memory state was reset but files exist
    if not state["current_video"]:
        vids = list(get_project_dir(project_id, "downloads").glob("*.mp4"))
        if vids: state["current_video"] = vids[0].name
    if not state["current_transcript"]:
        trans = list(get_project_dir(project_id, "transcripts").glob("*.txt"))
        if trans: state["current_transcript"] = trans[0].name
        
    return {
        "current_video": state["current_video"],
        "current_transcript": state["current_transcript"],
        "active_processes": list(state["active_processes"].keys()),
        "processed_clips": [f.name for f in processed_dir.glob("*.mp4")],
        "gdrive_authenticated": GOOGLE_TOKEN_FILE.exists(),
    }

# -- FRONTEND --
@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/index.html")
