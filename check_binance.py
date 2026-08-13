"""
Binance bağlantı tanılaması — saniyeler sürer, tam tarama beklemeye gerek yok.

Hesabın hangi uçtan ne döndürdüğünü gösterir. Çıktıda API anahtarı veya imza
BULUNMAZ; yalnızca bakiye ve hesap durumu yazdırılır.

Kullanım:  python check_binance.py
"""

import json
import os

import binance_trader as bt


def kisalt(v, n=400):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + " …"


def main():
    print("=== AYARLAR ===")
    print(f"  işlem açık   : {bt._enabled()}")
    print(f"  testnet      : {bt._testnet()}")
    print(f"  kuru çalışma : {bt._dry_run()}")
    key = os.environ.get("BINANCE_API_KEY", "")
    sec = os.environ.get("BINANCE_API_SECRET", "")
    print(f"  API key      : {'var (' + str(len(key)) + ' karakter)' if key else 'YOK'}")
    print(f"  API secret   : {'var (' + str(len(sec)) + ' karakter)' if sec else 'YOK'}")
    if not key or not sec:
        print("\nAnahtarlar ortamda yok — Secrets doğru bağlanmamış.")
        return

    api = bt.BinanceFutures()
    print(f"  adres        : {api.base}")

    # 1) İmzasız uç: ağ ve adres doğru mu?
    print("\n=== 1) SUNUCU SAATİ (imzasız) ===")
    try:
        print("  ", kisalt(api._request("GET", "/fapi/v1/time")))
    except Exception as e:
        print("  HATA:", e)
        return

    # 2) İmzalı uçlar: anahtar geçerli mi, bakiye nerede?
    for ad, yol in [("/fapi/v2/balance", "/fapi/v2/balance"),
                    ("/fapi/v2/account (özet)", "/fapi/v2/account")]:
        print(f"\n=== 2) {ad} ===")
        try:
            r = api._request("GET", yol, signed=True)
        except Exception as e:
            print("  HATA:", e)
            continue

        if yol.endswith("balance"):
            if not isinstance(r, list):
                print("  Beklenmedik yanıt tipi:", type(r).__name__, kisalt(r))
                continue
            print(f"  {len(r)} varlık satırı döndü")
            for b in r:
                bal = float(b.get("balance", 0) or 0)
                av = float(b.get("availableBalance", 0) or 0)
                cw = float(b.get("crossWalletBalance", 0) or 0)
                if bal or av or cw:
                    print(f"    {b.get('asset'):6s} balance={bal:.4f} "
                          f"available={av:.4f} crossWallet={cw:.4f}")
            if not any(float(b.get("balance", 0) or 0) for b in r):
                print("    (hiçbir varlıkta balance>0 yok)")
        else:
            for k in ("totalWalletBalance", "availableBalance",
                      "totalMarginBalance", "multiAssetsMargin", "canTrade"):
                if k in r:
                    print(f"    {k}: {r[k]}")
            assets = [a for a in r.get("assets", [])
                      if float(a.get("walletBalance", 0) or 0)]
            for a in assets:
                print(f"    varlık {a.get('asset')}: "
                      f"wallet={a.get('walletBalance')} "
                      f"available={a.get('availableBalance')}")
            if not assets:
                print("    (assets içinde walletBalance>0 olan yok)")

    # 3) Borsada gerçekten ne var: açık emirler ve pozisyonlar
    print("\n=== 3) AÇIK EMİRLER ===")
    try:
        orders = api.open_orders()
        if not orders:
            print("  (açık emir yok)")
        for o in orders:
            print(f"    {o.get('symbol'):14s} {o.get('type'):22s} "
                  f"{o.get('side'):5s} miktar={o.get('origQty')} "
                  f"fiyat={o.get('price')} stop={o.get('stopPrice')} "
                  f"reduceOnly={o.get('reduceOnly')}")
    except Exception as e:
        print("  HATA:", e)

    print("\n=== 3b) AÇIK ALGO (KOŞULLU) EMİRLER ===")
    try:
        algos = api.algo_open_orders()
        if not algos:
            print("  (açık koşullu emir yok)")
        for o in algos:
            print(f"    {o.get('symbol'):14s} {str(o.get('orderType') or o.get('type')):20s} "
                  f"{o.get('side'):5s} miktar={o.get('quantity')} "
                  f"tetik={o.get('triggerPrice')} closePos={o.get('closePosition')} "
                  f"algoId={o.get('algoId')}")
    except Exception as e:
        print("  HATA:", e)

    print("\n=== 4) AÇIK POZİSYONLAR ===")
    try:
        pos = api.positions()
        if not pos:
            print("  (açık pozisyon yok — giriş emri henüz dolmamış olabilir)")
        for s, p in pos.items():
            print(f"    {s:14s} {p['side']:5s} miktar={p['amt']} giriş={p['entry']}")
    except Exception as e:
        print("  HATA:", e)

    print("\n=== SONUÇ ===")
    b = api.balance_usdt()
    print(f"  balance_usdt() -> {b:.2f} USDT")
    if b <= 0:
        print("  Bakiye 0 okunuyor. Yukarıdaki dökümde bakiye görünüyorsa")
        print("  okuma mantığı yanlış alanı kullanıyor demektir.")


if __name__ == "__main__":
    main()
