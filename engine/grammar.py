"""Deterministic grammar-point detection over Sudachi tokens (GRAMMAR.md —
Grammar as token-anchored units, 2026-09-08).

A grammar point is, to the learner, a *thing attached to a word*: てしまう
after 食べ, ながら after 歩き, っぽい after 子供. The tokenizer already
splits those attachments into their own tokens, so a pattern can be
described as a short sequence of token constraints and found on every
line of every episode with no LLM in the loop — exactly the way a tracked
phrase is found by its lemma sequence (engine.lemma.match_phrase_units).
That is what makes grammar *emergent* in the workflow like words: Stage 1
detects it, the player paints the span, the popup marks it, the ledger
accrues exposure on every occurrence, not only the lines a curate pass
chose to annotate.

The matcher spec lives on the taxonomy row (`match` in
ledger/grammar_taxonomy.json, mirrored into grammar_points.match):

    "match": [ ALTERNATIVE, ALTERNATIVE, ... ]      # any alternative fires
    ALTERNATIVE = [ STEP, STEP, ... ]               # consecutive tokens
    STEP =
      "しまう"                lemma equals
      ["ちゃう", "じゃう"]     lemma is one of
      {}                      any one token
      {"l": ..., "s": ..., "p": ..., "p2": ..., "not_l": ..., "plain": true, "ctx": true}
         l      lemma (dictionary form) — str or list
         not_l  lemma must NOT be one of these — str or list (exclude the
                stative verbs before an exclamatory な from 禁止 な)
         s      surface as written — str or list (たら vs た; ちゃ vs て)
         p      part-of-speech top level, prefix match — str or list
                (動詞 助詞 助動詞 名詞 形容詞 形状詞 副詞 接尾辞 接頭辞 代名詞 …)
         p2     part-of-speech subtype, exact — str or list
                (接続助詞 終助詞 格助詞 副助詞 係助詞 準体助詞 非自立可能
                 助動詞語幹 一般 普通名詞 形容詞的 形状詞的 …)
         plain  true → the token is in dictionary form (surface == lemma):
                the verb before a prohibitive な, before なり, before と（条件）
         ctx    true → this token must be there but is NOT part of the
                painted unit (a context anchor: the plain verb before 禁止 な,
                the noun before ばかりに). Only leading/trailing steps should
                be ctx; a ctx step in the middle is painted anyway because a
                unit is one contiguous span.

    An empty list (`"match": []`) means the pattern is not deterministically
    detectable (its tokens are shared with an unrelated use and nothing in
    the token stream separates them) — it stays a curate-only annotation.

Selection: every alternative of every pattern is tried at every position;
the longest matches win (by total tokens matched, then by specificity —
the number of constraints the alternative states, so なきゃ pinned by
surface as 〜なければならない beats the bare-lemma 〜ない — then taxonomy
order), non-overlapping over the *painted* span, so 〜なければならない beats
〜ない and 〜させていただく beats 〜させる on the same tokens. Context tokens
may be shared between units.
"""

from collections import namedtuple

Unit = namedtuple("Unit", "pattern start end")

# Steps that pin at least one lemma index by it; everything else is tried
# at every position.
_WILD = "*"


def _as_list(v):
    if v is None:
        return None
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _norm_step(step):
    """One STEP → {'l','s','p','p2','plain','ctx'} with list-valued fields
    (None = unconstrained)."""
    base = {"l": None, "s": None, "p": None, "p2": None, "not_l": None,
            "plain": False, "ctx": False}
    if isinstance(step, str):
        return {**base, "l": [step]}
    if isinstance(step, (list, tuple)):
        return {**base, "l": list(step)}
    if isinstance(step, dict):
        return {**base, "l": _as_list(step.get("l")), "s": _as_list(step.get("s")),
                "p": _as_list(step.get("p")), "p2": _as_list(step.get("p2")),
                "not_l": _as_list(step.get("not_l")),
                "plain": bool(step.get("plain")), "ctx": bool(step.get("ctx"))}
    raise ValueError(f"bad grammar match step: {step!r}")


