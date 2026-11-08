"""Dry-run planner: normalize episode filenames under a library folder to the
`Show - NN[ - Title].ext` pattern (multi-season shows: `Show - S01E01`).
Never touches the share — writes plan.tsv for review; apply.py does the renames.

    python3 tools/library_rename/plan.py /Volumes/library/Japanese/drama tools/library_rename/overrides.drama.json OUTDIR

See README.md in this folder for the whole procedure."""
import os, re, sys, json, unicodedata
from collections import defaultdict
ROOT = sys.argv[1].rstrip('/')
OUT = sys.argv[3] if len(sys.argv) > 3 else '.'
os.makedirs(OUT, exist_ok=True)
VID = {'.mkv', '.mp4', '.avi'}
SUB = {'.srt', '.ass', '.ssa'}
OV = json.load(open(sys.argv[2]))
TITLES, DIRS, SKIP, FIX = OV['titles'], OV['dirs'], set(OV['skip']), OV['files']

KANJI = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
def kanji_num(s):
    if s.isdigit(): return int(s)
    tens, _, ones = s.partition('十')
    if '十' not in s: return KANJI[s]
    return (KANJI[tens] if tens else 1) * 10 + (KANJI[ones] if ones else 0)

def clean_title(name):
    t = re.sub(r'\s*[\[(]no[_ ]subs?[\])]', '', name, flags=re.I)
    t = re.sub(r'^\[[^\]]*\]\s*', '', t)
    return re.sub(r'\s*\((?:19|20)\d\d(?:-(?:19|20)\d\d)?\)\s*', ' ', t).strip()

NOISE = re.compile(r'''(?ix)\b(?:\d{3,4}p|\d{3,4}x\d{3,4}|x26[45]|h\.?26[45]|hevc|aac(?:2\.0)?|ac3|ddp?2\.0|5\.1|divx\d*|
  hdtv|web-?dl|webrip|bdrip|br720p|dvdrip|dvd|nf|amzn|raw|clean\ screen|multi-subs|japanese|webdl|netflix|
  full\ episode|eng\ sub|(?:19|20)\d\d)\b''')
GROUP_TAIL = re.compile(r'(?i)(?:[-.](?:DoA|ER|AOZ|JJ|NGB|Shimazu|MagicStar|IRENEBRO|farah|stormai|hotelpapers|yyets|dvdsars|Dr Dante|CiNEFiLE|D-Addicts|PPX)|\s+(?:AOZ|dvdsars|stormai|hotelpapers|yyets|farah))(?=\W|$)')
JUNK_TITLE = re.compile(r'(?i)^(?:finale?|end|final|the end|v\d|\W*)$')
NUM = r'(\d{1,3}(?:\.\d)?)'

def fmt(n):
    return f'{float(n):04.1f}' if '.' in n else f'{int(n):02d}'

def tidy(t):
    t = re.sub(r"^\s*'|'\s*$", '', t or '').strip(" -_.")
    t = re.sub(r'\s+', ' ', t)
    return None if not t or JUNK_TITLE.match(t) else t

