"""
ICT Checklist tarayıcısı — mevcut HTF/LTF Order Block stratejisinden (scanner.py)
BAĞIMSIZ, ikinci bir strateji. Aynı sembolleri tarar ama farklı bir metodoloji
kullanır: 5 ICT kriterini otomatik puanlar, skoru >= ICT_MIN_SCORE olan
kurulumlarda Telegram'a ayrı, kendi formatında bir uyarı gönderir.

Kriterler:
1. HTF Bias Uyumu     — LTF'deki son yapı kırılımı, günlük trend yönüyle aynı mı?
2. Kill Zone Zamanı   — kırılım London (07-10 UTC) veya New York (12-15 UTC)
                        kill zone'una denk geldi mi?
3. Liquidity Sweep    — en son Asya seansı (00-07 UTC) tepe/dibi süpürülüp
                        geri dönüldü mü?
4. MSS / Displacement — kırılım mumunun gövdesi, öncekilere göre belirgin
                        büyük mü (gerçek bir "displacement" mi)?
5. FVG / OTE Teması   — fiyat taze bir FVG içinde mi veya son bacağın
                        0.618-0.786 (OTE) retracement bölgesinde mi?

Bu basitleştirilmiş, otomatikleştirilebilir yaklaşımlardır — ICT'nin tam
metodolojisinin birebir yerine geçmez, yatırım tavsiyesi değildir.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

from scanner import (
    get_usdt_symbols,
    fetch_klines,
    compute_structure,
    send_telegram,
    PIVOT_LEN_HTF,
    PIVOT_LEN_LTF,
    CONFIRM_WINDOW,
    HTF_INTERVAL,
    LTF_INTERVAL,
    HTF_LIMIT,
    LTF_LIMIT,
    REQUEST_SLEEP,
)

ICT_STATE_FILE = os.path.join(os.path.dirname(__file__), "ict_state.json")
ICT_MIN_SCORE = 3  # 5 kriterden en az kaçı sağlanırsa uyarı gönderilsin

LONDON_KZ = (7, 10)   # UTC saat aralığı
NY_KZ = (12, 15)      # UTC saat aralığı

DISPLACEMENT_BODY_MULT = 1.5     # kırılım mumunun gövdesi, ortalamanın kaç katı olmalı
LIQUIDITY_SWEEP_LOOKBACK = 6     # kaç 4H mumu geriye bakılsın
OTE_SWING_LOOKBACK = 30          # OTE için kaç 4H mumluk bacağa bakılsın

CRITERIA_LABELS = [
    ("htf_bias", "HTF Bias Uyumu (günlük trendle aynı yön)"),
    ("killzone", "Kill Zone Zamanlaması (London/NY)"),
    ("liquidity_sweep", "Liquidity Sweep (Asya/önceki seans likiditesi)"),
    ("mss_displacement", "MSS / Displacement (güçlü kırılım mumu)"),
    ("fvg_ote", "FVG / OTE Teması (0.618-0.786 veya FVG)"),
]


def find_last_break(ltf_results, confirm_window):
    """Son confirm_window bar içinde en son oluşan yapı kırılımını (yön, index) döndürür."""
    n = len(ltf_results)
    for offset in range(0, confirm_window + 1):
        idx = n - 1 - offset
        if idx < 0:
            break
        if ltf_results[idx]["bull_break"]:
            return "long", idx
        if ltf_results[idx]["bear_break"]:
            return "short", idx
    return None, None


def compute_htf_bias(htf_candles):
    """Günlük grafikteki en son yapı kırılımının yönünü (long/short) döndürür."""
    results = compute_structure(htf_candles, PIVOT_LEN_HTF)
    last_bull_idx = last_bear_idx = None
    for i, r in enumerate(results):
        if r["bull_break"]:
            last_bull_idx = i
        if r["bear_break"]:
            last_bear_idx = i
    if last_bull_idx is None and last_bear_idx is None:
        return None
    if last_bull_idx is None:
        return "short"
    if last_bear_idx is None:
        return "long"
    return "long" if last_bull_idx > last_bear_idx else "short"


def in_killzone(open_time_ms):
    """4H mumun London veya NY kill zone'u ile kesiştiğini kontrol eder."""
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    start_hour = dt.hour
    end_hour = start_hour + 4

    def overlaps(zone):
        z0, z1 = zone
        return not (end_hour <= z0 or start_hour >= z1)

    return overlaps(LONDON_KZ) or overlaps(NY_KZ)


def get_latest_asian_range(ltf_candles):
    """En son (bugünkü veya bulunamazsa dünkü) Asya seansı (00-07 UTC) tepe/dibini döndürür."""
    if not ltf_candles:
        return None, None
    latest_dt = datetime.fromtimestamp(ltf_candles[-1]["close_time"] / 1000, tz=timezone.utc)
    for day_offset in (0, 1):
        target_date = (latest_dt - timedelta(days=day_offset)).date()
        session_candles = [
            c for c in ltf_candles
            if datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc).date() == target_date
            and datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc).hour in (0, 4)
        ]
        if session_candles:
            return max(c["high"] for c in session_candles), min(c["low"] for c in session_candles)
    return None, None


