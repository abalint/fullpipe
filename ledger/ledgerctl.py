#!/usr/bin/env python3
"""ledgerctl — the ledger's verbs (DESIGN.md — The Ledger).

    materialize-known    → live-Anki-known ∪ ledger-promoted; the set every mode reads
    compute-anki-known   → live recompute via AnkiConnect (cached ~6h)
    record-exposure      ← written by /immerse at analysis time; inert until watched
    mark-watched         ← flips episodes.watched=1 (P5)
    apply-taps           ← phone corrections; implies mark-watched; polls lapses (P6)
    promote              → recompute projection from evidence (the state machine)
    confirm / defer      ← answer the exposure prompt (known 'yes' / snooze 'not yet')
    rate                 ← 1-5 star rating + taste tags → append-only taste_events
    record-curation      ← /immerse curation block (genre/format/topics/difficulty)
    query                → coverage %, needs_review queue, evidence audits, ratings

Raw evidence is append-only truth; lemmas.status is a projection — rerunning
`promote` over evidence you already have is how thresholds get retuned.

CLI:
    python -m ledger.ledgerctl [--config PATH] [--db PATH] VERB [args]
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

from lib_config import load_config  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Evidence semantics (DESIGN.md — evidence sources table).
# tap_interest ("I want to learn this") is a *want*, not a knowledge claim:
# polarity/weight 0 so `promote` never reads it as known/learning. It persists
# across episodes and is retired only when the lemma becomes known (see
# active_interest).
# confirm_known is a deliberate "yes, I know it" answer to the exposure-triggered
# prompt — a knowledge claim as strong as a tap. confirm_defer ("not yet") is a
# scheduling signal, not knowledge: neutral polarity/weight, it only snoozes the
# re-prompt (see promote — the candidate rule).
POLARITY = {"exposure": 1, "tap_known": 1, "tap_unknown": -1, "tap_interest": 0,
            "mined_card": 0, "card_lapse": -1, "import": 1,
            "confirm_known": 1, "confirm_defer": 0}
WEIGHT = {"exposure": 1.0, "tap_known": 3.0, "tap_unknown": 3.0, "tap_interest": 0.0,
          "mined_card": 1.0, "card_lapse": 2.0, "import": 2.0,  # import: strong, but below a deliberate tap
          "confirm_known": 3.0, "confirm_defer": 0.0}

# Taste metadata (DESIGN.md — Taste metadata). Scalar columns the recommender
# filters/groups/correlates on; bulky embed-only payload goes to metadata JSON.
_EPISODE_META_COLUMNS = frozenset((
    "channel", "channel_id", "duration", "upload_date",
    "genre", "format", "difficulty_felt",
    "coverage_pct", "iplus1_count", "known_set_size",
))

# The six taste tags (DESIGN.md — "The tags"). Confound-breakers + attributors.
RATING_TAGS = frozenset((
    "already_knew", "over_my_head", "didnt_grab", "format_miss",  # negatives
    "fascinating", "loved_format",                                # positives
))
# Over-my-head censors the taste label: a difficulty-driven low star is not a
# taste-low, so it's excluded from the taste manifold (taste_valid=0).
_DIFFICULTY_TAG = "over_my_head"

# Frequency prior: per-lemma exposure threshold θ and episode-spread k,
# scaled by freq_rank (DESIGN.md — "kills the old slider").
# (max_rank_exclusive, theta_exposures, spread_episodes); None = rare/absent.
THETA_TABLE = [(2000, 2, 2), (10000, 4, 3), (None, 6, 4)]


def theta_for(freq_rank):
    for max_rank, theta, spread in THETA_TABLE:
        if max_rank is None or (freq_rank is not None and freq_rank < max_rank):
            return theta, spread
    return THETA_TABLE[-1][1], THETA_TABLE[-1][2]


# Grammar difficulty prior (GRAMMAR.md): θ exposures + episode spread from the
# JLPT tier — the difficulty analogue of THETA_TABLE, since grammar points have
# no corpus freq rank. Keys are levels 5=N5 (easiest) … 1=N1; a pattern with no
# tier (an approved proposal that was never placed) gets the strictest bar.
GRAMMAR_THETA = {5: (2, 2), 4: (2, 2), 3: (3, 3), 2: (4, 3), 1: (5, 4)}


def grammar_theta_for(level):
    return GRAMMAR_THETA.get(level, GRAMMAR_THETA[1])


# An exposure "qualifies" toward θ when the learner could parse the sentence
# around the item. Words carry other_unknown_count == 0 (Q1); phrase/grammar
# exposures carry the sentence's coverage classification instead — anything
# short of too_hard parses.
QUALIFYING_CLASSIFICATIONS = frozenset(("comprehensible", "i_plus_1", "reinforcement"))


def _exposure_qualifies(ctx):
    if ctx.get("other_unknown_count", 99) == 0:
        return True
    return ctx.get("classification") in QUALIFYING_CLASSIFICATIONS


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(db_path):
    """Connect and ensure the schema exists (idempotent)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    return conn


