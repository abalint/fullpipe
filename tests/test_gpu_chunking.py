"""GpuTranscriber splits long audio into pieces and shifts timestamps back."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import transcriber as T


def _tone(path, secs):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={secs}", "-c:a", "libmp3lame",
                    str(path)], check=True)


def test_long_audio_is_chunked_and_offset():
    with tempfile.TemporaryDirectory() as d:
        audio = Path(d) / "a.mp3"
        _tone(audio, 25)
        t = T.GpuTranscriber("http://gpu.invalid")
        t.CHUNK_OVER_SEC, t.CHUNK_SEC = 10, 10
        seen = []

        def one(p, lang="ja", cb=None):
            seen.append(p.name)
            if len(seen) == 2:
                raise T.TranscriptionError("GPU service returned no words")
            return [{"text": "x", "start": 1.0, "end": 2.5}]

        t._transcribe_one = one
        words = t.transcribe(audio)
        assert seen == ["part000.mp3", "part001.mp3", "part002.mp3"]
        assert [(w["start"], w["end"]) for w in words] == [(1.0, 2.5), (21.0, 22.5)]


def test_short_audio_is_one_request():
    with tempfile.TemporaryDirectory() as d:
        audio = Path(d) / "a.mp3"
        _tone(audio, 3)
        t = T.GpuTranscriber("http://gpu.invalid")
        t._transcribe_one = lambda p, lang="ja", cb=None: [{"text": p.name, "start": 0, "end": 1}]
        assert t.transcribe(audio)[0]["text"] == "a.mp3"
