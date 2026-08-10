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
from datetime import datetime, timezone

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
LEVERAGE_CAP = 20            # ne kadar dar SL olursa olsun bu değeri aşma
LEVERAGE_STEPS = [1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100]
MAX_POSITION_PCT = 0.20        # marjin, cüzdanın bu oranını tek işlemde aşmasın

# Kaldıraç ve pozisyon büyüklüğü artık sabit değil; işlemin TP1 R:R'ına göre
# (setup kalitesine göre) bu aralıklar içinde dinamik olarak ölçeklenir.
RR_SCALE_MIN = 1.0   # bu R:R veya altı -> minimum risk bütçesi kullanılır
RR_SCALE_MAX = 4.0   # bu R:R veya üstü -> maksimum risk bütçesi kullanılır
MARGIN_RISK_MIN = 0.15   # zayıf R:R'da: SL'e değince marjinin ~%15'i kaybedilsin
MARGIN_RISK_MAX = 0.35   # güçlü R:R'da: SL'e değince marjinin ~%35'i kaybedilsin
ACCOUNT_RISK_MIN = 0.005  # zayıf R:R'da: cüzdanın ~%0.5'i risk edilsin
ACCOUNT_RISK_MAX = 0.02   # güçlü R:R'da: cüzdanın ~%2'si risk edilsin


def risk_scale_factor(rr):
    """TP1 R:R'ını 0-1 aralığına ölçekler (RR_SCALE_MIN..RR_SCALE_MAX arasında).
    R:R bilinmiyorsa (TP bulunamadıysa) en muhafazakar (0) değeri döner."""
    if rr is None:
        return 0.0
    rr = max(RR_SCALE_MIN, min(RR_SCALE_MAX, rr))
    return (rr - RR_SCALE_MIN) / (RR_SCALE_MAX - RR_SCALE_MIN)


def dynamic_margin_risk(rr1):
    t = risk_scale_factor(rr1)
    return MARGIN_RISK_MIN + t * (MARGIN_RISK_MAX - MARGIN_RISK_MIN)


def dynamic_account_risk(rr1):
    t = risk_scale_factor(rr1)
    return ACCOUNT_RISK_MIN + t * (ACCOUNT_RISK_MAX - ACCOUNT_RISK_MIN)
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
        if not sym.isascii():
            continue  # bazı egzotik/meme sembolleri URL kodlamasında sorun çıkarıyor
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


def collect_pivot_levels(candles, length, kind):
    """Geçmiş pivot tepe/dip fiyatlarını (likidite seviyeleri) döndürür."""
    n = len(candles)
    values = [c["high"] for c in candles] if kind == "high" else [c["low"] for c in candles]
    levels = []
    for i in range(length, n - length):
        window = values[i - length:i + length + 1]
        target = max(window) if kind == "high" else min(window)
        if values[i] == target and window.count(target) == 1:
            levels.append(values[i])
    return levels


def pick_tp_levels(entry, ltf_levels, htf_levels, direction, count=3, min_gap_pct=0.15):
    """Entry'nin ötesindeki likidite seviyelerinden en yakın `count` tanesini seçer."""
    candidates = sorted(set(ltf_levels) | set(htf_levels))
    if direction == "long":
        candidates = [lv for lv in candidates if lv > entry]
    else:
        candidates = sorted([lv for lv in candidates if lv < entry], reverse=True)

    picked = []
    for lv in candidates:
        if all(abs(lv - p) / p > min_gap_pct / 100 for p in picked):
            picked.append(lv)
        if len(picked) == count:
            break
    while len(picked) < count:
        picked.append(None)
    return picked


def suggest_leverage(entry, sl, margin_risk_fraction):
    """SL mesafesine ve işlemin risk kalitesine (margin_risk_fraction, TP1 R:R'ından
    türetilir) göre kaldıraç önerir: SL'e değince marjinin ~margin_risk_fraction'ı
    kaybedilecek şekilde, likidasyona tampon payı bırakan mekanik bir hesaplama.
    Kişiselleştirilmiş yatırım tavsiyesi değildir."""
    sl_pct = abs(entry - sl) / entry
    if sl_pct <= 0:
        return None
    raw = margin_risk_fraction / sl_pct
    capped = min(LEVERAGE_CAP, raw)
    eligible = [s for s in LEVERAGE_STEPS if s <= capped]
    return max(eligible) if eligible else 1


