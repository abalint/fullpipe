# fullPipe GPU transcription service

A small HTTP service that runs on the **desktop (RTX 2070 Super)** and transcribes
Japanese audio for the Mac pipeline over Tailscale. It is the local, ~$0/min
replacement for YouTube auto-captions and the ElevenLabs cloud API.

It exposes the same word-level output shape the pipeline already consumes
(`engine.transcriber.words_to_srt`), so on the Mac side it's just a third
transcription engine selected via `asr.gpu_url` in `config.json`.

- **Backend:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) on CUDA
- **Recommended model:** `deepdml/faster-whisper-large-v3-turbo-ct2` (set in
  run_service.bat). The code default is still `kotoba-tech/kotoba-whisper-v2.0-faster`,
  but a 2026-07-08 calibration against creator-uploaded subs on 6 immersion-domain
  videos showed kotoba silently drops whole sentences on BGM-heavy content
  (26–44% CER vs YouTube auto-captions' 10–12%); large-v3-turbo scores 4–11%
  (beats auto-captions) at nearly the same speed, with
  `FULLPIPE_ASR_NO_REPEAT_NGRAM=0` (the repetition guard was a distil-model
  workaround and slightly hurts turbo).
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
| `FULLPIPE_ASR_NO_REPEAT_NGRAM` | `3` | forbid repeating N-token spans; curbs distil-model repetition loops (0 disables) |
| `FULLPIPE_GPU_TOKEN` | *(unset)* | bearer token; unset = no auth |
| `FULLPIPE_GPU_PORT` | `8422` | listen port |

## Mac side

In `config.json`:
```json
"asr": { "engine": "auto", "gpu_url": "http://100.x.x.x:8422", "gpu_token": "" }
```
`auto` prefers this service for Japanese, and transparently falls back to CPU
ReazonSpeech / ElevenLabs when the desktop is off.

## Manga OCR (tools/manga.py — MANGA.md)

`ocr_volume.py` is the second thing this box does: OCR a manga volume folder
with [mokuro](https://github.com/kha-white/mokuro) (comic-text-detector for
the speech-bubble boxes + manga-ocr for the text). Since the AI read
(MANGA.md) only the **boxes** matter — the text is a draft that Opus
subagents replace by reading the pages themselves. It is not part of the HTTP
service — the Mac runs it over ssh (`config.json → manga.remote_python /
remote_script`) and streams its progress:

```
I:\transcribe\mokuro\.venv\Scripts\python.exe I:\transcribe\ocr_volume.py "<volume dir>" "<out dir>"
```

Setup (done 2026-09-14): a separate venv, since torch is heavy and the ASR
service needs none of it —

```
"C:\Program Files\Python311\python.exe" -m venv I:\transcribe\mokuro\.venv
I:\transcribe\mokuro\.venv\Scripts\python.exe -m pip install "torch>=2.6" torchvision --index-url https://download.pytorch.org/whl/cu124
I:\transcribe\mokuro\.venv\Scripts\python.exe -m pip install mokuro "transformers<5"
```

Pins that matter: transformers 4.x refuses `torch.load` below torch 2.6
(manga-ocr ships `.bin` weights); transformers 5.x cannot load manga-ocr's
image-processor config. Models land in `HF_HOME=I:\transcribe\hf_cache`
(manga-ocr-base) and mokuro's own cache (comictextdetector.pt). ~3 s per page
on the 2070 Super; output is one JSON per page plus `_done.json`, cached under
`I:/transcribe/fullpipe_manga/<slug>/vNN/` so a re-run is free. The source
folder is only ever read.
