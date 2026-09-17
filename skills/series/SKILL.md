---
name: series
description: Ingest an already-downloaded TV/anime box set from the media server's library (the Raspberry Pi's `library` share, mounted at /Volumes/library/Japanese/...) into the fullPipe Immersion Workstation as a series playlist. `/series ingest <folder>` transcodes 480p copies on the Mac (VideoToolbox, originals never touched), parks a stage copy on the server's `t7` share, pairs each episode with its Japanese subtitles, enqueues the episodes with series/episode-order identity, and runs Stage 1 — the phone then shows the series grouped in playlist order with autoplay-next; curation stays in `/immerse`. Also `/series list | status <slug> | fetch <slug> | evict <slug> | remove <slug>` for the video retention tiers (phone ⇄ Mac ⇄ media server). Use for "/series", "ingest this series", "add the drama folder on the media server / the Pi", "set up a playlist for <show>", "free up disk from a watched series", "bring back the videos for <show>".
---

# /series — box sets from the media server

The originals live on the Raspberry Pi media server (192.168.0.147,
`mediaserver`), whose `library` share the Mac mounts as a network drive:
`/Volumes/library/Japanese/{anime,drama,videos,…}` (`config.json → library`,
tools/library.py). The desktop is no longer involved — it is only the GPU
box for ASR and manga boxing. This skill drives `tools/series.py`, which:

1. **scans** the folder on the mount (re-mounting the share first if it
   dropped — the login is in the keychain), parses episode numbers (EP01 /
   S01E05 / 第3話 / "Show - 07" …) and pairs each video with its Japanese
   subtitle (`.Jpn.srt` sidecar; else a text subtitle track inside the
   container; else nothing → Stage 1 ASRs it on the GPU box);
2. **transcodes a 480p H.264 copy on the Mac** (VideoToolbox, libx264
   fallback) straight off the mount into `~/immersion/episodes/<id>/video.mp4`,
   with the Japanese `.srt` beside the manifest
   (`~/immersion/series/<slug>/<slug>-eNN.ja.srt`). ffmpeg only reads the
   original — nothing under the library share is ever written or deleted.
   The SMB read (~13 MB/s) is the bottleneck, not the encoder: a 1 GB
   original takes ~1.5 min;
3. **parks a copy** of the mp4 + srt in the stage dir on the server's
   writable `t7` share (`/Volumes/t7/fullpipe_stage/<slug>/`) so a later
   `fetch` after an `evict` is a copy, not a transcode. Best-effort: if t7
   is unreachable the ingest still completes;
4. **enqueues** `series://<slug>/<n>` → job/episode id `ser_<slug>_eNN` with
   `series`, `series_title`, `ep_no` on the row, and lets the worker run
   Stage 1 (the running server's worker picks it up; `--no-drain` off = drain
   locally too).

Everything from `prepared` on is the normal flow: `/immerse` curates (run the
punctuation gate — Netflix-style subs have no 。, and ASR-only shows arrive
as long run-on segments; the repair gate matters most there — a show's own
jargon and character names get mangled the same way in every episode, and
the cross-episode registry carries the fixes forward), the phone pulls,
taps, marks watched, rates.

Subtitle discovery order when the folder has none: the show-graph repo's
kitsunekko mirror (`~/Documents/code/graphs/japaneseShowGraph/subs`), then
jimaku.cc / kitsunekko.net / SubDL / OpenSubtitles online. Older OVAs and
anything never on a JP streaming service usually have none — go straight to
GPU ASR rather than searching long.

## Commands

Folders can be given as they are on the Mac, relative to the series root, or
in the old desktop form — all three resolve to the mount:
`/Volumes/library/Japanese/drama/hotspot` = `drama/hotspot` = `E:/Japanese/drama/hotspot`.

```sh
PY=.venv/bin/python
$PY -m tools.series scan   "drama/hotspot"                               # dry look: episodes + subs pairing
$PY -m tools.series ingest "drama/hotspot" --slug hotspot --title "Hot Spot" [--episodes 1,3-5] [--dry-run] [--no-drain]
$PY -m tools.series list
$PY -m tools.series status hotspot                                       # per-episode state / video on Mac?
$PY -m tools.series fetch  hotspot [--episodes 2-4]                      # re-materialize evicted videos (stage copy, else re-transcode)
$PY -m tools.series evict  hotspot [--episodes ...] [--all]              # drop Mac video.mp4 (+mp3); watched only unless --all
$PY -m tools.series remove hotspot [--remote]                            # full delete on the Mac (+ stage copies on t7); never the originals
```

Ingest is idempotent: re-running skips local videos, stage copies and queue
rows that already exist, so an interrupted run just resumes. A long series
is best run in the background (`nohup … &`, log to a file) — per 45-min
1080p episode expect ~2 min read+transcode + ~30 s stage copy + ~30 s Stage 1.

Series ingested from the desktop era keep working: their manifests still say
`E:\Japanese\…`, and `tools/library.py` maps that prefix onto the mount
(`library.legacy_roots`), so `fetch` re-transcodes from the same original
on the Pi.

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
5. If a command fails with "could not mount smb://pi@192.168.0.147/…", the
   Pi is off or the keychain login is gone: `open smb://192.168.0.147` in
   Finder, log in as `pi`, tick "remember in keychain", mount `library`.

## Retention tiers (the point of the design)

| where | what | reclaim | restore |
|---|---|---|---|
| phone | 480p video + sidecars | swipe-delete a series row = **phone-local only** (server untouched; taps/prep cache kept) | ⬇ on the row / series header |
| Mac | `episodes/<id>/video.mp4` (+ `downloads/<id>.mp3`) | `evict` (watched by default) | `fetch`, or automatically when the phone asks `GET /video` (503 "restoring", retry) |
| media server `t7` | stage copies `/Volumes/t7/fullpipe_stage/<slug>/` | `remove --remote` | re-transcoded from the original on demand |
| media server `library` | originals under `/Volumes/library/Japanese` | **never** | — |

Derived data (transcript, coverage, curate, prep, picks, clips, ledger
evidence, cards) is never tied to the video's presence. `DELETE /jobs/{id}`
refuses series rows without `?force=true`; the real removal is `remove`.

## Phone side (MOBILE.md — Series)

Series rows group under a header (title · n/N watched · m on phone · ▶/⬇ next
unwatched) in playlist order with an EPnn chip; the player shows an **up next**
card when an episode ends and, if the next one is downloaded and Settings →
Autoplay is on, rolls into it after 8 s. Mark-watched stays deliberate.
