"""tools/import_tracker_pdf.py — the spreadsheet parser on crafted layout
lines: column splitting, jammed titles, h:mm lengths, date repairs, tab
detection, and the idempotent session ids."""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import import_tracker_pdf as T

LISTENING = """Show                type    # of episodes ~Length in minutes Total timeDate     Total hours daily
Claymore            アニメ                5                  20       100   3/4/25   3.50
Bogus Skill ~long title that runs into the numbers~アニメ10202003/7/253.33
なぜ外国人は日本を去るのか？youtube | 日本語ポッドキャスト1          EP33      23     23  3/8/25
【日本語オタク】youtube 1   @mattvsjapan [thCqYRsS6DY]40404/9/25
podcasts            youtube                1                   100         100     4/9/25
podcasts            youtube                1                   253         253     6/2/25
podcasts            youtube                1                   100         100     4/11/25
podcasts            youtube                1                   150         150     4/7/25
misc                misc                   1                  8:34         514     5/4/25   8.57
misc                                       0                  5:25         325     5/5/25
misc                                       0                  7:08         428     5/5/25
misc                                       0                  6:49         409     5/5/25
misc                                       0                                 0     5/5/25
"""
PASSIVE = """Title              # of episodes       ~Length in minutesTotal time            Date
big bang theory                     6                  20                120              3/4/25
misc                                                                     315             9/22/25
"""
OTHER = """# of reps          Time Taken (minutes)Date
               89               5.29              3/5/25
"""


class ImportTrackerTest(unittest.TestCase):
    def test_parse_and_repair(self):
        tabs = T.parse_pages([LISTENING, OTHER, PASSIVE])
        lis = tabs["listening"]
        self.assertEqual([(r["title"][:6], r["min"]) for r in lis], [
            ("Claymo", 100.0),
            ("Bogus ", 200.0),   # jammed 10·20·200 untangled
            ("なぜ外国人は", 23.0),  # wrapped title fragment + 'EP33' ignored
            ("【日本語オタ", 40.0),   # '4040' = 40 written twice
            ("podcas", 100.0),
            ("podcas", 253.0),
            ("podcas", 100.0),
            ("podcas", 150.0),
            ("misc", 514.0),     # h:mm length column is not a count
            ("misc", 325.0),
            ("misc", 428.0),
            ("misc", 409.0),
        ])
        days = [r["day"].isoformat() for r in lis]
        self.assertEqual(days[4:8], [
            "2025-04-09",
            "2025-04-10",  # 6/2/25 typed amid April → the day after the previous row
            "2025-04-11",
            "2025-04-12",  # 4/7/25 out of order in the daily era → day after prev
        ])
        self.assertEqual(days[-3:], ["2025-05-05", "2025-05-06", "2025-05-07"])  # stale tail dates walk forward
        self.assertEqual([bool(r.get("repaired")) for r in lis][4:], [False, True, False, True, False, False, True, True])
        self.assertEqual(lis[0]["type"], "アニメ")
        self.assertEqual(lis[0]["eps"], 5.0)
        # the Anki-reps page is not a time tab; the passive tab is
        self.assertEqual([r["min"] for r in tabs["passive"]], [120.0, 315.0])
        self.assertEqual(tabs["passive"][1]["day"].isoformat(), "2025-09-22")

    def test_sessions_are_deterministic_and_typed(self):
        tabs = T.parse_pages([LISTENING, PASSIVE])
        a = T.to_sessions(tabs)
        b = T.to_sessions(T.parse_pages([LISTENING, PASSIVE]))
        self.assertEqual([s["id"] for s in a], [s["id"] for s in b])
        self.assertEqual(len(set(s["id"] for s in a)), len(a))
        self.assertEqual(a[0]["kind"], "watch")
        self.assertEqual(a[0]["title"], "Claymore (アニメ)")
        self.assertEqual(a[0]["secs"], 6000.0)
        self.assertEqual(a[0]["source"], "import")
        self.assertEqual(a[-1]["kind"], "listen")
        self.assertEqual(a[-1]["title"], "misc")


if __name__ == "__main__":
    unittest.main()
