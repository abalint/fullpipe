---
name: series
description: Ingest an already-downloaded TV/anime box set from the PC's library (E:/Japanese/...) into the fullPipe Immersion Workstation as a series playlist. `/series ingest <pc-folder>` transcodes 480p copies on the desktop (NVENC, originals never touched), pulls them to the Mac, pairs each episode with its Japanese subtitles, enqueues the episodes with series/episode-order identity, and runs Stage 1 — the phone then shows the series grouped in playlist order with autoplay-next; curation stays in `/immerse`. Also `/series list | status <slug> | fetch <slug> | evict <slug> | remove <slug>` for the video retention tiers (phone ⇄ Mac ⇄ PC). Use for "/series", "ingest this series", "add the drama folder on the PC", "set up a playlist for <show>", "free up disk from a watched series", "bring back the videos for <show>".
---

# /series — box sets from the PC library

The originals live on the Windows desktop under `E:/Japanese/...` (drama /
anime / videos). This skill drives `tools/series.py`, which:

1. **scans** the PC folder over ssh (LAN address, `config.json → series`),
   parses episode numbers (EP01 / S01E05 / 第3話 / "Show - 07" …) and pairs each
   video with its Japanese subtitle (`.Jpn.srt` sidecar; else a text subtitle
   track inside the container; else nothing → Stage 1 ASRs it on the GPU box);
2. **transcodes a 480p H.264 copy on the PC** (NVENC, ~25× realtime) into the
   PC's stage dir (`I:/transcribe/fullpipe_stage/<slug>/`). ffmpeg only reads the
   original — nothing under the library folder is ever written or deleted;
3. **pulls** the copy + subs to the Mac (`~/immersion/episodes/<id>/video.mp4`,
   `~/immersion/series/<slug>/<slug>-eNN.ja.srt`) and writes the manifest
   `~/immersion/series/<slug>/series.json`;
4. **enqueues** `series://<slug>/<n>` → job/episode id `ser_<slug>_eNN` with
   `series`, `series_title`, `ep_no` on the row, and lets the worker run
   Stage 1 (the running server's worker picks it up; `--no-drain` off = drain
   locally too).

Everything from `prepared` on is the normal flow: `/immerse` curates (run the
punctuation gate — Netflix-style subs have no 。), the phone pulls, taps,
marks watched, rates, debriefs.

## Commands

```sh
PY=.venv/bin/python
$PY -m tools.series scan   "E:/Japanese/drama/hotspot"                  # dry look: episodes + subs pairing
$PY -m tools.series ingest "E:/Japanese/drama/hotspot" --slug hotspot --title "Hot Spot" [--episodes 1,3-5] [--dry-run] [--no-drain]
$PY -m tools.series list
$PY -m tools.series status hotspot                                       # per-episode state / video on Mac?
$PY -m tools.series fetch  hotspot [--episodes 2-4]                      # re-pull evicted videos from the PC
$PY -m tools.series evict  hotspot [--episodes ...] [--all]              # drop Mac video.mp4 (+mp3); watched only unless --all
$PY -m tools.series remove hotspot [--remote]                            # full delete on the Mac (never the originals)
```

Ingest is idempotent: re-running skips stage copies, local videos and queue
rows that already exist, so an interrupted run just resumes. A long series
is best run in the background (`nohup … &`, log to a file) — per 45-min
episode expect ~30 s transcode + ~30 s LAN pull + ~30 s Stage 1.

## Procedure

1. **Scan first** and show the user the pairing table (label · file · subs).
   Confirm slug/title when the folder name is cryptic (`hotspot` → "Hot
   Spot"). Watch for: two videos with the same number (`duplicates`), files
   with no parseable number (`unparsed`), subs `—` (will ASR — needs the GPU
   service up), dual-audio anime (Japanese audio track is auto-picked).
2. **Trial one episode** (`--episodes 1`) the first time a folder shape is
   new; check `status` shows `prepared` and skim `transcript.json`.
3. **Ingest the rest** in the background; report when the queue shows them
   `prepared`, then hand off to `/immerse`.
4. The server must be running the current code for the phone to see series
   fields (restart it after pulling changes).

## Retention tiers (the point of the design)

| where | what | reclaim | restore |
|---|---|---|---|
| phone | 480p video + sidecars | swipe-delete a series row = **phone-local only** (server untouched; taps/prep cache kept) | ⬇ on the row / series header |
| Mac | `episodes/<id>/video.mp4` (+ `downloads/<id>.mp3`) | `evict` (watched by default) | `fetch`, or automatically when the phone asks `GET /video` (503 "restoring", retry) |
| PC | stage copies `I:/transcribe/fullpipe_stage/<slug>/` | `remove --remote` | re-transcoded from the original on demand |
| PC | originals under `E:/Japanese` | **never** | — |

Derived data (transcript, coverage, curate, prep, picks, clips, ledger
evidence, cards) is never tied to the video's presence. `DELETE /jobs/{id}`
refuses series rows without `?force=true`; the real removal is `remove`.

## Phone side (MOBILE.md — Series)

Series rows group under a header (title · n/N watched · m on phone · ▶/⬇ next
unwatched) in playlist order with an EPnn chip; the player shows an **up next**
card when an episode ends and, if the next one is downloaded and Settings →
Autoplay is on, rolls into it after 8 s. Mark-watched stays deliberate.
