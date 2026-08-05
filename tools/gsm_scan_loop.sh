#!/usr/bin/env bash
# gsm_scan_loop.sh — periodic one-shot GSM cell snapshots -> CSV
#
# Uses the locally patched grgsm_scanner (SDCCH/8 scan disabled so it prints
# the cell table and exits instead of hanging on hopping channels).
#
# Output columns: timestamp,band,arfcn,freq_mhz,cid,lac,mcc,mnc,rssi
#
# Usage:
#   ./gsm_scan_loop.sh                 # GSM900, every 300s, gain 40, ppm 0
#   BAND=DCS1800 INTERVAL=600 GAIN=45 PPM=-2 ./gsm_scan_loop.sh
#   ONCE=1 ./gsm_scan_loop.sh          # single snapshot then exit

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCANNER="$HERE/grgsm_scanner"

BAND="${BAND:-GSM900}"
GAIN="${GAIN:-40}"
PPM="${PPM:-0}"
INTERVAL="${INTERVAL:-300}"          # seconds between snapshots
OUT="${OUT:-$HERE/../cells.csv}"     # CSV at project root
ONCE="${ONCE:-0}"

# Write header once.
if [[ ! -f "$OUT" ]]; then
  echo "timestamp,band,arfcn,freq_mhz,cid,lac,mcc,mnc,rssi" > "$OUT"
fi

snapshot() {
  local ts
  ts="$(date -Iseconds)"
  # One scan -> parse the upstream "ARFCN: .., Freq: ..M, CID: .., LAC: ..,
  # MCC: .., MNC: .., Pwr: .." line into CSV rows.
  "$SCANNER" -b "$BAND" -g "$GAIN" -p "$PPM" 2>/dev/null \
  | sed -nE "s/.*ARFCN:[[:space:]]*([0-9]+),.*Freq:[[:space:]]*([0-9.]+)M,.*CID:[[:space:]]*([0-9]+),.*LAC:[[:space:]]*([0-9]+),.*MCC:[[:space:]]*([0-9]+),.*MNC:[[:space:]]*([0-9]+),.*Pwr:[[:space:]]*(-?[0-9]+).*/${ts},${BAND},\1,\2,\3,\4,\5,\6,\7/p" \
  | tee -a "$OUT"
}

echo "Logging $BAND snapshots -> $OUT  (gain=$GAIN ppm=$PPM interval=${INTERVAL}s)"
if [[ "$ONCE" == "1" ]]; then
  snapshot
  exit 0
fi

while true; do
  echo "[$(date -Iseconds)] scanning..."
  snapshot || echo "  (scan produced no cells this cycle)"
  sleep "$INTERVAL"
done
