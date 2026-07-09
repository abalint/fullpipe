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
import threading
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
    comps = {}  # episode_id → token_comprehensibility; same never-changes contract
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

    def _comprehensibility(episode_id):
        """Coverage's token_comprehensibility (0..1) so the queue can show and
        sort by difficulty. None until Stage 1 has written coverage.json;
        misses are not cached so the value appears once the artifact does."""
        if episode_id in comps:
            return comps[episode_id]
        try:
            val = load_coverage(cfg, episode_id).get("stats", {}).get(
                "token_comprehensibility")
        except FileNotFoundError:
            return None
        if val is not None:
            comps[episode_id] = val
        return val

    def _annotate(job, verdict):
        return {**job, **_taste(verdict),
                "duration": _duration(job["episode_id"]),
                "comprehensibility": _comprehensibility(job["episode_id"])}

    @app.get("/jobs", dependencies=[Depends(auth)])
    def get_jobs():
        verdicts = _verdicts()
        return [_annotate(j, verdicts.get(j["episode_id"]))
                for j in q.list_jobs(queue_conn())]

    @app.get("/jobs/{id_}", dependencies=[Depends(auth)])
    def get_job(id_: str):
        job = get_job_or_404(id_)
        return _annotate(job, lc.query_enjoyment(ledger_conn(), job["episode_id"]))

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

    @app.post("/jobs/{id_}/retry", dependencies=[Depends(auth)])
    def post_retry(id_: str):
        """Re-queue a failed Stage-1 job — the phone's Retry button. The worker
        picks it up on its next poll (acquire is idempotent). Only `failed`
        jobs are retryable this way; a stranded in-flight job is reclaimed
        automatically at server startup (jobqueue.reap_stale)."""
        job = get_job_or_404(id_)
        updated = q.retry_job(queue_conn(), job["id"])
        if updated is None:
            raise HTTPException(
                409, f"job is {job['state']}, not failed — nothing to retry")
        return updated

    @app.post("/jobs/{id_}/passive", dependencies=[Depends(auth)])
    def post_passive(id_: str, body: dict):
        """Move a watched episode into (or back out of) the passive-listening
        collection. Shelving only — state, artifacts and ledger are untouched;
        the flag just decides which phone list shows the row."""
        job = get_job_or_404(id_)
        passive = bool(body.get("passive", True))
        if passive and job["state"] != "watched":
            raise HTTPException(
                409, f"job is {job['state']} — only watched episodes go passive")
        conn = queue_conn()
        q.set_passive(conn, job["id"], passive)
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
        if job["state"] == "pushing":
            raise HTTPException(
                409, "cards are being pushed to Anki — wait for the close-out to finish")
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
        comps.pop(ep, None)
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
        episode's high-value words), and `interest` — the standing "want to
        learn" lemmas (persist across shows until known) that appear here, so
        the player highlights them wherever they surface.

        Curated sentences additionally carry the curate pass's `grammar`
        (pattern + form note, GRAMMAR.md — proposals flagged) and `phrases`
        (canonical + surface), so the player's word popup can show the line's
        grammatical context; absent before curation and on untagged lines."""
        try:
            coverage = load_coverage(cfg, episode_id)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        curate_path = episode_dir(cfg, episode_id) / "curate.json"
        curate = read_json(curate_path) if curate_path.exists() else {}
        grammar_at: dict[int, list] = {}
        for g in curate.get("grammar", []):
            idx, pattern = g.get("sentence_idx"), g.get("pattern")
            if idx is None or not (pattern or g.get("proposed_pattern")):
                continue
            note = {"pattern": pattern or g["proposed_pattern"],
                    "note": g.get("form_note") or g.get("gloss") or ""}
            if not pattern:
                note["proposed"] = True
            grammar_at.setdefault(idx, []).append(note)
        phrases_at: dict[int, list] = {}
        for p in curate.get("phrases", []):
            idx = p.get("sentence_idx")
            if idx is None or not p.get("canonical"):
                continue
            phrases_at.setdefault(idx, []).append(
                {"canonical": p["canonical"], "surface": p.get("surface", "")})
        interest = lc.active_interest(ledger_conn())
        here = {t.get("l") for s in coverage["sentences"] for t in s["tokens"]}
        # ~300k rows, ~0.5s to load — cache it; freq only changes when
        # build_freq reruns (offline, rare), which warrants a server restart
        if not freq_cache:
            freq_cache.update(
                ledger_conn().execute("SELECT lemma, rank FROM freq").fetchall())
        freq = freq_cache

        def tok(t):
            rank = freq.get(t.get("l"))
            return {**t, "f": rank} if rank is not None else t

        def sent(s):
            d = {"idx": s["idx"], "start": s["start"],
                 "end": s["end"], "cls": s.get("classification"),
                 "tokens": [tok(t) for t in s["tokens"]]}
            if s["idx"] in grammar_at:
                d["grammar"] = grammar_at[s["idx"]]
            if s["idx"] in phrases_at:
                d["phrases"] = phrases_at[s["idx"]]
            return d

        return {"episode_id": episode_id,
                # curated: the curate pass has run, so grammar/phrases (and the
                # curate-authored defs in /definitions) are as good as they
                # get — the app uses this to stop refreshing its sidecars
                "curated": bool(curate),
                "candidates": [c["lemma"] for c in coverage.get("candidates", [])],
                "interest": sorted(interest & here),
                "sentences": [sent(s) for s in coverage["sentences"]]}

    @app.get("/definitions/{episode_id}", dependencies=[Depends(auth)])
    def get_definitions(episode_id: str):
        """JMdict entries for every content lemma in the episode — the
        player's any-word dictionary popup. Keyed by the Sudachi lemma the
        transcript tokens already carry, so the client needs no deinflection.
        Curate-authored `defs` (words JMdict lacks: names, onomatopoeia,
        compounds — glossed by the curate pass) merge in, flagged `ai`.
        {} until `tools.jmdict build` has produced <work_dir>/jmdict.db."""
        try:
            coverage = load_coverage(cfg, episode_id)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        curate_path = episode_dir(cfg, episode_id) / "curate.json"
        curate = read_json(curate_path) if curate_path.exists() else {}
        path = jmdict.db_path(cfg)
        if not path.exists():
            return jmdict.merge_curate_defs({}, curate)
        lemmas = {t["l"] for s in coverage["sentences"]
                  for t in s["tokens"] if t.get("c") and t.get("l")}
        conn = jmdict.open_db(path)  # per-request: sqlite handles are thread-bound
        try:
            return jmdict.merge_curate_defs(
                jmdict.lookup_many(conn, lemmas), curate)
        finally:
            conn.close()

    def media_auth(request: Request, token_q: str | None):
        """The app's Filesystem.downloadFile can't always send headers —
        media endpoints also accept ?t=. Tailnet-only traffic, so a query
        token is fine."""
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
            # plus the standing high-interest set carried over from prior shows
            try:
                from tools.select import run_select
                interest = lc.active_interest(conn)
                final = run_select(cfg, episode_id, payload.get("taps", []), interest)
                result["cards_selected"] = len(final)
            except FileNotFoundError:
                result["cards_selected"] = None  # no coverage staged (blob-only episode)
            _mark_job(episode_id, "reconciled")
        return result

    def _close_out(episode_id, picks):
        """The slow tail of mark-watched, off the request thread: cut clips +
        push cards (an ffmpeg encode and several AnkiConnect round-trips per
        card), poll lapses (grows with the collection), promote. Progress
        lands on the queue row; the terminal state is `watched`, carrying the
        push error if there was one (the phone offers retry — a re-POST of
        /watched is safe, AnkiConnect skips duplicates)."""
        qconn = q.open_queue(queue_db_path(cfg))  # own handles: thread-bound
        lconn = lc.open_db(cfg["ledger_db"])
        job = q.get_job(qconn, episode_id)

        def progress(msg):
            if job:
                q.set_progress(qconn, job["id"], msg)

        error = None
        if picks:
            try:
                from tools.deck import push_cards
                push_cards(cfg, episode_id, picks, conn=lconn,
                           log=lambda m: None, on_progress=progress)
            except Exception as e:
                # watched still stands; the queue row surfaces this for retry
                error = f"cards failed: {str(e)[:300]}"
        progress("polling Anki for lapses…")
        try:
            from functools import partial
            from ledger.anki_known import anki_request
            url = cfg.get("anki_connect_url", "http://localhost:8765")
            lc.poll_lapses(lconn, partial(anki_request, url=url))
        except Exception:
            pass  # best-effort, as before: the next close-out catches up
        try:
            lc.promote(lconn)
        except Exception as e:
            error = error or f"promote failed: {str(e)[:200]}"
        if job:
            q.set_state(qconn, job["id"], "watched",
                        episode_id=job["episode_id"], title=job.get("title"),
                        error=error)

    closeouts = {}  # episode_id → Thread; lets tests (and debuggers) join
    app.state.closeouts = closeouts

    @app.post("/watched/{episode_id}", dependencies=[Depends(auth)])
    def post_watched(episode_id: str, body: dict | None = None):
        """Post-watch close-out: activate exposures now, then hand the slow
        tail (clip cutting, Anki push, lapse poll) to a background thread so
        the phone gets its answer immediately. The queue row narrates the
        tail (state `pushing`, progress "card 3/12") and flips to `watched`
        when done — a push failure lands on the row's error, and a re-POST
        retries it (duplicates are skipped by AnkiConnect, so it's safe).
        Body {"cards": false} is the disliked-it branch: exposures still
        activate (you did watch it), but nothing lands in the deck."""
        qconn = queue_conn()
        job = q.get_job(qconn, episode_id)
        if job and job["state"] == "pushing":
            raise HTTPException(409, "close-out already running for this episode")
        conn = ledger_conn()
        try:
            result = lc.mark_watched(conn, episode_id)
        except KeyError as e:
            raise HTTPException(404, str(e))

        picks = None
        if not (body or {}).get("cards", True):
            result["cards"] = {"queued": 0, "note": "declined — cards skipped"}
        else:
            # cards: feedback-selected picks; if none were staged (watched with
            # no pre-watch feedback), still run selection so the standing
            # high-interest set prioritizes wanted words — falling back to raw
            # curated picks only when there's no coverage (blob-only episode).
            ep_dir = episode_dir(cfg, episode_id)
            picks_path = ep_dir / "final_picks.json"
            if picks_path.exists():
                picks = read_json(picks_path)
            elif (ep_dir / "picks.json").exists():
                try:
                    from tools.select import run_select
                    interest = lc.active_interest(conn)
                    picks = run_select(cfg, episode_id, [], interest)
                except FileNotFoundError:
                    picks = read_json(ep_dir / "picks.json")
            result["cards"] = ({"queued": len(picks)} if picks
                               else {"queued": 0, "note": "no picks staged"})

        if job:
            q.set_state(qconn, job["id"], "pushing",
                        episode_id=job["episode_id"], title=job.get("title"),
                        progress_msg=(f"pushing {len(picks)} cards…" if picks
                                      else "closing out…"))
        t = threading.Thread(target=_close_out, args=(episode_id, picks),
                             name=f"closeout-{episode_id}", daemon=True)
        closeouts[episode_id] = t
        t.start()
        return result

    @app.post("/episodes/{episode_id}/rating", dependencies=[Depends(auth)])
    def post_rating(episode_id: str, body: dict):
        """1-5 star rating + optional taste tags (null rating clears) — appended
        to the append-only taste_events log (DESIGN.md — Taste metadata). Body:
        {"rating": 1-5|null, "tags": ["over_my_head", ...]}. Re-POST appends a
        new review; the on-read verdict takes the latest. An optional client
        "review_id" makes the POST idempotent (offline-outbox replays dedupe
        instead of double-appending)."""
        try:
            return lc.record_rating(ledger_conn(), episode_id,
                                    body.get("rating"), body.get("tags"),
                                    review_id=body.get("review_id"))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except KeyError as e:
            raise HTTPException(404, str(e))

    # --- ledger reads ---------------------------------------------------------------

    FREQ_BANDS = (1000, 2000, 5000, 10000)

    @app.get("/stats", dependencies=[Depends(auth)])
    def get_stats():
        """Progress dashboard for the app's Stats tab: the ledger's headline
        counts plus frequency-band coverage — of the N most common corpus
        lemmas (show-penetration rank), how many are known. Ledger-only (no
        AnkiConnect), so it stays fast and works with Anki closed."""
        conn = ledger_conn()
        summary = lc.query_summary(conn)
        by_status = summary["lemmas_by_status"]  # words only (GRAMMAR.md §5)
        by_source = summary["evidence_by_source"]

        # frequency-band coverage: known WORDS ∩ corpus ranks, bucketed —
        # phrases/grammar have no corpus rank and must not dilute the join.
        # Join known (~few k rows) against freq — cheap despite freq's ~300k.
        known_ranks = [r[0] for r in conn.execute(
            "SELECT f.rank FROM lemmas l JOIN freq f ON f.lemma = l.lemma "
            "WHERE l.status = 'known' AND l.kind = 'word' AND f.rank IS NOT NULL")]
        bands = [{"band": b,
                  "known": sum(1 for r in known_ranks if r <= b),
                  "total": b}
                 for b in FREQ_BANDS]

        distinct_exposed = conn.execute(
            "SELECT COUNT(DISTINCT lemma) FROM evidence "
            "WHERE source = 'exposure' AND kind = 'word'").fetchone()[0]

        phrases = summary["phrases"]
        grammar = summary["grammar"]
        return {
            "known": by_status.get("known", 0),  # words only, meaning unchanged
            "learning": by_status.get("learning", 0),
            "episodes_watched": summary["episodes"]["watched"],
            "episodes_total": summary["episodes"]["total"],
            "cards_minted": summary["cards_minted"],
            "needs_review": summary["needs_review"],
            "confirm_candidates": summary["confirm_candidates"],  # all kinds
            "words_encountered": distinct_exposed,
            "want_to_learn": len(lc.active_interest(conn)),
            "freq_bands": bands,
            "evidence_by_source": by_source,
            # sibling axes (GRAMMAR.md): tracked phrases + grammar points
            "phrases_known": phrases["by_status"].get("known", 0),
            "phrases_learning": phrases["by_status"].get("learning", 0),
            "phrases_confirm_candidates": phrases["confirm_candidates"],
            "grammar_known": grammar["by_status"].get("known", 0),
            "grammar_learning": grammar["by_status"].get("learning", 0),
            "grammar_confirm_candidates": grammar["confirm_candidates"],
            "grammar_proposed": grammar["proposed"],
        }

    @app.get("/confirm", dependencies=[Depends(auth)])
    def get_confirm():
        """The exposure-confirmation queue: items whose watched exposures
        cleared their bar, surfaced for a human "do you know this?" instead of
        being auto-promoted. Typed — every candidate carries `kind`
        (word|phrase|grammar). Words and phrases are enriched with JMdict
        senses (when jmdict.db exists; phrase headwords are JMdict keys too);
        grammar rows carry pattern/level/gloss from the taxonomy."""
        conn = ledger_conn()
        candidates = lc.query_confirm_queue(conn)
        lookup = {c["lemma"] for c in candidates if c.get("kind") != "grammar"}
        path = jmdict.db_path(cfg)
        if lookup and path.exists():
            jconn = jmdict.open_db(path)  # per-request: sqlite handles are thread-bound
            try:
                defs = jmdict.lookup_many(jconn, lookup)
            finally:
                jconn.close()
            for c in candidates:
                if c.get("kind") != "grammar":
                    c["senses"] = defs.get(c["lemma"], [])
        return {"candidates": candidates}

    @app.post("/confirm", dependencies=[Depends(auth)])
    def post_confirm(body: dict):
        """Answer one confirmation. {"kind", "key", "known": true} promotes the
        item (confirm_known → known); {"known": false} is "not yet"
        (confirm_defer → stays learning, snoozed until a fresh qualifying
        exposure). Bare {"lemma", "known"} is accepted as kind='word' for
        older clients."""
        kind = (body.get("kind") or "word").strip()
        key = (body.get("key") or body.get("lemma") or "").strip()
        if not key:
            raise HTTPException(422, "missing key")
        if kind not in ("word", "phrase", "grammar"):
            raise HTTPException(422, f"unknown kind: {kind}")
        conn = ledger_conn()
        if kind == "grammar":
            # Only taxonomy patterns are confirmable — a typo'd pattern must
            # not mint a tracked key via the confirm path.
            if not conn.execute("SELECT 1 FROM grammar_points WHERE pattern = ?",
                                (key,)).fetchone():
                raise HTTPException(404, f"not a grammar point: {key}")
        known = bool(body.get("known", True))
        if known:
            lc.confirm_known_lemma(conn, key, kind=kind)
        else:
            lc.defer_known_lemma(conn, key, kind=kind)
        lc.promote(conn)
        table, col = (("grammar_points", "pattern") if kind == "grammar"
                      else ("lemmas", "lemma"))
        row = conn.execute(
            f"SELECT status FROM {table} WHERE {col} = ?", (key,)).fetchone()
        return {"kind": kind, "key": key, "lemma": key, "known": known,
                "status": row["status"] if row else None}

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
