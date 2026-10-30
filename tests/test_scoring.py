from fundus import db, scoring
from fundus.models import Listing

FX = {"CHF": 1.0, "EUR": 0.94}
HOME = {"home_country": "CH", "addresses": ["CH", "DE"], "shipping_estimate_chf": {"CH": 0, "DE": 7}}


def _score(h, market, fx, year=None):
    """Inländisches Angebot ohne Versandkosten: Endpreis = Preis."""
    return scoring.score_hit({"platform": "tutti", "ai_shipping": "nein", **h}, market, fx, year, HOME)


def test_score_scale():
    h = {"price": 100, "currency": "CHF", "ai_condition": "gut"}
    assert _score(h, 100, FX)[0] == 50                          # Marktpreis, typischer Zustand
    assert _score({**h, "price": 50}, 100, FX)[0] == 90         # halber Preis
    assert _score({**h, "price": 200}, 100, FX)[0] == 10        # doppelter Preis
    assert _score({**h, "ai_condition": "neu"}, 100, FX)[0] > 50  # gleicher Preis, besserer Zustand


def test_older_model_year_lowers_score():
    h = {"price": 100, "currency": "CHF", "ai_condition": "gut"}
    assert _score({**h, "ai_year": 2019}, 100, FX, 2023)[0] < _score(h, 100, FX, 2023)[0]


def test_scam_price_is_capped():
    score, reason, _ = _score({"price": 20, "currency": "CHF"}, 100, FX)
    assert score <= 40 and "Betrugsmuster" in reason


def test_single_price_wins():
    assert scoring.effective_price_chf({"price": 900, "single_price": 300, "currency": "CHF"}, FX) == 300


def test_market_only_from_relevant_hits_and_per_search():
    a = db.save_search({"query": "bar"})
    b = db.save_search({"query": "bar pro"})
    for i, price in enumerate((100, 110, 120, 5, 6)):
        l = Listing(platform="tutti", external_id=str(i), title=f"Bar {i}", url=f"u{i}", price=price)
        db.upsert_listing(l, "neutral", [])
        for sid in (a, b):
            db.add_hit(sid, l.key, False)
        db.update_hit_ai(a, l.key, price >= 100, "x", "gut")                # Zubehör (5, 6) unpassend
        db.update_hit_ai(b, l.key, False, "anderes Modell")
    assert scoring.rescore_search(a) == 110                                  # Zubehör verfälscht nicht
    assert scoring.rescore_search(b) is None                                 # andere Suche: eigene Bewertung
    hits = {h["key"]: h for h in db.hits_for_search(a)}
    assert hits["tutti:0"]["total_chf"] == 110 and hits["tutti:3"]["ai_score"] is None   # 100 + 10 Versand (Schätzung CH)


def test_total_cost_includes_shipping_and_import_vat():
    from fundus.costs import total_cost_chf
    de = {"platform": "kleinanzeigen", "price": 400, "currency": "EUR"}
    total, notes = total_cost_chf(de, FX, {**HOME, "import_vat": 0.081, "vat_free_limit_chf": 150})
    assert total == round((400 * 0.94 + 7) * 1.081, 2) and "inkl. Einfuhr-MWST" in notes
    ch = {"platform": "tutti", "price": 400, "currency": "CHF", "ai_shipping": "nein"}
    assert total_cost_chf(ch, FX, HOME)[0] == 400


def test_duplicates_do_not_count_twice():
    hits = [{"key": "a", "title": "Trust Bar 22m", "price": 150, "ai_relevant": 1, "hit_seen": "2"},
            {"key": "b", "title": "Trust  Bar 22m!", "price": 150, "ai_relevant": 1, "hit_seen": "1"}]
    scoring.mark_duplicates(hits)
    assert not hits[0].get("_dup") and hits[1]["_dup"]


def test_push_only_for_top_offers_after_ai(monkeypatch):
    from fundus import notify
    sent = []
    monkeypatch.setattr(notify, "_publish", lambda cfg, payload: sent.append(payload) or True)
    from fundus.config import get_config
    get_config()["ntfy"]["topic"] = "t"
    sid = db.save_search({"query": "bar"})
    for i, (price, rel) in enumerate(((50, True), (100, True), (110, True), (120, True), (5, False))):
        l = Listing(platform="tutti", external_id=str(i), title=f"Bar {i}", url=f"u{i}", price=price)
        db.upsert_listing(l, "neutral", [])
        db.add_hit(sid, l.key, True)
        db.update_hit_ai(sid, l.key, rel, "x", "gut", "nein")
    scoring.rescore_search(sid)
    keys = notify.notify_top_hits(sid)
    assert keys == ["tutti:0"]                               # nur das klar günstigste, Zubehör nie
    assert "Pkt." in sent[0]["message"]
    assert notify.notify_top_hits(sid) == []                 # nicht doppelt


def test_live_rates_override_config(monkeypatch):
    from fundus import fx

    monkeypatch.setitem(fx._cache, "rates", {"EUR": 0.92})
    r = fx.rates({"fx_to_chf": {"EUR": 0.94, "SEK": 0.082}})
    assert r["EUR"] == 0.92 and r["SEK"] == 0.082 and r["CHF"] == 1.0
