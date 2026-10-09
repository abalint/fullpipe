#!/usr/bin/env python3
"""ambience — a library of background sound for reading: looping ambience
(rain, waves, fire, noise) and mood music playlists, built on the Mac from
YouTube audio and served to the phone, which plays them under the manga
reader (or anything else) with its own volume per layer.

Two kinds of entry, one catalog:

  sounds  — loops. A half-hour stretch cut from a long ambience video
            (`sound add`) or noise synthesized here (`sound gen white|pink|
            brown`), encoded as Opus and made seamless: the tail of the cut
            is crossfaded into its head, so the phone's gapless loop never
            clicks. The phone layers any number of these, each at its own
            volume (rain + fire).
  moods   — music playlists. Each mood (focus, calm, jazz, …) is a folder of
            audio-only YouTube mixes (`music add <mood> <url|id|playlist>`),
            loudness-matched so a slider position means the same thing across
            moods. The phone shuffles through a mood as one music channel.

Everything is loudness-normalized (EBU R128) with ONE fixed gain per file
(measure, then `volume=`; a limiter only as a clip guard) — never ffmpeg's
single-pass loudnorm, whose dynamic mode pumps on wave swells and thunder.
Sounds sit at a background level, music a little louder, so the two
sliders start from a sensible mix.

Sourcing is the skill's job (skills/ambience/SKILL.md): `search` wraps the
same unauthenticated yt-dlp search /recommend uses, and the agent judges the
candidates (long, no narration, well-listened) before `add`ing them.

Layout under <work_dir>/ambience/:
    catalog.json              what the phone fetches (GET /ambience)
    sounds/<id>.ogg           one loop per sound
    music/<mood>/<video>.ogg  one file per mix
    tmp/                      yt-dlp scratch (cleaned per add)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.paths import (  # noqa: E402
    _NOWWIN, ffmpeg_path, ffprobe_path, ytdlp_extra_args, ytdlp_path)
from lib_config import load_config  # noqa: E402
from tools._staging import read_json, write_json  # noqa: E402

AMBIENCE_DIR = "ambience"
NOISE_COLORS = ("white", "pink", "brown")

DEFAULTS = {
    # seconds of a sound loop (the file is this long; the phone loops it).
    # Half an hour: long enough that a thunderstorm's claps or a wave set
    # don't come round recognizably — ~14 MB a sound at 64 k Opus
    "sound_seconds": 1800,
    # seconds of the crossfade that joins the loop's tail into its head
    "xfade": 4.0,
    # Opus bitrates. Broadband noise (rain, surf, thunder) is the hardest
    # thing a codec meets — 64 k sounded underwater — so sounds get 128 k;
    # music is near-transparent at 96 k (YouTube's own opus track is ~130 k)
    "sound_kbps": 128,
    "music_kbps": 96,
    # EBU R128 integrated loudness targets (LUFS): background vs foreground
    "sound_lufs": -22,
    "music_lufs": -16,
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- config / paths -------------------------------------------------------------

def ambience_cfg(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("ambience") or {})
    return out


def ambience_root(cfg, create=False):
    d = Path(cfg["work_dir"]).expanduser() / AMBIENCE_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def catalog_path(cfg):
    return ambience_root(cfg) / "catalog.json"


def empty_catalog():
    return {"built_at": None, "sounds": [], "moods": []}


def load_catalog(cfg):
    p = catalog_path(cfg)
    if not p.exists():
        return empty_catalog()
    cat = read_json(p)
    cat.setdefault("sounds", [])
    cat.setdefault("moods", [])
    return cat


class catalog_lock:
    """Serialize catalog read-modify-write across processes (a sounds batch
    and a music batch adding side by side would otherwise lose entries)."""

    def __init__(self, cfg):
        self.path = ambience_root(cfg, create=True) / ".catalog.lock"

    def __enter__(self):
        import fcntl
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def save_catalog(cfg, cat):
    cat["built_at"] = now_iso()
    cat["bytes"] = sum(s.get("bytes", 0) for s in cat["sounds"]) + sum(
        t.get("bytes", 0) for m in cat["moods"] for t in m["tracks"])
    write_json(catalog_path(cfg), cat)
    return cat


def slugify(s):
    out = []
    for ch in s.lower().strip():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def _check_id(kind, ident):
    if not ident or ident != slugify(ident):
        raise ValueError(f"{kind} id must be a plain ascii slug (got {ident!r})")


# --- probing ------------------------------------------------------------------------

def probe_seconds(path):
    r = subprocess.run([ffprobe_path(), "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, **_NOWWIN)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe could not read {path}: {r.stderr.strip()}")


def _ffmpeg(args, what):
    cmd = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, **_NOWWIN)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({what}): {r.stderr.strip()[-800:]}")


# --- YouTube ------------------------------------------------------------------------

def video_id_of(source):
    """A bare 11-char id, a watch/youtu.be URL, or None (a playlist etc.)."""
    s = source.strip()
    if len(s) == 11 and all(c.isalnum() or c in "-_" for c in s):
        return s
    from engine.downloader import _extract_video_id
    return _extract_video_id(s)


def search(query, n=10, min_minutes=None):
    """Candidate sources: the /recommend search edge (tools.harvest.search),
    plus the fields a sourcing agent judges on. Nothing is stored — ambience
    never enters the discover pool (its speech gate would drop it anyway)."""
    from tools.harvest import search as _search
    out = []
    for r in _search(query, n=n):
        dur = r.get("duration")
        minutes = round(dur / 60) if dur else None
        if min_minutes and (minutes is None or minutes < min_minutes):
            continue
        out.append({"video_id": r["video_id"], "title": r.get("title"),
                    "channel": r.get("channel"), "minutes": minutes,
                    "views": r.get("view_count"),
                    "url": f"https://www.youtube.com/watch?v={r['video_id']}"})
    return out


def expand_source(source):
    """One video → [id]; a playlist/channel URL → its video ids."""
    vid = video_id_of(source)
    if vid:
        return [vid]
    cmd = [ytdlp_path(), *ytdlp_extra_args(), "--flat-playlist", "--dump-json", source]
    r = subprocess.run(cmd, capture_output=True, text=True, **_NOWWIN)
    if r.returncode != 0:
        raise RuntimeError(f"could not expand {source}: {r.stderr.strip()[-400:]}")
    ids = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("id") and d["id"] not in ids:
            ids.append(d["id"])
    if not ids:
        raise RuntimeError(f"nothing to download at {source}")
    return ids


def fetch_audio(video_id, tmp, section=None, log=print):
    """yt-dlp bestaudio into `tmp`. `section` = (start, end) seconds pulls
    only that stretch through ffmpeg — but YouTube throttles that path to
    about realtime, so for anything over a minute the full download is
    faster (see add_sound). Returns (path, info), info = video metadata."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [ytdlp_path(), *ytdlp_extra_args(),
           "-f", "bestaudio/best", "--no-playlist", "--no-overwrites",
           "--write-info-json", "--no-progress",
           "-o", str(tmp / "%(id)s.%(ext)s")]
    if section:
        start, end = section
        cmd += ["--download-sections", f"*{int(start)}-{int(end)}"]
    cmd.append(url)
    log(f"  yt-dlp {video_id}" + (f" [{section[0]}–{section[1]} s]" if section else ""))
    r = subprocess.run(cmd, capture_output=True, text=True, **_NOWWIN)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {video_id}: {r.stderr.strip()[-600:]}")
    info_path = tmp / f"{video_id}.info.json"
    info = read_json(info_path) if info_path.exists() else {}
    files = [p for p in tmp.glob(f"{video_id}.*")
             if p.suffix not in (".json", ".part", ".ytdl")]
    if not files:
        raise RuntimeError(f"yt-dlp produced no audio file for {video_id}")
    return files[0], {"video_id": video_id, "title": info.get("title"),
                      "channel": info.get("channel") or info.get("uploader"),
                      "duration": info.get("duration"), "url": url}


