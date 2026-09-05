"""SudachiPy SplitMode C tokenization + coverage analysis.

Replaces audioPrime's fugashi-based lemma_analyzer. The whole workstation
standardizes on one tokenizer (DESIGN.md — Reuse map): SudachiPy SplitMode C,
with dictionary_form() as the ledger join key, normalized_form() for
variant-aware known matching (こもる counts as known when 籠る is known — both
normalize to 籠もる), and kanji-stem matching to expand the known set.

POS filters and card-worthiness rules come from the sentence-mining skill,
where they are already validated against real mined decks. Vocabulary
membership additionally excludes proper nouns, numerals, and auxiliary-
position 非自立可能 verbs (vocab_indices) — Sudachi tags all three, and
counting them as unknown "words" was the dominant noise source in coverage
(presenter surnames were the corpus's two most frequent unknowns).
"""

import re
from collections import namedtuple

# SudachiPy top-level POS categories (part_of_speech()[0]). Current
# sudachidict_core DOES emit 形状詞 for na-adjective stems (綺麗/静か/頑丈) —
# the sentence-mining skill's claim that they surface as 名詞 is stale, and
# omitting it silently drops na-adjectives from mining and the ledger.
CONTENT_POS_PREFIXES = ("名詞", "動詞", "形容詞", "形状詞", "副詞", "連体詞", "感動詞")
SKIP_POS_PREFIXES = ("助詞", "助動詞", "補助記号", "記号", "空白", "接尾辞", "接頭辞", "代名詞", "フィラー")

# part_of_speech()[1] subtypes that are never learnable vocabulary. Names
# (固有名詞) were the corpus's top "unknown words" — 深井/樋口 alone
# misclassified hundreds of sentences — and numerals (数詞: 90万, 30) carry
# no vocab signal. Counters stay (助数詞可能 is pos[2]; ヶ月/度 are learnable).
NON_VOCAB_POS2 = ("固有名詞", "数詞")

# 動詞,非自立可能 — verbs that double as grammar. In auxiliary position
# (immediately after て/で or an auxiliary: やってくれる, 夢でございます,
# 食べてしまう) they are grammar, not vocabulary; as main verbs
# (ジャガイモくれた, 時間がかかる) they still count.
AUX_CAPABLE_POS2 = "非自立可能"
# Always grammar regardless of position: ございます/ではございません is
# politeness morphology, and the preceding token varies too much (感動詞,
# 係助詞…) for the positional rule to catch it.
GRAMMAR_VERB_LEMMAS = frozenset({"ござる"})

PURE_DIGITS = re.compile(r"^[0-9０-９]+$")
PURE_ASCII = re.compile(r"^[A-Za-z]+$")
PUNCT_ONLY = re.compile(r"^[!?！？。、,\.…\-—~〜]+$")
SINGLE_KANA = re.compile(r"^[぀-ゟ゠-ヿー]$")
KANA_ONLY_SHORT = re.compile(r"^[぀-ゟ゠-ヿー]{1,2}$")
HAS_KANJI = re.compile(r"[一-鿿]")
KANJI_RE = re.compile(r"[一-鿿々]+")

HTML_TAG_RE = re.compile(r"<[^>]+>")
FURIGANA_RE = re.compile(r"([一-龯々]+)\[([ぁ-ゖァ-ヺー]+)\]")

Token = namedtuple("Token", "surface lemma normalized reading pos pos2",
                   defaults=[""])

_sudachi_tokenizer = None
_sudachi_split = None


def _sudachi():
    """Lazily build a single SudachiPy tokenizer (SplitMode C) shared across calls."""
    global _sudachi_tokenizer, _sudachi_split
    if _sudachi_tokenizer is None:
        from sudachipy import dictionary, tokenizer
        _sudachi_tokenizer = dictionary.Dictionary(dict="core").create()
        _sudachi_split = tokenizer.Tokenizer.SplitMode.C  # longest units — best for vocab
    return _sudachi_tokenizer, _sudachi_split


