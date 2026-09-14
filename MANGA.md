# Manga — volumes from the PC library, read on the phone (2026-09-14)

The reading sibling of the video pipeline. A manga volume goes through the
same spine — a sentence track → coverage → any-word popup → ledger
exposures — but the "transcript" is what the desktop GPU reads off the page
scans, and the phone renders it as an invisible, tappable, colour-washed
layer over the original art instead of playing anything. Nothing new lives in
the ledger: manga rows are episodes of kind `manga`, their marks are ordinary
tap batches (encounter mode `manga`), and exposure credit is the same
"the line played" rule — with page pseudo-times, so it reads as "the page was
on screen".

## Topology

```
PC (H:/manga/<Series>/<VOL n (JA)>/NNN.jpg — read-only)
  └─ gpu_service/ocr_volume.py  (mokuro venv, RTX 2070S)
       comic-text-detector → bubble boxes · manga-ocr → text per line
       cache: I:/transcribe/fullpipe_manga/<slug>/vNN/<page>.json (+ _done.json)
Mac (tools/manga.py, run by the worker)
  └─ episodes/manga_<slug>_vNN/
       pages/NNN.jpg      pulled as-is (one tar stream over ssh)
       ocr/NNN.json       mokuro's raw result (kept for rebuilds)
       transcript.json    bubble sentences in reading order, pseudo-timed
       manga.json         per page: file, size, blocks {box, vertical, font_size,
                          lines (chars per printed line), sents (idxs)}
       coverage.json      the usual Stage 1 output (tokens, candidates, exposures)
       curate.json        the /immerse manga pass: defs + synopsis (→ staged)
Phone (mobile: manga.ts · manga-layout.ts · views/manga-reader.ts · Read tab)
  └─ manga/<id>/{manga.json, transcript.json, definitions.json, pages/*}
```

## Identity and lifecycle

- Source `manga://<slug>/<n>` → job/episode id `manga_<slug>_vNN` (derivable
  offline, like `ser_` / `page_`). Rows carry `series`, `series_title`,
  `ep_no` (= volume) — the phone groups volumes under one header in order.
- `queued → downloading` (OCR on the PC, narrated "ocr: page 12/215" on the
  row; then the pulls) `→ tokenizing → prepared`. Readable at `prepared`.
- `/immerse` Step 1.6 (manga pass) writes `curate.json` (defs for names,
  sound words, slang; a synopsis) and the row goes `staged`. No cards, no
  prep doc, no picks, no recommender footprint.
- "Finished" = `POST /watched` from the reader's ✓ (never mints cards), or
  the play-fraction rule once the sittings cover 80 % of the pages.
- `DELETE /jobs/{id}` refuses manga rows without `?force=true` (they carry
  `series`); the phone's swipe-delete is local. `tools.manga remove <slug>
  [--remote]` is the real removal. The scans on the PC are never touched.

## Pseudo-time (why nothing in the ledger changed)

Every bubble sentence gets `start = page_index × 30 + k × 0.1` (k = bubble
order on the page). The reader records a viewtime sitting (kind `read` — a third VIEW_KIND, active like `watch` for exposure credit and the finished marker, tallied on its own on the Progress tab)
whose `played` ranges are page spans `[p×30, (p+1)×30]` for each page that was
on screen ≥ 1 s (capped at 5 min per page, paused in the background), with
`duration = pages × 30`. `ledgerctl.load_coverage` / `_exposure_credit` then
credit exactly the words on the pages read, a revisited page counts twice,
and the Progress tab counts the reading time as immersion. `manga.page_secs`
carries the constant to the phone.

## OCR

mokuro 0.2.5 (`MangaPageOcr`) in `I:/transcribe/mokuro/.venv` (Python 3.11,
torch 2.6+cu124 — transformers 4.x refuses `torch.load` below 2.6;
`transformers<5` — 5.x cannot load manga-ocr's image-processor config).
~3 s/page on the 2070 Super (~10 min per volume). Progress streams over ssh.

Reading order is reconstructed without panel detection
(`tools.manga.sort_blocks`): tiers by vertical overlap top→bottom, columns by
horizontal overlap right→left within a tier, top→bottom within a column. Good
enough for a transcript; the reader itself doesn't depend on it.

manga-ocr reads base text and mostly ignores furigana — but see "The AI
read": since 2026-09-14 its text is a draft only. Measured on the two
test volumes: Dandadan (no furigana) reads cleanly; Dragon Ball Color
(furigana) has ~1 in 5 bubbles with a ruby merged into the text (`どうしや`
for 自動車, `じいっい` for じい). Those are left as OCR misses — the manga
pass skips them — rather than hand-fixed.

## The AI read (2026-09-14, later the same day)

mokuro's text is only a **draft**. The real transcription is done by Opus
subagents reading the pages (`/manga read <id>` — skills/manga/READ_PROMPT.md):

```
tools.manga read-prep   → ocr/read/pages/<stem>.png (every mokuro box outlined +
                          numbered) + ocr/read/manifest.json (boxes + drafts)
Opus agents (5–6 in parallel, ~45 pages each, 10–15 min)
                        → ocr/read/<stem>.json  {blocks: {k: {lines, gloss}}, missed}
tools.manga read-apply  → transcript.json / manga.json rebuilt from the read
                          (empty boxes dropped: watermarks, logos), read/lines.json
                          (bubble gloss → every sentence idx of the bubble),
                          coverage re-run; manga.json read_by = "opus"
```

The reading agent sees the whole page, so it transcribes **and writes the
bubble's plain-English meaning in the same pass** — glosses reflect who is
talking to whom, puns, references. `/transcript` serves `read/lines.json`
glosses under the curate pass's `lines` (overrides only). The `/immerse`
manga pass now requires `read-status … applied: true` and does defs +
synopsis; it keys on sentence idxs, which the read rewrites, so order matters.

Why keep mokuro at all: the detector's boxes are the geometry the overlay
needs, and a vision model can't return pixel-accurate boxes. Its draft is
shown to the agent as a hint, labelled as often wrong.

## The reader (phone)

Behaviours ported from the comicReader app (Kotlin/Compose, the user's
reader): RTL page order by default (per-series toggle), tap zones (far side
in the reading direction turns forward, centre toggles the chrome), swipe to
turn, pinch 1–5× and double-tap 1↔2× about the finger, zoom kept across page
turns, a pull past the edge of a zoomed page turns it, spreads (w/h > 1.2)
fit to width, resume at the last page, page slider. The overlay lays each
bubble's tokens back into its printed lines by character count
(`manga-layout.blockLines`) so the washes sit on the printed words; `T`
shows the OCR text (checking a bubble), `◨` hides the washes. The gloss popup,
mark cycle, lookups and live sync are the player's, unchanged.

## Server API additions

| route | role |
|---|---|
| `GET /manga/library[?refresh=true]` | the PC's manga root: series → volumes (+ queue state); 10-min cache |
| `POST /manga/ingest {remote_dir, volumes?, title?, slug?}` | manifest + enqueue (the Read tab's 📚 picker) |
| `GET /manga/{id}` | `manga.json` |
| `GET /manga/{id}/page/{file}` | one page scan (`media_auth`: header or `?t=`) |

`/transcript`, `/definitions`, `/episodes/{id}/paint`, `/taps`, `/watched`,
`/viewtime` work unchanged (`is_text_kind` covers the no-cards branches).
