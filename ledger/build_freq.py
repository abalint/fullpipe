#!/usr/bin/env python3
"""Build the frequency prior from japaneseShowGraph.db show penetration (P7).

Ranks lemmas by the number of distinct shows containing them (~11k shows,
SudachiPy mode C — the ledger's exact join key). Penetration resists
one-show catchphrase inflation the way the phrases project validated with
its genre floor. Lemmas absent from the corpus fall back to the Leeds list
rank; lemmas absent from both stay NULL → the "rare/absent" θ-row.

Usage:
    python -m ledger.build_freq [--config PATH] [--db PATH]

One-off at bootstrap; rerun only if the show corpus is re-parsed. Once real
penetration numbers are visible, re-map ledgerctl.THETA_TABLE tiers from
rank counts to penetration percentiles (the "top ~2k" tier should sit on a
penetration cliff, not an arbitrary count).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib_config import load_config  # noqa: E402
from ledger.ledgerctl import open_db  # noqa: E402

# Same content-word filter as engine.lemma, applied to the show graph's full
# UniDic POS strings (e.g. 名詞-普通名詞-一般-*-*-*).
from engine.lemma import CONTENT_POS_PREFIXES, is_card_worthy  # noqa: E402


def build_penetration_ranks(show_graph_db):
    """Return [(lemma, penetration)] sorted by penetration desc."""
    src = sqlite3.connect(f"file:{show_graph_db}?mode=ro", uri=True)
    rows = src.execute(
        """SELECT m.dictionary_form, m.part_of_speech,
                  COUNT(DISTINCT sm.show_id) AS penetration
           FROM morphemes m
           JOIN show_morphemes sm ON sm.morpheme_id = m.id
           WHERE m.is_oov = 0
           GROUP BY m.dictionary_form"""
    ).fetchall()
    src.close()

    best = {}
    for lemma, pos, penetration in rows:
        if not pos or not any(pos.startswith(p) for p in CONTENT_POS_PREFIXES):
            continue
        if not is_card_worthy(lemma):
            continue
        if penetration > best.get(lemma, 0):
            best[lemma] = penetration

    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


def load_leeds(path):
    """Leeds ja_frequency.txt: one word per line, most frequent first."""
    ranks = {}
    p = Path(path)
    if not p.exists():
        return ranks
    with open(p, encoding="utf-8") as f:
        for rank, line in enumerate(f):
            word = line.strip()
            if word and word not in ranks:
                ranks[word] = rank
    return ranks


def build_freq(conn, show_graph_db, leeds_path=None):
    ranked = build_penetration_ranks(show_graph_db)
    conn.execute("DELETE FROM freq")
    conn.executemany(
        "INSERT INTO freq (lemma, rank, penetration, source) VALUES (?, ?, ?, 'show_graph')",
        [(lemma, rank, pen) for rank, (lemma, pen) in enumerate(ranked)],
    )

    leeds_added = 0
    if leeds_path:
        # P7: absent from the corpus → fall back to the Leeds rank as-is (the
        # θ tiers were originally scaled to that list), else NULL → rare tier.
        in_corpus = {lemma for lemma, _ in ranked}
        for word, rank in load_leeds(leeds_path).items():
            if word in in_corpus:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO freq (lemma, rank, penetration, source) "
                "VALUES (?, ?, NULL, 'leeds')",
                (word, rank),
            )
            leeds_added += 1

    # Refresh the cached freq_rank on existing lemma rows.
    conn.execute(
        """UPDATE lemmas SET freq_rank =
               (SELECT rank FROM freq WHERE freq.lemma = lemmas.lemma)"""
    )
    conn.commit()
    return {"show_graph_lemmas": len(ranked), "leeds_fallback": leeds_added}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db or cfg["ledger_db"]
    conn = open_db(db_path)

    freq_cfg = cfg.get("freq", {})
    show_db = freq_cfg.get("show_graph_db")
    if not show_db or not Path(show_db).exists():
        sys.exit(f"show_graph_db not found: {show_db!r} — set freq.show_graph_db in config.json")

    result = build_freq(conn, show_db, freq_cfg.get("leeds_fallback"))
    print(result)


if __name__ == "__main__":
    main()
