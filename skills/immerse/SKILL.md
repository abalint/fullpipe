---
name: immerse
description: Curation session for the fullPipe Immersion Workstation. Bare `/immerse` reviews the mobile job queue — reports what the worker is still preparing, surfaces failures, and runs the live curate pass (synopsis, key-vocab glosses, focal points, ~15-card native-audio shortlist → Anki push + phone prep doc) over every episode that's ready, asking the user what to curate when there's a choice. `/immerse <url|file>` handles one source directly, end to end (acquire → coverage → curate → render → deck). Writes exposure evidence to the known-lemma ledger (inert until watched). Use for "/immerse", "curate the queue", "what's ready to curate", "/immerse <url|file>", "prep this episode", "analyze this before I watch it", or any Japanese video URL paired with a mention of prep / pre-watch / immersion.
---

# /immerse — curation orchestrator

Chains the dumb tools (`acquire → coverage → render → deck`) and supplies the
intelligence between them: **you** read the actual episode and do the curation.
Deterministic plumbing stays in the tools; judgment stays here (DESIGN.md —
Skill topology). Since the mobile layer (MOBILE.md), the front half usually
already ran unattended — the worker drains the phone's queue through
acquire + coverage to `prepared` — so the default entry point is **review the
queue and curate what's ready**, not process one URL.

```
                        STAGE 1 (worker, unattended)      STAGE 2 (this skill, live)
queue ──► acquire ──► coverage ──────────────────────► [YOU: curate] ──► render ──► deck
             │             │                                 │              │          │
         transcript    exposures (inert)                curate.json     prep.html   cards
         sentences.srt candidates                       picks.json      (phone)     → Anki
```

## Conventions

- `FULLPIPE` = the fullPipe project root — this skill lives at
  `fullPipe/skills/immerse/SKILL.md`; resolve symlinks, then go two directories
  up. On this machine: `~/Documents/code/anki/fullPipe`.
- `PY = $FULLPIPE/.venv/bin/python` (python3.12 — SudachiPy has no 3.14 wheels;
  never use the repo-root `.venv`).
- Run every command **from `$FULLPIPE`** (`python -m tools.…`, `python -m server.…`).
- Per-episode artifacts live under `<work_dir>/episodes/<episode_id>/`
  (work_dir from config.json): `transcript.json`, `sentences.srt`,
  `coverage.json`, `curate.json`, `picks.json`, `prep.html`, `clips/`, `deck.apkg`.
- Queue CLI: `$PY -m server.jobqueue list | enqueue SOURCE | set-state ID STATE`.

## Step 0 — preflight

1. **Config.** If `$FULLPIPE/config.json` is missing, stop: tell the user to
   copy `config.example.json` → `config.json` and adjust decks / known-word
   sources (the `/setup` interview will eventually own this). Don't invent one.
