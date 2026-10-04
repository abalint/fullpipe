---
name: manga
description: Ingest manga volumes from the media server's library (the Raspberry Pi's `library` share, mounted at /Volumes/library/Japanese/manga/<Series>/<VOL n>/NNN.jpg) into the fullPipe Immersion Workstation as readable, tappable volumes. `/manga ingest <series>` scans the series folder on the mount, enqueues one job per volume, and the worker copies the page scans off the mount, boxes the bubbles on the desktop GPU (mokuro — the pages are parked on the PC over ssh just for that), and runs Stage 1 coverage; then `/manga read <episode_id>` has Opus subagents read every page and gloss every bubble in one pass (mokuro's text is only a draft) — the phone's Read tab then shows the series grouped by volume, and the manga reader lays colour-coded, tappable words over the original pages. Also `/manga voice <episode_id>` (the bubble-by-bubble ElevenLabs voice track the reader plays on a long-press) and `/manga library | scan <series> | list | status <slug> | remove <slug> | read-prep/read-status/read-apply <episode_id>`. Use for "/manga", "ingest this manga", "add <title> from the media server / the manga folder", "set up <manga> for reading", "queue volume N of <manga>".
---

# /manga — volumes from the media server

The page scans live on the Raspberry Pi media server (192.168.0.147), whose
`library` share the Mac mounts as a network drive:
`/Volumes/library/Japanese/manga/<Series>/<VOL n (JA)>/NNN.jpg` (`config.json
→ library.manga_root`, tools/library.py). They are never written to. The Pi
has no GPU, so the desktop still does the boxing: the Mac parks a volume's
pages on it over ssh (`config.json → manga.ocr_ssh_host`), runs mokuro, pulls
the JSON back and deletes the parked copy. This skill drives
`tools/manga.py`, which:

1. **scans** the series folder on the mount (re-mounting the share first if
   it dropped), numbers each sub-folder (`VOL 1 (JA)`, `Vol.01`, `第3巻`,
   `Chapter 12`, a bare number) and counts its page images;
2. **enqueues** `manga://<slug>/<n>` → job/episode id `manga_<slug>_vNN` with
   `series`, `series_title`, `ep_no` (= volume) on the row, so the phone groups
   volumes under one header like a box set;
3. leaves **Stage 1 to the worker** (the running server's, or a local drain):
   `downloading` = the pages copied off the mount to
   `~/immersion/episodes/manga_<slug>_vNN/pages/`, pushed to the PC
   (`I:/transcribe/fullpipe_manga_src/<slug>/vNN/`, one tar stream), OCR there
   (`gpu_service/ocr_volume.py` in the mokuro venv `I:/transcribe/mokuro/.venv`,
   cached under `I:/transcribe/fullpipe_manga/<slug>/vNN/`, narrated page by
   page on the row), the JSON pulled to `…/ocr/` and the parked pages dropped;
   then `transcript.json` (one sentence per bubble sentence, reading order
   right-to-left / top-to-bottom, **pseudo-timed**: start = page index × 30 s)
   and `manga.json` (the reader's page + bubble structure); `tokenizing` =
   coverage; then **`prepared`** — readable on the phone right away.
4. **The AI read (this skill, `/manga read <episode_id>`)** — mokuro's text is a
   draft (it merges furigana into words, misreads katakana, swaps rare kanji).
   Opus subagents read the pages themselves: `read-prep` renders every page
   with its boxes numbered + a manifest, agents write `ocr/read/<stem>.json`
   (printed lines per box **and the bubble's plain-English meaning**, one
   pass — READ_PROMPT.md), `read-apply` rebuilds transcript/manga.json from
   the read (empty boxes dropped) and re-runs coverage. The bubble glosses
   land on `/transcript` sentences right away (the reader shows them at the
   popup's foot).
5. **`/immerse` manga pass (Step 1.6)** takes it to `staged`: defs for what JMdict
   lacks (names, sound words, slang) + a short synopsis. No cards, no prep doc.
6. **The voice track (`/manga voice <episode_id>`, 2026-10-04)** — every bubble as
   a short ElevenLabs clip the reader plays on a long-press / ▶ in the popup.
   The reading agents already wrote who speaks and what the voice should read
   (READ_PROMPT.md `speaker` / `say` / `cast`); `tools.manga voice` compiles the
   script, keeps the series cast → voice map, renders the clips once
   (`tools/manga_voice.py`, MANGA.md "The voice track") and the phone pulls them
   with the volume.

Exposure credit is per **page viewed**: the reader's sittings carry played
ranges in page pseudo-seconds, so the words on the pages you actually read are
what the ledger credits (`exposure = the line played`, unchanged). Taps/marks
work exactly as in the player (encounter mode `manga`).

## Commands

Series folders can be given by name under the manga root, as a Mac path, or
in the old desktop form — all resolve to the mount:
`Dandadan` = `/Volumes/library/Japanese/manga/Dandadan` = `H:/manga/Dandadan`.

```sh
PY=.venv/bin/python
$PY -m tools.manga library                                   # every series + volumes on the media server (+ queue state via GET /manga/library)
$PY -m tools.manga scan   Dandadan                            # volumes that would be ingested
$PY -m tools.manga ingest Dandadan [--slug dandadan] [--title Dandadan] [--volumes 1,3-5] [--dry-run] [--no-drain]
$PY -m tools.manga list
$PY -m tools.manga status dandadan                            # per-volume state / pages on Mac / built?
$PY -m tools.manga archive dandadan|--all                     # mirror reads / voice clips / coverage onto the media server (t7)
$PY -m tools.manga restore dandadan [--volumes 2]             # back onto the Mac: artifacts + pages + queue rows
$PY -m tools.manga remove dandadan [--remote] [--force]       # Mac (+ PC OCR cache and parked pages); never the scans; needs a current archive
```

`--no-drain` (default when the server is up — its worker drains the queue) vs a
local drain (`$PY -m server.worker` / omit `--no-drain`) when the server is down.
Per ~200-page volume expect ~10 s copy off the mount, ~15 s push to the PC,
~10 min OCR on the 2070 Super (first run of a session also loads the models),
~30 s pull, ~30 s coverage. Volumes are ~60 MB of jpg each; the phone pulls
every page on ⬇. Both the media server and the PC must be on for a new
volume; a re-run with the OCR cache already local needs neither.

The phone can do the same without you: Read tab → **📚 library** lists the
manga root with queue state per volume (one walk of the mount, ~10 s, cached
10 min); **＋ queue** / **queue all** call `POST /manga/ingest`.

Volumes ingested from the desktop era keep working: their manifests still say
`H:\manga\…`, and `tools/library.py` maps that prefix onto the mount.

## The AI read — procedure (`/manga read <episode_id>`)

Run it on every volume once it is `prepared`, before the `/immerse` pass
(the read rewrites the sentence track, so anything keyed on sentence idx
must come after it).

```sh
$PY -m tools.manga read-prep   manga_dandadan_v01     # → {to_read: [stems…]}
$PY -m tools.manga read-status manga_dandadan_v01
$PY -m tools.manga read-apply  manga_dandadan_v01     # refuses under 95 % read
```

1. `read-prep`, then split `to_read` into batches of ~40–45 pages and launch
   one **Opus** subagent per batch (general-purpose, in parallel, 5–6 at a
   time): "Read `skills/manga/READ_PROMPT.md` and follow it for episode
   `<id>` (EPISODE_DIR = `~/immersion/episodes/<id>`), pages: <stems>." Each
   agent reads ~45 pages in 10–15 min.
2. When all report done, `read-status` → `read` should equal
   `pages_with_text` (re-launch an agent over any stems still missing), then
   `read-apply`. Skim a few pages' JSON against their PNG.
3. Hand off to `/immerse` Step 1.6 (defs + synopsis; its `lines` are
   overrides only now).

## The voice track — procedure (`/manga voice <episode_id>`)

After `read-apply` (the script rides on the read). Needs `ELEVENLABS_API_KEY`
(.env or the macOS keychain, `security add-generic-password -a "$USER" -s
ELEVENLABS_API_KEY -w`) with the text_to_speech + voices_read + user_read scopes.

```sh
$PY -m tools.manga voice prep   manga_dandadan_v02    # → {to_script: [stems…]} — [] when the read carried the fields
$PY -m tools.manga voice apply  manga_dandadan_v02    # script.json + cast merge → prints the cast (+ new names)
$PY -m tools.manga voice cast   dandadan              # the series cast; --set モモ=<voice_id> / --unset
$PY -m tools.manga voice voices --language ja         # the account's voices (add library voices on the site)
$PY -m tools.manga voice tts    manga_dandadan_v02 --dry-run   # clips / chars / credits by voice
$PY -m tools.manga voice tts    manga_dandadan_v02 [--pages 1-10]
$PY -m tools.manga voice status manga_dandadan_v02
```

1. `prep`. If `to_script` is non-empty (a volume read before the voice fields
   existed), split it into batches of ~40 pages and launch one Opus subagent
   per batch: "Read `skills/manga/VOICE_PROMPT.md` and follow it for episode
   `<id>` (EPISODE_DIR = `~/immersion/episodes/<id>`), pages: <stems>." —
   text only, so ~5 min a batch.
2. `apply`. Look at the cast it prints: every new name needs a `gender`
   (and `age` / `dialect` when they matter — the default voice is picked by
   the most specific `default_voices` key: `female:old`, `male:kansai`,
   `female`, …) and, for a main character, its own `voice_id` (`cast --set`).
3. `tts --dry-run`, then `tts`. A 200-page volume is ~1,500 requests, 5–10
   min at 3 in flight; `--pages` for a taste first. Re-running after a cast
   or script change renders only what changed.
4. The phone picks the track up on the next reader open (status line
   "voice n/N" while it pulls); a re-render replaces it.

## Procedure

1. **Scan first** (`scan` or `ingest --dry-run`) and show the volume table;
   confirm slug/title when the folder name is odd. A folder with no parseable
   number (`extras`, `omake`) is skipped and listed.
2. **Trial one volume** (`--volumes 1`) for a new series; when it's `prepared`,
   skim `transcript.json` for OCR quality. Furigana editions read a little
   worse (manga-ocr mostly ignores rubies but sometimes merges one into the
   base text — `どうしや` for `自動車`); note it in the manga pass rather than
   hand-fixing bubbles.
3. **Ingest the rest**; report when the queue shows them `prepared`, then hand
   off to `/immerse` (Step 1.6).
4. The server must run the current code for `/manga/*` routes (restart after
   pulling changes).
5. "could not mount smb://pi@192.168.0.147/library" = the Pi is off or the
   keychain login is gone: `open smb://192.168.0.147` in Finder, log in as
   `pi`, tick "remember in keychain", mount `library`.

## Retention

| where | what | reclaim | restore |
|---|---|---|---|
| phone | page scans + sidecars under `manga/<id>/` | swipe-delete a volume row = **phone-local only** | ⬇ on the row / series header |
| Mac | `episodes/manga_<slug>_vNN/{pages,ocr,read,voice,…}` | `remove` (refuses unless archived + current; `--force`) | `restore <slug>` — artifacts from t7, pages off the share, queue rows |
| media server (t7) | `fullpipe_archive/manga/<slug>/` — reads (with the voice fields), voice clips, coverage, curate, manifest, cast, queue snapshot | never | `archive <slug>|--all` writes it; also on read/watched, after a `voice tts`, and nightly |
| PC | OCR cache `I:/transcribe/fullpipe_manga/<slug>/` (+ parked pages under `fullpipe_manga_src/` while a job runs) | `remove --remote` | re-OCR'd on demand |
| media server | scans under `/Volumes/library/Japanese/manga` | **never** | — |

`DELETE /jobs/{id}` refuses manga rows without `?force=true` (they carry
`series`), same as box-set episodes.
