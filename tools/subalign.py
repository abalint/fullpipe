"""Check and fix the timing of found Japanese subtitles against an episode's audio.

Subtitles from our subs store / kitsunekko / jimaku are often timed to a
different cut than the rip in the library (TV-broadcast captions vs a CM-cut
fansub rip, a streaming cut vs a BD). This measures the offset on the GPU
ASR service and retimes the file:

  1. quick check — ASR of ~10 one-minute windows spread over the episode;
     each ASR segment is matched to the cue whose text it contains (char
     bigram coverage) within ±max_shift seconds; each window gets a
     median offset.
  2. all windows agree (spread ≤ 0.6 s) → one constant shift (or none if
     |shift| < 0.25 s).
  3. they disagree → piecewise: ASR of the whole episode in 2-min chunks,
     the same matching for every cue, a running median over matched cues,
     and every cue takes the offset of its nearest matched neighbours.

    python -m tools.subalign check <video.mp4> <subs.srt>
    python -m tools.subalign fix   <video.mp4> <subs.srt> [--out aligned.srt]

`fix` keeps the input as <subs>.orig.srt when writing in place. The report
(JSON on stdout) says what was done and how well the retimed file matches the
audio (`after.match_rate`): low rates mean the subs are for a different
episode or cut — look before using them. ASR results are cached (beside the
video, or `--cache <file>`) so check → fix, or trying a second candidate
subtitle file for the same episode, doesn't re-transcribe.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

TS = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


# --- srt ---------------------------------------------------------------------------------

def parse_srt(text):
    """[{start, end, text}] — tolerant of missing indices, stray blank lines
    and `00:` debris lines inside blocks."""
    cues, cur = [], None
    for line in text.replace("\r", "").split("\n"):
        m = TS.search(line)
        if m:
            g = [int(x) for x in m.groups()]
            st = g[0] * 3600 + g[1] * 60 + g[2] + int(str(g[3]).ljust(3, "0")[:3]) / 1000
            en = g[4] * 3600 + g[5] * 60 + g[6] + int(str(g[7]).ljust(3, "0")[:3]) / 1000
            cur = {"start": st, "end": en, "lines": []}
            cues.append(cur)
        elif cur is not None and line.strip() and not line.strip().isdigit() and line.strip() != "00:":
            cur["lines"].append(line.strip())
    return [{"start": c["start"], "end": c["end"], "text": "\n".join(c["lines"])} for c in cues if c["lines"]]


def fmt_ts(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues, path):
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}\n{c['text']}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")


# --- text matching -----------------------------------------------------------------------

def norm(text):
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"[（(][^）)]{0,20}[）)]", "", t)        # speaker names （真理子）
    t = re.sub(r"[<＜][^>＞]{0,20}[>＞]", "", t)
    return "".join(ch for ch in t if ch.isalnum())


def bigrams(s):
    return [s[i:i + 2] for i in range(len(s) - 1)]


def coverage(cue_n, seg_n):
    """Share of the cue's bigrams found in the segment."""
    cb = bigrams(cue_n)
    if not cb:
        return 0.0
    sb = set(bigrams(seg_n))
    return sum(1 for b in cb if b in sb) / len(cb)


def position_in(cue_n, seg_n):
    """Where in the segment text the cue starts (0..1), by its first
    well-matching bigram run."""
    best, best_i = 0, 0
    for i in range(len(seg_n) - 1):
        k = 0
        while i + k < len(seg_n) and k < len(cue_n) and seg_n[i + k] == cue_n[k]:
            k += 1
        if k > best:
            best, best_i = k, i
    if best < 2:  # the cue's head wasn't heard verbatim — locate any of its bigrams
        cb = bigrams(cue_n)
        for i in range(len(seg_n) - 1):
            if seg_n[i:i + 2] in cb[:4]:
                return i / max(1, len(seg_n))
        return 0.0
    return best_i / max(1, len(seg_n))


