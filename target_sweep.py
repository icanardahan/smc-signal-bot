"""
Hedef (take-profit) taraması: isabet oranı ile R:R arasındaki takası ölçer.

Kurulum tespiti DEĞİŞTİRİLMEZ — sweep, MSS, kill zone, PD array girişi ve
"aralık hedefi hâlâ ulaşılabilir olmalı" şartı aynen kalır. Sadece ÇIKIŞ
hedefi değiştirilir: TP = giriş ± k × risk. Her k için isabet oranı,
işlem başına beklenti (R) ve 100$/10x ile bakiye sonucu raporlanır.

Amaç: "isabet %65'e çıkar mı" sorusunu parametre uydurmadan yanıtlamak.
Yakın hedefle isabet yükselir ama kazançlar küçülür; kârlılığı belirleyen
isabet değil BEKLENTİdir. Bu tablo ikisini yan yana gösterir.

Kullanım:
    python target_sweep.py [gün] [sembol]
"""

import sys
import time
from bisect import bisect_right

import backtest as bt
import ict_scanner as ict

TP_MULTIPLES = [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]


def collect_signals(symbol, daily_all, m5_all, scans):
    """Botun üreteceği sinyalleri toplar (çıkış hedefi hariç her şey aynı)."""
    mc = [c["close_time"] for c in m5_all]
    dc = [c["close_time"] for c in daily_all]
    out = []
    seen = set()
    for now in scans:
        i5 = bisect_right(mc, now)
        idd = bisect_right(dc, now)
        if i5 < 120 or idd < 15:
            continue
        try:
            r = ict.evaluate(daily_all[:idd], m5_all[max(0, i5 - bt.WINDOW_BARS):i5])
        except Exception:
            continue
        if not r or not r["qualifies"] or r["mss_time"] in seen:
            continue
        seen.add(r["mss_time"])
        out.append({"symbol": symbol, "dir": r["direction"], "entry": r["entry"],
                    "sl": r["sl"], "idx": i5, "session": r["session"],
                    "range_tp": r["tp1"]})
    return out


def simulate_exit(sig, m5, tp_r):
    """Emir dolar mı, sonra TP mi SL mi? (sonuç_R, durum) döner."""
    entry, sl = sig["entry"], sig["sl"]
    is_long = sig["dir"] == "long"
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp = entry + tp_r * risk if is_long else entry - tp_r * risk

    fill_i = None
    fill_deadline = sig["idx"] + int(ict.FILL_TIMEOUT_HOURS * 12)   # 12 mum/saat
    for k in range(sig["idx"], min(len(m5), fill_deadline)):
        c = m5[k]
        if c["low"] <= entry <= c["high"]:
            fill_i = k
            break
    if fill_i is None:
        return (0.0, "expired")

    hold_deadline = fill_i + int(ict.POSITION_TIMEOUT_HOURS * 12)
    for k in range(fill_i, min(len(m5), hold_deadline)):
        c = m5[k]
        hit_sl = (c["low"] <= sl) if is_long else (c["high"] >= sl)
        hit_tp = (c["high"] >= tp) if is_long else (c["low"] <= tp)
        if hit_sl:          # aynı mumda ikisi de olursa SL sayılır (muhafazakâr)
            return (-1.0, "sl")
        if hit_tp:
            return (tp_r, "tp")
    # zaman aşımı: o anki fiyattan kapat
    k = min(len(m5), hold_deadline) - 1
    move = ict.pct_move(entry, m5[k]["close"], is_long)
    return (move / (100 * risk / entry), "timeout")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    end = int(time.time() * 1000)
    start = end - days * 86400_000
    symbols = bt.top_symbols_by_volume(n_sym)
    scans = bt.scan_times(start, end)

    print(f"{len(symbols)} sembol, {days} gün, {len(scans)} tarama anı")
    print("Sinyaller toplanıyor...\n")

    all_sigs = []
    data = {}
    for i, sym in enumerate(symbols, 1):
        try:
            daily = bt.fetch_range(sym, "1d", start - 120 * 86400_000, end)
            m5 = bt.fetch_range(sym, "5m", start - 4 * 86400_000, end)
        except Exception as e:
            print(f"[{sym}] veri hatası: {e}")
            continue
        if len(m5) < 500 or len(daily) < 30:
            continue
        sigs = collect_signals(sym, daily, m5, scans)
        if sigs:
            data[sym] = m5
            all_sigs += sigs
        print(f"[{i}/{len(symbols)}] {sym}: {len(sigs)} sinyal")

    print(f"\nToplam {len(all_sigs)} sinyal toplandı.\n")
    if not all_sigs:
        return

    print("=" * 78)
    print("HEDEF TARAMASI — isabet oranı / beklenti / 100$ ile 10x izole sonuç")
    print("=" * 78)
    print(f"{'TP':>6} {'işlem':>7} {'isabet':>8} {'beklenti':>10} "
          f"{'bakiye':>10} {'net':>9}")
    print("-" * 78)

    for k in TP_MULTIPLES:
        results = []
        for s in all_sigs:
            out = simulate_exit(s, data[s["symbol"]], k)
            if out:
                results.append(out)
        traded = [r for r in results if r[1] != "expired"]
        decided = [r for r in traded if r[1] in ("tp", "sl")]
        if not decided:
            continue
        wins = sum(1 for r in decided if r[1] == "tp")
        wr = 100 * wins / len(decided)
        exp = sum(r[0] for r in traded) / len(traded)

        # 100$ / 10x sermaye simülasyonu (risk yüzdesi bilinmediği için
        # R cinsinden: 1R = ortalama risk mesafesi kadar fiyat hareketi)
        trades_for_eq = []
        for (rmult, st), s in zip(results, all_sigs):
            if st == "expired":
                continue
            risk_pct = 100 * abs(s["entry"] - s["sl"]) / s["entry"]
            trades_for_eq.append({"status": "x", "move_pct": rmult * risk_pct,
                                  "exit_time": 0})
        bal, mdd, _ = bt.simulate_equity(trades_for_eq)
        print(f"{k:>5.2f}R {len(traded):>7} {wr:>7.1f}% {exp:>+9.3f}R "
              f"{bal:>9.2f}$ {bal-bt.START_BALANCE:>+8.2f}$")

    print("-" * 78)
    print("beklenti = işlem başına ortalama R (zaman aşımı dahil).")
    print("Kârlılığı isabet değil BEKLENTİ belirler: yakın hedef isabeti")
    print("yükseltir ama kazançları küçültür.")


if __name__ == "__main__":
    main()
