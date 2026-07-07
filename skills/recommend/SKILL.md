---
name: recommend
description: Curiosity engine for the fullPipe Immersion Workstation — "what should I watch next?" Harvests candidate Japanese videos from YouTube's unauthenticated graph (related-video rails around what you loved, JP-native search, fresh uploads from your channels), then YOU (this session — no cloud LLM) judge, rank, and diversify them against the taste on record, and hand the picks to the worker / `/immerse`. Bare `/recommend` does an open curiosity pass (breadth across genres, novelty-weighted); `/recommend about <topic>` narrows the region but keeps the variety. Reads the ledger's rated history + channels as seeds; writes nothing to the ledger (the crawl pool is a separate discovery store). Use for "/recommend", "/recommend about X", "recommend me videos", "what should I watch", "find me something new to immerse in", "discover Japanese channels".
---

# /recommend — curiosity orchestrator

The discovery half of the workstation (`ytSearch/DESIGN.md`). Same topology as
`/immerse`: **the dumb tool (`tools.harvest`) fetches raw candidates; the
judgment is yours.** Harvesting hits YouTube's own graph unauthenticated — no
key, no account, no PO token, no cloud LLM. The one thing an API would normally
do here — expand taste into native search terms and judge candidates — **you do
inline**, the same way `/immerse` restores punctuation with a subagent instead
of `gpt-4o-mini` (the user prefers CLI/agent over cloud API).

```
  DUMB TOOL (tools.harvest)                 THIS SESSION (judgment, no API)
  seeds ─► run(related+search+rss) ──────► [YOU: expand · judge · rank · diversify] ─► picks
            │                                        │                                    │
      YouTube graph, unauth              taste.md + rated history                enqueue → worker
      → discover.db (NOT the ledger)     → JP queries, scoring, clustering       or /immerse <url>
```

Why independent, not "harvest YouTube's algorithm": the personalized home feed
is unreachable unauthenticated (returns empty), and it optimizes watch-time /
centroid-convergence — the opposite of the novelty-seeking curiosity engine the
user wants. The reachable edges (similarity rail, search, RSS) are the right raw
material. (rec-system research 2026-07-06.)

## Conventions

- `FULLPIPE` = project root (resolve this skill's symlink, go two dirs up). On
  this machine: `~/Documents/code/anki/fullPipe`.
- `PY = $FULLPIPE/.venv/bin/python` (3.12). Run every command **from `$FULLPIPE`**.
- Tool: `$PY -m tools.harvest {seeds | run | list | set-status}`.
- Discovery store: `<work_dir>/discover.db` — separate from `ledger.db` by
  design, so crawl junk never pollutes the event-sourced ledger. Harvesting
  writes only here.
- `taste.md` (optional): a hand-written or agent-written taste digest at
  `$FULLPIPE/taste.md`. The legible profile you rank against; the user edits it.

## Step 0 — preflight

1. **Config + ledger.** If `config.json` is missing, stop (see `/immerse` Step 0).
   If the ledger has **no rated episodes** (`harvest seeds` → empty `rated`), the
   recommender has no taste to learn from yet — tell the user to watch and rate a
   few things through `/immerse` first, then come back. A cold recommender guesses.
2. **Topic?** `/recommend about <topic>` → focused mode (Step 2 seeds from the
   topic). Bare `/recommend` → open mode. Note the count if they asked for one
   ("recommend 5"); default to **6–8 picks**.

## Step 1 — read the taste (seeds)

```sh
$PY -m tools.harvest seeds        # JSON: rated[], channels[], liked_video_ids[], watched_count
```

Read it, plus `taste.md` if it exists. Build a working model of the taste from:

- **rated[]** — `{title, channel, rating, tags[], genre, format, topics[]}`, best
  first. Titles + tags carry most of the signal today (genre/format/topics are
  null on older episodes — provenance was added recently and backfills going
  forward). Read the **tags** carefully, they break the confounds:
  - `fascinating` / `loved_format` (+) — what drove a high score (topic vs format).
  - `didnt_grab` (−) genuine miss → move off that cluster.
  - `already_knew` (−) expertise → down-rank that domain (may generalize).
  - **`over_my_head` is NOT a taste-negative** — it's difficulty. A `5★ · over_my_head`
    means *loved it, was a stretch*; keep the topic, don't avoid it (revisit-when-stronger).
- **channels[]** — the strongest single taste predictor and your RSS + related seeds.
- **liked_video_ids[]** (rating ≥ 4) — the seeds whose related-rails you'll walk.

If `taste.md` is missing or stale, note it — you'll offer to write one in Step 6.

## Step 2 — expand taste into JP-native queries (your highest-leverage job)

`ytSearch/DESIGN.md` calls this "the single highest-leverage thing AI does here."
The user can't type these — the genre/format vocabulary is *cultural*, not
translational. From the taste model, write **~15–20 native Japanese search
strings**, deliberately **broad across clusters** (a curiosity engine, not a
niche-finder):

- The 解説 (explainer) ecosystem when it fits the taste: `ゆっくり解説`, `雑学`,
  `都市伝説`, `歴史解説`, `科学解説`, `ずんだもん解説`, `なるほど系`.
- The user's demonstrated veins from the rated titles (e.g. `散歩 vlog`,
  `秘境 旅`, `廃墟 探訪`, `限界ニュータウン` — read what they actually rated high).
