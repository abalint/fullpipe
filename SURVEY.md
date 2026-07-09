# Post-watch survey — design

> **Status: implemented 2026-07-09.** Backend (schema `channels` table +
> `taste_events` axis kinds; `ledgerctl` `SURVEY_AXES`/`record_rating`/`set_follow`/
> presenter-profile verbs + per-axis censor projection), `harvest.gather_seeds`,
> the `/episodes/{id}/rating` survey body, the `/recommend` + `/immerse` skills,
> `MOBILE.md`, and the mobile survey UI (`ratingBlock`: axes pips + follow + note).
> Covered by tests (test_ledger, test_tools `HarvestSeedTest`, test_server
> `test_survey_roundtrip`; mobile smoke). This doc is the rationale of record.

Expansion of the taste-capture step (mobile Step 4: *mark watched → rate → tag*)
from a single 1–5 star into a multi-axis survey, and the rules for converting
those answers into signals the `/recommend` judge can use.

Companion to `DESIGN.md` § *Taste metadata — the enjoyment spine* (`DESIGN.md:323-483`),
which this refines rather than replaces. Everything here is built to fit the
existing event-sourced ledger; most of it needs **no migration**.

---

## 1. Principles (what we're not changing)

1. **The scalar is a cache, the event log is truth.** Ratings already live in
   append-only `taste_events` (`schema.sql:109`) grouped by `review_id`;
   `episodes.rating` is a recomputed column. Re-rating appends — drift is
   preserved. The survey keeps this exactly; it just emits *more event kinds per
   review act*.

2. **Label track vs. feature track.** You supply *verdicts* (how you felt); the
   *content* supplies features (what it was). The transcript, ASR confidence,
   loudness, and speaking rate are already in reach, and the curate step already
   writes LLM-extracted attribution. So the survey only needs to capture what a
   machine can't read off the video — **your reaction** — and can lean on
   extracted features for the rest.

3. **Store, then brief — don't numerically encode (yet).** `/recommend` is an
   LLM judge (this session), not a formula; the kNN predicted-rating score is
   Phase-2 and dormant (`DESIGN.md:588-594`). So "convert survey → recommender
   values" means (a) store each answer as a typed event and (b) extend the
   judge's briefing in `SKILL.md`. No math. The numbers become *weights* only
   when Phase-2 kNN wakes up.

4. **A tag/axis earns its place only if it disambiguates something the star +
   passively-captured features cannot** (`DESIGN.md:355-357`). Every axis below
   changes a downstream decision.

---

## 2. The survey — axes and scale semantics

All graded axes are **1–5** for UI consistency with the star. The catch that
makes conversion non-trivial: **a "5" does not mean the same thing on every
slider.** Each axis carries a *scale-semantics* tag that governs how it converts.

| Axis | Store (`kind`) | Value | Scale type | A **5** means |
|---|---|---|---|---|
| **Overall** | `rating` | 1–5 | gut-check | *"was this a good experience?"* — sanity cross-check vs. the axes; unchanged from today |
| **Topic pull** | `topic_pull` | 1–5 | monotonic | subject gripped me → topic/domain reward, scaled |
| **Presenter** | `presenter` | 1–5 (1=dislike, 3=neutral, 5=love) | monotonic (bipolar) | *"did I enjoy listening to this person?"* — a single vector (user is not parasocial, so no affinity-vs-performance split) + the extracted presenter fingerprint (§4) |
| **Audio fidelity** | `audio_fidelity` | 1–5 | monotonic | the *recording*: crisp mic, low noise, no music bleed |
| **Speech clarity** | `speech_clarity` | 1–5 | monotonic | the *delivery*: clean articulation, not fast/slurred/thick-dialect |
| **Difficulty** | `difficulty` | 1–5 | **target-seeking** | *too hard* → **censors** taste; NOT "good". 1 = too easy → low yield |
| **Channel follow** | `follow` | block / less / neutral / more | **veto floor** | (not a slider — see below) |
| **Quality chips** | `tag` | slug | categorical | qualitative extras a slider can't hold (funny, calm, niche…) |
| **Free line** | `note` | text | LLM-parsed | I extract any axis you didn't tap + enrich the profile |

### Comprehension-dependence (which axes a difficulty-censor can invalidate)
Each graded axis also carries a `comprehension_dependent` flag, because
**censoring is per-axis, not per-review** (§3). Some verdicts are only valid if you
understood the video; others hold regardless:

- **Dependent** (censored when difficulty = 5): `topic_pull`, and `overall` in its
  content-proxy role — you can't judge whether a topic gripped you if you couldn't
  follow it.
- **Independent** (survive the censor): `presenter` (the act / energy / chemistry),
  `audio_fidelity` (recording quality), `speech_clarity` (phonetic delivery — fast/
  slurred is perceptible without meaning).

