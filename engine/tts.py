"""Piper TTS via Python API (piper-tts package)."""

import hashlib
import subprocess
import time
import wave
from pathlib import Path

from .paths import ffmpeg_path, _NOWWIN


class PiperTTS:
    """Wraps the piper-tts Python package for TTS synthesis."""

    def __init__(self, model_path):
        from piper import PiperVoice  # lazy: TTS is the last-resort card source
        self.model_path = Path(model_path)
        self.model_name = self.model_path.stem
        self._voice = PiperVoice.load(str(self.model_path))

    def cache_key(self, text):
        """MD5 hash including model name for unique caching."""
        key_input = f"{text}|piper|{self.model_name}"
        return hashlib.md5(key_input.encode()).hexdigest()[:10]

    def synthesize_to_mp3(self, text, output_path, logger=None, process_tracker=None):
        """Synthesize text to an mp3 file via piper Python API + ffmpeg convert."""
        t0 = time.monotonic()
        output_path = Path(output_path)
        wav_path = output_path.with_suffix(".wav")

        try:
            with wave.open(str(wav_path), "wb") as wav_file:
                self._voice.synthesize_wav(text, wav_file)
        except Exception as e:
            wav_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Piper synthesis failed for text {text[:80]!r}: {e}"
            ) from e

        if not wav_path.exists() or wav_path.stat().st_size == 0:
            wav_path.unlink(missing_ok=True)
            raise RuntimeError(f"Piper produced empty audio for text: {text[:80]!r}")

        _run = process_tracker.run if process_tracker else subprocess.run
        _run([
            ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(wav_path),
            "-acodec", "libmp3lame", "-q:a", "2",
            str(output_path),
        ], check=True, **_NOWWIN)

        wav_path.unlink(missing_ok=True)
        if logger:
            mp3_size = output_path.stat().st_size if output_path.exists() else 0
            logger.debug("TTS synthesized", chars=len(text), mp3_size_bytes=mp3_size,
                          elapsed_sec=round(time.monotonic() - t0, 3))
