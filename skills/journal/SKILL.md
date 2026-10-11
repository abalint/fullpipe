---
name: journal
description: Review the Japanese journal entries typed on the phone's Journal tab in the fullPipe Immersion Workstation. Bare `/journal` pulls every entry not yet reviewed at its current text, and YOU (this session — no cloud LLM) rewrite each one the way a native would actually SAY it to a friend (casual spoken register, not polished written Japanese), add plain-English notes on what changed, and stamp the review onto the entry so the phone shows it under the original in the same row — reviewed entries are skipped on the next run until they are edited again. Also `/journal list`, `/journal show <id>`, `/journal redo <id|all>`. Use for "/journal", "review my journal", "correct my journal entries", "check what I wrote in the journal", "did I write that right", "look at today's entry".
---

# /journal — a friend reads your journal

The phone's Journal tab is where the user writes freely in Japanese. This
skill is the reply: for each entry, the same thing said the way a Japanese
friend of the same age would say it out loud, plus short notes in English
on what moved and why. The review is stored on the entry's row in the
ledger db and rides `GET /journal` back to the phone, which shows it under
the original text inside the open row (and a ✓ on the closed row).

Nothing here touches the ledger's word knowledge — journal rows are
mirror + review only. The user is the writer; never edit `text`.

## Plumbing

`PY=$FULLPIPE/.venv/bin/python`, run from `$FULLPIPE` (`~/Documents/code/anki/fullPipe`).

| command | what |
|---|---|
| `$PY -m ledger.ledgerctl query journal --unreviewed` | the work list: entries never reviewed, or edited since their review (`reviewed_text != text`). Newest first. Each row: `id, text, created_at, updated_at, review_text, review_notes, reviewed_at, reviewed_text` |
| `$PY -m ledger.ledgerctl query journal` | every entry, reviews included |
| `$PY -m ledger.ledgerctl journal-review <id> <review.json>` | store `{"text": "<rewrite>", "notes": "<notes>"}` on the entry and stamp `reviewed_text = text` — the **reviewed marker**. The entry leaves `--unreviewed` until the user edits it again; then it comes back (the old review stays visible on the phone, labelled "for an earlier draft", until you replace it) |

The review JSON files go in the scratchpad (`<scratchpad>/journal/<id>.json`),
one per entry; `journal-review` reads the file so the Japanese never passes
through a shell quote.

The server need not be restarted: the phone pulls `GET /journal` when the
Journal tab opens, and the server reads the row live. (If `journal-review`
reports a missing column, the running server predates the review columns —
restart it per the usual hand-start.)

## Bare `/journal` — review what's new

1. `query journal --unreviewed`. Nothing → say so in one line and stop.
2. Write one review per entry (below). Up to ~4 entries: do them yourself,
   here. More: fan out to parallel **Opus** subagents, one entry (or a
   handful) each, briefing each with the whole "Writing the review" section
   verbatim plus its entries' JSON; each agent writes its review files and
   runs `journal-review` itself. Never cloud-API the text out.
3. `journal-review` each one. Re-run `query journal --unreviewed` to confirm
   the list is empty (an entry still listed = its text changed under you
   while you worked; review the new text).
4. Report, one line per entry: date · first line of the original · how many
   notes · the single biggest point. No need to repeat the whole rewrite in
   chat — it is on the phone.

## `/journal list` · `/journal show <id>` · `/journal redo <id|all>`

- `list`: `query journal` → date · first line · ✓ (reviewed, current) / ✎
  (edited since review) / — (unreviewed), one line each, newest first.
- `show <id>`: the entry, the review, the notes, as stored.
- `redo <id>`: re-review one entry even though its marker is current
  (the user disagrees with a review, or wants a second opinion — ask
  what they want different if they said). `redo all`: every entry.

## Writing the review

**Who you are:** a Japanese friend the user's age, reading a message they
sent you, telling them how *you* would have said it. Not a teacher, not an
editor. The user is deliberately writing the way they would **talk**, so
the target is natural **spoken** Japanese between peers (タメ口) — never
the written-journal register. Concretely:

