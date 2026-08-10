# SMC Sinyal Botu

Binance'deki tüm USDT paritelerini tarayan, aynı anda çalışan **iki bağımsız
strateji**:

1. **HTF/LTF Order Block stratejisi** (`scanner.py`): 1D grafikte Order
   Block/FVG bölgesine fiyat geri döndüğünde 4H'de BOS/CHoCH + taze FVG onayı
   gelirse Long/Short sinyali (giriş, SL, TP1/2/3, kaldıraç, pozisyon
   büyüklüğü) gönderir, açtığı pozisyonları SL/TP'ye kadar izler.
2. **ICT Checklist stratejisi** (`ict_scanner.py`): HTF Bias, Kill Zone,
   Liquidity Sweep, MSS/Displacement, FVG/OTE olmak üzere 5 ICT kriterini
   otomatik puanlar; skoru 3/5 veya üzerinde olan kurulumlarda ✅/❌
   checklist'iyle birlikte uyarı gönderir.

İkisi de birbirinden bağımsız çalışır, aynı Telegram sohbetine ayrı ayrı
etiketlenmiş mesajlar gönderir. GitHub Actions üzerinde her 4 saatte bir
otomatik ve ücretsiz çalışır (repo public olduğu için Actions dakikası
sınırsızdır) — sürekli açık bir bilgisayar gerekmez.

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
  uygular, onay bulunca Telegram'a mesaj atar. Durumunu `state.json`'da tutar.
- `ict_scanner.py`: Aynı sembolleri tarar ama tamamen ayrı bir metodoloji
  kullanır — `scanner.py`'daki veri çekme fonksiyonlarını import eder,
  kendi 5 kriterlik ICT puanlama mantığını uygular. Durumunu `ict_state.json`'da
  ayrı tutar.
- Her iki state dosyası da hangi sinyalin daha önce gönderildiğini tutar
  (aynı kırılım için tekrar tekrar mesaj gitmesin diye), her çalışmadan sonra
  otomatik commit edilir.
- Her workflow çalışması iki taramayı sırayla yapar (~480 sembol × 2 strateji),
  toplamda ~20-35 dakika sürebilir; bu normaldir.

## Parametreler
`scanner.py` başındaki sabitlerden ayarlanabilir: `CONFIRM_WINDOW`,
`PIVOT_LEN_HTF`, `PIVOT_LEN_LTF`, `LIQUIDITY_LOOKBACK`, `SL_ATR_MULT`,
`LEVERAGE_CAP`, `MAX_POSITION_PCT`, `RR_SCALE_MIN`, `RR_SCALE_MAX`,
`MARGIN_RISK_MIN/MAX`, `ACCOUNT_RISK_MIN/MAX`.

## Kaldıraç ve pozisyon büyüklüğü önerisi (dinamik)
Kaldıraç ve pozisyon büyüklüğü sabit değil, her işlemin **TP1 R:R kalitesine**
göre ölçeklenir — R:R ne kadar iyiyse (asimetrik, güçlü setup) o kadar fazla
risk bütçesi/kaldıraç, R:R zayıfsa o kadar az:

- R:R ≤ `RR_SCALE_MIN` (1.0) → minimum risk bütçesi (marjinin %15'i / cüzdanın %0.5'i)
- R:R ≥ `RR_SCALE_MAX` (4.0) → maksimum risk bütçesi (marjinin %35'i / cüzdanın %2'si)
- Arada doğrusal olarak ölçeklenir; TP1 bulunamazsa en muhafazakar (minimum) değer kullanılır.

Kaldıraç bu ölçeklenmiş marjin-riski ile SL mesafesinden, pozisyon büyüklüğü
ölçeklenmiş cüzdan-riski ile SL mesafesi ve kaldıraçtan hesaplanır. `LEVERAGE_CAP`
(20x) ve `MAX_POSITION_PCT` (%20) her durumda üst sınırdır.

