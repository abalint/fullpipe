#!/usr/bin/env python3
"""repair — subagent-driven transcript repair + vocabulary adjudication.

ASR damage poisons everything downstream of the transcript: a homophone
mistranscription (合法なりまして for 号砲鳴りまして) mints fake unknown
lemmas, garbled runs (ワンタイン) become "words", and names Sudachi's
dictionary doesn't know (いぶき) surface as mining candidates. A tokenizer
cannot catch any of this — it takes reading the sentence in context. This
tool is the deterministic half of that loop; a subagent (the /immerse skill,
Step 2.6) is the intelligent half.

    check EPISODE_ID   → export sentences (+ suspect-unknown hints when
                         coverage.json exists) to repair_blocks.json for the
                         subagent. Empty stdout when already repaired.
    apply EPISODE_ID F → ingest the subagent's repair_out.json: validate and
                         apply text edits sentence-by-sentence (bounded,
                         span-preserving — sentence count and timings never
                         change), rewrite sentences.srt + transcript.json,
                         and persist the name/non-word adjudications to
                         repair.json, which coverage folds into its
                         non-vocab set on every run.

Run order matters: the punctuation gate (tools.punctuate) re-derives
sentences from the raw subtitle blocks, so it must run BEFORE repair —
check refuses to export while a needed restore is still pending.
Re-run coverage after apply: token streams changed.

Subagent output shape (repair_out.json):
    {
      "edits":    [{"idx": 5, "old": "合法なりまして", "new": "号砲鳴りまして",
                    "why": "ASR homophone: race starting gun"}, ...],
      "names":    [{"surface": "いぶき", "kind": "person",
                    "note": "racer's given name"}, ...],
      "nonwords": ["ワンタイン", ...]
    }

CLI:
    python -m tools.repair check EPISODE_ID [--config PATH]
    python -m tools.repair apply EPISODE_ID REPAIR_JSON [--config PATH]
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import lemma as L  # noqa: E402
from engine import srt_parser as SP  # noqa: E402
from lib_config import load_config  # noqa: E402
from tools._staging import (episode_dir, load_transcript, read_json,  # noqa: E402
                            write_json)

BLOCKS_FILE = "repair_blocks.json"
REPAIR_FILE = "repair.json"

# An edit is a local fix, not a rewrite. Reject anything that grows/shrinks
# the sentence too much — a subagent that "improves" whole lines would
# desync the audio-span alignment word_align tolerates only in small doses.
MAX_LEN_DELTA = 15


def _suspects(cov):
    """Unknown lemmas with no freq_rank — the classic mistranscription
    signature ("common word" nobody has ever ranked) — plus every unknown's
    sentence, as focus hints for the subagent."""
    ranked = {c["lemma"]: c.get("freq_rank") for c in cov.get("candidates", [])}
    out = []
    for s in cov.get("sentences", []):
        for lem in s.get("unknown", []):
            out.append({"lemma": lem, "idx": s["idx"], "text": s["text"],
                        "freq_rank": ranked.get(lem)})
    return out


def cmd_check(cfg, episode_id, log):
    """Print the blocks path to stdout iff repair hasn't run; else nothing."""
    transcript = load_transcript(cfg, episode_id)
    if transcript.get("repair_applied"):
        log(f"ok: repair already applied "
            f"(source={transcript.get('repair_source', '?')})")
        return

    # Punctuation first — its apply re-derives sentences from raw blocks and
    # would clobber any repairs made before it.
    from tools.punctuate import needs_restore, raw_blocks
    try:
        needed, already = needs_restore(transcript, raw_blocks(transcript))
    except (KeyError, FileNotFoundError, OSError):
        needed = already = False  # no raw SRT to re-derive from → no clobber risk
    if needed and not already:
        raise SystemExit("punctuation restore pending — run the punctuation "
                         "gate (tools.punctuate) before repair")

    ep_dir = episode_dir(cfg, episode_id, create=True)
    payload = {
        "episode_id": episode_id,
        "sentences": [{"idx": s["idx"], "text": s["text"]}
                      for s in transcript["sentences"]],
    }
    cov_path = ep_dir / "coverage.json"
    if cov_path.exists():
        payload["suspects"] = _suspects(read_json(cov_path))
    blocks_path = ep_dir / BLOCKS_FILE
    write_json(blocks_path, payload)
    log(f"exported {len(payload['sentences'])} sentences"
        + (f" + {len(payload.get('suspects', []))} suspect hints"
           if "suspects" in payload else "")
        + " for a subagent")
    print(blocks_path)