def _migrate(conn):
    """Additive column migrations — CREATE IF NOT EXISTS in schema.sql never
    touches a pre-existing table, so new columns must be bolted on here."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
    for col, decl in (
        ("rating", "rating INTEGER"), ("rated_at", "rated_at TEXT"),
        ("channel", "channel TEXT"), ("channel_id", "channel_id TEXT"),
        ("duration", "duration REAL"), ("upload_date", "upload_date TEXT"),
        ("genre", "genre TEXT"), ("format", "format TEXT"),
        ("difficulty_felt", "difficulty_felt INTEGER"),
        ("coverage_pct", "coverage_pct REAL"),
        ("iplus1_count", "iplus1_count INTEGER"),
        ("known_set_size", "known_set_size INTEGER"),
        ("metadata", "metadata TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE episodes ADD COLUMN {decl}")
    card_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
    if "deleted_at" not in card_cols:
        conn.execute("ALTER TABLE cards ADD COLUMN deleted_at TEXT")
    lemma_cols = {r["name"] for r in conn.execute("PRAGMA table_info(lemmas)")}
    if "confirm_candidate" not in lemma_cols:
        conn.execute("ALTER TABLE lemmas ADD COLUMN confirm_candidate INTEGER NOT NULL DEFAULT 0")
    if "kind" not in lemma_cols:
        conn.execute("ALTER TABLE lemmas ADD COLUMN kind TEXT NOT NULL DEFAULT 'word'")
    ev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evidence)")}
    if "kind" not in ev_cols:
        conn.execute("ALTER TABLE evidence ADD COLUMN kind TEXT NOT NULL DEFAULT 'word'")
    # idx_exposure_once gained `kind` (GRAMMAR.md). schema.sql's CREATE INDEX
    # IF NOT EXISTS silently no-ops on the pre-kind shape (same name), so
    # detect the old shape here and recreate — after the ALTERs above.
    idx = [r["name"] for r in conn.execute("PRAGMA index_info(idx_exposure_once)")]
    if idx and idx[0] != "kind":
        conn.execute("DROP INDEX idx_exposure_once")
        conn.execute(
            """CREATE UNIQUE INDEX idx_exposure_once
               ON evidence(kind, lemma, episode_id, source) WHERE source = 'exposure'""")
    conn.commit()


# --- write verbs -------------------------------------------------------------

def _touch_lemma(conn, lemma, reading=None, pos=None, ts=None, kind="word"):
    """Ensure a lemmas row exists; fill reading/pos if newly learned.

    kind is set on creation only — an existing row keeps its kind (first
    writer wins; phrase rows are only ever created by the deliberate phrase
    paths, so a later default-'word' touch of the same key must not demote
    it)."""
    ts = ts or now_iso()
    conn.execute(
        """INSERT INTO lemmas (lemma, kind, reading, pos, first_seen, last_seen, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(lemma) DO UPDATE SET
               reading = COALESCE(lemmas.reading, excluded.reading),
               pos     = COALESCE(lemmas.pos, excluded.pos),
               last_seen = excluded.last_seen""",
        (lemma, kind, reading, pos, ts, ts, ts),
    )


def update_episode_meta(conn, episode_id, columns=None, metadata=None):
    """Upsert descriptive metadata onto an episode row — the enjoyment metric's
    attribution features (DESIGN.md — Taste metadata). Shared by acquire
    (yt-dlp provenance), coverage (coverage-at-watch), and /immerse curation.

    columns: whitelisted scalar columns (unknown keys ignored, so callers can
             pass a superset). metadata: merged into the episodes.metadata JSON
             blob (description/tags/topics/view_count — bulky embed-only payload).
    """
    columns = {k: v for k, v in (columns or {}).items()
               if k in _EPISODE_META_COLUMNS and v is not None}
    metadata = {k: v for k, v in (metadata or {}).items() if v is not None}
    ts = now_iso()
    conn.execute(
        "INSERT INTO episodes (id, processed_at) VALUES (?, ?) "
        "ON CONFLICT(id) DO NOTHING", (episode_id, ts))
    if columns:
        assignments = ", ".join(f"{c} = ?" for c in columns)
        conn.execute(f"UPDATE episodes SET {assignments} WHERE id = ?",
                     (*columns.values(), episode_id))
    if metadata:
        row = conn.execute(
            "SELECT metadata FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        merged = json.loads(row["metadata"]) if row and row["metadata"] else {}
        merged.update(metadata)
        conn.execute("UPDATE episodes SET metadata = ? WHERE id = ?",
                     (json.dumps(merged, ensure_ascii=False), episode_id))
    conn.commit()
    return {"episode_id": episode_id, "columns": sorted(columns),
            "metadata_keys": sorted(metadata)}


# Acquire's transcript.json episode block → episode row: scalar columns vs. the
# bulky JSON payload (DESIGN.md — "Rescue the discarded yt-dlp dump").
_ACQUIRE_META_COLUMNS = ("channel", "channel_id", "duration", "upload_date")
_ACQUIRE_META_JSON = ("view_count", "description", "tags")


def record_exposure(conn, episode, exposures):
    """Write inert exposure evidence for one analyzed episode.

    episode:   {"id", "title", "source", "kind"}
    exposures: {lemma: {"sentence_idx", "known_ratio", "other_unknown_count",
                        "reading", "pos"}}  (engine.lemma.analyze_transcript shape)

    Exposures are written unconditionally with their sentence context; the
    comprehension bar is applied at `promote`, not here (resolved Q1). The
    partial unique index makes re-runs no-ops (P4).
    """
    ts = now_iso()
    conn.execute(
        """INSERT INTO episodes (id, title, source, kind, processed_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               title = excluded.title, source = excluded.source,
               kind = excluded.kind, processed_at = excluded.processed_at""",
        (episode["id"], episode.get("title"), episode.get("source"),
         episode.get("kind"), ts),
    )
    written = 0
    for lemma, ctx in exposures.items():
        # kind rides in the context dict ('phrase' for tracked-phrase units
        # coverage detected; default 'word'). Only pre-existing phrase keys
        # ever arrive here — new phrase keys are created by record_curate_items.
        kind = ctx.get("kind", "word")
        _touch_lemma(conn, lemma, ctx.get("reading"), ctx.get("pos"), ts, kind=kind)
        context = {k: ctx[k] for k in ("sentence_idx", "known_ratio",
                                       "other_unknown_count", "classification")
                   if k in ctx}
        cur = conn.execute(
            """INSERT OR IGNORE INTO evidence
               (lemma, kind, source, polarity, weight, episode_id, context, ts)
               VALUES (?, ?, 'exposure', ?, ?, ?, ?, ?)""",
            (lemma, kind, POLARITY["exposure"], WEIGHT["exposure"],
             episode["id"], json.dumps(context, ensure_ascii=False), ts),
        )
        written += cur.rowcount
    # Persist the yt-dlp provenance acquire stashed in the episode block
    # (DESIGN.md — Taste metadata). Absent on local files / minimal payloads.
    cols = {k: episode.get(k) for k in _ACQUIRE_META_COLUMNS}
    meta = {k: episode.get(k) for k in _ACQUIRE_META_JSON}
    if any(v is not None for v in (*cols.values(), *meta.values())):
        update_episode_meta(conn, episode["id"], columns=cols, metadata=meta)
    conn.commit()
    return {"episode_id": episode["id"], "lemmas": len(exposures), "new_rows": written}


def record_mined_cards(conn, episode_id, cards):
    """Register minted cards: mined_card evidence + a cards row each.

    cards: [{"lemma", "sentence", "anki_guid", "anki_note_id", "kind"?}]
    (Called by the deck tool after pushing; kept here so every evidence
    source has exactly one writer in the ledger layer. kind defaults to
    'word' — a minted phrase card passes kind='phrase'.)
    """
    ts = now_iso()
    for c in cards:
        kind = c.get("kind", "word")
        _touch_lemma(conn, c["lemma"], ts=ts, kind=kind)
        conn.execute(
            """INSERT INTO evidence (lemma, kind, source, polarity, weight, episode_id, context, ts)
               VALUES (?, ?, 'mined_card', ?, ?, ?, ?, ?)""",
            (c["lemma"], kind, POLARITY["mined_card"], WEIGHT["mined_card"], episode_id,
             json.dumps({"sentence": c.get("sentence", "")[:200]}, ensure_ascii=False), ts),
        )
        conn.execute(
            """INSERT INTO cards (lemma, episode_id, sentence, anki_guid, anki_note_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (c["lemma"], episode_id, c.get("sentence"), c.get("anki_guid"),
             c.get("anki_note_id"), ts),
        )
    conn.commit()
    return {"episode_id": episode_id, "cards": len(cards)}


def import_known(conn, lemmas, origin="import"):
    """Bulk-seed known lemmas from an external list (bootstrap aid).

    E.g. an AnkiMorphs known-morphs export. One 'import' evidence row per
    lemma — strong positive, but weaker than a deliberate tap, and a fresh
    tap_unknown demotes it quietly (no needs_review: bulk lists are noisy,
    the user's correction just wins). Idempotent: lemmas that already have
    an import row are skipped, so re-importing an updated export only adds
    the new ones.

    Tokenizer caveat: external lists may come from a different lemmatizer
    (AnkiMorphs = MeCab); forms that don't match SudachiPy mode C
    dictionary forms simply never join against transcripts — harmless.
    """
    ts = now_iso()
    existing = {r[0] for r in conn.execute(
        "SELECT lemma FROM evidence WHERE source = 'import'")}
    context = json.dumps({"origin": origin}, ensure_ascii=False)
    total = added = 0
    for lemma in lemmas:
        lemma = lemma.strip()
        if not lemma:
            continue
        total += 1
        if lemma in existing:
            continue
        _touch_lemma(conn, lemma, ts=ts)
        conn.execute(
            """INSERT INTO evidence (lemma, source, polarity, weight, episode_id, context, ts)
               VALUES (?, 'import', ?, ?, NULL, ?, ?)""",
            (lemma, POLARITY["import"], WEIGHT["import"], context, ts),
        )
        existing.add(lemma)
        added += 1
    conn.commit()
    return {"origin": origin, "listed": total, "imported": added,
            "already_imported": total - added}


def mark_watched(conn, episode_id):
    """Flip the watched-gate: this episode's exposures become active (P5)."""
    cur = conn.execute("UPDATE episodes SET watched = 1 WHERE id = ?", (episode_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"episode not found in ledger: {episode_id}")
    return {"episode_id": episode_id, "watched": True}


def record_rating(conn, episode_id, rating, tags=None, review_id=None):
    """Append a taste-review event batch (DESIGN.md — Taste metadata).

    One review = a 'rating' row + one 'tag' row per selected tag, sharing a
    review_id. Append-only: re-rating adds a NEW batch (drift preserved,
    nothing overwritten); the enjoyment verdict is computed on read
    (query_enjoyment). rating: int 1–5, or None to clear (records a 'clear'
    event; tags ignored). tags: subset of RATING_TAGS.

    review_id: normally minted here, but an offline client may supply its own
    so an outbox re-flush after a flaky connection doesn't append the same
    review twice — a replayed review_id is a no-op ({"duplicate": True}).

    episodes.rating/rated_at are kept as a denormalized latest-rating cache for
    cheap reads (server /jobs) — the append-only taste_events log is the truth.
    """
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int)
                               or not 1 <= rating <= 5):
        raise ValueError(f"rating must be 1-5 or null, got {rating!r}")
    tags = list(dict.fromkeys(tags or []))  # dedupe, preserve order
    bad = [t for t in tags if t not in RATING_TAGS]
    if bad:
        raise ValueError(f"unknown taste tag(s): {bad}; allowed: {sorted(RATING_TAGS)}")
    if not conn.execute("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)).fetchone():
        raise KeyError(f"episode not found in ledger: {episode_id}")
    if review_id and conn.execute(
            "SELECT 1 FROM taste_events WHERE review_id = ?", (review_id,)).fetchone():
        return {"episode_id": episode_id, "review_id": review_id, "rating": rating,
                "tags": tags if rating is not None else [], "duplicate": True}

    ts = now_iso()
    review_id = review_id or uuid.uuid4().hex
    conn.execute(
        "INSERT INTO taste_events (episode_id, review_id, kind, value, ts) "
        "VALUES (?, ?, 'rating', ?, ?)",
        (episode_id, review_id, "clear" if rating is None else str(rating), ts))
    if rating is not None:
        for tag in tags:
            conn.execute(
                "INSERT INTO taste_events (episode_id, review_id, kind, value, ts) "
                "VALUES (?, ?, 'tag', ?, ?)", (episode_id, review_id, tag, ts))
    conn.execute(
        "UPDATE episodes SET rating = ?, rated_at = ? WHERE id = ?",
        (rating, ts if rating is not None else None, episode_id))
    conn.commit()
    return {"episode_id": episode_id, "review_id": review_id, "rating": rating,
            "tags": tags if rating is not None else []}


def set_rating(conn, episode_id, rating):
    """Back-compat shim → record_rating with no tags. Taste moved to the
    append-only taste_events log (DESIGN.md — Taste metadata)."""
    return record_rating(conn, episode_id, rating)


def record_curation(conn, episode_id, curation):
    """Denormalize the /immerse curation block onto the episode row (DESIGN.md
    — Taste metadata). genre/format/difficulty_felt → columns; topics → the
    metadata JSON. `curation` is the curate.json dict (extra keys ignored)."""
    columns = {k: curation.get(k) for k in ("genre", "format", "difficulty_felt")}
    metadata = {"topics": curation["topics"]} if curation.get("topics") is not None else {}
    return update_episode_meta(conn, episode_id, columns=columns, metadata=metadata)


def record_curate_items(conn, episode_id, curation, jmdict_conn=None):
    """Land /immerse curate's phrase + grammar emissions as (inert) exposure
    evidence (GRAMMAR.md — Production path). Detection is LLM-emits,
    server-validates: the LLM proposes, this function decides what may become
    a tracked key — nothing is key-minted silently.

    curation["phrases"]: [{sentence_idx, surface, canonical, classification}]
      canonical must be a JMdict headword (deinflection sidestepped: the LLM
      returns the dictionary form, we only check it's a real key) AND must
      itself tokenize to ≥2 Sudachi tokens — the canonical form, NOT the
      surface span, because inflected single words split on their auxiliaries
      (食べて → 食べ|て would qualify every te-form verb). Failures are
      returned in `rejected`, never written.

    curation["grammar"]: [{sentence_idx, pattern, classification, form_note}]
      pattern must already be a grammar_points key. An unrecognized pattern
      (or an explicit proposed_pattern + gloss/example) goes to
      grammar_proposed — the deliberate-growth gate (`ledgerctl
      grammar-approve`) — not to evidence.

    Exposures stay inert until the episode is watched, exactly like word
    exposures; context carries the sentence classification, which is the
    qualifying signal for phrase/grammar θ (_exposure_qualifies). Caller
    should `promote` after. Idempotent via idx_exposure_once.
    """
    from engine.lemma import tokenize
    ts = now_iso()
    conn.execute("INSERT INTO episodes (id, processed_at) VALUES (?, ?) "
                 "ON CONFLICT(id) DO NOTHING", (episode_id, ts))

    phrases_written, rejected = 0, []
    for p in curation.get("phrases") or []:
        canonical = (p.get("canonical") or "").strip()
        if not canonical:
            continue
        if jmdict_conn is None:
            rejected.append({"canonical": canonical, "reason": "jmdict_unavailable"})
            continue
        from tools import jmdict as J
        if not J.is_headword(jmdict_conn, canonical):
            rejected.append({"canonical": canonical, "reason": "not_a_jmdict_headword"})
            continue
        if len(tokenize(canonical)) < 2:
            rejected.append({"canonical": canonical, "reason": "single_token"})
            continue
        entries = J.lookup_many(jmdict_conn, [canonical], max_entries=1)
        readings = (entries.get(canonical) or [{}])[0].get("r") or []
        _touch_lemma(conn, canonical, reading=readings[0] if readings else None,
                     pos="expression", ts=ts, kind="phrase")
        context = {k: p[k] for k in ("sentence_idx", "classification") if k in p}
        cur = conn.execute(
            """INSERT OR IGNORE INTO evidence
               (lemma, kind, source, polarity, weight, episode_id, context, ts)
               VALUES (?, 'phrase', 'exposure', ?, ?, ?, ?, ?)""",
            (canonical, POLARITY["exposure"], WEIGHT["exposure"], episode_id,
             json.dumps(context, ensure_ascii=False), ts))
        phrases_written += cur.rowcount

    grammar_written, proposed = 0, []
    known_patterns = {r[0] for r in conn.execute("SELECT pattern FROM grammar_points")}
    for g in curation.get("grammar") or []:
        pattern = (g.get("pattern") or g.get("proposed_pattern") or "").strip()
        if not pattern:
            continue
        if pattern not in known_patterns or "proposed_pattern" in g:
            conn.execute(
                """INSERT INTO grammar_proposed (pattern, example, gloss, seen, first_seen)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(pattern) DO UPDATE SET
                       seen = grammar_proposed.seen + 1,
                       example = COALESCE(grammar_proposed.example, excluded.example),
                       gloss = COALESCE(grammar_proposed.gloss, excluded.gloss)""",
                (pattern, g.get("example"), g.get("gloss"), ts))
            proposed.append(pattern)
            continue
        context = {k: g[k] for k in ("sentence_idx", "classification", "form_note")
                   if k in g}
        cur = conn.execute(
            """INSERT OR IGNORE INTO evidence
               (lemma, kind, source, polarity, weight, episode_id, context, ts)
               VALUES (?, 'grammar', 'exposure', ?, ?, ?, ?, ?)""",
            (pattern, POLARITY["exposure"], WEIGHT["exposure"], episode_id,
             json.dumps(context, ensure_ascii=False), ts))
        grammar_written += cur.rowcount

    conn.commit()
    return {"episode_id": episode_id,
            "phrases": {"recorded": phrases_written, "rejected": rejected},
            "grammar": {"recorded": grammar_written,
                        "proposed": sorted(set(proposed))}}


def seed_grammar_points(conn, rows):
    """Load the once-authored taxonomy (ledger/grammar_taxonomy.json) into
    grammar_points. Upserts level/gloss; never touches promote's verdict
    columns, so re-seeding a revised taxonomy is safe."""
    ts = now_iso()
    for r in rows:
        conn.execute(
            """INSERT INTO grammar_points (pattern, level, gloss, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(pattern) DO UPDATE SET
                   level = excluded.level, gloss = excluded.gloss,
                   updated_at = excluded.updated_at""",
            (r["pattern"].strip(), r.get("level"), r.get("gloss"), ts))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM grammar_points").fetchone()[0]
    return {"seeded": len(rows), "grammar_points": n}


def approve_grammar_proposal(conn, pattern, level=None, gloss=None):
    """Deliberately grow the taxonomy: move a grammar_proposed row into
    grammar_points (GRAMMAR.md — nothing becomes a tracked key silently).
    level/gloss override the proposal's stored ones."""
    row = conn.execute("SELECT * FROM grammar_proposed WHERE pattern = ?",
                       (pattern,)).fetchone()
    if row is None:
        raise KeyError(f"no proposed grammar pattern: {pattern}")
    conn.execute(
        """INSERT INTO grammar_points (pattern, level, gloss, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(pattern) DO UPDATE SET
               level = COALESCE(excluded.level, grammar_points.level),
               gloss = COALESCE(excluded.gloss, grammar_points.gloss)""",
        (pattern, level, gloss or row["gloss"], now_iso()))
    conn.execute("DELETE FROM grammar_proposed WHERE pattern = ?", (pattern,))
    conn.commit()
    return {"pattern": pattern, "approved": True, "level": level,
            "gloss": gloss or row["gloss"]}


def add_phrase(conn, canonical, reading=None):
    """Deliberately track a non-JMdict phrase (the reviewed path for idioms
    record_curate_items rejected). Still refuses single-token keys — that
    guard protects the key space, reviewer or not."""
    from engine.lemma import tokenize
    if len(tokenize(canonical)) < 2:
        raise ValueError(f"not a multi-token phrase: {canonical}")
    _touch_lemma(conn, canonical, reading=reading, pos="expression", kind="phrase")
    conn.commit()
    return {"phrase": canonical, "tracked": True}


def purge_episode(conn, episode_id):
    """Unwind an episode's ledger footprint: evidence (inert exposures,
    pre-watch taps), minted-card records, tap batches, the episodes row —
    then recompute the projection so nothing stale survives.

    A WATCHED episode is never purged: its exposures were activated and its
    cards pushed — that knowledge is earned history that outlives the
    artifacts (deleting a fully-pipelined episode keeps only the lemma
    updates and the Anki collection).

    A RATED-but-unwatched episode keeps its episodes row: the rating is
    taste data the user chose to record (typically a dislike, followed by a
    discard), and losing it would defeat the point of rating before
    deleting. Everything knowledge-side still unwinds."""
    row = conn.execute(
        "SELECT watched, rating FROM episodes WHERE id = ?",
        (episode_id,)).fetchone()
    if row and row["watched"]:
        return {"purged": False, "reason": "watched — evidence retained"}
    ev = conn.execute(
        "DELETE FROM evidence WHERE episode_id = ?", (episode_id,)).rowcount
    cards = conn.execute(
        "DELETE FROM cards WHERE episode_id = ?", (episode_id,)).rowcount
    conn.execute("DELETE FROM tap_batches WHERE episode_id = ?", (episode_id,))
    rating_retained = bool(row and row["rating"] is not None)
    if not rating_retained:
        # No taste data to keep — drop the review log and the row together.
        conn.execute("DELETE FROM taste_events WHERE episode_id = ?", (episode_id,))
        conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    # lemmas is a projection of evidence — rows whose truth is now gone go too.
    # Phrase keys are exempt: they can be deliberately tracked before any
    # evidence exists (add_phrase), and a stale unknown-status phrase key is
    # harmless — detection just re-matches it later.
    orphaned = conn.execute(
        "DELETE FROM lemmas WHERE kind = 'word' AND "
        "lemma NOT IN (SELECT DISTINCT lemma FROM evidence)"
    ).rowcount
    conn.commit()
    promote(conn)  # heal counts/statuses that leaned on the purged evidence
    return {"purged": True, "evidence": ev, "cards": cards,
            "lemma_rows": orphaned, "rating_retained": rating_retained}


def poll_lapses(conn, anki_call):
    """Append card_lapse evidence for minted cards whose lapse count grew (P6),
    and flag cards the user deleted in Anki.

    A minted note that findCards can no longer locate was deleted (the user
    culls sub-par cards). We stamp cards.deleted_at so the lemma drops out of
    the re-mine guard (tools.coverage) — a still-wanted word (tap_interest)
    then becomes eligible for a fresh, better card next time it appears.

    anki_call(action, **params) — injectable for testing; production passes
    ledger.anki_known.anki_request bound to the configured URL.
    """
    rows = conn.execute(
        "SELECT id, lemma, episode_id, anki_note_id, lapses FROM cards "
        "WHERE anki_note_id IS NOT NULL AND deleted_at IS NULL"
    ).fetchall()
    ts = now_iso()
    new_lapses = 0
    deleted = 0
    for row in rows:
        card_ids = anki_call("findCards", query=f"nid:{row['anki_note_id']}")
        if not card_ids:
            # Note gone from Anki → user deleted the card. Re-open for re-mining.
            conn.execute("UPDATE cards SET deleted_at = ? WHERE id = ?", (ts, row["id"]))
            deleted += 1
            continue
        infos = anki_call("cardsInfo", cards=card_ids)
        current = max((c.get("lapses", 0) or 0) for c in infos)
        if current > row["lapses"]:
            conn.execute(
                """INSERT INTO evidence (lemma, source, polarity, weight, episode_id, context, ts)
                   VALUES (?, 'card_lapse', ?, ?, ?, ?, ?)""",
                (row["lemma"], POLARITY["card_lapse"], WEIGHT["card_lapse"],
                 row["episode_id"],
                 json.dumps({"lapses": current, "prev": row["lapses"]}), ts),
            )
            conn.execute("UPDATE cards SET lapses = ? WHERE id = ?", (current, row["id"]))
            new_lapses += 1
    conn.commit()
    return {"cards_polled": len(rows), "new_lapses": new_lapses, "deleted": deleted}


def apply_taps(conn, payload, anki_call=None, watched=True):
    """Apply a phone correction batch.

    payload: {"episode_id", "batch_id", "taps": [[lemma, mark], ...]}
    Marks: "k" → tap_known, "u" → tap_unknown (knowledge evidence, counted in
    `applied`). "h" → tap_interest, a durable "I want to learn this" want that
    is NOT knowledge (counted in `interest`): it persists across episodes and
    steers future card selection (tools.select) until the lemma becomes known.

    watched=True (the classic post-watch corrections blob) implies
    mark-watched (P5). The app's pre-watch feedback flow passes
    watched=False — there, watching is its own later step (POST /watched).

    batch_id makes re-flushes idempotent (MOBILE.md). Also polls minted-card
    lapses when an anki_call is supplied, then the caller should `promote`.
    """
    batch_id = payload.get("batch_id")
    episode_id = payload.get("episode_id")
    if batch_id:
        dup = conn.execute(
            "SELECT 1 FROM tap_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if dup:
            return {"batch_id": batch_id, "applied": 0, "interest": 0,
                    "duplicate": True}

    ts = now_iso()
    applied = interest = 0
    sources = {"k": "tap_known", "u": "tap_unknown", "h": "tap_interest"}
    for lemma, verdict in payload.get("taps", []):
        source = sources.get(verdict)
        if source is None:
            continue
        _touch_lemma(conn, lemma, ts=ts)
        conn.execute(
            """INSERT INTO evidence (lemma, source, polarity, weight, episode_id, context, ts)
               VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (lemma, source, POLARITY[source], WEIGHT[source], episode_id, ts),
        )
        if verdict == "h":
            interest += 1
        else:
            applied += 1

    if batch_id:
        conn.execute(
            "INSERT INTO tap_batches (batch_id, episode_id, applied_at) VALUES (?, ?, ?)",
            (batch_id, episode_id, ts),
        )
    conn.commit()

    result = {"batch_id": batch_id, "episode_id": episode_id, "interest": interest,
              "applied": applied, "duplicate": False}
    if episode_id and watched:
        # Pasting a prep doc's corrections is proof you watched it (P5).
        try:
            mark_watched(conn, episode_id)
            result["marked_watched"] = True
        except KeyError:
            result["marked_watched"] = False
    if anki_call is not None:
        result["lapse_poll"] = poll_lapses(conn, anki_call)
    return result


def _record_confirm(conn, key, source, kind="word"):
    """Append one confirm_known / confirm_defer evidence row for an item the
    exposure heuristic surfaced. The key is the word lemma, phrase headword,
    or grammar pattern; kind routes it to the right projection at promote.
    Caller should `promote` after."""
    ts = now_iso()
    if kind != "grammar":
        _touch_lemma(conn, key, ts=ts, kind=kind)
    conn.execute(
        """INSERT INTO evidence (lemma, kind, source, polarity, weight, episode_id, context, ts)
           VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)""",
        (key, kind, source, POLARITY[source], WEIGHT[source], ts))
    conn.commit()


def confirm_known_lemma(conn, key, kind="word"):
    """User answered "yes, I know it" to the exposure prompt — a deliberate
    knowledge claim (confirm_known) that promotes the item to known."""
    _record_confirm(conn, key, "confirm_known", kind=kind)


def defer_known_lemma(conn, key, kind="word"):
    """User answered "not yet" — record confirm_defer, which keeps the item in
    learning and snoozes the prompt until a fresh qualifying exposure lands."""
    _record_confirm(conn, key, "confirm_defer", kind=kind)


# --- promote (the state machine) ----------------------------------------------

def _judge(evs, theta, spread_needed, in_anki_known=False):
    """Apply promote's rule order to one item's evidence rows (any kind —
    word, phrase, or grammar; sources an item never receives simply yield
    empty lists). Returns the projection fields."""
    taps_known = [e for e in evs if e["source"] == "tap_known"]
    imports = [e for e in evs if e["source"] == "import"]
    confirms = [e for e in evs if e["source"] == "confirm_known"]
    defers = [e for e in evs if e["source"] == "confirm_defer"]
    negatives = [e for e in evs if e["source"] in ("tap_unknown", "card_lapse")]
    mined = [e for e in evs if e["source"] == "mined_card"]
    active_exposures = [e for e in evs if e["source"] == "exposure" and e["watched"]]

    qualifying = []
    for e in active_exposures:
        try:
            ctx = json.loads(e["context"] or "{}")
        except ValueError:
            ctx = {}
        if _exposure_qualifies(ctx):
            qualifying.append(e)
    q_count = len(qualifying)
    q_spread = len({e["episode_id"] for e in qualifying})

    positives = taps_known + imports + confirms + active_exposures
    last_negative = max((e["ts"] for e in negatives), default=None)
    last_positive = max((e["ts"] for e in positives), default=None)

    needs_review = 0
    confirm_candidate = 0
    # Ties go to the negative: taps are deliberate strong evidence, and a
    # same-second exposure/tap pair only happens when they were written
    # by the same run.
    if last_negative and (last_positive is None or last_negative >= last_positive):
        status = "learning"
        if taps_known or confirms or in_anki_known:
            needs_review = 1
    elif taps_known or imports or confirms:
        status = "known"
    elif q_count >= theta and q_spread >= spread_needed:
        # Exposures cleared the bar, but a fuzzy count can't *assert*
        # knowledge — surface it for confirmation instead of promoting.
        # Snooze after a "not yet": re-surface only once a qualifying
        # exposure lands after the latest defer.
        status = "learning"
        last_defer = max((e["ts"] for e in defers), default=None)
        newest_qualifying = max((e["ts"] for e in qualifying), default=None)
        if last_defer is None or (newest_qualifying and newest_qualifying > last_defer):
            confirm_candidate = 1
    elif mined:
        status = "learning"
    else:
        status = "unknown"

    # Coarse roll-up: orders the reconcile queue, nothing more.
    signed = sum(POLARITY[e["source"]] * WEIGHT[e["source"]]
                 for e in taps_known + imports + confirms + negatives + mined + active_exposures)
    return {
        "status": status,
        "needs_review": needs_review,
        "confirm_candidate": confirm_candidate,
        "confidence": max(-1.0, min(1.0, signed / 6.0)),
        "exposure_count": len(active_exposures),
        "episode_spread": len({e["episode_id"] for e in active_exposures}),
        "first_seen": min(e["ts"] for e in evs),
        "last_seen": max(e["ts"] for e in evs),
    }


def promote(conn, anki_known=None):
    """Recompute the projections (lemmas for word/phrase evidence,
    grammar_points for grammar evidence) from the append-only evidence log.
    One rule order for every item kind — first match wins:

    1. Fresh negative (tap_unknown / card_lapse newer than any positive)
       → learning. needs_review when a strong positive (tap_known / confirm_known)
       also exists, or when the lemma is live-Anki-known — there the demotion is a
       union no-op and the tap means *the card isn't doing its job* (Q2):
       route to REPLACE via the needs_review queue.
    2. tap_known / confirm_known / import (bulk-seeded external list) → known.
       An import is weaker than a tap: a fresh negative demotes it without
       needs_review.
    3. Qualifying exposures ≥ θ and spread ≥ k → NOT auto-known.
       A fuzzy interaction count can't assert knowledge, so the item stays
       `learning` and is flagged `confirm_candidate` — surfaced for the user to
       confirm ("do you know this?"). Confirming appends confirm_known (rule 2);
       "not yet" appends confirm_defer, which snoozes re-surfacing until a
       qualifying exposure lands *after* the defer. An exposure qualifies when
       its episode is watched and the learner could parse the sentence around
       the item: other_unknown_count = 0 for words (Q1), a non-too_hard
       coverage classification for phrases/grammar (_exposure_qualifies).
       θ comes from freq rank for words (theta_for), from the JLPT tier for
       grammar (grammar_theta_for); phrases have no corpus rank yet and get
       the rare-word bar.
    4. mined_card, no stronger positive → learning.
    5. else → unknown.
    """
    anki_known = anki_known or set()
    rows = conn.execute(
        """SELECT e.lemma, e.kind, e.source, e.ts, e.context, e.episode_id,
                  COALESCE(ep.watched, 0) AS watched
           FROM evidence e LEFT JOIN episodes ep ON ep.id = e.episode_id
           ORDER BY e.lemma, e.ts"""
    ).fetchall()

    freq = dict(conn.execute("SELECT lemma, rank FROM freq").fetchall())
    grammar_levels = dict(conn.execute(
        "SELECT pattern, level FROM grammar_points").fetchall())

    # kind is part of the group key so a word and a grammar pattern that
    # happen to share a string can't merge their evidence.
    by_key = {}
    for r in rows:
        by_key.setdefault((r["kind"], r["lemma"]), []).append(r)

    ts_now = now_iso()
    grammar_seen = 0
    for (kind, lemma), evs in by_key.items():
        if kind == "grammar":
            theta, spread_needed = grammar_theta_for(grammar_levels.get(lemma))
            v = _judge(evs, theta, spread_needed)
            conn.execute(
                """INSERT INTO grammar_points (pattern, status, confidence,
                       exposure_count, episode_spread, needs_review,
                       confirm_candidate, first_seen, last_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pattern) DO UPDATE SET
                       status = excluded.status,
                       confidence = excluded.confidence,
                       exposure_count = excluded.exposure_count,
                       episode_spread = excluded.episode_spread,
                       needs_review = excluded.needs_review,
                       confirm_candidate = excluded.confirm_candidate,
                       first_seen = COALESCE(grammar_points.first_seen, excluded.first_seen),
                       last_seen = excluded.last_seen,
                       updated_at = excluded.updated_at""",
                (lemma, v["status"], v["confidence"], v["exposure_count"],
                 v["episode_spread"], v["needs_review"], v["confirm_candidate"],
                 v["first_seen"], v["last_seen"], ts_now))
            grammar_seen += 1
            continue

        # freq only ever keys single Sudachi lemmas, so a phrase headword
        # misses → rare-word θ, per the docstring.
        freq_rank = freq.get(lemma)
        theta, spread_needed = theta_for(freq_rank)
        v = _judge(evs, theta, spread_needed, in_anki_known=lemma in anki_known)
        conn.execute(
            """INSERT INTO lemmas (lemma, kind, freq_rank, status, confidence,
                                   exposure_count, episode_spread, needs_review,
                                   confirm_candidate, first_seen, last_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(lemma) DO UPDATE SET
                   freq_rank = excluded.freq_rank,
                   status = excluded.status,
                   confidence = excluded.confidence,
                   exposure_count = excluded.exposure_count,
                   episode_spread = excluded.episode_spread,
                   needs_review = excluded.needs_review,
                   confirm_candidate = excluded.confirm_candidate,
                   first_seen = COALESCE(lemmas.first_seen, excluded.first_seen),
                   last_seen = excluded.last_seen,
                   updated_at = excluded.updated_at
               """,
            (lemma, kind, freq_rank, v["status"], v["confidence"],
             v["exposure_count"], v["episode_spread"], v["needs_review"],
             v["confirm_candidate"], v["first_seen"], v["last_seen"], ts_now),
        )

    # Heal grammar rows whose evidence vanished (episode purge): back to the
    # seeded baseline. Guarded so untouched taxonomy rows aren't rewritten
    # every promote.
    evidenced_patterns = [k for (kind, k) in by_key if kind == "grammar"]
    qmarks = ",".join("?" * len(evidenced_patterns)) or "''"
    conn.execute(
        f"""UPDATE grammar_points
            SET status='unknown', confidence=0, exposure_count=0,
                episode_spread=0, needs_review=0, confirm_candidate=0
            WHERE pattern NOT IN ({qmarks})
              AND (status != 'unknown' OR exposure_count != 0
                   OR confirm_candidate != 0 OR needs_review != 0)""",
        evidenced_patterns)
    conn.commit()

    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM lemmas GROUP BY status").fetchall())
    return {"lemmas_recomputed": len(by_key) - grammar_seen,
            "grammar_recomputed": grammar_seen,
            "status_counts": counts}


# --- read verbs ----------------------------------------------------------------

def materialize_known(conn, cfg, force_refresh=False):
    """live-Anki-known ∪ ledger-promoted — the set every mode reads.

    Returns the full known/learning picture plus the variant-matching sets
    engine.lemma.KnownSet consumes.
    """
    from ledger.anki_known import compute_anki_known
    anki_set, norm_known, stems = compute_anki_known(cfg, force_refresh=force_refresh)

    # Words only: phrase keys must not leak into the token-level known set —
    # their kanji stems would contaminate stem-matching (気を付ける → stem 気).
    # Phrases travel separately as {headword: status} for KnownSet's unit pass.
    ledger_known = {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE status = 'known' AND kind = 'word'")}
    learning = {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE status = 'learning' AND kind = 'word'")}
    phrases = dict(conn.execute(
        "SELECT lemma, status FROM lemmas WHERE kind = 'phrase'").fetchall())

    known = anki_set | ledger_known

    # Bridge external-lemmatizer forms into SudachiPy space. Imported lists
    # (AnkiMorphs = MeCab) carry kana lemmas (くる, ところ) that never equal
    # Sudachi mode-C dictionary forms (来る, 所); a single-morpheme known
    # string contributes its Sudachi dictionary + normalized forms so
    # KnownSet's variant matching can join it. Multi-morpheme strings are
    # skipped — no clean mapping.
    from engine.lemma import extract_kanji_stem, tokenize
    for lem in ledger_known:
        toks = tokenize(lem)
        if len(toks) == 1:
            known.add(toks[0].lemma)
            if toks[0].normalized:
                norm_known.add(toks[0].normalized)

    stems |= {s for s in (extract_kanji_stem(x) for x in known) if s}

    return {
        "known": known,
        "learning": learning - known,
        "norm_known": norm_known,
        "known_stems": stems,
        "phrases": phrases,
        "sources": {"anki": len(anki_set), "ledger": len(ledger_known),
                    "union": len(known)},
    }


def active_interest(conn, known=()):
    """The standing "words I want to learn" set (tap_interest), minus lemmas
    the user now knows. Persists across episodes and *through* card minting —
    a wanted word keeps being highlighted and prioritized until it's known
    (the user's rule). If its card was later deleted (cards.deleted_at, set by
    poll_lapses), the re-mine guard in tools.coverage reopens it, so selection
    will mint a fresh one.

    Retirement reads the ledger `lemmas` projection (status='known') — cheap,
    no AnkiConnect, so this stays usable in hot read paths (GET /transcript).
    `known` optionally adds extra known lemmas the caller already has in hand."""
    interested = {r[0] for r in conn.execute(
        "SELECT DISTINCT lemma FROM evidence WHERE source = 'tap_interest'")}
    ledger_known = {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE status = 'known'")}
    return interested - ledger_known - set(known)


def query_summary(conn):
    """Headline counts. lemmas_by_status stays WORDS-ONLY so its meaning (and
    the corpus-rank join in /stats) is unchanged by phrase/grammar tracking;
    the sibling `phrases` / `grammar` blocks carry the other two kinds.
    confirm_candidates is the all-kinds total — it feeds the app's confirm
    banner, which fronts one queue for all three."""
    status_counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM lemmas WHERE kind = 'word' "
        "GROUP BY status").fetchall())
    phrase_counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM lemmas WHERE kind = 'phrase' "
        "GROUP BY status").fetchall())
    grammar_counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM grammar_points GROUP BY status").fetchall())
    evidence_counts = dict(conn.execute(
        "SELECT source, COUNT(*) FROM evidence GROUP BY source").fetchall())
    episodes = conn.execute(
        "SELECT COUNT(*), SUM(watched) FROM episodes").fetchone()
    needs_review = conn.execute(
        "SELECT COUNT(*) FROM lemmas WHERE needs_review = 1").fetchone()[0]
    cc_lemmas = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM lemmas WHERE confirm_candidate = 1 "
        "GROUP BY kind").fetchall())
    cc_grammar = conn.execute(
        "SELECT COUNT(*) FROM grammar_points WHERE confirm_candidate = 1"
    ).fetchone()[0]
    return {
        "lemmas_by_status": status_counts,
        "phrases": {"by_status": phrase_counts,
                    "confirm_candidates": cc_lemmas.get("phrase", 0)},
        "grammar": {"by_status": grammar_counts,
                    "confirm_candidates": cc_grammar,
                    "proposed": conn.execute(
                        "SELECT COUNT(*) FROM grammar_proposed").fetchone()[0]},
        "evidence_by_source": evidence_counts,
        "episodes": {"total": episodes[0] or 0, "watched": episodes[1] or 0},
        "needs_review": needs_review,
        "confirm_candidates": cc_lemmas.get("word", 0) + cc_lemmas.get("phrase", 0)
                              + cc_grammar,
        "cards_minted": conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0],
    }


def query_needs_review(conn):
    rows = conn.execute(
        """SELECT lemma, status, confidence, exposure_count, episode_spread, last_seen
           FROM lemmas WHERE needs_review = 1 ORDER BY confidence""").fetchall()
    return [dict(r) for r in rows]


def query_confirm_queue(conn):
    """Items the exposure heuristic flagged for confirmation (confirm_candidate
    = 1) — "we think you know this; do you?". One queue, three kinds: words
    and phrases from `lemmas` (common words first — most likely a quick yes),
    then grammar points from `grammar_points` (easiest JLPT tier first). Every
    row carries `kind` (the typed key) and the watched episodes it turned up
    in as context."""
    rows = conn.execute(
        """SELECT lemma, kind, reading, pos, freq_rank, exposure_count,
                  episode_spread, last_seen
           FROM lemmas WHERE confirm_candidate = 1
           ORDER BY freq_rank IS NULL, freq_rank, exposure_count DESC""").fetchall()
    grows = conn.execute(
        """SELECT pattern, level, gloss, exposure_count, episode_spread, last_seen
           FROM grammar_points WHERE confirm_candidate = 1
           ORDER BY level IS NULL, level DESC, exposure_count DESC""").fetchall()
    if not rows and not grows:
        return []
    keys = [(r["kind"], r["lemma"]) for r in rows] + \
           [("grammar", g["pattern"]) for g in grows]
    qmarks = ",".join("(?,?)" for _ in keys)
    titles = {}
    for r in conn.execute(
            f"""SELECT e.lemma, e.kind, ep.title FROM evidence e
                JOIN episodes ep ON ep.id = e.episode_id
                WHERE e.source = 'exposure' AND ep.watched = 1
                      AND (e.kind, e.lemma) IN (VALUES {qmarks})
                ORDER BY ep.processed_at""",
            [x for pair in keys for x in pair]):
        seen = titles.setdefault((r["kind"], r["lemma"]), [])
        if r["title"] and r["title"] not in seen:
            seen.append(r["title"])
    # Furigana over kanji only: reading_segs peels okurigana off the lemma's
    # dictionary reading (通す → 通[とお]す). Also normalize the flat reading to
    # hiragana, matching what coverage emits on the wire. Works for phrases
    # too — furigana() tokenizes the headword and rubies each kanji core.
    from engine.lemma import furigana, kata_to_hira
    out = []
    for r in rows:
        d = dict(r)
        if d.get("reading"):
            d["reading"] = kata_to_hira(d["reading"])
        d["reading_segs"] = furigana(r["lemma"])
        d["episodes"] = titles.get((r["kind"], r["lemma"]), [])
        out.append(d)
    for g in grows:
        out.append({
            "lemma": g["pattern"], "kind": "grammar", "pattern": g["pattern"],
            "level": g["level"], "gloss": g["gloss"],
            "exposure_count": g["exposure_count"],
            "episode_spread": g["episode_spread"], "last_seen": g["last_seen"],
            "episodes": titles.get(("grammar", g["pattern"]), []),
        })
    return out


def query_why(conn, lemma):
    """Auditability: why does the ledger think what it thinks about *lemma*?"""
    lrow = conn.execute("SELECT * FROM lemmas WHERE lemma = ?", (lemma,)).fetchone()
    evs = conn.execute(
        """SELECT e.source, e.polarity, e.weight, e.episode_id, e.context, e.ts,
                  COALESCE(ep.watched, 0) AS watched, ep.title
           FROM evidence e LEFT JOIN episodes ep ON ep.id = e.episode_id
           WHERE e.lemma = ? ORDER BY e.ts""", (lemma,)).fetchall()
    return {
        "lemma": dict(lrow) if lrow else None,
        "evidence": [dict(r) for r in evs],
    }


def query_unwatched(conn):
    """Analyzed-but-unwatched episodes — inert exposure made visible (P5)."""
    rows = conn.execute(
        """SELECT ep.id, ep.title, ep.processed_at,
                  COUNT(e.id) AS inert_exposures
           FROM episodes ep LEFT JOIN evidence e
                ON e.episode_id = ep.id AND e.source = 'exposure'
           WHERE ep.watched = 0
           GROUP BY ep.id ORDER BY ep.processed_at""").fetchall()
    return [dict(r) for r in rows]


def _enjoyment_from_events(rows):
    """rows: one episode's taste_events ordered by id. Verdict = the latest
    review (highest-id rating event + the tags sharing its review_id)."""
    ratings = [r for r in rows if r["kind"] == "rating"]
    if not ratings:
        return None
    latest = ratings[-1]
    if latest["value"] == "clear":
        return {"rating": None, "tags": [], "taste_valid": None,
                "adjusted_enjoyment": None}
    rating = int(latest["value"])
    tags = [r["value"] for r in rows
            if r["kind"] == "tag" and r["review_id"] == latest["review_id"]]
    # Over-my-head decouples the star from the taste label (DESIGN.md).
    taste_valid = _DIFFICULTY_TAG not in tags
    return {"rating": rating, "tags": tags, "taste_valid": taste_valid,
            "adjusted_enjoyment": rating if taste_valid else None}


def query_enjoyment(conn, episode_id=None):
    """On-read enjoyment verdict from the append-only taste_events log — no
    materialized cache (DESIGN.md — Taste metadata). One episode → a verdict
    dict (None if never reviewed); no argument → {episode_id: verdict}."""
    if episode_id is not None:
        rows = conn.execute(
            "SELECT review_id, kind, value FROM taste_events "
            "WHERE episode_id = ? ORDER BY id", (episode_id,)).fetchall()
        return _enjoyment_from_events(rows)
    by_ep = {}
    for r in conn.execute(
            "SELECT episode_id, review_id, kind, value FROM taste_events ORDER BY id"):
        by_ep.setdefault(r["episode_id"], []).append(r)
    return {ep: _enjoyment_from_events(evs) for ep, evs in by_ep.items()}


def query_ratings(conn):
    """Every reviewed episode — the taste dataset future curation reads: the
    latest rating + its tags + the difficulty-decoupled enjoyment verdict
    (DESIGN.md — Taste metadata). Cleared episodes drop out."""
    rated = {ep: v for ep, v in query_enjoyment(conn).items()
             if v and v["rating"] is not None}
    if not rated:
        return []
    base = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, title, source, kind, watched, rated_at FROM episodes")}
    out = [{**base.get(ep, {"id": ep}), **v} for ep, v in rated.items()]
    out.sort(key=lambda e: e.get("rated_at") or "", reverse=True)   # tie-break
    out.sort(key=lambda e: e["rating"] or 0, reverse=True)          # primary
    return out


# --- CLI ------------------------------------------------------------------------

def _json_out(obj):
    def default(o):
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(type(o))
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=default))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ledgerctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.json (default: fullPipe/config.json)")
    ap.add_argument("--db", help="ledger db path (default: config ledger_db)")
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("init", help="create the ledger database")
    p = sub.add_parser("materialize-known", help="live-Anki-known ∪ ledger-promoted")
    p.add_argument("--refresh", action="store_true", help="ignore the Anki-known cache")
    p = sub.add_parser("compute-anki-known", help="recompute the live Anki known-set")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("record-exposure", help="write inert exposures for an episode")
    p.add_argument("payload", help="JSON file: {episode: {...}, exposures: {...}}")
    p = sub.add_parser("mark-watched", help="activate an episode's exposures")
    p.add_argument("episode_id")
    p = sub.add_parser("apply-taps", help="apply a tap batch (implies mark-watched + lapse poll)")
    p.add_argument("payload", help="JSON file: {episode_id, batch_id, taps: [[lemma, k|u]]}")
    p.add_argument("--no-lapse-poll", action="store_true")
    p = sub.add_parser("import-known",
                       help="bulk-seed known lemmas from a list (e.g. AnkiMorphs export)")
    p.add_argument("file", help="CSV or plain text, one lemma per line "
                                "(first CSV column; header row auto-skipped)")
    p.add_argument("--origin", default=None,
                   help="label recorded in the evidence context (default: file name)")
    sub.add_parser("promote", help="recompute the projection from evidence")
    p = sub.add_parser("confirm", help="confirm a candidate item as known ('yes')")
    p.add_argument("lemma", help="word lemma / phrase headword / grammar pattern")
    p.add_argument("--kind", choices=["word", "phrase", "grammar"], default="word")
    p = sub.add_parser("defer", help="snooze a candidate item ('not yet')")
    p.add_argument("lemma", help="word lemma / phrase headword / grammar pattern")
    p.add_argument("--kind", choices=["word", "phrase", "grammar"], default="word")
    p = sub.add_parser("grammar-seed",
                       help="load the once-authored grammar taxonomy into grammar_points")
    p.add_argument("json_path", nargs="?",
                   help="taxonomy JSON (default: ledger/grammar_taxonomy.json)")
    p = sub.add_parser("grammar-approve",
                       help="move a proposed grammar pattern into the taxonomy")
    p.add_argument("pattern")
    p.add_argument("--level", type=int, choices=[1, 2, 3, 4, 5],
                   help="JLPT tier 5=N5 … 1=N1 (omitted = strictest θ)")
    p.add_argument("--gloss")
    p = sub.add_parser("phrase-add",
                       help="deliberately track a non-JMdict phrase (reviewed path)")
    p.add_argument("canonical")
    p.add_argument("--reading")
    p = sub.add_parser("rate", help="rate an episode 1-5 stars + optional taste tags")
    p.add_argument("episode_id")
    p.add_argument("rating", help="1-5, or 'clear' to unrate")
    p.add_argument("--tag", action="append", default=[], choices=sorted(RATING_TAGS),
                   metavar="TAG", help="taste tag (repeatable): "
                   "already_knew|over_my_head|didnt_grab|format_miss|"
                   "fascinating|loved_format")
    p = sub.add_parser("record-curation",
                       help="persist /immerse curation metadata (genre/format/topics/difficulty)")
    p.add_argument("episode_id")
    p.add_argument("curate_json", help="path to the episode's curate.json")
    p = sub.add_parser("query", help="read the ledger")
    p.add_argument("what", choices=["summary", "needs-review", "confirm-queue",
                                    "why", "unwatched", "ratings",
                                    "grammar-proposed"])
    p.add_argument("lemma", nargs="?")

    args = ap.parse_args(argv)
    cfg = load_config(args.config, required=args.verb in
                      ("materialize-known", "compute-anki-known") or not args.db)
    db_path = args.db or cfg["ledger_db"]
    conn = open_db(db_path)

    if args.verb == "init":
        _json_out({"db": db_path, "initialized": True})
    elif args.verb == "materialize-known":
        _json_out(materialize_known(conn, cfg, force_refresh=args.refresh))
    elif args.verb == "compute-anki-known":
        from ledger.anki_known import compute_anki_known
        known, norm_known, stems = compute_anki_known(cfg, force_refresh=args.refresh)
        _json_out({"known": len(known), "norm_variants": len(norm_known),
                   "kanji_stems": len(stems)})
    elif args.verb == "record-exposure":
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        _json_out(record_exposure(conn, payload["episode"], payload["exposures"]))
    elif args.verb == "mark-watched":
        _json_out(mark_watched(conn, args.episode_id))
    elif args.verb == "apply-taps":
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        anki_call = None
        if not args.no_lapse_poll:
            from functools import partial
            from ledger.anki_known import anki_request
            url = (cfg or {}).get("anki_connect_url", "http://localhost:8765")
            anki_call = partial(anki_request, url=url)
        result = apply_taps(conn, payload, anki_call=anki_call)
        result["promote"] = promote(conn)
        _json_out(result)
    elif args.verb == "import-known":
        path = Path(args.file)
        lemmas = []
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines()):
            lemma = line.split(",")[0].strip().strip('"')
            # Header rows ("Morph-Lemma", "lemma", …) are ASCII; real entries
            # aren't. Only ever skip the first line, and only if it looks ASCII.
            if i == 0 and lemma.isascii():
                continue
            lemmas.append(lemma)
        result = import_known(conn, lemmas, origin=args.origin or path.name)
        result["promote"] = promote(conn)
        _json_out(result)
    elif args.verb == "promote":
        _json_out(promote(conn))
    elif args.verb == "confirm":
        confirm_known_lemma(conn, args.lemma, kind=args.kind)
        _json_out({"lemma": args.lemma, "kind": args.kind, "confirmed": True,
                   "promote": promote(conn)})
    elif args.verb == "defer":
        defer_known_lemma(conn, args.lemma, kind=args.kind)
        _json_out({"lemma": args.lemma, "kind": args.kind, "deferred": True,
                   "promote": promote(conn)})
    elif args.verb == "grammar-seed":
        path = Path(args.json_path) if args.json_path else \
            Path(__file__).resolve().parent / "grammar_taxonomy.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        _json_out(seed_grammar_points(conn, rows))
    elif args.verb == "grammar-approve":
        result = approve_grammar_proposal(conn, args.pattern,
                                          level=args.level, gloss=args.gloss)
        result["promote"] = promote(conn)
        _json_out(result)
    elif args.verb == "phrase-add":
        _json_out(add_phrase(conn, args.canonical, reading=args.reading))
    elif args.verb == "rate":
        rating = None if args.rating == "clear" else int(args.rating)
        _json_out(record_rating(conn, args.episode_id, rating, args.tag))
    elif args.verb == "record-curation":
        curation = json.loads(Path(args.curate_json).read_text(encoding="utf-8"))
        result = record_curation(conn, args.episode_id, curation)
        # Phrase/grammar emissions (GRAMMAR.md — Production path). Phrase keys
        # are validated against JMdict, so open it if built.
        from tools import jmdict as J
        jpath = J.db_path(cfg) if cfg else None
        jconn = J.open_db(jpath) if jpath and jpath.exists() else None
        try:
            result["items"] = record_curate_items(conn, args.episode_id,
                                                  curation, jmdict_conn=jconn)
        finally:
            if jconn is not None:
                jconn.close()
        result["promote"] = promote(conn)
        _json_out(result)
    elif args.verb == "query":
        if args.what == "summary":
            _json_out(query_summary(conn))
        elif args.what == "needs-review":
            _json_out(query_needs_review(conn))
        elif args.what == "confirm-queue":
            _json_out(query_confirm_queue(conn))
        elif args.what == "why":
            if not args.lemma:
                ap.error("query why requires a lemma")
            _json_out(query_why(conn, args.lemma))
        elif args.what == "unwatched":
            _json_out(query_unwatched(conn))
        elif args.what == "ratings":
            _json_out(query_ratings(conn))
        elif args.what == "grammar-proposed":
            _json_out([dict(r) for r in conn.execute(
                "SELECT * FROM grammar_proposed ORDER BY seen DESC, first_seen")])


if __name__ == "__main__":
    main()
