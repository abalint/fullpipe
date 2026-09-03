# taste.md — immersion taste digest

*What the ratings show, at the confidence they actually support (100 rated episodes
as of 2026-08-03). This is what /recommend ranks against. Edit freely.*

## Read this first: what the instrument can and can't tell us

Each episode has **one** number, recorded **once**, minutes after watching. That
measurement carries mood, time of day, fatigue, and difficulty along with taste,
and nothing re-measures it later. 100 episodes sounds like a lot; split across two
dozen genres it is two to five episodes per vein — enough to form a hypothesis,
not enough to establish a rule.

Two consequences the ranking pass should respect:

- **★3 is the neutral midpoint, not a mild negative.** A cluster of ★3s means *no
  signal yet*, and must not be written up as a demotion. This matters more than it
  used to: the 2026-07/08 explore rounds returned a wall of ★3s (below), and
  reading that wall as rejection would undo the whole point of the explore lane.
- **Stated reasons outrank inferred patterns.** When a note says *why*
  ("all AI images made it unwatchable"), that's evidence about a mechanism and is
  usable at n=1. A bare star cluster with no stated reason is a correlation across
  a handful of episodes, and is not. The two are kept in separate sections below.

## What this file cannot see (the recommendation loop)

Every rating below was produced by watching something /recommend surfaced — and
/recommend ranked candidates against this file. The digest therefore describes the
**interior of a loop**. The two-lane restructure (2026-07-26) has begun prying it
open: 芸人企画, 検証, 大食い, せんべろ, 釣り, 鑑定, マジック, 街頭インタビュー,
スポーツ企画, ゲーム実況, ドッキリ, 開封 all had zero rated episodes when this file
was last written and now have one or two each. Still untouched: コント, 漫才,
魚捌き, 料理エンタメ, クイズ対決, 人狼/ボードゲーム, Vtuber雑談, 心霊, 動物密着,
再現レシピ, 筋トレ検証 and more (see the skill's `ATLAS.md`). **Absence of signal
here is absence of exposure, not absence of taste.** Two consequences:

- This file's jurisdiction is the **exploit lane** only. It must never be cited
  against an explore-lane pick — "doesn't look like what they rate high" is the
  box talking.
- "Fun" does not resolve to "the highest-rated veins below." The record skews
  edifying because that's what was fed in, not because the user chose docs over
  entertainment. When the user asks for fun, the honest answer is mostly outside
  this file.

## Stated reasons — trust these even at n=1

These come from the user's own notes or from axes that are near-tautological.

- **AI-generated imagery is disqualifying.** ★1 裁判傍聴記, note verbatim: "All AI
  images made it unwatchable. Interesting topic though." A good topic does not
  survive it. Same family as the synthetic-TTS veto.
- **Synthetic-TTS narration (ゆっくり/VOICEROID/ずんだもん) is a hard no** — stated
  directly, outside the ratings.
- **Low speech density kills a pick regardless of subject.** ★1 まぐろ船 ("Very low
  speech density"), ★2 東京発酵スポット ("Low language density"). Nothing to hear =
  nothing to mine. This is what the speech gate is for — and it now bites at
  selection time: two 2026-08-03 candidates were dropped for silence, and a
  thrift-shop vlog was dropped for 116 speech tokens in 15 minutes.
- **Bad audio or mumbled delivery caps the score.** ★1 高岡早紀 (audio_fidelity=1).
  Where `audio_fidelity` or `speech_clarity` ≤ 2, the star is ≤ 2 across the whole
  set — but note this is close to a tautology (can't hear it → can't rate it), so
  it's a floor, not a preference.
- **Difficulty is a ceiling, not a taste-negative.** ★4 銀座散歩, ★4 高島平, ★4
  哲学対話, ★3 成田×東, ★3 ドロヘドロ, ★2 楽焼 all have comprehension-dependent axes
  censored. The 哲学対話 note is the clearest statement: *"Liked the news broadcast
  style, but the language was very difficult."* Keep the vein; it was just hard.

## Hypotheses from the stars — treat as leads, not rules

Counts are given because they're small. None of these should veto a candidate on
its own.

- **Documentaries following one singular person** (n=4 high: ★5 みなみかわ, ★5 森の
  15歳, ★4 クマと坊さん, ★4 洋傘職人). Still the most-supported positive lead.
  Possible qualifier: four ★2s are 密着 packages about an *ordinary* subject
  (タケノコ, 旅館女将, 競輪, プロレス) — suggesting the person carries it, not the
  format. Consistent, still only n=4 vs n=4.
- **Atmospheric walking with a question driving it** (n=2 at ★5: ガマランド, 銀座;
  plus ★4 高島平). Small but the two ★5s are unambiguous.
- **廃村 / ruins with a human or historical hook** (★4 舟森, ★4 樫山 vs ★2 滝谷).
  n=3 — the hook/no-hook split is a plausible read of three data points, no more.
- **Religion as lived experience** (★4 修行と父子, ★4 仏像解説, ★4 潜伏キリシタン,
  ★3 般若心経, ★3 寺の一人娘, ★3 遍路, ★2 禅修行). The largest single vein at n=7,
  skewing positive, with most mass at ★3–4 rather than ★5.
- **Geography / topography explainers** (★4 坂道, ★4 高島平, ★4 福知山線). n=3.
- **Quirky one-off experiments** (★4 校庭, ★4 トド, ★4 カバン). n=3, all "loved_format."
- **Solo travel with a constraint or a long haul** (★4 九州秘境5000キロ, ★4 雨の
  車中泊, ★3 太平洋フェリー `follow=more`). n=3, new since the last revision — the
  *endurance/constraint* frame, distinct from the atmospheric-walking vein.
- **Group banter — three or more friends riffing — rates low** (★1 クレーンゲーム,
  ★2 陰キャ土産, ★2 狩野英孝ぶらり旅). n=3, and it lines up with the stated crosstalk
  dislike, so this probably belongs in the mechanism section, not here. Solo and
  duo formats doing the same activities did not rate low. Prefer solo/duo.
- **Q&A / お悩み相談 / 質問コーナー** (★2 佐野, ★2 同棲カップル, ★2 くまみき). n=3,
  consistent, presenter=1–2. The most consistent negative lead — but n=3 does not
  support calling it "the single most reliable" anything.
- **Low-stakes 雑談 talking-head vlogs** (★2 ミキティ×2, ★2 一般女, ★2 アラサー2人).
  n=4, all "didnt_grab." Note the counterexample: ★4 大物家具の処分費 (にんじんママ,
  presenter=4) is the same format with a **concrete problem** in it. The lead may
  be about stakes, not about talking heads.

## The explore rounds so far — mostly unresolved, and that's the expected result

Fifteen-odd episodes from the 2026-07-26 and 2026-08-02 explore lanes are now
rated. The distribution: **★4** 落語 (鈴ヶ森); **★3** 大食い, せんべろ, 釣り, 鑑定
(オーデマピゲ), マジック種明かし, 科学実験 (Dr.STONE), 街頭インタビュー (看護師給料),
スポーツ企画 (バッテリィズ×トクサン), ソロゲーム実況 (Star Fox), 検証 (サイゼリヤ
ビンゴ), ドッキリ (害悪アラーム); **★1–2** クレーンゲーム, 変なホテル, 芸人ぶらり旅.

Read this correctly: one clear positive, a large ★3 mass that says *nothing yet*,
and three low scores that share a mechanism (group banter / low speech density),
not a subject. Do not compress this into "entertainment doesn't work" — that
conclusion is not available from twelve ★3s, and drawing it would rebuild the box.

**落語 is the round's real find** (★4, first sample): scripted, projected,
single-voice performance — unusually mineable, and it belongs to a whole native
tradition with zero further sampling. Treat 落語/講談 as an active exploit vein now.

## No signal yet — do not read these as negatives

- **Traditional-craft (職人) documentaries.** ★4 洋傘職人, ★3 畳, ★3 加賀象嵌, ★3
  津軽塗, ★2 楽焼 (difficulty-censored). A pile at the neutral midpoint with one
  above and one explained-away below: **unresolved**, not weak. Keep surfacing them.
- **昭和レトロ walking / 喫茶めぐり.** Two episodes at ★3. Nothing to conclude.
- **Rail / 乗り鉄 travel.** ★2 宗谷本線, ★2 富山路面電車 — n=2. (★3 太平洋フェリー is
  not rail and carries `follow=more`; don't count it here.)
- **Insects / entomology.** ★1 虫プロ is the only entomology-proper episode. n=1.
- **Everything in the ★3 explore block above.** By construction.

## Channel intents on record — and a known bug

- `follow=more`: **1日見てもいいですか?**, **historica**, エンイチぶらり旅。
- `follow=less`: japantuna
- `follow=block`: **しごとリアル【しごりあ】dip公式**, **佐野勇斗だぞ**

**The しごとリアル block is a system artifact, not a taste fact.** That channel has
★5 (follow=`more`), ★2, ★2, ★2 (follow=`block`). `set_follow` is a last-write-wins
upsert on the channel row, so the later `block` tap *overwrote* the earlier `more`
— the ★5's channel signal was erased, not outvoted. `harvest seeds` now marks it
`block_overridden`, which withholds the hard veto and leaves it a strong
down-weight; don't seed RSS from it, but a candidate resembling the ★5 episode is
allowed. The 佐野勇斗だぞ block is clean by contrast (one episode, one tap, no
contradicting signal).

## Requested but unproven: debate / dialogue

Asked for explicitly on 2026-07-24 — real people arguing philosophy, religion, or
politics, **not** stage debates or party 討論. Supporting evidence is still thin:
★4 永井玲衣「哲学対話」 (topic score censored by difficulty), ★4 ゆる哲学ラジオ
「ハーメルンの笛吹き男」("fascinating"), ★3 成田悠輔×東浩紀 (difficulty-censored),
★3 神がいるって証明できる？. Four episodes, two of them difficulty-censored.

Cautions when ranking the genre: crosstalk is the suspected failure mode (prefer
turn-taking dialogue over panel shows), and coverage runs low. Early ratings here
will be difficulty-confounded; don't read a low star as a taste verdict.

## Presenter fingerprint (rolled up)

Warm and characterful, two flavors both welcome: (a) a singular *real subject*
captured by an enthusiastic, reaction-heavy off-camera interviewer, or (b) calm,
evocative documentary narration over strong footage. Tentatively a third: (c) two
knowledgeable people talking to each other as equals (ゆる哲学ラジオ, 対談).

Dislikes: low-energy solo talking-head with no stakes, fan-facing Q&A, fast
overlapping crosstalk, and — newly — three-or-more-friend banter. Hard nos:
synthetic-TTS narration and AI-generated imagery.