# --- encoding -----------------------------------------------------------------------

def measure_lufs(path):
    """Integrated loudness (EBU R128) of a file, from ffmpeg's ebur128."""
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostats", "-i", str(path),
                        "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True, **_NOWWIN)
    val = None
    for line in r.stderr.splitlines():
        line = line.strip()
        if line.startswith("I:") and "LUFS" in line:
            try:
                val = float(line.split()[1])
            except (IndexError, ValueError):
                pass
    if val is None:
        raise RuntimeError(f"could not measure loudness of {path}: {r.stderr.strip()[-300:]}")
    return val


def _gain_filter(measured, target):
    """A single linear gain to the target, with a limiter as a clip guard
    (only a quiet source pushed up a lot ever touches it)."""
    gain = target - measured
    return f"volume={gain:.2f}dB,alimiter=limit=0.89:attack=5:release=200:level=false"


def _encode_opus(src, dst, kbps, lufs, extra_in=()):
    """Measure `src`, then encode it to Opus at one fixed gain."""
    measured = measure_lufs(src)
    _ffmpeg([*extra_in, "-i", str(src), "-vn", "-af",
             f"{_gain_filter(measured, lufs)},aformat=sample_rates=48000",
             "-c:a", "libopus", "-b:a", f"{kbps}k", str(dst)], "encode")
    return measured


