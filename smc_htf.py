"""
SMC (Smart Money Concepts) — Haftalık / Günlük / 4 Saatlik.

Kullanıcının TradingView'de takip ettiği LuxAlgo SMC göstergesindeki
KAVRAMLARIN Python uygulaması (Pine kodu kopyalanmadı; o script
CC BY-NC-SA lisanslıdır). Uygulanan kavramlar standart SMC bileşenleridir:

  - İki ölçekli yapı: swing (geniş) ve internal (dar) pivotlar
  - BOS / CHoCH ayrımı: kırılım trendle aynı yöndeyse BOS, tersse CHoCH
  - Order block: pivot ile kırılım arasındaki EN UÇ mum (son zıt mum değil)
  - Volatil mum filtresi: aralığı 2×ATR'yi aşan mumda high/low takas edilir,
    böylece devasa mumlar order block olarak seçilmez
  - Fair Value Gap: 3 mumluk boşluk + anlamlılık eşiği
  - Premium / Discount: trailing swing aralığına göre bölge

Neden HTF: 5 dakikalık uygulamada avantaj komisyonun altında kalıyordu
(stop medyanı %0.5, gidiş-dönüş komisyon %0.04-0.07). 4H yapıda stoplar
kat kat geniş olduğu için komisyon duyarlılığı yapısal olarak düşer.

Akış:
  Haftalık  -> ana bias (swing yapı yönü)
  Günlük    -> ara bias + premium/discount bölgesi
  4 Saatlik -> giriş yapısı (CHoCH/BOS, order block, FVG)
"""

SWING_LEN = 50          # swing pivot uzunluğu (LuxAlgo varsayılanı)
INTERNAL_LEN = 5        # internal pivot uzunluğu
ATR_LEN = 200           # volatilite ölçüsü
HIGH_VOL_MULT = 2.0     # bu katın üstündeki mumlarda high/low takas edilir
OB_SEARCH_MAX = 60      # order block ararken geriye bakılacak azami bar
FVG_THRESHOLD_MULT = 2.0  # FVG anlamlılık eşiği (ortalama gövde yüzdesinin katı)

BULLISH, BEARISH = 1, -1


# ---------------- Temel ölçüler ----------------
def atr(candles, n=ATR_LEN):
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        pc = candles[i - 1]["close"]
        trs.append(max(candles[i]["high"] - candles[i]["low"],
                       abs(candles[i]["high"] - pc), abs(candles[i]["low"] - pc)))
    w = trs[-n:] if len(trs) >= n else trs
    return sum(w) / len(w) if w else 0.0


def parsed_levels(candles):
    """Volatil mumlarda high/low takas edilir.

    Aralığı 2×ATR'yi aşan mumlar (haber mumları, fitiller) order block olarak
    seçilirse stop devasa olur. Takas, bu mumların 'en uç' seçilmesini
    engeller."""
    a = atr(candles)
    ph, pl = [], []
    for c in candles:
        vol = (c["high"] - c["low"]) >= HIGH_VOL_MULT * a if a else False
        ph.append(c["low"] if vol else c["high"])
        pl.append(c["high"] if vol else c["low"])
    return ph, pl


# ---------------- Pivot / yapı ----------------
def pivots(candles, length):
    """Bir mum, kendisinden önceki `length` mumun hepsinden daha uçtaysa pivot.

    Pivot ancak `length` bar sonra kesinleşir; bu yüzden ileriye bakış yoktur."""
    n = len(candles)
    hi = [c["high"] for c in candles]
    lo = [c["low"] for c in candles]
    ph = [None] * n
    pl = [None] * n
    for i in range(length, n):
        w_hi = hi[i - length:i]
        w_lo = lo[i - length:i]
        if w_hi and hi[i] > max(w_hi):
            ph[i] = hi[i]
        if w_lo and lo[i] < min(w_lo):
            pl[i] = lo[i]
    return ph, pl


def structure(candles, length):
    """Yapı kırılımlarını (BOS/CHoCH) ve order block'ları üretir.

    Her kırılımda:
      - trend yönü güncellenir
      - kırılım trendle AYNI yöndeyse BOS, TERSse CHoCH etiketlenir
      - order block, pivot ile kırılım arasındaki EN UÇ mumdur
    """
    n = len(candles)
    ph, pl = pivots(candles, length)
    p_hi, p_lo = parsed_levels(candles)
    cl = [c["close"] for c in candles]

    son_ph = son_pl = None          # en son onaylanan pivot seviyeleri
    ph_bar = pl_bar = None
    ph_gecti = pl_gecti = True
    trend = 0
    olaylar = []                     # (index, yön, tip, ob_top, ob_bot, ob_bar)

    for i in range(n):
        # Pivot ancak `length` bar sonra görünür olur (ileriye bakış yok)
        r = i - length
        if 0 <= r < n:
            if ph[r] is not None:
                son_ph, ph_bar, ph_gecti = ph[r], r, False
            if pl[r] is not None:
                son_pl, pl_bar, pl_gecti = pl[r], r, False

        if son_ph is not None and not ph_gecti and cl[i] > son_ph:
            tip = "CHoCH" if trend == BEARISH else "BOS"
            trend, ph_gecti = BULLISH, True
            ob = _order_block(p_hi, p_lo, ph_bar, i, BULLISH)
            olaylar.append((i, BULLISH, tip) + ob)

        if son_pl is not None and not pl_gecti and cl[i] < son_pl:
            tip = "CHoCH" if trend == BULLISH else "BOS"
            trend, pl_gecti = BEARISH, True
            ob = _order_block(p_hi, p_lo, pl_bar, i, BEARISH)
            olaylar.append((i, BEARISH, tip) + ob)

    return olaylar, trend


