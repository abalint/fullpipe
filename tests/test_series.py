"""tools.series: identity, episode-number parsing, folder pairing, the
Mac-side prepare (ffmpeg stubbed), the stage tier on the media server (a
temp dir stands in for the t7 share), the queue/ledger plumbing, and the
server's series-aware routes."""

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
from tools import library as L
from tools import series as S
from tools._staging import episode_dir


JA_SRT = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n".encode("utf-8")

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
        cmd = S.transcode_cmd("/Volumes/library/Japanese/a b/x.mkv", "/w/x.mp4", v, 1, cap=480)
        self.assertIn("scale=-2:480,format=yuv420p", cmd)
        self.assertIn("h264_videotoolbox", cmd)
        self.assertIn("/Volumes/library/Japanese/a b/x.mkv", cmd)  # argv, no shell quoting
        self.assertEqual(cmd[cmd.index("-map", cmd.index("-map") + 1) + 1], "0:a:1")
        cmd = S.transcode_cmd("a.mkv", "b.mp4", {"height": 480}, 0, cap=480, encoder="x264")
        self.assertNotIn("scale=-2:480,format=yuv420p", cmd)
        self.assertIn("libx264", cmd)


class LibraryPathsTest(unittest.TestCase):
    """tools.library: desktop-era paths map onto the mount; nothing outside
    the configured mounts is ever mounted."""

    CFG = {"library": {"mounts": {}, "series_root": "/Volumes/library/Japanese",
                       "manga_root": "/Volumes/library/Japanese/manga",
                       "legacy_roots": {"E:/Japanese": "/Volumes/library/Japanese",
                                        "H:/manga": "/Volumes/library/Japanese/manga"}}}

    def test_resolve(self):
        self.assertEqual(L.resolve(self.CFG, r"E:\Japanese\drama\hotspot\ep1.mkv"),
                         "/Volumes/library/Japanese/drama/hotspot/ep1.mkv")
        self.assertEqual(L.resolve(self.CFG, "H:/manga/Dandadan/VOL 1 (JA)"),
                         "/Volumes/library/Japanese/manga/Dandadan/VOL 1 (JA)")
        self.assertEqual(L.resolve(self.CFG, "drama/hotspot"),
                         "/Volumes/library/Japanese/drama/hotspot")
        self.assertEqual(L.resolve(self.CFG, "/Volumes/library/Japanese/anime/X/"),
                         "/Volumes/library/Japanese/anime/X")
        with self.assertRaises(FileNotFoundError):
            L.resolve(self.CFG, "Z:/nowhere/x.mkv")

    def test_mount_of_and_ensure(self):
        cfg = {"library": {"mounts": {"library": "/Volumes/library", "t7": "/Volumes/t7"}}}
        self.assertEqual(L.mount_of(cfg, "/Volumes/t7/fullpipe_stage/x.mp4"), ("t7", "/Volumes/t7"))
        self.assertIsNone(L.mount_of(cfg, "/tmp/elsewhere"))
        with unittest.mock.patch.object(L, "mount_share") as ms:
            L.ensure_mounted(cfg, "/tmp/elsewhere")  # not ours — never mounts
            ms.assert_not_called()


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

    def _library(self):
        """A stand-in for the mounted shares: an original + sidecar under
        <tmp>/library, an empty stage dir under <tmp>/t7."""
        lib_dir, stage = self.work / "library" / "drama" / "hotspot", self.work / "t7" / "stage"
        lib_dir.mkdir(parents=True)
        (lib_dir / "Hot.Spot.EP01.1080p.mkv").write_bytes(b"ORIGINAL")
        (lib_dir / "Hot.Spot.EP01.Jpn.srt").write_bytes(JA_SRT)
        self.cfg["library"] = {"mounts": {}, "series_root": str(self.work / "library"),
                               "stage_dir": str(stage),
                               "legacy_roots": {"E:/Japanese": str(self.work / "library")}}
        man = {"slug": "hotspot", "title": "Hot Spot", "remote_dir": "E:/Japanese/drama/hotspot",
               "cap": 480, "episodes": [{
                   "ep_no": 1, "label": "EP01", "id": "ser_hotspot_e01",
                   "remote_video": r"E:\Japanese\drama\hotspot\Hot.Spot.EP01.1080p.mkv",
                   "remote_subs": r"E:\Japanese\drama\hotspot\Hot.Spot.EP01.Jpn.srt"}]}
        S.save_manifest(self.cfg, man)
        return lib_dir, stage

    @staticmethod
    def _fake_ffmpeg(argv):
        out = Path(argv[-1])  # the output is always last
        if out.suffix == ".srt":  # a subtitle extraction: real Japanese text
            out.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n", encoding="utf-8")
        else:
            out.write_bytes(b"v")

    @staticmethod
    def _fake_ffmpeg_english_subs(argv):
        out = Path(argv[-1])
        if out.suffix == ".srt":  # a "jpn"-tagged track that is really English
            out.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n", encoding="utf-8")
        else:
            out.write_bytes(b"v")

    def test_materialize_transcodes_then_parks_a_stage_copy(self):
        lib_dir, stage = self._library()
        probe = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "height": 1080},
                             {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
                 "format": {"duration": "2755.07"}}
        with unittest.mock.patch.object(S, "probe", return_value=probe), \
                unittest.mock.patch.object(S, "_ffmpeg", side_effect=self._fake_ffmpeg) as ff:
            dest = S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
        self.assertEqual(dest.read_bytes(), b"v")
        argv = ff.call_args_list[0].args[0]
        self.assertIn("h264_videotoolbox", argv)
        self.assertEqual(argv[argv.index("-i") + 1], str(lib_dir / "Hot.Spot.EP01.1080p.mkv"))
        self.assertEqual(S.local_subs_path(self.cfg, "hotspot", 1).read_bytes(), JA_SRT)
        # stage tier: the copy + srt are parked on the (fake) t7 share
        self.assertEqual((stage / "hotspot" / "hotspot-e01.mp4").read_bytes(), b"v")
        self.assertTrue((stage / "hotspot" / "hotspot-e01.ja.srt").exists())
        ep = S.load_manifest(self.cfg, "hotspot")["episodes"][0]
        self.assertEqual((ep["subs"], ep["duration"], ep["size"]), ("sidecar", 2755.07, 1))
        # original untouched
        self.assertEqual((lib_dir / "Hot.Spot.EP01.1080p.mkv").read_bytes(), b"ORIGINAL")

        # evict, then fetch: the stage copy is copied back, no transcode
        S.evict(self.cfg, "hotspot", all_states=True, log=lambda m: None)
        self.assertFalse(dest.exists())
        with unittest.mock.patch.object(S, "_ffmpeg") as ff, \
                unittest.mock.patch.object(S, "probe") as pr:
            S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
            ff.assert_not_called()
            pr.assert_not_called()
        self.assertEqual(dest.read_bytes(), b"v")
        # nothing to do the second time either
        with unittest.mock.patch.object(S, "_ffmpeg") as ff:
            S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
            ff.assert_not_called()

    def test_materialize_extracts_embedded_subs_and_survives_no_stage(self):
        lib_dir, stage = self._library()
        self.cfg["library"]["stage_dir"] = ""  # no stage tier configured
        man = S.load_manifest(self.cfg, "hotspot")
        man["episodes"][0]["remote_subs"] = None
        S.save_manifest(self.cfg, man)
        probe = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc", "height": 720},
                             {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
                             {"index": 2, "codec_type": "audio", "tags": {"language": "jpn"}},
                             {"index": 3, "codec_type": "subtitle", "codec_name": "ass",
                              "tags": {"language": "jpn"}}],
                 "format": {"duration": "10"}}
        with unittest.mock.patch.object(S, "probe", return_value=probe), \
                unittest.mock.patch.object(S, "_ffmpeg", side_effect=self._fake_ffmpeg) as ff:
            S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
        calls = [c.args[0] for c in ff.call_args_list]
        self.assertEqual(len(calls), 2)  # transcode + subtitle extraction
        self.assertIn("0:a:1", calls[0])  # jpn is the second audio stream
        self.assertEqual(calls[1][calls[1].index("-map") + 1], "0:3")
        self.assertEqual(S.load_manifest(self.cfg, "hotspot")["episodes"][0]["subs"], "embedded")

    def test_materialize_skips_english_sidecar_for_embedded_jpn_track(self):
        # fan-sub box sets (Terrace House NF rips): an untagged English .ass
        # beside the mkv pairs as "the" subtitle; the jpn track inside wins
        lib_dir, stage = self._library()
        self.cfg["library"]["stage_dir"] = ""
        (lib_dir / "Hot.Spot.EP01.ass").write_text("[Script Info]\n", encoding="utf-8")
        man = S.load_manifest(self.cfg, "hotspot")
        man["episodes"][0]["remote_subs"] = r"E:\Japanese\drama\hotspot\Hot.Spot.EP01.ass"
        S.save_manifest(self.cfg, man)
        probe = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "height": 1080},
                             {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
                             {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                              "tags": {"language": "jpn"}}],
                 "format": {"duration": "10"}}

        def ffmpeg(argv):
            out = Path(argv[-1])
            if out.suffix != ".srt":
                out.write_bytes(b"v")
            elif "-map" in argv:  # the embedded jpn track
                out.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n", encoding="utf-8")
            else:  # the English fan-sub sidecar
                out.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n", encoding="utf-8")

        with unittest.mock.patch.object(S, "probe", return_value=probe), \
                unittest.mock.patch.object(S, "_ffmpeg", side_effect=ffmpeg):
            S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
        self.assertIn("こんにちは", S.local_subs_path(self.cfg, "hotspot", 1).read_text(encoding="utf-8"))
        self.assertEqual(S.load_manifest(self.cfg, "hotspot")["episodes"][0]["subs"], "embedded")

    def test_materialize_rejects_embedded_track_mislabeled_jpn(self):
        # dual-audio BD rips tag the English-for-Japanese-audio track "jpn":
        # the extracted text must be checked, and a non-Japanese track dropped
        lib_dir, stage = self._library()
        self.cfg["library"]["stage_dir"] = ""
        man = S.load_manifest(self.cfg, "hotspot")
        man["episodes"][0]["remote_subs"] = None
        S.save_manifest(self.cfg, man)
        probe = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc", "height": 720},
                             {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
                             {"index": 2, "codec_type": "subtitle", "codec_name": "ass",
                              "tags": {"language": "jpn"}}],
                 "format": {"duration": "10"}}
        with unittest.mock.patch.object(S, "probe", return_value=probe), \
                unittest.mock.patch.object(S, "_ffmpeg", side_effect=self._fake_ffmpeg_english_subs):
            S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
        self.assertIsNone(S.load_manifest(self.cfg, "hotspot")["episodes"][0]["subs"])
        self.assertFalse(S.local_subs_path(self.cfg, "hotspot", 1).exists())
        self.assertFalse((self.work / "t7").exists())


