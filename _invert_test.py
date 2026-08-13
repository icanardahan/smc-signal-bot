"""Ters işlem testi: aynı sinyallerde yönü çevirince ne oluyor?"""
import time
import backtest as bt
import scalp_lab as sl

end = int(time.time()*1000); start = end - 60*86400_000
syms = bt.top_symbols_by_volume(12)

def run_inv(c, ind, fn, tp_r, max_bars, invert):
    trades=[]; i=30; n=len(c)
    while i < n-2:
        s = fn(c,i,ind)
        if not s: i+=1; continue
        d, stop = s
        if invert:                      # yönü çevir, stop'u aynaya al
            entry0 = c[i+1]["open"]
            dist = abs(entry0-stop)
            d = "short" if d=="long" else "long"
            stop = entry0+dist if d=="short" else entry0-dist
        entry = c[i+1]["open"]; lg = d=="long"
        risk = abs(entry-stop)
        if risk<=0 or risk/entry<0.0005: i+=1; continue
        tp = entry+tp_r*risk if lg else entry-tp_r*risk
        out=None
        for k in range(i+1, min(n, i+1+max_bars)):
            b=c[k]
            if (b["low"]<=stop) if lg else (b["high"]>=stop): out=(-1.0,"stop",k); break
            if k>i+1 and ((b["high"]>=tp) if lg else (b["low"]<=tp)): out=(tp_r,"tp",k); break
        if out is None:
            k=min(n,i+1+max_bars)-1
            r=((c[k]["close"]-entry) if lg else (entry-c[k]["close"]))/risk
            out=(r,"timeout",k)
        trades.append({"R":out[0],"status":out[1],"risk_pct":100*risk/entry,
                       "move_pct":out[0]*100*risk/entry,"t":c[out[2]]["close_time"]})
        i=out[2]+1
    return trades

def money(h, fees=True):
    eq=[{"status":"tp1_hit" if t["R"]>0 else "sl_hit","move_pct":t["move_pct"],
         "exit_time":t["t"],"market_entry":True} for t in h]
    if not fees:
        o=bt.MAKER_FEE,bt.TAKER_FEE; bt.MAKER_FEE=bt.TAKER_FEE=0.0
        b,_,_=bt.simulate_equity(eq); bt.MAKER_FEE,bt.TAKER_FEE=o; return b-100
    b,_,_=bt.simulate_equity(eq); return b-100

res={}
for tf, strats, mb in [("5m", sl.SCALP, 60), ("15m", sl.DAY, 40)]:
    for sym in syms:
        try: c=bt.fetch_range(sym,tf,start,end)
        except Exception: continue
        if len(c)<500: continue
        ind={"atr":sl.atr_series(c),"vwap":sl.session_vwap(c),
             "ema9":sl.ema(c,9),"ema21":sl.ema(c,21)}
        m,s=sl.sma_std(c); ind["bb_m"],ind["bb_s"]=m,s
        for name,fn,tp_r in strats:
            for inv in (False,True):
                res.setdefault((name,inv),[]).extend(run_inv(c,ind,fn,tp_r,mb,inv))

print("="*96)
print("TERS İŞLEM TESTİ — aynı sinyaller, yön çevrilmiş")
print("="*96)
print(f"{'strateji':<32}{'yön':<8}{'işlem':>7}{'isabet':>8}{'komisyonsuz':>13}{'komisyonlu':>12}")
print("-"*96)
for name,_,_ in sl.SCALP+sl.DAY:
    for inv in (False,True):
        h=res.get((name,inv),[])
        if not h: continue
        w=sum(1 for t in h if t["R"]>0)
        print(f"{name:<32}{'TERS' if inv else 'normal':<8}{len(h):>7}"
              f"{100*w/len(h):>7.1f}%{money(h,False):>+12.1f}${money(h):>+11.1f}$")
print("-"*96)
