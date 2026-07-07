# Immersion Workstation — Design

A holistic Japanese immersion pipeline: **pre-watch analysis → watch → continuous
Anki review**, all reading and writing one shared *known-lemma ledger*. You point it at
a local file, a YouTube URL, or a topic; it produces a prerenderable prep doc for your
phone, curated native-audio Anki cards, and it quietly maintains a model of what you
already know.

> **Companion project — content discovery.** *What to watch next* lives outside
> fullPipe in **ytSearch** (`../../ytSearch/`, repo root `code/ytSearch/`). It
> harvests the live Japanese-YouTube graph (a curiosity engine, novelty-driven)
> and emits ranked picks that feed `/immerse`; fullPipe feeds back rating and
> completion signal for it to learn taste from. Coverage stays fullPipe's job —
> ytSearch treats it as a marker, not a filter. Design: `../../ytSearch/DESIGN.md`.

---

## Context — why this exists

Across the existing code library, several projects already do pieces of immersion
learning (`audioPrimeProd` analyzes + primes video, `ci/` generates synthetic i+1, the
`sentence-mining` skill mines cards, `phrases-full.db` is a 595M-token corpus). **Not one
of them persists a growing model of what the learner knows** — every one reads a static
word list and throws it away. AnkiMorphs (the usual source) structurally undercounts:
it only marks a word "known" once a *card* containing it matures, so it is blind to the
thousands of words known from immersion, cognates, or never carded.

The gap this project fills is that stateful spine — a **ledger** that starts from Anki
but grows from everything you watch and every correction you make — and a skill-driven
workflow that wraps the existing engines around it into one product.

**Design principles**
- **Skill-driven intelligence.** The smart steps (synopsis, focal-point selection, card
  curation, ambiguous ledger calls) are done by a live model reading *this* episode, not
  frozen SDK prompts. Deterministic plumbing (yt-dlp, ffmpeg, SudachiPy, genanki) stays
  dumb and scriptable.
- **Prerenderable / phone-first outputs.** Every artifact is a static file consumable
  offline: prep HTML, native-audio decks. The ledger persists independent of Anki being
  open.
- **Audio-forward.** The learner listens more than reads; cards carry *native* audio, not
  TTS, wherever possible.
- **Additive, not a fork.** Existing engines are preserved as pure functions; the new
  work is a layer on top. A traditional GUI for the non-agentic user segment remains an
  additive front-end later on the same deterministic core.

---

## Architecture — modes on a spine

```
┌─ INPUT ──────────────────────────────────────────────┐
│  local file  ·  YouTube URL  ·  topic string          │
└──────────────────────┬────────────────────────────────┘
             ┌──────────┴──────────┐
             │  ORCHESTRATION (new) │  ← replaces audioPrime's QThread pipeline.py
             │  skill-driven,       │
             │  ledger-aware        │
             └──────────┬──────────┘
   ┌─────────┬──────────┼──────────┬──────────────┐
 PRIME     ANALYZE     MINE      GENERATE       REPLACE
 interleaved  prep doc  i+1 cards  synthetic i+1  fix existing
 listening    +coverage from video (folds in ci/) cards in place
   └─────────┴──────────┼──────────┴──────────────┘
             ┌──────────┴──────────┐
             │  ENGINE (audioPrime) │  ← preserved wholesale, pure functions
             │  download·ASR·srt·   │
             │  lemma·tts·anki·m4b  │
             └──────────┬──────────┘
             ┌──────────┴──────────┐
             │  LEDGER (new)        │  ← the spine; every mode reads/writes it
             └─────────────────────┘
   exports → prep HTML · primed audio · Anki cards   (all phone-syncable)
```

Three existing projects become four+ modes of one workstation, all reading the same
"what I know" ledger. Nothing is thrown away; the primed-listening functionality gains a
memory.

---

## Reuse map — what comes from where

| Component | Source | Notes |
|---|---|---|
| download (yt-dlp) | `audioPrimeProd/src/core/downloader.py` | file + URL |
| ASR | `audioPrimeProd/src/core/transcriber.py` | ElevenLabs Scribe V2 (word timestamps) + ReazonSpeech (offline). Add AssemblyAI (diarization) as an option |
| sub parse / **sentence merge** / punct detect | `audioPrimeProd/src/core/srt_parser.py` | `merge_to_sentences`, `has_good_punctuation`, `words_to_srt` |
| **AI punctuation restore** | `ankiDeckMaker/src/core/punctuation.py` | diff-based, insert-only — the crown jewel (see Acquire) |
| lemma analysis / i+1 | `audioPrimeProd/src/core/lemma_analyzer.py` | **retokenize to SudachiPy mode C** (currently fugashi) |
| known-word live diff | `sentence-mining/scripts/analyze.py` (`load_known_intervals`) | SudachiPy tokenize Anki fields, known if interval ≥ 21d, cached |
| deck build / audio clip | `audioPrimeProd/src/core/anki.py` | clips native audio to the merged sentence span |
| TTS | `audioPrimeProd/src/core/tts.py`, `ci/pipeline/tts.py` | last-resort only (audio-forward) |
| interleaved listening / m4b | `audioPrimeProd/src/core/interleaver.py`, `m4b.py` | PRIME mode |
| synthetic i+1 generation | `ci/pipeline/generator.py`, `config/prompts.py` | GENERATE mode |
| replace / de-leech / rehab | `sentence-mining/scripts/replace_*.py` | REPLACE mode |
| external sentence sources | `sentence-mining/references/replace-mode.md` | Immersion Kit + Nadeshiko APIs (native audio fallback) |
| shareable skill pattern | `sentence-mining/` (config.json + .env + `/setup`) | distribution model |
| frequency list | `phrases/japaneseShowGraph/subs/japaneseShowGraph.db` | **show-penetration rank** (distinct shows containing the lemma, ~11k shows, mode C, media register). Leeds `ja_frequency.txt` fallback for lemmas absent from the corpus. *(resolved Q6 — see PROPOSALS.md P7)* |
| **scoring corpus** | `phrases/.../subs/phrases-full.db` (595M tok) | i+1 leverage / focal-point scoring. **Text-only → scoring brain, NOT a card source.** ⚠ tokenized at **mode B**, not C — re-parse before building leverage scoring (P1) |
| seed vocab (optional) | `japaneseSchool/japanese_l1_vocabulary_list.txt`, grade kanji | beginner bootstrap |