def build_loop(src, dst, seconds, xfade, kbps, lufs, start=0.0):
    """A seamless loop of `seconds` from `src` starting at `start`: the
    `xfade` seconds that follow the cut are crossfaded into the cut's first
    `xfade` seconds, so the file's end runs straight into its beginning.
    Needs start + seconds + xfade of source; a shorter source shrinks the
    loop (and the caller is told)."""
    avail = probe_seconds(src) - start
    if avail < xfade * 2 + 5:
        raise RuntimeError(f"source too short for a loop ({avail:.0f} s after {start:.0f} s)")
    seconds = min(float(seconds), avail - xfade)
    s, L, X = float(start), seconds, float(xfade)
    fc = (
        f"[0:a]atrim=start={s + L}:end={s + L + X},asetpts=PTS-STARTPTS[tail];"
        f"[0:a]atrim=start={s}:end={s + X},asetpts=PTS-STARTPTS[head];"
        f"[0:a]atrim=start={s + X}:end={s + L},asetpts=PTS-STARTPTS[body];"
        f"[tail][head]acrossfade=d={X}:c1=tri:c2=tri[join];"
        f"[join][body]concat=n=2:v=0:a=1,aformat=sample_rates=48000[out]"
    )
    # the seamed loop as PCM first, so the loudness is measured on exactly
    # what ships (not on the ten-hour source) before the one-gain encode
    with tempfile.TemporaryDirectory() as td:
        pcm = Path(td) / "loop.wav"
        _ffmpeg(["-i", str(src), "-filter_complex", fc, "-map", "[out]", "-vn",
                 "-c:a", "pcm_s16le", str(pcm)], "loop")
        _encode_opus(pcm, dst, kbps, lufs)
    return seconds


def gen_noise(color, dst, seconds, xfade, kbps, lufs):
    """Synthesized noise (ffmpeg anoisesrc), then the same seam treatment."""
    if color not in NOISE_COLORS:
        raise ValueError(f"noise color must be one of {NOISE_COLORS}")
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "noise.wav"
        _ffmpeg(["-f", "lavfi", "-i",
                 f"anoisesrc=color={color}:seed=42:duration={seconds + xfade + 1}:sample_rate=48000:amplitude=0.5",
                 "-c:a", "pcm_s16le", str(raw)], "noise")
        return build_loop(raw, dst, seconds, xfade, kbps, lufs)


def encode_track(src, dst, kbps, lufs):
    _encode_opus(src, dst, kbps, lufs)


# --- probing a candidate ----------------------------------------------------------------
# Titles lie: "intense thunder" videos with no thunder in the first hour, "ocean
# waves crashing" that are a distant wash. Measure before cutting (2026-10-08).

