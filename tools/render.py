#!/usr/bin/env python3
"""render — coverage (+ optional curate) → self-contained offline prep HTML.

One static file per episode: stats, key-vocabulary glossary, i+1 sentences
with the target word highlighted, reinforcement sentences, and the P9
corrections loop (tap → copy blob / share sheet / visible textarea). When
/immerse has run, curate.json adds synopsis, glosses, and focal points.

CLI:
    python -m tools.render EPISODE_ID [--out PATH] [--config PATH]
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import lemma as L  # noqa: E402
from lib_config import load_config  # noqa: E402
from tools._staging import episode_dir, load_coverage, load_transcript, read_json  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "render" / "template.html"

GLOSSARY_CAP = 100

# Japanese runs inside otherwise-arbitrary prose (kanji/kana/长音); only runs
# that actually contain kanji get tokenized for furigana.
_JP_RUN = re.compile(r"[぀-ヿ㐀-鿿々〆ー]+")
_HAS_KANJI = re.compile(r"[㐀-鿿々〆]")


def annotate(text):
    """Furigana segments for mixed prose: [[chunk, reading-or-None], ...].

    English (and kana-only Japanese) passes through unreadinged; kanji-bearing
    runs are tokenized so the template can render <ruby> per token. Segment
    texts concatenate back to the original string exactly.
    """
    if not text:
        return []
    segs = []

    def plain(chunk):
        if not chunk:
            return
        if segs and segs[-1][1] is None:
            segs[-1][0] += chunk
        else:
            segs.append([chunk, None])

    pos = 0
    for m in _JP_RUN.finditer(text):
        plain(text[pos:m.start()])
        run = m.group()
        if _HAS_KANJI.search(run):
            for t in L.tokenize(run):
                reading = L.kata_to_hira(t.reading) if t.reading else None
                if reading and reading != t.surface and _HAS_KANJI.search(t.surface):
                    segs.append([t.surface, reading])
                else:
                    plain(t.surface)
        else:
            plain(run)
        pos = m.end()
    plain(text[pos:])
    return segs


def reading_of(word):
    return "".join(L.kata_to_hira(t.reading) or t.surface for t in L.tokenize(word))


def build_prep_data(transcript, coverage, curate=None):
    """Assemble the JSON payload the template renders. Pure."""
    curate = curate or {}
    keywords = curate.get("keywords", [])
    by_word = {k["word"]: k for k in keywords}

    # Curate-time noise filter: tokenizer misparses (name fragments), ASR
    # garbage etc. are excluded from the doc entirely — glossary AND i+1.
    excluded = {e["lemma"] if isinstance(e, dict) else e
                for e in curate.get("exclude", [])}
    candidates = [c for c in coverage["candidates"] if c["lemma"] not in excluded]
    keywords = [k for k in keywords if k["word"] not in excluded]

    shown_idx = set(c["best"]["sentence_idx"] for c in candidates)
    shown_idx |= set(coverage.get("reinforcement", []))
    sentences_by_idx = {
        s["idx"]: {"idx": s["idx"], "start": s["start"], "tokens": s["tokens"]}
        for s in coverage["sentences"] if s["idx"] in shown_idx
    }

    def gloss_row(lemma, freq_rank, recurrence):
        # reading recomputed from the lemma, not taken from coverage: older
        # coverage.json files carry the inflected surface's reading (開け→あけ)
        k = by_word.get(lemma, {})
        return {
            "lemma": lemma,
            "reading": reading_of(lemma),
            "freq_rank": freq_rank,
            "recurrence": recurrence,
            "gloss": k.get("gloss", ""),
            "gloss_segs": annotate(k.get("gloss", "")),
            "note_segs": annotate(k.get("note", "")),
        }

    # Curated keywords lead the grid in hand-picked order (they may include
    # thematic words the candidate ranking buried); remaining candidates
    # follow in deterministic rank order.
    cand_by_lemma = {c["lemma"]: c for c in candidates}
    recurrence = {}
    for s in coverage["sentences"]:
        for u in s.get("unknown", []):
            recurrence[u] = recurrence.get(u, 0) + 1
    glossary, seen = [], set()
    for k in keywords:
        w = k["word"]
        c = cand_by_lemma.get(w)
        if c:
            glossary.append(gloss_row(w, c["freq_rank"], c["recurrence"]))
        else:
            glossary.append(gloss_row(w, None, recurrence.get(w, 0)))
        seen.add(w)
    for c in candidates:
        if c["lemma"] not in seen:
            glossary.append(gloss_row(c["lemma"], c["freq_rank"], c["recurrence"]))
    glossary = glossary[:GLOSSARY_CAP]

    iplus1 = [{
        "lemma": c["lemma"],
        "reading": reading_of(c["lemma"]),
        "sentence_idx": c["best"]["sentence_idx"],
    } for c in candidates if c["best"]["other_unknown_count"] == 0]

    return {
        "episode": {"id": coverage["episode_id"],
                    "title": transcript["episode"].get("title", "")},
        "stats": coverage["stats"],
        "curate": {
            "synopsis": curate.get("synopsis"),
            "synopsis_segs": annotate(curate.get("synopsis", "")),
            "focal_points": [{**fp, "why_segs": annotate(fp.get("why", ""))}
                             for fp in curate.get("focal_points", [])],
        } if curate else None,
        "glossary": glossary,
        "iplus1": iplus1,
        "reinforcement": coverage.get("reinforcement", []),
        "sentences_by_idx": sentences_by_idx,
    }


def render_html(prep_data):
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(prep_data, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")  # keep </script> out of the stream
    html = html.replace("__TITLE__", prep_data["episode"]["title"] or
                        prep_data["episode"]["id"])
    return html.replace("__PREP_DATA__", payload)


def run_render(cfg, episode_id, out=None):
    transcript = load_transcript(cfg, episode_id)
    coverage = load_coverage(cfg, episode_id)
    curate_path = episode_dir(cfg, episode_id) / "curate.json"
    curate = read_json(curate_path) if curate_path.exists() else None

    html = render_html(build_prep_data(transcript, coverage, curate))
    out = Path(out) if out else episode_dir(cfg, episode_id) / "prep.html"
    out.write_text(html, encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("episode_id")
    ap.add_argument("--out")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out = run_render(cfg, args.episode_id, args.out)
    print(str(out))


if __name__ == "__main__":
    main()
