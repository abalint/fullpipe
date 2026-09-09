"""Grammar-unit tooling (GRAMMAR.md — Grammar as token-anchored units).

    python -m tools.grammar try "食べちゃった。"          tokens + the units found
    python -m tools.grammar corpus [--out FILE]          cache every staged episode's
                                                         tokenized sentences + curate tags
    python -m tools.grammar check [--taxonomy F] [--corpus F] [--patterns P,P] [--misses]
                                                         hit counts, recall vs the curate
                                                         pass's tags, sample surfaces
    python -m tools.grammar backfill [--episode ID] [--dry-run]
                                                         detect units on every staged
                                                         coverage.json, write them per
                                                         sentence, record exposures

`check` is the authoring loop for taxonomy `match` specs: the curate pass
already tagged ~1.6k usages across the staged episodes (curate.json
`grammar`), which makes a labeled recall set; precision is judged from the
sample surfaces each pattern matched.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import grammar as G  # noqa: E402
from engine import lemma as L  # noqa: E402

TAXONOMY = Path(__file__).resolve().parent.parent / "ledger" / "grammar_taxonomy.json"


def tok_dicts(text):
    return [{"s": t.surface, "l": t.lemma, "p": t.pos, "p2": t.pos2}
            for t in L.tokenize(text)]


def load_taxonomy(path=TAXONOMY):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _episode_dirs(cfg):
    from tools._staging import episodes_root
    root = episodes_root(cfg)
    return sorted(p for p in root.iterdir() if (p / "coverage.json").exists())


def build_corpus(cfg, out):
    """Every staged episode's sentences, re-tokenized with POS (coverage
    tokens carry none), plus the curate pass's (sentence_idx → patterns)
    tags. One JSON file; ~1 min for 120 episodes."""
    corpus = []
    for d in _episode_dirs(cfg):
        cov = json.loads((d / "coverage.json").read_text(encoding="utf-8"))
        cur_path = d / "curate.json"
        tags = defaultdict(list)
        if cur_path.exists():
            try:
                cur = json.loads(cur_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cur = {}
            for g in cur.get("grammar") or []:
                if g.get("sentence_idx") is not None and g.get("pattern"):
                    tags[g["sentence_idx"]].append(g["pattern"])
        for s in cov["sentences"]:
            toks = tok_dicts(s["text"])
            corpus.append({"ep": d.name, "idx": s["idx"], "text": s["text"],
                           "cls": s.get("classification"),
                           "tokens": toks, "tags": tags.get(s["idx"], [])})
    Path(out).write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    return len(corpus)


def check(taxonomy_rows, corpus, patterns=None, show_misses=False, samples=6):
    """Per-pattern report: lines hit, episodes, recall against curate tags,
    sample surfaces. Returns (report dict, text lines)."""
    rows = [r for r in taxonomy_rows if not patterns or r["pattern"] in patterns]
    index = G.build_grammar_index(rows)
    want = {r["pattern"] for r in rows}
    hits = Counter()
    eps = defaultdict(set)
    surf = defaultdict(Counter)
    tagged = Counter()
    recalled = Counter()
    misses = defaultdict(list)
    for s in corpus:
        units = G.match_grammar_units(s["tokens"], index)
        here = {u.pattern for u in units}
        for u in units:
            hits[u.pattern] += 1
            eps[u.pattern].add(s["ep"])
            surf[u.pattern]["".join(t["s"] for t in s["tokens"][u.start:u.end])] += 1
        for p in s["tags"]:
            if p not in want:
                continue
            tagged[p] += 1
            if p in here:
                recalled[p] += 1
            elif len(misses[p]) < 5:
                misses[p].append({"ep": s["ep"], "idx": s["idx"], "text": s["text"],
                                  "tokens": " | ".join(f"{t['s']}/{t['l']}/{t['p']}-{t['p2']}"
                                                       for t in s["tokens"])})
    report = {}
    lines = []
    for r in rows:
        p = r["pattern"]
        has = bool(r.get("match"))
        rep = {"pattern": p, "level": r.get("level"), "has_match": has,
               "lines": hits[p], "episodes": len(eps[p]),
               "tagged": tagged[p], "recalled": recalled[p],
               "samples": surf[p].most_common(samples)}
        if show_misses and misses[p]:
            rep["misses"] = misses[p]
        report[p] = rep
        rec = f"{recalled[p]}/{tagged[p]}" if tagged[p] else "-"
        sample = " · ".join(f"{s}×{n}" for s, n in rep["samples"][:samples])
        flag = "" if has else "  [NO MATCH SPEC]"
        lines.append(f"N{r.get('level')} {p:<18} lines={hits[p]:<6} eps={len(eps[p]):<4} "
                     f"recall={rec:<8} {sample}{flag}")
        if show_misses:
            for m in misses[p]:
                lines.append(f"    MISS {m['ep']}#{m['idx']}: {m['text']}")
                lines.append(f"         {m['tokens']}")
    return report, lines


def backfill(cfg, episode_id=None, dry_run=False):
    """Detect grammar units on every staged episode (or one), write them
    into coverage.json per sentence (`grammar: [{pattern,start,end}]`) and
    record the exposures the way Stage 1 now does. Idempotent: the
    exposure index makes re-runs no-ops; the sentence field is rewritten."""
    from ledger import ledgerctl as lc
    from tools.coverage import grammar_pass
    conn = lc.open_db(cfg["ledger_db"])
    rows = lc.grammar_match_rows(conn)
    index = G.build_grammar_index(rows)
    done = []
    lines_hit = Counter()  # pattern -> lines it fires on, over every episode
    n_sentences = 0
    for d in _episode_dirs(cfg):
        if episode_id and d.name != episode_id:
            continue
        path = d / "coverage.json"
        try:
            cov = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # purged between the listing and now (the worker reconciles live)
        exposures = {}
        n_units = 0
        for s in cov["sentences"]:
            toks = tok_dicts(s["text"])
            if len(toks) != len(s["tokens"]):
                # tokenizer drift since coverage ran: match on the stored
                # lemmas/surfaces only (no POS) rather than misalign spans
                toks = [{"s": t.get("s"), "l": t.get("l")} for t in s["tokens"]]
            units = grammar_pass(s, toks, index, exposures)
            n_sentences += 1
            for p in {u["pattern"] for u in units}:
                lines_hit[p] += 1
            if units:
                s["grammar"] = units
                n_units += len(units)
            else:
                s.pop("grammar", None)
        cov["grammar_at"] = lc.now_iso()
        if not dry_run:
            path.write_text(json.dumps(cov, ensure_ascii=False, indent=1), encoding="utf-8")
            r = lc.record_exposure(conn, {"id": d.name}, exposures, meta=False)
            done.append({"episode": d.name, "units": n_units,
                         "patterns": len(exposures), "new_rows": r["new_rows"]})
        else:
            done.append({"episode": d.name, "units": n_units, "patterns": len(exposures)})
    if not dry_run and done and not episode_id and n_sentences:
        # the corpus-frequency prior (grammar_theta_for): only a full pass
        # measures it — every detectable pattern, zero included
        lc.set_grammar_corpus_freq(conn, {
            r["pattern"]: lines_hit[r["pattern"]] * 10000.0 / n_sentences for r in rows})
    if not dry_run and done:
        lc.promote(conn)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("try")
    p.add_argument("text")
    p.add_argument("--taxonomy", default=str(TAXONOMY))
    p = sub.add_parser("corpus")
    p.add_argument("--out", required=True)
    p = sub.add_parser("check")
    p.add_argument("--taxonomy", default=str(TAXONOMY))
    p.add_argument("--corpus", required=True)
    p.add_argument("--patterns", help="comma-separated subset")
    p.add_argument("--misses", action="store_true")
    p.add_argument("--json", help="write the full report here")
    p = sub.add_parser("backfill")
    p.add_argument("--episode")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.verb == "try":
        rows = load_taxonomy(args.taxonomy)
        index = G.build_grammar_index(rows)
        toks = tok_dicts(args.text)
        for i, t in enumerate(toks):
            print(f"{i:>2} {t['s']}/{t['l']}/{t['p']}-{t['p2']}")
        for u in G.match_grammar_units(toks, index):
            print(f"   {u.pattern}  [{u.start},{u.end})  "
                  f"{''.join(t['s'] for t in toks[u.start:u.end])}")
        return
    from lib_config import load_config
    cfg = load_config()
    if args.verb == "corpus":
        print(json.dumps({"sentences": build_corpus(cfg, args.out)}))
    elif args.verb == "check":
        rows = load_taxonomy(args.taxonomy)
        corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
        pats = set(args.patterns.split(",")) if args.patterns else None
        report, lines = check(rows, corpus, pats, show_misses=args.misses)
        print("\n".join(lines))
        with_spec = [r for r in report.values() if r["has_match"]]
        tagged = sum(r["tagged"] for r in with_spec)
        rec = sum(r["recalled"] for r in with_spec)
        print(f"\n{len(with_spec)}/{len(report)} patterns have a match spec; "
              f"{sum(1 for r in with_spec if r['lines'] == 0)} of them never fire; "
              f"recall on curate-tagged lines {rec}/{tagged}")
        if args.json:
            Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    elif args.verb == "backfill":
        for r in backfill(cfg, args.episode, args.dry_run):
            print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
