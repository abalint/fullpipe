"""Manga tests: identity + folder parsing, mokuro blocks → reading-order
sentence track + reader structure, the worker's manga Stage-1 path (PC OCR
and pull mocked, coverage real), and the manga-aware server routes."""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import ledgerctl as lc
from server import jobqueue as q
from server.worker import process_job, scan_stage2
from tools import manga as MG
from tools._staging import episode_dir, read_json, write_json
from tests.test_server import ServerTestBase

SLUG = "dandadan"
VOL_EP = "manga_dandadan_v01"

# two pages of mokuro output: bubbles deliberately listed out of reading order
OCR_P1 = {
    "version": "0.2.5", "img_width": 764, "img_height": 1200,
    "blocks": [
        # tier 2 (lower half), left bubble
        {"box": [80, 700, 200, 900], "vertical": True, "font_size": 20,
         "lines": ["猫のほうが", "速い！"]},
        # tier 1, left bubble
        {"box": [100, 50, 220, 300], "vertical": True, "font_size": 22,
         "lines": ["公園へ ", "行く。"]},
        # tier 1, right bubble (read first) — two sentences in one bubble
        {"box": [500, 40, 640, 320], "vertical": True, "font_size": 22,
         "lines": ["犬が走る。", "速いね。"]},
        # tier 2, right bubble
        {"box": [520, 720, 660, 950], "vertical": True, "font_size": 20,
         "lines": ["行こう"]},
    ],
}
OCR_P2 = {"version": "0.2.5", "img_width": 764, "img_height": 1200,
          "blocks": [{"box": [300, 100, 400, 200], "vertical": False,
                      "font_size": 18, "lines": ["おわり"]}]}


class TestIdentity(unittest.TestCase):
    def test_source_roundtrip(self):
        self.assertEqual(MG.manga_source(SLUG, 3), "manga://dandadan/3")
        self.assertEqual(MG.parse_manga_source("manga://dandadan/3"), (SLUG, 3))
        self.assertEqual(MG.manga_episode_id("manga://dandadan/3"), "manga_dandadan_v03")
        for bad in ("series://x/1", "https://youtu.be/abcDEF12345", "manga://", ""):
            self.assertIsNone(MG.manga_episode_id(bad), bad)

    def test_derive_job_id_and_kind(self):
        self.assertEqual(q.derive_job_id("manga://dandadan/1"), VOL_EP)
        self.assertEqual(q.job_kind(VOL_EP), "manga")
        self.assertEqual(q.job_kind("page_5ch_a_1"), "page")
        self.assertEqual(q.job_kind("yt_x"), "episode")
        self.assertTrue(q.is_text_kind(VOL_EP))
        self.assertFalse(q.is_text_kind("ser_x_e01"))
        with tempfile.TemporaryDirectory() as tmp:
            conn = q.open_queue(Path(tmp) / "queue.db")
            job, _ = q.enqueue(conn, "manga://dandadan/1", title="Dandadan Vol. 1",
                               series=SLUG, series_title="Dandadan", ep_no=1)
            self.assertEqual((job["kind"], job["series"], job["ep_no"]), ("manga", SLUG, 1))

    def test_parse_volume(self):
        cases = {"VOL 1 (JA)": 1, "Vol.01": 1, "v03": 3, "第12巻": 12, "7巻": 7,
                 "Chapter 004": 4, "Dandadan v12 (2023) (Digital)": 12,
                 "extras": None, "Volume 2": 2}
        for name, want in cases.items():
            self.assertEqual(MG.parse_volume(name), want, name)

    def test_group_volumes(self):
        root = "H:\\manga\\Dandadan"
        paths = []
        for v in (2, 1):
            paths += [f"{root}\\VOL {v} (JA)\\{n:03d}.jpg" for n in range(1, 6)]
        paths += [f"{root}\\extras\\{n}.png" for n in range(1, 4)]  # no number
        paths += [f"{root}\\cover.jpg", f"{root}\\VOL 3 (JA)\\only.jpg"]  # too few
        paths += [f"{root}\\VOL 1 (JA)\\._{n:03d}.jpg" for n in range(1, 6)]  # AppleDouble twins
        vols, unparsed = MG.group_volumes(paths, "H:/manga/Dandadan")
        self.assertEqual([(v["vol_no"], v["label"], v["pages"]) for v in vols],
                         [(1, "VOL 1 (JA)", 5), (2, "VOL 2 (JA)", 5)])
        self.assertEqual(unparsed, [f"{root}\\extras"])

    def test_series_folder_holding_pages_is_one_volume(self):
        paths = [f"H:\\manga\\OneShot\\{n:03d}.jpg" for n in range(1, 9)]
        vols, _ = MG.group_volumes(paths, "H:/manga/OneShot")
        self.assertEqual([(v["vol_no"], v["label"]) for v in vols], [(1, "OneShot")])


