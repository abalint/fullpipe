# taste.md — immersion taste digest

*What the ratings show, at the confidence they actually support (179 rated episodes
as of 2026-09-17; 79 of them added since the previous digest of 2026-08-03, almost
all in September). This is what /recommend ranks against. Edit freely.*

## Read this first: what the instrument can and can't tell us

Each episode has **one** number, recorded **once**, minutes after watching. That
measurement carries mood, time of day, fatigue, and difficulty along with taste,
and nothing re-measures it later. 179 episodes split across three dozen genres is
still two to eight episodes per vein — enough to form a hypothesis, and in one or
two places now enough to call a lead, not enough to establish a rule.

Two consequences the ranking pass should respect:

- **★3 is the neutral midpoint, not a mild negative.** 73 of 179 episodes sit
  there. A cluster of ★3s means *no signal yet*, and must not be written up as a
  demotion. The explore lane produces ★3 walls by design.
- **Stated reasons outrank inferred patterns.** When a note says *why* ("all AI
  images made it unwatchable"), that's evidence about a mechanism and is usable at
  n=1. A bare star cluster with no stated reason is a correlation across a
  handful of episodes, and is not. The two are kept in separate sections below.

## What this file cannot see (the recommendation loop)

Every rating below was produced by watching something /recommend surfaced — and
/recommend ranked candidates against this file. The digest therefore describes the
**interior of a loop**. The two-lane restructure (2026-07-26) has been prying it
open, and September's rounds are the proof: the two strongest veins in the whole
record — **audio drama** and **professional literary 朗読** — did not exist here on
2026-08-03. Both came out of the explore lane, and neither would have been
predicted by anything this file said at the time.

Since the last revision the explore lane has also sampled コント, 漫才, 大喜利,
すべらない話, 怪談語り, 浪曲, 魚捌き, 再現レシピ, 料理エンタメ, 人狼, Vtuber雑談, ゲーム
実況, 動物密着, 縛り生活, 美容師変身, 声優ラジオ, 芸能人ラジオ対談, 科学ポッドキャスト,
授業系解説, 海外在住vlog, 搭乗記, 街録 and the whole quiz family. Queued but still
unrated: 講談, 狂言, 深夜ラジオ, ラジオ人生相談, 芸人ポッドキャスト, COTEN RADIO, 将棋
解説, 公開講座, 医師解説, ソロ登山, マイクラ, 激辛, and the 2026-09-17 "expertise"
block (弁護士, 競馬, パチンコ, プロ野球, サッカー戦術, クラシック, 格ゲー大会, 家計, 家庭
菜園, 気象予報士). **Absence of signal here is absence of exposure, not absence of
taste.** Two consequences:

- This file's jurisdiction is the **exploit lane** only. It must never be cited
  against an explore-lane pick — "doesn't look like what they rate high" is the
  box talking. September's audio-drama find is exactly what that rule exists for.
- "Fun" does not resolve to "the highest-rated veins below." The record still
  skews toward what was fed in. When the user asks for fun, part of the honest
  answer is outside this file.

## The one pattern strong enough to call: the voice decides, the topic doesn't

This is the only finding in the file with enough mass behind it to use as a rule
rather than a lead. Over the 140 episodes where the presenter axis was tapped and
valid:

| presenter axis | n | mean ★ | range |
|---|---|---|---|
| 1 | 6 | 2.0 | all ★2 |
| 2 | 40 | 2.3 | ★1–3, never above ★3 |
| 3 | 52 | 3.0 | ★2–4 |
| 4 | 26 | 3.9 | ★3–5, never below ★3 |
| 5 | 16 | 4.4 | ★4–5, never below ★4 |

No episode with presenter ≤ 2 ever scored above ★3, and no episode with presenter
≥ 4 ever scored below ★3. `topic_pull` tracks the star too, but it leaks in both
directions: eight episodes with topic_pull=4 landed at ★3 (指詰め, せんべろ, 鑑定,
Dr.STONE, 街頭インタビュー, 般若心経, 歩荷, 神がいる), and six with topic_pull=3
landed at ★4 (福のラジオ, 推し燃ゆ, Crystal Kay, すごろく旅, コンカフェ嬢ドライブ,
本屋ひとりごこ). A subject the user cares about does not rescue a voice they don't
want to listen to, and a voice they like carries a subject they're neutral on.

Practical reading for the ranking pass: **rank on the speaker before the subject.**
A candidate from a channel whose presenter profile matches the fingerprint below is
a better bet than an on-topic candidate from an unknown voice. This is also why
the audio-drama vein works — every one of those is professional voice acting, and
`presenter`=5 on nine of the seventeen.

## Stated reasons — trust these even at n=1

These come from the user's own notes, from explicit asks logged in /recommend, or
from axes that are near-tautological.

- **AI-generated imagery is disqualifying.** ★1 裁判傍聴記, note verbatim: "All AI
  images made it unwatchable. Interesting topic though." A good topic does not
  survive it. Same family as the synthetic-TTS veto.
- **Synthetic-TTS narration (ゆっくり/VOICEROID/ずんだもん) is a hard no** — stated
  directly, outside the ratings.
- **Low speech density kills a pick regardless of subject.** ★1 まぐろ船 ("Very low
  speech density"), ★2 東京発酵スポット ("Low language density"). Nothing to hear =
  nothing to mine. The speech gate bites at selection time now.
- **Audio-only is wanted, in quantity.** 2026-09-03: "I've been enjoying the audio
  dramas." 2026-09-05: "I need about 40 hours of content, I want 20 hours of it to
  be audio based. audio dramas, podcasts, radio shows." Both stated; the ratings
  that followed back them up (below). Treat audio-native formats as a standing
  half of the diet, not a novelty lane.
- **Trivia / quiz shows were asked for** (2026-09-01) and **manga read aloud with
  discussion** (2026-09-01) and **GTA6 talk** (2026-08-23). Requested topics; see
  the quiz note under "difficulty is a ceiling."
- **Bad audio or mumbled delivery caps the score.** `audio_fidelity` ≤ 2 → mean
  ★2.2 over 21 episodes, never above ★3. Close to a tautology (can't hear it →
  can't rate it), so a floor, not a preference — but note that the three newest
  1日見てもいいですか? episodes all carry audio_fidelity=2 and all landed ★3.
- **Difficulty is a ceiling, not a taste-negative.** Fourteen episodes have
  difficulty=5 with the comprehension axes censored; their mean star (3.1) is no
  worse than the rest. The clearest cases now: ★5 荒木町散歩 (rated ★5 *with* the
  topic axis censored — the vein survives not understanding it), ★4 サンドウィッチ
  マン 漫才, and the whole quiz family (three of five at difficulty 5 — speed
  rounds and wordplay are hard by construction). Keep those veins; they were hard.

## Leads from the stars — ordered by how much data is behind them

Counts are given because they're small. Only the first two carry enough weight to
lean on; none should veto a candidate on its own.

- **Audio drama (オーディオドラマ / ラジオドラマ) — the strongest vein on record.**
  n=17: ★5 ×5, ★4 ×10, ★3 ×1, ★2 ×1, mean 4.1 against a whole-record mean of 3.1.
  Koto☆Hana alone is eight episodes, four ★5 and four ★4 (follow=more); オーディオ
  キネマ's 時代劇 ドラマCD is ★5 with `loved_format` and follow=more; the indie tier
  (シリアス中心声優団体 ★4 ×2, YoHaku.Lab ★4, happyhillmusic ★4, AudioMovie ★4) holds
  up; the 2002 broadcast archive 赤いバス is ★4. The two low scores are both the
  horror/psychological end (★2 令和版 夜のミステリー, ★3 1981 NHK 二階のある家) —
  n=2, don't over-read it, but ghost-story and 妖怪 folklore *did* rate ★5, so
  what they share is grim tone, not the supernatural. Sequels to a ★5 hold (イタコ
  探偵 ★5 → part 2 ★4; 雪原鉄道の夜 ★5, 雪原の夜行列車 ★4). Known cost from the
  atlas: uploader subtitles merge across speaker turns, so the deck yield is low
  — mine these for the prep doc and the popup, not the cards.
- **Professional literary 朗読.** n=2, both high: ★5 窪田等『セロ弾きのゴーシュ』
  (presenter 5, audio 5), ★4 梶裕貴『推し、燃ゆ』 (topic_pull only 3, presenter 4 —
  the voice carried it). Same shape as audio drama with one voice. Treat as an
  active exploit vein: named readers, single-work uploads.
- **Atmospheric walking with topography or history driving it.** ★5 荒木町 (楽待×
  ななすけ), ★5 ガマランド, ★5 銀座, ★4 高島平, ★4 坂道, ★4 らむ散歩 杉並バラック
  タウン, ★3 赤羽 (over_my_head), ★3 長崎 地形 (presenter 2). n=8, three ★5 and
  nothing below ★3. The strongest *video* vein, and the one that rides through
  difficulty. 楽待 RAKUMACHI and ななすけの散歩録 are the anchors.
- **Performed comedy — scripted or polished, solo or duo, on a stage or to
  camera.** ★4 東京03 コント (loved_format, presenter 5), ★4 サンドウィッチマン 漫才
  (difficulty-censored), ★4 粗品 エピソードトーク (loved_format), ★4 落語 鈴ヶ森
  (loved_format), ★3 一之輔 第十夜 (presenter 2, speech_clarity 2 — a covid-era
  streamed set, not a live 高座). n=5, four at ★4. Contrast the same comedians
  *riffing* rather than performing: ★2 かまいたち コンビニおにぎり (presenter 1),
  ★2 大喜利名回答 (presenter 1), ★2 狩野英孝ぶらり旅, ★3 霜降り明星 なぞなぞ, ★3
  バッテリィズ×トクサン. The split is material vs. improvised chatter, not the
  performer. 浪曲 (★2, sung, difficulty 4) did not carry over; 講談 and 狂言 are
  queued and unrated.
- **Produced radio and podcasts with a host steering the conversation.** ★5 池上彰
  (local file, follow=more), ★4 福山雅治 福のラジオ×大泉洋, ★4 フリーレン 声優ラジオ
  (presenter 5), ★4 荻上チキ Session 昭和100年, ★4 GOLDNRUSH Crystal Kay, ★4 サイエ
  ントーク, ★4 ゆる哲学 ハーメルン, ★4 Matt vs Japan; ★3 ゆる言語学, ★3 神がいる, ★3
  Root/B after-talk, ★3 アートのラジオ. n=12, eight at ★4+. Compare the unhosted
  "two creatives in conversation" shape: ★2 ラランド×又吉 (presenter 1), ★3 東出×
  角幡 (presenter 2), ★3 朝井リョウ, ★3 成田×東 (censored) — all difficulty 4–5. A
  host who runs the turn-taking seems to be the ingredient; see the mechanism
  section.
- **Solo travel with a constraint, a long haul, or a place worth the trip.** ★5
  隠岐諸島 (Rick Tanizaki — nature, geology, presenter 4), ★4 九州秘境5000キロ, ★4
  雨の車中泊, ★4 すごろく旅 鹿児島 (エンイチ, follow=more), ★3 太平洋フェリー ×2, ★3
  ソロキャンプ 四国カルスト (presenter 2). Against it: ★2 JAL ファーストクラス
  (presenter 1), ★2 羽田空港で丸一日, ★1 変なホテル. n=10, wide spread, and the
  spread is explained by the presenter axis, not by the travel.
- **Geography / topography explainers** (★4 坂道, ★4 高島平, ★4 福知山線廃線, ★5
  荒木町). Overlaps the walking vein; n=4, all high.
- **Quirky one-off experiments** (★4 校庭, ★4 トド, ★4 カバン; ★3 小屋DIY as a
  near-box sample). n=4, holding.
- **Religion as lived experience** (★4 修行と父子, ★4 仏像解説, ★4 潜伏キリシタン,
  ★3 般若心経, ★3 寺の一人娘, ★3 遍路, ★2 禅修行, ★2 小馬寺). n=8, most mass at
  ★3–4. Unchanged since last revision; no new samples.
- **Explainer / lecture to camera by one knowledgeable person** (★4 中田敦彦 北欧神
  話, ★4 仏像解説, ★4 にんじんママ 家具処分費, ★3 コヤミナティ 三億円事件 (censored,
  topic_pull 4), ★3 般若心経). n=5. 医師解説, 公開講座 and the 2026-09-17 expertise
  block will test this; nothing rated there yet.

## Leads that weakened — read before re-using the old digest

- **"Documentaries following one singular person" is no longer the most-supported
  positive lead; it is a ★3-median format.** Across the 密着 / one-subject
  documentary shape the record now holds about 32 episodes: five high (★5
  みなみかわ, ★5 森の15歳, ★4 クマと坊さん, ★4 修行と父子, ★4 洋傘職人), roughly
  sixteen at ★3 (江戸暮らしの百姓一家, 昭和大男, 猿人, 人口13人の夫婦, 美女江戸仙人,
  CODA, パン職人, 29歳漁師, 歩荷, 84歳年越し, 寺の一人娘, 色川地区, 離島漁師見習い,
  ヘルプマーク, 畳職人, 新人飼育員) and ten at ★2 (中村建設 社長, 保護猫, 銭湯最後の
  日, 青ヶ島運送, タケノコ, 旅館女将, 競輪, プロレス, 禅修行, 楽焼) plus ★1 まぐろ船.
  Mean ≈ 2.8, below the record. All five highs were rated in July; nothing in this
  shape has scored above ★3 since. The reading offered last time — the *person*
  carries it, not the format — is now the reading. Two cautions before treating
  that as taste: the three newest 1日見てもいいですか? episodes all have
  audio_fidelity=2 (outdoor handheld), so sound may be doing some of the damping;
  and the channel keeps follow=more, which stands. Surface its episodes when the
  subject is genuinely singular, not because the channel is on the list.
- **廃村 / ruins with a hook.** historica is now ★4 舟森, ★4 樫山, ★3 水荷浦, ★3 色川,
  ★2 滝谷, ★2 小馬寺. 小馬寺 had a temple-history hook and still landed ★2
  (topic_pull 2), so the hook/no-hook split doesn't hold at n=6. follow=more
  stands; the last four are ★2–3. Down-weight the median episode, keep the
  channel.
- **Group banter as the mechanism for low scores** needs reframing — see below.

## Mechanism notes (inferred, but consistent across many episodes)

- **It is turn discipline, not speaker count.** The 2026-08-03 note blamed
  three-or-more-friend banter. Since then: audio dramas with five or six voices
  are the best-rated thing in the record, and *duos* riffing unscripted rate ★2
  (かまいたち, バカリズム&佐久間, ラランド×又吉, 水溜りボンド). What the lows share
  is improvised, overlapping chat with nobody steering; what the highs share is a
  script, a host, or a performer holding the floor. The atlas's standing note
  ("take the solo or clean-turn-taking variant") still gives the right answer for
  the wrong reason: prefer *hosted or performed* over *riffing*, at any headcount.
- **Q&A / お悩み相談 / 質問コーナー and low-stakes 雑談 talking-heads** (★2 佐野, ★2
  同棲カップル, ★2 くまみき, ★2 ミキティ ×2 (one ★3), ★2 一般女, ★2 アラサー2人, ★2 Yu
  in London). n=8, nothing above ★3, every tapped one presenter ≤ 2. The counterexample
  stands: ★4 にんじんママ is the same format with a concrete problem in it. This is
  the same mechanism as above — no one is *doing* anything with the floor.
- **presenter=1 is a floor.** Six episodes, all ★2. When a channel profile reads
  as flat, sneering, or fan-facing, the topic doesn't matter.

## The explore rounds — what came back, and what's still open

Explore-lane picks rated since 2026-08-03 (cluster → result):

- **Clear positives (new exploit veins):** オーディオドラマ (★5, then a whole vein),
  朗読 (★5, ★4), コント (★4), 漫才 (★4), すべらない話 (★4), 声優ラジオ (★4), 芸能人
  ラジオ対談 (★4), 科学ポッドキャスト (★4), 授業系解説 中田 (★4), クイズ対決 QuizKnock
  (★4).
- **★3 wall — no signal yet, by construction:** 都市伝説考察 (censored, topic_pull
  4), 魚捌き, 動物園裏側, Vtuber雑談, 料理エンタメ リュウジ, 縛り生活 西成, 美容師
  変身, ラジオドキュメンタリー CBC, 大型DIY, 屋台グルメ 兄妹, ゲーム実況 花江, 謎解き
  duo / なぞなぞ / クイズ寄席 / 検定挑戦 (all censored), plus the 2026-07/08 block
  (大食い, せんべろ, 釣り, 鑑定, マジック, 科学実験, 街頭インタビュー, スポーツ企画,
  Star Fox, サイゼリヤビンゴ, ドッキリ).
- **Low, with a mechanism visible:** ★2 人狼 (censored, difficulty 5), ★2 再現
  レシピ (presenter 1), ★2 大喜利名回答 (presenter 1), ★2 ウーバー縛り生活 (duo
  riffing), ★2 街録ch (audio 2, speech 2), ★2 搭乗記 JAL (presenter 1), ★2 海外
  在住 talk-to-camera (雑談 shape), ★2 保護猫, ★2 浪曲 (sung, difficulty 4), and the
  older ★1 クレーンゲーム, ★1 変なホテル, ★2 芸人ぶらり旅.
- **The one cluster with two consistent lows and no clean explanation: 怪談語り.**
  ★2 稲川淳二 (difficulty 5, topic censored but topic_pull 4 valid, presenter 2) and
  ★2 夜馬裕 (presenter 2, difficulty 4). n=2, both hard, both presenter 2. A lead
  that this is a *delivery* mismatch (breathy, theatrical solo telling), not a
  subject one — ghost stories in audio-drama form rated ★5. Not a veto; the next
  怪談師 sample should be a cleaner teller, and if that also lands ★2, call it.

Read the whole block correctly: the explore lane's hit rate is real (ten new ★4–5
veins out of roughly forty clusters tried), the ★3 wall is the expected cost, and
the lows are mostly the mechanism section again, not subjects.

## No signal yet — do not read these as negatives

- **Traditional-craft (職人) documentaries.** ★4 洋傘職人, ★3 畳, ★3 加賀象嵌, ★3
  津軽塗, ★2 楽焼 (censored). Unchanged; still unresolved.
- **昭和レトロ walking / 喫茶めぐり.** Two at ★3; ★3 須磨 added (presenter 4,
  audio 5 — a clean voice that still didn't grip). Nothing to conclude.
- **Rail / 乗り鉄 travel.** ★2 宗谷本線, ★2 富山路面電車 — n=2, both `didnt_grab`.
  Ferries are not rail (★3 ×2, follow=more on エンイチ).
- **Insects / entomology.** ★1 虫プロ is the only sample. n=1.
- **Cooking.** ★3 リュウジ, ★3 焼き飯, ★3 ナポレオンフィッシュ, ★2 漫画飯, ★1 高岡早紀
  (audio 1). Mean 2.4 but every low has a mechanism (presenter 1, audio 1). Open.
- **Everything in the ★3 explore wall above.** By construction.

## Channel intents on record

- `follow=more`: **Koto☆Hana【Audio drama】** (new), **オーディオキネマ** (new),
  **1日見てもいいですか?**, **historica**, **エンイチぶらり旅。**; episode-level
  `more` also on 池上彰 (local file, no channel row) and on the しごとリアル ★5.
- `follow=less`: japantuna.
- `follow=block`: **佐野勇斗だぞ** (clean: one episode, one tap), **ベアチャンネル**
  (new — the 統一教会座談会, ★1, no note; see the debate section), and
  **しごとリアル【しごりあ】dip公式** (overridden).

**The しごとリアル block is a system artifact, not a taste fact.** That channel has
★5 (follow=`more`), ★2, ★2, ★2 (follow=`block`). `set_follow` is a last-write-wins
upsert, so the later `block` overwrote the earlier `more`. `harvest seeds` marks it
`block_overridden`, which withholds the hard veto and leaves a strong down-weight;
don't seed RSS from it, but a candidate resembling the ★5 episode is allowed.

## Requested but only partly proven: debate / dialogue

Asked for explicitly on 2026-07-24 — real people arguing philosophy, religion, or
politics, **not** stage debates or party 討論. What the record now says:

- The *hosted* version works: ★4 荻上チキ Session (辻田真佐憲 on 昭和100年), ★4
  永井玲衣 哲学対話 (censored; "Liked the news broadcast style, but the language was
  very difficult"), ★4 ゆる哲学ラジオ ハーメルン, ★3 神がいるって証明できる？.
- The *unhosted* two-thinkers version has not: ★3 成田×東 (censored), ★3 東出昌大×
  角幡唯介 (presenter 2), ★3 朝井リョウ, ★2 ラランドニシダ×又吉 (presenter 1). All
  difficulty 4–5.
- The one sample of literal religious argument — ★1 統一教会座談会 (believer vs.
  non-believer, ベアチャンネル) — got a channel block with no note. n=1, mechanism
  unknown; do not generalize it to the genre, but don't resurface that channel.

Ranking guidance: seek the broadcast-produced shape (a host, a guest, an editor)
and treat coverage as the limiting factor. Early low stars here remain
difficulty-confounded.

## Presenter fingerprint (rolled up)

Three flavors, all confirmed by September's data:

- (a) **A performed voice** — professional voice actors in an ensemble, a single
  narrator reading prose, a comedian delivering material, a 落語家 on the 高座.
  Clean, projected, enunciated, with a script or a routine behind it. This is the
  flavor the record now rates highest, and it is almost entirely audio.
- (b) **Calm, evocative narration or a curious walker over strong footage** —
  地形 walks, island travel, quirky experiments. Solo, unhurried, driven by a
  question or a place.
- (c) **A host running a conversation with one guest or one partner** — radio
  interviews, 声優 web radio, duo podcasts with a structure. Two knowledgeable
  people talking *to each other* under someone's steering.

Dislikes: flat or sneering delivery (presenter 1 is a floor), fan-facing Q&A,
low-stakes 雑談 with nothing at stake, improvised riffing without a host at any
headcount, and breathy theatrical solo telling (怪談, tentative). Hard nos:
synthetic-TTS narration and AI-generated imagery.
