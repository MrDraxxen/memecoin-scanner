#!/bin/bash
PLIST="$HOME/Library/LaunchAgents/com.memecoin.scanner.plist"
launchctl unload "$PLIST" >/dev/null 2>&1
rm -f "$PLIST"
echo "Skener na Macu zastaven a odinstalovan. Tajne udaje jsou dal v ~/.memecoin-scanner (muzes smazat)."