class TestBlocks(unittest.TestCase):
    def test_reading_order_right_to_left_top_to_bottom(self):
        ordered = MG.sort_blocks(OCR_P1["blocks"])
        self.assertEqual([b["lines"][0] for b in ordered],
                         ["犬が走る。", "公園へ ", "行こう", "猫のほうが"])

    def test_page_from_ocr(self):
        sentences = []
        page = MG.page_from_ocr(0, "001.jpg", OCR_P1, sentences)
        texts = [s["text"] for s in sentences]
        self.assertEqual(texts, ["犬が走る。", "速いね。", "公園へ行く。", "行こう", "猫のほうが速い！"])
        # bubbles carry per-line char counts (whitespace dropped) + their sentence runs
        b0, b1 = page["blocks"][0], page["blocks"][1]
        self.assertEqual((b0["lines"], b0["sents"]), ([5, 4], [0, 1]))
        self.assertEqual((b1["lines"], b1["sents"]), ([3, 3], [2]))
        # the lines concatenate back to the sentence text exactly
        self.assertEqual(sum(b0["lines"]), len("".join(texts[i] for i in b0["sents"])))
        # pseudo-times: page index × PAGE_SECS, bubbles a tenth apart
        self.assertEqual([s["start"] for s in sentences][:4], [0.0, 0.0, 0.1, 0.2])
        page2 = MG.page_from_ocr(3, "004.jpg", OCR_P2, sentences)
        self.assertEqual(sentences[-1]["start"], 3 * MG.PAGE_SECS)
        self.assertFalse(page2["blocks"][0]["vertical"])

    def test_line_boxes_ride_along_when_polygons_pair_with_the_lines(self):
        ocr = {"img_width": 764, "img_height": 1200, "blocks": [
            {"box": [100, 50, 220, 300], "vertical": True, "font_size": 22,
             "lines": ["公園へ ", "", "行く。"],
             "lines_coords": [[[190, 52], [220, 50], [219, 180], [190, 181]],
                              [[160, 50], [186, 50], [186, 60], [160, 60]],
                              [[100, 50], [130, 50], [130, 300], [100, 300]]]},
            # polygon count off (a furigana column mokuro counted): box only
            {"box": [500, 40, 640, 320], "vertical": True, "font_size": 22,
             "lines": ["犬が走る。"], "lines_coords": [[[500, 40], [640, 40], [640, 320], [500, 320]]] * 2},
        ]}
        sentences = []
        page = MG.page_from_ocr(0, "p.jpg", ocr, sentences)
        b1, b0 = page["blocks"]  # reading order: the right-hand bubble first
        # the empty line is dropped with its polygon; boxes are axis-aligned
        self.assertEqual(b0["lines"], [3, 3])
        self.assertEqual(b0["line_boxes"], [[190.0, 50.0, 220.0, 181.0], [100.0, 50.0, 130.0, 300.0]])
        self.assertNotIn("line_boxes", b1)
        # the AI read pairs by the raw line order too
        read = {"0": {"lines": ["公園へ", "行く。"], "gloss": None}}
        ocr["blocks"][0]["lines_coords"] = ocr["blocks"][0]["lines_coords"][::2]
        page = MG.page_from_ocr(0, "p.jpg", ocr, [], read=read, glosses={})
        self.assertEqual(len(page["blocks"][1]["line_boxes"]), 2)  # the 公園 bubble, read second

    def test_blocks_without_japanese_are_not_dialogue(self):
        ocr = {"img_width": 100, "img_height": 100, "blocks": [
            {"box": [0, 0, 10, 10], "vertical": False, "font_size": 8, "lines": ["ｍａｎｇｏ「ｅｏｄｅｒ．ｔｏ"]},
            {"box": [0, 20, 10, 30], "vertical": False, "font_size": 8, "lines": ["12"]},
            {"box": [0, 40, 10, 50], "vertical": True, "font_size": 8, "lines": ["ん？"]}]}
        sentences = []
        page = MG.page_from_ocr(0, "p.jpg", ocr, sentences)
        self.assertEqual([s["text"] for s in sentences], ["ん？"])
        self.assertEqual(len(page["blocks"]), 1)

    def test_read_replaces_draft_and_glosses_every_sentence_of_the_bubble(self):
        sentences, glosses = [], {}
        read = {"2": {"lines": ["犬が走る。", "速いね。"], "gloss": "The dog runs. Fast, huh."},
                "0": {"lines": [], "gloss": None},  # dropped: a watermark
                "1": {"lines": ["公園へ", "行く。"], "gloss": ""}}
        page = MG.page_from_ocr(0, "001.jpg", OCR_P1, sentences, read=read, glosses=glosses)
        texts = [x["text"] for x in sentences]
        self.assertEqual(texts, ["犬が走る。", "速いね。", "公園へ行く。", "行こう"])
        self.assertEqual(glosses, {0: "The dog runs. Fast, huh.", 1: "The dog runs. Fast, huh."})
        self.assertEqual([b["k"] for b in page["blocks"]], [2, 1, 3])

    def test_split_keeps_every_character(self):
        self.assertEqual(MG.split_sentences("え…ちょっ！？いや"), ["え…", "ちょっ！", "？", "いや"])
        self.assertEqual("".join(MG.split_sentences("犬が走る。公園")), "犬が走る。公園")


