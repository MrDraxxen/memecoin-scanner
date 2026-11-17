#!/bin/bash
# Instalace skeneru na Mac: spousteni kazdych 10 minut pres launchd.
set -e
DIR="$HOME/memecoin-scanner"
DATA="$HOME/.memecoin-scanner"
PLIST="$HOME/Library/LaunchAgents/com.memecoin.scanner.plist"

echo ""
echo "=== Memecoin skener – instalace na Mac ==="
echo ""

if ! /usr/bin/python3 -c "import sys" >/dev/null 2>&1; then
  echo "Chybi Python. Spust:  xcode-select --install"
  echo "Po dokonceni instalace spust tento skript znovu."
  exit 1
fi
if [ ! -f "$DIR/bot/scan.py" ]; then
  echo "Nenasel jsem $DIR/bot/scan.py – nejdriv stahni repozitar (viz navod)."
  exit 1
fi

mkdir -p "$DATA"
chmod 700 "$DATA"

if [ -f "$DATA/secrets.env" ]; then
  echo "Tajne udaje uz existuji ($DATA/secrets.env), preskakuji."
else
  echo "Vloz udaje (pri psani se nezobrazuji, to je v poradku). Kazdy potvrd Enterem."
  read -r -s -p "TELEGRAM_BOT_TOKEN: " TG_TOKEN; echo
  read -r -p "TELEGRAM_CHAT_ID: " TG_CHAT
  read -r -s -p "HELIUS_API_KEY (muzes nechat prazdne): " HELIUS; echo
  umask 077
  {
    echo "TELEGRAM_BOT_TOKEN=\"$(echo "$TG_TOKEN" | tr -d '[:space:]')\""
    echo "TELEGRAM_CHAT_ID=\"$(echo "$TG_CHAT" | tr -d '[:space:]')\""
    echo "HELIUS_API_KEY=\"$(echo "$HELIUS" | tr -d '[:space:]')\""
  } > "$DATA/secrets.env"
  chmod 600 "$DATA/secrets.env"
  echo "Ulozeno do $DATA/secrets.env (jen pro tvuj ucet)."
fi

chmod +x "$DIR/mac/run.sh"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.memecoin.scanner</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/mac/run.sh</string></array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DATA/launchd.log</string>
  <key>StandardErrorPath</key><string>$DATA/launchd.log</string>
</dict>
</plist>
PL

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"

echo ""
echo "Hotovo. Prvni beh se prave spustil, za 1-2 minuty prijde zprava na Telegram."
echo "Dal pobezi kazdych 10 minut, kdyz je Mac zapnuty a neuspany."
echo ""
echo "Prubeh:     tail -n 30 $DATA/scan.log"
echo "Zastavit:   launchctl unload $PLIST"
echo ""
