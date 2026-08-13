"""
Binance USDⓈ-M Futures otomatik emir modülü.

Sinyal üretimi ict_scanner.py'da kalır; burası SADECE emir yerleştirme ve
pozisyon yönetimi yapar.

Akış:
  1. Sinyal gelince: giriş LIMIT emri + SL (STOP_MARKET) + 3 kademe TP
     (TAKE_PROFIT_MARKET, reduceOnly) yerleştirilir.
  2. Her taramada borsadaki gerçek durum okunur (yerel state'e güvenilmez).
  3. TP1 dolunca SL iptal edilip TP1 seviyesine taşınır.
     TP2 dolunca SL, TP2 seviyesine taşınır.

Güvenlik:
  - Varsayılan TESTNET ve DRY_RUN açık. Gerçek işlem için ikisi de kapatılmalı.
  - API anahtarları yalnızca ortam değişkeninden okunur, asla loglanmaz.
  - Emirler borsanın tick/step/minNotional filtrelerine yuvarlanır; aksi halde
    borsa reddeder.
  - Aynı sembolde açık pozisyon varken yeni giriş yapılmaz.

Ortam değişkenleri:
  BINANCE_API_KEY, BINANCE_API_SECRET   (GitHub Secrets)
  BINANCE_TESTNET=1                     (varsayılan 1)
  BINANCE_DRY_RUN=1                     (varsayılan 1 — emir GÖNDERİLMEZ)
  BINANCE_TRADING_ENABLED=1             (bu olmadan modül hiç çalışmaz)
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

LIVE_BASE = "https://fapi.binance.com"
TESTNET_BASE = "https://testnet.binancefuture.com"

LEVERAGE = 10
MARGIN_PCT = 0.10          # bakiyenin bu oranı marjin (10x ile nominal = bakiye)
TP_SHARES = (0.50, 0.30, 0.20)   # TP1 / TP2 / TP3 kapanış oranları
MAX_OPEN_POSITIONS = 10
RECV_WINDOW = 5000


def _enabled():
    return os.environ.get("BINANCE_TRADING_ENABLED") == "1"


def _testnet():
    return os.environ.get("BINANCE_TESTNET", "1") == "1"


def _dry_run():
    return os.environ.get("BINANCE_DRY_RUN", "1") == "1"


class BinanceFutures:
    def __init__(self):
        self.key = os.environ.get("BINANCE_API_KEY", "")
        self.secret = os.environ.get("BINANCE_API_SECRET", "")
        self.base = TESTNET_BASE if _testnet() else LIVE_BASE
        self.dry = _dry_run()
        self._filters = {}

    # ---------------- alt seviye ----------------
    def _request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = RECV_WINDOW
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            qs = f"{qs}&signature={sig}"
        else:
            qs = urllib.parse.urlencode(params)

        url = f"{self.base}{path}"
        data = None
        if method == "GET":
            url = f"{url}?{qs}" if qs else url
        else:
            data = qs.encode()

        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-MBX-APIKEY": self.key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            # Anahtarlar asla loglanmaz; sadece borsanın hata mesajı
            raise RuntimeError(f"Binance {e.code}: {body}") from None

    # ---------------- sembol filtreleri ----------------
    def load_filters(self):
        """tickSize / stepSize / minNotional — yuvarlama için şart."""
        info = self._request("GET", "/fapi/v1/exchangeInfo")
        for s in info["symbols"]:
            f = {"tick": None, "step": None, "min_notional": 0.0,
                 "qty_prec": s.get("quantityPrecision", 3),
                 "price_prec": s.get("pricePrecision", 2)}
            for flt in s["filters"]:
                if flt["filterType"] == "PRICE_FILTER":
                    f["tick"] = float(flt["tickSize"])
                elif flt["filterType"] == "LOT_SIZE":
                    f["step"] = float(flt["stepSize"])
                elif flt["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    f["min_notional"] = float(flt.get("notional", 0) or 0)
            self._filters[s["symbol"]] = f

    def _round(self, value, increment, precision):
        if not increment:
            return round(value, precision)
        return round(round(value / increment) * increment, precision)

    def round_price(self, symbol, price):
        f = self._filters.get(symbol)
        if not f:
            return price
        return self._round(price, f["tick"], f["price_prec"])

    def round_qty(self, symbol, qty):
        f = self._filters.get(symbol)
        if not f:
            return qty
        # miktar AŞAĞI yuvarlanır; yukarı yuvarlamak bakiyeyi aşabilir
        step, prec = f["step"], f["qty_prec"]
        if step:
            qty = int(qty / step) * step
        return round(qty, prec)

    def min_notional(self, symbol):
        return self._filters.get(symbol, {}).get("min_notional", 0.0)

    # ---------------- hesap ----------------
    def balance_usdt(self):
        """USDT bakiyesi. Sıfır dönerse sebebini anlamak için hesaptaki tüm
        sıfırdan büyük varlıklar loglanır (anahtar bilgisi içermez)."""
        rows = self._request("GET", "/fapi/v2/balance", signed=True)
        usdt = None
        nonzero = []
        for b in rows:
            bal = float(b.get("balance", 0) or 0)
            if b.get("asset") == "USDT":
                usdt = b
            if bal > 0:
                nonzero.append(f"{b.get('asset')}={bal:g}")

        if usdt is None:
            print(f"  UYARI: hesapta USDT satırı yok. Varlıklar: {[r.get('asset') for r in rows]}")
            return 0.0

        bal = float(usdt.get("balance", 0) or 0)
        avail = float(usdt.get("availableBalance", 0) or 0)
        if bal <= 0:
            print("  UYARI: USDT bakiyesi 0. Testnet hesabına bakiye yüklenmemiş olabilir.")
            print(f"  Sıfırdan büyük varlıklar: {nonzero or 'yok'}")
        else:
            print(f"  Bakiye: {bal:.2f} USDT (kullanılabilir {avail:.2f})")
        return bal

    def positions(self):
        """Borsadaki GERÇEK açık pozisyonlar (yerel state'e güvenilmez)."""
        out = {}
        for p in self._request("GET", "/fapi/v2/positionRisk", signed=True):
            amt = float(p["positionAmt"])
            if amt != 0:
                out[p["symbol"]] = {
                    "amt": amt, "entry": float(p["entryPrice"]),
                    "side": "long" if amt > 0 else "short",
                }
        return out

    def open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def setup_symbol(self, symbol):
        """Kaldıraç ve izole marjin ayarı. Zaten ayarlıysa borsa hata döner,
        bu normaldir ve yutulur."""
        if self.dry:
            return
        try:
            self._request("POST", "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": LEVERAGE}, signed=True)
        except RuntimeError as e:
            print(f"  [{symbol}] kaldıraç: {e}")
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except RuntimeError:
            pass   # "No need to change margin type" — zaten izole

    # ---------------- emirler ----------------
    def _order(self, params):
        if self.dry:
            print(f"  [KURU] emir gönderilmedi: {params}")
            return {"dry_run": True, "params": params}
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def place_entry(self, symbol, side, qty, price):
        """Giriş: bekleyen LIMIT emri (maker komisyonu için)."""
        return self._order({
            "symbol": symbol, "side": "BUY" if side == "long" else "SELL",
            "type": "LIMIT", "timeInForce": "GTC",
            "quantity": qty, "price": price,
        })

    # --- Koşullu emirler ---
    # Binance 2025-12-09'da STOP_MARKET / TAKE_PROFIT_MARKET gibi koşullu
    # emirleri /fapi/v1/order'dan ALGO servisine taşıdı. Eski uçtan
    # gönderilirse -4120 (STOP_ORDER_SWITCH_ALGO) döner.
    # Tetik seviyesi parametresi de "stopPrice" değil "triggerPrice".
    def _algo_order(self, params):
        params = dict(params, algoType="CONDITIONAL")
        if self.dry:
            print(f"  [KURU] algo emri gönderilmedi: {params}")
            return {"dry_run": True, "params": params}
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def place_stop(self, symbol, side, stop_price):
        """SL: tetiklenince pozisyonun tamamını piyasadan kapatır."""
        return self._algo_order({
            "symbol": symbol, "side": "SELL" if side == "long" else "BUY",
            "type": "STOP_MARKET", "triggerPrice": stop_price,
            "closePosition": "true", "workingType": "MARK_PRICE",
        })

    def place_tp(self, symbol, side, qty, stop_price):
        """Kademeli TP: yalnızca pozisyonu azaltır (reduceOnly)."""
        return self._algo_order({
            "symbol": symbol, "side": "SELL" if side == "long" else "BUY",
            "type": "TAKE_PROFIT_MARKET", "triggerPrice": stop_price,
            "quantity": qty, "reduceOnly": "true", "workingType": "MARK_PRICE",
        })

    def algo_open_orders(self, symbol=None):
        """Açık koşullu emirler. Normal /fapi/v1/openOrders bunları GÖSTERMEZ —
        algo emirleri ayrı serviste tutuluyor."""
        params = {"symbol": symbol} if symbol else {}
        r = self._request("GET", "/fapi/v1/algoOpenOrders", params, signed=True)
        return r if isinstance(r, list) else r.get("orders", [])

    def cancel_algo(self, algo_id):
        if self.dry:
            print(f"  [KURU] algo emri iptal edilmedi: #{algo_id}")
            return
        return self._request("DELETE", "/fapi/v1/algoOrder",
                             {"algoId": algo_id}, signed=True)

    def cancel_order(self, symbol, order_id):
        if self.dry:
            print(f"  [KURU] iptal edilmedi: {symbol} #{order_id}")
            return
        return self._request("DELETE", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_all(self, symbol):
        if self.dry:
            print(f"  [KURU] tüm emirler iptal edilmedi: {symbol}")
            return
        return self._request("DELETE", "/fapi/v1/allOpenOrders",
                             {"symbol": symbol}, signed=True)


# ---------------- yüksek seviye akış ----------------
def compute_quantity(api, symbol, entry_price, balance):
    """Marjin = bakiyenin MARGIN_PCT'i, nominal = marjin × kaldıraç."""
    notional = balance * MARGIN_PCT * LEVERAGE
    if notional < api.min_notional(symbol):
        return 0.0, notional
    return api.round_qty(symbol, notional / entry_price), notional


def open_trade(api, symbol, direction, entry, sl, tps, balance):
    """SADECE giriş (LIMIT) emrini yerleştirir.

    Koruma emirleri burada kurulmaz: Binance `reduceOnly` emirleri açık
    pozisyon yokken reddeder (-2022). Giriş limit emri dolana kadar pozisyon
    olmadığı için SL/TP, pozisyon açıldıktan sonra ensure_protection() ile
    kurulur."""
    entry = api.round_price(symbol, entry)
    qty, notional = compute_quantity(api, symbol, entry, balance)
    if qty <= 0:
        print(f"  [{symbol}] miktar çok küçük (min notional {api.min_notional(symbol)}), atlandı")
        return None

    api.setup_symbol(symbol)
    print(f"  [{symbol}] {direction.upper()} giriş={entry} miktar={qty} "
          f"nominal≈{notional:.1f} USDT")
    api.place_entry(symbol, direction, qty, entry)

    # Limit emir piyasanın ters tarafındaysa ANINDA dolar. Bu durumda koruma
    # emirlerini bir sonraki taramaya bırakmak, pozisyonu saatlerce stopsuz
    # bırakır. Hemen kontrol et; dolduysa SL/TP'yi şimdi kur.
    if not api.dry:
        time.sleep(1)                      # borsanın pozisyonu işlemesi için
        try:
            pos = api.positions().get(symbol)
            if pos and pos["amt"]:
                print(f"  [{symbol}] emir anında doldu → koruma hemen kuruluyor")
                ensure_protection(api, symbol, direction, pos["amt"], sl, tps)
        except Exception as e:
            print(f"  [{symbol}] anlık dolum kontrolü başarısız: {e}")

    return {"symbol": symbol, "direction": direction, "entry": entry,
            "qty": qty, "sl": sl, "tps": list(tps)}


def ensure_protection(api, symbol, direction, pos_amt, sl, tps):
    """Pozisyon açıldıysa SL ve kademeli TP emirlerini kurar (bir kez).

    Her taramada çağrılır; borsadaki açık emirlere bakıp eksikse tamamlar.
    Böylece giriş emri ne zaman dolarsa dolsun koruma kurulmuş olur."""
    if not pos_amt:
        return False
    try:
        # Koşullu emirler ALGO servisinde; normal openOrders'da görünmezler
        orders = api.algo_open_orders(symbol)
    except Exception as e:
        print(f"  [{symbol}] algo emirleri okunamadı: {e}")
        return False

    def _tip(o):
        return o.get("orderType") or o.get("type")

    has_sl = any(_tip(o) == "STOP_MARKET" for o in orders)
    has_tp = any(_tip(o) == "TAKE_PROFIT_MARKET" for o in orders)
    if has_sl and has_tp:
        return False                      # koruma zaten kurulu

    qty = abs(pos_amt)
    print(f"  [{symbol}] pozisyon açıldı ({qty}), koruma emirleri kuruluyor")

    if not has_sl:
        api.place_stop(symbol, direction, api.round_price(symbol, sl))

    if not has_tp:
        # Son kademe kalan miktarı alır ki yuvarlama artığı açıkta kalmasın
        placed = 0.0
        valid = [(t, s) for t, s in zip(tps, TP_SHARES) if t is not None]
        for i, (tp, share) in enumerate(valid):
            q = api.round_qty(symbol, qty - placed) if i == len(valid) - 1 \
                else api.round_qty(symbol, qty * share)
            if q <= 0:
                continue
            try:
                api.place_tp(symbol, direction, q, api.round_price(symbol, tp))
                placed += q
            except Exception as e:
                print(f"  [{symbol}] TP{i+1} kurulamadı: {e}")
    return True


def trail_stop(api, symbol, direction, pos_amt, original_qty, tps, state):
    """TP1 dolunca SL'yi TP1'e, TP2 dolunca TP2'ye taşır.

    Hangi TP'nin dolduğu, borsadaki KALAN pozisyon miktarından anlaşılır —
    yerel state'e değil gerçek duruma bakılır."""
    if not original_qty:
        return state
    filled_ratio = 1 - abs(pos_amt) / original_qty
    level = state.get("sl_level", 0)

    new_level = level
    if filled_ratio >= TP_SHARES[0] + TP_SHARES[1] - 0.01 and level < 2:
        new_level = 2      # TP2 doldu -> SL, TP2'ye
    elif filled_ratio >= TP_SHARES[0] - 0.01 and level < 1:
        new_level = 1      # TP1 doldu -> SL, TP1'e

    if new_level == level:
        return state

    target = tps[new_level - 1]
    if target is None:
        return state
    print(f"  [{symbol}] TP{new_level} doldu → SL, TP{new_level} seviyesine taşınıyor ({target})")

    # Eski stop emrini iptal et, yenisini kur (algo servisi üzerinden)
    try:
        for o in api.algo_open_orders(symbol):
            if (o.get("orderType") or o.get("type")) == "STOP_MARKET":
                api.cancel_algo(o.get("algoId"))
    except Exception as e:
        print(f"  [{symbol}] eski stop iptal edilemedi: {e}")
        return state                       # iptal edemeden yenisini kurma
    api.place_stop(symbol, direction, api.round_price(symbol, target))
    state["sl_level"] = new_level
    return state