def tokenize(text):
    """Tokenize with SudachiPy (SplitMode C — keeps meaningful compounds whole,
    e.g. 警察官/被写体 as single words rather than 警察+官).

    Returns Token(surface, lemma, normalized, reading, pos, pos2) per token:
      lemma      = dictionary_form()   (走った→走る; keeps surface orthography)
      normalized = normalized_form()   (collapses spelling variants: こもる/籠る→籠もる)
      reading    = reading_form()      (katakana)
      pos        = part_of_speech()[0] (top-level category, e.g. 名詞/動詞)
      pos2       = part_of_speech()[1] (subtype: 固有名詞/数詞/非自立可能/普通名詞…)
    """
    tok, mode = _sudachi()
    out = []
    for m in tok.tokenize(text, mode):
        p = m.part_of_speech()
        out.append(Token(
            m.surface(),
            m.dictionary_form(),
            m.normalized_form(),
            m.reading_form(),
            p[0],
            p[1],
        ))
    return out


def strip_html(s):
    s = HTML_TAG_RE.sub("", s.replace("<br>", "\n").replace("<br/>", "\n"))
    return s.replace("　", " ").replace("\xa0", " ").strip()


def strip_furigana(s):
    # Anki furigana lives as `漢字[かんじ]`; drop the reading so the tokenizer sees clean text.
    return FURIGANA_RE.sub(r"\1", s)


def is_content_word(pos, pos2=""):
    if any(pos.startswith(p) for p in SKIP_POS_PREFIXES):
        return False
    if pos2 in NON_VOCAB_POS2:
        return False
    return any(pos.startswith(p) for p in CONTENT_POS_PREFIXES)


def is_card_worthy(lemma):
    """Filter out lemmas that carry no learnable-vocabulary signal."""
    if not lemma:
        return False
    if PURE_DIGITS.match(lemma) or PURE_ASCII.match(lemma) or PUNCT_ONLY.match(lemma):
        return False
    if SINGLE_KANA.match(lemma):
        return False
    if not HAS_KANJI.search(lemma) and KANA_ONLY_SHORT.match(lemma):
        return False
    return True


def kata_to_hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _is_kana(c):
    return "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヶ" or c == "ー"


def _extend_segs(segs, token_segs):
    """Append [chunk, reading-or-None] pairs, merging adjacent bare chunks."""
    for chunk, reading in token_segs:
        if not chunk:
            continue
        if reading is None and segs and segs[-1][1] is None:
            segs[-1][0] += chunk
        else:
            segs.append([chunk, reading])


def token_segs(surface, reading):
    """[chunk, reading-or-None] segments for ONE token, furigana over the
    kanji core only: matching leading/trailing kana — okurigana like す・る・
    い — are peeled off both sides so the rt sits on the kanji, not the kana.
    Chunks concatenate back to `surface` exactly.

    e.g. (通す, とおす) → [[通,とお],[す,None]]   (切ない, せつない) →
         [[切,せつ],[ない,None]]   (大丈夫, だいじょうぶ) → [[大丈夫,だいじょうぶ]]
    """
    if not reading or not HAS_KANJI.search(surface):
        return [[surface, None]]
    s, r = surface, kata_to_hira(reading)
    tail = ""
    while s and r and _is_kana(s[-1]) and s[-1] == r[-1]:
        tail = s[-1] + tail
        s, r = s[:-1], r[:-1]
    head = ""
    while s and r and _is_kana(s[0]) and s[0] == r[0]:
        head += s[0]
        s, r = s[1:], r[1:]
    if s and r and HAS_KANJI.search(s):
        core = [s, r]
    else:
        core = [s, None]  # reading didn't align to a clean kanji core — bare
    return [[head, None], core, [tail, None]]


