"""yt-dlp CLI wrapper for downloading YouTube audio and subtitles."""

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .paths import ytdlp_path, ytdlp_extra_args, _NOWWIN
from .local_file import is_local_file
from .transcriber import TranscriptionError, transcribe_audio_to_srt


def _resolve_transcription_engine(sub_lang: str, engine_pref: str = "auto",
                                  elevenlabs_api_key: str = None) -> str:
    """Determine which transcription engine to use.

    engine_pref is "auto" | "elevenlabs" | "reazonspeech".
    Returns "reazonspeech" or "elevenlabs".
    """
    if engine_pref == "reazonspeech":
        # ReazonSpeech is Japanese-only; fall back to ElevenLabs otherwise
        return "reazonspeech" if sub_lang == "ja" else "elevenlabs"
    if engine_pref == "elevenlabs":
        return "elevenlabs"
    # auto
    if sub_lang == "ja" and not elevenlabs_api_key:
        return "reazonspeech"
    return "elevenlabs"


def fetch_full_metadata(url, cookie_browser=None, logger=None, process_tracker=None):
    """Fetch rich provenance metadata from a media URL (DESIGN.md — Taste
    metadata: "rescue the discarded yt-dlp dump").

    Args:
        url: Media URL (YouTube, stand.fm, or any yt-dlp supported site)
        cookie_browser: Browser name to extract cookies from (e.g. "firefox", "chrome")

    Returns:
        dict: uploader, title, channel, channel_id, duration (seconds),
        upload_date (YYYYMMDD), view_count, description, tags (list). Fields the
        site doesn't provide are None (tags: []).

    Raises:
        RuntimeError: If metadata extraction fails
    """
    # Use JSON output to get reliable metadata from any site
    cmd = [
        ytdlp_path(),
        *ytdlp_extra_args(),
        "--dump-json",
        "--no-playlist",
    ]
    if cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])
    cmd.append(url)

    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Sign in to confirm" in stderr or "LOGIN_REQUIRED" in stderr:
            cookie_hint = ""
            if not cookie_browser:
                cookie_hint = " Pass cookie_browser (e.g. firefox) to reuse browser cookies."
            raise RuntimeError(
                f"YouTube requires authentication for this video.{cookie_hint}"
            )
        if "HTTP Error 413" in stderr or "Failed to extract any player response" in stderr:
            cookie_hint = ""
            if not cookie_browser:
                cookie_hint = " Also try passing cookie_browser (e.g. firefox)."
            raise RuntimeError(
                "yt-dlp may be outdated (YouTube returned HTTP 413). "
                f"Update yt-dlp (brew upgrade yt-dlp).{cookie_hint}"
            )
        raise RuntimeError(f"Failed to fetch video metadata: {stderr}")

    try:
        metadata = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse metadata: {e}")

    # Try multiple fields for uploader/channel name (different sites use different fields)
    uploader = (
        metadata.get("channel") or
        metadata.get("uploader") or
        metadata.get("creator") or
        metadata.get("artist")
    )

    title = metadata.get("title") or metadata.get("fulltitle") or "Unknown"
    domain = metadata.get("webpage_url_domain", "")

    # For sites without uploader info, try to extract from title
    if not uploader:
        # stand.fm format: "#123 Episode Title - Show Name | stand.fm (1)"
        if "stand.fm" in domain or " - " in title and " | " in title:
            # Extract text between " - " and " | "
            parts = title.split(" - ", 1)
            if len(parts) == 2:
                second_part = parts[1].split(" | ", 1)
                if len(second_part) >= 1:
                    uploader = second_part[0].strip()

    # Final fallback
    if not uploader:
        uploader = domain.replace("www.", "").title() if domain else "Unknown"

    if logger:
        logger.debug("Metadata fetched", uploader=uploader, title=title)
    return {
        "uploader": uploader,
        "title": title,
        "channel": metadata.get("channel") or metadata.get("uploader"),
        "channel_id": metadata.get("channel_id") or metadata.get("uploader_id"),
        "duration": metadata.get("duration"),
        "upload_date": metadata.get("upload_date"),
        "view_count": metadata.get("view_count"),
        "description": metadata.get("description"),
        "tags": metadata.get("tags") or [],
    }


def fetch_video_metadata(url, cookie_browser=None, logger=None, process_tracker=None):
    """Back-compat wrapper → (uploader, title). Callers wanting full provenance
    use fetch_full_metadata."""
    meta = fetch_full_metadata(url, cookie_browser=cookie_browser, logger=logger,
                               process_tracker=process_tracker)
    return meta["uploader"], meta["title"]


