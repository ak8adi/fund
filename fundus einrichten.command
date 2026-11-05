#!/bin/bash
# Baut «fundus.app» in diesem Ordner (eigenes Fenster, Symbol im Dock) und legt sie ins Dock.
# Der Server selbst läuft weiter als LaunchAgent «ch.fundus.serve» auf Port 8100.
# Nach Änderungen an mac/ oder nach dem Verschieben des Ordners einfach nochmals ausführen.
set -e
cd "$(dirname "$0")"
FUND="$(pwd)"
APP="$FUND/fundus.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! command -v swiftc >/dev/null; then
  echo "Zum Bauen fehlen die Entwicklerwerkzeuge von Apple."
  echo "Bitte im Terminal «xcode-select --install» ausführen und danach dieses Skript nochmals öffnen."
  exit 1
fi

osascript -e 'tell application id "ch.fundus.app" to quit' >/dev/null 2>&1 || true

echo "Baue fundus.app …"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$TMP/fundus.iconset"
swiftc -O -target "$(uname -m)-apple-macos13.0" mac/main.swift -o "$APP/Contents/MacOS/fundus"

for s in 16 32 128 256 512; do
  swift mac/symbol.swift "$TMP/fundus.iconset/icon_${s}x${s}.png" $s
  swift mac/symbol.swift "$TMP/fundus.iconset/icon_${s}x${s}@2x.png" $((s * 2))
done
iconutil -c icns "$TMP/fundus.iconset" -o "$APP/Contents/Resources/fundus.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>ch.fundus.app</string>
  <key>CFBundleName</key><string>fundus</string>
  <key>CFBundleDisplayName</key><string>fundus</string>
  <key>CFBundleExecutable</key><string>fundus</string>
  <key>CFBundleIconFile</key><string>fundus</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>FundusPort</key><string>8100</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST
codesign --force --sign - "$APP"
touch "$APP"

# Ins Dock legen (nur wenn noch nicht drin)
if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q '"bundle-identifier" = "ch.fundus.app"'; then
  defaults write com.apple.dock persistent-apps -array-add \
    "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://$APP/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"
  killall Dock
fi

open "$APP"
echo "Fertig: fundus liegt im Dock und öffnet sich im eigenen Fenster."
