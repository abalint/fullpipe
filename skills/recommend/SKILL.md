---
name: recommend
description: Curiosity engine for the fullPipe Immersion Workstation — "what should I watch next?" Harvests candidate Japanese videos from YouTube's unauthenticated graph (related-video rails around what you loved, JP-native search, fresh uploads from your channels), then YOU (this session — no cloud LLM) judge, rank, and diversify them in two lanes — an exploit lane ranked against the taste on record, and a taste-blind explore lane drawn from ATLAS.md's unsampled clusters under a hard quota — and hand the picks to the worker / `/immerse`. Bare `/recommend` does an open curiosity pass (breadth across genres, novelty-weighted); `/recommend about <topic>` narrows the region but keeps the variety. Reads the ledger's rated history + channels as seeds; writes nothing to the ledger (the crawl pool is a separate discovery store). Use for "/recommend", "/recommend about X", "recommend me videos", "what should I watch", "find me something new to immerse in", "discover Japanese channels".
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

**Two lanes, one hard quota.** A pass that ranks everything against taste
converges: related-rails, channel RSS, and taste-expanded queries can only fetch
the *neighborhood of what's already been watched*, and the ratings only describe
what past passes fed them — a feedback loop (the 2026-07-26 "new and fun" pass
returned the same docs/walking because of exactly this). So every pass runs an
**exploit lane** (taste-anchored, the machinery below) and an **explore lane**
supplied by `ATLAS.md` (same directory) — a taste-blind map of native Japanese
YouTube clusters the rated history has never touched. The explore quota is
structural (Step 0.4), enforced by provenance and the re-skin test (Step 4) —
never by a "novelty bonus" inside taste scoring, which is the version that
already failed.

## Conventions

- `FULLPIPE` = project root (resolve this skill's symlink, go two dirs up). On
  this machine: `~/Documents/code/anki/fullPipe`.
- `PY = $FULLPIPE/.venv/bin/python` (3.12). Run every command **from `$FULLPIPE`**.
- Tool: `$PY -m tools.harvest {seeds | run | list | gate-speech | estimate-coverage | set-status}`.
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
3. **Comprehensibility mode?** If the ask reaches for *easy-to-follow* content —
   `/recommend comprehensible`, "highly comprehensible", "stuff I can actually
   follow", "made for natives but easy", "low difficulty" — flip on
   **comprehensibility mode**. It composes with topic/open: it does **not**
   narrow the subject, it re-weights *toward what the learner can parse right
   now*. Three things change: Step 2 biases the query clusters toward easy-but-
   native genres (below), the speech gate runs **earlier and harder** (silent
   content is doubly useless here), and the Step 4.6 coverage estimate becomes a
   **ranking driver**, not just a displayed number. Crucial distinction the user
   drew: they want **native-audience** content that happens to be easy — *not*
   learner/graded material (slow-Japanese-for-learners channels, JLPT drills).
   High estimated coverage is necessary but not sufficient; a graded-reader
   channel scores high and is still a miss. Keep the native-viewer bar from
   Step 4.
   In the default (non-comprehensibility) pass the estimate is still **computed
   and shown** — it's cheap and informative — but novelty stays primary and you
   don't filter on it ([[recommend-novelty-over-centroid]]: taste is a guardrail,
   not gravity; comprehensibility is a *calibrator*, honored hard only when asked).