def parse(stem):
    """-> dict(season, ep, sp, title, ad)"""
    s = unicodedata.normalize('NFKC', stem)
    r = dict(season=None, ep=None, sp=None, title=None, ad='解説放送版' in s)
    s = re.sub(r'【解説放送版】', ' ', s)
    s = re.sub(r'\s*\[ep[0-9a-z]{8}\]$', '', s)                     # TVer id
    if re.search(r'[぀-ヿ一-鿿]', s):                # TVer-style Japanese name
        m = (re.search(r'第\s*([0-9一二三四五六七八九十]+)\s*話', s) or re.search(r'(?:^|\s)([0-9]+)話', s)
             or re.search(r'\bep\.([0-9]+)', s))
        if m:
            ms = re.search(r'season\s*(\d+)', s, re.I)
            rest = s[m.end():].strip()
            mt = re.match(r'^[「【](.*?)[」】]', rest)
            title = mt.group(1) if mt else rest
            msp = re.search(r'((?:初回|元日)?(?:拡大)?SP)\s*$', s[:m.start()].strip())
            if msp and title: title = f'{title} ({msp.group(1)})'
            r.update(season=int(ms.group(1)) if ms else None, ep=f'{kanji_num(m.group(1)):02d}', title=tidy(title))
            # keep the original Japanese punctuation in the title
            if r['title'] and (mt or rest):
                o = re.sub(r'【解説放送版】', ' ', stem); o = re.sub(r'\s*\[ep[0-9a-z]{8}\]$', '', o)
                om = re.search(r'(?:第\s*[0-9０-９一二三四五六七八九十]+\s*話|(?:^|\s)[0-9０-９]+話|ep\.[0-9]+)', o)
                if om:
                    orest = o[om.end():].strip(' 　')
                    omt = re.match(r'^[「【](.*?)[」】]', orest)
                    ot = (omt.group(1) if omt else orest).strip(' 　')
                    if msp: ot = f'{ot} ({msp.group(1)})'
                    r['title'] = ot or r['title']
            return r
    m = re.search(r'S(\d{1,2})\s*E(\d{1,3})', s, re.I) or re.search(r'(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)', s)
    if m:
        rest = s[m.end():]
        mt = re.match(r'\s*-\s*(.+)$', rest)
        mw = re.match(r'\.Week(\d+)', rest)
        r.update(season=int(m.group(1)), ep=f'{int(m.group(2)):02d}',
                 title=tidy(mt.group(1)) if mt else (f'Week {int(mw.group(1)):02d}' if mw else None))
        if r['title'] and r['title'].lower() == 'trailer': r['title'] = 'Trailer'
        return r
    s2 = re.sub(r'\[([^\]]*)\]', lambda b: ' ' + b.group(1) + ' ' if re.search(r'(?i)\bep\s*\d', b.group(1)) else ' ', s)
    s2 = GROUP_TAIL.sub(' ', s2.replace('_', ' '))
    s2 = re.sub(r'\((?![^)]*(?:final|end))[^)]*\)', ' ', s2, flags=re.I)
    s2 = NOISE.sub(' ', s2)
    msp = re.search(r'(?i)(?<![a-z])(?:sp|special)\s*(?:ep)?\s*(\d{0,2})(?![a-z])', s2)
    if msp and not re.search(r'(?i)(?:(?<![a-z])(?:ep|e|episode)\s*|\s-\s+)\d{1,3}\b.*special', s2):
        n = msp.group(1)
        mt = re.search(r'(?i)special\s*-\s*(.+)$', s2)
        r.update(sp='SP' + (str(int(n)) if n else ''), title=tidy(mt.group(1)) if mt else None)
        return r
    m = (re.search(r'(?i)(?<![a-z])(?:episode|ep|e)[\s.]*' + NUM + r'(?:v\d|end|final)?(?!\d)', s2)
         or re.search(r'(?i)\bdisc\s*(\d{1,3})\b', s2)
         or re.search(r'\s-\s+' + NUM + r'(?:v\d)?(?!\d)', s2))
    if not m:
        nums = list(re.finditer(r'(?<![\d.])' + NUM + r'(?:v\d)?(?![\d°])', s2))
        m = nums[-1] if nums else None
    if m:
        rest = s2[m.end():]
        mt = re.match(r"\s*-\s*(.+?)\s*$", rest) or re.match(r"\s+((?:[A-Z][a-z]+\s*)*Special)\s*$", rest)
        r.update(ep=fmt(m.group(1)), title=tidy(mt.group(1)) if mt else None)
        return r
    for pat, lab in [(r'(?i)another\s*ending', ('SP', 'Another Ending')), (r'(?i)final\s*story', ('SP', 'Final Story')),
                     (r'(?i)omnibus.?prequel', ('SP', 'Omnibus Prequel')), (r'(?i)omnibus.?sequel', ('SP', 'Omnibus Sequel')),
                     (r'(?i)movie', ('Movie', None))]:
        if re.search(pat, s):
            r.update(sp=lab[0], title=lab[1]); return r
    return r

def context(rd):
    """(show title, season|'file'|special-label|None) for a file's directory."""
    parts = rd.split('/')
    for i in range(len(parts), 0, -1):
        k = '/'.join(parts[:i])
        if k in DIRS: return DIRS[k]
    return [TITLES.get(parts[0], clean_title(parts[0])), None]

def lang_tail(stem):
    m = re.search(r'\.(jpn|ja|jp|eng|en|chs|cht|zh|kor)$', stem, re.I)
    return m.group(0) if m else ''

def skipped(rel):
    return any(rel == s or rel.startswith(s + '/') for s in SKIP)

