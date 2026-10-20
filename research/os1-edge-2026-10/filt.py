import json,sys,math,numpy as np,collections,datetime as dt
sys.path.insert(0,'/private/tmp/claude-502/bt')
import os
from btlib import stock_closes,iv
rows=[json.loads(l) for l in open('/private/tmp/claude-502/bt/samples.jsonl')]
sc={u:stock_closes(u) for u in {r["und"] for r in rows}}
def rv20(u,day):
    ks=sorted(k for k in sc[u] if k<=day)[-21:]
    if len(ks)<15: return None
    r=np.diff(np.log([sc[u][k] for k in ks])); return float(np.std(r,ddof=1)*math.sqrt(252))
S=[]
for r in rows:
    d=(dt.date.fromisoformat(r["exp"])-dt.timedelta(days=7)).isoformat()
    for typ in ("put","call"):
        if typ not in r: continue
        x=r[typ]; cred=(x["ps"]-x["pl"])*0.95
        if not 0<cred<x["W"]: continue
        sig=iv(x["ps"],r["S"],x["Ks"],7/365,typ); rv=rv20(r["und"],d)
        if sig is None or not rv: continue
        S.append(dict(und=r["und"],exp=r["exp"],typ=typ,pnl=(cred-x["loss"])/x["W"],ratio=sig/rv,iv=sig))
print("n",len(S))
def rep(xs,l):
    if len(xs)<20: print(f"{l:28s} n={len(xs)}"); return
    p=np.array([s["pnl"] for s in xs]); byw=collections.defaultdict(list)
    for s in xs: byw[s["exp"]].append(s["pnl"])
    wk=np.array([np.mean(v) for v in byw.values()])
    print(f"{l:28s} n={len(xs):4d} weeks={len(wk):3d} meanPnL/W={p.mean():+.4f} se={wk.std(ddof=1)/np.sqrt(len(wk)):.4f}")
cut="2025-07-01"
for name,f in [("ALL",lambda s:True),("puts",lambda s:s["typ"]=="put"),
               ("ratio>=1.2",lambda s:s["ratio"]>=1.2),("ratio>=1.2 puts",lambda s:s["ratio"]>=1.2 and s["typ"]=="put"),
               ("ratio>=1.5",lambda s:s["ratio"]>=1.5),("ratio<0.9",lambda s:s["ratio"]<0.9),
               ("iv>=0.25",lambda s:s["iv"]>=0.25)]:
    xs=[s for s in S if f(s)]
    rep([s for s in xs if s["exp"]<cut],name+" TRAIN"); rep([s for s in xs if s["exp"]>=cut],name+" TEST")
q=np.quantile([s["ratio"] for s in S],[0,.25,.5,.75,1]); print("ratio quartile edges",q.round(2))
for i in range(4): rep([s for s in S if q[i]<=s["ratio"]<=q[i+1]],f"ratio Q{i+1}")
