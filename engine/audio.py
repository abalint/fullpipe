"""ffmpeg helpers for slicing, loudness, speed adjustment, and concatenation."""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .paths import ffmpeg_path, ffprobe_path, _NOWWIN

MIN_SLICE_DURATION = 0.08  # seconds — below this, ffmpeg produces invalid MP3s

_duration_cache = {}


def _clear_duration_cache():
    """Clear the duration probe cache (for test isolation)."""
    _duration_cache.clear()


def probe_audio_duration(filepath, process_tracker=None):
    """Return audio duration in seconds, or None if ffprobe gives non-numeric output."""
    cache_key = str(filepath)
    if cache_key in _duration_cache:
        return _duration_cache[cache_key]

    path = Path(filepath)
    if not path.exists() or path.stat().st_size == 0:
        return None

    ffprobe_cmd = [ffprobe_path(), "-v", "error"]
    # ffprobe 8 can occasionally mis-detect valid MP3 slices as raw VVC.
    # Pinning the demuxer for .mp3 files avoids bogus duration=N/A.
    if path.suffix.lower() == ".mp3":
        ffprobe_cmd.extend(["-f", "mp3"])
    ffprobe_cmd.extend([
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    _run = process_tracker.run if process_tracker else subprocess.run
    try:
        result = _run(ffprobe_cmd, capture_output=True, text=True, check=True, timeout=10, **_NOWWIN)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    raw = result.stdout.strip()
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(duration) or duration <= 0:
        return None

    _duration_cache[cache_key] = duration
    return duration


def validate_audio_file(filepath):
    """Check if an audio file is decodable via ffprobe. Returns True if valid."""
    return probe_audio_duration(filepath) is not None


def _next_version(path):
    """Return path with _v2, _v3, ... suffix, finding the first unused name."""
    path = Path(path)
    stem = path.stem
    parent = path.parent
    suffix = path.suffix
    v = 2
    while True:
        candidate = parent / f"{stem}_v{v}{suffix}"
        if not candidate.exists():
            return candidate
        v += 1


def extract_audio_to_mp3(input_path, output_path, process_tracker=None):
    """Extract the audio stream from a video file and save as mp3."""
    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-vn",
        "-codec:a", "libmp3lame", "-q:a", "0",
        str(output_path),
    ]
    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
    if result.returncode != 0:
        raise RuntimeError(f"extract_audio_to_mp3 failed: {result.stderr.strip()}")


def slice_audio(input_path, start, end, output_path, target_lufs=None,
                process_tracker=None):
    """Extract a segment from an audio file using ffmpeg.

    When *target_lufs* is set, loudness-normalize the slice to that integrated
    loudness in the same ffmpeg pass (no intermediate re-encode).
    """
    duration = end - start
    if duration < MIN_SLICE_DURATION:
        raise ValueError(
            f"slice_audio called with duration {duration:.3f}s < MIN_SLICE_DURATION "
            f"({MIN_SLICE_DURATION}s). Caller must skip or merge this interval."
        )
    filters = ["aresample=async=1"]
    if target_lufs is not None:
        # loudnorm upsamples to 192 kHz internally; pin the output rate back
        # down (libmp3lame tops out at 48 kHz anyway).
        filters.append(f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11")
        filters.append("aresample=48000")
    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run([
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-af", ",".join(filters),
        "-acodec", "libmp3lame", "-q:a", "2",
        output_path,
    ], capture_output=True, text=True, **_NOWWIN)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg slice failed (exit {result.returncode}) for "
            f"{input_path} [{start:.3f}s → {end:.3f}s]: {result.stderr.strip()}"
        )


def measure_loudness(filepath, process_tracker=None):
    """Measure integrated loudness (LUFS) of an audio file."""
    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run([
        ffmpeg_path(), "-y", "-hide_banner", "-i", filepath,
        "-af", "loudnorm=print_format=json", "-f", "null", "-",
    ], capture_output=True, text=True, **_NOWWIN)
    m = re.search(r'\{[^}]+\}', result.stderr, re.DOTALL)
    if m:
        data = json.loads(m.group())
        return float(data.get("input_i", -24))
    return -24.0


def match_loudness(input_path, output_path, target_lufs, process_tracker=None):
    """Adjust audio to match a target loudness (LUFS)."""
    _run = process_tracker.run if process_tracker else subprocess.run
    _run([
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-af", f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11",
        "-acodec", "libmp3lame", "-q:a", "2",
        output_path,
    ], check=True, **_NOWWIN)


def concatenate_segments(segment_files, output_path, logger=None, process_tracker=None):
    """Concatenate audio segments using ffmpeg concat demuxer."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")
        concat_list = f.name

    _run = process_tracker.run if process_tracker else subprocess.run
    try:
        _run([
            ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-acodec", "libmp3lame", "-q:a", "2",
            output_path,
        ], check=True, **_NOWWIN)
    finally:
        os.unlink(concat_list)
    if logger:
        logger.debug("Concatenated segments", segment_count=len(segment_files))


def adjust_speed(input_path, output_path, speed, process_tracker=None):
    """Adjust audio playback speed using ffmpeg atempo filter."""
    filters = []
    s = speed
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    filters.append(f"atempo={s:.4f}")

    _run = process_tracker.run if process_tracker else subprocess.run
    _run([
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-af", ",".join(filters),
        "-acodec", "libmp3lame", "-q:a", "2",
        output_path,
    ], check=True, **_NOWWIN)


def _build_atempo_chain(speed):
    """Build a list of atempo filter strings for the given speed factor."""
    filters = []
    s = speed
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    filters.append(f"atempo={s:.4f}")
    return filters


def adjust_speed_and_normalize(input_path, output_path, speed, target_lufs,
                               process_tracker=None):
    """Adjust speed and normalize loudness in a single ffmpeg call.

    Combines atempo chain + loudnorm into one -af filter, eliminating
    an intermediate file and subprocess call.
    """
    filters = []
    if speed != 1.0:
        filters.extend(_build_atempo_chain(speed))
    filters.append(f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11")

    _run = process_tracker.run if process_tracker else subprocess.run
    _run([
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-af", ",".join(filters),
        "-acodec", "libmp3lame", "-q:a", "2",
        output_path,
    ], check=True, **_NOWWIN)


def detect_gaps_from_subtitles(srt_path, min_gap_duration=2.0, total_duration=None):
    """Detect gaps between subtitle entries that exceed *min_gap_duration*.

    Uses subtitle timing data instead of audio amplitude analysis, giving
    precise gap boundaries aligned with actual speech.

    When *total_duration* is provided, also detects:
    - Pre-roll gap: 0.0 to first subtitle start
    - Post-roll gap: last subtitle end to total_duration

    Returns list of (gap_start, gap_end) tuples — same format as the old
    ``detect_silences`` output so callers are unaffected.
    """
    from .srt_parser import parse_srt

    subs = parse_srt(str(srt_path))
    if not subs:
        return []

    gaps = []

    # Pre-roll gap: silence before first subtitle
    if total_duration is not None and subs[0][0] >= min_gap_duration:
        gaps.append((0.0, subs[0][0]))

    for i in range(len(subs) - 1):
        gap_start = subs[i][1]       # end of current subtitle
        gap_end = subs[i + 1][0]     # start of next subtitle
        if gap_end - gap_start >= min_gap_duration:
            gaps.append((gap_start, gap_end))

    # Post-roll gap: silence after last subtitle
    if total_duration is not None and total_duration - subs[-1][1] >= min_gap_duration:
        gaps.append((subs[-1][1], total_duration))

    return gaps


def _compute_keep_segments(silences, total_duration, half_padding=0.250):
    """Convert silence intervals into keep-segments.

    For each long silence, keep half_padding from each end (natural ambient sound).
    Returns list of (start, end) keep-segments.
    """
    if not silences:
        return [(0.0, total_duration)]

    segments = []
    cursor = 0.0

    for sil_start, sil_end in silences:
        # Keep audio before this silence
        keep_end = min(sil_start + half_padding, sil_end)
        if keep_end - cursor >= MIN_SLICE_DURATION:
            segments.append((cursor, keep_end))

        # Resume after the silence with padding
        cursor = max(sil_end - half_padding, sil_start)

    # Keep audio after last silence
    if total_duration - cursor >= MIN_SLICE_DURATION:
        segments.append((cursor, total_duration))

    return segments


def condense_audio(input_path, output_path, srt_path, logger=None, process_tracker=None):
    """Remove long gaps (≥2s between subtitles) from audio, keeping 500ms padding.

    Uses subtitle timing data for precise gap boundaries instead of audio
    amplitude analysis.  Also trims long pre-roll (before first subtitle)
    and post-roll (after last subtitle).

    Returns True if condensing happened, False if no long gaps found (file copied as-is).
    """
    total = probe_audio_duration(input_path, process_tracker=process_tracker)
    if total is None:
        shutil.copy2(input_path, output_path)
        return False

    gaps = detect_gaps_from_subtitles(srt_path, total_duration=total)
    if not gaps:
        shutil.copy2(input_path, output_path)
        return False

    segments = _compute_keep_segments(gaps, total)
    if not segments:
        shutil.copy2(input_path, output_path)
        return False

    if logger:
        logger.debug("Condensing audio", gap_count=len(gaps), keep_segments=len(segments),
                      input_duration_sec=round(total, 2))

    with tempfile.TemporaryDirectory() as tmp_dir:
        slice_paths = []
        for i, (start, end) in enumerate(segments):
            slice_path = os.path.join(tmp_dir, f"seg_{i:04d}.mp3")
            slice_audio(input_path, start, end, slice_path, process_tracker=process_tracker)
            slice_paths.append(slice_path)

        concatenate_segments(slice_paths, output_path, process_tracker=process_tracker)

    if logger:
        output_duration = probe_audio_duration(output_path)
        if output_duration is not None:
            logger.debug("Condense complete", output_duration_sec=round(output_duration, 2),
                          trimmed_sec=round(total - output_duration, 2))

    return True


def text_hash(text):
    """Short hash of text for cache filenames."""
    return hashlib.md5(text.encode()).hexdigest()[:10]
