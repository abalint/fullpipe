"""Server tests: queue semantics, the HTTP routes over a temp workspace, and
the worker's Stage-2 artifact detection. No network, no ASR — artifacts are
written directly, the way test_tools.py stages fixtures."""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from ledger import ledgerctl as lc
from server import jobqueue as q
from server.app import create_app, queue_db_path
from server.worker import scan_stage2
from tools._staging import episode_dir, write_json

EP = "yt_testvideo12"

TRANSCRIPT = {
    "episode": {"id": EP, "title": "テスト動画", "uploader": "u", "source": "x",
                "kind": "youtube", "audio": "a.mp3", "raw_srt": "r.srt"},
    "sentences": [
        {"idx": 0, "start": 0.0, "end": 2.0, "text": "犬が走る。"},
        {"idx": 1, "start": 2.0, "end": 4.0, "text": "公園へ行く。"},
    ],
}

COVERAGE = {
    "episode_id": EP,
    "stats": {"comprehensible": 1, "reinforcement": 0, "i_plus_1": 1,
              "too_hard": 0, "total_sentences": 2,
              "token_comprehensibility": 0.8, "known_set_size": 5,
              "learning_size": 0, "unknown_lemmas": 1,
              "candidates_dropped_by_cap": 0},
    "sentences": [
        {"idx": 0, "start": 0.0, "end": 2.0, "text": "犬が走る。",
         "classification": "comprehensible", "known_ratio": 1.0, "unknown": [],
         "tokens": [{"s": "犬", "l": "犬", "r": "いぬ", "c": True, "k": True}]},
        {"idx": 1, "start": 2.0, "end": 4.0, "text": "公園へ行く。",
         "classification": "i_plus_1", "known_ratio": 0.8, "unknown": ["公園"],
         "tokens": [{"s": "公園", "l": "公園", "r": "こうえん", "c": True, "k": False}]},
    ],
    "candidates": [{
        "lemma": "公園", "reading": "こうえん", "surface": "公園", "pos": "名詞",
        "freq_rank": 120, "recurrence": 1, "leverage": None,
        "best": {"sentence_idx": 1, "other_unknown_count": 0,
                 "start": 2.0, "end": 4.0, "text": "公園へ行く。"},
    }],
    "reinforcement": [],
    "exposures": {"公園": {"sentence_idx": 1, "known_ratio": 0.8,
                          "other_unknown_count": 0,
                          "reading": "こうえん", "pos": "名詞"}},
}


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = Path(self.tmp.name)
        self.cfg = {
            "work_dir": str(work),
            "ledger_db": str(work / "ledger.db"),
            "anki_connect_url": "http://localhost:1",  # nothing listens — poll must degrade
            "server": {"token": "sekrit"},
        }
        self.client = TestClient(create_app(self.cfg, start_worker=False))
        self.auth = {"Authorization": "Bearer sekrit"}

    def tearDown(self):
        self.tmp.cleanup()

    def stage_episode(self, with_curate=False):
        ep_dir = episode_dir(self.cfg, EP, create=True)
        write_json(ep_dir / "transcript.json", TRANSCRIPT)
        write_json(ep_dir / "coverage.json", COVERAGE)
        (ep_dir / "sentences.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n犬が走る。\n", encoding="utf-8")
        if with_curate:
            write_json(ep_dir / "curate.json", {
                "synopsis": "犬の話。",
                "keywords": [{"word": "公園", "gloss": "park", "note": "公園へ行く"}],
                "focal_points": [], "exclude": [],
            })
            (ep_dir / "prep.html").write_text("<html></html>", encoding="utf-8")
        # taps/watched need the episode in the ledger (record-exposure does this live)
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.record_exposure(conn, TRANSCRIPT["episode"], COVERAGE["exposures"])
        conn.close()
        return ep_dir


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = q.open_queue(Path(self.tmp.name) / "queue.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_derive_youtube_id_matches_acquire(self):
        self.assertEqual(q.derive_job_id("https://www.youtube.com/watch?v=abcDEF12345"),
                         "yt_abcDEF12345")
        self.assertEqual(q.derive_job_id("https://youtu.be/abcDEF12345"),
                         "yt_abcDEF12345")

    def test_enqueue_idempotent(self):
        j1, created1 = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        j2, created2 = q.enqueue(self.conn, "https://www.youtube.com/watch?v=abcDEF12345")
        self.assertTrue(created1)
        self.assertFalse(created2)  # same video, different URL form → same job
        self.assertEqual(j1["id"], j2["id"])
        self.assertEqual(len(q.list_jobs(self.conn)), 1)

    def test_reenqueue_failed_resets_to_queued(self):
        job, _ = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        q.set_state(self.conn, job["id"], "failed", error="boom")
        j2, created = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        self.assertFalse(created)
        self.assertEqual(j2["state"], "queued")
        self.assertIsNone(j2["error"])

    def test_reenqueue_completed_is_noop(self):
        job, _ = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        q.set_state(self.conn, job["id"], "staged")
        j2, created = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        self.assertFalse(created)
        self.assertEqual(j2["state"], "staged")

    def test_delete_job_drops_row(self):
        job, _ = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        self.assertTrue(q.delete_job(self.conn, job["id"]))
        self.assertIsNone(q.get_job(self.conn, job["id"]))
        self.assertFalse(q.delete_job(self.conn, job["id"]))  # already gone

    def test_episode_id_falls_back_to_job_id(self):
        job, _ = q.enqueue(self.conn, "https://example.com/some/episode")
        self.assertTrue(job["id"].startswith("src_"))
        self.assertEqual(job["episode_id"], job["id"])  # best-known id pre-acquire
        q.set_state(self.conn, job["id"], "prepared", episode_id=EP)
        self.assertEqual(q.get_job(self.conn, EP)["episode_id"], EP)


class TestRoutes(ServerTestBase):
    def test_auth_required(self):
        self.assertEqual(self.client.get("/jobs").status_code, 401)
        self.assertEqual(self.client.get("/health").status_code, 200)  # liveness stays open

    def test_enqueue_and_read(self):
        r = self.client.post("/jobs", json={"source": "https://youtu.be/abcDEF12345"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()["created"])
        job_id = r.json()["id"]
        self.assertEqual(self.client.get("/jobs", headers=self.auth).json()[0]["id"], job_id)
        self.assertEqual(self.client.get(f"/jobs/{job_id}", headers=self.auth)
                         .json()["state"], "queued")
        self.assertEqual(self.client.post("/jobs", json={}, headers=self.auth)
                         .status_code, 422)

    def test_curate_requires_prepared(self):
        r = self.client.post("/jobs", json={"source": "https://youtu.be/abcDEF12345"},
                             headers=self.auth)
        job_id = r.json()["id"]
        self.assertEqual(self.client.post(f"/jobs/{job_id}/curate", headers=self.auth)
                         .status_code, 409)
        q.open_queue(queue_db_path(self.cfg))  # separate handle is fine
        conn = q.open_queue(queue_db_path(self.cfg))
        q.set_state(conn, job_id, "prepared", episode_id=EP)
        r = self.client.post(f"/jobs/{job_id}/curate", headers=self.auth)
        self.assertEqual(r.json()["state"], "curating")

    def test_jobs_annotated_with_duration(self):
        # before Stage 1 there is nothing to measure
        r = self.client.post("/jobs", json={"source": "https://youtu.be/testvideo12"},
                             headers=self.auth)
        job_id = r.json()["id"]
        self.assertEqual(job_id, EP)
        self.assertIsNone(self.client.get("/jobs", headers=self.auth).json()[0]["duration"])
        # once the transcript exists its last timestamp is the fallback runtime
        # (the staged video.mp4, when present, is ffprobed instead)
        self.stage_episode()
        self.assertEqual(self.client.get("/jobs", headers=self.auth)
                         .json()[0]["duration"], 4.0)
        self.assertEqual(self.client.get(f"/jobs/{job_id}", headers=self.auth)
                         .json()["duration"], 4.0)

    def test_prep_serves_render_payload(self):
        self.stage_episode(with_curate=True)
        r = self.client.get(f"/prep/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["episode"]["id"], EP)
        self.assertEqual(data["glossary"][0]["lemma"], "公園")
        self.assertEqual(data["curate"]["synopsis"], "犬の話。")
        self.assertEqual(len(data["iplus1"]), 1)
        self.assertEqual(self.client.get("/prep/nope", headers=self.auth).status_code, 404)

    def test_transcript_serves_all_sentences(self):
        # /prep ships only the i+1/reinforcement subset; the player's subtitle
        # overlay needs every sentence with timing + tokens
        self.stage_episode()
        r = self.client.get(f"/transcript/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["episode_id"], EP)
        self.assertEqual(len(data["sentences"]), 2)
        self.assertEqual(data["sentences"][0]["start"], 0.0)
        self.assertEqual(data["sentences"][1]["end"], 4.0)
        self.assertEqual(data["sentences"][1]["tokens"][0]["l"], "公園")
        self.assertEqual(self.client.get("/transcript/nope", headers=self.auth)
                         .status_code, 404)
        self.assertEqual(self.client.get(f"/transcript/{EP}").status_code, 401)

    def test_definitions_for_episode_lemmas(self):
        self.stage_episode()
        # dictionary not built yet → graceful empty map, not an error
        r = self.client.get(f"/definitions/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {})
        # build a one-entry dict db in the workspace, ask again
        import sqlite3

        from tools import jmdict
        conn = sqlite3.connect(jmdict.db_path(self.cfg))
        jmdict.build_db(conn, iter([
            (2, {"公園", "こうえん"},
             {"k": ["公園"], "r": ["こうえん"],
              "s": [{"pos": ["noun"], "g": ["(public) park"]}]}),
        ]))
        conn.close()
        data = self.client.get(f"/definitions/{EP}", headers=self.auth).json()
        self.assertIn("公園", data)  # content lemma with an entry
        self.assertNotIn("犬", data)  # content lemma, no dict entry
        self.assertEqual(data["公園"][0]["s"][0]["g"], ["(public) park"])
        self.assertEqual(self.client.get("/definitions/nope", headers=self.auth)
                         .status_code, 404)
        self.assertEqual(self.client.get(f"/definitions/{EP}").status_code, 401)

    def test_video_and_subs(self):
        ep_dir = self.stage_episode()
        self.assertEqual(self.client.get(f"/video/{EP}", headers=self.auth)
                         .status_code, 404)
        (ep_dir / "video.mp4").write_bytes(b"\x00" * 64)
        r = self.client.get(f"/video/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        # range support is what makes phone streaming/seek work
        r = self.client.get(f"/video/{EP}", headers={**self.auth, "Range": "bytes=0-9"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(len(r.content), 10)
        r = self.client.get(f"/video/{EP}/subs", headers=self.auth)
        self.assertIn("犬が走る", r.text)

    def test_taps_are_prewatch_feedback(self):
        self.stage_episode()
        payload = {"episode_id": EP, "batch_id": "b" * 16,
                   "taps": [["公園", "h"], ["犬", "k"]]}
        r = self.client.post("/taps", json=payload, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["applied"], 1)  # only "k" is ledger evidence
        self.assertIn("promote", body)
        self.assertIsNotNone(body["cards_selected"])

        # replay of the same batch_id is a dedup no-op
        r2 = self.client.post("/taps", json=payload, headers=self.auth)
        self.assertTrue(r2.json()["duplicate"])
        self.assertEqual(r2.json()["applied"], 0)

        conn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(conn.execute(
            "SELECT status FROM lemmas WHERE lemma='犬'").fetchone()[0], "known")
        # feedback does NOT imply watched — watching is its own later step
        self.assertEqual(conn.execute(
            "SELECT watched FROM episodes WHERE id=?", (EP,)).fetchone()[0], 0)

    def test_feedback_then_watched_flow(self):
        """The 2026-07-05 workflow: review → feedback selects cards →
        watch → mark-watched pushes them."""
        ep_dir = self.stage_episode(with_curate=True)
        # curate authored a pool of two; user says they know 公園 and want 犬
        write_json(ep_dir / "picks.json", [
            {"lemma": "公園", "sentence_idx": 1, "reading": "こうえん", "english": "park"},
            {"lemma": "犬", "sentence_idx": 0, "reading": "いぬ", "english": "dog"},
        ])
        r = self.client.post("/taps", json={
            "episode_id": EP, "batch_id": "d" * 16,
            "taps": [["公園", "k"], ["犬", "h"]]}, headers=self.auth)
        self.assertEqual(r.json()["cards_selected"], 1)  # 公園 pruned, 犬 kept
        final = json.loads((ep_dir / "final_picks.json").read_text(encoding="utf-8"))
        self.assertEqual([p["lemma"] for p in final], ["犬"])

        # mark watched: exposures activate; card push attempted (Anki is down
        # here — watched must still stand and the error must surface)
        r = self.client.post(f"/watched/{EP}", headers=self.auth)
        body = r.json()
        self.assertTrue(body["watched"])
        self.assertEqual(body["cards"]["pushed"], 0)
        self.assertIn("error", body["cards"])
        conn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(conn.execute(
            "SELECT watched FROM episodes WHERE id=?", (EP,)).fetchone()[0], 1)

    def test_taps_reconciles_job(self):
        self.stage_episode()
        conn = q.open_queue(queue_db_path(self.cfg))
        job, _ = q.enqueue(conn, "https://youtu.be/testvideo12")
        q.set_state(conn, job["id"], "staged", episode_id=EP)
        self.client.post("/taps", json={"episode_id": EP, "batch_id": "c" * 16,
                                        "taps": [["公園", "k"]]}, headers=self.auth)
        self.assertEqual(q.get_job(conn, EP)["state"], "reconciled")

    def test_watched_without_feedback(self):
        self.stage_episode()
        r = self.client.post(f"/watched/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["watched"])
        self.assertEqual(r.json()["cards"]["pushed"], 0)  # nothing staged to push
        self.assertEqual(self.client.post("/watched/ghost", headers=self.auth)
                         .status_code, 404)

    def test_watched_skip_cards(self):
        """The disliked-it branch: {"cards": false} activates exposures and
        closes the job without touching the deck, even with picks staged."""
        ep_dir = self.stage_episode(with_curate=True)
        write_json(ep_dir / "picks.json", [
            {"lemma": "公園", "sentence_idx": 1, "reading": "こうえん", "english": "park"},
        ])
        conn, _ = self._enqueue_at("staged")
        r = self.client.post(f"/watched/{EP}", json={"cards": False},
                             headers=self.auth)
        body = r.json()
        self.assertTrue(body["watched"])
        self.assertEqual(body["cards"]["pushed"], 0)
        # Anki is down in this suite, so a push attempt would surface an
        # error — its absence proves the push was skipped, not just failed
        self.assertNotIn("error", body["cards"])
        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT watched FROM episodes WHERE id=?", (EP,)).fetchone()[0], 1)
        self.assertEqual(q.get_job(conn, EP)["state"], "watched")

    def test_rating_roundtrip(self):
        """Rate over HTTP → ledger row; job listings carry the stars back."""
        self.stage_episode()
        conn, _ = self._enqueue_at("watched")

        r = self.client.post(f"/episodes/{EP}/rating",
                             json={"rating": 4, "tags": ["fascinating"]},
                             headers=self.auth)
        body = r.json()
        self.assertEqual(body["rating"], 4)
        self.assertEqual(body["tags"], ["fascinating"])
        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT rating FROM episodes WHERE id=?", (EP,)).fetchone()[0], 4)

        # annotated on both job reads + the coverage report (with the verdict)
        jobs = self.client.get("/jobs", headers=self.auth).json()
        self.assertEqual([j["rating"] for j in jobs if j["episode_id"] == EP], [4])
        self.assertEqual(self.client.get(f"/jobs/{EP}", headers=self.auth)
                         .json()["rating"], 4)
        cov = self.client.get("/coverage", headers=self.auth).json()
        rating_row = next(e for e in cov["ratings"] if e["id"] == EP)
        self.assertEqual(rating_row["rating"], 4)
        self.assertEqual(rating_row["tags"], ["fascinating"])
        self.assertTrue(rating_row["taste_valid"])

        # re-rate overwrites; null clears
        self.client.post(f"/episodes/{EP}/rating", json={"rating": 1}, headers=self.auth)
        self.assertEqual(self.client.get(f"/jobs/{EP}", headers=self.auth)
                         .json()["rating"], 1)
        r = self.client.post(f"/episodes/{EP}/rating", json={"rating": None},
                             headers=self.auth)
        self.assertIsNone(r.json()["rating"])
        self.assertIsNone(self.client.get(f"/jobs/{EP}", headers=self.auth)
                          .json()["rating"])

    def test_rating_rejects_bad_input(self):
        self.stage_episode()
        for bad in (0, 6, "great"):
            r = self.client.post(f"/episodes/{EP}/rating", json={"rating": bad},
                                 headers=self.auth)
            self.assertEqual(r.status_code, 422, f"rating {bad!r} should 422")
        self.assertEqual(self.client.post("/episodes/ghost/rating",
                                          json={"rating": 3}, headers=self.auth)
                         .status_code, 404)

    def _enqueue_at(self, state):
        conn = q.open_queue(queue_db_path(self.cfg))
        job, _ = q.enqueue(conn, "https://youtu.be/testvideo12")
        q.set_state(conn, job["id"], state, episode_id=EP)
        return conn, job

    def test_delete_unwatched_purges_everything(self):
        ep_dir = self.stage_episode(with_curate=True)
        (ep_dir / "video.mp4").write_bytes(b"\x00" * 16)
        # a cached source download for this video id must go too
        dl = Path(self.cfg["work_dir"]) / "downloads"
        dl.mkdir(exist_ok=True)
        (dl / f"{EP[3:]}.mp3").write_bytes(b"\x00")
        conn, job = self._enqueue_at("staged")
        # pre-watch known-tap → episode has evidence beyond the inert exposures
        self.client.post("/taps", json={"episode_id": EP, "batch_id": "e" * 16,
                                        "taps": [["公園", "k"]]}, headers=self.auth)

        r = self.client.request("DELETE", f"/jobs/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ledger"]["purged"])
        self.assertFalse(ep_dir.exists())
        self.assertEqual(list(dl.iterdir()), [])
        self.assertIsNone(q.get_job(conn, EP))
        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT COUNT(*) FROM evidence WHERE episode_id=?", (EP,)).fetchone()[0], 0)
        self.assertIsNone(lconn.execute(
            "SELECT id FROM episodes WHERE id=?", (EP,)).fetchone())
        # 公園's only evidence was this episode — projection row gone too
        self.assertIsNone(lconn.execute(
            "SELECT lemma FROM lemmas WHERE lemma='公園'").fetchone())

    def test_delete_unwatched_rated_keeps_rating(self):
        """Rate-then-discard: the dislike survives as a rating-only tombstone
        while the knowledge side (evidence, lemmas) fully unwinds."""
        ep_dir = self.stage_episode(with_curate=True)
        conn, _ = self._enqueue_at("staged")
        self.client.post(f"/episodes/{EP}/rating", json={"rating": 1},
                         headers=self.auth)

        r = self.client.request("DELETE", f"/jobs/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ledger"]["purged"])
        self.assertTrue(r.json()["ledger"]["rating_retained"])
        self.assertFalse(ep_dir.exists())
        self.assertIsNone(q.get_job(conn, EP))
        lconn = lc.open_db(self.cfg["ledger_db"])
        row = lconn.execute(
            "SELECT watched, rating FROM episodes WHERE id=?", (EP,)).fetchone()
        self.assertFalse(row["watched"])
        self.assertEqual(row["rating"], 1)
        self.assertEqual(lconn.execute(
            "SELECT COUNT(*) FROM evidence WHERE episode_id=?", (EP,)).fetchone()[0], 0)
        self.assertIsNone(lconn.execute(
            "SELECT lemma FROM lemmas WHERE lemma='公園'").fetchone())

    def test_delete_watched_retains_ledger(self):
        ep_dir = self.stage_episode()
        conn, job = self._enqueue_at("staged")
        self.client.post(f"/watched/{EP}", headers=self.auth)  # full pipeline

        r = self.client.request("DELETE", f"/jobs/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ledger"]["purged"])
        self.assertFalse(ep_dir.exists())  # artifacts still go
        self.assertIsNone(q.get_job(conn, EP))  # queue row still goes
        lconn = lc.open_db(self.cfg["ledger_db"])
        # earned history stays: watched episode row + activated exposures
        self.assertEqual(lconn.execute(
            "SELECT watched FROM episodes WHERE id=?", (EP,)).fetchone()[0], 1)
        self.assertGreater(lconn.execute(
            "SELECT COUNT(*) FROM evidence WHERE episode_id=?", (EP,)).fetchone()[0], 0)

    def test_delete_running_job_conflicts(self):
        conn, job = self._enqueue_at("downloading")
        r = self.client.request("DELETE", f"/jobs/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.request("DELETE", "/jobs/ghost", headers=self.auth)
                         .status_code, 404)

    def test_coverage_query(self):
        self.stage_episode()
        r = self.client.get("/coverage", headers=self.auth)
        self.assertEqual(r.json()["summary"]["episodes"]["total"], 1)
        self.assertIn("needs_review", r.json())


class TestSelect(unittest.TestCase):
    """Pure selection rules: known prunes, interest jumps the queue, cap holds."""

    POOL = [{"lemma": w, "sentence_idx": i, "reading": "", "english": w}
            for i, w in enumerate(["a", "b", "c", "d"])]
    COV = {"candidates": [
        {"lemma": "e", "reading": "え",
         "best": {"sentence_idx": 9, "other_unknown_count": 0,
                  "start": 1.0, "end": 4.0, "text": "…"}},
        {"lemma": "f", "reading": "ふ",
         "best": {"sentence_idx": 10, "other_unknown_count": 3,
                  "start": 1.0, "end": 4.0, "text": "…"}},
    ]}

    def test_known_pruned_interest_first(self):
        from tools.select import select_picks
        final = select_picks(self.POOL, self.COV,
                             [["a", "k"], ["c", "h"]], cap=15)
        self.assertEqual([p["lemma"] for p in final], ["c", "b", "d"])

    def test_cap_and_rescue(self):
        from tools.select import select_picks
        # e is rescuable (0 other unknowns, sane clip); f is not (3 others)
        final = select_picks(self.POOL, self.COV,
                             [["e", "h"], ["f", "h"]], cap=3)
        lemmas = [p["lemma"] for p in final]
        self.assertEqual(lemmas[0], "e")
        self.assertTrue(final[0].get("rescued"))
        self.assertEqual(len(final), 3)
        self.assertNotIn("f", lemmas)

    def test_no_feedback_keeps_pool_order(self):
        from tools.select import select_picks
        final = select_picks(self.POOL, self.COV, [], cap=2)
        self.assertEqual([p["lemma"] for p in final], ["a", "b"])


class TestWorkerDrain(ServerTestBase):
    """drain() — the /prepare skill's one-shot local Stage 1. acquire /
    stage_video / run_coverage are patched: queue semantics are under test,
    not the pipeline (test_tools.py owns that)."""

    URL = "https://youtu.be/testvideo12"

    def setUp(self):
        super().setUp()
        self.conn = q.open_queue(queue_db_path(self.cfg))
        self.record = {"episode": {"id": EP, "title": "テスト動画",
                                   "kind": "youtube"}}

    def patched(self, acquire=None, **extra):
        import server.worker as w
        patches = {
            "acquire": acquire or (lambda src, cfg, log=None: self.record),
            "stage_video": lambda cfg, src, rec, log=None: None,
            "run_coverage": lambda cfg, ep: None,
            **extra,
        }
        ctxs = [unittest.mock.patch.object(w, k, v) for k, v in patches.items()]
        return ctxs

    def drain(self, sources=(), **kw):
        from server.worker import drain
        ctxs = self.patched(**kw)
        with ctxs[0], ctxs[1], ctxs[2]:
            return drain(self.cfg, self.conn, sources, log=lambda m: None)

    def test_enqueues_and_prepares(self):
        summary = self.drain([self.URL])
        self.assertEqual(summary["enqueued"], ["yt_testvideo12"])
        self.assertEqual(summary["prepared"], ["yt_testvideo12"])
        job = q.get_job(self.conn, EP)
        self.assertEqual(job["state"], "prepared")
        self.assertEqual(job["title"], "テスト動画")

    def test_drains_preexisting_queue_without_sources(self):
        q.enqueue(self.conn, self.URL)
        summary = self.drain()
        self.assertEqual(summary["enqueued"], [])
        self.assertEqual(summary["prepared"], ["yt_testvideo12"])

    def test_acquire_failure_lands_in_failed(self):
        def boom(src, cfg, log=None):
            raise RuntimeError("yt-dlp exploded")
        summary = self.drain([self.URL], acquire=boom)
        self.assertEqual(summary["failed"], ["yt_testvideo12"])
        job = q.get_job(self.conn, "yt_testvideo12")
        self.assertEqual(job["state"], "failed")
        self.assertIn("yt-dlp exploded", job["error"])

    def test_reenqueue_failed_source_retries(self):
        job, _ = q.enqueue(self.conn, self.URL)
        q.set_state(self.conn, job["id"], "failed", error="boom")
        summary = self.drain([self.URL])
        self.assertEqual(summary["prepared"], ["yt_testvideo12"])
        self.assertEqual(q.get_job(self.conn, EP)["state"], "prepared")

    def test_done_job_not_rerun(self):
        job, _ = q.enqueue(self.conn, self.URL)
        q.set_state(self.conn, job["id"], "staged", episode_id=EP)
        summary = self.drain([self.URL])
        self.assertEqual(summary["skipped"], ["yt_testvideo12"])
        self.assertEqual(summary["prepared"], [])
        self.assertEqual(q.get_job(self.conn, EP)["state"], "staged")


class TestWorkerScan(ServerTestBase):
    def test_stage2_artifacts_flip_to_staged(self):
        conn = q.open_queue(queue_db_path(self.cfg))
        job, _ = q.enqueue(conn, "https://youtu.be/testvideo12")
        q.set_state(conn, job["id"], "prepared", episode_id=EP, title="テスト動画")

        self.stage_episode(with_curate=False)
        scan_stage2(self.cfg, conn, log=lambda m: None)
        self.assertEqual(q.get_job(conn, EP)["state"], "prepared")  # not yet

        self.stage_episode(with_curate=True)
        scan_stage2(self.cfg, conn, log=lambda m: None)
        job = q.get_job(conn, EP)
        self.assertEqual(job["state"], "staged")
        self.assertEqual(job["title"], "テスト動画")


if __name__ == "__main__":
    unittest.main()
