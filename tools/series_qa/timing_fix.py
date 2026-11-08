"""Fix misplaced stretches in a curated episode's transcript: runs of ≥4
consecutive matched sentences agreeing (±2 s) on an offset > 3 s get the whole
stretch — from the run's first to last sentence, plus unmatched sentences up
to the neighbouring runs' midpoints — shifted by the run median. Card picks
never move. Writes only if more sentences match the audio within 1 s after."""
import json, os, sys, glob, statistics, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.subalign import match_pairs, write_srt
apply = '--apply' in sys.argv
for slug_ep in [a for a in sys.argv[1:] if not a.startswith('--')]:
    slug, ep = slug_ep.split(':')
    d = os.path.expanduser(f'~/immersion/episodes/ser_{slug}_e{ep}')
    tr = json.load(open(d + '/transcript.json')); S = tr['sentences']
    picked = {p['sentence_idx'] for p in json.load(open(d + '/picks.json'))} if os.path.exists(d + '/picks.json') else set()
    segs = [s for v in json.load(open(os.path.expanduser(f'~/immersion/series/{slug}/{slug}-e{ep}.subalign_asr.json'))).values() for s in v]
    cues = lambda ss: [{'start': s['start'], 'end': s['end'], 'text': s['text']} for s in ss]
    pairs = sorted(match_pairs(cues(S), segs, 300, precise=True))
    runs = []
    for i, o in pairs:
        if runs and abs(o - statistics.median([x for _, x in runs[-1]])) <= 2.0:
            runs[-1].append((i, o))
        else:
            runs.append([(i, o)])
    def within1(ss):
        p = match_pairs(cues(ss), segs, 10, precise=True)
        return sum(abs(o) <= 1 for _, o in p)
    new = [dict(s) for s in S]; moved = 0
    for r, run in enumerate(runs):
        med = statistics.median(o for _, o in run)
        if len(run) < 4 or abs(med) <= 3:
            continue
        lo = run[0][0] if r == 0 else (runs[r - 1][-1][0] + run[0][0] + 1) // 2
        hi = run[-1][0] if r == len(runs) - 1 else (run[-1][0] + runs[r + 1][0][0]) // 2
        if r == len(runs) - 1:
            hi = len(S) - 1
        for i in range(lo, hi + 1):
            if i in picked:
                continue
            new[i]['start'] = round(S[i]['start'] + med, 3); new[i]['end'] = round(S[i]['end'] + med, 3); moved += 1
    b, a = within1(S), within1(new)
    ok = moved and a > b
    print(f'{slug} e{ep}: moved {moved}, within-1s matches {b} -> {a}', 'APPLIED' if ok and apply else ('would apply' if ok else 'skip'))
    if ok and apply:
        for f in ('transcript.json', 'sentences.srt'):
            bak = f'{d}/{f}.pre-timingfix'
            if not os.path.exists(bak): shutil.copy2(f'{d}/{f}', bak)
        tr['sentences'] = new
        json.dump(tr, open(d + '/transcript.json', 'w'), ensure_ascii=False, indent=2)
        write_srt(new, d + '/sentences.srt')
