#!/bin/bash
# SMC botunu bu makinede çalıştırır.
#
# Neden GitHub Actions değil: Binance'in GERÇEK vadeli ucu (fapi.binance.com)
# Actions sunucularından HTTP 451 (bölge kısıtı) döndürüyor; ölçüldü.
# Testnet ucu farklı alan adı olduğu için orada sorun çıkmıyordu.
#
# Depo bilerek Desktop DIŞINDA (~/smc-signal-bot): macOS Desktop klasörünü
# koruyor ve launchd oradaki dosyaları okuyamıyor ("Operation not permitted";
# ölçüldü — launchd ev dizinini okuyabiliyor, Desktop'ı okuyamıyor).
# Masaüstündeki klasör buraya işaret eden bir kısayol.

set -uo pipefail
cd "$(dirname "$0")"

# --- döngü modu ---
# "--loop" ile çalışırsa: tara, SMC_SLEEP saniye bekle, tekrar tara.
# Bekleme tarama BİTTİKTEN sonra başlar, yani koşular üst üste binmez.
SLEEP_SN="${SMC_SLEEP:-300}"

# TEK KOŞU KİLİDİ. Zamanlayıcı ile elle başlatılan koşu çakışabiliyor
# (ölçüldü: aynı 5 kurulum iki kez işlendi). Gerçek parada bu, her sinyal
# için iki emir demek. mkdir POSIX'te atomik olduğu için kilit olarak
# kullanılıyor; sahibi ölmüşse kilit devralınır.
KILIT="$PWD/.run.lock"
kilit_al() {
  if ! mkdir "$KILIT" 2>/dev/null; then
    ESKI=$(cat "$KILIT/pid" 2>/dev/null || echo "")
    if [ -n "$ESKI" ] && kill -0 "$ESKI" 2>/dev/null; then
      echo "$(date '+%H:%M:%S') başka bir tarama sürüyor (pid $ESKI), atlandı." \
        >> "logs/$(date +%Y-%m-%d).log" 2>/dev/null || true
      return 1
    fi
    rm -rf "$KILIT"; mkdir "$KILIT" 2>/dev/null || return 1
  fi
  echo $$ > "$KILIT/pid"
  return 0
}
trap 'rm -rf "$KILIT"' EXIT INT TERM

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

tarama_yap() {
  kilit_al || return 0
  mkdir -p logs
  local LOG="logs/$(date +%Y-%m-%d).log"
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
    "$PY" -u smc_scanner.py
  } >> "$LOG" 2>&1
  local KOD=$?
  rm -rf "$KILIT"
  if [ $KOD -ne 0 ]; then
    echo "tarama hata verdi (çıkış kodu $KOD), son satırlar:" >&2
    tail -20 "$LOG" >&2
    hata_bildir "Log: $(pwd)/$LOG (çıkış kodu $KOD)"
  fi
  return $KOD
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

if [ "${1:-}" = "--loop" ]; then
  echo "döngü modu: tarama bitince ${SLEEP_SN}s bekleyip tekrar başlayacak."
  while true; do
    tarama_yap || true
    sleep "$SLEEP_SN"
  done
fi

tarama_yap
exit $?
