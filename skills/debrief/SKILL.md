---
name: debrief
description: Post-watch comprehension conversation for the fullPipe Immersion Workstation. Bare `/debrief` works the phone's debrief queue — episodes the user flagged with the app's 🗣 button — falling back to the most recently watched episode (asking when there's a choice); `/debrief <episode|title>` targets one. Runs a free-recall retell (scored against a hidden, weighted idea-unit map) plus a short non-leading gap pass to measure what the user actually understood — reported as two axes, event/content recall vs reflective/meaning recall — then answers the exposure-confirmation queue from the knowledge the user demonstrated (confirm/defer), clears the episode's debrief flag (re-arming delete), and closes with an honest read of what was and wasn't caught. Use for "/debrief", "quiz me on what I watched", "comprehension test", "test my understanding of that episode", "did I actually understand that", "post-watch check".
---

# /debrief — post-watch comprehension conversation

The mirror of `/immerse`: that skill preps an episode *before* watching; this
one interrogates comprehension *after*. You (this session) read the transcript
the user just watched and hold a conversation that measures how much of it
actually landed — then converts the demonstrated knowledge into ledger answers
for the exposure-confirmation queue, which is far higher-fidelity evidence
than the phone's yes/no confirm taps.

**Free recall, not a quiz.** Learned the hard way (2026-07-12): a spine of
posed questions inflates the score and is gameable — a viewer who understood
nothing can still guess "comedian revisits mean-boss job and grows up" from
the premise, and any hint in the question leaks the answer. So the primary
instrument is an **unprompted retell**: the user narrates what they remember,
you score only what they *volunteer* against a hidden idea-unit map. Producing
specific content is the thing a non-watcher can't fake; that's what makes it
honest. Posed questions come **after** the retell, sparingly, and only to
probe or re-expose gaps — never as the main measurement.

**English only in the prompts; no Japanese in the questions.** The user chose
English-mode testing on purpose (an answer bottlenecked by Japanese
*production* would mask comprehension) and asked that questions carry **no
Japanese script or romaji** — describe the scene/line in English instead of
quoting it. Save Japanese lines for the **close-out**, where quoting the
missed line back is welcome re-exposure. One consequence to accept: this boxes
out confirm-queue *vocab* checks, which need the target word named — leave
those items untested (they stay queued) unless the user opts in to naming a
single word. Answers in English or Japanese both count.

**Know the user's profile.** Standing finding (memory
`reflective-comprehension-weakness`): event/fact recall is strong, reflective/
inferential/emotional content barely lands. Expect and *measure* that split;
don't be surprised by it, and don't grind aimed questions at the reflective
gap once it's confirmed absent — surface and discuss it instead (see Step 3).

**Timing matters:** episode artifacts (`transcript.json`, `curate.json`,
`coverage.json`) survive mark-watched but are deleted when the user
swipe-deletes the episode on the phone (`DELETE /jobs/{id}` removes the
episode dir). Debrief before delete — a swipe-deleted episode cannot be
tested (no transcript survives; CLAUDE.md: never try to recover it from the
phone by destructive means).

**The debrief flag** protects exactly this window: the app's queue rows carry
a **"🗣 debrief"** button (`POST /jobs/{id}/debrief`) that sets `debrief: 1`
on the queue row. While set, the server refuses `DELETE` for the job and the
app blocks swipe-delete, so the transcript is guaranteed to still be there.
Flagged jobs are this skill's worklist; **clearing the flag when a debrief
completes is part of the job** (Step 4) — it's what re-arms delete, so a
forgotten clear leaves the user unable to tidy their queue, and a premature
clear un-protects an episode they still want tested.

## Conventions

- `FULLPIPE` = the fullPipe project root — this skill lives at
  `fullPipe/skills/debrief/SKILL.md`; resolve symlinks, then go two
  directories up. On this machine: `~/Documents/code/anki/fullPipe`.
- `PY = $FULLPIPE/.venv/bin/python` (python3.12; never the repo-root `.venv`).
- Run every command **from `$FULLPIPE`**.
- Per-episode artifacts: `<work_dir>/episodes/<episode_id>/` (work_dir from
  config.json).
- No Anki preflight needed: `confirm`/`defer` write the ledger and recompute
  the projection only — AnkiConnect is never touched.

## Step 0 — preflight

