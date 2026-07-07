#!/usr/bin/env python3
"""fullPipe GPU transcription service — runs on the desktop (RTX 2070S) and is
called by the Mac pipeline over Tailscale.

A drop-in remote counterpart to engine/transcriber.py's cloud/CPU engines:
accepts an uploaded audio file and returns word-level timestamps in the exact
shape engine.transcriber.words_to_srt() consumes:

    {"words": [{"text": str, "start": float, "end": float}, ...],
     "language": "ja", "duration": float, "model": str}

Backend is faster-whisper (CTranslate2) on CUDA, defaulting to the
Japanese-tuned Kotoba-Whisper v2.0. The model is chosen via the FULLPIPE_ASR_MODEL
env var so it can be swapped (large-v3, a Qwen3-ASR CT2 build, …) without code
changes.

Run:
    set FULLPIPE_GPU_TOKEN=<shared-secret>   # optional but recommended
    python service.py                        # serves 0.0.0.0:8422

Env:
    FULLPIPE_ASR_MODEL     HF id or local path (default kotoba-tech/kotoba-whisper-v2.0-faster)
    FULLPIPE_ASR_DEVICE    cuda | cpu           (default cuda)
    FULLPIPE_ASR_COMPUTE   float16 | int8_float16 | int8 (default float16)
    FULLPIPE_GPU_TOKEN     bearer token; if unset, auth is disabled
    FULLPIPE_GPU_PORT      listen port          (default 8422)
    FULLPIPE_ASR_VAD       1 to enable Silero VAD filtering (default 1)
"""

import os
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse


def _register_cuda_dlls():
    """On Windows, make the pip-installed CUDA runtime DLLs loadable.

    The nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels drop cublas64_12.dll and
    cudnn64_9.dll under site-packages/nvidia/*/bin, but CTranslate2 can't find
    them unless those dirs are on the DLL search path. Registering them here
    keeps the service self-contained — no PATH edits or activate scripts needed.
    Silently no-ops off Windows / when the wheels aren't present.
    """
    if not sys.platform.startswith("win"):
        return
    for entry in sys.path:
        nvidia = Path(entry) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            if bin_dir.is_dir():
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    pass
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


_register_cuda_dlls()

MODEL_ID = os.environ.get("FULLPIPE_ASR_MODEL", "kotoba-tech/kotoba-whisper-v2.0-faster")
DEVICE = os.environ.get("FULLPIPE_ASR_DEVICE", "cuda")
COMPUTE = os.environ.get("FULLPIPE_ASR_COMPUTE", "float16")
TOKEN = os.environ.get("FULLPIPE_GPU_TOKEN", "")
PORT = int(os.environ.get("FULLPIPE_GPU_PORT", "8422"))
VAD = os.environ.get("FULLPIPE_ASR_VAD", "1") not in ("0", "false", "False", "")

app = FastAPI(title="fullPipe GPU transcription", version="1.0")

# Loaded lazily on first request so the process starts (and /health answers)
# even while the model is still downloading/warming.
_MODEL = None
_MODEL_ERR = None


def _get_model():
    global _MODEL, _MODEL_ERR
    if _MODEL is not None:
        return _MODEL
    if _MODEL_ERR is not None:
        raise _MODEL_ERR
    try:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(MODEL_ID, device=DEVICE, compute_type=COMPUTE)
        return _MODEL
    except Exception as e:  # noqa: BLE001 — surfaced to the caller as 503
        _MODEL_ERR = e
        raise


def _check_auth(authorization: str | None):
    if not TOKEN:
        return
    expected = f"Bearer {TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_ID,
        "device": DEVICE,
        "compute": COMPUTE,
        "vad": VAD,
        "model_loaded": _MODEL is not None,
        "auth_required": bool(TOKEN),
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = "ja",
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)

    try:
        model = _get_model()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"model unavailable: {e}")

    # Persist the upload to a temp file — faster-whisper reads from a path.
    suffix = Path(file.filename or "audio").suffix or ".mp3"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        data = await file.read()
        tmp.write(data)
        tmp.flush()
        tmp.close()

        t0 = time.time()
        # NOTE: no word_timestamps — Kotoba-Whisper (and other distil-whisper
        # models) have only 2 decoder layers, so faster-whisper's word-alignment
        # (DTW over attention heads) hangs. Segment-level timestamps are all the
        # pipeline needs anyway: acquire.py re-segments into sentences via
        # merge_to_sentences(). Each Whisper segment becomes one "word" entry.
        # condition_on_previous_text=False: without it, a distil model that hits
        # a hard patch (music-under-speech, overlap) can collapse and skip large
        # spans — on a 2-min test it silently dropped ~65s of speech. Disabling
        # it restores full coverage and curbs repetition loops.
        segments, info = model.transcribe(
            tmp.name,
            language=language,
            vad_filter=VAD,
            beam_size=5,
            condition_on_previous_text=False,
        )

        words = []
        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                words.append({
                    "text": text,
                    "start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                })

        if not words:
            raise HTTPException(status_code=422, detail="no speech detected in audio")

        return JSONResponse({
            "words": words,
            "language": info.language,
            "duration": round(float(info.duration), 2),
            "model": MODEL_ID,
            "elapsed": round(time.time() - t0, 2),
        })
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
