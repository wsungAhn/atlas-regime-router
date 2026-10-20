import json,sys,math,numpy as np,collections
sys.path.insert(0,'/private/tmp/claude-502/bt')
from btlib import iv,bs
rows=[json.loads(l) for l in open('/private/tmp/claude-502/bt/chains.jsonl')]
T=7/365
def build(r,tdelta,wfrac):
    out=[]
    for typ in ("put","call"):
        opts=[]
        for c in r["chain"]:
            if c["typ"]!=typ or c["p"]<0.03: continue
            s_=iv(c["p"],r["S"],c["K"],T,typ)
            if s_ is None: continue
            opts.append((c["K"],c["p"],abs(bs(r["S"],c["K"],T,s_,typ)[1])))
        if len(opts)<3: continue
        sh=min(opts,key=lambda o:abs(o[2]-tdelta))
        if abs(sh[2]-tdelta)>0.07: continue
        tgt=sh[0]-wfrac*r["S"] if typ=="put" else sh[0]+wfrac*r["S"]
        cand=[o for o in opts if (o[0]<sh[0] if typ=="put" else o[0]>sh[0])]
        if not cand: continue
        lg=min(cand,key=lambda o:abs(o[0]-tgt)); W=abs(sh[0]-lg[0])
        if W<=0.2*wfrac*r["S"]: continue
        cred=(sh[1]-lg[1])*0.95
        if not 0<cred<W: continue
        ST=r["ST"]; loss=(max(0,sh[0]-ST)-max(0,lg[0]-ST)) if typ=="put" else (max(0,ST-sh[0])-max(0,ST-lg[0]))
        out.append(dict(exp=r["exp"],typ=typ,pnl=(cred-loss)/W,win=loss==0,cw=cred/W))
    return out
cut="2025-07-01"
def rep(xs,l):
    if len(xs)<30: print(f"  {l} n={len(xs)}"); return
    p=np.array([s["pnl"] for s in xs]); byw=collections.defaultdict(list)
    for s in xs: byw[s["exp"]].append(s["pnl"])
    wk=np.array([np.mean(v) for v in byw.values()])
    print(f"  {l:6s} n={len(xs):4d} win={np.mean([s['win'] for s in xs]):.2f} be={1-np.mean([s['cw'] for s in xs]):.2f} meanPnL/W={p.mean():+.4f} se={wk.std(ddof=1)/np.sqrt(len(wk)):.4f}")
for td in (0.10,0.20,0.30):
    for wf in (0.015,0.03):
        S=[x for r in rows for x in build(r,td,wf)]
        print(f"delta={td:.2f} width={wf*100:.1f}%")
        rep(S,"ALL"); rep([s for s in S if s["exp"]<cut],"TRAIN"); rep([s for s in S if s["exp"]>=cut],"TEST")