> **Motivating case — manzai you didn't understand.** You follow *none* of the
> wordplay (difficulty 5) but love the two performers' act, so you mark
> `presenter = 5`. Per-review censoring would throw the whole verdict away.
> Per-axis censoring drops `topic_pull` (rightly — you can't rate a topic you
> didn't parse) but **keeps `presenter = 5`**, so the recommender learns to find
> more high-energy performer-driven acts *without* concluding you're interested in
> whatever they were riffing about. This is the payoff of splitting presenter from
> topic: on one scalar, "loved the act, understood nothing" is inexpressible.

### Why the two audio axes are split
Fidelity and clarity are orthogonal: a studio-crisp mic (fidelity 5) can record a
fast slurrer (clarity 2). They feed *different* consumers — fidelity → whether
extracted sentence-audio is worth putting on a card; clarity → comprehensibility
and mining yield. Clarity is partly entangled with difficulty (a slurrer is
harder), so the conversion must **not let both independently punish** the same
video (§3).

### Why follow is not a slider
The bottom of the follow scale (**block**) is a *hard veto*, not a weak
down-weight — averaging it into anything is wrong. Keep it as explicit states:
`block · less · neutral · more`. `more` is the answer to the founding problem
("3/5 video, but I want more from them") — it's a **channel** signal fully
decoupled from *this video's* score.

---

## 3. Conversion rules (scale type → recommender move)

The scale-semantics tag on each axis is the whole conversion. Three types:

**Monotonic** (`topic_pull`, `presenter`, `audio_fidelity`, `speech_clarity`, `overall`)
- Higher = better; magnitude = strength.
- **Now:** surfaced to the judge as graded evidence (`SKILL.md` Step 1/4). E.g.
  high `topic_pull` → topic reward; low `presenter` → down-weight this
  presenter's *attribute fingerprint*, not just the channel.
- **Phase 2:** become weighted features in the kNN taste vector.

**Target-seeking** (`difficulty`)
- Does **not** enter the taste weight. Two uses:
  1. **Per-axis censor** — `difficulty = 5` invalidates only the
     *comprehension-dependent* axes (`topic_pull`, `overall`-as-content), setting
     their `taste_valid=false` / `adjusted_enjoyment=None`; comprehension-
     independent axes (`presenter`, `audio_fidelity`, `speech_clarity`) survive
     and feed the recommender normally (§2, manzai case). This is the graded,
     per-axis generalization of today's review-level `over_my_head` tag
     (`ledgerctl.py:1121-1153`). A hard-but-loved video stays *off* the taste
     manifold for topic while its performer/production signal lives, and it keeps
     its topic for the graduation queue.
  2. **Level-matching** — the recommender prefers candidates near your
     comprehension level (feeds the ytSearch graduation queue), rather than
     maximizing difficulty.
- **Anti-double-count:** when `difficulty` already censors, a low `speech_clarity`
  must not *also* apply a taste penalty for the same video — clarity's negative
  is absorbed by the difficulty censor. Clarity only stands alone as a taste/
  minability signal when difficulty is in the comfortable band.

**Veto floor** (`follow`)
- `block` → hard filter: drop the channel from seeds *and* from the candidate
  pool.
- `less / neutral / more` → **channel-state** weight, decoupled from video
  ratings. `more` overrides a mediocre `overall` so the channel stays a strong
  seed. This replaces the crude `MAX(rating)` that is currently the *only*
  per-channel state (`harvest.py:304-309`).

**Free note** (`note`) — parsed by the session at recommend time to fill any axis
you skipped and to feed the presenter profile.

---

## 4. Two genuinely-new stores

Everything above rides the existing generic `taste_events` table. Two things have
**no home today** and are the real build:

### 4a. Channel-follow state
Channels aren't a table — they're derived from `episodes.channel_id`, and the
only per-channel state is `MAX(rating)` (`harvest.py:304-309`). So "follow"
literally can't be expressed. **Fix:** the `follow` event is emitted per-episode;
`gather_seeds` aggregates the *latest* follow per `channel_id` into the seed list,
and `block` filters the channel out. MVP can derive this exactly like everything
else — no new table strictly required for follow alone.

> **Repeat-exposure rationale.** The user is not parasocial, so they don't
> naturally binge one creator — but repeated exposure to a single voice is a top
> listening-comprehension booster. So `follow = more` / high `presenter` should
> re-surface *that speaker's own other videos*, not only "similar speakers." The
> recommender manufactures the repeat-exposure loop a parasocial learner gets for
> free.

### 4b. Presenter / channel fingerprint  *(the "AI has no insight into the presenter" fix)*
A `👍 presenter` is opaque unless we know *what* was likable — otherwise it can
only ever mean "more from this exact channel," never "find me an unknown
presenter I'd also love." **Fix:** the curate step (which already writes
`genre/format/topics/difficulty_felt`) also writes a **persistent per-channel
profile** — attributes I read off the transcript: speaking rate, dialect,
register, warmth vs. deadpan, humor markers, energy, topics they gravitate to —
accumulated across their videos and stamped with your verdicts. Then the judge
matches that **fingerprint** against channels you've never seen (your
"novelty over centroid" goal, by attribute rather than by name).

