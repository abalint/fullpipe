# Immersion Workstation (fullPipe)

Japanese immersion pipeline: **pre-watch analysis → watch → continuous Anki
review**, all reading and writing one shared known-lemma ledger. Design in
`DESIGN.md`, review findings in `PROPOSALS.md`, mobile client plan in `MOBILE.md`.

**Content discovery** — *what to watch next* — is a separate companion project,
**ytSearch**, at `../../ytSearch/` (repo root `code/ytSearch/`). It harvests the
live Japanese-YouTube graph and ranks picks that feed `/immerse`; fullPipe feeds
back rating/completion signal. See `../../ytSearch/DESIGN.md`.

## Setup

```sh
python3.12 -m venv .venv            # SudachiPy needs ≤3.13
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json  # then edit for your Anki decks
cp .env.example .env                # then add API keys
```

External binaries from PATH: `ffmpeg`, `ffprobe`, `yt-dlp` (brew).

## Layout

```
engine/     vendored pure functions (audioPrimeProd + ankiDeckMaker sources)
  paths.py        ffmpeg/ffprobe/yt-dlp resolution (replaces bin_paths)
  downloader.py   yt-dlp audio+subs / video download (config getters → args)
  local_file.py   local media → mp3 + companion-subtitle discovery
  transcriber.py  ElevenLabs Scribe V2 + ReazonSpeech k2-v2 (offline; set
                  FULLPIPE_REAZONSPEECH_DIR — see PROPOSALS.md P8 spike)
  srt_parser.py   parse · dedup scrolling subs · merge_to_sentences
  punctuation.py  AI punctuation restore (diff-based, insert-only)
  lemma.py        SudachiPy SplitMode C tokenizer + coverage/i+1 analysis (new)
  anki.py         subs2srs deck build w/ stable note guids
  audio.py        ffmpeg slice/loudness/condense helpers
  tts.py          Piper TTS (last resort — audio-forward)
ledger/     the spine (event-sourced; see DESIGN.md — The Ledger)
  schema.sql      lemmas · evidence · episodes · cards · freq · tap_batches
  ledgerctl.py    the eight verbs + promote state machine (CLI below);
                  materialize-known bridges external-lemmatizer forms into
                  Sudachi space (MeCab kana lemmas くる/ところ → 来る/所 via
                  normalized_form) so imported lists join transcripts
  anki_known.py   live Anki known-set via AnkiConnect (~6h cache)
  build_freq.py   P7: show-penetration freq ranks from japaneseShowGraph.db
tools/      dumb CLIs (importable modules + argparse mains)
  acquire.py      source → cleaned subs → punctuation → merged sentences,
                  staged under <work_dir>/episodes/<id>/
  coverage.py     transcript + ledger → classification, ranked candidates
                  (freq_rank·recurrence·leverage columns), records exposures
  deck.py         picks.json → native-audio clips → AnkiConnect push (note ids
                  for lapse polling) or --apkg fallback; registers mined cards.
                  Mints onto the user's own note type via config deck.note_type
                  + field_map (values may be lists — Anki requires the first
                  field non-empty); built-in model otherwise
  jmdict.py       JMdict_e → <work_dir>/jmdict.db (one-off `build`, ~9 MB
                  download) + lemma lookups; feeds GET /definitions — the
                  mobile player's any-word dictionary popup (lemma-keyed, so
                  no deinflection needed)
  stock.py        YouTube-only watchable-stock gauge for /autopilot: staged /
                  to-curate / in-flight hours + the curate/recommend/drain verdict
  render.py       coverage (+curate.json) → self-contained prep.html with
                  furigana throughout (annotate() tokenizes Japanese runs in
                  prose at build time); vocab grid word|reading|usage|english
                  with english masked until tapped; honors curate.json
                  "exclude" (misparse/junk filter)
skills/     /immerse (built) · next: /reconcile · /generate · /replace · /setup
  immerse/SKILL.md        pre-watch orchestrator: acquire → coverage → live
                          curation (synopsis/glosses/focal points/picks) →
                          render → deck; discovered via ../.claude/skills/
  autopilot/SKILL.md      unattended top-up: `/loop 30m /autopilot` measures the
                          YouTube-only watchable stock (tools/stock.py) and,
                          under autopilot.min_hours, curates prepared jobs
                          with parallel Opus subagents (/immerse per episode)
                          and runs /recommend to refill the pipeline
  scripts/ensure_anki.sh  launch Anki + wait for stable AnkiConnect (preflight)
render/     template.html (P9 tap/copy/share loop, ruby furigana, masked
            definitions w/ per-row peek + show-all) + demo-prep.html sample
tests/      unittest suites (44 passing)
```

