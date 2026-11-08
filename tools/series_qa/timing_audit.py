"""Per-episode timing audit: match final transcript sentences against the cached
ASR (wide ±300 s window) and report 5-minute buckets whose median offset is
> 3 s — a misplaced stretch the curate agent may not have noticed."""
import json, os, sys, glob, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.subalign import match_pairs
for slug in sys.argv[1:]:
    for ep_dir in sorted(glob.glob(os.path.expanduser(f'~/immersion/episodes/ser_{slug}_e*'))):
        ep = ep_dir.rsplit('_e', 1)[1]
        cache = os.path.expanduser(f'~/immersion/series/{slug}/{slug}-e{ep}.subalign_asr.json')
        if not os.path.exists(cache) or not os.path.exists(ep_dir + '/transcript.json'):
            continue
        sents = json.load(open(ep_dir + '/transcript.json'))['sentences']
        segs = [s for v in json.load(open(cache)).values() for s in v]
        pairs = match_pairs([{'start': s['start'], 'end': s['end'], 'text': s['text']} for s in sents], segs, 300, precise=True)
        b = {}
        for i, o in pairs:
            b.setdefault(int(sents[i]['start'] // 300), []).append(o)
        bad = [(k * 5, round(statistics.median(v), 1), len(v)) for k, v in sorted(b.items())
               if len(v) >= 4 and abs(statistics.median(v)) > 3]
        if bad:
            print(f'{slug} e{ep}: off-buckets (min, median offset s, n) {bad}')
print('done')