- **Focused mode** (`about <topic>`): seed from the topic but keep novelty — emit
  ~5 *different angles* on it (native format variants), not 5 near-duplicates.

Breadth is the policy: jump between subcultures, don't drill one.

## Step 3 — harvest

Feed all three edges in one run — liked-video related-rails, your JP queries, and
channel RSS:

```sh
$PY -m tools.harvest run \
  --related <liked_video_id> [<id> ...] \
  --search "<jp query 1>" "<jp query 2>" ... \
  --rss <channel_id> [<channel_id> ...]
```

It prints the **new** candidates (already filtered against the ledger and
deduped). Then pull the full open pool so earlier harvests are in the running:

```sh
$PY -m tools.harvest list --status new
```

Each candidate: `{video_id, title, channel, channel_id, duration, view_count,
edge, seed, meta}`. `edge` tells you provenance (`related` = similarity to a
liked video; `search` = your query; `rss` = fresh from a known channel);
`meta.info` on related carries the view/age text, `meta.published` on RSS the
upload date.

## Step 4 — judge · rank · diversify (the intelligence; no API)

This is the LLM-judge, running as **you**. Score each candidate against the taste
model and the objective function (`ytSearch/DESIGN.md`):

- **relevance** — title/channel/topic fit to what they rate high,
- **novelty** — topical distance from recently-watched: **rewarded, not penalized**
  (push away from the centroid while staying glancingly-intellectual),
- **− expertise-redundancy** — down-rank domains tagged `already_knew` or rated low,
- **− repetition** — don't stack one channel/format/topic; spread them,
- **glance-fit** — does it read like the calm, curious, learnable content they like
  (vs engagement-bait drama, rage-clickbait, brand-drama, overlong 総集編 unless matched).

Then **diversify**: round-robin across genre/format clusters so the final list is
deliberately varied, and every few picks **force a wildcard** from a cluster
they've never sampled — serendipity injection. A curiosity engine that converges
stops being curious; keep the exploration budget.

Drop obvious junk (non-Japanese unless clearly wanted, livestream VODs, pure
music, misparses). Land on the requested count (default 6–8).

**Synthetic-TTS voices are hard-filtered — and it's a two-tier filter you back
up.** The user can't listen to the ゆっくり / VOICEROID / VOICEVOX / ずんだもん
synthetic-narrator voice. Tier 1 is deterministic: `harvest run` marks candidates
whose title/channel hit `discover.format_blocklist` (config.json) as `status=
'filtered'`, so they never reach you as `new` — no action needed. Tier 2 is
**you**: those formats brand themselves in the title, but an occasional one uses
the voice without labeling it (an unfamiliar VOICEROID character, an unbranded
TTS channel). If you recognize a candidate as synthetic-TTS narration, **drop it
and `set-status <id> dismissed`**, even though it passed Tier 1. When unsure,
say so in the pick's line rather than silently including it. (If the user ever
wants to see what got auto-filtered: `harvest list --status filtered`; to tune
the list, edit `discover.format_blocklist` then `harvest refilter`.)

Comprehensibility / coverage is **not** computed here — it's the on-ramp in
`/immerse`, lazily, once a pick is committed. Selection is pure metadata.

## Step 4.5 — speech gate (mandatory, before you present)

