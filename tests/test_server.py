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
        self.app = create_app(self.cfg, start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer sekrit"}

    def tearDown(self):
        self.join_closeout()  # never let a close-out outlive its temp dir
        self.tmp.cleanup()

    def join_closeout(self, episode_id=EP):
        """POST /watched hands the card push to a background thread — wait
        for it so assertions see the terminal state."""
        t = self.app.state.closeouts.get(episode_id)
        if t:
            t.join(timeout=30)

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

    def test_passive_flag_roundtrip(self):
        job, _ = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        self.assertIs(job["passive"], False)  # default, as a real bool
        q.set_passive(self.conn, job["id"], True)
        self.assertIs(q.get_job(self.conn, job["id"])["passive"], True)
        q.set_passive(self.conn, job["id"], False)
        self.assertIs(q.get_job(self.conn, job["id"])["passive"], False)

    def test_passive_column_migrates_old_db(self):
        # a queue.db from before the passive column: open_queue must ALTER it in
        import sqlite3
        db = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, episode_id TEXT, "
            "source TEXT NOT NULL, title TEXT, state TEXT NOT NULL DEFAULT 'queued', "
            "progress_msg TEXT, error TEXT, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL);")
        conn.execute("INSERT INTO jobs (id, source, created_at, updated_at) "
                     "VALUES ('yt_x', 's', 't', 't')")
        conn.commit()
        conn.close()
        migrated = q.open_queue(db)
        self.assertIs(q.get_job(migrated, "yt_x")["passive"], False)

    def test_episode_id_falls_back_to_job_id(self):
        job, _ = q.enqueue(self.conn, "https://example.com/some/episode")
        self.assertTrue(job["id"].startswith("src_"))
        self.assertEqual(job["episode_id"], job["id"])  # best-known id pre-acquire
        q.set_state(self.conn, job["id"], "prepared", episode_id=EP)
        self.assertEqual(q.get_job(self.conn, EP)["episode_id"], EP)

    def test_reap_stale_reclaims_stage1_and_pushing(self):
        # a crash mid-flight strands jobs in states DELETE also refuses — the
        # reaper must free them so the executor and the phone can proceed.
        stage1, _ = q.enqueue(self.conn, "https://youtu.be/aaaaaaaaaaa")
        q.set_state(self.conn, stage1["id"], "transcribing", error=None)
        pushing, _ = q.enqueue(self.conn, "https://youtu.be/bbbbbbbbbbb")
        q.set_state(self.conn, pushing["id"], "pushing")
        untouched, _ = q.enqueue(self.conn, "https://youtu.be/ccccccccccc")
        q.set_state(self.conn, untouched["id"], "staged")

        reaped = q.reap_stale(self.conn)
        self.assertEqual(set(reaped), {stage1["id"], pushing["id"]})
        self.assertEqual(q.get_job(self.conn, stage1["id"])["state"], "queued")
        pj = q.get_job(self.conn, pushing["id"])
        self.assertEqual(pj["state"], "watched")  # mark_watched already ran
        self.assertIn("retry", pj["error"])
        self.assertEqual(q.get_job(self.conn, untouched["id"])["state"], "staged")
        self.assertEqual(q.reap_stale(self.conn), [])  # idempotent

    def test_retry_job_requeues_only_failed(self):
        job, _ = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        q.set_state(self.conn, job["id"], "failed", error="boom")
        updated = q.retry_job(self.conn, job["id"])
        self.assertEqual(updated["state"], "queued")
        self.assertIsNone(updated["error"])
        # a non-failed job (or a ghost) is not retryable
        q.set_state(self.conn, job["id"], "staged")
        self.assertIsNone(q.retry_job(self.conn, job["id"]))
        self.assertIsNone(q.retry_job(self.conn, "ghost"))


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

    def test_passive_requires_watched(self):
        r = self.client.post("/jobs", json={"source": "https://youtu.be/abcDEF12345"},
                             headers=self.auth)
        job_id = r.json()["id"]
        # not watched yet → refused
        self.assertEqual(self.client.post(f"/jobs/{job_id}/passive", json={},
                                          headers=self.auth).status_code, 409)
        conn = q.open_queue(queue_db_path(self.cfg))
        q.set_state(conn, job_id, "watched")
        r = self.client.post(f"/jobs/{job_id}/passive", json={}, headers=self.auth)
        self.assertIs(r.json()["passive"], True)
        self.assertIs(self.client.get("/jobs", headers=self.auth)
                      .json()[0]["passive"], True)
        # un-shelving is always allowed
        r = self.client.post(f"/jobs/{job_id}/passive", json={"passive": False},
                             headers=self.auth)
        self.assertIs(r.json()["passive"], False)

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
        # a freq row so the corpus-rank enrichment has something to attach
        conn = lc.open_db(self.cfg["ledger_db"])
        conn.execute("INSERT INTO freq (lemma, rank, source) VALUES ('公園', 120, 'show_graph')")
        conn.commit()
        conn.close()
        r = self.client.get(f"/transcript/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["episode_id"], EP)
        self.assertEqual(len(data["sentences"]), 2)
        self.assertEqual(data["sentences"][0]["start"], 0.0)
        self.assertEqual(data["sentences"][1]["end"], 4.0)
        self.assertEqual(data["sentences"][1]["tokens"][0]["l"], "公園")
        # highlight-tier enrichment: classification, corpus rank, candidates
        self.assertEqual(data["sentences"][0]["cls"], "comprehensible")
        self.assertEqual(data["sentences"][1]["cls"], "i_plus_1")
        self.assertEqual(data["sentences"][1]["tokens"][0]["f"], 120)
        self.assertNotIn("f", data["sentences"][0]["tokens"][0])  # 犬 has no rank
        self.assertEqual(data["candidates"], ["公園"])
        self.assertEqual(self.client.get("/transcript/nope", headers=self.auth)
                         .status_code, 404)
        self.assertEqual(self.client.get(f"/transcript/{EP}").status_code, 401)
        # pre-curation: no annotation keys at all, and flagged uncurated so
        # the app knows to refresh its sidecar once curation lands
        self.assertNotIn("grammar", data["sentences"][0])
        self.assertNotIn("phrases", data["sentences"][0])
        self.assertFalse(data["curated"])

    def test_transcript_carries_curated_grammar_and_phrases(self):
        # the player popup's line context (GRAMMAR.md): curate.json grammar/
        # phrase tags ride on their sentence; proposals are flagged
        ep_dir = self.stage_episode(with_curate=True)
        write_json(ep_dir / "curate.json", {
            "synopsis": "犬の話。",
            "keywords": [], "focal_points": [], "exclude": [],
            "grammar": [
                {"sentence_idx": 1, "pattern": "〜てしまう",
                 "form_note": "行っちゃった = 行く+てしまう",
                 "classification": "i_plus_1"},
                {"sentence_idx": 1, "proposed_pattern": "ら抜き言葉",
                 "gloss": "見られる→見れる", "example": "見れて"},
                {"pattern": "〜てくる"},  # no sentence_idx — dropped, not a 500
            ],
            "phrases": [
                {"sentence_idx": 0, "surface": "気を付けて",
                 "canonical": "気を付ける", "classification": "comprehensible"},
            ],
        })
        data = self.client.get(f"/transcript/{EP}", headers=self.auth).json()
        self.assertTrue(data["curated"])
        self.assertNotIn("grammar", data["sentences"][0])
        self.assertEqual(data["sentences"][1]["grammar"], [
            {"pattern": "〜てしまう", "note": "行っちゃった = 行く+てしまう"},
            {"pattern": "ら抜き言葉", "note": "見られる→見れる", "proposed": True},
        ])
        self.assertEqual(data["sentences"][0]["phrases"],
                         [{"canonical": "気を付ける", "surface": "気を付けて"}])
        self.assertNotIn("phrases", data["sentences"][1])

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

    def test_definitions_merge_curate_authored_defs(self):
        # words JMdict lacks get their gloss from the curate pass (`defs` in
        # curate.json), flagged ai — and never shadow a real JMdict entry
        import sqlite3

        from tools import jmdict
        ep_dir = self.stage_episode()
        conn = sqlite3.connect(jmdict.db_path(self.cfg))
        jmdict.build_db(conn, iter([
            (2, {"公園", "こうえん"},
             {"k": ["公園"], "r": ["こうえん"],
              "s": [{"pos": ["noun"], "g": ["(public) park"]}]}),
        ]))
        conn.close()
        write_json(ep_dir / "curate.json", {
            "synopsis": "", "keywords": [], "focal_points": [], "exclude": [],
            "defs": [
                {"word": "犬", "reading": "いぬ", "gloss": "dog",
                 "pos": "noun"},
                {"word": "公園", "reading": "こうえん",
                 "gloss": "must not shadow JMdict"},
                {"word": "ノイズ"},  # glossless row is dropped, not a 500
            ],
        })
        data = self.client.get(f"/definitions/{EP}", headers=self.auth).json()
        self.assertEqual(data["犬"], [{"k": ["犬"], "r": ["いぬ"],
                                      "s": [{"pos": ["noun"], "g": ["dog"]}],
                                      "ai": True}])
        self.assertNotIn("ai", data["公園"][0])  # JMdict wins
        self.assertNotIn("ノイズ", data)

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
        self.assertEqual(body["applied"], 1)   # only "k" is knowledge evidence
        self.assertEqual(body["interest"], 1)  # "h" persists as tap_interest
        self.assertIn("promote", body)
        self.assertIsNotNone(body["cards_selected"])

        # replay of the same batch_id is a dedup no-op
        r2 = self.client.post("/taps", json=payload, headers=self.auth)
        self.assertTrue(r2.json()["duplicate"])
        self.assertEqual(r2.json()["applied"], 0)

        conn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(conn.execute(
            "SELECT status FROM lemmas WHERE lemma='犬'").fetchone()[0], "known")
        # the high-interest tap is durable (persists across episodes) and 公園
        # is not known, so it stands as active interest
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM evidence "
                         "WHERE lemma='公園' AND source='tap_interest'").fetchone()[0], 1)
        self.assertIn("公園", lc.active_interest(conn, known=()))
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

        # mark watched: the response returns immediately (exposures active,
        # push queued); the push itself runs in the background and — Anki is
        # down here — its error must land on the queue row for retry
        qconn, _ = self._enqueue_at("reconciled")
        r = self.client.post(f"/watched/{EP}", headers=self.auth)
        body = r.json()
        self.assertTrue(body["watched"])
        self.assertEqual(body["cards"]["queued"], 1)
        self.assertIn(q.get_job(qconn, EP)["state"], ("pushing", "watched"))
        self.join_closeout()
        job = q.get_job(qconn, EP)
        self.assertEqual(job["state"], "watched")
        self.assertIn("cards failed", job["error"])
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
        self.assertEqual(r.json()["cards"]["queued"], 0)  # nothing staged to push
        self.assertEqual(self.client.post("/watched/ghost", headers=self.auth)
                         .status_code, 404)

    def test_watched_conflicts_while_pushing(self):
        """A close-out is already running — a second POST must not stack."""
        self.stage_episode()
        self._enqueue_at("pushing")
        r = self.client.post(f"/watched/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 409)

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
        self.assertEqual(body["cards"]["queued"], 0)
        self.assertIn("declined", body["cards"]["note"])
        self.join_closeout()
        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT watched FROM episodes WHERE id=?", (EP,)).fetchone()[0], 1)
        # Anki is down in this suite, so a push attempt would land an error
        # on the row — its absence proves the push was skipped, not failed
        job = q.get_job(conn, EP)
        self.assertEqual(job["state"], "watched")
        self.assertIsNone(job["error"])

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

    def test_survey_roundtrip(self):
        """Full survey over HTTP → axes + follow persist and ride back on /jobs."""
        self.stage_episode()
        self._enqueue_at("watched")
        lconn = lc.open_db(self.cfg["ledger_db"])
        lc.update_episode_meta(lconn, EP,
                               columns={"channel": "Guy", "channel_id": "UCsurvey"})
        lconn.close()

        r = self.client.post(
            f"/episodes/{EP}/rating",
            json={"rating": 4, "tags": ["fascinating"],
                  "axes": {"topic_pull": 5, "presenter": 5, "difficulty": 5},
                  "follow": "more", "note": "loved the act"},
            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["axes"]["presenter"], 5)

        job = self.client.get(f"/jobs/{EP}", headers=self.auth).json()
        self.assertEqual(job["axes"]["topic_pull"], 5)
        self.assertEqual(job["follow"], "more")

        lconn = lc.open_db(self.cfg["ledger_db"])
        # follow upserted onto the channel; manzai censor holds through HTTP
        self.assertEqual(lconn.execute(
            "SELECT follow_state FROM channels WHERE channel_id='UCsurvey'"
        ).fetchone()[0], "more")
        v = lc.query_enjoyment(lconn, EP)
        self.assertFalse(v["axis_valid"]["topic_pull"])  # censored at difficulty 5
        self.assertTrue(v["axis_valid"]["presenter"])     # survives
        lconn.close()

        # bad axis / follow → 422
        self.assertEqual(self.client.post(
            f"/episodes/{EP}/rating", json={"rating": 4, "axes": {"topic_pull": 9}},
            headers=self.auth).status_code, 422)
        self.assertEqual(self.client.post(
            f"/episodes/{EP}/rating", json={"rating": 4, "follow": "subscribe"},
            headers=self.auth).status_code, 422)

    def test_rating_review_id_replay_is_idempotent(self):
        """The offline outbox re-flushes with the same review_id — one review."""
        self.stage_episode()
        self._enqueue_at("watched")
        payload = {"rating": 5, "tags": ["loved_format"], "review_id": "rev1"}
        first = self.client.post(f"/episodes/{EP}/rating", json=payload,
                                 headers=self.auth).json()
        self.assertNotIn("duplicate", first)
        replay = self.client.post(f"/episodes/{EP}/rating", json=payload,
                                  headers=self.auth).json()
        self.assertTrue(replay["duplicate"])
        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT COUNT(*) FROM taste_events WHERE episode_id=? AND kind='rating'",
            (EP,)).fetchone()[0], 1)

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
        self.join_closeout()  # deleting mid-push would 409

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
        # mid-card-push is just as protected — cards are landing in Anki
        q.set_state(conn, job["id"], "pushing", episode_id=EP)
        r = self.client.request("DELETE", f"/jobs/{EP}", headers=self.auth)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.request("DELETE", "/jobs/ghost", headers=self.auth)
                         .status_code, 404)

    def test_jobs_carry_comprehensibility(self):
        """The queue's sort/display metric rides on both job reads."""
        self.stage_episode()
        self._enqueue_at("staged")
        jobs = self.client.get("/jobs", headers=self.auth).json()
        self.assertEqual([j["comprehensibility"] for j in jobs
                          if j["episode_id"] == EP], [0.8])
        self.assertEqual(self.client.get(f"/jobs/{EP}", headers=self.auth)
                         .json()["comprehensibility"], 0.8)

    def test_coverage_query(self):
        self.stage_episode()
        r = self.client.get("/coverage", headers=self.auth)
        self.assertEqual(r.json()["summary"]["episodes"]["total"], 1)
        self.assertIn("needs_review", r.json())

    def test_retry_requeues_failed_job(self):
        conn, job = self._enqueue_at("failed")
        q.set_state(conn, job["id"], "failed", episode_id=EP, error="boom")
        r = self.client.post(f"/jobs/{EP}/retry", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "queued")
        self.assertIsNone(r.json()["error"])
        # retrying a non-failed job is a 409, a ghost is a 404
        q.set_state(conn, job["id"], "staged", episode_id=EP)
        self.assertEqual(self.client.post(f"/jobs/{EP}/retry", headers=self.auth)
                         .status_code, 409)
        self.assertEqual(self.client.post("/jobs/ghost/retry", headers=self.auth)
                         .status_code, 404)

    def test_confirm_queue_and_answer(self):
        # six watched exposures of a rare lemma → a confirmation candidate
        conn = lc.open_db(self.cfg["ledger_db"])
        for i in range(6):
            ep = {"id": f"c{i}", "title": f"Ep {i}", "source": "t", "kind": "local"}
            lc.record_exposure(conn, ep, {"蝶": {"sentence_idx": 0, "known_ratio": 1.0,
                                                "other_unknown_count": 0}})
            lc.mark_watched(conn, f"c{i}")
        lc.promote(conn)
        conn.close()

        cands = self.client.get("/confirm", headers=self.auth).json()["candidates"]
        self.assertEqual([c["lemma"] for c in cands], ["蝶"])
        self.assertIn("episodes", cands[0])  # watched-episode context rides along
        self.assertEqual(self.client.get("/stats", headers=self.auth)
                         .json()["confirm_candidates"], 1)

        # "yes" → known, queue empties; "?" body without a lemma is a 422
        r = self.client.post("/confirm", headers=self.auth,
                             json={"lemma": "蝶", "known": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "known")
        self.assertEqual(self.client.get("/confirm", headers=self.auth)
                         .json()["candidates"], [])
        self.assertEqual(self.client.post("/confirm", headers=self.auth, json={})
                         .status_code, 422)

    def test_confirm_defer_snoozes(self):
        conn = lc.open_db(self.cfg["ledger_db"])
        for i in range(6):
            ep = {"id": f"d{i}", "title": f"Ep {i}", "source": "t", "kind": "local"}
            lc.record_exposure(conn, ep, {"鯨": {"sentence_idx": 0, "known_ratio": 1.0,
                                                "other_unknown_count": 0}})
            lc.mark_watched(conn, f"d{i}")
        lc.promote(conn)
        conn.close()
        # "not yet" keeps it learning and pulls it from the queue
        r = self.client.post("/confirm", headers=self.auth,
                             json={"lemma": "鯨", "known": False})
        self.assertEqual(r.json()["status"], "learning")
        self.assertEqual(self.client.get("/confirm", headers=self.auth)
                         .json()["candidates"], [])

    def test_stats_endpoint(self):
        # seed the ledger: a known lemma in-corpus, plus an exposure + freq row
        self.stage_episode()
        conn = lc.open_db(self.cfg["ledger_db"])
        conn.execute("INSERT INTO freq (lemma, rank, source) VALUES ('公園', 120, 'g')")
        # 公園 already exists (record_exposure seeded it) — promote it to known
        conn.execute("UPDATE lemmas SET status='known' WHERE lemma='公園'")
        conn.commit()
        conn.close()
        r = self.client.get("/stats", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["known"], 1)
        self.assertEqual(body["episodes_total"], 1)
        self.assertEqual(body["words_encountered"], 1)  # 公園 exposure
        # 公園 (rank 120) counts toward every band whose ceiling ≥ 120
        band1000 = next(b for b in body["freq_bands"] if b["band"] == 1000)
        self.assertGreaterEqual(band1000["known"], 1)


class TestTypedConfirm(ServerTestBase):
    """GRAMMAR.md: the confirm queue and /stats over the three item kinds."""

    MINI_JMDICT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE JMdict [<!ENTITY exp "expressions (phrases, clauses, etc.)">]>
<JMdict>
<entry><ent_seq>1</ent_seq>
<k_ele><keb>気を付ける</keb></k_ele><r_ele><reb>きをつける</reb></r_ele>
<sense><pos>&exp;</pos><gloss>to be careful</gloss></sense>
</entry>
</JMdict>
"""

    def seed_typed_candidates(self):
        """A phrase and an N5 grammar point, both over their θ bar."""
        import io
        import sqlite3
        from tools import jmdict
        jconn = sqlite3.connect(Path(self.cfg["work_dir"]) / "jmdict.db")
        jmdict.build_db(jconn, jmdict.parse_entries(
            io.BytesIO(self.MINI_JMDICT.encode())))
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.seed_grammar_points(conn, [
            {"pattern": "〜てしまう", "level": 5, "gloss": "completion/regret"}])
        for i in range(6):  # phrase bar is the rare-word θ (6 over ≥4 episodes)
            ep = {"id": f"t{i}", "title": f"Ep {i}", "source": "t", "kind": "local"}
            lc.record_exposure(conn, ep, {})
            lc.record_curate_items(conn, f"t{i}", {
                "phrases": [{"sentence_idx": 0, "surface": "気を付けて",
                             "canonical": "気を付ける",
                             "classification": "comprehensible"}],
                "grammar": [{"sentence_idx": 0, "pattern": "〜てしまう",
                             "classification": "comprehensible"}],
            }, jmdict_conn=jconn)
            lc.mark_watched(conn, f"t{i}")
        lc.promote(conn)
        conn.close()
        jconn.close()

    def test_typed_queue_and_answers(self):
        self.seed_typed_candidates()
        cands = self.client.get("/confirm", headers=self.auth).json()["candidates"]
        by_kind = {c["kind"]: c for c in cands}
        self.assertEqual(set(by_kind), {"phrase", "grammar"})
        # phrase card: JMdict senses attach exactly like a word's
        ph = by_kind["phrase"]
        self.assertEqual(ph["lemma"], "気を付ける")
        self.assertEqual(ph["senses"][0]["s"][0]["g"], ["to be careful"])
        # grammar card: taxonomy fields, no senses
        g = by_kind["grammar"]
        self.assertEqual((g["pattern"], g["level"], g["gloss"]),
                         ("〜てしまう", 5, "completion/regret"))
        self.assertNotIn("senses", g)

        # typed answers hit the right projection
        r = self.client.post("/confirm", headers=self.auth,
                             json={"kind": "phrase", "key": "気を付ける",
                                   "known": True})
        self.assertEqual(r.json()["status"], "known")
        r = self.client.post("/confirm", headers=self.auth,
                             json={"kind": "grammar", "key": "〜てしまう",
                                   "known": False})
        self.assertEqual(r.json()["status"], "learning")  # defer, snoozed
        self.assertEqual(self.client.get("/confirm", headers=self.auth)
                         .json()["candidates"], [])

        # a non-taxonomy pattern can't be minted through confirm; bad kind 422
        self.assertEqual(self.client.post(
            "/confirm", headers=self.auth,
            json={"kind": "grammar", "key": "〜偽パターン", "known": True}
        ).status_code, 404)
        self.assertEqual(self.client.post(
            "/confirm", headers=self.auth,
            json={"kind": "bogus", "key": "x", "known": True}).status_code, 422)

    def test_stats_kind_siblings(self):
        self.seed_typed_candidates()
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.confirm_known_lemma(conn, "気を付ける", kind="phrase")
        lc.confirm_known_lemma(conn, "〜てしまう", kind="grammar")
        lc.promote(conn)
        conn.close()
        body = self.client.get("/stats", headers=self.auth).json()
        # the words-only headline is untouched by phrase/grammar knowns
        self.assertEqual(body["known"], 0)
        self.assertEqual(body["words_encountered"], 0)
        self.assertEqual(body["phrases_known"], 1)
        self.assertEqual(body["grammar_known"], 1)
        self.assertEqual(body["grammar_proposed"], 0)


class TestSelect(unittest.TestCase):
    """Pure selection rules: known prunes, interest jumps the queue — no cap
    exists (card volume is the curation bar's outcome, never a count)."""

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
                             [["a", "k"], ["c", "h"]])
        self.assertEqual([p["lemma"] for p in final], ["c", "b", "d"])

    def test_rescue_only_true_i_plus_1(self):
        from tools.select import select_picks
        # e is rescuable (0 other unknowns, sane clip); f is not (3 others)
        final = select_picks(self.POOL, self.COV,
                             [["e", "h"], ["f", "h"]])
        lemmas = [p["lemma"] for p in final]
        self.assertEqual(lemmas[0], "e")
        self.assertTrue(final[0].get("rescued"))
        self.assertEqual(len(final), 5)  # whole pool + rescue — no cap
        self.assertNotIn("f", lemmas)

    def test_no_feedback_keeps_pool_order(self):
        from tools.select import select_picks
        final = select_picks(self.POOL, self.COV, [])
        self.assertEqual([p["lemma"] for p in final], ["a", "b", "c", "d"])

    def test_standing_interest_prioritizes_pool_pick(self):
        # No taps this episode, but "c" is a carried-over wanted word: it
        # jumps ahead of pool order even without being re-tapped.
        from tools.select import select_picks
        final = select_picks(self.POOL, self.COV, [],
                             standing_interest=["c"])
        self.assertEqual(final[0]["lemma"], "c")

    def test_standing_interest_rescues_candidate(self):
        # "e" isn't in the curated pool but is a wanted word with a clean
        # candidate → rescued into a fresh card, no re-tap needed.
        from tools.select import select_picks
        final = select_picks(self.POOL, self.COV, [],
                             standing_interest=["e"])
        e = next(p for p in final if p["lemma"] == "e")
        self.assertTrue(e.get("rescued"))

    def test_fresh_tap_outranks_standing_interest(self):
        from tools.select import select_picks
        final = select_picks(self.POOL, self.COV, [["b", "h"]],
                             standing_interest=["c"])
        # fresh tap "b" first, then standing interest "c", then pool order
        self.assertEqual([p["lemma"] for p in final][:2], ["b", "c"])


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
