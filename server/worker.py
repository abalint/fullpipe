"""Stage-1 batch worker + Stage-2 completion watcher (MOBILE.md pipeline).

Drains the queue one job at a time (the PC's ASR/tokenize load is serial by
nature):  queued → downloading → transcribing → tokenizing → prepared.
Per job: acquire (audio + subs + sentence transcript) → stage the phone's
480p H.264 video → coverage (flags, candidates, inert exposures).

Stage 2 stays live and human-triggered (/immerse over a PREPARED job); the
worker only *watches* for its artifacts — curate.json + prep.html appearing
flips prepared/curating → staged. No intelligence here.

Runs two ways: as the server's background thread (Worker) and one-shot from
the CLI — `python -m server.worker [SOURCE ...]` enqueues the sources, drains
the queue, and exits (the /prepare skill's engine; no server needed).
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.downloader import download_video  # noqa: E402
from engine.paths import _NOWWIN, ffmpeg_path, ffprobe_path  # noqa: E402
from server import jobqueue as q  # noqa: E402
from tools._staging import downloads_dir, episode_dir  # noqa: E402
from tools.acquire import acquire  # noqa: E402
from tools.coverage import run_coverage  # noqa: E402

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts"}

# acquire's progress lines that mean "we're past download, into ASR"
_TRANSCRIBE_MARKERS = ("transcrib", "whisper", "reazon", "elevenlabs", "asr")


def video_codec(path):
    out = subprocess.run(
        [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, **_NOWWIN)
    return out.stdout.strip().splitlines()[0] if out.stdout.strip() else None


def normalize_video(src, dest, log=lambda m: None):
    """src → dest as H.264 mp4 (MOBILE.md: the pipeline's only transcode).

    Already-H.264 sources are remuxed (stream copy — seconds); VP9/AV1 get a
    real transcode so any Android player can handle the file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    codec = video_codec(src)
    if codec is None:
        raise RuntimeError(f"no video stream in {src}")
    if codec == "h264":
        log("remuxing (already H.264)…")
        args = ["-c", "copy"]
    else:
        log(f"transcoding {codec} → H.264…")
        args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k"]
    r = subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(src), *args,
         "-movflags", "+faststart", str(tmp)],
        capture_output=True, text=True, **_NOWWIN)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")
    tmp.replace(dest)
    return dest


def stage_video(cfg, source, record, log=lambda m: None):
    """Land <episode_dir>/video.mp4 for the phone. Returns the path or None
    (audio-only sources have no video — the job still prepares fine)."""
    ep_dir = episode_dir(cfg, record["episode"]["id"], create=True)
    dest = ep_dir / "video.mp4"
    if dest.exists():
        return dest

    if record["episode"]["kind"] == "youtube":
        resolution = cfg.get("server", {}).get("video_resolution", "480p")
        log(f"downloading video ({resolution})…")
        src = download_video(source, downloads_dir(cfg), resolution=resolution,
                             progress_callback=log)
    else:
        p = Path(source)
        src = p if p.suffix.lower() in VIDEO_EXTS else None
    if src is None:
        return None
    return normalize_video(src, dest, log=log)


def process_job(cfg, conn, job, log=print):
    """Run Stage 1 for one job. State transitions + artifacts; raises nothing
    (failures land in state='failed' with the error on the job)."""
    job_id = job["id"]
    try:
        q.set_state(conn, job_id, "downloading")
        state = {"current": "downloading"}

        def progress(msg):
            msg = str(msg).strip()
            if state["current"] == "downloading" and any(
                    m in msg.lower() for m in _TRANSCRIBE_MARKERS):
                state["current"] = "transcribing"
                q.set_state(conn, job_id, "transcribing", progress_msg=msg)
            else:
                q.set_progress(conn, job_id, msg)
            log(f"  [{job_id}] {msg}")

        record = acquire(job["source"], cfg, log=progress)
        episode_id = record["episode"]["id"]
        title = record["episode"].get("title")

        try:
            stage_video(cfg, job["source"], record, log=progress)
        except Exception as e:  # video is best-effort; prep/taps still work
            log(f"  [{job_id}] video staging failed: {e}")
            q.set_progress(conn, job_id, f"video unavailable: {e}")

        q.set_state(conn, job_id, "tokenizing", episode_id=episode_id, title=title)
        run_coverage(cfg, episode_id)

        q.set_state(conn, job_id, "prepared", episode_id=episode_id, title=title)
        log(f"  [{job_id}] prepared ({episode_id})")
    except Exception as e:
        q.set_state(conn, job_id, "failed", error=str(e)[:500])
        log(f"  [{job_id}] FAILED: {e}")