Standard tokenizer everywhere: **SudachiPy SplitMode C** — aligns with
`japaneseShowGraph.db` (parsed `--mode C`) and the `sentence-mining` skill; retires
fugashi (the only holdout). **Correction (2026-07-05): `phrases-full.db` does NOT
align** — the phrase parser's default is `--mode B` (`phrases/tools/phraseParser/
parse.py:299`) and the full-corpus parse used the default, so mode-C lemma joins
against it silently miss on compounds. Not a blocker for the `/immerse` MVP (which
doesn't need corpus leverage); re-parse at mode C before leverage scoring lands (P1).
Use `normalized_form()` (collapses orthographic variants, e.g. 籠る/こもる → 籠もる) and
kanji-stem matching to expand the known set (also mitigates undercount).

**Correction (2026-07-05, found in implementation):** current `sudachidict_core`
DOES emit 形状詞 for na-adjective stems (綺麗/静か/頑丈) — the `sentence-mining`
skill's comment that they surface as 名詞 is stale, and its content-POS filter
silently drops na-adjectives. `engine/lemma.py` includes 形状詞 in
`CONTENT_POS_PREFIXES`; the upstream skill should take the same fix, and any
freq table built before it should be rebuilt (`ledger/build_freq.py`, ~85s).

---

## The Ledger — the spine

### Philosophy: event-sourced, and persist only the delta

Raw **evidence** is the truth (append-only); a per-lemma **status** is a cached projection
recomputed by `promote`. This buys re-tunable thresholds (rerun `promote` over signals you
already have), auditability ("why does it think I know 諦める?"), and conflict handling by
rule rather than imperative spaghetti.

**Key refinement (from the `sentence-mining` skill): don't persist what Anki already
stores.** The Anki-known set is **recomputed live** each session (SudachiPy-tokenize the
configured Anki fields via AnkiConnect; a lemma is known once its highest card interval ≥
21d; cache ~6h). The ledger persists *only the evidence Anki structurally can't see* —
exposure and taps. This removes the stale-export "resync" problem entirely.

```
materialize-known  =  live-Anki-known  ∪  ledger-promoted(exposure, taps)
```

The ledger's persistent surface is exactly the **delta between what Anki knows and what
you actually know** — a tight, defensible object.

### States (non-monotonic — negative evidence demotes)

```
                tap_known · (exposure ≥ θ & spread ≥ k)
   unknown ───────────────────────────────────────────────►  known
      ▲ │                                                     ▲  │
      │ │ mined_card                                          │  │
      │ ▼                                                     │  ▼
      └ learning ◄──── card_lapse · tap_unknown ──────────────┘
```

(`mature` dropped in v1 — no `promote` rule ever produced it and its only consumer
treated it identically to `known`; Anki-side maturity already arrives via the live
union. Reintroduce only if a consumer needs the distinction. See P2.)

Statuses earn their keep in **coverage analysis**, which classifies each sentence four
ways: all-known (comprehensible, counts for exposure) · one unknown that is `learning`
(*reinforcement gold*) · one unknown truly `unknown` (*mining candidate*) · two+ unknown
(too hard). Known-set for i+1 = `{known}`; `learning` counts as not-yet-known.

### Evidence sources (persisted)

| source | polarity | meaning |
|---|---|---|
| `exposure` | +weak, accrues | seen in a **watched** episode. Written unconditionally with `known_ratio` + `other_unknown_count` context; the comprehension bar is applied at `promote`, not at write — retunable over evidence already collected *(resolved Q1)* |
| `tap_known` | +strong | tapped "I know this" in the prep doc |
| `tap_unknown` | −strong | tapped "I don't know" |
| `tap_interest` | 0 (a *want*, not knowledge) | tapped ★ "I want to learn this". Durable across episodes: `active_interest` (= tap_interest − known) keeps steering card selection (prioritize + rescue) and player highlighting until the lemma is known. A minted card that's later deleted in Anki (`cards.deleted_at`) reopens the lemma for a fresh mining candidate |
| `mined_card` | learning | pipeline minted a card for it |
| `card_lapse` | −medium | a minted card lapsed in Anki |
| `import` | +strong (< tap) | bulk-seeded from an external known list (e.g. an AnkiMorphs known-morphs export) at bootstrap; a fresh `tap_unknown` demotes it quietly — no `needs_review`, bulk lists are noisy |

(`ankimorphs` / `anki_card` are **not** persisted — recomputed live.)

### Schema (SQLite)

```sql
-- Cached projection of the NON-Anki verdict. Fast reads for materialize-known.
CREATE TABLE lemmas (
    lemma          TEXT PRIMARY KEY,      -- SudachiPy dictionary form = join key
    reading        TEXT,                  -- homograph disambiguation aid
    pos            TEXT,
    freq_rank      INTEGER,               -- ja_frequency.txt; NULL if absent
    status         TEXT NOT NULL DEFAULT 'unknown',   -- unknown|learning|known
    confidence     REAL NOT NULL DEFAULT 0,
    exposure_count INTEGER NOT NULL DEFAULT 0,
    episode_spread INTEGER NOT NULL DEFAULT 0,         -- distinct watched episodes
    needs_review   INTEGER NOT NULL DEFAULT 0,         -- conflict → /reconcile queue
    first_seen TEXT, last_seen TEXT, updated_at TEXT NOT NULL
);

-- Append-only. THE truth. status above is a projection of this.
CREATE TABLE evidence (
    id         INTEGER PRIMARY KEY,
    lemma      TEXT NOT NULL,
    source     TEXT NOT NULL, -- exposure|tap_known|tap_unknown|tap_interest|mined_card|card_lapse
    polarity   INTEGER NOT NULL,-- +1 / -1 / 0(=learning)
    weight     REAL NOT NULL DEFAULT 1.0,
    episode_id TEXT,          -- real column, not JSON: watched-gate + spread query it (P3)
    context    TEXT,          -- JSON: {sentence_idx, known_ratio, other_unknown_count, ...}
    ts         TEXT NOT NULL
);
CREATE INDEX idx_evidence_lemma   ON evidence(lemma);
CREATE INDEX idx_evidence_episode ON evidence(episode_id);
-- Idempotent /immerse re-runs: at most one exposure row per lemma per episode (P4).
-- Also caps a talky episode at one exposure — spread does the multi-episode work.
CREATE UNIQUE INDEX idx_exposure_once ON evidence(lemma, episode_id, source)
    WHERE source = 'exposure';

