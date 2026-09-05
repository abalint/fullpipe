"""tools.stock — the /autopilot gauge. Pure `compute` over synthetic queue
rows; no ledger, no ffprobe, no network."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import stock

CFG = {"work_dir": "/nonexistent", "ledger_db": "/nonexistent/ledger.db",
       "autopilot": {"min_hours": 10, "target_hours": 14}}


def job(id_, state, secs=None, **extra):
    return {"id": id_, "episode_id": id_, "state": state, "title": id_,
            "error": None, "series": None, **extra}, secs


def build(*specs):
    jobs, durs = [], {}
    for j, secs in specs:
        jobs.append(j)
        if secs is not None:
            durs[j["episode_id"]] = secs
    return jobs, durs


class StockTest(unittest.TestCase):
    def test_youtube_only_and_buckets(self):
        jobs, durs = build(
            job("yt_a", "staged", 3600), job("yt_b", "staged", 1800),
            job("yt_c", "prepared", 3600), job("yt_d", "curating", 1800),
            job("yt_e", "queued", 3600), job("yt_f", "downloading", 3600),
            job("yt_g", "watched", 36000), job("yt_h", "failed", 3600),
            job("ser_show_e01", "staged", 36000, series="show", ep_no=1),
            job("page_5ch_x_1", "staged", 36000), job("local_abc", "staged", 36000))
        rep = stock.compute(CFG, jobs, durations=durs, server_alive=True)
        self.assertEqual(rep["hours"], {"staged": 1.5, "to_curate": 1.5,
                                        "in_flight": 2.0, "pipeline": 5.0})
        self.assertEqual([r["id"] for r in rep["failed"]], ["yt_h"])
        self.assertTrue(rep["verdict"]["curate"])
        self.assertTrue(rep["verdict"]["recommend"])
        self.assertFalse(rep["verdict"]["drain"])
        self.assertEqual(rep["deficit_hours"], 9.0)
        # avg of the 6 counted rows = 3000 s ≈ 0.83 h → ceil(9 / 0.83) = 11 picks
        self.assertEqual(rep["picks_needed"], 11)

    def test_enough_stock_is_a_noop(self):
        jobs, durs = build(job("yt_a", "staged", 11 * 3600), job("yt_c", "prepared", 3600))
        rep = stock.compute(CFG, jobs, durations=durs, server_alive=True)
        self.assertFalse(rep["verdict"]["act"])
        self.assertFalse(rep["verdict"]["curate"])  # gated on the floor by default
        rep = stock.compute(CFG, jobs, durations=durs, server_alive=True,
                            curate_prepared_always=True)
        self.assertTrue(rep["verdict"]["curate"])
        self.assertFalse(rep["verdict"]["recommend"])

    def test_pipeline_in_flight_suppresses_recommend(self):
        jobs, durs = build(job("yt_a", "staged", 2 * 3600), job("yt_e", "queued", 9 * 3600))
        rep = stock.compute(CFG, jobs, durations=durs, server_alive=True)
        self.assertEqual(rep["hours"]["pipeline"], 11.0)
        self.assertFalse(rep["verdict"]["recommend"])
        self.assertEqual(rep["deficit_hours"], 0)

    def test_unknown_duration_uses_average_and_flags_it(self):
        jobs, durs = build(job("yt_a", "staged", 3600), job("yt_b", "staged", 1800),
                           job("yt_e", "queued"))
        probed = []
        rep = stock.compute(CFG, jobs, durations=durs, server_alive=True,
                            probe=lambda ep: probed.append(ep))
        self.assertEqual(probed, ["yt_e"])
        row = rep["in_flight"][0]
        self.assertTrue(row["estimated"])
        self.assertEqual(row["hours"], 0.75)
        self.assertEqual(rep["hours"]["pipeline"], 2.25)

    def test_empty_queue_falls_back_to_default_pick_length(self):
        rep = stock.compute(CFG, [], server_alive=True)
        self.assertEqual(rep["avg_hours_per_pick"], 0.5)
        self.assertEqual(rep["deficit_hours"], 14.0)
        self.assertEqual(rep["picks_needed"], 28)
        self.assertTrue(rep["verdict"]["recommend"])
        self.assertFalse(rep["verdict"]["curate"])

    def test_recommend_cooldown(self):
        now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        rep = stock.compute(CFG, [], server_alive=True, now=now,
                            last_pass=now - timedelta(hours=1))
        self.assertFalse(rep["verdict"]["recommend"])
        self.assertIn("cooldown", rep["verdict"]["reason"])
        rep = stock.compute(CFG, [], server_alive=True, now=now,
                            last_pass=now - timedelta(hours=4))
        self.assertTrue(rep["verdict"]["recommend"])

    def test_drain_only_when_server_down(self):
        jobs, durs = build(job("yt_e", "queued", 3600))
        self.assertTrue(stock.compute(CFG, jobs, durations=durs,
                                      server_alive=False)["verdict"]["drain"])
        self.assertFalse(stock.compute(CFG, jobs, durations=durs,
                                       server_alive=True)["verdict"]["drain"])
        jobs, durs = build(job("yt_f", "downloading", 3600))
        self.assertFalse(stock.compute(CFG, jobs, durations=durs,
                                       server_alive=False)["verdict"]["drain"])

    def test_cli_overrides_beat_config(self):
        jobs, durs = build(job("yt_a", "staged", 11 * 3600))
        rep = stock.compute(CFG, jobs, durations=durs, server_alive=True, min_hours=12)
        self.assertTrue(rep["verdict"]["recommend"])
        self.assertEqual(rep["settings"]["min_hours"], 12)

    def test_brief_line(self):
        rep = stock.compute(CFG, [], server_alive=True)
        self.assertIn("do: recommend", stock.brief(rep))


if __name__ == "__main__":
    unittest.main()
