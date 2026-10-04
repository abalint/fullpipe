# The AI read — instructions for a reading agent

You are transcribing manga pages for a language-learning reader. mokuro (a
small OCR model) has already **boxed** every text region on each page and
numbered the boxes; its own reading of the text is a **draft that is often
wrong** (furigana merged into words, パ read as ハ, rare kanji swapped,
stutters merged). You read the page image yourself — the image is the truth —
and write, per box, the printed lines and what the bubble means.

## Inputs

- `EPISODE_DIR/ocr/read/manifest.json` — `pages[]`: `{n, file, stem, image,
  blocks: [{k, box, vertical, draft}]}`, and `cast`: the series cast so far
  (`name → {kana, gender, age, note}`; use these names). Your batch is a
  list of `stem`s.
- `EPISODE_DIR/ocr/read/pages/<stem>.png` — the page with red numbered
  boxes (the number is the box's `k`, drawn just outside its top-right
  corner). Look at it with the Read tool.

## Output — one file per page: `EPISODE_DIR/ocr/read/<stem>.json`

```json
{"file": "016.jpg",
 "blocks": {
   "0": {"lines": ["これが", "自動車か！", "話には", "きいたこと", "あるぞ"],
         "gloss": "So this is a car! I've heard about them.",
         "speaker": "悟空", "say": "これが自動車か！話にはきいたことあるぞ"},
   "5": {"lines": ["へんなこと", "しないで", "しょうね！"],
         "gloss": "You're not going to do anything weird, right?!",
         "speaker": "ブルマ", "say": "へんなことしないでしょうね！"},
   "14": {"lines": ["ん？"], "gloss": null, "speaker": "悟空", "say": "ん？"},
   "9": {"lines": [], "gloss": null, "speaker": null, "say": null}
 },
 "cast": [
   {"name": "悟空", "kana": "ごくう", "gender": "male", "age": "child",
    "dialect": "inaka", "note": "the hero; a wild boy with a tail; says オラ"},
   {"name": "ブルマ", "kana": "ぶるま", "gender": "female", "age": "teen",
    "note": "city girl hunting the dragon balls"}
 ],
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
- `gloss`: a **translation of the bubble** — the line as the character
  says it, in natural English, the way the licensed English edition would
  letter it: first/second person kept (あんたのそういうとこマジウザイ →
  "That's what's so damn annoying about you."), never reported speech or
  narration ("She finds his attitude annoying." / "He apologizes." are
  wrong). Keep the tone — rude stays rude, stiff stays stiff. Give one
  for any bubble a learner with ~4000 known words might not fully get —
  slang, contractions, dropped particles, ellipsis, dialect, wordplay, a
  sound word carrying the meaning, or simply a long sentence. Read the
  page as a whole so pronouns and "you" land on the right person. Only
  after the translation, and only when it is the point, add a short
  parenthetical note (a pun, a cultural reference, what a slang word
  literally is): "Seriously annoying. (マジ = seriously; ウザい =
  irritating)". A sound effect, sign, chapter title or credit may be
  given in brackets instead ("[door slams]", "[Chapter 3: …]"). `null` for a bubble that needs
  nothing (a lone はい, ん？, a name). No grammar jargon, ever.
- **The voice fields** (`speaker`, `say`, `cast`) — the volume is also
  read aloud bubble by bubble by a Japanese text-to-speech voice, one
  clip per box, with a voice per character (tools/manga_voice.py). You
  are the one who sees the page, so you say who speaks and what the
  voice should read:
  - `speaker`: the cast name — the name the manga calls them (センパイ,
    オカルン, ターボババア), the same string on every page. Use the names
    in the manifest's `cast` (the series cast so far) when the character
    is there. `null` for narration/captions, signs, titles, sound effects
    and anything no character says; `"モブ"` for an unnamed extra or a
    crowd. Add every character you name to the file's `cast` (once per
    file): `kana` = how the name is read, `gender` = `female` / `male` /
    `neutral`, `age` = `child` / `teen` / `adult` / `old`, `dialect` =
    how they talk when it is marked — `kansai`, `kyushu`, `tohoku`,
    `inaka` (unspecified rural), `rough` (yakuza / delinquent speech),
    `archaic` (〜じゃ, ワシ, old-person or samurai speech) — or `null` for
    standard speech; `note` = who they are in a few words. The voice for
    an uncast character is chosen from gender + age + dialect, so an
    old woman who says ワシ…じゃ should be `age: "old", dialect: "archaic"`.
  - `say`: the text the voice reads — the bubble as lettered, changed only
    where the voice would stumble or misread: **every proper name in
    kana** as it is read (長瀞さん → ながとろさん, 高倉健 → たかくらけん,
    the furigana tells you — this is the one place the furigana IS used);
    ordinary words stay in kanji. Stutters as a repeat with a pause
    (だ…だから → だ、だから); a stutter that is only lettering noise
    (ち…ち…ちょっと) → the word. One ー for a drawn-out vowel (ええええ →
    ええ, 〜〜〜 → nothing). Keep 。！？、… (the voice uses them); drop
    「」♡ ♪ ☆. A bubble with two sentences is one `say`. Never translate,
    never fix the grammar, never add words. `null` when nothing should be
    read: a sound effect no one says (ドン, ガタッ, ザワザワ — a spoken
    noise like へっ, うっ, はぁ, ぎゃー is a line), a sign, a chapter
    title, a page number, credits, an empty box.
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
is done, report how many pages you wrote, the cast names you used, and
anything systematic you saw.
