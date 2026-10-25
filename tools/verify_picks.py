"""Re-listen to every card pick on the GPU before an episode is staged.

ASR sentence spans are often wrong in ways the transcript can't show: whisper
segment `end` = next segment `start` (inflated spans), sentence STARTS after a
music gap sitting seconds early, a repaired target word that was never said.
For each entry in picks.json this cuts the sentence's exact span and a ±6 s
window, transcribes both on the GPU service, and prints the exact-span match
ratio plus a proposed tightened span found in the window's word timestamps.

    OK  #idx lemma span …  exact≥0.6
    BAD #idx lemma span …  exact<0.6     (+ "proposed a-b score=…")

`--apply` writes the proposed span for every BAD pick whose proposal scores
≥ 0.6 (and lands in 1.5–15 s) into transcript.json + sentences.srt, then
re-runs coverage. A BAD pick whose proposal is also bad should be dropped.
Promoted from the batch-curation scratchpads (Trinity Blood, 2026-10-16).

CLI:
    python -m tools.verify_picks EPISODE_ID [--apply] [--config PATH]
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import srt_parser as SP  # noqa: E402
from engine.transcriber import GpuTranscriber  # noqa: E402
from lib_config import load_config  # noqa: E402
from tools._staging import (episode_dir, load_transcript, read_json,  # noqa: E402
                            write_json)
from tools.deck import clip_match_ratio, _norm_ja  # noqa: E402

PASS = 0.6
WINDOW_S = 6.0


def _cut(audio, start, end, out):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
                    "-to", f"{end:.3f}", "-i", str(audio), "-ac", "1", "-ar", "16000",
                    str(out)], check=True)


def best_window(words, expected, base):
    """Slide over the window's words; return (start, end, score) of the
    contiguous run that best matches `expected`, penalizing overshoot."""
    exp = _norm_ja(expected)
    best = (None, None, 0.0)
    for i in range(len(words)):
        acc = ""
        for j in range(i, len(words)):
            acc += words[j].get("text", "")
            over = max(0, len(_norm_ja(acc)) - len(exp)) / max(1, len(exp))
            score = clip_match_ratio(expected, acc) - 0.5 * over
            if score > best[2]:
                best = (base + words[i]["start"], base + words[j]["end"], score)
            if len(_norm_ja(acc)) > len(exp) * 1.5:
                break
    return best


def verify(cfg, episode_id, apply=False):
    ep_dir = episode_dir(cfg, episode_id)
    transcript = load_transcript(cfg, episode_id)
    sents = {s["idx"]: s for s in transcript["sentences"]}
    audio = transcript["episode"]["audio"]
    picks = read_json(ep_dir / "picks.json")
    gpu = GpuTranscriber(cfg["asr"]["gpu_url"], token=cfg["asr"].get("gpu_token"))

    changes = {}
    with tempfile.TemporaryDirectory(prefix=f"verify_{episode_id}_") as tmp:
        tmp = Path(tmp)
        for p in picks:
            s = sents[p["sentence_idx"]]
            st, en, text = s["start"], s["end"], s["text"]
            exact = tmp / f"exact_{s['idx']}.wav"
            _cut(audio, st, en, exact)
            try:
                heard = "".join(w.get("text", "") for w in gpu.transcribe(exact, "ja"))
                r_exact = clip_match_ratio(text, heard)
            except Exception as e:  # GPU down → report, never block staging
                heard, r_exact = f"<err {e}>", 0.0
            wst, wen = max(0.0, st - WINDOW_S), en + WINDOW_S
            win = tmp / f"win_{s['idx']}.wav"
            _cut(audio, wst, wen, win)
            try:
                bs, be, br = best_window(gpu.transcribe(win, "ja"), text, wst)
            except Exception:
                bs, be, br = None, None, 0.0
            flag = "OK " if r_exact >= PASS else "BAD"
            print(f"{flag} #{s['idx']} {p['lemma']} span {st:.1f}-{en:.1f} "
                  f"({en - st:.1f}s) exact={r_exact:.2f} heard={heard[:40]!r}")
            if bs is not None:
                print(f"      proposed {bs:.1f}-{be:.1f} ({be - bs:.1f}s) score={br:.2f}")
                if r_exact < PASS and br >= PASS and 1.5 <= be - bs <= 15:
                    changes[s["idx"]] = (round(bs - 0.15, 3), round(be + 0.15, 3))

    if changes and apply:
        for idx, (ns, ne) in changes.items():
            sents[idx]["start"], sents[idx]["end"] = ns, ne
        write_json(ep_dir / "transcript.json", transcript)
        SP.write_srt([(s["start"], s["end"], s["text"]) for s in transcript["sentences"]],
                     ep_dir / "sentences.srt")
        print("APPLIED", changes)
        subprocess.run([sys.executable, "-m", "tools.coverage", episode_id],
                       cwd=str(Path(__file__).resolve().parent.parent), check=False)
    elif changes:
        print("would change:", changes, "(re-run with --apply)")
    return changes


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verify_picks", description=__doc__.split("\n")[0])
    ap.add_argument("episode_id")
    ap.add_argument("--apply", action="store_true",
                    help="write proposed spans for BAD picks (score ≥ 0.6)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    verify(load_config(args.config), args.episode_id, apply=args.apply)


if __name__ == "__main__":
    main()