def download_youtube(url, output_dir, progress_callback=None, cookie_browser=None, sub_lang="ja",
                     force_transcription=False, logger=None,
                     process_tracker=None, transcribe_fallback=True,
                     engine_pref="auto", elevenlabs_api_key=None):
    """Download audio (mp3) and subtitles (srt) from a media URL.

    This function supports YouTube and other yt-dlp compatible sites (e.g., stand.fm, Vimeo, etc.).

    Args:
        url: Media URL (YouTube, stand.fm, or any yt-dlp supported site)
        output_dir: Directory to save files to
        progress_callback: Optional callable(status_string) for progress updates
        cookie_browser: Browser name to extract cookies from (e.g. "firefox", "chrome")
        sub_lang: Subtitle language code (e.g. "ja", "ko", "es")
        force_transcription: If True, skip subtitle download and always transcribe

    Returns:
        (mp3_path, srt_path) tuple of Path objects

    Raises:
        ValueError: If URL is invalid, video is private, or no subtitles found
        RuntimeError: If download fails
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Extract video ID from URL for predictable output filenames
    # For YouTube, this extracts the video ID. For other sites, falls back to yt-dlp's ID.
    video_id = _extract_video_id(url)
    if not video_id:
        # Fallback: use yt-dlp to get the video ID
        if progress_callback:
            progress_callback("Extracting video ID...")
        try:
            video_id = _fetch_video_id_from_ytdlp(url, cookie_browser,
                                                    process_tracker=process_tracker)
        except RuntimeError as e:
            raise ValueError(f"Could not extract video ID from URL: {url}. Error: {e}")

    if not video_id:
        raise ValueError(f"Could not extract video ID from URL: {url}")

    outtmpl = str(output_dir / f"{video_id}.%(ext)s")

    cmd = [
        ytdlp_path(),
        *ytdlp_extra_args(),
        "--format", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3",
        "--output", outtmpl,
        "--no-playlist",
        "--no-overwrites",  # Don't re-download files that already exist
    ]

    # Only request subtitles if not forcing transcription
    if not force_transcription:
        cmd.extend([
            "--write-auto-sub", "--write-sub",
            "--sub-langs", sub_lang,
            "--convert-subs", "srt",
        ])

    if cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])

    # Throttle to stay under YouTube's rate limiter. `-t sleep` is yt-dlp's
    # official preset for this: --sleep-subtitles 5 --sleep-requests 0.75
    # --sleep-interval 10 --max-sleep-interval 20 (tracks upstream tuning).
    cmd.extend(["-t", "sleep"])

    cmd.append(url)

    # Check if audio already exists
    mp3_path = output_dir / f"{video_id}.mp3"
    audio_already_exists = mp3_path.exists()
    if logger:
        logger.debug("Download starting", video_id=video_id, audio_cached=audio_already_exists,
                      force_transcription=force_transcription)

    if progress_callback:
        if audio_already_exists:
            progress_callback("Checking for subtitles (audio already cached)...")
        else:
            progress_callback("Downloading audio...")

    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(
        cmd,
        capture_output=True,
        text=True,
        **_NOWWIN,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "is not available" in stderr or "Private video" in stderr:
            raise ValueError(f"Could not access video: {stderr}")
        if "Sign in to confirm" in stderr or "LOGIN_REQUIRED" in stderr:
            cookie_hint = ""
            if not cookie_browser:
                cookie_hint = " Pass cookie_browser (e.g. firefox) to reuse browser cookies."
            raise RuntimeError(
                f"YouTube requires authentication for this video.{cookie_hint}"
            )
        if "HTTP Error 413" in stderr or "Failed to extract any player response" in stderr:
            cookie_hint = ""
            if not cookie_browser:
                cookie_hint = " Also try passing cookie_browser (e.g. firefox)."
            raise RuntimeError(
                "yt-dlp may be outdated (YouTube returned HTTP 413). "
                f"Update yt-dlp (brew upgrade yt-dlp).{cookie_hint}"
            )
        raise RuntimeError(f"Download failed: {stderr}")

    # Find the output files
    srt_path = output_dir / f"{video_id}.{sub_lang}.srt"

    if not mp3_path.exists():
        raise RuntimeError(f"MP3 file not found after download: {mp3_path}")

    if progress_callback and audio_already_exists:
        progress_callback("Audio cache verified")

    # Check if subtitles exist, fallback to transcription if enabled
    if not srt_path.exists():
        # Check if force transcription or transcription fallback is enabled
        if force_transcription or transcribe_fallback:
            engine = _resolve_transcription_engine(sub_lang, engine_pref, elevenlabs_api_key)
            if logger:
                logger.info("Transcription fallback triggered", engine=engine,
                            force=force_transcription, sub_lang=sub_lang)

            if engine == "reazonspeech":
                from .transcriber import reazonspeech_transcribe_to_srt
                if progress_callback:
                    label = "Force transcribing audio with ReazonSpeech..." if force_transcription else "No subtitles found, transcribing with ReazonSpeech..."
                    progress_callback(label)
                try:
                    reazonspeech_transcribe_to_srt(
                        audio_path=mp3_path,
                        output_srt_path=srt_path,
                        progress_callback=progress_callback,
                    )
                    if progress_callback:
                        progress_callback("Transcription complete")
                except TranscriptionError as e:
                    raise RuntimeError(f"Transcription failed: {e}")
            else:
                api_key = elevenlabs_api_key
                if not api_key:
                    raise RuntimeError(
                        f"No {sub_lang} subtitles found and transcription is enabled but no API key provided. "
                        "Set ELEVENLABS_API_KEY in .env, or use engine_pref=reazonspeech for Japanese audio."
                    )

                if progress_callback:
                    label = "Force transcribing audio with ElevenLabs..." if force_transcription else f"No subtitles found, transcribing audio with ElevenLabs..."
                    progress_callback(label)

                try:
                    transcribe_audio_to_srt(
                        audio_path=mp3_path,
                        output_srt_path=srt_path,
                        language_code=sub_lang,
                        api_key=api_key,
                        progress_callback=progress_callback
                    )
                    if progress_callback:
                        progress_callback("Transcription complete")
                except TranscriptionError as e:
                    raise RuntimeError(f"Transcription failed: {e}")
        else:
            raise RuntimeError(
                f"No {sub_lang} subtitles found. This video may not have {sub_lang} captions. "
                "Pass transcribe_fallback=True or force_transcription=True to transcribe instead."
            )

    if progress_callback:
        progress_callback("Download complete")

    if logger:
        mp3_size = mp3_path.stat().st_size if mp3_path.exists() else 0
        srt_size = srt_path.stat().st_size if srt_path.exists() else 0
        logger.debug("Download complete", mp3_size_bytes=mp3_size, srt_size_bytes=srt_size,
                      video_id=video_id)

    return mp3_path, srt_path


def download_video(url, output_dir, progress_callback=None, cookie_browser=None, logger=None, resolution="", process_tracker=None):
    """Download video (mp4) from a media URL using yt-dlp.

    This downloads the full video (not just audio) for video output mode.

    Args:
        url: Media URL (YouTube, stand.fm, or any yt-dlp supported site)
        output_dir: Directory to save files to
        progress_callback: Optional callable(status_string) for progress updates
        cookie_browser: Browser name to extract cookies from
        resolution: Preferred resolution (e.g. "720p", "1080p", "Best", "Audio Only")

    Returns:
        Path to downloaded mp4 file

    Raises:
        RuntimeError: If download fails
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    video_id = _extract_video_id(url)
    if not video_id:
        if progress_callback:
            progress_callback("Extracting video ID...")
        try:
            video_id = _fetch_video_id_from_ytdlp(url, cookie_browser,
                                                    process_tracker=process_tracker)
        except RuntimeError as e:
            raise RuntimeError(f"Could not extract video ID from URL: {url}. Error: {e}")

    mp4_path = output_dir / f"{video_id}.mp4"

    if mp4_path.exists():
        if progress_callback:
            progress_callback("Video file already cached")
        return mp4_path

    outtmpl = str(output_dir / f"{video_id}.%(ext)s")

    _RES_FORMAT = {
        "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "1440p": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
        "4K":    "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    }
    fmt = _RES_FORMAT.get(resolution, "bestvideo+bestaudio/best")

    cmd = [
        ytdlp_path(),
        *ytdlp_extra_args(),
        "--format", fmt,
        "--merge-output-format", "mp4",
        "--output", outtmpl,
        "--no-playlist",
        "--no-overwrites",
    ]

    if cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])

    # Throttle to stay under YouTube's rate limiter (see download_youtube).
    cmd.extend(["-t", "sleep"])

    cmd.append(url)

    if progress_callback:
        progress_callback("Downloading video...")

    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Video download failed: {stderr}")

    if not mp4_path.exists():
        raise RuntimeError(f"MP4 file not found after download: {mp4_path}")

    if progress_callback:
        progress_callback("Video download complete")

    return mp4_path