def furigana(word):
    """[surface, reading-or-None] segments for a single word, with furigana over
    the KANJI ONLY (token_segs per Sudachi token, dictionary readings — so the
    caller need not supply an inflected surface's reading). Segment texts
    concatenate back to `word` exactly; kana-only tokens carry no reading.

    e.g. 通す → [[通,とお],[す,None]]   行く → [[行,い],[く,None]]
         大丈夫 → [[大丈夫,だいじょうぶ]]   くれる → [[くれる,None]]
    """
    segs = []
    for t in tokenize(word):
        reading = kata_to_hira(t.reading) if t.reading else None
        _extend_segs(segs, token_segs(t.surface, reading))
    return segs


def extract_kanji_stem(lemma):
    m = KANJI_RE.match(lemma)
    return m.group(0) if m else ""


def _is_aux_position(tokens, i):
    """True when a 非自立可能 verb at index i is functioning as grammar:
    a hard-listed grammar verb, or directly after て/で (接続助詞) or an
    auxiliary (でございます' で lemmatizes to 助動詞 だ)."""
    if tokens[i].lemma in GRAMMAR_VERB_LEMMAS:
        return True
    if i == 0:
        return False
    prev = tokens[i - 1]
    return (prev.pos.startswith("助動詞")
            or (prev.pos.startswith("助詞") and prev.pos2 == "接続助詞"))


def vocab_indices(tokens, non_vocab=frozenset()):
    """Indices of tokens that count as learnable vocabulary: content POS,
    card-worthy lemma, not a proper noun / numeral (NON_VOCAB_POS2), and not
    an auxiliary-capable verb in auxiliary position. Positional — pass the
    FULL token sequence of the sentence, not a pre-filtered one.

    non_vocab: episode-level exclusions by lemma or surface — the AI repair
    pass's adjudications (names Sudachi's dictionary doesn't know, ASR
    non-words), loaded from repair.json by coverage."""
    return [i for i, t in enumerate(tokens)
            if is_content_word(t.pos, t.pos2) and is_card_worthy(t.lemma)
            and t.lemma not in non_vocab and t.surface not in non_vocab
            and not (t.pos2 == AUX_CAPABLE_POS2 and _is_aux_position(tokens, i))]


def content_tokens(text):
    """Content-word, card-worthy tokens of *text* — the vocabulary signal."""
    toks = tokenize(text)
    return [toks[i] for i in vocab_indices(toks)]


class KnownSet:
    """Variant-aware membership test for 'does the learner know this token?'.

    known        set of dictionary_form lemmas
    norm_known   set of normalized_form values (spelling-variant expansion)
    known_stems  set of kanji stems of known lemmas (stem-match expansion)
    phrases      {JMdict headword: ledger status} — the tracked multi-token
                 phrases (GRAMMAR.md). Matched as units by phrase_units();
                 never part of token membership.
    """

    def __init__(self, known, norm_known=frozenset(), known_stems=frozenset(),
                 phrases=None):
        self.known = set(known)
        self.norm_known = set(norm_known)
        self.known_stems = set(known_stems)
        self.phrases = dict(phrases or {})
        self._phrase_index = None  # first lemma -> [(headword, lemma_seq)], lazy

    def __contains__(self, token):
        if token.lemma in self.known:
            return True
        if token.normalized and token.normalized in self.norm_known:
            return True
        stem = extract_kanji_stem(token.lemma)
        return bool(stem) and stem in self.known_stems

    def __len__(self):
        return len(self.known)

    def phrase_units(self, tokens):
        """Occurrences of tracked phrases in a full token sequence (particles
        included — 気を付ける needs its を). Matching is deterministic and
        inflection-proof: the headword's own lemma sequence against the
        sentence tokens' lemmas (気を付けて → 気|を|付ける|て matches
        気|を|付ける). Greedy left-to-right, longest match first,
        non-overlapping. Returns [{phrase, status, start, end}] with
        start/end indexing *tokens* (end exclusive)."""
        if not self.phrases:
            return []
        if self._phrase_index is None:
            idx = {}
            for hw in self.phrases:
                seq = tuple(t.lemma for t in tokenize(hw))
                if len(seq) < 2:
                    continue  # single-token keys are words, not phrases
                idx.setdefault(seq[0], []).append((hw, seq))
            for cands in idx.values():
                cands.sort(key=lambda c: -len(c[1]))
            self._phrase_index = idx
        lemmas = [t.lemma for t in tokens]
        units = []
        i = 0
        while i < len(lemmas):
            for hw, seq in self._phrase_index.get(lemmas[i], ()):
                if tuple(lemmas[i:i + len(seq)]) == seq:
                    units.append({"phrase": hw,
                                  "status": self.phrases.get(hw, "unknown"),
                                  "start": i, "end": i + len(seq)})
                    i += len(seq) - 1
                    break
            i += 1
        return units