def momentary_series(path, skip=300, seconds=7200):
    """Momentary loudness (100 ms steps, LUFS) over a stretch of the file."""
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostats", "-ss", str(skip), "-t", str(seconds),
                        "-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, **_NOWWIN)
    out = []
    for line in r.stderr.splitlines():
        if " M:" in line and "TARGET" in line:
            try:
                out.append(float(line.split(" M:")[1].split()[0]))
            except ValueError:
                pass
    return [m for m in out if m > -70]


def transient_events(series, rise_db=7.0, window=300, gap=50):
    """Indices of loud transients (thunder claps, gusts) over a rolling
    median floor; at most one per `gap` steps."""
    import statistics
    ev, last = [], -10 ** 9
    for i, m in enumerate(series):
        floor = statistics.median(series[max(0, i - window):i] or [m])
        if m > floor + rise_db and i - last > gap:
            ev.append(i)
            last = i
    return ev


def probe(path, kind="auto", skip=300, seconds=7200, loop_seconds=1800):
    """What a candidate actually sounds like, in numbers: loudness floor,
    dynamic depth, transient count per 30 min (thunder), swell depth and
    the dominant wave period (waves), and the busiest 30-min window to
    cut a loop from. `kind` auto: both views."""
    import statistics
    M = momentary_series(path, skip, seconds)
    n = len(M)
    if n < 600:
        raise RuntimeError("too little audio to probe")
    q = statistics.quantiles(M, n=10)
    res = {"hours": round(n / 36000, 2), "floor_lufs": round(q[4], 1),
           "depth_db": round(q[8] - q[0], 1)}
    if kind in ("auto", "thunder"):
        ev = transient_events(M)
        steps = int(loop_seconds * 10)
        best, bestc = 0, -1
        for start in range(0, max(1, n - steps), 3000):
            c = sum(1 for e in ev if start <= e < start + steps)
            if c > bestc:
                best, bestc = start, c
        res.update(events_per_30m=round(len(ev) / (n / 18000), 1),
                   busiest_start_s=skip + best // 10, busiest_events=bestc)
    if kind in ("auto", "waves"):
        sm = [statistics.fmean(M[i:i + 30]) for i in range(0, n - 30, 10)]
        mu = statistics.fmean(sm)
        var = sum((x - mu) ** 2 for x in sm) or 1.0
        best_lag, best_r = None, -1.0
        for lag in range(3, 21):
            r = sum((sm[i] - mu) * (sm[i + lag] - mu) for i in range(len(sm) - lag)) / var
            if r > best_r:
                best_lag, best_r = lag, r
        sq = statistics.quantiles(sm, n=10)
        res.update(swell_db=round(sq[8] - sq[0], 1), wave_period_s=best_lag,
                   periodicity=round(best_r, 2))
    return res


def probe_source(cfg, source, kind="auto", keep_dir=None, log=print):
    """Download a candidate (full — fast) and probe it. With `keep_dir` the
    audio stays there, so the pick can be cut with `sound add --from-file`."""
    vid = video_id_of(source)
    if not vid:
        raise ValueError(f"not a single video: {source}")
    tmp = Path(keep_dir) if keep_dir else _tmp_dir(cfg)
    tmp.mkdir(parents=True, exist_ok=True)
    have = [p for p in tmp.glob(f"{vid}.*") if p.suffix not in (".json", ".part")]
    try:
        src, info = (have[0], {"video_id": vid}) if have else fetch_audio(vid, tmp, log=log)
        res = probe(src, kind, loop_seconds=ambience_cfg(cfg)["sound_seconds"])
        res.update(video_id=vid, title=info.get("title"), file=str(src) if keep_dir else None)
        return res
    finally:
        if not keep_dir:
            shutil.rmtree(tmp, ignore_errors=True)


# --- catalog edits ------------------------------------------------------------------

def _file_entry(path, root):
    return {"file": str(path.relative_to(root)), "ms": int(probe_seconds(path) * 1000),
            "bytes": path.stat().st_size, "built_at": now_iso()}


def _tmp_dir(cfg):
    """A fresh scratch dir per download — unique, so two adds can run side
    by side (a sounds batch and a music batch); each add removes its own."""
    d = ambience_root(cfg, create=True) / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="dl-", dir=d))