def _validate_edits(edits, sentences):
    """Split subagent edits into (applicable, rejected-with-reason).

    An edit applies to exactly one sentence (idx), replaces text actually
    present there, and stays local (MAX_LEN_DELTA)."""
    ok, rejected = [], []
    n = len(sentences)
    for e in edits or []:
        idx, old, new = e.get("idx"), e.get("old") or "", e.get("new") or ""
        why = None
        if not isinstance(idx, int) or not (0 <= idx < n):
            why = f"idx {idx!r} out of range"
        elif not old:
            why = "empty old"
        elif old == new:
            why = "no-op"
        elif "\n" in new:
            why = "newline in replacement"
        elif old not in sentences[idx]["text"]:
            why = f"old not found in sentence {idx}"
        elif abs(len(new) - len(old)) > MAX_LEN_DELTA:
            why = f"len delta {abs(len(new) - len(old))} > {MAX_LEN_DELTA}"
        if why:
            rejected.append({**e, "reason": why})
        else:
            ok.append(e)
    return ok, rejected


def _non_vocab_lemmas(names, nonwords):
    """Expand adjudicated strings into the lemma/surface keys coverage
    excludes. Each string contributes itself plus every content token's
    lemma (矢崎ヒカル → 矢崎, ヒカル) so membership survives however the
    surrounding sentence tokenizes."""
    keys = set()
    for s in list(names) + list(nonwords):
        s = (s or "").strip()
        if not s:
            continue
        keys.add(s)
        for t in L.tokenize(s):
            if L.is_content_word(t.pos) and L.is_card_worthy(t.lemma):
                keys.add(t.lemma)
    return sorted(keys)


def cmd_apply(cfg, episode_id, repair_json, log):
    transcript = load_transcript(cfg, episode_id)
    data = read_json(repair_json)
    sentences = transcript["sentences"]

    edits, rejected = _validate_edits(data.get("edits"), sentences)
    for e in edits:
        s = sentences[e["idx"]]
        s["text"] = s["text"].replace(e["old"], e["new"])

    names = [n if isinstance(n, str) else n.get("surface", "")
             for n in data.get("names") or []]
    nonwords = [str(w) for w in data.get("nonwords") or []]
    non_vocab = _non_vocab_lemmas(names, nonwords)

    ep_dir = episode_dir(cfg, episode_id)
    if edits:
        SP.write_srt([(s["start"], s["end"], s["text"]) for s in sentences],
                     ep_dir / "sentences.srt")
    transcript["repair_applied"] = True
    transcript["repair_source"] = "subagent"
    write_json(ep_dir / "transcript.json", transcript)

    write_json(ep_dir / REPAIR_FILE, {
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "edits": edits,
        "edits_rejected": rejected,
        "names": data.get("names") or [],
        "nonwords": nonwords,
        "non_vocab": non_vocab,
    })
    (ep_dir / BLOCKS_FILE).unlink(missing_ok=True)

    log(f"applied {len(edits)} edits ({len(rejected)} rejected), "
        f"{len(non_vocab)} non-vocab keys ({ep_dir / REPAIR_FILE})")
    for r in rejected:
        log(f"  rejected: idx={r.get('idx')} {r.get('old', '')!r}: {r['reason']}")
    if edits or non_vocab:
        log("re-run coverage next — token streams changed")


def load_non_vocab(cfg, episode_id):
    """The episode's adjudicated non-vocab keys, or an empty set. Coverage
    calls this on every run so repair survives re-analysis."""
    path = episode_dir(cfg, episode_id) / REPAIR_FILE
    if not path.exists():
        return frozenset()
    return frozenset(read_json(path).get("non_vocab", ()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="export sentences (+hints) for a subagent")
    c.add_argument("episode_id")
    a = sub.add_parser("apply", help="ingest subagent repairs; rewrite artifacts")
    a.add_argument("episode_id")
    a.add_argument("repair_json", help="the subagent's repair output (JSON)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if args.cmd == "check":
        cmd_check(cfg, args.episode_id, log)
    else:
        cmd_apply(cfg, args.episode_id, args.repair_json, log)


if __name__ == "__main__":
    main()
