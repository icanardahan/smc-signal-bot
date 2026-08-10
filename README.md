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

## Zorunlu şartlar (her iki strateji için)
Checklist/onay mantığından bağımsız olarak, aşağıdaki şart sağlanmazsa sinyal
**hiç gönderilmez**:

- **Minimum R:R** — `TP1_RR = |TP1 - Giriş| / |Giriş - SL|` hesaplanır;
  `MIN_TP1_RR` (varsayılan **1.5**) altındaysa işlem "Düşük R:R (asimetrik
  değil)" gerekçesiyle reddedilir.
- **Geometri tutarlılığı** — long'da `SL < Giriş < TP1`, short'ta
  `TP1 < Giriş < SL` olmalı.

## Pozisyon takibi (SL/TP olayları + açık pozisyon özeti)
Bir sinyal gönderildikten sonra bot o pozisyonu `state.json`'da izlemeye devam
eder:
- **Anlık olay mesajı**: SL, TP1, TP2 veya TP3 seviyesine değinildiğinde
  (bir sonraki 4H mumlarına bakılarak) anında ayrı bir Telegram mesajı gelir,
  içinde o seviyedeki fiyat P&L% ve kaldıraçlı marjin P&L% bulunur.
- **Açık pozisyon özeti**: Her taramanın sonunda, henüz SL/TP3'e ulaşmamış
  tüm pozisyonlar için tek bir özet mesaj gönderilir — güncel fiyat, fiyat
  P&L%, kaldıraçlı marjin P&L%, ve kalan SL/TP seviyeleri.
- **Zaman aşımı**: Pozisyon açıldıktan sonra `POSITION_TIMEOUT_HOURS`
  (varsayılan **12 saat**) boyunca ne SL'e ne TP3'e ulaşmazsa otomatik
  "Zaman Aşımı" olarak işaretlenir, kapatılır ve parite yeniden taramaya
  dahil edilir. Telegram'a o anki P&L ile bildirim gider.
- TP3'e ulaşan, SL'e takılan veya zaman aşımına uğrayan pozisyonlar kapanmış
  sayılır, artık izlenmez ve o sembol+yönde yeni sinyal alınabilir.

## ICT Checklist stratejisi (ict_scanner.py)
`scanner.py`'daki stratejiden bağımsız, ikinci bir yöntem. "Altın Kural"
mantığıyla çalışır: 5 kriter **çekirdek** ve **onay** olarak ikiye ayrılır,
uyarı sadece çekirdeğin tamamı + onaylardan en az biri sağlandığında gider
(toplam skor en az 4/5):

Yapı kırılımı 4H'de bulunur; ancak **likidite avı, displacement ve giriş
tetiği 15 dakikalık veride, gerçek saatleriyle** aranır — çünkü bir kill zone
penceresi 3 saattir ve 4H mumla ölçülemez. Kill zone saatleri **New York yerel
saatiyle** tanımlıdır, böylece yaz/kış saati (EST/EDT) geçişinde pencere UTC'de
kaymaz.

**Çekirdek (hepsi sağlanmalı):**
1. **Kill Zone Zamanlaması** — likidite avı **ve** displacement, London
   (02:00-05:00 NY) veya New York (07:00-10:00 NY) penceresi **içinde**
   gerçekleşti mi? Mumun ne zaman açıldığı değil, hareketin ne zaman olduğu
   ölçülür.
2. **Liquidity Sweep** — Asya seansının (20:00-00:00 NY) tepe/dibi süpürülüp
   fiyat geri döndü mü? (Judas swing)
3. **MSS / Displacement** — sweep'ten sonra, gövdesi önceki 20 mumun
   ortalamasının en az `DISPLACEMENT_BODY_MULT` (1.5x) katı olan yönlü bir
   kırılım mumu oluştu mu?

**Onaylar (en az 1 sağlanmalı):**
4. **HTF Bias Uyumu** — 4H'deki son yapı kırılımının yönü, günlük grafikteki
   son yapı kırılımıyla (bias) aynı mı?
5. **FVG / OTE Teması** — fiyat displacement bacağının FVG'sine veya
   0.618-0.786 (OTE) bölgesine geri çekildi mi?

Uyarı mesajında SL/TP1/TP2/TP3 (R:R ile), her kriter ✅/❌ ile ayrı ayrı
çekirdek/onay bölümlerinde, ayrıca sweep ve displacement'ın NY saatiyle
gerçekleşme zamanı gösterilir. Parametreler `ict_scanner.py` başında
ayarlanabilir: `CORE_CRITERIA`, `CONFIRM_CRITERIA`, `MIN_CONFIRMATIONS`,
`LONDON_KZ_NY`, `NY_KZ_NY`, `ASIAN_SESSION_NY`, `LTF_KZ_INTERVAL`,
`KZ_LOOKBACK_CANDLES`, `DISPLACEMENT_BODY_MULT`.

## Sınırlamalar
- OB/FVG/BOS tespiti basitleştirilmiş bir yaklaşımdır, TradingView'daki Pine
  Script ile birebir aynı mantığı kullanır ama farklı piyasa koşullarında
  yanlış sinyal üretebilir.
- TP1/TP2/TP3 hesaplaması geçmiş pivot tepe/diplerine (likidite seviyelerine)
  dayanır, gelecekteki fiyat hareketini garanti etmez.
- Kaldıraç/pozisyon önerileri R:R'a göre ölçeklenir ama yine de varsayımlara
  dayanır; kendi sermayeni ve risk toleransını mutlaka göz önünde bulundur.
- ICT checklist kriterleri basitleştirilmiş, otomatikleştirilebilir
  yaklaşımlardır — ICT'nin tam metodolojisinin birebir yerine geçmez.
  Sweep/displacement/giriş 15 dakikalık veride ölçülür; ICT'nin önerdiği
  1M-5M çözünürlükten daha kabadır.
- Bu bir yatırım tavsiyesi değildir; gerçek parayla kullanmadan önce sinyalleri
  gözle/backtest ile doğrula.
