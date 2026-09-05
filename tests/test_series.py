"""tools.series: identity, episode-number parsing, PC-folder pairing, the
queue/ledger plumbing, and the server's series-aware routes. The PC itself is
never contacted — Remote is stubbed."""

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from engine import srt_parser as SP
from ledger import ledgerctl as lc
from server import jobqueue as q
from server.app import create_app
from tools import series as S
from tools._staging import episode_dir


class IdentityTest(unittest.TestCase):
    def test_source_roundtrip(self):
        self.assertEqual(S.parse_series_source("series://hotspot/3"), ("hotspot", 3))
        self.assertEqual(S.series_source("hotspot", 3), "series://hotspot/3")
        self.assertIsNone(S.parse_series_source("https://youtu.be/x"))
        self.assertIsNone(S.parse_series_source("/tmp/a.mkv"))

    def test_episode_id_is_stable_and_queue_derives_it(self):
        self.assertEqual(S.series_episode_id("series://hotspot/1"), "ser_hotspot_e01")
        self.assertEqual(S.series_episode_id("series://show/203"), "ser_show_e203")
        self.assertEqual(q.derive_job_id("series://hotspot/1"), "ser_hotspot_e01")

    def test_slugify(self):
        self.assertEqual(S.slugify("Hot Spot (2025)"), "hot-spot-2025")


class EpisodeParseTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            "Hot.Spot.EP01.1080p.NF.WEB-DL.AAC2.0.H.264-MagicStar.mkv": (None, 1),
            "[Sub] Show - 07 [1080p][x264].mkv": (None, 7),
            "Show.S02E03.720p.mkv": (2, 3),
            "show s1e12.mp4": (1, 12),
            "第3話 タイトル.mp4": (None, 3),
            "Episode 10.mkv": (None, 10),
            "Frieren 05.mkv": (None, 5),
            "Movie (2010) 1080p.mkv": (None, None),
        }
        for name, want in cases.items():
            self.assertEqual(S.parse_episode(name), want, name)

    def test_order_and_label(self):
        self.assertEqual(S.ep_no_of(2, 3), 203)
        self.assertEqual(S.ep_no_of(None, 3), 3)
        self.assertEqual(S.ep_label(2, 3), "S2E03")
        self.assertEqual(S.ep_label(None, 3), "EP03")

    def test_spec(self):
        self.assertEqual(S.parse_episode_spec("1,3-5"), {1, 3, 4, 5})
        self.assertIsNone(S.parse_episode_spec(None))


class GroupFilesTest(unittest.TestCase):
    def test_pairs_japanese_sidecar_by_episode_number(self):
        d = r"E:\Japanese\drama\hotspot"
        sub = d + r"\[MagicStar] Hot Spot EP01 [WEBDL] [JPN_ENG_CHT_SUB]"
        paths = [
            d + r"\Hot.Spot.EP02.1080p.mkv",
            d + r"\Hot.Spot.EP01.1080p.mkv",
            sub + r"\Hot.Spot.EP01.1080p.Cht.srt",
            sub + r"\Hot.Spot.EP01.1080p.Eng.srt",
            sub + r"\Hot.Spot.EP01.1080p.Jpn.srt",
            d + r"\cover.jpg",
        ]
        eps, unparsed = S.group_files(paths)
        self.assertEqual([e["ep_no"] for e in eps], [1, 2])
        self.assertTrue(eps[0]["remote_subs"].endswith("Jpn.srt"))  # not the folder's JPN tag
        self.assertIsNone(eps[1]["remote_subs"])
        self.assertEqual(unparsed, [])

    def test_lone_untagged_srt_is_assumed_to_match(self):
        eps, _ = S.group_files([r"D:\s\Show - 01.mkv", r"D:\s\Show - 01.srt"])
        self.assertTrue(eps[0]["remote_subs"].endswith("01.srt"))

    def test_two_sub_languages_untagged_pick_none(self):
        eps, _ = S.group_files([r"D:\s\Show - 01.mkv", r"D:\s\Show - 01.a.srt",
                                r"D:\s\Show - 01.b.srt"])
        self.assertIsNone(eps[0]["remote_subs"])

    def test_pick_streams_prefers_japanese_audio_and_text_subs(self):
        probe = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "height": 1080},
            {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
            {"index": 2, "codec_type": "audio", "tags": {"language": "jpn"}},
            {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
             "tags": {"language": "jpn"}},
            {"index": 4, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "jpn"}},
        ]}
        video, a_idx, sub = S._pick_streams(probe)
        self.assertEqual(video["index"], 0)
        self.assertEqual(a_idx, 1)  # second audio stream = jpn
        self.assertEqual(sub, 4)  # bitmap subs skipped

    def test_transcode_cmd_scales_only_when_taller(self):
        v = {"height": 1080}
        cmd = S.transcode_cmd(r"E:\a b\x.mkv", "I:/stage/x.mp4", v, 1, cap=480)
        self.assertIn("scale=-2:480", cmd)
        self.assertIn("h264_nvenc", cmd)
        self.assertIn('"E:\\a b\\x.mkv"', cmd)
        self.assertIn("-map 0:a:1", cmd)
        cmd = S.transcode_cmd("a.mkv", "b.mp4", {"height": 480}, 0, cap=480, encoder="x264")
        self.assertNotIn("scale=", cmd)
        self.assertIn("libx264", cmd)