def match_pairs(cues, segs, max_shift, min_cov=0.6, min_len=4, precise=False):
    """[(cue_index, offset)]: each cue matched to the ASR segment that best
    contains its text, among segments within ±max_shift of the cue's time.
    offset = audio time − subtitle time."""
    segs = [dict(s, n=norm(s["text"])) for s in segs if s.get("text")]
    segs.sort(key=lambda s: s["start"])
    starts = [s["start"] for s in segs]
    import bisect
    pairs = []
    for i, c in enumerate(cues):
        cn = norm(c["text"])
        if len(cn) < min_len:
            continue
        lo = bisect.bisect_left(starts, c["start"] - max_shift - 30)
        hi = bisect.bisect_right(starts, c["start"] + max_shift + 30)
        scored = []
        for s in segs[lo:hi]:
            cov = coverage(cn, s["n"])
            if cov >= min_cov:
                scored.append((cov, s))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        # ambiguous (two different places say the same thing) → skip
        if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05 and abs(scored[1][1]["start"] - scored[0][1]["start"]) > 3:
            continue
        s = scored[0][1]
        pos = position_in(cn, s["n"])
        if precise and pos > 0.08 and len(s["n"]) > 1.6 * len(cn):
            continue  # cue sits mid-segment: its start time would only be interpolated
        t = s["start"] + pos * (s["end"] - s["start"])
        off = t - c["start"]
        if abs(off) <= max_shift:
            pairs.append((i, off))
    return pairs


# --- ASR ---------------------------------------------------------------------------------

def _gpu(cfg):
    from engine.transcriber import GpuTranscriber
    asr = cfg.get("asr", {})
    return GpuTranscriber(asr["gpu_url"], token=asr.get("gpu_token"))


def duration_of(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip())


def asr_window(cfg, video, start, dur, cache):
    key = f"{start:.1f}+{dur:.1f}"
    if key in cache:
        return cache[key]
    with tempfile.TemporaryDirectory(prefix="subalign_") as tmp:
        part = Path(tmp) / "w.mp3"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                        "-t", str(dur), "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-b:a", "48k", str(part)], check=True)
        try:
            words = _gpu(cfg)._transcribe_one(part, "ja")
        except Exception as ex:  # silence → no words; anything else bubbles as empty + note
            if "no words" in str(ex) or "no speech" in str(ex):
                words = []
            else:
                raise
    segs = [{"start": w["start"] + start, "end": w["end"] + start, "text": w.get("text") or w.get("word") or ""}
            for w in words if isinstance(w.get("start"), (int, float))]
    cache[key] = segs
    return segs


# --- the two passes ----------------------------------------------------------------------

def quick_windows(dur, n=10, length=60.0):
    usable = max(dur - 2 * length, length)
    return [(length + usable * (k + 0.5) / n - length / 2, length) for k in range(n)]


def full_windows(dur, length=120.0):
    out, t = [], 0.0
    while t < dur - 1:
        out.append((t, min(length, dur - t)))
        t += length
    return out


