#!/usr/bin/env python3
"""ledgerctl — the ledger's verbs (DESIGN.md — The Ledger).

    materialize-known    → the ledger's promoted known set; the set every mode reads
    import-anki          ← one-shot: snapshot the live Anki known-set into the ledger
                           as 'import' evidence (origin anki_final); needs Anki up
    record-exposure      ← written by /immerse at analysis time; inert until watched
    mark-watched         ← flips episodes.watched=1 (P5)
    apply-taps           ← phone corrections; implies mark-watched; polls lapses (P6)
    promote              → recompute projection from evidence (the state machine)
    confirm / defer      ← answer the exposure prompt (known 'yes' / snooze 'not yet')
    rate                 ← post-watch survey (star + axes + tags + follow) → taste_events
    set-follow           ← set a channel's follow intent (block|less|neutral|more)
    presenter-get/-set   ← read/store a channel's presenter fingerprint (SURVEY.md §4c)
    record-curation      ← /immerse curation block (genre/format/topics/difficulty)
    record-view-session  ← phone-recorded playback time (watch|listen) → view_sessions
    query                → coverage %, needs_review queue, evidence audits, ratings

Raw evidence is append-only truth; lemmas.status is a projection — rerunning
`promote` over evidence you already have is how thresholds get retuned.

CLI:
    python -m ledger.ledgerctl [--config PATH] [--db PATH] VERB [args]
"""

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

from lib_config import load_config  # noqa: E402
from ledger import confirm_model  # noqa: E402

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
# lookup: the popup opened on a word and no mark followed — a "what was that
# again?" that is neither knowledge nor its absence (weight 0, never moves
# status or lists). Counted per word per episode with the list the word was
# painted from at the tap, so the ledger can say how many lookups precede a ✓
# and how often a listed word is looked up (query calibration).
POLARITY = {"exposure": 1, "tap_known": 1, "tap_unknown": -1, "tap_interest": 0, "lookup": 0,
            "mined_card": 0, "card_lapse": -1, "import": 1,
            "confirm_known": 1, "confirm_defer": 0}
WEIGHT = {"exposure": 1.0, "tap_known": 3.0, "tap_unknown": 3.0, "tap_interest": 0.0, "lookup": 0.0,
          "mined_card": 1.0, "card_lapse": 2.0, "import": 2.0,  # import: strong, but below a deliberate tap
          "confirm_known": 3.0, "confirm_defer": 0.0}

# Taste metadata (DESIGN.md — Taste metadata). Scalar columns the recommender
# filters/groups/correlates on; bulky embed-only payload goes to metadata JSON.
_EPISODE_META_COLUMNS = frozenset((
    "channel", "channel_id", "duration", "upload_date",
    "genre", "format", "difficulty_felt",
    "coverage_pct", "iplus1_count", "known_set_size",
    "series", "ep_no",
))

# The six taste tags (DESIGN.md — "The tags"). Confound-breakers + attributors.
# These are the categorical chips the graded axes below can't hold; the free
# 'note' event is the pressure valve for anything outside this set.
RATING_TAGS = frozenset((
    "already_knew", "over_my_head", "didnt_grab", "format_miss",  # negatives
    "fascinating", "loved_format",                                # positives
))

# Graded 1-5 survey axes beyond the overall star (SURVEY.md §2). Each is its own
# taste_events kind; `scale` governs how the verdict projects it:
#   monotonic — higher = better; a soft per-axis weight for the recommender.
#   target    — a sweet spot, not a max; `difficulty` never enters the taste
#               weight — it censors (below) and level-matches instead.
# comprehension_dependent axes are the ones a too-hard video invalidates: you
# can't judge whether a topic gripped you if you couldn't follow it, but you can
# still love the performers' act (the manzai case, SURVEY.md §2). Censoring is
# therefore PER-AXIS, not per-review.
SURVEY_AXES = {
    "topic_pull":     {"scale": "monotonic", "comprehension_dependent": True},
    "presenter":      {"scale": "monotonic", "comprehension_dependent": False},
    "audio_fidelity": {"scale": "monotonic", "comprehension_dependent": False},
    "speech_clarity": {"scale": "monotonic", "comprehension_dependent": False},
    "difficulty":     {"scale": "target",    "comprehension_dependent": False},
}
# Channel-follow is a state with a veto floor (block), not a graded axis —
# decoupled from any single video's score (SURVEY.md §4a).
FOLLOW_STATES = ("block", "less", "neutral", "more")

# Difficulty at/above this censors the comprehension-dependent axes + the overall
# taste label. A projection parameter (verdict is computed on read), re-tunable
# with no re-rating (SURVEY.md §6). The legacy `over_my_head` tag is the
# pre-survey way of asserting the same thing and still censors, for old reviews.
DIFFICULTY_CENSOR = 5
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
# around the item. Words carry other_unknown_count (Q1) — at most
# WORD_QUALIFYING_MAX_OTHER_UNKNOWN other gaps in the sentence; phrase/grammar
# exposures carry the sentence's coverage classification instead — anything
# short of too_hard parses.
#
# The bar was 0 (the item is the sentence's only gap) until 2026-09-07. The
# 2026-09-07 calibration (`ledgerctl query calibration`, 245 confirm answers)
# found the yes-rate flat across 0-gap, ≤1-gap and ≤2-gap counts (AUC .53 /
# .54 / .55 — none separates "yes" from "not yet"), while the 0-gap bar
# starved everything below the top 2,000: of 814 mid-frequency words met in
# 6+ watched episodes, 31 had cleared θ. ≤1 clears 117 at the same precision.
WORD_QUALIFYING_MAX_OTHER_UNKNOWN = 1
QUALIFYING_CLASSIFICATIONS = frozenset(("comprehensible", "i_plus_1", "reinforcement"))

# A player (`watch`) sitting that covers this fraction of an episode activates
# its exposures like a close-out would: watching with the subtitles off and
# never tapping is still watching. Listen-tab (`listen`) plays never activate
# — passive exposure is tallied apart (seen_passive), not counted toward θ.
PLAY_ACTIVATION_FRACTION = 0.8
# A session that reports no media length at all: one play if it ran this long.
_PLAY_UNKNOWN_DURATION_SECS = 600.0


# Precision gate on the think-you-know list (2026-09-07, 226 confirm answers):
# clearing θ alone was right 61 % of the time. What separated "yes" from "not
# yet" was the *context*, not the count — the mean known_ratio of the
# sentences the word was met in (≤0.5: 30 % yes · 0.6: 60 % · 0.7: 68 % ·
# 0.8+: 75 %), a prior "not yet" (35 % yes after one), and verbs (47 % yes —
# Sudachi's potential/classical lemma forms いける・取れる・信ずる read as
# "wrong word"). Words need mean known_ratio ≥ CONFIRM_MIN_KNOWN_RATIO over
# their watched exposures (verbs: the higher bar), and after a "not yet" the
# exposures landing since must re-clear θ / k on their own. On the answer set
# that reads 82 % precision at 55 % recall. Phrase/grammar exposures carry no
# known_ratio and are gated by θ alone, as before.
CONFIRM_MIN_KNOWN_RATIO = 0.7
CONFIRM_MIN_KNOWN_RATIO_VERB = 0.8
_VERB_POS = "動詞"


def _exposure_qualifies(ctx):
    if ctx.get("other_unknown_count", 99) <= WORD_QUALIFYING_MAX_OTHER_UNKNOWN:
        return True
    return ctx.get("classification") in QUALIFYING_CLASSIFICATIONS


def _ctx(e):
    try:
        return json.loads(e["context"] or "{}")
    except ValueError:
        return {}


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
        ("series", "series TEXT"), ("ep_no", "ep_no INTEGER"),
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
    for col in ("seen_active", "seen_passive", "lookups", "lookups_listed"):
        if col not in lemma_cols:
            conn.execute(f"ALTER TABLE lemmas ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    if "confirm_score" not in lemma_cols:
        conn.execute("ALTER TABLE lemmas ADD COLUMN confirm_score REAL")
    if "seen_by_mode" not in lemma_cols:
        conn.execute("ALTER TABLE lemmas ADD COLUMN seen_by_mode TEXT")
    vs_cols = {r["name"] for r in conn.execute("PRAGMA table_info(view_sessions)")}
    if vs_cols and "source" not in vs_cols:
        conn.execute("ALTER TABLE view_sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'app'")
    if vs_cols and "modes" not in vs_cols:
        conn.execute("ALTER TABLE view_sessions ADD COLUMN modes TEXT")
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
_ACQUIRE_META_COLUMNS = ("channel", "channel_id", "duration", "upload_date",
                         "series", "ep_no")
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
                                       "other_unknown_count", "classification",
                                       "occ", "occ_clean", "occ_near")
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