Because follow-state *and* a real profile are now genuine per-channel state (not
just `MAX(rating)`), this is the one place worth **adding a table**:

```
channels(
  channel_id   TEXT PRIMARY KEY,
  channel      TEXT,
  follow_state TEXT,     -- block|less|neutral|more (latest)
  profile      TEXT,     -- JSON: extracted attribute fingerprint + verdict history
  updated_at   TEXT
)
```

Added via the `_migrate` list (`ledgerctl.py:121-127`). Follow (4a) can populate
`follow_state`; curate populates `profile`.

### Feature track (objective proxies, for the label→feature join)
The sliders are *labels* on measurable features — so they can be pre-filled or
sanity-checked, and **disagreement is itself signal** (you find a certain "messy"
delivery charming):
- `audio_fidelity` ↔ SNR / loudness stats
- `speech_clarity` ↔ ASR confidence + words-per-minute (already produced at
  transcription)
- presenter attributes ↔ transcript profiling (curate step)

---

### 4c. Fingerprint attributes & the `channels.profile` shape

**Organizing principle:** the profile is the **feature track** — machine-observed
presenter traits — kept strictly separate from your **verdicts** (the label track
in `taste_events`). The recommender's power is in *joining* them; they never
comingle in storage.

**Attributes** (JUDGED = LLM from transcript · MEASURED = from ASR/audio):

| Group | Attribute | Source |
|---|---|---|
| Delivery | speaking rate (WPM), articulation | MEASURED (ASR conf + WPM) |
| | dialect / accent | JUDGED |
| | register (formal/polite-plain/casual/rough/mixed) | JUDGED |
| | energy (calm ↔ animated) | JUDGED |
| Style | humor (dry/absurd/wholesome/none…) | JUDGED |
| | warmth (detached ↔ warm) | JUDGED |
| | scaffolding (assumes-native ↔ teacherly — learnability) | JUDGED |
| Gravitation | topics tended-to | rollup of `episodes.topics` |
| | formats used | rollup of `episodes.format` |
| Cast | single/duo/ensemble/rotating/narration | JUDGED (robustness) |
| Production | audio-fidelity central tendency | MEASURED (SNR/loudness) |

**`channels.profile` JSON** — the **primary** field is prose `characterization`
(the LLM judge consumes it directly and free text absorbs every degenerate case);
structured fields are secondary, for cheap filtering + Phase-2 kNN:

```json
{
  "characterization": "Calm solo explainer. Standard Tokyo dialect, polite-plain register. Slow, over-articulated delivery — very learner-friendly. Dry, understated humor; warm but not effusive. Gravitates to daily-life essays and light tech.",
  "cast": "single",            // single|duo|ensemble|rotating|narration
  "presenters": null,          // optional name(s); null when unknown
  "dialect": ["standard"],     // array → absorbs mixed / multi-speaker
  "register": "polite-plain",
  "energy": 2,                 // 1 calm .. 5 animated   (judged)
  "warmth": 4,                 // 1 detached .. 5 warm   (judged)
  "humor": ["dry"],            // tags; [] if none       (judged)
  "scaffolding": 4,            // 1 assumes-native .. 5 teacherly (judged)
  "topics_gravitated": ["daily-life", "tech-light"],
  "formats": ["monologue", "vlog"],
  "measured": {                // cached aggregate of per-video proxies
    "wpm":            {"mean": 180, "range": [150, 210]},
    "asr_confidence": {"mean": 0.94},
    "audio_fidelity": {"mean": 4}
  },
  "variance": "Usually calm; reaction videos spike energy to ~4 and wpm to ~230.",
  "provenance": { "observations": 5, "updated_at": "<stamped at write>" }
}
```

**Rules:**
1. **No verdicts in the profile** — ratings are joined from `taste_events` by
   `channel_id` at read time; the event log stays the single source of truth.
2. **`measured` is a cache** (same pattern as `episodes.rating`) — per-video
   proxies live with the episode; the profile holds the rolled-up mean + range.
3. **Built incrementally at curate time — forced, not optional.** Raw transcripts
   and staged videos are **purged after watch** (server-side, per `CLAUDE.md`), so
   a fingerprint *cannot* be recomputed from scratch — the raw material is gone.
   Each curate pass folds the current video's observation into the existing
   profile (LLM reads this transcript + current `characterization` → writes the
   updated one; `observations++`). **The profile is the durable memory of a
   presenter; the transcript is ephemeral** — mirroring how the ledger keeps
   derived evidence and discards the raw.
