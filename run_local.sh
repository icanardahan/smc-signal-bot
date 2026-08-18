#!/bin/bash
# SMC botunu bu makinede çalıştırır.
#
# Neden GitHub Actions değil: Binance'in GERÇEK vadeli ucu (fapi.binance.com)
# Actions sunucularından HTTP 451 (bölge kısıtı) döndürüyor; ölçüldü.
# Testnet ucu farklı alan adı olduğu için orada sorun çıkmıyordu.
#
# launchd bu betiği Desktop'tan çalıştırabilmek için /bin/bash'e
# "Tam Disk Erişimi" izni ister (macOS Desktop klasörünü korur).

set -uo pipefail
cd "$(dirname "$0")"

hata_bildir() {
  # Tarama çökerse sessiz kalmasın. GitHub Actions'taki failure() adımının
  # yerel karşılığı; yereldeki tek uyarı kanalı bu.
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || return 0
  "${PY:-python3}" - "$1" <<'PY' 2>/dev/null
import json, os, sys, urllib.request
t, c = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
if t and c:
    msg = ("🚨 <b>SMC taraması BAŞARISIZ</b>\nBu koşuda sinyal üretilmedi "
           "ve açık pozisyonların stopu SÜRÜKLENMEDİ.\n" + sys.argv[1])
    d = json.dumps({"chat_id": c, "text": msg, "parse_mode": "HTML"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{t}/sendMessage", data=d,
        headers={"Content-Type": "application/json"}), timeout=15).read()
PY
}

if [ ! -f .env ]; then
  echo "HATA: .env yok. '.env.example' dosyasını .env olarak kopyalayıp doldur." >&2
  exit 1
fi
set -a; source .env; set +a

if [ -z "${BINANCE_API_KEY:-}" ] || [[ "${BINANCE_API_KEY}" == buraya_* ]]; then
  echo "HATA: .env içindeki API anahtarları doldurulmamış." >&2
  exit 1
fi

PY="${SMC_PYTHON:-python3}"
mkdir -p logs
LOG="logs/$(date +%Y-%m-%d).log"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
  "$PY" -u smc_scanner.py
} >> "$LOG" 2>&1
KOD=$?

if [ $KOD -ne 0 ]; then
  echo "tarama hata verdi (çıkış kodu $KOD), son satırlar:" >&2
  tail -20 "$LOG" >&2
  hata_bildir "Log: $(pwd)/$LOG (çıkış kodu $KOD)"
fi
exit $KOD