Deferred vendoring: `interleaver.py` / `m4b.py` (PRIME mode) still live in
audioPrimeProd — they drag in the GUI-coupled translator/video modules; vendor
them when PRIME mode is built.

## ledgerctl

```sh
.venv/bin/python -m ledger.ledgerctl [--db PATH] VERB
```

| verb | what |
|---|---|
| `init` | create the database |
| `materialize-known [--refresh]` | live-Anki-known ∪ ledger-promoted |
| `compute-anki-known [--refresh]` | recompute the live Anki set (cached ~6h) |
| `record-exposure payload.json` | inert exposures for an analyzed episode |
| `mark-watched EPISODE_ID` | activate an episode's exposures (P5) |
| `apply-taps payload.json` | tap batch → implies mark-watched + lapse poll + promote |
| `import-known list.csv` | bulk-seed knowns from an external list (AnkiMorphs export etc.) + promote |
| `promote` | recompute the projection (retunes thresholds for free) |
| `confirm LEMMA` / `defer LEMMA` | answer the exposure prompt: known ('yes') / snooze ('not yet') |
| `query summary\|needs-review\|confirm-queue\|why LEMMA\|unwatched` | read the ledger |

Bootstrap order: `init` → `build_freq` → `compute-anki-known`, plus
`import-known` if you have an external known list (e.g. an AnkiMorphs
known-morphs export — how this install was seeded, 3,046 lemmas). Imports are
strong positive evidence but weaker than a deliberate tap: a fresh
`tap_unknown` demotes one quietly (bulk lists are noisy, no `needs_review`).

## Backups

The ledger is the one artifact that's expensive to lose, so it gets a daily
off-site backup. `tools/backup_ledger.sh` takes a consistent `VACUUM INTO`
snapshot (safe under WAL-mode writes), verifies `integrity_check`, gzips it,
keeps a local copy in `~/immersion/backups/`, and uploads to a cloud remote
via rclone — pruning both sides beyond `KEEP_DAYS` (30).

```sh
rclone config                                    # one-time: make a remote named 'japanese' (or set REMOTE_NAME)
cp server/app.fullpipe.backup.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/app.fullpipe.backup.plist   # daily @ 03:00
tools/backup_ledger.sh                           # run once by hand to verify
```

Restore: `gzcat ~/immersion/backups/ledger-YYYY-MM-DD.db.gz > ledger.db`
(or `rclone copy japanese:fullpipe-backups/ledger-YYYY-MM-DD.db.gz .`). Logs in
`~/immersion/backup.log`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

## Pipeline (what /immerse runs)

```sh
P=.venv/bin/python
$P -m tools.acquire "https://youtu.be/..."     # → episode_id
$P -m tools.coverage EPISODE_ID                # analyze + record exposures
# the /immerse curate pass writes curate.json (synopsis, {word,gloss,note}
# keywords, focal points, exclude junk-filter) + picks.json
# ([{lemma, sentence_idx, reading, english, notes, context}]), then:
$P -m tools.render EPISODE_ID                  # → prep.html (phone)
$P -m tools.deck EPISODE_ID picks.json         # clips + AnkiConnect push
# after watching: apply-taps / mark-watched, then promote
```

## Status (2026-07-05)