def phrase_span(tokens, canonical, surface=None):
    """Token range [start, end) a phrase occupies in a coverage sentence's
    token list (dicts carrying "l"/"s"), or None when it can't be placed.

    Lemma-sequence match first — the rule KnownSet.phrase_units applies at
    Stage 1, so 血が騒いだ finds 血|が|騒ぐ whatever the inflection — then
    the curate-emitted surface as a fallback (joined token surfaces,
    whitespace tokens transparent) for headwords the tokenizer splits
    differently than JMdict spells them. The player paints the span as one
    unit and the popup keys its phrase layer on it (GRAMMAR.md)."""
    lemmas = [t.get("l") for t in tokens]
    try:
        seq = [t.lemma for t in tokenize(canonical)]
    except Exception:  # tokenizer unavailable — surface only
        seq = []
    n = len(seq)
    if n >= 2:
        for i in range(len(lemmas) - n + 1):
            if lemmas[i:i + n] == seq:
                return i, i + n
    target = re.sub(r"\s+", "", surface or "")
    if not target:
        return None
    for i in range(len(tokens)):
        if not (tokens[i].get("s") or "").strip():
            continue
        acc = ""
        for j in range(i, len(tokens)):
            acc += (tokens[j].get("s") or "").strip()
            if acc == target:
                return i, j + 1
            if not target.startswith(acc):
                break
    return None


def analyze_sentence(text, known_set, learning=frozenset(), non_vocab=frozenset()):
    """Classify one sentence against the materialized known set.

    Args:
        text: the sentence
        known_set: a KnownSet (or anything supporting `token in x`)
        learning: set of lemmas currently in 'learning' (counts as not-known
                  for i+1, but flags the sentence as reinforcement)
        non_vocab: lemmas/surfaces adjudicated non-vocabulary (repair.json)

    Returns dict with tokens, unknown lists, known_ratio, and the four-way
    classification DESIGN.md's coverage analysis uses:
      comprehensible  — all units known (counts for exposure)
      reinforcement   — exactly one unknown and it is `learning`
      i_plus_1        — exactly one unknown, truly unknown (mining candidate)
      too_hard        — two or more unknown units

    Unknowns are counted over UNITS (GRAMMAR.md — i+1 with phrases): a tracked
    phrase the sentence contains is one unit whose known-ness is its ledger
    status, and its component tokens leave the tally — a line that is "one
    known phrase away" is i+1. Token-level fields (tokens / unknown /
    known_ratio) keep their word-level meaning; unknown_lemmas / unknown_count
    are unit-level (phrase keys included).
    """
    all_tokens = tokenize(text)
    content_idx = vocab_indices(all_tokens, non_vocab)
    tokens = [all_tokens[i] for i in content_idx]
    unknown = [t for t in tokens if t not in known_set]

    phrase_units = (known_set.phrase_units(all_tokens)
                    if hasattr(known_set, "phrase_units") else [])
    covered = set()
    for u in phrase_units:
        covered.update(range(u["start"], u["end"]))

    unknown_units = {t.lemma for i, t in zip(content_idx, tokens)
                     if i not in covered and t not in known_set}
    learning_units = unknown_units & set(learning)
    for u in phrase_units:
        if u["status"] == "known":
            continue
        unknown_units.add(u["phrase"])
        if u["status"] == "learning":
            learning_units.add(u["phrase"])

    known_count = len(tokens) - len(unknown)
    known_ratio = known_count / len(tokens) if tokens else 1.0

    if not unknown_units:
        classification = "comprehensible"
    elif len(unknown_units) == 1:
        the_key = next(iter(unknown_units))
        classification = "reinforcement" if the_key in learning_units else "i_plus_1"
    else:
        classification = "too_hard"

    return {
        "text": text,
        "tokens": tokens,
        "unknown": unknown,
        "phrases": phrase_units,
        "unknown_lemmas": sorted(unknown_units),
        "unknown_count": len(unknown_units),
        "known_ratio": known_ratio,
        "classification": classification,
    }