def _extract_video_id(url):
    """Extract YouTube video ID from various URL formats.

    Returns None for non-YouTube URLs.
    """
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def _fetch_video_id_from_ytdlp(url, cookie_browser=None, process_tracker=None):
    """Fetch video ID from any yt-dlp supported site.

    Args:
        url: Media URL
        cookie_browser: Optional browser name for cookies

    Returns:
        Video ID string (never None or empty)

    Raises:
        RuntimeError: If ID extraction fails or returns empty
    """
    cmd = [ytdlp_path(), "--print", "id", "--no-playlist"]
    if cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])
    cmd.append(url)

    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to extract video ID: {result.stderr.strip()}")

    video_id = result.stdout.strip()
    if not video_id:
        raise RuntimeError("Empty video ID returned from yt-dlp")

    # Sanitize video ID to ensure it's safe for filenames
    # Remove any characters that could cause issues
    video_id = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', video_id)
    if not video_id:
        raise RuntimeError("Video ID became empty after sanitization")

    return video_id


def _is_single_video_url(url):
    """Return True if the URL points to a single video (not a playlist/channel).

    For YouTube URLs, checks for video ID and absence of playlist parameter.
    For non-YouTube URLs, returns True (assumes single video, yt-dlp will handle playlists).
    """
    video_id = _extract_video_id(url)
    if not video_id:
        # Non-YouTube URL - assume single video, let yt-dlp handle playlist detection
        return True
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    # If it has a list= param, it's a YouTube playlist link
    if "list" in params:
        return False
    return True


