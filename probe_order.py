"""
Emir tipi denemesi — Binance hangi koruma emri biçimini kabul ediyor?

Hata -4120 ("Order type not supported for this endpoint") aldığımız için
birkaç varyantı sırayla dener, kabul edileni bulur ve HEMEN İPTAL EDER.
Stop seviyeleri fiyattan çok uzak seçilir; tetiklenme riski yoktur.

Kullanım: python probe_order.py
"""

import binance_trader as bt


def dene(api, ad, params):
    try:
        r = api._request("POST", "/fapi/v1/order", params, signed=True)
        oid = r.get("orderId")
        print(f"  ✅ {ad}  -> kabul edildi (orderId={oid})")
        try:
            api._request("DELETE", "/fapi/v1/order",
                         {"symbol": params["symbol"], "orderId": oid}, signed=True)
            print("      (iptal edildi)")
        except Exception as e:
            print(f"      İPTAL EDİLEMEDİ: {e}")
        return True
    except Exception as e:
        print(f"  ❌ {ad}  -> {str(e)[:110]}")
        return False


def main():
    api = bt.BinanceFutures()
    api.load_filters()

    pos = api.positions()
    if not pos:
        print("Açık pozisyon yok — deneme için bir pozisyon gerekiyor.")
        return
    sym, p = next(iter(pos.items()))
    side = p["side"]
    qty = abs(p["amt"])
    entry = p["entry"]
    kapat = "SELL" if side == "long" else "BUY"

    # Tetiklenmeyecek kadar uzak seviyeler
    uzak_stop = api.round_price(sym, entry * (0.5 if side == "long" else 1.5))
    uzak_tp = api.round_price(sym, entry * (1.5 if side == "long" else 0.5))
    q = api.round_qty(sym, qty)

    print(f"Deneme sembolü: {sym} ({side}, miktar={q}, giriş={entry})")
    print(f"Stop denemesi {uzak_stop}, TP denemesi {uzak_tp} — ikisi de çok uzak\n")

    print("--- STOP varyantları ---")
    dene(api, "STOP_MARKET + closePosition + MARK_PRICE",
         {"symbol": sym, "side": kapat, "type": "STOP_MARKET",
          "stopPrice": uzak_stop, "closePosition": "true",
          "workingType": "MARK_PRICE"})
    dene(api, "STOP_MARKET + closePosition (workingType yok)",
         {"symbol": sym, "side": kapat, "type": "STOP_MARKET",
          "stopPrice": uzak_stop, "closePosition": "true"})
    dene(api, "STOP_MARKET + quantity + reduceOnly",
         {"symbol": sym, "side": kapat, "type": "STOP_MARKET",
          "stopPrice": uzak_stop, "quantity": q, "reduceOnly": "true"})
    dene(api, "STOP_MARKET + quantity (reduceOnly yok)",
         {"symbol": sym, "side": kapat, "type": "STOP_MARKET",
          "stopPrice": uzak_stop, "quantity": q})
    dene(api, "STOP (limit) + quantity + price",
         {"symbol": sym, "side": kapat, "type": "STOP",
          "stopPrice": uzak_stop, "price": uzak_stop,
          "quantity": q, "timeInForce": "GTC", "reduceOnly": "true"})

    print("\n--- TAKE PROFIT varyantları ---")
    dene(api, "TAKE_PROFIT_MARKET + quantity + reduceOnly",
         {"symbol": sym, "side": kapat, "type": "TAKE_PROFIT_MARKET",
          "stopPrice": uzak_tp, "quantity": q, "reduceOnly": "true"})
    dene(api, "TAKE_PROFIT_MARKET + quantity (reduceOnly yok)",
         {"symbol": sym, "side": kapat, "type": "TAKE_PROFIT_MARKET",
          "stopPrice": uzak_tp, "quantity": q})
    dene(api, "LIMIT + reduceOnly (klasik kâr al)",
         {"symbol": sym, "side": kapat, "type": "LIMIT", "price": uzak_tp,
          "quantity": q, "timeInForce": "GTC", "reduceOnly": "true"})

    print("\nKabul edilen biçimi binance_trader.py'de kullanacağız.")


if __name__ == "__main__":
    main()
