# fundus – Analyse aus der Metaperspektive

Stand 30.10.2026. Grundlage: Code auf `main` (0bae890) und deine Daten vom MacBook Pro
(6 aktive Suchen, 815 Treffer, 34 Läufe).

**Ziel des Programms, in einem Satz:** *Welches sind jetzt gerade die besten Angebote für mein Produkt –
unter Berücksichtigung von Preis, Zustand, Vertrauen und dem, was mich das Ding am Ende wirklich kostet?*

---

## 1. Der Ablauf heute

```
 Zeitplan (07:40 / 18:20 ± 25 min)
        │
 ① Abfrage        pro Suche × Plattform 1 Seite, neueste zuerst (11 Plattformen)
        │
 ② Vorfilter      Ausschlusswörter im Titel, Preis min/max
        │
 ③ Speichern      Inserat + Treffer; Median aller Treffer → Vertrauens-Ampel (Regeln)
        │
 ④ Meldung        ntfy für jeden neuen Treffer  ← noch VOR jeder Relevanzprüfung
        │
 ⑤ Details        Beschreibungsseiten nachladen (max. 15 pro Suche)
        │
 ⑥ KI             Gemini, Stapel à 20 unbewertete Treffer: relevant? Tier S/A/B/C, Score 0–100
        │
 ⑦ Anzeige        sortiert nach KI-Score; S/A oben als «Tier-List», Rest darunter, Fehltreffer eingeklappt
```

## 2. Das Kernproblem

Die Pipeline beantwortet heute die Frage *«Was ist neu?»* gut, aber die Frage *«Was ist das beste
Angebot?»* nur zufällig. Drei Gründe:

1. **Am Eingang kommt vor allem Rauschen.** 70–85 % aller Treffer sind Fehltreffer (Skistiefel, MacBooks,
   CDs …). Die KI verbringt den Grossteil ihrer Arbeit mit Aussortieren statt mit Bewerten.
2. **Das Ranking ist relativ statt absolut.** Gemini soll «die besten 30 %» *innerhalb eines 20er-Stapels*
   markieren. Ein S aus Stapel 1 und ein S aus Stapel 4 sind nicht vergleichbar, und ein neues, deutlich
   besseres Angebot stuft alte S-Treffer nie herunter.
3. **Der Preis ist nicht der Preis.** Verglichen werden Angebotspreise. Versand, Einfuhr-MWST und
   «nur Abholung in Schweden» fliessen nicht ein. Ein Inserat für 400 € in Kiel und eines für 420 CHF in
   Winterthur sind für dich nicht gleich teuer.

## 3. Befunde mit deinen Daten

### A. Rauschen pro Plattform

| Plattform | Treffer | davon relevant (KI) | Top-Tier S/A |
|---|---|---|---|
| Kleinanzeigen | 175 | **65 %** | 34 |
| Ricardo | 72 | 23 % | 4 |
| Willhaben | 141 | 16 % | 5 |
| Blocket | 91 | 7 % | 1 |
| **Tradera** | **336** | **0 %** (1 von 256) | 1 |

Tradera liefert pro Suche immer 80 Treffer, egal wonach gesucht wird (CDs, Autos, Mützen). Es ist die
grösste Quelle – und reine Last: 80 Anfragen-Ergebnisse, KI-Kosten und Fehlalarme pro Lauf.
Blocket und Tradera sind zudem schwedisch; Versand nach CH/DE ist dort die Ausnahme.

### B. Relevanz hängt am Inserat statt an der Suche
`ai_relevant`, `ai_tier`, `ai_score` liegen in der Tabelle `listings`. Dasselbe Inserat in zwei Suchen
bekommt also *eine* Bewertung. Beispiel aus deinen Daten: «Core Sensor4 Pro Bar» ist für *core sensor pro 4*
relevant – und erscheint deshalb auch bei *core sensor pro 3* als relevanter Treffer. 10 Inserate sind
betroffen. Dasselbe gilt für «Ausblenden»: es markiert das Inserat in **allen** Suchen als irrelevant.

### C. Der Median ist verschmutzt
Der Vergleichspreis für die Vertrauens-Ampel ist der Median **aller** Treffer einer Suche, inkl. Zubehör
und Fehltreffer. Folge bei *thule urban glide 3*: 67 von 198 Treffern stehen auf 🔴/🟡 «Preis deutlich unter
dem Median». Eine Babyschale für 80 CHF wird als «38 % des Medians» markiert, obwohl sie gar nicht mit
Kinderwagen verglichen werden dürfte. Die Ampel warnt so oft, dass sie nichts mehr aussagt.

### D. Verkaufte Inserate bleiben oben
`last_seen` wird gepflegt, aber nirgends verwendet. Bei *core sensor pro 4* wurden 51 von 147 Treffern seit
über 3 Tagen nicht mehr gesehen, darunter 6 Top-Tier-Treffer. Sie stehen weiter oben in der Liste.

### E. Meldungen vor der Prüfung
Die Push-Meldung geht in Schritt ④ raus, die KI prüft erst in Schritt ⑥. Bei 70–85 % Fehltreffern heisst
das: die meisten Meldungen betreffen Dinge, die du nicht suchst. Die Meldung sagt auch nichts darüber, ob
das Angebot gut ist.

