"""Job queue — the MOBILE.md lifecycle, SQLite-backed (<work_dir>/queue.db).

One queue, two producers (phone share-sheet / PC), one executor (the worker).
Jobs are idempotent by a source-derived id: for YouTube the video id is
derivable at enqueue time (yt_<id> — identical to the episode_id acquire will
assign), local files hash to local_<sha> the same way, 5ch threads parse to
page_5ch_<board>_<thread> (the page_ prefix IS the job's kind marker), series
episodes (series://<slug>/<n>, tools.series) to ser_<slug>_e<nn>, and
anything else gets src_<sha(url)> until acquire reports the real episode_id
at `prepared`.
Re-enqueuing an existing source is a no-op; re-enqueuing a `failed` job
resets it to `queued` (that IS the retry action the phone surfaces).
"""

import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.downloader import _extract_video_id  # noqa: E402
from engine.local_file import generate_local_file_id, is_local_file  # noqa: E402
from tools.pages import page_episode_id  # noqa: E402
from tools.series import series_episode_id  # noqa: E402

STATES = ("queued", "downloading", "transcribing", "tokenizing", "prepared",
          "curating", "staged", "pushing", "watched", "reconciled", "failed")
STAGE1_STATES = ("downloading", "transcribing", "tokenizing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,          -- source-derived; stable for the job's life
    episode_id   TEXT,                      -- authoritative once acquire has run
    source       TEXT NOT NULL,
    title        TEXT,
    state        TEXT NOT NULL DEFAULT 'queued',
    passive      INTEGER NOT NULL DEFAULT 0, -- in the passive-listening collection
    debrief      INTEGER NOT NULL DEFAULT 0, -- queued for a /debrief conversation
    series       TEXT,                       -- tools.series slug (box-set episodes only)
    series_title TEXT,                       -- display name of the series
    ep_no        INTEGER,                    -- playlist order within the series
    progress_msg TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_queue(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # pre-flag databases: CREATE IF NOT EXISTS won't touch them
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for flag in ("passive", "debrief"):
        if flag not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {flag} INTEGER NOT NULL DEFAULT 0")
            conn.commit()
    for col, decl in (("series", "series TEXT"), ("series_title", "series_title TEXT"),
                      ("ep_no", "ep_no INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {decl}")
            conn.commit()
    return conn


def derive_job_id(source):
    """Stable id from the source alone — matches acquire's episode_id where
    that is derivable without touching the network."""
    series_id = series_episode_id(source)
    if series_id:
        return series_id
    if is_local_file(source):
        return generate_local_file_id(source)
    page_id = page_episode_id(source)
    if page_id:
        return page_id
    vid = _extract_video_id(source)
    if vid:
        return f"yt_{vid}"
    return "src_" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def job_dict(row):
    if row is None:
        return None
    d = dict(row)
    # The phone keys everything on episode_id; before acquire runs the job id
    # is the best identifier we have, so present it as such.
    d["episode_id"] = d["episode_id"] or d["id"]
    d["passive"] = bool(d.get("passive"))
    d["debrief"] = bool(d.get("debrief"))
    # Pages are marked by their id prefix (page_episode_id) — derived, not
    # stored, so pre-pages rows need no migration.
    d["kind"] = "page" if d["id"].startswith("page_") else "episode"
    return d


def enqueue(conn, source, *, title=None, series=None, series_title=None, ep_no=None):
    """Idempotent enqueue. Returns (job, created). Series episodes
    (tools.series) pass their playlist identity so the phone can group and
    order them before Stage 1 has run."""
    job_id = derive_job_id(source)
    ts = now_iso()
    existing = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if existing:
        if existing["state"] == "failed":
            conn.execute(
                "UPDATE jobs SET state='queued', error=NULL, progress_msg=NULL, "
                "updated_at=? WHERE id=?", (ts, job_id))
            conn.commit()
            return job_dict(conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()), False
        return job_dict(existing), False
    conn.execute(
        "INSERT INTO jobs (id, source, title, series, series_title, ep_no, state, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
        (job_id, source, title, series, series_title, ep_no, ts, ts))
    conn.commit()
    return job_dict(conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()), True


def get_job(conn, id_or_episode):
    row = conn.execute(
        "SELECT * FROM jobs WHERE id = ? OR episode_id = ?",
        (id_or_episode, id_or_episode)).fetchone()
    return job_dict(row)


def list_jobs(conn):
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [job_dict(r) for r in rows]


def set_state(conn, job_id, state, *, episode_id=None, title=None,
              progress_msg=None, error=None):
    assert state in STATES, state
    sets, params = ["state=?", "updated_at=?"], [state, now_iso()]
    if episode_id is not None:
        sets.append("episode_id=?"); params.append(episode_id)
    if title is not None:
        sets.append("title=?"); params.append(title)
    # progress/error always overwritten on state change: stale messages from a
    # previous state read as lies on the queue screen
    sets.append("progress_msg=?"); params.append(progress_msg)
    sets.append("error=?"); params.append(error)
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()


def set_progress(conn, job_id, msg):
    conn.execute("UPDATE jobs SET progress_msg=?, updated_at=? WHERE id=?",
                 (msg, now_iso(), job_id))
    conn.commit()


def set_passive(conn, job_id, passive):
    """Flip a job in/out of the passive-listening collection. Pure shelving:
    state and artifacts are untouched, only which phone list shows it."""
    conn.execute("UPDATE jobs SET passive=?, updated_at=? WHERE id=?",
                 (1 if passive else 0, now_iso(), job_id))
    conn.commit()


def set_debrief(conn, job_id, debrief):
    """Flag a job as queued for a post-watch /debrief conversation (or clear
    the flag once the debrief ran). Pure flag flip like passive — state and
    artifacts untouched. While set, the server refuses DELETE for the job:
    the debrief needs the transcript, and deletion would destroy it."""
    conn.execute("UPDATE jobs SET debrief=?, updated_at=? WHERE id=?",
                 (1 if debrief else 0, now_iso(), job_id))
    conn.commit()


def delete_job(conn, job_id):
    """Drop the queue row. Artifact/ledger cleanup is the caller's job
    (server DELETE route) — the queue only owns lifecycle state."""
    cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    return cur.rowcount > 0


def next_queued(conn):
    return job_dict(conn.execute(
        "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
    ).fetchone())


def reap_stale(conn):
    """Recover jobs stranded by a process crash/restart. Called once at
    executor startup (the server's Worker.run, the CLI drain) — the single
    executor means nothing is *legitimately* mid-flight at that moment, so any
    in-flight state is a corpse to reclaim:

      • a Stage-1 state (downloading/transcribing/tokenizing) → back to
        `queued`, so the worker simply re-runs it (acquire is idempotent);
      • `pushing` → `watched` with an error, since mark_watched already ran
        before the close-out thread that died — the remaining card push is
        exactly the phone's existing re-POST-/watched retry.

    Without this a crash left the row un-runnable *and* un-deletable (DELETE
    refuses these states). Returns the list of reclaimed job ids."""
    ts = now_iso()
    reaped = []
    for row in conn.execute(
            "SELECT id, state FROM jobs WHERE state IN (?,?,?,?)",
            (*STAGE1_STATES, "pushing")).fetchall():
        if row["state"] == "pushing":
            conn.execute(
                "UPDATE jobs SET state='watched', progress_msg=NULL, "
                "error=?, updated_at=? WHERE id=?",
                ("card push interrupted by a server restart — re-submit to retry",
                 ts, row["id"]))
        else:
            conn.execute(
                "UPDATE jobs SET state='queued', progress_msg=NULL, error=NULL, "
                "updated_at=? WHERE id=?", (ts, row["id"]))
        reaped.append(row["id"])
    if reaped:
        conn.commit()
    return reaped


def retry_job(conn, job_id):
    """Re-queue a failed job (the phone's Retry button). Returns the updated
    job dict, or None if the job doesn't exist or isn't in a retryable state."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["state"] != "failed":
        return None
    conn.execute(
        "UPDATE jobs SET state='queued', error=NULL, progress_msg=NULL, "
        "updated_at=? WHERE id=?", (now_iso(), job_id))
    conn.commit()
    return job_dict(conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


# --- CLI (used by /immerse for queue review; humans can poke it too) -----------

def main(argv=None):
    import argparse
    import json

    from lib_config import load_config

    ap = argparse.ArgumentParser(
        prog="jobqueue", description="inspect/drive the fullPipe job queue")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="all jobs as JSON, newest first")
    p = sub.add_parser("enqueue", help="add a source (idempotent)")
    p.add_argument("source")
    p = sub.add_parser("set-state", help="force a job's lifecycle state")
    p.add_argument("id", help="job id or episode_id")
    p.add_argument("state", choices=STATES)
    p = sub.add_parser("delete", help="drop a queue row (row only — artifacts stay)")
    p.add_argument("id", help="job id or episode_id")
    p = sub.add_parser("set-debrief",
                       help="flag/unflag a job for a /debrief conversation")
    p.add_argument("id", help="job id or episode_id")
    p.add_argument("flag", choices=["on", "off"])
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    conn = open_queue(Path(cfg["work_dir"]) / "queue.db")

    if args.verb == "list":
        print(json.dumps(list_jobs(conn), ensure_ascii=False, indent=2))
    elif args.verb == "enqueue":
        job, created = enqueue(conn, args.source)
        print(json.dumps({**job, "created": created}, ensure_ascii=False, indent=2))
    elif args.verb == "set-state":
        job = get_job(conn, args.id)
        if not job:
            ap.error(f"no such job: {args.id}")
        set_state(conn, job["id"], args.state,
                  episode_id=job["episode_id"], title=job.get("title"))
        print(json.dumps(get_job(conn, job["id"]), ensure_ascii=False, indent=2))
    elif args.verb == "delete":
        job = get_job(conn, args.id)
        if not job:
            ap.error(f"no such job: {args.id}")
        delete_job(conn, job["id"])
        print(json.dumps({"deleted": job["id"]}, ensure_ascii=False, indent=2))
    elif args.verb == "set-debrief":
        job = get_job(conn, args.id)
        if not job:
            ap.error(f"no such job: {args.id}")
        set_debrief(conn, job["id"], args.flag == "on")
        print(json.dumps(get_job(conn, job["id"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
