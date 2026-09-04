#!/usr/bin/env python3
"""series — ingest already-downloaded box sets from the PC (MOBILE.md — Series).

The originals live on the Windows desktop (E:/Japanese/...) and are never
written to. For each episode the PC (NVENC) transcodes a 480p H.264 copy into
its stage dir, the Mac pulls that copy over the LAN into
<work_dir>/episodes/<id>/video.mp4 together with the Japanese subtitle
sidecar, and a queue job is enqueued with the series' playlist identity
(series slug + episode order) so the normal Stage 1 → curate → watch flow
runs unchanged. Derived data (transcript, coverage, curation, cards, ledger
evidence) is never tied to the video's presence: the phone drops its local
copy freely, the Mac can `evict` a watched episode's video to reclaim disk,
and either is re-materialized from the PC's stage copy (or re-transcoded from
the original) on demand.

Identity: source `series://<slug>/<ep_no>` → episode id `ser_<slug>_e<nn>`,
stable across evict/fetch cycles (unlike local_<hash>, which bakes in mtime).

CLI:
    python -m tools.series scan   <pc-dir>                     # what would be ingested
    python -m tools.series ingest <pc-dir> [--slug S] [--title T] [--episodes 1,3-5]
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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._staging import downloads_dir, episode_dir, read_json, write_json  # noqa: E402

SCHEME = "series://"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".wmv", ".flv"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
JA_TOKENS = {"ja", "jpn", "jp", "japanese", "日本語", "jap"}
DEFAULTS = {
    "ssh_host": "transcribe-svc@192.168.0.230",
    "ssh_identity": "~/.ssh/transcribe_remote_ed25519",
    "remote_stage_dir": "I:/transcribe/fullpipe_stage",
    "ssh_timeout": 3600,
}


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
    return {**DEFAULTS, **(cfg.get("series") or {})}


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
    base = f"{scfg['remote_stage_dir'].rstrip('/')}/{slug}/{slug}-e{int(ep_no):02d}"
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


# --- the PC over ssh -------------------------------------------------------------

def _win(p):
    return str(p).replace("/", "\\")


class Remote:
    """cmd.exe on the desktop over the LAN ssh host (Windows OpenSSH)."""

    def __init__(self, scfg):
        self.host = scfg["ssh_host"]
        self.identity = os.path.expanduser(scfg["ssh_identity"])
        self.timeout = int(scfg.get("ssh_timeout", 3600))

    def _base(self, prog):
        return [prog, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-i", self.identity, "-o", "IdentitiesOnly=yes"]

    def run(self, cmd, timeout=None, check=True):
        r = subprocess.run([*self._base("ssh"), self.host, cmd],
                           capture_output=True, timeout=timeout or self.timeout)
        out = r.stdout.decode("utf-8", errors="replace")
        err = r.stderr.decode("utf-8", errors="replace")
        # Windows OpenSSH's pq-kex banner lands on stderr for every call
        err = "\n".join(l for l in err.splitlines() if not l.startswith("**")).strip()
        if check and r.returncode != 0:
            raise RuntimeError(f"remote command failed ({r.returncode}): {cmd[:120]}…\n"
                               f"{(err or out).strip()[-600:]}")
        return r.returncode, out, err

    def listing(self, remote_dir):
        """Every file under remote_dir, full Windows paths (UTF-8 via chcp)."""
        _, out, _ = self.run(f'chcp 65001 >nul & dir /s /b /a-d "{_win(remote_dir)}"')
        return [l.strip().lstrip("\ufeff") for l in out.splitlines() if l.strip()]

    def exists(self, remote_path):
        rc, _, _ = self.run(f'if exist "{_win(remote_path)}" (exit 0) else (exit 1)',
                            check=False)
        return rc == 0

    def size(self, remote_path):
        rc, out, _ = self.run(f'for %A in ("{_win(remote_path)}") do @echo %~zA',
                              check=False)
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    def probe(self, remote_path):
        _, out, _ = self.run(
            'ffprobe -v error -show_entries '
            'stream=index,codec_type,codec_name,height,pix_fmt:stream_tags=language,title'
            f':format=duration -of json "{_win(remote_path)}"')
        return json.loads(out or "{}")

    def scp_from(self, remote_path, local_path):
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_name(local_path.name + ".part")
        r = subprocess.run([*self._base("scp"), "-q", f"{self.host}:{remote_path}", str(tmp)],
                           capture_output=True, timeout=self.timeout)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"scp failed: {r.stderr.decode('utf-8', 'replace').strip()[-400:]}")
        tmp.replace(local_path)
        return local_path


# --- scanning the PC folder ---------------------------------------------------------

def _wname(p):
    """File name of a PC (backslash) path — pathlib on the Mac won't split it."""
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
    for key in sorted(videos, key=lambda k: (k[0] or 0, k[1])):
        vids = sorted(videos[key])
        # prefer a subtitle tagged Japanese, then .srt over .ass/.vtt, same dir first
        cands = subs.get(key, [])
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
    remote = Remote(series_cfg(cfg))
    paths = remote.listing(remote_dir)
    episodes, unparsed = group_files(paths)
    log(f"{len(paths)} files under {remote_dir}: {len(episodes)} episode(s)")
    return episodes, unparsed