def suggest_position_pct(entry, sl, leverage, account_risk_pct):
    """SL'e değince cüzdanın ~account_risk_pct'i (TP1 R:R'ından türetilir)
    kaybedilecek şekilde, kullanılacak marjinin cüzdana oranını (%) hesaplar.
    Kişiselleştirilmiş yatırım tavsiyesi değildir, işlemin risk/getiri kalitesine
    göre ölçeklenen mekanik bir hesaplamadır."""
    sl_pct = abs(entry - sl) / entry
    if sl_pct <= 0 or not leverage:
        return None
    notional_pct = account_risk_pct / sl_pct
    margin_pct = notional_pct / leverage
    return min(margin_pct, MAX_POSITION_PCT)


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

    ltf_highs = collect_pivot_levels(ltf_candles[-(LIQUIDITY_LOOKBACK + PIVOT_LEN_LTF):], PIVOT_LEN_LTF, "high")
    ltf_lows = collect_pivot_levels(ltf_candles[-(LIQUIDITY_LOOKBACK + PIVOT_LEN_LTF):], PIVOT_LEN_LTF, "low")
    htf_highs = collect_pivot_levels(htf_candles, PIVOT_LEN_HTF, "high")
    htf_lows = collect_pivot_levels(htf_candles, PIVOT_LEN_HTF, "low")

    if price_in_bull_zone and long_bos_idx is not None and bull_fvg:
        ltf_ob = ltf_results[long_bos_idx]
        sl = (ltf_ob["ob_bot"] if ltf_ob["ob_dir"] == 1 and ltf_ob["ob_bot"] is not None else low) - atr * SL_ATR_MULT
        tp1, tp2, tp3 = pick_tp_levels(close, ltf_highs, htf_highs, "long")
        rrs = [(tp - close) / (close - sl) if (tp is not None and close > sl) else None for tp in (tp1, tp2, tp3)]
        margin_risk = dynamic_margin_risk(rrs[0])
        account_risk = dynamic_account_risk(rrs[0])
        leverage = suggest_leverage(close, sl, margin_risk)
        long_signal = {
            "direction": "LONG",
            "bos_close_time": ltf_ob["close_time"],
            "entry": close,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "rr1": rrs[0], "rr2": rrs[1], "rr3": rrs[2],
            "leverage": leverage,
            "position_pct": suggest_position_pct(close, sl, leverage, account_risk),
            "margin_risk": margin_risk,
            "account_risk": account_risk,
            "zone_top": zone_top,
            "zone_bot": zone_bot,
        }

    if price_in_bear_zone and short_bos_idx is not None and bear_fvg:
        ltf_ob = ltf_results[short_bos_idx]
        sl = (ltf_ob["ob_top"] if ltf_ob["ob_dir"] == -1 and ltf_ob["ob_top"] is not None else high) + atr * SL_ATR_MULT
        tp1, tp2, tp3 = pick_tp_levels(close, ltf_lows, htf_lows, "short")
        rrs = [(close - tp) / (sl - close) if (tp is not None and sl > close) else None for tp in (tp1, tp2, tp3)]
        margin_risk = dynamic_margin_risk(rrs[0])
        account_risk = dynamic_account_risk(rrs[0])
        leverage = suggest_leverage(close, sl, margin_risk)
        short_signal = {
            "direction": "SHORT",
            "bos_close_time": ltf_ob["close_time"],
            "entry": close,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "rr1": rrs[0], "rr2": rrs[1], "rr3": rrs[2],
            "leverage": leverage,
            "position_pct": suggest_position_pct(close, sl, leverage, account_risk),
            "margin_risk": margin_risk,
            "account_risk": account_risk,
            "zone_top": zone_top,
            "zone_bot": zone_bot,
        }

    return {"long": long_signal, "short": short_signal, "close": close, "ltf_candles": ltf_candles}


def monitor_position(pos, ltf_candles, direction):
    """Açık bir pozisyonu son taramadan bu yana gelen mumlarla günceller.
    SL/TP1/TP2/TP3 seviyelerine değinip değinmediğini sırayla kontrol eder.
    (updated_pos, events) döner; events örn. ["tp1_hit", "sl_hit"]."""
    if not pos.get("entry") or pos.get("sl") is None:
        return pos, []  # eski format / eksik veri, izlenemez
    if pos.get("status") in ("sl_hit", "tp3_hit"):
        return pos, []  # kapanmış, izlemeye gerek yok

    last_checked = pos.get("last_checked_close_time", pos.get("bos_close_time", 0))
    new_candles = [c for c in ltf_candles if c["close_time"] > last_checked]
    if not new_candles:
        return pos, []

    is_long = direction == "long"
    sl, tp1, tp2, tp3 = pos["sl"], pos.get("tp1"), pos.get("tp2"), pos.get("tp3")
    status = pos.get("status", "open")
    events = []

    for c in new_candles:
        hit_sl = (c["low"] <= sl) if is_long else (c["high"] >= sl)
        if hit_sl:
            status = "sl_hit"
            events.append("sl_hit")
            break
        if status == "open" and tp1 is not None:
            hit = (c["high"] >= tp1) if is_long else (c["low"] <= tp1)
            if hit:
                status = "tp1_hit"
                events.append("tp1_hit")
        if status == "tp1_hit" and tp2 is not None:
            hit = (c["high"] >= tp2) if is_long else (c["low"] <= tp2)
            if hit:
                status = "tp2_hit"
                events.append("tp2_hit")
        if status == "tp2_hit" and tp3 is not None:
            hit = (c["high"] >= tp3) if is_long else (c["low"] <= tp3)
            if hit:
                status = "tp3_hit"
                events.append("tp3_hit")

    pos["status"] = status
    pos["last_checked_close_time"] = new_candles[-1]["close_time"]
    return pos, events


