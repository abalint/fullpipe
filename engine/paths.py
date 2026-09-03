"""Resolve external binaries (ffmpeg, ffprobe, yt-dlp) from PATH.

Replaces audioPrime's bin_paths.py: fullPipe is not a bundled GUI app, so
there is no app-local bin/ directory — binaries come from the system
(brew install ffmpeg yt-dlp). _NOWWIN is kept so vendored callers can do
**_NOWWIN unconditionally.
"""

import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# Suppress CMD windows when spawning subprocesses on Windows; empty elsewhere.
_NOWWIN: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32"
    else {}
)


@lru_cache(maxsize=None)
def _resolve(name: str) -> str:
    # Prefer a copy installed alongside the interpreter (the project venv):
    # brew's yt-dlp formula lags upstream by months, and a stale yt-dlp fails
    # downloads outright (YouTube retires player clients faster than that).
    # `pip install -U yt-dlp` in .venv is then the fix, with no PATH juggling.
    local = Path(sys.executable).parent / name
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"'{name}' not found on PATH — install it (e.g. brew install {name})"
    )


def ffmpeg_path() -> str:
    return _resolve("ffmpeg")


def ffprobe_path() -> str:
    return _resolve("ffprobe")


def ytdlp_path() -> str:
    return _resolve("yt-dlp")


def ytdlp_extra_args() -> list:
    """Extra args prepended to every yt-dlp call (none for system installs)."""
    return []
