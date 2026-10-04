"""Voice-track tests (tools/manga_voice.py): the script from the read's
voice fields / the agents' pages / the lettered text, cast merging and
voice resolution, the render plan + an idempotent render against a fake
ElevenLabs, the phone's index, and the server routes."""

import sys
import tempfile
import unittest.mock
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_server import ServerTestBase
from tools import manga as MG
from tools import manga_voice as MV
from tools._staging import episode_dir, read_json, write_json

EP = "manga_dandadan_v02"
SLUG = "dandadan"


def stage_volume(cfg, with_read_fields=True):
    """A two-page volume: manga.json + transcript.json + ocr/read files."""
    ep_dir = episode_dir(cfg, EP, create=True)
    sentences = [
        {"idx": 0, "start": 0.0, "end": 0.1, "text": "高倉健ってさ…"},
        {"idx": 1, "start": 0.1, "end": 0.2, "text": "だ、だって！"},
        {"idx": 2, "start": 0.1, "end": 0.2, "text": "こわいよ！"},
        {"idx": 3, "start": 0.2, "end": 0.3, "text": "ドドド"},
        {"idx": 4, "start": 30.0, "end": 30.1, "text": "ヒヒヒ…"},
    ]
    write_json(ep_dir / "transcript.json", {
        "episode": {"id": EP, "title": "Dandadan Vol. 2", "uploader": "Dandadan",
                    "source": "manga://dandadan/2", "kind": "manga"},
        "acquired_at": "2026-10-04T00:00:00+00:00", "punctuation_restored": False,
        "sentences": sentences})
    write_json(ep_dir / "manga.json", {
        "episode_id": EP, "slug": SLUG, "title": "Dandadan Vol. 2", "series_title": "Dandadan",
        "vol_no": 2, "label": "VOL 2 (JA)", "reading": "rtl", "page_secs": 30.0,
        "page_count": 2, "read_by": "opus", "pages_read": 2, "built_at": "x",
        "pages": [
            {"n": 0, "file": "001.jpg", "w": 764, "h": 1200, "blocks": [
                {"box": [500, 40, 640, 320], "vertical": True, "font_size": 20,
                 "lines": [7], "sents": [0], "k": 2},
                {"box": [100, 50, 220, 300], "vertical": True, "font_size": 20,
                 "lines": [5, 5], "sents": [1, 2], "k": 0},
                {"box": [80, 700, 200, 900], "vertical": True, "font_size": 20,
                 "lines": [3], "sents": [3], "k": 1}]},
            {"n": 1, "file": "002.jpg", "w": 764, "h": 1200, "blocks": [
                {"box": [300, 100, 400, 200], "vertical": True, "font_size": 20,
                 "lines": [4], "sents": [4], "k": 0}]},
        ]})
    write_json(ep_dir / "read" / "lines.json", {"episode_id": EP, "lines": [
        {"idx": 0, "gloss": "Ken Takakura, huh..."}]})
    p1 = {"file": "001.jpg", "blocks": {
        "2": {"lines": ["高倉健ってさ…"], "gloss": "Ken Takakura, huh..."},
        "0": {"lines": ["だ、だって！", "こわいよ！"], "gloss": None},
        "1": {"lines": ["ドドド"], "gloss": "[rumble]"}}, "missed": [], "notes": ""}
    p2 = {"file": "002.jpg", "blocks": {"0": {"lines": ["ヒヒヒ…"], "gloss": None}}}
    if with_read_fields:
        p1["blocks"]["2"].update({"speaker": "モモ", "say": "たかくらけんってさ…"})
        p1["blocks"]["0"].update({"speaker": "オカルン", "say": "だ、だって！「こわいよ！」"})
        p1["blocks"]["1"].update({"speaker": None, "say": None})
        p1["cast"] = [{"name": "モモ", "kana": "もも", "gender": "female", "age": "teen", "note": "heroine"},
                      {"name": "オカルン", "kana": "おかるん", "gender": "male", "age": "teen", "note": "nerd"}]
        p2["blocks"]["0"].update({"speaker": "ターボババア", "say": "ヒヒヒ…"})
        p2["cast"] = [{"name": "ターボババア", "kana": "たーぼばばあ", "gender": "female", "age": "old",
                       "note": "yokai"}]
    write_json(ep_dir / "ocr" / "read" / "001.json", p1)
    write_json(ep_dir / "ocr" / "read" / "002.json", p2)
    MG.manga_dir(cfg, SLUG, create=True)
    return ep_dir


