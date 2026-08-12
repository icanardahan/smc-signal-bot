"""
ICT 2022 modelinin geçmiş veri üzerinde simülasyonu.

Botun GERÇEK çalışma şeklini taklit eder:
  - Tarama yalnızca cron saatlerinde yapılır (her 4 saatte bir), yani canlıda
    kaçırılacak kurulumlar burada da kaçırılır.
  - Her tarama anında modele SADECE o ana kadarki mumlar verilir (ileriye
    bakış yok) — canlı taramayla aynı `evaluate()` fonksiyonu çağrılır.
  - Sinyal çıkınca bekleyen emir konur; dolum, SL/TP ve zaman aşımı canlıdaki
    `monitor_position()` ile aynı mantıkla simüle edilir.

Sonuç R katları (R multiple) cinsinden raporlanır: 1R = giriş ile SL arası
mesafe. TP1'de çıkış varsayılır (dokümanın birincil hedefi).

Kullanım:
    python backtest.py [gün_sayısı] [sembol_sayısı]
"""

import json
import os
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone

import ict_scanner as ict

SCAN_HOURS_UTC = tuple(range(24))  # workflow cron saatleri (saatlik)
SCAN_MINUTE = 5
WINDOW_BARS = 1152  # her taramada modele verilen kuyruk penceresi (~4 gün)

# ---- Sermaye modeli: 100 dolar, 10x izole marjin ----
START_BALANCE = 100.0
LEVERAGE = 10
MARGIN_PCT = 0.10   # her işlemde bakiyenin %10'u marjin olarak ayrılır
                    # (10x ile nominal = bakiye; aynı anda ~10 pozisyon mümkün)
# Komisyon: giriş BEKLEYEN (limit) emirle olduğu için maker; TP çıkışı da limit
# emirle alınabilir (maker). Sadece SL/zaman aşımı piyasa emri = taker.
MAKER_FEE = 0.0002  # Binance futures maker ~%0.02
TAKER_FEE = 0.0005  # Binance futures taker ~%0.05


def round_trip_fee(status):
    """Giriş her zaman maker; çıkış kazançta maker, stopta taker."""
    exit_fee = MAKER_FEE if status in ("tp1_hit", "tp2_hit", "tp3_hit") else TAKER_FEE
    return MAKER_FEE + exit_fee


def top_symbols_by_volume(n):
    """En likit N USDT paritesi. Alfabetik ilk N sembol düşük hacimli
    coinlere denk geldiği için temsili bir backtest vermez."""
    allowed = set(ict.get_usdt_symbols())
    rows = ict.http_get_json(f"{ict.BINANCE_BASE}/api/v3/ticker/24hr")
    vols = [(float(r["quoteVolume"]), r["symbol"]) for r in rows
            if r["symbol"] in allowed]
    vols.sort(reverse=True)
    return [s for _, s in vols[:n]]


CACHE_DIR = os.path.join(os.path.dirname(__file__), ".backtest_cache")


def fetch_range(symbol, interval, start_ms, end_ms):
    """Binance 1000 mum sınırını aşmak için sayfalayarak veri çeker.
    Sonuç diske önbelleklenir — parametre değiştirip tekrar denerken
    aynı veriyi baştan indirmemek için."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Gün bazlı kova: aynı gün içindeki tekrar koşular veriyi yeniden indirmez
    # (saat bazlı anahtar her koşuda değişip önbelleği işe yaramaz kılıyordu).
    key = f"{symbol}_{interval}_{start_ms // 86400000}_{end_ms // 86400000}.json"
    path = os.path.join(CACHE_DIR, key)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)

    out = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"{ict.BINANCE_BASE}/api/v3/klines?symbol={symbol}"
               f"&interval={interval}&startTime={cursor}&endTime={end_ms}&limit=1000")
        rows = ict.http_get_json(url)
        if not rows:
            break
        for r in rows:
            if r[6] <= end_ms:
                out.append({"open_time": r[0], "close_time": r[6],
                            "open": float(r[1]), "high": float(r[2]),
                            "low": float(r[3]), "close": float(r[4])})
        nxt = rows[-1][0] + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(ict.REQUEST_SLEEP)

    with open(path, "w") as f:
        json.dump(out, f)
    return out


def scan_times(start_ms, end_ms):
    """Cron'un tetikleneceği anlar."""
    t = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(
        minute=SCAN_MINUTE, second=0, microsecond=0)
    out = []
    while int(t.timestamp() * 1000) < end_ms:
        if t.hour in SCAN_HOURS_UTC:
            ms = int(t.timestamp() * 1000)
            if ms >= start_ms:
                out.append(ms)
        t = t.replace(hour=(t.hour + 1) % 24)
        if t.hour == 0:
            t = datetime.fromtimestamp(t.timestamp() + 86400, tz=timezone.utc).replace(
                hour=0, minute=SCAN_MINUTE, second=0, microsecond=0)
    return out


