---
name: prepare
description: Run fullPipe Stage 1 locally — the acquire → video-staging → coverage pass the sync server's worker normally does when the phone app enqueues a job. `/prepare <url|file> [...]` enqueues the source(s) and drains the queue synchronously on this machine; bare `/prepare` drains whatever is already queued (e.g. jobs the phone shared while the server was down). Ends at `prepared` — curation stays in `/immerse`. Use for "/prepare", "prep stage one for this", "run stage 1 locally", "download and analyze this without the app", "the server's down, process the queue", or a Japanese video URL paired with queue/download/prepare language (no curation asked).
---

# /prepare — local Stage 1

Runs the mobile pipeline's unattended half (MOBILE.md Stage 1: acquire →
stage 480p video → coverage) on this machine, synchronously, without the sync
server or the app. Everything is deterministic plumbing — the intelligence
(curation) stays in `/immerse`; this skill just drives the drain and reports.
Same queue db, same states, same artifacts as the server's worker, so the
phone picks up the results next sync and `/immerse` sees the jobs as ready.

```
              THIS SKILL (one-shot, local)                     /immerse (later)
sources ──► enqueue ──► acquire ──► video.mp4 ──► coverage ──► [curate …]
                            │           │             │
                        transcript   phone's       exposures (inert)
                        sentences.srt 480p H.264   candidates
```

## Conventions

- `FULLPIPE` = the fullPipe project root — this skill lives at
  `fullPipe/skills/prepare/SKILL.md`; resolve symlinks, then go two
  directories up. On this machine: `~/Documents/code/anki/fullPipe`.
- `PY = $FULLPIPE/.venv/bin/python` (python3.12 — SudachiPy has no 3.14
  wheels; never use the repo-root `.venv`).
- Run every command **from `$FULLPIPE`**.

## Step 0 — preflight

1. **Config.** If `$FULLPIPE/config.json` is missing, stop: tell the user to
   copy `config.example.json` → `config.json` first. Don't invent one.
2. **Anki up.** Coverage computes the known-set via AnkiConnect (cached ~6h
   at `<work_dir>/.known_cache.json`). Run
   `bash $FULLPIPE/skills/scripts/ensure_anki.sh`; if it fails but the cache
   is fresher than `known_words.cache_hours`, continue — otherwise stop and
   tell the user (a wrong known-set poisons the analysis).
3. **Server collision.** If the sync server is up, its worker thread is
   already draining this same queue — a local drain would race it job-by-job.
   Check health at the host/port from config's `server` block (`"host":
   "tailscale"` means the machine's tailnet IP — `tailscale ip -4`):
   `curl -s -m 2 http://$(tailscale ip -4):<port>/health`, falling back to
   `pgrep -fl server.app`. If it's alive, don't drain — enqueue instead
   (`$PY -m server.jobqueue enqueue <src>`) and tell the user the running
   worker will grind it; go local only if they explicitly stop the server
   first (memory: it runs nohup'd — kill the pid).

## Step 1 — enqueue + drain

```sh
$PY -m server.worker "<url-or-file>" [more sources…]   # stdout = JSON summary
$PY -m server.worker                                    # bare: drain what's queued
```

Progress streams on stderr (download %, transcribe, tokenize — relay the
interesting lines); stdout ends with a summary:
`{"enqueued": [...], "prepared": [...], "failed": [...], "skipped": [...]}`.

Semantics (all queue rules, same as the server path):

- Enqueue is **idempotent** — an already-done job comes back in `skipped`
  with a note; a `failed` one silently resets to queued (that IS the retry).
- Jobs run serially (ASR/tokenize are serial by nature); each lands in
  `prepared` or `failed`, video failure alone is non-fatal (audio-only
  sources still prepare — the phone just has no video to pull).
- Long sources take minutes (download + possible transcode + possible ASR).
  Run it in the background and report progress rather than sitting silent.

## Step 2 — report + hand off

One tight block:

- per source: state reached (`prepared`/`failed`/`skipped`), title, episode
  id; for failures the `error` field and whether a retry looks worthwhile
  (yt-dlp gone/region-locked source → ask before retrying).
- coverage headline per prepared episode (read `coverage.json` `stats`):
  sentences, token comprehensibility %, i+1 count, candidates.
- **the hand-off:** jobs now sit in `prepared` — `/immerse` curates them
  (synopsis, glosses, card pool) and the phone app can already see them in
  the queue. Don't start curating here; offer to run `/immerse` next.

## Failure modes

| symptom | meaning | move |
|---|---|---|
| acquire: "poor punctuation and no OPENAI_API_KEY" | choppier sentence merge | warn; artifacts still usable |
| acquire: no subs + no ELEVENLABS_API_KEY + no `FULLPIPE_REAZONSPEECH_DIR` | no transcript possible | job lands `failed`; ask for a key or the offline model dir |
| coverage: AnkiConnect failed after 3 attempts | Anki closed, no fresh cache | `ensure_anki.sh`, re-enqueue (failed → queued) |
| "video unavailable: …" in progress | video staging failed, prep continued | mention it — phone won't have the video, everything else works |
| job `failed` with a yt-dlp error | source gone/region-locked/needs cookies | show the error; retry only if the user says the source is fine |

## Notes

- This is exactly `server/worker.py`'s `process_job` run in the foreground —
  `python -m server.worker` — not a parallel implementation. Fix bugs there.
- Re-running on the same source is safe: downloads cached, exposures deduped,
  done jobs skipped.
- The drain also runs the Stage-2 artifact scan at the end, so episodes
  curated while the server was down flip `curating → staged` for free.
