"""Engine smoke tests: imports, sentence merging, punctuation diff, tokenizer."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import lemma as L
from engine import srt_parser as SP
from engine import word_align as WA
from engine.punctuation import (_extract_punct_insertions, _realign_to_blocks,
                                get_language_config)

JA_ENDERS = r'[。！？.!?」]\s*$'


class ImportTest(unittest.TestCase):
    def test_all_modules_import(self):
        import engine.anki  # noqa: F401
        import engine.audio  # noqa: F401
        import engine.downloader  # noqa: F401
        import engine.local_file  # noqa: F401
        import engine.paths  # noqa: F401
        import engine.punctuation  # noqa: F401
        import engine.srt_parser  # noqa: F401
        import engine.transcriber  # noqa: F401
        import engine.tts  # noqa: F401
        import engine.word_align  # noqa: F401


class AudioLoudnessTest(unittest.TestCase):
    """slice_audio(target_lufs=…) must land sources of very different volume
    at the same loudness — the guarantee that card audio isn't jarring."""

    def _tone(self, work, name, volume):
        path = work / name
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=4",
             "-af", f"volume={volume}",
             "-acodec", "libmp3lame", str(path)], check=True)
        return path

    def test_slice_normalizes_to_target(self):
        from engine.audio import measure_loudness, slice_audio

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            loud = self._tone(work, "loud.mp3", "0.9")
            quiet = self._tone(work, "quiet.mp3", "0.05")

            clips = []
            for src in (loud, quiet):
                clip = work / f"clip_{src.stem}.mp3"
                slice_audio(str(src), 0.5, 3.5, str(clip), target_lufs=-16.0)
                clips.append(measure_loudness(str(clip)))

            for lufs in clips:
                self.assertAlmostEqual(lufs, -16.0, delta=3.0)
            # The point of normalization: both clips end up at the same level.
            self.assertLess(abs(clips[0] - clips[1]), 2.0)

    def test_slice_without_target_keeps_source_volume(self):
        from engine.audio import measure_loudness, slice_audio

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            quiet = self._tone(work, "quiet.mp3", "0.05")
            clip = work / "clip.mp3"
            slice_audio(str(quiet), 0.5, 3.5, str(clip))
            # Un-normalized slice stays at the source's (quiet) level.
            self.assertLess(measure_loudness(str(clip)), -25.0)


class SrtParserTest(unittest.TestCase):
    def test_merge_to_sentences_reconstructs_fragments(self):
        subs = [
            (0.0, 2.0, "今日は天気が"),
            (2.0, 4.0, "いいですね。"),
            (4.0, 6.0, "散歩に行きましょう。"),
        ]
        merged = SP.merge_to_sentences(subs, JA_ENDERS)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0][2], "今日は天気がいいですね。")
        self.assertEqual(merged[0][0], 0.0)
        self.assertEqual(merged[0][1], 4.0)  # audio span covers the merged range

    def test_merge_splits_jammed_sentences(self):
        subs = [(0.0, 4.0, "行くよ。待って！")]
        merged = SP.merge_to_sentences(subs, JA_ENDERS)
        self.assertEqual([m[2] for m in merged], ["行くよ。", "待って！"])

    def test_has_good_punctuation(self):
        good = [(0, 1, "そうですね。"), (1, 2, "はい。")]
        bad = [(0, 1, "そうですね"), (1, 2, "はい"), (2, 3, "でも"), (3, 4, "やはり"), (4, 5, "違う")]
        self.assertTrue(SP.has_good_punctuation(good, r'[。！？]'))
        self.assertFalse(SP.has_good_punctuation(bad, r'[。！？]'))

    def test_filter_non_speech(self):
        subs = [(0, 1, "[音楽]"), (1, 2, "♪♪"), (2, 3, "こんにちは")]
        self.assertEqual(len(SP.filter_non_speech(subs)), 1)


class PunctuationTest(unittest.TestCase):
    def test_insert_only_diff(self):
        cfg = get_language_config("Japanese")
        original = "今日は天気がいいですね散歩に行きましょう"
        # LLM inserted punctuation AND rewrote a word — rewrite must be discarded.
        llm_output = "今日は天候がいいですね。散歩に行きましょう。"
        result = _extract_punct_insertions(original, llm_output, cfg["punct_chars"])
        self.assertEqual(result, "今日は天気がいいですね。散歩に行きましょう。")

    def test_realign_to_blocks(self):
        cfg = get_language_config("Japanese")
        blocks = ["今日は天気が", "いいですね散歩に", "行きましょう"]
        merged = "今日は天気がいいですね。散歩に行きましょう。"
        realigned = _realign_to_blocks(blocks, merged, cfg["punct_chars"])
        self.assertEqual(realigned, ["今日は天気が", "いいですね。散歩に", "行きましょう。"])
        # Original character content is preserved block-for-block
        strip = lambda s: ''.join(c for c in s if c not in cfg["punct_chars"])
        for orig, new in zip(blocks, realigned):
            self.assertEqual(strip(new), orig)