def simulate_symbol(symbol, daily_all, m5_all, scans):
    """Bir sembolde botun ne yapacağını baştan sona simüle eder."""
    m5_close = [c["close_time"] for c in m5_all]
    d_close = [c["close_time"] for c in daily_all]
    trades = []
    state = {}  # yön -> pozisyon

    for now in scans:
        i5 = bisect_right(m5_close, now)
        idd = bisect_right(d_close, now)
        if i5 < 120 or idd < 15:
            continue
        # Model yalnızca son SESSION_DAYS_BACK güne bakar; tüm geçmişi vermek
        # sonucu değiştirmez ama taramayı yavaşlatır. Güvenli bir kuyruk
        # penceresi yeterli (4 gün = 1152 adet 5dk mum).
        m5 = m5_all[max(0, i5 - WINDOW_BARS):i5]
        daily = daily_all[:idd]

        # 1) Takipteki emir/pozisyonları güncelle
        for d in ("long", "short"):
            pos = state.get(d)
            if pos and pos.get("status") not in ict.CLOSED_STATES:
                pos, _ = ict.monitor_position(pos, m5, d)
                state[d] = pos
                if pos["status"] in ict.CLOSED_STATES:
                    trades.append(finish(symbol, d, pos))

        # 2) Yeni sinyal var mı?
        try:
            r = ict.evaluate(daily, m5)
        except Exception:
            continue
        if not r or not r["qualifies"]:
            continue

        d = r["direction"]
        pos = state.get(d)
        if pos and pos.get("mss_time") == r["mss_time"]:
            continue
        if pos and pos.get("status") not in ict.CLOSED_STATES:
            continue

        state[d] = {
            "mss_time": r["mss_time"], "signal_time": m5[-1]["close_time"],
            "last_checked_close_time": m5[-1]["close_time"],
            "entry": r["entry"], "entry_kind": r["entry_kind"], "sl": r["sl"],
            "tp1": r["tp1"], "tp2": r["tp2"], "tp3": r["tp3"],
            "status": "pending", "session": r["session"], "rr1": r["rr1"],
        }

    # Simülasyon sonunda hâlâ açık kalanları da sonuçlandır
    for d, pos in state.items():
        if pos and pos.get("status") not in ict.CLOSED_STATES:
            pos, _ = ict.monitor_position(pos, m5_all, d)
            if pos["status"] in ict.CLOSED_STATES:
                trades.append(finish(symbol, d, pos))
            else:
                trades.append(finish(symbol, d, pos, unresolved=True))
    return trades


def finish(symbol, direction, pos, unresolved=False):
    """İşlemi R katı VE fiyat yüzdesi cinsinden sonuçlandırır.
    TP1'de çıkış varsayılır (dokümanın birincil hedefi)."""
    entry, sl, tp1 = pos["entry"], pos["sl"], pos.get("tp1")
    risk = abs(entry - sl)
    st = pos["status"]
    is_long = direction == "long"

    if st == "sl_hit":
        r, move_pct = -1.0, ict.pct_move(entry, sl, is_long)
    elif st == "be_stop":
        # TP1 alındı, sonra başabaş stopla çıkıldı: TP1 kazancı cepte kalır
        r = abs(tp1 - entry) / risk if risk else 0.0
        move_pct = ict.pct_move(entry, tp1, is_long)
    elif st in ("tp1_hit", "tp2_hit", "tp3_hit"):
        r = abs(tp1 - entry) / risk if risk else 0.0
        move_pct = ict.pct_move(entry, tp1, is_long)
    elif st == "timeout":
        if pos.get("tp1_reached"):
            # TP1'e ulaşılmıştı: "TP1'de çıkış" varsayımı gereği kazanç sayılır
            r = abs(tp1 - entry) / risk if risk else 0.0
            move_pct = ict.pct_move(entry, tp1, is_long)
        else:
            ex = pos.get("exit_price", entry)
            move_pct = ict.pct_move(entry, ex, is_long)
            r = move_pct / (100 * risk / entry) if risk else 0.0
    else:  # expired: emir hiç dolmadı, para riske girmedi
        r, move_pct = 0.0, 0.0

    return {"symbol": symbol, "direction": direction, "status": st,
            "R": r, "move_pct": move_pct, "session": pos.get("session"),
            "rr1": pos.get("rr1"),
            "risk_pct": 100 * risk / entry if entry else None,
            "entry_kind": pos.get("entry_kind"),
            "exit_time": pos.get("exit_time", 0), "tp1_reached": pos.get("tp1_reached", False),
            "unresolved": unresolved}