def _order_block(p_hi, p_lo, pivot_bar, break_bar, bias):
    """Order block = pivot ile kırılım arasındaki EN UÇ mum.

    Bullish kırılımda en DÜŞÜK dip, bearish kırılımda en YÜKSEK tepe.
    (Yaygın 'son zıt renkli mum' yaklaşımından farklıdır ve daha
    tutarlı seviyeler verir.)"""
    bas = max(0, (pivot_bar if pivot_bar is not None else break_bar) - 0)
    bas = max(bas, break_bar - OB_SEARCH_MAX)
    son = break_bar
    if son <= bas:
        return (None, None, None)
    dilim = range(bas, son)
    if bias == BULLISH:
        idx = min(dilim, key=lambda k: p_lo[k])
    else:
        idx = max(dilim, key=lambda k: p_hi[k])
    return (p_hi[idx], p_lo[idx], idx)


# ---------------- FVG ----------------
def fair_value_gaps(candles, esik_carpani=FVG_THRESHOLD_MULT):
    """3 mumluk boşluk + anlamlılık eşiği.

    Eşik, ortalama mum gövdesi yüzdesinin katıdır — küçük, önemsiz
    boşluklar elenir."""
    n = len(candles)
    if n < 3:
        return []
    deltalar = [abs(c["close"] - c["open"]) / c["open"] * 100 for c in candles if c["open"]]
    ort = sum(deltalar) / len(deltalar) if deltalar else 0
    esik = ort * esik_carpani

    out = []
    for i in range(2, n):
        a, b, c = candles[i - 2], candles[i - 1], candles[i]
        d = abs(b["close"] - b["open"]) / b["open"] * 100 if b["open"] else 0
        if d < esik:
            continue
        if c["low"] > a["high"] and b["close"] > a["high"]:
            out.append((i, BULLISH, a["high"], c["low"]))
        elif c["high"] < a["low"] and b["close"] < a["low"]:
            out.append((i, BEARISH, c["high"], a["low"]))
    return out


# ---------------- Premium / Discount ----------------
def premium_discount(candles, length=SWING_LEN):
    """Trailing swing aralığına göre bölge.

    Long yalnızca discount'ta (alt yarı), short yalnızca premium'da (üst yarı)
    aranır — ICT/SMC'nin temel kuralı."""
    seg = candles[-length * 3:] if len(candles) > length * 3 else candles
    if not seg:
        return None, None, None
    top = max(c["high"] for c in seg)
    bot = min(c["low"] for c in seg)
    if top <= bot:
        return None, None, None
    fiyat = candles[-1]["close"]
    konum = (fiyat - bot) / (top - bot)      # 0 = dip, 1 = tepe
    return top, bot, konum


LIQ_LEN = 20            # likidite havuzu pivot uzunluğu


def liquidity_levels(candles, length=LIQ_LEN):
    """Geçmiş pivot tepe/dipleri = likidite havuzları.

    Uzunluk seçimi kritik: 5 barlık pivotlar o kadar sık ki en yakın havuz
    stop mesafesinin altında kalıyor (ölçüldü: R:R medyanı 0.62). Aralığın
    ucu ise ters uçta — 11.7R, pozisyon oraya varmadan zaman aşımına giriyor.
    20 bar anlamlı havuzları verir."""
    ph, pl = pivots(candles, length)
    return ([h for h in ph if h is not None],
            [l for l in pl if l is not None])


def next_liquidity(entry, direction, highs, lows, min_dist, max_dist, count=3):
    """Girişten en az `min_dist`, en çok `max_dist` uzaktaki likidite havuzları.

    'En yakın havuz' değil, 'yeterince uzaktaki ilk havuz' hedeflenir:
    hedef stop mesafesinden yakınsa işlem komisyondan sonra kazandırmaz,
    çok uzaksa hiç ulaşılamaz."""
    if direction == BULLISH:
        aday = sorted(x for x in highs if entry + min_dist <= x <= entry + max_dist)
    else:
        aday = sorted((x for x in lows if entry - max_dist <= x <= entry - min_dist),
                      reverse=True)
    secilen = []
    for lv in aday:
        if all(abs(lv - p) >= min_dist * 0.5 for p in secilen):
            secilen.append(lv)
        if len(secilen) == count:
            break
    return secilen + [None] * (count - len(secilen))


