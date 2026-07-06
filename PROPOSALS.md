# Proposals — design-review findings, 2026-07-05

Outcome of the DESIGN.md review. Each proposal is one detected problem with a concrete
fix. **Status key:** `implemented` = built and tested (see file) · `accepted` = folded
into DESIGN.md, implement as written when its component is built · `spike` = needs a
short experiment before committing.

| # | Title | Status | Where / when |
|---|---|---|---|
| P1 | Re-parse `phrases-full.db` at SplitMode C | accepted | before corpus-leverage scoring (`coverage` leverage column is NULL until then) |
| P2 | Drop `mature` from the ledger | implemented | `ledger/schema.sql` |
| P3 | `episode_id` as a real evidence column | implemented | `ledger/schema.sql` |
| P4 | Exposure idempotency by unique index | implemented | `ledger/schema.sql` + `record_exposure` (INSERT OR IGNORE) |
| P5 | `mark-watched` verb + apply-taps implication | implemented | `ledger/ledgerctl.py` (`query unwatched` reports inert exposure) |
| P6 | Lapse polling in `apply-taps` | implemented | `ledger/ledgerctl.py` — **amended, see F2 below** |
| P7 | Frequency ranks from `japaneseShowGraph.db` penetration | implemented | `ledger/build_freq.py` (295k lemmas; rank-2000 penetration = 2,413/10,922 shows) |
| P8 | ReazonSpeech word-timestamp spike | spike | before offline ASR path |
| P9 | Prep-doc tap-export robustness | implemented | `render/template.html` (copy + visible textarea + share sheet + localStorage best-effort) |

---

## P1 — Re-parse `phrases-full.db` at SplitMode C

**Problem.** DESIGN.md claimed the scoring corpus aligned with the mode-C tokenizer
standard. It doesn't: the phrase parser defaults to `--mode B`
(`phrases/tools/phraseParser/parse.py:299`), the README's documented invocation never
overrides it, and PLAN.md's iteration notes discuss mode-B segmentation artifacts. Only
`japaneseShowGraph.db` is mode C (`STATE.md:141`). Mode-C known-lemma joins against a
mode-B corpus silently miss on compounds — leverage scores would be quietly wrong, with
no error to notice.

**Proposal.** Re-run the full-corpus parse with `--mode C` into a sibling database:

```sh
python3.12 tools/phraseParser/parse.py \
    japaneseShowGraph/subs/minSubs/ \
    -o japaneseShowGraph/subs/phrases-full-c.db \
    --mode C --resume
```

Keep the mode-B database untouched — the phrases research project's n-gram findings are
built on it and re-deriving them is out of scope. `fullPipe` reads only the mode-C copy.

**Cost.** ~4h wall-clock (matches the recorded mode-B parse), ~30 GB disk. Not a blocker
for the `/immerse` MVP, which needs no corpus leverage; it gates only the
leverage-scoring feature of `coverage`.

**Rejected alternative.** Fuzzy/decomposition matching between mode-C known lemmas and
mode-B tokens — asymmetric (a known compound doesn't imply its parts are known, nor the
reverse), and it puts a permanent correctness asterisk on every score to save one batch
job.

---

## P2 — Drop `mature` from the ledger

**Problem.** The state diagram showed `known → mature`, but no `promote` rule ever
produced `mature`, and its only consumer (the i+1 known-set `{known, mature}`) treated
it identically to `known`. An unreachable state with an indistinguishable consumer is
pure schema noise.

**Proposal.** Three states: `unknown | learning | known` (applied to DESIGN.md).
Anki-side maturity already enters via the live union (`interval ≥ 21d`). Reintroduce a
fourth state only when a consumer actually needs the distinction — the candidate is
REPLACE-mode prioritization ("rehab cards for fragile knowns first"), at which point the
promote rule comes with it (e.g. `episode_spread ≥ 2k`).

---

## P3 — `episode_id` as a real evidence column

**Problem.** `episode_id` lived inside the `context` JSON blob, but the system's two
hottest queries key on it: the watched-gate (activate exposures when
`episodes.watched=1`) and `episode_spread` (distinct watched episodes per lemma). Both
would run on `json_extract` over an append-only table that only grows.

**Proposal (applied to DESIGN.md).**

```sql
episode_id TEXT,   -- nullable: taps arrive tied to an episode's prep doc, lapses don't
CREATE INDEX idx_evidence_episode ON evidence(episode_id);
```

`context` keeps the genuinely per-event payload (`sentence_idx`, `known_ratio`,
`other_unknown_count`). Rule of thumb going forward: anything `promote` filters or
groups by is a column; anything only humans read during audits is JSON.

---

