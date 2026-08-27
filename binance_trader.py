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
        self.available_usdt = avail
        return bal

    def sizing_balance(self):
        """Pozisyon büyüklüğü hesabında kullanılacak bakiye.

        Toplam cüzdan bakiyesi DEĞİL: marjın bir kısmı açık pozisyonlarda
        bağlı olabilir (ölçüldü: toplam 5009 USDT, kullanılabilir 3244).
        Toplamla boyutlandırmak, borsanın yetersiz marj diye reddedeceği
        emirler üretir."""
        bal = self.balance_usdt()
        return min(bal, getattr(self, "available_usdt", bal) or bal)

    def api_izinleri(self):
        """Anahtarın izinleri (spot ucundan, salt okunur). Okunamazsa None.

        Binance, Futures iznini ancak IP kısıtlaması uygulanmış anahtarlarda
        veriyor. Ev IP'si değişirse izin bozulmaz ama istekler -2015 alır;
        ikisini ayırt edebilmek için izin durumunu ayrıca okuyoruz."""
        eski = self.base
        try:
            self.base = "https://api.binance.com"
            return self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)
        except Exception:
            return None
        finally:
            self.base = eski

    def dis_ip(self):
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
                return r.read().decode().strip()
        except Exception:
            return "?"

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

    def realized_pnl(self, limit=1000):
        """Borsadaki GERÇEK kâr/zarar — /fapi/v1/income üzerinden.

        Kâğıt üzerindeki simülasyon (smc_scanner.pnl_usd) her işlemi sabit
        100 USDT nominal varsayar, ama gerçek pozisyonlar risk-bazlı
        boyutlandırılıyor (stop mesafesine göre nominal 11-150 USDT arası
        değişiyor). Ölçüldü: kâğıt hesap -95.63$ derken gerçek net sonuç
        +5.97$ idi — iki değer birbiriyle KIYASLANAMAZ. Bu fonksiyon tek
        güvenilir kaynaktır."""
        pnl = self._request("GET", "/fapi/v1/income",
                            {"incomeType": "REALIZED_PNL", "limit": limit},
                            signed=True)
        fee = self._request("GET", "/fapi/v1/income",
                            {"incomeType": "COMMISSION", "limit": limit},
                            signed=True)
        toplam_pnl = sum(float(x["income"]) for x in pnl)
        toplam_fee = sum(float(x["income"]) for x in fee)
        return {"pnl": toplam_pnl, "komisyon": toplam_fee,
                "net": toplam_pnl + toplam_fee, "islem_sayisi": len(pnl)}

    def open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def setup_symbol(self, symbol, leverage=LEVERAGE):
        """Kaldıraç + İZOLE marjin ayarı. Başarılıysa True döner.

        İzole marjin şart: cross marjinde tek bir işlemin zararı TÜM bakiyeyi
        yiyebilir, izolede ise yalnızca o pozisyonun marjını. Eskiden buradaki
        hata sessizce yutuluyordu; ayar tutmazsa pozisyon cross açılır ve bunu
        kimse fark etmezdi. Artık ayarlanamazsa False dönüp işlem atlanıyor.

        Borsa zaten izole ise -4046 ("No need to change margin type") döner;
        bu bir hata değildir."""
        if self.dry:
            return True
        tamam = True
        try:
            self._request("POST", "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": int(leverage)}, signed=True)
        except RuntimeError as e:
            print(f"  [{symbol}] kaldıraç ayarlanamadı: {e}")
            tamam = False
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except RuntimeError as e:
            if "-4046" in str(e):
                pass                      # zaten izole
            else:
                print(f"  [{symbol}] İZOLE marjin ayarlanamadı: {e}")
                tamam = False
        return tamam

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
        algo emirleri ayrı serviste tutuluyor.

        Yol adı "openAlgoOrders" — "algoOpenOrders" DEĞİL. Binance'in kendi
        doküman sayfası ikincisini yazıyor ama o yol 404 (-5000) veriyor;
        testnet'e sorularak ölçüldü. Yanlış yol sessiz bir felakete yol
        açıyordu: liste okunamayınca koruma emirleri hiç kurulmuyordu."""
        params = {"symbol": symbol} if symbol else {}
        r = self._request("GET", "/fapi/v1/openAlgoOrders", params, signed=True)
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


def update_stop(api, symbol, direction, new_stop):
    """Sürüklenen stop: koruma emrini yeni seviyeye taşır.

    ÖNCE eskiyi iptal eder, SONRA yeni emri koyar. Ters sıra (önce yeni,
    sonra eski) denendi ve ÇALIŞMIYOR: Binance aynı sembol+yönde iki
    closePosition STOP_MARKET emrini aynı anda kabul etmiyor
    (-4130 "An open stop or take profit order with GTE and closePosition
    in the direction is existing"). O sırayla fonksiyon hep başarısız
    oluyordu ve stop HİÇ SÜRÜKLENMİYORDU — sessizce, fark edilmeden.

    Bu sırada iptal ile yeni emrin arasında kısa bir an (bir istek turu)
    pozisyon stopsuz kalır. Bu risk, kabul edilmiş: alternatifi stopun hiç
    güncellenmemesiydi, ki bu daha kötü. Yeni emir başarısız olursa eski
    zaten iptal edilmiş olabileceği için sonuç Telegram'a bildirilir."""
    new_stop = api.round_price(symbol, new_stop)
    try:
        orders = api.algo_open_orders(symbol)
    except Exception as e:
        print(f"  [{symbol}] algo emirleri okunamadı, stop taşınmadı: {e}")
        return False

    def _tip(o):
        return o.get("orderType") or o.get("type")

    def _trig(o):
        try:
            return float(o.get("triggerPrice") or o.get("stopPrice") or 0)
        except (TypeError, ValueError):
            return 0.0

    eski = [o for o in orders if _tip(o) == "STOP_MARKET"]
    if any(_trig(o) == new_stop for o in eski):
        return False                      # zaten doğru seviyede

    for o in eski:
        try:
            api.cancel_algo(o.get("algoId") or o.get("orderId"))
        except Exception as e:
            print(f"  [{symbol}] eski stop iptal edilemedi: {e}")

    try:
        api.place_stop(symbol, direction, new_stop)
    except Exception as e:
        # -2021 "Order would immediately trigger": fiyat hesaplanan yeni
        # stopu ZATEN geçmiş (yaşandı — PAXGUSDT bu yüzden bir tur boyunca
        # tamamen korumasız kaldı, hata sadece terminale yazılıyor, Telegram'a
        # gitmiyordu). Eski emir zaten iptal edildiği için burada durmak
        # pozisyonu tamamen açıkta bırakır. Güncel fiyata göre bir güvenlik
        # tamponuyla HEMEN yeniden dene; bu, elle yaptığım acil müdahalenin
        # otomatikleşmiş hali.
        if "-2021" in str(e):
            try:
                fiyat = float(api._request(
                    "GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])
                guvenli = api.round_price(
                    symbol, fiyat * (1.015 if direction == "short" else 0.985))
                api.place_stop(symbol, direction, guvenli)
                print(f"  [{symbol}] hedef stop {new_stop} tetiklenirdi, "
                      f"güvenlik tamponuyla {guvenli} kondu")
                return True
            except Exception as e2:
                print(f"  [{symbol}] GÜVENLİK TAMPONU DA BAŞARISIZ, "
                      f"POZİSYON KORUMASIZ: {e2}")
                raise RuntimeError(f"[{symbol}] KORUMASIZ: {e2}") from None
        print(f"  [{symbol}] YENİ STOP KONULAMADI (eski iptal edildi, "
              f"pozisyon ŞU AN KORUMASIZ OLABİLİR): {e}")
        raise RuntimeError(f"[{symbol}] KORUMASIZ: {e}") from None
    print(f"  [{symbol}] stop taşındı → {new_stop}")
    return True


def close_position(api, symbol, direction):
    """Pozisyonu piyasadan kapatır ve artık emirleri temizler.

    Süre aşımında bot pozisyonu 'kapandı' sayıp yalnızca emirleri iptal
    ediyordu; pozisyonun kendisi borsada AÇIK kalıyor, üstelik stop emri de
    iptal edildiği için tamamen korumasız ve artık izlenmiyor oluyordu."""
    pos = api.positions().get(symbol)
    if not pos or not pos["amt"]:
        cancel_everything(api, symbol)
        return False
    qty = api.round_qty(symbol, abs(pos["amt"]))
    if qty <= 0:
        return False
    print(f"  [{symbol}] süre doldu → pozisyon piyasadan kapatılıyor ({qty})")
    api._order({"symbol": symbol,
                "side": "SELL" if direction == "long" else "BUY",
                "type": "MARKET", "quantity": qty, "reduceOnly": "true"})
    cancel_everything(api, symbol)
    return True


def cancel_everything(api, symbol):
    """Hem normal hem ALGO emirlerini iptal eder.

    cancel_all() yalnızca /fapi/v1/allOpenOrders'ı çağırır; koşullu emirler
    ayrı serviste durduğu için orada görünmez ve iptal edilmez."""
    try:
        api.cancel_all(symbol)
    except Exception as e:
        print(f"  [{symbol}] normal emirler iptal edilemedi: {e}")
    try:
        for o in api.algo_open_orders(symbol):
            api.cancel_algo(o.get("algoId") or o.get("orderId"))
    except Exception as e:
        print(f"  [{symbol}] algo emirleri iptal edilemedi: {e}")


def safe_leverage(risk_frac, azami=LEVERAGE, guvenlik=2.0, bakim=0.005):
    """Stopun likidasyondan ÖNCE tetiklenmesini garantileyen kaldıraç.

    İzole marjinde likidasyon, yaklaşık (1/kaldıraç - bakım marjı) kadarlık
    ters harekette gerçekleşir. 10x'te bu ~%9.5; stop mesafesi bunun üstündeyse
    stop hiç çalışmaz, pozisyon likide olur ve çıkış backtest'te modellenen
    şey olmaz (ölçüldü: TUTUSDT kurulumunda stop %17.13 idi).

    Risk-bazlı sizing'de nominal zaten risk bütçesinden geliyor; kaldıraç
    yalnızca marjı belirler. Bu yüzden kaldıracı düşürmek riski ARTIRMAZ,
    sadece stopu işlevsel kılar."""
    if risk_frac <= 0:
        return azami
    return max(1, min(azami, int(1.0 / (guvenlik * risk_frac + bakim))))


def risk_based_notional(api, symbol, entry, sl, balance,
                        risk_pct_of_balance=2.0, max_open=5, leverage=LEVERAGE):
    """Nominal büyüklük, stop mesafesine göre: her işlemde AYNI dolar riski.

    Sabit marjin kullanılamaz — stop mesafesi işlemden işleme %0.5 ile %8
    arasında değişiyor, sabit marjinde bu işlem başına 16 kat farklı dolar
    riski demek. Stratejinin doğrulandığı portföy simülasyonu da (bakiye 499$,
    azami düşüş %25) bu risk-normalize sizing ile ölçüldü."""
    risk_frac = abs(entry - sl) / entry
    if risk_frac <= 0:
        return 0.0
    notional = (balance * risk_pct_of_balance / 100) / risk_frac
    # Tek işlemin marjı bakiyenin belli bir oranını aşmasın. max_open=0
    # (sınırsız) durumunda bölme yapılamayacağı için %10 tavanı kullanılır —
    # 10 slotluk ayarın karşılığı. Sınırsız modda işlem SAYISINI sınırlayan
    # şey borsadaki kullanılabilir marj olur.
    pay = (1.0 / max_open) if max_open else 0.10
    return min(notional, balance * pay * leverage)


def open_trade_trailing(api, symbol, direction, entry, sl, balance,
                        risk_pct_of_balance=2.0, max_open=5):
    """Sürüklenen stop modeli: sabit TP yok, yalnızca giriş + koruyucu stop."""
    entry = api.round_price(symbol, entry)
    risk_frac = abs(entry - sl) / entry if entry else 0
    kald = safe_leverage(risk_frac)
    notional = risk_based_notional(api, symbol, entry, sl, balance,
                                   risk_pct_of_balance, max_open, leverage=kald)
    if notional < api.min_notional(symbol):
        print(f"  [{symbol}] nominal {notional:.1f} < min {api.min_notional(symbol)}, atlandı")
        return None
    qty = api.round_qty(symbol, notional / entry)
    if qty <= 0:
        return None

    if not api.setup_symbol(symbol, kald):
        print(f"  [{symbol}] kaldıraç/izole marjin kurulamadı — işlem ATLANDI "
              f"(cross marjinde açmaktansa hiç açmamak yeğdir)")
        return None
    print(f"  [{symbol}] {direction.upper()} giriş={entry} miktar={qty} "
          f"nominal≈{notional:.1f} USDT marj≈{notional / kald:.2f} "
          f"kaldıraç={kald}x (stop %{100 * risk_frac:.2f}, "
          f"risk≈{balance * risk_pct_of_balance / 100:.2f} USDT)")
    api.place_entry(symbol, direction, qty, entry)

    # Limit emir anında dolarsa pozisyon stopsuz kalmasın
    if not api.dry:
        time.sleep(1)
        try:
            pos = api.positions().get(symbol)
            if pos and pos["amt"]:
                print(f"  [{symbol}] emir anında doldu → stop hemen kuruluyor")
                ensure_protection(api, symbol, direction, pos["amt"], sl,
                                  (None, None, None))
        except Exception as e:
            print(f"  [{symbol}] anlık dolum kontrolü başarısız: {e}")

    return {"symbol": symbol, "direction": direction, "entry": entry,
            "qty": qty, "sl": sl, "notional": notional}


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
        # Liste okunamadıysa emirleri KURMAYA DEVAM ET. Eskiden burada
        # return False vardı ve tek bir okuma hatası pozisyonu tamamen
        # stopsuz bırakıyordu. Fazladan bir stop zararsız (ilki tetiklenince
        # Binance diğerini iptal eder), stopsuz pozisyon değil.
        print(f"  [{symbol}] algo emirleri okunamadı ({e}) — koruma yine de kuruluyor")
        orders = []

    def _tip(o):
        return o.get("orderType") or o.get("type")

    istenen_tp = [t for t in tps if t is not None]
    has_sl = any(_tip(o) == "STOP_MARKET" for o in orders)
    # Sürüklenen stop modelinde sabit TP istenmez; o durumda TP'yi "kurulu" say,
    # yoksa her taramada koruma eksik görünür ve boşuna yeniden kurulmaya çalışılır.
    has_tp = any(_tip(o) == "TAKE_PROFIT_MARKET" for o in orders) or not istenen_tp
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
