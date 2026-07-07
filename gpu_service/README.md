# fullPipe GPU transcription service

A small HTTP service that runs on the **desktop (RTX 2070 Super)** and transcribes
Japanese audio for the Mac pipeline over Tailscale. It is the local, ~$0/min
replacement for YouTube auto-captions and the ElevenLabs cloud API.

It exposes the same word-level output shape the pipeline already consumes
(`engine.transcriber.words_to_srt`), so on the Mac side it's just a third
transcription engine selected via `asr.gpu_url` in `config.json`.

- **Backend:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) on CUDA
- **Default model:** `kotoba-tech/kotoba-whisper-v2.0-faster` — Japanese-tuned,
  in-domain-strong on natural/broadcast speech, ~6× faster than Whisper large-v3.
- **VAD:** Silero VAD filtering on by default (helps noisy audio).

## Endpoints

- `GET /health` → model / device / load status
- `POST /transcribe` (multipart `file=@audio.mp3`, query `?language=ja`) →
  `{"words": [{"text","start","end"}], "language", "duration", "model", "elapsed"}`

## Setup on the desktop (Windows + NVIDIA)

1. Install Python 3.10–3.12 and ensure the NVIDIA driver is current.
2. Create a venv and install deps:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   The `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels provide the CUDA 12
   runtime libraries faster-whisper needs. If you hit `Could not load cudnn`,
   add the wheels' `bin` dirs to `PATH` (under `.venv\Lib\site-packages\nvidia\*\bin`).
3. (Recommended) set a shared token so only the pipeline can call it:
   ```
   set FULLPIPE_GPU_TOKEN=<some-secret>
   ```
   Put the same value in the Mac's `config.json` → `asr.gpu_token`.
4. Run:
   ```
   python service.py
   ```
   First request downloads the model (~1–2 GB) and warms CUDA; later requests
   are fast (5 h of audio ≈ 10–15 min on a 2070 Super).

## Config knobs (env vars)

| Var | Default | Notes |
|-----|---------|-------|
| `FULLPIPE_ASR_MODEL` | `kotoba-tech/kotoba-whisper-v2.0-faster` | swap to `large-v3`, a Qwen3-ASR CT2 build, etc. |
| `FULLPIPE_ASR_DEVICE` | `cuda` | `cpu` to test without a GPU |
| `FULLPIPE_ASR_COMPUTE` | `float16` | `int8_float16` / `int8` to cut VRAM |
| `FULLPIPE_ASR_VAD` | `1` | Silero VAD filtering |
| `FULLPIPE_GPU_TOKEN` | *(unset)* | bearer token; unset = no auth |
| `FULLPIPE_GPU_PORT` | `8422` | listen port |

## Mac side

In `config.json`:
```json
"asr": { "engine": "auto", "gpu_url": "http://100.72.37.37:8422", "gpu_token": "" }
```
`auto` prefers this service for Japanese, and transparently falls back to CPU
ReazonSpeech / ElevenLabs when the desktop is off.
