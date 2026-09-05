"""tools.jmdict: parse a miniature JMdict (with the real file's internal-DTD
entity style), build the SQLite tables, and look lemmas up common-first."""

import io
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import jmdict

MINI_JMDICT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE JMdict [
<!ENTITY n "noun (common) (futsuumeishi)">
<!ENTITY vs "noun or participle which takes the aux. verb suru">
<!ENTITY v1 "Ichidan verb">
<!ENTITY prt "particle">
<!ENTITY uk "word usually written using kana alone">
]>
<JMdict>
<entry>
<ent_seq>1</ent_seq>
<k_ele><keb>公園</keb><ke_pri>news1</ke_pri><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>こうえん</reb><re_pri>news1</re_pri></r_ele>
<sense><pos>&n;</pos><gloss>(public) park</gloss></sense>
</entry>
<entry>
<ent_seq>2</ent_seq>
<k_ele><keb>公演</keb></k_ele>
<r_ele><reb>こうえん</reb></r_ele>
<sense><pos>&n;</pos><pos>&vs;</pos>
<gloss>public performance</gloss><gloss>concert</gloss></sense>
</entry>
<entry>
<ent_seq>3</ent_seq>
<r_ele><reb>する</reb><re_pri>ichi1</re_pri></r_ele>
<sense><pos>&vs;</pos><gloss>to do</gloss><gloss>to carry out</gloss></sense>
</entry>
<entry>
<ent_seq>4</ent_seq>
<k_ele><keb>骨</keb></k_ele>
<r_ele><reb>ほね</reb></r_ele>
<sense></sense>
</entry>
<entry>
<ent_seq>5</ent_seq>
<k_ele><keb>許す</keb></k_ele>
<r_ele><reb>ゆるす</reb></r_ele>
<sense><pos>&vs;</pos><gloss>to permit</gloss><gloss>to allow</gloss></sense>
</entry>
<entry>
<ent_seq>6</ent_seq>
<k_ele><keb>照る</keb><ke_pri>news1</ke_pri></k_ele>
<r_ele><reb>てる</reb><re_pri>news1</re_pri></r_ele>
<sense><pos>&v1;</pos><gloss>to shine</gloss></sense>
</entry>
<entry>
<ent_seq>7</ent_seq>
<r_ele><reb>てる</reb></r_ele>
<sense><pos>auxiliary verb</pos><gloss>to be ...-ing</gloss></sense>
</entry>
<entry>
<ent_seq>8</ent_seq>
<k_ele><keb>野</keb><ke_pri>news1</ke_pri><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>の</reb><re_pri>news1</re_pri></r_ele>
<sense><pos>&n;</pos><gloss>field</gloss></sense>
</entry>
<entry>
<ent_seq>9</ent_seq>
<k_ele><keb>乃</keb></k_ele>
<r_ele><reb>の</reb><re_pri>spec1</re_pri></r_ele>
<sense><pos>&prt;</pos><gloss>indicates possessive</gloss></sense>
</entry>
<entry>
<ent_seq>10</ent_seq>
<k_ele><keb>彼方</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>かなた</reb><re_pri>ichi1</re_pri></r_ele>
<r_ele><reb>あなた</reb><re_pri>ichi1</re_pri></r_ele>
<sense><pos>&n;</pos><misc>&uk;</misc><gloss>beyond</gloss></sense>
</entry>
<entry>
<ent_seq>11</ent_seq>
<k_ele><keb>貴方</keb></k_ele>
<r_ele><reb>あなた</reb></r_ele>
<sense><pos>&n;</pos><misc>&uk;</misc><gloss>you</gloss></sense>
</entry>
<entry>
<ent_seq>12</ent_seq>
<k_ele><keb>ＡＩ</keb></k_ele>
<r_ele><reb>エーアイ</reb></r_ele>
<sense><pos>&n;</pos><gloss>artificial intelligence</gloss></sense>
</entry>
<entry>
<ent_seq>13</ent_seq>
<k_ele><keb>帝王切開</keb></k_ele>
<r_ele><reb>ていおうせっかい</reb></r_ele>
<sense><pos>&n;</pos><gloss>Caesarean section</gloss></sense>
</entry>
<entry>
<ent_seq>14</ent_seq>
<k_ele><keb>入る</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>はいる</reb><re_pri>ichi1</re_pri></r_ele>
<sense><pos>&v1;</pos><gloss>to enter</gloss></sense>
</entry>
<entry>
<ent_seq>15</ent_seq>
<k_ele><keb>気を付ける</keb></k_ele>
<k_ele><keb>気をつける</keb></k_ele>
<r_ele><reb>きをつける</reb></r_ele>
<sense><pos>&v1;</pos><gloss>to be careful</gloss></sense>
</entry>
<entry>
<ent_seq>17</ent_seq>
<k_ele><keb>市内</keb></k_ele>
<r_ele><reb>しない</reb></r_ele>
<sense><pos>&n;</pos><gloss>within the city</gloss></sense>
</entry>
<entry>
<ent_seq>18</ent_seq>
<k_ele><keb>航する</keb></k_ele>
<r_ele><reb>こうする</reb></r_ele>
<sense><pos>&vs;</pos><gloss>to voyage</gloss></sense>
</entry>
<entry>
<ent_seq>16</ent_seq>
<k_ele><keb>と言う</keb></k_ele>
<r_ele><reb>という</reb></r_ele>
<sense><pos>&prt;</pos><misc>&uk;</misc><gloss>called; named</gloss></sense>
</entry>
</JMdict>
"""


class TestJmdict(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.count = jmdict.build_db(
            self.conn, jmdict.parse_entries(io.BytesIO(MINI_JMDICT.encode())))

    def test_build_skips_glossless_entries(self):
        self.assertEqual(self.count, 17)  # entry 4 (骨) has no glosses, dropped

    def test_lookup_by_kanji_and_kana(self):
        out = jmdict.lookup_many(self.conn, ["公園", "する", "ない"])
        self.assertEqual(out["公園"][0]["s"][0]["g"], ["(public) park"])
        self.assertEqual(out["公園"][0]["r"], ["こうえん"])
        self.assertEqual(out["する"][0]["s"][0]["g"][0], "to do")
        self.assertNotIn("ない", out)  # missing lemmas are absent, not empty

    def test_pos_entities_shortened(self):
        out = jmdict.lookup_many(self.conn, ["公園"])
        self.assertEqual(out["公園"][0]["s"][0]["pos"], ["noun"])

    def test_lookup_falls_back_to_normalized_form(self):
        # 許せる (potential) isn't a headword; its Sudachi normalized form 許す is
        out = jmdict.lookup_many(self.conn, ["許せる"])
        self.assertIn("許せる", out)
        self.assertEqual(out["許せる"][0]["s"][0]["g"][0], "to permit")
        # a genuinely unknown word still returns nothing
        self.assertNotIn("存在しない語", jmdict.lookup_many(self.conn, ["存在しない語"]))

    def test_shared_reading_orders_common_first(self):
        out = jmdict.lookup_many(self.conn, ["こうえん"])
        self.assertEqual(len(out["こうえん"]), 2)
        self.assertEqual(out["こうえん"][0]["k"], ["公園"])  # pri beats 公演

    def test_kana_key_prefers_kana_natural_entry(self):
        # a kana lemma means the word is written kana — the kanji-less てる
        # auxiliary outranks common-but-kanji 照る
        out = jmdict.lookup_many(self.conn, ["てる"])
        self.assertEqual(out["てる"][0]["s"][0]["g"], ["to be ...-ing"])
        self.assertEqual(out["てる"][1]["k"], ["照る"])  # demoted, not hidden

    def test_kana_key_first_reading_beats_secondary(self):
        # both 貴方 and 彼方 are uk; あなた is 貴方's PRIMARY reading but
        # 彼方's secondary (かなた) — 貴方 "you" must lead despite lower pri
        out = jmdict.lookup_many(self.conn, ["あなた"])
        self.assertEqual(out["あなた"][0]["s"][0]["g"], ["you"])

    def test_kana_key_grammar_pos_rescues_function_word(self):
        # JMdict keys the possessive particle under 乃/之 — kanji-headed, no
        # uk — so among non-natural entries a particle POS beats 野's pri
        out = jmdict.lookup_many(self.conn, ["の"])
        self.assertEqual(out["の"][0]["s"][0]["g"], ["indicates possessive"])

    def test_uk_flag_survives_the_wire_format(self):
        out = jmdict.lookup_many(self.conn, ["貴方"])
        self.assertTrue(out["貴方"][0]["s"][0]["uk"])
        park = jmdict.lookup_many(self.conn, ["公園"])
        self.assertNotIn("uk", park["公園"][0]["s"][0])  # absent, not False

    def test_ascii_lemma_falls_back_to_fullwidth_key(self):
        # transcripts carry acronyms half-width; JMdict keys them full-width
        out = jmdict.lookup_many(self.conn, ["AI"])
        self.assertEqual(out["AI"][0]["s"][0]["g"], ["artificial intelligence"])
        self.assertNotIn("ZZZ", jmdict.lookup_many(self.conn, ["ZZZ"]))

    def test_worklist_junk_filter(self):
        junk = ["。", "…", "2019", "6億", "1日", "ぇ", "p", " ", "・"]
        for lemma in junk:
            self.assertTrue(jmdict.WORKLIST_JUNK.search(lemma), lemma)
        real = ["てらっしゃる", "大濠公園", "ドガ", "LINE", "アイコン"]
        for lemma in real:
            self.assertFalse(jmdict.WORKLIST_JUNK.search(lemma), lemma)

    def test_compound_entries_joins_split_compounds(self):
        # Sudachi splits 帝王切開 into 帝王|切開 — the run's join is a real
        # headword and re-tokenizes to the same lemma sequence → served
        sentences = [{"tokens": [
            {"s": "帝王", "l": "帝王"}, {"s": "切開", "l": "切開"},
            {"s": "の", "l": "の"}, {"s": "話", "l": "話"}]}]
        out = jmdict.compound_entries(self.conn, sentences)
        self.assertEqual(out["帝王切開"][0]["s"][0]["g"], ["Caesarean section"])

    def test_compound_entries_dictionary_form_of_inflected_tail(self):
        # 気を付けて: surface concat isn't a headword, surfaces+final-lemma is
        sentences = [{"tokens": [
            {"s": "気", "l": "気"}, {"s": "を", "l": "を"},
            {"s": "付け", "l": "付ける"}, {"s": "て", "l": "て"}]}]
        out = jmdict.compound_entries(self.conn, sentences)
        self.assertEqual(out["気を付ける"][0]["s"][0]["g"], ["to be careful"])

    def test_compound_entries_spelling_variant_canonical(self):
        # 気をつけて run ↔ canonical 気を付ける: the kana spelling is a key of
        # the same entry, and same_lexeme unifies つける/付ける via
        # Sudachi's normalized form
        sentences = [{"tokens": [
            {"s": "気", "l": "気"}, {"s": "を", "l": "を"},
            {"s": "つけ", "l": "つける"}, {"s": "て", "l": "て"}]}]
        out = jmdict.compound_entries(self.conn, sentences)
        self.assertEqual(out["気をつける"][0]["s"][0]["g"], ["to be careful"])

    def test_compound_entries_skips_grammar_patterns(self):
        # と+いう joins to という, a real headword — but a run that starts
        # inside a particle chain is a grammar pattern (〜という, 〜に関して),
        # the grammar axis's job, not a lexical unit
        sentences = [{"tokens": [
            {"s": "犬", "l": "犬"}, {"s": "と", "l": "と"},
            {"s": "いう", "l": "いう"}, {"s": "話", "l": "話"}]}]
        self.assertNotIn("という", jmdict.compound_entries(self.conn, sentences))

    def test_compound_entries_rejects_homophones(self):
        # し+ない reads しない = 市内 "within the city"; Sudachi reads 市内 as
        # 市|内 = シ|ナイ, so a reading-sequence match alone admitted it. One
        # content token (する) → glue; and こう+する (adverb + verb) is not
        # 航する (noun + verb) even though both read コウ|スル
        sentences = [{"tokens": [
            {"s": "し", "l": "する"}, {"s": "ない", "l": "ない"},
            {"s": "。", "l": "。"},
            {"s": "こう", "l": "こう"}, {"s": "する", "l": "する"}]}]
        out = jmdict.compound_entries(self.conn, sentences)
        self.assertNotIn("しない", out)
        self.assertNotIn("こうする", out)

    def test_compound_entries_rejects_accidental_concats(self):
        # は+いる joins to はいる — a key for 入る "to enter", but 入る
        # tokenizes to ONE token, not [は, いる]: not this span's word
        sentences = [{"tokens": [
            {"s": "犬", "l": "犬"}, {"s": "は", "l": "は"},
            {"s": "いる", "l": "いる"}]}]
        self.assertNotIn("はいる",
                         jmdict.compound_entries(self.conn, sentences))

    def test_compound_entries_skips_runs_and_junk(self):
        # runs never cross punctuation; all-single-kana runs (aux clusters)
        # are the inflection chain's job, not compounds
        sentences = [{"tokens": [
            {"s": "帝王", "l": "帝王"}, {"s": "。", "l": "。"},
            {"s": "切開", "l": "切開"}, {"s": "た", "l": "た"},
            {"s": "し", "l": "し"}]}]
        self.assertEqual(jmdict.compound_entries(self.conn, sentences), {})

    def test_merge_repair_names_keys_by_surface(self):
        result = {"公園": [{"k": ["公園"], "r": ["こうえん"],
                           "s": [{"pos": ["noun"], "g": ["(public) park"]}]}]}
        repair = {"names": [
            {"surface": "鬼本", "kind": "person", "note": "the curator"},
            {"surface": "公園", "kind": "place", "note": "episode's park"},
            {"surface": "無記", "kind": "person"},  # noteless → skipped
        ]}
        out = jmdict.merge_repair_names(result, repair)
        self.assertEqual(out["鬼本"][0]["s"][0]["g"], ["the curator"])
        self.assertEqual(out["鬼本"][0]["s"][0]["pos"], ["name (person)"])
        self.assertTrue(out["鬼本"][0]["ai"])
        # word JMdict has: name entry prepended, dictionary entry kept
        self.assertEqual(len(out["公園"]), 2)
        self.assertTrue(out["公園"][0]["ai"])
        self.assertNotIn("無記", out)


if __name__ == "__main__":
    unittest.main()