class FakeClient:
    """Stands in for MV.ElevenLabs: records every tts call, returns a
    distinct byte string per text, and can refuse request stitching."""

    def __init__(self, reject_context=False, fail_text=None):
        self.calls = []
        self.reject_context = reject_context
        self.fail_text = fail_text
        self._no_context = False

    def subscription(self):
        return {"tier": "starter", "character_count": 100, "character_limit": 40000}

    def tts(self, voice_id, text, model_id, language=None, previous_text=None, next_text=None,
            output_format="mp3_44100_64", seed=None, voice_settings=None):
        self.calls.append({"voice_id": voice_id, "text": text, "model": model_id,
                           "language": language, "previous_text": previous_text,
                           "next_text": next_text, "format": output_format})
        if text == self.fail_text:
            raise RuntimeError("ElevenLabs tts 500: boom")
        return f"MP3:{voice_id}:{text}".encode("utf-8"), "req-1"


class TestText(unittest.TestCase):
    def test_tts_text_normalizes_what_the_voice_trips_on(self):
        self.assertEqual(MV.tts_text("「こわいよ！」"), "こわいよ！")
        self.assertEqual(MV.tts_text("いいや．．．そんな..."), "いいや…そんな…")
        self.assertEqual(MV.tts_text("ええ…… と　思う"), "ええ…と思う")
        self.assertEqual(MV.tts_text(None), "")
        self.assertEqual(MV.tts_text("（笑）"), "笑")

    def test_credits_per_char(self):
        self.assertEqual(MV.credits_per_char("eleven_v3"), 1.0)
        self.assertEqual(MV.credits_per_char("eleven_flash_v2_5"), 0.5)
        self.assertEqual(MV.credits_per_char("eleven_v4_turbo"), 0.5)

    def test_parse_pages(self):
        self.assertEqual(MV.parse_pages("3"), (3, 3))
        self.assertEqual(MV.parse_pages("1-20"), (1, 20))
        self.assertIsNone(MV.parse_pages(None))
        with self.assertRaises(ValueError):
            MV.parse_pages("a-b")


class TestScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"work_dir": self.tmp.name,
                    "manga": {"voice": {"default_voices": {"female": "F", "male": "M", "neutral": "N"}}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_fields_make_the_script_and_the_cast(self):
        stage_volume(self.cfg)
        man = MV.voice_prep(self.cfg, EP, log=lambda m: None)
        self.assertEqual([p["done"] for p in man["pages"]], [True, True])  # scripted by the read
        self.assertEqual(sorted(man["cast"]), ["オカルン", "ターボババア", "モモ"])
        self.assertEqual(man["pages"][0]["blocks"][0]["gloss"], "Ken Takakura, huh...")
        out = MV.voice_apply(self.cfg, EP, log=lambda m: None)
        script = read_json(MV.voice_dir(self.cfg, EP) / "script.json")
        says = [(c["speaker"], c["say"], c["sents"]) for c in script["clips"]]
        self.assertEqual(says, [("モモ", "たかくらけんってさ…", [0]),
                                ("オカルン", "だ、だって！こわいよ！", [1, 2]),  # brackets dropped, one clip
                                ("ターボババア", "ヒヒヒ…", [4])])
        self.assertEqual((out["skipped"], out["defaulted"]), (1, 0))  # the sound effect
        cast = MV.load_cast(self.cfg, SLUG)["cast"]
        self.assertEqual(cast["ターボババア"]["age"], "old")
        self.assertIsNone(cast["モモ"]["voice_id"])

    def test_agent_pages_fill_in_for_a_read_without_voice_fields(self):
        ep_dir = stage_volume(self.cfg, with_read_fields=False)
        man = MV.voice_prep(self.cfg, EP, log=lambda m: None)
        self.assertEqual([p["done"] for p in man["pages"]], [False, False])
        with self.assertRaises(RuntimeError):  # nothing scripted
            MV.voice_apply(self.cfg, EP, log=lambda m: None)
        vdir = MV.voice_dir(self.cfg, EP)
        write_json(vdir / "pages" / "001.json", {"blocks": {
            "2": {"speaker": "モモ", "say": "たかくらけんってさ…"},
            "0": {"speaker": "オカルン", "say": "だ、だって！こわいよ！"},
            "1": {"speaker": None, "say": None}},
            "cast": [{"name": "モモ", "kana": "もも", "gender": "female", "age": "teen"}]})
        write_json(vdir / "pages" / "002.json", {"blocks": {}})  # agent forgot the block
        out = MV.voice_apply(self.cfg, EP, log=lambda m: None)
        script = read_json(vdir / "script.json")
        self.assertEqual([c["say"] for c in script["clips"]],
                         ["たかくらけんってさ…", "だ、だって！こわいよ！", "ヒヒヒ…"])
        self.assertEqual(out["defaulted"], 1)  # the forgotten bubble says its lettered text
        cast = MV.load_cast(self.cfg, SLUG)["cast"]
        self.assertEqual(cast["オカルン"]["note"], "(uncast: 1 bubbles)")  # named, never cast
        self.assertIsNone(cast["オカルン"]["gender"])
        del ep_dir

    def test_cast_merge_keeps_voices_and_fills_gaps(self):
        cast = {"モモ": {"kana": None, "gender": "female", "age": None, "note": None, "voice_id": "V1"}}
        added = MV.merge_cast(cast, [{"name": "モモ", "kana": "もも", "age": "teen"},
                                     {"name": "アイラ", "gender": "female"}, {"nope": 1}])
        self.assertEqual(added, ["アイラ"])
        self.assertEqual(cast["モモ"], {"kana": "もも", "gender": "female", "age": "teen",
                                       "note": None, "voice_id": "V1"})  # no dialect key added to an old entry
        self.assertIsNone(cast["アイラ"]["voice_id"])

    def test_resolve_voice(self):
        vcfg = MV.voice_cfg(self.cfg)
        cast = {"モモ": {"gender": "female", "voice_id": "MOMO"},
                "オカルン": {"gender": "male", "voice_id": None},
                "？": {"gender": None, "voice_id": None}}
        self.assertEqual(MV.resolve_voice(cast, "モモ", vcfg), ("MOMO", "モモ"))
        self.assertEqual(MV.resolve_voice(cast, "オカルン", vcfg), ("M", "default:male"))
        self.assertEqual(MV.resolve_voice(cast, "？", vcfg), ("N", "default:neutral"))
        self.assertEqual(MV.resolve_voice(cast, None, vcfg), ("N", "default:neutral"))
        self.assertEqual(MV.resolve_voice(cast, "unknown", vcfg), ("N", "default:neutral"))
        self.assertEqual(MV.resolve_voice(cast, "モモ", {"default_voices": {}}), ("MOMO", "モモ"))
        self.assertEqual(MV.resolve_voice(cast, None, {"default_voices": {}}), (None, None))
        # age / dialect pick the most specific default on offer
        dv = {"default_voices": {"female": "F", "female:old": "FO", "male:kansai": "MK",
                                 "kansai": "K", "male:old:kansai": "MOK"}}
        self.assertEqual(MV.resolve_voice({"婆": {"gender": "female", "age": "old"}}, "婆", dv), ("FO", "default:female:old"))
        self.assertEqual(MV.resolve_voice({"爺": {"gender": "male", "age": "old", "dialect": "kansai"}}, "爺", dv),
                         ("MOK", "default:male:old:kansai"))
        self.assertEqual(MV.resolve_voice({"x": {"gender": "male", "dialect": "kansai"}}, "x", dv), ("MK", "default:male:kansai"))
        self.assertEqual(MV.resolve_voice({"y": {"gender": "neutral", "dialect": "kansai"}}, "y", dv), ("K", "default:kansai"))
        self.assertEqual(MV.default_voice_keys({"gender": "female", "age": "teen", "dialect": "kyushu"}),
                         ["female:teen:kyushu", "female:kyushu", "female:teen", "kyushu", "female",
                          "neutral", "narration", "default"])

    def test_render_plan_and_an_idempotent_render(self):
        stage_volume(self.cfg)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)
        MV.save_cast(self.cfg, {"slug": SLUG, "cast": {
            **MV.load_cast(self.cfg, SLUG)["cast"],
            "モモ": {"gender": "female", "voice_id": "MOMO"}}})
        dry = MV.synthesize(self.cfg, EP, dry_run=True, log=lambda m: None)
        # たかくらけんってさ… (10) + だ、だって！こわいよ！ (11) + ヒヒヒ… (4)
        self.assertEqual((dry["to_render"], dry["kept"], dry["chars"]), (3, 0, 11 + 10 + 4))
        self.assertEqual(dry["by_voice"], {"モモ": 10, "default:male": 11, "default:female": 4})
        self.assertEqual(dry["credits"], 25.0)
        fake = FakeClient()
        out = MV.synthesize(self.cfg, EP, client=fake, log=lambda m: None)
        self.assertEqual((out["rendered"], out["errors"], out["indexed"]), (3, [], 3))
        vdir = MV.voice_dir(self.cfg, EP)
        self.assertEqual(sorted(p.name for p in (vdir / "clips").iterdir()),
                         ["001_0.mp3", "001_2.mp3", "002_0.mp3"])
        self.assertEqual((vdir / "clips" / "001_2.mp3").read_bytes(), "MP3:MOMO:たかくらけんってさ…".encode())
        # neighbours on the same page ride as context; page boundaries don't
        momo = next(c for c in fake.calls if c["voice_id"] == "MOMO")
        self.assertEqual((momo["previous_text"], momo["next_text"]), (None, "だ、だって！こわいよ！"))
        self.assertEqual(momo["language"], "ja")
        self.assertEqual(momo["model"], "eleven_v4")
        granny = next(c for c in fake.calls if c["text"] == "ヒヒヒ…")
        self.assertEqual((granny["previous_text"], granny["next_text"]), (None, None))
        idx = read_json(vdir / "index.json")
        by_file = {c["file"]: c for c in idx["clips"]}
        self.assertEqual(by_file["001_0.mp3"]["sents"], [1, 2])
        self.assertEqual(by_file["001_2.mp3"]["speaker"], "モモ")
        self.assertEqual(by_file["002_0.mp3"]["voice"], "default:female")
        # again: nothing changed → nothing rendered, index intact
        fake2 = FakeClient()
        out2 = MV.synthesize(self.cfg, EP, client=fake2, log=lambda m: None)
        self.assertEqual((out2["rendered"], out2["kept"], fake2.calls), (0, 3, []))
        # a voice change re-renders only that speaker's bubbles
        cast = MV.load_cast(self.cfg, SLUG)
        cast["cast"]["モモ"]["voice_id"] = "MOMO2"
        MV.save_cast(self.cfg, cast)
        fake3 = FakeClient()
        out3 = MV.synthesize(self.cfg, EP, client=fake3, log=lambda m: None)
        self.assertEqual((out3["rendered"], out3["kept"]), (1, 2))
        self.assertEqual([c["voice_id"] for c in fake3.calls], ["MOMO2"])
        # a bubble the script drops is removed from disk + index
        script = read_json(vdir / "script.json")
        script["clips"] = [c for c in script["clips"] if not (c["page"] == 1 and c["k"] == 0)]
        write_json(vdir / "script.json", script)
        out4 = MV.synthesize(self.cfg, EP, client=FakeClient(), log=lambda m: None)
        self.assertEqual(out4["indexed"], 2)
        self.assertFalse((vdir / "clips" / "002_0.mp3").exists())

    def test_render_errors_are_collected_not_fatal(self):
        stage_volume(self.cfg)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)
        fake = FakeClient(fail_text="ヒヒヒ…")
        out = MV.synthesize(self.cfg, EP, client=fake, log=lambda m: None)
        self.assertEqual((out["rendered"], len(out["errors"]), out["indexed"]), (2, 1, 2))
        self.assertEqual(out["errors"][0]["file"], "002_0.mp3")
        # the next run retries just the failed one
        out2 = MV.synthesize(self.cfg, EP, client=FakeClient(), log=lambda m: None)
        self.assertEqual((out2["rendered"], out2["indexed"]), (1, 3))

    def test_plan_refuses_an_uncast_speaker_without_defaults(self):
        self.cfg["manga"] = {}
        stage_volume(self.cfg)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)
        with self.assertRaises(RuntimeError) as cm:
            MV.plan(self.cfg, EP)
        self.assertIn("no voice for", str(cm.exception))

    def test_usage_check_refuses_a_run_that_does_not_fit(self):
        stage_volume(self.cfg)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)

        class Broke(FakeClient):
            def subscription(self):
                return {"tier": "free", "character_count": 9990, "character_limit": 10000}
        with self.assertRaises(RuntimeError):
            MV.synthesize(self.cfg, EP, client=Broke(), log=lambda m: None)
        out = MV.synthesize(self.cfg, EP, client=Broke(), force=True, log=lambda m: None)
        self.assertEqual(out["rendered"], 3)

    def test_pages_narrow_the_render(self):
        stage_volume(self.cfg)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)
        fake = FakeClient()
        out = MV.synthesize(self.cfg, EP, client=fake, pages=(2, 2), log=lambda m: None)
        self.assertEqual((out["rendered"], [c["text"] for c in fake.calls]), (1, ["ヒヒヒ…"]))


