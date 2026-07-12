"""tools.repair — subagent transcript repair: validation, apply, coverage feed."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import lemma as L  # noqa: E402
from tools import repair as R  # noqa: E402
from tools._staging import episode_dir, read_json, write_json  # noqa: E402

EP = "test_ep"


def make_episode(work_dir, sentences, repair_applied=False):
    cfg = {"work_dir": str(work_dir)}
    d = episode_dir(cfg, EP, create=True)
    transcript = {
        "episode": {"id": EP, "title": "t"},
        "sentences": [
            {"idx": i, "start": float(i), "end": float(i) + 1.0, "text": t}
            for i, t in enumerate(sentences)
        ],
    }
    if repair_applied:
        transcript["repair_applied"] = True
    write_json(d / "transcript.json", transcript)
    return cfg, d


class RepairCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_check_exports_sentences_and_suspects(self):
        cfg, d = make_episode(self.tmp.name, ["合法なりまして。", "こんにちは。"])
        write_json(d / "coverage.json", {
            "candidates": [{"lemma": "合法", "freq_rank": None}],
            "sentences": [{"idx": 0, "text": "合法なりまして。",
                           "unknown": ["合法"], "tokens": []}],
        })
        logs = []
        R.cmd_check(cfg, EP, logs.append)
        blocks = read_json(d / "repair_blocks.json")
        self.assertEqual(len(blocks["sentences"]), 2)
        self.assertEqual(blocks["suspects"][0]["lemma"], "合法")
        self.assertIsNone(blocks["suspects"][0]["freq_rank"])

    def test_check_skips_when_already_applied(self):
        cfg, d = make_episode(self.tmp.name, ["こんにちは。"], repair_applied=True)
        logs = []
        R.cmd_check(cfg, EP, logs.append)
        self.assertFalse((d / "repair_blocks.json").exists())


class RepairApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg, self.d = make_episode(
            self.tmp.name,
            ["合法なりまして競争スタート。", "山口いぶきが1着。"])

    def apply(self, payload):
        out = self.d / "repair_out.json"
        write_json(out, payload)
        logs = []
        R.cmd_apply(self.cfg, EP, out, logs.append)
        return logs

    def test_edit_applied_and_artifacts_rewritten(self):
        self.apply({"edits": [{"idx": 0, "old": "合法なりまして",
                               "new": "号砲鳴りまして"}]})
        t = read_json(self.d / "transcript.json")
        self.assertEqual(t["sentences"][0]["text"], "号砲鳴りまして競争スタート。")
        self.assertTrue(t["repair_applied"])
        self.assertEqual(t["repair_source"], "subagent")
        self.assertIn("号砲鳴りまして", (self.d / "sentences.srt").read_text())
        # timings untouched
        self.assertEqual(t["sentences"][0]["start"], 0.0)
        rep = read_json(self.d / "repair.json")
        self.assertEqual(len(rep["edits"]), 1)

    def test_bad_edits_rejected_not_fatal(self):
        self.apply({"edits": [
            {"idx": 99, "old": "x", "new": "y"},                   # out of range
            {"idx": 0, "old": "ここにない", "new": "y"},            # not found
            {"idx": 0, "old": "合法", "new": "合" * 40},            # len delta
            {"idx": 1, "old": "1着", "new": "1着"},                 # no-op
        ]})
        rep = read_json(self.d / "repair.json")
        self.assertEqual(len(rep["edits"]), 0)
        self.assertEqual(len(rep["edits_rejected"]), 4)
        t = read_json(self.d / "transcript.json")
        self.assertEqual(t["sentences"][0]["text"], "合法なりまして競争スタート。")

    def test_names_and_nonwords_expand_to_non_vocab(self):
        self.apply({"names": [{"surface": "いぶき", "kind": "person"},
                              "矢崎ヒカル"],
                    "nonwords": ["ワンタイン"]})
        nv = R.load_non_vocab(self.cfg, EP)
        self.assertIn("いぶき", nv)
        self.assertIn("ワンタイン", nv)
        # multi-token name contributes component lemmas too
        self.assertIn("ヒカル", nv)

    def test_load_non_vocab_empty_without_repair(self):
        self.assertEqual(R.load_non_vocab({"work_dir": self.tmp.name}, "nope"),
                         frozenset())


class NonVocabAnalysisTest(unittest.TestCase):
    def test_adjudicated_name_leaves_unknown_tally(self):
        ks = L.KnownSet(known={"着"})
        d = L.analyze_sentence("山口いぶきが1着。", ks)
        self.assertIn("いぶき", d["unknown_lemmas"])  # Sudachi thinks common noun
        d = L.analyze_sentence("山口いぶきが1着。", ks, non_vocab={"いぶき"})
        self.assertEqual(d["unknown_lemmas"], [])
        self.assertEqual(d["classification"], "comprehensible")

    def test_non_vocab_excluded_from_exposures(self):
        ks = L.KnownSet(known=set())
        res = L.analyze_transcript([(0.0, 1.0, "ワンタインでかかる。")], ks,
                                   non_vocab={"ワンタイン"})
        self.assertNotIn("ワンタイン", res["exposures"])


if __name__ == "__main__":
    unittest.main()
