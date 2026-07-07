"""Tool tests: segmentation, coverage ranking, deck push (fake AnkiConnect), render."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import ledgerctl as lc
from tools import acquire, coverage as cov_tool, deck, render
from tools._staging import episode_dir, write_json

KNOWN_BUNDLE = {
    "known": {"犬", "走る", "公園", "見る", "毎日", "行く"},
    "learning": {"設計"},
    "norm_known": set(),
    "known_stems": set(),
}

TRANSCRIPT = {
    "episode": {"id": "test_ep", "title": "テスト", "source": "x", "kind": "local",
                "audio": "REPLACED_IN_SETUP"},
    "sentences": [
        {"idx": 0, "start": 0.0, "end": 2.0, "text": "犬が公園を走る。"},
        {"idx": 1, "start": 2.0, "end": 4.0, "text": "犬が縄張りを走る。"},
        {"idx": 2, "start": 4.0, "end": 6.0, "text": "毎日設計を見る。"},
        {"idx": 3, "start": 6.0, "end": 8.0, "text": "頑丈な縄張りへ行く。"},
    ],
}


class SegmentTest(unittest.TestCase):
    def test_segment_without_api_key(self):
        subs = [(0.0, 2.0, "今日は天気が"), (2.0, 4.0, "いいですね。"),
                (4.0, 6.0, "散歩に行きましょう。")]
        sentences, restored = acquire.segment(subs, api_key=None, log=lambda m: None)
        self.assertFalse(restored)
        self.assertEqual([t for _, _, t in sentences],
                         ["今日は天気がいいですね。", "散歩に行きましょう。"])

    def test_clean_subs_drops_non_speech(self):
        subs = [(0, 1.0, "[音楽]"), (1, 2.0, "こんにちは。")]
        self.assertEqual(len(acquire.clean_subs(subs)), 1)


class CoverageAnalyzeTest(unittest.TestCase):
    def analyze(self, **kw):
        return cov_tool.analyze(TRANSCRIPT, KNOWN_BUNDLE, **kw)

    def test_classifications(self):
        cov = self.analyze()
        by_idx = {s["idx"]: s["classification"] for s in cov["sentences"]}
        self.assertEqual(by_idx[0], "comprehensible")
        self.assertEqual(by_idx[1], "i_plus_1")       # 縄張り the only gap
        self.assertEqual(by_idx[2], "reinforcement")  # 設計 is learning
        self.assertEqual(by_idx[3], "too_hard")       # 頑丈 + 縄張り

    def test_candidate_ranking_and_exclusions(self):
        freq = {"縄張り": 6000, "頑丈": 3000}
        cov = self.analyze(freq=freq)
        lemmas = [c["lemma"] for c in cov["candidates"]]
        self.assertNotIn("設計", lemmas)          # learning → reinforcement, not mining
        # 縄張り has a true-i+1 best sentence → outranks 頑丈 despite worse freq
        self.assertEqual(lemmas[0], "縄張り")
        self.assertEqual(cov["candidates"][0]["best"]["other_unknown_count"], 0)
        self.assertEqual(cov["candidates"][0]["best"]["sentence_idx"], 1)
        self.assertEqual(cov["candidates"][0]["recurrence"], 2)
        self.assertIsNone(cov["candidates"][0]["leverage"])  # P1 pending

    def test_already_carded_excluded(self):
        cov = self.analyze(already_carded={"縄張り"})
        self.assertNotIn("縄張り", [c["lemma"] for c in cov["candidates"]])

    def test_exposure_payload_shape(self):
        cov = self.analyze()
        exp = cov["exposures"]
        self.assertIn("犬", exp)
        self.assertEqual(exp["犬"]["other_unknown_count"], 0)
        for ctx in exp.values():
            self.assertLessEqual(
                {"sentence_idx", "known_ratio", "other_unknown_count"},
                set(ctx.keys()))


class DeckAndRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = Path(self.tmp.name)
        self.cfg = {"work_dir": str(work), "ledger_db": str(work / "ledger.db"),
                    "deck": {"name": "Test Mining"}}
        # 10s tone as the "native audio"
        self.audio = work / "tone.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=10",
             "-acodec", "libmp3lame", str(self.audio)], check=True)
        transcript = json.loads(json.dumps(TRANSCRIPT))
        transcript["episode"]["audio"] = str(self.audio)
        write_json(episode_dir(self.cfg, "test_ep", create=True) / "transcript.json",
                   transcript)
        self.conn = lc.open_db(":memory:")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_anki(self, calls, note_ids=None, fail_lemmas=()):
        note_ids = iter(note_ids or [1001, 1002, 1003])

        def call(action, **params):
            calls.append((action, params))
            if action == "modelNames":
                return []
            if action == "addNote":
                if params["note"]["fields"]["Lemma"] in fail_lemmas:
                    raise RuntimeError("AnkiConnect: duplicate")
                return next(note_ids)
            return None
        return call

    def test_push_cards(self):
        calls = []
        picks = [{"lemma": "縄張り", "sentence_idx": 1, "reading": "なわばり"}]
        result = deck.push_cards(self.cfg, "test_ep", picks,
                                 anki_call=self._fake_anki(calls),
                                 conn=self.conn, log=lambda m: None)
        self.assertEqual(result["pushed"], 1)
        actions = [a for a, _ in calls]
        self.assertIn("createModel", actions)   # model was missing → created
        self.assertIn("createDeck", actions)
        self.assertIn("storeMediaFile", actions)
        # Clip really exists and is a valid mp3 slice
        clip = episode_dir(self.cfg, "test_ep") / "clips" / "fullPipe_test_ep_0001.mp3"
        self.assertTrue(clip.exists() and clip.stat().st_size > 0)
        # ...and is loudness-normalized (source tone is ~-3.6 LUFS; the push
        # path must land it at deck.CLIP_TARGET_LUFS so cards review evenly).
        from engine.audio import measure_loudness
        self.assertAlmostEqual(measure_loudness(str(clip)),
                               deck.CLIP_TARGET_LUFS, delta=3.0)
        # Ledger: cards row with the AnkiConnect note id + mined_card → learning
        row = self.conn.execute(
            "SELECT anki_note_id FROM cards WHERE lemma='縄張り'").fetchone()
        self.assertEqual(row[0], 1001)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM lemmas WHERE lemma='縄張り'").fetchone()[0], "learning")

    def _stage_video(self):
        """Land a 10s test-pattern video.mp4 in the episode dir (frame source)."""
        vid = episode_dir(self.cfg, "test_ep", create=True) / "video.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=duration=10:size=320x240:rate=5",
             "-pix_fmt", "yuv420p", str(vid)], check=True)
        return vid

    def test_push_attaches_frame_when_video_present(self):
        self._stage_video()
        calls = []
        picks = [{"lemma": "縄張り", "sentence_idx": 1, "reading": "なわばり"}]
        result = deck.push_cards(self.cfg, "test_ep", picks,
                                 anki_call=self._fake_anki(calls),
                                 conn=self.conn, log=lambda m: None)
        self.assertEqual(result["pushed"], 1)
        # A real JPEG frame was extracted to images/
        frame = episode_dir(self.cfg, "test_ep") / "images" / "fullPipe_test_ep_0001.jpg"
        self.assertTrue(frame.exists() and frame.stat().st_size > 0)
        # It was stored as media and referenced in the Image field as an <img>
        stored = [p["filename"] for a, p in calls if a == "storeMediaFile"]
        self.assertIn("fullPipe_test_ep_0001.jpg", stored)
        note = next(p for a, p in calls if a == "addNote")["note"]
        self.assertEqual(note["fields"]["Image"], '<img src="fullPipe_test_ep_0001.jpg">')

    def test_push_without_video_leaves_image_empty(self):
        # No video.mp4 staged → card still mints, Image field is blank.
        calls = []
        picks = [{"lemma": "縄張り", "sentence_idx": 1, "reading": "なわばり"}]
        result = deck.push_cards(self.cfg, "test_ep", picks,
                                 anki_call=self._fake_anki(calls),
                                 conn=self.conn, log=lambda m: None)
        self.assertEqual(result["pushed"], 1)
        self.assertFalse((episode_dir(self.cfg, "test_ep") / "images").exists())
        note = next(p for a, p in calls if a == "addNote")["note"]
        self.assertEqual(note["fields"]["Image"], "")

    def test_ensure_model_migrates_pre_image_copy(self):
        # A built-in model minted before the Image field exists → migrate it.
        old_fields = ["Expression", "Audio", "Lemma", "Reading", "Source", "Sequence"]
        calls = []

        def fake(action, **params):
            calls.append((action, params))
            if action == "modelNames":
                return [deck.MODEL_NAME]
            if action == "modelFieldNames":
                return old_fields
            return None

        deck._ensure_model(fake)
        actions = {a for a, _ in calls}
        self.assertNotIn("createModel", actions)   # already exists
        adds = [p for a, p in calls if a == "modelFieldAdd"]
        self.assertEqual([p["fieldName"] for p in adds],
                         ["Image", "Notes", "Context"])
        self.assertEqual([p["index"] for p in adds], [6, 7, 8])  # appended
        self.assertIn("updateModelTemplates", actions)
        self.assertIn("updateModelStyling", actions)

    def test_push_skips_duplicates(self):
        calls = []
        picks = [{"lemma": "縄張り", "sentence_idx": 1},
                 {"lemma": "頑丈", "sentence_idx": 3}]
        result = deck.push_cards(self.cfg, "test_ep", picks,
                                 anki_call=self._fake_anki(calls, fail_lemmas={"頑丈"}),
                                 conn=self.conn, log=lambda m: None)
        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["skipped"], ["頑丈"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM cards WHERE lemma='頑丈'").fetchone())

    def test_push_onto_user_note_type(self):
        cfg = {**self.cfg,
               "deck": {"name": "MinePrime", "note_type": "Sentence Cards",
                        "field_map": {"sentence": ["Sentence", "Japanese"],
                                      "audio": "Audio",
                                      "english": "English",
                                      "notes": "Notes",
                                      "context": "Context"}}}
        calls = []

        def fake(action, **params):
            calls.append((action, params))
            if action == "modelNames":
                return ["Sentence Cards"]
            if action == "addNote":
                return 2001
            return None

        picks = [{"lemma": "縄張り", "sentence_idx": 1, "reading": "なわばり",
                  "english": "The dog runs its territory.",
                  "notes": "縄張り(なわばり): an animal's defended territory.",
                  "context": "The host is describing the stray dog's routine."}]
        result = deck.push_cards(cfg, "test_ep", picks, anki_call=fake,
                                 conn=self.conn, log=lambda m: None)
        self.assertEqual(result["pushed"], 1)
        self.assertNotIn("createModel", [a for a, _ in calls])  # user model reused
        note = next(p for a, p in calls if a == "addNote")["note"]
        self.assertEqual(note["modelName"], "Sentence Cards")
        self.assertEqual(set(note["fields"]),
                         {"Sentence", "Japanese", "Audio", "English",
                          "Notes", "Context"})
        self.assertEqual(note["fields"]["Sentence"], "犬が縄張りを走る。")
        self.assertEqual(note["fields"]["Japanese"], "犬が縄張りを走る。")
        self.assertEqual(note["fields"]["English"], "The dog runs its territory.")
        self.assertEqual(note["fields"]["Notes"],
                         "縄張り(なわばり): an animal's defended territory.")
        self.assertEqual(note["fields"]["Context"],
                         "The host is describing the stray dog's routine.")
        self.assertTrue(note["fields"]["Audio"].startswith("[sound:"))

    def test_push_unknown_note_type_fails_loudly(self):
        cfg = {**self.cfg, "deck": {"name": "X", "note_type": "Nope"}}
        with self.assertRaisesRegex(RuntimeError, "note type 'Nope' not found"):
            deck.push_cards(cfg, "test_ep",
                            [{"lemma": "縄張り", "sentence_idx": 1}],
                            anki_call=lambda action, **kw: [],
                            conn=self.conn, log=lambda m: None)

    def test_build_apkg(self):
        picks = [{"lemma": "縄張り", "sentence_idx": 1, "reading": "なわばり"}]
        result = deck.build_apkg(self.cfg, "test_ep", picks,
                                 conn=self.conn, log=lambda m: None)
        self.assertTrue(Path(result["apkg"]).exists())
        row = self.conn.execute(
            "SELECT anki_guid, anki_note_id FROM cards WHERE lemma='縄張り'").fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])

    def test_render_excludes_curated_junk(self):
        cov = cov_tool.analyze(TRANSCRIPT, KNOWN_BUNDLE, freq={"縄張り": 6000})
        write_json(episode_dir(self.cfg, "test_ep") / "coverage.json", cov)
        write_json(episode_dir(self.cfg, "test_ep") / "curate.json", {
            "synopsis": "犬の話。",
            "keywords": [{"word": "縄張り", "gloss": "territory"}],
            "exclude": [{"lemma": "縄張り", "why": "test: pretend misparse"}],
        })
        out = render.run_render(self.cfg, "test_ep")
        html = out.read_text(encoding="utf-8")
        data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0]
                          .replace("<\\/", "</"))
        self.assertNotIn("縄張り", [g["lemma"] for g in data["glossary"]])
        self.assertNotIn("縄張り", [x["lemma"] for x in data["iplus1"]])

    def test_render(self):
        cov = cov_tool.analyze(TRANSCRIPT, KNOWN_BUNDLE, freq={"縄張り": 6000})
        write_json(episode_dir(self.cfg, "test_ep") / "coverage.json", cov)
        write_json(episode_dir(self.cfg, "test_ep") / "curate.json", {
            "synopsis": "犬の話。",
            "keywords": [{"word": "縄張り", "gloss": "territory"}],
            "focal_points": [{"word": "縄張り", "why": "recurs and unlocks both hard sentences"}],
        })
        out = render.run_render(self.cfg, "test_ep")
        html = out.read_text(encoding="utf-8")
        self.assertNotIn("__PREP_DATA__", html)
        self.assertIn("縄張り", html)
        self.assertIn("territory", html)
        self.assertIn("犬の話。", html)
        self.assertNotIn("</script>\\", html)  # payload escaping sanity
        # i+1 target sentence is embedded for token-level rendering
        data = html.split("const DATA = ", 1)[1].split(";\n", 1)[0]
        payload = json.loads(data.replace("<\\/", "</"))
        self.assertEqual(payload["iplus1"][0]["lemma"], "縄張り")
        self.assertIn("1", payload["sentences_by_idx"])


if __name__ == "__main__":
    unittest.main()