def scan_stage2(cfg, conn, log=print):
    """prepared/curating → staged once /immerse has written its artifacts."""
    for job in q.list_jobs(conn):
        if job["state"] not in ("prepared", "curating"):
            continue
        ep_dir = episode_dir(cfg, job["episode_id"])
        if (ep_dir / "curate.json").exists() and (ep_dir / "prep.html").exists():
            q.set_state(conn, job["id"], "staged",
                        episode_id=job["episode_id"], title=job.get("title"))
            log(f"  [{job['id']}] staged (curate artifacts detected)")


def drain(cfg, conn, sources=(), log=print):
    """One-shot synchronous drain — Stage 1 without the server (PC-local).

    Enqueue any given sources (idempotent; a failed job re-enqueues to
    queued, which is the retry), then process queued jobs until none remain
    and pick up Stage-2 artifacts. Same states, same artifacts as the
    server's thread — the phone sees identical results next time it syncs.
    """
    summary = {"enqueued": [], "prepared": [], "failed": [], "skipped": [],
               "reaped": q.reap_stale(conn)}
    for src in sources:
        job, created = q.enqueue(conn, src)
        if not created and job["state"] != "queued":
            log(f"  [{job['id']}] already {job['state']} — not re-running")
            summary["skipped"].append(job["id"])
            continue
        summary["enqueued"].append(job["id"])
    while True:
        job = q.next_queued(conn)
        if job is None:
            break
        log(f"starting {job['id']} ({job['source']})")
        process_job(cfg, conn, job, log=log)
        final = q.get_job(conn, job["id"])
        state = final["state"] if final else "failed"
        summary["prepared" if state == "prepared" else "failed"].append(job["id"])
    scan_stage2(cfg, conn, log=log)
    return summary


def main(argv=None):
    import argparse
    import json

    from lib_config import load_config

    ap = argparse.ArgumentParser(
        prog="worker",
        description="run Stage 1 locally: enqueue sources, drain the queue, exit")
    ap.add_argument("--config")
    ap.add_argument("sources", nargs="*",
                    help="URLs/files to enqueue before draining (none = just "
                         "drain whatever is queued)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    conn = q.open_queue(Path(cfg["work_dir"]) / "queue.db")
    summary = drain(cfg, conn, args.sources, log=lambda m: print(m, file=sys.stderr))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


class Worker(threading.Thread):
    """Background drain loop. One SQLite connection of its own (thread-bound)."""

    def __init__(self, cfg, queue_db, poll_interval=5.0, log=print):
        super().__init__(daemon=True, name="fullpipe-worker")
        self.cfg = cfg
        self.queue_db = queue_db
        self.poll_interval = poll_interval
        self.log = log
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        conn = q.open_queue(self.queue_db)
        reaped = q.reap_stale(conn)
        if reaped:
            self.log(f"worker: reclaimed {len(reaped)} stranded job(s): "
                     f"{', '.join(reaped)}")
        self.log("worker: draining queue")
        while not self._stop.is_set():
            try:
                scan_stage2(self.cfg, conn, log=self.log)
                job = q.next_queued(conn)
                if job:
                    self.log(f"worker: starting {job['id']} ({job['source']})")
                    process_job(self.cfg, conn, job, log=self.log)
                    continue  # immediately look for the next one
            except Exception as e:
                self.log(f"worker: loop error: {e}")
            self._stop.wait(self.poll_interval)


if __name__ == "__main__":
    main()
