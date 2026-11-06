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

**Ricardo** steht hinter Cloudflare mit Ratenlimit: wer Seiten dicht hintereinander abruft, bekommt HTTP 429.
fundus ruft dort darum nur die Suchseite ab – ohne Parameter, so wie es Ricardos robots.txt erlaubt
(`Allow: /*/s/`, `Disallow: /*/s/*?`) –, nie einzelne Inseratseiten, und mit mindestens 60 s Abstand.
Nach einer Sperre ruht eine Plattform 1 h (oder so lange, wie der Server per `Retry-After` verlangt),
bei weiteren Sperren in Folge 2, 4 … bis 24 h. Bot-Schutz umgehen (Browser-Tarnung, Proxys, Captcha-Löser)
gehört bewusst nicht dazu.

**Bilder auswerten:** In jeder Suche lässt sich ein Prüfauftrag für die Fotos eingeben («Bestimme den Jahrgang
der Trust Bar»). Gemini sieht sich dann bis zu 4 Fotos pro passendem Angebot an und passt Fazit, Jahrgang,
Zustand, Anforderung und Score an. Danach bleibt das für neue Angebote dieser Suche aktiv, bis man es ausschaltet.

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

**Neupreis**: fundus sucht im Netz nach Shops und zusätzlich nach der Produkt- bzw. Serienseite bei toppreise.ch
(führt auch Auslaufmodelle, solange ein Shop sie hat), liest den Preis aus den Produktdaten, die KI prüft nur, ob
es genau das Produkt ist (Seiten mit vielen Produkten zählen nicht, bei Varianten gilt die der verlinkten Seite)
– eine andere Generation (M1/M4, «Pro 3»/«Pro 4») zählt nicht. Als Neupreis gilt das
günstigste passende *lieferbare* Angebot (ausverkaufte stehen markiert darunter), ausser es liegt unter 75 % des zweitgünstigsten (Fehlpreis). Allgemeine Suchen
(«sessel») bekommen keinen Neupreis. Von Hand eingetragene Neupreise haben Vorrang.

**Noch zu haben?** Was in den Suchergebnissen steht, ist online. Die besten passenden Treffer, die im Lauf nicht
mehr auftauchen, prüft fundus direkt (alle 6 h, bis 8 pro Suche); beim Klick auf ein Angebot (`/go/…`) prüft es
das Inserat nochmals, wenn die letzte Prüfung älter als 10 Minuten ist, und zeigt «nicht mehr verfügbar» oder
«reserviert», statt dich auf eine leere Seite zu schicken. Kleinanzeigen zeigt «Reserviert»/«Gelöscht» nur auf
der mobilen Seite im HTML (Desktop fügt es per Skript ein) – fundus prüft dort deshalb die mobile Seite. Alles, was
oben auf der Suchseite steht (beste Gebote, knapp dahinter), wird bei jedem Lauf geprüft, wenn die letzte Prüfung
älter als 2 h ist.

**Schon weg**: verschwundene passende Angebote bleiben gespeichert (dazu jede Preisänderung in `price_history`).
Unter dem Neupreis steht das günstigste und das beste verpasste Angebot, der mittlere Preis der verschwundenen
und das beste aktuelle zum Vergleich. Ricardo und eBay: Laufzeitende aus der Suche, nach
Ablauf gilt das Inserat ohne weitere Anfrage als weg; eBay prüft zusätzlich über die API. Auktionen sind auf der
Karte markiert (Gebote, aktuelles Gebot, Restzeit).

**Ort und Beschreibung**: Ricardo liefert Ort, Versandart und Laufzeit in der Suchkarte, die Beschreibung lädt
erst «Details» beim Anklicken (eine Anfrage, wie ein Besuch). eBay-Beschreibungen kommen über die API.

**Suchseite**: oben die besten Gebote (Pareto-Front: kein anderes Angebot ist zugleich günstiger und besser,
nach Aussortieren von Lockangeboten, nicht erfüllten Anforderungen, fragwürdigen Verkäufern und Reservierten).
Darunter zum Ausklappen: die klassische Liste, Preis-Wert-Karte, Vergleich der Finalisten, Preisleiter nach
Zustand, Lieferwege und Marktlage (`views.py`, `_views.html`). Offene Ansichten merkt sich der Browser.

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