## P4 — Exposure idempotency by unique index

**Problem.** DESIGN.md asserted the episodes table "enables idempotent exposure" but
nothing enforced it: re-running `/immerse` on the same episode would append duplicate
exposure rows and inflate `exposure_count`. Separately, one talky episode could
contribute 30 exposures of the same word, making `exposure_count` noise relative to
`episode_spread`.

**Proposal (applied to DESIGN.md).** One partial unique index solves both:

```sql
CREATE UNIQUE INDEX idx_exposure_once ON evidence(lemma, episode_id, source)
    WHERE source = 'exposure';
```

`record-exposure` uses `INSERT OR IGNORE`; re-runs become no-ops by construction. With
one exposure row per lemma per episode, `exposure_count` ≈ episodes-seen-in and the
θ-table semantics ("top-2k lemma: 2 exposures across 2 episodes") become exact rather
than approximate. Taps stay unconstrained — repeated taps are legitimate new evidence
and rule 1's recency logic wants them.

---

## P5 — `mark-watched` verb (who flips the watched-gate?)

**Problem.** The watched-gate is the design's guard against inflating the known count
with unwatched analysis — but nothing specified who sets `episodes.watched = 1`. An
activation switch nobody throws means exposures stay inert forever and the ledger never
converges.

**Proposal (applied to DESIGN.md).** Seventh ledgerctl verb:

```
mark-watched <episode_id>
```