def record_rating(conn, episode_id, rating, tags=None, review_id=None,
                  axes=None, follow=None, note=None):
    """Append a post-watch survey review as one taste_events batch (DESIGN.md —
    Taste metadata; SURVEY.md — the survey).

    One review shares a review_id and emits: a 'rating' row (the overall star) +
    one row per graded axis in `axes` + one 'tag' row per chip + optional
    'follow'/'note' rows. Append-only: re-rating adds a NEW batch (drift
    preserved); the verdict is computed on read (query_enjoyment).

      rating : int 1–5, or None to clear (records a 'clear' event; tags/axes/note
               ignored — see below on follow).
      tags   : subset of RATING_TAGS (categorical chips).
      axes   : {axis_name: int 1–5} over SURVEY_AXES (topic_pull, presenter,
               audio_fidelity, speech_clarity, difficulty).
      follow : one of FOLLOW_STATES — a per-CHANNEL intent decoupled from this
               video's score; also upserted onto channels.follow_state. Recorded
               even on a rating clear, since it's about the channel, not the video.
      note   : free text; the judge parses it to fill any axis you didn't tap.

    review_id: normally minted here, but an offline client may supply its own so
    an outbox re-flush doesn't double-append — a replayed review_id is a no-op.

    episodes.rating/rated_at are a denormalized latest-rating cache for cheap
    reads (server /jobs); the append-only log is the truth.
    """
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int)
                               or not 1 <= rating <= 5):
        raise ValueError(f"rating must be 1-5 or null, got {rating!r}")
    tags = list(dict.fromkeys(tags or []))  # dedupe, preserve order
    bad = [t for t in tags if t not in RATING_TAGS]
    if bad:
        raise ValueError(f"unknown taste tag(s): {bad}; allowed: {sorted(RATING_TAGS)}")
    axes = dict(axes or {})
    bad_axes = [a for a in axes if a not in SURVEY_AXES]
    if bad_axes:
        raise ValueError(f"unknown survey axis/axes: {bad_axes}; "
                         f"allowed: {sorted(SURVEY_AXES)}")
    for a, v in axes.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
            raise ValueError(f"survey axis {a!r} must be 1-5, got {v!r}")
    if follow is not None and follow not in FOLLOW_STATES:
        raise ValueError(f"follow must be one of {FOLLOW_STATES}, got {follow!r}")
    row = conn.execute(
        "SELECT channel_id, channel FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if not row:
        raise KeyError(f"episode not found in ledger: {episode_id}")
    if review_id and conn.execute(
            "SELECT 1 FROM taste_events WHERE review_id = ?", (review_id,)).fetchone():
        return {"episode_id": episode_id, "review_id": review_id, "rating": rating,
                "tags": tags if rating is not None else [],
                "axes": axes if rating is not None else {},
                "follow": follow, "duplicate": True}

    ts = now_iso()
    review_id = review_id or uuid.uuid4().hex

    def _ev(kind, value):
        conn.execute(
            "INSERT INTO taste_events (episode_id, review_id, kind, value, ts) "
            "VALUES (?, ?, ?, ?, ?)", (episode_id, review_id, kind, str(value), ts))

    _ev("rating", "clear" if rating is None else rating)
    if rating is not None:
        for tag in tags:
            _ev("tag", tag)
        for axis, val in axes.items():
            _ev(axis, val)
        if note:
            _ev("note", note)
    # Follow is a channel intent, not a video verdict — survives a rating clear.
    if follow is not None:
        _ev("follow", follow)
        set_follow(conn, row["channel_id"], row["channel"], follow, ts=ts)

    conn.execute(
        "UPDATE episodes SET rating = ?, rated_at = ? WHERE id = ?",
        (rating, ts if rating is not None else None, episode_id))
    conn.commit()
    return {"episode_id": episode_id, "review_id": review_id, "rating": rating,
            "tags": tags if rating is not None else [],
            "axes": axes if rating is not None else {}, "follow": follow}


def set_follow(conn, channel_id, channel, state, ts=None):
    """Upsert a channel's follow intent (SURVEY.md §4a). `block` is a hard veto
    the recommender drops from seeds; `more` keeps a channel a strong seed even
    when the video that prompted it was mediocre. No-op without a channel_id
    (local files, provenance-less sources)."""
    if not channel_id:
        return {"channel_id": None, "follow_state": state, "stored": False}
    if state not in FOLLOW_STATES:
        raise ValueError(f"follow must be one of {FOLLOW_STATES}, got {state!r}")
    ts = ts or now_iso()
    conn.execute(
        """INSERT INTO channels (channel_id, channel, follow_state, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(channel_id) DO UPDATE SET
               follow_state = excluded.follow_state,
               channel = COALESCE(excluded.channel, channels.channel),
               updated_at = excluded.updated_at""",
        (channel_id, channel, state, ts))
    conn.commit()
    return {"channel_id": channel_id, "follow_state": state, "stored": True}


def get_presenter_profile(conn, channel_id):
    """The current presenter fingerprint for a channel, or None (SURVEY.md §4c).
    Read this before a curate pass to feed the incremental merge — the profile is
    the durable memory of a presenter that the ephemeral transcript folds into."""
    if not channel_id:
        return None
    row = conn.execute(
        "SELECT profile FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if not row or not row["profile"]:
        return None
    try:
        return json.loads(row["profile"])
    except (json.JSONDecodeError, TypeError):
        return None


def _as_list(v):
    return [x for x in (v or []) if isinstance(x, str)] if isinstance(v, list) else []


def set_presenter_profile(conn, channel_id, channel, profile, episode_id=None):
    """Store a channel's presenter fingerprint (SURVEY.md §4c). `profile` is the
    already-merged dict the curate step produced from (this transcript + the
    prior profile) — the LLM does the semantic merge; this function persists it
    and guards the write against a concurrent sibling.

    Parallel curate agents on one series (autopilot waves, `/series` batches)
    each read the profile, merge their episode in, and write back — last
    writer wins, and the losers' observations vanish (Angel Cop, 2026-09-05:
    six agents, the stored profile ended at 5 of 6 episodes). So the write
    happens under BEGIN IMMEDIATE and is reconciled against what is stored
    NOW, not what the caller read earlier:

      - `provenance.episodes` is the union of stored + incoming (+ episode_id);
        `provenance.observations` is at least its length (legacy profiles
        without an episode list keep max(incoming, stored) + 1 when the
        incoming count is not ahead of the stored one).
      - if the stored profile carries episodes the incoming one never saw,
        the stored snapshot (characterization, measured, variance) is kept
        under `provenance.folded` so the next curate pass can fold it into
        the prose properly — nothing observed is thrown away — and the
        result reports `stale_merge: true` with the folded episode ids.

    Stamps provenance.updated_at. No-op without a channel_id."""
    if not channel_id:
        return {"channel_id": None, "stored": False}
    profile = dict(profile or {})
    prov = dict(profile.get("provenance") or {})
    incoming_eps = _as_list(prov.get("episodes"))
    if episode_id and episode_id not in incoming_eps:
        incoming_eps.append(episode_id)

    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")  # take the write lock before the read
    try:
        stored = get_presenter_profile(conn, channel_id) or {}
        s_prov = dict(stored.get("provenance") or {})
        stored_eps = _as_list(s_prov.get("episodes"))
        unseen = [e for e in stored_eps if e not in incoming_eps]
        episodes = list(dict.fromkeys(stored_eps + incoming_eps))

        obs_in = prov.get("observations") or 0
        obs_st = s_prov.get("observations") or 0
        stale = bool(unseen) or (bool(stored) and not stored_eps and obs_in <= obs_st)
        observations = max(obs_in, len(episodes)) if episodes else obs_in
        if stale and not unseen:
            # legacy profile (no episode list) overwritten under a race: the
            # incoming count was derived from a stale read — count both.
            observations = max(observations, obs_st + 1)

        folded = [f for f in (prov.get("folded") or []) if isinstance(f, dict)]
        if unseen:
            snap = {k: stored.get(k) for k in ("characterization", "measured", "variance")
                    if stored.get(k) is not None}
            snap["episodes"] = unseen
            folded.append(snap)
            folded = folded[-5:]
            # keep the overwritten snapshots' own folded backlog too
            for f in (s_prov.get("folded") or []):
                if isinstance(f, dict) and f not in folded:
                    folded.append(f)
            folded = folded[-5:]

        if episodes:
            prov["episodes"] = episodes
        prov["observations"] = observations
        if folded:
            prov["folded"] = folded
        else:
            prov.pop("folded", None)
        prov["updated_at"] = now_iso()
        profile["provenance"] = prov
        conn.execute(
            """INSERT INTO channels (channel_id, channel, profile, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   profile = excluded.profile,
                   channel = COALESCE(excluded.channel, channels.channel),
                   updated_at = excluded.updated_at""",
            (channel_id, channel, json.dumps(profile, ensure_ascii=False), prov["updated_at"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    out = {"channel_id": channel_id, "observations": observations, "stored": True}
    if stale:
        out["stale_merge"] = True
        out["folded_episodes"] = unseen
    return out


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


# Immersion-time log kinds (MOBILE.md — viewing time): active watching in
# the in-app player vs passive listening in the background audio service.
VIEW_KINDS = ("watch", "listen")
# Where a session came from: recorded by the app's player/service, typed in
# by hand on the Progress tab (listening done outside the app), or imported
# from the pre-app spreadsheet (tools/import_tracker_pdf.py).
VIEW_SOURCES = ("app", "manual", "import")
# Subtitle state of a player sitting, seconds each (view_sessions.modes):
# on = full subs · kw = keyword lines only · off = subs hidden · audio = the
# 🎧 handoff (screen off, native service). Listen-tab time is its own kind.
SUB_MODES = ("on", "kw", "off", "audio")
# Where a word was met when it was looked up or marked (claim / lookup
# context.mode): a player state, the Listen tab, a 5ch page, the prep doc.
ENCOUNTER_MODES = SUB_MODES + ("listen", "page", "prep")


def record_view_session(conn, session):
    """Store one phone-recorded playback session (MOBILE.md — viewing time).

    `session` is the client's segment: {id, episode_id, kind: watch|listen,
    day: YYYY-MM-DD (device-local), start: ISO, secs, reached?, duration?,
    title?}. Append-only and idempotent on the client-minted id, so an outbox
    re-flush is a no-op. The episode need not exist in the ledger: time spent
    is a fact about the learner's day, not about the episode's lifecycle, and
    it must survive the episode being deleted (the title rides along for
    display)."""
    if not isinstance(session, dict):
        raise ValueError("session must be an object")
    sid = session.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("session id required")
    ep = session.get("episode_id")
    if not isinstance(ep, str) or not ep.strip():
        raise ValueError("episode_id required")
    kind = session.get("kind")
    if kind not in VIEW_KINDS:
        raise ValueError(f"kind must be one of {VIEW_KINDS}, got {kind!r}")
    day = session.get("day")
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")
    start = session.get("start")
    if not isinstance(start, str) or not start:
        raise ValueError("start (ISO timestamp) required")

    def _num(key, required=False):
        v = session.get(key)
        if v is None:
            if required:
                raise ValueError(f"{key} required")
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{key} must be a non-negative number, got {v!r}")
        return float(v)

    secs = _num("secs", required=True)
    reached = _num("reached")
    duration = _num("duration")
    title = session.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string")
    source = session.get("source") or "app"
    if source not in VIEW_SOURCES:
        raise ValueError(f"source must be one of {VIEW_SOURCES}, got {source!r}")
    modes = session.get("modes")
    if modes is not None:
        if not isinstance(modes, dict):
            raise ValueError("modes must be an object of {state: secs}")
        bad = set(modes) - set(SUB_MODES)
        if bad:
            raise ValueError(f"unknown subtitle state(s): {sorted(bad)}; allowed: {SUB_MODES}")
        for k, v in modes.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"modes[{k}] must be a non-negative number, got {v!r}")
        modes = {k: float(v) for k, v in modes.items() if v > 0} or None
    if conn.execute("SELECT 1 FROM view_sessions WHERE id = ?", (sid,)).fetchone():
        return {"id": sid, "duplicate": True}
    conn.execute(
        "INSERT INTO view_sessions (id, episode_id, title, kind, day, start, secs, "
        "reached, duration, source, modes, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, ep, title, kind, day, start, secs, reached, duration, source,
         json.dumps(modes) if modes else None, now_iso()))
    conn.commit()
    return {"id": sid, "duplicate": False}


def delete_view_session(conn, sid):
    """Remove one session — the Progress tab's ✕ on a hand-typed entry (the
    app only offers it for source='manual'; recorded sittings are facts and
    imported rows are managed on the PC). Idempotent: a missing id is fine."""
    n = conn.execute("DELETE FROM view_sessions WHERE id = ?", (sid,)).rowcount
    conn.commit()
    return {"id": sid, "deleted": n > 0}


def query_view_sessions(conn, since=None):
    """Every stored playback session (optionally from device-day `since`,
    inclusive), oldest first — the phone merges these by id into its local
    log, so a reinstalled app gets its history back."""
    sql = ("SELECT id, episode_id, title, kind, day, start, secs, reached, duration, source, "
           "modes FROM view_sessions")
    params = ()
    if since:
        sql += " WHERE day >= ?"
        params = (since,)
    sql += " ORDER BY day, start, id"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["modes"] = json.loads(d["modes"]) if d["modes"] else None
        out.append(d)
    return out


def query_view_totals(conn):
    """Per-day watch/listen seconds, newest day first — the CLI's readable
    view of the time log (the phone renders its own weeks)."""
    out = {}
    for r in conn.execute(
            "SELECT day, kind, SUM(secs) AS secs FROM view_sessions "
            "GROUP BY day, kind ORDER BY day DESC"):
        out.setdefault(r["day"], {"day": r["day"], "watch": 0.0, "listen": 0.0})
        out[r["day"]][r["kind"]] = round(r["secs"], 1)
    return list(out.values())


def record_curate_items(conn, episode_id, curation, jmdict_conn=None):
    """Land /immerse curate's phrase + grammar emissions as (inert) exposure
    evidence (GRAMMAR.md — Production path). Detection is LLM-emits,
    server-validates: the LLM proposes, this function decides what may become
    a tracked key — nothing is key-minted silently.

    curation["phrases"]: [{sentence_idx, surface, canonical, classification,
                           gloss?, reading?}]
      canonical must be a JMdict headword (deinflection sidestepped: the LLM
      returns the dictionary form, we only check it's a real key) — OR, when
      JMdict lacks it, carry a curate-authored `gloss` (the AI pass fills the
      dictionary's gaps the same way `defs` does for words; the gloss is the
      deliberate act that mints the key, and /definitions serves it) — AND
      must itself tokenize to ≥2 Sudachi tokens — the canonical form, NOT the
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
        gloss = (p.get("gloss") or "").strip()
        if not J.is_headword(jmdict_conn, canonical) and not gloss:
            rejected.append({"canonical": canonical, "reason": "not_a_jmdict_headword",
                             "hint": "add a gloss (+ reading) to the phrase entry to track it"})
            continue
        if len(tokenize(canonical)) < 2:
            rejected.append({"canonical": canonical, "reason": "single_token"})
            continue
        entries = J.lookup_many(jmdict_conn, [canonical], max_entries=1)
        readings = (entries.get(canonical) or [{}])[0].get("r") or []
        reading = readings[0] if readings else (p.get("reading") or "").strip() or None
        _touch_lemma(conn, canonical, reading=reading,
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


def add_non_vocab(conn, entries, origin=None):
    """Register repair-gate adjudications in the cross-episode registry.

    entries: [{key, kind, note?}] — kind ∈ name|nonword. First flag wins
    (INSERT OR IGNORE): a key's origin records the episode that discovered
    it, and re-flagging from later episodes is a no-op."""
    ts = now_iso()
    n = 0
    for e in entries:
        key = (e.get("key") or "").strip()
        if not key:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO non_vocab (key, kind, note, origin, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, e.get("kind") or "name", e.get("note"), origin, ts))
        n += cur.rowcount
    conn.commit()
    return {"added": n, "total": conn.execute(
        "SELECT COUNT(*) FROM non_vocab").fetchone()[0]}


def get_non_vocab(conn):
    """All registered non-vocab keys, for coverage's exclusion set."""
    return frozenset(r[0] for r in conn.execute("SELECT key FROM non_vocab"))


def remove_non_vocab(conn, key):
    """Un-register a key the repair gate over-flagged (it was a real word)."""
    cur = conn.execute("DELETE FROM non_vocab WHERE key = ?", (key,))
    conn.commit()
    return {"key": key, "removed": cur.rowcount}


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

    payload: {"episode_id", "batch_id", "taps": [[key, mark, kind?], ...]}
    Marks: "k" → tap_known, "u" → tap_unknown (knowledge evidence, counted in
    `applied`). "h" → tap_interest, a durable "I want to learn this" want that
    is NOT knowledge (counted in `interest`): it persists across episodes and
    steers future card selection (tools.select) until the lemma becomes known.
    kind (optional third element) is 'word' (default) or 'phrase' — the
    player's phrase layer marks a multi-word expression (血が騒ぐ) as its
    own item, independent of its component words (GRAMMAR.md): the evidence
    row carries the kind, and the lemmas row is created as a phrase if the
    key is new (a deliberate mark is as good as `phrase-add`).

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
    # lookups first: a ✓ in the same batch snapshots the opens that led to it
    lookups = _apply_lookups(conn, episode_id, payload.get("lookups") or [], ts)
    for entry in payload.get("taps", []):
        lemma, verdict = entry[0], entry[1]
        kind = entry[2] if len(entry) > 2 and entry[2] else "word"
        # what the word was painted as when the mark was made (LOOKUP_LISTS)
        # and where it was met (ENCOUNTER_MODES: subtitle state / page / prep)
        painted = entry[3] if len(entry) > 3 and entry[3] in LOOKUP_LISTS else None
        met = entry[4] if len(entry) > 4 and entry[4] in ENCOUNTER_MODES else None
        source = sources.get(verdict)
        if source is None or kind not in ("word", "phrase"):
            continue
        _touch_lemma(conn, lemma, ts=ts, kind=kind,
                     pos="expression" if kind == "phrase" else None)
        # The phone syncs marks live and each batch is the episode's *whole*
        # mark set (so card selection sees every k/h), so the same tap arrives
        # again with every later change. One tap on one word in one episode is
        # one piece of evidence — never let re-sent snapshots stack weight.
        if conn.execute(
            """SELECT 1 FROM evidence
               WHERE lemma = ? AND kind = ? AND source = ? AND episode_id IS ?""",
            (lemma, kind, source, episode_id),
        ).fetchone():
            continue
        context = None
        if source in CLAIM_SOURCES:
            context = json.dumps({"snap": claim_snapshot(conn, lemma, kind, ts),
                                  "list": painted, "mode": met}, ensure_ascii=False)
        conn.execute(
            """INSERT INTO evidence (lemma, kind, source, polarity, weight, episode_id, context, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lemma, kind, source, POLARITY[source], WEIGHT[source], episode_id, context, ts),
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
              "applied": applied, "lookups": lookups, "duplicate": False}
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


LOOKUP_LISTS = ("confirm", "interest", "should_know", "known", "none")


def _apply_lookups(conn, episode_id, entries, ts):
    """Land a batch's popup lookups: [[key, n, {list: n, …}, kind?, {mode: n}?], …] —
    the phone's cumulative count for this episode, so every re-sent batch
    carries the whole set and the row is replaced, never stacked (one
    lookup row per item per episode). `n` is every open of the popup on the
    item; the per-list split says what it was painted as at each tap
    (LOOKUP_LISTS; 'none' = plain); the optional fifth element splits the
    opens by where the word was met (ENCOUNTER_MODES — subtitle state /
    page / prep). Returns the number of rows written."""
    written = 0
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        lemma, n = entry[0], entry[1]
        lists = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
        kind = entry[3] if len(entry) > 3 and entry[3] else "word"
        if not isinstance(lemma, str) or not lemma or kind not in ("word", "phrase"):
            continue
        if isinstance(n, bool) or not isinstance(n, (int, float)) or n <= 0:
            continue
        lists = {k: int(v) for k, v in lists.items()
                 if k in LOOKUP_LISTS and isinstance(v, (int, float)) and v > 0}
        modes = entry[4] if len(entry) > 4 and isinstance(entry[4], dict) else {}
        modes = {k: int(v) for k, v in modes.items()
                 if k in ENCOUNTER_MODES and isinstance(v, (int, float)) and v > 0}
        payload = {"n": int(n), "lists": lists}
        if modes:
            payload["modes"] = modes
        context = json.dumps(payload, ensure_ascii=False)
        _touch_lemma(conn, lemma, ts=ts, kind=kind,
                     pos="expression" if kind == "phrase" else None)
        row = conn.execute(
            """SELECT id, context FROM evidence
               WHERE lemma = ? AND kind = ? AND source = 'lookup' AND episode_id IS ?""",
            (lemma, kind, episode_id)).fetchone()
        if row:
            if row["context"] != context:
                conn.execute("UPDATE evidence SET context = ?, ts = ? WHERE id = ?",
                             (context, ts, row["id"]))
                written += 1
            continue
        conn.execute(
            """INSERT INTO evidence (lemma, kind, source, polarity, weight, episode_id, context, ts)
               VALUES (?, ?, 'lookup', 0, 0.0, ?, ?, ?)""",
            (lemma, kind, episode_id, context, ts))
        written += 1
    return written


def _lookup_totals(evs):
    """(lookups, lookups_listed) over an item's lookup rows — every popup
    open, and the opens made while the word sat on a list (blue / ★ /
    green; 'known' and 'none' are not lists)."""
    total = listed = 0
    for e in evs:
        if e["source"] != "lookup":
            continue
        ctx = _ctx(e)
        total += int(ctx.get("n", 0))
        listed += sum(int(v) for k, v in (ctx.get("lists") or {}).items()
                      if k in ("confirm", "interest", "should_know"))
    return total, listed


def _record_confirm(conn, key, source, kind="word"):
    """Append one confirm_known / confirm_defer evidence row for an item the
    exposure heuristic surfaced. The key is the word lemma, phrase headword,
    or grammar pattern; kind routes it to the right projection at promote.
    Caller should `promote` after."""
    ts = now_iso()
    context = None
    if kind != "grammar":
        _touch_lemma(conn, key, ts=ts, kind=kind)
        context = json.dumps({"snap": claim_snapshot(conn, key, kind, ts), "list": "prompt"},
                             ensure_ascii=False)
    conn.execute(
        """INSERT INTO evidence (lemma, kind, source, polarity, weight, episode_id, context, ts)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
        (key, kind, source, POLARITY[source], WEIGHT[source], context, ts))
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

# --- claim snapshots + the adaptive scorer --------------------------------------
# Every knowledge claim (✓ / ✗ / prompt yes / not-yet) stores, in its own
# evidence row, a snapshot of how the word had been met up to that moment
# (`context.snap`) and what it was painted as on the phone (`context.list`:
# confirm / interest / should_know / known / none, or 'prompt' for the
# exposure queue). That is the training data for confirm_model: append-only,
# so a later change of features or rules can re-read the whole history.

CLAIM_SOURCES = ("tap_known", "tap_unknown", "confirm_known", "confirm_defer")
MODEL_MIN_ROWS = 150      # labeled snapshots before the fit replaces the hand gate
MODEL_REFIT_EVERY = 25    # new labeled snapshots between automatic refits
_KANA_RE = re.compile(r"[぀-ヿー]+")


def _days_between(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return 0.0


def _snapshot(rows, at_ts, freq_rank=None, pos=None, lemma="", exclude_id=None):
    """Feature snapshot of one word from its evidence rows (id, source, ts,
    context, episode_id, watched) up to `at_ts` — the claim's own row, when
    it already exists (backfill), is passed as exclude_id. Shared by the
    claim-time recorder and promote's live scoring so train and serve
    agree by construction."""
    before = [r for r in rows if r["ts"] <= at_ts and r["id"] != exclude_id]
    exp = [r for r in before if r["source"] == "exposure" and r["watched"]]
    ctxs = [_ctx(r) for r in exp]
    krs = [float(c["known_ratio"]) for c in ctxs
           if isinstance(c.get("known_ratio"), (int, float))]
    lookups, lookups_listed = _lookup_totals(before)
    return {
        "rank": freq_rank,
        "eps": len({r["episode_id"] for r in exp}),
        "q": sum(_exposure_qualifies(c) for c in ctxs),
        "q0": sum(c.get("other_unknown_count", 99) == 0 for c in ctxs),
        "kr_mean": round(sum(krs) / len(krs), 3) if krs else None,
        "kr_max": max(krs) if krs else None,
        "occ": sum(c.get("occ", 1) for c in ctxs),
        "lookups": lookups,
        "lookups_listed": lookups_listed,
        "days_first": round(_days_between(exp[0]["ts"], at_ts), 1) if exp else 0.0,
        "days_last": round(_days_between(max(r["ts"] for r in exp), at_ts), 1) if exp else 0.0,
        "defers": sum(r["source"] == "confirm_defer" for r in before),
        "prior_known": sum(r["source"] in ("tap_known", "confirm_known") for r in before),
        "prior_unknown": sum(r["source"] == "tap_unknown" for r in before),
        "is_verb": pos == _VERB_POS,
        "is_kana": bool(lemma) and bool(_KANA_RE.fullmatch(lemma)),
        "length": len(lemma),
    }


def _lemma_rows(conn, lemma, kind="word"):
    return conn.execute(
        """SELECT e.id, e.source, e.ts, e.context, e.episode_id,
                  COALESCE(ep.watched, 0) AS watched
           FROM evidence e LEFT JOIN episodes ep ON ep.id = e.episode_id
           WHERE e.lemma = ? AND e.kind = ? ORDER BY e.ts, e.id""", (lemma, kind)).fetchall()


def claim_snapshot(conn, lemma, kind="word", at_ts=None):
    """The snapshot to store on a claim being written now."""
    at_ts = at_ts or now_iso()
    rank = conn.execute("SELECT rank FROM freq WHERE lemma = ?", (lemma,)).fetchone()
    pos = conn.execute("SELECT pos FROM lemmas WHERE lemma = ?", (lemma,)).fetchone()
    snap = _snapshot(_lemma_rows(conn, lemma, kind), at_ts,
                     rank[0] if rank else None, pos[0] if pos else None, lemma)
    # global context at the moment — not a model feature, kept for later study
    snap["known_set_size"] = conn.execute(
        "SELECT COUNT(*) FROM lemmas WHERE status = 'known'").fetchone()[0]
    return snap


def backfill_snapshots(conn):
    """Stamp `snap` onto historical claim rows (words) that lack one, from
    the evidence that preceded each. Re-runnable; rows already carrying a
    snapshot are left alone (a claim's snapshot is a fact about that
    moment and is never recomputed)."""
    freq = dict(conn.execute("SELECT lemma, rank FROM freq").fetchall())
    pos_by = dict(conn.execute("SELECT lemma, pos FROM lemmas").fetchall())
    claims = conn.execute(
        f"""SELECT id, lemma, ts, context FROM evidence
            WHERE kind = 'word' AND source IN ({",".join("?" * len(CLAIM_SOURCES))})
            ORDER BY lemma, ts""", CLAIM_SOURCES).fetchall()
    done = 0
    rows_cache = {}
    for r in claims:
        ctx = _ctx(r)
        if "snap" in ctx:
            continue
        if r["lemma"] not in rows_cache:
            rows_cache[r["lemma"]] = _lemma_rows(conn, r["lemma"])
        ctx["snap"] = _snapshot(rows_cache[r["lemma"]], r["ts"], freq.get(r["lemma"]),
                                pos_by.get(r["lemma"]), r["lemma"], exclude_id=r["id"])
        conn.execute("UPDATE evidence SET context = ? WHERE id = ?",
                     (json.dumps(ctx, ensure_ascii=False), r["id"]))
        done += 1
    conn.commit()
    return {"claims": len(claims), "stamped": done}


def training_rows(conn):
    """[(snapshot, label)] for the scorer: prompt answers (yes / not yet)
    and ✓ / ✗ marks made on a blue word — every claim that judged a word
    the list had put forward. Claims on unlisted words are kept in the
    evidence (and in the calibration report) but not fit on: they carry no
    "no" side, and mostly say which words were known before this system."""
    rows = []
    for r in conn.execute(
            f"""SELECT source, context FROM evidence
                WHERE kind = 'word' AND source IN ({",".join("?" * len(CLAIM_SOURCES))})""",
            CLAIM_SOURCES):
        ctx = _ctx(r)
        snap = ctx.get("snap")
        if not snap:
            continue
        if r["source"].startswith("confirm") or ctx.get("list") == "confirm":
            rows.append((snap, r["source"] in ("tap_known", "confirm_known")))
    return rows


def fit_confirm_model(conn, target=0.8):
    """Fit the scorer on training_rows and store it (models.name='confirm').
    Returns its metrics; with too few rows nothing is stored."""
    rows = training_rows(conn)
    if len(rows) < MODEL_MIN_ROWS:
        return {"fitted": False, "rows": len(rows), "needed": MODEL_MIN_ROWS}
    model = confirm_model.fit(rows, target=target)
    conn.execute(
        """INSERT INTO models (name, params, n_rows, trained_at) VALUES ('confirm', ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET params = excluded.params,
               n_rows = excluded.n_rows, trained_at = excluded.trained_at""",
        (json.dumps(model), len(rows), now_iso()))
    conn.commit()
    return {"fitted": True, "rows": len(rows), "cutoff": model["cutoff"], **model["metrics"]}


def load_confirm_model(conn):
    row = conn.execute("SELECT params, n_rows, trained_at FROM models WHERE name = 'confirm'").fetchone()
    if not row:
        return None
    model = json.loads(row["params"])
    model["n_rows"] = row["n_rows"]
    model["trained_at"] = row["trained_at"]
    return model


def _maybe_refit(conn):
    """promote's hook: refit once MODEL_REFIT_EVERY new labeled snapshots
    have accrued since the stored fit (or on first reaching MODEL_MIN_ROWS)."""
    n = len(training_rows(conn))
    if n < MODEL_MIN_ROWS:
        return None
    model = load_confirm_model(conn)
    if model is None or n >= model["n_rows"] + MODEL_REFIT_EVERY:
        target = (model or {}).get("metrics", {}).get("target_precision", 0.8)
        fit_confirm_model(conn, target=target)
        model = load_confirm_model(conn)
    return model


def _judge(evs, theta, spread_needed, in_anki_known=False, plays=None, pos=None,
           model=None, freq_rank=None, lemma=""):
    """Apply promote's rule order to one item's evidence rows (any kind —
    word, phrase, or grammar; sources an item never receives simply yield
    empty lists). Returns the projection fields.

    plays: {episode_id: (active_plays, passive_plays)} from episode_plays —
    multiplies each exposure's per-episode occurrence count (`occ`; 1 when
    the row predates occurrence tracking) into the "times seen" tallies:
    seen_active over watched exposures (a watched episode is at least one
    play), seen_passive over every exposure (Listen-tab plays count whether
    or not the episode was ever watched with subtitles).

    pos: the item's Sudachi part of speech (words) — verbs face the higher
    known-ratio bar (CONFIRM_MIN_KNOWN_RATIO_VERB).

    model: the fitted confirm scorer (load_confirm_model) — when it carries
    a cutoff, its P(known) over the word's live snapshot replaces the hand
    gate; the score is projected either way (confirm_score)."""
    plays = plays or {}
    taps_known = [e for e in evs if e["source"] == "tap_known"]
    imports = [e for e in evs if e["source"] == "import"]
    confirms = [e for e in evs if e["source"] == "confirm_known"]
    defers = [e for e in evs if e["source"] == "confirm_defer"]
    negatives = [e for e in evs if e["source"] in ("tap_unknown", "card_lapse")]
    mined = [e for e in evs if e["source"] == "mined_card"]
    active_exposures = [e for e in evs if e["source"] == "exposure" and e["watched"]]

    qualifying = []
    seen_active = 0.0
    known_ratios = []
    by_mode = {}  # subtitle state → times seen; "unknown" = plays no sitting described
    for e in active_exposures:
        ctx = _ctx(e)
        if _exposure_qualifies(ctx):
            qualifying.append(e)
        if isinstance(ctx.get("known_ratio"), (int, float)):
            known_ratios.append(float(ctx["known_ratio"]))
        occ = ctx.get("occ", 1)
        active, _passive, modes = plays.get(e["episode_id"], (0.0, 0.0, {}))
        active = max(1.0, active)
        seen_active += occ * active
        for m, n in modes.items():
            by_mode[m] = by_mode.get(m, 0.0) + occ * n
        rest = active - sum(modes.values())
        if rest > 1e-9:
            by_mode["unknown"] = by_mode.get("unknown", 0.0) + occ * rest
    seen_passive = 0.0
    for e in evs:
        if e["source"] != "exposure":
            continue
        n = _ctx(e).get("occ", 1) * plays.get(e["episode_id"], (0.0, 0.0, {}))[1]
        seen_passive += n
        if n:
            by_mode["listen"] = by_mode.get("listen", 0.0) + n
    by_mode = {m: int(round(n)) for m, n in by_mode.items() if round(n) >= 1}
    lookups, lookups_listed = _lookup_totals(evs)
    q_count = len(qualifying)
    q_spread = len({e["episode_id"] for e in qualifying})

    positives = taps_known + imports + confirms + active_exposures
    last_negative = max((e["ts"] for e in negatives), default=None)
    last_positive = max((e["ts"] for e in positives), default=None)

    needs_review = 0
    confirm_candidate = 0
    cleared_bar = q_count >= theta and q_spread >= spread_needed
    # Precision gate (see CONFIRM_MIN_KNOWN_RATIO): the sentences the word
    # was met in were mostly understood. Items whose exposures carry no
    # known_ratio (phrases, grammar) are gated by θ alone.
    kr_bar = CONFIRM_MIN_KNOWN_RATIO_VERB if pos == _VERB_POS else CONFIRM_MIN_KNOWN_RATIO
    context_ok = (not known_ratios or
                  sum(known_ratios) / len(known_ratios) >= kr_bar)
    confirm_score = None
    if model is not None and known_ratios:
        # the adaptive scorer: same snapshot function as the claims it was
        # fit on, taken "now" (a ts past every row)
        snap = _snapshot(evs, "9999", freq_rank, pos, lemma)
        confirm_score = round(confirm_model.predict(model, snap), 3)
        if model.get("cutoff") is not None:
            context_ok = confirm_score >= model["cutoff"]

    def _candidate():
        # Exposures cleared the bar, but a fuzzy count can't *assert*
        # knowledge — surface it for confirmation instead of promoting.
        # After a "not yet" the word re-earns the bar: only qualifying
        # exposures that landed after the latest defer count, and they must
        # clear θ / k by themselves (one fresh sighting used to be enough —
        # deferred words came back "yes" 13 times in 73).
        if not context_ok:
            return 0
        last_defer = max((e["ts"] for e in defers), default=None)
        if last_defer is None:
            return 1
        fresh = [e for e in qualifying if e["ts"] > last_defer]
        return int(len(fresh) >= theta and
                   len({e["episode_id"] for e in fresh}) >= spread_needed)

    # Ties go to the negative: taps are deliberate strong evidence, and a
    # same-second exposure/tap pair only happens when they were written
    # by the same run.
    if last_negative and (last_positive is None or last_negative >= last_positive):
        status = "learning"
        if taps_known or confirms or in_anki_known:
            needs_review = 1
        # A ✗ retracts the *known* claim, not the exposures: a word you have
        # already met θ times in parseable lines goes straight onto the
        # think-you-know list (LIVE_REVIEW.md §5a) rather than into limbo —
        # the next encounter is the self-test. ✗ itself never snoozes.
        if cleared_bar:
            confirm_candidate = _candidate()
    elif taps_known or imports or confirms:
        status = "known"
    elif cleared_bar:
        status = "learning"
        confirm_candidate = _candidate()
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
        "seen_active": int(round(seen_active)),
        "seen_passive": int(round(seen_passive)),
        "lookups": lookups,
        "lookups_listed": lookups_listed,
        "confirm_score": confirm_score,
        "seen_by_mode": json.dumps(by_mode) if by_mode else None,
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
       route to REPLACE via the needs_review queue. The negative only cancels
       the knowledge claim: if the item's qualifying exposures already clear
       θ (rule 3) it is flagged confirm_candidate at once — a ✗ on a word you
       have met often enough lands it on the think-you-know list, and one on
       a frequent word lets it fall into the should-know window (should_know
       reads status ≠ known), instead of leaving it in an unlisted limbo.
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
    activate_played_episodes(conn)
    plays = episode_plays(conn)
    model = _maybe_refit(conn)
    rows = conn.execute(
        """SELECT e.id, e.lemma, e.kind, e.source, e.ts, e.context, e.episode_id,
                  COALESCE(ep.watched, 0) AS watched
           FROM evidence e LEFT JOIN episodes ep ON ep.id = e.episode_id
           ORDER BY e.lemma, e.ts"""
    ).fetchall()

    freq = dict(conn.execute("SELECT lemma, rank FROM freq").fetchall())
    pos_by_lemma = dict(conn.execute("SELECT lemma, pos FROM lemmas").fetchall())
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
        v = _judge(evs, theta, spread_needed, in_anki_known=lemma in anki_known,
                   plays=plays, pos=pos_by_lemma.get(lemma),
                   model=model if kind == "word" else None, freq_rank=freq_rank,
                   lemma=lemma)
        conn.execute(
            """INSERT INTO lemmas (lemma, kind, freq_rank, status, confidence,
                                   exposure_count, episode_spread, seen_active,
                                   seen_passive, lookups, lookups_listed, confirm_score,
                                   seen_by_mode, needs_review, confirm_candidate,
                                   first_seen, last_seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(lemma) DO UPDATE SET
                   freq_rank = excluded.freq_rank,
                   status = excluded.status,
                   confidence = excluded.confidence,
                   exposure_count = excluded.exposure_count,
                   episode_spread = excluded.episode_spread,
                   seen_active = excluded.seen_active,
                   seen_passive = excluded.seen_passive,
                   lookups = excluded.lookups,
                   lookups_listed = excluded.lookups_listed,
                   confirm_score = excluded.confirm_score,
                   seen_by_mode = excluded.seen_by_mode,
                   needs_review = excluded.needs_review,
                   confirm_candidate = excluded.confirm_candidate,
                   first_seen = COALESCE(lemmas.first_seen, excluded.first_seen),
                   last_seen = excluded.last_seen,
                   updated_at = excluded.updated_at
               """,
            (lemma, kind, freq_rank, v["status"], v["confidence"],
             v["exposure_count"], v["episode_spread"], v["seen_active"],
             v["seen_passive"], v["lookups"], v["lookups_listed"], v["confirm_score"],
             v["seen_by_mode"], v["needs_review"], v["confirm_candidate"],
             v["first_seen"], v["last_seen"], ts_now),
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


def episode_plays(conn):
    """{episode_id: (active_plays, passive_plays, {sub_state: plays})} from the
    phone's recorded sittings (view_sessions, source='app'). A play is
    wall-clock playing time over the media length, unclipped — a Listen-tab
    loop that ran an episode twice is two plays. `watch` (the in-app player,
    subtitles on or off) is active; `listen` (the Listen tab's queue) is
    passive. The two are kept apart all the way to the lemma row
    (seen_active / seen_passive): passive exposure counts, but it counts
    differently. The third element splits the active plays by the sitting's
    subtitle state (SUB_MODES) where the app reported one. Hand-typed and
    imported sittings carry no real episode id and fall out naturally."""
    durations = dict(conn.execute(
        "SELECT id, duration FROM episodes WHERE duration IS NOT NULL").fetchall())
    plays = {}
    for r in conn.execute(
            "SELECT episode_id, kind, secs, duration, modes FROM view_sessions "
            "WHERE source = 'app'"):
        dur = r["duration"] or durations.get(r["episode_id"])
        if dur and dur > 0:
            frac = r["secs"] / dur
            per_sec = 1.0 / dur
        else:
            frac = 1.0 if r["secs"] >= _PLAY_UNKNOWN_DURATION_SECS else 0.0
            per_sec = frac / r["secs"] if r["secs"] else 0.0
        a, p, by_mode = plays.get(r["episode_id"], (0.0, 0.0, {}))
        by_mode = dict(by_mode)
        if r["kind"] == "watch":
            a += frac
            try:
                modes = json.loads(r["modes"]) if r["modes"] else {}
            except ValueError:
                modes = {}
            for m, secs in modes.items():
                if m in SUB_MODES and isinstance(secs, (int, float)):
                    by_mode[m] = by_mode.get(m, 0.0) + secs * per_sec
        else:
            p += frac
        plays[r["episode_id"]] = (a, p, by_mode)
    return plays


def activate_played_episodes(conn):
    """Flip watched=1 on episodes the player has carried past
    PLAY_ACTIVATION_FRACTION of their length — the no-taps, subtitles-off
    watch that never went through the close-out. Idempotent; promote calls
    it first so those exposures judge as active. Returns the ids flipped."""
    flipped = []
    for ep, (active, _passive, _modes) in episode_plays(conn).items():
        if active >= PLAY_ACTIVATION_FRACTION:
            cur = conn.execute(
                "UPDATE episodes SET watched = 1 WHERE id = ? AND watched = 0", (ep,))
            if cur.rowcount:
                flipped.append(ep)
    if flipped:
        conn.commit()
    return flipped


def occurrences_from_coverage(cov):
    """{item key: {occ, occ_clean, occ_near}} recounted from a coverage.json
    (its per-sentence tokens and phrase units) — the same tally
    engine.lemma.analyze_transcript stamps on fresh exposures, for rows
    written before occurrence tracking. Phrase keys are prefixed 'phrase:'."""
    out = {}

    def bump(key, other):
        o = out.setdefault(key, {"occ": 0, "occ_clean": 0, "occ_near": 0})
        o["occ"] += 1
        if other == 0:
            o["occ_clean"] += 1
        elif other == 1:
            o["occ_near"] += 1

    for sent in cov.get("sentences", []):
        unknown = set(sent.get("unknown") or ())
        for t in sent.get("tokens", []):
            if not t.get("c"):
                continue
            bump(t["l"], len(unknown) - (1 if t["l"] in unknown else 0))
        for u in sent.get("phrases") or ():
            hw = u["phrase"]
            bump("phrase:" + hw, len(unknown) - (1 if hw in unknown else 0))
    return out


def backfill_occurrences(conn, episodes_root):
    """Stamp occ / occ_clean / occ_near onto exposure rows whose episode still
    has a coverage.json under episodes_root (purged episodes can't be
    recounted; their rows keep reading as one occurrence). Re-runnable — a
    row already carrying `occ` is left alone unless the recount differs."""
    root = Path(episodes_root)
    episodes = 0
    updated = 0
    for cov_path in sorted(root.glob("*/coverage.json")):
        try:
            cov = json.loads(cov_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ep = cov.get("episode_id") or cov_path.parent.name
        counts = occurrences_from_coverage(cov)
        if not counts:
            continue
        episodes += 1
        rows = conn.execute(
            "SELECT id, lemma, kind, context FROM evidence "
            "WHERE source = 'exposure' AND episode_id = ?", (ep,)).fetchall()
        for r in rows:
            key = ("phrase:" + r["lemma"]) if r["kind"] == "phrase" else r["lemma"]
            c = counts.get(key)
            if not c:
                continue
            ctx = _ctx(r)
            if all(ctx.get(k) == v for k, v in c.items()):
                continue
            ctx.update(c)
            conn.execute("UPDATE evidence SET context = ? WHERE id = ?",
                         (json.dumps(ctx, ensure_ascii=False), r["id"]))
            updated += 1
    conn.commit()
    return {"episodes": episodes, "rows_updated": updated}


# --- read verbs ----------------------------------------------------------------

def materialize_known(conn, cfg=None):
    """The ledger's known set — the set every mode reads.

    Ledger-only: no AnkiConnect, no cache. The Anki collection's known words
    were folded in once as 'import' evidence (origin anki_final, verb
    import-anki — LIVE_REVIEW.md §7), so the projection already holds
    everything Anki ever knew. `cfg` is accepted for call-site compatibility
    and unused.

    Returns the full known/learning picture plus the variant-matching sets
    engine.lemma.KnownSet consumes.
    """
    # Words only: phrase keys must not leak into the token-level known set —
    # their kanji stems would contaminate stem-matching (気を付ける → stem 気).
    # Phrases travel separately as {headword: status} for KnownSet's unit pass.
    ledger_known = {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE status = 'known' AND kind = 'word'")}
    learning = {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE status = 'learning' AND kind = 'word'")}
    phrases = dict(conn.execute(
        "SELECT lemma, status FROM lemmas WHERE kind = 'phrase'").fetchall())

    known = set(ledger_known)
    norm_known = set()

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

    stems = {s for s in (extract_kanji_stem(x) for x in known) if s}

    return {
        "known": known,
        "learning": learning - known,
        "norm_known": norm_known,
        "known_stems": stems,
        "phrases": phrases,
        "sources": {"ledger": len(ledger_known), "union": len(known)},
    }


def import_anki(conn, cfg):
    """One-shot migration off Anki (LIVE_REVIEW.md §7 step 1): scan the live
    collection via AnkiConnect one last time and write every lemma whose
    highest card interval meets the threshold as 'import' evidence (origin
    anki_final), then promote. Idempotent via import_known's skip of lemmas
    that already carry an import row. Not part of any pipeline step — run by
    hand, with Anki open."""
    from ledger.anki_known import compute_anki_known
    known, _norm, _stems = compute_anki_known(cfg, force_refresh=True)
    result = import_known(conn, sorted(known), origin="anki_final")
    result["anki_known"] = len(known)
    result["promote"] = promote(conn)
    return result


def active_interest(conn, known=()):
    """The standing "words I want to learn" set (tap_interest), minus lemmas
    the user now knows. Persists across episodes and *through* card minting —
    a wanted word keeps being highlighted and prioritized until it's known
    (the user's rule). If its card was later deleted (cards.deleted_at, set by
    poll_lapses), the re-mine guard in tools.coverage reopens it, so selection
    will mint a fresh one.

    Retirement reads the ledger `lemmas` projection (status='known') — cheap,
    no AnkiConnect, so this stays usable in hot read paths (GET /transcript).
    `known` optionally adds extra known lemmas the caller already has in hand.

    Graduation (LIVE_REVIEW.md §3): a wanted word whose exposures cleared θ
    is a confirm candidate — it has moved on to the "think you know" list
    and leaves this one. A "not yet" answer clears the candidate flag, and
    the word is back here."""
    interested = {r[0] for r in conn.execute(
        "SELECT DISTINCT lemma FROM evidence WHERE source = 'tap_interest'")}
    graduated = {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE status = 'known' OR confirm_candidate = 1")}
    return interested - graduated - set(known)


# Frequency-table rows that aren't vocabulary: laughter / filler transcribed
# as tokens (ハハハ, フフッ, う~ん) and truncated fragments (ちょっ). Kept in
# the corpus ranks — they are genuinely frequent — but never worth painting.
_NOT_VOCAB_RE = re.compile(r"[~〜]|[っッ]$")
_NOT_VOCAB_POS = frozenset(("感動詞",))


def should_know(conn, n=100):
    """The "you should know this" list (LIVE_REVIEW.md §3): the n most
    frequent corpus lemmas (show-penetration ranks, not the Leeds fallback)
    that are not known, not already awaiting a confirm (blue), and not
    starred (★ is the user's own list and wins). A rolling window — as
    words graduate by ✓ / ★ / promotion the next ranks slide in. Ordered by
    rank so callers can keep the order."""
    interest = active_interest(conn)
    rows = conn.execute(
        """SELECT f.lemma, l.pos FROM freq f
           LEFT JOIN lemmas l ON l.lemma = f.lemma
           WHERE f.source = 'show_graph'
             AND COALESCE(l.status, 'unknown') != 'known'
             AND COALESCE(l.confirm_candidate, 0) = 0
           ORDER BY f.rank LIMIT ?""", (max(n * 4, 50),)).fetchall()
    out = []
    for lemma, pos in rows:
        if lemma in interest or pos in _NOT_VOCAB_POS or _NOT_VOCAB_RE.search(lemma):
            continue
        out.append(lemma)
        if len(out) >= n:
            break
    return out


def marked_unknown(conn, kind="word"):
    """Items the user has explicitly ✗'d (tap_unknown) and not since re-claimed
    as known (status ≠ known — a later ✓ / confirm wins at promote). The paint
    endpoint ships this so the phone can *un*-know a token whose sidecar `k`
    flag froze back when coverage still believed the word was known: the
    live paint's `known` list is additive, so without this a ✗ would never
    reach the screen."""
    return {r[0] for r in conn.execute(
        """SELECT DISTINCT e.lemma FROM evidence e
           JOIN lemmas l ON l.lemma = e.lemma AND l.kind = e.kind
           WHERE e.source = 'tap_unknown' AND e.kind = ? AND l.status != 'known'""",
        (kind,))}


def confirm_words(conn):
    """The word-kind confirmation queue as a bare lemma set — "we think you
    know this; do you?". Same rows as query_confirm_queue (confirm_candidate
    = 1) minus the readings/episodes context, cheap enough for hot read paths
    (GET /transcript, where it drives the player's think-you-know highlight)."""
    return {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE kind = 'word' AND confirm_candidate = 1")}


def phrase_statuses(conn, keys):
    """{headword: status} for the given phrase keys — untracked keys read as
    'unknown'. The transcript sidecar snapshots this per line so the player
    can paint a phrase span before the live paint state arrives."""
    keys = list(keys)
    if not keys:
        return {}
    rows = conn.execute(
        "SELECT lemma, status FROM lemmas WHERE kind = 'phrase' AND lemma IN (%s)"
        % ",".join("?" * len(keys)), keys).fetchall()
    out = {k: "unknown" for k in keys}
    out.update({r[0]: r[1] for r in rows})
    return out


def known_phrases(conn):
    """Known PHRASE keys (status = 'known') — the phrase half of the paint
    state's additive known list (GET /episodes/{id}/paint)."""
    return {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE kind = 'phrase' AND status = 'known'")}


def confirm_phrases(conn):
    """The phrase-kind confirmation queue as a bare headword set."""
    return {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE kind = 'phrase' AND confirm_candidate = 1")}


def known_words(conn):
    """The ledger's known WORDS as a bare lemma set (status = 'known' — every
    promoted tap/import/confirm). Ledger-only, no AnkiConnect, for hot read
    paths: GET /episodes/{id}/paint hands the app what has become known
    since an episode's coverage froze its `k` flags."""
    return {r[0] for r in conn.execute(
        "SELECT lemma FROM lemmas WHERE kind = 'word' AND status = 'known'")}


def confirm_grammar(conn):
    """The grammar-kind confirmation queue as a bare pattern set — the
    grammar_points analogue of confirm_words, for the player's line badge."""
    return {r[0] for r in conn.execute(
        "SELECT pattern FROM grammar_points WHERE confirm_candidate = 1")}


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


def _watched_titles(conn, keys):
    """(kind, key) → titles of the watched episodes the item was exposed in,
    oldest first — the "seen in …" context on a review card."""
    if not keys:
        return {}
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
    return titles


def _enrich_word_row(d, titles):
    """Wire shape shared by every word-review list (confirm queue, ★ list,
    should-know list). Furigana over kanji only: reading_segs peels okurigana
    off the lemma's dictionary reading (通す → 通[とお]す). Also normalize the
    flat reading to hiragana, matching what coverage emits on the wire. Works
    for phrases too — furigana() tokenizes the headword and rubies each kanji
    core."""
    from engine.lemma import furigana, kata_to_hira
    if isinstance(d.get("seen_by_mode"), str):
        d["seen_by_mode"] = json.loads(d["seen_by_mode"])
    if d.get("reading"):
        d["reading"] = kata_to_hira(d["reading"])
    d["reading_segs"] = furigana(d["lemma"])
    if not d.get("reading") and d["reading_segs"]:
        # a word the ledger never touched (should-know rows): flat reading
        # from the tokenizer's segs so the card still shows one
        d["reading"] = "".join(r or sfc for sfc, r in d["reading_segs"])
    d["episodes"] = titles.get((d.get("kind") or "word", d["lemma"]), [])
    return d


def query_word_list(conn, lemmas):
    """Review rows for an ordered list of word lemmas — the ★ high-interest
    set and the should-know window, presented the way the confirm queue is
    (LIVE_REVIEW.md §1: three lists, one card). Order is the caller's.
    Words the ledger has never touched (a should-know word not yet met in a
    watched episode) still get a row: corpus rank from `freq`, no reading,
    zero exposures."""
    lemmas = list(lemmas)
    if not lemmas:
        return []
    qmarks = ",".join("?" for _ in lemmas)
    by_lemma = {}
    for r in conn.execute(
            f"""SELECT lemma, reading, pos, freq_rank, exposure_count,
                       episode_spread, seen_active, seen_passive, lookups,
                       lookups_listed, confirm_score, seen_by_mode, last_seen
                FROM lemmas WHERE lemma IN ({qmarks})""", lemmas):
        by_lemma[r["lemma"]] = dict(r)
    ranks = {r[0]: r[1] for r in conn.execute(
        f"SELECT lemma, rank FROM freq WHERE lemma IN ({qmarks})", lemmas)}
    titles = _watched_titles(conn, [("word", l) for l in lemmas])
    out = []
    for lemma in lemmas:
        d = by_lemma.get(lemma) or {
            "lemma": lemma, "reading": None, "pos": None, "freq_rank": None,
            "exposure_count": 0, "episode_spread": 0, "seen_active": 0,
            "seen_passive": 0, "lookups": 0, "lookups_listed": 0, "confirm_score": None,
            "last_seen": None}
        if d["freq_rank"] is None:
            d["freq_rank"] = ranks.get(lemma)
        d["kind"] = "word"
        out.append(_enrich_word_row(d, titles))
    return out


def query_confirm_queue(conn):
    """Items the exposure heuristic flagged for confirmation (confirm_candidate
    = 1) — "we think you know this; do you?". One queue, three kinds: words
    and phrases from `lemmas` (common words first — most likely a quick yes),
    then grammar points from `grammar_points` (easiest JLPT tier first). Every
    row carries `kind` (the typed key) and the watched episodes it turned up
    in as context."""
    rows = conn.execute(
        """SELECT lemma, kind, reading, pos, freq_rank, exposure_count,
                  episode_spread, seen_active, seen_passive, lookups,
                  lookups_listed, confirm_score, seen_by_mode, last_seen
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
    titles = _watched_titles(conn, keys)
    out = [_enrich_word_row(dict(r), titles) for r in rows]
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


def query_calibration(conn, target=0.6):
    """Does the think-you-know bar fire when the learner actually knows the
    word? Re-runnable report over the ledger's own answers (words only;
    bulk-imported lemmas excluded — no exposure trail).

    Labels: the exposure prompt's answers (confirm_known = yes,
    confirm_defer = not yet) are the calibration set — each is a direct
    "do you know this?" at a recorded exposure state. ✓/✗ taps are listed
    apart: a ✓ is near-certain by construction (you tap what you know), so
    its *timing* — how many watched episodes the word had appeared in — is
    the useful number, not its yes-rate.

    For every answer the state at that moment: watched episodes containing
    the word (`eps`), exposures clearing the current qualifying bar (`q`),
    and occurrences seen in those episodes (`occ`; rows without a count read
    as 1, so this only means something once backfill-occurrences / fresh
    Stage-1 runs have stamped them — `occ_known` says how many rows had a
    real count). Yes-rates are bucketed per freq band; `suggested_theta` is
    the smallest q bucket (n ≥ 10) whose yes-rate reaches `target`, or null
    when no bucket does — which is the 2026-09-07 finding (the count doesn't
    separate yes from not-yet in the 2–8 range; see
    WORD_QUALIFYING_MAX_OTHER_UNKNOWN)."""
    watched = {r[0] for r in conn.execute("SELECT id FROM episodes WHERE watched = 1")}
    freq = dict(conn.execute("SELECT lemma, rank FROM freq").fetchall())
    by_lemma = {}
    for r in conn.execute(
            "SELECT lemma, source, episode_id, context, ts FROM evidence "
            "WHERE kind = 'word' ORDER BY ts, id"):
        by_lemma.setdefault(r["lemma"], []).append(r)

    def band(rank):
        if rank is None:
            return "rare"
        for max_rank, _t, _s in THETA_TABLE:
            if max_rank is None:
                return f"{THETA_TABLE[-2][0]}+"
            if rank < max_rank:
                return f"<{max_rank}"

    def bucket(n):
        return str(n) if n < 8 else "8+"

    answers, taps = [], []
    occ_known = occ_rows = 0
    lookups_before_known = {}   # band → bucket(lookups before the first ✓/yes) → words
    lookups_by_list = {}        # list → popup opens while painted as that list
    for lemma, evs in by_lemma.items():
        if any(e["source"] == "import" for e in evs):
            continue
        b = band(freq.get(lemma))
        seen = []
        looked = 0
        known_at = None
        for e in evs:
            if e["source"] == "exposure":
                seen.append(e)
                continue
            if e["source"] == "lookup":
                ctx = _ctx(e)
                for k, v in (ctx.get("lists") or {}).items():
                    lookups_by_list[k] = lookups_by_list.get(k, 0) + int(v)
                if known_at is None:
                    looked += int(ctx.get("n", 0))
                continue
            if e["source"] not in ("confirm_known", "confirm_defer", "tap_known", "tap_unknown"):
                continue
            if e["source"] in ("confirm_known", "tap_known") and known_at is None:
                known_at = e["ts"]
                d = lookups_before_known.setdefault(b, {})
                d[bucket(looked)] = d.get(bucket(looked), 0) + 1
            ctxs = [_ctx(x) for x in seen if x["episode_id"] in watched]
            occ_rows += len(ctxs)
            occ_known += sum("occ" in c for c in ctxs)
            krs = [c["known_ratio"] for c in ctxs
                   if isinstance(c.get("known_ratio"), (int, float))]
            state = {"band": b, "eps": len(ctxs),
                     "q": sum(_exposure_qualifies(c) for c in ctxs),
                     "occ": sum(c.get("occ", 1) for c in ctxs),
                     "kr": round(sum(krs) / len(krs), 1) if krs else None,
                     "yes": e["source"] in ("confirm_known", "tap_known")}
            (answers if e["source"].startswith("confirm") else taps).append(state)

    def rates(rows, key):
        g = {}
        for r in rows:
            k = (r["band"], bucket(r[key]) if key != "kr" else str(r[key]))
            y, n = g.get(k, (0, 0))
            g[k] = (y + r["yes"], n + 1)
        out = {}
        for (b, k), (y, n) in sorted(g.items()):
            out.setdefault(b, {})[k] = {"yes": y, "n": n, "rate": round(y / n, 2)}
        return out

    by_q = rates(answers, "q")
    suggested = {}
    for b, buckets in by_q.items():
        pick = None
        for k, v in buckets.items():
            if k != "8+" and v["n"] >= 10 and v["rate"] >= target:
                pick = int(k)
                break
        suggested[b] = pick
    tap_eps = {}
    for t in taps:
        if t["yes"]:
            k = (t["band"], bucket(t["eps"]))
            tap_eps[k] = tap_eps.get(k, 0) + 1
    tap_timing = {}
    for (b, k), n in sorted(tap_eps.items()):
        tap_timing.setdefault(b, {})[k] = n
    return {
        "target_yes_rate": target,
        "qualifying_bar": {"word_max_other_unknown": WORD_QUALIFYING_MAX_OTHER_UNKNOWN,
                           "theta_table": THETA_TABLE,
                           "min_known_ratio": CONFIRM_MIN_KNOWN_RATIO,
                           "min_known_ratio_verb": CONFIRM_MIN_KNOWN_RATIO_VERB},
        "confirm_answers": len(answers),
        "confirm_yes_rate": round(sum(a["yes"] for a in answers) / len(answers), 2)
                            if answers else None,
        "yes_rate_by_qualifying": by_q,
        "yes_rate_by_episodes": rates(answers, "eps"),
        "yes_rate_by_occurrences": rates(answers, "occ"),
        # mean known_ratio of the sentences the word was met in, to 0.1 —
        # the precision gate's own axis (CONFIRM_MIN_KNOWN_RATIO)
        "yes_rate_by_known_ratio": rates(answers, "kr"),
        "occurrence_coverage": {"rows": occ_rows, "with_count": occ_known},
        "suggested_theta": suggested,
        "tap_known_by_episodes_seen": tap_timing,
        "tap_unknown": sum(1 for t in taps if not t["yes"]),
        # popup opens (no mark) before the word's first ✓ / confirm-yes, per
        # band; and how the opens split by what the word was painted as.
        "lookups_before_known": {b: dict(sorted(d.items())) for b, d in
                                 sorted(lookups_before_known.items())},
        "lookups_by_list": dict(sorted(lookups_by_list.items())),
        "model": _model_report(conn),
    }


def _model_report(conn):
    """The adaptive scorer as the calibration report shows it: training-set
    size, out-of-fold metrics, the cutoff in force (None = hand gate), and
    the standardized weights — each is "one standard deviation more of this
    feature moves the log-odds by …", so sign and size read directly."""
    model = load_confirm_model(conn)
    n = len(training_rows(conn))
    if model is None:
        return {"active": False, "training_rows": n, "needed": MODEL_MIN_ROWS}
    return {
        "active": model.get("cutoff") is not None,
        "training_rows": n, "fitted_on": model["n_rows"], "trained_at": model["trained_at"],
        "cutoff": model.get("cutoff"), "metrics": model["metrics"],
        "weights": dict(sorted(zip(model["features"], model["weights"]),
                               key=lambda kv: -abs(kv[1]))),
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
    review (highest-id rating event + the axis/tag/note rows sharing its
    review_id), plus the latest follow intent across all reviews.

    Censoring is PER-AXIS (SURVEY.md §2): when the review is too hard
    (difficulty ≥ DIFFICULTY_CENSOR, or the legacy over_my_head tag), the
    comprehension-DEPENDENT axes (topic_pull) and the overall taste label are
    invalidated — but comprehension-INDEPENDENT axes (presenter, audio_fidelity,
    speech_clarity) survive: you can love the act without following a word."""
    ratings = [r for r in rows if r["kind"] == "rating"]
    follow = next((r["value"] for r in reversed(rows) if r["kind"] == "follow"), None)
    if not ratings:
        if follow is None:
            return None
        return {"rating": None, "tags": [], "taste_valid": None,
                "adjusted_enjoyment": None, "axes": {}, "axis_valid": {},
                "difficulty": None, "note": None, "follow": follow}
    latest = ratings[-1]
    rid = latest["review_id"]
    if latest["value"] == "clear":
        return {"rating": None, "tags": [], "taste_valid": None,
                "adjusted_enjoyment": None, "axes": {}, "axis_valid": {},
                "difficulty": None, "note": None, "follow": follow}
    rating = int(latest["value"])
    batch = [r for r in rows if r["review_id"] == rid]
    tags = [r["value"] for r in batch if r["kind"] == "tag"]
    axes = {r["kind"]: int(r["value"]) for r in batch if r["kind"] in SURVEY_AXES}
    note = next((r["value"] for r in batch if r["kind"] == "note"), None)

    difficulty = axes.get("difficulty")
    # Too-hard censor: the graded difficulty axis OR the legacy over_my_head tag.
    censored = (difficulty is not None and difficulty >= DIFFICULTY_CENSOR) \
        or _DIFFICULTY_TAG in tags
    axis_valid = {a: not (censored and SURVEY_AXES[a]["comprehension_dependent"])
                  for a in axes if a != "difficulty"}
    # The overall star is a comprehension-dependent content proxy → censored too.
    taste_valid = not censored
    return {"rating": rating, "tags": tags, "taste_valid": taste_valid,
            "adjusted_enjoyment": rating if taste_valid else None,
            "axes": axes, "axis_valid": axis_valid, "difficulty": difficulty,
            "note": note, "follow": follow}


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
    sub.add_parser("materialize-known", help="the ledger's promoted known set")
    sub.add_parser("import-anki", help="one-shot: fold the live Anki known-set into "
                                       "the ledger as import evidence (needs Anki up)")
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
    p = sub.add_parser("non-vocab-remove",
                       help="un-register an over-flagged non-vocab key")
    p.add_argument("key")
    p = sub.add_parser("phrase-add",
                       help="deliberately track a non-JMdict phrase (reviewed path)")
    p.add_argument("canonical")
    p.add_argument("--reading")
    p = sub.add_parser("rate", help="post-watch survey: overall star + axes + tags + follow")
    p.add_argument("episode_id")
    p.add_argument("rating", help="1-5, or 'clear' to unrate")
    p.add_argument("--tag", action="append", default=[], choices=sorted(RATING_TAGS),
                   metavar="TAG", help="taste chip (repeatable): "
                   "already_knew|over_my_head|didnt_grab|format_miss|"
                   "fascinating|loved_format")
    for axis in sorted(SURVEY_AXES):
        p.add_argument(f"--{axis.replace('_', '-')}", dest=axis, type=int,
                       choices=[1, 2, 3, 4, 5], metavar="1-5",
                       help=f"{axis} survey axis (1-5)")
    p.add_argument("--follow", choices=FOLLOW_STATES,
                   help="channel intent (decoupled from this video's score)")
    p.add_argument("--note", help="free-text reaction; the judge parses it")
    p = sub.add_parser("set-follow", help="set a channel's follow intent directly")
    p.add_argument("channel_id")
    p.add_argument("state", choices=FOLLOW_STATES)
    p.add_argument("--channel", help="display name")
    p = sub.add_parser("presenter-get", help="print a channel's presenter fingerprint (JSON)")
    p.add_argument("channel_id")
    p = sub.add_parser("presenter-set",
                       help="store a channel's merged presenter fingerprint (SURVEY.md §4c)")
    p.add_argument("channel_id")
    p.add_argument("profile_json", help="path to the merged profile JSON")
    p.add_argument("--channel", help="display name")
    p.add_argument("--episode", help="episode id this observation comes from "
                   "(recorded in provenance.episodes; lets concurrent writers merge)")
    p = sub.add_parser("record-curation",
                       help="persist /immerse curation metadata (genre/format/topics/difficulty)")
    p.add_argument("episode_id")
    p.add_argument("curate_json", help="path to the episode's curate.json")
    p = sub.add_parser("record-view-session",
                       help="store one phone-recorded playback session (JSON file)")
    p.add_argument("session_json")
    sub.add_parser("backfill-snapshots",
                   help="stamp claim snapshots onto historical ✓/✗/yes/not-yet rows")
    p = sub.add_parser("fit-confirm-model",
                       help="fit the adaptive think-you-know scorer on the claim snapshots")
    p.add_argument("--target", type=float, default=0.8,
                   help="precision the cutoff must reach out-of-fold (default 0.8)")
    p = sub.add_parser("backfill-occurrences",
                       help="stamp per-episode occurrence counts onto exposure rows "
                            "from the coverage.json files still on disk")
    p.add_argument("--episodes", help="episodes root (default: <work_dir>/episodes)")
    p = sub.add_parser("query", help="read the ledger")
    p.add_argument("what", choices=["summary", "needs-review", "confirm-queue",
                                    "why", "unwatched", "ratings", "channels",
                                    "grammar-proposed", "non-vocab", "viewtime",
                                    "calibration"])
    p.add_argument("lemma", nargs="?")
    p.add_argument("--target", type=float, default=0.6,
                   help="calibration: yes-rate a θ bucket must reach (default 0.6)")

    args = ap.parse_args(argv)
    cfg = load_config(args.config, required=args.verb == "import-anki" or not args.db)
    db_path = args.db or cfg["ledger_db"]
    conn = open_db(db_path)

    if args.verb == "init":
        _json_out({"db": db_path, "initialized": True})
    elif args.verb == "materialize-known":
        b = materialize_known(conn)
        _json_out({"known": len(b["known"]), "learning": len(b["learning"]),
                   "norm_variants": len(b["norm_known"]),
                   "kanji_stems": len(b["known_stems"]),
                   "phrases": len(b["phrases"]), "sources": b["sources"]})
    elif args.verb == "import-anki":
        _json_out(import_anki(conn, cfg))
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
    elif args.verb == "backfill-snapshots":
        _json_out(backfill_snapshots(conn))
    elif args.verb == "fit-confirm-model":
        result = fit_confirm_model(conn, target=args.target)
        result["promote"] = promote(conn)
        _json_out(result)
    elif args.verb == "backfill-occurrences":
        root = args.episodes or ((cfg or {}).get("work_dir") and
                                 str(Path(cfg["work_dir"]) / "episodes"))
        if not root:
            ap.error("backfill-occurrences needs --episodes or a config with work_dir")
        result = backfill_occurrences(conn, root)
        result["promote"] = promote(conn)
        _json_out(result)
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
    elif args.verb == "non-vocab-remove":
        _json_out(remove_non_vocab(conn, args.key))
    elif args.verb == "rate":
        rating = None if args.rating == "clear" else int(args.rating)
        axes = {a: getattr(args, a) for a in SURVEY_AXES
                if getattr(args, a) is not None}
        _json_out(record_rating(conn, args.episode_id, rating, args.tag,
                                axes=axes, follow=args.follow, note=args.note))
    elif args.verb == "set-follow":
        _json_out(set_follow(conn, args.channel_id, args.channel, args.state))
    elif args.verb == "presenter-get":
        _json_out(get_presenter_profile(conn, args.channel_id))
    elif args.verb == "presenter-set":
        profile = json.loads(Path(args.profile_json).read_text(encoding="utf-8"))
        _json_out(set_presenter_profile(conn, args.channel_id, args.channel, profile,
                                        episode_id=args.episode))
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
    elif args.verb == "record-view-session":
        payload = json.loads(Path(args.session_json).read_text(encoding="utf-8"))
        _json_out(record_view_session(conn, payload))
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
        elif args.what == "channels":
            _json_out([{**dict(r),
                        "profile": json.loads(r["profile"]) if r["profile"] else None}
                       for r in conn.execute(
                           "SELECT channel_id, channel, follow_state, profile, updated_at "
                           "FROM channels ORDER BY updated_at DESC")])
        elif args.what == "grammar-proposed":
            _json_out([dict(r) for r in conn.execute(
                "SELECT * FROM grammar_proposed ORDER BY seen DESC, first_seen")])
        elif args.what == "non-vocab":
            _json_out([dict(r) for r in conn.execute(
                "SELECT key, kind, note, origin, ts FROM non_vocab "
                "ORDER BY ts DESC, key")])
        elif args.what == "viewtime":
            _json_out(query_view_totals(conn))
        elif args.what == "calibration":
            _json_out(query_calibration(conn, target=args.target))


if __name__ == "__main__":
    main()