4. **Roll-up needs no store** — "your taste-in-presenters" (for discovering
   unknown channels) is synthesized by the judge at recommend time from the
   characterizations of all liked channels. Consistent with the no-cloud-LLM,
   judge-does-the-work design.

## 5. Concrete seams (where each piece plugs in)

In dependency order — descriptions only, no code here:

1. **Vocabulary** — add a `SURVEY_AXES` registry beside `RATING_TAGS`
   (`ledgerctl.py:62`) mapping each `kind` → `{scale_type, censors, polarity}`.
   Keep `RATING_TAGS` for the categorical quality chips.
2. **Write path** — extend `record_rating` (`ledgerctl.py:345`) to accept a
   structured survey dict and emit one event per axis under a shared `review_id`
   (keep the old scalar signature working). Mirror the richer body in
   `POST /episodes/{id}/rating` (`server/app.py:509`) and the `MOBILE.md:200`
   contract.
3. **Projection** — extend `_enjoyment_from_events` (`ledgerctl.py:1121`) to read
   the new kinds, emit per-axis latest values, and compute **per-axis** validity:
   `difficulty = 5` censors only the `comprehension_dependent` axes, leaving the
   rest valid. This replaces the single review-level `taste_valid` boolean with a
   per-axis map (superseding the `over_my_head`-only rule).
4. **Seeds** — `gather_seeds` (`harvest.py:286-309`) adds the per-axis fields to
   `rated[]`, aggregates per-channel `follow_state`, and block-filters.
5. **Judge briefing** — extend `SKILL.md` Step 1 (`:58-71`) and Step 4
   (`:116-132`): the axis→move table above, difficulty-as-censor, follow-state
   handling, and **presenter-fingerprint matching** for discovery.
6. **New stores** — the `channels` table (§4b); curate writes the profile; the
   mobile survey UI gets the 5-pip sliders + follow control + note field.

**Deferred (unchanged):** Phase-2 kNN (`tools/taste_knn.py`, Ruri v3). When it
wakes, the survey axes become its feature weights — the design above is what
makes that possible without re-collecting taste.

---

## 5a. Robustness — degenerate presenter cases (the fingerprint is never load-bearing)

**Hard invariant: the fingerprint is optional enrichment, never a required key.**
Rating, watching, ledger writes, and the `/recommend` judge must all function with
`channels.profile = null`. The system works today with zero fingerprints; this
layer only *adds* signal. Worst case for any odd video = fall back to today's
behavior (channel-name + the graded axes), never a failure.

- **No identifiable presenter** (narration-only, brand channel, faceless VO): the
  speech gate already drops wordless/ambient videos, so whatever reaches the
  pipeline has a voice to rate. Curate writes thin/absent presenter traits;
  matching falls back to channel-name + other axes; the presenter *rating* still
  records (you're rating the narration).
- **Multi-presenter** (manzai, interview, panel, podcast): `profile` is a
  **free-form attribute bag, not a rigid single-speaker record** — the LLM writes
  "duo, rapid call-and-response" or "host + rotating guests, formal." You rate the
  ensemble on the presenter axis. Nothing assumes one scalar speaker.
- **Rotating cast** (news, compilations): the fingerprint captures the
  *format-level* stable trait ("multi-anchor news register") rather than averaging
  distinct people into an incoherent voice. Variance is expected, not an error.

**Enforcing rule: curation must never fail or block on presenter identification.**
The survey + ledger write path is independent of the fingerprint; fingerprinting
is a separate best-effort step that is allowed to no-op and write nothing.

## 6. Open decisions

**Resolved:**
- **Difficulty censor threshold → `=5` only, and per-axis.** A 4 ("struggled but
  got through") still counts as a trustworthy verdict; only a true "I was lost"
  (5) censors — and even then it censors *only the comprehension-dependent axes*
  (`topic_pull`, `overall`), never `presenter`/`audio_fidelity`/`speech_clarity`
  (the manzai case, §2). Low-stakes: both the threshold and the per-axis flags are
  *projection parameters* (verdict is computed on read from the event log), so
  they're re-tunable later with no re-rating.
- **Channels table → build now.** Follow-state + presenter profile are genuine
  per-channel state, so the `channels` table (§4b) ships with the survey rather
  than deriving follow first.

**Still open:**
- **Chip vocabulary** — which qualitative extras stay chips (funny, calm, niche,
  wholesome…) vs. get promoted to sliders? Keep the chip set small; let the free
  note grow it, promote frequently-typed notes later.
- **Overall: keep as gut-check** (your call — you wanted it retained) — confirmed
  in as a cross-check, not the optimization target.
- **Speech-clarity pre-fill** — auto-fill the slider from ASR confidence + WPM, or
  leave blank and only use the proxy to flag disagreement?
