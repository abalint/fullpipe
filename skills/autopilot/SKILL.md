---
name: autopilot
description: Unattended top-up for the fullPipe Immersion Workstation. One `/autopilot` tick measures the YouTube-only watchable stock (staged hours on the queue — series box sets, 5ch pages and local files are ignored) and, when it is under the configured floor (default 10 h), runs the pipeline for you — parallel Opus subagents curate every prepared episode the way `/immerse` would, and one Opus subagent runs `/recommend` non-interactively to enqueue enough new picks to close the deficit. Above the floor it reports one line and does nothing. Designed to be looped: `/loop 30m /autopilot`. Use for "/autopilot", "keep my queue topped up", "make sure I always have content", "run the pipeline automatically", "top up the watch queue".
---

# /autopilot — keep ≥ N hours of YouTube content ready

The hands-off wrapper around `/recommend` + `/immerse`. Same topology as the
rest of the workstation: **a dumb tool (`tools.stock`) measures; the skills do
the judgment** — here delegated to parallel Opus subagents so a tick finishes
in one pass. Nothing in this skill invents new behavior: every curation and
recommendation step is the existing skill text, run with its interactive
questions answered by policy (below) instead of by the user.

```
   /loop 30m /autopilot
        │
        ▼
   tools.stock ──► staged < min_hours? ──no──► one line, stop
        │ yes
        ├─► to_curate (prepared/curating) ──► N × Opus subagent: /immerse for ONE episode each (parallel waves)
        ├─► pipeline < min_hours          ──► 1 × Opus subagent: /recommend, enqueue picks to close the deficit
        └─► queued + server down          ──► drain Stage 1 locally (server.worker) in the background
```

**Scope is YouTube only** (job ids `yt_*`). Series episodes (`/series`), 5ch
pages and local files never count toward the stock and are never curated by
this skill — they stay manual.

## Conventions

