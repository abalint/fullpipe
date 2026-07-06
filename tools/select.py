#!/usr/bin/env python3
"""select — phone feedback + curated card pool → final card picks.

The curate pass authors picks.json as a POOL (viable, glossed, english'd card
candidates — more than a day's cap). This tool applies the user's pre-watch
feedback deterministically:

    known taps ("k")      → dropped from the pool (they know it; no card)
    high-interest ("h")   → jump the queue, in tap order
    everything else       → keeps its pool order
    cap                   → deck.new_cards_per_day

High-interest lemmas *outside* the pool are rescued from coverage candidates
when a sane sentence exists (other_unknown_count ≤ 1, clip 1.5–15s) — those
carry no curated english (flagged "rescued": true).

Writes <episode_dir>/feedback.json (the raw taps, audit trail) and
<episode_dir>/final_picks.json (what deck pushes at mark-watched).

CLI:
    python -m tools.select EPISODE_ID feedback.json [--config PATH]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib_config import load_config  # noqa: E402
from tools._staging import episode_dir, load_coverage, read_json, write_json  # noqa: E402

MIN_CLIP, MAX_CLIP = 1.5, 15.0


def select_picks(pool, coverage, taps, cap, standing_interest=()):
    """Pure selection. taps: [[lemma, "k"|"h"], ...] (others ignored).

    standing_interest: durable high-interest lemmas carried over from prior
    episodes (ledger tap_interest, minus known). They behave like this
    episode's own "h" taps — jumping the queue and getting rescued from
    coverage candidates when a clean sentence exists — but rank just behind
    a fresh tap. This is what makes a wanted word keep surfacing card-worthy
    across shows until it's learned."""
    known = {l for l, v in taps if v == "k"}
    tapped = [l for l, v in taps if v == "h"]
    fresh = set(tapped)
    # fresh taps first (tap order), then standing interest not re-tapped here
    interest = tapped + [l for l in standing_interest if l not in fresh]

    by_lemma = {p["lemma"]: p for p in pool}
    kept = [p for p in pool if p["lemma"] not in known]

    # rescue interest lemmas the curate pool didn't cover
    rescued = []
    covered = set(by_lemma)
    cand_by_lemma = {c["lemma"]: c for c in coverage.get("candidates", [])}
    for lem in interest:
        if lem in covered or lem in known:
            continue
        c = cand_by_lemma.get(lem)
        if not c:
            continue
        b = c["best"]
        if b["other_unknown_count"] > 1:
            continue
        if not (MIN_CLIP <= b["end"] - b["start"] <= MAX_CLIP):
            continue
        rescued.append({"lemma": lem, "sentence_idx": b["sentence_idx"],
                        "reading": c.get("reading"), "english": "",
                        "rescued": True})

    pri = {l: i for i, l in enumerate(interest)}
    ordered = sorted(
        kept + rescued,
        key=lambda p: (pri.get(p["lemma"], len(pri)),        # interest first, tap order
                       kept.index(p) if p in kept else 10**9)  # then pool order
    )
    return ordered[:cap]


def run_select(cfg, episode_id, taps, standing_interest=()):
    """Apply feedback for one episode. Returns the final picks.

    standing_interest: durable high-interest lemmas (ledger tap_interest minus
    known); sorted here so a given ledger state yields a stable card order."""
    ep_dir = episode_dir(cfg, episode_id)
    pool_path = ep_dir / "picks.json"
    pool = read_json(pool_path) if pool_path.exists() else []
    coverage = load_coverage(cfg, episode_id)
    cap = cfg.get("deck", {}).get("new_cards_per_day", 15)

    final = select_picks(pool, coverage, taps, cap, sorted(standing_interest))
    write_json(ep_dir / "feedback.json", {
        "taps": taps,
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    write_json(ep_dir / "final_picks.json", final)
    return final


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("episode_id")
    ap.add_argument("feedback", help='JSON file: {"taps": [[lemma, "k"|"h"], ...]}')
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    fb = json.loads(Path(args.feedback).read_text(encoding="utf-8"))
    from ledger import ledgerctl as lc
    conn = lc.open_db(cfg["ledger_db"])
    interest = lc.active_interest(conn)
    final = run_select(cfg, args.episode_id, fb["taps"], interest)
    print(f"selected {len(final)} cards", file=sys.stderr)
    print(str(episode_dir(cfg, args.episode_id) / "final_picks.json"))


if __name__ == "__main__":
    main()
