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
from server.worker import (max_height, normalize_video, scan_stage2,
                          stage_video, video_props)
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
         "tokens": [{"s": "犬", "l": "犬", "r": "いぬ", "c": True, "k": True},
                    {"s": "が", "l": "が", "r": "が", "c": False, "k": True}]},
        {"idx": 1, "start": 2.0, "end": 4.0, "text": "公園へ行く。",
         "classification": "i_plus_1", "known_ratio": 0.8, "unknown": ["公園"],
         "tokens": [{"s": "公園", "l": "公園", "r": "こうえん", "c": True, "k": False},
                    {"s": "と", "l": "と", "r": "と", "c": False, "k": True},
                    {"s": "いう", "l": "いう", "r": "いう", "c": False, "k": True}]},
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
        self.assertNotIn("debrief", q.get_job(migrated, "yt_x"))

    def test_retired_debrief_column_is_hidden(self):
        # queue.db files from before 2026-09 still carry the column; the wire
        # shape must not
        self.conn.execute("ALTER TABLE jobs ADD COLUMN debrief INTEGER NOT NULL DEFAULT 0")
        job, _ = q.enqueue(self.conn, "https://youtu.be/abcDEF12345")
        self.assertNotIn("debrief", job)

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

    def test_transcript_carries_standing_lists(self):
        # the two ledger lists the player highlights: "want to learn"
        # (tap_interest, green) and "think you know" (confirm queue, blue).
        # Both are filtered to the lemmas that actually appear in this episode.
        self.stage_episode()
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.apply_taps(conn, {"episode_id": EP, "batch_id": "b" * 16,
                             "taps": [["犬", "h"], ["蝶", "h"]]}, watched=False)
        conn.execute("UPDATE lemmas SET confirm_candidate = 1 WHERE lemma = '公園'")
        conn.commit()
        conn.close()
        data = self.client.get(f"/transcript/{EP}", headers=self.auth).json()
        self.assertEqual(data["interest"], ["犬"])  # 蝶 is not in this episode
        self.assertEqual(data["confirm"], ["公園"])
        # the green list is on the wire too — 公園 is the only ranked lemma
        # here and it is blue, so nothing qualifies
        self.assertEqual(data["should_know"], [])

    def test_paint_state_is_live(self):
        """GET /episodes/{id}/paint: the ledger's lists as of NOW, narrowed to
        this episode — a word tapped known elsewhere shows up as known here
        even though coverage froze its `k`, the confirm queue is current,
        and grammar candidates are matched against the curated line patterns."""
        ep_dir = self.stage_episode(with_curate=True)
        write_json(ep_dir / "curate.json", {
            "synopsis": "犬の話。", "keywords": [], "focal_points": [], "exclude": [],
            "grammar": [{"sentence_idx": 1, "pattern": "〜てしまう"}],
        })
        conn = lc.open_db(self.cfg["ledger_db"])
        # 公園 was unknown at coverage time; a tap (from any episode) makes it known
        lc.apply_taps(conn, {"episode_id": EP, "batch_id": "p" * 16,
                             "taps": [["公園", "k"], ["犬", "h"]]}, watched=False)
        lc.promote(conn)  # what POST /taps does after applying
        conn.execute("UPDATE lemmas SET confirm_candidate = 1 WHERE lemma = '犬'")
        conn.execute("INSERT INTO lemmas (lemma, kind, status, confirm_candidate, updated_at) "
                     "VALUES ('蝶', 'word', 'learning', 1, 'now')")  # not in this episode
        for pat, cand in (("〜てしまう", 1), ("〜てくる", 1), ("〜ながら", 0)):
            conn.execute("INSERT INTO grammar_points (pattern, status, confirm_candidate, "
                         "updated_at) VALUES (?, 'learning', ?, 'now')", (pat, cand))
        conn.commit()
        conn.close()
        data = self.client.get(f"/episodes/{EP}/paint", headers=self.auth).json()
        self.assertEqual(data["known"], ["公園"])
        self.assertEqual(data["unknown"], [])
        self.assertEqual(data["confirm"], ["犬"])
        # 犬 graduated to the blue list, so it has left the ★ list
        self.assertEqual(data["interest"], [])
        self.assertEqual(data["should_know"], [])  # 公園 known, 犬 unranked
        self.assertEqual(data["grammar_confirm"], ["〜てしまう"])
        self.assertEqual(
            self.client.get("/episodes/nope/paint", headers=self.auth).status_code, 404)
        # ✗ from a list card: 公園 leaves known and the paint says so explicitly,
        # because the sidecar's token `k` can only be undone by subtraction
        r = self.client.post("/lists/mark", headers=self.auth,
                             json={"lemma": "公園", "mark": "u"}).json()
        self.assertEqual(r["status"], "learning")
        data = self.client.get(f"/episodes/{EP}/paint", headers=self.auth).json()
        self.assertEqual(data["known"], [])
        self.assertEqual(data["unknown"], ["公園"])

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
        # not placeable on 犬が → no span; untracked → status unknown
        self.assertEqual(data["sentences"][0]["phrases"],
                         [{"canonical": "気を付ける", "surface": "気を付けて",
                           "status": "unknown"}])
        self.assertNotIn("phrases", data["sentences"][1])

    def test_transcript_phrases_carry_span_and_status(self):
        """A phrase is one unit for the player: its token span (so 血が騒いだ
        paints as a whole) and its ledger status; the curate pass's emissions
        merge with the tracked phrases Stage 1 detected on the same line."""
        ep_dir = self.stage_episode(with_curate=True)
        cov = json.loads(json.dumps(COVERAGE))
        cov["sentences"][0]["tokens"] = [
            {"s": "血", "l": "血", "r": "ち", "c": True, "k": True},
            {"s": "が", "l": "が", "r": "が", "c": False, "k": True},
            {"s": "騒い", "l": "騒ぐ", "r": "さわい", "c": True, "k": True},
            {"s": "だ", "l": "だ", "r": "だ", "c": False, "k": True}]
        cov["sentences"][1]["phrases"] = [{"phrase": "気を付ける", "status": "unknown"}]
        write_json(ep_dir / "coverage.json", cov)
        write_json(ep_dir / "curate.json", {
            "synopsis": "犬の話。", "keywords": [], "focal_points": [], "exclude": [],
            "phrases": [
                {"sentence_idx": 0, "surface": "血が騒いだ", "canonical": "血が騒ぐ",
                 "classification": "too_hard"},
                {"sentence_idx": 1, "surface": "気を付けて", "canonical": "気を付ける",
                 "classification": "comprehensible"},  # also Stage-1 tracked → one entry
            ]})
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.add_phrase(conn, "血が騒ぐ")
        lc.confirm_known_lemma(conn, "血が騒ぐ", kind="phrase")
        lc.promote(conn)
        conn.close()
        data = self.client.get(f"/transcript/{EP}", headers=self.auth).json()
        self.assertEqual(data["sentences"][0]["phrases"], [
            {"canonical": "血が騒ぐ", "surface": "血が騒いだ", "status": "known",
             "start": 0, "end": 3}])
        self.assertEqual(data["sentences"][1]["phrases"], [
            {"canonical": "気を付ける", "surface": "気を付けて", "status": "unknown"}])
        # the paint state carries the phrase axis, narrowed to this episode
        paint = self.client.get(f"/episodes/{EP}/paint", headers=self.auth).json()
        self.assertEqual(paint["phrase_known"], ["血が騒ぐ"])
        self.assertEqual(paint["phrase_confirm"], [])
        self.assertEqual(paint["phrase_interest"], [])

    def test_single_token_headword_is_not_a_phrase(self):
        """万が一-style: the curate pass emitted a one-token headword as a
        phrase. It's a word (its own token + word row) — serving it as a
        phrase too would give the popup two identical entries."""
        ep_dir = self.stage_episode(with_curate=True)
        write_json(ep_dir / "curate.json", {
            "synopsis": "犬の話。", "keywords": [], "focal_points": [], "exclude": [],
            "phrases": [{"sentence_idx": 1, "surface": "公園",
                         "canonical": "公園", "classification": "i_plus_1"}]})
        data = self.client.get(f"/transcript/{EP}", headers=self.auth).json()
        self.assertNotIn("phrases", data["sentences"][1])
        paint = self.client.get(f"/episodes/{EP}/paint", headers=self.auth).json()
        self.assertEqual(paint["phrase_known"], [])

    def test_tracked_phrase_is_matched_live_on_the_transcript(self):
        """A phrase the ledger tracks (marked from the popup after coverage
        froze) is placed on the line by lemma sequence — no curate entry, no
        coverage entry needed — so it paints on the next open."""
        self.stage_episode(with_curate=True)
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.add_phrase(conn, "という")  # と|いう on sentence 1
        conn.close()
        data = self.client.get(f"/transcript/{EP}", headers=self.auth).json()
        self.assertEqual(data["sentences"][1]["phrases"], [
            {"canonical": "という", "surface": "", "status": "unknown",
             "start": 1, "end": 3}])
        self.assertNotIn("phrases", data["sentences"][0])
        # the paint state's phrase axis is narrowed to the same live set
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.confirm_known_lemma(conn, "という", kind="phrase")
        lc.promote(conn)
        conn.close()
        paint = self.client.get(f"/episodes/{EP}/paint", headers=self.auth).json()
        self.assertEqual(paint["phrase_known"], ["という"])

    def test_phrase_tap_lands_as_phrase_evidence(self):
        """The popup's phrase layer marks the expression, not its words: a
        [key, mark, "phrase"] tap creates/updates the phrase item only, and
        a ★ on it joins the paint state's phrase_interest."""
        ep_dir = self.stage_episode(with_curate=True)
        write_json(ep_dir / "curate.json", {
            "synopsis": "犬の話。", "keywords": [], "focal_points": [], "exclude": [],
            "phrases": [{"sentence_idx": 0, "surface": "気を付けて",
                         "canonical": "気を付ける", "classification": "comprehensible"},
                        {"sentence_idx": 1, "surface": "血が騒いだ",
                         "canonical": "血が騒ぐ", "classification": "too_hard"}]})
        r = self.client.post("/taps", json={
            "episode_id": EP, "batch_id": "q" * 16,
            "taps": [["気を付ける", "k", "phrase"], ["血が騒ぐ", "h", "phrase"], ["犬", "k"]]},
            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["applied"], 2)
        self.assertEqual(r.json()["interest"], 1)
        conn = lc.open_db(self.cfg["ledger_db"])
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT lemma, kind, status FROM lemmas")}
        self.assertEqual(rows["気を付ける"], ("phrase", "known"))
        self.assertEqual(rows["血が騒ぐ"], ("phrase", "unknown"))
        self.assertNotIn("気", rows)  # the words inside are untouched
        conn.close()
        paint = self.client.get(f"/episodes/{EP}/paint", headers=self.auth).json()
        self.assertEqual(paint["phrase_known"], ["気を付ける"])
        self.assertEqual(paint["phrase_interest"], ["血が騒ぐ"])
        self.assertNotIn("血が騒ぐ", paint["interest"])  # phrase keys stay on their axis

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
            (1, {"が"},
             {"k": [], "r": ["が"],
              "s": [{"pos": ["particle"], "g": ["subject marker"]}]}),
            (1, {"と言う", "という"},
             {"k": ["と言う"], "r": ["という"],
              "s": [{"pos": ["particle"], "g": ["called; named"]}]}),
            (1, {"気を付ける", "きをつける"},
             {"k": ["気を付ける"], "r": ["きをつける"],
              "s": [{"pos": ["expression"], "g": ["to be careful"]}]}),
        ]))
        conn.close()
        # the line's curated phrases are served by canonical for the popup's
        # phrase layer — no token carries that key
        write_json(episode_dir(self.cfg, EP) / "curate.json", {
            "synopsis": "", "keywords": [], "focal_points": [], "exclude": [],
            "phrases": [{"sentence_idx": 0, "surface": "気を付けて",
                         "canonical": "気を付ける", "classification": "comprehensible"}]})
        data = self.client.get(f"/definitions/{EP}", headers=self.auth).json()
        self.assertEqual(data["気を付ける"][0]["s"][0]["g"], ["to be careful"])
        self.assertIn("公園", data)  # content lemma with an entry
        self.assertIn("が", data)  # any-word popup: non-content lemmas too
        self.assertNotIn("犬", data)  # lemma with no dict entry
        # adjacent と+いう run joins to a headword, but a particle-led run is
        # a grammar pattern (〜という), not a lexical unit — never a compound
        # key (tools/jmdict.py compound_entries); a deliberately tracked
        # phrase still reaches the popup via episode_phrases (test above)
        self.assertNotIn("という", data)
        self.assertEqual(data["公園"][0]["s"][0]["g"], ["(public) park"])
        self.assertEqual(self.client.get("/definitions/nope", headers=self.auth)
                         .status_code, 404)
        self.assertEqual(self.client.get(f"/definitions/{EP}").status_code, 401)

    def test_definitions_merge_repair_gate_names(self):
        # the repair gate's name adjudications (surface + kind + note) serve
        # the popup for name taps — no curate pass needed
        ep_dir = self.stage_episode()
        write_json(ep_dir / "repair.json", {
            "names": [{"surface": "犬", "kind": "person",
                       "note": "the show's dog, Inu"},
                      {"surface": "謎", "kind": "person"},  # noteless → skipped
                      "bare-string"],  # legacy shape → skipped, not a 500
        })
        data = self.client.get(f"/definitions/{EP}", headers=self.auth).json()
        self.assertEqual(data["犬"][0]["s"][0]["g"], ["the show's dog, Inu"])
        self.assertEqual(data["犬"][0]["s"][0]["pos"], ["name (person)"])
        self.assertTrue(data["犬"][0]["ai"])
        self.assertNotIn("謎", data)

    def test_definitions_merge_curate_authored_defs(self):
        # words JMdict lacks get their gloss from the curate pass (`defs` in
        # curate.json) as the sole entry; words JMdict HAS get the episode-
        # sense entry PREPENDED (context-first popup, dictionary after)
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
                 "gloss": "the park near the studio"},
                {"word": "ノイズ"},  # glossless row is dropped, not a 500
            ],
        })
        data = self.client.get(f"/definitions/{EP}", headers=self.auth).json()
        self.assertEqual(data["犬"], [{"k": ["犬"], "r": ["いぬ"],
                                      "s": [{"pos": ["noun"], "g": ["dog"]}],
                                      "ai": True}])
        # episode sense first, real dictionary entry preserved after
        self.assertEqual(len(data["公園"]), 2)
        self.assertTrue(data["公園"][0]["ai"])
        self.assertEqual(data["公園"][0]["s"][0]["g"],
                         ["the park near the studio"])
        self.assertNotIn("ai", data["公園"][1])
        self.assertEqual(data["公園"][1]["s"][0]["g"], ["(public) park"])
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

    def test_taps_after_watched_do_not_regress_the_job(self):
        """Feedback and mark-watched are independent and may arrive in either
        order. Late feedback still lands in the ledger, but must not drag the
        row back to the pre-watch `reconciled` state (that un-did the watch on
        the phone and re-armed delete)."""
        self.stage_episode()
        qconn, _ = self._enqueue_at("watched")
        r = self.client.post("/taps", json={"episode_id": EP, "batch_id": "e" * 16,
                                            "taps": [["公園", "k"]]}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["applied"], 1)  # evidence still recorded
        self.assertEqual(q.get_job(qconn, EP)["state"], "watched")
        conn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE lemma='公園' AND source='tap_known'"
        ).fetchone()[0], 1)

        # same during the background close-out: `pushing` must survive too
        q.set_state(qconn, q.get_job(qconn, EP)["id"], "pushing", episode_id=EP)
        self.client.post("/taps", json={"episode_id": EP, "batch_id": "f" * 16,
                                        "taps": [["犬", "h"]]}, headers=self.auth)
        self.assertEqual(q.get_job(qconn, EP)["state"], "pushing")

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

    def test_viewtime_roundtrip(self):
        """A phone-recorded session lands in view_sessions, replays dedupe on
        id, GET hands it back (filtered by device-day), and the episode does
        NOT have to exist — time spent outlives a deleted row."""
        seg = {"id": "abc123", "episode_id": "yt_gone", "title": "消えた動画",
               "kind": "watch", "day": "2026-09-02",
               "start": "2026-09-02T20:15:00-06:00", "secs": 1234.5,
               "reached": 1500.0, "duration": 1800.0}
        r = self.client.post("/viewtime", json=seg, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"id": "abc123", "duplicate": False})
        # replay (outbox double-flush) is a no-op
        r = self.client.post("/viewtime", json=seg, headers=self.auth)
        self.assertEqual(r.json(), {"id": "abc123", "duplicate": True})
        listen = {**seg, "id": "def456", "kind": "listen", "day": "2026-09-01",
                  "start": "2026-09-01T08:00:00-06:00", "secs": 600}
        self.client.post("/viewtime", json=listen, headers=self.auth)

        lconn = lc.open_db(self.cfg["ledger_db"])
        self.assertEqual(lconn.execute(
            "SELECT COUNT(*) FROM view_sessions").fetchone()[0], 2)
        body = self.client.get("/viewtime", headers=self.auth).json()
        self.assertEqual([x["id"] for x in body["sessions"]], ["def456", "abc123"])
        self.assertEqual(body["sessions"][1]["secs"], 1234.5)
        self.assertEqual(body["sessions"][1]["title"], "消えた動画")
        since = self.client.get("/viewtime?since=2026-09-02", headers=self.auth).json()
        self.assertEqual([x["id"] for x in since["sessions"]], ["abc123"])
        # the CLI's per-day totals
        totals = lc.query_view_totals(lconn)
        self.assertEqual(totals, [
            {"day": "2026-09-02", "watch": 1234.5, "listen": 0.0},
            {"day": "2026-09-01", "watch": 0.0, "listen": 600.0},
        ])

    def test_viewtime_source_and_delete(self):
        """`source` rides along (app by default; manual for hand-typed entries,
        import for the spreadsheet), comes back on GET, and DELETE removes a
        row idempotently."""
        base = {"episode_id": "manual", "kind": "listen", "day": "2026-09-02",
                "start": "2026-09-02T12:00:00-06:00", "secs": 1800, "title": "car radio"}
        self.client.post("/viewtime", json={**base, "id": "m1", "source": "manual"},
                         headers=self.auth)
        self.client.post("/viewtime", json={**base, "id": "a1"}, headers=self.auth)
        r = self.client.post("/viewtime", json={**base, "id": "x", "source": "guess"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 422)
        rows = {x["id"]: x for x in
                self.client.get("/viewtime", headers=self.auth).json()["sessions"]}
        self.assertEqual(rows["m1"]["source"], "manual")
        self.assertEqual(rows["a1"]["source"], "app")
        r = self.client.delete("/viewtime/m1", headers=self.auth)
        self.assertEqual(r.json(), {"id": "m1", "deleted": True})
        self.assertEqual(self.client.delete("/viewtime/m1", headers=self.auth).json(),
                         {"id": "m1", "deleted": False})
        self.assertEqual([x["id"] for x in
                          self.client.get("/viewtime", headers=self.auth).json()["sessions"]],
                         ["a1"])

    def test_viewtime_rejects_bad_input(self):
        base = {"id": "x1", "episode_id": EP, "kind": "watch", "day": "2026-09-02",
                "start": "2026-09-02T20:15:00-06:00", "secs": 10}
        for bad in ({**base, "kind": "skim"}, {**base, "day": "9/2/2026"},
                    {**base, "secs": -1}, {**base, "secs": "10"},
                    {**base, "id": ""}, {k: v for k, v in base.items() if k != "start"}):
            r = self.client.post("/viewtime", json=bad, headers=self.auth)
            self.assertEqual(r.status_code, 422, bad)
        self.assertEqual(self.client.post("/viewtime", json=base).status_code, 401)

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

    def test_jobs_carry_curation_genre(self):
        """The queue's genre chip/filter reads /immerse's curation labels
        straight off the ledger's episode row; absent → null, not missing."""
        self.stage_episode()
        self._enqueue_at("staged")
        job = self.client.get(f"/jobs/{EP}", headers=self.auth).json()
        self.assertIsNone(job["genre"])
        self.assertIsNone(job["channel"])
        lc.record_curation(lc.open_db(self.cfg["ledger_db"]), EP, {
            "genre": "documentary", "format": "live-action", "topics": ["trains"]})
        jobs = self.client.get("/jobs", headers=self.auth).json()
        self.assertEqual([(j["genre"], j["format"]) for j in jobs
                          if j["episode_id"] == EP], [("documentary", "live-action")])
        self.assertEqual(self.client.get(f"/jobs/{EP}", headers=self.auth)
                         .json()["genre"], "documentary")

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

    def test_word_lists_review_like_confirm(self):
        # LIVE_REVIEW.md §1: the ★ list and the should-know window are
        # reviewable with the confirm queue's row shape + actions
        conn = lc.open_db(self.cfg["ledger_db"])
        conn.executemany(
            "INSERT OR REPLACE INTO freq (lemma, rank, source) VALUES (?, ?, ?)",
            [("猫", 1, "show_graph"), ("犬", 2, "show_graph"), ("設計", 900, "show_graph")])
        ep = {"id": "w0", "title": "Ep W", "source": "t", "kind": "local"}
        lc.record_exposure(conn, ep, {"設計": {"sentence_idx": 0, "known_ratio": 0.5,
                                              "other_unknown_count": 3}})
        lc.mark_watched(conn, "w0")
        lc.apply_taps(conn, {"episode_id": "w0", "batch_id": "b1",
                             "taps": [["設計", "h"], ["犬", "h"]]})
        lc.promote(conn)
        conn.close()

        r = self.client.get("/lists/interest", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        words = r.json()["words"]
        self.assertEqual([w["lemma"] for w in words], ["犬", "設計"])  # common first
        w = words[1]
        self.assertEqual(w["kind"], "word")
        self.assertEqual(w["freq_rank"], 900)
        self.assertEqual(w["episodes"], ["Ep W"])
        self.assertIn("reading_segs", w)

        r = self.client.get("/lists/should_know", headers=self.auth)
        words = r.json()["words"]
        self.assertEqual([w["lemma"] for w in words], ["猫"])  # 犬 is ★, 設計 too
        self.assertEqual(words[0]["freq_rank"], 1)  # rank from freq, never met
        self.assertEqual(words[0]["exposure_count"], 0)
        self.assertEqual(self.client.get("/stats", headers=self.auth)
                         .json()["should_know"], 1)
        self.assertEqual(self.client.get("/lists/nope", headers=self.auth)
                         .status_code, 404)

        # ★ on a green word → it moves to the ★ list; ✓ on a ★ word → known
        r = self.client.post("/lists/mark", headers=self.auth,
                             json={"lemma": "猫", "mark": "h"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["interest"])
        self.assertEqual(self.client.get("/lists/should_know", headers=self.auth)
                         .json()["words"], [])
        r = self.client.post("/lists/mark", headers=self.auth,
                             json={"lemma": "犬", "mark": "k", "batch_id": "m1"})
        self.assertEqual(r.json()["status"], "known")
        self.assertFalse(r.json()["interest"])
        self.assertEqual([w["lemma"] for w in self.client.get(
            "/lists/interest", headers=self.auth).json()["words"]], ["猫", "設計"])
        # a re-flush of the same batch is a no-op
        self.assertTrue(self.client.post("/lists/mark", headers=self.auth,
                                         json={"lemma": "犬", "mark": "k",
                                               "batch_id": "m1"}).json()["duplicate"])
        self.assertEqual(self.client.post("/lists/mark", headers=self.auth,
                                          json={"lemma": "犬", "mark": "x"}).status_code, 422)
        self.assertEqual(self.client.post("/lists/mark", headers=self.auth,
                                          json={"mark": "k"}).status_code, 422)

    def test_stats_endpoint(self):
        # seed the ledger: a known lemma in-corpus, plus an exposure + freq row
        self.stage_episode()
        conn = lc.open_db(self.cfg["ledger_db"])
        conn.execute("INSERT INTO freq (lemma, rank, source) VALUES ('公園', 120, 'show_graph')")
        # 公園 already exists (record_exposure seeded it) — promote it to known
        conn.execute("UPDATE lemmas SET status='known' WHERE lemma='公園'")
        # Leeds fallback rows share the corpus rank space (の is Leeds rank 0)
        # and must not count toward a corpus band; nor does a corpus word at
        # rank 1000 (0-based → the 1001st word) belong to the top-1000 band.
        conn.executemany(
            "INSERT OR REPLACE INTO freq (lemma, rank, source) VALUES (?, ?, ?)",
            [("の", 0, "leeds"), ("境界", 1000, "show_graph")])
        conn.executemany(
            "INSERT OR REPLACE INTO lemmas (lemma, kind, status, updated_at) "
            "VALUES (?, 'word', 'known', '2026-01-01T00:00:00')",
            [("の",), ("境界",)])
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
        self.assertEqual(band1000["known"], 1)  # 公園 only — not の, not 境界
        band2000 = next(b for b in body["freq_bands"] if b["band"] == 2000)
        self.assertEqual(band2000["known"], 2)  # 境界 (rank 1000) joins here
        for b in body["freq_bands"]:
            self.assertLessEqual(b["known"], b["total"])


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


class TestVideoStaging(unittest.TestCase):
    """The phone pulls staged video over Tailscale, so a staged file must
    honour server.video_resolution no matter where the source came from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _src(self, name, height, codec="libx264"):
        """A 1s H.264 test pattern at the given height (16:9)."""
        path = self.dir / name
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc=duration=1:size={height * 16 // 9}x{height}:rate=5",
             "-c:v", codec, "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    def test_max_height_parses_resolution(self):
        for res, want in [("480p", 480), ("1080p", 1080), ("720", 720),
                          ("Best", None), ("", None), (None, None)]:
            self.assertEqual(max_height(res), want, res)

    def test_taller_than_cap_is_scaled_down(self):
        src = self._src("tall.mp4", 720)
        dest = self.dir / "out" / "video.mp4"
        normalize_video(src, dest, cap=480)
        codec, height = video_props(dest)
        self.assertEqual(codec, "h264")
        self.assertEqual(height, 480)

    def test_within_cap_is_remuxed_untouched(self):
        src = self._src("short.mp4", 360)
        dest = self.dir / "out" / "video.mp4"
        normalize_video(src, dest, cap=480)
        self.assertEqual(video_props(dest), ("h264", 360))

    def test_no_cap_leaves_height_alone(self):
        src = self._src("tall2.mp4", 720)
        dest = self.dir / "out" / "video.mp4"
        normalize_video(src, dest, cap=None)
        self.assertEqual(video_props(dest), ("h264", 720))

    def test_local_source_is_capped_by_config(self):
        """Regression: local files used to be remuxed at full source
        resolution, staging a 1080p master the phone could not pull."""
        src = self._src("local.mp4", 720)
        cfg = {"work_dir": str(self.dir / "work"),
               "server": {"video_resolution": "480p"}}
        record = {"episode": {"id": "local_test", "kind": "local"}}
        dest = stage_video(cfg, str(src), record)
        self.assertEqual(video_props(dest)[1], 480)
        self.assertLess(dest.stat().st_size, src.stat().st_size)


if __name__ == "__main__":
    unittest.main()