2. **First-ever run** (no ledger db at the config's `ledger_db` path):
   ```sh
   $PY -m ledger.ledgerctl init
   $PY -m ledger.build_freq          # ~85s; show-penetration freq table (P7)
   ```
   If the user has an external known-word list (e.g. an AnkiMorphs
   known-morphs export), seed it too:
   `$PY -m ledger.ledgerctl import-known list.csv` (idempotent; MeCab kana
   lemma forms are bridged to Sudachi automatically at materialize).
3. **Anki up.** Coverage recomputes the live Anki known-set via AnkiConnect
   (cached ~6h under `<work_dir>/.known_cache.json`). Run
   `bash $FULLPIPE/skills/scripts/ensure_anki.sh`. If it fails **but** the
   known-cache is fresher than `known_words.cache_hours`, continue (coverage
   will hit the cache); otherwise stop and tell the user — the known-set is the
   core of the analysis, don't run with a wrong one.
4. **Keys** (from `$FULLPIPE/.env`, loaded automatically): `OPENAI_API_KEY`
   is the worker's unattended punctuation restore — **in this live skill you
   punctuate with a subagent instead** (Step 2.5), so the key is irrelevant
   here; `ELEVENLABS_API_KEY` only when there are no usable subs at all (ASR).
   Don't preflight these — acquire degrades loudly (see Failure modes) and most
   YouTube sources have subs.

## Step 1 — take stock of the queue (default entry)

```sh
$PY -m server.jobqueue list
```

(A missing/empty queue is normal on a PC-only setup — skip to Step 2 with the
user's URL.) Group jobs by state and present the whole picture in one compact
block, then decide together what this session does:

| state | meaning | your move |
|---|---|---|
| `prepared` · `curating` | Stage 1 done — **ready for live curation** | curate it (Steps 3–6) |
| `queued` · `downloading` · `transcribing` · `tokenizing` | worker still grinding | report progress (`progress_msg`); don't touch |
| `staged` | curated, phone can pull | nothing to do — mention it |
| `watched` · `reconciled` | loop closed | nothing to do |
| `failed` | Stage 1 blew up | show `error`; **ask** whether to retry (re-`enqueue` the same source resets it to queued) or drop it |

**Ask, don't assume** (AskUserQuestion when interactive):

- Several episodes ready → which to curate now, one / a subset / all? (Default
  suggestion: all of them — that's the morning batch.)
- A `failed` job → retry or ignore?
- Nothing ready and nothing in flight → does the user want to enqueue
  something (`$PY -m server.jobqueue enqueue <url>`) for the worker, or hand
  you a URL to run directly (Step 2)?
- Worker not running (jobs sit in `queued` and the server isn't up) → offer:
  drain the queue locally right now (`$PY -m server.worker` — the /prepare
  skill's one-shot Stage 1; same states and artifacts, then curate what it
  prepares), or tell them how to start the server
  (`.venv/bin/python -m server.app` / launchd).

For each episode chosen: `$PY -m server.jobqueue set-state <id> curating`,
then run the **punctuation gate (Step 2.5)** — a worker that ran without
`OPENAI_API_KEY` leaves choppy, unpunctuated sentences you should fix before
reading — and only then continue with Step 3 (its transcript + coverage already
exist). Curate episodes **one at a time**, completing each through Step 6
before the next.

## Step 2 — acquire + coverage (direct mode / missing artifacts only)

Skip this entirely when `<episode_dir>/transcript.json` **and**
`coverage.json` already exist (the worker's Stage 1 output). Otherwise:

```sh
$PY -m tools.acquire "<url-or-file>" --no-punct-restore   # stdout = episode_id
# → run the punctuation gate (Step 2.5) here, BEFORE coverage —
#   re-segmenting changes sentence indices coverage depends on
$PY -m tools.coverage EPISODE_ID            # stdout = path to coverage.json
```

`--no-punct-restore` skips acquire's OpenAI restore on purpose: when subs lack
punctuation you restore it with a subagent (Step 2.5), which reads better than
`gpt-4o-mini` and needs no key. (Drop the flag only if you deliberately want
the cheap OpenAI pass — e.g. batch-acquiring many sources unattended.)

(If the user wants the phone in the loop for this episode — video staged,
queue entry tracked — run `$PY -m server.worker "<url-or-file>"` instead of
the two commands above: same artifacts plus `video.mp4` and a `prepared`
queue row. That's the /prepare skill's engine.)

Acquire reuses cached downloads (idempotent); `--force-transcribe` skips
subtitle discovery and goes straight to ASR — only on user request or when the
source subs turn out to be garbage. Coverage classifies every sentence
(comprehensible / reinforcement / i+1 / too-hard), ranks up to 50 candidate
unknowns, and **records inert exposures** to the ledger (activation happens at
mark-watched — the watched-gate). Re-runs are idempotent. `--refresh-known`
forces a live Anki rescan past the ~6h cache — use when the user says they
just reviewed a lot (worth offering when curating a job the worker prepared
many hours ago).

Relay the coverage stats line to the user (sentences, comprehensibility %,
i+1 count, candidates) before curating — it sets expectations. If token
comprehensibility is under ~80%, say so plainly: the episode is hard for them
and the prep doc matters more than the cards.

## Step 2.5 — punctuation gate (subagent restore)

Speech transcripts (ASR, or auto-subs stripped of punctuation) arrive as
run-on text. Sentence boundaries drive **everything downstream** — the merged
sentences, their audio-clip spans, the i+1 classification — so unpunctuated
subs must get sentence-ending marks (。？！) before curation. The worker does
this unattended with OpenAI when it has a key; **here you do it with a
subagent** (better than `gpt-4o-mini`, no key needed). Run the gate on every
episode, both entry paths:

```sh
$PY -m tools.punctuate check EPISODE_ID     # stdout = blocks path, or empty
```

- **Empty stdout** → nothing to do (subs already punctuated, or already
  restored). Skip straight to Step 3 (queue path) / coverage (direct path).
- **A path on stdout** (`<episode_dir>/punct_blocks.json`, a
  `[{"idx","text"}, …]` list of raw blocks) → this episode needs punctuation.
  **Spawn a subagent** (Agent tool, `general-purpose`) to restore it:

  > Read `<episode_dir>/punct_blocks.json` — a JSON list of Japanese subtitle
  > blocks `{"idx", "text"}` from a speech transcript, lacking sentence
  > punctuation. Return a JSON list of the **same length and idx order** where
  > each `text` has sentence-ending punctuation inserted: 。 at declarative
  > ends, ？ after questions, ！ after exclamations. **Only insert 。？！** —
  > do not add commas, and do not rewrite, delete, reorder, translate, or
  > otherwise change any character (non-punctuation edits are discarded
  > downstream anyway, so they only waste effort). A block that ends
  > mid-sentence gets no ending mark; a block spanning a boundary gets the mark
  > mid-text. Write the result to `<episode_dir>/punct_out.json` and reply with
  > that path only.

  Then feed it back through the insert-only merge and rewrite the artifacts:

  ```sh
  $PY -m tools.punctuate apply EPISODE_ID <episode_dir>/punct_out.json
  ```

  `apply` runs the subagent's text through the **same diff** the OpenAI path
  uses (`engine.punctuation`) — it keeps the original characters byte-for-byte
  and absorbs only the punctuation, so audio spans stay aligned even if the
  subagent slips and rewrites a word. It re-segments, rewrites `sentences.srt`
  + `transcript.json` (`punctuation_source: "subagent"`), and deletes the
  blocks file. If it errors on a block-count mismatch, re-spawn the subagent,
  stressing *exactly one entry per input block, same order*.

- **After `apply`, coverage is stale** — sentence indices changed. Re-run it:
  direct path, this IS the Step 2 coverage call (run it now, after the gate);
  queue path, re-run `$PY -m tools.coverage EPISODE_ID` before Step 3 (offer
  `--refresh-known` if the worker prepared it long ago).

## Step 3 — curate (the live intelligence; this is your job, not a tool's)

Read, in this order:

1. `coverage.json` → `stats` and `candidates` (each candidate: `lemma`,
   `reading`, `pos`, `freq_rank`, `recurrence`, `leverage` (null until P1),
   and `best` = its most i+1 sentence with `other_unknown_count`).
2. `sentences.srt` (or `transcript.json`) — **actually read the episode**,
   start to finish. The synopsis, glosses-in-context, and focal points are only
   as good as your reading of it.
3. If `coverage.json` is too big to read whole, read `stats` + `candidates`
   from it and take sentence text from `sentences.srt`.

Then produce, in `<episode_dir>/curate.json`:

```json
{
  "synopsis": "2–4 sentences, English. Pre-watch orientation: premise, setting,
                who's talking, main topics. Orient, don't spoil.",
  "genre": "one label: explainer | vlog | documentary | comedy | interview | news | gameplay | …",
  "format": "one label for the production style: talking-head | ゆっくり解説 | VOICEROID解説 | live-action | animation | slideshow | podcast | …",
  "topics": ["2–5 short topic tags, English, e.g. history, food-science, folklore"],
  "difficulty_felt": 3,
  "keywords":  [{"word": "縄張り", "gloss": "territory"}, ...],
  "focal_points": [{"word": "縄張り", "why": "one line: recurs ×6 and both hard scenes hinge on it"}, ...],
  "weighting": "one line: how you weighed freq_rank vs recurrence vs thematic centrality for THIS episode",
  "picks_rationale": "one or two lines on what you kept vs cut and why",
  "exclude": [{"lemma": "すける", "why": "tokenizer misparse of the host's name ななすけ"}, ...],
  "phrases": [{"sentence_idx": 12, "surface": "気を付けて", "canonical": "気を付ける",
               "classification": "i_plus_1"}, ...],
  "grammar": [{"sentence_idx": 3, "pattern": "〜てしまう", "form_note": "食べちゃった = 食べる+てしまう (contracted past)",
               "classification": "comprehensible"}, ...]
}
```

- **genre / format / topics / difficulty_felt — the taste-metadata block**
  (DESIGN.md — Taste metadata). These feed the emergent enjoyment metric and
  the ytSearch discovery engine, so keep them *categorical and consistent*:
  reuse the same `genre`/`format` labels across episodes (they become the
  recommender's bandit arms), don't invent a fresh phrase each time. `topics`
  are the content hooks. `difficulty_felt` is **your** subjective read of how
  hard the episode is to follow, 1 (easy) – 5 (hard) — a human complement to
  the measured coverage %, not a copy of it. All four are optional but cheap;
  fill them whenever you can judge them from your reading.

- **exclude — filter the junk before anything else.** Scan the candidates for
  non-words and put them here; render drops them from the vocab grid AND
  their sentences from the i+1 list. Junk includes: tokenizer misparses
  (fragments of names/handles — check each candidate's `best.text`: if the
  "word" sits inside a longer name/katakana run like ななすけ→すける, it's
  not a word), ASR hallucinations, and product/UI strings not worth study
  (ハイキングモードオン). Real proper nouns that orient the viewer (place
  names, the show's title) may stay as glossary keywords — just never card
  them. When in doubt whether something is a real lemma, check its
  `freq_rank`: a "common word" you've never seen with an absurd best-sentence
  is usually a parse artifact.

- **keywords** — entries are `{"word", "gloss", "note"}`. Gloss **every**
  candidate, plus any thematic words you rescue that the ranking buried
  (keyword order leads the vocab grid; leftover candidates follow). The grid
  renders word | reading | note | gloss, with the gloss masked behind a
  tap-to-peek so full coverage can't spoil the know-it/don't-know self-test.
  `gloss`: the sense **used in this episode**, not the dictionary's first
  sense, ≤4 words. `note`: a short usage snippet from the episode **in
  Japanese** (the actual collocation — context that doesn't reveal the
  meaning); furigana is added automatically at render time.
- **focal_points** — 3–7 words. Weigh the deterministic columns live
  (freq_rank · recurrence · corpus leverage when P1 lands) **plus** thematic
  centrality from your reading; one-line rationale each. Record your effective
  weighting in `weighting` — after ~20 episodes a good fixed heuristic gets
  promoted into the dumb layer (resolved Q4).
- **phrases — multi-word expressions the tokenizer shattered** (GRAMMAR.md).
  While reading, when a line uses an idiom/MWE as a unit (気を付ける,
  仕方がない, 取り返しがつかない — not a coincidental word run), emit it:
  `surface` as it appears, `canonical` = its **JMdict dictionary form** (you
  are the deinflector — the recorder only validates the key), and the
  sentence's coverage `classification` (from `coverage.json`; it gates
  whether the exposure counts toward promotion). The recorder rejects
  anything that isn't a JMdict headword or is a single Sudachi token, and
  reports rejects — surface genuinely idiomatic rejects to the user; a
  deliberate `ledgerctl phrase-add` tracks them anyway. Sentences whose
  `coverage.json` entry already lists the phrase (a `phrases` array on the
  sentence) don't need re-emitting — those are already-tracked keys recorded
  at Stage 1.
- **grammar — pattern usages, matched into the fixed taxonomy** (GRAMMAR.md).
  Tag notable grammar usages with the canonical `pattern` key from
  `grammar_points` (`ledgerctl query summary` shows the taxonomy exists; when
  unsure of a key, check with
  `sqlite3 <ledger> "SELECT pattern FROM grammar_points WHERE pattern LIKE '%しまう%'"`).
  Never invent a variant spelling of an existing key. A genuinely novel
  pattern (colloquial/dialectal) goes in as
  `{"proposed_pattern": "...", "gloss": "...", "example": "..."}` — it lands
  in the review queue (`ledgerctl query grammar-proposed` →
  `grammar-approve`), never straight into the taxonomy. `form_note` carries
  the word-form structure worth showing (食べさせられた = causative-passive
  of 食べる). Tag what a learner would *notice*: the N+1-ish patterns, keigo
  shifts, contractions — not every 〜ます in the episode.
- Write valid JSON, `ensure_ascii` irrelevant — just write UTF-8.

**Completeness check — do this before moving on:** every candidate in
`coverage.json` must end up either glossed in `keywords` or listed in
`exclude`. Count them: `len(keywords ∪ exclude) ≥ len(candidates)`. Silent
leftovers render as blank rows at the bottom of the phone's vocab grid
(word + ×N and nothing else) — that's a curation bug, not a display quirk.

**Persist the taste-metadata block** once `curate.json` is written:

```sh
$PY -m ledger.ledgerctl record-curation EPISODE_ID <episode_dir>/curate.json
```

Denormalizes `genre`/`format`/`difficulty_felt` onto the episode row and
`topics` into its metadata JSON — the enjoyment-metric attribution features
(DESIGN.md — Taste metadata) — **and** lands the `phrases`/`grammar` blocks as
inert exposure evidence (validated against JMdict / the grammar taxonomy;
GRAMMAR.md). The output reports `items.phrases.rejected` and
`items.grammar.proposed` — relay both to the user rather than silently moving
on. Idempotent: safe to re-run if you revise `curate.json`.

And in `<episode_dir>/picks.json`, the card **pool** (workflow decided
2026-07-05: the user's phone feedback makes the final cut, not you):

```json
[{"lemma": "縄張り", "sentence_idx": 9, "reading": "なわばり",
  "sentence_furigana": "あの 犬[いぬ]は 自分[じぶん]の 縄張[なわば]りを 守[まも]っていた。",
  "english": "That dog was guarding its own territory.",
  "notes": "縄張り(なわばり) — literally a 'roped-off area'; here the animal-behavior sense, a territory an animal defends. Colloquially also a person's/gang's turf. Takes を張る/を守る as its natural verbs.",
  "context": "The host is explaining why the shrine's stray dog barks at delivery workers but ignores regulars."}, ...]
```

The pool is **ordered by your preference** — after feedback, `tools.select`
prunes known-tapped lemmas, moves high-interest (★) taps to the front —
alongside the *standing* interest set carried over from earlier shows (★ taps
persist in the ledger until the word is known) — and caps at
`deck.new_cards_per_day`; the final picks are pushed to Anki when the user
marks the episode watched (`POST /watched`), not at curate time. Your
pool order IS the default selection, so lead with your best.

Include `english` (a natural full-sentence translation) on **every** pool
entry whenever config's `deck.field_map` maps an `english` field — you're
reading the sentences anyway, and it becomes the card back. Skip it if the
map has no english key.

Likewise include `notes` and `context` (both **English**) on every pool entry
whenever the field_map maps them — you have the full transcript in front of
you now; nothing downstream can reconstruct this:

- `notes` — a mini usage note on the **target word as used in this sentence**.
  Isolate the target word first (write it with its reading), then explain the
  nuance that matters here: which sense is in play, slang/colloquial register,
  the grammar pattern it sits in (conjugation, particle it takes, set
  collocation), politeness level, or how it differs from the near-synonym a
  learner would reach for. 1–3 sentences; say something the dictionary gloss
  doesn't. If the line uses notable slang or a grammar point beyond the target
  word itself, mention that too.
- `context` — 1–2 sentences of situational grounding: what the video was
  talking about when this line came up (who's speaking, about what, in what
  scene). Enough that the card makes sense months later without rewatching —
  but don't just restate the sentence's own content.

Include `sentence_furigana` on **every** pool entry: the full card sentence
with readings **you write yourself** from your reading of the line in context
— never a dictionary, lookup tool, or the Japanese-support addon. You heard
the transcript's context; a dictionary hasn't. Format (Anki furigana syntax,
rendered as ruby by the card template; hidden behind a reveal toggle at
review time):

- Reading in square brackets immediately after each kanji run, an ASCII space
  *before* the run: `あの 犬[いぬ]は 縄張[なわば]りを 守[まも]っていた。`
  (no space needed at sentence start). Bracket only the kanji run — okurigana
  stays outside (`守[まも]っていた`, not `守っていた[まもっていた]`).
- Hiragana readings. Kana-only stretches, ASCII, digits, and punctuation are
  left untouched.
- Readings must be the ones **spoken in the audio** — resolve context-sensitive
  readings yourself (方 かた/ほう, 行った いった/おこなった, counters, names as
  the speaker says them). When genuinely unsure, listen again or leave that
  run un-annotated rather than guess.
- Everything outside the brackets must reproduce the transcript sentence
  character-for-character — `tools.deck` verifies this (strips brackets and
  spaces, compares) and silently falls back to the bare sentence on mismatch,
  so a typo costs the furigana, not the card.

**Selection bar** (DESIGN.md — Card philosophy; curate ruthlessly, fewer good
cards beats hitting the cap):

- Pool size: aim for up to ~2× `deck.new_cards_per_day` (default cap 10) so
  known-taps can prune without starving the final cut — but never pad with
  weak entries to get there. With the strict bars below, a short high-quality
  pool (even under the cap) is the expected outcome, not a failure.
- Complete merged sentence — drop fragments, trail-offs, interjection-only lines.
- **Strict i+1: require `other_unknown_count == 0`.** The card's sentence must
  have exactly one gap — the target. Drop a lemma whose only sentences carry a
  second unknown; it isn't ready yet and will resurface once the other word is
  known. (A tapped-interest word gets rescued deterministically, but still only
  from a true-i+1 sentence.)
- **Frequency/recurrence floor:** don't card a lemma that is *both* rare
  (`freq_rank` null — outside the show-frequency corpus) *and* a one-off
  (`recurrence == 1`) in this episode. Low leverage: you may never meet it
  again, so it's not worth a card. Exception: a tapped-interest word the user
  explicitly wants. Rare-but-recurring or common-but-one-off words are fine.
- **Strict on the target word**: correctly transcribed, in the card's intended
  sense/reading, ideally in its canonical collocation. Relaxed on incidental
  ASR errors elsewhere (the audio is the real native line regardless).
- Clip-length sanity: `end - start` between ~1.5s and ~15s (deck pads ±0.5s);
  outside that the audio card is bad regardless of text.
- You may pick a **different sentence** than `best` for a lemma — any
  `sentence_idx` from the transcript works; `best` is just the default ranking's
  suggestion.
- Candidates already exclude ledger-`learning` lemmas and already-carded ones;
  don't re-add them.

## Step 4 — render

```sh
$PY -m tools.render EPISODE_ID              # stdout = path to prep.html
```

Self-contained offline HTML: synopsis, focal points, tappable glossary +
i+1 sentences (tap = "know it"/"don't"), reinforcement list, and the
copy-corrections blob (P9). On the queue path the phone app pulls the same
content as JSON (`GET /prep`) the moment the job goes `staged` — the HTML is
the no-app fallback (AirDrop / Files).

## Step 5 — deck (direct mode ONLY)

**Queue path: skip this step.** Cards are pushed automatically by the server
when the user marks the episode watched — the flow is review → feedback
(`POST /taps` runs `tools.select` over your pool) → watch → `POST /watched`
(deck push + exposure activation). Pushing at curate time would bypass the
user's feedback.

Direct mode (PC-only, no phone in the loop — the user asked for a one-shot
prep): push now, since there's no feedback step coming:

```sh
$PY -m tools.deck EPISODE_ID <episode_dir>/picks.json
```

Cuts native-audio clips at the merged-sentence spans and pushes via
AnkiConnect — onto the config's `deck.note_type`/`field_map` if set (the
user's own note type, which must already exist), else the built-in
"fullPipe Sentence Mining" model; deck from config; note ids recorded for
lapse polling. Registers `mined_card` evidence and runs `promote` — picked
lemmas become ledger-`learning`. In direct mode trim the pool to the cap
yourself first (top of your ordering).

- Duplicate notes are skipped with a log line, not an error — report them.
- If AnkiConnect is down and can't be revived: `--apkg` builds
  `<episode_dir>/deck.apkg` (stable guids) for manual import instead. Prefer
  the live push.

## Step 6 — close out the job + report

Queue path: `$PY -m server.jobqueue set-state <id> staged` (the server's
worker would notice the artifacts and do this within seconds anyway; setting
it directly also covers server-down sessions). Then report, one tight block
per episode:

- comprehensibility % and sentence-class counts,
- focal points with rationales,
- the card pool (count + your top picks; in direct mode: cards actually pushed),
- where the prep doc is: phone app pulls it automatically now that the job is
  `staged` (plus the `prep.html` path as fallback),
- **the loop-closer** (queue path): on the phone — review, tap ✔ known /
  ★ high-interest, **Submit feedback** (prunes/prioritizes the card pool,
  updates the ledger), watch, then **Mark watched** (pushes the final cards
  to Anki, activates exposures, cleans the phone copy). The copy-blob →
  `/reconcile` path is the offline fallback.

When curating a batch, end with a one-line queue summary: N staged this
session, M still in flight, any failures awaiting a retry decision.

## Failure modes

| symptom | meaning | move |
|---|---|---|
| `server.jobqueue list` → no queue.db | mobile layer unused on this machine | fine — direct mode |
| acquire: "poor punctuation…" / "deferring restore to the /immerse subagent" | subs lack sentence punctuation | expected — the punctuation gate (Step 2.5) restores it via subagent; no key needed |
| `punctuate apply`: "got N blocks, expected M" | subagent dropped/merged/added lines | re-spawn the subagent, stressing one entry per input block in the same order |
| acquire: no subs + no ELEVENLABS_API_KEY + no `FULLPIPE_REAZONSPEECH_DIR` | no transcript possible | stop; ask for a key or the offline model dir |
| coverage: AnkiConnect failed after 3 attempts | Anki closed mid-run, no fresh cache | `ensure_anki.sh`, re-run coverage |
| job `failed` with a yt-dlp error | source gone/region-locked/needs cookies | show the error; retry only if the user says the source is fine |
| deck: "skip <lemma>: … duplicate" | note already exists | fine — report it |
| known-source matched 0 cards (stderr) | config `known_words.sources` query wrong | flag to user; known-set is undercounting |

## Notes

- Re-running the whole skill on the same source is safe: downloads cached,
  exposures deduped, already-carded lemmas excluded from candidates.
- The worker's Stage 1 (MOBILE.md) is exactly Steps 1–2's tooling run
  unattended — that's why artifacts-exist ⇒ skip is always correct. The one
  live-only step is the punctuation gate (Step 2.5): the worker restores with
  OpenAI (or falls back to choppy sentences if it had no key), and
  `punctuate check` is what tells you whether that fallback still needs
  fixing — so run the gate on prepared jobs too, not just direct acquires.
- A `curating` job you didn't just start usually means a previous session
  marked it and stopped (or the phone's curate button was tapped): treat it as
  ready — pick it up and finish it.