- `FULLPIPE` = project root (resolve this skill's symlink, go two dirs up). On
  this machine: `~/Documents/code/anki/fullPipe`. Run every command from there.
- `PY = $FULLPIPE/.venv/bin/python` (3.12).
- Settings live in `config.json → autopilot` (all optional; defaults in
  `tools/stock.py`): `min_hours` (10), `target_hours` (14 — the level a
  top-up refills to, so one watched episode doesn't re-trigger a harvest),
  `recommend_cooldown_hours` (3), `curate_prepared_always` (false),
  `max_parallel_curate` (4).
- Log: `<work_dir>/autopilot-log.jsonl`, one line per tick that acted.
- **Never ask the user anything.** This skill runs while they're away. Every
  choice `/immerse` and `/recommend` would put to the user has a policy answer
  in the subagent prompts below. If something is genuinely blocked (no
  config), log it, report it, and stop the tick — the next tick retries.

## Step 0 — measure

```sh
$PY -m tools.stock            # JSON; `--brief` for the one-liner
```

Read `hours`, `verdict`, `to_curate`, `deficit_hours`, `picks_needed`,
`failed`, `server_up`. If `verdict.act` is false: print the `--brief` line and
**stop** (in a `/loop` dynamic tick this is a `noop`). Also stop if
`config.json` is missing (say so).

Report `failed` jobs in the tick summary every time, but never auto-retry
them: a yt-dlp failure usually means the source is gone or region-locked, and
`/recommend` will simply pick something else to fill the hours.

## Step 1 — preflight (only when acting)

1. **No Anki.** Coverage and curation read the known-set from the ledger
   alone; never launch, ping, or wait for Anki in this skill.
2. **Stage 1 executor.** If `verdict.drain` (queued jobs, no server): start
   `$PY -m server.worker` **in the background** (Bash `run_in_background`) so
   the queue keeps moving; the curate lane picks those up on a later tick.
   If the server is up, do nothing — its worker thread owns the queue.
3. **Claim the curate work** so a concurrent tick can't double-spawn:
   for every `to_curate` id, `$PY -m server.jobqueue set-state <id> curating`.

## Step 2 — spawn the lanes (all in ONE message, in parallel)

Use the Agent tool with `subagent_type: "general-purpose"` and
`model: "opus"` for every subagent. Launch the recommend subagent (if
`verdict.recommend`) **and** the first wave of curate subagents (up to
`max_parallel_curate`, if `verdict.curate`) in the same message so they run
concurrently. When a curate wave finishes, launch the next wave until
`to_curate` is exhausted. Each subagent works on exactly one job, so
parallelism is safe: artifacts are per-episode directories; the ledger is
SQLite (short writes, 5 s busy timeout — see Failure modes).

### 2a — curate subagent (one per episode; prompt template)

> You are curating ONE episode for the fullPipe Immersion Workstation,
> unattended. Project root: `<FULLPIPE>`; run every command from there with
> `PY=<FULLPIPE>/.venv/bin/python`. Episode: `<EPISODE_ID>` — title
> «<TITLE>», artifacts in `<EPISODE_DIR>` (transcript.json, coverage.json
> already exist; the job is already in state `curating`).
>
> Read `<FULLPIPE>/skills/immerse/SKILL.md` in full and follow it for this
> single episode: **Step 2.5 (punctuation gate) → Step 2.6 (repair gate) →
> re-run `$PY -m tools.coverage <EPISODE_ID>` if either gate changed
> anything → Step 3 (curate.json with the full taste-metadata block, defs
> from `tools.jmdict missing`, `record-curation`, presenter fingerprint,
> picks.json) → Step 4 (render) → Step 6 (`set-state <ID> staged`).** Skip
> Step 1 (queue triage — done) and Step 5 (deck — queue path pushes at
> mark-watched). Also read `<FULLPIPE>/CLAUDE.md`.
>
> Policy for the interactive points, since nobody is watching: never call
> AskUserQuestion; curate this episode fully; the punctuation and repair
> gates are mandatory (run `check`, and if it prints a path do the work; if
> it prints nothing but the sentences are still multi-line run-ons, export the
> blocks yourself and run the gate anyway). Sibling agents may be curating the
> same channel/series at the same time: do presenter-get → merge →
> presenter-set as one late step, pass `--episode <EPISODE_ID>`, list every
> episode in `provenance.episodes`, and fold any `provenance.folded` backlog.
> If the Agent tool is available, spawn the gate subagents exactly as the
> skill says; if it is not, do the gate's task yourself inline with the same
> rules (write punct_out.json / repair_out.json, then `apply`). Card bar:
> only genuinely excellent cards, zero is fine, no target count; every pool
> entry needs english, notes, context and sentence_furigana. If a step fails
> twice (database locked after retries, missing artifact), stop
> and report the failure — do NOT mark the job staged.
>
> Reply with a compact report only: state reached, comprehensibility %,
> focal points, pool size, and any rejected phrases / proposed grammar /
> gate rejects the user should know about.

Substitute FULLPIPE, EPISODE_ID, TITLE, EPISODE_DIR per job.

### 2b — recommend subagent (one; prompt template)

> You are running the fullPipe `/recommend` skill unattended, as an
> automatic top-up. Project root `<FULLPIPE>`; run every command from there
> with `PY=<FULLPIPE>/.venv/bin/python`. Read
> `<FULLPIPE>/skills/recommend/SKILL.md` in full (plus `ATLAS.md` next to it
> and `<FULLPIPE>/taste.md`) and run an **open pass** (bare `/recommend`,
> Step 0.4 default quota: at least half explore) with these changes:
>
> - **The goal is hours, not a count.** The YouTube pipeline is
>   `<PIPELINE_H>` h against a floor of `<MIN_H>` h; enqueue picks whose
>   summed `duration` is at least `<DEFICIT_H>` h (about `<PICKS_NEEDED>`
>   videos at the recent average). Keep the lane quota over the final set.
>   Candidate durations come with the harvest rows. Prefer 15–90 min
>   videos; nothing under 8 min unless it's exceptional.
> - **No questions.** Never call AskUserQuestion. Every pick is taken:
>   `$PY -m server.jobqueue enqueue "https://www.youtube.com/watch?v=<id>"`
>   then `$PY -m tools.harvest set-status <id> queued`. Skip Step 6
>   (taste.md rewrite offer).
> - Every gate stands: synthetic-TTS voices are dropped, `gate-speech` runs
>   on the shortlist and only `ja` picks are enqueued, `estimate-coverage`
>   runs and is shown, `blocked_channel_ids` are vetoed, no videos already in
>   the ledger/queue. Do not lower the bar to hit the hours — if the
>   harvest runs dry, harvest again with new atlas clusters; if it still
>   falls short, enqueue what clears the bar and say how many hours are
>   still missing.
> - Append the pass to `<WORK_DIR>/recommend-log.jsonl` exactly as the
>   skill specifies, with `"ask": "autopilot top-up (+<DEFICIT_H>h)"` and
>   `"mode": "open"`, plus a `"ts"` field (ISO-8601 with timezone).
>
> Reply with a compact report only: the picks (title · channel · lane ·
> cluster · duration · est. coverage · URL) and the total hours enqueued.

Substitute FULLPIPE, WORK_DIR, PIPELINE_H, MIN_H, DEFICIT_H, PICKS_NEEDED.

## Step 3 — close the tick

1. Re-run `$PY -m tools.stock --brief` for the after-picture.
2. Append one line to `<work_dir>/autopilot-log.jsonl`:

   ```json
   {"ts": "…", "before": {"staged": 6.1, "pipeline": 6.1}, "after": {"staged": 7.0, "pipeline": 14.3},
    "curated": ["yt_…"], "curate_failed": ["yt_…"], "recommended": ["<id>", …], "hours_enqueued": 8.2,
    "drained": false, "notes": "…"}
   ```

3. Report, one tight block: stock before → after; per curated episode the
   subagent's headline (comprehensibility, focal points, pool size); the
   recommend picks with lanes and hours; failures and what the next tick
   will retry. Then stop — the loop calls again.

## Running it

- Start a Claude Code session in `$FULLPIPE` and type `/loop 30m /autopilot`.
  A quiet tick is a single `tools.stock` call; an acting tick runs for as
  long as the subagents need (a curate is ~10–20 min per episode, waves in
  parallel). The recommend lane's picks land in `queued`; the server's
  worker preps them to `prepared` over the following hour, and the **next**
  tick curates them — so a top-up normally completes across two or three
  ticks.
- The session must be able to run tools without prompting (auto mode, or a
  permission allowlist covering `.venv/bin/python -m …` and file writes
  under `~/immersion`).
- The sync server should be up for Stage 1 (memory: it's hand-started —
  `nohup .venv/bin/python -m server.app`); the drain fallback covers a down
  server but the phone can't sync until it's back.

## Failure modes

| symptom | meaning | move |
|---|---|---|
| `tools.stock` → `config.json not found` | no config | stop the tick; nothing to do unattended |
| `verdict.recommend` false but stock low | pipeline already holds enough (queued/prepared) or the cooldown (`last_recommend_pass`) is active | wait for the worker / the next tick; curate lane still runs |
| a curate subagent reports `database is locked` | two writers hit the ledger's 5 s busy window | the subagent retries the command once; if it fails again it reports and leaves the job `curating` — next tick re-claims it |
| jobs stuck in `queued` with `server_up: true` | the server's worker thread is wedged | note it; if it persists two ticks, say so in the report (restarting the server is the user's call) |
| the recommend subagent enqueues < deficit | the pool ran dry under the quality bar | expected; next tick (after cooldown) harvests again |
| a curate subagent can't run the Agent tool | subagents can't nest | it does the gate work inline (prompt policy) — same rules, same `apply` |
| a `failed` job keeps showing | source gone / region-locked | never auto-retried; the hours get filled by other picks — report it each tick so the user can retry or delete from the phone |

## Notes

- The floor is measured on `staged` (watchable now); the deficit is measured
  on the whole pipeline (staged + to-curate + in-flight), so a harvest never
  piles on top of work that's already on its way.
- `target_hours` > `min_hours` is deliberate hysteresis: a top-up refills to
  the target so a single watched episode doesn't trigger a fresh harvest.
- Curation of prepared jobs is gated on the floor by default, matching the
  ask ("when under 10 h, run /recommend and /immerse"). Set
  `autopilot.curate_prepared_always: true` to curate anything the phone
  shares as soon as it's prepared, regardless of stock.
- This skill writes nothing the manual skills don't: the same artifacts,
  queue states, ledger rows and logs — a tick is indistinguishable from the
  user having run `/immerse` and `/recommend` and answered "all of them".