class LemmaTest(unittest.TestCase):
    def test_tokenize_mode_c(self):
        tokens = L.tokenize("警察官が犬を見た。")
        lemmas = [t.lemma for t in tokens]
        self.assertIn("警察官", lemmas)  # mode C keeps the compound whole

    def test_furigana_over_kanji_only(self):
        # okurigana is peeled: reading sits on the kanji, す/く stay bare
        self.assertEqual(L.furigana("通す"), [["通", "とお"], ["す", None]])
        self.assertEqual(L.furigana("行く"), [["行", "い"], ["く", None]])
        # no okurigana → whole-word ruby; kana-only → no reading at all
        self.assertEqual(L.furigana("大丈夫"), [["大丈夫", "だいじょうぶ"]])
        self.assertEqual(L.furigana("くれる"), [["くれる", None]])
        # segments always reconstruct the input exactly
        for w in ("通す", "行く", "大丈夫", "くれる", "食べる"):
            self.assertEqual("".join(s[0] for s in L.furigana(w)), w)

    def test_content_tokens_filters_particles(self):
        tokens = L.content_tokens("犬が走った。")
        lemmas = [t.lemma for t in tokens]
        self.assertIn("犬", lemmas)
        self.assertIn("走る", lemmas)  # dictionary form
        self.assertNotIn("が", lemmas)
        self.assertNotIn("た", lemmas)

    def test_known_set_normalized_variant(self):
        # こもる is known when 籠る is known — both normalize to 籠もる.
        base = L.tokenize("籠る")[0]
        variant = L.tokenize("こもる")[0]
        self.assertEqual(base.normalized, variant.normalized)
        ks = L.KnownSet(known={"籠る"}, norm_known={base.normalized})
        self.assertIn(variant, ks)

    def test_analyze_sentence_classification(self):
        ks = L.KnownSet(known={"犬", "走る", "公園"})
        d = L.analyze_sentence("犬が公園を走る。", ks)
        self.assertEqual(d["classification"], "comprehensible")

        d = L.analyze_sentence("犬が縄張りを走る。", ks)
        self.assertEqual(d["classification"], "i_plus_1")
        self.assertEqual(d["unknown_lemmas"], ["縄張り"])

        d = L.analyze_sentence("犬が縄張りを走る。", ks, learning={"縄張り"})
        self.assertEqual(d["classification"], "reinforcement")

    def test_analyze_transcript_exposure_shape(self):
        ks = L.KnownSet(known={"犬", "走る"})
        sentences = [(0.0, 2.0, "犬が走る。"), (2.0, 4.0, "犬が縄張りを守る。")]
        result = L.analyze_transcript(sentences, ks)
        self.assertEqual(result["total_sentences"], 2)
        exp = result["exposures"]
        self.assertIn("犬", exp)
        # 犬's best context is the fully-known sentence → other_unknown_count 0
        self.assertEqual(exp["犬"]["other_unknown_count"], 0)
        # 縄張り appears only in a sentence where it is the sole gap... but 守る
        # is also unknown there, so its exposure carries other unknowns.
        self.assertIn("縄張り", exp)
        self.assertEqual(exp["縄張り"]["other_unknown_count"], 1)

    # --- phrase units (GRAMMAR.md — i+1 with phrases) -----------------------

    def test_phrase_units_match_inflected(self):
        # the headword's lemma sequence matches the inflected surface:
        # 気を付けて → 気|を|付ける|て contains 気|を|付ける
        ks = L.KnownSet(known=set(), phrases={"気を付ける": "known"})
        units = ks.phrase_units(L.tokenize("今日は気を付けてね。"))
        self.assertEqual([(u["phrase"], u["status"]) for u in units],
                         [("気を付ける", "known")])

    def test_single_token_phrase_key_never_matches(self):
        # defensive: a single-token key can't be a phrase unit — it would turn
        # every ordinary word into a "phrase"
        ks = L.KnownSet(known=set(), phrases={"犬": "known"})
        self.assertEqual(ks.phrase_units(L.tokenize("犬が走る。")), [])

    def test_unknown_phrase_is_one_unit_iplus1(self):
        # every word is known; the phrase itself is the single unknown unit
        ks = L.KnownSet(known={"今日", "気", "付ける", "する"},
                        phrases={"気を付ける": "unknown"})
        d = L.analyze_sentence("今日は気を付けて。", ks)
        self.assertEqual(d["classification"], "i_plus_1")
        self.assertEqual(d["unknown_lemmas"], ["気を付ける"])

    def test_known_phrase_covers_unknown_components(self):
        # 付ける is unknown as a word, but the known phrase is the unit in
        # play — its component tokens leave the unknown tally
        ks = L.KnownSet(known={"今日"}, phrases={"気を付ける": "known"})
        d = L.analyze_sentence("今日は気を付けて。", ks)
        self.assertEqual(d["classification"], "comprehensible")
        self.assertEqual(d["unknown_lemmas"], [])

    def test_learning_phrase_is_reinforcement(self):
        ks = L.KnownSet(known={"今日"}, phrases={"気を付ける": "learning"})
        d = L.analyze_sentence("今日は気を付けて。", ks)
        self.assertEqual(d["classification"], "reinforcement")

    def test_transcript_emits_phrase_exposures(self):
        ks = L.KnownSet(known={"今日", "犬", "走る"},
                        phrases={"気を付ける": "learning"})
        sentences = [(0.0, 2.0, "今日は気を付けて。"), (2.0, 4.0, "犬が走る。")]
        exp = L.analyze_transcript(sentences, ks)["exposures"]
        self.assertEqual(exp["気を付ける"]["kind"], "phrase")
        self.assertEqual(exp["気を付ける"]["classification"], "reinforcement")
        self.assertNotIn("kind", exp["犬"])  # words are unmarked (default)