def pct_move(entry, price, is_long):
    return (price - entry) / entry * 100 if is_long else (entry - price) / entry * 100


EVENT_LABELS = {
    "sl_hit": ("🛑", "SL VURULDU"),
    "tp1_hit": ("🎯", "TP1 VURULDU"),
    "tp2_hit": ("🎯🎯", "TP2 VURULDU"),
    "tp3_hit": ("🏁", "TP3 VURULDU (pozisyon tamamen kapandı)"),
}

EVENT_PRICE_KEY = {"sl_hit": "sl", "tp1_hit": "tp1", "tp2_hit": "tp2", "tp3_hit": "tp3"}


def format_event_message(symbol, direction, pos, event):
    emoji, label = EVENT_LABELS[event]
    is_long = direction == "long"
    price = pos[EVENT_PRICE_KEY[event]]
    entry = pos["entry"]
    lev = pos.get("leverage") or 1
    price_pct = pct_move(entry, price, is_long)
    margin_pct = price_pct * lev
    return (
        f"{emoji} <b>{label}</b> — {symbol} {direction.upper()}\n"
        f"Giriş: {entry:.6g}  Seviye: {price:.6g}\n"
        f"Fiyat P&L: {price_pct:+.2f}%  Marjin P&L (~{lev}x): {margin_pct:+.2f}%"
    )


def _fmt_level(v):
    return f"{v:.6g}" if v is not None else "n/a"


def format_open_positions_digest(open_positions):
    """open_positions: [(symbol, direction, pos, current_price), ...]"""
    status_labels = {"open": "açık", "tp1_hit": "TP1 vuruldu, devam ediyor", "tp2_hit": "TP2 vuruldu, devam ediyor"}
    blocks = []
    for symbol, direction, pos, current_price in open_positions:
        is_long = direction == "long"
        entry = pos["entry"]
        lev = pos.get("leverage") or 1
        price_pct = pct_move(entry, current_price, is_long)
        margin_pct = price_pct * lev
        emoji = "🟢" if is_long else "🔴"
        status_text = status_labels.get(pos.get("status", "open"), pos.get("status"))
        blocks.append(
            f"{emoji} <b>{symbol} {direction.upper()}</b> — {status_text}\n"
            f"Giriş: {entry:.6g}  Güncel: {current_price:.6g}\n"
            f"Fiyat P&L: {price_pct:+.2f}%  Marjin P&L (~{lev}x): {margin_pct:+.2f}%\n"
            f"SL: {_fmt_level(pos.get('sl'))}  TP1: {_fmt_level(pos.get('tp1'))}  "
            f"TP2: {_fmt_level(pos.get('tp2'))}  TP3: {_fmt_level(pos.get('tp3'))}"
        )

    # Telegram mesaj limiti (4096 karakter) aşılmasın diye parçalara böl
    messages = []
    current = "📊 <b>Açık pozisyonlar</b>\n\n"
    for block in blocks:
        if len(current) + len(block) + 2 > 3500:
            messages.append(current.rstrip())
            current = ""
        current += block + "\n\n"
    if current.strip():
        messages.append(current.rstrip())
    return messages


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


def _fmt_tp(tp, rr):
    if tp is None:
        return "n/a (yeterli likidite seviyesi bulunamadı)"
    rr_text = f"{rr:.2f}" if rr else "n/a"
    return f"{tp:.6g}  (R:R ≈ {rr_text})"