class MarkupStripTest(unittest.TestCase):
    def test_netflix_markup(self):
        cases = {
            "\u202a-（男の子の父親）もう治ったか？\u202c \u202a-（男の子）うん\u202c": "もう治ったか？ うん",
            "\u202a（テレビ:キャスター）\u202c \u202a戦後 地元の住民によって⸺\u202c": "戦後 地元の住民によって",
            "富士浅田(ふじあさだ)冬祭りは": "富士浅田冬祭りは",
            "（遠藤(えんどう)）おお…": "おお…",
            "そしたら お客さんが… （足音）": "そしたら お客さんが…",
            "（はなをすする音）": "",
            "本当にいいのかな": "本当にいいのかな",
        }
        for src, want in cases.items():
            self.assertEqual(SP.strip_sub_markup(src), want, src)

    def test_strip_markup_removes_styling_tags(self):
        # ffmpeg .ass→.srt keeps <font>/<b> wrappers and {\\an8} overrides
        raw = ('<font face="Open Sans Semibold" size="45"><b>（雨の音）</b></font>'
               '<font face="Open Sans Semibold" size="45"><b>（警察官Ａ） '
               'まったく お前もツイてないな</b></font>')
        self.assertEqual(SP.strip_sub_markup(raw), "まったく お前もツイてないな")
        self.assertEqual(SP.strip_sub_markup("{\\an8}<i>行くぞ</i>"), "行くぞ")
        # reading glosses after Latin / full-width acronyms, also inside a cue
        self.assertEqual(SP.strip_sub_markup("MaxTac(マックスタック)を呼べ！"), "MaxTacを呼べ！")
        self.assertEqual(SP.strip_sub_markup("（ＡＶ(エーブイ)の飛行音） 何言ってんだよ"), "何言ってんだよ")
        self.assertEqual(SP.strip_sub_markup("せめて６：４(ろくよん)にならない？"), "せめて６：４にならない？")
        self.assertEqual(SP.strip_sub_markup("MaxTac()に任せて"), "MaxTacに任せて")  # Netflix leaves empty glosses

    def test_strip_markup_drops_emptied_cues(self):
        subs = [(0, 1, "（足音）"), (1, 2, "\u202aそっか\u202c")]
        self.assertEqual(SP.strip_markup(subs), [(1, 2, "そっか")])


class QueueAndLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.cfg = {"work_dir": str(self.work), "ledger_db": str(self.work / "ledger.db")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_carries_series_identity(self):
        conn = q.open_queue(self.work / "queue.db")
        job, created = q.enqueue(conn, "series://hotspot/2", title="Hot Spot EP02",
                                 series="hotspot", series_title="Hot Spot", ep_no=2)
        self.assertTrue(created)
        self.assertEqual(job["id"], "ser_hotspot_e02")
        self.assertEqual((job["series"], job["series_title"], job["ep_no"]),
                         ("hotspot", "Hot Spot", 2))
        self.assertEqual(job["kind"], "episode")
        self.assertEqual(job["title"], "Hot Spot EP02")
        # plain rows keep null series fields (old-DB migration shape)
        plain, _ = q.enqueue(conn, "https://youtu.be/abcDEF12345")
        self.assertIsNone(plain["series"])

    def test_ledger_persists_series_columns(self):
        conn = lc.open_db(self.cfg["ledger_db"])
        lc.record_exposure(conn, {"id": "ser_hotspot_e01", "title": "Hot Spot EP01",
                                  "source": "series://hotspot/1", "kind": "series",
                                  "channel": "Hot Spot", "channel_id": "series:hotspot",
                                  "series": "hotspot", "ep_no": 1, "duration": 2755.0}, {})
        row = conn.execute("SELECT series, ep_no, channel, duration FROM episodes "
                           "WHERE id='ser_hotspot_e01'").fetchone()
        self.assertEqual(tuple(row), ("hotspot", 1, "Hot Spot", 2755.0))

