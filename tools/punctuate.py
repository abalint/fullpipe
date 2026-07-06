#!/usr/bin/env python3
"""punctuate — subagent-driven punctuation restore for an acquired episode.

The worker's OpenAI restore (engine.punctuation) runs unattended in Stage 1.
When Claude is live (the /immerse skill, Stage 2) a subagent can punctuate
instead — better than gpt-4o-mini and with no OpenAI dependency. This tool is
the deterministic half of that loop; the subagent is the intelligent half.

    check EPISODE_ID   → is a restore needed and not yet done? If so, export the
                         raw cleaned blocks to punct_blocks.json for the subagent.
    apply EPISODE_ID F → ingest the subagent's punctuated blocks (F), run them
                         through the SAME insert-only diff the OpenAI path uses
                         (so text stays byte-identical to the audio spans),
                         re-merge to sentences, rewrite sentences.srt +
                         transcript.json.

Because apply reuses engine.punctuation._extract_punct_insertions, a subagent
that rewrites words instead of only inserting 。？！ can't corrupt the audio
alignment — every non-punctuation edit is discarded, exactly as with OpenAI.

CLI:
    python -m tools.punctuate check EPISODE_ID [--config PATH]
    python -m tools.punctuate apply EPISODE_ID BLOCKS_JSON [--config PATH]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import srt_parser as SP  # noqa: E402
from engine.punctuation import (_extract_punct_insertions,  # noqa: E402
                                get_language_config)
from lib_config import load_config  # noqa: E402
from tools._staging import (episode_dir, load_transcript, read_json,  # noqa: E402
                            write_json)

JA = get_language_config("Japanese")
BLOCKS_FILE = "punct_blocks.json"


def clean_subs(subs):
    """Dedup scrolling auto-subs and drop non-speech blocks (mirrors acquire)."""
    return SP.filter_non_speech(SP.deduplicate_scrolling_subs(subs))


def raw_blocks(transcript):
    """The cleaned raw subtitle blocks a restore operates on.

    Rebuilt from the raw SRT (not transcript["sentences"], which may already be
    the choppy duration/length-capped fallback) so restore starts from the same
    input acquire's segment() saw.
    """
    raw_srt = transcript["episode"]["raw_srt"]
    return clean_subs(SP.parse_srt(raw_srt))


def needs_restore(transcript, subs):
    """(needed, already) — was punctuation lacking, and has it been restored?"""
    needed = not SP.has_good_punctuation(subs, JA["sentence_punct"])
    already = bool(transcript.get("punctuation_restored"))
    return needed, already


def cmd_check(cfg, episode_id, log):
    """Print the blocks path to stdout iff a restore is needed; else nothing.

    Empty stdout is the skill's "skip" signal; a path is its "punctuate this"
    signal. Human-readable status always goes to stderr via log.
    """
    transcript = load_transcript(cfg, episode_id)
    subs = raw_blocks(transcript)
    needed, already = needs_restore(transcript, subs)

    if not needed:
        log("ok: subtitles already have punctuation — no restore needed")
        return
    if already:
        src = transcript.get("punctuation_source", "openai")
        log(f"ok: punctuation already restored (source={src})")
        return

    ep_dir = episode_dir(cfg, episode_id, create=True)
    blocks_path = ep_dir / BLOCKS_FILE
    write_json(blocks_path, [{"idx": i, "text": t} for i, (_, _, t) in enumerate(subs)])
    log(f"restore needed: {len(subs)} blocks exported for a subagent")
    print(blocks_path)  # stdout = the one machine-readable result


def _load_punctuated(path, n):
    """Read the subagent's output into a list of n texts aligned to raw blocks.

    Accepts [{"idx","text"}, ...] (idx-keyed, order-independent) or a bare
    ["text", ...] list. Raises on any count/index mismatch so the caller can
    re-run the subagent rather than silently misalign audio spans.
    """
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(data).__name__}")
    if len(data) != n:
        raise ValueError(
            f"{path}: got {len(data)} blocks, expected {n} — the subagent must "
            "return exactly one entry per input block, same order")

    if data and isinstance(data[0], dict):
        texts = [None] * n
        for entry in data:
            i = entry["idx"]
            if not (0 <= i < n) or texts[i] is not None:
                raise ValueError(f"{path}: bad or duplicate idx {i}")
            texts[i] = entry["text"]
        if any(t is None for t in texts):
            raise ValueError(f"{path}: missing idx after load")
        return texts
    return [str(t) for t in data]


def cmd_apply(cfg, episode_id, blocks_json, log):
    transcript = load_transcript(cfg, episode_id)
    subs = raw_blocks(transcript)
    punctuated = _load_punctuated(blocks_json, len(subs))
    punct_chars = JA["punct_chars"]

    # Insert-only diff per block: keep the original text exactly, absorb only the
    # sentence-ending marks the subagent added. Block boundaries == audio-span
    # boundaries, so punctuation never migrates across a timestamp.
    restored = []
    added = 0
    for (start, end, orig), punct in zip(subs, punctuated):
        safe = _extract_punct_insertions(orig, punct, punct_chars)
        added += sum(safe.count(c) for c in punct_chars) - sum(orig.count(c) for c in punct_chars)
        restored.append((start, end, safe))

    sentences = SP.merge_to_sentences(restored, JA["sentence_enders"])

    ep_dir = episode_dir(cfg, episode_id)
    SP.write_srt(sentences, ep_dir / "sentences.srt")
    transcript["punctuation_restored"] = True
    transcript["punctuation_source"] = "subagent"
    transcript["sentences"] = [
        {"idx": i, "start": round(s, 3), "end": round(e, 3), "text": t}
        for i, (s, e, t) in enumerate(sentences)
    ]
    write_json(ep_dir / "transcript.json", transcript)
    (ep_dir / BLOCKS_FILE).unlink(missing_ok=True)
    log(f"restored {added} marks over {len(subs)} blocks → {len(sentences)} "
        f"sentences ({ep_dir/'sentences.srt'})")
    log("re-run coverage next — sentence indices have changed")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="detect a needed-but-undone restore; export blocks")
    c.add_argument("episode_id")
    a = sub.add_parser("apply", help="ingest subagent-punctuated blocks; rewrite artifacts")
    a.add_argument("episode_id")
    a.add_argument("blocks_json", help="the subagent's punctuated blocks (JSON)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if args.cmd == "check":
        cmd_check(cfg, args.episode_id, log)
    else:
        cmd_apply(cfg, args.episode_id, args.blocks_json, log)


if __name__ == "__main__":
    main()
