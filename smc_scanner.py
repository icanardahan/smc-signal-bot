"""
SMC Haftalık/Günlük/4H tarayıcı — sürüklenen stoplu, yalnızca LONG.

Kullanıcının TradingView'de takip ettiği LuxAlgo SMC göstergesinin kavramlarına
göre çalışır ve baktığı üç zaman dilimini kullanır: haftalık ve günlük yön,
4 saatlik giriş.

Akış:
  haftalık + günlük yön AYNI olmalı -> fiyat discount bölgesinde -> 4H'de
  bias yönünde taze CHoCH/BOS -> order block ortasına BEKLEYEN limit emir ->
  stop OB'nin altına -> çıkış SÜRÜKLENEN STOP (sabit kâr al YOK).

Neden bu biçim (hepsi 2 yıl, 40 sembol, sabit evren üzerinde ölçüldü):
  - Sadece long: short beklentisi -0.046R, long +0.116R.
  - Sürüklenen stop: sabit TP1/TP2/TP3 ile +0.122R (t=1.71, gürültü);
    sürüklenen stopla +0.240R (t=2.33). Kazancın çoğu birkaç büyük
    işlemden geldiği için TP1'de yarıyı kapatmak tam da onları kesiyordu.
  - Hedef seviyesi yalnızca R:R filtresi olarak kullanılır; işlem oraya
    varınca kapanmaz.

DİKKAT — bu strateji KANITLANMIŞ DEĞİLDİR. Çok sayıda varyant denendiği için
t=2.33 tek başına yeterli kanıt sayılmaz ve 2026 dilimi eksidir. Bu yüzden
amaç ileri testtir; otomatik işlem varsayılan olarak KAPALIDIR.

Kurulum arama ve stop sürükleme mantığı smc_htf.py içindedir; backtest de
AYNI fonksiyonları çağırır, böylece ikisi ayrışamaz.
"""

import json
import os
import time
from datetime import datetime, timezone

import smc_htf as smc
from ict_scanner import (BINANCE_BASE, EXCLUDE_BASE_STABLES, EXCLUDE_SUFFIXES,
                         http_get_json, fetch_klines, send_telegram)

STATE_FILE = os.path.join(os.path.dirname(__file__), "smc_state.json")

# --- Doğrulanmış parametreler (backtest ile birebir aynı olmalı) ---
SETUP_MAX_AGE_BARS = 6      # kırılım en fazla 1 gün eski
SL_ATR_MULT = 0.25
MIN_RR = 1.5
MAX_RR = 6.0
LIQ_LEN = 20
DISCOUNT_MAX = 0.5
DIR_FILTER = "long"
TRAIL_LEN = 5
FILL_TIMEOUT_BARS = 30      # 5 gün dolmazsa emir iptal
HOLD_TIMEOUT_BARS = 60      # 10 gün sonra pozisyon kapatılır

UNIVERSE_SIZE = int(os.environ.get("SMC_UNIVERSE", "40"))
MAX_OPEN = int(os.environ.get("SMC_MAX_OPEN", "5"))
RISK_PCT_OF_BALANCE = float(os.environ.get("SMC_RISK_PCT", "2.0"))
LEVERAGE = 10

H4_LIMIT = 500
D1_LIMIT = 300
W1_LIMIT = 200

BAR_MS = 4 * 3600 * 1000

LIVE_STATES = ("pending", "open")
CLOSED_STATES = ("stopped", "trail_stop", "expired", "timeout")