-- Episodes: enables spread, idempotent exposure, and the watched-gate.
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,      -- stable hash of source
    title TEXT, source TEXT, kind TEXT,       -- youtube|local
    watched INTEGER DEFAULT 0,                 -- gates exposure activation
    processed_at TEXT
);

-- Minted cards: enables graduation/lapse feedback + prevents re-mining.
CREATE TABLE cards (
    id INTEGER PRIMARY KEY,
    lemma TEXT, episode_id TEXT, sentence TEXT,
    anki_guid TEXT,          -- genanki stable guid (.apkg fallback path)
    anki_note_id INTEGER,    -- from AnkiConnect addNote (primary path) — what
                             -- lapse polling queries; guid isn't searchable
                             -- through AnkiConnect (P6 amendment)
    lapses INTEGER NOT NULL DEFAULT 0,   -- last polled value; evidence on increase
    created_at TEXT
);

-- Frequency prior (P7): show-penetration ranks, Leeds fallback rows.
CREATE TABLE freq (
    lemma TEXT PRIMARY KEY, rank INTEGER NOT NULL,
    penetration INTEGER, source TEXT NOT NULL   -- show_graph|leeds
);

-- Idempotent tap flushes (MOBILE.md): replayed batch_id = no-op.
CREATE TABLE tap_batches (
    batch_id TEXT PRIMARY KEY, episode_id TEXT, applied_at TEXT NOT NULL
);
```

(As-built: `ledger/schema.sql`.)

### Two design moves

**Watched-gate.** `/immerse` writes `exposure` rows tagged with `episode_id`, but they are
**inert** until `episodes.watched = 1`. Watching is the activation switch — analyzing 10
episodes you never watched doesn't inflate your known count.

**Frequency prior kills the old slider.** Replace audioPrime's blunt global "assume 30% of
common unknowns are known" with a **per-lemma exposure threshold** scaled by `freq_rank`:

| freq_rank | θ (exposures) | spread (episodes) |
|---|---|---|
| top ~2k | 2 | 2 |
| 2k–10k | 4 | 3 |
| rare / absent | 6 | 4 |

### `promote` — derivation rules (first match wins)

1. Fresh `tap_unknown` (newer than any positive) → **learning** (cancels known); set
   `needs_review` if a strong positive also exists. **If the lemma is live-Anki-known,
   demotion is a no-op** (the union at `materialize` puts it straight back) — that tap
   means *the card isn't doing its job*: set `needs_review` and route to REPLACE as a
   rehab candidate instead *(resolved Q2)*.
2. `tap_known` → **known**.
3. Qualifying exposures ≥ θ(freq_rank) **and** `episode_spread ≥ k` → **known**. An
   exposure *qualifies* when its episode is watched and `other_unknown_count = 0` —
   the target was the sentence's only gap, a true i+1 acquisition event. (Relax to
   "allow 1 other unknown on sentences of 8+ content tokens" later if convergence
   feels slow — retunable by rerunning `promote`.) *(resolved Q1)*
4. `mined_card`, no stronger positive → **learning**.
5. else → **unknown**.

(Live-Anki-known is unioned in at `materialize`, not in `promote`.) Confidence is a coarse
roll-up used only to order the reconcile queue and feed the frequency prior.

*Implemented refinements (2026-07-05, `ledger/ledgerctl.py`):* `card_lapse`
participates in rule 1's "fresh negative" alongside `tap_unknown` — the state
diagram's `known → learning` lapse edge needs it and the rules text omitted
it. Timestamp ties between a negative and a positive go to the negative:
taps are deliberate strong evidence, and same-second pairs only occur when
one run wrote both.

### Verbs (the public contract every tool/skill calls)

```
materialize-known    → live-Anki-known ∪ ledger-promoted; the set every mode reads
compute-anki-known   → live recompute via AnkiConnect (cached ~6h)  [replaces resync]
record-exposure      ← written by /immerse at analysis time; inert until watched
mark-watched         ← flips episodes.watched=1; apply-taps implies it for its
                       episode, manual for episodes watched without tapping (P5)
apply-taps           ← phone corrections; also polls AnkiConnect (cardsInfo on
                       cards.anki_guid) for lapses since last run → card_lapse (P6)
import-known         ← bulk-seed knowns from an external list (AnkiMorphs
                       known-morphs export etc.) as 'import' evidence; idempotent
