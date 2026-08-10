"""
ICT Checklist tarayıcısı — mevcut HTF/LTF Order Block stratejisinden (scanner.py)
BAĞIMSIZ, ikinci bir strateji. Aynı sembolleri tarar ama farklı bir metodoloji
kullanır: "Altın Kural" — 3 çekirdek kriterin HEPSİ + 2 onay kriterinden EN AZ
1'i sağlanırsa (toplam skor >= 4/5) Telegram'a ayrı, kendi formatında bir
uyarı gönderir.

Çekirdek (hepsi gerekli):
1. Kill Zone Zamanı   — kırılım London (07-10 UTC) veya New York (12-15 UTC)
                        kill zone'una denk geldi mi?
2. Liquidity Sweep    — en son Asya seansı (00-07 UTC) tepe/dibi süpürülüp
                        geri dönüldü mü?
3. MSS / Displacement — kırılım mumunun gövdesi, öncekilere göre belirgin
                        büyük mü (gerçek bir "displacement" mi)?

Onaylar (en az 1 gerekli):
4. HTF Bias Uyumu     — LTF'deki son yapı kırılımı, günlük trend yönüyle aynı mı?
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
    compute_atr,
    collect_pivot_levels,
    pick_tp_levels,
    is_valid_setup,
    send_telegram,
    PIVOT_LEN_HTF,
    PIVOT_LEN_LTF,
    CONFIRM_WINDOW,
    HTF_INTERVAL,
    LTF_INTERVAL,
    HTF_LIMIT,
    LTF_LIMIT,
    LIQUIDITY_LOOKBACK,
    SL_ATR_MULT,
    REQUEST_SLEEP,
)

ICT_STATE_FILE = os.path.join(os.path.dirname(__file__), "ict_state.json")

# Altın Kural: 3 çekirdek kriterin HEPSİ sağlanmalı, buna ek olarak 2 onay
# kriterinden EN AZ 1'i sağlanmalı (toplam skor en az 4/5).
CORE_CRITERIA = ["killzone", "liquidity_sweep", "mss_displacement"]
CONFIRM_CRITERIA = ["htf_bias", "fvg_ote"]
MIN_CONFIRMATIONS = 1

LONDON_KZ = (7, 10)   # UTC saat aralığı
NY_KZ = (12, 15)      # UTC saat aralığı

DISPLACEMENT_BODY_MULT = 1.5     # kırılım mumunun gövdesi, ortalamanın kaç katı olmalı
LIQUIDITY_SWEEP_LOOKBACK = 6     # kaç 4H mumu geriye bakılsın
OTE_SWING_LOOKBACK = 30          # OTE için kaç 4H mumluk bacağa bakılsın

CRITERIA_LABELS = {
    "htf_bias": "HTF Bias Uyumu (günlük trendle aynı yön)",
    "killzone": "Kill Zone Zamanlaması (London/NY)",
    "liquidity_sweep": "Liquidity Sweep (Asya/önceki seans likiditesi)",
    "mss_displacement": "MSS / Displacement (güçlü kırılım mumu)",
    "fvg_ote": "FVG / OTE Teması (0.618-0.786 veya FVG)",
}


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
    break_result = ltf_results[break_idx]

    criteria = {
        "htf_bias": htf_bias == direction if htf_bias else False,
        "killzone": in_killzone(break_candle["open_time"]),
        "liquidity_sweep": compute_liquidity_sweep(ltf_candles, direction),
        "mss_displacement": compute_mss_displacement(ltf_candles, break_idx),
        "fvg_ote": compute_fvg_or_ote(ltf_candles, direction),
    }
    score = sum(criteria.values())
    core_ok = all(criteria[k] for k in CORE_CRITERIA)
    confirm_count = sum(criteria[k] for k in CONFIRM_CRITERIA)
    checklist_ok = core_ok and confirm_count >= MIN_CONFIRMATIONS

    # SL/TP hesaplaması: scanner.py'daki ana stratejiyle aynı yöntem —
    # kırılımın oluşturduğu order block'un ötesine SL, geçmiş likidite
    # (pivot tepe/dip) seviyelerine TP1/TP2/TP3.
    close = ltf_candles[-1]["close"]
    atr = compute_atr(ltf_candles, 14)
    ltf_highs = collect_pivot_levels(ltf_candles[-(LIQUIDITY_LOOKBACK + PIVOT_LEN_LTF):], PIVOT_LEN_LTF, "high")
    ltf_lows = collect_pivot_levels(ltf_candles[-(LIQUIDITY_LOOKBACK + PIVOT_LEN_LTF):], PIVOT_LEN_LTF, "low")
    htf_highs = collect_pivot_levels(htf_candles, PIVOT_LEN_HTF, "high")
    htf_lows = collect_pivot_levels(htf_candles, PIVOT_LEN_HTF, "low")

    if direction == "long":
        sl = (break_result["ob_bot"] if break_result["ob_dir"] == 1 and break_result["ob_bot"] is not None
              else break_candle["low"]) - atr * SL_ATR_MULT
        tp1, tp2, tp3 = pick_tp_levels(close, ltf_highs, htf_highs, "long")
        rrs = [(tp - close) / (close - sl) if (tp is not None and close > sl) else None for tp in (tp1, tp2, tp3)]
    else:
        sl = (break_result["ob_top"] if break_result["ob_dir"] == -1 and break_result["ob_top"] is not None
              else break_candle["high"]) + atr * SL_ATR_MULT
        tp1, tp2, tp3 = pick_tp_levels(close, ltf_lows, htf_lows, "short")
        rrs = [(close - tp) / (sl - close) if (tp is not None and sl > close) else None for tp in (tp1, tp2, tp3)]

    # Checklist geçse bile geometri tutarsızsa (SL girişin ters tarafında,
    # ya da geçerli TP yoksa) sinyal gönderilmez.
    qualifies = checklist_ok and is_valid_setup(close, sl, tp1, direction)

    return {
        "direction": direction,
        "score": score,
        "criteria": criteria,
        "qualifies": qualifies,
        "price": close,
        "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": rrs[0], "rr2": rrs[1], "rr3": rrs[2],
        "break_close_time": break_candle["close_time"],
    }


def _fmt_tp(tp, rr):
    if tp is None:
        return "n/a (yeterli likidite seviyesi bulunamadı)"
    rr_text = f"{rr:.2f}" if rr else "n/a"
    return f"{tp:.6g}  (R:R ≈ {rr_text})"


def format_ict_message(symbol, result):
    direction = result["direction"]
    emoji = "🟢" if direction == "long" else "🔴"
    lines = [
        f"🧭 <b>ICT Checklist Sinyali</b> — {emoji} {symbol} {direction.upper()} "
        f"(Skor: {result['score']}/5)",
        f"Giriş: {result['price']:.6g}",
        f"SL: {result['sl']:.6g}",
        f"TP1: {_fmt_tp(result['tp1'], result['rr1'])}",
        f"TP2: {_fmt_tp(result['tp2'], result['rr2'])}",
        f"TP3: {_fmt_tp(result['tp3'], result['rr3'])}",
        "",
        "<b>Çekirdek (hepsi sağlanmalı):</b>",
    ]
    for key in CORE_CRITERIA:
        mark = "✅" if result["criteria"][key] else "❌"
        lines.append(f"{mark} {CRITERIA_LABELS[key]}")
    lines.append("")
    lines.append("<b>Onaylar (en az 1 sağlanmalı):</b>")
    for key in CONFIRM_CRITERIA:
        mark = "✅" if result["criteria"][key] else "❌"
        lines.append(f"{mark} {CRITERIA_LABELS[key]}")
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

        if result is None or not result["qualifies"]:
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