- Plain forms, never です・ます. 〜ている → 〜てる, 〜てしまう → 〜ちゃう/〜じゃう,
  〜ておく → 〜とく, 〜ては → 〜ちゃ, 〜なければ → 〜なきゃ, けれども → けど,
  〜のだ → 〜んだ, という → って, では → じゃ, 〜と思う stays but hedges
  often become 〜かな / 〜気がする / 〜っぽい. Sentence-final けど / し /
  から / から ね trailing off are normal in speech — use them where a friend
  would.
- Particles a friend drops in speech (を, は, が after a topic already set,
  the が in 〜が欲しい etc.) get dropped where that is what a native does;
  sentence-final ね / よ / な / かな / じゃん / でしょ where the *meaning*
  calls for them (seeking agreement, telling, wondering), not sprinkled.
- Spoken connectors over written ones: でも / で / それで / だから / あと /
  てか / ってか over しかし / そして / また / 従って.
- Vocabulary stays at the user's level and in their words wherever the word
  is right. A rewrite is not an upgrade; do not reach for a rarer word
  because it is "better Japanese". Swap a word only when a native would not
  use it there (wrong word, wrong collocation, textbook-only word a friend
  would never say).
- Pronouns: drop 私は / 僕は wherever a native would (almost always). Do not
  introduce gendered sentence-final particles (〜わ, 〜かしら, 〜ぜ, 〜ぞ) the
  user did not use; 〜な and 〜かな are fine for anyone.
- Keep the user's **meaning, order and content**. If a sentence is
  ambiguous, pick the most likely reading, say in the notes what you took it
  to mean, and (only if it matters) give the other reading in one line.
  Never add content, opinions or detail that is not in the original.
- Leave alone what is already natural. A fluent sentence copied unchanged
  into the rewrite is a correct review. The user needs to see which parts
  land as-is.
- Mixed-in English words / titles stay as the user wrote them.
- Something unparseable: keep your best guess in the rewrite and one note
  saying "I read this as …; if you meant …, then …".

**The rewrite (`text`)** is the whole entry as said to a friend, in the
user's line breaks (one line → one line). Keep it plain text. Do not
annotate inside it — no brackets, no alternatives in slashes.

**The notes (`notes`)** — plain text, one line per point, each starting
with `・`, biggest naturalness win first, usually 2–6 lines; one or zero
for a short entry that was already fine. Each line: what changed → why, as
one short sentence, with the Japanese inline. Rules (these matter more
than anything else here):

- **Plain English only. No grammar terminology** — not "particle",
  "topic marker", "volitional", "nominalizer", "passive", "copula", "te-form",
  "conjunctive". Say what the speaker is doing: "a friend wouldn't put は
  here — the topic's already set", "〜すね isn't a form; the telling-you-so
  feeling is 〜ね after a plain form: 思うね", "ている squeezes to てる when
  talking".
- Lead with the thing that most made it sound written / foreign / wrong.
  Corrected spelling or a mis-conjugation is a note too, but after register.
- No praise padding, no "great job". If something was already natural and
  worth noticing (an idiom placed right), one line is fine: "〜 is exactly
  how it's said."
- Never hedge with "might be more natural". It is how you'd say it, or it
  isn't a note.

**Format of the file** (`<scratchpad>/journal/<id>.json`):

```json
{"text": "Anki、要るかな？今はいらないと思うけど。でもみんな逆のこと言うんだよね。どうしようかな、分からん。",
 "notes": "・必要するか → 要るかな／必要かな: 必要 doesn't take する; wondering out loud to yourself is 〜かな.\n・いまとは思うないから → 今はいらないと思うけど: the thing you don't think is いらない; 'not think' is 思わない, and the trailing けど is how you leave a thought hanging with a friend.\n・みんなは逆思うすね → みんな逆のこと言うんだよね: 'everyone says the opposite' — 〜すね isn't a form; the 'you know how it is' feeling is 〜んだよね."}
```

Write the file with a heredoc or Python, never by echoing the Japanese
through shell quoting. Check `json.loads` before `journal-review`.

## When the user asks about an entry in chat

If they paste or name an entry and ask "is this right?", review it the same
way in chat — and, if it is a journal entry on the server, stamp it with
`journal-review` too so the phone carries the same answer.
