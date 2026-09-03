"""Pages tests: 5ch URL parsing, thread HTML → posts, the page Stage-1 path
(fetch mocked, coverage real), and the page-aware server routes. Fixture
markup mirrors the live classic read.cgi structure (asahi.5ch.io, 2026-08)."""

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import ledgerctl as lc
from server import jobqueue as q
from server.worker import process_job
from tools import pages as PG
from tools._staging import episode_dir, read_json
from tests.test_server import ServerTestBase

URL = "https://itest.5ch.io/asahi/test/read.cgi/newsplus/1787045314"
PAGE_EP = "page_5ch_newsplus_1787045314"

THREAD_HTML = """
<html><head><title>t</title></head><body>
<h1 id="threadtitle">【テスト】犬が公園で走る  [記者★]
</h1>
<div id="1" data-date="NG" data-userid="ID:abc123" data-id="1" class="clear post">
<div open="" class="post-header"><div><span class="postid">1</span>
<span class="postusername"><b>記者 ★</b></span></div>
<span><span class="date">2026/08/18(火) 18:28:34.19</span>
<span class="uid">ID:abc123</span></span></div>
<div class="post-content"> 犬が走る。公園へ行く。 <br>  <br> 詳しくは
<a href="https://example.com/x" rel="noopener" target="_blank">リンク</a> </div></div>
<div id="2" data-date="NG" data-userid="ID:def456" data-id="2" class="clear post">
<div open="" class="post-header"><div><span class="postid">2</span>
<span class="postusername">名無しどんぶらこ</span></div>
<span><span class="date">2026/08/18(火) 18:29:47.15</span>
<span class="uid">ID:def456</span></span></div>
<div class="post-content"> <a href="../test/read.cgi/newsplus/1787045314/1"
rel="noopener noreferrer" target="_blank" class="reply_link">&gt;&gt;1</a> <br>
猫のほうが速い！ </div></div>
</body></html>
"""


class TestUrlParsing(unittest.TestCase):
    def test_itest_and_classic_forms_agree(self):
        for u in (URL,
                  "https://asahi.5ch.net/test/read.cgi/newsplus/1787045314/",
                  "https://asahi.5ch.io/test/read.cgi/newsplus/1787045314/l50"):
            self.assertEqual(PG.page_episode_id(u), PAGE_EP, u)

    def test_non_page_sources_pass_through(self):
        for u in ("https://www.youtube.com/watch?v=abcDEF12345",
                  "https://itest.5ch.io/subback/newsplus",
                  "/Users/x/video.mp4", ""):
            self.assertIsNone(PG.page_episode_id(u), u)

    def test_derive_job_id_routes_pages(self):
        self.assertEqual(q.derive_job_id(URL), PAGE_EP)
        # different URL forms of the same thread → same job (idempotent enqueue)
        self.assertEqual(
            q.derive_job_id("https://asahi.5ch.net/test/read.cgi/newsplus/1787045314/"),
            PAGE_EP)

    def test_job_dict_kind_from_prefix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conn = q.open_queue(Path(tmp) / "queue.db")
            page_job, _ = q.enqueue(conn, URL)
            vid_job, _ = q.enqueue(conn, "https://youtu.be/abcDEF12345")
            self.assertEqual(page_job["kind"], "page")
            self.assertEqual(vid_job["kind"], "episode")


class TestParseThread(unittest.TestCase):
    def test_posts_and_bodies(self):
        t = PG.parse_thread(THREAD_HTML)
        self.assertEqual(t["title"], "【テスト】犬が公園で走る  [記者★]")
        self.assertEqual(len(t["posts"]), 2)
        p1, p2 = t["posts"]
        self.assertEqual((p1["n"], p1["name"], p1["uid"]),
                         (1, "記者 ★", "abc123"))
        # sentences intact, blank line kept, anchor collapsed to its href
        self.assertEqual(p1["body"].split("\n"),
                         ["犬が走る。公園へ行く。", "",
                          "詳しくは", "https://example.com/x"])
        self.assertEqual(p2["replies_to"], [1])
        self.assertTrue(p2["body"].startswith(">>1"))

    def test_empty_thread_raises(self):
        with self.assertRaises(RuntimeError):
            PG.parse_thread("<html><body>nothing here</body></html>")

    def test_split_sentences(self):
        self.assertEqual(PG.split_sentences("犬が走る。公園へ行く。"),
                         ["犬が走る。", "公園へ行く。"])
        self.assertEqual(PG.split_sentences("ｷﾀ━━━━(ﾟ∀ﾟ)━━━━!!"),
                         ["ｷﾀ━━━━(ﾟ∀ﾟ)━━━━!!"])


