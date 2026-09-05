# Live review — retiring Anki, reviewing inside immersion time

Design proposal, 2026-09-04 (revised same day after review). Status: the
**painter is built** (§6 six colours on every surface, ★ global and
offline-safe via the phone's mark journal, the should-know list at 100 —
build-order steps 1, 2 and 4 minus the alias table); the exposure fixes
(§4), mark semantics (§5) and migration (§7) are still proposals.
Companion to DESIGN.md (ledger), SURVEY.md (post-watch), GRAMMAR.md (item
kinds).

## 0. Principles (fixed by the user, 2026-09-04)

- **Nothing outside the watch.** No post-watch recall, no review screen,
  no scheduled anything. If an idea adds a step after or between episodes,
  it is out.
- **One popup, one interaction.** Every word, whatever list it is on, opens
  the same card: word, reading, definition, the same mark cycle. No hidden
  glosses, no "did you know it?" variant. You cannot judge "known" without
  the definition in front of you.
- **False "known" is cheap.** Marking a word known that you don't know is
  self-healing: next time you meet it and don't know it, ★ puts it back in
  the pipeline. So the system should never guard against that mistake at
  the cost of friction.
- **Rare words wait.** A ★ word that doesn't recur stays ★ until it does.
  No orphan rescue, no micro-deck.
- **Everything is in the immersion.** Natives and many successful learners
  never use SRS. The job is to make each encounter count for more, not to
  add encounters outside content.

## 1. The idea

Three global word lists drive the paint, and words graduate between them
by set subtraction on the ledger. The only explicit signal is the tap you
already make.

| List | Meaning | Enters by | Leaves by |
|---|---|---|---|
| **High interest** ★ | "I want to learn this" | ★ tap on any surface | promoted to think-you-know, or ✓ |
| **Think you know** | exposures cleared the bar | ledger `promote` (θ qualifying exposures, spread k) | ✓ → known · ★ → back to interest, snoozed · silence → see §5b |
| **Should know** | the 100 most frequent words not yet known | rolling window over the corpus freq table, minus the other lists | promoted to think-you-know, ★'d into interest, or ✓ |

Plus the episode-local paint that already exists (unknown / i+1 target /
curated keyword), reduced to fewer hues — §6.

## 2. What the ledger says today (read-only queries, 2026-09-04)

| Fact | Value |
|---|---|
| words known / learning / unknown | 3,735 / 660 / 18,144 |
| known within corpus rank ≤ 2000 | 1,468 of 2,000 |
| think-you-know queue (words) | 51 |
| high-interest, still unknown | 192 (of 198 ever starred) |
| starred words never met in a second episode | 66 (one third) |
| "learning" words that are learning *only* because an Anki card exists | ~600 of 660 |
| confirm answers so far | 149 "I know it", 114 "not yet" |
| watched exposures of top-2000 words that qualify toward θ | **16 %** (5,391 of 33,726) |

**The think-you-know pipe is starved.** A word exposure qualifies toward θ
only when its best sentence in the episode has *zero* other unknowns
(`_exposure_qualifies`; word exposures never carry a `classification`, so
the classification fallback applies to phrases and grammar only —
engine/lemma.py:394). At ~54 % coverage that is rare: くれる, corpus rank 7,
has 38 watched exposures since 2026-07-08 and not one qualifies, so it sits
"learning" behind a single "not yet". θ=2 for top-2000 words is
effectively θ≈12 episodes. With taps as the only explicit signal, this
pipe is the whole engine — §4 fixes it.

## 3. List definitions (server)

All three are ledger reads narrowed per episode by the existing
`GET /episodes/{id}/paint`; the phone gets a fourth field.

```
known         = lemmas.status='known'                                   (unchanged)
think_you_know= lemmas.confirm_candidate=1                              (unchanged)
interest      = distinct tap_interest − known − think_you_know          (was: − known only)
should_know   = first N of freq ORDER BY rank
                WHERE lemma ∉ known ∪ think_you_know ∪ interest
                  AND pos ∉ {感動詞}            -- ハハハ / ああっ / フフッ
                  AND not laughter/filler regex  -- ^[ハフへ]+ッ?$, う~ん, ちょっ
                  AND rank source = show_graph   -- not the Leeds fallback rows
                N = 100 (config: should_know_window) — refills as words leave
```

