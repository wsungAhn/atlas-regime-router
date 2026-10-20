import pandas as pd,numpy as np,glob,re,sys
sys.path.insert(0,'src')
from friction import parse_occ
fs=[f for f in sorted(glob.glob('data/quote_panel/20*.parquet')) if 'positions' not in f and 'r1' not in f]
d=pd.concat([pd.read_parquet(f) for f in fs]); d['ts']=pd.to_datetime(d['ts'])
d['day']=d.ts.dt.date
syms=sorted(d.underlying.unique()); print(syms, d.day.min(), d.day.max())
parsed=d.occ.map(parse_occ); d['exp']=parsed.map(lambda x:x[1]); d['typ']=parsed.map(lambda x:x[2]); d['K']=parsed.map(lambda x:x[3])
bars={}
for s in syms:
    try:
        b=pd.read_parquet(f'data/friction/bars/{s}_daily.parquet'); bars[s]=b['close']
    except Exception as e: pass
def settle(s,exp):
    b=bars.get(s)
    if b is None: return None
    ix=pd.to_datetime(b.index).tz_localize(None).normalize()
    m=pd.Timestamp(exp).normalize()
    x=b[ix==m]
    return float(x.iloc[-1]) if len(x) else None
rows=[]
for (s,day),g in d.groupby(['underlying',d.day]):
    g=g[g.ts==g.ts.min()]
    for exp,ge in g[(g.dte>=5)&(g.dte<=9)].groupby('exp'):
        for typ in ('put','call'):
            c=ge[ge.typ==typ].dropna(subset=['mid','delta'])
            c=c[(c.bid>0)&(c.ask>0)]
            if len(c)<4: continue
            sh=c.iloc[(c.delta.abs()-0.20).abs().argsort()[:1]].iloc[0]
            target=sh.K*(1-0.015) if typ=='put' else sh.K*(1+0.015)
            lg=c.iloc[(c.K-target).abs().argsort()[:1]].iloc[0]
            W=abs(sh.K-lg.K)
            if W<=0: continue
            cred=(sh.mid-lg.mid)*0.95
            S=settle(s,exp)
            if S is None or cred<=0 or cred>=W: continue
            loss=max(0,sh.K-S)-max(0,lg.K-S) if typ=='put' else max(0,S-sh.K)-max(0,S-lg.K)
            rows.append(dict(sym=s,day=day,exp=exp,typ=typ,dte=sh.dte,cw=cred/W,pnl_w=(cred-loss)/W,win=loss==0,dlt=abs(sh.delta)))
R=pd.DataFrame(rows); print('all samples',len(R),'expiries',R.exp.nunique())
# one entry per (sym,exp,typ): dte closest to 7
R['dd']=(R.dte-7).abs(); R=R.sort_values('dd').groupby(['sym','exp','typ']).head(1)
ETF=("SPY","QQQ","GLD","TLT","SLV","IWM","XLE","XLF","DIA")
def rep(x,l):
    if not len(x): print(l,'n=0'); return
    print(f"{l:14s} n={len(x):3d} win={x.win.mean():.2f} be_win={(1-x.cw).mean():.2f} meanC/W={x.cw.mean():.3f} mean_short|delta|={x.dlt.mean():.2f} meanPnL/W={x.pnl_w.mean():+.3f} (se {x.pnl_w.std()/np.sqrt(len(x)):.3f})")
rep(R,'ALL'); rep(R[R.sym.isin(ETF)],'ETF9'); rep(R[~R.sym.isin(ETF)],'nonETF')
rep(R[R.typ=='put'],'puts'); rep(R[R.typ=='call'],'calls'); rep(R[R.cw>=0.10],'c/w>=.10')
print(R.groupby('exp').size().to_string())