Bu kişiselleştirilmiş yatırım tavsiyesi değildir — işlemin R:R'ına göre
ölçeklenen mekanik bir hesaplamadır, kendi risk toleransına göre `scanner.py`
başındaki aralık sabitlerini değiştirebilirsin.

## Pozisyon takibi (SL/TP olayları + açık pozisyon özeti)
Bir sinyal gönderildikten sonra bot o pozisyonu `state.json`'da izlemeye devam
eder:
- **Anlık olay mesajı**: SL, TP1, TP2 veya TP3 seviyesine değinildiğinde
  (bir sonraki 4H mumlarına bakılarak) anında ayrı bir Telegram mesajı gelir,
  içinde o seviyedeki fiyat P&L% ve kaldıraçlı marjin P&L% bulunur.
- **Açık pozisyon özeti**: Her taramanın sonunda, henüz SL/TP3'e ulaşmamış
  tüm pozisyonlar için tek bir özet mesaj gönderilir — güncel fiyat, fiyat
  P&L%, kaldıraçlı marjin P&L%, ve kalan SL/TP seviyeleri.
- TP3'e ulaşan veya SL'e takılan pozisyonlar kapanmış sayılır, artık izlenmez.

## ICT Checklist stratejisi (ict_scanner.py)
`scanner.py`'daki stratejiden bağımsız, ikinci bir yöntem. 5 kriteri otomatik
puanlar (`ICT_MIN_SCORE = 3` ve üzeri skorlarda uyarı gönderir):

1. **HTF Bias Uyumu** — LTF'deki (4H) son yapı kırılımının yönü, günlük
   grafikteki son yapı kırılımıyla (bias) aynı mı?
2. **Kill Zone Zamanlaması** — kırılım mumu London (07-10 UTC) veya New York
   (12-15 UTC) kill zone'u ile kesişiyor mu?
3. **Liquidity Sweep** — en son Asya seansı (00-07 UTC) tepe/dibi süpürülüp
   fiyat geri döndü mü?
4. **MSS / Displacement** — kırılım mumunun gövdesi, önceki 20 muma göre en
   az `DISPLACEMENT_BODY_MULT` (1.5x) büyük mü?
5. **FVG / OTE Teması** — fiyat taze bir FVG içinde mi veya son bacağın
   0.618-0.786 (OTE) retracement bölgesinde mi?

Uyarı mesajında her kriter ✅/❌ ile gösterilir, skor ve fiyat bilgisi eklenir.
Parametreler `ict_scanner.py` başında ayarlanabilir: `ICT_MIN_SCORE`,
`LONDON_KZ`, `NY_KZ`, `DISPLACEMENT_BODY_MULT`, `LIQUIDITY_SWEEP_LOOKBACK`,
`OTE_SWING_LOOKBACK`.

## Sınırlamalar
- OB/FVG/BOS tespiti basitleştirilmiş bir yaklaşımdır, TradingView'daki Pine
  Script ile birebir aynı mantığı kullanır ama farklı piyasa koşullarında
  yanlış sinyal üretebilir.
- TP1/TP2/TP3 hesaplaması geçmiş pivot tepe/diplerine (likidite seviyelerine)
  dayanır, gelecekteki fiyat hareketini garanti etmez.
- Kaldıraç/pozisyon önerileri R:R'a göre ölçeklenir ama yine de varsayımlara
  dayanır; kendi sermayeni ve risk toleransını mutlaka göz önünde bulundur.
- ICT checklist kriterleri basitleştirilmiş, otomatikleştirilebilir
  yaklaşımlardır — ICT'nin tam metodolojisinin birebir yerine geçmez
  (özellikle kill zone ve OTE tespiti 4H çözünürlükte yaklaşıktır).
- Bu bir yatırım tavsiyesi değildir; gerçek parayla kullanmadan önce sinyalleri
  gözle/backtest ile doğrula.
