# Immersion Workstation — Mobile Client & Sync Server

The phone is the **primary interface** (queue · watch · pre/post-watch taps); the PC is the
**analysis system** (download · ASR · tokenize · curate · ledger). This document specifies
the layer that binds them: a small PC server, a two-stage pipeline, and an offline-tolerant
Android client, all over a private Tailscale mesh.

It supersedes DESIGN.md's v1 phone round-trip (static prep-doc HTML + copy-paste "corrections"
blob). That flow was chosen for *zero infra*; a private mesh makes reachability free, so the
online path is now server-backed. The copy-blob fallback (PROPOSALS.md P9) was retired from
the UI on 2026-07-07 — the offline outbox covers unreachability, so taps queue and sync
instead of round-tripping through the clipboard.

---

## Principles

- **Single source of truth on the PC.** The ledger and all staged artifacts live on the PC.
  The phone is a **mirror**, never a second authoritative copy — so there is no two-copy merge,
  no resync reconciliation. This is the same spine DESIGN.md already defines; the server just
  exposes it.
- **Offline-tolerant client.** Never require live connectivity *at watch time*. The phone pulls
  while reachable, caches locally, queues taps in an outbox, and flushes opportunistically. The
  PC being asleep degrades to "syncs later," never to "lost work."
- **Server is thin.** The server is an HTTP wrapper over the existing `ledgerctl` verbs plus a
  job queue. No new intelligence lives here — the smart work stays in the live `/immerse` skill
  (DESIGN.md's "intelligence in skills, not frozen prompts").
- **Two-stage pipeline.** An unattended deterministic **prep batch** runs from the queue; a
  deliberate, live **curate** step runs when you sit down. Batching the heavy plumbing does not
  compromise the in-the-loop curation principle.

---

## Topology

```
   PHONE (primary UI, offline-tolerant)          PC (analysis system, authoritative)
   ┌─────────────────────────────┐               ┌──────────────────────────────────┐
   │ queue screen / share-sheet  │──enqueue──────▶│  server (FastAPI)                │
   │ prep-doc viewer (tappable)  │◀──prep/cards───│   ├─ job queue                   │
   │ video player + subs         │◀──480p video───│   ├─ ledgerctl verbs over HTTP   │
   │ tap outbox → flush          │──taps─────────▶│   └─ staging dir                 │
   │ background sync (WorkMgr)   │                │  worker  → Stage 1 (batch)       │
   └──────────────┬──────────────┘               │          → Stage 2 (curate/live) │
                  │        Tailscale (WireGuard)  │  ledger (SQLite) — the spine     │
                  └───────────────────────────────▶  /immerse writes here            │
                                                  └──────────────────────────────────┘
```

---

## Transport — Tailscale

- **Discovery is free.** MagicDNS gives the PC a stable hostname (`http://<pc>:<port>`), reachable
  from the phone on or off the home network. No mDNS, no port-forwarding, no dynamic-IP dance.
- **Encryption is free.** All traffic is WireGuard-encrypted inside the tailnet, so plain HTTP is
  acceptable for a native client (add an Android cleartext-traffic exception scoped to that host).
  If a browser/PWA secure-context is ever needed, issue certs with `tailscale cert`.
- **Auth is light.** The tailnet already authenticates devices. **Bind the server to the Tailscale
  interface only** (not `0.0.0.0`); add a shared bearer token as belt-and-suspenders. Never expose
  publicly.
- **Availability.** The phone can only sync when the server is up, so run it as a **background
  service** (launchd on macOS / systemd on Linux), not a hand-started process.

---

## Two-stage pipeline

DESIGN.md's `/immerse` did download → ASR → tokenize → analyze → build in one live pass. That
front matter is split off into an unattended batch so you can queue overnight and only spend live
attention on curation.

```
STAGE 1 — PREP BATCH   (unattended; the worker drains the queue automatically)
  queued → downloading(480p) → transcribing → tokenizing → PREPARED
  artifacts staged:  480p H.264 video · punctuated/reconstructed subs sidecar ·
                     tokenized transcript · coverage flags + ranked unknown candidates

STAGE 2 — CURATE       (explicit; the live model — /immerse over PREPARED jobs)
  PREPARED → curating → STAGED
  artifacts staged:  synopsis · thematic keywords · focal points ·
                     ~15 curated native-audio cards (pushed to Anki) · prep-doc JSON
  ledger writes:     exposure (inert until watched) · mined_card
```

**Stage 2 is `/immerse` refactored to consume `PREPARED` jobs** rather than a raw URL — the
download/ASR/tokenize it used to do up front is exactly the batch that already ran. The
`hold_for_review` idea from earlier drafts is moot: Stage 2 is *always* the deliberate step now.

### Job lifecycle

| state | stage | set by | phone reaction |
|---|---|---|---|
| `queued` | — | producer (phone or PC) | — |
| `downloading` · `transcribing` · `tokenizing` | 1 | worker | — |
| `prepared` | 1 done | worker | **pull video** (big, slow — do it overnight) |
| `curating` | 2 | curate step | — |
| `staged` | 2 done | curate step | **pull prep-doc** (tiny, fast); review + tap |
| `reconciled` | — | `POST /taps` (pre-watch feedback) | ready to watch; cards selected. Set **only from a pre-watch state** — feedback that arrives after mark-watched (either tap order, or an outbox flush) still records its evidence but leaves a `pushing`/`watched` row alone |
| `pushing` | — | `POST /watched` | close-out running in the background (clips + Anki push + lapse poll); `progress_msg` narrates it ("pushing card 3/12"); delete is refused |
| `watched` | — | close-out thread | terminal: cards pushed to Anki (skipped for the disliked-it branch, body `{cards:false}`); a failed push lands on the row's `error` — re-POST `/watched` retries; **local files are kept** (rewatch / passive listening; deletion is manual only) |
| `failed` | any | worker | surface error + retry action |

Orthogonal to state: a `passive` flag (`POST /jobs/{id}/passive`) shelves a
`watched` episode into the phone's **Listen** tab — the passive-listening
collection. The row leaves the queue screen but keeps its state and artifacts;
the phone loops the **already-downloaded episode mp4s** as background audio —
the native player reads the same on-device file the in-app watch/rewatch uses
(`setDataSource(file://…)`), so passive listening never re-downloads media the
phone already has. Un-shelving flips the flag back; delete from the Listen tab
is the same `DELETE /jobs/{id}`.

Also orthogonal: a `debrief` flag (`POST /jobs/{id}/debrief`, the queue row's
**"🗣 debrief"** button, allowed from `staged` onward) queues an episode for a
post-watch comprehension conversation — the PC-side `/debrief` skill treats
flagged jobs as its worklist and clears the flag when the conversation is done
(`jobqueue set-debrief <id> off`). While the flag is set, `DELETE /jobs/{id}`
is refused (409) and the app blocks swipe-delete with an explainer: the
conversation reads the episode's transcript, which delete would destroy. The
row shows a `🗣 debrief` chip on both the Queue and Listen tabs.

The Listen tab's now-playing bar carries mp3-player transport (2026-07-10):
scrubber + elapsed/duration clock (also on the lock screen — the MediaSession
publishes duration + `SEEK_TO`), ±10 s skips, speed, and a native sleep timer
(15/30/45/60 min — armed in the service, so it fires with the webview dead).
Per-episode resume positions persist in the service's SharedPreferences and
mirror the video player's saved positions both ways, so a track picks up where
either surface left it, even after a process kill; a finished track clears its
resume point and the loop restarts it from the top. Headphone unplug pauses
(`ACTION_AUDIO_BECOMING_NOISY`). The in-player 🎧 audio mode shares the same
service and gained the live scrubber + cue-level ⏮/⏭ jumps.

The downloaded mp4 survives for the Listen loop for free: **the phone never
auto-deletes videos** (revised 2026-07-09 — the eager delete-at-mark-watched was
removed; it broke passive listening and blocked rewatch). Videos stay on the
device until you delete them explicitly (swipe-delete on a queue or Listen row).
So both passive entry points keep the file with no re-download: the prep view's
**"🎧 + listen"** button (marks watched + shelves passive in one tap) and
shelving *after* the fact from the queue's watched **"🎧 passive"** button. The
`⬇` fallback on the Listen tab only appears if the episode was never downloaded
(or was manually deleted).

*(Revised 2026-07-05: taps are pre-watch feedback — known-taps correct the
ledger, ★ high-interest taps steer card selection — so `reconciled` now
precedes `watched`, and the Anki push happens at `watched`, after the user
has actually seen the episode. "Unknown" taps were dropped: candidates are
presumed unknown; untapped = unknown.)*

Jobs are idempotent by `episode_id` (the ledger's stable source hash); re-enqueuing a completed
source is a no-op, matching the exposure unique-index guarantee in DESIGN.md.

---

## Bidirectional initiation — one queue, two producers

Both sides only **enqueue**; the PC worker is the sole executor — whether it
runs as the server's background thread or one-shot from the CLI
(`python -m server.worker [SOURCE ...]`, the `/prepare` skill: enqueue +
synchronous drain, no server needed).

- **From the PC** — `/immerse <url>` (or a small `queuectl` CLI) enqueues.
- **From the phone** — an "add to queue" screen, plus an **Android share-sheet target**: share a
  YouTube link straight out of the YouTube app into the client and it is queued. No copy-paste.

---

## Decoupled pulls (default, decided 2026-07-05)

The video is the dominant transfer; the prep-doc + cards are tiny. Do **not** make the phone wait
for the live curate step to start moving the big bytes:

- **Video pulls at `prepared`** — overnight, over unmetered wifi + while charging.
- **Prep-doc + cards pull at `staged`** — seconds, after the morning curate pass.

**Result:** queue a batch before bed → wake to every video already local → run one curate action →
the lightweight artifacts land in seconds → go. The slow transfer overlaps sleep; the manual step
gates only the small stuff.

*Alternative (rejected as default):* pull nothing until fully `staged`. Simpler (one pull trigger)
but forces the multi-GB transfer into the morning, defeating the wake-up-ready goal. Available as a
one-flag mode for anyone who wants strict "ready = everything ready" atomicity.

---

## Overnight flow

```
 night   queue 4 sources (phone share-sheet / PC)
   │
   ├─ worker drains queue → each job: download 480p → ASR → punctuate → tokenize → coverage
   │                        → PREPARED
   ├─ phone (charging + wifi) auto-pulls each PREPARED job's 480p video + subs
   │
 morning trigger CURATE (live) → synopsis · focal points · ~15 cards → push to Anki → STAGED
   │
   ├─ phone pulls prep-doc + cards (seconds); cards ride AnkiConnect → AnkiWeb → AnkiDroid
   └─ every video local, prep docs cached, deck synced — offline-ready
```

---

## Server API

Thin HTTP over `ledgerctl` verbs + the queue. (Verbs: `materialize-known`, `compute-anki-known`,
`record-exposure`, `mark-watched`, `apply-taps`, `promote`, `rate`, `query` — see DESIGN.md.)

| endpoint | maps to | notes |
|---|---|---|
| `POST /jobs` | enqueue | `{source: url\|file\|topic}` → `episode_id`; idempotent |
| `GET /jobs` · `GET /jobs/{id}` | queue read | lifecycle state + progress; annotated with `duration` (seconds) and `comprehensibility` (coverage's token_comprehensibility, 0..1) for the queue's sort/display |
| `POST /jobs/{id}/curate` | launch Stage 2 | kicks the live `/immerse` curate over one/many `prepared` jobs |
| `POST /jobs/{id}/passive` | shelve to Listen tab | `{passive: bool}` — flags a `watched` episode as passive-listening material (409 otherwise; un-shelving always allowed). Pure flag flip: state/artifacts/ledger untouched; `passive` rides back on `GET /jobs` |
| `POST /jobs/{id}/debrief` | queue for /debrief | `{debrief: bool}` — flags an episode (`staged`/`reconciled`/`pushing`/`watched`; 409 earlier) for the PC-side post-watch comprehension conversation; unflagging always allowed. Pure flag flip, but **while set `DELETE /jobs/{id}` is refused** — the debrief needs the transcript. The `/debrief` skill reads flagged jobs as its worklist and unflags on completion; `debrief` rides back on `GET /jobs` |
| `GET /video/{id}` | staged file | **resumable** (HTTP range) — available at `prepared` |
| `GET /video/{id}/subs` | staged file | subtitle sidecar |
| `GET /prep/{id}` | prep-doc JSON | available at `staged`; pre-tokenized sentences w/ readings + glosses |
| `GET /transcript/{id}` | staged coverage | **every** sentence w/ start/end + tokens (prep ships only the i+1 subset) — drives the in-app player's tap-able subtitle overlay; available at `prepared`. tokens carry `t` (aligned start seconds, engine/word_align.py — word/segment granularity for ASR, cue granularity for hand-subs) pacing the player's roll-up window; absent on pre-alignment episodes, where the player falls back to proportional pacing. Top-level `curated` = the curate pass's grammar/phrase notes are aboard; the app downloads its sidecars at video-download time (usually `prepared`) and refreshes them once the episode turns up staged (`refreshSidecars`) |
| `GET /definitions/{id}` | jmdict.db + curate.json + repair.json | JMdict entries for **every** lemma in the episode — content words, particles, aux verbs, pronouns, names — the player's any-word popup (kana keys rank kana-natural/grammar entries first, so の leads with the particle, not 野). Keyed by the Sudachi lemma already on each token, so no client deinflection; the app narrates conjugation itself from the token chain (mobile `inflection.ts`). Compounds/expressions Sudachi splits (帝王切開 → 帝王\|切開, そういう → そう\|いう) ride along keyed by the joined span — validated headwords only (`compound_entries`) — and the app reconstructs the join on tap (mobile `compounds.ts`). `{}` until `tools.jmdict build` has run. Curate-authored `defs` rows merge in flagged `ai`: sole entry for words JMdict lacks (worklist from `tools.jmdict missing`), **prepended** episode-sense entry for words it has — the popup leads with the sense used in this episode, full dictionary entries after. The repair gate's `names` (surface + kind + note) merge the same way, so name taps answer without waiting for curation |
| `POST /taps` | `apply-taps` + `tools.select` | `{episode_id, batch_id, taps:[[lemma,"k"\|"h"],…]}`; pre-watch feedback: "k"→ledger, "h"→card priority; runs final card selection; does NOT imply watched |
| `POST /watched/{id}` | `mark-watched` + `tools.deck` | post-watch close-out: activates exposures immediately, then **pushes the selected cards to Anki in a background thread** (responds with `{cards: {queued: N}}`; the job row narrates progress via state `pushing` and flips to `watched`, carrying any push error); re-POST retries a failed push. Body `{cards: false}` = watched-but-disliked: exposures still activate, no cards pushed |
| `POST /episodes/{id}/rating` | `record-rating` | post-watch **survey** (SURVEY.md) → append-only `taste_events`. Body `{rating: 1-5\|null, tags:[…], axes:{…}, follow, note, review_id?}`. `axes` are graded 1–5 on `topic_pull·presenter·audio_fidelity·speech_clarity·difficulty` (own sliders; a 5 on `difficulty` = too-hard, not "good"). `follow` ∈ `block·less·neutral·more` is a per-**channel** intent decoupled from the star (kept even when `rating` is null) → upserts `channels.follow_state`. `note` = free text. `tags` (chips) ∈ `already_knew·over_my_head·didnt_grab·format_miss·fascinating·loved_format`. Re-POST appends a new review (on-read verdict takes the latest); a client `review_id` makes it idempotent for outbox replay. Rating+tags+axes+follow ride back on `GET /jobs` (`_taste`) so the app pre-fills a re-review. Ratable pre-watch; a rated-but-unwatched episode keeps its rating through `DELETE /jobs/{id}` (rating-only ledger tombstone) |
| `GET /coverage` | `query` | coverage %, trends, `needs_review` queue, mining candidates |
| `GET /health` | — | liveness for the client's reachability check |

---

## Video handling

- **Download at 480p** directly (`yt-dlp -f "bestvideo[height<=480]+bestaudio/best[height<=480]"`) —
  cheaper than downloading high and transcoding down, and skips a whole transcode stage.
- **Remux/transcode to H.264 mp4** (the pipeline's only transcode). YouTube 480p is often VP9/AV1;
  normalizing to H.264 guarantees playback on any Android player. ffmpeg is already vendored.
- **Ship the subtitle sidecar** with the video so you watch with the exact subs the analysis used.
- **Retention — both sides** (video maintenance is a first-class concern):
  - *PC:* a staged video may be purged once the phone confirms pull **and** the episode is
    `reconciled`.
  - *Phone:* **no automatic deletion** (revised 2026-07-09) — videos are kept after `watched` for
    rewatch and passive listening, and removed only by an explicit swipe-delete. *Planned:* a visible
    storage-used readout and an opt-in size cap (LRU over non-passive watched videos) so a large
    queue can't fill the device; until then, reclaim space by deleting rows by hand.

---

## The Android client

**Build: Capacitor** (web UI wrapped native) — recommended. Reuses the `render/` prep-doc template
DESIGN.md already plans, gets native audio + filesystem + offline cache cheaply, and sidesteps the
iOS-style `file://` fragility that PROPOSALS.md P9 works around. *Native Kotlin* is the alternative
if best-in-class furigana/tap typography is worth a separate codebase.

**Responsibilities**
- Render the prep doc: pre-tokenized sentences (no on-device tokenizer needed), furigana + glosses,
  every word a tap target, focal points highlighted.
- Capture "know / don't-know" taps → local **outbox**; flush to `POST /taps` when reachable. Each
  batch carries a client-generated `batch_id` so a re-flush after reconnect is **idempotent**.
- Offline cache of pulled prep docs + videos; background sync via **WorkManager** constrained to
  *unmetered network + charging*.
- In-app learning player: WebView `<video>` over the downloaded local file
  under a subtitle overlay built from the tokenized transcript —
  watch-time word taps land in the same tap store/outbox as prep-doc taps.
  Prep-doc keywords highlight orange and pop gloss + curate notes on tap;
  subtitle modes on / keyword-only / off; cues linger to the next line so ASR
  end-times don't cut subs early. Replay-line / prev / next / speed /
  furigana / fullscreen; resume position.
- Queue screen: per-item lifecycle state, download progress, storage used, pin/delete.
- **Not a card reviewer** — AnkiDroid owns review (cards arrive via AnkiConnect → AnkiWeb →
  AnkiDroid). The client may deep-link into AnkiDroid.

---

## Sync semantics

- **One outbox for every write** *(built 2026-07-06)*. All client-side mutations — tap batches,
  mark-watched, ratings, enqueues — are typed actions in a FIFO outbox, flushed opportunistically
  (submit / app-foreground / network-return). FIFO preserves the workflow order: an episode's
  feedback flushes before its close-out. Downloaded episodes are therefore fully usable offline:
  watch, tap, mark watched, rate — the server catches up at the next sync.
- **Idempotent replays.** `batch_id` on every tap POST and a client-minted `review_id` on every
  rating POST; the server dedupes both. `POST /watched` and `POST /jobs` are idempotent by
  construction. So a double-flush after a flaky connection is harmless. A permanently rejected
  action (404/409/410/422 — e.g. the episode was deleted on the PC) is dropped instead of
  poisoning the queue.
- **Offline queue.** The client caches the last `GET /jobs` snapshot and rebuilds the queue screen
  from it when unreachable, overlaying pending outbox actions (a queued mark-watched reads as
  watched with a `⇪ pending sync` chip). Prep docs auto-cache for staged episodes on every online
  queue load, and the `⬇` download bundle includes the prep doc alongside video / subs /
  transcript / definitions.
- **Per-artifact readiness.** "Ready" is per artifact class, not per job: video ready at `prepared`,
  prep+cards ready at `staged`. The decoupled-pull default relies on this.
- **Reconcile.** `POST /taps` *is* the `/reconcile` round-trip — no copy-paste blob; offline
  taps wait in the outbox and sync when reachable (the P9 blob button was retired 2026-07-07).

---

## New components

```
fullPipe/
└── server/                  # FastAPI: job queue + ledgerctl verbs over HTTP, Tailscale-bound  [BUILT]
    ├── app.py               #   routes (table above) + auth + uvicorn entry
    ├── jobqueue.py          #   job model + lifecycle (named to dodge stdlib `queue`)
    ├── worker.py            #   Stage 1 batch drain · video staging · Stage 2 artifact watch
    └── app.fullpipe.server.plist   # launchd service template

anki/mobile/                 # Capacitor Android client (prep viewer · video player · sync)  [BUILT]
                             # — its own subproject, not under fullPipe/ (decided 2026-07-05);
                             #   see mobile/README.md for layout + build
```

Reuses: `engine/` (download·ASR·tokenize·anki), `ledger/ledgerctl.py` (the verbs), `render/`
(prep-doc template, now hydrated by `GET /prep`). Adds only the queue/worker and the client.

---

## Next steps

*Groundwork already in place (2026-07-05 build): the ledger side of `POST /taps` exists —
`apply_taps` / `poll_lapses` / `mark_watched` are importable functions, `tap_batches` gives
batch_id replay dedup, and Stage 1's per-episode artifacts (`transcript.json` · `sentences.srt` ·
`coverage.json` · `prep.html` · `clips/`) are laid out under `<work_dir>/episodes/<id>/` by the
`acquire`/`coverage`/`render`/`deck` tools (see README.md). The server wraps these; it does not
reimplement them.*

1. ~~Stand up `server/` with the job queue + the `ledgerctl` verb routes; bind to the Tailscale
   interface as a launchd service.~~ **Done 2026-07-05.** The worker runs Stage 1 itself
   (`tools.acquire` + video staging + `tools.coverage`) and *watches* for Stage-2 artifacts
   (`curate.json` + `prep.html` appearing flips prepared/curating → staged); `POST /curate`
   just marks the state — the live curate remains `/immerse` on the PC.
2. ~~Teach `/immerse` to consume `prepared` jobs.~~ **Done 2026-07-05.** Bare `/immerse` is now
   queue-aware: reviews the queue (`server.jobqueue` CLI), asks what to curate, skips
   acquire/coverage when Stage-1 artifacts exist, and closes jobs to `staged` itself.
3. ~~Scaffold the Capacitor client~~ **Done 2026-07-05 → `anki/mobile/`.** Remaining from this
   item: WorkManager background video pull (downloads are manual-tap for now)
   and the retention/pin/storage-cap controls.
4. ~~Wire the Android share-sheet enqueue target.~~ **Done** (ShareTargetPlugin → queue screen).
5. Prove the overnight flow end-to-end on one batch: queue at night → videos local by morning →
   curate → cards + prep sync in seconds.