def add_sound(cfg, source, ident, title, emoji=None, start=0.0, seconds=None,
              log=print, src_path=None):
    """Cut a loop from a YouTube video. `start` skips an intro; the loop
    length defaults to the config's sound_seconds. `src_path` cuts from an
    audio file already on disk (the video is still named by `source` for
    the catalog's provenance)."""
    _check_id("sound", ident)
    ac = ambience_cfg(cfg)
    seconds = seconds or ac["sound_seconds"]
    vid = video_id_of(source)
    if not vid:
        raise ValueError(f"not a single video: {source}")
    root = ambience_root(cfg, create=True)
    tmp = _tmp_dir(cfg)
    try:
        # the whole audio track, not a --download-sections cut: yt-dlp's own
        # downloader pulls a ten-hour opus stream in under a minute, while
        # the ffmpeg-seeked section crawls near realtime (YouTube throttles
        # it) — 300 MB of scratch beats a twenty-minute wait
        if src_path:
            src = Path(src_path)
            info = {"video_id": vid, "title": None, "channel": None, "duration": None,
                    "url": f"https://www.youtube.com/watch?v={vid}"}
            ij = src.with_name(f"{vid}.info.json")
            if ij.exists():
                d = read_json(ij)
                info.update(title=d.get("title"), channel=d.get("channel") or d.get("uploader"),
                            duration=d.get("duration"))
        else:
            src, info = fetch_audio(vid, tmp, log=log)
        dst = root / "sounds" / f"{ident}.ogg"
        dst.parent.mkdir(parents=True, exist_ok=True)
        log(f"  building {seconds:.0f} s loop from {start:.0f} s → {dst.name}")
        got = build_loop(src, dst, seconds, ac["xfade"], ac["sound_kbps"], ac["sound_lufs"],
                         start=start)
        if got < seconds - 1:
            log(f"  note: source ran out — loop is {got:.0f} s")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    entry = {"id": ident, "title": title, "emoji": emoji or "🔊",
             **_file_entry(dst, root),
             "source": {**info, "start": start}}
    with catalog_lock(cfg):
        cat = load_catalog(cfg)
        cat["sounds"] = [s for s in cat["sounds"] if s["id"] != ident] + [entry]
        save_catalog(cfg, cat)
    return entry


def add_noise(cfg, color, title=None, emoji=None, ident=None, seconds=None, log=print):
    ac = ambience_cfg(cfg)
    ident = ident or f"{color}-noise"
    _check_id("sound", ident)
    seconds = seconds or min(ac["sound_seconds"], 600)  # noise is noise — any seam is invisible
    root = ambience_root(cfg, create=True)
    dst = root / "sounds" / f"{ident}.ogg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    log(f"  synthesizing {color} noise → {dst.name}")
    gen_noise(color, dst, seconds, ac["xfade"], ac["sound_kbps"], ac["sound_lufs"])
    entry = {"id": ident, "title": title or f"{color.title()} noise",
             "emoji": emoji or {"white": "▒", "pink": "🌸", "brown": "🟫"}[color],
             **_file_entry(dst, root), "source": {"generated": color}}
    with catalog_lock(cfg):
        cat = load_catalog(cfg)
        cat["sounds"] = [s for s in cat["sounds"] if s["id"] != ident] + [entry]
        save_catalog(cfg, cat)
    return entry


def ensure_mood(cfg, ident, title=None, emoji=None):
    _check_id("mood", ident)
    with catalog_lock(cfg):
        cat = load_catalog(cfg)
        mood = next((m for m in cat["moods"] if m["id"] == ident), None)
        if mood is None:
            mood = {"id": ident, "title": title or ident.replace("-", " ").title(),
                    "emoji": emoji or "🎵", "tracks": []}
            cat["moods"].append(mood)
        else:
            if title:
                mood["title"] = title
            if emoji:
                mood["emoji"] = emoji
        save_catalog(cfg, cat)
    return mood


