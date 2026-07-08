# Grammar & phrase tracking — design

> **Status: BUILT 2026-07-08** (both phases, same session as the design).
> The taxonomy shipped with 474 patterns (quality bar over the ~600 target —
> variants folded into canonical keys, surface collisions disambiguated with
> parenthetical tags: 〜られる（受身）/（可能）/（尊敬）). Deviations from the
> original notes are marked **[as built]** inline; acceptance criteria in §8
> are all covered by tests (`PhraseGrammarTest`, `TestTypedConfirm`, the
> phrase-unit engine tests, and the mobile smoke tests).

Two new *tracked* axes alongside the vocabulary ledger, both riding the
machinery already built for words (exposure → confirm → known; the confirm
queue; `promote`):

1. **Phrases / MWEs** — 気を付ける, 取り返しがつかない as single units, not the
   morpheme dust Sudachi emits (気 | を | 付ける).
2. **Grammar points** — 〜させられる, 〜てしまう, conditionals, keigo — the patterns
   in a line, plus the word-form structure Sudachi discards (食べさせられた =
   causative-passive of 食べる).

Decisions taken 2026-07-08: both are **tracked** (first-class ledger items,
not just reference annotations).

## Principle — one ledger, three item kinds

The ledger stays the spine; we generalize its *key* from "lemma" to
`(kind, key)` with `kind ∈ {word, phrase, grammar}`. Evidence, `promote`, the
confirm queue, the exposure gate, even card minting are reused unchanged — a
phrase is "known" exactly the way a word is. This is the whole reason the two
prior decisions are cheap: we are not building a second engine, we are widening
the one we have.

Determinism is the hard constraint (as with tokenization): a tracked item needs
a **stable key that joins across episodes**. Free-text LLM output can't be the
key. Each axis resolves this differently below.

### One database (2026-07-08)

All authoritative persistent state lives in the **single ledger DB** — grammar
points, phrases, and word evidence join the lemmas/evidence/taste tables already
there; nothing here adds a new file. The same principle folds the two remaining
state files into the ledger DB: the **job queue** (`queue.db`) and the
**discovery pool** (`discover.db`) become table groups in it (queue rows carry a
`state`/`kind`; discovery keeps its own tables but in the one file). The only
thing that stays a *separate, droppable* file is **`jmdict.db`** — it is
rebuildable reference data (regenerated from `JMdict_e` in one command, ~40 MB),
not user state; putting it in the backed-up ledger would 40× the daily
`VACUUM INTO` snapshot for zero data-loss benefit. It can be `ATTACH`-ed to the
ledger connection for cross-DB lookups if a single *connection* is wanted. (The
external corpora `phrases-full.db` / `japaneseShowGraph.db` are read-only source
data outside the project and are never merged.)

## Phrases — JMdict *is* the phrase dictionary

The key insight: the idioms Sudachi shatters are **already JMdict headwords**
(気を付ける, 取り返しがつかない, 好きになる, 仕方がない … all present). So:

