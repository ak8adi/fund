#!/bin/zsh
# Baut fundus.apk (Android-Hülle) ohne Gradle, nur mit JDK und Android SDK im Benutzerordner (wie die Zeit-App).
# Nur nötig, wenn sich an der Hülle selbst etwas ändert (android/) – die Oberfläche kommt vom Mac.
# Adresse: web.public_url aus config.yaml (Tailscale Serve, nur im eigenen Tailnet erreichbar).
# Danach auf dem Handy (Tailscale an) laden: <public_url>/app.apk
set -e
cd "${0:A:h}"
export JAVA_HOME="$HOME/Library/Android/jdk/Contents/Home"
SDK="$HOME/Library/Android/sdk"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"
ABLAGE="$HOME/Library/Application Support/fundus"
OUT=build
rm -rf $OUT && mkdir -p $OUT/res $OUT/classes $OUT/assets "$ABLAGE"

# Adresse aus config.yaml
ADRESSE=$(cd .. && .venv/bin/python -c "from fundus.config import get_config; print(get_config()['web'].get('public_url') or '')" 2>/dev/null)
[[ "$ADRESSE" == https://*.ts.net* ]] || { echo "web.public_url in config.yaml fehlt (Tailscale-Adresse, https://….ts.net)"; exit 1; }
ADRESSE=${ADRESSE%/}
print -r -- "$ADRESSE" > $OUT/assets/adresse.txt
HOST=${${ADRESSE#https://}%%/*}
sed "s/FUNDUS_HOST/$HOST/" AndroidManifest.xml > $OUT/AndroidManifest.xml

# Symbole aus mac/symbol.swift (gleiches Zeichen wie die Mac-App)
cp -R res $OUT/res-quelle
for dichte groesse in mdpi 48 hdpi 72 xhdpi 96 xxhdpi 144 xxxhdpi 192; do
  mkdir -p $OUT/res-quelle/mipmap-$dichte
  swift ../mac/symbol.swift $OUT/res-quelle/mipmap-$dichte/ic_launcher.png $groesse
  swift ../mac/symbol.swift $OUT/res-quelle/mipmap-$dichte/ic_launcher_vordergrund.png $(( groesse * 9 / 4 )) vordergrund
done

# Ressourcen, Code, dex
$BT/aapt2 compile --dir $OUT/res-quelle -o $OUT/res.zip
$BT/aapt2 link -o $OUT/fundus-roh.apk -I "$JAR" --manifest $OUT/AndroidManifest.xml -A $OUT/assets $OUT/res.zip
"$JAVA_HOME/bin/javac" -source 8 -target 8 -Xlint:-options -classpath "$JAR" -d $OUT/classes src/ch/fundus/app/*.java
$BT/d8 --min-api 26 --lib "$JAR" --output $OUT $OUT/classes/ch/fundus/app/*.class
(cd $OUT && zip -q fundus-roh.apk classes.dex)
$BT/zipalign -f 4 $OUT/fundus-roh.apk $OUT/fundus-ausgerichtet.apk

# Signieren: Schlüssel einmalig erzeugen und behalten (sonst lassen sich Updates nicht installieren)
KS="$ABLAGE/android.keystore"
PW="$ABLAGE/android.passwort"
if [[ ! -f "$KS" ]]; then
  openssl rand -base64 24 > "$PW" && chmod 600 "$PW"
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KS" -storepass "$(cat $PW)" -keypass "$(cat $PW)" \
    -alias fundus -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=fundus" >/dev/null 2>&1
  chmod 600 "$KS"
fi
$BT/apksigner sign --ks "$KS" --ks-pass file:"$PW" --ks-key-alias fundus --out fundus.apk $OUT/fundus-ausgerichtet.apk
$BT/apksigner verify fundus.apk
rm -rf $OUT
echo "Fertig: $PWD/fundus.apk ($ADRESSE)"
echo "Auf dem Handy (Tailscale an) öffnen: $ADRESSE/app.apk"