def add_music(cfg, mood_id, source, title=None, emoji=None, limit=None, log=print):
    """Add every video behind `source` (one video, or a playlist/channel) to
    the mood as an audio-only track. Already-present videos are skipped, so
    re-adding a playlist only pulls what's new."""
    ensure_mood(cfg, mood_id, title, emoji)
    ac = ambience_cfg(cfg)
    root = ambience_root(cfg, create=True)
    ids = expand_source(source)
    if limit:
        ids = ids[:limit]
    added = []
    for vid in ids:
        cat = load_catalog(cfg)
        mood = next(m for m in cat["moods"] if m["id"] == mood_id)
        if any(t["id"] == vid for t in mood["tracks"]):
            log(f"  {vid}: already in {mood_id}")
            continue
        tmp = _tmp_dir(cfg)
        try:
            src, info = fetch_audio(vid, tmp, log=log)
            dst = root / "music" / mood_id / f"{vid}.ogg"
            dst.parent.mkdir(parents=True, exist_ok=True)
            log(f"  encoding {info.get('title') or vid} → {dst.relative_to(root)}")
            encode_track(src, dst, ac["music_kbps"], ac["music_lufs"])
        except RuntimeError as e:
            log(f"  {vid}: {e}")
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        track = {"id": vid, "title": info.get("title") or vid,
                 "channel": info.get("channel"), "url": info["url"],
                 **_file_entry(dst, root)}
        with catalog_lock(cfg):
            cat = load_catalog(cfg)
            mood = next(m for m in cat["moods"] if m["id"] == mood_id)
            mood["tracks"].append(track)
            save_catalog(cfg, cat)
        added.append(track)
    return added


def remove(cfg, ident, track=None):
    """Drop a sound, a whole mood, or (with `track`) one track of a mood.
    Files go with the entry."""
    with catalog_lock(cfg):
        return _remove(cfg, ident, track)


def _remove(cfg, ident, track):
    cat = load_catalog(cfg)
    root = ambience_root(cfg)
    gone = []
    found = False

    def unlink(entry):
        p = root / entry["file"]
        if p.exists():
            p.unlink()
        gone.append(entry["file"])

    for s in list(cat["sounds"]):
        if s["id"] == ident and not track:
            found = True
            unlink(s)
            cat["sounds"].remove(s)
    for m in list(cat["moods"]):
        if m["id"] != ident:
            continue
        found = True
        if track:
            for t in list(m["tracks"]):
                if t["id"] == track:
                    unlink(t)
                    m["tracks"].remove(t)
        else:
            for t in m["tracks"]:
                unlink(t)
            cat["moods"].remove(m)
            shutil.rmtree(root / "music" / ident, ignore_errors=True)
    if not found:
        raise ValueError(f"nothing in the catalog called {ident!r}")
    save_catalog(cfg, cat)
    return gone


def rebuild(cfg):
    """Re-probe every file (sizes, durations) and drop entries whose file is
    gone — after hand-editing the folder."""
    with catalog_lock(cfg):
        return _rebuild(cfg)


def _rebuild(cfg):
    cat = load_catalog(cfg)
    root = ambience_root(cfg)
    keep = []
    for s in cat["sounds"]:
        p = root / s["file"]
        if p.exists():
            s.update({k: v for k, v in _file_entry(p, root).items() if k != "built_at"})
            keep.append(s)
    cat["sounds"] = keep
    for m in cat["moods"]:
        tracks = []
        for t in m["tracks"]:
            p = root / t["file"]
            if p.exists():
                t.update({k: v for k, v in _file_entry(p, root).items() if k != "built_at"})
                tracks.append(t)
        m["tracks"] = tracks
    return save_catalog(cfg, cat)