def analyse(cfg, video, cues, max_shift=600.0, force_full=False, cache_path=None, drift=False):
    cache_p = Path(cache_path) if cache_path else Path(video).with_suffix(".subalign_asr.json")
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    dur = duration_of(video)
    report = {"duration": round(dur, 1), "cues": len(cues)}
    win_offsets = []
    if not force_full:
        for (st, ln) in quick_windows(dur):
            segs = asr_window(cfg, video, st, ln, cache)
            near = [c for c in cues if st - max_shift - 10 <= c["start"] <= st + ln + max_shift + 10]
            idx = {id(c): i for i, c in enumerate(cues)}
            pairs = match_pairs(near, segs, max_shift)
            offs = [o for _, o in pairs]
            if len(offs) >= 2:
                win_offsets.append({"at": round(st), "offset": round(statistics.median(offs), 2), "pairs": len(offs)})
        cache_p.write_text(json.dumps(cache, ensure_ascii=False))
        report["windows"] = win_offsets
        meds = [w["offset"] for w in win_offsets]
        if len(meds) >= 5 and max(meds) - min(meds) <= 0.6:
            shift = statistics.median(meds)
            report.update(mode="none" if abs(shift) < 0.25 else "constant", shift=round(shift, 2))
            report["offsets"] = [shift] * len(cues)
            return report, cache
        if len(meds) < 5:
            report["note"] = f"only {len(meds)} of 10 windows matched — trying the full pass"
    # piecewise
    for (st, ln) in full_windows(dur):
        asr_window(cfg, video, st, ln, cache)
    cache_p.write_text(json.dumps(cache, ensure_ascii=False))
    # match against every ASR pass cached for this episode (quick windows +
    # full chunks): differently-cut windows hear the same line slightly
    # differently, and the extra evidence separates small (5-s) cuts
    seen, segs = set(), []
    for v in cache.values():
        for sg in v:
            k = (round(sg["start"], 1), sg["text"])
            if k not in seen:
                seen.add(k); segs.append(sg)
    pairs = match_pairs(cues, segs, max_shift, precise=True)
    report["pairs"] = len(pairs)
    if len(pairs) < max(10, len(cues) // 10):
        report.update(mode="failed", note=f"only {len(pairs)} cues matched the audio — wrong episode or cut?")
        return report, cache
    # --drift: a rip and a broadcast can run at slightly different speeds
    # (frame-rate conversion) — offsets then drift linearly under the cuts.
    # Off by default: small real cuts (5-s sponsor cards, Kinpachi) look like
    # drift in-sample and the line model then merges them wrongly.
    def fit_err(out):
        return statistics.median(abs(o - out[i]) for i, o in pairs)
    out, plateaus = piecewise_offsets(cues, pairs)
    slope, icpt = drift_fit([(cues[i]["start"], o) for i, o in pairs]) if drift else (0.0, 0.0)
    if drift and abs(slope) * (cues[-1]["start"] - cues[0]["start"]) >= 0.5:
        resid = [(i, o - (icpt + slope * cues[i]["start"])) for i, o in pairs]
        d_out, d_plat = piecewise_offsets(cues, resid)
        d_out = [o + icpt + slope * c["start"] for o, c in zip(d_out, cues)]
        if fit_err(d_out) < fit_err(out) - 0.05:
            for p in d_plat:  # report the offset at each stretch's start
                p[0] = round(p[0] + icpt + slope * p[1], 2)
            out, plateaus = d_out, d_plat
            report["drift_s_per_hour"] = round(slope * 3600, 2)
    # cuts only ever move the offset one way (a CM-cut rip drops time, an
    # extended cut adds it); offsets that swing back and forth are matches
    # against the wrong lines, not a timing map
    steps = [b[0] - a[0] for a, b in zip(plateaus, plateaus[1:])]
    trend = sum(steps)
    against = sum(abs(x) for x in steps if abs(x) > 1.5 and (x > 0) != (trend > 0))
    if against > 8.0:  # a few seconds back can be a real edit; tens of seconds is the wrong subs
        report.update(mode="failed", plateaus=plateaus,
                      note="offsets swing back and forth — these subs don't follow this rip")
        return report, cache
    report.update(mode="piecewise", plateaus=plateaus)
    report["offsets"] = out
    return report, cache


def piecewise_offsets(cues, pairs, jump=2.5, min_run=4, small_jump=4.0, small_run=10):
    """One constant offset per stretch between cuts. Stretches are runs of
    matched cues whose (median-smoothed) offsets stay within `jump` s; runs
    shorter than `min_run` are noise and fold into a neighbour. Each stretch
    takes the median of its raw offsets; an unmatched cue takes the stretch
    of the nearest matched cue (by subtitle time). Returns (per-cue offsets,
    [[offset, first_cue_start_s, n_matched]])."""
    import bisect
    # drop outlier matches (a line matched to the wrong place): keep a pair
    # only when it sits within 2 s of the median of its ±6 neighbours
    offs = [o for _, o in pairs]
    pairs = [p for j, p in enumerate(pairs)
             if abs(p[1] - statistics.median(offs[max(0, j - 6): j + 7])) <= 2.0] or pairs
    idxs = [i for i, _ in pairs]
    raw = [o for _, o in pairs]
    runs = changepoint_runs(raw, min_run=min_run)
    changed = True
    while changed and len(runs) > 1:
        changed = False
        meds = [statistics.median(raw[j] for j in run) for run in runs]
        for r in range(len(runs)):
            nb = [abs(meds[r] - meds[x]) for x in (r - 1, r + 1) if 0 <= x < len(runs)]
            # a short run is noise; so is a small jump (< small_jump s) without
            # strong support — real cuts (CM breaks, trims) are big
            if len(runs[r]) < min_run or (min(nb) < small_jump and len(runs[r]) < small_run):
                med = statistics.median(raw[j] for j in runs[r])
                cands = [x for x in (r - 1, r + 1) if 0 <= x < len(runs)]
                tgt = min(cands, key=lambda x: abs(statistics.median(raw[j] for j in runs[x]) - med))
                runs[tgt] = sorted(runs[tgt] + runs[r]); del runs[r]; changed = True
                break
        # adjacent runs that ended up with the same offset merge
        for r in range(len(runs) - 1):
            a = statistics.median(raw[j] for j in runs[r]); b = statistics.median(raw[j] for j in runs[r + 1])
            if abs(a - b) <= jump:
                runs[r] = runs[r] + runs[r + 1]; del runs[r + 1]; changed = True
                break
    # cuts move the offset one way only; a weakly supported run against the
    # episode's direction (a next-episode preview repeating earlier lines,
    # a recap) is mismatches — fold it into its nearest neighbour
    def med(r):
        return statistics.median(raw[j] for j in r)
    strong = [r for r in runs if len(r) >= small_run]
    trend = sum(med(b) - med(a) for a, b in zip(strong, strong[1:]))
    changed = trend != 0
    while changed and len(runs) > 1:
        changed = False
        for r in range(1, len(runs)):
            step = med(runs[r]) - med(runs[r - 1])
            if abs(step) > 1.5 and (step > 0) != (trend > 0):
                weak = r if len(runs[r]) < len(runs[r - 1]) else r - 1
                if len(runs[weak]) >= small_run:
                    continue  # well supported both sides — analyse() reports it
                cands = [x for x in (weak - 1, weak + 1) if 0 <= x < len(runs)]
                tgt = min(cands, key=lambda x: abs(med(runs[x]) - med(runs[weak])))
                runs[tgt] = sorted(runs[tgt] + runs[weak]); del runs[weak]; changed = True
                break
    run_of = {}
    plateaus = []
    for r in runs:
        val = statistics.median(raw[j] for j in r)
        for j in r:
            run_of[j] = val
        plateaus.append([round(val, 2), round(cues[idxs[r[0]]]["start"]), len(r)])
    out = []
    for i, c in enumerate(cues):
        j = bisect.bisect_left(idxs, i)
        if j < len(idxs) and idxs[j] == i:
            out.append(run_of[j]); continue
        cand = [x for x in (j - 1, j) if 0 <= x < len(idxs)]
        best = min(cand, key=lambda x: abs(cues[idxs[x]]["start"] - c["start"]))
        out.append(run_of[best])
    # place each cut where the raw subtitles have their gap: a rip that drops
    # a CM break of L seconds needs a ≥ L-s silence in the broadcast captions
    # at that point — otherwise the stretches either side overlap in time
    for a_run, b_run in zip(runs, runs[1:]):
        a = run_of[a_run[0]]; b = run_of[b_run[0]]
        if a - b <= 3.0:
            continue  # time added or a tiny step: nearest-neighbour is fine
        lo = max(0, idxs[a_run[-1]] - 3); hi = min(len(cues) - 1, idxs[b_run[0]] + 3)
        if hi <= lo:
            continue
        g = max(range(lo, hi), key=lambda k: cues[k + 1]["start"] - cues[k]["end"])
        for k in range(lo, hi + 1):
            out[k] = a if k <= g else b
    return out, plateaus


def drift_fit(pts, sample=4000):
    """Robust line offset = icpt + slope·t (Theil–Sen on a sample of point
    pairs far enough apart in time; intercept = median residual)."""
    import random
    if len(pts) < 10:
        return 0.0, 0.0
    rnd = random.Random(0)
    slopes = []
    for _ in range(sample):
        (t1, o1), (t2, o2) = rnd.sample(pts, 2)
        if abs(t2 - t1) > 120:
            slopes.append((o2 - o1) / (t2 - t1))
    if not slopes:
        return 0.0, 0.0
    slope = statistics.median(slopes)
    icpt = statistics.median(o - slope * t for t, o in pts)
    return slope, icpt


def changepoint_runs(raw, penalty=6.0, min_run=4):
    """Optimal step-function fit (L1, dynamic programming): split the matched
    offsets into runs minimising Σ|offset − run median| + penalty per extra
    run. Robust to the scattered mismatches that break greedy smoothing, and
    finds small (5-s) cuts that a jump threshold misses."""
    n = len(raw)
    if n < 2 * min_run:
        return [list(range(n))]
    # cost[i][j]: L1 deviation of raw[i:j] from its median, for runs ≥ min_run
    import bisect
    best = [0.0] + [float("inf")] * n
    prev = [0] * (n + 1)
    for i in range(n):
        if best[i] == float("inf"):
            continue
        srt, tot_cost = [], 0.0
        for j in range(i, n):
            bisect.insort(srt, raw[j])
            if j + 1 - i < min_run:
                continue
            med = srt[len(srt) // 2]
            c = sum(abs(x - med) for x in srt)
            cand = best[i] + c + penalty
            if cand < best[j + 1]:
                best[j + 1], prev[j + 1] = cand, i
    cuts, j = [], n
    while j > 0:
        cuts.append((prev[j], j)); j = prev[j]
    return [list(range(a, b)) for a, b in reversed(cuts)]


def residual(cues, segs):
    """After retiming: offsets of precisely matched cues (should hug 0)."""
    pairs = match_pairs(cues, segs, 10.0, precise=True)
    offs = sorted(abs(o) for _, o in pairs)
    if not offs:
        return {"pairs": 0}
    return {"pairs": len(offs), "median_abs": round(statistics.median(offs), 2),
            "p90_abs": round(offs[int(0.9 * (len(offs) - 1))], 2),
            "within_1s": round(sum(o <= 1.0 for o in offs) / len(offs), 2)}


def verify(cfg, video, cues, cache, max_shift=3.0):
    """After retiming: share of cues in the quick windows whose text is heard
    within ±3 s of their new time."""
    dur = duration_of(video)
    hit = tot = 0
    for (st, ln) in quick_windows(dur):
        segs = asr_window(cfg, video, st, ln, cache)
        near = [c for c in cues if st + 2 <= c["start"] <= st + ln - 4 and len(norm(c["text"])) >= 4]
        if not near:
            continue
        pairs = match_pairs(near, segs, max_shift, min_cov=0.5)
        hit += len(pairs)
        tot += len(near)
    return {"checked": tot, "heard": hit, "match_rate": round(hit / tot, 2) if tot else None}


def retime_episode(cfg, episode_id, raw_srt, aligned_srt, video, cache_path, apply=False, min_delta=1.0):
    """Fix the timing of an already-curated episode whose subtitles were
    aligned by an older version: re-align the raw (store) subs, and shift each
    transcript sentence by (new − old) offset of the caption cue it came from.
    Text, sentence indices and card picks are untouched — pick sentences were
    tuned against the audio by verify_picks, so they never move. Writes only
    when the sentences' fit to the ASR improves."""
    from tools._staging import episode_dir, read_json, write_json
    import bisect
    raw = parse_srt(Path(raw_srt).read_text(encoding="utf-8-sig", errors="replace"))
    old = parse_srt(Path(aligned_srt).read_text(encoding="utf-8-sig", errors="replace"))
    # old aligned cue k ↔ raw cue by order + text
    m, r = {}, 0
    for k, c in enumerate(old):
        while r < len(raw) and norm(raw[r]["text"]) != norm(c["text"]):
            r += 1
        if r < len(raw):
            m[k] = r; r += 1
    rep, cache = analyse(cfg, video, raw, cache_path=cache_path, force_full=True)
    if rep.get("mode") not in ("piecewise", "constant", "none"):
        return {"applied": False, "why": f"re-alignment {rep.get('mode')}"}
    new_off = rep["offsets"]
    starts = [c["start"] for c in old]
    def delta_at(t):
        k = max(0, bisect.bisect_right(starts, t + 0.05) - 1)
        if k not in m:
            return 0.0
        return new_off[m[k]] - (old[k]["start"] - raw[m[k]]["start"])
    ep = episode_dir(cfg, episode_id)
    tr = read_json(ep / "transcript.json")
    sents = tr["sentences"]
    picked = set()
    if (ep / "picks.json").exists():
        picked = {p["sentence_idx"] for p in read_json(ep / "picks.json")}
    seen, segs = set(), []
    for v in cache.values():
        for sg in v:
            key = (round(sg["start"], 1), sg["text"])
            if key not in seen:
                seen.add(key); segs.append(sg)
    def fit(ss):
        cues = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in ss]
        return residual(cues, segs)
    before = fit(sents)
    # sentences come from the old cues in order, and their starts are cue
    # starts — walk both lists in step (the old times are what is out of
    # order, so a time search would land on the wrong cue)
    owner, k = [], 0
    for s0 in sents:
        hit = next((j for j in range(k, min(k + 40, len(old)))
                    if abs(old[j]["start"] - s0["start"]) < 0.06), None)
        if hit is not None:
            k = hit
        owner.append(k)
    def delta_of(i):
        kk = owner[i]
        if kk not in m:
            return 0.0
        return new_off[m[kk]] - (old[kk]["start"] - raw[m[kk]]["start"])
    new_sents, moved = [], 0
    for i, s0 in enumerate(sents):
        s1 = dict(s0)
        if i not in picked:
            d = delta_of(i)
            if abs(d) > min_delta:
                s1["start"] = round(s0["start"] + d, 3); s1["end"] = round(s0["end"] + d, 3); moved += 1
        new_sents.append(s1)
    after = fit(new_sents)
    def backward(ss):  # sentences that start >1 s before their predecessor: a misplaced cut
        return sum(1 for x, y in zip(ss, ss[1:]) if y["start"] < x["start"] - 1.0)
    before["backward"], after["backward"] = backward(sents), backward(new_sents)
    better = ((after.get("within_1s") or 0) > (before.get("within_1s") or 0) + 0.02
              or (after["backward"] < before["backward"]
                  and (after.get("within_1s") or 0) >= (before.get("within_1s") or 0) - 0.02))
    out = {"moved": moved, "of": len(sents), "before": before, "after": after,
           "plateaus": rep.get("plateaus"), "applied": False}
    if apply and better and moved:
        import shutil
        for f in ("transcript.json", "sentences.srt"):
            bak = ep / (f + ".pre-retime")
            if not bak.exists():
                shutil.copy2(ep / f, bak)
        tr["sentences"] = new_sents
        write_json(ep / "transcript.json", tr)
        write_srt(new_sents, ep / "sentences.srt")
        out["applied"] = True
    elif apply:
        out["why"] = "no improvement" if moved else "nothing to move"
    return out


def main(argv=None):
    from lib_config import load_config
    ap = argparse.ArgumentParser(prog="subalign", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb", choices=["check", "fix", "retime"])
    ap.add_argument("video")
    ap.add_argument("subs")
    ap.add_argument("--out")
    ap.add_argument("--full", action="store_true", help="skip the quick check, retime piecewise")
    ap.add_argument("--max-shift", type=float, default=600.0,
                    help="largest offset searched, s (TV captions keep the CM breaks: 6+ min by the end)")
    ap.add_argument("--cache", help="ASR cache file (default: beside the video)")
    ap.add_argument("--drift", action="store_true", help="also fit a linear speed drift (frame-rate mismatch)")
    ap.add_argument("--aligned", help="retime: the old aligned srt the episode was built from")
    ap.add_argument("--media", help="retime: the episode's video")
    ap.add_argument("--apply", action="store_true", help="retime: write transcript.json + sentences.srt")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.verb == "retime":
        # retime <episode_id> <raw store srt> --aligned <old aligned srt> --video <mp4> [--apply]
        res = retime_episode(cfg, args.video, args.subs, args.aligned, args.media, args.cache, apply=args.apply)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    subs = Path(args.subs)
    cues = parse_srt(subs.read_text(encoding="utf-8", errors="replace"))
    report, cache = analyse(cfg, args.video, cues, args.max_shift, force_full=args.full,
                            cache_path=args.cache, drift=args.drift)
    report.pop("offsets", None) if args.verb == "check" else None
    if args.verb == "fix" and report["mode"] in ("constant", "piecewise", "none"):
        offs = report.pop("offsets")
        new = [dict(c, start=c["start"] + o, end=c["end"] + o) for c, o in zip(cues, offs)]
        # cues pushed before 0 or past the end of the video (a broadcast's
        # next-episode preview the rip doesn't carry) have no audio to time to
        dur = report.get("duration") or duration_of(args.video)
        dropped = sum(1 for c in new if c["end"] <= 0 or c["start"] >= dur)
        new = [c for c in new if c["end"] > 0 and c["start"] < dur]
        if dropped:
            report["dropped_outside_video"] = dropped
        out = Path(args.out) if args.out else subs
        if out == subs and report["mode"] != "none":
            orig = subs.with_suffix(".orig.srt")
            if not orig.exists():
                orig.write_text(subs.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        if report["mode"] != "none" or out != subs:
            write_srt(new, out)
        report["after"] = verify(cfg, args.video, new, cache)
        full = [seg for k, v in cache.items() if k.endswith("+30.0") or True for seg in v]
        report["after"]["residual"] = residual(new, full)
        (Path(args.cache) if args.cache else Path(args.video).with_suffix(".subalign_asr.json")).write_text(
            json.dumps(cache, ensure_ascii=False))
    report.pop("offsets", None)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["mode"] != "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
