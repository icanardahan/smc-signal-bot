# SMC Sinyal Botu

Binance'deki tüm USDT paritelerini tarar; 1D grafikte Order Block/FVG bölgesine
fiyat geri döndüğünde 4H'de BOS/CHoCH + taze FVG onayı gelirse Telegram'a
Long/Short sinyali (giriş, SL, TP, R:R ile) gönderir. GitHub Actions üzerinde
her 4 saatte bir otomatik ve ücretsiz çalışır — sürekli açık bir bilgisayar
gerekmez.

## Kurulum

### 1. Telegram bot oluştur
1. Telegram'da **@BotFather**'a git, `/newbot` yaz, adını belirle.
2. Sana verdiği **bot token**'ı kaydet (örn. `123456:ABC-DEF...`).
3. Botuna Telegram'dan bir mesaj gönder (herhangi bir şey, örn. `/start`).
4. Chat ID'ni öğrenmek için tarayıcıda şu adresi aç (TOKEN yerine kendi
   token'ını yaz):
   `https://api.telegram.org/botTOKEN/getUpdates`
   Dönen JSON içinde `"chat":{"id": ...}` alanındaki sayı senin chat ID'in.

### 2. GitHub reposu oluştur ve bu klasörü push'la
```bash
cd "/Users/icanardahan/Desktop/smc-signal-bot"
git init
git add .
git commit -m "SMC sinyal botu ilk kurulum"
gh repo create smc-signal-bot --private --source=. --remote=origin --push
```
(`gh` CLI kurulu ve giriş yapılmış olmalı; yoksa GitHub üzerinden elle repo
oluşturup `git remote add origin <url>` ile bağlayabilirsin.)

### 3. GitHub Secrets ekle
Repo sayfasında **Settings → Secrets and variables → Actions → New repository
secret** ile ikisini ekle:
- `TELEGRAM_BOT_TOKEN` → BotFather'dan aldığın token
- `TELEGRAM_CHAT_ID` → yukarıda bulduğun chat ID

### 4. Çalıştır
Workflow her 4 saatte bir otomatik tetiklenir. İlk testi hemen görmek için
repo sayfasında **Actions → SMC Sinyal Taraması → Run workflow** ile elle
tetikleyebilirsin.

## Nasıl çalışıyor
- `scanner.py`: Binance API'den 1D ve 4H mum verisi çeker, TradingView'daki
  Pine Script ile aynı Order Block / FVG / BOS-CHoCH mantığını Python'da
  uygular, onay bulunca Telegram'a mesaj atar.
- `state.json`: Hangi sinyalin daha önce gönderildiğini tutar (aynı BOS için
  tekrar tekrar mesaj gitmesin diye), her çalışmadan sonra otomatik commit
  edilir.
- Bir çalışma ~300-400 sembolü tek tek taradığı için birkaç dakika sürebilir;
  bu normaldir.

## Parametreler
`scanner.py` başındaki sabitlerden ayarlanabilir: `CONFIRM_WINDOW`,
`PIVOT_LEN_HTF`, `PIVOT_LEN_LTF`, `LIQUIDITY_LOOKBACK`, `SL_ATR_MULT`.

## Sınırlamalar
- OB/FVG/BOS tespiti basitleştirilmiş bir yaklaşımdır, TradingView'daki Pine
  Script ile birebir aynı mantığı kullanır ama farklı piyasa koşullarında
  yanlış sinyal üretebilir.
- TP hesaplaması gerçek likidite havuzu analizi değil, son N bardaki en
  yüksek/düşük fiyat.
- Bu bir yatırım tavsiyesi değildir; gerçek parayla kullanmadan önce sinyalleri
  gözle/backtest ile doğrula.
