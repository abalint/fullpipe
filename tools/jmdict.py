#!/usr/bin/env python3
"""JMdict → SQLite for the player's any-word dictionary popup.

The mobile player looks words up by the *lemma* Sudachi already put on every
transcript token, so no deinflection machinery (the hard part of Yomitan) is
needed — just a key→entry table over JMdict_e (the same EDRDG dictionary
Yomitan ships). Entries are keyed by every kanji and reading form; lookups
return compact JSON the server relays verbatim.

Usage:
    python -m tools.jmdict build [--config PATH] [--xml JMdict_e(.gz)]
    python -m tools.jmdict lookup WORD [WORD ...]
    python -m tools.jmdict missing EPISODE_ID

`build` downloads JMdict_e.gz from EDRDG (CC BY-SA, ~9 MB) when --xml is not
given, and writes <work_dir>/jmdict.db (~40 MB). One-off; rerun to refresh.

`missing` lists the episode's lemmas (ALL words, not just vocab — the popup
answers any tap) that have NO JMdict entry (after the normalized-form and
full-width fallbacks) with an example line each — the words the player's
popup would shrug at. The curate pass glosses the real ones into
curate.json's `defs`; /definitions serves them alongside JMdict and the
repair gate's name adjudications (merge_repair_names).
"""

import argparse
import gzip
import json
import re
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib_config import load_config  # noqa: E402

HAS_KANJI = re.compile(r"[㐀-鿿々〆]")
JMDICT_URL = "http://ftp.edrdg.org/pub/Nihongo/JMdict_e.gz"
_TO_FULLWIDTH = {c: c + 0xFEE0 for c in range(0x21, 0x7F)}
# `missing` worklist junk: punctuation/symbols only, digit-led (90万, 2019,
# 1日 — numeral+counter, not glossable vocab), lone kana or ASCII letter,
# whitespace
WORKLIST_JUNK = re.compile(
    r"^[0-9０-９]|^[぀-ゟ゠-ヿーA-Za-z]$|^[^ぁ-ヿ㐀-鿿々〆A-Za-z0-9０-９]+$|^\s*$")
# Tokens a compound run can't cross: digits/whitespace-only lemmas, or no
# Japanese/Latin at all (punctuation). MUST stay in lockstep with the mobile
# app's NO_LOOKUP (prep-render.ts) — the client rebuilds run keys on tap,
# and a mismatch means the phone constructs keys that were never served.
RUN_BREAK = re.compile(
    r"^[\s0-9０-９]*$|^[^ぁ-ゖァ-ヶー㐀-鿿々〆A-Za-z0-9０-９]+$")
SINGLE_KANA = re.compile(r"^[぀-ゟ゠-ヿー]$")
COMPOUND_MAX_TOKENS = 4  # 気を付ける = 3 tokens, お疲れ様 = 3; 4 covers the tail
MAX_SENSES = 8
MAX_GLOSSES = 5
DEFAULT_MAX_ENTRIES = 4  # per-lemma cap on lookup


def db_path(cfg):
    return Path(cfg["work_dir"]) / "jmdict.db"


