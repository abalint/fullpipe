---
name: series
description: Ingest an already-downloaded TV/anime box set from the media server's library (the Raspberry Pi's `library` share, mounted at /Volumes/library/Japanese/...) into the fullPipe Immersion Workstation as a series playlist. `/series ingest <folder>` transcodes 480p copies on the Mac (VideoToolbox, originals never touched), parks a stage copy on the server's `t7` share, pairs each episode with its Japanese subtitles, enqueues the episodes with series/episode-order identity, and runs Stage 1 — the phone then shows the series grouped in playlist order with autoplay-next; curation stays in `/immerse`. Also `/series list | status <slug> | archive <slug>|--all | fetch <slug> | restore <slug> | evict <slug> [--artifacts] | remove <slug>` for the retention tiers (phone ⇄ Mac ⇄ media server): `archive` mirrors a series' curation artifacts (and 480p copies) onto the media server so the Mac is never the only holder, `evict --artifacts` then frees the Mac, and a phone ⬇ / `fetch` brings everything back. Use for "/series", "ingest this series", "add the drama folder on the media server / the Pi", "set up a playlist for <show>", "free up disk from a watched series", "move <show> onto the media server", "bring back the videos for <show>".
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
   is unreachable the ingest still completes. The curation artifacts get
   their own tier next door (`/Volumes/t7/fullpipe_archive/<slug>/`) —
   see *Archive tier* below;
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

Subtitle discovery when the folder has none — **always do this before
letting Stage 1 ASR anything, no matter how old or obscure the show is**
(user rule, 2026-10-04): the show-graph repo's kitsunekko mirror
(`~/Documents/code/graphs/japaneseShowGraph/subs`, only a handful of shows),
then the two full index pages, which are one curl each and grep in seconds:

```sh
curl -s -A Mozilla/5.0 https://jimaku.cc/ | grep -i -o -E 'href="/entry/[0-9]+"[^>]*>[^<]*<title fragment>[^<]*'
curl -s -A Mozilla/5.0 'https://kitsunekko.net/dirlist.php?dir=subtitles%2Fjapanese%2F' | grep -i '<title fragment>'
```

(grep both the romaji and the katakana title), then SubDL / OpenSubtitles
(both often Cloudflare-walled from curl — a WebSearch for
`<title> 日本語字幕 srt` is the fallback). A hit → pre-place
`<slug>-eNNN.ja.srt` in `~/immersion/series/<slug>/` and ingest (the subs
pre-placement note in memory). Only after all of that comes up empty is GPU
ASR the answer — report the search as done, with what was checked.

## Commands

Folders can be given as they are on the Mac, relative to the series root, or
in the old desktop form — all three resolve to the mount:
`/Volumes/library/Japanese/drama/hotspot` = `drama/hotspot` = `E:/Japanese/drama/hotspot`.

```sh
PY=.venv/bin/python
$PY -m tools.series scan   "drama/hotspot"                               # dry look: episodes + subs pairing
$PY -m tools.series ingest "drama/hotspot" --slug hotspot --title "Hot Spot" [--episodes 1,3-5] [--dry-run] [--no-drain]
$PY -m tools.series ingest "baseball/2026" --sequential --slug pl2026 --title "パ・リーグ 2026"   # no episode numbers in the names (dated games/one-offs): 1..N by date, then name; numbers kept on re-ingest, new files append
$PY -m tools.series list
$PY -m tools.series status hotspot                                       # per-episode state / video on Mac?
$PY -m tools.series archive hotspot | --all [--episodes ...]            # mirror artifacts (+ park 480p copies) onto the media server; never deletes
$PY -m tools.series fetch  hotspot [--episodes 2-4]                      # re-materialize evicted artifacts + videos (archive / stage copy / else re-transcode)
$PY -m tools.series restore hotspot [--episodes ...] [--overwrite]       # artifacts + queue rows back from the archive, no video
$PY -m tools.series evict  hotspot [--episodes ...] [--all] [--artifacts]   # drop Mac video.mp4 (+mp3); watched only unless --all; --artifacts = derived files too, only where the archive is current
$PY -m tools.series remove hotspot [--remote]                            # full delete on the Mac (+ stage copies and archive on t7); never the originals
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
| phone | 480p video + sidecars | swipe-delete a series row = **phone-local only** (server untouched; taps/prep cache kept) | ⬇ on the row / series header — the button reads "restoring…" while the Mac pulls an evicted episode back from the media server, then downloads |
| Mac | `episodes/<id>/video.mp4` (+ `downloads/<id>.mp3`) | `evict` (watched by default) | `fetch`, or automatically when the phone asks `GET /video` (503 + Retry-After, the phone polls) |
| Mac | the derived files: `episodes/<id>/*` (transcript, coverage, curate, prep, picks, clips…), `downloads/<id>.ja.*` | `evict --artifacts` — per episode, **only once the archive is verified current** | `fetch` / `restore`, or automatically when the phone asks for the video, prep, subs or definitions (503, restore, retry) |
| media server `t7` | stage copies `/Volumes/t7/fullpipe_stage/<slug>/` + the artifact archive `/Volumes/t7/fullpipe_archive/<slug>/` | `remove --remote` | videos re-transcoded from the original on demand; the archive is the only other copy of the artifacts — keep it |
| media server `library` | originals under `/Volumes/library/Japanese` | **never** | — |

Derived data is never tied to the video's presence. `DELETE /jobs/{id}`
refuses series rows without `?force=true`; the real removal is `remove`.
The ledger (exposure evidence, marks, ratings) is one SQLite file for
everything, not per series — its off-site copy is the daily ledger backup.

## Archive tier (2026-09-20)

`archive <slug>` mirrors onto the media server everything the Mac derived
for a series: each `episodes/<id>/` minus `video.mp4`, the
`downloads/<id>.ja.srt|.ja.words.json` sidecars, the manifest and srt
beside it, and a `series/queue.json` snapshot of the queue rows (state,
watched, passive) so a restore onto a fresh Mac keeps its progress. It is
a **mirror, never a move**: a re-run copies only files whose size/mtime
changed (re-curation) and deletes nothing. It also **backfills the stage
tier** — a 480p copy on the Mac with no stage copy is parked — which is
what the desktop-era series (hotspot, dorohedoro, edgerunners) needed.

It runs by itself in three places, so the server stays current without
anyone remembering: the sync server archives an episode in the background
the moment it flips to watched (that is when its artifacts are final and
the phone is about to free its copy), `tools/backup_ledger.sh` runs
`archive --all` nightly, and `/immerse` may call it after curating a
series batch. Run it by hand after a big curation pass if you want the
server current now.

`status <slug>` shows the tiers per episode: `video_local` /
`artifacts_local` (Mac) and `staged` / `archived` (media server). The
"move it off the Mac" flow for a finished show is

```sh
$PY -m tools.series archive <slug>              # mirror (idempotent)
$PY -m tools.series evict <slug> --artifacts    # Mac frees video + derived files; unarchived episodes are kept and named
```

and a rewatch later is the phone's ⬇ (or `fetch`): the server restores
artifacts first, then the video, answering 503 + Retry-After meanwhile.

## Phone side (MOBILE.md — Series)

Series rows group under a header (title · n/N watched · m on phone · ▶/⬇ next
unwatched) in playlist order with an EPnn chip; the player shows an **up next**
card when an episode ends and, if the next one is downloaded and Settings →
Autoplay is on, rolls into it after 8 s. Mark-watched stays deliberate.
