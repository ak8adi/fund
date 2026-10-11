# Nächste Schritte

Stand 11.10.2026: Code fertig, Tests grün. Die Adapter sind aber noch **nicht gegen die echten Seiten
getestet**, weil sie beim Bau nicht erreichbar waren.

1. Auf dem Mac installieren: `./deploy/install-mac.sh`
2. Adapter prüfen:
   ```
   .venv/bin/fundus probe tutti "kite"
   .venv/bin/fundus probe ricardo "kite"
   .venv/bin/fundus probe anibis "kite"
   ```
   Zeigt einer 0 Inserate: nochmals mit `--save` laufen lassen und Ausgabe + `data/probe-<plattform>.html`
   zur Anpassung weitergeben. Die Such-URLs in `config.yaml` sind eine Annahme und evtl. zu korrigieren.
3. eBay-Schlüssel holen (developer.ebay.com → Application Keys → Production) und in `config.yaml` eintragen,
   dann `fundus probe ebay:EBAY_DE "kite"`.
4. ntfy-Topic setzen, in der App abonnieren, `fundus ntfy-test`.
5. Erste Suchen anlegen: z.B. Elektronik, Möbel, Kitesurf-Zubehör.

Später: Facebook Marketplace, optionaler Umkreisfilter.
