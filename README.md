# fundus

Private Gebrauchtwaren-Suche über mehrere Plattformen: Suchbegriff einmal eingeben, fundus prüft 1–2 Mal pro Tag
**Ricardo, Tutti, Anibis, Kleinanzeigen, Willhaben, Markt.de und eBay (CH, DE, FR, IT, AT …)** und meldet neue Inserate per **ntfy** aufs Handy –
mit einer kurzen Ampel zur Vertrauenswürdigkeit des Anbieters.

- Weboberfläche (FastAPI + HTMX), ein Nutzer, kein Login
- SQLite in `data/fundus.db`
- Jede Plattform und jedes eBay-Land einzeln ein-/ausschaltbar, pro Suche zusätzlich einschränkbar
- Läuft auf dem Mac (launchd) oder in Docker

## Start auf dem Mac

```bash
git clone git@github.com:inderfab/fundus.git && cd fundus
./deploy/install-mac.sh          # venv, config.yaml, Autostart beim Login
open http://127.0.0.1:8100
```

Danach in `config.yaml` mindestens setzen:

1. **ntfy-Topic** – langer, zufälliger Name (Topics auf ntfy.sh sind öffentlich). In der ntfy-App
   (iOS/Android/Mac) dasselbe Topic abonnieren. Test: `fundus ntfy-test`
2. **eBay-Schlüssel** – kostenlos auf [developer.ebay.com](https://developer.ebay.com) → *Application Keys* →
   Production *App ID* und *Cert ID*. Ohne Schlüssel bleibt eBay einfach aus.

Nach Änderungen an `config.yaml`: `launchctl kickstart -k gui/$(id -u)/ch.inderfab.fundus`

Ohne Autostart, zum Ausprobieren: `pip install -e . && fundus serve`

## Wie abgefragt wird

- **Pro Suche und Quelle genau eine Anfrage**: erste Ergebnisseite, neueste zuerst. 5 Suchen × 8 Quellen
  = 40 Anfragen pro Lauf, verteilt mit 4–11 s Pause.
- Zeitplan standardmässig 07:40 und 18:20 mit ±25 min Zufallsversatz. War der Mac im Ruhezustand, wird
  der Lauf beim Aufwachen nachgeholt (bis 3 h).
- **Erster Lauf einer neuen Suche meldet nichts**, er merkt sich nur den aktuellen Bestand. Gemeldet wird,
  was danach neu dazukommt.
- Blockt eine Plattform (Captcha, HTTP 403/429), wird sie für den Rest des Laufs übersprungen und der
  Fehler unter *Plattformen* angezeigt.

| Plattform | Weg | Verkäuferdaten |
|---|---|---|
| eBay | offizielle Browse API | viel (Bewertungen, Anzahl) |
| Ricardo | Suchseite, eingebettetes JSON | je nach Seite |
| Tutti | Suchseite, eingebettetes JSON | wenig |
| Anibis | Suchseite, eingebettetes JSON | wenig |
| Kleinanzeigen | Suchseite, HTML-Parsing | wenig |
| Willhaben | Suchseite, JSON / HTML | wenig |
| Markt.de | Suchseite, HTML-Parsing | wenig |

Die Scraping-Adapter lesen nicht HTML-Elemente, sondern das JSON, das die Seiten für sich selbst
mitliefern (`__NEXT_DATA__`, `ld+json`). Das übersteht Layout-Änderungen besser. Die Such-URLs stehen in
`config.yaml` und lassen sich ohne Codeänderung anpassen.

### Wenn eine Plattform nichts mehr findet

```bash
fundus probe tutti "kite 9m"          # zeigt URL, erkannte Inserate und Verkäuferdaten
fundus probe ricardo "x100" --save    # speichert die Rohseite unter data/probe-ricardo.html
fundus probe ebay:EBAY_FR "kitesurf"
```

Hilft das nicht: im Browser die Suchseite öffnen, DevTools → Netzwerk → nach JSON-Antworten schauen.
Wird nur mit echtem Browser geladen: `pip install -e '.[browser]' && playwright install chromium` und für
die Plattform `fetcher: browser` setzen.

## Vertrauens-Check

Regelbasiert, ohne KI, mit Begründung in einer Zeile:

- 🟢 **Vertrauenswürdig** – viele, gute Bewertungen, älteres Konto
- ⚪ **Keine Auffälligkeiten** – nichts Negatives, aber auch kaum Daten (typisch Tutti/Anibis)
- 🟡 **Genauer anschauen** – z.B. kein Foto, kaum Bewertungen, Preis deutlich unter Median
- 🔴 **Vorsicht** – z.B. Preis > 65 % unter dem Median der gleichen Suche, Konto wenige Tage alt,
  «nur WhatsApp», «Vorauskasse», «PayPal Freunde»

Ein Warnsignal-Detektor, keine Garantie.

## Docker / Server

```bash
mkdir -p data && cp config.example.yaml data/config.yaml   # anpassen
docker compose up -d --build
```

Alles liegt in `./data` (Konfiguration und Datenbank). Umzug vom Mac auf den Server: `data/` kopieren.

## Neue Plattform

Adapter-Klasse in `fundus/adapters/` schreiben (`search()` gibt `Listing`-Objekte zurück) und in
`fundus/adapters/__init__.py` eintragen. Für Seiten mit eingebettetem JSON reicht meist eine Unterklasse von
`ScrapedAdapter` mit `base_url` und `build_url()`.

Geplant: Facebook Marketplace (braucht Login), optionaler Umkreisfilter.

## Hinweis

Nur für den privaten Gebrauch gedacht. Die AGB der Plattformen untersagen automatisierte Abfragen meist;
fundus hält die Last bewusst minimal.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