If `$FULLPIPE/config.json` is missing, stop (same rule as /immerse: tell the
user to copy `config.example.json`, don't invent one). Nothing else — this
skill only reads artifacts and writes ledger confirmations + debrief scores.

## Step 1 — pick the episode

```sh
$PY -m server.jobqueue list
```

**Jobs with `debrief: true` are the worklist** — the user flagged them on the
phone precisely to be tested, so bare `/debrief` takes them without asking:
one flagged → debrief it; several flagged → work through all of them oldest
first (each one Step 2 → Step 4 to completion before the next), unless the
user narrows it. Only when nothing is flagged, fall back to:

- **One obvious candidate** (a single `watched`/`pushing` episode since the
  last debrief) → take it, say which.
- **Several plausible** → ask which one (AskUserQuestion), newest first,
  title + watched-when.
- **`/debrief <arg>`** → match the arg against episode ids / titles / the
  source URL (flag state irrelevant — an explicit ask wins); direct-mode
  episodes with no queue row are fine if `<work_dir>/episodes/<id>/` exists.
- **No queue.db** (PC-only setup) → ask for the episode id or title and look
  under `<work_dir>/episodes/`.

Gate before starting:

- `<episode_dir>/transcript.json` must exist. If the dir is gone the episode
  was swipe-deleted — say so plainly and stop; there is nothing to test from.
- Episode not yet marked watched (`staged`/`reconciled`)? The conversation is
  still valid, but its exposures are inert so none of its items are in the
  confirm queue yet — tell the user, run the conversation anyway, and remind
  them the mark-watched step on the phone is what activates exposures and
  pushes cards. Do **not** mark it watched yourself (that fires the whole
  card-push close-out; it's the phone's move).

## Step 2 — build the idea-unit map (silently, before the first prompt)

Read everything **up front, in one pass** — mid-conversation re-reads risk
pasting answers into view and break the flow:

1. `transcript.json` / `sentences.srt` — the whole episode, start to finish.
2. `curate.json` — synopsis, focal points, keywords, grammar, phrases (may be
   absent for a blob-only or uncurated episode; the transcript alone is
   enough).
3. `coverage.json` → `stats` — how hard the episode measured; calibrates how
   deep to probe.
4. The confirmation queue:

   ```sh
   $PY -m ledger.ledgerctl query confirm-queue
   ```

   Each candidate carries `kind` (word|phrase|grammar) and `episodes` — the
   watched episodes it turned up in. Split into: **this episode's items**
   (its title appears in `episodes`) — these get woven into the conversation —
   and the rest, which you leave alone (mention the global queue size in the
   close-out if it's large).

Then build a hidden **idea-unit map** — never show it. Segment the episode
into its distinct idea units (a proposition, event, or claim the viewer could
have taken away), and for each record:

- a short label (what it is),
- a **weight** ≈ its importance/airtime (the map is the scoring rubric, so a
  throwaway one-liner weighs ~0.5, the thesis weighs 2–3),
- a **tag**: `concrete` (an event or a stated fact — what happened, who,
  numbers) vs `reflective` (an argument, stance, the emotional throughline,
  the so-what). This tag is the axis that matters — see the two scores below.

Aim for completeness, not curation: the map is what the retell is checked
*against*, so include the small units too (you just weight them low). Use
coverage.json's per-sentence classifications to sanity-check which lines the
model expected to be comprehensible.

**The native-viewer bar** governs *weighting and grading*, not inclusion:
weight toward what a native who watched once could still recall (story beats,
the thesis, striking facts, cause-and-effect) and keep incidental figures/
dates/counts/names low. When grading the retell, never dock the user for
omitting a number or a fine contrast a native wouldn't have kept either.

Confirm-queue items ride along in the map (tag them so you remember to try
them), but note the no-Japanese constraint above usually leaves vocab checks
untested — that's expected, not a failure.

## Step 3 — the conversation

Two phases. Plain conversational turns throughout (AskUserQuestion is wrong
here: answers are free-form). The user can bail any time ("that's enough") →
jump to Step 4 with whatever was covered.

### Phase A — free recall (the measurement)

Open with a single un-framed prompt: *"tell me everything you remember about
that episode, in whatever order it comes."* Then **say nothing that seeds
content** — no "and what about the machine?", no topic list. Let them run.
A short "anything else?" to drain the well is fine; a nudge that names a
segment is not (it converts recall into recognition and inflates the score).

As they talk, check what they *volunteer* against the idea-unit map:

- Score each unit **1 / 0.5 / 0** — reproduced clearly / vague or partial /
  absent. Match on *content*, not phrasing or language; a rough English gloss
  of an event counts as recall of that event.
- The map's weights are pre-registered, so the two scores are arithmetic:
  **event/content recall** = Σ(weight·score) over `concrete` units ÷ their
  total weight; **reflective/meaning recall** = the same over `reflective`
  units. Report both; the blended figure is the least useful of the three
  because it hides the split.

### Phase B — the gap pass (probe + re-expose)

*After* the retell, work the units they left out — but purpose-first:

- **If a gap might be omission not absence** (they narrate events but rarely
  volunteer themes — most people don't), aim one *non-leading* probe at the
  segment: describe the moment in English and ask what the point of it was.
  Producing it now = it was there (score it, note "cued"); a blank or a
  premise-level guess = a real gap.
- **Once the reflective gap is confirmed** (see the user's standing profile —
  aimed questions there mostly don't help him), stop testing it and switch to
  **re-exposure**: walk the missed thread back to them plainly ("here's the
  arc you didn't catch — he argued X, and the closing point was Y"). This is
  the actionable half of the debrief, and it matters more the longer the
  video; scale it up accordingly and keep it a short *conversation*, not an
  interrogation.
- Grade the *comprehension*, not the phrasing; answers in either language
  count.

**Grading confirm-queue items** — the bar for `confirm` is a demonstrated,
specific knowledge claim (the docstring calls it "as strong as a tap"):

| the user… | verdict |
|---|---|
| gives the meaning/function as used in the episode, unprompted or lightly prompted | **confirm** |
| gets it only after you reveal most of it, guesses, or hedges ("something like…?" that misses) | **defer** |
| never got asked (conversation ended early) | **leave it** — untested items stay queued; defer means "tested and not yet", not "unasked" |

## Step 4 — record + close out

Batch the ledger writes at the end (a late answer can revise an early
impression):

```sh
$PY -m ledger.ledgerctl confirm <KEY> --kind word|phrase|grammar   # demonstrated
$PY -m ledger.ledgerctl defer   <KEY> --kind word|phrase|grammar   # tested, not yet
```

(`confirm` promotes to known; `defer` snoozes until a fresh qualifying
exposure. Both recompute the projection.)

**Persist the rubric** — the scores outlive the episode dir (swipe-delete
purges artifacts; the ledger is the durable side of the
coverage→comprehension calibration). Write the scored rubric to a JSON file
(scratchpad is fine) and record it:

```sh
$PY -m ledger.ledgerctl record-debrief <episode_id> <debrief.json>
```

The payload: `comprehension_pct` (0..1, the blended airtime-weighted total over
the whole map — required), `language_pct` (0..1, **repurposed** as the
**reflective/meaning-recall** subtotal — the `reflective`-tagged units only;
this is the diagnostic axis, so record it whenever the map had reflective
units), `lag_days` (watch → debrief gap, from the queue row's watched
timestamp; omit if unknowable), and `questions` (the scored idea-unit map
verbatim: `[{q, weight, score, audio_only, note}, …]` — reuse `audio_only`
loosely for the concrete/reflective split and spell out `concrete`/`reflective`
in each `note`). Re-debriefs append — the drift is the improvement signal, and
a corrected pass supersedes a flawed one via a `METHOD NOTE` unit rather than a
delete. Skip the write only when the debrief didn't happen
(same bar as the flag clear below). `query debriefs` shows the history —
prediction (`coverage_pct`) next to measurement, oldest first; consult it in
Step 2 when a baseline exists ("last few debriefs at ~55% coverage measured
~0.4 — is this one on trend?").

**Clear the debrief flag** — the episode is tested, so re-arm delete on the
phone (do this even for episodes that weren't flagged-but-debriefed anyway:
`set-debrief off` on an unflagged job is a harmless no-op):

```sh
$PY -m server.jobqueue set-debrief <id> off
```

Skip the clear only when the debrief didn't actually happen (user bailed
before the gist question, artifacts missing) — the flag is the user's
"don't delete yet" and must outlive a false start.

Then report, one tight block:

- **The honest read** — what landed (gist, which details) and what didn't,
  with the actual lines for anything missed. An encouraging-but-false "you
  got it all!" defeats the entire point of the skill; so does rubbing it in.
  If the miss pattern is structural (everything after minute 12, every line
  from the fast speaker, everything not on screen), name it — that's
  actionable.
- **The two scores** — from the idea-unit map: **event/content recall** and
  **reflective/meaning recall**. Lead with the split (it's the finding); the
  blended number is secondary. State the watch→debrief lag next to them — a
  stale watch deflates both, and the reader should know how much salt to add.
- **Confirm outcomes** — N confirmed known, M deferred, K left untested (and
  the global queue size if items from other episodes are piling up).
- **Suggestions when earned** — a segment worth rewatching, a word worth a ★
  tap next time it surfaces, a pattern to read up on. Skip when there's
  nothing real to say.

## Failure modes

| symptom | meaning | move |
|---|---|---|
| no `watched` jobs and no arg | nothing to debrief | say so; offer to debrief a `staged`/`reconciled` episode conversation-only (see Step 1 gate) |
| episode dir missing / no transcript.json | swipe-deleted on the phone — artifacts purged | stop for that episode; nothing to test from |
| `query confirm-queue` → `[]` or no items from this episode | exposures haven't cleared the bar (or not yet marked watched) | fine — run the conversation, skip confirm/defer; `record-debrief` still happens (needs an episodes row — record-exposure creates it, so any prepared episode has one) |
| no queue.db | mobile layer unused here | direct mode: ask for the episode id, read from `<work_dir>/episodes/` |
| episode watched long ago, user remembers little | memory test, not comprehension test | say so, keep the retell short and don't push the gap pass; grade confirm items normally (knowing a word survives forgetting the plot) |

## Notes

- Re-running /debrief on the same episode is safe: confirm/defer are
  append-only evidence and the projection recomputes; a second pass just
  re-tests. Don't re-confirm items already promoted — the queue query
  naturally drops them.
- This skill never writes taste data. The post-watch survey (SURVEY.md) stays
  the phone's job; if the test contradicts the user's difficulty rating
  that's interesting conversation material, not a ledger write.
