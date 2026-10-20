import os,sys,json,datetime as dt,math,time
sys.path.insert(0,'/private/tmp/claude-502/bt')
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
from pilot import get,contracts,bars
UNDS=["SPY","QQQ","IWM","GLD","TLT","XLE","XLF","DIA","SLV"]
START=dt.date(2024,3,1); END=dt.date(2026,10,9)
OUT="/private/tmp/claude-502/bt/chains.jsonl"
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
        chain=[{"K":float(c["strike_price"]),"typ":c["type"],"p":b[c["symbol"]]["c"],"v":b[c["symbol"]]["v"]} for c in near if c["symbol"] in b]
        if chain:
            open(OUT,"a").write(json.dumps({"und":und,"exp":es,"S":S,"ST":ST,"chain":chain})+"\n")
    print(und,"done",flush=True)
