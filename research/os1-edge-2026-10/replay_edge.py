import sys,json,numpy as np,pandas as pd
sys.path.insert(0,'src')
from friction_backtest import load_live_episodes
from friction_calibrate import load_inputs
from friction_compare import run_variants,_episode_key
from os1_replay import episode_structure,ETF9
eps,_=load_live_episodes('/Users/Mung/dev/atlas-options-hackathon/registry/decisions.jsonl')
tab=pd.read_csv('data/friction/half_spread_table.csv')
bars,iv,miss=load_inputs(eps,'data/friction/bars')
cal=json.load(open('reports/friction/calibration-2026-10-19.json'))
sc={"central":{"beta":cal["fit"]["beta"],"h_mult":cal["fit"]["h_mult"]},"pessimistic":{"beta":cal["fit_pessimistic"]["beta"],"h_mult":cal["fit_pessimistic"]["h_mult"]}}
res=run_variants(eps,bars,iv,tab,scenarios=sc,r=0.045,s=0.5,equity=50000)
E={_episode_key(e):e for e in eps}
def stats(rows,label):
    rows=list(rows)
    if not rows: print(label,'n=0'); return
    p=np.array([r['pnl'] for r in rows]); cw=np.array([r['cw'] for r in rows])
    w=p>0
    aw=p[w].mean() if w.any() else float('nan'); al=p[~w].mean() if (~w).any() else float('nan')
    # breakeven win rate from ratio: loss/(win+loss) with win=c, loss=w-c => 1-c/w
    be=(1-cw).mean()
    print(f"{label:28s} n={len(p):3d} win={w.mean():.2f} avgW={aw:8.1f} avgL={al:8.1f} payoff={abs(aw/al) if al==al and al else float('nan'):.2f} meanPnL={p.mean():8.1f} mean c/w={cw.mean():.2f} breakeven_win(from c/w)={be:.2f}")
for scen in ("central","pessimistic"):
    print("==",scen)
    rows=[]
    for r in res[scen]["X2"]["rows"]:
        e=E[_episode_key(r)]; st=episode_structure(e); W=max(st["widths"]) 
        rows.append(dict(pnl=r['realized_pnl'],cw=e['limit_credit']/W,sym=r['symbol'],reg=r.get('regime'),side=st['side'],qty=e['qty']))
    stats(rows,"X2 all")
    stats([r for r in rows if r['sym'] in ETF9],"X2 ETF9")
    stats([r for r in rows if r['sym'] not in ETF9],"X2 non-ETF")
    stats([r for r in rows if r['cw']>=0.10],"X2 c/w>=0.10")
    stats([r for r in rows if r['cw']>=0.10 and r['sym'] in ETF9],"X2 ETF9 & c/w>=0.10")
    for side in ("bull_put","bear_call","condor"): stats([r for r in rows if r['side']==side],"X2 "+side)
    # per-contract-normalised (qty-independent): pnl per contract
    print("per-contract mean pnl (all/ETF9):", np.mean([r['pnl']/r['qty'] for r in rows]).round(1), np.mean([r['pnl']/r['qty'] for r in rows if r['sym'] in ETF9]).round(1))
    # c/w distribution
    cws=np.array([r['cw'] for r in rows]); print("c/w quantiles 10/50/90:", np.quantile(cws,[.1,.5,.9]).round(2))
