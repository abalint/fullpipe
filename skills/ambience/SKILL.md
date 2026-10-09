---
name: ambience
description: Background sound for reading in the fullPipe Immersion Workstation — looping ambience (rain, waves, fire, noise…) and mood music playlists, built on the Mac from YouTube audio (tools/ambience.py) and played by the phone under the manga reader (or anything) with a volume per layer. `/ambience` reports the library; `/ambience add sound <what>` / `/ambience add music <mood> <what>` source new entries — YOU search YouTube (the /recommend search edge), judge the candidates (long, no narration, well-listened, no music under a "nature" sound), and cut/encode them; `/ambience remove <id>`. Use for "/ambience", "add a rain sound", "add a <mood> playlist", "more music for reading", "background sound for the reader", "white noise in the app", "what ambience do I have".
---

# /ambience — sound under the reading

The phone's ♫ button (manga reader top bar, Read and Listen tabs) opens a
sheet: every **sound** is a row you switch on and set a level for — layer as
many as you like (rain + fire) — and every **mood** is a chip that puts one
music channel on shuffle. The mix is remembered; a sound or mood downloads
to the phone the first time it's switched on. Music ducks while a bubble's
voice clip speaks; loops don't. Nothing here touches the ledger.

The library lives in `~/immersion/ambience/` (`catalog.json` + `sounds/` +
`music/<mood>/`), served by `GET /ambience` and `/ambience/<file>`.
`tools/ambience.py` builds it:

| verb | what |
|---|---|
| `probe <url\|id> [--kind thunder\|waves] [--keep DIR]` | measure a candidate: loudness floor, transients per 30 min + busiest window (thunder), swell depth + wave period (waves) |
| `search "<query>" [-n N] [--min-minutes M]` | YouTube candidates (unauthenticated yt-dlp search — the same edge `/recommend` uses) as `{video_id, title, channel, minutes, views, url}` |
| `sound add <url\|id> --id <slug> --title <名前> [--emoji 🌧] [--start S] [--seconds L] [--from-file F]` | cut a loop: full audio pulled (fast), `[start, start+L]` cut, the tail crossfaded into the head (seamless), Opus **128k**, one measured gain to −22 LUFS (never dynamic loudnorm). Default 30 min |
| `sound gen white\|pink\|brown` | synthesized noise, same seam treatment, no download |
| `mood add <slug> --title "<Title>" --emoji 🎧` | create / retitle a mood |
| `music add <mood> <url\|id\|playlist\|channel> [--limit N]` | every video behind the source becomes one audio-only track (Opus 96k, one measured gain to −16 LUFS); already-present videos are skipped |
| `remove <id> [--track <video_id>]` | a sound, a mood, or one of its tracks (files go too) |
| `list` · `rebuild` | the catalog summarized · re-probe files, drop missing ones |

`PY=$FULLPIPE/.venv/bin/python`, run from `$FULLPIPE`: `$PY -m tools.ambience …`.
Config: `ambience.sound_seconds` (1800), `xfade`, bitrates, loudness targets.

## Bare `/ambience`

`$PY -m tools.ambience list` → report sounds (id · title · minutes · MB) and
moods (id · title · tracks · hours · MB), one line each. Mention the phone
pulls on demand, so size on the Mac ≠ size on the phone.

## `/ambience add sound <what>` — e.g. "add a fireplace sound", "add ocean"

1. **Search in Japanese first, then English**, long form: the ambience
   channels title for sleep — `<thing> 睡眠 10時間`, `<thing> 環境音 作業用`,
   `<thing> sounds 8 hours`. Run 2–3 queries with `--min-minutes 120`:
   `$PY -m tools.ambience search "焚き火 音 睡眠 10時間" -n 8 --min-minutes 120`.
