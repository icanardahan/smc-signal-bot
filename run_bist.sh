#!/bin/bash
# BIST tarayıcısını döngüde çalıştırır.
#
# Seans kontrolü tarayıcının içinde: BIST kapalıyken (hafta içi 10:00-18:00
# TRT dışı) hiçbir şey yapmadan çıkar. Bu yüzden döngü 7/24 dönebilir,
# kapalıyken maliyeti yok.

set -uo pipefail
cd "$(dirname "$0")"

SLEEP_SN="${BIST_SLEEP:-900}"      # 15 dk; 1H barlarda daha sık taramak gereksiz

KILIT="$PWD/.bist.lock"
kilit_al() {
  if ! mkdir "$KILIT" 2>/dev/null; then
    ESKI=$(cat "$KILIT/pid" 2>/dev/null || echo "")
    if [ -n "$ESKI" ] && kill -0 "$ESKI" 2>/dev/null; then return 1; fi
    rm -rf "$KILIT"; mkdir "$KILIT" 2>/dev/null || return 1
  fi
  echo $$ > "$KILIT/pid"; return 0
}
trap 'rm -rf "$KILIT"' EXIT INT TERM

tarama_yap() {
  set -a; source .env; set +a      # ayarları HER turda yeniden oku
  kilit_al || return 0
  mkdir -p logs
  local LOG="logs/bist-$(date +%Y-%m-%d).log"
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
    "${SMC_PYTHON:-python3}" -u bist_scanner.py
  } >> "$LOG" 2>&1
  local KOD=$?
  rm -rf "$KILIT"
  [ $KOD -ne 0 ] && tail -15 "$LOG" >&2
  return $KOD
}

if [ "${1:-}" = "--loop" ]; then
  echo "BIST döngüsü: her turdan sonra ${SLEEP_SN}s bekleme"
  while true; do
    tarama_yap || true
    sleep "$SLEEP_SN"
  done
fi
tarama_yap
