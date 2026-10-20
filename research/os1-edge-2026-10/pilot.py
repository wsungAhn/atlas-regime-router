import os,sys,requests,datetime as dt,time,json,numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
h={"APCA-API-KEY-ID":os.environ["ALPACA_API_KEY"],"APCA-API-SECRET-KEY":os.environ["ALPACA_SECRET_KEY"]}
def get(url,params,tries=5):
    for i in range(tries):
        r=requests.get(url,headers=h,params=params,timeout=30)
        if r.status_code==429: time.sleep(2*(i+1)); continue
        r.raise_for_status(); return r.json()
    raise RuntimeError("rate")
def contracts(und,exp):
    out=[];tok=None
    while True:
        p={"underlying_symbols":und,"expiration_date":exp,"status":"inactive","limit":1000}
        if tok: p["page_token"]=tok
        j=get("https://paper-api.alpaca.markets/v2/options/contracts",p)
        out+=j.get("option_contracts",[]); tok=j.get("next_page_token")
        if not tok: return out
def bars(syms,day):
    res={}
    for i in range(0,len(syms),50):
        j=get("https://data.alpaca.markets/v1beta1/options/bars",{"symbols":",".join(syms[i:i+50]),"timeframe":"1Day","start":day,"end":day+"T23:59:59Z","limit":1000})
        for k,v in j.get("bars",{}).items():
            if v: res[k]=v[0]
    return res
if __name__=="__main__":
    und,exp,day,S=sys.argv[1],sys.argv[2],sys.argv[3],float(sys.argv[4])
    cs=contracts(und,exp); print("contracts",len(cs))
    near=[c for c in cs if 0.85*S<=float(c["strike_price"])<=1.15*S]
    b=bars([c["symbol"] for c in near],day); print("near",len(near),"with bars on day",len(b))
    for c in sorted(near,key=lambda c:float(c["strike_price"]))[::6][:12]:
        print(c["symbol"],b.get(c["symbol"],{}).get("c"),b.get(c["symbol"],{}).get("v"))