class FakeRemote:
    """Stands in for the ssh side: OCR + pulls land fixture files locally."""

    def __init__(self, tmp):
        self.calls = []
        self.tmp = Path(tmp)
        self.timeout = 5
        self.host = "fake"

    def listing(self, remote_dir):
        root = str(remote_dir).replace("/", "\\")
        return [f"{root}\\VOL 1 (JA)\\{n:03d}.jpg" for n in range(1, 4)]


def fake_remote_ocr(remote, mcfg, src_dir, out_dir, log=print, timeout=None):
    remote.calls.append(("ocr", src_dir, out_dir))
    d = remote.tmp / "remote_ocr"
    d.mkdir(exist_ok=True)
    write_json(d / "001.json", OCR_P1)
    write_json(d / "002.json", OCR_P2)
    write_json(d / "003.json", {"blocks": [], "img_width": 764, "img_height": 1200})
    write_json(d / "_done.json", {"pages": ["001.jpg", "002.jpg", "003.jpg"],
                                  "page_count": 3, "elapsed": 1.5})


def fake_pull_tree(remote, remote_dir, local_dir, log=print):
    remote.calls.append(("pull", remote_dir, str(local_dir)))
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    if "fullpipe_manga" in str(remote_dir):  # the OCR cache
        for p in (remote.tmp / "remote_ocr").glob("*.json"):
            (local_dir / p.name).write_bytes(p.read_bytes())
    else:  # the page scans
        for n in range(1, 4):
            (local_dir / f"{n:03d}.jpg").write_bytes(b"\xff\xd8jpeg")