Graduation falls out of the subtraction: a ★ word whose exposures clear θ
becomes a confirm candidate, which removes it from `interest`; a should-know
word the same way. A ★ on a blue word clears `confirm_candidate` (it is the
"not yet" answer) and the word is back in interest. ✓ removes it from all.

**Lemma-form trap.** The freq table keys Sudachi dictionary forms, so the
should-know window will surface 信ずる (rank 163; 信じる is rank 934),
いける (59) beside 行ける (337), 取れる, 会える, 思える — potential and
classical forms as separate lemmas. The user has deferred いける four
times and 取れる three, which reads as "I know this, the word is wrong" not
"not yet". Before shipping should-know, fold potential/classical variants
onto their base verb via Sudachi `normalized_form` + a small alias table
(`ledger/lemma_alias.json`), applied when building the window and when a
tap lands. Otherwise the list is 30 % noise on day one.

## 4. Fixing the starved pipe

Two changes to exposure recording / `_judge`, independent of the lists:

1. **Record the classification on word exposures** (one line in
   engine/lemma.py) so `_exposure_qualifies` can use it, as it already does
   for phrases and grammar.
2. **Count attended exposures.** A watched exposure where the word was
   *painted* (interest / should-know / think-you-know at prep time) counts
   toward θ with other_unknown ≤ 2, not 0. The paint is the noticing the
   incidental-learning literature says separates encounters that teach from
   encounters that don't (Uchihara, Webb & Yanagisawa 2019: repetition
   effect r = .34, moderated by engagement and visual support). The server
   already computes the lists when it serves the sidecar; persist that set
   in `coverage.json` as `painted_at_prep` and stamp `"painted": true` on
   the exposure context.

With both, θ for a top-2000 word is again ≈ 2–4 episodes rather than ≈ 12.

## 5. Making each encounter count (all inside the player)

**a. The popup is the review.** Unchanged card: word, reading, definition,
line grammar, mark cycle ✓ → ★ → ✗ → clear (✗ "unknown" added 2026-09-05).
What changes is what the marks *mean* per list, server-side, with no UI
difference:

| tapped | on a plain word | on ★ | on blue (think-you-know) | on green (should-know) |
|---|---|---|---|---|
| ✓ | tap_known → known | tap_known → known | confirm_known → known | tap_known → known |
| ★ | tap_interest → interest | (cycle → ✗) | confirm_defer + tap_interest → interest, snoozed | tap_interest → interest |
| ★ on a **known** word | tap_interest newer than last positive → learning (the lapse signal; today a no-op) |
| ✗ (any word, **built**) | tap_unknown → out of known; nothing else is special-cased: `promote` re-judges at once, so if its qualifying exposures already clear θ it is blue immediately, and if it sits in the frequency window it is green immediately (`should_know` reads status ≠ known). On the phone the ✗ paints the unknown wash on the spot, `/paint` ships an `unknown` list so the sidecar's frozen `k` is undone on every surface, and the surfaces re-pull `/paint` after the batch lands so the blue/green shows in the same sitting. |

The Progress tab's confirm list stays as a batch surface but is no longer
the primary path.

**b. No self-promotion.** A blue word stays blue until you ✓ it. Nothing
becomes known without a manual mark (user rule, 2026-09-04): the ledger
may *suggest* (blue), never *decide*.

**c. Inline glance-gloss for ★ and green words (toggle).** For words on
the two "you want / should learn this" lists only — a handful per episode —
show a one-word English gloss as a second ruby line under the word on the
subtitle. No tap, no popup, no flow change; the meaning arrives with the
encounter. This is the glossing effect (Yanagisawa, Webb & Uchihara 2019
meta-analysis: glossed input outperforms unglossed for incidental
learning, L1 glosses best). Off for blue words (you are meant to test
yourself there) and for plain unknowns (too many). Gloss source: the
curate pass's gloss if present, else first JMdict sense, ≤ 12 chars.

**d. Content as the scheduler.** `/recommend` and the queue view get a
*due-word density* sort for prepared episodes: count of interest +
should-know lemmas in the transcript not exposed in ≥ 14 days. Series
ingest already helps — same-series vocabulary recurs (narrow reading).
This is the only "spacing" mechanism, and it costs no interaction.

**e. Rare ★ words** stay ★ until they recur. The paint guarantees you
notice them when they do. Nothing else.

What is genuinely traded away: rank > 10k words will be acquired slower
than with cards (Webb 2007 puts full receptive knowledge at 10+ contextual
encounters), and ledger confidence gets noisier because taps replace
interval evidence. Both accepted (§0).