def build(rel):
    rd, fn = os.path.split(rel); stem, ext = os.path.splitext(fn)
    lt = lang_tail(stem) if ext.lower() in SUB else ''
    base = stem[:-len(lt)] if lt else stem
    if rel in FIX: return FIX[rel] + lt + ext, 'override'
    title, mode = context(rd)
    # already normal for this show → leave it (makes re-runs a no-op)
    if re.match(re.escape(title) + r' - (?:S\d\dE)?(?:\d\d(?:\.\d)?|SP\d*|Movie)(?: - .+| \(.+\))?$', base):
        return fn, ''
    p = parse(base)
    if isinstance(mode, str) and mode != 'file':          # a specials folder
        lab = mode if not p['ep'] or mode != 'SP' else f"SP{int(float(p['ep']))}"
        if mode == 'SP' and p['sp'] and p['sp'] != 'SP': lab = p['sp']
        num, p['title'] = lab, (p['title'] if mode == 'SP' else None)
    elif p['sp']:
        num = p['sp']
    elif p['ep'] is None:
        return '', 'UNPARSED'
    elif isinstance(mode, int):
        num = f"S{mode:02d}E{p['ep']}"
    elif mode == 'file':
        num = f"S{p['season']:02d}E{p['ep']}" if p['season'] is not None else p['ep']
    else:
        num = p['ep']
    new = f'{title} - {num}' + (f" - {p['title']}" if p['title'] else '') + (' (解説放送版)' if p['ad'] else '')
    new = new.translate(str.maketrans({'/': '／', ':': '：', '?': '？', '*': '＊', '"': '”', '<': '＜', '>': '＞', '|': '｜', '\\': '＼'}))
    return new + lt + (ext.lower() if ext.lower() in SUB else ext), ''

rows = []
for dp, dns, fns in os.walk(ROOT):
    dns.sort(); rd = os.path.relpath(dp, ROOT)
    if rd == '.': continue                                 # loose top-level films stay as they are
    for fn in sorted(fns):
        rel = f'{rd}/{fn}'
        if os.path.splitext(fn)[1].lower() not in VID | SUB or skipped(rel): continue
        new, note = build(rel)
        rows.append([rel, new, note])
# a subtitle takes its video's new name (same show, same episode key) so pairs line up exactly
KEY = re.compile(r'^(.*?) - ((?:S\d\dE)?\d\d(?:\.\d)?|SP\d*|Movie)(?= |$|\.)')
def key_of(rel, new):
    stem = os.path.splitext(new)[0]
    lt = lang_tail(stem); stem = stem[:-len(lt)] if lt else stem
    m = KEY.match(stem)
    return (rel.split('/')[0], m.group(1), m.group(2)) if m else None
vmap = defaultdict(list)
for r in rows:
    if r[1] and os.path.splitext(r[0])[1].lower() in VID and '(duplicate)' not in r[1]:
        k = key_of(r[0], r[1])
        if k: vmap[k].append(os.path.splitext(r[1])[0])
for r in rows:
    ext = os.path.splitext(r[0])[1].lower()
    if r[1] and ext in SUB and r[2] != 'override':
        k = key_of(r[0], r[1]); vs = vmap.get(k, [])
        if len(vs) == 1:
            lt = lang_tail(os.path.splitext(os.path.basename(r[0]))[0])
            r[1] = vs[0] + lt + ext
        elif not vs:
            r[2] = (r[2] + ' no-video').strip()
seen = defaultdict(list)
for r in rows:
    if r[1]: seen[(os.path.dirname(r[0]), r[1].lower())].append(r)
import difflib
vids_by_dir = defaultdict(list)
for r in rows:
    if os.path.splitext(r[0])[1].lower() in VID: vids_by_dir[r[0].split('/')[0]].append(os.path.splitext(os.path.basename(r[0]))[0])
for rs in seen.values():
    if len(rs) < 2: continue
    if all(os.path.splitext(r[0])[1].lower() in SUB for r in rs):
        show = rs[0][0].split('/')[0]
        def score(r):
            st = os.path.splitext(os.path.basename(r[0]))[0].lower()
            return max((difflib.SequenceMatcher(None, st, v.lower()).ratio() for v in vids_by_dir[show]), default=0)
        rs.sort(key=score, reverse=True)
        for i, r in enumerate(rs[1:], 1):
            stem, ext = os.path.splitext(r[1])
            r[1] = f"{stem} (alt{'' if i == 1 else ' ' + str(i)}){ext}"; r[2] = (r[2] + ' alt-sub').strip()
    else:
        for r in rs: r[2] = (r[2] + ' COLLISION').strip()
with open(os.path.join(OUT, 'plan.tsv'), 'w') as f:
    for r in rows: f.write('\t'.join(r) + '\n')
print(len(rows), 'files;', sum(1 for r in rows if r[1] and r[1] != os.path.basename(r[0])), 'renames;',
      sum('UNPARSED' in r[2] for r in rows), 'unparsed;', sum('COLLISION' in r[2] for r in rows), 'colliding')
