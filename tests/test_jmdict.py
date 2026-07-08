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
</JMdict>
"""


class TestJmdict(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.count = jmdict.build_db(
            self.conn, jmdict.parse_entries(io.BytesIO(MINI_JMDICT.encode())))

    def test_build_skips_glossless_entries(self):
        self.assertEqual(self.count, 4)  # entry 4 (骨) has no glosses, dropped

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


if __name__ == "__main__":
    unittest.main()
