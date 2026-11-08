# Curate-agent briefing (template — /recommend Step 7)

The /recommend orchestrator copies this file into its scratchpad as BRIEFING.md, replacing
`<SCRATCH>` with that scratchpad's absolute path, and points each curate agent at it. Edit this
template (not the copies) when a batch teaches a new hazard.

You are curating ONE YouTube episode for the fullPipe immersion pipeline, as Stage 2 of the `/immerse`
skill. Your episode id, title, channel and runtime are in your prompt. Several other agents are doing
sibling episodes concurrently — stay inside your own episode's files.

## Paths / conventions
- FULLPIPE = /Users/andrew/Documents/code/anki/fullPipe — run EVERY command from there.
- PY = /Users/andrew/Documents/code/anki/fullPipe/.venv/bin/python (python 3.12; never another venv).
- Episode dir = /Users/andrew/immersion/episodes/<EPISODE_ID>/ (transcript.json, sentences.srt,
  coverage.json, words.json, video.mp4 exist). Never touch video.mp4 or anything under
  ~/immersion/downloads. ~/immersion is user data — write only the artifacts named below in your
  episode dir. Never write anywhere under /Volumes.
- Temp files: ONLY in <SCRATCH>/<EPISODE_ID>/
  (mkdir it). Sibling agents share the scratchpad root — a plain-named file there WILL be clobbered.
- The full skill text is at FULLPIPE/.claude/skills/immerse/SKILL.md — read Steps 2.5, 2.6, 3, 4, 6
  before starting. This briefing overrides it where they differ. Do all gate work INLINE yourself
  (do NOT spawn helper subagents — the concurrent-agent limit is shared with siblings).
- The job is already in state `curating` (claimed by the orchestrator). Finish by setting it `staged`.
- Transcript source: YouTube `ja-orig` auto-captions (ASR) or creator subs, or GPU whisper if neither
  existed — check `transcript.json → episode` for which. Auto-subs have NO punctuation and long
  run-ons: the punctuation gate is load-bearing. Expect ASR homophone misreads and garbled names.

## Step A — punctuation gate (inline)
```
$PY -m tools.punctuate check <EPISODE_ID>     # prints <ep_dir>/punct_blocks.json
```
If it prints NOTHING but the transcript is still run-ons (sentences > ~60 chars / > ~12 s with more
than one utterance), that is the known checker false-pass (hazard 1): export the blocks yourself —
`punct_blocks.json` is just `[{"idx","text"}]` over the cleaned raw SRT blocks in order (read
`episode.raw_srt`; `tools/punctuate.py` shows how check builds it — reuse its function) — and run the
gate anyway; say so in the report.
Copy punct_blocks.json into your scratchpad dir first (apply deletes it; a 2nd apply re-reads the raw
subs so you can re-apply from a revised punct_out.json without re-exporting).
Write `<ep_dir>/punct_out.json`: same length, same idx order, each `text` with 。？！ inserted. ONLY
insert 。？！ (no commas, no rewrites — non-punctuation edits are discarded by the diff anyway).
- Mark at EVERY speaker change inside a block; interjections (ああ / うん / はい / えっ) are their own
  sentences; 。 after a trailing "…", after laughter / song lines; a block ending mid-sentence gets no
  mark; a block spanning a boundary gets the mark mid-text.
Then `$PY -m tools.punctuate apply <EPISODE_ID> <ep_dir>/punct_out.json`.
VERIFY: print spans of transcript.json sentences; anything > ~12 s holding more than one utterance,
or a sentence > ~60 chars, is a missed boundary — revise punct_out.json and re-apply (prior batches
needed 1–3 passes). Known: `apply` re-segments at ASCII decimal points ("1.5倍", "0.5g", URLs) — if a
number you'd card got split, hand-merge those two sentences in transcript.json + sentences.srt
consistently (text, start/end) and re-run coverage; otherwise accept it. Cosmetic splits at ？ inside
「…？」 and at closing 」 — ignore. Report the number of passes.