promote              → recompute projection from evidence (the state machine)
query                → coverage %, trends, needs_review queue, mining candidates
```

`materialize-known` additionally **bridges external-lemmatizer forms into
SudachiPy space**: single-morpheme ledger-known strings contribute their
Sudachi dictionary + normalized forms (MeCab kana lemmas くる/ところ from an
AnkiMorphs import join transcript tokens 来る/所 via `normalized_form`).

### Bootstrap

First run, `compute-anki-known` derives the known lemmas from the Anki collection,
and `import-known` can seed from an external list — as built, this install was
seeded with a 3,046-lemma AnkiMorphs known-morphs export (the mining deck itself
was brand new, so the live Anki scan contributed 0 until minted cards mature).
Everything else is `unknown`. Early prep docs over-flag words you actually know
(AnkiMorphs never carded the basics learned pre-Anki — 公園/水/中-tier); every
`tap_known` and every watched episode's exposure converges the ledger toward your
true corpus within a handful of episodes. **The undercount self-heals.** The
masked-definition vocab grid exists for exactly this: mark known/unknown first,
peek at the English after.

---

## Taste metadata — the enjoyment spine

> **Built 2026-07-06** — the ledger side (`taste_events` + episode-meta columns),
> the server (`POST /rating` takes tags; `GET /jobs` carries them back), and the
> mobile tag picker are live end to end; verified phone → server → ledger. This
> section is the design that shipped.

*What to watch next* (ytSearch, `../../ytSearch/`) learns from a signal fullPipe
emits: how much a watched episode was enjoyed. Today that signal is a single
mutable `episodes.rating` int (1–5, added 2026-07-05) — and a lone scalar is too
thin to learn from. This section is the richer per-episode record that makes the
emergent enjoyment metric meaningful. **Capture is deliberately decoupled from
consumption**: it accrues from the next watch onward, with no ytSearch code in
sight — the discovery engine's kNN taste prior is noisy at ~10 ratings and sharp
at ~200, so the training set has to be filling during the build gap.

### Philosophy: enjoyment is a projection, not a column

Same move as the lemma ledger. **Taste events are append-only truth; a per-episode
`enjoyment` verdict is a recomputed projection.** A mutable rating column (as first
built) overwrites on re-rate and erases history — wrong for taste, because a taste
that *drifts* (you grow expert in a domain and cool on it) is the single thing the
discovery engine's expertise-inference most wants to see. Ratings stay off the
lemma `evidence` log (orthogonal to knowledge) but earn their own parallel log.

### Why a scalar isn't enough

A low star conflates axes that demand **opposite** recommender responses — "already
expert, bored" (→ less of this topic) vs. "too hard, bailed" (→ same topic, easier)
vs. "the format grated" (→ same topic, other format). The fix is a small set of
**tags** that break those confounds, governed by one razor:

> A tag earns its place only if it disambiguates something the star + passively
> captured features cannot.

That razor drops host preference (falls out of the `channel` feature),
audio/production quality (that's card-quality feedback — a different channel, and
noise in a taste model), and plain "good topic" (redundant with a high star +
persisted `topics[]`).

### The tags (resolved 2026-07-06)

Six, flat, valenced, multi-select, all optional. The star is the required verdict;
tags are **exception markers** — tapped only when there's something specific to say,
so most episodes carry a star and zero tags (silence = "read the star at face
value").

| tag | polarity | disambiguates | recommender move |
|---|---|---|---|
| **Already knew it** | − | expertise vs. topic-dislike | expertise-redundancy penalty on topic/domain (may generalize) |
| **Over my head** | − | difficulty vs. bad rec | **decouple star from the taste label**; "revisit when stronger" |
| **Didn't grab me** | − | genuine taste miss (no confound) | taste penalty — move off the cluster |
| **Format didn't land** | − | format vs. topic | format-axis penalty, keep the topic |
| **Fascinating** | + | which axis drove a high score | topic/domain reward |
| **Loved the format** | + | which axis drove a high score | format-axis reward |

**The load-bearing one is Over my head:** a difficulty-driven low star is *not* a
taste-low. The projection computes an **adjusted enjoyment** that discounts (or
excludes) such samples from the taste manifold — otherwise a hard-but-appealing
topic trains the model to avoid something you'd love once ready. This is also the
natural feed for ytSearch's graduation-queue question (shelve too-hard-but-on-topic
for later).

**UX:** rate → optional tag picker → append. Show **all six regardless of the
star** (grouped liked / didn't), never valence-filtered — the informative combos
are cross-valence, e.g. *4★ · Fascinating · Over my head* (loved it, but a stretch).

### What else to persist per episode

The tags explain the score; these let the metric *attribute* it — and most are
thrown away today.

- **Rescue the discarded yt-dlp dump.** `downloader.fetch_video_metadata()` already
  pulls the full `--dump-json` and keeps only title + uploader. Persist **channel
  (+id), duration, upload_date, view_count, description, tags**. Cheapest,
  highest-value change — these are the raw features the embedding/kNN feed on, and
  **channel is the strongest single taste predictor** (and ytSearch's collab-graph
  seed).
- **Structured curation block.** `/immerse` already reads the whole episode; have it
  emit `{genre, format, topics[], difficulty_felt}` into `curate.json` **and**
  denormalize it. Genre/format/topic exist *nowhere* today; this also seeds
  ytSearch's fixed genre taxonomy (its bandit needs stable arms).
- **Coverage-at-watch snapshot.** Promote comprehensibility %, i+1 count, known-set
  size from `coverage.json` onto the record. It's the **difficulty confound
  control** — you can't separate "boring" from "too hard" without knowing how hard
  it measurably was.

### Schema (resolved 2026-07-06)

Append-only events; the verdict is computed **on read** — no materialized cache.
The lemma ledger caches its projection in `lemmas` because 295k lemmas make
recompute expensive; taste volume is hundreds of episodes ever, so a view/query
over the events is right-sized. Same event-sourced philosophy (re-tunable,
auditable), no `promote`-style rebuild.

```sql
CREATE TABLE taste_events (
    id         INTEGER PRIMARY KEY,
    episode_id TEXT NOT NULL,
    review_id  TEXT NOT NULL,   -- groups one review act (rating + its tags), à la tap_batches
    kind       TEXT NOT NULL,   -- 'rating' | 'tag'
    value      TEXT NOT NULL,   -- rating: '1'..'5'  ·  tag: slug (already_knew|over_my_head|...)
    ts         TEXT NOT NULL
);
CREATE INDEX idx_taste_episode ON taste_events(episode_id);
```

One review appends a `rating` row + N `tag` rows sharing a `review_id`; re-rating
appends a **new** batch (drift preserved, nothing overwritten). Rating leaves the
mutable `episodes.rating` column behind and moves here.

**On-read `enjoyment` verdict** (from the latest `review_id` per episode):
`rating`, `tags[]`, `taste_valid` (0 when `over_my_head` present →
difficulty-censored, excluded from the taste manifold), `adjusted_enjoyment` (the
star when `taste_valid`, else null). The Over-my-head decoupling rule is one clause
here.

**Capture fields — columns vs. JSON**, split by how the recommender uses each
(filter / group / correlate → real column; bulky embed-only payload → JSON, à la
`evidence.context`):

| real columns on `episodes` | `metadata` JSON |
|---|---|
| channel, channel_id, duration, upload_date | description, tags[], view_count |
| genre, format *(ytSearch bandit arms)* | topics[] |
| coverage_pct, iplus1_count, known_set_size, difficulty_felt | — |

Writers map onto existing stages: **acquire** → yt-dlp columns + `metadata` JSON
(widen the discard in `downloader.py`); **coverage** → the coverage snapshot;
**/immerse curate** → genre/format/topics/difficulty_felt; **rating endpoint** →
`record-rating(episode_id, rating, tags[])` appends a `taste_events` batch
(replaces the mutable `set_rating`).

### Difficulty is its own axis (and a maturing one)

Two difficulty reads are kept because they answer different questions:
`difficulty_felt` (the `/immerse` model's subjective estimate) and coverage-at-watch
(measured known-lemma overlap). **Coverage is a maturing signal** — noisy while the
ledger still undercounts, sharpening into a real difficulty predictor as `tap_known`
+ exposure converge it toward your true corpus. Neither ever crosses into the
enjoyment features: card-review performance and card yield are language-difficulty
signal, never taste.

**Deferred signals:**

- **Comprehension quiz.** Coverage measures *vocabulary overlap* — a proxy for the
  real target, *did you actually follow it*. A short (~5-question) LLM-generated
  comprehension quiz, prerendered at curate time and offered *after* `watched`
  (post-watch, no spoiler), would measure that target directly. Two payoffs: it's
  ground-truth that sharpens the `over_my_head` decoupling beyond self-report, and —
  the bigger one — scoring it against predicted coverage over time **calibrates the
  coverage→comprehension mapping itself** (retunes the `promote` θ thresholds), so
  the quiz trains the *ledger*, not just the episode record. Content is *meaning*
  comprehension (main point, key facts), not vocab recall (taps/cards own that).
  Higher friction than a tag, so keep it optional and likely only near the i+1
  frontier where the difficulty signal matters most. **Not now.**
- **Watch-completion granularity** (watch %, bail point) waits for an in-app video
  player; a `file://` handoff can't measure it honestly. Behavioral signal today is
  still the binary `watched` gate.

---

## Discovery — `/recommend` (ytSearch Phase 1)

> **Built 2026-07-06** — Phase 1 of the discovery half (designed in the sibling
> `../../ytSearch/DESIGN.md`) shipped *inside* fullPipe: `tools/harvest.py` +
> `skills/recommend/SKILL.md`. Backed by a 2026-07-06 research pass (findings +
> `/recommend` decisions in `[[rec-system-acquisition-research]]`; full evidence
> report archived as a Claude artifact). This section is what shipped and why.

The *"what should I watch next?"* half. Same topology as the enjoyment spine
feeds it: `/recommend` reads the `taste_events` + episode-meta the watch loop
records, and hands picks back to `/immerse`. It never writes the ledger — the
crawl pool is a **separate discovery store** (`<work_dir>/discover.db`) so harvest
junk can't pollute the event-sourced truth.

### The decision: independent recommender, not "harvest YouTube's algorithm"

The instinct is to borrow YouTube's own recommender. Two probed facts kill it:
the **unauthenticated personalized home feed is unreachable** (InnerTube
`FEwhat_to_watch` returns empty — "watch something first"), and even reachable it
optimizes watch-time / centroid-convergence — the *opposite* of the
novelty-seeking curiosity engine the taste calls for. So fullPipe builds an
**independent, content-based recommender over structural graph edges that need no
login** — which is also the better fit for the stated taste.

### Topology (dumb tool + smart skill, no cloud LLM)

Same split as everything else here — and the LLM half runs as **the Claude Code
session**, not an API (the user prefers CLI/agent over cloud; precedent: the
`/immerse` punctuation gate moved off `gpt-4o-mini` onto a subagent).

- **`tools/harvest.py`** (dumb) — pulls candidates from three unauthenticated
  edges, all probe-verified reachable with no key/account/PO-token: `related`
  (InnerTube `/next` similarity rail around liked videos), `search` (yt-dlp
  `ytsearchN:` — where the skill's JP query-expansion lands), `rss`
  (`feeds/videos.xml` fresh uploads from known channels). Dedupes against the
  ledger + the store, writes to `discover.db`. Verbs: `seeds · run · list ·
  set-status · refilter · gate-speech`.
- **`/recommend`** (smart) — reads `harvest seeds` (rated history + channels +
  liked ids) and `taste.md`, **expands taste into ~15–20 native JP queries**
  (the single highest-leverage AI step — the genre vocabulary is cultural, not
  translational), drives `harvest run`, then **judges / ranks / diversifies** the
  pool against the objective (relevance × novelty − expertise-redundancy −
  repetition, round-robin across genre clusters, forced wildcards), and hands
  picks to `/immerse` or the worker queue. `about <topic>` narrows the region but
  keeps the variety.

### The synthetic-TTS format filter (two-tier)

The user can't listen to the ゆっくり / VOICEROID / ずんだもん synthetic-narrator
voices that saturate the JP 解説 ecosystem. Filtered in two tiers: **(1)
deterministic** — `discover.format_blocklist` in `config.json` (substrings on
title+channel; matched at `harvest run` time → `status='filtered'`, kept but never
surfaced, à la "log what you prune"). Match the *format compounds* (`ゆっくり解説`,
`【ゆっくり`) **not** bare `ゆっくり` (= "leisurely" — a `ゆっくり散歩` walking vlog is
exactly the loved content). **(2) judgment** — `/recommend` drops any unlabeled
TTS it recognizes. Edit the list, then `harvest refilter` to re-clean the pool.

### The speech gate (and why it's tied to the subtitle path)

A recommendation is worthless if there's no Japanese speech to mine — a wordless
整地 work video or a ジオラマ build can score perfectly on taste and yield zero
cards. Titles never say "silent," so `/recommend` Step 4.5 runs a deterministic
probe (`harvest gate-speech`) on the ranked shortlist and moves the speechless
picks to `status='no_speech'` before presenting. The signal is **YouTube caption
presence**: Japanese speech ⇔ `language=='ja'` or a `ja-orig` ASR caption track
(a plain `ja` auto-caption is a translation; manual `ja` subs can be uploader
text on a silent video — neither counts).

> **⚠️ When we build the no-subtitle acquisition route, this gate must widen.**
> The gate uses *YouTube's own captions* as the proxy for "has speech." That is
> only correct while the pipeline itself depends on those captions. Today the ASR
> path (ElevenLabs Scribe / ReazonSpeech, see the acquire table) is opt-in
> (`--force-transcribe`, or a key/offline-model requirement) — so a video with
> Japanese speech but **no YouTube captions** is genuinely unusable *right now*
> and the gate correctly drops it as `silent`. Once no-subtitle videos become
> first-class (we transcribe them ourselves by default), that same drop becomes a
> **false negative**: those videos are exactly the wider breadth the discovery
> engine should reach, and caption-presence can no longer distinguish "no
> captions but spoken" from "genuinely silent." At that point the gate needs an
> **audio-based** speech signal instead of a caption-based one — e.g. a
> lightweight VAD / short ASR probe on a sampled clip, or reusing the acquire
> transcriber's own speech/no-speech verdict — and `probe_speech()` in
> `tools/harvest.py` + Step 4.5 in `skills/recommend/SKILL.md` should be updated
> together. Keep this note in sync with whichever route ships.

### Account risk & the (deferred) keep-warm layer

Harvesting is anonymous, so it carries no account risk. Feeding watch-signal
*back* to YouTube to keep the real account's own recommendations warm is a
**separate, optional, deferred** layer: fire `yt-dlp --cookies-from-browser
--mark-watched` from the existing `mark_watched()` close-out — one authed ping per
genuine watch, the lowest-risk cookie profile (research: restrictions are
temporary, playback-scoped, and reported mainly on throwaway accounts, not
paced real ones). Not part of `/recommend`; not the engine.

### Corrections the research made to the ytSearch design

- **Featured-channels tab is often gone** (`This channel does not have a channels
  tab`) — the collab graph via description-mining is the reliable version of that
  idea; don't lean on the tab.
- **The `/next` rail is a *similarity* edge, not personalization** — treat it as
  "near what you liked," which is exactly what it's for.

### Deferred (Phase 2, when ratings thicken to ~dozens+)

A local embedding + kNN taste prior (`tools/taste_knn.py`, Ruri v3 — local, no
API) as a continuous predicted-rating score under the judge; plus
playlist-co-occurrence and collab-graph (description-mining) edges for broader
lateral reach. Dormant now — at a handful of ratings the kNN is cold and the LLM
judge carries the pass. See `ytSearch/DESIGN.md` for the full discovery vision.

---

## Card philosophy

**The subs2srs flaw:** each card is one *subtitle line* — a fragment, not a full
sentence — which defeats the purpose of studying whole-sentence usage.

**The resolution (keeps native audio):** `merge_to_sentences` reconstructs complete
sentences from subtitle/ASR fragments (merging trailed-off lines forward, splitting lines
that jammed two sentences together), and `tools/deck.py` cuts the native audio clip to span
that **merged** range (±0.5s pad) and grabs a still frame at the sentence midpoint (from the
phone-staged `video.mp4`, or a local video source; audio-only episodes mint without one — a
missing frame never sinks the card). Result: *full reconstructed sentence + real native
audio of that whole sentence + screenshot.* No TTS. Beats both vanilla subs2srs (fragments)
and AI-sentence+TTS (synthetic audio). The frame lands in the note's `Image` field via the
`field_map`, so the note template decides where/whether it shows.

**Source priority (reordered for an audio-forward learner — native audio is the axis):**
1. **Reconstructed full sentence from the watched video** — native audio, authentic, the
   line you just heard in context (best retention). Primary.
2. **Immersion Kit / Nadeshiko** — native audio from *other* media; fallback when the
   watched line is unsalvageable (mumbled, BGM-drowned, off-screen context).
3. **`phrases-full.db` / AI-generated + TTS** — last resort; text-only → TTS, avoid.

So the giant corpus is the **scoring brain, not the card source**: text is perfect for
computing i+1 leverage, ranking which unknowns unlock the most future sentences,
frequency, "does this word recur enough to deserve a card." *The corpus scores and
selects; the video supplies the actual card.*

**Low volume (~10 new cards/day) is the enabler.** The job is not "mine all 40 i+1
sentences" — it's "surface the ~10 highest-value and curate ruthlessly." Quality bar:
- complete merged sentence (drop fragments/trail-offs),
- clean audio, dialogue not drowned by BGM/SFX,
- target word transcribed correctly and in the card's intended sense/reading,
- prefer the canonical collocation,
- strict i+1: the target is the sentence's *only* unknown (other_unknown_count == 0),
- above the frequency/recurrence floor: not both rare (freq_rank null) and a one-off.

Curation may relax on *incidental surrounding-word* ASR errors (the card's audio is the
real native line regardless of text) but stays strict on the **target word** and the
**audio**. Rank survivors by *leverage × audio-quality*, keep the top ~10. A short pool
under the cap is the expected result of these bars, not a shortfall.

**Consequence for the ledger:** with few cards but fast immersion learning, cards cover
only ~10% of knowledge growth. The **exposure/tap pathways are the primary record** of
what you know — the ledger is the main instrument, not a nice-to-have.

---

## Acquire stage — transcription + AI punctuation

Many sources have no subtitles or poor ones. Because sentence reconstruction keys off
sentence-ending punctuation, a punctuation-restore pass is **load-bearing**, not optional.

```
source subs exist AND has_good_punctuation() ──► use them
        else ▼
   ASR (Scribe / ReazonSpeech / AssemblyAI)     → words + (start,end) [+ speaker]
        → words_to_srt                          → timed blocks, maybe unpunctuated
        → has_good_punctuation()? no ──► punctuation.py (diff, insert-only)
        → merge_to_sentences()                  → complete sentences w/ correct spans
        → cut native audio at sentence boundary → the card
```

**The crown jewel — non-destructive punctuation.** `punctuation.py._extract_punct_insertions`
diffs raw ASR text against the LLM-punctuated text and keeps **only the punctuation the LLM
inserted**, discarding every other "helpful" edit. Naive "ask an LLM to punctuate" rewrites
words and desyncs the text from ASR word-timestamps, so later audio cuts land in the wrong
place. Diff-insert-only makes the pass non-destructive by construction → **word timestamps
stay valid → audio cuts land at real sentence boundaries.** `_realign_to_blocks` maps marks
back onto timed blocks, falling back to the original block on failure.

**Audio-forward implication.** On sub-less sources, ASR *timestamps* determine audio-clip
quality → favor word-level engines (Scribe, AssemblyAI). ReazonSpeech (`nemo-v2`, subword
RNN-T) emits segment spans + per-**subword point** timestamps only — no word start/end
pairs. Adequate for sentence-boundary cuts (map the sentence-final subword's timestamp,
±0.5s pad) but coarser than Scribe: keep Scribe the default, ReazonSpeech the offline
fallback. NeMo's `transcribe(timestamps=True)` and ReazonSpeechX both offer word-level
output — spike before writing any alignment code *(resolved Q5, see P8)*. AssemblyAI diarization sharpens
sentence boundaries in dialogue and lets a card show who's speaking. ASR *text* errors are
less fatal here than for reading-first learners (audio is the real content) — except when
the target word itself is mis-transcribed (drop those).

---

## Skill topology

**Dumb tools** (CLI, deterministic, no AI — vendored engine + ledger CRUD):
`acquire` (file/URL → audio + sentence-segmented transcript) · `coverage` (transcript +
ledger → i+1 flags, ranked unknowns) · `deck` (sentences → cards w/ native audio) ·
`prime` (interleaved audio / m4b) · `render` (analysis → static prep HTML) · `harvest`
(unauthenticated YouTube graph → discovery candidates in `discover.db`) · `ledgerctl`
(the seven verbs).

**Smart skills** (Claude, live, reading real episode data):
- `/immerse <file|url>` — pre-watch orchestrator: runs `acquire` + `coverage`, then *does
  the intelligent analysis directly* (synopsis, thematic keywords, focal-point selection,
  curating the i+1 shortlist), then `render` + `deck`. Writes `exposure` (inert) +
  `mined_card` to the ledger. **When driven from the mobile queue this splits into two
  stages** — an unattended deterministic *prep batch* (acquire + coverage) and a live
  *curate* pass over already-prepared jobs. See `MOBILE.md`.
- `/reconcile` — pull phone taps, run `apply-taps` (implies `mark-watched` for the
  episode; polls minted-card lapses) + `promote`, surface `needs_review`.
- `/generate <topic>` — synthetic i+1 (folds in `ci/`).
- `/replace` — fix existing bad cards in place (folds in the sentence-mining skill).
- `/recommend [about <topic>]` — curiosity orchestrator (discovery half): reads
  the taste on record, expands it into native JP queries, drives `harvest`, then
  judges / ranks / diversifies the candidate pool and hands picks to `/immerse`.
  All judgment inline (no cloud LLM). See the *Discovery* section above.
- `/setup` — config interview → per-user `config.json`.

Intelligence lives in skills, not frozen prompts — validated by the `sentence-mining`
skill, which already has Claude write Japanese explanations inline and curate candidates
per-item rather than shelling out to a fixed API call.

---

## Phone / prerenderable outputs

- **Prep doc** — one self-contained static HTML per episode: synopsis, focal points,
  key-vocab grid, the i+1 sentence list. Opens offline. **Furigana throughout** —
  ruby on every kanji token in sentences AND on Japanese runs inside prose
  (render-time `annotate()` tokenizes them; no client-side dictionary). The vocab
  grid is `word | reading | usage-note | english`: the usage note is the episode's
  actual collocation (Japanese, non-revealing), and the **english ships masked**
  (tap ··· to peek per row, show-all toggle for review) so definitions can't bias
  the know-it/don't-know self-test. Curate's `exclude` list keeps tokenizer
  misparses and product strings out of the doc entirely. Words are tappable
  ("I know this" / "I don't"); taps → `localStorage` → a **"copy corrections"**
  blob pasted into `/reconcile` on desktop. Don't rely on `localStorage` persisting
  *between* opens (iOS treats file:// / Files-app origins inconsistently) — the tap-then-copy
  flow happens in one sitting, which is all it needs. Fallbacks: render the blob as visible
  selectable text (clipboard API denied) and offer `navigator.share` as a second path
  *(resolved Q3, see P9)*. **This copy-blob loop is now the offline fallback** — the primary
  loop is a server-backed **mobile client over Tailscale** (single authoritative ledger on
  the PC; the phone is an offline-tolerant mirror that POSTs taps and pulls prep + video).
  See `MOBILE.md`.
- **Review** — push cards live via **AnkiConnect** to desktop Anki → AnkiWeb sync →
  AnkiMobile/AnkiDroid (cleaner than shuttling `.apkg` files). `.apkg` is the offline
  fallback. The ledger persists independently of Anki being open.

---

## Distribution — the userbase split

Agentic (skill-driven) vs. traditional-UI splits the userbase, but the architecture
protects it: intelligence sits in skills *on top of* dumb, UI-agnostic engine scripts, so
a GUI is an **additive front-end later**, not a rewrite. For now, adopt the
`sentence-mining` skill's shareable pattern: per-user `config.json` (gitignored) + `.env` +
a `/setup` interview probing note types / fields / decks / known-word sources / banks.

---

## Open questions — resolved 2026-07-05

Implementation details for each live in `PROPOSALS.md`.

1. **Exposure comprehension bar** → gate at `promote`, not at write. Write every
   exposure with `known_ratio` + `other_unknown_count` context; v1 promote bar is
   `other_unknown_count = 0` (the target was the sentence's only gap — a true i+1
   event). A flat 0.8 ratio is the wrong shape: it means "only gap" on a 5-token
   sentence but admits a second unknown on a 10-token one. Retunable for free by
   rerunning `promote`.
2. **Demotion aggressiveness** → demote *and* flag for ledger-only words (taps are
   deliberate strong evidence; over-demotion self-heals). For live-Anki-known words
   demotion is a union no-op — route to `needs_review` / REPLACE instead: the tap
   means the card isn't working.
3. **Phone-tap round-trip** → localStorage + copy-blob v1. Zero infra, offline, benign
   failure mode (lost taps = slower convergence, self-heals). Add visible-text and
   share-sheet fallbacks. Defer synced-file until the paste loop actually annoys.
4. **Focal-point weighting** → all three signals, computed deterministically by
   `coverage` (freq_rank · corpus leverage · thematic centrality as columns), weighted
   live per-episode by the model in `/immerse` with a one-line rationale each. The
   skill logs its effective weighting into the episode record so a good fixed
   heuristic can be promoted into the dumb layer after ~20 episodes of data.
5. **ReazonSpeech timestamp granularity** → confirmed **not word-level**: segment
   spans + per-subword point timestamps (subword RNN-T). Fine for sentence-boundary
   cuts; Scribe stays the online default. Spike NeMo `timestamps=True` /
   ReazonSpeechX for word-level before writing alignment code (P8).
6. **Frequency source** → neither Leeds nor JPDB: derive `freq_rank` from
   `japaneseShowGraph.db` **show-penetration** (distinct shows containing the lemma).
   Domain-matched (media register), same tokenizer + SplitMode C, no licensing issues,
   and penetration resists one-show catchphrase inflation. Leeds as fallback for
   lemmas absent from the corpus (P7).

---

## Proposed project structure

```
fullPipe/                     # ✅ = built 2026-07-05 (see README.md for usage)
├── DESIGN.md                 # this document
├── README.md                 # ✅ setup, layout, CLI reference
├── config.example.json       # ✅ decks, known-sources, asr, freq paths
├── .env.example              # ✅ API keys (OpenAI punctuation, ElevenLabs ASR)
├── lib_config.py             # ✅ config.json + .env loader shared by all tools
├── engine/                   # ✅ vendored from audioPrimeProd + ankiDeckMaker (pure fns)
│   ├── paths.py              # ✅ ffmpeg/ffprobe/yt-dlp from PATH (replaces bin_paths)
│   ├── downloader.py  transcriber.py  srt_parser.py  punctuation.py  # ✅
│   ├── local_file.py  audio.py                                       # ✅
│   ├── lemma.py              # ✅ NEW — SudachiPy mode C + coverage analysis
│   ├── anki.py  tts.py       # ✅ (anki.py adds stable note guids)
│   └── (interleaver.py, m4b.py deferred to PRIME mode — GUI-coupled deps)
├── ledger/
│   ├── schema.sql            # ✅ the tables above
│   ├── ledgerctl.py          # ✅ ledger verbs + promote + taste (record_rating/query_enjoyment/record_curation)
│   ├── anki_known.py         # ✅ live known-set recompute (SudachiPy + AnkiConnect)
│   └── build_freq.py         # ✅ P7 show-penetration ranks
├── tools/                    # ✅ dumb CLI: acquire, coverage, deck, render, harvest (discovery)
│   └── (prime deferred with the interleaver)
├── tests/                    # ✅ 93 unittest cases (ledger/tools/server/engine)
├── skills/                   # ✅ /immerse, /prepare, /recommend (+ scripts/ensure_anki.sh); NEXT: /reconcile, /setup, /generate, /replace
├── render/                   # ✅ template.html + demo-prep.html (also hydrated by GET /prep)
├── server/                   # ✅ FastAPI queue + ledgerctl verbs over Tailscale (MOBILE.md); taste tags on /rating
└── (mobile client)           # ✅ Capacitor Android app → sibling repo anki/mobile/ (MOBILE.md); stars + taste-tag picker
```

---

## Next steps

1. ~~Resolve the open questions~~ — done 2026-07-05 (accepted proposals in `PROPOSALS.md`).
2. ~~Scaffold `fullPipe/`~~ — done 2026-07-05: engine vendored on SudachiPy mode C,
   `ledger/schema.sql` + `ledgerctl` seven verbs (P2–P6), `build_freq` (P7, 295k
   lemmas from the real corpus), 37 tests passing.
3. ~~Dumb tools~~ — done 2026-07-05: `acquire` · `coverage` · `deck` · `render`
   implement the MINE flow mechanics (acquire → classify → rank candidates →
   native-audio cards via AnkiConnect w/ note ids → prep doc with the P9 tap loop).
4. **Build `/immerse` end-to-end on one real episode** — the skill chains the tools
   and does the live curation (synopsis, focal points, ~15-card shortlist); verify
   native-audio full-sentence cards and a working prep doc before widening scope.
   Then `/reconcile` (apply-taps + promote + unwatched report) and `/setup`.
   *2026-07-05: **done.** Skill written (`skills/immerse/SKILL.md`, discovered
   via the repo's `.claude/skills` symlink; `skills/scripts/ensure_anki.sh`
   preflight), smoke-verified offline, then run on a real episode
   (yt_2LjsOMpzJ8E, 散歩録: 119 sentences → 7 curated native-audio cards onto
   the user's own 'Sentence Cards' note type via deck.note_type/field_map,
   prep doc, 482 inert exposures). En route this hardened: `import-known`
   bootstrap verb + kana-form bridge in materialize-known, list-valued
   field_map (Anki's first-field-non-empty rule), furigana + masked-definition
   vocab grid in the prep doc, and the curate `exclude` junk filter. Remaining
   from this step: `/reconcile` and `/setup`.*
5. Before corpus-leverage scoring: re-parse `phrases-full.db` at mode C (P1);
   spike ReazonSpeech word timestamps before offline alignment code (P8).
6. Mobile client + sync server (`MOBILE.md`): stand up `server/` (job queue + verbs over
   Tailscale), split `/immerse` into prep-batch + curate stages, scaffold the Capacitor
   client, and prove the overnight queue → wake-up-ready flow on one batch.
   (Ledger-side groundwork already in: `tap_batches` idempotency, per-artifact
   staging layout, `apply_taps`/`poll_lapses` as importable functions.)
   *2026-07-05: **done** — `server/` (FastAPI over Tailscale, Stage-1 worker) and
   the Capacitor client at `anki/mobile/` built; `/prepare` skill added for local
   Stage 1.*
7. **Taste metadata** — *done 2026-07-06.* Append-only `taste_events` log + the
   episode-meta columns; `record_rating` / `query_enjoyment` (on-read verdict with
   the `over_my_head` decoupling) / `update_episode_meta` / `record_curation`.
   yt-dlp provenance rescued in acquire, coverage-at-watch snapshot, and the
   `/immerse` `{genre, format, topics, difficulty_felt}` curation block all land on
   the `episodes` row. `POST /rating` takes tags; `GET /jobs` carries them back;
   the mobile app grew the stars + six-tag picker. Verified phone → server → ledger.
8. **Discovery — `/recommend` Phase 1** — *done 2026-07-06.* Preceded by a research
   pass (independent recommender over the unauthenticated graph beats harvesting
   YouTube's unreachable, wrong-fit personalized feed). `tools/harvest.py`
   (related / search / rss edges → `discover.db`, deduped vs. the ledger) +
   `skills/recommend/SKILL.md` (seeds → JP query-expansion → judge/rank/diversify
   → handoff to `/immerse`), all judgment inline (no cloud LLM). Two-tier
   synthetic-TTS (ゆっくり/VOICEROID) format filter via `discover.format_blocklist`.
   Verified end-to-end on the real ledger. **Next:** Phase 2 — local Ruri kNN
   prior + playlist/collab edges when ratings thicken; the optional
   `--mark-watched` keep-warm layer. See the *Discovery* section.
```
