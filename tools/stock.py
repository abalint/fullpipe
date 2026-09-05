"""Watchable-stock gauge for the /autopilot skill — the dumb half of "do I
have enough content prepared?"

Counts **YouTube episodes only** (job ids `yt_*`): series box sets
(`ser_*`, tools.series), 5ch pages (`page_*`), local files and opaque
sources are deliberately ignored — the autopilot tops up the YouTube
pipeline, nothing else. Watched / reconciled / failed rows never count.

Three hour buckets, from the queue's lifecycle states (MOBILE.md):

    staged      curated, the phone can pull it   → "watchable now"
    to_curate   prepared | curating              → /immerse's work
    in_flight   queued | downloading | transcribing | tokenizing → the worker's

`pipeline` = all three. The verdict block is the whole point:

    curate     staged < min_hours and there is something to curate
    recommend  pipeline < min_hours (nothing coming that would fix it) and the
               last /recommend pass is older than the cooldown
    drain      queued jobs exist but no sync server is up to grind them

`deficit_hours` is target_hours − pipeline: /recommend should enqueue enough
runtime to close it (picks_needed is a hint from the recent average length —
the target is hours, not a count). Durations come from the ledger's episode
row (yt-dlp provenance), else ffprobe on the staged video, else the
transcript's last timestamp; a queued job that has none yet is counted at the
running average and flagged `estimated`.

    .venv/bin/python -m tools.stock            # JSON report
    .venv/bin/python -m tools.stock --brief    # one line for humans
"""

import json
import math
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.paths import ffprobe_path  # noqa: E402
from lib_config import load_config  # noqa: E402
from server import jobqueue as q  # noqa: E402
from tools._staging import episode_dir, load_transcript  # noqa: E402

DEFAULTS = {
    "min_hours": 10.0,          # below this the autopilot acts
    "target_hours": 14.0,       # /recommend fills the pipeline back up to this
    "recommend_cooldown_hours": 3.0,  # don't re-harvest while the last pass is this fresh
    "curate_prepared_always": False,  # curate prepared jobs even when stock is fine
    "max_parallel_curate": 4,   # subagents /autopilot spawns per wave
    "fallback_hours_per_pick": 0.5,   # avg length guess when the ledger has none
}

STAGED = ("staged",)
TO_CURATE = ("prepared", "curating")
IN_FLIGHT = ("queued",) + q.STAGE1_STATES


def is_youtube_job(job):
    return job["id"].startswith("yt_") and not job.get("series")


def settings(cfg, **overrides):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (cfg.get("autopilot") or {}).items() if k in s})
    s.update({k: v for k, v in overrides.items() if v is not None})
    return s


def _ledger_durations(cfg):
    """episode_id → seconds from the ledger (no schema side effects: plain
    read, and nothing if the db isn't there yet)."""
    path = Path(cfg["ledger_db"])
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        if "duration" not in cols:
            return {}
        return {r[0]: r[1] for r in conn.execute(
            "SELECT id, duration FROM episodes WHERE duration IS NOT NULL")}
    finally:
        conn.close()


def _probe_duration(cfg, episode_id):
    video = episode_dir(cfg, episode_id) / "video.mp4"
    if video.exists():
        try:
            out = subprocess.run(
                [ffprobe_path(), "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", str(video)],
                capture_output=True, text=True, check=True)
            return float(out.stdout.strip())
        except Exception:
            pass
    try:
        sentences = load_transcript(cfg, episode_id).get("sentences") or []
        if sentences:
            return float(sentences[-1]["end"])
    except (FileNotFoundError, KeyError, ValueError, TypeError):
        pass
    return None


def _last_recommend_pass(cfg):
    """Timestamp of the newest recommend-log.jsonl line (date-only lines are
    read as midnight UTC), or None."""
    path = Path(cfg["work_dir"]) / "recommend-log.jsonl"
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = entry.get("ts") or entry.get("date")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if last is None or ts > last:
            last = ts
    return last