2. **Judge** — the sound must be the thing itself, nothing else:
   - **no music bed** (titles with 音楽 / BGM / 528Hz / ヒーリング / piano
     alongside the nature word usually have one — skip), no narration, no
     ASMR whispering, no "途中広告" jingles mid-stream;
   - **no thunder unless asked** (`雷なし` is a plus for plain rain);
   - prefer **≥ 2 h** (a short video loops with a visible seam in the
     source itself) and **well-listened** (views are the only quality
     signal you have — a million-plus is a safe pick, under ten thousand
     needs a reason);
   - channels seen to be clean: 熟睡BGM, SST Nature Sounds, Forest of wing,
     オトノモウフ, 癒しの水の音ch, 癒しの自然音・環境音チャンネル, VICOMINC
     (trains), Black Screen Ambient Sounds (blizzard).
3. **Probe before you cut — titles lie** (2026-10-08: an "intense
   thunderstorm" had no thunder in its first hour; an "ocean waves crashing"
   was a distant wash the user called nothing like the ocean). For each
   shortlisted candidate: `$PY -m tools.ambience probe <id> --keep
   /tmp/amb-cand` → `floor_lufs`, `depth_db`, and
   - thunder: `events_per_30m` (claps over the rain floor; **~20–50** is a
     present storm, < 12 reads as "no thunder", > 100 is a continuous
     rumble/gust artefact), `busiest_start_s` (cut from there);
   - waves: `swell_db` (**≥ 7** = surf you can hear breaking; < 5 = a flat
     wash), `wave_period_s` 8–15 with `periodicity` ≥ 0.3 = real sets.
   Pick by the numbers, then `sound add <id> … --start <busiest_start_s>
   --from-file /tmp/amb-cand/<id>.webm` (no second download). Measured
   2026-10-08: 雷雨 = gVKEM4K8J8A (≈50/30 min), 嵐 = f0tpROu3hr0 (≈18,
   torrential, real), 海 = WHPEKLQID4U (8.8 dB swells, 13 s), 浜辺 =
   vPhg6sc1Mk4 (7.6 dB, 9 s); rejected bn9F19Hi1Lk / JekUNGo-RVk /
   r89PrCE7tMU (flat), Gz-jBbuqHZg / bEICXUZReDM (gust/rumble artefacts).
4. **Cut**: `--start 300` skips intro fades/jingles; keep the default 30 min
   unless the user asks for longer. Japanese `--title` (the phone shows
   it), an emoji. Slug ids: `rain`, `heavy-rain`, `fire`.
5. Tell the user what was added (title · source channel · 30 min · MB) and
   that it appears on the phone after **↻ sync library** in the sheet.

## `/ambience add music <mood> <what>` — e.g. "add a jazz mood", "more lo-fi"

1. If the mood is new: `mood add <slug> --title "<Title>" --emoji <e>`.
   Existing moods (2026-10-08 seed): focus 🎧 · calm 🌙 · jazz ☕ ·
   ghibli 🍃 · citypop 🌆 · upbeat ⚡ · classical 🎻 · game 🎮.
2. **Search** `作業用BGM <genre> 1時間`, `<genre> mix 作業用`, `<genre>
   playlist` with `--min-minutes 40`. Judge: **continuous mixes** (one
   video = one track of 1–4 h; the phone shuffles tracks, so three mixes a
   mood is plenty); skip anything with talking (radio, VTuber chatter —
   a hololive "chill mix" is fine, a 雑談 is not), pomodoro timers with
   bells, and `ゆっくり`/VOICEROID content (the user cannot stand those
   voices). Loudness is matched on encode, so a quiet piano mix and a loud
   EDM one sit at the same slider.
3. `music add <mood> <video_id>` per pick (a playlist URL works too, with
   `--limit`). Each 1 h of audio is ~45 MB on the Mac and the phone.
4. Report the tracks added and the mood's total hours.

## `/ambience remove <id>`

`$PY -m tools.ambience remove <id>` (or `--track <video_id>` for one mix).
The phone keeps its downloaded copy until the user deletes it from the
sheet's storage line or Settings — say so.

## Hazards

- A sound loop made from a video that was itself a short loop carries the
  source's seam; prefer true long recordings.
- `--download-sections` seeks the stream — if yt-dlp fails on a video with
  "requested format not available", retry without `--start` (full pull) or
  pick another source.
- Never route ambience through `tools.harvest` storage or `/recommend`'s
  speech gate (`probe_speech` labels music/ambience `silent` and drops it);
  `tools.ambience search` writes nothing.
