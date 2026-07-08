# Feature audit — 2026-07-08

A "what's obviously missing" pass over the whole system (backend `fullPipe/` +
the Capacitor client at `anki/mobile/`), read against the intent in `DESIGN.md`,
`PROPOSALS.md`, and `MOBILE.md`. Findings only; ordered by how obviously a user
would feel the absence. Items **#1** and **#2** are fixed in the same session —
see the *Status* notes.

## The biggest missing features

### 1. No progress / stats surface anywhere — **FIXED 2026-07-08**
The whole premise is a known-lemma ledger that tracks comprehension growth, and
the learner never got to *see* it. The app nav was only Queue / Listen / Prep /
Settings; the server had no stats endpoint. The ledger already holds
known/learning counts, coverage, the `needs_review` queue, episodes watched,
cards minted, exposures — none surfaced.

*Fix:* `GET /stats` (`server/app.py`) aggregates the ledger — headline counts,
evidence-by-source, and **frequency-band coverage** (of the top 1k/2k/5k/10k
most common corpus lemmas, how many are known) built from the `freq` table ∩
known lemmas. New **Progress** tab in the app (`mobile/src/views/stats.ts`) with
a nav entry, offline-cached like the queue snapshot.

### 2. Failed jobs were a dead end — and could wedge permanently — **FIXED 2026-07-08**
Two halves of the same gap:
- *App:* a `failed` job showed its error but offered no retry — you had to
  delete and re-paste the URL.
- *Server, worse:* a crash mid-Stage-1 (`downloading`/`transcribing`/
  `tokenizing`) or mid-`pushing` left the job stuck in that state forever — no
  reaper reset it, and `DELETE /jobs` refuses those states (409). The row was
  both un-runnable and un-deletable.

*Fix:* `reap_stale()` (`server/jobqueue.py`) runs at every executor startup
(server `Worker.run`, CLI `drain`): dangling STAGE1 → `queued` (the worker
re-runs them), stranded `pushing` → `watched` with a "re-submit to retry" error
(the phone's existing retry-cards path picks it up). New `POST /jobs/{id}/retry`
re-queues a failed job; the app grows a **Retry** button on failed rows.

## Still open (documented, not yet built)

### 3. Only 1 of the 5 designed "modes" exists
`DESIGN.md` centers on PRIME · ANALYZE · MINE · GENERATE · REPLACE. Only MINE
(+ the prep-doc half of ANALYZE) ships. **REPLACE is a data-generated-but-never-
consumed dead end**: `promote` fills a `needs_review` queue "to route to
REPLACE" but nothing reads it. `/generate` (synthetic i+1) and true PRIME
(interleaved audio, vs. today's passive-shelf flag) are unbuilt.

### 4. No onboarding / `/setup`
First run dumps you on a raw Settings form to hand-type a Tailscale MagicDNS
host + token (a raw `100.x` IP silently fails). The designed `/setup` config
interview doesn't exist, so new users also hand-write `config.json`.

### 5. Downloads are fully manual — no background pull or retention
README-acknowledged: no WorkManager pre-fetch on wifi+charging, no retention/pin
UI, no metered-data guard, no free-space check, no cancel/resume. The "overnight
queue → wake-up-ready" flow the design is built around isn't closed.

### 6. `/reconcile` skill unbuilt
The online tap path (`POST /taps`) works, but the offline copy-paste-blob
reconcile path the immerse skill points at as its fallback has no skill.

### 7. Corpus-leverage scoring is inert
`tools/coverage.py` hardcodes `"leverage": None` (gated on the unbuilt P1 mode-C
corpus re-parse). Candidate ranking silently falls back to freq+recurrence only.

### 8. JMdict dictionary silently empty on a fresh install
`GET /definitions` returns `{}` if `jmdict.db` is absent, and the build is a
manual one-off never wired into setup — the player's tap-lookup popup is blank
with no error surfaced.

### 9. Offline ASR never built (P8 spike only)
ElevenLabs online is the sole transcription path; the GPU service emits
segment-level timestamps only.

### 10. Smaller app UX gaps
No search/filter across the queue; no AnkiDroid deep-link; passive "now playing"
bar has no scrubber/progress; the bare Prep tab is a confusing fallback that can
only show the most-recent doc; errors use blocking `alert()`/`confirm()`; the
prep bar's `▶ Watch` renders even when the video isn't downloaded.

### 11. Auth is minimal (acceptable for the tailnet-only threat model)
Single shared static token (empty ⇒ auth off), media endpoints take the token
as a URL query param, CORS wide open, no per-device tokens or expiry. Fine while
Tailscale-only; no defense-in-depth if ever exposed.

## Verified present (not gaps)
Engine-level ASR retry/backoff/timeout, idempotent enqueue + failed-job
re-queue, tap/rating idempotency (`batch_id`/`review_id`), background close-out
with error-on-row + safe re-POST retry, purge-on-delete with watched/rated
retention. P2–P7 and P9 implemented. Native `PassiveAudio`/`ShareTarget` plugins
are real and registered; offline empty/error states for Queue/Listen/Prep and
the outbox sync are implemented.