A pick is worthless if there's nothing to hear: **every recommendation must
contain Japanese speech to mine.** Wordless / music-only / ambient footage — a
整地 (land-leveling) work video, a ジオラマ build, a pure-ambient 4K walk with no
narration — can score perfectly on taste and still be a non-starter. Metadata
alone can't tell (titles don't say "silent"), so this is a deterministic probe,
not a judgment call. Run it on your **ranked shortlist** (a few more than the
target, so drops don't leave you short) before writing the block:

```sh
$PY -m tools.harvest gate-speech <id> <id> <id> ...
```

It probes each via yt-dlp and returns a `verdict` per id, moving the speechless
ones to `status='no_speech'` (cached in `meta.speech`, so re-runs are free):

- `ja` — Japanese speech present → **keep, present it.**
- `silent` — no speech at all (music/ambient) → **dropped; do not present.**
- `non_ja` — speech, but not Japanese → **dropped** (also fails the JP filter).
- `unknown` — probe failed (private/geo/removed) → left `new`; mention the
  uncertainty in the pick's line rather than vouching for it.

Only present `ja` picks. If drops leave you under the count, pull the next
candidates from the ranked pool and gate those too. (The signal: YouTube's ASR
emits a `ja-orig` caption track / sets `language=ja` only when it actually hears
Japanese — a plain `ja` auto-caption is just a translation, and manual `ja` subs
can be uploader text on a silent video, so neither is trusted. Rare miss: a
Japanese video whose uploader disabled auto-captions reads as `silent`; if a pick
you're confident is spoken comes back `silent`, `gate-speech --recheck` it or
trust your judgment and note it.)

> **Future work:** this gate proxies "has speech" via YouTube captions, which is
> only valid while the pipeline *needs* those captions. When the no-subtitle
> acquisition route ships (we transcribe caption-less videos ourselves by
> default), this gate must switch to an **audio-based** speech signal or it will
> wrongly drop the wider breadth of caption-less-but-spoken videos. See
> `DESIGN.md → The speech gate` for the full note.

## Step 5 — present + hand off

Present the ranked picks as one compact block — per pick: **title · channel**, a
one-line *why* (the relevance/novelty rationale, and the edge if telling — "same
walking-vlog neighborhood as ★5 ガマランド" / "new cluster: wildcard"), and the URL
`https://www.youtube.com/watch?v=<video_id>`.

Then offer the handoff (AskUserQuestion when interactive — which to take):

- **Enqueue for the worker** so Stage 1 preps them overnight and they're waiting
  in `/immerse`: `$PY -m server.jobqueue enqueue "https://www.youtube.com/watch?v=<id>"`
  (idempotent; the worker drains the queue — MOBILE.md).
- **Or curate one now**: `/immerse https://www.youtube.com/watch?v=<id>` end to end.

Record the outcome on each candidate so nothing re-surfaces next run:

```sh
$PY -m tools.harvest set-status <video_id> queued        # taken → enqueued/curated
$PY -m tools.harvest set-status <video_id> dismissed     # user rejected it
```

(Leave un-mentioned candidates `new` — they stay in the pool for next time.)

## Step 6 — refresh taste.md (offer, when useful)

If `taste.md` was missing or the rated set has grown a lot since it was written,
offer to (re)write it from the rated history — the legible digest ("you reliably
rate high: …; you bail on: …; ambiguous: …"). Three jobs: it feeds your next
ranking pass, it's **editable** so the user corrects you, and it answers "I don't
know what I'm looking for" by reflecting taste back. Keep it short and honest to
the data; don't invent preferences the ratings don't show.

## Notes

- **No cloud LLM, no account, no key.** Harvest is anonymous HTTP to YouTube;
  judgment is this session. The keep-warm layer (firing `yt-dlp --mark-watched`
  from the existing mark-watched flow to keep the real account's history alive)
  is a *separate, optional* piece — not part of `/recommend`.
- **The `related` edge is additive, not idempotent** — YouTube's `/next` returns a
  slightly different neighborhood each call, so re-running `run --related` on the
  same seeds legitimately discovers *more* of the cluster; the store dedupes, so
  it only ever grows with genuinely-new videos. Re-harvest freely.
- **Seeds are thin early.** With few rated episodes and sparse channel provenance,
  lean on `search` (your JP expansion) more than `related`/`rss`; as the ledger
  fills, the graph edges get richer. One good seed unravels a subculture.
- **Deferred (Phase 2), when ratings thicken (~dozens+):** a local embedding +
  kNN prior (`tools/taste_knn.py`, Ruri v3 — local, no API) to give a continuous
  predicted-rating score under the judge; plus playlist-co-occurrence and
  collab-graph (description-mining) edges for broader lateral reach. Not now —
  at a handful of ratings the kNN is cold and the LLM judge carries the pass.
- **Corrections baked in** (from the 2026-07-06 research): the featured-channels
  tab is often gone, so don't rely on it; the `/next` rail is a *similarity*
  edge, not personalization — treat it as "near what you liked," which is exactly
  what you want it for.

## Failure modes

| symptom | meaning | move |
|---|---|---|
| `harvest seeds` → empty `rated` | no ratings yet | stop; tell the user to watch + rate a few via `/immerse` first |
| `harvest run` → `0 new` on every edge | pool exhausted / all already seen or in ledger | widen the JP queries (new clusters), or re-run `--related` (additive); or rank what's already in `list` |
| `related` returns nothing for a seed | InnerTube `/next` shape changed or bot-checked | fine — search + rss still carry the run; if persistent, bump `INNERTUBE_CLIENT_VERSION` in `tools/harvest.py` |
| `search` empty for a query | yt-dlp bot-check or too-narrow query | broaden the query; if all queries fail, the IP may be flagged — try later |
| `gate-speech` → most picks `silent`/`unknown` | yt-dlp bot-check / IP flagged (extraction failing), not genuinely silent | verify one by hand; if extraction is broken, gate can't vouch — say so and lean on judgment, don't drop everything |
| a pick fails in `/immerse` later | source gone / region-locked / needs cookies | that's `/immerse`'s failure path, not this one — `set-status <id> dismissed` and move on |
