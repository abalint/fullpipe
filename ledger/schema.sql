-- Immersion Workstation ledger (DESIGN.md — The Ledger).
-- Event-sourced: `evidence` is the append-only truth; `lemmas` is a cached
-- projection recomputed by `promote`. Persists only the delta Anki can't see
-- (exposure + taps) — the Anki-known set is recomputed live, never stored.
-- P2: no `mature` state. P3: episode_id is a real column. P4: exposure
-- idempotency via partial unique index.

PRAGMA journal_mode = WAL;

-- Cached projection of the NON-Anki verdict. Fast reads for materialize-known.
CREATE TABLE IF NOT EXISTS lemmas (
    lemma          TEXT PRIMARY KEY,      -- SudachiPy dictionary form = join key
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
CREATE TABLE IF NOT EXISTS evidence (
    id         INTEGER PRIMARY KEY,
    lemma      TEXT NOT NULL,
    source     TEXT NOT NULL, -- exposure|tap_known|tap_unknown|tap_interest|mined_card|card_lapse|import
    polarity   INTEGER NOT NULL,-- +1 / -1 / 0(=learning)
    weight     REAL NOT NULL DEFAULT 1.0,
    episode_id TEXT,          -- real column, not JSON: watched-gate + spread query it (P3)
    context    TEXT,          -- JSON: {sentence_idx, known_ratio, other_unknown_count, ...}
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_lemma   ON evidence(lemma);
CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence(episode_id);
-- Idempotent /immerse re-runs: at most one exposure row per lemma per episode (P4).
-- Also caps a talky episode at one exposure — spread does the multi-episode work.
CREATE UNIQUE INDEX IF NOT EXISTS idx_exposure_once ON evidence(lemma, episode_id, source)
    WHERE source = 'exposure';

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
    metadata TEXT,                              -- JSON: description, tags[], topics[], view_count
    processed_at TEXT
);

-- Append-only taste log (DESIGN.md — Taste metadata: "enjoyment is a
-- projection, not a column"). One review = one 'rating' row + one 'tag' row per
-- selected tag, sharing review_id. Re-rating appends a NEW batch (drift
-- preserved, nothing overwritten); the enjoyment verdict is computed on read
-- (query_enjoyment) — no materialized cache, taste volume doesn't warrant one.
CREATE TABLE IF NOT EXISTS taste_events (
    id         INTEGER PRIMARY KEY,
    episode_id TEXT NOT NULL,
    review_id  TEXT NOT NULL,   -- groups the rating + its tags into one review act
    kind       TEXT NOT NULL,   -- rating|tag
    value      TEXT NOT NULL,   -- rating: '1'..'5' | 'clear'  ·  tag: slug
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_taste_episode ON taste_events(episode_id);

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
