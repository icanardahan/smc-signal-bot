"""
SMC HTF/LTF onay tarayıcısı — Binance USDT paritelerini tarar, 1D order block/FVG
bölgelerine 4H'de BOS/CHoCH + taze FVG onayı gelirse Telegram'a sinyal gönderir.

TradingView'daki Pine Script indikatörüyle aynı mantığın Python portudur, ancak
tüm USDT pariteleri üzerinde otomatik çalışır ve durumu (hangi sinyal daha önce
gönderildi) state.json dosyasında tutar.
"""

import json
import os
import time
import urllib.request
import urllib.error

# api.binance.com bazı bölgelerden (ör. GitHub Actions ABD sunucuları) 451 ile
# engelleniyor; data-api.binance.vision aynı public market-data uçlarını
# coğrafi kısıtlama olmadan sunuyor.
BINANCE_BASE = "https://data-api.binance.vision"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# ---------------- Ayarlar (Pine script'teki inputlarla aynı) ----------------
PIVOT_LEN_HTF = 3
PIVOT_LEN_LTF = 3
CONFIRM_WINDOW = 15       # 4H bar cinsinden
LIQUIDITY_LOOKBACK = 100  # TP için geriye bakılacak 4H bar sayısı
SL_ATR_MULT = 0.15
HTF_INTERVAL = "1d"
LTF_INTERVAL = "4h"
HTF_LIMIT = 400
LTF_LIMIT = 400
OB_SEARCH_MAX = 50
REQUEST_SLEEP = 0.15  # rate-limit için istekler arası bekleme (saniye)

EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
EXCLUDE_BASE_STABLES = {"USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "GBP", "AEUR", "USTC"}


def http_get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "smc-signal-bot"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 418):
                wait = 5 * (attempt + 1)
                print(f"Rate limited ({e.code}), {wait}s bekleniyor...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2)
    raise RuntimeError(f"İstek başarısız: {url}")


def get_usdt_symbols():
    info = http_get_json(f"{BINANCE_BASE}/api/v3/exchangeInfo")
    symbols = []
    for s in info["symbols"]:
        if s["quoteAsset"] != "USDT":
            continue
        if s["status"] != "TRADING":
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        sym = s["symbol"]
        if sym.endswith(EXCLUDE_SUFFIXES):
            continue
        if s["baseAsset"] in EXCLUDE_BASE_STABLES:
            continue
        symbols.append(sym)
    return sorted(symbols)


def fetch_klines(symbol, interval, limit):
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    raw = http_get_json(url)
    candles = []
    now_ms = int(time.time() * 1000)
    for row in raw:
        open_time, o, h, l, c, v, close_time = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        if close_time > now_ms:
            continue  # henüz kapanmamış mum
        candles.append({
            "open_time": open_time,
            "close_time": close_time,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
        })
    return candles


def true_range(prev_close, high, low):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr(candles, length=14):
    if len(candles) < length + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        trs.append(true_range(candles[i - 1]["close"], candles[i]["high"], candles[i]["low"]))
    window = trs[-length:]
    return sum(window) / len(window)


def compute_structure(candles, length):
    """Pine'daki f_structure() fonksiyonunun causal (lookahead içermeyen) Python portu."""
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    closes = [c["close"] for c in candles]

    raw_ph = [None] * n
    raw_pl = [None] * n
    for i in range(length, n - length):
        window_h = highs[i - length:i + length + 1]
        if highs[i] == max(window_h) and window_h.count(max(window_h)) == 1:
            raw_ph[i] = highs[i]
        window_l = lows[i - length:i + length + 1]
        if lows[i] == min(window_l) and window_l.count(min(window_l)) == 1:
            raw_pl[i] = lows[i]

    last_ph = None
    last_pl = None
    ob_top = None
    ob_bot = None
    ob_dir = 0
    results = []

    for i in range(n):
        reveal_idx = i - length
        if 0 <= reveal_idx < n and raw_ph[reveal_idx] is not None:
            last_ph = raw_ph[reveal_idx]
        if 0 <= reveal_idx < n and raw_pl[reveal_idx] is not None:
            last_pl = raw_pl[reveal_idx]

        prev_close = closes[i - 1] if i > 0 else None
        bull_break = last_ph is not None and closes[i] > last_ph and (prev_close is None or prev_close <= last_ph)
        bear_break = last_pl is not None and closes[i] < last_pl and (prev_close is None or prev_close >= last_pl)

        if bull_break:
            for j in range(i - 1, max(i - OB_SEARCH_MAX, -1), -1):
                if closes[j] < opens[j]:
                    ob_top, ob_bot, ob_dir = highs[j], lows[j], 1
                    break
            last_ph = None

        if bear_break:
            for j in range(i - 1, max(i - OB_SEARCH_MAX, -1), -1):
                if closes[j] > opens[j]:
                    ob_top, ob_bot, ob_dir = highs[j], lows[j], -1
                    break
            last_pl = None

        results.append({
            "ob_top": ob_top,
            "ob_bot": ob_bot,
            "ob_dir": ob_dir,
            "bull_break": bull_break,
            "bear_break": bear_break,
            "close_time": candles[i]["close_time"],
        })

    return results


def find_recent_bos(ltf_results, direction, confirm_window):
    """Son bardan geriye doğru confirm_window içinde en yakın BOS/CHoCH barını bulur."""
    n = len(ltf_results)
    key = "bull_break" if direction == "long" else "bear_break"
    for offset in range(0, confirm_window + 1):
        idx = n - 1 - offset
        if idx < 0:
            break
        if ltf_results[idx][key]:
            return idx
    return None


def evaluate_symbol(symbol):
    htf_candles = fetch_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    time.sleep(REQUEST_SLEEP)
    ltf_candles = fetch_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    time.sleep(REQUEST_SLEEP)

    if len(htf_candles) < PIVOT_LEN_HTF * 2 + 5 or len(ltf_candles) < PIVOT_LEN_LTF * 2 + 5:
        return None

    htf_results = compute_structure(htf_candles, PIVOT_LEN_HTF)
    ltf_results = compute_structure(ltf_candles, PIVOT_LEN_LTF)

    htf_last = htf_results[-1]
    ltf_last = ltf_results[-1]
    close = ltf_candles[-1]["close"]
    low = ltf_candles[-1]["low"]
    high = ltf_candles[-1]["high"]
    atr = compute_atr(ltf_candles, 14)

    zone_top, zone_bot, zone_dir = htf_last["ob_top"], htf_last["ob_bot"], htf_last["ob_dir"]
    price_in_bull_zone = zone_dir == 1 and zone_bot is not None and zone_bot <= close <= zone_top
    price_in_bear_zone = zone_dir == -1 and zone_bot is not None and zone_bot <= close <= zone_top

    bull_fvg = ltf_candles[-1]["low"] > ltf_candles[-3]["high"] if len(ltf_candles) >= 3 else False
    bear_fvg = ltf_candles[-1]["high"] < ltf_candles[-3]["low"] if len(ltf_candles) >= 3 else False

    long_bos_idx = find_recent_bos(ltf_results, "long", CONFIRM_WINDOW)
    short_bos_idx = find_recent_bos(ltf_results, "short", CONFIRM_WINDOW)

    long_signal = None
    short_signal = None

    if price_in_bull_zone and long_bos_idx is not None and bull_fvg:
        ltf_ob = ltf_results[long_bos_idx]
        sl = (ltf_ob["ob_bot"] if ltf_ob["ob_dir"] == 1 and ltf_ob["ob_bot"] is not None else low) - atr * SL_ATR_MULT
        recent_highs = [c["high"] for c in ltf_candles[-(LIQUIDITY_LOOKBACK + 1):-1]]
        tp = max(recent_highs) if recent_highs else close
        rr = (tp - close) / (close - sl) if close > sl else None
        long_signal = {
            "direction": "LONG",
            "bos_close_time": ltf_ob["close_time"],
            "entry": close,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "zone_top": zone_top,
            "zone_bot": zone_bot,
        }

    if price_in_bear_zone and short_bos_idx is not None and bear_fvg:
        ltf_ob = ltf_results[short_bos_idx]
        sl = (ltf_ob["ob_top"] if ltf_ob["ob_dir"] == -1 and ltf_ob["ob_top"] is not None else high) + atr * SL_ATR_MULT
        recent_lows = [c["low"] for c in ltf_candles[-(LIQUIDITY_LOOKBACK + 1):-1]]
        tp = min(recent_lows) if recent_lows else close
        rr = (close - tp) / (sl - close) if sl > close else None
        short_signal = {
            "direction": "SHORT",
            "bos_close_time": ltf_ob["close_time"],
            "entry": close,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "zone_top": zone_top,
            "zone_bot": zone_bot,
        }

    return {"long": long_signal, "short": short_signal}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil, mesaj gönderilemedi:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"Telegram gönderim hatası: {e}")


