#!/usr/bin/env python3
"""series — ingest already-downloaded box sets from the media server (MOBILE.md — Series).

The originals live on the Raspberry Pi media server's `library` share,
mounted on the Mac (`/Volumes/library/Japanese/...` — tools/library.py),
and are never written to. For each episode the Mac transcodes a 480p H.264
copy (VideoToolbox; libx264 fallback) straight off the mount into
<work_dir>/episodes/<id>/video.mp4, puts the Japanese subtitle sidecar
beside the manifest, parks a copy of both in the stage dir on the server's
writable `t7` share (so a later fetch is a copy, not a transcode), and a
queue job is enqueued with the series' playlist identity (series slug +
episode order) so the normal Stage 1 → curate → watch flow runs unchanged.
Derived data (transcript, coverage, curation, cards, ledger evidence) is
never tied to the video's presence: the phone drops its local copy freely,
the Mac can `evict` a watched episode's video to reclaim disk, and either is
re-materialized from the stage copy (or re-transcoded from the original)
on demand.

Identity: source `series://<slug>/<ep_no>` → episode id `ser_<slug>_e<nn>`,
stable across evict/fetch cycles (unlike local_<hash>, which bakes in mtime).

Folders are given as they are on the Mac (`/Volumes/library/Japanese/drama/
hotspot`), relative to the series root (`drama/hotspot`), or in the old
desktop form (`E:/Japanese/drama/hotspot`) — all three resolve to the mount.

CLI:
    python -m tools.series scan   <folder>                     # what would be ingested
    python -m tools.series ingest <folder> [--slug S] [--title T] [--episodes 1,3-5]
                                           [--dry-run] [--no-drain]
    python -m tools.series list
    python -m tools.series status <slug>
    python -m tools.series fetch  <slug> [--episodes ...]      # re-materialize video.mp4
    python -m tools.series evict  <slug> [--episodes ...] [--all]   # drop video.mp4 (watched only unless --all)
    python -m tools.series remove <slug> [--remote]            # full delete (jobs, artifacts, ledger footprint)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import library as lib  # noqa: E402
from tools._staging import downloads_dir, episode_dir, read_json, write_json  # noqa: E402

SCHEME = "series://"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".wmv", ".flv"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
JA_TOKENS = {"ja", "jpn", "jp", "japanese", "日本語", "jap"}
DEFAULT_CAP = 480  # phone-sized copies


# --- identity ------------------------------------------------------------------

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s or "series"


def series_source(slug, ep_no):
    return f"{SCHEME}{slug}/{int(ep_no)}"


def parse_series_source(source):
    """'series://hotspot/3' → ('hotspot', 3); None for anything else."""
    m = re.match(r"^series://([a-z0-9][a-z0-9-]*)/(\d+)$", str(source or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def episode_id_for(slug, ep_no):
    return f"ser_{slug}_e{int(ep_no):02d}"


def series_episode_id(source):
    """Job/episode id for a series source, None if it isn't one. Same shape
    as the other id derivations in server.jobqueue: derivable offline."""
    parsed = parse_series_source(source)
    return episode_id_for(*parsed) if parsed else None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- config / manifests ---------------------------------------------------------

def series_cfg(cfg):
    """The library block (tools.library): mounts, roots, stage dir."""
    return lib.library_cfg(cfg)


def series_root(cfg):
    return Path(cfg["work_dir"]).expanduser() / "series"


def series_dir(cfg, slug, create=False):
    d = series_root(cfg) / slug
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(cfg, slug):
    return series_dir(cfg, slug) / "series.json"


def load_manifest(cfg, slug):
    p = manifest_path(cfg, slug)
    if not p.exists():
        raise FileNotFoundError(f"no series '{slug}' under {series_root(cfg)}")
    return read_json(p)


def save_manifest(cfg, man):
    series_dir(cfg, man["slug"], create=True)
    write_json(manifest_path(cfg, man["slug"]), man)


def list_series(cfg):
    root = series_root(cfg)
    if not root.exists():
        return []
    return [read_json(p) for p in sorted(root.glob("*/series.json"))]


def find_episode(man, ep_no):
    for e in man["episodes"]:
        if e["ep_no"] == int(ep_no):
            return e
    raise KeyError(f"{man['slug']} has no episode {ep_no}")


def local_subs_path(cfg, slug, ep_no):
    return series_dir(cfg, slug) / f"{slug}-e{int(ep_no):02d}.ja.srt"


def remote_stage_paths(scfg, slug, ep_no):
    """(mp4, srt) of the episode's stage copy on the server's t7 share —
    (None, None) when no stage dir is configured."""
    sd = (scfg.get("stage_dir") or "").rstrip("/")
    if not sd:
        return None, None
    base = f"{sd}/{slug}/{slug}-e{int(ep_no):02d}"
    return base + ".mp4", base + ".ja.srt"


# --- episode-number parsing -----------------------------------------------------

_EP_PATTERNS = [
    # S01E05 / s1e5
    (re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?![0-9])"), True),
    # EP01 / Ep.01 / E01 / ep 1
    (re.compile(r"(?<![A-Za-z0-9])[Ee][Pp]?[ ._]?(\d{1,3})(?![0-9])"), False),
    # 第1話 / 第01回 / 1話
    (re.compile(r"第\s?(\d{1,3})\s?[話回]"), False),
    (re.compile(r"(?<![0-9])(\d{1,3})\s?話"), False),
    # Episode 1 / episode_01
    (re.compile(r"(?i)(?:^|[\s._\-\[])episode[\s._-]*(\d{1,3})(?![0-9])"), False),
    # "Show - 01 [1080p]" / "Show - 01.mkv" / "Show 01v2"
    (re.compile(r"\s-\s(\d{1,3})(?=\s|\.|$|v\d|\s?[\[\(])"), False),
    # last resort: a bare 2-digit number not part of a resolution/year/codec
    (re.compile(r"(?<![0-9A-Za-z])(\d{2})(?![0-9pPkKxX])"), False),
]


def parse_episode(name):
    """(season|None, ep|None) from a file/dir name. Tries the explicit
    forms first; the bare-number fallback is only for 'Show 01.mkv' style."""
    stem = Path(name).stem
    for rx, has_season in _EP_PATTERNS:
        m = rx.search(stem)
        if m:
            if has_season:
                return int(m.group(1)), int(m.group(2))
            return None, int(m.group(1))
    return None, None


def ep_no_of(season, ep):
    """Sortable playlist order: season folds in as hundreds (S2E3 → 203)."""
    return (season or 0) * 100 + ep if season else ep


def ep_label(season, ep):
    return f"S{season}E{ep:02d}" if season else f"EP{ep:02d}"


# --- scanning the library folder ---------------------------------------------------------

def _wname(p):
    """File name of a path with either separator (old manifests carry the
    desktop's backslashes; the mount is POSIX)."""
    return re.split(r"[\\/]", str(p))[-1]


def _wdir(p):
    return str(p)[: len(str(p)) - len(_wname(p))]


def _wsuffix(p):
    name = _wname(p)
    return ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""


def _tokens(p):
    stem = _wname(p).rsplit(".", 1)[0]
    return {t.lower() for t in re.split(r"[\s._\-\[\]\(\)]+", stem) if t}


def _is_ja_sub(path):
    # the file's own name only: a folder called [JPN_ENG_CHT_SUB] tags nothing
    return bool(_tokens(path) & JA_TOKENS)


def group_files(paths):
    """Pair videos with a Japanese subtitle by parsed episode number.
    Returns episodes sorted by playlist order, plus files nothing claimed."""
    videos, subs, unparsed = {}, {}, []
    for p in paths:
        ext = _wsuffix(p)
        if ext not in VIDEO_EXTS and ext not in SUB_EXTS:
            continue
        name = _wname(p)
        # sub language tags read as junk to the episode parser — drop them first
        season, ep = parse_episode(re.sub(r"(?i)\.(ja|jpn|jp|japanese|eng|en|cht|chs|zh)\b", "", name))
        if ep is None:
            unparsed.append(p)
            continue
        key = (season, ep)
        if ext in VIDEO_EXTS:
            videos.setdefault(key, []).append(p)
        else:
            subs.setdefault(key, []).append(p)
    episodes = []
    # a single-season box set: subs named "Show - 01.ass" (no season, e.g. from
    # kitsunekko) belong to the S01E01 videos
    seasons = {k[0] for k in videos if k[0] is not None}
    lone_season = next(iter(seasons)) if len(seasons) == 1 else None
    for key in sorted(videos, key=lambda k: (k[0] or 0, k[1])):
        vids = sorted(videos[key])
        # prefer a subtitle tagged Japanese, then .srt over .ass/.vtt, same dir first
        cands = list(subs.get(key, []))
        if not cands and lone_season is not None and key[0] == lone_season:
            cands = list(subs.get((None, key[1]), []))
        vdir = _wdir(vids[0])
        cands.sort(key=lambda s: (not _is_ja_sub(s), _wsuffix(s) != ".srt",
                                  _wdir(s) != vdir, s))
        ja = [s for s in cands if _is_ja_sub(s)]
        # an untagged lone .srt next to the video is assumed to match the audio
        chosen = ja[0] if ja else (cands[0] if len(cands) == 1 else None)
        season, ep = key
        episodes.append({
            "season": season, "ep": ep, "ep_no": ep_no_of(season, ep),
            "label": ep_label(season, ep),
            "remote_video": vids[0], "remote_subs": chosen,
            "duplicates": vids[1:],
        })
    return episodes, unparsed


def scan(cfg, remote_dir, log=print):
    remote_dir = lib.resolve(cfg, remote_dir)
    lib.ensure_mounted(cfg, remote_dir, log=log)
    paths = lib.listing(remote_dir)
    episodes, unparsed = group_files(paths)
    log(f"{len(paths)} files under {remote_dir}: {len(episodes)} episode(s)")
    return episodes, unparsed


# --- transcode / subtitle prep on the Mac -------------------------------------------

def probe(path):
    """ffprobe (local — the original is read in place over the mount)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,height,pix_fmt:stream_tags=language,title"
         ":format=duration", "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout or "{}")


def _pick_streams(probe):
    """(video stream, audio index-within-audio, text-sub stream index or None)
    — Japanese audio/subs preferred (dual-audio anime), else the first."""
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("codec_name") not in ("png", "mjpeg", "bmp")), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    a_idx = 0
    for i, s in enumerate(audios):
        if (s.get("tags", {}).get("language") or "").lower() in ("jpn", "ja", "japanese"):
            a_idx = i
            break
    sub = None
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        lang = (s.get("tags", {}).get("language") or "").lower()
        if lang in ("jpn", "ja", "japanese") and s.get("codec_name") in ("subrip", "ass", "ssa", "webvtt", "mov_text"):
            sub = s["index"]
            break
    return video, a_idx, sub


def transcode_cmd(src, dst, video, a_idx, cap=DEFAULT_CAP, encoder="videotoolbox"):
    """ffmpeg argv: 480p H.264 + stereo AAC, faststart. VideoToolbox is the
    Mac's hardware encoder (far faster than realtime, the SMB read is the
    bottleneck); libx264 is the software fallback."""
    height = int(video.get("height") or 0)
    vf = ["format=yuv420p"]
    if cap and height > cap:
        vf.insert(0, f"scale=-2:{cap}")
    if encoder == "videotoolbox":
        venc = ["-c:v", "h264_videotoolbox", "-b:v", "1500k", "-maxrate", "2500k",
                "-bufsize", "5000k", "-profile:v", "high", "-allow_sw", "1"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "high"]
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-map", "0:v:0", "-map", f"0:a:{a_idx}", "-sn", "-dn",
            "-vf", ",".join(vf), *venc,
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart", str(dst)]


def _ffmpeg(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-400:]}")
    return r


def _stage_has(cfg, path):
    """The stage copy exists (and the share is up). Never raises — the stage
    tier is a shortcut, not a requirement."""
    if not path:
        return False
    try:
        lib.ensure_mounted(cfg, path, log=lambda m: None)
    except RuntimeError:
        return False
    return os.path.isfile(path) and os.path.getsize(path) > 0


def stage_copies(cfg, slug, ep_no, dest, subs, log=print, label=None):
    """Park the Mac's 480p copy + srt in the stage dir on the server's t7
    share, so a later fetch after an evict is a copy, not a transcode.
    Best-effort: a missing share only costs the shortcut."""
    stage_mp4, stage_srt = remote_stage_paths(series_cfg(cfg), slug, ep_no)
    if not stage_mp4:
        return False
    try:
        lib.ensure_mounted(cfg, stage_mp4, log=log)
        if not _stage_has(cfg, stage_mp4):
            log(f"  {label or ep_label_of(slug, ep_no)}: parking the 480p copy on the media server…")
            lib.copy_file(dest, stage_mp4)
        if subs.exists() and not os.path.isfile(stage_srt):
            lib.copy_file(subs, stage_srt)
        return True
    except (OSError, RuntimeError) as ex:
        log(f"  stage copy skipped ({ex})")
        return False


def ep_label_of(slug, ep_no):
    return f"{slug} e{int(ep_no):02d}"


def prepare_episode(cfg, man, ep, log=print):
    """The Mac's 480p copy of an original on the library share (skipped if
    video.mp4 is already there) and its Japanese .srt beside the manifest —
    from the sidecar, or extracted from the container's text subtitle
    track. The original is only ever read. Returns {"video", "subs",
    "duration"}."""
    slug, ep_no = man["slug"], ep["ep_no"]
    src = lib.resolve(cfg, ep["remote_video"])
    lib.ensure_mounted(cfg, src, log=log)
    if not os.path.isfile(src):
        raise RuntimeError(f"original missing on the library share: {src}")
    dest = video_path(cfg, slug, ep_no)
    subs = local_subs_path(cfg, slug, ep_no)
    pr = probe(src)
    video, a_idx, sub_idx = _pick_streams(pr)
    if video is None:
        raise RuntimeError(f"no video stream in {src}")
    result = {"video": False, "subs": None,
              "duration": float(pr.get("format", {}).get("duration") or 0) or None}
    cap = man.get("cap", DEFAULT_CAP)

    if dest.exists() and dest.stat().st_size > 0:
        log(f"  {ep['label']}: 480p copy already on the Mac")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part.mp4")
        log(f"  {ep['label']}: transcoding {video.get('height')}p → {cap}p on the Mac (VideoToolbox)…")
        try:
            _ffmpeg(transcode_cmd(src, tmp, video, a_idx, cap))
        except RuntimeError as ex:
            log(f"  {ep['label']}: VideoToolbox failed ({str(ex)[-200:]}); retrying with libx264…")
            _ffmpeg(transcode_cmd(src, tmp, video, a_idx, cap, encoder="x264"))
        tmp.replace(dest)
    result["video"] = True

    if subs.exists():
        result["subs"] = ep.get("subs") or "sidecar"
    elif ep.get("remote_subs"):
        sc = lib.resolve(cfg, ep["remote_subs"])
        if lib.suffix_of(sc) == ".srt":
            lib.copy_file(sc, subs)
        else:  # .ass/.vtt → srt
            _ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", sc,
                     "-c:s", "srt", str(subs)])
        result["subs"] = "sidecar"
    elif sub_idx is not None:
        log(f"  {ep['label']}: extracting embedded Japanese subtitle track {sub_idx}…")
        _ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                 "-map", f"0:{sub_idx}", "-c:s", "srt", str(subs)])
        result["subs"] = "embedded"
    else:
        log(f"  {ep['label']}: no Japanese subtitles — Stage 1 will ASR it")
    stage_copies(cfg, slug, ep_no, dest, subs, log=log, label=ep["label"])
    return result


# --- materialize / evict on the Mac -------------------------------------------------

def video_path(cfg, slug, ep_no):
    return episode_dir(cfg, episode_id_for(slug, ep_no)) / "video.mp4"


def materialize(cfg, slug, ep_no, log=print, remote=None):
    """Ensure <episode_dir>/video.mp4 (and the subtitle sidecar) exist
    locally: copy the stage copy from the server's t7 share when there is
    one, else transcode from the original on the library share. Records
    subs/duration/fetched_at on the manifest row. Returns the video path.
    (`remote` is accepted for old callers and ignored.)"""
    man = load_manifest(cfg, slug)
    ep = find_episode(man, ep_no)
    dest = video_path(cfg, slug, ep_no)
    subs = local_subs_path(cfg, slug, ep_no)
    stage_mp4, stage_srt = remote_stage_paths(series_cfg(cfg), slug, ep_no)
    changed = False
    if not (dest.exists() and dest.stat().st_size > 0):
        if _stage_has(cfg, stage_mp4):
            log(f"  {ep['label']}: copying the 480p stage copy from the media server…")
            lib.copy_file(stage_mp4, dest)
            if not subs.exists() and _stage_has(cfg, stage_srt):
                lib.copy_file(stage_srt, subs)
        else:
            res = prepare_episode(cfg, man, ep, log=log)
            ep["subs"] = res["subs"]
            if res.get("duration"):
                ep["duration"] = res["duration"]
            ep["staged_at"] = now_iso()
        ep["fetched_at"] = now_iso()
        ep["size"] = dest.stat().st_size
        changed = True
    elif not subs.exists() and ep.get("subs"):
        # video here, sidecar lost: the stage copy, else a fresh extraction
        if _stage_has(cfg, stage_srt):
            lib.copy_file(stage_srt, subs)
        else:
            prepare_episode(cfg, man, ep, log=log)
    if changed:
        save_manifest(cfg, man)
    return dest


def evict(cfg, slug, ep_nos=None, all_states=False, log=print):
    """Drop the Mac's video.mp4 (+ the acquire mp3) for a series' episodes to
    reclaim disk. Everything derived stays; `fetch` (or the phone's download
    button, via the server) brings the video back from the PC. Watched
    episodes only unless all_states."""
    from server import jobqueue as q
    man = load_manifest(cfg, slug)
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    freed, evicted, kept = 0, [], []
    for ep in man["episodes"]:
        if ep_nos and ep["ep_no"] not in ep_nos:
            continue
        job = q.get_job(conn, ep["id"])
        state = job["state"] if job else None
        if not all_states and state not in ("watched", "pushing") and state is not None:
            kept.append(f"{ep['label']} ({state})")
            continue
        for p in (video_path(cfg, slug, ep["ep_no"]),
                  downloads_dir(cfg) / f"{ep['id']}.mp3"):
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
        evicted.append(ep["label"])
    log(f"evicted {len(evicted)} video(s), {freed / 1e6:.0f} MB freed"
        + (f"; kept unwatched: {', '.join(kept)}" if kept else ""))
    return {"evicted": evicted, "kept": kept, "freed_bytes": freed}


# --- ingest -----------------------------------------------------------------------

def parse_episode_spec(spec):
    """'1,3-5' → {1,3,4,5}; None → None (all)."""
    if not spec:
        return None
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def ingest(cfg, remote_dir, slug=None, title=None, episodes=None, dry_run=False,
           log=print):
    """Scan → manifest → per episode: transcode (or stage copy) + subs →
    enqueue. Idempotent: a re-run skips local videos, stage copies and queue
    rows that already exist, so an interrupted ingest just resumes."""
    remote_dir = lib.resolve(cfg, remote_dir)
    title = title or _wname(remote_dir.rstrip("/\\"))
    slug = slug or slugify(title)
    found, unparsed = scan(cfg, remote_dir, log=log)
    if not found:
        raise RuntimeError(f"no episodes with parseable numbers under {remote_dir}"
                           + (f" (unparsed: {unparsed[:5]})" if unparsed else ""))
    wanted = parse_episode_spec(episodes)
    # "--episodes 1" on a single-season S01Exx set means episode 1 (ep_no 101)
    seasons = {e["season"] for e in found if e["season"] is not None}
    plain_ok = len(seasons) <= 1
    picked = [e for e in found if wanted is None or e["ep_no"] in wanted
              or (plain_ok and e["ep"] in wanted)]
    for e in picked:
        log(f"  {e['label']:>6}  {_wname(e['remote_video'])}"
            f"  subs={'✓ ' + _wname(e['remote_subs']) if e['remote_subs'] else '— (probe)'}"
            + (f"  ⚠ duplicates: {len(e['duplicates'])}" if e["duplicates"] else ""))
    if unparsed:
        log(f"  ignored (no episode number): {len(unparsed)} file(s)")
    if dry_run:
        return {"slug": slug, "title": title, "episodes": picked, "unparsed": unparsed}

    # manifest: merge with an existing one (re-ingest adds episodes, keeps timestamps)
    try:
        man = load_manifest(cfg, slug)
    except FileNotFoundError:
        man = {"slug": slug, "title": title, "remote_dir": remote_dir,
               "cap": 480, "created_at": now_iso(), "episodes": []}
    by_no = {e["ep_no"]: e for e in man["episodes"]}
    for e in picked:
        row = by_no.setdefault(e["ep_no"], {})
        row.update({
            "ep_no": e["ep_no"], "label": e["label"], "season": e["season"], "ep": e["ep"],
            "id": episode_id_for(slug, e["ep_no"]),
            "title": f"{man['title']} {e['label']}",
            "remote_video": e["remote_video"], "remote_subs": e["remote_subs"],
        })
        row.pop("remote_stage", None)  # desktop-era field; the stage path is config now
    man["episodes"] = sorted(by_no.values(), key=lambda r: r["ep_no"])
    save_manifest(cfg, man)

    from server import jobqueue as q
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    summary = {"slug": slug, "title": man["title"], "staged": [], "enqueued": [],
               "already": [], "failed": []}
    for e in picked:
        row = by_no[e["ep_no"]]
        try:
            materialize(cfg, slug, e["ep_no"], log=log)
            # materialize saved subs/duration/fetched_at/size on its own copy —
            # reload so the next iteration's save doesn't clobber them
            man = load_manifest(cfg, slug)
            by_no = {r["ep_no"]: r for r in man["episodes"]}
            row = by_no[e["ep_no"]]
            summary["staged"].append(row["label"])
            job, created = q.enqueue(conn, series_source(slug, e["ep_no"]),
                                     title=row["title"], series=slug,
                                     series_title=man["title"], ep_no=e["ep_no"])
            (summary["enqueued"] if created or job["state"] == "queued"
             else summary["already"]).append(job["id"])
            log(f"  {row['label']}: {'queued' if created else job['state']} ({job['id']})")
        except Exception as ex:  # keep going — one bad file shouldn't sink the set
            log(f"  {row['label']}: FAILED — {ex}")
            summary["failed"].append(f"{row['label']}: {ex}")
    return summary


def status(cfg, slug):
    from server import jobqueue as q
    man = load_manifest(cfg, slug)
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    rows = []
    for e in man["episodes"]:
        job = q.get_job(conn, e["id"])
        v = video_path(cfg, slug, e["ep_no"])
        rows.append({"ep_no": e["ep_no"], "label": e["label"], "id": e["id"],
                     "state": job["state"] if job else None,
                     "video_local": v.exists(),
                     "video_mb": round(v.stat().st_size / 1e6) if v.exists() else None,
                     "subs": e.get("subs")})
    return {"slug": slug, "title": man["title"], "remote_dir": man["remote_dir"],
            "episodes": rows}


def remove(cfg, slug, remote_too=False, log=print):
    """Full delete of a series on the Mac: queue rows, episode dirs, the
    ledger footprint of unwatched episodes (watched evidence is kept, as the
    server's DELETE does), the manifest — and with remote_too the stage
    copies on the server's t7 share. The originals on the library share are
    never touched."""
    import shutil

    from ledger import ledgerctl as lc
    from server import jobqueue as q
    man = load_manifest(cfg, slug)
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    ledger = lc.open_db(cfg["ledger_db"])
    removed = []
    for e in man["episodes"]:
        d = episode_dir(cfg, e["id"])
        if d.exists():
            shutil.rmtree(d)
        for p in downloads_dir(cfg).glob(f"{e['id']}.*"):
            p.unlink()
        lc.purge_episode(ledger, e["id"])
        q.delete_job(conn, e["id"])
        removed.append(e["id"])
    if remote_too:
        sd = lib.stage_dir(cfg)
        if sd:
            lib.ensure_mounted(cfg, sd, log=log)
            shutil.rmtree(f"{sd}/{slug}", ignore_errors=True)
    shutil.rmtree(series_dir(cfg, slug), ignore_errors=True)
    log(f"removed series {slug}: {len(removed)} episode(s)")
    return {"removed": removed, "remote_stage_removed": remote_too}


# --- CLI -----------------------------------------------------------------------------

def main(argv=None):
    from lib_config import load_config

    ap = argparse.ArgumentParser(prog="series", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("scan", help="list the episodes a library folder would ingest")
    p.add_argument("remote_dir")
    p = sub.add_parser("ingest", help="transcode on the Mac, enqueue")
    p.add_argument("remote_dir")
    p.add_argument("--slug")
    p.add_argument("--title")
    p.add_argument("--episodes", help="e.g. 1,3-5 (default: all)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-drain", action="store_true",
                   help="only enqueue; leave Stage 1 to the server's worker")
    sub.add_parser("list", help="known series")
    p = sub.add_parser("status", help="per-episode state for one series")
    p.add_argument("slug")
    p = sub.add_parser("fetch", help="re-materialize evicted videos (stage copy or re-transcode)")
    p.add_argument("slug")
    p.add_argument("--episodes")
    p = sub.add_parser("evict", help="drop local video.mp4 (watched only unless --all)")
    p.add_argument("slug")
    p.add_argument("--episodes")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("remove", help="delete the series from the Mac (never the originals)")
    p.add_argument("slug")
    p.add_argument("--remote", action="store_true", help="also drop the stage copies on the media server")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if args.verb == "scan":
        eps, unparsed = scan(cfg, args.remote_dir, log=log)
        print(json.dumps({"episodes": eps, "unparsed": unparsed}, ensure_ascii=False, indent=2))
    elif args.verb == "ingest":
        summary = ingest(cfg, args.remote_dir, slug=args.slug, title=args.title,
                         episodes=args.episodes, dry_run=args.dry_run, log=log)
        if not args.dry_run and not args.no_drain and summary["enqueued"]:
            from server import jobqueue as q
            from server.worker import drain
            conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
            summary["drain"] = drain(cfg, conn, log=log)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    elif args.verb == "list":
        print(json.dumps([{"slug": m["slug"], "title": m["title"],
                           "episodes": len(m["episodes"]), "remote_dir": m["remote_dir"]}
                          for m in list_series(cfg)], ensure_ascii=False, indent=2))
    elif args.verb == "status":
        print(json.dumps(status(cfg, args.slug), ensure_ascii=False, indent=2))
    elif args.verb == "fetch":
        man = load_manifest(cfg, args.slug)
        wanted = parse_episode_spec(args.episodes)
        for e in man["episodes"]:
            if wanted is None or e["ep_no"] in wanted:
                materialize(cfg, args.slug, e["ep_no"], log=log)
        print(json.dumps(status(cfg, args.slug), ensure_ascii=False, indent=2))
    elif args.verb == "evict":
        print(json.dumps(evict(cfg, args.slug, parse_episode_spec(args.episodes),
                               all_states=args.all, log=log), ensure_ascii=False, indent=2))
    elif args.verb == "remove":
        print(json.dumps(remove(cfg, args.slug, remote_too=args.remote, log=log),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