## 6. Colours — collapse the warm family

Over video today there are five warm hues on a black outline:

| class | hex | meaning |
|---|---|---|
| `kw` | #ff9f2e orange | curated keyword (has a gloss) |
| `hl-hv` | #ff6b52 coral, bold | high-value candidate |
| `hl-target` | #ff6b52 coral, bold, underline | sole unknown on an i+1 line |
| `hl-unk` | #e8907e dim coral | any other unknown |
| `hl-lrn` | #ffc94d amber | reinforcement (word on a young Anki card) |

Orange, coral and dim coral are not distinguishable at subtitle size, and
`hl-lrn` dies with Anki. Proposed set — one hue per *list*, weight for
episode-local emphasis:

| paint | hue | rule |
|---|---|---|
| known | white | absence is the signal (unchanged) |
| unknown | coral #ff6b52 | normal weight; **bold** = high-value candidate; bold + underline = i+1 target |
| curated keyword | amber #ffc94d | reuses the freed reinforcement hue; farther from coral than orange was |
| high interest ★ | violet #c58fff | unchanged; painted from the global list, not the local tap store |
| think you know | blue #6fd0ff | unchanged |
| should know | green #7ee787 | new |
| corpus-tracked | pale #b9cfe8 | audit tier only, unchanged |

Precedence in `tokenHighlight`: keyword › interest › think-you-know ›
should-know › target › unknown › corpus. Global lists outrank local
arithmetic, as `hl-know` already does.

## 7. Migration off Anki

1. ~~Final Anki scan; write every lemma whose highest interval ≥ 21 d as
   `import` evidence (origin `anki_final`). Ledger-only known set from then
   on; `materialize_known` stops calling AnkiConnect.~~ — done 2026-09-05
   (`ledgerctl import-anki`; no pipeline step or skill preflight touches
   Anki any more).
2. Drop `mined_card` from the promote rule order → the ~600 card-only
   "learning" words become plain unknown/exposure-driven, and the
   should-know window fills from them first (they are the most frequent
   unknowns by construction).
3. `tools/deck.py`, `tools/select.py` and the /immerse deck push become
   opt-in; the curate pass keeps producing glosses (they feed §5c).
4. `cards` table stays as history; `poll_lapses` retired.

## 8. Build order

1. ~~Paint ★ from the global list on all three surfaces~~ — done 2026-09-04
   (`interestFor` + the never-cleared `fp.marks` journal in mobile store.ts,
   so a ★ made offline paints in the next show before it syncs).
2. ~~Colour collapse (§6)~~ — done 2026-09-04; the corpus "all" tier and
   the reinforcement hue are gone.
3. Exposure fixes (§4) — small, and the whole engine depends on them.
4. ~~Should-know list~~ (`ledgerctl.should_know`, on both `/transcript` and
   `/paint`) — done 2026-09-04; the **alias table is still open**, so
   信ずる / いける-type rows will show green until ✓'d.
4b. ~~Review the ★ and should-know lists like Confirm~~ — done 2026-09-04
   (`GET /lists/{interest,should_know}` + `POST /lists/mark`; Progress-tab
   banners → `#/list/…`, the same card as `#/confirm` with ✓ / ★ verbs).
5. Mark semantics per list + lapse rule (§5a), glance-gloss (§5c).
6. Due-word sort (§5d).
7. Migration (§7) once the confirm queue is visibly moving.

## Sources

- Uchihara, Webb & Yanagisawa (2019), *The Effects of Repetition on
  Incidental Vocabulary Learning: A Meta-Analysis of Correlational Studies*,
  Language Learning — https://onlinelibrary.wiley.com/doi/abs/10.1111/lang.12343
- Yanagisawa, Webb & Uchihara (2019), *How do different forms of glossing
  contribute to L2 vocabulary learning from reading? A meta-regression
  analysis* — https://takumiuchihara.weebly.com/uploads/1/2/3/7/123756989/yanagisawa-webb-uchihara-2019-glossing_meta-analysis.pdf
- Webb (2007), *The Effects of Repetition on Vocabulary Knowledge* —
  https://www.researchgate.net/publication/31064743_The_Effects_of_Repetition_on_Vocabulary_Knowledge
- Webb (2008), *The effects of context on incidental vocabulary learning* —
  http://www2.hawaii.edu/~readfl/rfl/October2008/webb/webb.html
- Refold, *Sentence Mining* — https://refold.la/roadmap/library/sentence-mining