# --- transcode / subtitle prep on the PC ---------------------------------------------

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


def transcode_cmd(src, dst, video, a_idx, cap=480, encoder="nvenc"):
    height = int(video.get("height") or 0)
    too_tall = bool(cap and height > cap)
    vf = ["format=yuv420p"]
    if too_tall:
        vf.insert(0, f"scale=-2:{cap}")
    if encoder == "nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "27",
                "-b:v", "0", "-maxrate", "2500k", "-bufsize", "5000k", "-profile:v", "high"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "high"]
    parts = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f'"{_win(src)}"',
             "-map", "0:v:0", "-map", f"0:a:{a_idx}", "-sn", "-dn",
             "-vf", '"' + ",".join(vf) + '"', *venc,
             "-c:a", "aac", "-b:a", "128k", "-ac", "2",
             "-movflags", "+faststart", f'"{_win(dst)}"']
    return " ".join(parts)


def remote_prepare_episode(remote, ep, stage_mp4, stage_srt, cap=480, log=print):
    """On the PC: 480p copy of the original into the stage dir (skipped if it
    is already there) and a Japanese .srt beside it — from the sidecar, or
    extracted from the container's text subtitle track. Read-only on the
    original. Returns {"video": bool, "subs": "sidecar"|"embedded"|None}."""
    stage_dir = stage_mp4.rsplit("/", 1)[0]
    remote.run(f'if not exist "{_win(stage_dir)}" mkdir "{_win(stage_dir)}"')
    probe = remote.probe(ep["remote_video"])
    video, a_idx, sub_idx = _pick_streams(probe)
    if video is None:
        raise RuntimeError(f"no video stream in {ep['remote_video']}")
    result = {"video": False, "subs": None,
              "duration": float(probe.get("format", {}).get("duration") or 0) or None}

    if remote.exists(stage_mp4) and (remote.size(stage_mp4) or 0) > 0:
        log(f"  {ep['label']}: stage copy already on the PC")
        result["video"] = True
    else:
        tmp = stage_mp4 + ".part.mp4"
        log(f"  {ep['label']}: transcoding {video.get('height')}p → {cap}p on the PC (NVENC)…")
        rc, out, err = remote.run(transcode_cmd(ep["remote_video"], tmp, video, a_idx, cap),
                                  check=False)
        if rc != 0:
            log(f"  {ep['label']}: NVENC failed ({err[-200:]}); retrying with libx264…")
            remote.run(transcode_cmd(ep["remote_video"], tmp, video, a_idx, cap, encoder="x264"))
        remote.run(f'move /y "{_win(tmp)}" "{_win(stage_mp4)}" >nul')
        result["video"] = True

    if remote.exists(stage_srt):
        result["subs"] = "sidecar" if ep.get("remote_subs") else "embedded"
    elif ep.get("remote_subs"):
        src = ep["remote_subs"]
        if _wsuffix(src) == ".srt":
            remote.run(f'copy /y "{_win(src)}" "{_win(stage_srt)}" >nul')
        else:  # .ass/.vtt → srt
            remote.run(f'ffmpeg -hide_banner -loglevel error -y -i "{_win(src)}" '
                       f'-c:s srt "{_win(stage_srt)}"')
        result["subs"] = "sidecar"
    elif sub_idx is not None:
        log(f"  {ep['label']}: extracting embedded Japanese subtitle track {sub_idx}…")
        remote.run(f'ffmpeg -hide_banner -loglevel error -y -i "{_win(ep["remote_video"])}" '
                   f'-map 0:{sub_idx} -c:s srt "{_win(stage_srt)}"')
        result["subs"] = "embedded"
    else:
        log(f"  {ep['label']}: no Japanese subtitles — Stage 1 will ASR it")
    return result


# --- materialize / evict on the Mac -------------------------------------------------

def video_path(cfg, slug, ep_no):
    return episode_dir(cfg, episode_id_for(slug, ep_no)) / "video.mp4"