def format_message(symbol, sig):
    direction = sig["direction"]
    emoji = "🟢" if direction == "LONG" else "🔴"

    bos_dt = datetime.fromtimestamp(sig["bos_close_time"] / 1000, tz=timezone.utc)
    now_dt = datetime.now(timezone.utc)
    age_hours = (now_dt - bos_dt).total_seconds() / 3600
    bos_time_text = bos_dt.strftime("%Y-%m-%d %H:%M UTC")

    if age_hours < 0.5:
        freshness = "az önce oluştu"
    else:
        freshness = f"~{age_hours:.1f} saat önce oluştu"

    lev = sig.get("leverage")
    lev_text = f"~{lev}x" if lev else "n/a"
    pos_pct = sig.get("position_pct")
    pos_text = f"~%{pos_pct*100:.1f}" if pos_pct else "n/a"
    margin_risk = sig.get("margin_risk")
    account_risk = sig.get("account_risk")
    quality_text = f"TP1 R:R ≈ {sig['rr1']:.2f} baz alındı" if sig.get("rr1") else "TP1 bulunamadı, en muhafazakar seviye kullanıldı"

    return (
        f"{emoji} <b>{direction}</b> — {symbol}\n"
        f"HTF Bölge (1D): {sig['zone_bot']:.6g} - {sig['zone_top']:.6g}\n"
        f"Giriş: {sig['entry']:.6g}\n"
        f"SL: {sig['sl']:.6g}\n"
        f"TP1: {_fmt_tp(sig['tp1'], sig['rr1'])}\n"
        f"TP2: {_fmt_tp(sig['tp2'], sig['rr2'])}\n"
        f"TP3: {_fmt_tp(sig['tp3'], sig['rr3'])}\n"
        f"Önerilen kaldıraç: {lev_text} (SL'e değince marjinin ~%{margin_risk*100:.0f}'i risk edilecek şekilde, işlem kalitesine göre ölçeklendi)\n"
        f"Önerilen pozisyon büyüklüğü: cüzdanının {pos_text}'i (SL'e değince cüzdanın ~%{account_risk*100:.2f}'i risk edilecek şekilde, işlem kalitesine göre ölçeklendi)\n"
        f"Risk ölçeklendirme temeli: {quality_text}\n"
        f"⚠️ Bunlar yatırım tavsiyesi değildir, işlemin R:R kalitesine göre ölçeklenen mekanik bir hesaplamadır — kendi risk toleransına göre ayarla\n"
        f"Onay mumu (4H kapanış): {bos_time_text} ({freshness})\n"
        f"Zaman dilimi: 1D → 4H onay"
    )


def main():
    state = load_state()
    symbols = get_usdt_symbols()
    print(f"{len(symbols)} sembol taranacak.")

    sent = 0
    events_sent = 0
    open_positions = []

    for i, symbol in enumerate(symbols):
        try:
            result = evaluate_symbol(symbol)
        except Exception as e:
            print(f"[{symbol}] hata: {e}")
            continue

        if result is None:
            continue

        sym_state = state.get(symbol, {})
        ltf_candles = result["ltf_candles"]
        current_price = result["close"]

        for direction_key in ("long", "short"):
            existing_pos = sym_state.get(direction_key)

            # 1) Mevcut açık pozisyon varsa SL/TP1/TP2/TP3'e değindi mi kontrol et
            if isinstance(existing_pos, dict) and existing_pos.get("entry"):
                updated_pos, events = monitor_position(existing_pos, ltf_candles, direction_key)
                sym_state[direction_key] = updated_pos
                for ev in events:
                    print(f"[{symbol}] {direction_key} {ev}")
                    send_telegram(format_event_message(symbol, direction_key, updated_pos, ev))
                    events_sent += 1
                if updated_pos.get("status") in ("open", "tp1_hit", "tp2_hit"):
                    open_positions.append((symbol, direction_key, updated_pos, current_price))

            # 2) Yeni bir sinyal var mı (yeni/farklı bir BOS)?
            sig = result[direction_key]
            if not sig:
                continue
            existing_pos = sym_state.get(direction_key)
            existing_bos = existing_pos.get("bos_close_time") if isinstance(existing_pos, dict) else existing_pos
            if existing_bos == sig["bos_close_time"]:
                continue  # bu BOS için zaten alarm gönderildi / izleniyor

            print(f"[{symbol}] {sig['direction']} sinyali gönderiliyor (BOS: {sig['bos_close_time']})")
            send_telegram(format_message(symbol, sig))
            new_pos = {
                "bos_close_time": sig["bos_close_time"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp1": sig["tp1"], "tp2": sig["tp2"], "tp3": sig["tp3"],
                "leverage": sig["leverage"],
                "status": "open",
                "last_checked_close_time": sig["bos_close_time"],
            }
            sym_state[direction_key] = new_pos
            open_positions.append((symbol, direction_key, new_pos, current_price))
            sent += 1

        if sym_state:
            state[symbol] = sym_state

        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(symbols)} tarandı...")

    save_state(state)

    for digest_msg in format_open_positions_digest(open_positions):
        send_telegram(digest_msg)

    print(f"Tarama bitti. {sent} yeni sinyal, {events_sent} pozisyon olayı gönderildi. "
          f"{len(open_positions)} pozisyon açık.")


if __name__ == "__main__":
    main()
