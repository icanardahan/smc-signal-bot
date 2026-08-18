# Zamanlayıcı (macOS launchd)

İki ajan var:

| Dosya | İş |
|---|---|
| `com.icanardahan.smcbot.plist` | Saatte bir `run_local.sh` çalıştırır |
| `com.icanardahan.smcbot.awake.plist` | `caffeinate -s -i` ile boşta uykuyu engeller |

## Kurulum / yeniden kurulum

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
for f in ~/Library/LaunchAgents/com.icanardahan.smcbot*.plist; do
  launchctl unload "$f" 2>/dev/null; launchctl load "$f"
done
launchctl list | grep smcbot
```

## Durdurma

```bash
for f in ~/Library/LaunchAgents/com.icanardahan.smcbot*.plist; do launchctl unload "$f"; done
```

Bu yalnızca taramayı durdurur. Binance'teki açık pozisyonlar ve stop
emirleri yerinde kalır; onları borsadan kapatmak gerekir.

## Uyku hakkında bilinmesi gerekenler

- `caffeinate` **boşta** uykuyu engeller. **Kapak kapanırsa Mac yine uyur** —
  bunu engellemenin yolu yok. Kapağı açık bırak.
- Mac'i uyandırmak (`pmset schedule/repeat`) yönetici parolası ister; bu
  kurulum bilerek parolasız tutuldu.
- Depo Desktop DIŞINDA olmalı: macOS Desktop'ı koruyor ve launchd oradaki
  dosyaları okuyamıyor ("Operation not permitted"). `~/Desktop/smc-signal-bot`
  buraya işaret eden bir kısayol.
- Mac yine de uyursa: uyanınca launchd kaçırılan koşuyu bir kez çalıştırır ve
  tarayıcı o sırada oluşmuş TÜM 4H barlarını sırayla işler (dolum, stop, süre
  aşımı hepsi yakalanır). Uyku boyunca stop borsada DURUR ama SÜRÜKLENMEZ.
