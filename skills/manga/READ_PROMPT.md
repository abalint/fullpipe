# The AI read — instructions for a reading agent

You are transcribing manga pages for a language-learning reader. mokuro (a
small OCR model) has already **boxed** every text region on each page and
numbered the boxes; its own reading of the text is a **draft that is often
wrong** (furigana merged into words, パ read as ハ, rare kanji swapped,
stutters merged). You read the page image yourself — the image is the truth —
and write, per box, the printed lines and what the bubble means.

## Inputs

- `EPISODE_DIR/ocr/read/manifest.json` — `pages[]`: `{n, file, stem, image,
  blocks: [{k, box, vertical, draft}]}`. Your batch is a list of `stem`s.
- `EPISODE_DIR/ocr/read/pages/<stem>.png` — the page with red numbered
  boxes (the number is the box's `k`, drawn just outside its top-right
  corner). Look at it with the Read tool.

## Output — one file per page: `EPISODE_DIR/ocr/read/<stem>.json`

```json
{"file": "016.jpg",
 "blocks": {
   "0": {"lines": ["これが", "自動車か！", "話には", "きいたこと", "あるぞ"],
         "gloss": "So this is a car! I've heard about them."},
   "5": {"lines": ["へんなこと", "しないで", "しょうね！"],
         "gloss": "You're not going to do anything weird, right?!"},
   "14": {"lines": ["ん？"], "gloss": null},
   "9": {"lines": [], "gloss": null}
 },
 "missed": [],
 "notes": ""}
```

Rules:

- **Every box `k` in the manifest gets an entry.** `lines` = the printed
  lines exactly as lettered, in reading order: for vertical text the
  rightmost column first, then leftwards; top to bottom within a column.
  Keep the line breaks as printed (the reader lays the text back into the
  columns by character count). Keep punctuation as lettered (！ ？ … ー 〜
  「」). No spaces. **Never write furigana** — the small kana beside kanji
  are a reading aid, not text; write the kanji.
- A box that holds no real Japanese text — a scan-site watermark, a
  publisher logo, a page number, romaji on a sign — gets `"lines": []`.
  Lettered sound effects the box catches (ドン, ギィ…) are transcribed.
- Stutters and elongations as lettered (ヤ…ヤムチャ, ふ〜ん); dialect and
  rough speech as lettered (オラ, ねえ, だべ) — never "correct" the
  Japanese.
- `gloss`: one plain-English line saying what the speaker is saying or
  doing in this bubble, for any bubble a learner with ~4000 known words
  might not fully get — slang, contractions, dropped particles, ellipsis,
  dialect, wordplay, a sound word carrying the meaning, or simply a long
  sentence. Read the page as a whole so the gloss reflects who is talking
  to whom; mention a pun or a cultural reference briefly when it is the
  point. `null` for a bubble that needs nothing (a lone はい, ん？, a
  name). No grammar jargon, ever ("what the speaker is doing", not
  "causative-passive").
- `missed`: lettered dialogue on the page that has **no box at all**
  (rare) — list the text so it can be reported; it can't be laid out.
- If the image is unreadable for a box, transcribe what you can and note
  it in `notes`.

Practicalities the first readers settled on:

- The scans are ~764×1200; dense or stylised boxes are easier from a crop of
  the clean `pages/<stem>.jpg` upscaled 2–3× (Pillow is in `.venv`). The raw
  mokuro file `ocr/<stem>.json` has `lines_coords` per box when a column
  boundary is unclear.
- Nested / duplicate boxes on the same text: transcribe the text once in the
  outer (or first) box, `[]` in the other, and say so in `notes`. A box that
  catches only the tail of a neighbouring bubble's column gets just what is
  inside it, with the pairing noted.
- Large drawn sound effects are art, not `missed`; mention them in `notes`.
  `missed` is for lettered dialogue with no box.
- Keep a lone trailing column of punctuation (`！`, `…`) as its own line.

Write the JSON with the Write tool (UTF-8, `ensure_ascii` not needed).
Work page by page; do not skip pages; do not stop early — when your batch
is done, report how many pages you wrote and anything systematic you saw.