# ---------------- Evren ----------------
def top_symbols(n=UNIVERSE_SIZE):
    """Hacme göre ilk n USDT paritesi — backtest'in kullandığı evrenle aynı tanım."""
    data = http_get_json(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    rows = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("USDT") or not s.isascii() or s.endswith(EXCLUDE_SUFFIXES):
            continue
        if s[:-4] in EXCLUDE_BASE_STABLES:
            continue
        try:
            rows.append((float(t["quoteVolume"]), s))
        except (KeyError, ValueError):
            continue
    rows.sort(reverse=True)
    return [s for _, s in rows[:n]]


# ---------------- Durum ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _fmt(v):
    if v is None:
        return "n/a"
    return f"{v:.8f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.4f}".rstrip("0").rstrip(".")


def _ts(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d.%m %H:%M UTC")


# ---------------- Pozisyon takibi ----------------
def monitor(pos, h4):
    """Bekleyen/açık pozisyonu yeni 4H barlarıyla ilerletir.

    Backtest'teki simulate() ile aynı sırayı izler: önce stop kontrolü, dolum
    mumunda stop sürüklenmez, stop yalnızca lehe hareket eder."""
    olaylar = []
    entry, stop = pos["entry"], pos["stop"]
    is_long = pos["dir"] == "long"
    tl = smc.trail_levels(h4, is_long, TRAIL_LEN)

    for k, c in enumerate(h4):
        if c["close_time"] <= pos["last_bar"]:
            continue
        pos["last_bar"] = c["close_time"]
        pos["bars"] = pos.get("bars", 0) + 1
        just_filled = False

        if pos["status"] == "pending":
            if c["low"] <= entry <= c["high"]:
                pos["status"] = "open"
                pos["fill_time"] = c["close_time"]
                pos["bars"] = 0
                just_filled = True
                olaylar.append(("filled", c))
            elif pos["bars"] >= FILL_TIMEOUT_BARS:
                pos["status"] = "expired"
                olaylar.append(("expired", c))
                break
            else:
                continue

        if (c["low"] <= stop) if is_long else (c["high"] >= stop):
            pos["status"] = "trail_stop" if pos.get("trailed") else "stopped"
            pos["exit"] = stop
            olaylar.append((pos["status"], c))
            break

        if just_filled:                    # dolum mumunda stop sürüklenmez
            continue

        yeni = tl[k]
        if yeni is not None:
            ileri = (yeni > stop) if is_long else (yeni < stop)
            if ileri:
                stop = yeni
                pos["stop"] = stop
                pos["trailed"] = True
                olaylar.append(("trail", c))

        if pos["bars"] >= HOLD_TIMEOUT_BARS:
            pos["status"] = "timeout"
            pos["exit"] = c["close"]
            olaylar.append(("timeout", c))
            break

    return olaylar


def pnl_pct(pos, price):
    e = pos["entry"]
    return (price - e) / e * 100 if pos["dir"] == "long" else (e - price) / e * 100


# ---------------- Mesajlar ----------------
def signal_message(symbol, sig, bal):
    # Sizing sabit marjin DEĞİL: stop mesafesi işlemden işleme çok değişiyor,
    # sabit marjinde risk 16 kata kadar farklılaşıyor. Doğrulanan sonuç
    # işlem başına bakiyenin %2'sini riske eden sizing ile ölçüldü.
    risk_usdt = bal * RISK_PCT_OF_BALANCE / 100
    notional = min(risk_usdt / (sig["risk_pct"] / 100), bal / MAX_OPEN * LEVERAGE)
    marj = notional / LEVERAGE
    return (
        f"🟢 <b>{symbol} LONG</b>  (SMC H/G/4S)\n"
        f"<i>{sig['tip']} — order block girişi</i>\n\n"
        f"Giriş (limit) : <b>{_fmt(sig['entry'])}</b>\n"
        f"Başlangıç stop: <b>{_fmt(sig['sl'])}</b>  (%{sig['risk_pct']:.2f})\n"
        f"Referans hedef: {_fmt(sig['tps'][0])}  (R:R {sig['rr']:.2f})\n\n"
        f"⚠️ <b>Sabit kâr al yok.</b> Stop, 4H yapının arkasından yukarı "
        f"sürüklenir; işlem stop ile kapanır. Referans hedef yalnızca "
        f"kurulum filtresidir, orada kapatma yapılmaz.\n\n"
        f"Büyüklük: <b>{notional:.1f} USDT</b> nominal "
        f"(~{marj:.2f} USDT marj, {LEVERAGE}x izole)\n"
        f"Riskin: <b>{risk_usdt:.2f} USDT</b> — stop mesafesi %{sig['risk_pct']:.2f} "
        f"olduğu için büyüklük buna göre küçültüldü.\n"
        f"Emir {FILL_TIMEOUT_BARS // 6} gün dolmazsa iptal."
    )


def event_message(symbol, pos, event, candle):
    p = pos["entry"]
    if event == "filled":
        return (f"✅ <b>{symbol}</b> emir doldu → pozisyon açık\n"
                f"Giriş {_fmt(p)} | stop {_fmt(pos['stop'])}")
    if event == "trail":
        return (f"🔒 <b>{symbol}</b> stop yukarı taşındı → <b>{_fmt(pos['stop'])}</b>\n"
                f"Giriş {_fmt(p)} | fiyat {_fmt(candle['close'])} "
                f"({pnl_pct(pos, candle['close']):+.2f}%)")
    if event in ("stopped", "trail_stop"):
        r = pnl_pct(pos, pos["exit"])
        ikon = "🟡" if event == "trail_stop" else "🔴"
        ad = "sürüklenen stop" if event == "trail_stop" else "stop"
        return (f"{ikon} <b>{symbol}</b> {ad} ile kapandı @ {_fmt(pos['exit'])}\n"
                f"Sonuç: <b>{r:+.2f}%</b> (10x → {r * 10:+.1f}% marj)")
    if event == "timeout":
        r = pnl_pct(pos, pos["exit"])
        return (f"⏱ <b>{symbol}</b> süre doldu, kapatıldı @ {_fmt(pos['exit'])}\n"
                f"Sonuç: <b>{r:+.2f}%</b>")
    if event == "expired":
        return f"⚪️ <b>{symbol}</b> emir {FILL_TIMEOUT_BARS // 6} günde dolmadı, iptal."
    return f"{symbol}: {event}"


# ---------------- Ana akış ----------------
def _init_trading():
    try:
        import binance_trader as bt
    except Exception as e:
        print(f"Otomatik işlem modülü yüklenemedi: {e}")
        return None, None, 0.0
    if not bt._enabled():
        return None, None, 0.0
    api = bt.BinanceFutures()
    try:
        api.load_filters()
        bal = api.balance_usdt()
    except Exception as e:
        print(f"Binance bağlantı hatası, otomatik işlem devre dışı: {e}")
        return None, None, 0.0
    mod = "TESTNET" if bt._testnet() else "GERÇEK PARA"
    if bt._dry_run():
        mod += " / KURU ÇALIŞMA"
    print(f"Otomatik işlem AÇIK — {mod} | bakiye {bal:.2f} USDT")
    return bt, api, bal


def main():
    state = load_state()
    positions = state.setdefault("positions", {})
    bt, api, bal = _init_trading()
    if not bal:
        bal = 100.0

    # 1) Açık/bekleyen pozisyonları ilerlet
    for symbol, pos in list(positions.items()):
        if pos["status"] not in LIVE_STATES:
            continue
        try:
            h4 = fetch_klines(symbol, "4h", 200)
        except Exception as e:
            print(f"[{symbol}] veri alınamadı: {e}")
            continue
        for event, candle in monitor(pos, h4):
            print(f"  [{symbol}] {event}")
            send_telegram(event_message(symbol, pos, event, candle))
            if api and event == "trail":
                try:
                    bt.update_stop(api, symbol, pos["dir"], pos["stop"])
                except Exception as e:
                    print(f"  [{symbol}] stop taşınamadı: {e}")
        if api and pos["status"] == "open":
            try:
                p = api.positions().get(symbol)
                if p and p["amt"]:
                    bt.ensure_protection(api, symbol, pos["dir"], p["amt"],
                                         pos["stop"], (None, None, None))
            except Exception as e:
                print(f"  [{symbol}] koruma kontrolü başarısız: {e}")
        if api and pos["status"] in CLOSED_STATES:
            try:
                api.cancel_all(symbol)
            except Exception as e:
                print(f"  [{symbol}] artık emirler temizlenemedi: {e}")

    acik = sum(1 for p in positions.values() if p["status"] in LIVE_STATES)

    # 2) Yeni kurulum ara
    symbols = top_symbols()
    print(f"{len(symbols)} sembol taranıyor | açık/bekleyen: {acik}/{MAX_OPEN}")
    for symbol in symbols:
        if acik >= MAX_OPEN:
            print("Eşzamanlı pozisyon sınırına ulaşıldı, tarama durduruldu.")
            break
        p = positions.get(symbol)
        if p and p["status"] in LIVE_STATES:
            continue
        try:
            h4 = fetch_klines(symbol, "4h", H4_LIMIT)
            d1 = fetch_klines(symbol, "1d", D1_LIMIT)
            w1 = fetch_klines(symbol, "1w", W1_LIMIT)
        except Exception as e:
            print(f"[{symbol}] veri alınamadı: {e}")
            continue

        sig = smc.find_setup(h4, d1, w1, setup_max_age=SETUP_MAX_AGE_BARS,
                             sl_atr_mult=SL_ATR_MULT, min_rr=MIN_RR,
                             max_rr=MAX_RR, liq_len=LIQ_LEN,
                             discount_max=DISCOUNT_MAX, dir_filter=DIR_FILTER)
        if not sig:
            continue

        # Aynı order block'a tekrar girme
        if p and abs(p.get("entry", 0) - sig["entry"]) < 1e-12:
            continue

        print(f"[{symbol}] KURULUM {sig['tip']} giriş={sig['entry']:.6g} "
              f"stop={sig['sl']:.6g} R:R={sig['rr']:.2f}")
        send_telegram(signal_message(symbol, sig, bal))

        positions[symbol] = {
            "status": "pending", "dir": sig["dir"], "entry": sig["entry"],
            "sl": sig["sl"], "stop": sig["sl"], "trailed": False, "bars": 0,
            "signal_time": h4[-1]["close_time"], "last_bar": h4[-1]["close_time"],
            "rr": sig["rr"], "risk_pct": sig["risk_pct"], "tip": sig["tip"],
        }
        acik += 1

        if api:
            try:
                bt.open_trade_trailing(api, symbol, sig["dir"], sig["entry"],
                                       sig["sl"], bal,
                                       risk_pct_of_balance=RISK_PCT_OF_BALANCE,
                                       max_open=MAX_OPEN)
            except Exception as e:
                print(f"  [{symbol}] emir açılamadı: {e}")

    save_state(state)
    print("Tarama bitti.")


if __name__ == "__main__":
    main()
