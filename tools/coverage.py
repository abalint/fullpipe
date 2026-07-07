#!/usr/bin/env python3
"""coverage — transcript + ledger → i+1 flags and ranked mining candidates.

Classifies every sentence four ways (comprehensible / reinforcement / i+1 /
too-hard), ranks unknown lemmas with deterministic signal columns — the live
model in /immerse weighs them per-episode (resolved Q4) — and writes the
exposure payload. By default it also records the (inert) exposures to the
ledger, which is what /immerse does at analysis time.

Signal columns per candidate:
    freq_rank    show-penetration rank (P7); NULL → rare
    recurrence   occurrences within this episode
    leverage     corpus i+1 leverage — NULL until phrases-full re-parse (P1)

CLI:
    python -m tools.coverage EPISODE_ID [--refresh-known] [--no-record] [--config PATH]
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import lemma as L  # noqa: E402
from engine import word_align as WA  # noqa: E402
from lib_config import load_config  # noqa: E402
from ledger import ledgerctl as lc  # noqa: E402
from tools._staging import episode_dir, load_transcript, read_json, write_json  # noqa: E402

CANDIDATE_CAP = 50


def lemma_reading(lemma):
    """Dictionary-form reading. A token's own reading is the *inflected
    surface's* (開け→あけ, たまん→たまん) — wrong for a glossary keyed by
    lemma, so re-tokenize the lemma itself."""
    return "".join(L.kata_to_hira(t.reading) or t.surface for t in L.tokenize(lemma))


def analyze(transcript, known_bundle, freq=None, already_carded=frozenset(),
            words=None):
    """Pure coverage analysis. Returns the coverage.json payload (unrecorded).

    known_bundle: dict from ledgerctl.materialize_known (known / learning /
    norm_known / known_stems). freq: {lemma: rank}. words: the episode's raw
    ASR timed words (words.json), used to attach a per-token start time "t"
    that paces the player's subtitle roll-up; None/misaligned → no "t".
    """
    freq = freq or {}
    ks = L.KnownSet(known_bundle["known"], known_bundle.get("norm_known", ()),
                    known_bundle.get("known_stems", ()))
    learning = set(known_bundle.get("learning", ()))

    sentences = [(s["start"], s["end"], s["text"]) for s in transcript["sentences"]]
    result = L.analyze_transcript(sentences, ks, learning)

    char_times = WA.sentence_char_times(
        [s["text"] for s in transcript["sentences"]], WA.char_timeline(words),
    ) if words else None

    out_sentences = []
    recurrence = {}
    best = {}  # unknown lemma -> best sentence info
    for d in result["sentences"]:
        times = char_times[d["index"]] if char_times else None
        pos = 0  # cursor over the sentence's content chars
        toks = []
        for t in L.tokenize(d["text"]):
            tok = {
                "s": t.surface,
                "l": t.lemma,
                "r": L.kata_to_hira(t.reading),
                "c": L.is_content_word(t.pos) and L.is_card_worthy(t.lemma),
                "k": t in ks,
            }
            if times is not None:
                n = sum(1 for ch in t.surface if WA.is_content_char(ch))
                if n and pos < len(times):
                    tok["t"] = round(times[pos], 2)
                pos += n
            toks.append(tok)
        out_sentences.append({
            "idx": d["index"], "start": d["start"], "end": d["end"],
            "text": d["text"],
            "classification": d["classification"],
            "known_ratio": round(d["known_ratio"], 3),
            "unknown": d["unknown_lemmas"],
            "tokens": toks,
        })
        for t in d["unknown"]:
            recurrence[t.lemma] = recurrence.get(t.lemma, 0) + 1
            other = d["unknown_count"] - 1
            cur = best.get(t.lemma)
            if cur is None or (other, d["index"]) < (cur["other_unknown_count"], cur["sentence_idx"]):
                best[t.lemma] = {
                    "sentence_idx": d["index"],
                    "other_unknown_count": other,
                    "start": d["start"], "end": d["end"],
                    "text": d["text"],
                    "surface": t.surface,
                    "reading": L.kata_to_hira(t.reading),
                    "pos": t.pos,
                }

    candidates = []
    for lem, b in best.items():
        if lem in learning or lem in already_carded:
            continue
        candidates.append({
            "lemma": lem,
            "reading": lemma_reading(lem),
            "surface": b["surface"],
            "pos": b["pos"],
            "freq_rank": freq.get(lem),
            "recurrence": recurrence[lem],
            "leverage": None,  # P1 gates this column
            "best": {k: b[k] for k in
                     ("sentence_idx", "other_unknown_count", "start", "end", "text")},
        })
    # Deterministic default order: true i+1 first, then frequency, then
    # recurrence. The live curate pass may re-weight (Q4).
    big = 10 ** 9
    candidates.sort(key=lambda c: (
        c["best"]["other_unknown_count"] > 0,
        c["freq_rank"] if c["freq_rank"] is not None else big,
        -c["recurrence"],
    ))
    dropped = max(0, len(candidates) - CANDIDATE_CAP)

    return {
        "episode_id": transcript["episode"]["id"],
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            **result["counts"],
            "total_sentences": result["total_sentences"],
            "token_comprehensibility": round(result["token_comprehensibility"], 4),
            "known_set_size": len(ks),
            "learning_size": len(learning),
            "unknown_lemmas": len(best),
            "candidates_dropped_by_cap": dropped,
        },
        "sentences": out_sentences,
        "candidates": candidates[:CANDIDATE_CAP],
        "reinforcement": [d["idx"] for d in out_sentences
                          if d["classification"] == "reinforcement"],
        "exposures": result["exposures"],
    }


def run_coverage(cfg, episode_id, refresh_known=False, record=True, conn=None):
    transcript = load_transcript(cfg, episode_id)
    conn = conn or lc.open_db(cfg["ledger_db"])

    known_bundle = lc.materialize_known(conn, cfg, force_refresh=refresh_known)
    freq = dict(conn.execute("SELECT lemma, rank FROM freq").fetchall())
    # Live cards only: a deleted card (user culled a sub-par one) reopens the
    # lemma for a fresh mining candidate — matters for still-wanted words.
    carded = {r[0] for r in conn.execute(
        "SELECT lemma FROM cards WHERE deleted_at IS NULL")}

    words_path = episode_dir(cfg, episode_id) / "words.json"
    words = read_json(words_path).get("words") if words_path.exists() else None

    cov = analyze(transcript, known_bundle, freq, carded, words=words)
    cov["known_sources"] = known_bundle["sources"]

    if record:
        r = lc.record_exposure(conn, transcript["episode"], cov["exposures"])
        cov["recorded"] = r
        # Coverage-at-watch snapshot — the difficulty confound control
        # (DESIGN.md — Taste metadata). Maturing signal: sharpens as the ledger
        # converges toward the true known-set.
        s = cov["stats"]
        lc.update_episode_meta(conn, episode_id, columns={
            "coverage_pct": s["token_comprehensibility"],
            "iplus1_count": s["i_plus_1"],
            "known_set_size": s["known_set_size"],
        })
    else:
        cov["recorded"] = None

    write_json(episode_dir(cfg, episode_id) / "coverage.json", cov)
    return cov


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("episode_id")
    ap.add_argument("--refresh-known", action="store_true")
    ap.add_argument("--no-record", action="store_true",
                    help="skip writing exposures to the ledger")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    cov = run_coverage(cfg, args.episode_id, refresh_known=args.refresh_known,
                       record=not args.no_record)
    s = cov["stats"]
    print(f"sentences={s['total_sentences']} comprehensibility="
          f"{s['token_comprehensibility']:.1%} i+1={s['i_plus_1']} "
          f"reinforcement={s['reinforcement']} candidates={len(cov['candidates'])}",
          file=sys.stderr)
    print(str(episode_dir(cfg, args.episode_id) / "coverage.json"))


if __name__ == "__main__":
    main()
