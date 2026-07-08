"""Ledger state-machine tests: schema, idempotency, watched-gate, promote rules."""

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import ledgerctl as lc


def _exposure_payload(episode_id, lemmas, other_unknown=0):
    return (
        {"id": episode_id, "title": f"ep {episode_id}", "source": "test", "kind": "local"},
        {lem: {"sentence_idx": 0, "known_ratio": 0.9,
               "other_unknown_count": other_unknown} for lem in lemmas},
    )


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.conn = lc.open_db(":memory:")

    def _expose_watched(self, lemma, n_episodes, other_unknown=0, start_idx=0):
        for i in range(start_idx, start_idx + n_episodes):
            ep, exp = _exposure_payload(f"ep{i}", [lemma], other_unknown)
            lc.record_exposure(self.conn, ep, exp)
            lc.mark_watched(self.conn, f"ep{i}")

    def test_schema_tables(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"lemmas", "evidence", "episodes", "cards", "freq",
                         "tap_batches"} <= tables)

    def test_exposure_idempotent(self):
        ep, exp = _exposure_payload("e1", ["犬"])
        r1 = lc.record_exposure(self.conn, ep, exp)
        r2 = lc.record_exposure(self.conn, ep, exp)
        self.assertEqual(r1["new_rows"], 1)
        self.assertEqual(r2["new_rows"], 0)  # P4: re-run is a no-op
        count = self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        self.assertEqual(count, 1)

    def test_watched_gate(self):
        # 6 unwatched episodes: exposures stay inert, lemma stays unknown.
        for i in range(6):
            ep, exp = _exposure_payload(f"e{i}", ["猫"])
            lc.record_exposure(self.conn, ep, exp)
        lc.promote(self.conn)
        status = self.conn.execute(
            "SELECT status, exposure_count FROM lemmas WHERE lemma='猫'").fetchone()
        self.assertEqual(status["status"], "unknown")
        self.assertEqual(status["exposure_count"], 0)

        # Watch them: rare-tier θ=6/spread 4 is met → surfaced for confirmation
        # (NOT auto-known — a fuzzy count can't assert knowledge).
        for i in range(6):
            lc.mark_watched(self.conn, f"e{i}")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, exposure_count, episode_spread, confirm_candidate "
            "FROM lemmas WHERE lemma='猫'").fetchone()
        self.assertEqual(row["status"], "learning")
        self.assertEqual(row["confirm_candidate"], 1)
        self.assertEqual(row["exposure_count"], 6)
        self.assertEqual(row["episode_spread"], 6)

        # Confirming it ("yes, I know it") promotes to known and clears the flag.
        lc.confirm_known_lemma(self.conn, "猫")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, confirm_candidate FROM lemmas WHERE lemma='猫'").fetchone()
        self.assertEqual(row["status"], "known")
        self.assertEqual(row["confirm_candidate"], 0)

    def test_rating_set_clear_and_query(self):
        ep, exp = _exposure_payload("er", ["犬"])
        lc.record_exposure(self.conn, ep, exp)
        self.assertEqual(lc.set_rating(self.conn, "er", 4)["rating"], 4)
        rated = lc.query_ratings(self.conn)
        self.assertEqual([(r["id"], r["rating"]) for r in rated], [("er", 4)])
        self.assertIsNotNone(rated[0]["rated_at"])
        lc.set_rating(self.conn, "er", 2)  # re-rate overwrites
        self.assertEqual(lc.query_ratings(self.conn)[0]["rating"], 2)
        lc.set_rating(self.conn, "er", None)  # clear
        self.assertEqual(lc.query_ratings(self.conn), [])

    def test_rating_validation(self):
        ep, exp = _exposure_payload("er", ["犬"])
        lc.record_exposure(self.conn, ep, exp)
        for bad in (0, 6, 3.5, "4", True):
            with self.assertRaises(ValueError):
                lc.set_rating(self.conn, "er", bad)
        with self.assertRaises(KeyError):
            lc.set_rating(self.conn, "nope", 3)

    def test_rating_migration_adds_columns(self):
        # A pre-rating DB (episodes without the columns) must be healed by open_db.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "old.db")
            raw = sqlite3.connect(db)
            raw.execute("""CREATE TABLE episodes (
                id TEXT PRIMARY KEY, title TEXT, source TEXT, kind TEXT,
                watched INTEGER DEFAULT 0, processed_at TEXT)""")
            raw.execute("INSERT INTO episodes (id) VALUES ('old_ep')")
            raw.commit()
            raw.close()
            conn = lc.open_db(db)
            lc.set_rating(conn, "old_ep", 5)
            self.assertEqual(lc.query_ratings(conn)[0]["rating"], 5)
            conn.close()

    def test_theta_scaled_by_freq_rank(self):
        # Top-2k lemma needs only 2 exposures / 2 episodes.
        self.conn.execute(
            "INSERT INTO freq (lemma, rank, penetration, source) VALUES ('食べる', 100, 5000, 'show_graph')")
        self._expose_watched("食べる", 2)
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, freq_rank, confirm_candidate FROM lemmas WHERE lemma='食べる'").fetchone()
        # crosses the top-2k bar → confirmation candidate, not auto-known
        self.assertEqual(row["status"], "learning")
        self.assertEqual(row["confirm_candidate"], 1)
        self.assertEqual(row["freq_rank"], 100)

        # A rare lemma with the same 2 watched exposures stays unknown.
        self._expose_watched("薔薇", 2)
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='薔薇'").fetchone()["status"], "unknown")

    def test_confirm_queue_and_defer_snooze(self):
        # rare-tier θ=6/spread 4: six watched exposures surface a candidate
        self._expose_watched("蝶", 6)
        lc.promote(self.conn)
        self.conn.execute("UPDATE lemmas SET reading='チョウ' WHERE lemma='蝶'")
        queue = lc.query_confirm_queue(self.conn)
        self.assertEqual([c["lemma"] for c in queue], ["蝶"])
        self.assertEqual(queue[0]["reading"], "ちょう")  # furigana normalized to hiragana
        self.assertIn("episodes", queue[0])  # carries watched-episode context

        # "not yet" snoozes it: no longer a candidate, still learning
        lc.defer_known_lemma(self.conn, "蝶")
        lc.promote(self.conn)
        self.assertEqual(lc.query_confirm_queue(self.conn), [])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='蝶'").fetchone()["status"], "learning")

        # a fresh qualifying exposure AFTER the defer re-surfaces it
        time.sleep(1.1)  # ts resolution is seconds — land the exposure past the defer
        self._expose_watched("蝶", 1, start_idx=6)
        lc.promote(self.conn)
        self.assertEqual([c["lemma"] for c in lc.query_confirm_queue(self.conn)], ["蝶"])

        # confirming clears it from the queue and marks known
        lc.confirm_known_lemma(self.conn, "蝶")
        lc.promote(self.conn)
        self.assertEqual(lc.query_confirm_queue(self.conn), [])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='蝶'").fetchone()["status"], "known")

    def test_exposure_comprehension_bar(self):
        # Q1: exposures with other unknowns in the sentence don't qualify.
        self._expose_watched("走る", 6, other_unknown=2)
        lc.promote(self.conn)
        row = self.conn.execute("SELECT status, exposure_count FROM lemmas WHERE lemma='走る'").fetchone()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["exposure_count"], 6)  # activated, just not qualifying

    def test_tap_known_promotes(self):
        lc.apply_taps(self.conn, {"episode_id": None, "batch_id": "b1",
                                  "taps": [["諦める", "k"]]})
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='諦める'").fetchone()["status"], "known")

    def test_tap_unknown_demotes_and_flags_conflict(self):
        lc.apply_taps(self.conn, {"episode_id": None, "batch_id": "b1",
                                  "taps": [["諦める", "k"]]})
        time.sleep(1.1)  # ts resolution is seconds; make the demotion strictly fresher
        lc.apply_taps(self.conn, {"episode_id": None, "batch_id": "b2",
                                  "taps": [["諦める", "u"]]})
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, needs_review FROM lemmas WHERE lemma='諦める'").fetchone()
        self.assertEqual(row["status"], "learning")  # rule 1 cancels known
        self.assertEqual(row["needs_review"], 1)     # strong positive also exists

    def test_tap_unknown_on_anki_known_flags_replace(self):
        # Q2: demotion of a live-Anki-known word is a union no-op — the tap
        # means the card isn't working → needs_review.
        lc.apply_taps(self.conn, {"episode_id": None, "batch_id": "b1",
                                  "taps": [["時計", "u"]]})
        lc.promote(self.conn, anki_known={"時計"})
        row = self.conn.execute(
            "SELECT status, needs_review FROM lemmas WHERE lemma='時計'").fetchone()
        self.assertEqual(row["needs_review"], 1)

    def test_import_known_promotes(self):
        r = lc.import_known(self.conn, ["諦める", "頑丈", "  "], origin="ankimorphs")
        self.assertEqual(r["imported"], 2)  # blank line dropped
        lc.promote(self.conn)
        for lemma in ("諦める", "頑丈"):
            self.assertEqual(self.conn.execute(
                "SELECT status FROM lemmas WHERE lemma=?", (lemma,)
            ).fetchone()["status"], "known")

    def test_import_idempotent(self):
        lc.import_known(self.conn, ["諦める"])
        r = lc.import_known(self.conn, ["諦める", "頑丈"])
        self.assertEqual(r["imported"], 1)
        self.assertEqual(r["already_imported"], 1)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE source='import'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_tap_unknown_demotes_import_quietly(self):
        # Bulk lists are noisy: the user's correction wins with no conflict flag.
        lc.import_known(self.conn, ["諦める"])
        lc.apply_taps(self.conn, {"episode_id": None, "batch_id": "b1",
                                  "taps": [["諦める", "u"]]})
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, needs_review FROM lemmas WHERE lemma='諦める'").fetchone()
        self.assertEqual(row["status"], "learning")   # tie goes to the negative
        self.assertEqual(row["needs_review"], 0)      # import ≠ deliberate tap

    def test_materialize_bridges_external_lemma_forms(self):
        # MeCab-style kana lemmas from an import must join Sudachi tokens
        # via normalized_form (くる→来る, ところ→所).
        import tempfile
        lc.import_known(self.conn, ["くる", "ところ"])
        lc.promote(self.conn)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"work_dir": tmp,
                   "known_words": {"sources": [], "cache_hours": 0}}
            bundle = lc.materialize_known(self.conn, cfg)
        self.assertIn("来る", bundle["norm_known"])
        self.assertIn("所", bundle["norm_known"])
        from engine.lemma import KnownSet, tokenize
        ks = KnownSet(bundle["known"], bundle["norm_known"], bundle["known_stems"])
        kita = next(t for t in tokenize("公園に来た。") if t.surface == "来")
        self.assertIn(kita, ks)

    def test_mined_card_is_learning(self):
        lc.record_mined_cards(self.conn, "e1", [
            {"lemma": "設計", "sentence": "この設計は美しい。", "anki_guid": "g1",
             "anki_note_id": 111}])
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='設計'").fetchone()["status"], "learning")

    def test_tap_batch_idempotent(self):
        payload = {"episode_id": None, "batch_id": "batch-x", "taps": [["犬", "k"]]}
        r1 = lc.apply_taps(self.conn, payload)
        r2 = lc.apply_taps(self.conn, payload)
        self.assertEqual(r1["applied"], 1)
        self.assertTrue(r2["duplicate"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM evidence").fetchone()[0], 1)

    def test_apply_taps_implies_mark_watched(self):
        ep, exp = _exposure_payload("epw", ["犬"])
        lc.record_exposure(self.conn, ep, exp)
        r = lc.apply_taps(self.conn, {"episode_id": "epw", "batch_id": "b9",
                                      "taps": [["犬", "k"]]})
        self.assertTrue(r["marked_watched"])  # P5 implicit path
        self.assertEqual(self.conn.execute(
            "SELECT watched FROM episodes WHERE id='epw'").fetchone()[0], 1)

    def test_lapse_poll(self):
        lc.record_mined_cards(self.conn, "e1", [
            {"lemma": "設計", "sentence": "s", "anki_guid": "g1", "anki_note_id": 42}])

        def fake_anki(action, **params):
            if action == "findCards":
                return [4242]
            if action == "cardsInfo":
                return [{"cardId": 4242, "lapses": 3}]
            raise AssertionError(action)

        r1 = lc.poll_lapses(self.conn, fake_anki)
        self.assertEqual(r1["new_lapses"], 1)
        # Second poll with the same count: no new evidence (P6 delta semantics).
        r2 = lc.poll_lapses(self.conn, fake_anki)
        self.assertEqual(r2["new_lapses"], 0)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE source='card_lapse'").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_interest_persists_until_known(self):
        # "h" writes durable tap_interest (not knowledge); it stays active
        # across episodes and is retired only when the lemma becomes known.
        lc.apply_taps(self.conn, {"episode_id": "e1", "batch_id": "bi",
                                  "taps": [["設計", "h"]]})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE source='tap_interest'").fetchone()[0], 1)
        # not a knowledge claim: promote leaves it unknown
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='設計'").fetchone()
        self.assertNotEqual(row["status"], "known")
        self.assertIn("設計", lc.active_interest(self.conn, known=()))
        # once known, it drops out of the active set
        self.assertNotIn("設計", lc.active_interest(self.conn, known={"設計"}))

    def test_deleted_card_reopens_for_remine(self):
        # A minted card the user later deletes in Anki: poll_lapses can't find
        # the note → stamps deleted_at, and the lemma leaves the live-card set.
        lc.record_mined_cards(self.conn, "e1", [
            {"lemma": "設計", "sentence": "s", "anki_guid": "g1", "anki_note_id": 99}])
        r = lc.poll_lapses(self.conn, lambda action, **p: [])  # findCards → gone
        self.assertEqual(r["deleted"], 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT deleted_at FROM cards WHERE anki_note_id=99").fetchone()[0])
        # live-card guard (what coverage uses) no longer contains the lemma
        live = {row[0] for row in self.conn.execute(
            "SELECT lemma FROM cards WHERE deleted_at IS NULL")}
        self.assertNotIn("設計", live)

    def test_card_lapse_demotes(self):
        # Diagram: known → learning via card_lapse (fresh negative).
        self._expose_watched("勝負", 6)
        lc.confirm_known_lemma(self.conn, "勝負")  # exposures now only surface; confirm makes it known
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='勝負'").fetchone()["status"], "known")
        time.sleep(1.1)
        lc.record_mined_cards(self.conn, "ep0", [
            {"lemma": "勝負", "sentence": "s", "anki_guid": "g", "anki_note_id": 7}])
        time.sleep(1.1)

        def fake_anki(action, **params):
            return [700] if action == "findCards" else [{"lapses": 1}]

        lc.poll_lapses(self.conn, fake_anki)
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='勝負'").fetchone()["status"], "learning")

    def test_query_unwatched(self):
        ep, exp = _exposure_payload("eq", ["犬", "猫"])
        lc.record_exposure(self.conn, ep, exp)
        unwatched = lc.query_unwatched(self.conn)
        self.assertEqual(len(unwatched), 1)
        self.assertEqual(unwatched[0]["inert_exposures"], 2)
        lc.mark_watched(self.conn, "eq")
        self.assertEqual(lc.query_unwatched(self.conn), [])

    def test_query_why(self):
        ep, exp = _exposure_payload("e1", ["犬"])
        lc.record_exposure(self.conn, ep, exp)
        lc.promote(self.conn)
        why = lc.query_why(self.conn, "犬")
        self.assertEqual(why["lemma"]["status"], "unknown")
        self.assertEqual(len(why["evidence"]), 1)
        self.assertEqual(why["evidence"][0]["source"], "exposure")

    # --- taste metadata (DESIGN.md — Taste metadata) --------------------------

    def _episode(self, episode_id="er", lemma="犬"):
        ep, exp = _exposure_payload(episode_id, [lemma])
        lc.record_exposure(self.conn, ep, exp)

    def test_taste_events_table(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("taste_events", tables)

    def test_record_rating_with_tags(self):
        self._episode()
        r = lc.record_rating(self.conn, "er", 4, ["fascinating", "loved_format"])
        self.assertEqual(r["rating"], 4)
        self.assertEqual(r["tags"], ["fascinating", "loved_format"])
        v = lc.query_enjoyment(self.conn, "er")
        self.assertEqual(v["rating"], 4)
        self.assertEqual(set(v["tags"]), {"fascinating", "loved_format"})
        self.assertTrue(v["taste_valid"])
        self.assertEqual(v["adjusted_enjoyment"], 4)

    def test_over_my_head_censors_taste_label(self):
        # A difficulty-driven low star is not a taste-low: excluded from the
        # taste manifold (taste_valid=False, adjusted_enjoyment=None).
        self._episode()
        lc.record_rating(self.conn, "er", 2, ["over_my_head"])
        v = lc.query_enjoyment(self.conn, "er")
        self.assertEqual(v["rating"], 2)
        self.assertFalse(v["taste_valid"])
        self.assertIsNone(v["adjusted_enjoyment"])
        # Still a rated episode in the dataset, just flagged.
        rated = lc.query_ratings(self.conn)
        self.assertEqual(rated[0]["id"], "er")
        self.assertFalse(rated[0]["taste_valid"])

    def test_rerate_appends_batch_and_latest_wins(self):
        # Append-only: re-rating keeps history (drift), verdict takes the latest.
        self._episode()
        lc.record_rating(self.conn, "er", 4, ["fascinating"])
        lc.record_rating(self.conn, "er", 2, ["didnt_grab"])
        v = lc.query_enjoyment(self.conn, "er")
        self.assertEqual(v["rating"], 2)
        self.assertEqual(v["tags"], ["didnt_grab"])
        rating_rows = self.conn.execute(
            "SELECT COUNT(*) FROM taste_events WHERE episode_id='er' AND kind='rating'"
        ).fetchone()[0]
        self.assertEqual(rating_rows, 2)  # both batches preserved

    def test_clear_rating_appends_clear_event(self):
        self._episode()
        lc.record_rating(self.conn, "er", 3, ["fascinating"])
        lc.record_rating(self.conn, "er", None)  # clear
        v = lc.query_enjoyment(self.conn, "er")
        self.assertIsNone(v["rating"])
        self.assertEqual(lc.query_ratings(self.conn), [])
        # the 'clear' is itself a logged event, not an erasure
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM taste_events WHERE kind='rating' AND episode_id='er'"
        ).fetchone()[0], 2)

    def test_rating_client_review_id_dedupes_replay(self):
        # Offline outbox replay: the same client review_id must not append a
        # second review batch (flaky-connection re-flush safety).
        self._episode()
        r1 = lc.record_rating(self.conn, "er", 4, ["fascinating"],
                              review_id="client123")
        self.assertEqual(r1["review_id"], "client123")
        self.assertNotIn("duplicate", r1)
        r2 = lc.record_rating(self.conn, "er", 4, ["fascinating"],
                              review_id="client123")
        self.assertTrue(r2["duplicate"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM taste_events WHERE episode_id='er'"
        ).fetchone()[0], 2)  # one rating + one tag row, not four
        # a different review_id is a genuine re-rate and appends as before
        lc.record_rating(self.conn, "er", 2, review_id="client456")
        self.assertEqual(lc.query_enjoyment(self.conn, "er")["rating"], 2)

    def test_rating_rejects_unknown_tag(self):
        self._episode()
        with self.assertRaises(ValueError):
            lc.record_rating(self.conn, "er", 3, ["bogus_tag"])
        with self.assertRaises(KeyError):
            lc.record_rating(self.conn, "nope", 3, ["fascinating"])

    def test_update_episode_meta_columns_and_json_merge(self):
        lc.update_episode_meta(self.conn, "ep1",
                               columns={"channel": "Ch", "duration": 610.0,
                                        "not_a_column": "x"},
                               metadata={"view_count": 999, "tags": ["a"]})
        # a later writer merges into metadata without clobbering earlier keys
        lc.update_episode_meta(self.conn, "ep1",
                               columns={"coverage_pct": 0.82},
                               metadata={"topics": ["history"]})
        row = self.conn.execute(
            "SELECT channel, duration, coverage_pct, metadata FROM episodes "
            "WHERE id='ep1'").fetchone()
        self.assertEqual(row["channel"], "Ch")
        self.assertEqual(row["duration"], 610.0)
        self.assertEqual(row["coverage_pct"], 0.82)
        meta = json.loads(row["metadata"])
        self.assertEqual(meta, {"view_count": 999, "tags": ["a"],
                                "topics": ["history"]})
        # unknown column silently ignored (callers may pass a superset)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
        self.assertNotIn("not_a_column", cols)

    def test_record_exposure_persists_provenance(self):
        episode = {"id": "yt_x", "title": "t", "source": "u", "kind": "youtube",
                   "channel": "散歩", "channel_id": "UC1", "duration": 610.0,
                   "upload_date": "20250101", "view_count": 42,
                   "description": "d", "tags": ["walk", "asmr"]}
        lc.record_exposure(self.conn, episode,
                           {"犬": {"sentence_idx": 0, "known_ratio": 0.9,
                                   "other_unknown_count": 0}})
        row = self.conn.execute(
            "SELECT channel, channel_id, duration, upload_date, metadata "
            "FROM episodes WHERE id='yt_x'").fetchone()
        self.assertEqual(row["channel"], "散歩")
        self.assertEqual(row["duration"], 610.0)
        self.assertEqual(row["upload_date"], "20250101")
        meta = json.loads(row["metadata"])
        self.assertEqual(meta["view_count"], 42)
        self.assertEqual(meta["tags"], ["walk", "asmr"])
        self.assertEqual(meta["description"], "d")

    def test_record_curation(self):
        self._episode("epc")
        lc.record_curation(self.conn, "epc", {
            "synopsis": "ignored here", "genre": "explainer",
            "format": "ゆっくり解説", "topics": ["history", "folklore"],
            "difficulty_felt": 3})
        row = self.conn.execute(
            "SELECT genre, format, difficulty_felt, metadata FROM episodes "
            "WHERE id='epc'").fetchone()
        self.assertEqual(row["genre"], "explainer")
        self.assertEqual(row["format"], "ゆっくり解説")
        self.assertEqual(row["difficulty_felt"], 3)
        self.assertEqual(json.loads(row["metadata"])["topics"],
                         ["history", "folklore"])

    def test_purge_retains_rated_keeps_taste_events(self):
        self._episode("er")
        lc.record_rating(self.conn, "er", 4, ["fascinating"])
        res = lc.purge_episode(self.conn, "er")
        self.assertTrue(res["rating_retained"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE id='er'").fetchone()[0], 1)
        self.assertGreater(self.conn.execute(
            "SELECT COUNT(*) FROM taste_events WHERE episode_id='er'").fetchone()[0], 0)

    def test_purge_unrated_drops_taste_events(self):
        self._episode("eu", "猫")
        lc.record_rating(self.conn, "eu", 3)
        lc.record_rating(self.conn, "eu", None)  # cleared → no taste to keep
        res = lc.purge_episode(self.conn, "eu")
        self.assertFalse(res["rating_retained"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE id='eu'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM taste_events WHERE episode_id='eu'").fetchone()[0], 0)


# --- phrases & grammar (GRAMMAR.md — one ledger, three item kinds) -------------

MINI_JMDICT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE JMdict [
<!ENTITY exp "expressions (phrases, clauses, etc.)">
<!ENTITY n "noun (common) (futsuumeishi)">
]>
<JMdict>
<entry>
<ent_seq>1</ent_seq>
<k_ele><keb>気を付ける</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>きをつける</reb></r_ele>
<sense><pos>&exp;</pos><gloss>to be careful</gloss><gloss>to pay attention</gloss></sense>
</entry>
<entry>
<ent_seq>2</ent_seq>
<k_ele><keb>犬</keb></k_ele>
<r_ele><reb>いぬ</reb></r_ele>
<sense><pos>&n;</pos><gloss>dog</gloss></sense>
</entry>
</JMdict>
"""


class PhraseGrammarTest(unittest.TestCase):
    def setUp(self):
        import io
        import sqlite3
        from tools import jmdict
        self.conn = lc.open_db(":memory:")
        self.jconn = sqlite3.connect(":memory:")
        jmdict.build_db(self.jconn,
                        jmdict.parse_entries(io.BytesIO(MINI_JMDICT.encode())))

    def _stage1(self, episode_id):
        # In the real pipeline Stage 1 (record_exposure) creates the episode
        # row with a title before curate ever runs — mirror that.
        ep, exp = _exposure_payload(episode_id, ["公園"])
        lc.record_exposure(self.conn, ep, exp)

    def _curate_phrase(self, episode_id, classification="comprehensible",
                       canonical="気を付ける", jconn="default"):
        self._stage1(episode_id)
        curation = {"phrases": [{"sentence_idx": 0, "surface": "気を付けて",
                                 "canonical": canonical,
                                 "classification": classification}]}
        return lc.record_curate_items(
            self.conn, episode_id, curation,
            jmdict_conn=self.jconn if jconn == "default" else jconn)

    def _curate_grammar(self, episode_id, pattern="〜てしまう",
                        classification="comprehensible", **extra):
        self._stage1(episode_id)
        curation = {"grammar": [{"sentence_idx": 0, "pattern": pattern,
                                 "classification": classification, **extra}]}
        return lc.record_curate_items(self.conn, episode_id, curation)

    # --- phrases (Phase 1 acceptance) --------------------------------------

    def test_is_headword(self):
        from tools import jmdict
        self.assertTrue(jmdict.is_headword(self.jconn, "気を付ける"))
        self.assertTrue(jmdict.is_headword(self.jconn, "きをつける"))
        self.assertFalse(jmdict.is_headword(self.jconn, "存在しない語"))

    def test_phrase_validation_rules(self):
        # not a JMdict headword → rejected, never key-minted
        r = self._curate_phrase("p0", canonical="変な組み合わせ")
        self.assertEqual(r["phrases"]["recorded"], 0)
        self.assertEqual(r["phrases"]["rejected"][0]["reason"],
                         "not_a_jmdict_headword")
        # a single-token headword is a word, not a phrase (the canonical-form
        # rule: 犬 is a headword but tokenizes to one unit)
        r = self._curate_phrase("p0", canonical="犬")
        self.assertEqual(r["phrases"]["rejected"][0]["reason"], "single_token")
        # no jmdict.db → validation can't run → reject, don't guess
        r = self._curate_phrase("p0", jconn=None)
        self.assertEqual(r["phrases"]["rejected"][0]["reason"],
                         "jmdict_unavailable")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM lemmas WHERE kind='phrase'").fetchone()[0], 0)

    def test_phrase_recorded_with_jmdict_reading(self):
        r = self._curate_phrase("p1")
        self.assertEqual(r["phrases"]["recorded"], 1)
        row = self.conn.execute(
            "SELECT kind, reading, pos FROM lemmas WHERE lemma='気を付ける'").fetchone()
        self.assertEqual(row["kind"], "phrase")
        self.assertEqual(row["reading"], "きをつける")
        self.assertEqual(row["pos"], "expression")
        # idempotent re-run (P4, kind-aware index)
        r2 = self._curate_phrase("p1")
        self.assertEqual(r2["phrases"]["recorded"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE kind='phrase'").fetchone()[0], 1)

    def test_phrase_exposure_confirm_known_flow(self):
        # phrases carry no corpus rank → rare-word bar (θ=6, spread 4)
        for i in range(6):
            self._curate_phrase(f"pe{i}")
            lc.mark_watched(self.conn, f"pe{i}")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, confirm_candidate, exposure_count FROM lemmas "
            "WHERE lemma='気を付ける'").fetchone()
        self.assertEqual(row["status"], "learning")  # never auto-known
        self.assertEqual(row["confirm_candidate"], 1)
        self.assertEqual(row["exposure_count"], 6)

        queue = lc.query_confirm_queue(self.conn)
        ph = next(c for c in queue if c["kind"] == "phrase")
        self.assertEqual(ph["lemma"], "気を付ける")
        self.assertEqual(ph["reading"], "きをつける")
        self.assertTrue(ph["episodes"])  # watched-episode context

        lc.confirm_known_lemma(self.conn, "気を付ける", kind="phrase")
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='気を付ける'").fetchone()["status"],
            "known")
        # words-only headline unaffected; the phrase rides the sibling block
        summary = lc.query_summary(self.conn)
        self.assertEqual(summary["lemmas_by_status"].get("known", 0), 0)
        self.assertEqual(summary["phrases"]["by_status"]["known"], 1)

    def test_phrase_too_hard_exposures_do_not_qualify(self):
        for i in range(6):
            self._curate_phrase(f"ph{i}", classification="too_hard")
            lc.mark_watched(self.conn, f"ph{i}")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT confirm_candidate, exposure_count FROM lemmas "
            "WHERE lemma='気を付ける'").fetchone()
        self.assertEqual(row["confirm_candidate"], 0)  # activated, not qualifying
        self.assertEqual(row["exposure_count"], 6)

    def test_add_phrase_deliberate_and_purge_survival(self):
        lc.add_phrase(self.conn, "取り返しがつかない", reading="とりかえしがつかない")
        with self.assertRaises(ValueError):
            lc.add_phrase(self.conn, "犬")  # single token — never a phrase key
        # an unrelated purge must not sweep the deliberately tracked key
        ep, exp = _exposure_payload("px", ["猫"])
        lc.record_exposure(self.conn, ep, exp)
        lc.purge_episode(self.conn, "px")
        self.assertEqual(self.conn.execute(
            "SELECT kind FROM lemmas WHERE lemma='取り返しがつかない'"
        ).fetchone()["kind"], "phrase")

    # --- grammar (Phase 2 acceptance) ---------------------------------------

    SEED = [{"pattern": "〜てしまう", "level": 5, "gloss": "completion/regret"},
            {"pattern": "〜させられる", "level": 3, "gloss": "causative-passive"}]

    def test_real_taxonomy_seed_loads(self):
        # the once-authored inventory (ledger/grammar_taxonomy.json) is the
        # canonical key space — it must load clean and stay collision-free
        path = Path(__file__).resolve().parent.parent / "ledger" / "grammar_taxonomy.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(rows), 400)
        self.assertEqual(len({r["pattern"] for r in rows}), len(rows))
        self.assertTrue(all(r["level"] in (1, 2, 3, 4, 5) for r in rows))
        r = lc.seed_grammar_points(self.conn, rows)
        self.assertEqual(r["grammar_points"], len(rows))
        # every seeded row starts at the unknown baseline
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM grammar_points WHERE status != 'unknown'"
        ).fetchone()[0], 0)

    def test_grammar_seed_and_theta(self):
        r = lc.seed_grammar_points(self.conn, self.SEED)
        self.assertEqual(r["grammar_points"], 2)
        # re-seeding updates gloss/level but never promote's verdict columns
        self.conn.execute(
            "UPDATE grammar_points SET status='known' WHERE pattern='〜てしまう'")
        self.conn.commit()
        lc.seed_grammar_points(self.conn, [
            {"pattern": "〜てしまう", "level": 4, "gloss": "revised"}])
        row = self.conn.execute(
            "SELECT level, gloss, status FROM grammar_points "
            "WHERE pattern='〜てしまう'").fetchone()
        self.assertEqual((row["level"], row["gloss"], row["status"]),
                         (4, "revised", "known"))
        # θ ladder: easy tiers need little, N1 the most, unplaced = strictest
        self.assertEqual(lc.grammar_theta_for(5), (2, 2))
        self.assertEqual(lc.grammar_theta_for(1), (5, 4))
        self.assertEqual(lc.grammar_theta_for(None), lc.GRAMMAR_THETA[1])

    def test_grammar_exposure_confirm_flow(self):
        lc.seed_grammar_points(self.conn, self.SEED)
        # N5 tier: θ=2 exposures over 2 episodes — qualifying classification only
        for i in range(2):
            r = self._curate_grammar(f"g{i}",
                                     form_note="食べちゃった = 食べる+てしまう")
            self.assertEqual(r["grammar"]["recorded"], 1)
            lc.mark_watched(self.conn, f"g{i}")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, confirm_candidate FROM grammar_points "
            "WHERE pattern='〜てしまう'").fetchone()
        self.assertEqual(row["status"], "learning")  # never auto-known
        self.assertEqual(row["confirm_candidate"], 1)

        queue = lc.query_confirm_queue(self.conn)
        g = next(c for c in queue if c["kind"] == "grammar")
        self.assertEqual((g["pattern"], g["level"], g["gloss"]),
                         ("〜てしまう", 5, "completion/regret"))
        self.assertTrue(g["episodes"])

        # defer snoozes it out of the queue, still learning
        lc.defer_known_lemma(self.conn, "〜てしまう", kind="grammar")
        lc.promote(self.conn)
        self.assertEqual([c for c in lc.query_confirm_queue(self.conn)
                          if c["kind"] == "grammar"], [])
        # a fresh qualifying exposure after the defer re-surfaces it
        time.sleep(1.1)
        self._curate_grammar("g9")
        lc.mark_watched(self.conn, "g9")
        lc.promote(self.conn)
        self.assertTrue([c for c in lc.query_confirm_queue(self.conn)
                         if c["kind"] == "grammar"])
        # confirm → known in grammar_points
        lc.confirm_known_lemma(self.conn, "〜てしまう", kind="grammar")
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM grammar_points WHERE pattern='〜てしまう'"
        ).fetchone()["status"], "known")

    def test_grammar_too_hard_exposures_do_not_qualify(self):
        lc.seed_grammar_points(self.conn, self.SEED)
        for i in range(3):
            self._curate_grammar(f"gt{i}", classification="too_hard")
            lc.mark_watched(self.conn, f"gt{i}")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, confirm_candidate, exposure_count FROM grammar_points "
            "WHERE pattern='〜てしまう'").fetchone()
        self.assertEqual(row["confirm_candidate"], 0)
        self.assertEqual(row["exposure_count"], 3)

    def test_unrecognized_pattern_goes_to_proposed(self):
        lc.seed_grammar_points(self.conn, self.SEED)
        r = self._curate_grammar("gp0", pattern="〜てまう",
                                 example="やってまうで", gloss="Kansai てしまう")
        self.assertEqual(r["grammar"]["recorded"], 0)
        self.assertEqual(r["grammar"]["proposed"], ["〜てまう"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE kind='grammar'").fetchone()[0], 0)
        # re-sighting bumps `seen`, keeps the first example
        self._curate_grammar("gp1", pattern="〜てまう")
        row = self.conn.execute(
            "SELECT seen, example FROM grammar_proposed WHERE pattern='〜てまう'"
        ).fetchone()
        self.assertEqual(row["seen"], 2)
        self.assertEqual(row["example"], "やってまうで")

        # deliberate approval moves it into the taxonomy; recording then works
        lc.approve_grammar_proposal(self.conn, "〜てまう", level=3)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM grammar_proposed WHERE pattern='〜てまう'").fetchone())
        r = self._curate_grammar("gp2", pattern="〜てまう")
        self.assertEqual(r["grammar"]["recorded"], 1)
        with self.assertRaises(KeyError):
            lc.approve_grammar_proposal(self.conn, "〜ないやつ")

    def test_word_and_grammar_share_string_without_collision(self):
        # kind is part of the exposure key and the promote group: the same
        # string tracked as a word and a grammar pattern never merges.
        lc.seed_grammar_points(self.conn, [
            {"pattern": "ばかり", "level": 4, "gloss": "just/only"}])
        ep, exp = _exposure_payload("wc0", ["ばかり"])
        lc.record_exposure(self.conn, ep, exp)
        self._curate_grammar("wc0", pattern="ばかり")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE lemma='ばかり'").fetchone()[0], 2)
        lc.mark_watched(self.conn, "wc0")
        lc.promote(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT kind FROM lemmas WHERE lemma='ばかり'").fetchone()["kind"],
            "word")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM grammar_points WHERE pattern='ばかり'").fetchone())

    def test_summary_confirm_total_spans_kinds(self):
        lc.seed_grammar_points(self.conn, self.SEED)
        for i in range(2):
            self._curate_grammar(f"s{i}")
            self._curate_phrase(f"s{i}")
            lc.mark_watched(self.conn, f"s{i}")
        for i in range(2, 6):
            self._curate_phrase(f"s{i}")
            lc.mark_watched(self.conn, f"s{i}")
        lc.promote(self.conn)
        summary = lc.query_summary(self.conn)
        self.assertEqual(summary["phrases"]["confirm_candidates"], 1)
        self.assertEqual(summary["grammar"]["confirm_candidates"], 1)
        # the headline total spans all kinds (the Stage-1 word 公園 also
        # crossed its bar here — 6 watched qualifying exposures)
        word_cc = self.conn.execute(
            "SELECT COUNT(*) FROM lemmas WHERE confirm_candidate=1 "
            "AND kind='word'").fetchone()[0]
        self.assertEqual(summary["confirm_candidates"], word_cc + 2)


if __name__ == "__main__":
    unittest.main()