## Step B — repair gate (inline, ALWAYS after punctuation)
```
$PY -m tools.coverage <EPISODE_ID>            # refresh first so `suspects` are post-gate
$PY -m tools.repair check <EPISODE_ID>        # prints <ep_dir>/repair_blocks.json
```
Read sentences + suspects, write `<ep_dir>/repair_out.json`
`{"edits":[{"idx","old","new","why"}], "names":[{"surface","kind","note"}], "nonwords":["…"]}`.
- edits = LOCAL homophone/garble fixes where context makes the intended text certain. `old` verbatim
  inside sentence `idx`; never merge/split/reorder sentences; never edit on a guess (an unfixed error
  is recoverable, a wrong fix is not). Creator-sub / ASR speaker labels pasted mid-sentence
  (GOLDNRUSH's 深井/樋口/ヤンヤン-style name tags, single-kana tags) → delete as edits — expect hundreds
  on a podcast; a small script to generate those edits is fine.
- names: ONLY true proper nouns that are NOT ordinary words in any other context. The list feeds a
  cross-episode `non_vocab` registry that HIDES the key from every future episode's coverage, so a
  real word mis-flagged as a name disappears from the user's learning forever. Never flag ordinary
  words that happen to be names/titles/places/brands JMdict has as vocabulary (東, 森, 丸, 巨人, 和尚,
  神父, 大将, ネガティブ, イオン …) — give those a `defs` entry instead. Names already in the registry
  from other episodes still show in `tools.jmdict missing` for YOUR episode unless YOUR repair.json
  flags them — re-flagging a presenter is expected.
- `$PY -m tools.repair apply <EPISODE_ID> <ep_dir>/repair_out.json`. It reports rejected edits; relay
  any real fix the guardrail blocked. HAZARD: a SECOND apply overwrites repair.json wholesale (wipes
  the first pass's names/nonwords) — if you must re-apply, include the full merged lists.
- If you discover after apply that you flagged a real word: `$PY -m ledger.ledgerctl non-vocab-remove KEY`.
Then `$PY -m tools.coverage <EPISODE_ID>` again (final). Relay its stats line in your report.

## Clip spans (before choosing picks)
Every card sentence must span 1.5–15 s AND be plausible for its text (~4–8 chars/s). YouTube auto-sub
timing is usually fine, but a sentence followed by a pause/music can carry an inflated `end`. For a
sentence you intend to pick, you MAY tighten its `end` (never move `start` earlier than the real
speech) in BOTH transcript.json and sentences.srt consistently, then re-run coverage.
`<ep_dir>/words.json` has word timestamps — use it to find where the last word actually ends.

## Step C — curate (your real job)
Read coverage.json (`stats`, `candidates` — each has lemma/reading/pos/freq_rank/recurrence/best) and
read the WHOLE transcript, start to finish. Then write `<ep_dir>/curate.json` exactly per SKILL.md
Step 3: synopsis (2–4 English sentences, orient, no spoilers), genre + format (categorical, reuse
labels: genre interview|podcast|talk-radio|comedy|explainer|lecture|audio-drama|gameplay|news…;
format talking-head|podcast|live-action|radio|animation|slideshow|audio-only…), topics (2–5 tags),
difficulty_felt 1–5, keywords (gloss EVERY candidate, ≤4-word episode sense + Japanese usage `note`;
also hand-rescue thematic words the 50-cap buried — `candidates_dropped_by_cap` is 70–470 here),
focal_points 3–7 with one-line why, weighting, picks_rationale, exclude (non-words/misparses/junk),
phrases (real idioms the tokenizer shattered, canonical = JMdict headword; gloss+reading if JMdict
lacks it), grammar (form_note ONLY where it earns its place — plain English, "what the speaker is
doing", never a linguistics term; canonical pattern keys from `grammar_points`), defs (every row of
`$PY -m tools.jmdict missing <EPISODE_ID>` a viewer would tap: words JMdict lacks, people/places/shows
with who/what glosses, the episode's own terms, plus a def for any keyword whose JMdict first sense
misleads here).
Completeness: len(keywords ∪ exclude) ≥ len(candidates); every `jmdict missing` row def'd, excluded,
or consciously skipped (bare numbers, ASR garbage). Note `jmdict missing` does not filter by your
own curate.json defs, and lowercase latin names (akb, fj) still show after a name flag — def them.
Stage-1 grammar detector misfires seen before (handle with a form_note only, never "fix" the
detector): ちゃ = ては tagged 〜てしまう; ん-for-る / 〜ん (行けん) tagged classical ぬ; plain やる tagged
〜てやる; musing な (思い出すな), んじゃないですか, わけじゃないですか tagged prohibition; なるべく → 〜べく;
無茶振り → 〜ふりをする; むずい → 〜ずに; ちゃんと → ちゃん; なんで → 〜ので; 伸びしろ → 〜ろ; 上がらん → ぬ.
Genuinely novel/dialectal patterns may go in as `proposed_pattern` entries with a tested `match`
spec ($PY -m tools.grammar try "<line>").

Then `$PY -m ledger.ledgerctl record-curation <EPISODE_ID> <ep_dir>/curate.json`. Fix every
`not_a_jmdict_headword` reject by adding gloss+reading to that phrase and re-running. Relay
`grammar.proposed` and any reject you left. ("grammar recorded 0" is a known stale-context quirk.)
HAZARD 9: record-curation re-runs coverage and recorded phrases can surface as NEW candidates —
re-read `candidates` after it and gloss/exclude any newcomer (re-run record-curation if you edited).

## Step D — picks.json (the card pool)
`<ep_dir>/picks.json`, ordered by your preference, each entry: lemma, sentence_idx, reading,
sentence_furigana (Anki syntax, readings YOU resolve from context, ASCII space before each kanji run,
okurigana outside the brackets, everything outside brackets reproduces the sentence char-for-char),
english (natural full-sentence translation), notes (1–3 sentences on the target word AS USED HERE —
sense, register, collocation, the pattern it sits in; say something the gloss doesn't), context (1–2
sentences: who is speaking to whom about what — enough to make sense months later).
ALL FOUR of english/notes/context/sentence_furigana are mandatory on every entry (deck drops entries
missing them).
Selection bar — no cap, no quota, zero is fine: complete merged sentence (no fragments/trail-offs/
interjection-only); STRICT i+1 (`other_unknown_count == 0` for the chosen sentence); not both
freq_rank null AND recurrence 1; target word correctly transcribed in the intended sense; span
1.5–15 s and plausible; clean single-speaker audio (no crosstalk, no song). Smell test: the user's
reality is ~1 excellent card per 10 min at best; a denser pool almost certainly has padding —
recheck. You may pick a different sentence_idx than `best`. Candidates already exclude
learning/carded lemmas.

Mandatory pick verification BEFORE set-state staged (from FULLPIPE; needs the desktop GPU):
```
$PY -m tools.verify_picks <EPISODE_ID>
```
It cuts every pick's exact span + a ±6 s window, re-transcribes both on the GPU service, prints
OK/BAD with the heard text and a proposed tightened span. If any pick is BAD and the proposal scores
≥ 0.6, re-run with `--apply` (rewrites transcript.json + sentences.srt for just those sentences and
re-runs coverage); if the proposal is also bad, drop that pick. If the GPU service is unreachable,
say so and continue (the deck's own audio gate fails open). Report the result.

## Step E — presenter fingerprint (SKILL.md Step 3 presenter steps)
Do the presenter step yourself per SKILL.md (a sibling may share your channel, so make it LATE):
presenter-get → merge your observation → presenter-set with `--episode <EPISODE_ID>`, list every
episode in `provenance.episodes`, fold any `provenance.folded` backlog. Do it as one LATE step.

## Step F — render + close
```
$PY -m tools.render <EPISODE_ID>
$PY -m server.jobqueue set-state <EPISODE_ID> staged
```
Do NOT run tools.deck (queue path: cards push when the user marks it watched).

## Report back (≤ 250 words, one block)
episode id · comprehensibility % + class counts (final coverage) · punct passes (and whether check
false-passed) · repair: edit count + 2–3 notable fixes, names flagged / nonwords count, anything
pruned · focal points (word + why, terse) · pool: count + each lemma with its sentence idx ·
verify_picks result · grammar proposed / phrase rejects · presenter merged ·
any hazard hit or anything the user should know. No file dumps.

## Standing addendum
- Zsh: an unquoted `$var` holding several words is NOT split — use arrays or `set -o shwordsplit`;
  give ffmpeg `-n` (never overwrite) on any cut. Never write under ~/immersion/downloads or /Volumes.
- Picks that rest on a repaired target word: keep only if verify_picks hears it. Same reading in a
  different spelling = confirmed, keep; a different sound = drop.
- Names: flag in `names` ONLY if not an ordinary word anywhere. Repairing a guest's full name can
  surface its parts (e.g. 砂鉄 from 武田砂鉄) as candidates — exclude per-episode, don't flag as a name.
- Creator-sub / ASR speaker labels (GOLDNRUSH: 深井/樋口/ヤンヤン-style tags, hundreds) → delete as edits.
- Dubbed/foreign-language stretches: whisper invents Japanese "translations" and
  "ご視聴ありがとうございました" over them — never card those lines; exclude them.
- Your prompt may carry episode-specific notes from the orchestrator — they override this file.

- **Creator captions carry on-screen telop text (2026-10-26, BMSG THE LAST PIECE).** Uploader
  subs on TV/audition shows paste caption-card text into the speech — `[TRAINEE歴 約2年]`,
  `プレゼンス（存在感）` — and a telop spanning two blocks can swallow a spoken line. Delete the
  telop via repair edits (recover any speech hidden inside it); never card a telop-only sentence.
- **Phone-in and venue audio (2026-10-27).** Children's phone lines (こども電話相談) come out of whisper as
  junk — card only the host/expert side and expect a pile of nonwords for the registry. Reverb-heavy hall
  recordings (詩のボクシング) can lose the first minutes entirely (one 9-min block with 2 lines): report the
  gap, don't splice. University-posted speech subs ran 0.5–2 s late — tighten spans before verify_picks.
  The 〜てやる matcher fires on plain やる — form_note it.
- **Whisper all-hiragana drift (2026-10-31, Koto☆Hana / 窪田等 朗読).** On narration-heavy audio, GPU whisper can emit
  long stretches with no kanji (ごねん, ほんにん) that Sudachi mis-segments (→ごねる, ほんに), skewing coverage. Restore
  kanji only where meaning is verifiable — for 青空文庫 readings, align against the Aozora text (reliable); otherwise leave
  them and never card a kana-garbled line. Dialect (遠野弁): never "repair" dialect into standard; card host/standard lines.
- **ASMR roleplay subs are a script, not a transcript (2026-11-03, BlueKatie).** Creator subs pass the punctuation
  check legitimately but the voice improvises around them — only ~1/3 of lines matched on GPU re-listen (11 of 14
  first picks failed). They also carry the listener's imagined replies as `あなた（…）`, stage directions `（先生に向かって）`
  and IME typos — delete those via repair edits. Whisper may return "no speech detected" on the whisper voice. Run
  verify_picks EARLY and pick replacements from re-heard lines; a small pool is the right outcome.
- **"曲入り" radio re-uploads (2026-11-03, スナック ラジオ).** Full songs left in: whisper can drop a whole song
  (237 s gap, nothing transcribed) or transcribe English lyrics — never card lyrics; report the gap, don't splice.
- **Short repair keys match inside longer names (2026-11-03, 気象通報).** A repair edit on a short
  string (海西→快晴) also rewrote it inside an unrelated longer token (上海西南西 → 上快晴南西). Before
  applying, grep the transcript for every occurrence of each short (≤2-char) key and anchor it with its
  neighbours (海西、 / 海西で) or list the longer name first; re-apply with the full name list, never a
  second partial apply (that wipes the first's record).
- **Leading-dash episode ids (2026-11-03, yt_-LtWqBAw0NU).** YouTube ids can start with `-`; pass them
  after `--` or as `--episode=yt_-…` so the CLI doesn't read a flag.
