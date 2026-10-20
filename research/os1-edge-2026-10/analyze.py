import json,numpy as np,collections
rows=[json.loads(l) for l in open('/private/tmp/claude-502/bt/samples.jsonl')]
S=[]
for r in rows:
    for typ in ("put","call"):
        if typ in r:
            x=r[typ]; cred=x["ps"]-x["pl"]
            S.append(dict(und=r["und"],exp=r["exp"],typ=typ,cred=cred,W=x["W"],loss=x["loss"],delta=x["delta"],S=r["S"]))
S=[s for s in S if 0<s["cred"]<s["W"]]
print("expiries",len({(r['und'],r['exp']) for r in rows}),"samples",len(S))
def rep(xs,l):
    if len(xs)<5: print(l,'n=',len(xs)); return
    for hc in (0.0,0.05,0.10):
        pnl=np.array([(s["cred"]*(1-hc)-s["loss"])/s["W"] for s in xs])
        win=np.mean([s["loss"]==0 for s in xs]); cw=np.mean([s["cred"]*(1-hc)/s["W"] for s in xs])
        # cluster by exp date (week) for se
        byw=collections.defaultdict(list)
        for s,p in zip(xs,pnl): byw[s["exp"]].append(p)
        wk=np.array([np.mean(v) for v in byw.values()])
        print(f"{l:10s} haircut={hc:.2f} n={len(xs):4d} weeks={len(wk):3d} win={win:.2f} be_win={1-cw:.2f} meanC/W={cw:.3f} meanPnL/W={pnl.mean():+.4f} week-clustered se={wk.std(ddof=1)/np.sqrt(len(wk)):.4f}")
rep(S,"ALL"); rep([s for s in S if s["typ"]=="put"],"puts"); rep([s for s in S if s["typ"]=="call"],"calls")
for u in sorted({s["und"] for s in S}): rep([s for s in S if s["und"]==u],u)
yrs=sorted({s["exp"][:4] for s in S})
for y in yrs: rep([s for s in S if s["exp"][:4]==y],y)
# losses: size
L=[s["loss"]/s["W"] for s in S if s["loss"]>0]; print("loss/W when ITM: mean %.2f median %.2f"%(np.mean(L),np.median(L)), "share full-width", np.mean([l>=0.99 for l in L]))