def compute_liquidity_sweep(ltf_candles, direction):
    asian_high, asian_low = get_latest_asian_range(ltf_candles)
    if asian_high is None:
        return False
    recent = ltf_candles[-LIQUIDITY_SWEEP_LOOKBACK:]
    last_close = ltf_candles[-1]["close"]
    if direction == "long":
        swept = any(c["low"] < asian_low for c in recent)
        return swept and last_close > asian_low
    else:
        swept = any(c["high"] > asian_high for c in recent)
        return swept and last_close < asian_high


def compute_mss_displacement(ltf_candles, break_idx):
    body = abs(ltf_candles[break_idx]["close"] - ltf_candles[break_idx]["open"])
    prior = ltf_candles[max(0, break_idx - 20):break_idx]
    bodies = [abs(c["close"] - c["open"]) for c in prior]
    avg_body = sum(bodies) / len(bodies) if bodies else 0
    return avg_body > 0 and body >= DISPLACEMENT_BODY_MULT * avg_body


def compute_fvg_or_ote(ltf_candles, direction):
    if len(ltf_candles) >= 3:
        bull_fvg = ltf_candles[-1]["low"] > ltf_candles[-3]["high"]
        bear_fvg = ltf_candles[-1]["high"] < ltf_candles[-3]["low"]
        if direction == "long" and bull_fvg:
            return True
        if direction == "short" and bear_fvg:
            return True

    lookback = ltf_candles[-OTE_SWING_LOOKBACK:]
    if not lookback:
        return False
    swing_low = min(c["low"] for c in lookback)
    swing_high = max(c["high"] for c in lookback)
    rng = swing_high - swing_low
    if rng <= 0:
        return False
    current = ltf_candles[-1]["close"]
    if direction == "long":
        ote_top = swing_high - 0.618 * rng
        ote_bot = swing_high - 0.786 * rng
        return ote_bot <= current <= ote_top
    else:
        ote_bot = swing_low + 0.618 * rng
        ote_top = swing_low + 0.786 * rng
        return ote_bot <= current <= ote_top


def evaluate_symbol_ict(symbol):
    htf_candles = fetch_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    time.sleep(REQUEST_SLEEP)
    ltf_candles = fetch_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    time.sleep(REQUEST_SLEEP)

    if len(htf_candles) < PIVOT_LEN_HTF * 2 + 5 or len(ltf_candles) < PIVOT_LEN_LTF * 2 + 5:
        return None

    ltf_results = compute_structure(ltf_candles, PIVOT_LEN_LTF)
    direction, break_idx = find_last_break(ltf_results, CONFIRM_WINDOW)
    if direction is None:
        return None  # skorlanacak bir yapı kırılımı yok

    htf_bias = compute_htf_bias(htf_candles)
    break_candle = ltf_candles[break_idx]

    criteria = {
        "htf_bias": htf_bias == direction if htf_bias else False,
        "killzone": in_killzone(break_candle["open_time"]),
        "liquidity_sweep": compute_liquidity_sweep(ltf_candles, direction),
        "mss_displacement": compute_mss_displacement(ltf_candles, break_idx),
        "fvg_ote": compute_fvg_or_ote(ltf_candles, direction),
    }
    score = sum(criteria.values())

    return {
        "direction": direction,
        "score": score,
        "criteria": criteria,
        "price": ltf_candles[-1]["close"],
        "break_close_time": break_candle["close_time"],
    }


def format_ict_message(symbol, result):
    direction = result["direction"]
    emoji = "🟢" if direction == "long" else "🔴"
    lines = [
        f"🧭 <b>ICT Checklist Sinyali</b> — {emoji} {symbol} {direction.upper()} "
        f"(Skor: {result['score']}/5)",
        f"Fiyat: {result['price']:.6g}",
        "",
    ]
    for key, label in CRITERIA_LABELS:
        mark = "✅" if result["criteria"][key] else "❌"
        lines.append(f"{mark} {label}")
    lines.append("")
    lines.append(
        "⚠️ Bu, diğer HTF/LTF Order Block stratejisinden BAĞIMSIZ ayrı bir "
        "yöntemdir (ICT checklist tabanlı). Yatırım tavsiyesi değildir."
    )
    return "\n".join(lines)


def load_state():
    if os.path.exists(ICT_STATE_FILE):
        with open(ICT_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(ICT_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def main():
    state = load_state()
    symbols = get_usdt_symbols()
    print(f"[ICT] {len(symbols)} sembol taranacak.")

    sent = 0
    for i, symbol in enumerate(symbols):
        try:
            result = evaluate_symbol_ict(symbol)
        except Exception as e:
            print(f"[ICT][{symbol}] hata: {e}")
            continue

        if result is None or result["score"] < ICT_MIN_SCORE:
            continue

        sym_state = state.get(symbol, {})
        direction = result["direction"]
        last_alerted = sym_state.get(direction)
        if last_alerted == result["break_close_time"]:
            continue  # bu kırılım için zaten uyarı gönderildi

        print(f"[ICT][{symbol}] {direction} skor={result['score']}/5 sinyali gönderiliyor")
        send_telegram(format_ict_message(symbol, result))
        sym_state[direction] = result["break_close_time"]
        state[symbol] = sym_state
        sent += 1

        if (i + 1) % 50 == 0:
            print(f"[ICT] {i + 1}/{len(symbols)} tarandı...")

    save_state(state)
    print(f"[ICT] Tarama bitti. {sent} yeni sinyal gönderildi.")


if __name__ == "__main__":
    main()
