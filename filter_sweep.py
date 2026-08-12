"""
Kalite filtresi taraması: "daha az ama daha kaliteli işlem" arayışı.

Teşhis: avantaj (+0.124R) komisyonla neredeyse aynı büyüklükte. Komisyon
yükü işlem SAYISINA ve nominal büyüklüğe bağlı olduğu için, işlem sayısını
azaltıp beklentiyi koruyan/yükselten filtreler net kârı artırır.

Yöntem — eğri uydurmayı önlemek için:
  1. Sinyaller EN GEVŞEK ayarlarla bir kez toplanır (metadata ile).
  2. Filtreler sonradan uygulanır (yeniden tarama gerekmez).
  3. Her filtre 1. YARIDA seçilir, 2. YARIDA doğrulanır. İkisinde de
     tutmayan filtre reddedilir.

Aday filtrelerin gerekçeleri:
  - risk/ATR tabanı: stop ne kadar genişse, sabit % risk için nominal o kadar
    küçük olur; komisyon nominale orantılı olduğundan doğrudan fee yükünü düşürür.
  - her iki giriş tetiği (FVG VE OTE): konfluans, tek tetikten güçlü olmalı.
  - aralık hedefi R:R tabanı: hedefe daha çok yer = daha kaliteli kurulum.
  - seans: London ve NY'nin ayrı ayrı davranışı.

Kullanım:
    python filter_sweep.py [gün] [sembol]
"""

import sys
import time
from bisect import bisect_right

import backtest as bt
import ict_scanner as ict
from target_sweep import simulate_exit

TP_R = ict.TP1_R_MULTIPLE   # çıkış 1R (hedef taramasında en tutarlı çıkan)


def collect_loose(symbol, daily_all, m5_all, scans, h4_all=None):
    """Sinyalleri en gevşek ayarlarla toplar; filtreler sonradan uygulanır."""
    mc = [c["close_time"] for c in m5_all]
    dc = [c["close_time"] for c in daily_all]
    h4c = [c["close_time"] for c in (h4_all or [])]
    out, seen = [], set()
    for now in scans:
        i5 = bisect_right(mc, now)
        idd = bisect_right(dc, now)
        if i5 < 120 or idd < 15:
            continue
        try:
            r = ict.evaluate(daily_all[:idd], m5_all[max(0, i5 - bt.WINDOW_BARS):i5],
                             h4_all[:bisect_right(h4c, now)] if h4_all else None)
        except Exception:
            continue
        if not r or not r["qualifies"] or r["mss_time"] in seen:
            continue
        seen.add(r["mss_time"])
        risk = abs(r["entry"] - r["sl"])
        out.append({
            "symbol": symbol, "dir": r["direction"], "entry": r["entry"],
            "sl": r["sl"], "idx": i5, "idx_time": now, "session": r["session"],
            "risk_pct": r["risk_pct"], "atr_pct": r["atr_pct"],
            "risk_atr": (r["risk_pct"] / r["atr_pct"]) if r["atr_pct"] else 0,
            "range_rr": abs(r["range_tp"] - r["entry"]) / risk if risk else 0,
            "both_triggers": r["criteria"]["fvg_entry"] and r["criteria"]["ote_entry"],
            "vwap_ok": r.get("vwap_ok"), "ichimoku_ok": r.get("ichimoku_ok"),
            "vol_surge": r.get("vol_surge"), "obv_ok": r.get("obv_ok"),
        })
    return out


