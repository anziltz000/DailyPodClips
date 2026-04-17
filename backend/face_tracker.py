"""
face_tracker.py — Active speaker face detection + dynamic FFmpeg crop generation.
Uses MediaPipe Face Detection at 2fps sampling to minimize CPU load on OCL Free Tier.
Generates a smooth, centered 1:1 crop that follows the active/largest face.
"""
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# MediaPipe import — works on ARM64
import mediapipe as mp

from backend.config import (
    FACE_TRACK_FPS, FACE_TRACK_SMOOTHING, FACE_TRACK_MIN_CONFIDENCE,
    FFMPEG_VIDEO_CODEC, FFMPEG_VIDEO_PRESET, FFMPEG_VIDEO_CRF,
    FFMPEG_PIXEL_FORMAT, FFMPEG_AUDIO_CODEC, FFMPEG_AUDIO_BITRATE,
    FFMPEG_AUDIO_SAMPLE_RATE,
)


def get_video_info(video_path: str) -> dict:
    """Get video dimensions and fps using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream["codec_type"] == "video":
                return {
                    "width": int(stream["width"]),
                    "height": int(stream["height"]),
                    "fps": eval(stream.get("r_frame_rate", "30/1")),
                    "duration": float(stream.get("duration", 0)),
                }
    except Exception:
        pass
    return {"width": 1920, "height": 1080, "fps": 30, "duration": 0}


def detect_faces_sampled(video_path: str, sample_fps: int = FACE_TRACK_FPS) -> list:
    """
    Sample the video at `sample_fps` frames per second and detect faces.
    Returns a list of (timestamp_sec, [face_boxes]) where each face_box is
    (x_center_norm, y_center_norm, width_norm, height_norm).
    """
    mp_face = mp.solutions.face_detection
    face_detection = mp_face.FaceDetection(
        model_selection=1,  # 1 = full-range model (better for distant faces)
        min_detection_confidence=FACE_TRACK_MIN_CONFIDENCE,
    )

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"cv2.VideoCapture failed for {video_path}")
        return []

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_interval = max(1, int(video_fps / sample_fps))

    detections = []
    frame_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            timestamp = frame_idx / video_fps
            # Convert BGR to RGB for MediaPipe
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = face_detection.process(rgb)

            faces = []
            if results.detections:
                for det in results.detections:
                    bbox = det.location_data.relative_bounding_box
                    # Center coordinates (normalized 0-1)
                    cx = bbox.xmin + bbox.width / 2
                    cy = bbox.ymin + bbox.height / 2
                    faces.append({
                        "cx": cx, "cy": cy,
                        "w": bbox.width, "h": bbox.height,
                        "score": det.score[0],
                    })

            detections.append({"time": timestamp, "faces": faces})

        frame_idx += 1

    cap.release()
    face_detection.close()
    return detections


def pick_active_speaker(detections: list) -> list:
    """
    From all detected faces across frames, pick the most prominent face
    (largest + most consistent) as the 'active speaker' to track.
    Returns list of (timestamp, cx, cy) for the chosen face.
    """
    if not detections:
        return []

    # Strategy: Track the largest face in each frame.
    # For podcast-style content, the speaker is usually the largest face.
    track_points = []
    for det in detections:
        if det["faces"]:
            # Pick the largest face by bounding box area
            best = max(det["faces"], key=lambda f: f["w"] * f["h"])
            track_points.append({
                "time": det["time"],
                "cx": best["cx"],
                "cy": best["cy"],
            })
        elif track_points:
            # No face found — hold the last known position
            track_points.append({
                "time": det["time"],
                "cx": track_points[-1]["cx"],
                "cy": track_points[-1]["cy"],
            })

    return track_points


def smooth_track(track_points: list, window: int = FACE_TRACK_SMOOTHING) -> list:
    """Apply a moving average to smooth the crop coordinates and avoid jitter."""
    if len(track_points) < 2:
        return track_points

    cx_vals = np.array([p["cx"] for p in track_points])
    cy_vals = np.array([p["cy"] for p in track_points])

    # Simple moving average with padding
    kernel = np.ones(window) / window
    cx_smooth = np.convolve(cx_vals, kernel, mode="same")
    cy_smooth = np.convolve(cy_vals, kernel, mode="same")

    # Fix edges — use original values at boundaries
    half_w = window // 2
    cx_smooth[:half_w] = cx_vals[:half_w]
    cx_smooth[-half_w:] = cx_vals[-half_w:]
    cy_smooth[:half_w] = cy_vals[:half_w]
    cy_smooth[-half_w:] = cy_vals[-half_w:]

    smoothed = []
    for i, p in enumerate(track_points):
        smoothed.append({
            "time": p["time"],
            "cx": float(cx_smooth[i]),
            "cy": float(cy_smooth[i]),
        })

    return smoothed


def generate_crop_filter(video_path: str, temp_dir: str) -> Optional[str]:
    """
    Main entry point: Detect faces, pick speaker, smooth track,
    and generate an FFmpeg crop filtergraph script.
    Returns the path to the filter script, or None if no faces found.
    """
    info = get_video_info(video_path)
    w, h = info["width"], info["height"]
    fps = info["fps"]

    # 1:1 crop size is the minimum of width and height
    crop_size = min(w, h)

    # Detect faces at low fps
    detections = detect_faces_sampled(video_path)

    if not detections or not any(d["faces"] for d in detections):
        return None  # No faces found

    # Pick and smooth the active speaker track
    track = pick_active_speaker(detections)
    track = smooth_track(track)

    if not track:
        return None

    # Generate FFmpeg sendcmd script for dynamic cropping
    # This tells FFmpeg to change crop coordinates at specific timestamps
    filter_lines = []
    for point in track:
        # Convert normalized coords to pixel coords
        # Center the 1:1 crop on the face
        crop_x = int(point["cx"] * w - crop_size / 2)
        crop_y = int(point["cy"] * h - crop_size / 2)

        # Clamp to video bounds
        crop_x = max(0, min(crop_x, w - crop_size))
        crop_y = max(0, min(crop_y, h - crop_size))

        filter_lines.append({
            "time": point["time"],
            "x": crop_x,
            "y": crop_y,
        })

    # Write the sendcmd script
    script_path = Path(temp_dir) / "crop_commands.txt"
    with open(script_path, "w") as f:
        for i, fl in enumerate(filter_lines):
            t = fl["time"]
            # Each line: time crop x and y
            f.write(f"{t:.3f} crop x {fl['x']};\n")
            f.write(f"{t:.3f} crop y {fl['y']};\n")

    # Also save the crop data as JSON for debugging
    debug_path = Path(temp_dir) / "crop_data.json"
    with open(debug_path, "w") as f:
        json.dump({
            "video": {"width": w, "height": h, "fps": fps},
            "crop_size": crop_size,
            "points": filter_lines,
        }, f, indent=2)

    return str(script_path)


def apply_crop_filter(video_path: str, filter_script_path: str, output_path: str) -> int:
    """
    Apply the dynamic crop filter to the video using FFmpeg sendcmd.
    Returns the ffmpeg return code.
    """
    info = get_video_info(video_path)
    crop_size = min(info["width"], info["height"])

    center_x = (info["width"] - crop_size) // 2
    center_y = (info["height"] - crop_size) // 2

    filter_complex = (
        f"sendcmd=f='{filter_script_path}',"
        f"crop={crop_size}:{crop_size}:{center_x}:{center_y},"
        f"scale=1080:1080:flags=lanczos"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", filter_complex,
        "-c:v", FFMPEG_VIDEO_CODEC,
        "-preset", FFMPEG_VIDEO_PRESET,
        "-crf", FFMPEG_VIDEO_CRF,
        "-profile:v", "high",
        "-pix_fmt", FFMPEG_PIXEL_FORMAT,
        "-c:a", FFMPEG_AUDIO_CODEC,
        "-b:a", FFMPEG_AUDIO_BITRATE,
        "-ar", FFMPEG_AUDIO_SAMPLE_RATE,
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode
