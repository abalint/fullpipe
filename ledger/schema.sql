-- Immersion Workstation ledger (DESIGN.md — The Ledger).
-- Event-sourced: `evidence` is the append-only truth; `lemmas` is a cached
-- projection recomputed by `promote`. Persists only the delta Anki can't see
-- (exposure + taps) — the Anki-known set is recomputed live, never stored.
-- P2: no `mature` state. P3: episode_id is a real column. P4: exposure
-- idempotency via partial unique index.

PRAGMA journal_mode = WAL;

-- Cached projection of the NON-Anki verdict. Fast reads for materialize-known.
-- Holds two item kinds sharing one key space (GRAMMAR.md — one ledger, three
-- item kinds): kind='word' (SudachiPy dictionary form) and kind='phrase'
-- (JMdict multi-token headword, e.g. 気を付ける). Grammar has its own
-- projection table (grammar_points) — its key is the pattern string.
CREATE TABLE IF NOT EXISTS lemmas (
    lemma          TEXT PRIMARY KEY,      -- SudachiPy dictionary form / JMdict phrase headword = join key
    kind           TEXT NOT NULL DEFAULT 'word',  -- word|phrase
    reading        TEXT,                  -- homograph disambiguation aid
    pos            TEXT,
    freq_rank      INTEGER,               -- from freq table; NULL if absent
    status         TEXT NOT NULL DEFAULT 'unknown',   -- unknown|learning|known
    confidence     REAL NOT NULL DEFAULT 0,
    exposure_count INTEGER NOT NULL DEFAULT 0,         -- activated (watched) exposures
    episode_spread INTEGER NOT NULL DEFAULT 0,         -- distinct watched episodes
    needs_review   INTEGER NOT NULL DEFAULT 0,         -- conflict → /reconcile queue
    confirm_candidate INTEGER NOT NULL DEFAULT 0,      -- exposures crossed θ → ask the user (not auto-known)
    first_seen TEXT, last_seen TEXT, updated_at TEXT NOT NULL
);

-- Append-only. THE truth. lemmas.status above is a projection of this.
-- The item key ALWAYS lives in `lemma` (word lemma / phrase headword / grammar
-- pattern); `kind` selects which projection table the row feeds (GRAMMAR.md
-- — Schema & idempotency).
CREATE TABLE IF NOT EXISTS evidence (
    id         INTEGER PRIMARY KEY,
    lemma      TEXT NOT NULL, -- the item key: word lemma | phrase headword | grammar pattern
    kind       TEXT NOT NULL DEFAULT 'word', -- word|phrase|grammar
    source     TEXT NOT NULL, -- exposure|tap_known|tap_unknown|tap_interest|mined_card|card_lapse|import
    polarity   INTEGER NOT NULL,-- +1 / -1 / 0(=learning)
    weight     REAL NOT NULL DEFAULT 1.0,
    episode_id TEXT,          -- real column, not JSON: watched-gate + spread query it (P3)
    context    TEXT,          -- JSON: {sentence_idx, known_ratio, other_unknown_count, ...}
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_lemma   ON evidence(lemma);
CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence(episode_id);
-- Idempotent /immerse re-runs: at most one exposure row per item per episode
-- (P4). `kind` is part of the key so a word and a grammar pattern sharing a
-- string can't collide. Also caps a talky episode at one exposure — spread
-- does the multi-episode work.
CREATE UNIQUE INDEX IF NOT EXISTS idx_exposure_once ON evidence(kind, lemma, episode_id, source)
    WHERE source = 'exposure';

-- Grammar projection (GRAMMAR.md): the taxonomy spine + promote's cached
-- verdict, keyed by the canonical pattern string (= evidence.lemma for
-- kind='grammar'). Seeded once from ledger/grammar_taxonomy.json
-- (`ledgerctl grammar-seed`); per-episode curation only matches into it or
-- proposes into grammar_proposed — nothing becomes a tracked key silently.
CREATE TABLE IF NOT EXISTS grammar_points (
    pattern TEXT PRIMARY KEY,          -- 〜てしまう (the join key)
    level INTEGER,                     -- JLPT tier 5=N5 … 1=N1; NULL = unplaced (strictest θ)
    gloss TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',   -- unknown|learning|known
    confidence REAL NOT NULL DEFAULT 0,
    exposure_count INTEGER NOT NULL DEFAULT 0,
    episode_spread INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    confirm_candidate INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT, last_seen TEXT, updated_at TEXT NOT NULL
);

-- Patterns curation met that aren't in the taxonomy (colloquial/dialectal) —
-- the deliberate-growth gate. Approving one (`ledgerctl grammar-approve`)
-- moves it into grammar_points; until then it is NOT a tracked key.
CREATE TABLE IF NOT EXISTS grammar_proposed (
    pattern TEXT PRIMARY KEY,
    example TEXT,                      -- one sentence it was seen in
    gloss TEXT,                        -- the proposer's one-line description
    seen INTEGER NOT NULL DEFAULT 1,   -- distinct sightings (bumped on re-propose)
    first_seen TEXT
);

-- Episodes: enables spread, idempotent exposure, and the watched-gate.
-- The metadata columns below are the enjoyment metric's attribution features
-- (DESIGN.md — Taste metadata). Split by how the recommender uses each: scalars
-- it filters/groups/correlates on are real columns; bulky embed-only payload
-- (description, tags[], topics[], view_count) lives in the metadata JSON blob.
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,      -- stable hash of source
    title TEXT, source TEXT, kind TEXT,       -- youtube|local
    watched INTEGER DEFAULT 0,                 -- gates exposure activation
    rating INTEGER,                            -- latest-rating cache; the truth is taste_events
    rated_at TEXT,                             -- latest rating's timestamp (cache)
    channel TEXT, channel_id TEXT,             -- yt-dlp provenance (strongest taste predictor)
    duration REAL, upload_date TEXT,           -- yt-dlp provenance (YYYYMMDD)
    genre TEXT, format TEXT,                    -- /immerse curation (ytSearch bandit arms)
    difficulty_felt INTEGER,                    -- /immerse subjective difficulty (1–5)
    coverage_pct REAL,                          -- coverage-at-watch: the difficulty confound control
    iplus1_count INTEGER, known_set_size INTEGER,
    comprehension_pct REAL,                     -- latest /debrief measured comprehension (cache; truth = debriefs)
    language_pct REAL,                          -- latest /debrief audio-only subtotal (cache)
    debriefed_at TEXT,                          -- latest debrief's timestamp (cache)
    metadata TEXT,                              -- JSON: description, tags[], topics[], view_count
    processed_at TEXT
);

-- Append-only taste log (DESIGN.md — Taste metadata: "enjoyment is a
-- projection, not a column"; SURVEY.md — the multi-axis post-watch survey).
-- One review = one 'rating' row + one row per graded survey axis + one 'tag'
-- row per chip + optional 'follow'/'note', all sharing review_id. Re-rating
-- appends a NEW batch (drift preserved, nothing overwritten); the enjoyment
-- verdict is computed on read (query_enjoyment) — no materialized cache.
CREATE TABLE IF NOT EXISTS taste_events (
    id         INTEGER PRIMARY KEY,
    episode_id TEXT NOT NULL,
    review_id  TEXT NOT NULL,   -- groups a review act's rows
    kind       TEXT NOT NULL,   -- rating | tag | <survey axis> | follow | note
    value      TEXT NOT NULL,   -- rating/axis: '1'..'5' (rating also 'clear') ·
                                -- tag: slug · follow: block|less|neutral|more · note: text
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_taste_episode ON taste_events(episode_id);

-- Append-only /debrief log (DESIGN.md — Measured comprehension). One row per
-- post-watch comprehension interview: the rubric's two scores plus the scored
-- question list. Survives swipe-delete (episode artifacts don't), so this is
-- the durable side of the coverage→comprehension calibration: coverage_pct is
-- the ledger's *prediction* of difficulty, comprehension_pct is the *measured*
-- outcome, and their gap over time is both the improvement curve and the
-- error signal for the coverage model. Re-debriefs append (drift preserved);
-- episodes.comprehension_pct/language_pct/debriefed_at cache the latest.
CREATE TABLE IF NOT EXISTS debriefs (
    id                INTEGER PRIMARY KEY,
    episode_id        TEXT NOT NULL,
    debrief_id        TEXT NOT NULL,  -- idempotency: a replayed debrief_id is a no-op
    comprehension_pct REAL NOT NULL,  -- 0..1 airtime-weighted rubric total (episode comprehension)
    language_pct      REAL,           -- 0..1 audio-only-probe subtotal; NULL = none asked
    lag_days          REAL,           -- watch → debrief gap the scores are conditioned on
    questions         TEXT,           -- JSON rubric: [{q, weight, score, audio_only, note}, …]
    ts                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_debriefs_episode ON debriefs(episode_id);

-- Per-presenter durable state (SURVEY.md §4). Channels were previously derived
-- from episodes.channel_id with MAX(rating) as their only signal; this table
-- gives them two things that don't belong on a video: a follow intent decoupled
-- from any single video's score, and an accumulated presenter fingerprint.
-- The fingerprint is built INCREMENTALLY at curate time because raw transcripts
-- are purged after watch — the profile is the durable memory, the transcript is
-- ephemeral. Verdicts are NOT stored here (joined from taste_events by
-- channel_id); this is purely the content-derived feature track.
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY,
    channel      TEXT,
    follow_state TEXT,     -- block|less|neutral|more (latest; NULL = never set)
    profile      TEXT,     -- JSON presenter fingerprint (SURVEY.md §4c); NULL until curated
    updated_at   TEXT
);

-- Minted cards: enables graduation/lapse feedback + prevents re-mining.
-- anki_note_id is set when the card is pushed via AnkiConnect (the primary
-- path) and is what lapse polling queries; anki_guid covers the .apkg
-- fallback (genanki stable guid → match on a later resync).
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    lemma TEXT, episode_id TEXT, sentence TEXT,
    anki_guid TEXT,
    anki_note_id INTEGER,
    lapses INTEGER NOT NULL DEFAULT 0,        -- last polled value (P6)
    deleted_at TEXT,                          -- set when lapse-poll finds the note gone
                                              -- from Anki (user deleted a sub-par card);
                                              -- re-opens the lemma for re-mining
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_lemma ON cards(lemma);

-- Frequency prior (P7): show-penetration ranks from japaneseShowGraph.db,
-- Leeds fallback for lemmas absent from the corpus. Loaded by build_freq.py.
CREATE TABLE IF NOT EXISTS freq (
    lemma       TEXT PRIMARY KEY,
    rank        INTEGER NOT NULL,
    penetration INTEGER,          -- distinct shows containing the lemma (NULL for fallback rows)
    source      TEXT NOT NULL     -- show_graph|leeds
);

-- Idempotent tap flushes from the mobile client (MOBILE.md — sync semantics):
-- a re-POST of the same batch_id is a no-op.
CREATE TABLE IF NOT EXISTS tap_batches (
    batch_id   TEXT PRIMARY KEY,
    episode_id TEXT,
    applied_at TEXT NOT NULL
);

-- Cross-episode non-vocabulary registry (the repair gate's adjudications):
-- names ASR/Sudachi misread as ordinary words (いぶき, ともしげ) and ASR
-- non-words. Every coverage run excludes these keys (matched against token
-- lemma AND surface), so a presenter flagged once never pollutes another
-- episode's unknowns — including the worker's first parse of a new episode.
CREATE TABLE IF NOT EXISTS non_vocab (
    key    TEXT PRIMARY KEY,  -- lemma or surface to exclude
    kind   TEXT NOT NULL,     -- name | nonword
    note   TEXT,              -- who/what it is (the subagent's note)
    origin TEXT,              -- episode_id that first flagged it
    ts     TEXT NOT NULL
);

-- Immersion time log (MOBILE.md — viewing time): one row per playback
-- session the phone recorded. `secs` is wall-clock seconds actually spent
-- playing — a rewound stretch counts again, a pause counts nothing — so the
-- sum is the honest "hours of exposure" number. `reached` vs `duration`
-- says whether the episode was finished. Client-minted ids make the outbox
-- replay-safe; `day` is the DEVICE-local calendar day the time belongs to
-- (the phone decides what "today" is, not the server's clock).
CREATE TABLE IF NOT EXISTS view_sessions (
    id          TEXT PRIMARY KEY,   -- client-minted (replay dedup)
    episode_id  TEXT NOT NULL,
    title       TEXT,               -- snapshot: survives episode deletion
    kind        TEXT NOT NULL,      -- watch (in-app player) | listen (passive service)
    day         TEXT NOT NULL,      -- device-local YYYY-MM-DD
    start       TEXT NOT NULL,      -- ISO wall-clock start of the session
    secs        REAL NOT NULL,      -- wall-clock seconds spent playing
    reached     REAL,               -- furthest media position seen (s)
    duration    REAL,               -- media length (s), when the client knew it
    source      TEXT NOT NULL DEFAULT 'app',  -- app (recorded) | manual (typed in) | import (historic sheet)
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_view_sessions_day ON view_sessions(day);
