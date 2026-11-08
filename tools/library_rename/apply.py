"""Apply a reviewed plan.tsv on the library share. Renames only, within the same
folder; never overwrites; logs every rename (new -> old) for undo; then rewrites
remote_video/remote_subs in every ~/immersion/series/*/series.json that points
at a renamed file (backup beside it as .pre-rename.bak).

    python3 tools/library_rename/apply.py /Volumes/library/Japanese/drama OUTDIR/plan.tsv <log-name>

Only with the user's explicit OK for that run — the library share is read-only by default (CLAUDE.md)."""
import os, sys, json, shutil, glob
ROOT = sys.argv[1].rstrip('/')
PLAN = sys.argv[2]
LOGDIR = os.path.expanduser('~/immersion/library_rename_log'); os.makedirs(LOGDIR, exist_ok=True)
LOG = os.path.join(LOGDIR, sys.argv[3] + '.tsv')
plan = [l.rstrip('\n').split('\t') for l in open(PLAN)]
todo = [(o, n) for o, n, _ in plan if n and n != os.path.basename(o)]
done = errs = 0
with open(LOG, 'a') as log:
    for old, new in todo:
        src = os.path.join(ROOT, old); dst = os.path.join(os.path.dirname(src), new)
        if not os.path.exists(src): print('MISSING', old); errs += 1; continue
        same = os.path.normcase(src).lower() == dst.lower()   # case-only change on a case-insensitive share
        if os.path.exists(dst) and not same: print('EXISTS', dst); errs += 1; continue
        if same:
            tmp = dst + '.renaming'; os.rename(src, tmp); os.rename(tmp, dst)
        else:
            os.rename(src, dst)
        log.write(f'{os.path.relpath(dst, ROOT)}\t{old}\n'); done += 1
print('renamed', done, 'errors', errs, 'log', LOG)

# series manifests
mp = {o.lower(): os.path.join(os.path.dirname(o), n) for o, n in todo}
LEGACY = {'/Volumes/library/Japanese/': 'E:/Japanese/'}   # desktop-era manifests
for path in sorted(glob.glob(os.path.expanduser('~/immersion/series/*/series.json'))):
    slug = os.path.basename(os.path.dirname(path))
    man = json.load(open(path)); n = 0
    prefixes = [ROOT + '/'] + [ROOT.replace(k, v, 1) + '/' for k, v in LEGACY.items() if ROOT.startswith(k)]
    refs = [e.get(k) for e in man['episodes'] for k in ('remote_video', 'remote_subs') if e.get(k)]
    if not any(r.replace('\\', '/').startswith(p) for r in refs for p in prefixes): continue
    shutil.copy2(path, path + '.pre-rename.bak')
    for e in man['episodes']:
        for k in ('remote_video', 'remote_subs'):
            v = e.get(k)
            if not v: continue
            rel = v.replace('\\', '/')
            for pre in prefixes:
                if rel.startswith(pre): rel = rel[len(pre):]
            newrel = mp.get(rel.lower())
            if newrel:
                e[k] = os.path.join(ROOT, newrel); n += 1
    json.dump(man, open(path, 'w'), ensure_ascii=False, indent=2)
    print(slug, 'manifest refs updated', n)
