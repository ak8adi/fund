#!/bin/bash
# Installiert fundus als Hintergrunddienst auf dem Mac (startet beim Login, startet bei Absturz neu).
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ch.inderfab.fundus"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .
[ -f config.yaml ] || cp config.example.yaml config.yaml
mkdir -p data

sed -e "s#__DIR__#$DIR#g" -e "s#__LABEL__#$LABEL#g" deploy/fundus.plist.template > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

PORT=$(grep -E '^\s+port:' config.yaml | head -1 | awk '{print $2}')
echo "fundus läuft: http://127.0.0.1:${PORT:-8100}"
echo "Log: $DIR/data/fundus.log"
echo "Stoppen: launchctl bootout gui/$(id -u)/$LABEL"