**The MINE flow works end-to-end on real episodes.** Scaffold + dumb tools
done (engine vendored on SudachiPy mode C, ledger with P2–P7, freq table from
the real corpus); the `/immerse` skill is written
(`skills/immerse/SKILL.md`, discovered via the repo's
`.claude/skills/immerse` symlink), was smoke-verified offline on a synthetic
episode, and has run one real episode (yt_2LjsOMpzJ8E: 119 sentences → 7
curated native-audio cards pushed to the user's own note type via
deck.note_type/field_map, prep doc with furigana + masked-definition vocab
grid, 482 inert exposures awaiting the watch).

Ledger bootstrap on this install: `import-known` seeded 3,046 lemmas from an
AnkiMorphs known-morphs export ('import' evidence source);
`materialize-known` bridges its MeCab kana lemma forms into Sudachi space via
normalized_form. Known-set undercount (basics the export never carded) is
expected and heals through prep-doc taps.

**Notes:** `CONTENT_POS_PREFIXES` includes 形状詞 (na-adjectives —
sudachidict_core emits it despite the sentence-mining skill's stale comment);
rerun `build_freq` on any db bootstrapped before that fix. Known polish item:
merged sentences keep stray ASCII spaces from subtitle-block joins
(「それで も」) — visible on cards; strip for ja in merge_to_sentences.

**Mobile layer (built 2026-07-05, later that day — MOBILE.md):** `server/`
(FastAPI job queue + ledgerctl verbs over HTTP; Stage-1 worker does
acquire → 480p H.264 video staging → coverage; Stage-2 stays `/immerse`,
detected by artifact watch; launchd plist included; smoke-tested live against
the real workspace) and the Android client at `anki/mobile/` (Capacitor —
queue screen, offline prep viewer with tap outbox, share-sheet enqueue; see
mobile/README.md). Server config block is in config.json (`server.token`
shared with the app). 94 tests passing
(`.venv/bin/python -m unittest discover -s tests`).

**In-app learning player (2026-07-06):** the client plays episodes itself
(WebView video: downloaded file, else server stream) under a subtitle overlay
built from the tokenized transcript via the new `GET /transcript/{id}` (all
coverage.json sentences w/ timing + tokens — /prep only ships the i+1
subset). Words in the subs are tap targets feeding the same tap store as the
prep doc; replay-line/prev/next/speed/furigana/fullscreen, resume position,
prep-doc timestamps deep-link to the moment, VLC handoff kept as fallback.
Same day, tiered word highlighting: /transcript now also carries per-sentence
classification (`cls`), per-token corpus freq rank (`f`), and the ranked
candidate lemmas (`candidates`), feeding the player's text-color-only
highlight tiers (off / focus / learn / all), i+1 line badge + target
underline, and an `Aa` panel with subtitle size + height prefs (see
mobile/README.md — Player).

**Offline-first client (2026-07-06):** downloaded episodes are fully usable
with the server down or the phone off-grid. The client's `⬇` bundle now
includes the prep doc (alongside video/subs/transcript/definitions), prep docs
auto-cache for every staged episode on queue load, and the queue screen falls
back to the last `GET /jobs` snapshot offline. Every client write — tap
batches, mark-watched, ratings, enqueues — rides a typed FIFO outbox flushed
when the server is back (MOBILE.md — Sync semantics); ratings carry a
client-minted `review_id` that `record_rating` now dedupes, so replays never
double-append to the taste log.

**Local Stage 1 (2026-07-05): `/prepare` skill** (`skills/prepare/SKILL.md`,
symlinked like `/immerse`) runs Stage 1 without the app or server —
`python -m server.worker [SOURCE ...]` enqueues the sources and drains the
queue synchronously (same `drain`/`process_job` code as the server thread,
same queue db/states/artifacts), then hands off to `/immerse` at `prepared`.
Bare invocation drains whatever is already queued.

**Series ingest (2026-09-04): `/series` skill** (`skills/series/SKILL.md`,
`tools/series.py`) — already-downloaded box sets on the PC (`E:/Japanese/...`)
become playlists: the desktop transcodes 480p copies (NVENC, originals only
read), the Mac pulls them over the LAN with the Japanese subs, and each
episode is queued as `ser_<slug>_eNN` carrying `series`/`ep_no`. Video is
tiered (phone ⇄ Mac ⇄ PC stage copy) so deleting it anywhere never touches
the transcript/coverage/curation/ledger; the phone groups series in order
and autoplays the next episode. Netflix-style subtitle markup (bidi marks,
speaker tags, dialogue dashes) is now stripped in acquire for every source.

**Taste metadata (2026-07-06):** the per-episode enjoyment signal that feeds
ytSearch (DESIGN.md — Taste metadata). Enjoyment is a *projection over an
append-only `taste_events` log*, mirroring the lemma ledger (evidence →
projection): one review = a `rating` row + a `tag` row per tag, sharing a
`review_id`; re-rating appends a new batch (drift preserved) and the on-read
verdict (`query_enjoyment`) takes the latest. The six tags break the confounds a
scalar can't — `already_knew` · `over_my_head` · `didnt_grab` · `format_miss` ·
`fascinating` · `loved_format`; `over_my_head` decouples the star from the taste
label (`taste_valid=false`). `ledgerctl rate <ep> <1-5|clear> [--tag …]`,
`record-curation`, `query ratings`; `POST /episodes/{id}/rating {rating, tags}`;
ratings + tags annotate `GET /jobs` and ride on `GET /coverage`. Attribution
features land on the `episodes` row too — yt-dlp provenance (channel/duration/
upload_date + description/tags/view_count JSON) from acquire, `{genre, format,
topics, difficulty_felt}` from `/immerse` curation, and a coverage-at-watch
snapshot. App (`anki/mobile/`): stars + the six grouped tag buttons on watched
queue rows and the post-watch prep bar (append-on-tap, multi-select).

**VLC + streaming removed (2026-07-07):** the app is download-then-play only.
Gone: the VLC handoff (ExternalPlayer plugin + queue/player buttons), the
player's stream-from-server fallback, and the queue's "▶ stream" link —
streaming never worked reliably and the whole flow assumes local files on the
phone. Server media endpoints stay (they serve the downloads; ?t= query auth
kept for Filesystem.downloadFile).

**Progress surface + job recovery (2026-07-08, `AUDIT.md` #1/#2):** `GET /stats`
aggregates the ledger — known/learning counts, episodes watched, cards minted,
distinct words encountered, and **frequency-band coverage** (of the top
1k/2k/5k/10k show-penetration lemmas, how many are known) — feeding a new
**Progress** tab in the app (offline-cached like the queue snapshot). Separately,
`jobqueue.reap_stale` (run at every executor startup — server `Worker.run`, CLI
`drain`) reclaims jobs stranded by a crash: dangling Stage-1 states → `queued`,
stranded `pushing` → `watched` with a re-submit-to-retry error, so a wedged row
is neither un-runnable nor un-deletable. `POST /jobs/{id}/retry` re-queues a
failed job (the app's `↻ retry` button). See `AUDIT.md` for the full gap list.

**Confirm-known replaces silent exposure promotion (2026-07-08):** a fuzzy
"met it N times across k episodes" count can't *assert* you know a word, so
`promote` no longer auto-flips exposure-qualified lemmas to `known`. They stay
`learning` with a `confirm_candidate` flag and surface in a confirmation queue —
`GET /confirm` (candidates + JMdict senses + the watched episodes they appeared
in), `POST /confirm {lemma, known}`, `ledgerctl confirm/defer`, `query
confirm-queue`. Answering "yes" appends `confirm_known` (→ known); "not yet"
appends `confirm_defer` (stays learning, snoozed until a fresh qualifying
exposure lands). The app grows a **Confirm words** queue reached from a banner on
the Progress tab. Re-promoting the live ledger moved 85 exposure-only knowns into
the queue (deliberate taps + the AnkiMorphs import are unaffected).

Next: `/reconcile` skill for the offline-blob path (the online path is now
`POST /taps`), `/setup` config interview, teach `/immerse` to consume
`prepared` jobs (skip re-acquire), WorkManager video pull + retention on the
client, then the end-to-end overnight-batch proof. Before corpus-leverage
scoring: re-parse phrases-full.db at mode C (P1); ReazonSpeech word-timestamp
spike before offline alignment code (P8).