def evaluate_filter(sigs, data, keep, mid_time):
    """Filtreyi uygular, 1. ve 2. yarı için (adet, isabet, beklenti, net$) döner."""
    halves = [[], []]
    for s in sigs:
        if not keep(s):
            continue
        out = simulate_exit(s, data[s["symbol"]], TP_R)
        if not out or out[1] == "expired":
            continue
        halves[0 if s["idx_time"] <= mid_time else 1].append((out, s))

    res = []
    for h in halves:
        if not h:
            res.append((0, 0.0, 0.0, 0.0))
            continue
        dec = [x for x in h if x[0][1] in ("tp", "sl")]
        wr = 100 * sum(1 for x in dec if x[0][1] == "tp") / len(dec) if dec else 0
        exp = sum(x[0][0] for x in h) / len(h)
        trades = [{"status": "tp1_hit" if o[1] == "tp" else "sl_hit",
                   "move_pct": o[0] * s["risk_pct"], "exit_time": 0} for o, s in h]
        bal, _, _ = bt.simulate_equity(trades)
        res.append((len(h), wr, exp, bal - bt.START_BALANCE))
    return res


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    end = int(time.time() * 1000)
    start = end - days * 86400_000
    symbols = bt.top_symbols_by_volume(n_sym)
    scans = bt.scan_times(start, end)

    # En gevşek ayarlar: filtreler sonradan uygulanacak
    ict.MIN_RISK_ATR = 0.5
    ict.MIN_RISK_PCT = 0.10
    ict.MIN_TP1_RR = 2.0
    ict.MIN_CONFIRMATIONS = 1

    print(f"{len(symbols)} sembol, {days} gün, {len(scans)} tarama anı")
    print("Sinyaller gevşek ayarlarla toplanıyor...\n")

    sigs, data = [], {}
    for i, sym in enumerate(symbols, 1):
        try:
            daily = bt.fetch_range(sym, "1d", start - 120 * 86400_000, end)
            m5 = bt.fetch_range(sym, "5m", start - 4 * 86400_000, end)
            h4 = bt.fetch_range(sym, "4h", start - 60 * 86400_000, end)
        except Exception:
            continue
        if len(m5) < 500 or len(daily) < 30:
            continue
        s = collect_loose(sym, daily, m5, scans, h4)
        if s:
            data[sym] = m5
            sigs += s
        print(f"[{i}/{len(symbols)}] {sym}: {len(s)}", flush=True)

    print(f"\nToplam {len(sigs)} ham sinyal.\n")
    if not sigs:
        return
    mid = sorted(s["idx_time"] for s in sigs)[len(sigs) // 2]

    filters = [
        ("filtresiz (temel)",            lambda s: True),
        ("risk >= 1.0 ATR",              lambda s: s["risk_atr"] >= 1.0),
        ("risk >= 1.5 ATR",              lambda s: s["risk_atr"] >= 1.5),
        ("risk >= 2.0 ATR",              lambda s: s["risk_atr"] >= 2.0),
        ("risk >= 2.5 ATR",              lambda s: s["risk_atr"] >= 2.5),
        ("her iki tetik (FVG+OTE)",      lambda s: s["both_triggers"]),
        ("aralık R:R >= 3",              lambda s: s["range_rr"] >= 3),
        ("aralık R:R >= 4",              lambda s: s["range_rr"] >= 4),
        ("aralık R:R >= 6",              lambda s: s["range_rr"] >= 6),
        ("sadece London",                lambda s: s["session"] == "london"),
        ("sadece NY",                    lambda s: s["session"] == "ny"),
        ("risk>=2ATR + her iki tetik",   lambda s: s["risk_atr"] >= 2.0 and s["both_triggers"]),
        ("risk>=2ATR + R:R>=4",          lambda s: s["risk_atr"] >= 2.0 and s["range_rr"] >= 4),
        ("VWAP teyidi",                  lambda s: s["vwap_ok"] is True),
        ("Ichimoku 4H teyidi",           lambda s: s["ichimoku_ok"] is True),
        ("Hacim patlaması >=1.5x",       lambda s: (s["vol_surge"] or 0) >= 1.5),
        ("Hacim patlaması >=2.0x",       lambda s: (s["vol_surge"] or 0) >= 2.0),
        ("OBV teyidi",                   lambda s: s["obv_ok"] is True),
        ("VWAP + hacim>=1.5x",           lambda s: s["vwap_ok"] is True and (s["vol_surge"] or 0) >= 1.5),
        ("Ichimoku + hacim>=1.5x",       lambda s: s["ichimoku_ok"] is True and (s["vol_surge"] or 0) >= 1.5),
        ("risk>=1.5ATR + hacim>=1.5x",   lambda s: s["risk_atr"] >= 1.5 and (s["vol_surge"] or 0) >= 1.5),
    ]

    print("=" * 92)
    print(f"FİLTRE TARAMASI — çıkış {TP_R}R, 100$/10x")
    print("=" * 92)
    print(f"{'filtre':<28}{'1.yarı: adet':>13}{'isabet':>8}{'bekl.':>8}{'net$':>8}"
          f"{'2.yarı: adet':>15}{'isabet':>8}{'bekl.':>8}{'net$':>8}")
    print("-" * 92)
    for name, fn in filters:
        (n1, w1, e1, d1), (n2, w2, e2, d2) = evaluate_filter(sigs, data, fn, mid)
        print(f"{name:<28}{n1:>13}{w1:>7.1f}%{e1:>+8.3f}{d1:>+8.2f}"
              f"{n2:>15}{w2:>7.1f}%{e2:>+8.3f}{d2:>+8.2f}")
    print("-" * 92)
    print("Bir filtre ancak İKİ yarıda da beklentiyi koruyorsa gerçek sayılır.")
    print("Sadece bir yarıda parlayan filtre eğri uydurmadır.")


if __name__ == "__main__":
    main()
