"""tools.ambience: the catalog + the server's routes. ffmpeg does the real
work (noise synthesis + the loop seam), so the tests exercise it for real
on tiny loops; YouTube is never touched."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import ambience as A  # noqa: E402
from tests.test_server import ServerTestBase  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestLoops(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"work_dir": self.tmp.name, "ambience": {"xfade": 1.0}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_noise_loop_lands_in_catalog(self):
        e = A.add_noise(self.cfg, "brown", seconds=6, log=lambda m: None)
        self.assertEqual(e["id"], "brown-noise")
        self.assertEqual(e["file"], "sounds/brown-noise.ogg")
        self.assertAlmostEqual(e["ms"] / 1000, 6, delta=0.3)  # the seam adds nothing
        self.assertTrue((A.ambience_root(self.cfg) / e["file"]).exists())
        cat = A.load_catalog(self.cfg)
        self.assertEqual([s["id"] for s in cat["sounds"]], ["brown-noise"])
        self.assertEqual(cat["bytes"], e["bytes"])
        # re-adding replaces, never duplicates
        A.add_noise(self.cfg, "brown", seconds=6, log=lambda m: None)
        self.assertEqual(len(A.load_catalog(self.cfg)["sounds"]), 1)

    def test_loop_from_a_longer_source_is_cut_at_start(self):
        src = Path(self.tmp.name) / "src.wav"
        A._ffmpeg(["-f", "lavfi", "-i", "anoisesrc=color=white:duration=30:sample_rate=48000",
                   "-c:a", "pcm_s16le", str(src)], "src")
        dst = Path(self.tmp.name) / "loop.ogg"
        got = A.build_loop(src, dst, seconds=8, xfade=1.0, kbps=48, lufs=-22, start=5)
        self.assertEqual(got, 8)
        self.assertAlmostEqual(A.probe_seconds(dst), 8, delta=0.3)
        # a source that can't cover the loop shrinks it instead of failing
        got = A.build_loop(src, dst, seconds=60, xfade=1.0, kbps=48, lufs=-22, start=20)
        self.assertAlmostEqual(got, 9, delta=0.1)

    def test_remove_and_rebuild(self):
        A.add_noise(self.cfg, "white", seconds=6, log=lambda m: None)
        A.add_noise(self.cfg, "pink", seconds=6, log=lambda m: None)
        gone = A.remove(self.cfg, "white-noise")
        self.assertEqual(gone, ["sounds/white-noise.ogg"])
        self.assertFalse((A.ambience_root(self.cfg) / "sounds/white-noise.ogg").exists())
        with self.assertRaises(ValueError):
            A.remove(self.cfg, "nope")
        # a file deleted by hand drops out on rebuild
        (A.ambience_root(self.cfg) / "sounds/pink-noise.ogg").unlink()
        self.assertEqual(A.rebuild(self.cfg)["sounds"], [])


class TestMoods(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"work_dir": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def test_mood_ids_are_slugs(self):
        with self.assertRaises(ValueError):
            A.ensure_mood(self.cfg, "Lo Fi")
        m = A.ensure_mood(self.cfg, "lo-fi", title="Lo-fi focus", emoji="🎧")
        self.assertEqual(m["title"], "Lo-fi focus")
        A.ensure_mood(self.cfg, "lo-fi", title="Lo-fi")  # retitle, no duplicate
        cat = A.load_catalog(self.cfg)
        self.assertEqual([(m["id"], m["title"]) for m in cat["moods"]], [("lo-fi", "Lo-fi")])
        self.assertEqual(A.remove(self.cfg, "lo-fi"), [])
        self.assertEqual(A.load_catalog(self.cfg)["moods"], [])

    def test_video_id_parsing(self):
        self.assertEqual(A.video_id_of("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(A.video_id_of("https://youtu.be/dQw4w9WgXcQ?t=3"), "dQw4w9WgXcQ")
        self.assertIsNone(A.video_id_of("https://www.youtube.com/playlist?list=PLabc"))


class TestRoutes(ServerTestBase):
    def test_empty_catalog_then_files(self):
        r = self.client.get("/ambience", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sounds"], [])
        self.assertEqual(self.client.get("/ambience").status_code, 401)
        root = A.ambience_root(self.cfg, create=True)
        (root / "sounds").mkdir()
        (root / "sounds" / "rain.ogg").write_bytes(b"OggS")
        (root / "music" / "focus").mkdir(parents=True)
        (root / "music" / "focus" / "abc.ogg").write_bytes(b"OggS")
        r = self.client.get("/ambience/sounds/rain.ogg?t=sekrit")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/ogg")
        self.assertEqual(self.client.get("/ambience/sounds/rain.ogg").status_code, 401)
        self.assertEqual(self.client.get("/ambience/music/focus/abc.ogg?t=sekrit").status_code, 200)
        self.assertEqual(self.client.get("/ambience/music/focus/zzz.ogg?t=sekrit").status_code, 404)
        self.assertEqual(self.client.get("/ambience/sounds/..%2Fcatalog.json?t=sekrit").status_code, 404)


if __name__ == "__main__":
    unittest.main()
