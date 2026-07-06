"""fullPipe sync server — thin HTTP over the queue + ledgerctl verbs (MOBILE.md).

No new intelligence: routes map 1:1 onto existing functions. Bind to the
Tailscale interface only (config server.host "tailscale" auto-resolves via
`tailscale ip -4`); bearer token as belt-and-suspenders. Never expose publicly.

Run:
    .venv/bin/python -m server.app [--config PATH] [--host H] [--port P] [--no-worker]
"""

import argparse
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from engine.paths import ffprobe_path  # noqa: E402
from ledger import ledgerctl as lc  # noqa: E402
from lib_config import load_config  # noqa: E402
from server import jobqueue as q  # noqa: E402
from server.worker import Worker  # noqa: E402
from tools import jmdict  # noqa: E402
from tools._staging import (  # noqa: E402
    downloads_dir, episode_dir, load_coverage, load_transcript, read_json)
from tools.render import build_prep_data  # noqa: E402


def queue_db_path(cfg):
    return str(Path(cfg["work_dir"]) / "queue.db")


def create_app(cfg, start_worker=True):
    token = cfg.get("server", {}).get("token", "")

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        if start_worker:
            worker = Worker(cfg, queue_db_path(cfg))
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="fullPipe sync server", lifespan=lifespan)
    # The Capacitor webview is a different origin (https://localhost); the
    # tailnet + token are the actual access control, so CORS can be open.
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    def auth(request: Request):
        if token and request.headers.get("Authorization") != f"Bearer {token}":
            raise HTTPException(401, "bad or missing bearer token")

    # Per-request connections: SQLite handles are thread-bound and FastAPI's
    # sync endpoints run on a threadpool. Opening is cheap (idempotent schema).
    def queue_conn():
        return q.open_queue(queue_db_path(cfg))

    def ledger_conn():
        return lc.open_db(cfg["ledger_db"])

    def get_job_or_404(id_):
        job = q.get_job(queue_conn(), id_)
        if not job:
            raise HTTPException(404, f"no such job: {id_}")
        return job

    # --- health (unauthenticated liveness for the client's reachability check)

    @app.get("/health")
    def health():
        return {"ok": True}

    # --- queue -----------------------------------------------------------------

    @app.post("/jobs", status_code=201, dependencies=[Depends(auth)])
    def post_job(body: dict):
        source = (body.get("source") or "").strip()
        if not source:
            raise HTTPException(422, "missing source")
        job, created = q.enqueue(queue_conn(), source)
        return {**job, "created": created}

    def _verdicts():
        """episode_id → enjoyment verdict (rating + tags) from the taste log."""
        return lc.query_enjoyment(ledger_conn())

    def _taste(verdict):
        return {"rating": verdict["rating"] if verdict else None,
                "tags": verdict["tags"] if verdict else []}

    durations = {}  # episode_id → seconds; staged artifacts never change in place
    freq_cache = {}  # lemma → corpus rank, for /transcript highlight tiers

    def _duration(episode_id):
        """Runtime in seconds: ffprobe the staged video, else the transcript's
        last timestamp. None until Stage 1 has produced either; misses are not
        cached so the value appears as soon as the artifacts do."""
        if episode_id in durations:
            return durations[episode_id]
        dur = None
        video = episode_dir(cfg, episode_id) / "video.mp4"
        if video.exists():
            try:
                out = subprocess.run(
                    [ffprobe_path(), "-v", "error", "-show_entries",
                     "format=duration", "-of", "csv=p=0", str(video)],
                    capture_output=True, text=True, check=True)
                dur = float(out.stdout.strip())
            except Exception:
                dur = None
        if dur is None:
            try:
                sentences = load_transcript(cfg, episode_id).get("sentences") or []
                if sentences:
                    dur = sentences[-1]["end"]
            except FileNotFoundError:
                pass
        if dur is not None:
            durations[episode_id] = dur
        return dur

    @app.get("/jobs", dependencies=[Depends(auth)])
    def get_jobs():
        verdicts = _verdicts()
        return [{**j, **_taste(verdicts.get(j["episode_id"])),
                 "duration": _duration(j["episode_id"])}
                for j in q.list_jobs(queue_conn())]

    @app.get("/jobs/{id_}", dependencies=[Depends(auth)])
    def get_job(id_: str):
        job = get_job_or_404(id_)
        return {**job, **_taste(lc.query_enjoyment(ledger_conn(), job["episode_id"])),
                "duration": _duration(job["episode_id"])}

    @app.post("/jobs/{id_}/curate", dependencies=[Depends(auth)])
    def post_curate(id_: str):
        """Mark the deliberate Stage-2 step as underway. The live curate is
        /immerse on the PC (intelligence stays in skills); the worker flips
        the job to `staged` when its artifacts appear."""
        job = get_job_or_404(id_)
        if job["state"] not in ("prepared", "curating"):
            raise HTTPException(409, f"job is {job['state']}, not prepared")
        conn = queue_conn()
        q.set_state(conn, job["id"], "curating",
                    episode_id=job["episode_id"], title=job.get("title"))
        return q.get_job(conn, job["id"])

    @app.delete("/jobs/{id_}", dependencies=[Depends(auth)])
    def delete_job(id_: str):
        """Remove a job and all its artifacts: episode dir (video, transcript,
        coverage, prep, clips), the cached source download, the queue row, and
        — unless the episode was fully pipelined (watched) — its ledger
        footprint. Watched episodes retain their lemma evidence and Anki
        cards; only the files and the queue row go."""
        job = get_job_or_404(id_)
        if job["state"] in q.STAGE1_STATES:
            raise HTTPException(
                409, f"job is {job['state']} — let Stage 1 finish or fail first")
        ep = job["episode_id"]
        files_removed = 0
        d = episode_dir(cfg, ep)
        if d.exists():
            files_removed += sum(1 for p in d.rglob("*") if p.is_file())
            shutil.rmtree(d)
        # cached source download (yt_<vid> → downloads/<vid>.*). Local-file
        # sources are the user's own files — never touched.
        if ep.startswith("yt_"):
            for p in downloads_dir(cfg).glob(f"{ep[3:]}.*"):
                p.unlink()
                files_removed += 1
        ledger = lc.purge_episode(ledger_conn(), ep)
        q.delete_job(queue_conn(), job["id"])
        durations.pop(ep, None)
        return {"deleted": job["id"], "episode_id": ep,
                "files_removed": files_removed, "ledger": ledger}

    # --- staged artifacts --------------------------------------------------------

    @app.get("/prep/{episode_id}", dependencies=[Depends(auth)])
    def get_prep(episode_id: str):
        try:
            transcript = load_transcript(cfg, episode_id)
            coverage = load_coverage(cfg, episode_id)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        curate_path = episode_dir(cfg, episode_id) / "curate.json"
        curate = read_json(curate_path) if curate_path.exists() else None
        return build_prep_data(transcript, coverage, curate)

    @app.get("/transcript/{episode_id}", dependencies=[Depends(auth)])
    def get_transcript(episode_id: str):
        """Full tokenized sentence track for the in-app player's subtitle
        overlay: every sentence with timing + prep-shaped tokens (/prep ships
        only the i+1/reinforcement subset). Available from `prepared`, same
        as the video.

        Enriched for the player's highlight tiers: each sentence carries its
        coverage classification (`cls` — i_plus_1/reinforcement/... ), each
        token the corpus freq rank (`f`, absent = not in the corpus), and the
        doc the ranked candidate lemmas (`candidates`, coverage order = the
        episode's high-value words)."""
        try:
            coverage = load_coverage(cfg, episode_id)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        # ~300k rows, ~0.5s to load — cache it; freq only changes when
        # build_freq reruns (offline, rare), which warrants a server restart
        if not freq_cache:
            freq_cache.update(
                ledger_conn().execute("SELECT lemma, rank FROM freq").fetchall())
        freq = freq_cache

        def tok(t):
            rank = freq.get(t.get("l"))
            return {**t, "f": rank} if rank is not None else t

        return {"episode_id": episode_id,
                "candidates": [c["lemma"] for c in coverage.get("candidates", [])],
                "sentences": [{"idx": s["idx"], "start": s["start"],
                               "end": s["end"], "cls": s.get("classification"),
                               "tokens": [tok(t) for t in s["tokens"]]}
                              for s in coverage["sentences"]]}

    @app.get("/definitions/{episode_id}", dependencies=[Depends(auth)])
    def get_definitions(episode_id: str):
        """JMdict entries for every content lemma in the episode — the
        player's any-word dictionary popup. Keyed by the Sudachi lemma the
        transcript tokens already carry, so the client needs no deinflection.
        {} until `tools.jmdict build` has produced <work_dir>/jmdict.db."""
        try:
            coverage = load_coverage(cfg, episode_id)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        path = jmdict.db_path(cfg)
        if not path.exists():
            return {}
        lemmas = {t["l"] for s in coverage["sentences"]
                  for t in s["tokens"] if t.get("c") and t.get("l")}
        conn = jmdict.open_db(path)  # per-request: sqlite handles are thread-bound
        try:
            return jmdict.lookup_many(conn, lemmas)
        finally:
            conn.close()

    def media_auth(request: Request, token_q: str | None):
        """External players (VLC) can't send headers — media endpoints also
        accept ?token=. Tailnet-only traffic, so a query token is fine."""
        if not token:
            return
        if request.headers.get("Authorization") == f"Bearer {token}":
            return
        if token_q == token:
            return
        raise HTTPException(401, "bad or missing token")

    @app.get("/video/{episode_id}")
    def get_video(episode_id: str, request: Request, t: str | None = None):
        media_auth(request, t)
        path = episode_dir(cfg, episode_id) / "video.mp4"
        if not path.exists():
            raise HTTPException(404, f"no staged video for {episode_id}")
        return FileResponse(path, media_type="video/mp4")  # starlette serves ranges

    @app.get("/video/{episode_id}/subs")
    def get_subs(episode_id: str, request: Request, t: str | None = None):
        media_auth(request, t)
        path = episode_dir(cfg, episode_id) / "sentences.srt"
        if not path.exists():
            raise HTTPException(404, f"no subs for {episode_id}")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    # --- taps / watched (the reconcile round-trip) --------------------------------

    def _mark_job(episode_id, state):
        conn = queue_conn()
        job = q.get_job(conn, episode_id)
        if job:
            q.set_state(conn, job["id"], state,
                        episode_id=job["episode_id"], title=job.get("title"))

    @app.post("/taps", dependencies=[Depends(auth)])
    def post_taps(payload: dict):
        """Pre-watch feedback: known taps → ledger evidence, high-interest
        taps → card selection. Does NOT imply watched — watching is a later,
        separate step (the workflow decided 2026-07-05: review → feedback →
        watch → mark-watched pushes the cards)."""
        episode_id = payload.get("episode_id")
        if not episode_id:
            raise HTTPException(422, "missing episode_id")
        conn = ledger_conn()
        result = lc.apply_taps(conn, payload, anki_call=None, watched=False)
        if not result["duplicate"]:
            result["promote"] = lc.promote(conn)
            # run card selection from the full tap set (k prunes, h prioritizes)
            try:
                from tools.select import run_select
                final = run_select(cfg, episode_id, payload.get("taps", []))
                result["cards_selected"] = len(final)
            except FileNotFoundError:
                result["cards_selected"] = None  # no coverage staged (blob-only episode)
            _mark_job(episode_id, "reconciled")
        return result

    @app.post("/watched/{episode_id}", dependencies=[Depends(auth)])
    def post_watched(episode_id: str, body: dict | None = None):
        """Post-watch close-out: activate exposures AND push the selected
        cards to Anki. Re-POST retries a failed push (duplicates are skipped
        by AnkiConnect, so it's safe). Body {"cards": false} is the
        disliked-it branch: exposures still activate (you did watch it), but
        nothing lands in the deck."""
        conn = ledger_conn()
        try:
            result = lc.mark_watched(conn, episode_id)
        except KeyError as e:
            raise HTTPException(404, str(e))

        if not (body or {}).get("cards", True):
            result["cards"] = {"pushed": 0, "note": "declined — cards skipped"}
        else:
            # cards: feedback-selected picks; fall back to curated picks capped
            # at the daily limit (pool order = curate's preference order)
            ep_dir = episode_dir(cfg, episode_id)
            cap = cfg.get("deck", {}).get("new_cards_per_day", 15)
            picks_path = ep_dir / "final_picks.json"
            picks = read_json(picks_path) if picks_path.exists() else None
            if picks is None and (ep_dir / "picks.json").exists():
                picks = read_json(ep_dir / "picks.json")[:cap]
            if picks:
                try:
                    from tools.deck import push_cards
                    result["cards"] = push_cards(cfg, episode_id, picks, conn=conn,
                                                 log=lambda m: None)
                except Exception as e:
                    # watched still stands; the app surfaces this and offers retry
                    result["cards"] = {"pushed": 0, "error": str(e)[:300]}
            else:
                result["cards"] = {"pushed": 0, "note": "no picks staged"}

        try:
            from functools import partial
            from ledger.anki_known import anki_request
            url = cfg.get("anki_connect_url", "http://localhost:8765")
            result["lapse_poll"] = lc.poll_lapses(conn, partial(anki_request, url=url))
        except Exception as e:
            result["lapse_poll"] = {"skipped": str(e)[:120]}
        result["promote"] = lc.promote(conn)
        _mark_job(episode_id, "watched")
        return result

    @app.post("/episodes/{episode_id}/rating", dependencies=[Depends(auth)])
    def post_rating(episode_id: str, body: dict):
        """1-5 star rating + optional taste tags (null rating clears) — appended
        to the append-only taste_events log (DESIGN.md — Taste metadata). Body:
        {"rating": 1-5|null, "tags": ["over_my_head", ...]}. Re-POST appends a
        new review; the on-read verdict takes the latest."""
        try:
            return lc.record_rating(ledger_conn(), episode_id,
                                    body.get("rating"), body.get("tags"))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except KeyError as e:
            raise HTTPException(404, str(e))

    # --- ledger reads ---------------------------------------------------------------

    @app.get("/coverage", dependencies=[Depends(auth)])
    def get_coverage():
        conn = ledger_conn()
        return {
            "summary": lc.query_summary(conn),
            "needs_review": lc.query_needs_review(conn),
            "unwatched": lc.query_unwatched(conn),
            "ratings": lc.query_ratings(conn),
        }

    return app


def resolve_host(host):
    """config host "tailscale" → this machine's tailnet IPv4 (bind to the
    mesh interface only, per MOBILE.md)."""
    if host != "tailscale":
        return host
    import subprocess
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, check=True)
        return out.stdout.strip().splitlines()[0]
    except Exception as e:
        print(f"WARNING: could not resolve tailscale IP ({e}); "
              "binding to 127.0.0.1", file=sys.stderr)
        return "127.0.0.1"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--host", help="override config server.host")
    ap.add_argument("--port", type=int, help="override config server.port")
    ap.add_argument("--no-worker", action="store_true",
                    help="serve only; don't drain the queue")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    srv = cfg.get("server", {})
    host = resolve_host(args.host or srv.get("host", "tailscale"))
    port = args.port or srv.get("port", 8321)

    import uvicorn
    print(f"fullPipe server on http://{host}:{port} "
          f"(token {'set' if srv.get('token') else 'NOT SET'})")
    uvicorn.run(create_app(cfg, start_worker=not args.no_worker),
                host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