RISK_PCT_PER_TRADE = 0.02   # risk bazlı modelde: her işlemde bakiyenin %2'si


def simulate_equity_risk_based(trades):
    """Her işlemde bakiyenin sabit bir YÜZDESİ riske atılır; pozisyon boyutu
    stop mesafesine göre ayarlanır (kaldıraç 10x ile sınırlı).

    Sabit marjin modelinde stopu geniş olan işlem otomatik olarak çok daha
    fazla dolar riske atıyor; bu da sonucu birkaç işlemin şansına bağlıyor.
    Doğru karşılaştırma için R beklentisiyle uyumlu olan bu model kullanılır."""
    bal = START_BALANCE
    peak = bal
    mdd = 0.0
    n = 0
    for t in sorted(trades, key=lambda x: x["exit_time"] or 0):
        if t["status"] == "expired" or not t.get("risk_pct"):
            continue
        risk_usd = bal * RISK_PCT_PER_TRADE
        notional = risk_usd / (t["risk_pct"] / 100)
        notional = min(notional, bal * LEVERAGE)     # 10x üst sınırı
        pnl = notional * t["move_pct"] / 100
        pnl -= notional * round_trip_fee(t["status"])
        pnl = max(pnl, -bal * MARGIN_PCT)            # izole marjin sınırı
        bal += pnl
        peak = max(peak, bal)
        mdd = min(mdd, (bal - peak) / peak * 100)
        n += 1
        if bal <= 1:
            break
    return bal, mdd, n


def simulate_equity(trades):
    """100 dolarlık bakiyeyi, 10x izole marjinle kronolojik olarak işletir.
    Her işlemde bakiyenin MARGIN_PCT'i marjin olarak ayrılır; 10x kaldıraçla
    nominal büyüklük bunun 10 katıdır. İzole marjinde bir işlemde
    kaybedilebilecek en fazla tutar o işlemin marjinidir."""
    bal = START_BALANCE
    peak = bal
    mdd = 0.0
    curve = []
    for t in sorted(trades, key=lambda x: x["exit_time"] or 0):
        if t["status"] == "expired":
            continue  # emir dolmadı, komisyon/pozisyon yok
        margin = bal * MARGIN_PCT
        notional = margin * LEVERAGE
        pnl = notional * t["move_pct"] / 100
        pnl -= notional * round_trip_fee(t["status"])   # giriş + çıkış komisyonu
        pnl = max(pnl, -margin)                  # izole: en fazla marjin kadar
        bal += pnl
        peak = max(peak, bal)
        mdd = min(mdd, (bal - peak) / peak * 100)
        curve.append(bal)
        if bal <= 1:
            break
    return bal, mdd, curve