def format_message(symbol, sig):
    direction = sig["direction"]
    emoji = "🟢" if direction == "LONG" else "🔴"
    rr_text = f"{sig['rr']:.2f}" if sig["rr"] else "n/a"
    return (
        f"{emoji} <b>{direction}</b> — {symbol}\n"
        f"HTF Bölge (1D): {sig['zone_bot']:.6g} - {sig['zone_top']:.6g}\n"
        f"Giriş: {sig['entry']:.6g}\n"
        f"SL: {sig['sl']:.6g}\n"
        f"TP: {sig['tp']:.6g}\n"
        f"R:R ≈ {rr_text}\n"
        f"Zaman dilimi: 1D → 4H onay"
    )


def main():
    state = load_state()
    symbols = get_usdt_symbols()
    print(f"{len(symbols)} sembol taranacak.")

    sent = 0
    for i, symbol in enumerate(symbols):
        try:
            result = evaluate_symbol(symbol)
        except Exception as e:
            print(f"[{symbol}] hata: {e}")
            continue

        if result is None:
            continue

        sym_state = state.get(symbol, {})

        for direction_key in ("long", "short"):
            sig = result[direction_key]
            if not sig:
                continue
            last_alerted = sym_state.get(direction_key)
            if last_alerted == sig["bos_close_time"]:
                continue  # bu BOS için zaten alarm gönderildi
            send_telegram(format_message(symbol, sig))
            sym_state[direction_key] = sig["bos_close_time"]
            sent += 1

        if sym_state:
            state[symbol] = sym_state

        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(symbols)} tarandı...")

    save_state(state)
    print(f"Tarama bitti. {sent} yeni sinyal gönderildi.")


if __name__ == "__main__":
    main()
