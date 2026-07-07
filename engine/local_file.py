"""Utilities for processing local audio/video files through the pipeline."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from .paths import ffmpeg_path, _NOWWIN
from .transcriber import LANGUAGE_CODE_MAP

# Display names for companion-subtitle discovery (video.Japanese.srt).
# Replaces audioPrime's LANGUAGE_REGISTRY lookup.
LANGUAGE_DISPLAY_NAMES = {
    "ja": "Japanese", "ko": "Korean", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "zh-Hans": "Chinese (Simplified)", "zh-Hant": "Chinese (Traditional)",
}

# Audio and video extensions accepted by the file picker and path validation
SUPPORTED_EXTENSIONS = {
    # Audio
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus",
    # Audiobook
    ".m4b",
    # Video
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts",
}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts"}

AUDIOBOOK_EXTENSIONS = {".m4b"}

AUDIO_ONLY_EXTENSIONS = SUPPORTED_EXTENSIONS - VIDEO_EXTENSIONS


def is_m4b(filepath):
    """Return True if *filepath* has an M4B audiobook extension."""
    return Path(filepath).suffix.lower() in AUDIOBOOK_EXTENSIONS


def get_video_path(filepath):
    """Return *filepath* if it has a video extension, else None."""
    if Path(filepath).suffix.lower() in VIDEO_EXTENSIONS:
        return str(Path(filepath).resolve())
    return None


def is_local_file(entry):
    """Return True if *entry* looks like an existing local file (not a URL)."""
    if re.match(r"https?://", entry, re.IGNORECASE):
        return False
    return Path(entry).is_file()


def generate_local_file_id(filepath):
    """Return a stable cache key for a local file.

    The key is ``local_<hex>`` where *hex* is derived from the absolute path,
    file size, and modification time so that re-processing the same unchanged
    file reuses the cache.
    """
    p = Path(filepath).resolve()
    stat = p.stat()
    blob = f"{p}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    return "local_" + hashlib.sha256(blob).hexdigest()[:16]


def get_local_file_metadata(filepath):
    """Return ``(channel_name, title)`` derived from the file path.

    *channel_name* is the parent directory name and *title* is the file stem.
    """
    p = Path(filepath).resolve()
    return p.parent.name or "Local", p.stem


def convert_to_mp3(filepath, output_mp3, process_tracker=None):
    """Convert *filepath* to mp3 at *output_mp3* using ffmpeg.

    If the source is already mp3, it is copied instead.  Video streams are
    stripped with ``-vn``.
    """
    src = Path(filepath)
    dst = Path(output_mp3)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.suffix.lower() == ".mp3":
        shutil.copy2(str(src), str(dst))
        return

    cmd = [
        ffmpeg_path(),
        "-y", "-i", str(src),
        "-vn",                   # strip video
        "-acodec", "libmp3lame",
        "-q:a", "2",            # high quality VBR
        str(dst),
    ]
    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr.strip()}")


def discover_subtitle_file(media_path, lang_code):
    """Find a companion SRT file next to *media_path*.

    Search order:
      1. ``video.ja.srt``          (2-letter audioPrime code)
      2. ``video.jpn.srt``         (3-letter ElevenLabs code via LANGUAGE_CODE_MAP)
      3. ``video.Japanese.srt``    (display name from LANGUAGE_REGISTRY)
      4. ``video.srt``             (bare, assumed match)

    Returns the Path if found, else None.
    """
    base = Path(media_path).resolve()
    stem = base.stem
    parent = base.parent

    # 1) 2-letter code
    candidate = parent / f"{stem}.{lang_code}.srt"
    if candidate.is_file():
        return candidate

    # 2) 3-letter code
    three = LANGUAGE_CODE_MAP.get(lang_code)
    if three:
        candidate = parent / f"{stem}.{three}.srt"
        if candidate.is_file():
            return candidate

    # 3) Display name
    display_name = LANGUAGE_DISPLAY_NAMES.get(lang_code)
    if display_name:
        candidate = parent / f"{stem}.{display_name}.srt"
        if candidate.is_file():
            return candidate

    # 4) Bare
    candidate = parent / f"{stem}.srt"
    if candidate.is_file():
        return candidate

    return None


def prepare_local_file(filepath, download_dir, lang_code,
                       progress_callback=None, force_transcription=False,
                       process_tracker=None, transcribe_fallback=True,
                       engine_pref="auto", elevenlabs_api_key=None,
                       gpu_url=None, gpu_token=None):
    """Prepare a local file for the pipeline, mirroring download_youtube().

    Converts to mp3, discovers or transcribes subtitles, and returns
    ``(mp3_path, srt_path)`` inside *download_dir*.

    Args:
        filepath: Path to the local audio/video file
        download_dir: Cache directory (same as used by download_youtube)
        lang_code: Subtitle language code (e.g. "ja")
        progress_callback: Optional callable(str) for status messages
        force_transcription: If True, skip subtitle discovery and always transcribe

    Returns:
        (mp3_path, srt_path) tuple of Path objects

    Raises:
        RuntimeError: If conversion or subtitle discovery/transcription fails
    """
    filepath = Path(filepath).resolve()
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    file_id = generate_local_file_id(filepath)
    mp3_path = download_dir / f"{file_id}.mp3"
    srt_path = download_dir / f"{file_id}.{lang_code}.srt"

    # Convert to mp3 (skip if cached)
    if not mp3_path.exists():
        if progress_callback:
            progress_callback("Converting to mp3...")
        convert_to_mp3(filepath, mp3_path, process_tracker=process_tracker)
    else:
        if progress_callback:
            progress_callback("Using cached mp3")

    # Subtitle handling
    if not srt_path.exists():
        if force_transcription:
            # Skip subtitle discovery entirely
            _transcribe_local(mp3_path, srt_path, lang_code, progress_callback,
                              label="Force transcribing audio...",
                              engine_pref=engine_pref,
                              elevenlabs_api_key=elevenlabs_api_key,
                              gpu_url=gpu_url, gpu_token=gpu_token)
        else:
            # Try to find a companion SRT
            companion = discover_subtitle_file(filepath, lang_code)
            if companion:
                if progress_callback:
                    progress_callback(f"Found subtitle file: {companion.name}")
                shutil.copy2(str(companion), str(srt_path))
            elif transcribe_fallback:
                _transcribe_local(mp3_path, srt_path, lang_code, progress_callback,
                                  label="No subtitle file found, transcribing...",
                                  engine_pref=engine_pref,
                                  elevenlabs_api_key=elevenlabs_api_key,
                                  gpu_url=gpu_url, gpu_token=gpu_token)
            else:
                raise RuntimeError(
                    f"No {lang_code} subtitle file found next to {filepath.name}. "
                    "Place a .srt file alongside the media file "
                    f"(e.g. {filepath.stem}.{lang_code}.srt), "
                    "or pass transcribe_fallback=True / force_transcription=True."
                )

    if progress_callback:
        progress_callback("Local file ready")

    return mp3_path, srt_path


def _transcribe_local(mp3_path, srt_path, lang_code, progress_callback, label,
                      engine_pref="auto", elevenlabs_api_key=None,
                      gpu_url=None, gpu_token=None):
    """Run transcription for a local file using the configured engine.

    Delegates to the shared dispatcher so local files get the same GPU-service
    routing (and GPU→local/cloud fallback) as downloaded videos.
    """
    from .downloader import transcribe_with_engine
    transcribe_with_engine(
        mp3_path, srt_path, lang_code,
        engine_pref=engine_pref,
        elevenlabs_api_key=elevenlabs_api_key,
        gpu_url=gpu_url,
        gpu_token=gpu_token,
        progress_callback=progress_callback,
        label=label,
    )