def resolve_urls(raw_urls, cookie_browser=None, progress_callback=None, logger=None,
                 process_tracker=None):
    """Expand a list of URLs into individual video URLs.

    Single video URLs pass through directly. Playlist and channel URLs
    are expanded via yt-dlp --flat-playlist.

    Supports YouTube and other yt-dlp compatible sites.

    Args:
        raw_urls: List of URL strings
        cookie_browser: Browser name to extract cookies from
        progress_callback: Optional callable(current_index, total, message)

    Returns:
        List of individual video URL strings (deduplicated, order preserved)

    Raises:
        ValueError: If yt-dlp fails to resolve a URL
    """
    seen_ids = set()
    resolved = []
    total = len(raw_urls)
    if logger:
        logger.debug("Resolving URLs", input_count=total)

    for i, url in enumerate(raw_urls):
        if progress_callback:
            progress_callback(i, total, f"Resolving URL {i + 1}/{total}...")

        # Local file paths pass through directly — never touch yt-dlp
        if is_local_file(url):
            resolved.append(url)
            continue

        # Try to extract YouTube video ID for efficiency
        yt_video_id = _extract_video_id(url)
        if yt_video_id and _is_single_video_url(url):
            # YouTube single video - add directly
            if yt_video_id not in seen_ids:
                seen_ids.add(yt_video_id)
                resolved.append(f"https://www.youtube.com/watch?v={yt_video_id}")
        else:
            # For non-YouTube URLs or playlists, try to detect if it's a single video first
            is_youtube = "youtube.com" in url or "youtu.be" in url

            if not is_youtube:
                # Non-YouTube URL - check if it's a single video or playlist
                # Try fetching the video ID to confirm it's accessible
                try:
                    vid_id = _fetch_video_id_from_ytdlp(url, cookie_browser,
                                                        process_tracker=process_tracker)
                    if vid_id and vid_id not in seen_ids:
                        seen_ids.add(vid_id)
                        resolved.append(url)
                        continue
                except RuntimeError:
                    # Could not get ID, try as playlist
                    pass

            # Playlist, channel, or multi-video URL
            # Use yt-dlp to resolve and get canonical URLs
            cmd = [
                ytdlp_path(),
                "--flat-playlist",
                "--dump-json",
            ]
            if cookie_browser:
                cmd.extend(["--cookies-from-browser", cookie_browser])
            cmd.append(url)

            _run = process_tracker.run if process_tracker else subprocess.run
            result = _run(cmd, capture_output=True, text=True, **_NOWWIN)
            if result.returncode != 0:
                raise ValueError(
                    f"Failed to resolve URL: {url}\n{result.stderr.strip()}"
                )

            # Process playlist/multi-video output
            output = result.stdout.strip()
            if not output:
                raise ValueError(f"Could not resolve URL: {url}")

            lines = output.splitlines()
            for line in lines:
                if not line.strip():
                    continue
                entry = json.loads(line)
                vid_id = entry.get("id")
                vid_url = entry.get("webpage_url") or entry.get("url")

                if vid_id and vid_id not in seen_ids:
                    seen_ids.add(vid_id)
                    # Use webpage_url for canonical URLs when available
                    if vid_url and vid_url.startswith("http"):
                        resolved.append(vid_url)
                    elif yt_video_id:
                        # YouTube URL - reconstruct
                        resolved.append(f"https://www.youtube.com/watch?v={vid_id}")
                    else:
                        # Fallback to original URL for non-YouTube
                        resolved.append(url)

    if progress_callback:
        progress_callback(total, total, f"Resolved {len(resolved)} videos")

    if logger:
        logger.debug("URL resolution complete", input_count=total, resolved_count=len(resolved),
                      deduped=total - len(resolved) + len(seen_ids) - len(resolved))

    return resolved