class ArchiveTierTest(unittest.TestCase):
    """The curation artifacts' tier on the media server (a temp dir stands
    in for the t7 share): archive mirrors, evict --artifacts is gated on it,
    materialize / restore bring everything back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.archive = self.work / "t7" / "archive"
        self.stage = self.work / "t7" / "stage"
        self.cfg = {"work_dir": str(self.work), "ledger_db": str(self.work / "ledger.db"),
                    "library": {"mounts": {}, "series_root": str(self.work / "library"),
                                "stage_dir": str(self.stage), "archive_dir": str(self.archive),
                                "legacy_roots": {}}}
        S.save_manifest(self.cfg, {
            "slug": "hotspot", "title": "Hot Spot", "remote_dir": "drama/hotspot", "cap": 480,
            "episodes": [{"ep_no": 1, "label": "EP01", "id": "ser_hotspot_e01",
                          "title": "Hot Spot EP01", "remote_video": "x/EP01.mkv"},
                         {"ep_no": 2, "label": "EP02", "id": "ser_hotspot_e02",
                          "title": "Hot Spot EP02", "remote_video": "x/EP02.mkv"}]})
        S.local_subs_path(self.cfg, "hotspot", 1).write_text("1\n", encoding="utf-8")
        self.conn = q.open_queue(self.work / "queue.db")
        for n in (1, 2):
            q.enqueue(self.conn, f"series://hotspot/{n}", title=f"Hot Spot EP0{n}",
                      series="hotspot", series_title="Hot Spot", ep_no=n)
            q.set_state(self.conn, f"ser_hotspot_e0{n}", "watched", episode_id=f"ser_hotspot_e0{n}")
        self._stage_local("ser_hotspot_e01")

    def tearDown(self):
        self.tmp.cleanup()

    def _stage_local(self, ep):
        d = episode_dir(self.cfg, ep, create=True)
        for name, body in (("transcript.json", '{"sentences": []}'), ("coverage.json", "{}"),
                           ("curate.json", '{"synopsis": "x"}'), ("sentences.srt", "1\n")):
            (d / name).write_text(body, encoding="utf-8")
        (d / "video.mp4").write_bytes(b"VIDEO")
        (d / "video.mp4.part.mp4").write_bytes(b"junk")
        (d / "clips").mkdir(exist_ok=True)
        (d / "clips" / "c1.mp3").write_bytes(b"clip")
        dl = self.work / "downloads"
        dl.mkdir(exist_ok=True)
        (dl / f"{ep}.ja.srt").write_text("1\n", encoding="utf-8")
        (dl / f"{ep}.ja.words.json").write_text("[]", encoding="utf-8")
        (dl / f"{ep}.mp3").write_bytes(b"audio")
        return d

    def test_archive_mirrors_artifacts_not_video_and_backfills_stage(self):
        res = S.archive(self.cfg, "hotspot", log=lambda m: None)
        root = self.archive / "hotspot"
        ep = root / "episodes" / "ser_hotspot_e01"
        self.assertTrue((ep / "transcript.json").exists())
        self.assertTrue((ep / "curate.json").exists())
        self.assertTrue((ep / "clips" / "c1.mp3").exists())
        self.assertFalse((ep / "video.mp4").exists())            # video is the stage tier's
        self.assertFalse((ep / "video.mp4.part.mp4").exists())   # in-flight junk skipped
        self.assertTrue((root / "downloads" / "ser_hotspot_e01.ja.srt").exists())
        self.assertTrue((root / "downloads" / "ser_hotspot_e01.ja.words.json").exists())
        self.assertFalse((root / "downloads" / "ser_hotspot_e01.mp3").exists())
        self.assertTrue((root / "series" / "series.json").exists())
        self.assertTrue((root / "series" / "hotspot-e01.ja.srt").exists())
        self.assertTrue((root / "README.txt").exists())
        snap = S.read_json(root / "series" / "queue.json")
        self.assertEqual({j["id"]: j["state"] for j in snap["jobs"]},
                         {"ser_hotspot_e01": "watched", "ser_hotspot_e02": "watched"})
        # the Mac's 480p copy had no stage copy yet → parked (desktop-era backfill)
        self.assertEqual((self.stage / "hotspot" / "hotspot-e01.mp4").read_bytes(), b"VIDEO")
        self.assertEqual(res["staged"], ["EP01"])
        self.assertEqual(res["copied"], 9)
        # a second pass copies nothing (size + mtime match), never deletes
        res2 = S.archive(self.cfg, "hotspot", log=lambda m: None)
        self.assertEqual((res2["copied"], res2["skipped"]), (0, 9))
        self.assertTrue(S.archive_status(self.cfg, "hotspot", "ser_hotspot_e01")["current"])
        # re-curation changes a file → only that file goes again
        import os, time
        cur = episode_dir(self.cfg, "ser_hotspot_e01") / "curate.json"
        cur.write_text('{"synopsis": "longer text"}', encoding="utf-8")
        os.utime(cur, (time.time() + 10, time.time() + 10))
        st = S.archive_status(self.cfg, "hotspot", "ser_hotspot_e01")
        self.assertEqual((st["archived"], st["current"], st["missing"]),
                         (True, False, ["episodes/ser_hotspot_e01/curate.json"]))
        res3 = S.archive(self.cfg, "hotspot", log=lambda m: None)
        self.assertEqual(res3["copied"], 1)
        self.assertIn("longer", (ep / "curate.json").read_text(encoding="utf-8"))

    def test_evict_artifacts_is_gated_on_a_current_archive(self):
        d = episode_dir(self.cfg, "ser_hotspot_e01")
        res = S.evict(self.cfg, "hotspot", artifacts=True, log=lambda m: None)
        self.assertEqual(res["evicted"], ["EP02"])  # nothing local for EP02: nothing to lose
        self.assertEqual(len(res["kept"]), 1)
        self.assertTrue(res["kept"][0].startswith("EP01 (not archived: episodes/ser_hotspot_e01/"), res["kept"])
        self.assertTrue((d / "transcript.json").exists())
        S.archive(self.cfg, "hotspot", log=lambda m: None)
        res = S.evict(self.cfg, "hotspot", artifacts=True, log=lambda m: None)
        self.assertEqual(res["evicted"], ["EP01", "EP02"])
        self.assertFalse(d.exists())
        self.assertEqual(list((self.work / "downloads").glob("ser_hotspot_e01.*")), [])
        # the manifest + subs beside it stay on the Mac (tiny; the phone's rows still resolve)
        self.assertTrue(S.manifest_path(self.cfg, "hotspot").exists())
        # status reads the server-side tiers
        st = S.status(self.cfg, "hotspot")["episodes"][0]
        self.assertEqual((st["video_local"], st["artifacts_local"], st["staged"], st["archived"]),
                         (False, False, True, True))

    def test_materialize_restores_artifacts_before_the_video(self):
        S.archive(self.cfg, "hotspot", log=lambda m: None)
        S.evict(self.cfg, "hotspot", artifacts=True, log=lambda m: None)
        d = episode_dir(self.cfg, "ser_hotspot_e01")
        self.assertFalse(d.exists())
        S.materialize(self.cfg, "hotspot", 1, log=lambda m: None)
        self.assertEqual((d / "video.mp4").read_bytes(), b"VIDEO")            # stage copy
        self.assertTrue((d / "transcript.json").exists())                     # archive
        self.assertTrue((d / "clips" / "c1.mp3").exists())
        self.assertTrue((self.work / "downloads" / "ser_hotspot_e01.ja.words.json").exists())
        self.assertFalse((self.work / "downloads" / "ser_hotspot_e01.mp3").exists())  # nothing reads it

    def test_restore_brings_back_files_and_queue_rows(self):
        S.archive(self.cfg, "hotspot", log=lambda m: None)
        S.evict(self.cfg, "hotspot", artifacts=True, log=lambda m: None)
        # a fresh Mac: no manifest, no queue rows
        import shutil
        shutil.rmtree(S.series_dir(self.cfg, "hotspot"))
        q.delete_job(self.conn, "ser_hotspot_e01")
        q.delete_job(self.conn, "ser_hotspot_e02")
        res = S.restore(self.cfg, "hotspot", log=lambda m: None)
        self.assertTrue(S.manifest_path(self.cfg, "hotspot").exists())
        self.assertTrue(S.local_subs_path(self.cfg, "hotspot", 1).exists())
        self.assertTrue((episode_dir(self.cfg, "ser_hotspot_e01") / "curate.json").exists())
        self.assertFalse((episode_dir(self.cfg, "ser_hotspot_e01") / "video.mp4").exists())
        self.assertEqual(sorted(res["queue_rows"]), ["ser_hotspot_e01", "ser_hotspot_e02"])
        job = q.get_job(self.conn, "ser_hotspot_e01")
        self.assertEqual((job["state"], job["series"], job["ep_no"]), ("watched", "hotspot", 1))

    def test_remove_remote_drops_stage_and_archive(self):
        S.archive(self.cfg, "hotspot", log=lambda m: None)
        self.cfg["ledger_db"] = str(self.work / "ledger.db")
        S.remove(self.cfg, "hotspot", remote_too=True, log=lambda m: None)
        self.assertFalse((self.archive / "hotspot").exists())
        self.assertFalse((self.stage / "hotspot").exists())

    def test_no_archive_tier_is_quiet(self):
        self.cfg["library"]["archive_dir"] = ""
        self.assertEqual(S.restore_artifacts(self.cfg, "hotspot", "ser_hotspot_e01"), 0)
        self.assertEqual(S.archive_status(self.cfg, "hotspot", "ser_hotspot_e01"),
                         {"archived": False, "current": False, "missing": None})
        with self.assertRaises(RuntimeError):
            S.archive(self.cfg, "hotspot", log=lambda m: None)


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

    def test_restore_503_carries_retry_after(self):
        with unittest.mock.patch.object(S, "materialize", lambda *a, **k: None):
            r = self.client.get("/video/ser_hotspot_e01?t=t")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("retry-after"), "10")

    def test_missing_series_artifacts_restore_instead_of_404(self):
        """The phone pulls prep/subs after the video: an episode whose
        derived files were evicted to the media server answers 503 (restore
        kicked off) rather than 404, then serves once the files are back."""
        calls = []

        def fake_restore(cfg, slug, ep_id, log=None, overwrite=False):
            calls.append((slug, ep_id))
            d = episode_dir(cfg, ep_id, create=True)
            (d / "sentences.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n",
                                             encoding="utf-8")
            (d / "transcript.json").write_text('{"sentences": []}', encoding="utf-8")
            (d / "coverage.json").write_text('{"sentences": []}', encoding="utf-8")
            return 1
        with unittest.mock.patch.object(S, "restore_artifacts", fake_restore):
            r = self.client.get("/video/ser_hotspot_e01/subs?t=t")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.headers.get("retry-after"), "10")
        import time
        for _ in range(50):
            if (episode_dir(self.cfg, "ser_hotspot_e01") / "sentences.srt").exists():
                break
            time.sleep(0.05)
        self.assertEqual(calls, [("hotspot", "ser_hotspot_e01")])
        self.assertEqual(self.client.get("/video/ser_hotspot_e01/subs?t=t").status_code, 200)
        # a plain (non-series) episode is still a 404
        self.assertEqual(self.client.get("/prep/yt_nothing", headers=self.auth).status_code, 404)
        # nothing archived → the next request says so (404) instead of polling forever
        (episode_dir(self.cfg, "ser_hotspot_e01") / "sentences.srt").unlink()
        with unittest.mock.patch.object(S, "restore_artifacts", lambda *a, **k: 0):
            self.assertEqual(self.client.get("/video/ser_hotspot_e01/subs?t=t").status_code, 503)
            for _ in range(50):
                if not any(th.name == "restore-ser_hotspot_e01" and th.is_alive()
                           for th in __import__("threading").enumerate()):
                    break
                time.sleep(0.05)
            r = self.client.get("/video/ser_hotspot_e01/subs?t=t")
        self.assertEqual(r.status_code, 404)
        self.assertIn("nothing archived", r.json()["detail"])

    def test_watched_series_episode_is_archived_in_the_background(self):
        calls = []

        def fake_archive(cfg, slug, ep_nos=None, log=None):
            calls.append((slug, ep_nos))
            return {}
        self.cfg["library"] = {"archive_dir": str(Path(self.tmp.name) / "t7" / "archive")}
        job = q.get_job(q.open_queue(Path(self.tmp.name) / "queue.db"), "ser_hotspot_e01")
        with unittest.mock.patch.object(S, "archive", fake_archive):
            t = self.app.state.archive_series_episode(job)
            t.join(5)
        self.assertEqual(calls, [("hotspot", {1})])
        # nothing for a non-series job, or without an archive tier
        self.assertIsNone(self.app.state.archive_series_episode({"source": "https://youtu.be/x"}))
        self.cfg["library"]["archive_dir"] = ""
        self.assertIsNone(self.app.state.archive_series_episode(job))

    def test_missing_plain_video_is_404(self):
        conn = q.open_queue(Path(self.cfg["work_dir"]) / "queue.db")
        q.enqueue(conn, "https://youtu.be/abcDEF12345")
        r = self.client.get("/video/yt_abcDEF12345?t=t")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()


def test_group_sequential_numbers_by_date_and_keeps_known():
    d = "/Volumes/library/Japanese/baseball/2026/"
    paths = [d + "【公式】B VS C(2026年3月27日)｜試合速報｜パ・リーグ.com.ts",
             d + "【公式】A VS D(2026年4月1日)｜試合速報.ts",
             d + "【公式】A VS B(2026年3月27日)｜試合速報.ts",
             d + "【公式】A VS B(2026年3月27日)｜試合速報.ja.srt"]
    eps, unparsed = S.group_sequential(paths)
    assert unparsed == []
    assert [e["name"] for e in eps] == ["A VS B(2026年3月27日)", "B VS C(2026年3月27日)",
                                        "A VS D(2026年4月1日)"]
    assert [e["ep_no"] for e in eps] == [1, 2, 3]
    assert eps[0]["remote_subs"].endswith(".ja.srt") and eps[1]["remote_subs"] is None
    # a re-ingest keeps assigned numbers; a new earlier-dated file appends
    known = {e["remote_video"]: e["ep_no"] for e in eps}
    eps2, _ = S.group_sequential(paths + [d + "【公式】X VS Y(2026年3月20日).ts"], known)
    assert {e["name"]: e["ep_no"] for e in eps2}["X VS Y(2026年3月20日)"] == 4
    assert {e["name"]: e["ep_no"] for e in eps2}["A VS B(2026年3月27日)"] == 1
