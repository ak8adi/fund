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

## Erledigt (12.11.2026) – Gruppen in der Übersicht

- Gruppen als Abschnitt mit feiner Linie links (auch mit nur einer Suche), Kopf: Name, Gemini-Kopie, beste Angebote
- Ziehen (static/reorder.js): Suche über eine Gruppe ziehen = hinein, darüber hinaus = heraus; Gruppenkopf ziehen =
  ganze Gruppe verschieben; Name antippen = umbenennen (leer = auflösen); «Neue Gruppe» unten legt oben eine leere an
  (gespeichert erst, wenn eine Suche drin ist). Server: /searches/order mit {"groups": {id: name}}, db.set_search_groups
- Nachtrag: «Ordnen» oben schaltet auf schmale Zeilen mit Griff ≡ (sofort ziehen, kein Langdruck nötig);
  Gruppe anlegen/bearbeiten über ein Blatt (Name + Auswahl der Suchen, Auflösen) → POST /groups. Inline-Umbenennen entfernt.
  Grund für «springt zurück»: Dienst war nach der Python-Änderung nicht neu gestartet. **Nach jeder Python-Änderung
  den Dienst neu starten** (Vorlagen und static brauchen das nicht).

## Erledigt (12.11.2026 nachmittags) – Einklappen, Tutti-Meldung, eigener Ausgang, Suchbegriffe

- Übersicht: Pfeil links vor dem Namen klappt eine Suche auf eine Zeile (Name, «neu», gebraucht ab, Urteil bzw.
  neu ab), Gruppenname klappt die Gruppe (Anzahl Suchen, bestes Angebot). Zustand serverseitig in kv `ov_folded`
  (POST /overview/fold), gilt auf allen Geräten. Kein «alle einklappen» – «Ordnen» zeigt schon alles schmal.
  Gruppenname antippen klappt jetzt (statt Bearbeiten-Blatt; dafür bleibt «Bearbeiten»).
- Tutti war nicht gesperrt: 12 von 13 Abfragen lieferten, eine bekam einmal HTTP 502 – und der letzte Fehler stand
  als Plattformfehler da. Jetzt: fetch_html wiederholt 500/502/504 und Netzfehler einmal nach 8 s (nie 403/429/503);
  nur teilweise gescheitert → `platforms.last_partial` («1 von 13 Abfragen fehlgeschlagen»), Plattform gilt als liefernd.
- `network.proxy` (aus): fester eigener Ausgang für alle Plattform- und Shop-Anfragen. Keine Rotation.
- Suchbegriffe: Umlaute gefaltet (poang = poäng); «Auch suchen nach» (searches.alt_queries, höchstens 2) wird mit
  abgefragt (nur Seite 1, erster Abruf = Bestand) und gilt im Vorfilter. «Vorschlagen» fragt Gemini, aktiviert
  aber nichts: an den echten Suchen lag die KI bei allgemeinen Begriffen oft daneben. Bestehende Suchen: keine
  Alternativen gesetzt. Kosten je Alternative: eine Abfrage mehr pro Plattform und Lauf (Ricardo +60 s).

## Lernen mit mehr Daten (Stand 12.11.2026, 3 Wochen, 79 Läufe)

Signal in der DB: 170 manuell ausgeblendet (137 davon «sessel», sonst 0–8 je Suche), 1 eingeblendet, ~205 verschwundene
passende Angebote mit Preis (MacBooks 33–59, sonst 0–23), 3 Verkäufer-Antworten, 62 geöffnete Angebote (/go/, nur im
Log), Preisänderungen bei ~350 Inseraten. Trefferquote je Plattform: markt.de 28 Treffer / 0 Top-Angebote.
Rangfolge nach Nutzen pro Aufwand (nichts davon umgesetzt):
1. Ausgeblendete/eingeblendete Angebote einer Suche als Beispiele in den KI-Prompt (letzte ~15 Titel + Grund) –
   sofort wirksam bei «sessel», wächst bei den anderen mit.
2. Trefferquote je Plattform und Suche in Analytics; Plattformen ohne passende Treffer pro Suche abwählen
   (weniger Anfragen = auch weniger Sperr-Risiko).
3. Kleine Prüfsammlung (~100 Inserate mit Urteil aus 1. und Stichproben), an der jede Prompt-Änderung gemessen wird.
4. Marktpreis getrennt: verschwunden (≈ bezahlt) vs. angeboten; «schnell weg zu tiefem Preis» als Deal-Signal –
   lohnt erst ab ~30 verschwundenen pro Suche (heute nur die MacBooks).
5. Geöffnete Angebote (/go/) speichern statt nur loggen – Signal für später.
Zu wenig Daten für: gelernte Gewichte im Score, Preisvorhersage pro Produkt. Utopisch: ein System, das ohne
Rückmeldung von selbst besser wird; eigene Modelle trainieren.

## Danach (vorgeschlagen, noch nicht bestätigt)

- Suchprofil pro Suche (KI, einmalig): Produkt/Kategorie, Pflichtmerkmale, Varianten für den Marktpreis,
  Übersetzungen für FR/IT/SE, sinnvolle Plattformen – speist Abfrage, Vorfilter, KI, Bewertung, Neupreis.
  Erster Baustein steht: searches.alt_queries + matching.search_terms/matches_search; weitere Felder dort anhängen.
- Meldungen nur, wenn ein Angebot neu unter die besten 3 kommt oder dort im Preis fällt
- Plattformen: markt.de (0 Top-Treffer) und Blocket (Versand aus Schweden unrealistisch) abschalten? Entscheid Nutzer
- Eigener Ausgang für fundus (Tailscale + kleiner Rechner/VPS mit tinyproxy, dann network.proxy setzen)? Entscheid Nutzer
- Ricardo: «neu» unzuverlässig (Suche nicht nach Datum sortiert)