class TestClient(unittest.TestCase):
    def test_context_fields_are_dropped_when_the_model_rejects_them(self):
        import requests

        class R:
            def __init__(self, code, text=b"", headers=None):
                self.status_code, self.content, self.headers = code, text, headers or {}
                self.text = text.decode() if isinstance(text, bytes) else text
        seen = []

        def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
            seen.append(json)
            if "previous_text" in (json or {}):  # eleven_v3's actual answer (400 unsupported_model)
                return R(400, b'{"detail":{"code":"unsupported_model","message":"Providing previous_text or next_text is not yet supported"}}')
            return R(200, b"MP3")
        with unittest.mock.patch.object(requests, "request", fake_request):
            c = MV.ElevenLabs(api_key="k", sleep=lambda s: None)
            audio, _ = c.tts("V", "はい", "eleven_v3", language="ja", previous_text="前")
            self.assertEqual(audio, b"MP3")
            self.assertEqual(len(seen), 2)
            self.assertTrue(c._no_context)
            c.tts("V", "いいえ", "eleven_v3", previous_text="前")
            self.assertEqual(len(seen), 3)  # straight to the no-context call

    def test_retries_on_429_then_gives_up(self):
        import requests

        class R:
            status_code, text, content, headers = 429, "slow down", b"", {"retry-after": "0"}
        with unittest.mock.patch.object(requests, "request", lambda *a, **k: R()):
            c = MV.ElevenLabs(api_key="k", retries=3, sleep=lambda s: None)
            with self.assertRaises(RuntimeError) as cm:
                c.subscription()
            self.assertIn("gave up", str(cm.exception))

    def test_missing_key(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                MV.ElevenLabs()


class TestRoutes(ServerTestBase):
    def test_voice_index_and_clips(self):
        r = self.client.get(f"/manga/{EP}/voice", headers=self.auth)
        self.assertEqual(r.status_code, 404)  # no track = no audio, not an error
        self.cfg["manga"] = {"voice": {"default_voices": {"female": "F", "male": "M", "neutral": "N"}}}
        stage_volume(self.cfg)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)
        MV.synthesize(self.cfg, EP, client=FakeClient(), log=lambda m: None)
        r = self.client.get(f"/manga/{EP}/voice", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["clips"]), 3)
        r = self.client.get(f"/manga/{EP}/voice/001_2.mp3?t=sekrit")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/mpeg")
        self.assertEqual(r.content, "MP3:F:たかくらけんってさ…".encode())
        self.assertEqual(self.client.get(f"/manga/{EP}/voice/001_2.mp3").status_code, 401)
        self.assertEqual(self.client.get(f"/manga/{EP}/voice/..%2Fscript.json?t=sekrit").status_code, 404)
        self.assertEqual(self.client.get(f"/manga/{EP}/voice/nope.mp3?t=sekrit").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class TestArchive(unittest.TestCase):
    """The archive tier for manga (tools.manga archive / restore / remove):
    everything derived — reads, voice clips, coverage — mirrored onto the
    t7 stand-in; the page scans and read-prep renders are not; restore
    brings a volume back onto a bare Mac with its queue row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = Path(self.tmp.name)
        self.archive = work / "t7" / "archive"
        lib_root = work / "library" / "manga" / "Dandadan" / "VOL 2 (JA)"
        lib_root.mkdir(parents=True)
        for n in (1, 2):
            (lib_root / f"{n:03d}.jpg").write_bytes(b"\xff\xd8jpeg")
        self.cfg = {"work_dir": str(work), "ledger_db": str(work / "ledger.db"),
                    "library": {"mounts": {}, "manga_root": str(work / "library" / "manga"),
                                "archive_dir": str(self.archive), "legacy_roots": {}},
                    "manga": {"voice": {"default_voices": {"female": "F", "male": "M", "neutral": "N"}}}}
        stage_volume(self.cfg)
        ep_dir = episode_dir(self.cfg, EP)
        (ep_dir / "pages").mkdir()
        for n in (1, 2):
            (ep_dir / "pages" / f"{n:03d}.jpg").write_bytes(b"\xff\xd8jpeg")
        (ep_dir / "ocr" / "read" / "pages").mkdir(parents=True)
        (ep_dir / "ocr" / "read" / "pages" / "001.png").write_bytes(b"render")
        MG.save_manifest(self.cfg, {"slug": SLUG, "title": "Dandadan", "remote_dir": str(lib_root.parent),
                                    "created_at": "x", "volumes": [
                                        {"vol_no": 2, "label": "VOL 2 (JA)", "id": EP, "title": "Dandadan Vol. 2",
                                         "remote_dir": str(lib_root), "pages": 2, "page_count": 2}]})
        from server import jobqueue as q
        self.q = q
        self.conn = q.open_queue(work / "queue.db")
        q.enqueue(self.conn, "manga://dandadan/2", title="Dandadan Vol. 2", series=SLUG,
                  series_title="Dandadan", ep_no=2)
        q.set_state(self.conn, EP, "watched", episode_id=EP)
        MV.voice_prep(self.cfg, EP, log=lambda m: None)
        MV.voice_apply(self.cfg, EP, log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_archive_mirrors_derived_files_not_scans_and_render_archives(self):
        st = MG.archive_status(self.cfg, SLUG, EP)
        self.assertEqual((st["archived"], st["current"]), (False, False))
        with self.assertRaises(RuntimeError):  # not archived → remove refuses
            MG.remove(self.cfg, SLUG, log=lambda m: None)
        out = MV.synthesize(self.cfg, EP, client=FakeClient(), log=lambda m: None)
        self.assertGreater(out["archived"], 0)  # the render mirrored itself
        root = self.archive / "manga" / SLUG
        self.assertTrue((root / "episodes" / EP / "voice" / "clips" / "001_2.mp3").exists())
        self.assertTrue((root / "episodes" / EP / "ocr" / "read" / "001.json").exists())
        self.assertTrue((root / "episodes" / EP / "transcript.json").exists())
        self.assertTrue((root / "manga" / "manga.json").exists())
        self.assertTrue((root / "manga" / "cast.json").exists())
        self.assertFalse((root / "episodes" / EP / "pages").exists())  # the scans are the share's
        self.assertFalse((root / "episodes" / EP / "ocr" / "read" / "pages").exists())
        snap = read_json(root / "manga" / "queue.json")
        self.assertEqual(snap["jobs"][0]["state"], "watched")
        st = MG.archive_status(self.cfg, SLUG, EP)
        self.assertEqual((st["archived"], st["current"], st["missing"]), (True, True, []))
        again = MG.archive(self.cfg, SLUG, log=lambda m: None)
        self.assertEqual(again["copied"], 0)  # idempotent
        # now remove is allowed, and restore brings everything back onto a bare Mac
        MG.remove(self.cfg, SLUG, log=lambda m: None)
        self.assertFalse(episode_dir(self.cfg, EP).exists())
        self.assertFalse(MG.manifest_path(self.cfg, SLUG).exists())
        self.assertIsNone(self.q.get_job(self.conn, EP))
        res = MG.restore(self.cfg, SLUG, log=lambda m: None)
        self.assertEqual((res["series_files"], res["pages"], res["queue_rows"]), (2, 2, [EP]))
        ep_dir = episode_dir(self.cfg, EP)
        self.assertTrue((ep_dir / "voice" / "clips" / "001_2.mp3").exists())
        self.assertTrue((ep_dir / "voice" / "index.json").exists())
        self.assertTrue((ep_dir / "pages" / "002.jpg").exists())
        self.assertEqual(self.q.get_job(self.conn, EP)["state"], "watched")  # kept from the snapshot
        self.assertEqual(MV.load_cast(self.cfg, SLUG)["cast"]["モモ"]["kana"], "もも")

    def test_archive_all_and_no_tier(self):
        self.assertEqual([a["slug"] for a in MG.archive_all(self.cfg, log=lambda m: None)], [SLUG])
        self.cfg["library"]["archive_dir"] = ""
        self.assertEqual(MG.archive_status(self.cfg, SLUG, EP)["missing"], None)
        with self.assertRaises(RuntimeError):
            MG.archive(self.cfg, SLUG, log=lambda m: None)
        MG.remove(self.cfg, SLUG, log=lambda m: None)  # no tier configured → no gate