class TestMangaStage1(ServerTestBase):
    def ingest_and_run(self):
        conn = q.open_queue(Path(self.cfg["work_dir"]) / "queue.db")
        remote = FakeRemote(self.tmp.name)
        summary = MG.ingest(self.cfg, "H:/manga/Dandadan", remote=remote, log=lambda m: None)
        self.assertEqual(summary["enqueued"], [VOL_EP])
        job = q.get_job(conn, VOL_EP)
        self.assertEqual((job["state"], job["series"], job["ep_no"], job["title"]),
                         ("queued", SLUG, 1, "Dandadan Vol. 1"))
        with unittest.mock.patch.object(MG, "Remote", lambda mcfg: remote), \
                unittest.mock.patch.object(MG, "remote_ocr", fake_remote_ocr), \
                unittest.mock.patch.object(MG, "pull_tree", fake_pull_tree):
            process_job(self.cfg, conn, job, log=lambda m: None)
        return conn, remote, q.get_job(conn, VOL_EP)

    def test_worker_manga_path_lands_prepared(self):
        conn, remote, job = self.ingest_and_run()
        self.assertEqual(job["state"], "prepared", job.get("error"))
        self.assertEqual(job["kind"], "manga")
        self.assertEqual([c[0] for c in remote.calls], ["ocr", "pull", "pull"])

        ep_dir = episode_dir(self.cfg, VOL_EP)
        transcript = read_json(ep_dir / "transcript.json")
        coverage = read_json(ep_dir / "coverage.json")
        doc = read_json(ep_dir / "manga.json")
        self.assertEqual(transcript["episode"]["kind"], "manga")
        self.assertEqual(transcript["episode"]["uploader"], "Dandadan")
        texts = [s["text"] for s in transcript["sentences"]]
        self.assertEqual(texts[0], "犬が走る。")
        self.assertEqual(len(coverage["sentences"]), len(texts))
        self.assertEqual(doc["page_count"], 3)
        self.assertEqual([p["file"] for p in doc["pages"]], ["001.jpg", "002.jpg", "003.jpg"])
        self.assertEqual(doc["pages"][2]["blocks"], [])  # an empty page stays a page
        self.assertEqual(doc["pages"][0]["blocks"][0]["sents"], [0, 1])
        self.assertTrue((ep_dir / "pages" / "002.jpg").exists())

        lconn = lc.open_db(self.cfg["ledger_db"])
        row = lconn.execute("SELECT kind, watched FROM episodes WHERE id=?",
                            (VOL_EP,)).fetchone()
        self.assertEqual((row["kind"], row["watched"]), ("manga", 0))
        # manifest remembers the pull
        man = MG.load_manifest(self.cfg, SLUG)
        self.assertEqual(man["volumes"][0]["page_count"], 3)

    def test_rerun_skips_ocr_when_cached(self):
        conn, remote, _ = self.ingest_and_run()
        remote.calls.clear()
        q.set_state(conn, VOL_EP, "queued")
        with unittest.mock.patch.object(MG, "Remote", lambda mcfg: remote), \
                unittest.mock.patch.object(MG, "remote_ocr", fake_remote_ocr), \
                unittest.mock.patch.object(MG, "pull_tree", fake_pull_tree):
            process_job(self.cfg, conn, q.get_job(conn, VOL_EP), log=lambda m: None)
        self.assertEqual(remote.calls, [])  # OCR json + pages already local
        self.assertEqual(q.get_job(conn, VOL_EP)["state"], "prepared")

    def test_routes_taps_read_and_stage2(self):
        conn, _, _ = self.ingest_and_run()
        r = self.client.get(f"/manga/{VOL_EP}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["page_count"], 3)
        self.assertEqual(self.client.get("/manga/yt_nope", headers=self.auth).status_code, 404)
        # page scans: header auth or ?t=, no path tricks
        r = self.client.get(f"/manga/{VOL_EP}/page/001.jpg?t=sekrit")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"\xff\xd8jpeg")
        self.assertEqual(self.client.get(f"/manga/{VOL_EP}/page/001.jpg").status_code, 401)
        self.assertEqual(self.client.get(f"/manga/{VOL_EP}/page/..%2Fmanga.json",
                                         headers=self.auth).status_code, 404)
        # transcript serves the pseudo-timed sentences with tokens
        t = self.client.get(f"/transcript/{VOL_EP}", headers=self.auth).json()
        self.assertEqual(t["sentences"][-1]["start"], MG.PAGE_SECS)
        self.assertTrue(t["sentences"][0]["tokens"])
        jobs = self.client.get("/jobs", headers=self.auth).json()
        self.assertEqual(jobs[0]["kind"], "manga")

        # taps: evidence only, no card selection, no state change
        r = self.client.post("/taps", headers=self.auth, json={
            "episode_id": VOL_EP, "batch_id": "b1", "taps": [["公園", "k", "", "none", "manga"]]})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["cards_selected"])
        self.assertEqual(self.client.get(f"/jobs/{VOL_EP}", headers=self.auth)
                         .json()["state"], "prepared")

        # a sitting over page 1 only: exposure credit follows the pages viewed
        r = self.client.post("/viewtime", headers=self.auth, json={
            "id": "s1", "episode_id": VOL_EP, "kind": "read", "day": "2026-09-14",
            "start": "2026-09-14T10:00:00", "secs": 40, "reached": 30.0,
            "duration": 3 * MG.PAGE_SECS, "played": [[0.0, 30.0]]})
        self.assertEqual(r.status_code, 200)
        lconn = lc.open_db(self.cfg["ledger_db"])
        cov = lc.load_coverage(lconn)[VOL_EP]
        self.assertEqual(cov["ranges"], [(0.0, 30.0)])  # a read sitting credits like a watch
        self.assertEqual(self.client.get("/viewtime", headers=self.auth).json()
                         ["sessions"][0]["kind"], "read")
        credit = lc._exposure_credit({"episode_id": VOL_EP, "watched": 0},
                                     {"occ": 1, "at": [0.0], "t": 0.0}, {VOL_EP: cov})
        self.assertEqual(credit, (1.0, True))
        credit = lc._exposure_credit({"episode_id": VOL_EP, "watched": 0},
                                     {"occ": 1, "at": [30.0], "t": 30.0}, {VOL_EP: cov})
        self.assertEqual(credit, (1.0, True))  # page 2's start sits on the range edge
        credit = lc._exposure_credit({"episode_id": VOL_EP, "watched": 0},
                                     {"occ": 1, "at": [60.0], "t": 60.0}, {VOL_EP: cov})
        self.assertEqual(credit, (0.0, False))

        # the manga pass writes curate.json alone → staged (no prep.html needed);
        # its whole-line glosses ride on the transcript sentences
        write_json(episode_dir(self.cfg, VOL_EP) / "curate.json", {
            "defs": [], "lines": [{"idx": 0, "gloss": "The dog runs."}, {"idx": "x", "gloss": "no"}]})
        scan_stage2(self.cfg, conn, log=lambda m: None)
        self.assertEqual(q.get_job(conn, VOL_EP)["state"], "staged")
        t = self.client.get(f"/transcript/{VOL_EP}", headers=self.auth).json()
        self.assertTrue(t["curated"])
        self.assertEqual(t["sentences"][0]["gloss"], "The dog runs.")
        self.assertNotIn("gloss", t["sentences"][1])

        # finished: exposures activate, never cards
        r = self.client.post(f"/watched/{VOL_EP}", headers=self.auth, json={"cards": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cards"]["note"], "manga — no cards")
        self.join_closeout(VOL_EP)
        self.assertEqual(self.client.get(f"/jobs/{VOL_EP}", headers=self.auth)
                         .json()["state"], "watched")

        # series-style row: DELETE is refused without force (phone-local delete)
        self.assertEqual(self.client.delete(f"/jobs/{VOL_EP}", headers=self.auth).status_code, 409)

    def test_ai_read_prep_apply_and_transcript_glosses(self):
        conn, _, _ = self.ingest_and_run()
        # real jpgs so the annotated renders can be made
        from PIL import Image
        ep_dir = episode_dir(self.cfg, VOL_EP)
        for f in ("001.jpg", "002.jpg", "003.jpg"):
            Image.new("RGB", (764, 1200), "white").save(ep_dir / "pages" / f)
        man = MG.read_prep(self.cfg, VOL_EP, log=lambda m: None)
        todo = [p["stem"] for p in man["pages"] if p["blocks"] and not p["done"]]
        self.assertEqual(todo, ["001", "002"])  # page 3 has no boxes
        self.assertTrue((ep_dir / "ocr" / "read" / "pages" / "001.png").exists())
        self.assertEqual(man["pages"][0]["blocks"][2]["draft"], ["犬が走る。", "速いね。"])
        with self.assertRaises(RuntimeError):  # nothing read yet
            MG.read_apply(self.cfg, VOL_EP, log=lambda m: None)
        write_json(ep_dir / "ocr" / "read" / "001.json", {"blocks": {
            "2": {"lines": ["犬が走る。", "速いね。"], "gloss": "The dog runs. Fast, huh."},
            "1": {"lines": ["公園へ", "行く。"], "gloss": None},
            "3": {"lines": ["行こう"], "gloss": "Let's go."},
            "0": {"lines": ["猫のほうが", "速い！"], "gloss": None}}})
        write_json(ep_dir / "ocr" / "read" / "002.json", {"blocks": {
            "0": {"lines": ["終わり"], "gloss": "The end."}}})  # corrected おわり
        st = MG.read_apply(self.cfg, VOL_EP, log=lambda m: None)
        self.assertEqual((st["read"], st["pages_with_text"]), (2, 2))
        doc = read_json(ep_dir / "manga.json")
        self.assertEqual(doc["read_by"], "opus")
        t = self.client.get(f"/transcript/{VOL_EP}", headers=self.auth).json()
        texts = ["".join(x["s"] for x in s_["tokens"]) for s_ in t["sentences"]]
        self.assertIn("終わり", texts)
        self.assertEqual(t["sentences"][0]["gloss"], "The dog runs. Fast, huh.")
        self.assertEqual(t["sentences"][1]["gloss"], "The dog runs. Fast, huh.")
        self.assertNotIn("gloss", t["sentences"][2])
        self.assertFalse(t["curated"])  # the pass (defs, synopsis) hasn't run
        self.assertTrue(MG.read_status(self.cfg, VOL_EP)["applied"])

    def test_library_and_ingest_routes(self):
        remote = FakeRemote(self.tmp.name)
        lib = [{"name": "Dandadan", "slug": SLUG, "remote_dir": "H:/manga/Dandadan",
                "volumes": [{"vol_no": 1, "label": "VOL 1 (JA)", "pages": 3}]}]
        with unittest.mock.patch.object(MG, "library", return_value=lib), \
                unittest.mock.patch.object(MG, "Remote", lambda mcfg: remote):
            r = self.client.get("/manga/library", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.json()["series"][0]["volumes"][0]["state"])
            r = self.client.post("/manga/ingest", headers=self.auth,
                                 json={"remote_dir": "H:/manga/Dandadan", "volumes": [1]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["enqueued"], [VOL_EP])
            r = self.client.get("/manga/library", headers=self.auth)
            self.assertEqual(r.json()["series"][0]["volumes"][0]["state"], "queued")
            self.assertEqual(self.client.post("/manga/ingest", headers=self.auth,
                                              json={}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
