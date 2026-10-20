import json,math,numpy as np,sys,collections
from timestral import TimestralForecaster
closes=json.load(open('/private/tmp/claude-502/bt/closes.json'))
f=TimestralForecaster.from_pretrained("mistralai/Timestral-123M")
H=5; CTX_MIN=250
rows=[]
for u,c in closes.items():
    days=sorted(c); px=np.array([c[d] for d in days]); r=np.diff(np.log(px)); rd=days[1:]
    ar=np.abs(r)
    for t in range(CTX_MIN,len(r)-H,5):         # origin after return index t-1 known
        past=r[:t]; fut=r[t:t+H]
        real=math.sqrt(np.mean(fut**2)*252)
        rv20=math.sqrt(np.mean(past[-20:]**2)*252)
        lam=0.94; w=lam**np.arange(len(past))[::-1]; ew=math.sqrt(np.sum(w*past**2)/np.sum(w)*252)
        fc=f.forecast(np.abs(past[-400:]).astype(np.float32),prediction_length=H)
        med=np.asarray(fc.median).reshape(-1)[:H]
        ts=float(np.mean(med))*math.sqrt(math.pi/2)*math.sqrt(252)
        rows.append(dict(u=u,d=rd[t-1],real=real,rv20=rv20,ew=ew,ts=ts))
json.dump(rows,open('/private/tmp/claude-502/bt/ts_vol.json','w')); print(len(rows))