def summary(cfg):
    cat = load_catalog(cfg)
    return {
        "built_at": cat.get("built_at"),
        "sounds": [{"id": s["id"], "title": s["title"], "emoji": s["emoji"],
                    "minutes": round(s["ms"] / 60000, 1), "mb": round(s["bytes"] / 1e6, 1)}
                   for s in cat["sounds"]],
        "moods": [{"id": m["id"], "title": m["title"], "emoji": m["emoji"],
                   "tracks": len(m["tracks"]),
                   "hours": round(sum(t["ms"] for t in m["tracks"]) / 3.6e6, 1),
                   "mb": round(sum(t["bytes"] for t in m["tracks"]) / 1e6, 0)}
                  for m in cat["moods"]],
    }


# --- CLI ------------------------------------------------------------------------------

def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="YouTube candidates for a sound or a mood")
    p.add_argument("query")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--min-minutes", type=int, default=None)

    p = sub.add_parser("probe", help="measure a candidate before cutting it (thunder claps / wave swells)")
    p.add_argument("source", help="video url or id")
    p.add_argument("--kind", choices=("auto", "thunder", "waves"), default="auto")
    p.add_argument("--keep", default=None, help="keep the download in this dir (cut with sound add --from-file)")

    p = sub.add_parser("sound", help="loops")
    ss = p.add_subparsers(dest="sub", required=True)
    q = ss.add_parser("add", help="cut a loop from a YouTube video")
    q.add_argument("source", help="video url or id")
    q.add_argument("--id", required=True, help="slug, e.g. rain")
    q.add_argument("--title", required=True)
    q.add_argument("--emoji")
    q.add_argument("--start", type=float, default=0.0, help="skip the intro (s)")
    q.add_argument("--seconds", type=float, default=None, help="loop length (s)")
    q.add_argument("--from-file", default=None, help="cut from this audio file instead of downloading")
    q = ss.add_parser("gen", help="synthesize white / pink / brown noise")
    q.add_argument("color", choices=NOISE_COLORS)
    q.add_argument("--id")
    q.add_argument("--title")
    q.add_argument("--emoji")
    q.add_argument("--seconds", type=float, default=None)

    p = sub.add_parser("mood", help="a music mood (playlist)")
    ms = p.add_subparsers(dest="sub", required=True)
    q = ms.add_parser("add", help="create or retitle a mood")
    q.add_argument("id")
    q.add_argument("--title")
    q.add_argument("--emoji")

    p = sub.add_parser("music", help="tracks of a mood")
    mu = p.add_subparsers(dest="sub", required=True)
    q = mu.add_parser("add", help="add a video, playlist or channel to a mood")
    q.add_argument("mood")
    q.add_argument("source")
    q.add_argument("--title", help="mood title (when creating it)")
    q.add_argument("--emoji")
    q.add_argument("--limit", type=int, default=None, help="first N of a playlist")

    p = sub.add_parser("remove", help="drop a sound, a mood, or one of its tracks")
    p.add_argument("id")
    p.add_argument("--track", help="video id of one track in the mood")

    sub.add_parser("list", help="the catalog, summarized")
    sub.add_parser("rebuild", help="re-probe files; drop entries whose file is gone")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    t0 = time.time()

    if a.cmd == "search":
        out = search(a.query, n=a.n, min_minutes=a.min_minutes)
    elif a.cmd == "probe":
        out = probe_source(cfg, a.source, a.kind, a.keep, log=_log)
    elif a.cmd == "sound" and a.sub == "add":
        out = add_sound(cfg, a.source, a.id, a.title, a.emoji, a.start, a.seconds, log=_log,
                        src_path=a.from_file)
    elif a.cmd == "sound" and a.sub == "gen":
        out = add_noise(cfg, a.color, a.title, a.emoji, a.id, a.seconds, log=_log)
    elif a.cmd == "mood":
        out = ensure_mood(cfg, a.id, a.title, a.emoji)
    elif a.cmd == "music":
        out = {"added": add_music(cfg, a.mood, a.source, a.title, a.emoji, a.limit, log=_log)}
    elif a.cmd == "remove":
        out = {"removed": remove(cfg, a.id, a.track)}
    elif a.cmd == "list":
        out = summary(cfg)
    elif a.cmd == "rebuild":
        out = summary(cfg) if rebuild(cfg) else {}
    else:
        ap.error("unknown command")
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    _log(f"done in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