class TestPageStage1(ServerTestBase):
    """acquire_page + real coverage over the fixture thread, via the worker."""

    def setUp(self):
        super().setUp()
        # real run_coverage → materialize_known: no Anki sources, no cache
        self.cfg["known_words"] = {"sources": [], "cache_hours": 0}

    def run_page_job(self):
        conn = q.open_queue(queue_db_path_for(self.cfg))
        job, created = q.enqueue(conn, URL)
        self.assertTrue(created)
        with unittest.mock.patch.object(PG, "fetch_thread",
                                        return_value=THREAD_HTML):
            process_job(self.cfg, conn, job, log=lambda m: None)
        return conn, q.get_job(conn, job["id"])

    def test_worker_page_path_lands_staged(self):
        conn, job = self.run_page_job()
        self.assertEqual(job["state"], "staged", job.get("error"))
        self.assertEqual(job["kind"], "page")
        self.assertEqual(job["episode_id"], PAGE_EP)
        self.assertEqual(job["title"], "【テスト】犬が公園で走る  [記者★]")

        ep_dir = episode_dir(self.cfg, PAGE_EP)
        transcript = read_json(ep_dir / "transcript.json")
        coverage = read_json(ep_dir / "coverage.json")
        page = read_json(ep_dir / "page.json")

        # page lines index into the transcript's sentence track
        self.assertEqual(transcript["episode"]["kind"], "page")
        texts = [s["text"] for s in transcript["sentences"]]
        self.assertIn("犬が走る。", texts)
        p1 = page["posts"][0]
        self.assertEqual(p1["lines"][0],
                         [texts.index("犬が走る。"), texts.index("公園へ行く。")])
        self.assertEqual(p1["lines"][1], [])  # blank line preserved as empty run
        self.assertEqual(len(coverage["sentences"]), len(texts))

        # exposures recorded with the page kind (inert until read)
        lconn = lc.open_db(self.cfg["ledger_db"])
        row = lconn.execute("SELECT kind, watched FROM episodes WHERE id=?",
                            (PAGE_EP,)).fetchone()
        self.assertEqual((row["kind"], row["watched"]), ("page", 0))

    def test_page_routes_taps_and_read(self):
        self.run_page_job()

        # GET /page serves the reader structure
        r = self.client.get(f"/page/{PAGE_EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["post_count"], 2)
        self.assertEqual(
            self.client.get("/page/yt_nope", headers=self.auth).status_code, 404)

        # jobs list carries kind so the app can split the tabs
        jobs = self.client.get("/jobs", headers=self.auth).json()
        self.assertEqual(jobs[0]["kind"], "page")

        # taps: ledger evidence lands, but no card selection, no state change
        r = self.client.post("/taps", headers=self.auth, json={
            "episode_id": PAGE_EP, "batch_id": "b1",
            "taps": [["公園", "k"], ["猫", "h"]]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["page"])
        self.assertIsNone(body["cards_selected"])
        self.assertEqual(self.client.get(f"/jobs/{PAGE_EP}", headers=self.auth)
                         .json()["state"], "staged")

        # finished reading: exposures activate, cards skipped even if asked for
        r = self.client.post(f"/watched/{PAGE_EP}", headers=self.auth,
                             json={"cards": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cards"]["note"], "page — no cards")
        self.join_closeout(PAGE_EP)
        self.assertEqual(self.client.get(f"/jobs/{PAGE_EP}", headers=self.auth)
                         .json()["state"], "watched")
        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT watched FROM episodes WHERE id=?", (PAGE_EP,)).fetchone()[0], 1)

        # throwaway: delete removes the files, keeps the read evidence
        r = self.client.delete(f"/jobs/{PAGE_EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(episode_dir(self.cfg, PAGE_EP).exists())
        self.assertIsNotNone(lconn.execute(
            "SELECT id FROM episodes WHERE id=?", (PAGE_EP,)).fetchone())


def queue_db_path_for(cfg):
    return str(Path(cfg["work_dir"]) / "queue.db")


if __name__ == "__main__":
    unittest.main()
