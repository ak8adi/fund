# Arbeitsstand (zum nahtlosen Weitermachen)

**Hauptziel, an dem alles gemessen wird:** Welches sind jetzt die besten Angebote für mein Produkt zum Preis, den es
mich wirklich kostet – und lohnt sich gebraucht gegenüber neu überhaupt? Melde mir nur das.

Arbeitsweise: stufenweise, grösster Punkt zuerst; nichts verschlimmbessern (Wünsche nicht zu wörtlich nehmen);
Texte in der App kurz und klar. Nach jeder fertigen Änderung (Tests grün) automatisch committen und pushen. Dienst nach Änderungen neu starten:
`launchctl kickstart -k gui/$(id -u)/ch.fundus.serve` (vorher `sqlite3 data/fundus.db ".backup …"`).
Tests: `.venv/bin/python -m pytest -q`.

## Erledigt (11.11.2026, nicht committet)

- Auktionen: Gebot ist kein Preis (`trust.bidding`), eigener Bereich, Alarm vor Ablauf (`auctions.py`), App 1.3
- ntfy stillgelegt (`ntfy.enabled: false`), App-Meldungen bleiben
- Vorfilter: «16GB» ≠ «16», «mac book» = «MacBook», Preisrahmen gilt für Bestand
- Neupreis: toppreise.ch + geizhals.de direkt, Nachfolger/Auslaufmodell, bewährte Shop-URLs, Urteil «lohnt sich gebraucht?»
- Einfuhr-MWST separat ausgewiesen, nie im Endpreis; Fahrt nach Jestetten nicht eingerechnet (Entscheid Nutzer)
- Kleinanzeigen/Willhaben: tiefer erster Abruf, Nachblättern; Kleinanzeigen mit Preisrahmen

## Erledigt (11.11.2026 abends, nicht committet) – Dienst läuft damit, App 1.4 gebaut (android/fundus.apk)

- Auktions-Alarm: abgelaufene Meldungen nie zustellen (app_notices.expires_at), Uhrzeit im Titel, exakter Wecker
  und rascher Neuversuch in der App 1.4, Option `ntfy.auctions`
- Übersicht schnell: Orte im Speicher (db.geo_get), views.build zwischengespeichert pro Datenstand (db.version)
- Neu kaufen: Liste aller Shops (approx = ab-Preis bei genannter Grösse), günstigstes aus der Schweiz (head.local),
  Nachfolger/erneuert als eigene Abschnitte, Shop von Hand ergänzen (kv shops:<id>), zweite Suchformulierung
- Gruppen (searches.group_name, views.grouped) und «Für Gemini kopieren» (views.gemini_prompt, /api/gemini)
- Erklärtexte gestrafft (Suchseite, Neu kaufen, Analytics, Formulare, Einstellungen)

Nachtrag 12.11.: Sonderversionen (Foil …) und «Modellnummer als Grösse» beim Neupreis behoben (newprice.SPECIAL_VERSIONS, exact_size-Prüfung). Offen aus diesem Auftrag: nichts. Beim Nutzer: App 1.4 installieren (<public_url>/app.apk), Gruppe bei den
Kitebar-Suchen selbst eintragen (Suche bearbeiten › Mehr Optionen › Gruppe).

## Erledigt (12.11.2026 morgens, nicht committet) – App flüssig und ruhiger Look

- Ruckeln behoben: Liste aller Angebote öffnet nie mehr von selbst und lädt sich nicht mehr alle 3 s neu; Statuszeile
  per eigenem Abruf (4 s nur bei Scan/KI, sonst 30 s, nichts im Hintergrund, nur bei Änderung ersetzen); Langdruck-Sortieren
  blockiert das Scrollen nur noch auf den Infokarten; keine Unschärfe-, Einblend- und Dauer-Animationen; lange Listen
  mit content-visibility
- Übersicht war breiter als der Bildschirm (Gruppenzeile) – behoben
- Look: flach, Tonflächen statt Schatten, Pillen-Knöpfe, Reiterleiste mit Symbolen und Material-Markierung,
  beste Angebote auf dem Handy als kompakte Liste, Schalter in den Einstellungen repariert

## Danach (vorgeschlagen, noch nicht bestätigt)

- Suchprofil pro Suche (KI, einmalig): Produkt/Kategorie, Pflichtmerkmale, Varianten für den Marktpreis,
  Übersetzungen für FR/IT/SE, sinnvolle Plattformen – speist Abfrage, Vorfilter, KI, Bewertung, Neupreis
- Meldungen nur, wenn ein Angebot neu unter die besten 3 kommt oder dort im Preis fällt
- Plattformen: markt.de (0 Top-Treffer) und Blocket (Versand aus Schweden unrealistisch) abschalten? Entscheid Nutzer
- Ricardo: «neu» unzuverlässig (Suche nicht nach Datum sortiert)