def open_db(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _pri_score(elem):
    """Commonness from ke_pri/re_pri tags: each news1/ichi1/spec1/gai1 counts
    double, other tags (news2, nfXX, …) once. Higher = more common."""
    score = 0
    for pri in elem.findall("./ke_pri") + elem.findall("./re_pri"):
        score += 2 if pri.text and pri.text.endswith("1") else 1
    return score


def _short_pos(text):
    """Entity-expanded pos ("noun (common) (futsuumeishi)") → "noun"."""
    return (text or "").split(" (")[0]


def parse_entries(fileobj):
    """Yield (pri, keys, data) per <entry>; data is the compact wire dict
    {"k": kanji forms, "r": readings, "s": [{"pos": [...], "g": [...]}]},
    senses usually written in kana flagged "uk" (drives kana-key ranking)."""
    for _, entry in ET.iterparse(fileobj):
        if entry.tag != "entry":
            continue
        kanji = [k.text for k in entry.findall("./k_ele/keb") if k.text]
        kana = [r.text for r in entry.findall("./r_ele/reb") if r.text]
        senses = []
        for sense in entry.findall("./sense")[:MAX_SENSES]:
            glosses = [g.text for g in sense.findall("./gloss") if g.text]
            if not glosses:
                continue
            s = {
                "pos": [_short_pos(p.text) for p in sense.findall("./pos")],
                "g": glosses[:MAX_GLOSSES],
            }
            if any((m.text or "").startswith("word usually written using kana")
                   for m in sense.findall("./misc")):
                s["uk"] = True
            senses.append(s)
        if senses and (kanji or kana):
            pri = max((_pri_score(e) for e in
                       entry.findall("./k_ele") + entry.findall("./r_ele")),
                      default=0)
            yield pri, set(kanji + kana), {"k": kanji, "r": kana, "s": senses}
        entry.clear()  # keep iterparse memory flat


def build_db(conn, entries):
    """(Re)build the two tables from a parse_entries stream. Returns count."""
    conn.executescript(
        "DROP TABLE IF EXISTS entries; DROP TABLE IF EXISTS keys;"
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, pri INTEGER NOT NULL,"
        "                      data TEXT NOT NULL);"
        "CREATE TABLE keys (key TEXT NOT NULL, id INTEGER NOT NULL);")
    n = 0
    for pri, keys, data in entries:
        n += 1
        conn.execute("INSERT INTO entries (id, pri, data) VALUES (?, ?, ?)",
                     (n, pri, json.dumps(data, ensure_ascii=False)))
        conn.executemany("INSERT INTO keys (key, id) VALUES (?, ?)",
                         [(k, n) for k in keys])
    conn.execute("CREATE INDEX idx_keys_key ON keys(key)")
    conn.commit()
    return n


def is_headword(conn, text):
    """Is *text* a JMdict headword (kanji or reading form)? The phrase-key
    validator (GRAMMAR.md): a curate-emitted phrase canonical must be a real
    key so tracked phrases join across episodes."""
    return conn.execute("SELECT 1 FROM keys WHERE key = ? LIMIT 1",
                        (text,)).fetchone() is not None


def lookup_many(conn, lemmas, max_entries=DEFAULT_MAX_ENTRIES):
    """{lemma: [entry, …]} common-first; lemmas with no entry are absent.

    A lemma that isn't a JMdict headword falls back to its Sudachi
    *normalized* form: lexicalized inflections carry a lemma JMdict lacks but a
    normalized form it has (許せる → 許す, い抜き させる/passive, spelling variants
    like 繋ぐ→繫ぐ). This is the light deinflection the module's original
    "lemma is enough" assumption missed for potential/variant forms.

    A kana-only key ranks kana-natural entries above raw pri, which puts 野
    before the particle の and 照る before the てる auxiliary — backwards for
    a kana transcript token (Sudachi lemmatizes content words to their kanji
    form; a kana lemma means the word is written kana). Ordering: entries
    that are kana-natural (no kanji forms, or a sense flagged uk) first;
    within a tier, key-is-first-reading beats a secondary reading (貴方/あなた
    over 彼方, primary かなた); among the NON-natural leftovers a grammar POS
    (particle/auxiliary) rescues kanji-keyed function words (乃/之 = の's
    possessive particle) over 野. Stable sort keeps pri order for the rest —
    so ubiquitous 事 still beats the rare こと command particle."""
    def _rows(key):
        rows = conn.execute(
            "SELECT e.data FROM keys k JOIN entries e ON e.id = k.id"
            " WHERE k.key = ? ORDER BY e.pri DESC, e.id LIMIT ?",
            (key, max_entries * 3)).fetchall()
        if rows and not HAS_KANJI.search(key):
            def rank(row):
                e = json.loads(row[0])
                natural = not e["k"] or any(s.get("uk") for s in e["s"])
                grammar = any("particle" in p or "auxiliary" in p
                              for p in e["s"][0]["pos"])
                return (not natural, e["r"][:1] != [key],
                        not natural and not grammar)
            rows = sorted(rows, key=rank)
        return rows[:max_entries]

    out, misses = {}, []
    for lemma in set(lemmas):
        rows = _rows(lemma)
        if rows:
            out[lemma] = [json.loads(r[0]) for r in rows]
        else:
            misses.append(lemma)
    if misses:
        from engine.lemma import tokenize
        for lemma in misses:
            # JMdict keys Latin acronyms full-width (ＳＮＳ, ＡＩ); transcripts
            # carry them half-width
            if lemma.isascii():
                rows = _rows(lemma.translate(_TO_FULLWIDTH))
                if rows:
                    out[lemma] = [json.loads(r[0]) for r in rows]
                continue
            toks = tokenize(lemma)
            norm = toks[0].normalized if len(toks) == 1 else None
            if not norm or norm == lemma:
                continue  # multi-token or no better key — genuinely absent
            rows = _rows(norm)
            if rows:
                out[lemma] = [json.loads(r[0]) for r in rows]
    return out


def ai_entry(d):
    """A curate-authored `defs` row → the compact JMdict wire shape the app
    already renders, flagged `ai` (episode-specific, not EDRDG)."""
    word, reading = d.get("word", ""), d.get("reading", "")
    entry = {"k": [word] if HAS_KANJI.search(word) else [],
             "r": [reading or word],
             "s": [{"pos": [d["pos"]] if d.get("pos") else [],
                    "g": [d.get("gloss", "")]}],
             "ai": True}
    return entry


def merge_curate_defs(result, curate):
    """Fold curate.json's `defs` into a lookup_many result.

    Words JMdict lacks get the AI entry as their only entry. Words JMdict
    HAS get the AI entry PREPENDED: curate wrote it from the episode's
    actual context, so the popup leads with the sense the viewer just heard
    — full dictionary entries follow, so a wrong AI gloss can demote but
    never hide the real lookup."""
    for d in curate.get("defs", []):
        word = d.get("word")
        if not word or not d.get("gloss"):
            continue
        entries = result.get(word)
        if entries:
            if not any(e.get("ai") for e in entries):
                result[word] = [ai_entry(d)] + entries
        else:
            result[word] = [ai_entry(d)]
    return result


def compound_entries(conn, sentences, max_entries=2):
    """JMdict entries for compounds and expressions Sudachi (SplitMode C)
    still splits into adjacent tokens: 帝王切開 → 帝王|切開, そういう →
    そう|いう, 気を付ける → 気|を|付ける. Keyed by the joined span so the
    popup can answer a tap on any component with the whole's meaning.

    For every run of 2..COMPOUND_MAX_TOKENS adjacent lookup-worthy tokens,
    two candidate keys: the surface concat, and surfaces+final-LEMMA (an
    inflected tail still finds its dictionary form: 気を付けて → 気を付ける).
    A candidate counts only when it is a JMdict headword AND re-tokenizing
    it reproduces the run's exact lemma sequence — same determinism trick as
    GRAMMAR.md's phrase validator, and what rejects accidental
    concatenations (は+いる is NOT 入る: tokenize(はいる) → [はいる]).
    Each ENTRY must ALSO tokenize its own canonical form (first kanji form,
    else first reading) back to the same segmentation — matched on Sudachi
    normalized forms OR reading sequences, since each alone wobbles on a
    spelling variant (という↔と言う unify via normalized 言う but read
    イウ/ユウ; じゃない↔じゃ無い read ジャ|ナイ but じゃ normalizes だ/で
    depending on spelling). Homographs differ in SEGMENTATION, so both
    checks fail together: する+た writes した, a JMdict key — but only for
    舌 "tongue", one token, not two. 舌/仕手/死ね die here; という/そういう/
    じゃない/気にする pass. Runs of
    nothing but single-kana grammar tokens (た+し, ん+だ) are skipped — those
    are auxiliary clusters, the inflection chain's job, and their JMdict
    coincidences (たし "want to") mislead more than they help.

    The mobile player mirrors the run/key construction on tap
    (compoundKeysAt), so the two sides MUST stay in lockstep."""
    from engine.lemma import tokenize
    out, checked = {}, set()
    for s in sentences:
        toks = s["tokens"]
        for i, t0 in enumerate(toks):
            if not t0.get("l") or RUN_BREAK.search(t0["l"]):
                continue
            for L in range(2, COMPOUND_MAX_TOKENS + 1):
                if i + L > len(toks):
                    break
                tail = toks[i + L - 1]
                if not tail.get("l") or RUN_BREAK.search(tail["l"]):
                    break
                run = toks[i:i + L]
                if all(SINGLE_KANA.match(t["l"]) for t in run):
                    continue  # auxiliary cluster, not a compound
                lemmas = tuple(t["l"] for t in run)
                surf = "".join(t["s"] for t in run)
                stem = "".join(t["s"] for t in run[:-1]) + tail["l"]
                for key in {surf, stem}:
                    if (key, lemmas) in checked:
                        continue
                    checked.add((key, lemmas))
                    if key in out or not is_headword(conn, key):
                        continue
                    key_toks = tokenize(key)
                    if tuple(t.lemma for t in key_toks) != lemmas:
                        continue  # accidental concat, not this span
                    key_norm = tuple(t.normalized for t in key_toks)
                    key_read = tuple(t.reading for t in key_toks)
                    good = []
                    for e in lookup_many(conn, [key],
                                         max_entries + 2).get(key, []):
                        form = e["k"][0] if e["k"] else e["r"][0]
                        if form != key:
                            form_toks = tokenize(form)
                            if (tuple(t.normalized for t in form_toks)
                                    != key_norm
                                    and tuple(t.reading for t in form_toks)
                                    != key_read):
                                continue  # same key, different word
                        good.append(e)
                    if good:
                        out[key] = good[:max_entries]
    return out


def merge_repair_names(result, repair):
    """Fold the repair gate's `names` adjudications into a lookup_many
    result, keyed by surface (post-repair transcripts carry adjudicated
    names as single tokens, lemma == surface). The kind+note the gate wrote
    (person/place/brand + who they are) is exactly what a tap on a name
    should show. Curate-authored defs win: a name that already has an `ai`
    entry is left alone."""
    for n in repair.get("names", []):
        if not isinstance(n, dict):
            continue
        surface, note = n.get("surface"), n.get("note")
        if not surface or not note:
            continue
        entry = ai_entry({"word": surface, "gloss": note,
                          "pos": f"name ({n['kind']})" if n.get("kind")
                                 else "name"})
        entries = result.get(surface)
        if entries:
            if not any(e.get("ai") for e in entries):
                result[surface] = [entry] + entries
        else:
            result[surface] = [entry]
    return result


def missing(cfg, episode_id):
    """[{lemma, count, example}] for the episode's lemmas with no JMdict
    entry — the curate pass's worklist for `defs`.

    ALL lemmas, not just content/vocab ones — /definitions serves every
    token's lemma, so the worklist must cover the same keyset or the popup
    shrugs at exactly the words the vocab filter dropped. Excluded:
    WORKLIST_JUNK (punctuation, digit-led, lone kana), the repair gate's
    `nonwords` (adjudicated ASR garbage — there is nothing true to write
    about ワンタイン), and its noted `names` (merge_repair_names already
    serves those to the popup)."""
    from tools._staging import episode_dir, load_coverage, read_json
    from tools.repair import REPAIR_FILE, _non_vocab_lemmas
    coverage = load_coverage(cfg, episode_id)
    repair_path = episode_dir(cfg, episode_id) / REPAIR_FILE
    repair = read_json(repair_path) if repair_path.exists() else {}
    skip = set(_non_vocab_lemmas([], repair.get("nonwords") or []))
    skip.update(n["surface"] for n in repair.get("names") or []
                if isinstance(n, dict) and n.get("surface") and n.get("note"))
    lemmas, example, count = set(), {}, {}
    for s in coverage["sentences"]:
        for t in s["tokens"]:
            lm = t.get("l")
            if not lm or WORKLIST_JUNK.search(lm) or lm in skip:
                continue
            lemmas.add(lm)
            count[lm] = count.get(lm, 0) + 1
            example.setdefault(lm, s.get("text", ""))
    path = db_path(cfg)
    if not path.exists():
        raise FileNotFoundError(f"{path} — run: python -m tools.jmdict build")
    conn = open_db(path)
    try:
        found = lookup_many(conn, lemmas)
    finally:
        conn.close()
    return [{"lemma": lm, "count": count[lm], "example": example[lm]}
            for lm in sorted(lemmas - set(found),
                             key=lambda x: (-count[x], x))]


def build(cfg, xml_path=None):
    out = db_path(cfg)
    if xml_path is None:
        xml_path = Path(cfg["work_dir"]) / "JMdict_e.gz"
        if not xml_path.exists():
            print(f"downloading {JMDICT_URL} …", file=sys.stderr)
            urllib.request.urlretrieve(JMDICT_URL, xml_path)
    xml_path = Path(xml_path)
    opener = gzip.open if xml_path.suffix == ".gz" else open
    conn = sqlite3.connect(out)
    with opener(xml_path, "rb") as f:
        n = build_db(conn, parse_entries(f))
    conn.close()
    print(f"{out}: {n} entries")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="JMdict_e → <work_dir>/jmdict.db")
    b.add_argument("--xml", help="local JMdict_e(.gz); downloaded if omitted")
    q = sub.add_parser("lookup", help="debug: print entries for words")
    q.add_argument("words", nargs="+")
    m = sub.add_parser("missing",
                       help="episode content lemmas with no JMdict entry")
    m.add_argument("episode_id")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.cmd == "build":
        build(cfg, args.xml)
    elif args.cmd == "missing":
        print(json.dumps(missing(cfg, args.episode_id),
                         ensure_ascii=False, indent=2))
    else:
        conn = open_db(db_path(cfg))
        print(json.dumps(lookup_many(conn, args.words),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
