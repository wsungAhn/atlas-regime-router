import os,sys,json,datetime as dt,math,time
sys.path.insert(0,'/private/tmp/claude-502/bt')
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
from pilot import get,contracts,bars
UNDS=["SPY","QQQ","IWM","GLD","TLT","XLE","XLF","DIA","SLV"]
START=dt.date(2024,3,1); END=dt.date(2026,10,9)
OUT="/private/tmp/claude-502/bt/samples.jsonl"
R=0.045
def stock_closes(und):
    out={};tok=None
    while True:
        p={"symbols":und,"timeframe":"1Day","start":"2024-02-15","end":"2026-10-17","adjustment":"raw","feed":"sip","limit":10000}
        if tok:p["page_token"]=tok
        j=get("https://data.alpaca.markets/v2/stocks/bars",p)
        for b in j["bars"].get(und,[]): out[b["t"][:10]]=b["c"]
        tok=j.get("next_page_token")
        if not tok: return out
def bs(S,K,T,sig,typ):
    d1=(math.log(S/K)+(R+.5*sig*sig)*T)/(sig*math.sqrt(T)); d2=d1-sig*math.sqrt(T)
    if typ=="call": return S*norm.cdf(d1)-K*math.exp(-R*T)*norm.cdf(d2), norm.cdf(d1)
    return K*math.exp(-R*T)*norm.cdf(-d2)-S*norm.cdf(-d1), norm.cdf(d1)-1
def iv(price,S,K,T,typ):
    intrinsic=max(0,(S-K) if typ=="call" else (K-S))
    if price<=intrinsic+0.005: return None
    try: return brentq(lambda s:bs(S,K,T,s,typ)[0]-price,0.02,3.0)
    except Exception: return None
done=set()
if os.path.exists(OUT):
    for l in open(OUT): j=json.loads(l); done.add((j["und"],j["exp"]))
fridays=[]
d=START
while d<=END:
    if d.weekday()==4: fridays.append(d)
    d+=dt.timedelta(days=1)
for und in UNDS:
    sc=stock_closes(und); print(und,"stock days",len(sc),flush=True)
    for E in fridays:
        es=E.isoformat()
        if (und,es) in done: continue
        D=(E-dt.timedelta(days=7)); ds=D.isoformat()
        if ds not in sc or es not in sc: continue
        S=sc[ds]; ST=sc[es]
        try:
            cs=contracts(und,es)
            if not cs: continue
            near=[c for c in cs if 0.86*S<=float(c["strike_price"])<=1.14*S]
            b=bars([c["symbol"] for c in near],ds)
        except Exception as e:
            print("err",und,es,e,flush=True); continue
        T=7/365
        res={"und":und,"exp":es,"S":S,"ST":ST}
        for typ in ("put","call"):
            opts=[]
            for c in near:
                if c["type"]!=typ or c["symbol"] not in b: continue
                K=float(c["strike_price"]); p=b[c["symbol"]]["c"]
                if p<0.03: continue
                s_=iv(p,S,K,T,typ)
                if s_ is None: continue
                opts.append((K,p,bs(S,K,T,s_,typ)[1]))
            if len(opts)<3: continue
            sh=min(opts,key=lambda o:abs(abs(o[2])-0.20))
            if abs(abs(sh[2])-0.20)>0.07: continue
            tgt=sh[0]-0.015*S if typ=="put" else sh[0]+0.015*S
            cand=[o for o in opts if (o[0]<sh[0] if typ=="put" else o[0]>sh[0])]
            if not cand: continue
            lg=min(cand,key=lambda o:abs(o[0]-tgt))
            W=abs(sh[0]-lg[0])
            if W<=0.2*0.015*S: continue
            loss=(max(0,sh[0]-ST)-max(0,lg[0]-ST)) if typ=="put" else (max(0,ST-sh[0])-max(0,ST-lg[0]))
            res[typ]={"Ks":sh[0],"Kl":lg[0],"ps":sh[1],"pl":lg[1],"delta":abs(sh[2]),"W":W,"loss":loss}
        if "put" in res or "call" in res:
            open(OUT,"a").write(json.dumps(res)+"\n")
    print(und,"done",flush=True)