def analyze_transcript(sentences, known_set, learning=frozenset(),
                       non_vocab=frozenset()):
    """Analyze all sentences of an episode for coverage.

    Args:
        sentences: list of (start_sec, end_sec, text) from the SRT parser
        known_set: KnownSet from ledger materialize-known
        learning: lemmas in 'learning' status
        non_vocab: lemmas/surfaces adjudicated non-vocabulary (repair.json)

    Returns dict with per-sentence details (each carries index/start/end and
    the analyze_sentence fields) plus summary stats and the exposure payload
    shape record-exposure expects: every content lemma with its sentence
    context (known_ratio, other_unknown_count).
    """
    details = []
    counts = {"comprehensible": 0, "reinforcement": 0, "i_plus_1": 0, "too_hard": 0}
    total_tokens = 0
    known_tokens = 0
    exposures = {}  # lemma -> best (lowest other_unknown_count) context

    for idx, (start, end, text) in enumerate(sentences):
        d = analyze_sentence(text, known_set, learning, non_vocab)
        d["index"] = idx
        d["start"] = start
        d["end"] = end
        details.append(d)
        counts[d["classification"]] += 1
        total_tokens += len(d["tokens"])
        known_tokens += len(d["tokens"]) - len(d["unknown"])

        for t in d["tokens"]:
            other_unknown = d["unknown_count"] - (1 if t.lemma in d["unknown_lemmas"] else 0)
            ctx = {
                "sentence_idx": idx,
                "known_ratio": round(d["known_ratio"], 3),
                "other_unknown_count": other_unknown,
                "reading": t.reading,
                "pos": t.pos,
            }
            best = exposures.get(t.lemma)
            if best is None or ctx["other_unknown_count"] < best["other_unknown_count"]:
                exposures[t.lemma] = ctx

        # Already-tracked phrases met in this sentence accrue exposure too
        # (kind rides in the context; record_exposure routes it). New phrase
        # keys are never minted here — only curate's recorder does that.
        for u in d["phrases"]:
            other_unknown = d["unknown_count"] - (1 if u["phrase"] in d["unknown_lemmas"] else 0)
            ctx = {
                "sentence_idx": idx,
                "known_ratio": round(d["known_ratio"], 3),
                "other_unknown_count": other_unknown,
                "classification": d["classification"],
                "kind": "phrase",
            }
            best = exposures.get(u["phrase"])
            if best is None or ctx["other_unknown_count"] < best["other_unknown_count"]:
                exposures[u["phrase"]] = ctx

    return {
        "sentences": details,
        "counts": counts,
        "total_sentences": len(details),
        "total_tokens": total_tokens,
        "known_tokens": known_tokens,
        "token_comprehensibility": known_tokens / total_tokens if total_tokens else 0.0,
        "i_plus_1_sentences": [d for d in details if d["classification"] == "i_plus_1"],
        "exposures": exposures,
    }