### F. Zwei Wechselkurs-Tabellen
`config.py` (EUR 0.94, kein DKK/NOK/PLN) und `ai.py` (eigene Tabelle) rechnen unabhängig. Ranking,
Preisfilter und KI sehen dadurch leicht unterschiedliche CHF-Preise.

## 4. Fehler (Bugs)

| # | Wo | Was passiert |
|---|---|---|
| 1 | `ai.py:129` | Der Prompt-Teil ist kein f-String. Gemini bekommt wörtlich `{'WICHTIG: Schliesse Angebote aus …' if search.get('require_shipping') else ''}` – die Anweisung «nur mit Versand» kommt nie als Anweisung an. |
| 2 | `web/app.py:292, 305` | `update_listing_ai(relevant=None)` → `int(None)` → Absturz (HTTP 500), sobald man bei einer Suche die Einstellung «Sets erlauben» umschaltet und betroffene Treffer existieren. |
| 3 | `ai.py:89` | Die KI-Auswertung lädt pro 20er-Stapel bis zu 3 Ricardo-Seiten und läuft in einer Schleife über alle Stapel – das umgeht die neue Begrenzung auf 3 Ricardo-Abrufe pro Lauf. |
| 4 | `ai.py:195` + `db.upsert_listing` | Der von der KI erkannte Einzelpreis (Set) überschreibt `price`; der nächste Lauf schreibt den Set-Preis wieder zurück. Der Preis springt hin und her. |
| 5 | `db.hide_hit` | «Ausblenden» setzt nicht `hidden = 1`, sondern markiert das Inserat global als irrelevant (siehe B). |
| 6 | Läufe | Ein Server-Neustart während eines Laufs hinterlässt einen offenen Lauf; die KI-Auswertung startet für diese Suche erst beim nächsten Lauf (*baby einschlagdecke winter*: 160 Treffer, 0 bewertet). |
| 7 | Facebook, Leboncoin, Subito | Liefern seit Beginn 0 Treffer bzw. 403 – kosten aber bei jedem Lauf je Suche eine Anfrage plus Wartezeit. |

## 5. Zielbild: vom «Was ist neu?» zum «Was ist am besten?»

```
 ① Abfrage        nur Plattformen mit Nutzen für CH + DE-Lieferadresse
        │
 ② Vorfilter      ALLE Suchwörter müssen vorkommen (normalisiert: «Sensor4» = «Sensor 4»),
        │         Ausschlusswörter auch in der Beschreibung, Preisrahmen
        │
 ③ Normalisieren  Gesamtpreis in CHF = Preis + Versand (falls bekannt) + Einfuhr-MWST
        │         (DE→CH ab 150 CHF Warenwert) · ein einziger Wechselkurs
        │
 ④ KI: Merkmale   pro Suche (nicht pro Inserat): relevant? Variante/Modell, Zustand (neu/sehr gut/
        │         gebraucht/defekt), Set-Inhalt, Einzelpreis, Versand möglich?
        │         → absolute Merkmale statt relativer Tiers
        │
 ⑤ Marktpreis     Median nur aus RELEVANTEN Treffern (inkl. Preis-Historie verschwundener Inserate)
        │
 ⑥ Deal-Score     Ersparnis gg. Marktpreis, korrigiert um Zustand und Vertrauen;
        │         «nicht mehr gesehen» → vermutlich verkauft → raus aus dem Ranking
        │
 ⑦ Anzeige        pro Suche «Beste Angebote jetzt»: Top 3 mit Gesamtpreis und einem Satz Begründung
        │
 ⑧ Meldung        erst NACH der KI, nur wenn ein Angebot in die Top 3 kommt oder X % unter Marktpreis liegt
```

Der wichtigste Gedanke: **Die KI liefert Fakten (Merkmale), das Ranking rechnet das Programm.** Dann sind
Scores über Stapel und Zeit hinweg vergleichbar, ein neues besseres Angebot verdrängt automatisch ein
älteres, und die Begründung «23 % unter Marktpreis, sehr guter Zustand, Versand nach DE» ist nachvollziehbar.

## 6. Plan in Etappen

**Etappe 1 – Fehler & Rauschen (klein, sofort spürbar)**
- Bugs 1–5 beheben.
- Tradera entfernen; Facebook, Leboncoin, Subito ausschalten oder entfernen.
- Vorfilter «alle Suchwörter» (normalisiert) für alle Plattformen.
- Verschwundene Inserate (> 2 Läufe nicht gesehen) als «vermutlich weg» markieren und unten einsortieren.

**Etappe 2 – Richtige Bewertung**
- KI-Ergebnisse pro Suche speichern (Spalten in `hits` statt `listings`).
- Marktpreis nur aus relevanten Treffern; Vertrauens-Ampel darauf umstellen.
- KI liefert Merkmale (Zustand, Variante, Versand, Einzelpreis) statt Tiers.

**Etappe 3 – Bestes Angebot**
- Gesamtpreis in CHF (Versand, Einfuhr-MWST, ein Wechselkurs).
- Deal-Score und Ansicht «Beste Angebote jetzt».
- Meldungen erst nach der KI, nur für echte Top-Angebote.