4. **Mood + novelty demand → the lane quota.** Parse two things from the ask's
   wording before touching any data:
   - **Mood.** An *entertainment* ask ("fun", "enjoyable", "something to laugh
     at", "light", "not another documentary") vs a *learn/edify* ask vs unstated.
     CRITICAL: "fun" does **not** mean "my highest-rated veins" — the rated
     record is edification-heavy because that's what past passes fed it, not
     because the user prefers edification (taste.md, "What this file cannot
     see"). An entertainment ask pulls the explore lane from the atlas's
     fun/hybrid-tagged clusters, and biases even the exploit lane toward the
     playful end of the record (quirky experiments) over docs.
   - **Novelty demand.** "new", "fresh", "different", "surprise me", "outside my
     usual", "don't box me in" → explore-heavy.
   **The quota is hard and structural:** bare open pass → at least **half** the
   picks from the explore lane; explicit novelty ask → at least **two-thirds**;
   `about <topic>` mode → at least a quarter (unsampled angles *on the topic*
   count). A pick counts toward the quota only if it traces to an unsampled
   atlas cluster **and** survives the re-skin test (Step 4). If explore
   candidates run short, harvest more (Step 3 is cheap) — never backfill the
   quota from the exploit lane.

## Step 1 — read the taste (seeds)

```sh
$PY -m tools.harvest seeds   # rated[], channels[], liked_video_ids[], blocked_channel_ids[], watched_count
```

Read it, plus `taste.md` if it exists. Build a working model of the taste from:

- **rated[]** — the post-watch survey per episode (SURVEY.md): `{title, channel,
  rating, tags[], genre, format, topics[], axes{}, axis_valid{}, difficulty,
  taste_valid, adjusted_enjoyment, follow, note}`, best first. How to read it:
  - **`axes`** are graded 1–5 on their own vectors — `topic_pull` (did the subject
    grip me), `presenter` (did I enjoy listening to this person), `audio_fidelity`
    (recording), `speech_clarity` (delivery — fast/slurred vs clean), `difficulty`.
    Don't average them; each steers a different lever.
  - **`axis_valid` is the censor** (SURVEY.md §2). When a video was too hard
    (`difficulty` 5), the comprehension-dependent axes are marked `false` — trust
    only the `true` ones. The classic case: understood-nothing manzai where
    `presenter`=5 is valid but `topic_pull` is not → learn "more of this performer,"
    NOT "more of that topic." `taste_valid=false` / `adjusted_enjoyment=null` says
    the same about the overall star: keep the topic, don't read the low score as a
    taste-negative (revisit-when-stronger).
  - **`follow`** is a per-channel intent decoupled from the star — `more` means keep
    this channel a strong seed even on a mediocre video; treat it as a positive
    channel signal regardless of `rating`.
  - **`note`** is free text — parse it to fill any axis the user didn't tap.
  - **chips (`tags`)**: `fascinating`/`loved_format` (+), `didnt_grab` (−, move off
    the cluster), `already_knew` (−, down-rank domain). `over_my_head` is the legacy
    difficulty flag (same as `difficulty`=5).
- **channels[]** — `{channel, channel_id, best_rating, follow_state, block_overridden,
  profile}`, followed-first. The strongest single taste predictor and your RSS + related seeds.
  **`profile`** is the presenter fingerprint (SURVEY.md §4c) — a prose
  `characterization` + attributes (dialect, register, energy, humor, speaking rate…).
  Roll the profiles of your liked channels into a **taste-in-presenters** ("calm,
  clear, mid-register solo explainers with dry humor") and match *that* against a
  candidate's channel/title to discover **unknown** channels, not just resurface known
  ones. A not-parasocial user won't binge on their own, so a `follow=more` / high
  `presenter` means deliberately re-surface that speaker's OTHER videos too (repeat
  exposure is a top listening booster).
- **liked_video_ids[]** — rating ≥ 4 **or** `follow=more`; the related-rail seeds.
- **blocked_channel_ids[]** — `follow=block` with nothing contradicting it. A hard
  veto: never recommend these, and drop any candidate whose `channel_id` is in the list.
- **`block_overridden: true`** on a channel — it was blocked, but it also produced a
  rating ≥ 4, so the veto is withheld (SURVEY.md §2, "the last-write-wins hazard":
  a block tapped on a weak episode erases an earlier `more` rather than outvoting
  it). Treat as a **strong down-weight, not a ban**: don't seed RSS from it, don't
  spend a pick on its median output, but do surface a candidate that resembles the
  episode that earned the peak. Say in the pick's line that the channel is blocked
  and why you're overriding, so the user can re-block it deliberately if they meant it.

If `taste.md` is missing or stale, note it — you'll offer to write one in Step 6.

## Step 1.5 — map the unexplored (atlas + pass log)

Read `ATLAS.md` (this skill's directory) — the taste-blind cluster map — and the
pass log at `<work_dir>/recommend-log.jsonl` (one JSON line per past pass; may
not exist yet). Mark each atlas cluster:

- **sampled** — the rated history has episodes in it → exploit territory; taste
  applies.
- **offered** — presented in a recent pass but never taken/rated → rotate away
  unless the ask points straight at it.
- **unsampled** — never rated, not recently offered → **explore-lane supply.**

Pick **3–5 unsampled clusters** for this pass: match the ask's mood (fun ask →
fun/hybrid tags), rotate against the log, and prefer clusters *far* from the
rated veins. Atlas entries flagged `near-box` resemble an existing vein in
experience shape — they're fine picks but do **not** satisfy a novelty ask or
the quota. The sampled/unsampled call comes from the ledger + log at runtime,
never from memory of past passes.

## Step 2 — expand taste into JP-native queries (your highest-leverage job)

`ytSearch/DESIGN.md` calls this "the single highest-leverage thing AI does here."
The user can't type these — the genre/format vocabulary is *cultural*, not
translational. Build **two query pools**, one per lane:

**Exploit pool (~8–10 strings)** — from the taste model, as ever:

- The 解説 (explainer) ecosystem when it fits the taste: `ゆっくり解説`, `雑学`,
  `都市伝説`, `歴史解説`, `科学解説`, `ずんだもん解説`, `なるほど系`.
- The user's demonstrated veins from the rated titles (e.g. `散歩 vlog`,
  `秘境 旅`, `廃墟 探訪`, `限界ニュータウン` — read what they actually rated high).

**Explore pool (~8–10 strings) — taste-blind by construction.** Take the chosen
unsampled clusters' search strings from `ATLAS.md` verbatim, plus light variants.
Do **not** select, reword, or filter these through the taste model — the whole
point is to fetch what the taste-anchored edges structurally cannot. Note which
queries belong to which pool: the candidate's `seed` field is how lane
provenance survives into Step 4. (The `related`/`rss` edges serve only the
exploit lane; search is the explore lane's *only* edge, so give it real strings.)

Mode adjustments:

- **Focused mode** (`about <topic>`): both pools seed from the topic — exploit =
  the topic through rated-vein formats, explore = the topic through unsampled
  atlas formats (`<topic> 検証`, `<topic> 対決`, `<topic> 街頭インタビュー`…).
  Emit *different angles*, not near-duplicates.
- **Comprehensibility mode** (Step 0.3): bias the clusters toward genres that
  skew *easy but still native* — one clear speaker, everyday register, concrete
  visually-scaffolded topics (the observed pattern: "follows the pictures" content
  lands, dense narration-only doesn't — [[debrief-calibration-baseline]]). Good
  veins: 日常 vlog / ルーティン, 一人暮らし, 簡単料理 / 作り置き (talking-to-camera
  cooking), 商店街・食べ歩き, ペット / 猫 vlog, カフェ 作業, ゆるい 雑談 / soft
  spoken talk-to-camera. Steer *away from* the hard-to-parse end: dense
  documentary narration, fast 漫才 / お笑い, heavy-dialect content, technical
  解説 lecture, overlapping-crosstalk variety. Still emit these as native search
  strings, still spread across clusters — you're shifting the *difficulty prior*,
  not abandoning breadth. Do **not** add learner/graded terms (`日本語 学習`,
  `やさしい日本語`, JLPT) — those surface the learner content the user rejects.

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

This is the LLM-judge, running as **you**. Judge the two lanes **separately** —
they have different objective functions, and letting taste leak into the explore
lane is precisely how every pass converges.

**Global gates (both lanes).** Mechanism-level only — the things the user has
stated as mechanisms, which hold regardless of lane: synthetic-TTS narration
(two-tier filter below), AI-generated imagery, `blocked_channel_ids`, obvious
junk (non-Japanese unless clearly wanted, livestream VODs, pure music,
misparses), the speech gate (4.5). These are the **only** vetoes allowed to
touch an explore candidate.

**Exploit lane** — score against the taste model (`ytSearch/DESIGN.md`):

- **relevance** — title/channel/topic fit to what they rate high (use the *valid*
  axes only — a censored `topic_pull` is not evidence about topic),
- **presenter-fit** — does the candidate's channel/title match the rolled-up
  taste-in-presenters (dialect, register, energy, delivery from the fingerprints)?
  This is how an unknown channel earns a slot,
- **follow pull** — `follow=more` channels get their own uploads + related surfaced
  (manufactured repeat-exposure); a `block_overridden` channel is heavily
  down-weighted but still eligible (Step 1),
- **− expertise-redundancy** — down-rank domains tagged `already_knew` or rated low,
- **− repetition** — don't stack one channel/format/topic; spread them,
- **glance-fit** — does it read like the calm, curious, learnable content they like
  (vs engagement-bait drama, rage-clickbait, brand-drama, overlong 総集編 unless matched).

**Explore lane — taste-fit is forbidden as a criterion.** "Doesn't look like
what they rate high" is a *disqualifying reason to drop an explore candidate*,
never a valid one — absence of taste evidence is the point. Rank instead by:

- **native-audience enjoyment** — view count (especially relative to the
  channel's size), a format natives demonstrably watch for pleasure, a title
  with a real hook rather than keyword soup,
- **cluster spread** — round-robin so ≥3 distinct atlas clusters appear among
  the explore picks; never two explore picks from one cluster,
- **learnability floor** — prefer clear single/dual speakers over
  crosstalk-by-design formats (mechanism, not taste: stated dislike),
- **the ask's mood** — a fun ask ranks the funnier candidate up.

**The re-skin test (quota enforcement).** Before counting a pick toward the
explore quota, strip the label and ask: *is the core experience one of the rated
veins?* A 団地 walk is walking; a 漁師 密着 is a singular-person doc; a 廃校
exploration is ruins; a countryside-移住 vlog is day-in-the-life. If yes, it's
an exploit pick wearing a costume — fine to include on merit, but it does not
count toward the quota. The test is about the experience shape, not the topic
noun.

**Prefilter law.** When the pool is too big to eyeball and you script a triage:
taste-scoring may only ever touch **exploit-lane** candidates. Explore
candidates are triaged by junk-dropping + per-cluster round-robin (+ view count
as a rough fun proxy) — nothing else. The 2026-07-26 pass converged exactly
because a taste-vein-scored prefilter culled 3,243 candidates before judgment;
the novel ones never reached the judge.

Land on the requested count (default 6–8), quota satisfied.

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

The *final* comprehensibility number is still `/immerse`'s job (computed on the
cleaned, punctuation-restored, repaired transcript once a pick is committed).
But a **ranking-grade estimate** is now available at selection time from the free
ASR caption — Step 4.6. In comprehensibility mode it's a ranking driver; in the
default pass it's a shown-but-not-decisive annotation. Everything else in this
step is still pure metadata.

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

## Step 4.6 — comprehensibility estimate (always run on the shortlist)

The selection-time answer to "will I actually be able to follow this?" Run it on
the speech-gated shortlist (the `ja` survivors), always — it's cheap and the
number is worth showing even in an open pass:

```sh
$PY -m tools.harvest estimate-coverage <id> <id> <id> ...
```

It pulls each candidate's `ja-orig` ASR caption (no video download — tens of KB),
tokenizes it with the **same** `engine.lemma` the real coverage pass uses, and
scores it against the **live ledger known-set**, returning per id:

- `pct` — estimated token comprehensibility (0–1), on the *same scale as
  `coverage.json`*. `iplus1` — count of one-unknown-away lines (mining yield).
  `tokens` — content tokens seen. Cached in `meta.coverage_est`; re-runs are free.
- `verdict='no_caption'`, `pct=null` — no `ja-orig` track to score (uploader
  disabled auto-subs, or it slipped the speech gate). Don't invent a number;
  present it as "coverage unknown" or drop it in comprehensibility mode.

**Read the number honestly — it is a floor, not a promise.** It's raw ASR with
no punctuation-restore or repair pass, so ASR non-words and un-adjudicated names
count *against* the learner (the cross-episode name registry claws some back, but
fresh names still cost). So the real coverage.json usually lands **at or above**
the estimate, and lived comprehension lands well below either: a one-off
post-watch interview (2026-07-11, since retired) mapped a 54% coverage.json to
~35% real comprehension ([[debrief-calibration-baseline]]). Use it to **compare
candidates** and set a floor, not to predict the watch.

How to use it:

- **Comprehensibility mode** (Step 0.3): make it a primary ranking key. Sort the
  taste-worthy, speech-passed shortlist by `pct` descending, drop `no_caption`,
  and prefer picks in a genuinely-followable band (roughly `pct ≳ 0.75` given the
  downward bias and the ~35%-of-54% comprehension mapping). A high `iplus1`
  alongside high `pct` is the sweet spot: followable
  *and* still teaching. Present the % on every pick.
- **Default / open pass**: still run it, still show `pct` on each pick's line, but
  keep novelty the ranker — a fascinating 55% wildcard belongs in an open pass;
  it just belongs *labelled*.

## Step 5 — present + hand off

Present the ranked picks as one compact block — per pick: **title · channel**,
the **lane** (an explore pick names its atlas cluster: "explore: 魚捌き — first
sample of this cluster"; an exploit pick names its evidence: "same walking-vlog
neighborhood as ★5 ガマランド"), the **est. coverage** (`~NN%` from Step 4.6, or
"coverage unknown" for a `no_caption`), a one-line *why*, and the URL
`https://www.youtube.com/watch?v=<video_id>`. Show the % even in an open pass;
in comprehensibility mode it's the sort key so lead with it. Frame explore picks
honestly as **experiments**: there is no taste evidence for them by design, and
their post-watch survey ratings are the highest-value signal the system can
collect — they're what grow the map.

Then offer the handoff (AskUserQuestion when interactive — which to take):

- **Enqueue for the worker** so Stage 1 preps them overnight and they're waiting
  in `/immerse`: `$PY -m server.jobqueue enqueue "https://www.youtube.com/watch?v=<id>"`
  (idempotent; the worker drains the queue — MOBILE.md).
- **Or curate one now**: `/immerse https://www.youtube.com/watch?v=<id>` end to end.

Record the outcome on each candidate so nothing re-surfaces next run:

```sh
$PY -m tools.harvest set-status <video_id> queued        # taken → enqueued/curated
$PY -m tools.harvest set-status <video_id> dismissed     # user rejected it
$PY -m tools.harvest set-status <video_id> presented     # pitched, no decision
```

(Leave un-mentioned candidates `new` — they stay in the pool for next time.
`presented` keeps a pitched-but-undecided pick out of the next pass's pool —
re-pitching the same video is convergence too — while staying retrievable via
`list --status presented`.)

Then **append the pass to the log** so Step 1.5 can rotate next time — one JSON
line to `<work_dir>/recommend-log.jsonl`:

```json
{"date": "YYYY-MM-DD", "ask": "<the user's words>", "mode": "open|topic|comprehensible",
 "explore_quota": "N of M", "explore_clusters": ["魚捌き", "クイズ対決", ...],
 "exploit_veins": ["singular-person doc", ...],
 "picks": [{"id": "...", "lane": "explore|exploit", "cluster": "...", "outcome": "queued|dismissed|presented"}]}
```

## Step 6 — refresh taste.md (offer, when useful)

If `taste.md` was missing or the rated set has grown a lot since it was written,
offer to (re)write it from the rated history — the legible digest ("you reliably
rate high: …; you bail on: …; ambiguous: …"). Three jobs: it feeds your next
ranking pass, it's **editable** so the user corrects you, and it answers "I don't
know what I'm looking for" by reflecting taste back. Keep it short and honest to
the data; don't invent preferences the ratings don't show. Any rewrite must keep
the **"What this file cannot see"** section (the recommendation-loop caveat):
taste.md describes the interior of the loop that produced it, its jurisdiction
is the exploit lane only, and it must never be cited against an explore pick.
Also offer to fold newly-rated explore clusters into `ATLAS.md` markers and the
digest — that's how the map grows.

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
| `estimate-coverage` → all `no_caption` | yt-dlp bot-check / IP flagged (caption fetch failing), not genuinely caption-less | same as above — the estimate can't vouch; fall back to judgment and say the number's missing, don't drop everything |
| `estimate-coverage` `pct` reads implausibly low | raw-ASR bias (names/ASR-garble counted unknown) on a name-dense or noisy-audio video | expected floor behavior — the real coverage.json lands higher; note it, don't treat the estimate as the verdict |
| a pick fails in `/immerse` later | source gone / region-locked / needs cookies | that's `/immerse`'s failure path, not this one — `set-status <id> dismissed` and move on |
| the picks look like the last pass despite a novelty ask | taste leaked into the explore lane — taste-scored prefilter, taste-worded explore queries, or re-skins counted toward the quota | re-run Steps 1.5–4 explore-only: unsampled atlas clusters, verbatim atlas queries, taste-blind triage, re-skin test; check `recommend-log.jsonl` for cluster repeats |
| explore picks keep rating ★1–2 on *mechanism* notes (silent, TTS, crosstalk) | the global gates are leaking, not taste failing | fix the gate (blocklist term, harder speech-gate), don't shrink the quota |