- **Detection.** Longest-match scan of each sentence's surface against JMdict
  headwords yields phrase *candidates* deterministically (no LLM needed for the
  dictionary cases). The curate LLM's job is only to (a) confirm a candidate is
  really the idiom here vs. a coincidental span, and (b) surface non-JMdict
  phrases as *proposed* phrase items (reviewed before they become keys — never
  silent-created, so the key space can't fragment).
- **Key.** The JMdict headword string. Stable, joins cleanly, and carries a
  definition for free.
- **Storage.** `lemmas.kind = 'phrase'`. Everything else — reading, freq,
  status, confirm, even a mined card — works as-is. Phrase freq ranks can come
  from `phrases-full.db` (the phrase corpus already exists — P1's mode-C
  re-parse would align it).
- **Ledger interaction.** A phrase and its component words coexist: 気を付ける is
  tracked *and* 気/付ける stay tracked. i+1 gains a phrase dimension ("this line
  is one known phrase away").

Phrases need **no new taxonomy** and reuse the vocab tables — so they ship
first, independent of the grammar work.

## Grammar — a taxonomy is the gating dependency

JMdict doesn't cover grammar patterns, so grammar needs its own canonical
inventory. Without a fixed taxonomy, the LLM's descriptions of the same pattern
won't join across episodes — the tokenization-nondeterminism problem, one level
up.

- **Taxonomy.** A fixed table of grammar points: `id`, `pattern` (〜てしまう),
  `level` (JLPT/difficulty tier), `gloss`. The pattern surface forms are facts,
  not copyrightable; glosses/examples can be LLM-generated. **Source is the one
  open decision** — see below.
- **Classification.** During curate the LLM tags each sentence with the grammar
  point ids it uses, constrained to the taxonomy. An unrecognized pattern does
  **not** create a point silently — it goes to a *proposed grammar point* review
  (grows the taxonomy deliberately, mirroring the confirm-queue philosophy).
- **Word-form labels.** The conjugation structure (食べる→食べさせる→食べさせられる)
  rides along as an annotation on the grammar tag / card — reference, since the
  *tracked* unit is the grammar point, not each inflection.
- **Storage.** `grammar_points` table (the taxonomy + projection), keyed by
  `pattern`. Grammar evidence is ordinary `evidence` rows with `kind='grammar'`
  and the pattern in `lemma` (the item key always lives in `lemma`; see
  *Implementation notes §2*). `promote` runs the same state machine over grammar
  evidence, with `level` as the difficulty prior in place of corpus freq. A
  grammar confirm-queue falls out for free.

## Schema

The authoritative DDL — `kind` on `lemmas`/`evidence`, `grammar_points` keyed by
`pattern`, the recreated idempotency index — is in **Implementation notes §2**.
Summary: one key column (`evidence.lemma`) for every item; `evidence.kind`
selects the projection table (`lemmas` for word/phrase, `grammar_points` for
grammar). No polymorphic id column.

## Production path (where the data comes from)

`/immerse` curate already reads the full transcript and writes `curate.json`.
It gains two emissions per episode **[as built — exact shapes]**:

- `phrases`: `[{sentence_idx, surface, canonical, classification}]` —
  confirmed phrase units, canonical = JMdict dictionary form.
- `grammar`: `[{sentence_idx, pattern | proposed_pattern, classification,
  form_note, example?, gloss?}]`.

The recorder is `ledgerctl.record_curate_items`, folded into the existing
`record-curation` CLI verb (one curate.json → taste metadata + phrase/grammar
evidence + promote, one call). It lands these as inert exposures, activated on
watch by the same gate. **Division of labor with Stage 1 [as built]:** curate
introduces *new* keys; coverage deterministically re-detects *already-tracked*
phrases every episode (`KnownSet.phrase_units`) and writes their exposures
itself — so tracked phrases keep accruing evidence even on episodes never
curated. No new sync surface on the phone — the confirm queue already renders
any candidate; it just grows to cover `kind='phrase'` and grammar rows.

## Build order

1. **Phrases** — JMdict longest-match + `kind='phrase'` + curate confirm.
   No taxonomy dependency; reuses the vocab flow end to end. Ship first.
2. **Grammar** — after the taxonomy is chosen: `grammar_points` table + seed,
   evidence/promote extension, curate classification, proposed-point review.

## Grammar taxonomy source — resolved 2026-07-08

**LLM-authored once, then classify-into (never generate per-episode).** The LLM
(this session) enumerates the standard grammar inventory a single time — JLPT
N5→N1, `pattern` + `level` + `gloss` rows — into `grammar_points`, reviewed
by the user. **[as built]** `ledger/grammar_taxonomy.json`, 474 rows
(N5 64 · N4 78 · N3 68 · N2 130 · N1 134), loaded with `ledgerctl
grammar-seed` (upserts level/gloss, never touches promote's verdict columns —
re-seeding a revised taxonomy is safe). Proposals are approved with
`ledgerctl grammar-approve PATTERN [--level N] [--gloss …]` and inspected with
`ledgerctl query grammar-proposed`. That fixed table is the canonical spine: it gives stable ids and a
`level` difficulty prior (the θ analogue for grammar-i+1) with no external list
or licensing. Thereafter, per-episode curation only **matches** usages into the
existing table or **proposes** a genuinely novel pattern (colloquial/dialectal)
to the same review gate the confirm queue uses — nothing becomes a tracked key
silently. The discipline that matters: authored *once* (stable), not regenerated
each episode (which would fragment 〜てしまう into synonyms that never join).

(Decided alongside: `jmdict.db` stays a separate rebuildable cache — see *One
database* above.)

---

# Implementation notes (handoff)

Written for an agent with no prior context. Read `DESIGN.md` (The Ledger) and
`AUDIT.md` (the confirm-known flow this reuses) first. All line references are
to symbols, not line numbers.

## File & symbol map

| Change | File · symbol |
|---|---|
| `kind` column, `grammar_points`, idempotency indexes | `ledger/schema.sql`; migrate in `ledger/ledgerctl.py:_migrate` |
| touch a phrase row | `ledger/ledgerctl.py:_touch_lemma` (add `kind=` param) |
| record phrase/grammar exposures | new `record_grammar_exposure` / phrase path in `record_exposure` (`ledger/ledgerctl.py`) |
| grammar state machine + level θ | `ledger/ledgerctl.py:promote` (second pass), new `GRAMMAR_THETA`/`grammar_theta_for` beside `THETA_TABLE`/`theta_for` |
| confirm verbs for grammar | `confirm_known_lemma`/`defer_known_lemma` → generalize via `_record_confirm`; add grammar equivalents |
| queue read (typed) | `ledger/ledgerctl.py:query_confirm_queue`, `query_summary` |
| phrase detection | `tools/jmdict.py` (add `is_headword`/phrase-key helper), consumed in `tools/coverage.py:run_coverage` |
| i+1 with phrases | `engine/lemma.py:analyze_sentence` (+ `KnownSet`) |
| endpoints | `server/app.py` `get_confirm`/`post_confirm`/`get_stats` |
| curate emissions | `skills/immerse/SKILL.md` (writes `curate.json`); consumed by the recorder |
| app | `mobile/src/types.ts` (`ConfirmCandidate`, `Stats`), `src/api.ts` (`confirmWord`), `src/views/confirm.ts`, `src/views/stats.ts` |

## 1. Phrase detection rule (the crux)

A JMdict headword is a phrase candidate **only if the canonical form itself
tokenizes to ≥2 Sudachi tokens** — otherwise every ordinary noun (a
single-token headword) becomes a "phrase." **[as built]** The test runs on the
*canonical*, NOT the matched surface span: inflected single verbs split on
their auxiliaries (食べて → 食べ|て = 2 tokens), so a surface-span rule would
admit every te-form verb; 食べる → 1 token rejects it, 気を付ける → 3 accepts.
This also spares the recorder from locating the surface in the sentence at
all. Detection is **LLM-emits, server-validates** (the key is what must be
deterministic, not the detection):

- Curate (`/immerse`) emits, per line,
  `{sentence_idx, surface, canonical, classification}` where `canonical` is
  the intended JMdict dictionary form and `classification` is the sentence's
  coverage classification (the qualifying signal — see §3).
- The recorder (`ledgerctl.record_curate_items`, run by `record-curation`)
  validates: `canonical` **is** a JMdict headword (`jmdict.is_headword`)
  **and** `len(tokenize(canonical)) >= 2`. Fail → returned in
  `phrases.rejected` with a reason, never key-minted; a reviewed non-JMdict
  idiom can be deliberately tracked with `ledgerctl phrase-add` (which still
  enforces the ≥2-token guard).
- Inflection is why raw longest-match is unreliable (sentence says 気を付けて,
  headword is 気を付ける). Letting the LLM return the canonical form and only
  validating that it's a real key sidesteps deinflection entirely.
- **[as built] Already-tracked phrases ALSO match deterministically at
  Stage 1**: `KnownSet.phrase_units` compares each tracked headword's own
  lemma sequence (気|を|付ける) against the sentence tokens' lemmas — an
  inflection-proof match with no deinflector. So coverage re-detects known
  keys every episode (exposure accrual, i+1 units, candidates) with no LLM in
  the loop; curate's job narrows to *new* keys and idiom-vs-coincidence
  judgment.

Store the phrase as a `lemmas` row: `lemma = canonical`, `kind='phrase'`,
`reading` = the JMdict entry's primary reading, `pos='expression'`. Furigana
for display via `engine.lemma.furigana(canonical)` (already kanji-only).
`_touch_lemma`'s upsert never changes an existing row's `kind` (first writer
wins) — a later default-`word` touch of a phrase key can't demote it.

## 2. Schema & idempotency (exact)

The item **key always lives in `evidence.lemma`** (which is `NOT NULL` — a
polymorphic nullable `grammar_id` was rejected for exactly that reason). A new
`evidence.kind` says which projection table the row feeds; for grammar the key
*is* the pattern string. This matches the `(kind, key)` principle at the top and
keeps one code path.

```sql
ALTER TABLE lemmas   ADD COLUMN kind TEXT NOT NULL DEFAULT 'word';  -- word|phrase (projection kind)
ALTER TABLE evidence ADD COLUMN kind TEXT NOT NULL DEFAULT 'word';  -- word|phrase|grammar; key in `lemma`

CREATE TABLE grammar_points (          -- the projection table for kind='grammar'
    pattern TEXT PRIMARY KEY,          -- 〜てしまう  (the join key; = evidence.lemma)
    level INTEGER, gloss TEXT,
    status TEXT NOT NULL DEFAULT 'unknown', confidence REAL NOT NULL DEFAULT 0,
    exposure_count INTEGER NOT NULL DEFAULT 0, episode_spread INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0, confirm_candidate INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT, last_seen TEXT, updated_at TEXT NOT NULL);
CREATE TABLE grammar_proposed (pattern TEXT PRIMARY KEY, example TEXT,
    gloss TEXT,                        -- [as built] the proposer's one-liner
    seen INTEGER NOT NULL DEFAULT 1, first_seen TEXT);
```

Idempotency: the P4 index must include `kind` so a word and a grammar pattern
that happen to share a string can't collide in one episode. Recreate it
(migration-safe — existing rows default to `kind='word'`):

```sql
DROP INDEX IF EXISTS idx_exposure_once;
CREATE UNIQUE INDEX idx_exposure_once
    ON evidence(kind, lemma, episode_id, source) WHERE source='exposure';
```

**[as built] Migration ordering subtlety:** `open_db` executes `schema.sql`
(all `IF NOT EXISTS`) *before* `_migrate`, so on a pre-kind DB the updated
index DDL is a silent no-op (same name, old shape). `_migrate` therefore
detects the old shape via `PRAGMA index_info(idx_exposure_once)` (first
column ≠ `kind`) and drops/recreates it — after the `ALTER TABLE`s. Fresh DBs
get the new shape straight from `schema.sql` and the check no-ops.

No new evidence *sources*: exposures stay `source='exposure'` (now with
`kind`); `confirm_known`/`confirm_defer` already exist and apply to any kind.
`POLARITY`/`WEIGHT` are keyed by `source`, so they're unchanged.

Projection tables by kind: `kind IN ('word','phrase')` → `lemmas`
(`lemmas.kind` records which); `kind='grammar'` → `grammar_points` (keyed by the
pattern string, mirroring `lemmas`' string key). `_touch_lemma` gains a `kind`
param; add a `_touch_grammar(conn, pattern, level, gloss)`. `_record_confirm`
gains a `kind` param and writes it on the evidence row.

Word-form labels (食べる→食べさせる→食べさせられる) live in the grammar exposure's
`context` JSON (`{"form_note": "...", "sentence_idx": n, "classification": ...}`),
surfaced on the confirm card and prep — reference only, not a tracked key.

## 3. `promote` for grammar (second pass)

**[as built]** The rule order (rules 1–5) is extracted into a shared `_judge`
helper; `promote` groups evidence by `(kind, lemma)` and dispatches each group
to its projection table — `lemmas` for word/phrase, `grammar_points` for
grammar. One state machine, two write targets. Grammar rows whose evidence
vanished (episode purge) are healed back to the seeded baseline at the end of
every promote. Differences from the word pass:

- No corpus freq. Threshold from `level` — a plain dict (five levels, 1:1
  lookup; a range-scan table shape bought nothing), with unplaced patterns
  (approved proposals without a tier) getting the strictest bar:

  ```python
  # level 5=N5 (easiest) … 1=N1; grammar_theta_for(None) → GRAMMAR_THETA[1]
  GRAMMAR_THETA = {5: (2, 2), 4: (2, 2), 3: (3, 3), 2: (4, 3), 1: (5, 4)}
  ```
- "Qualifying" exposure analogue: the item was in a sentence the learner
  could parse. `_exposure_qualifies(ctx)` accepts either signal —
  `other_unknown_count == 0` (words, Q1) or classification ∈
  {`comprehensible`,`i_plus_1`,`reinforcement`} (phrases/grammar, recorded in
  the exposure `context`) — so one check serves all three kinds.
- Confirm/defer identical to words: exposures cross θ → `confirm_candidate=1`
  (never auto-known); `confirm_known`→known; `confirm_defer` snoozes until a
  qualifying exposure lands after the defer.

## 4. Confirm queue & endpoints (typed key)

Generalize the item identity to `{kind, key}`:

- `query_confirm_queue` returns a UNION: `lemmas WHERE confirm_candidate=1`
  (kind word|phrase, enriched with JMdict senses as today) **plus**
  `grammar_points WHERE confirm_candidate=1` (kind `grammar`, carrying
  `pattern`/`level`/`gloss`, no reading/senses).
- `POST /confirm` body becomes `{kind, key, known}` (`key` = the lemma string,
  the phrase headword, or the grammar pattern). Keep accepting bare
  `{lemma, known}` as `kind='word'` for back-compat. Dispatch on `kind` in
  `_record_confirm` (which now writes `evidence.kind`), then `promote`.
- App `ConfirmCandidate` gains `kind` and (for grammar) `pattern`/`level`/`gloss`;
  `src/views/confirm.ts` renders a grammar card (pattern + level badge + gloss,
  no ruby); `api.confirmWord(kind, key, known)`.

## 5. `/stats` and `kind`

Keep the headline `known` and freq-band math **words-only** so their meaning and
the corpus-rank join are unchanged: add `WHERE kind='word'` to the known/status/
freq queries in `query_summary` and `get_stats`. Add sibling counts:
`phrases_known`, `phrases_confirm_candidates`, `grammar_known`,
`grammar_confirm_candidates`. App Progress tab: a phrases tile and a grammar
tile/section; the confirm banner counts all three kinds.

## 6. coverage / i+1 with phrases

After phrase detection for a sentence, count unknowns over **units**, not
tokens: a detected phrase is one unit whose known-ness is its `lemmas` status;
its component tokens are removed from the unknown tally when the phrase is the
unit in play. A sentence stays `i_plus_1` when exactly one unknown *unit* (word
or phrase) remains. **[as built]** `KnownSet` gains a `phrases` dict
({headword: status}, from `materialize_known`) and `phrase_units()`;
`analyze_sentence`'s `unknown_lemmas`/`unknown_count`/`classification` are
unit-level (phrase keys included), while the token-level fields (`tokens`,
`unknown`, `known_ratio`) keep their word meaning — the wire shapes stay
stable. Word-level tracking coexists: component words still accrue their own
exposures inside a phrase. Tracked-but-unknown phrases enter the candidate
pool (`coverage.json` `candidates`, `kind='phrase'`) like lemmas, so
`tools/select.py`/`tools/deck.py` can mint a phrase card unchanged (audio is
the sentence span regardless); `record_mined_cards` passes an optional `kind`
through to the evidence row. Phrase keys are excluded from `materialize_known`'s
token known-set and stem bridging (a phrase's kanji stem would contaminate
stem matching).

## 7. queue + discover → ledger (separate phase)

Point `server/jobqueue.py:open_queue` (and the discovery store) at
`cfg["ledger_db"]`; create their tables there via schema/migration, namespaced
if needed (`jobs` is already unambiguous; prefix discovery tables `discover_`).
Queue state is transient — migrate by draining first, then starting fresh is
acceptable; the discovery pool should be copied row-for-row. `tools/backup_ledger.sh`
then covers them for free. Sequence this *after* phrases; it's orthogonal to the
feature and shouldn't block it.

## 8. Acceptance criteria (per phase) — all ✅ 2026-07-08

**Phase 1 — phrases** (`tests/test_ledger.py::PhraseGrammarTest`,
`test_server.py::TestTypedConfirm`, `test_engine.py::LemmaTest` phrase-unit
tests, `mobile/src/smoke.test.ts`):
- ✅ `is_headword` true for 気を付ける, false for a non-key; the recorder
  rejects single-token canonicals (犬) and non-headwords, with reasons.
- ✅ A curate emission for "気を付けて…" yields a phrase exposure keyed
  気を付ける; re-running the recorder is idempotent (P4, kind-aware index).
- ✅ Phrase rides exposure→confirm→known; appears in `query_confirm_queue` with
  `kind='phrase'` and JMdict senses; `POST /confirm {kind:'phrase',...}` promotes.
- ✅ `/stats` reports `known` unchanged (words only) plus `phrases_known`.
- ✅ App confirm queue renders a phrase card (badge + senses); Progress shows
  the phrases tile.

**Phase 2 — grammar**:
- ✅ Taxonomy seed loads (474 `grammar_points`); `grammar_theta_for(level)`
  matches `GRAMMAR_THETA`, `None` → strictest.
- ✅ Grammar exposure (qualifying-classification only; `too_hard` doesn't
  count) → `confirm_candidate` at θ, never auto-known; confirm/defer/snooze
  mirror the word tests.
- ✅ Unrecognized pattern lands in `grammar_proposed` (no evidence row);
  `grammar-approve` moves it into the taxonomy, after which recording works.
- ✅ Confirm queue + `/stats` + app render grammar items (typed key, JLPT
  level badge, taxonomy gloss).
- ✅ A word and a grammar pattern sharing a string never merge (kind-scoped
  evidence, groups, and idempotency index).

Remaining definition-of-done (needs a real episode, not a test): `/immerse`
emits the new `curate.json` blocks on the next curation pass, and the confirm
queue shows typed items on the phone once something crosses θ. The live ledger
is migrated and seeded (474 patterns, word counts untouched).