Two paths in: **implicit** — `apply-taps` marks its episode watched (pasting a prep
doc's corrections is proof you watched it); **explicit** — `ledgerctl mark-watched` for
episodes watched without any taps (a real case: an episode easy enough to need no
corrections is exactly one whose exposures should count). `/reconcile` ends by listing
analyzed-but-unwatched episodes so silent inert exposure is visible, not forgotten.

---

## P6 — Lapse polling in `apply-taps`

**Problem.** `card_lapse` was defined as an evidence source (−medium) but no mechanism
ever generated it — a dead letter in the state machine. Rule: every evidence source
must name its writer.

**Proposal (applied to DESIGN.md).** `apply-taps` (i.e. every `/reconcile`) polls
AnkiConnect for the minted cards in `cards`:

1. `cardsInfo` on all `cards.anki_guid` (bounded: only pipeline-minted cards).
2. Store `lapses` per card in the `cards` table; on increase since last poll, append one
   `card_lapse` evidence row (polarity −1, `episode_id` from the card's row).
3. `promote` as usual.

Piggybacking on `/reconcile` needs no daemon and no schedule; lapse evidence is at most
one review-session stale, which is well within the ledger's tolerance.

---

## P7 — Frequency ranks from `japaneseShowGraph.db` show penetration

**Problem.** The frequency prior (the θ table) was sourced from Leeds `ja_frequency.txt`
— 20-year-old web text, wrong register for media Japanese — with JPDB as the floated
alternative (scraping/ToS problems, unknown tokenization).

**Proposal.** A one-off build script `ledger/build_freq.py`:

1. Read per-show morpheme frequencies from `japaneseShowGraph.db` (already mode C — the
   ledger's exact join key).
2. Rank lemmas by **show penetration** (distinct shows containing the lemma, ~11k
   shows), not raw token count — penetration resists single-show catchphrase inflation,
   the same trick the phrases project validated with its genre floor.
3. Emit `lemma → freq_rank`; load into `lemmas.freq_rank` at bootstrap.
4. Lemmas absent from the corpus: fall back to Leeds rank if present, else NULL → the
   "rare / absent" θ-row (6 exposures / 4 episodes), which is the correct conservative
   default.

Re-map the θ tiers from ranks to penetration percentiles once the real distribution is
visible (the "top ~2k" tier should correspond to a penetration cliff, not an arbitrary
count).

---

## P8 — ReazonSpeech word-timestamp spike

**Problem.** Offline audio-cut quality hinged on an unverified assumption. Confirmed:
`reazonspeech-nemo-v2` (subword RNN-T) emits segment spans plus per-**subword point**
timestamps — no word start/end pairs.

**Proposal.** Timeboxed spike (~half a day) on one real episode before writing any
offline alignment code:

1. Baseline: cut sentence audio by mapping the sentence-final subword's timestamp
   (+0.5s pad). Listen to ~20 cuts; count clipped starts/ends.
2. Try NeMo `transcribe(timestamps=True)` word-level output on the vendored NeMo
   version — if it works, the problem dissolves.
3. Fallback candidate: [ReazonSpeechX](https://github.com/gunyarakun/ReazonSpeechX)
   (word-level timestamps + diarization wrapper).

**Decision rule.** If baseline cuts are clean at sentence boundaries, ship the simple
mapping — the pipeline only ever cuts at sentence boundaries, so word-level precision
may be unnecessary. Scribe remains the online default regardless (word timestamps +
better accuracy); ReazonSpeech is the offline fallback only.

---

## P9 — Prep-doc tap-export robustness

> **Superseded as the primary loop (2026-07-05).** `MOBILE.md` makes a server-backed mobile
> client over Tailscale the primary tap round-trip (`POST /taps`, single authoritative ledger
> on the PC). The copy-blob below **remains the offline fallback** — the robustness measures
> here still apply to the static-HTML path.

**Problem.** The v1 tap round-trip leaned on two fragile browser behaviors:
`localStorage` persistence for file:// / Files-app origins (iOS treats these
inconsistently — storage can vanish between opens) and the async clipboard API (denied
in some webview/file contexts).

**Proposal (applied to DESIGN.md).** Design for the single-sitting flow — tap during
the episode, copy at the end — which needs no cross-open persistence:

1. Keep taps in a JS variable, mirror to `localStorage` as best-effort crash recovery.
2. "Copy corrections" button → `navigator.clipboard`, and on failure or by default also
   render the blob as **visible selectable text** (`<textarea readonly>`) — manual
   long-press-copy always works.
3. Offer `navigator.share` (native share sheet) as a second path to get the blob off
   the phone (Notes, AirDrop, message-to-self).
4. Blob format: compact JSON `{episode_id, taps: [[lemma, "k"|"u"], ...]}` — small
   enough for a clipboard, structured enough for `apply-taps` to parse strictly.

---

# Implementation findings — 2026-07-05 (scaffold + tools build)

Deviations and discoveries from actually building the above. Each is folded
into DESIGN.md where it changes the design.

## F1 — 形状詞: na-adjectives were silently dropped

`sudachidict_core` DOES emit 形状詞 as the top-level POS for na-adjective
stems (綺麗/静か/頑丈 all confirmed), contrary to the `sentence-mining`
skill's comment ("na-adjectives are 名詞 in Sudachi — no 形状詞 category").
Its `CONTENT_POS_PREFIXES` therefore filtered them out of the known-set scan
and candidate mining — a whole vocabulary class invisible, with no error.

**Fix:** `engine/lemma.py` includes 形状詞 in `CONTENT_POS_PREFIXES`
(caught by a failing coverage test, verified against the dictionary).
**Follow-ups:** rebuild any freq table bootstrapped before the fix (~85s);
apply the same one-line fix upstream in
`~/Downloads/sentence-mining/scripts/analyze.py` (its known-set undercounts
na-adjectives today).

## F2 — P6 amendment: lapse polling keys on note ids, not guids

P6 assumed `cardsInfo` on `cards.anki_guid`. AnkiConnect's search syntax
cannot query by guid, so a guid-only cards table can't be polled. The
primary push path (`tools/deck.py`) uses `addNote`, which returns a note id
— `cards.anki_note_id` stores it and polling does `findCards("nid:…")` →
`cardsInfo` → append `card_lapse` on lapse-count increase. `anki_guid`
remains for the `.apkg` fallback path (match on a later resync). Schema
carries both columns plus `lapses` (last polled value).

## F3 — promote: card_lapse joins rule 1; timestamp ties go to the negative

The DESIGN state diagram has a `known → learning` edge via `card_lapse`,
but the promote rules text listed only `tap_unknown` as the demoter —
`card_lapse` evidence would have been dead weight. Implemented rule 1
treats both as the "fresh negative". Separately, "newer than any positive"
is `>=` not `>`: with second-resolution timestamps a tap written in the
same second as an exposure row must still win (taps are deliberate strong
evidence; genuine same-second conflicts only arise within a single write).

## F4 — Leeds fallback keeps its raw rank

P7 says lemmas absent from the show corpus "fall back to Leeds rank".
Implemented literally: the Leeds rank is inserted as-is (not offset past
the corpus ranks), since the θ tiers were originally scaled against that
list. Revisit alongside the penetration-percentile re-mapping.

## F5 — batch idempotency table

MOBILE.md's idempotent tap re-flush ("server dedupes replays by batch_id")
needs server-side memory; that lives in the ledger as `tap_batches`
(batch_id PK). `apply_taps` is a no-op for a seen batch_id. The offline
copy-blob generates a fresh batch_id per copy click, so an edited re-copy
applies while a double-paste of the same blob dedupes.

## Deferred vendoring

`interleaver.py` / `m4b.py` (PRIME mode) stay in audioPrimeProd for now —
they import the GUI-coupled translator/video/process-tracker modules.
Vendor alongside the PRIME tool when that mode is built.
