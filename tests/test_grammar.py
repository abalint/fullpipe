"""Grammar as token-anchored units (GRAMMAR.md, 2026-09-08): the matcher,
Stage 1 detection + exposure, the ledger's grammar marks and lists, and the
transcript / paint wire shapes."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import grammar as G  # noqa: E402
from engine import lemma as L  # noqa: E402
from ledger import ledgerctl as lc  # noqa: E402

ROWS = [
    {"pattern": "〜て", "level": 5, "gloss": "te", "match": [[["て", "で"]]]},
    {"pattern": "〜ない", "level": 5, "gloss": "not", "match": [["ない"]]},
    {"pattern": "〜てしまう", "level": 4, "gloss": "end up",
     "match": [[["て", "で"], "しまう"], [["ちゃう", "じゃう"]]]},
    {"pattern": "〜なければならない", "level": 4, "gloss": "must",
     "match": [[{"l": "ない", "s": ["なけれ"]}, "ば", "なる", "ない"],
               [{"l": "ない", "s": "なきゃ"}]]},
    {"pattern": "〜な（禁止）", "level": 4, "gloss": "don't",
     "match": [[{"p": "動詞", "plain": True, "ctx": True}, {"l": "な", "p2": "終助詞"}]]},
    {"pattern": "〜させる", "level": 4, "gloss": "make do", "match": [[["せる", "させる"]]]},
    {"pattern": "〜させていただく", "level": 3, "gloss": "humbly do",
     "match": [[["せる", "させる"], "て", "いただく"]]},
    {"pattern": "〜そうだ（様態）", "level": 4, "gloss": "looks like",
     "match": [[{"l": "そう", "p": "形状詞", "p2": "助動詞語幹"}]]},
    {"pattern": "〜そうだ（伝聞）", "level": 4, "gloss": "I hear",
     "match": [[{"l": "そう", "p": "名詞", "p2": "助動詞語幹"}]]},
    {"pattern": "〜ようだ", "level": 4, "gloss": "seems", "match": []},
]


def units(text, rows=ROWS):
    toks = L.tokenize(text)
    idx = G.build_grammar_index(rows)
    return [(u.pattern, "".join(t.surface for t in toks[u.start:u.end]))
            for u in G.match_grammar_units(toks, idx)]


class MatcherTest(unittest.TestCase):
    def test_attachment_is_the_unit_not_the_head_word(self):
        # the verb is a word with its own paint; the unit is what's attached
        self.assertEqual(units("全部食べてしまった。"), [("〜てしまう", "てしまっ")])
        self.assertEqual(units("食べちゃった。"), [("〜てしまう", "ちゃっ")])

    def test_longest_match_wins_over_its_parts(self):
        self.assertEqual(units("行かなければならない。"),
                         [("〜なければならない", "なければならない")])
        self.assertEqual(units("させていただきます。"),
                         [("〜させていただく", "せていただき")])

    def test_specificity_breaks_same_length_ties(self):
        # なきゃ pinned by surface beats the bare-lemma 〜ない on the same token
        self.assertEqual(units("行かなきゃ。"), [("〜なければならない", "なきゃ")])
        self.assertEqual(units("行かない。"), [("〜ない", "ない")])

    def test_context_anchor_is_required_but_not_painted(self):
        self.assertEqual(units("乗るな。"), [("〜な（禁止）", "な")])
        self.assertEqual(units("綺麗だな。"), [])  # な after だ, not a verb

    def test_pos_splits_surface_collisions(self):
        self.assertEqual(units("雨が降りそうだ。"), [("〜そうだ（様態）", "そう")])
        self.assertEqual(units("雨が降るそうだ。"), [("〜そうだ（伝聞）", "そう")])

    def test_main_verb_use_is_not_the_pattern(self):
        self.assertEqual(units("本をしまう。"), [])

    def test_empty_match_is_curate_only(self):
        self.assertEqual(units("雨のようだ。"), [])
        self.assertNotIn("〜ようだ", {p for p, _ in units("雨のようだ。")})

    def test_dict_tokens_without_pos_still_match_lemma_specs(self):
        # the backfill / live pass may only have coverage tokens (s, l)
        toks = [{"s": "食べ", "l": "食べる"}, {"s": "て", "l": "て"}, {"s": "しまっ", "l": "しまう"}]
        idx = G.build_grammar_index(ROWS)
        self.assertEqual([u.pattern for u in G.match_grammar_units(toks, idx)], ["〜てしまう"])

    def test_malformed_spec_raises(self):
        with self.assertRaises(ValueError):
            G.compile_match([[{"ctx": True}]])
        with self.assertRaises(ValueError):
            G.compile_match("て")

    def test_real_taxonomy_specs_compile(self):
        path = Path(__file__).resolve().parent.parent / "ledger" / "grammar_taxonomy.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for r in rows:
            G.compile_match(r.get("match"))
        G.build_grammar_index(rows)


class CoverageTest(unittest.TestCase):
    def test_stage1_writes_units_and_one_exposure_per_pattern(self):
        from tools.coverage import analyze
        transcript = {"episode": {"id": "ep_g"}, "sentences": [
            {"start": 0.0, "end": 1.0, "text": "全部食べてしまった。"},
            {"start": 1.0, "end": 2.0, "text": "本を読んじゃった。"},
            {"start": 2.0, "end": 3.0, "text": "行かない。"}]}
        known = {"known": {"全部", "食べる", "本", "読む", "行く", "しまう", "を", "て", "た", "ない"},
                 "learning": set(), "norm_known": set(), "known_stems": set(),
                 "phrases": {}, "sources": {}}
        cov = analyze(transcript, known, grammar=ROWS)
        self.assertTrue(cov["grammar_at"])
        s0, s1, s2 = cov["sentences"]
        self.assertEqual(s0["grammar"], [{"pattern": "〜てしまう", "start": 2, "end": 4}])
        self.assertEqual(s1["grammar"], [{"pattern": "〜てしまう", "start": 3, "end": 4}])
        self.assertEqual(s2["grammar"], [{"pattern": "〜ない", "start": 1, "end": 2}])
        exp = cov["exposures"]["〜てしまう"]
        self.assertEqual((exp["kind"], exp["occ"], exp["classification"]),
                         ("grammar", 2, "comprehensible"))
        self.assertEqual(cov["exposures"]["〜ない"]["kind"], "grammar")

    def test_no_taxonomy_means_no_grammar_field(self):
        from tools.coverage import analyze
        transcript = {"episode": {"id": "ep_g"}, "sentences": [
            {"start": 0.0, "end": 1.0, "text": "食べてしまった。"}]}
        known = {"known": set(), "learning": set(), "norm_known": set(),
                 "known_stems": set(), "phrases": {}, "sources": {}}
        cov = analyze(transcript, known)
        self.assertNotIn("grammar", cov["sentences"][0])


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.conn = lc.open_db(":memory:")
        lc.seed_grammar_points(self.conn, ROWS)

    def _expose(self, ep, pattern="〜てしまう", other=0, cls="comprehensible"):
        lc.record_exposure(self.conn, {"id": ep, "title": ep}, {
            pattern: {"sentence_idx": 0, "known_ratio": 1.0, "other_unknown_count": other,
                      "classification": cls, "kind": "grammar", "occ": 3}})
        lc.mark_watched(self.conn, ep)

    def test_seed_stores_match_and_order(self):
        rows = lc.grammar_match_rows(self.conn)
        self.assertEqual([r["pattern"] for r in rows][:3], ["〜て", "〜ない", "〜てしまう"])
        self.assertNotIn("〜ようだ", [r["pattern"] for r in rows])  # [] = curate-only
        self.assertEqual(json.loads(rows[2]["match"]), ROWS[2]["match"])
        with self.assertRaises(ValueError):
            lc.seed_grammar_points(self.conn, [{"pattern": "x", "match": [[{"ctx": True}]]}])

    def test_stage1_grammar_exposure_rides_the_confirm_flow(self):
        for i in range(2):
            self._expose(f"g{i}")
        lc.promote(self.conn)
        row = self.conn.execute(
            "SELECT status, confirm_candidate, exposure_count FROM grammar_points "
            "WHERE pattern = '〜てしまう'").fetchone()
        self.assertEqual(tuple(row), ("learning", 1, 2))  # N4 θ = (2, 2)
        # never a lemmas row for a grammar key
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM lemmas WHERE lemma = '〜てしまう'").fetchone())

    def test_unknown_pattern_exposure_is_dropped(self):
        self._expose("g1", pattern="〜nope")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE lemma = '〜nope'").fetchone())

    def test_meta_false_leaves_the_episode_row_alone(self):
        lc.record_exposure(self.conn, {"id": "e1", "title": "Real title"}, {})
        lc.record_exposure(self.conn, {"id": "e1"}, {
            "〜ない": {"sentence_idx": 0, "other_unknown_count": 0, "kind": "grammar"}},
            meta=False)
        self.assertEqual(self.conn.execute(
            "SELECT title FROM episodes WHERE id = 'e1'").fetchone()[0], "Real title")

    def test_grammar_tap_marks_and_lists(self):
        self._expose("g1")
        res = lc.apply_taps(self.conn, {"episode_id": "g1", "batch_id": "b1", "taps": [
            ["〜てしまう", "k", "grammar"], ["〜ない", "h", "grammar"],
            ["〜させる", "u", "grammar"], ["〜nope", "k", "grammar"]]})
        self.assertEqual((res["applied"], res["interest"]), (2, 1))
        lc.promote(self.conn)
        lists = lc.grammar_lists(self.conn)
        self.assertEqual(lists["known"], {"〜てしまう"})
        self.assertEqual(lists["interest"], {"〜ない"})
        self.assertEqual(lists["unknown"], {"〜させる"})
        self.assertEqual(lc.grammar_statuses(self.conn, ["〜てしまう", "〜させる", "〜x"]),
                         {"〜てしまう": "known", "〜させる": "learning", "〜x": "unknown"})
        self.assertEqual(lc.grammar_glosses(self.conn, ["〜ない"])["〜ない"],
                         {"gloss": "not", "level": 5})
        # a grammar ★ never leaks into the word interest set
        self.assertNotIn("〜ない", lc.active_interest(self.conn))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM lemmas WHERE lemma IN ('〜てしまう', '〜ない', '〜させる')").fetchone())

    def test_grammar_lookup_is_counted(self):
        self._expose("g1")
        lc.apply_taps(self.conn, {"episode_id": "g1", "batch_id": "b1", "taps": [],
                                  "lookups": [["〜てしまう", 2, {"none": 2}, "grammar"]]})
        row = self.conn.execute(
            "SELECT kind, context FROM evidence WHERE source = 'lookup'").fetchone()
        self.assertEqual(row["kind"], "grammar")

    def test_fold_rekeys_evidence(self):
        lc.seed_grammar_points(self.conn, [
            {"pattern": "〜られる（受身）", "level": 4, "gloss": "x"},
            {"pattern": "〜られる（可能）", "level": 4, "gloss": "y"}])
        self._expose("f1", pattern="〜られる（受身）")
        self._expose("f2", pattern="〜られる（可能）")
        self._expose("f2", pattern="〜られる（受身）")  # collides after the fold
        folds = {"〜られる（受身）": "〜られる", "〜られる（可能）": "〜られる"}
        r = lc.seed_grammar_points(
            self.conn, ROWS + [{"pattern": "〜られる", "level": 4, "gloss": "z",
                                "match": [[["れる", "られる"]]]}], folds=folds)
        lc.promote(self.conn)
        self.assertEqual(r["folded_rows"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE lemma = '〜られる'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM grammar_points WHERE pattern LIKE '〜られる（%'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT episode_spread FROM grammar_points WHERE pattern = '〜られる'").fetchone()[0], 2)

    def test_approve_carries_the_proposers_match(self):
        lc.record_curate_items(self.conn, "c1", {"grammar": [
            {"sentence_idx": 0, "proposed_pattern": "〜やがる", "gloss": "contempt",
             "example": "つるんでやがる", "match": [["やがる"]]}]})
        lc.approve_grammar_proposal(self.conn, "〜やがる", level=1)
        self.assertIn("〜やがる", [r["pattern"] for r in lc.grammar_match_rows(self.conn)])


class ServerTest(unittest.TestCase):
    """Transcript + paint carry grammar units with spans, statuses, glosses."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from server.app import create_app
        from tools._staging import episode_dir, write_json
        self.tmp = tempfile.TemporaryDirectory()
        work = Path(self.tmp.name)
        self.cfg = {"work_dir": str(work), "ledger_db": str(work / "ledger.db"),
                    "server": {"token": "t"}, "queue_db": str(work / "queue.db")}
        self.app = create_app(self.cfg, start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer t"}
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.seed_grammar_points(conn, ROWS)
        lc.confirm_known_lemma(conn, "〜ない", kind="grammar")
        lc.promote(conn)
        conn.close()
        d = episode_dir(self.cfg, "ep_g", create=True)
        toks = [{"s": t.surface, "l": t.lemma, "r": "", "c": False, "k": True}
                for t in L.tokenize("全部食べてしまった。")]
        self.cov = {"episode_id": "ep_g", "analyzed_at": "2026-09-08T00:00:00+00:00",
                    "stats": {}, "candidates": [], "reinforcement": [], "exposures": {},
                    "sentences": [
                        {"idx": 0, "start": 0.0, "end": 1.0, "text": "全部食べてしまった。",
                         "classification": "comprehensible", "known_ratio": 1.0,
                         "unknown": [], "tokens": toks},
                        {"idx": 1, "start": 1.0, "end": 2.0, "text": "行かない。",
                         "classification": "comprehensible", "known_ratio": 1.0,
                         "unknown": [], "tokens": [
                             {"s": "行か", "l": "行く", "r": "", "c": True, "k": True},
                             {"s": "ない", "l": "ない", "r": "", "c": False, "k": True},
                             {"s": "。", "l": "。", "r": "", "c": False, "k": True}]}]}
        write_json(d / "transcript.json", {"episode": {"id": "ep_g"}, "sentences": []})
        write_json(d / "coverage.json", self.cov)
        self.dir = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_sidecar_gets_a_live_pass_and_notes_merge(self):
        from tools._staging import write_json
        write_json(self.dir / "curate.json", {"grammar": [
            {"sentence_idx": 0, "pattern": "〜てしまう", "form_note": "ate the lot"},
            {"sentence_idx": 1, "proposed_pattern": "〜ねえ", "gloss": "rough ない"}]})
        data = self.client.get("/transcript/ep_g", headers=self.auth).json()
        self.assertEqual(data["sentences"][0]["grammar"], [
            {"pattern": "〜てしまう", "start": 2, "end": 4, "status": "unknown",
             "note": "ate the lot"}])
        self.assertEqual(data["sentences"][1]["grammar"], [
            {"pattern": "〜ない", "start": 1, "end": 2, "status": "known"},
            {"pattern": "〜ねえ", "note": "rough ない", "proposed": True}])
        self.assertEqual(data["grammar_points"]["〜てしまう"], {"gloss": "end up", "level": 4})
        self.assertIn("〜ない", data["grammar_points"])
        paint = self.client.get("/episodes/ep_g/paint", headers=self.auth).json()
        self.assertEqual(paint["grammar_known"], ["〜ない"])
        self.assertEqual(paint["grammar_confirm"], [])
        self.assertEqual(paint["grammar_interest"], [])
        self.assertEqual(paint["grammar_unknown"], [])

    def test_stage1_units_are_served_as_written(self):
        from tools._staging import write_json
        cov = dict(self.cov)
        cov["grammar_at"] = "2026-09-08T00:00:00+00:00"
        cov["sentences"][0]["grammar"] = [{"pattern": "〜て", "start": 2, "end": 3}]
        write_json(self.dir / "coverage.json", cov)
        data = self.client.get("/transcript/ep_g", headers=self.auth).json()
        self.assertEqual(data["sentences"][0]["grammar"],
                         [{"pattern": "〜て", "start": 2, "end": 3, "status": "unknown"}])
        self.assertNotIn("grammar", data["sentences"][1])

    def test_grammar_taps_land_and_paint(self):
        r = self.client.post("/taps", headers=self.auth, json={
            "episode_id": "ep_g", "batch_id": "b1",
            "taps": [["〜てしまう", "h", "grammar"], ["〜させる", "u", "grammar"]]}).json()
        self.assertEqual((r["applied"], r["interest"]), (1, 1))
        paint = self.client.get("/episodes/ep_g/paint", headers=self.auth).json()
        self.assertEqual(paint["grammar_interest"], ["〜てしまう"])
        self.assertEqual(paint["grammar_unknown"], [])  # 〜させる isn't in this episode


if __name__ == "__main__":
    unittest.main()