def bias_of(candles, length=SWING_LEN):
    """Bir zaman diliminin yönü: son yapı kırılımının yönü."""
    olaylar, trend = structure(candles, length)
    return trend if trend else None


# ---------------- Kurulum ve çıkış (backtest ve canlı bot ORTAK kullanır) ----------------
def find_setup(h4, daily, weekly, *, setup_max_age=6, sl_atr_mult=0.25,
               min_rr=1.5, max_rr=6.0, liq_len=LIQ_LEN, discount_max=0.5,
               require_choch=False, dir_filter=None):
    """Kurulum varsa sözlük, yoksa None. Son mum "şu an" kabul edilir.

    Bu fonksiyonu HEM smc_htf_backtest HEM smc_scanner çağırır. Kopyalanmamalı:
    canlı botun mantığı backtest'ten ayrışırsa test edilen şey çalışmıyor
    demektir ve bunu fark etmek çok zor olur."""
    if len(h4) < 120 or len(daily) < 60 or len(weekly) < 20:
        return None

    # 1) Haftalık ve günlük yön aynı olmalı
    w_bias = bias_of(weekly, 10)
    d_bias = bias_of(daily, 20)
    if not w_bias or not d_bias or w_bias != d_bias:
        return None
    bias = w_bias

    # 2) Fiyat doğru bölgede mi (long -> discount, short -> premium)
    top, bot, konum = premium_discount(h4)
    if konum is None:
        return None
    if bias == BULLISH and konum > discount_max:
        return None
    if bias == BEARISH and konum < 1 - discount_max:
        return None

    # 3) 4H'de bias yönünde taze kırılım
    olaylar, _ = structure(h4, INTERNAL_LEN)
    son = None
    for ev in reversed(olaylar):
        i, yon, tip, ob_top, ob_bot, ob_bar = ev
        if len(h4) - 1 - i > setup_max_age:
            break
        # Not: pencerede ters yönde DAHA YENİ bir kırılım olması teorik olarak
        # mümkün ama ölçüldüğünde hiç gerçekleşmiyor — structure() bir
        # kırılımdan sonra yeni pivot oluşmadan tekrar tetiklenmiyor.
        if yon == bias and ob_top is not None:
            if require_choch and tip != "CHoCH":
                continue
            son = ev
            break
    if son is None:
        return None

    i, yon, tip, ob_top, ob_bot, ob_bar = son
    yon_ad = "long" if yon == BULLISH else "short"
    if dir_filter and dir_filter != yon_ad:
        return None

    fiyat = h4[-1]["close"]
    a = atr(h4, 14)

    # 4) Giriş order block'un ortasına, SL ötesine
    entry = (ob_top + ob_bot) / 2
    if yon == BULLISH:
        sl = ob_bot - a * sl_atr_mult
        if fiyat <= entry:           # fiyat zaten OB'nin içinde/altında
            return None
        uc = top
    else:
        sl = ob_top + a * sl_atr_mult
        if fiyat >= entry:
            return None
        uc = bot

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # 5) Hedef yalnızca R:R filtresi için — çıkış SÜRÜKLENEN STOP ile olur.
    #    Sabit TP'li çıkış ölçüldü ve daha kötü: +0.122R (t=1.71) karşı
    #    sürüklenen stopta +0.240R (t=2.33).
    hi, lo = liquidity_levels(h4, liq_len)
    tps = next_liquidity(entry, yon, hi, lo,
                         min_dist=risk * min_rr, max_dist=risk * max_rr)
    if tps[0] is None:
        return None
    rr = abs(tps[0] - entry) / risk
    if rr < min_rr or rr > max_rr:
        return None

    return {"dir": yon_ad, "entry": entry, "sl": sl, "tps": tps,
            "risk_pct": 100 * risk / entry, "rr": rr, "tip": tip,
            "ob_top": ob_top, "ob_bot": ob_bot}


TRAIL_LEN = 5


def trail_levels(h4, is_long, trail_len=TRAIL_LEN):
    """Her bar için stopun çekilebileceği seviye (yoksa None).

    `k` barında `k - trail_len` barının pivot olup olmadığına bakılır. Pivot
    yalnızca kendinden ÖNCEKİ barlara göre tanımlı olduğu ve ancak `trail_len`
    bar sonra kullanıldığı için ileriye bakış yoktur."""
    ph, pl = pivots(h4, trail_len)
    src = pl if is_long else ph
    return [src[k - trail_len] if k >= trail_len else None
            for k in range(len(h4))]