def server_up(cfg, timeout=2.0):
    """Is the sync server (and so its worker thread) alive? HTTP health on the
    configured host/port, falling back to a process check."""
    srv = cfg.get("server") or {}
    host = srv.get("host", "tailscale")
    if host == "tailscale":
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=5)
            host = out.stdout.strip().splitlines()[0] if out.returncode == 0 else "127.0.0.1"
        except Exception:
            host = "127.0.0.1"
    port = srv.get("port", 8321)
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        pass
    try:
        r = subprocess.run(["pgrep", "-f", "server.app"], capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def compute(cfg, jobs, *, durations=None, probe=None, last_pass=None,
            now=None, server_alive=None, **overrides):
    """Pure core: queue rows + duration lookups → the report dict. `probe` is
    called for episode ids the ledger has no duration for."""
    s = settings(cfg, **overrides)
    durations = dict(durations or {})
    probe = probe or (lambda ep: None)
    now = now or datetime.now(timezone.utc)

    rows = {"staged": [], "to_curate": [], "in_flight": [], "failed": []}
    for job in jobs:
        if not is_youtube_job(job):
            continue
        st = job["state"]
        bucket = ("staged" if st in STAGED else "to_curate" if st in TO_CURATE
                  else "in_flight" if st in IN_FLIGHT else "failed" if st == "failed"
                  else None)
        if bucket is None:
            continue
        ep = job["episode_id"]
        secs = durations.get(ep)
        if secs is None:
            secs = probe(ep)
        rows[bucket].append({"id": job["id"], "episode_id": ep, "state": st,
                             "title": job.get("title"), "secs": secs,
                             "error": job.get("error")})

    known = [r["secs"] for b in ("staged", "to_curate", "in_flight")
             for r in rows[b] if r["secs"]]
    avg_secs = (sum(known) / len(known)) if known else s["fallback_hours_per_pick"] * 3600
    for b in ("staged", "to_curate", "in_flight"):
        for r in rows[b]:
            r["estimated"] = r["secs"] is None
            if r["secs"] is None:
                r["secs"] = avg_secs
            r["hours"] = round(r["secs"] / 3600, 2)

    hours = {b: round(sum(r["secs"] for r in rows[b]) / 3600, 2)
             for b in ("staged", "to_curate", "in_flight")}
    hours["pipeline"] = round(hours["staged"] + hours["to_curate"] + hours["in_flight"], 2)

    below = hours["staged"] < s["min_hours"]
    deficit = max(0.0, s["target_hours"] - hours["pipeline"]) if hours["pipeline"] < s["min_hours"] else 0.0
    cooldown_ok = True
    if last_pass is not None:
        cooldown_ok = now - last_pass >= timedelta(hours=s["recommend_cooldown_hours"])

    curate = bool(rows["to_curate"]) and (below or s["curate_prepared_always"])
    recommend = deficit > 0 and cooldown_ok
    queued = [r for r in rows["in_flight"] if r["state"] == "queued"]
    drain = bool(queued) and server_alive is False

    reasons = []
    if not below:
        reasons.append(f"staged {hours['staged']}h ≥ min {s['min_hours']}h")
    else:
        reasons.append(f"staged {hours['staged']}h < min {s['min_hours']}h")
    if curate:
        reasons.append(f"{len(rows['to_curate'])} to curate")
    if deficit > 0 and not cooldown_ok:
        reasons.append("recommend on cooldown")
    elif recommend:
        reasons.append(f"pipeline {hours['pipeline']}h → need +{deficit:.1f}h")
    elif below:
        reasons.append(f"pipeline {hours['pipeline']}h ≥ min — nothing to recommend")
    if drain:
        reasons.append(f"{len(queued)} queued, server down")

    return {
        "youtube_only": True,
        "settings": s,
        "hours": hours,
        "staged": rows["staged"],
        "to_curate": rows["to_curate"],
        "in_flight": rows["in_flight"],
        "failed": rows["failed"],
        "avg_hours_per_pick": round(avg_secs / 3600, 2),
        "deficit_hours": round(deficit, 2),
        "picks_needed": math.ceil(deficit / (avg_secs / 3600)) if deficit > 0 else 0,
        "last_recommend_pass": last_pass.isoformat() if last_pass else None,
        "server_up": server_alive,
        "verdict": {"curate": curate, "recommend": recommend, "drain": drain,
                    "act": curate or recommend or drain,
                    "reason": "; ".join(reasons)},
    }


def report(cfg, **overrides):
    conn = q.open_queue(Path(cfg["work_dir"]) / "queue.db")
    try:
        jobs = q.list_jobs(conn)
    finally:
        conn.close()
    return compute(cfg, jobs, durations=_ledger_durations(cfg),
                   probe=lambda ep: _probe_duration(cfg, ep),
                   last_pass=_last_recommend_pass(cfg),
                   server_alive=server_up(cfg), **overrides)


def brief(rep):
    h, v = rep["hours"], rep["verdict"]
    acts = [k for k in ("curate", "recommend", "drain") if v[k]] or ["nothing"]
    return (f"stock: staged {h['staged']}h · to-curate {h['to_curate']}h · "
            f"in-flight {h['in_flight']}h · pipeline {h['pipeline']}h · "
            f"do: {', '.join(acts)} ({v['reason']})")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="stock", description="YouTube-only watchable-stock gauge (/autopilot)")
    ap.add_argument("--config")
    ap.add_argument("--min-hours", type=float)
    ap.add_argument("--target-hours", type=float)
    ap.add_argument("--brief", action="store_true", help="one human line instead of JSON")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    rep = report(cfg, min_hours=args.min_hours, target_hours=args.target_hours)
    if args.brief:
        print(brief(rep))
    else:
        print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
