---
name: debrief
description: Post-watch comprehension conversation for the fullPipe Immersion Workstation. Bare `/debrief` works the phone's debrief queue — episodes the user flagged with the app's 🗣 button — falling back to the most recently watched episode (asking when there's a choice); `/debrief <episode|title>` targets one. Runs an English conversation that tests what the user actually understood — main idea → key details → targeted probes of focal vocab/grammar quoted from the transcript — then answers the exposure-confirmation queue from the knowledge the user demonstrated (confirm/defer), clears the episode's debrief flag (re-arming delete), and closes with an honest read of what was and wasn't caught. Use for "/debrief", "quiz me on what I watched", "comprehension test", "test my understanding of that episode", "did I actually understand that", "post-watch check".
---

# /debrief — post-watch comprehension conversation

The mirror of `/immerse`: that skill preps an episode *before* watching; this
one interrogates comprehension *after*. You (this session) read the transcript
the user just watched and hold a conversation that measures how much of it
actually landed — then converts the demonstrated knowledge into ledger answers
for the exposure-confirmation queue, which is far higher-fidelity evidence
than the phone's yes/no confirm taps.

**Questions in English, content in Japanese.** The user chose English-mode
testing on purpose: an answer limited by Japanese *production* skill would
mask real comprehension. Ask in English; quote the episode's Japanese lines
freely (that's re-exposure, a feature); accept answers in either language.

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
skill only reads artifacts and writes ledger confirmations.

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

## Step 2 — prepare the spine (silently, before the first question)

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

Then draft a hidden checklist — never show it:

- **Gist** (1 question): premise / main arc, open-ended.
- **Key details** (3–5): specific content questions spread across the
  runtime — beginning, middle, end — so a doze-off in the second half shows
  up. Prefer details that matter to the arc over trivia.
- **Targeted probes**: the curate pass's focal points and any confirm-queue
  items, each anchored to its actual line — quote the Japanese and ask what
  it meant there, or describe the moment and ask what word/pattern the
  presenter used. Grammar items: quote the sentence, ask what the pattern
  contributes to it.
- **Inference** (1–2): a why/so-what question that only someone who followed
  the thread can answer (speaker's stance, the joke's setup, what happens
  next and why).

Scale to the episode: ~6–10 spine questions for a 20-minute video; fewer for
a short. Every this-episode confirm-queue item gets covered — woven in where
natural, else in a quick lightning round at the end ("a few quick word
checks: what did 縄張り mean in this one?").

## Step 3 — the conversation

Plain conversational turns — one question at a time, wait for the answer,
follow up. (AskUserQuestion is wrong here: answers are free-form.)

Rules of engagement:

- **Never leak the answer in the question.** "What was the shrine dog
  guarding?" not "Why was the dog guarding its territory (縄張り)?"
- **Follow up on partials** before moving on — "close; what happened right
  after?" A second chance distinguishes shaky from absent.
- **Adapt live.** Nailing everything → skip ahead to inference and the
  hardest probes. Struggling → step down to scaffolded questions (offer the
  scene, ask for the meaning) and shorten the spine; a failed test should
  end kindly but honestly, not grind on.
- Answers in English or Japanese both count; grade the *comprehension*, not
  the phrasing.
- The user can bail any time ("that's enough") → jump to Step 4 with
  whatever was covered.

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
  from the fast speaker), name it — that's actionable.
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
| `query confirm-queue` → `[]` or no items from this episode | exposures haven't cleared the bar (or not yet marked watched) | fine — run the conversation, skip the ledger writes |
| no queue.db | mobile layer unused here | direct mode: ask for the episode id, read from `<work_dir>/episodes/` |
| episode watched long ago, user remembers little | memory test, not comprehension test | say so, shorten the spine to gist + focal vocab; grade confirm items normally (knowing a word survives forgetting the plot) |

## Notes

- Re-running /debrief on the same episode is safe: confirm/defer are
  append-only evidence and the projection recomputes; a second pass just
  re-tests. Don't re-confirm items already promoted — the queue query
  naturally drops them.
- This skill never writes taste data. The post-watch survey (SURVEY.md) stays
  the phone's job; if the test contradicts the user's difficulty rating
  that's interesting conversation material, not a ledger write.
