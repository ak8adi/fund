# fundus

Private Gebrauchtwaren-Suche über mehrere Plattformen: Suchbegriff einmal eingeben, fundus prüft 1–2 Mal pro Tag
**Ricardo, Tutti, Anibis, Kleinanzeigen, Willhaben, Markt.de, Blocket und eBay (CH, DE, FR, IT, AT …)**,
lässt jedes Inserat von einer KI (Gemini) prüfen und meldet nur die wirklich guten Angebote per **ntfy** aufs Handy.

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
- Blockt eine Plattform (Captcha, HTTP 403/429), wird sie für 24 h pausiert. Die Statuszeile auf der
  Startseite zeigt pro Plattform: grün = liefert, gelb = 0 Treffer, rot = Fehler/pausiert, grau = aus.

| Plattform | Weg | Verkäuferdaten |
|---|---|---|
| eBay | offizielle Browse API | viel (Bewertungen, Anzahl) |
| Ricardo | Suchseite, eingebettetes JSON | je nach Seite |
| Tutti | Suchseite, eingebettetes JSON | wenig |
| Anibis | Suchseite, eingebettetes JSON | wenig |
| Kleinanzeigen | Suchseite, HTML-Parsing | wenig |
| Willhaben | Suchseite, JSON / HTML | wenig |
| Markt.de | Suchseite, HTML-Parsing | wenig |
| Blocket | Suchseite, HTML-Parsing | wenig |

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

## Was mit jedem Inserat passiert

1. **Vorfilter** ohne KI: alle Suchwörter im Titel (Modellnummern zwingend), keine Ausschlusswörter im Titel
   (es zählen Wortanfänge: «dell» trifft «Dell XPS», nicht «Modell»), Preis innerhalb min/max.
2. **KI-Fakten** (Gemini, Stapel à 20): ist es das gesuchte Produkt oder Zubehör, Zustand, Jahrgang, Versand,
   Set-Inhalt und Einzelpreis, Versandgrösse, deine Anforderungen (Freitext, z.B. «nur 3-rädrig») und
   Hinweise auf Betrug. Die KI bewertet keine Preise.
3. **Lieferweg** (`delivery.py`): Postversand in der Schweiz, DHL an Paket4you in Jestetten inkl. Gebühr und
   MWST über 150 CHF, oder Abholung mit Fahrzeit ab Winterthur (OpenStreetMap). Ergibt den Endpreis bis zu dir.
4. **Deal-Score** (`scoring.py`): Endpreis gegen den Marktpreis (Median der passenden Treffer), angepasst an
   Zustand und Jahrgang, gedeckelt durch den Neupreis. 50 = Marktpreis, 90 = halber Preis. Stufen S/A/B/C.
5. **Meldung** per ntfy ab Score 65 (A), mit Bild, Endpreis, Lieferweg und Einschätzung.

**Neupreis**: fundus sucht im Netz nach Shops, liest den Preis aus den Produktdaten der Shopseite, die KI prüft
nur, ob es genau das Produkt ist. Als Neupreis gilt das günstigste passende Angebot (mit Link). Von Hand
eingetragene Neupreise haben Vorrang.

**Notizfeld** pro Inserat: Antwort des Verkäufers oder eigene Notiz – die KI bewertet das Inserat danach neu.
Steht «Betrug», «Fake» o.ä. darin, wird es sofort ausgeblendet.

## Vertrauens-Check

Regelbasiert, ohne KI, mit Begründung in einer Zeile:

- 🟢 **Vertrauenswürdig** – viele, gute Bewertungen, älteres Konto
- ⚪ **Keine Auffälligkeiten** – nichts Negatives, aber auch kaum Daten (typisch Tutti/Anibis)
- 🟡 **Genauer anschauen** – z.B. kein Foto, kaum Bewertungen
- 🔴 **Vorsicht** – z.B. Konto wenige Tage alt, «nur WhatsApp», «Vorauskasse», «PayPal Freunde»

Auffällig billige Angebote (unter 35 % des Marktpreises) drückt der Deal-Score. Ein Warnsignal-Detektor,
keine Garantie.

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
