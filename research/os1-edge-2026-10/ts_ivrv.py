import json,math,numpy as np,datetime as dt
from timestral import TimestralForecaster
closes=json.load(open('closes.json')); f=TimestralForecaster.from_pretrained("mistralai/Timestral-123M")
rows=[json.loads(l) for l in open('chains.jsonl')]
out={}
for r in rows:
    u=r['und']; D=(dt.date.fromisoformat(r['exp'])-dt.timedelta(days=7)).isoformat()
    c=closes[u]; days=sorted(c)
    if D not in c: continue
    i=days.index(D)                      # returns known through day i
    if i<250: continue
    px=np.array([c[d] for d in days[:i+1]]); past=np.diff(np.log(px))
    fc=f.forecast(np.abs(past[-400:]).astype(np.float32),prediction_length=5)
    med=np.asarray(fc.median).reshape(-1)[:5]
    out[f"{u}|{r['exp']}"]=dict(ts=float(np.mean(med))*math.sqrt(math.pi/2)*math.sqrt(252),rv20=math.sqrt(np.mean(past[-20:]**2)*252))
json.dump(out,open('ts_origin.json','w')); print(len(out))