    def test_manifest_roundtrip_and_evict(self):
        man = {"slug": "hotspot", "title": "Hot Spot", "remote_dir": "E:/x", "cap": 480,
               "episodes": [{"ep_no": 1, "label": "EP01", "id": "ser_hotspot_e01"},
                            {"ep_no": 2, "label": "EP02", "id": "ser_hotspot_e02"}]}
        S.save_manifest(self.cfg, man)
        self.assertEqual(S.load_manifest(self.cfg, "hotspot")["title"], "Hot Spot")
        self.assertEqual([m["slug"] for m in S.list_series(self.cfg)], ["hotspot"])
        conn = q.open_queue(self.work / "queue.db")
        for n in (1, 2):
            q.enqueue(conn, f"series://hotspot/{n}", series="hotspot", ep_no=n)
            (episode_dir(self.cfg, f"ser_hotspot_e0{n}", create=True) / "video.mp4").write_bytes(b"x" * 10)
            (episode_dir(self.cfg, f"ser_hotspot_e0{n}") / "coverage.json").write_text("{}")
        q.set_state(conn, "ser_hotspot_e01", "watched")
        q.set_state(conn, "ser_hotspot_e02", "staged")
        res = S.evict(self.cfg, "hotspot", log=lambda m: None)
        self.assertEqual(res["evicted"], ["EP01"])
        self.assertEqual(res["kept"], ["EP02 (staged)"])
        self.assertFalse(S.video_path(self.cfg, "hotspot", 1).exists())
        self.assertTrue(S.video_path(self.cfg, "hotspot", 2).exists())
        # derived data untouched
        self.assertTrue((episode_dir(self.cfg, "ser_hotspot_e01") / "coverage.json").exists())
        res = S.evict(self.cfg, "hotspot", all_states=True, log=lambda m: None)
        self.assertEqual(res["evicted"], ["EP01", "EP02"])

    def test_materialize_pulls_from_stage_copy(self):
        man = {"slug": "hotspot", "title": "Hot Spot", "remote_dir": "E:/x", "cap": 480,
               "episodes": [{"ep_no": 1, "label": "EP01", "id": "ser_hotspot_e01",
                             "remote_video": r"E:\x\ep1.mkv", "remote_subs": None}]}
        S.save_manifest(self.cfg, man)
        remote = unittest.mock.Mock()
        remote.exists.return_value = True
        remote.size.return_value = 123

        def scp(remote_path, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_bytes(b"v" if remote_path.endswith(".mp4") else b"1\n")
            return Path(local_path)
        remote.scp_from.side_effect = scp
        dest = S.materialize(self.cfg, "hotspot", 1, log=lambda m: None, remote=remote)
        self.assertTrue(dest.exists())
        self.assertTrue(S.local_subs_path(self.cfg, "hotspot", 1).exists())
        self.assertEqual(remote.scp_from.call_count, 2)
        # second call: nothing to pull, nothing to transcode
        S.materialize(self.cfg, "hotspot", 1, log=lambda m: None, remote=remote)
        self.assertEqual(remote.scp_from.call_count, 2)
        self.assertEqual(S.load_manifest(self.cfg, "hotspot")["episodes"][0]["size"], 1)


class ServerRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = Path(self.tmp.name)
        self.cfg = {"work_dir": str(work), "ledger_db": str(work / "ledger.db"),
                    "anki_connect_url": "http://localhost:1", "server": {"token": "t"}}
        self.app = create_app(self.cfg, start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer t"}
        conn = q.open_queue(work / "queue.db")
        q.enqueue(conn, "series://hotspot/1", title="Hot Spot EP01", series="hotspot",
                  series_title="Hot Spot", ep_no=1)
        q.set_state(conn, "ser_hotspot_e01", "watched", episode_id="ser_hotspot_e01")
        S.save_manifest(self.cfg, {"slug": "hotspot", "title": "Hot Spot", "remote_dir": "E:/x",
                                   "cap": 480, "episodes": [{"ep_no": 1, "label": "EP01",
                                                             "id": "ser_hotspot_e01"}]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_jobs_carry_series_fields(self):
        jobs = self.client.get("/jobs", headers=self.auth).json()
        self.assertEqual((jobs[0]["series"], jobs[0]["series_title"], jobs[0]["ep_no"]),
                         ("hotspot", "Hot Spot", 1))

    def test_delete_refused_without_force(self):
        r = self.client.delete("/jobs/ser_hotspot_e01", headers=self.auth)
        self.assertEqual(r.status_code, 409)
        self.assertIn("series", r.json()["detail"])
        r = self.client.delete("/jobs/ser_hotspot_e01?force=true", headers=self.auth)
        self.assertEqual(r.status_code, 200)

    def test_missing_series_video_triggers_restore(self):
        calls = []

        def fake_materialize(cfg, slug, ep_no, log=None, remote=None):
            calls.append((slug, ep_no))
            d = episode_dir(cfg, "ser_hotspot_e01", create=True)
            (d / "video.mp4").write_bytes(b"\x00" * 16)
        with unittest.mock.patch.object(S, "materialize", fake_materialize):
            r = self.client.get("/video/ser_hotspot_e01?t=t")
            self.assertEqual(r.status_code, 503)
            self.assertIn("restored", r.json()["detail"])
        # the restore ran in a background thread — poll for its output
        import time
        for _ in range(50):
            if (episode_dir(self.cfg, "ser_hotspot_e01") / "video.mp4").exists():
                break
            time.sleep(0.05)
        self.assertEqual(calls, [("hotspot", 1)])
        r = self.client.get("/video/ser_hotspot_e01?t=t")
        self.assertEqual(r.status_code, 200)

    def test_missing_plain_video_is_404(self):
        conn = q.open_queue(Path(self.cfg["work_dir"]) / "queue.db")
        q.enqueue(conn, "https://youtu.be/abcDEF12345")
        r = self.client.get("/video/yt_abcDEF12345?t=t")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
