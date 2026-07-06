"""ffmpeg helper for grabbing a still frame from a video (card images)."""

import subprocess
from pathlib import Path

from .paths import ffmpeg_path, _NOWWIN

# Card frames are downscaled to this width (px). Small enough to keep the Anki
# collection lean, large enough to read on a phone. Height follows the aspect.
FRAME_WIDTH = 480


def extract_frame(video_path, timestamp, output_path, width=FRAME_WIDTH,
                  process_tracker=None):
    """Grab one JPEG frame from *video_path* at *timestamp* seconds.

    Seeks with ``-ss`` before ``-i`` (fast keyframe seek — a card still doesn't
    need frame-exact timing) and scales to *width* preserving aspect. Raises
    RuntimeError if ffmpeg fails or writes nothing.
    """
    ts = max(0.0, float(timestamp))
    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2:flags=lanczos",
        "-q:v", "3",
        str(output_path),
    ]
    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
    out = Path(output_path)
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(
            f"extract_frame failed for {video_path} @ {ts:.3f}s: "
            f"{result.stderr.strip()}")
    return output_path
