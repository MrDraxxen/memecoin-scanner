#!/bin/bash
# Spousti skener jednou. Vola ho macOS (launchd) kazdych 10 minut.
DIR="$HOME/memecoin-scanner"
DATA="$HOME/.memecoin-scanner"
LOG="$DATA/scan.log"
mkdir -p "$DATA"
cd "$DIR" || exit 1

# Nacist tajne udaje (token, chat id, helius klic)
set -a
. "$DATA/secrets.env"
set +a
export SCANNER_STATE="$DATA/seen.json"

# Stahnout nejnovejsi verzi bota (zmeny, ktere udela Claude na GitHubu)
git pull -q --ff-only >/dev/null 2>&1

# Log drzet maly (poslednich ~2000 radku)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

echo "===== $(date '+%d.%m.%Y %H:%M:%S') =====" >> "$LOG"
/usr/bin/python3 bot/scan.py >> "$LOG" 2>&1