def materialize(cfg, slug, ep_no, log=print, remote=None):
    """Ensure <episode_dir>/video.mp4 (and the subtitle sidecar) exist
    locally, pulling from the PC's stage copy — re-transcoding from the
    original first if the stage copy is gone. Returns the video path."""
    man = load_manifest(cfg, slug)
    ep = find_episode(man, ep_no)
    scfg = series_cfg(cfg)
    remote = remote or Remote(scfg)
    stage_mp4, stage_srt = remote_stage_paths(scfg, slug, ep_no)
    dest = video_path(cfg, slug, ep_no)
    subs = local_subs_path(cfg, slug, ep_no)
    if not (remote.exists(stage_mp4) and (remote.size(stage_mp4) or 0) > 0) or \
            (not subs.exists() and not remote.exists(stage_srt)):
        remote_prepare_episode(remote, ep, stage_mp4, stage_srt,
                               cap=man.get("cap", 480), log=log)
    if not dest.exists():
        log(f"  {ep['label']}: pulling 480p copy from the PC…")
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote.scp_from(stage_mp4, dest)
        ep["fetched_at"] = now_iso()
        ep["size"] = dest.stat().st_size
        save_manifest(cfg, man)
    if not subs.exists() and remote.exists(stage_srt):
        remote.scp_from(stage_srt, subs)
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
    """Scan → manifest → per episode: PC transcode + subs → pull → enqueue.
    Idempotent: a re-run skips stage copies, local videos and queue rows that
    already exist, so an interrupted ingest just resumes."""
    scfg = series_cfg(cfg)
    remote = Remote(scfg)
    title = title or _wname(remote_dir.rstrip("/\\"))
    slug = slug or slugify(title)
    found, unparsed = scan(cfg, remote_dir, log=log)
    if not found:
        raise RuntimeError(f"no episodes with parseable numbers under {remote_dir}"
                           + (f" (unparsed: {unparsed[:5]})" if unparsed else ""))
    wanted = parse_episode_spec(episodes)
    picked = [e for e in found if wanted is None or e["ep_no"] in wanted]
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
            "remote_stage": remote_stage_paths(scfg, slug, e["ep_no"])[0],
        })
    man["episodes"] = sorted(by_no.values(), key=lambda r: r["ep_no"])
    save_manifest(cfg, man)

    from server import jobqueue as q
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    summary = {"slug": slug, "title": man["title"], "staged": [], "enqueued": [],
               "already": [], "failed": []}
    for e in picked:
        row = by_no[e["ep_no"]]
        try:
            stage_mp4, stage_srt = remote_stage_paths(scfg, slug, e["ep_no"])
            res = remote_prepare_episode(remote, row, stage_mp4, stage_srt,
                                         cap=man["cap"], log=log)
            row["subs"] = res["subs"]
            if res.get("duration"):
                row["duration"] = res["duration"]
            row["staged_at"] = now_iso()
            save_manifest(cfg, man)
            materialize(cfg, slug, e["ep_no"], log=log, remote=remote)
            # materialize saved fetched_at/size on its own copy — reload so the
            # next iteration's save doesn't clobber them
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
    server's DELETE does), the manifest — and with remote_too the PC's stage
    copies. The originals under the PC's library dir are never touched."""
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
        scfg = series_cfg(cfg)
        Remote(scfg).run(
            f'if exist "{_win(scfg["remote_stage_dir"])}\\{slug}" '
            f'rmdir /s /q "{_win(scfg["remote_stage_dir"])}\\{slug}"')
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
    p = sub.add_parser("scan", help="list the episodes a PC folder would ingest")
    p.add_argument("remote_dir")
    p = sub.add_parser("ingest", help="transcode on the PC, pull, enqueue")
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
    p = sub.add_parser("fetch", help="re-pull evicted videos from the PC")
    p.add_argument("slug")
    p.add_argument("--episodes")
    p = sub.add_parser("evict", help="drop local video.mp4 (watched only unless --all)")
    p.add_argument("slug")
    p.add_argument("--episodes")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("remove", help="delete the series from the Mac (never the originals)")
    p.add_argument("slug")
    p.add_argument("--remote", action="store_true", help="also drop the PC's stage copies")
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
        remote = Remote(series_cfg(cfg))
        for e in man["episodes"]:
            if wanted is None or e["ep_no"] in wanted:
                materialize(cfg, args.slug, e["ep_no"], log=log, remote=remote)
        print(json.dumps(status(cfg, args.slug), ensure_ascii=False, indent=2))
    elif args.verb == "evict":
        print(json.dumps(evict(cfg, args.slug, parse_episode_spec(args.episodes),
                               all_states=args.all, log=log), ensure_ascii=False, indent=2))
    elif args.verb == "remove":
        print(json.dumps(remove(cfg, args.slug, remote_too=args.remote, log=log),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
