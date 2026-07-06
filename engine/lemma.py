"""SudachiPy SplitMode C tokenization + coverage analysis.

Replaces audioPrime's fugashi-based lemma_analyzer. The whole workstation
standardizes on one tokenizer (DESIGN.md — Reuse map): SudachiPy SplitMode C,
with dictionary_form() as the ledger join key, normalized_form() for
variant-aware known matching (こもる counts as known when 籠る is known — both
normalize to 籠もる), and kanji-stem matching to expand the known set.

POS filters and card-worthiness rules come from the sentence-mining skill,
where they are already validated against real mined decks.
"""

import re
from collections import namedtuple

# SudachiPy top-level POS categories (part_of_speech()[0]). Current
# sudachidict_core DOES emit 形状詞 for na-adjective stems (綺麗/静か/頑丈) —
# the sentence-mining skill's claim that they surface as 名詞 is stale, and
# omitting it silently drops na-adjectives from mining and the ledger.
CONTENT_POS_PREFIXES = ("名詞", "動詞", "形容詞", "形状詞", "副詞", "連体詞", "感動詞")
SKIP_POS_PREFIXES = ("助詞", "助動詞", "補助記号", "記号", "空白", "接尾辞", "接頭辞", "代名詞", "フィラー")

PURE_DIGITS = re.compile(r"^[0-9０-９]+$")
PURE_ASCII = re.compile(r"^[A-Za-z]+$")
PUNCT_ONLY = re.compile(r"^[!?！？。、,\.…\-—~〜]+$")
SINGLE_KANA = re.compile(r"^[぀-ゟ゠-ヿー]$")
KANA_ONLY_SHORT = re.compile(r"^[぀-ゟ゠-ヿー]{1,2}$")
HAS_KANJI = re.compile(r"[一-鿿]")
KANJI_RE = re.compile(r"[一-鿿々]+")

HTML_TAG_RE = re.compile(r"<[^>]+>")
FURIGANA_RE = re.compile(r"([一-龯々]+)\[([ぁ-ゖァ-ヺー]+)\]")

Token = namedtuple("Token", "surface lemma normalized reading pos")

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

    Returns Token(surface, lemma, normalized, reading, pos) per token:
      lemma      = dictionary_form()   (走った→走る; keeps surface orthography)
      normalized = normalized_form()   (collapses spelling variants: こもる/籠る→籠もる)
      reading    = reading_form()      (katakana)
      pos        = part_of_speech()[0] (top-level category, e.g. 名詞/動詞)
    """
    tok, mode = _sudachi()
    out = []
    for m in tok.tokenize(text, mode):
        out.append(Token(
            m.surface(),
            m.dictionary_form(),
            m.normalized_form(),
            m.reading_form(),
            m.part_of_speech()[0],
        ))
    return out


def strip_html(s):
    s = HTML_TAG_RE.sub("", s.replace("<br>", "\n").replace("<br/>", "\n"))
    return s.replace("　", " ").replace("\xa0", " ").strip()


def strip_furigana(s):
    # Anki furigana lives as `漢字[かんじ]`; drop the reading so the tokenizer sees clean text.
    return FURIGANA_RE.sub(r"\1", s)


def is_content_word(pos):
    if any(pos.startswith(p) for p in SKIP_POS_PREFIXES):
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


def extract_kanji_stem(lemma):
    m = KANJI_RE.match(lemma)
    return m.group(0) if m else ""


def content_tokens(text):
    """Content-word, card-worthy tokens of *text* — the vocabulary signal."""
    return [t for t in tokenize(text)
            if is_content_word(t.pos) and is_card_worthy(t.lemma)]


class KnownSet:
    """Variant-aware membership test for 'does the learner know this token?'.

    known        set of dictionary_form lemmas
    norm_known   set of normalized_form values (spelling-variant expansion)
    known_stems  set of kanji stems of known lemmas (stem-match expansion)
    """

    def __init__(self, known, norm_known=frozenset(), known_stems=frozenset()):
        self.known = set(known)
        self.norm_known = set(norm_known)
        self.known_stems = set(known_stems)

    def __contains__(self, token):
        if token.lemma in self.known:
            return True
        if token.normalized and token.normalized in self.norm_known:
            return True
        stem = extract_kanji_stem(token.lemma)
        return bool(stem) and stem in self.known_stems

    def __len__(self):
        return len(self.known)


def analyze_sentence(text, known_set, learning=frozenset()):
    """Classify one sentence against the materialized known set.

    Args:
        text: the sentence
        known_set: a KnownSet (or anything supporting `token in x`)
        learning: set of lemmas currently in 'learning' (counts as not-known
                  for i+1, but flags the sentence as reinforcement)

    Returns dict with tokens, unknown lists, known_ratio, and the four-way
    classification DESIGN.md's coverage analysis uses:
      comprehensible  — all content tokens known (counts for exposure)
      reinforcement   — exactly one unknown and it is `learning`
      i_plus_1        — exactly one unknown, truly unknown (mining candidate)
      too_hard        — two or more unknown lemmas
    """
    tokens = content_tokens(text)
    unknown = [t for t in tokens if t not in known_set]
    unique_unknown = {t.lemma for t in unknown}

    known_count = len(tokens) - len(unknown)
    known_ratio = known_count / len(tokens) if tokens else 1.0

    if not unique_unknown:
        classification = "comprehensible"
    elif len(unique_unknown) == 1:
        the_lemma = next(iter(unique_unknown))
        classification = "reinforcement" if the_lemma in learning else "i_plus_1"
    else:
        classification = "too_hard"

    return {
        "text": text,
        "tokens": tokens,
        "unknown": unknown,
        "unknown_lemmas": sorted(unique_unknown),
        "unknown_count": len(unique_unknown),
        "known_ratio": known_ratio,
        "classification": classification,
    }


def analyze_transcript(sentences, known_set, learning=frozenset()):
    """Analyze all sentences of an episode for coverage.

    Args:
        sentences: list of (start_sec, end_sec, text) from the SRT parser
        known_set: KnownSet from ledger materialize-known
        learning: lemmas in 'learning' status

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
        d = analyze_sentence(text, known_set, learning)
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