class WordAlignTest(unittest.TestCase):
    def test_char_timeline_interpolates_within_word_spans(self):
        tl = WA.char_timeline([
            {"text": "こんにちは", "start": 1.0, "end": 2.0},
            {"text": "、元気？", "start": 3.0, "end": 3.6},  # punct skipped
        ])
        self.assertEqual("".join(ch for ch, _ in tl), "こんにちは元気")
        self.assertAlmostEqual(tl[0][1], 1.0)
        self.assertAlmostEqual(tl[1][1], 1.2)  # 5 chars over 1s
        self.assertAlmostEqual(tl[5][1], 3.0)  # 元 opens the second word

    def test_alignment_survives_punctuation_restoration(self):
        # ASR emitted no punctuation; sentences got 。 inserted and were split
        # differently — alignment is content-chars only, so times still land.
        timeline = WA.char_timeline([
            {"text": "今日は天気がいいですね散歩に行きましょう",
             "start": 0.0, "end": 4.0},
        ])
        times = WA.sentence_char_times(
            ["今日は天気がいいですね。", "散歩に行きましょう。"], timeline)
        self.assertIsNotNone(times)
        self.assertEqual(len(times[0]), 11)  # content chars only
        self.assertAlmostEqual(times[0][0], 0.0)
        # 散 is char 11 of 20 → 4.0 * 11/20
        self.assertAlmostEqual(times[1][0], 2.2)

    def test_alignment_bridges_dropped_blocks_and_stays_monotonic(self):
        # A cleaned-out block (♪♪ annotation) leaves a hole in the sentence
        # stream; surrounding matches must still align and never go backward.
        timeline = WA.char_timeline([
            {"text": "犬が走る", "start": 0.0, "end": 1.0},
            {"text": "ラララ", "start": 1.0, "end": 2.0},   # dropped downstream
            {"text": "公園へ行く", "start": 5.0, "end": 6.0},
        ])
        times = WA.sentence_char_times(["犬が走る。", "公園へ行く。"], timeline)
        self.assertIsNotNone(times)
        self.assertAlmostEqual(times[0][0], 0.0)
        self.assertAlmostEqual(times[1][0], 5.0)
        flat = [t for s in times for t in s]
        self.assertEqual(flat, sorted(flat))

    def test_mismatched_streams_refuse_to_guess(self):
        # e.g. a stale words sidecar from a different transcription
        timeline = WA.char_timeline([{"text": "全然違う音声の内容です",
                                      "start": 0.0, "end": 3.0}])
        self.assertIsNone(WA.sentence_char_times(
            ["犬が公園を走る。", "毎日設計を見る。"], timeline))
        self.assertIsNone(WA.sentence_char_times(["犬が走る。"], []))


if __name__ == "__main__":
    unittest.main()