def _step_ok(step, tok):
    """tok: anything with .lemma/.surface/.pos/.pos2 (engine.lemma.Token) or
    a coverage token dict {l, s, p?, p2?}."""
    if isinstance(tok, dict):
        lemma, surface = tok.get("l") or "", tok.get("s") or ""
        pos, pos2 = tok.get("p") or "", tok.get("p2") or ""
    else:
        lemma, surface, pos, pos2 = tok.lemma, tok.surface, tok.pos, tok.pos2
    if step["l"] is not None and lemma not in step["l"]:
        return False
    if step["not_l"] is not None and lemma in step["not_l"]:
        return False
    if step["s"] is not None and surface not in step["s"]:
        return False
    if step["p"] is not None and not any(pos.startswith(p) for p in step["p"]):
        return False
    if step["p2"] is not None and pos2 not in step["p2"]:
        return False
    if step["plain"] and surface != lemma:
        return False
    return True


def compile_match(spec):
    """Validate and normalize one taxonomy row's `match` value. Returns a
    list of alternatives, each a list of normalized steps; [] for
    None/empty. Raises ValueError on a malformed spec."""
    if not spec:
        return []
    if not isinstance(spec, list) or not all(isinstance(a, list) for a in spec):
        raise ValueError("match must be a list of alternatives (lists of steps)")
    alts = []
    for alt in spec:
        if not alt:
            raise ValueError("empty alternative")
        steps = [_norm_step(s) for s in alt]
        if all(s["ctx"] for s in steps):
            raise ValueError("an alternative needs at least one non-ctx step")
        alts.append(steps)
    return alts


def build_grammar_index(rows):
    """rows: iterable of {pattern, match} (match = the spec above; a JSON
    string is accepted). Returns the index match_grammar_units consumes:
    {first_lemma | '*': [(order, pattern, steps), ...]}. Rows without a
    usable match are skipped; a malformed one raises (the seed validates)."""
    import json
    index = {}
    for order, row in enumerate(rows):
        spec = row.get("match")
        if isinstance(spec, str):
            spec = json.loads(spec) if spec.strip() else None
        for steps in compile_match(spec):
            first = steps[0]
            keys = first["l"] if first["l"] else [_WILD]
            for k in keys:
                index.setdefault(k, []).append((order, row["pattern"], steps))
    return index


def _matches_at(tokens, i, steps):
    if i + len(steps) > len(tokens):
        return False
    return all(_step_ok(st, tokens[i + j]) for j, st in enumerate(steps))


def _specificity(steps):
    return sum((st["l"] is not None) + (st["s"] is not None) + (st["p"] is not None)
               + (st["p2"] is not None) + (st["not_l"] is not None) + st["plain"]
               for st in steps)


def _painted(i, steps):
    lo = 0
    while lo < len(steps) and steps[lo]["ctx"]:
        lo += 1
    hi = len(steps)
    while hi > lo and steps[hi - 1]["ctx"]:
        hi -= 1
    return i + lo, i + hi


def _is_space(tok):
    if isinstance(tok, dict):
        return (tok.get("p") or "").startswith("空白") or not (tok.get("s") or "").strip()
    return tok.pos.startswith("空白") or not tok.surface.strip()


def match_grammar_units(tokens, index):
    """Every grammar unit in one sentence's FULL token sequence (particles and
    auxiliaries included). Returns [Unit(pattern, start, end)] ordered by
    start, end exclusive over the painted span. Longest match wins, ties by
    taxonomy order, non-overlapping over painted spans. Whitespace tokens
    (hand-subtitled series lines carry them mid-construction: させて いただく)
    are transparent — a unit may span one, and it is painted with it."""
    if not index:
        return []
    sig = [i for i, t in enumerate(tokens) if not _is_space(t)]
    if len(sig) != len(tokens):
        inner = match_grammar_units([tokens[i] for i in sig], index)
        return [Unit(u.pattern, sig[u.start], sig[u.end - 1] + 1) for u in inner]
    wild = index.get(_WILD, ())
    found = []  # (-(total len), -specificity, order, start, painted span, pattern)
    for i, tok in enumerate(tokens):
        lemma = tok.get("l") if isinstance(tok, dict) else tok.lemma
        cands = list(index.get(lemma, ())) + list(wild)
        for order, pattern, steps in cands:
            if _matches_at(tokens, i, steps):
                ps, pe = _painted(i, steps)
                found.append((-len(steps), -_specificity(steps), order, i, ps, pe, pattern))
    found.sort()
    taken = [False] * len(tokens)
    units = []
    for _, _, _, _, ps, pe, pattern in found:
        if any(taken[ps:pe]):
            continue
        for k in range(ps, pe):
            taken[k] = True
        units.append(Unit(pattern, ps, pe))
    units.sort(key=lambda u: (u.start, u.end))
    return units


def units_as_dicts(units):
    return [{"pattern": u.pattern, "start": u.start, "end": u.end} for u in units]
