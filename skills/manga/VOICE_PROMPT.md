# The voice script — instructions for a script agent

You are writing the voice script for a manga volume: for every speech
bubble, **who says it** and **exactly what text a Japanese text-to-speech
voice should read aloud**. The reader on the phone plays one short clip per
bubble when the learner taps it. You work from text only (the AI read's
transcription and bubble glosses); you do not see the pages.

## Inputs

- `EPISODE_DIR/voice/manifest.json` — `synopsis` (who the people are),
  `cast` (the series cast so far: `name → {kana, gender, age, note,
  voice_id}` — **use these names verbatim**), and `pages[]`:
  `{n, file, stem, blocks: [{k, sents, text, gloss}]}` in reading order.
  `text` is the bubble as lettered; `gloss` is the licensed-translation
  style meaning (it often says who is speaking to whom). Your batch is a
  list of `stem`s.

## Output — one file per page: `EPISODE_DIR/voice/pages/<stem>.json`

```json
{"file": "013.jpg",
 "blocks": {
   "0": {"speaker": "長瀞", "say": "要するにこれ"},
   "1": {"speaker": "長瀞", "say": "ジークフリートってセンパイ自身ですよね？"},
   "2": {"speaker": "センパイ", "say": "いいや…そんなことは…"},
   "4": {"speaker": "センパイ", "say": "だ、だからそんなんじゃないって…"},
   "7": {"speaker": null, "say": null}
 },
 "cast": [
   {"name": "長瀞", "kana": "ながとろ", "gender": "female", "age": "teen",
    "note": "first-year; teases Senpai; the title character"},
   {"name": "センパイ", "kana": "せんぱい", "gender": "male", "age": "teen",
    "note": "shy second-year artist; real name 八雲 (はちおうじ) is rarely used"}
 ]}
```

Rules:

- **Every block `k` in the manifest page gets an entry.**
- `speaker`: the cast name. Use the name from `cast` when the character is
  already there; otherwise the name the manga uses for them (the name
  people call them — センパイ, not a real name nobody says). `null` for
  narration, captions, signs, titles and anything not spoken by a
  character; for a crowd / unnamed extra use `"モブ"`. Pick from the
  context — the gloss, the page, who was just talking — never leave a
  real character's line as `null` just because it is short. Add every
  character you name to `cast` (once per file is enough): `kana` is the
  reading of the name, `gender` is `female` / `male` / `neutral`, `age`
  is `child` / `teen` / `adult` / `old`.
- `say`: what the voice reads. Start from `text` as lettered and change
  only what would make the voice stumble or misread:
  - **Names in kana.** Every proper name — people, places, invented
    words — is written in hiragana/katakana as it is read
    (長瀞さん → ながとろさん, 八雲 → はちおうじ, 瀞 alone → とろ). Common
    words stay in kanji: TTS reads ordinary Japanese well; it is names it
    guesses. When the reading is uncertain, choose the one the series
    uses (furigana in the manga, the anime) and note it in the cast `note`.
  - **Stutters** as lettered when they are a plain repeat with a pause
    (だ…だから → だ、だから); drop a stutter that is only lettering noise
    (ちょ、ちょっと is fine; ち…ち…ちょっと → ちょっと if it would be read as
    gibberish). The point is that the clip sounds like the line said by
    that person.
  - **Elongations**: keep one ー (えーっ, ふーん); collapse runs
    (ええええええ → えええ, 〜〜〜 → one 〜 or nothing).
  - **Punctuation**: keep 。！？、… — the voice uses them for pauses and
    tone. Drop 「」 brackets and ♡ ♪ ☆ symbols (or turn ♪ into nothing).
  - **Sound effects and noises** that a person would not say (ドン, ガタッ,
    ザワザワ, a drawn ギィ) → `"say": null`. A spoken noise (へっ, うっ,
    はぁ…, ぎゃー) is a line: keep it.
  - **Signs, chapter titles, page numbers, credits, the publisher's text**
    → `null`.
  - A bubble with two sentences stays one `say` (the clip is the bubble).
  - Never translate, never "correct" the grammar, never add words the
    bubble does not have; dialect and rough speech stay as they are.
- A mis-transcription you are sure of (the read is Opus's, so rare): fix
  the reading in `say` only; the printed `text` is not yours to change.

Write the JSON with the Write tool (UTF-8). Work page by page; do not skip
pages; when your batch is done, report how many pages you wrote and the
cast names you used (so the other agents' names can be reconciled).
