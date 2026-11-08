import json,os,glob,re,sys
bad=0; tot=0
for slug in sys.argv[1:]:
  for d in sorted(glob.glob(os.path.expanduser(f'~/immersion/episodes/ser_{slug}_e*'))):
    e=os.path.basename(d)
    try:
        cov=json.load(open(d+'/coverage.json')); cur=json.load(open(d+'/curate.json')); picks=json.load(open(d+'/picks.json'))
    except FileNotFoundError as x: print(e,'MISSING',x.filename); bad+=1; continue
    tr=json.load(open(d+'/transcript.json'))['sentences']
    glossed={k['word'] for k in cur.get('keywords',[])}|{x['lemma'] if isinstance(x,dict) else x for x in cur.get('exclude',[])}
    miss=[c['lemma'] for c in cov['candidates'] if c['lemma'] not in glossed]
    probs=[]
    for p in picks:
        tot+=1
        for f in ('english','notes','context','sentence_furigana'):
            if not p.get(f): probs.append(f"{p['lemma']}:no {f}")
        s=tr[p['sentence_idx']]
        plain=re.sub(r'\[[^\]]*\]','',p.get('sentence_furigana','')).replace(' ','')
        if plain!=s['text'].replace(' ',''): probs.append(f"{p['lemma']}:furigana≠text")
        if not 1.5<=s['end']-s['start']<=15: probs.append(f"{p['lemma']}:span {s['end']-s['start']:.1f}")
    if miss or probs or not os.path.exists(d+'/prep.html'): bad+=1; print(e,'unglossed:',miss[:6],probs[:6])
print(tot,'picks;',bad,'episodes with issues')