def report(trades):
    if not trades:
        print("Hiç sinyal üretilmedi.")
        return
    filled = [t for t in trades if t["status"] not in ("expired",)]
    resolved = [t for t in trades if t["status"] in ("sl_hit", "be_stop", "tp1_hit", "tp2_hit", "tp3_hit") or (t["status"] == "timeout" and t.get("tp1_reached"))]
    wins = [t for t in resolved if t["R"] > 0]
    losses = [t for t in resolved if t["R"] < 0]
    expired = [t for t in trades if t["status"] == "expired"]
    timeouts = [t for t in trades if t["status"] == "timeout"]

    print("=" * 62)
    print("BACKTEST SONUCU")
    print("=" * 62)
    print(f"Üretilen sinyal      : {len(trades)}")
    print(f"  emir dolmadı (iptal): {len(expired)}")
    print(f"  dolan pozisyon      : {len(filled)}")
    print(f"  zaman aşımı         : {len(timeouts)}")
    print(f"  sonuçlanan (SL/TP)  : {len(resolved)}")
    if resolved:
        wr = 100 * len(wins) / len(resolved)
        total_r = sum(t["R"] for t in resolved)
        avg_r = total_r / len(resolved)
        print()
        print(f"Kazanan / Kaybeden   : {len(wins)} / {len(losses)}   (isabet %{wr:.1f})")
        print(f"Toplam getiri        : {total_r:+.2f}R")
        print(f"İşlem başına ortalama: {avg_r:+.3f}R")
        if wins:
            print(f"Ortalama kazanç      : {sum(t['R'] for t in wins)/len(wins):+.2f}R")
        if losses:
            print(f"Ortalama kayıp       : {sum(t['R'] for t in losses)/len(losses):+.2f}R")
        # Sermaye eğrisi ve maksimum geri çekilme
        eq = 0.0
        peak = 0.0
        mdd = 0.0
        for t in resolved:
            eq += t["R"]
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        print(f"Maks. geri çekilme   : {mdd:.2f}R")
        print()
        for sess in ("london", "ny"):
            sub = [t for t in resolved if t["session"] == sess]
            if sub:
                w = sum(1 for t in sub if t["R"] > 0)
                print(f"  {sess:7s}: {len(sub)} işlem, isabet %{100*w/len(sub):.0f}, "
                      f"{sum(t['R'] for t in sub):+.2f}R")
    # Risk mesafesi dağılımı: R:R filtresi çok küçük riskli (SL'i girişe bir
    # kıl payı uzak) kurulumları seçiyorsa işlemler gürültüde stop olur.
    risks = sorted(t["risk_pct"] for t in resolved if t.get("risk_pct"))
    if risks:
        def q(p):
            return risks[min(len(risks) - 1, int(p * len(risks)))]
        print()
        print("Risk mesafesi (|giriş-SL| / giriş, %):")
        print(f"  medyan {q(0.5):.3f}%   |  %25: {q(0.25):.3f}%   |  %75: {q(0.75):.3f}%")
        print(f"  en dar {risks[0]:.4f}%  |  en geniş {risks[-1]:.3f}%")
        tiny = [r for r in risks if r < 0.15]
        print(f"  %0.15'ten dar olan: {len(tiny)}/{len(risks)} işlem")

    # ---- Dolar bazlı sonuç ----
    bal, mdd_pct, curve = simulate_equity(trades)
    print()
    print("-" * 62)
    print(f"SERMAYE ({START_BALANCE:.0f}$ başlangıç, {LEVERAGE}x izole marjin, "
          f"işlem başına bakiyenin %{MARGIN_PCT*100:.0f}'i marjin)")
    print("-" * 62)
    print(f"Başlangıç            : {START_BALANCE:8.2f}$")
    print(f"Bitiş                : {bal:8.2f}$")
    print(f"Net                  : {bal - START_BALANCE:+8.2f}$  "
          f"({100*(bal-START_BALANCE)/START_BALANCE:+.1f}%)")
    print(f"Maks. geri çekilme   : {mdd_pct:8.1f}%")
    print(f"İşlem gören pozisyon : {len(curve)}")
    print(f"(komisyon: maker %{MAKER_FEE*100:.2f} giriş/TP, taker %{TAKER_FEE*100:.2f} stop)")

    rb_bal, rb_mdd, rb_n = simulate_equity_risk_based(trades)
    print()
    print(f"RİSK BAZLI boyutlama (her işlemde bakiyenin %{RISK_PCT_PER_TRADE*100:.0f}'i risk,")
    print(f"pozisyon stop mesafesine göre, kaldıraç {LEVERAGE}x ile sınırlı):")
    print(f"  Bitiş              : {rb_bal:8.2f}$   "
          f"({100*(rb_bal-START_BALANCE)/START_BALANCE:+.1f}%)  "
          f"maks. geri çekilme {rb_mdd:.1f}%")
    print("  ↑ Bu model R beklentisiyle tutarlıdır. Sabit marjin modelinde")
    print("    stopu geniş işlemler çok daha fazla dolar riske attığı için")
    print("    sonuç birkaç işlemin şansına bağlı kalır.")

    print()
    print("Not: 1R = giriş-SL mesafesi. TP1'de çıkış varsayıldı; zaman aşımı ve")
    print("çözülmemiş işlemler 0R (nötr) sayıldı. Komisyon/kayma dahil değildir.")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400_000
    symbols = top_symbols_by_volume(n_sym)

    print(f"{len(symbols)} sembol (hacme göre en likit), son {days} gün simüle ediliyor...")
    scans = scan_times(start_ms, end_ms)
    print(f"{len(scans)} tarama anı (4 saatte bir)\n")

    all_trades = []
    for i, sym in enumerate(symbols, 1):
        try:
            daily = fetch_range(sym, "1d", start_ms - 120 * 86400_000, end_ms)
            m5 = fetch_range(sym, "5m", start_ms - 4 * 86400_000, end_ms)
        except Exception as e:
            print(f"[{sym}] veri hatası: {e}")
            continue
        if len(m5) < 500 or len(daily) < 30:
            print(f"[{sym}] yetersiz veri, atlandı")
            continue
        t = simulate_symbol(sym, daily, m5, scans)
        all_trades += t
        print(f"[{i}/{len(symbols)}] {sym}: {len(t)} sinyal "
              f"({len(m5)} adet 5dk mum)")

    print()
    report(all_trades)


if __name__ == "__main__":
    main()
