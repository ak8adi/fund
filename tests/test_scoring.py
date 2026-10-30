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
    assert hits["tutti:0"]["total_chf"] == 112 and hits["tutti:3"]["ai_score"] is None   # 100 + 12 PostPac bis 10 kg


def test_delivery_routes():
    from fundus import delivery as d
    cfg = {**HOME, "import_vat": 0.081, "vat_free_limit_chf": 150}
    # DE, gross genug für MWST: DHL 10 kg + Paket4you L + 8,1 % auf Ware und Versand
    de = {"platform": "kleinanzeigen", "price": 400, "currency": "EUR", "ai_size": "L", "ai_weight": 8}
    p = d.plan(de, FX, cfg)
    goods = 400 * 0.94 + 10.49 * 0.94
    assert p.total == round(goods * 1.081 + 6 * 0.94, 2) and "Paket4you" in p.label and "MWST" in p.label
    # DE, klein und günstig: keine MWST
    small = d.plan({**de, "price": 50, "ai_size": "S", "ai_weight": 1}, FX, cfg)
    assert "ohne MWST" in small.label and small.total == round((50 + 4.19 + 2.5) * 0.94, 2)
    # CH: PostPac, Sperrgut, Abholung, Möbel
    ch = {"platform": "tutti", "price": 400, "currency": "CHF"}
    assert d.plan({**ch, "ai_size": "M", "ai_weight": 3}, FX, cfg).total == 412
    assert "Sperrgut" in d.plan({**ch, "ai_size": "XL"}, FX, cfg).label
    assert d.plan({**ch, "ai_shipping": "nein"}, FX, cfg).total == 400
    assert "zu gross" in d.plan({**ch, "ai_size": "fracht"}, FX, cfg).label
    # Versandkosten aus dem Inserat haben Vorrang
    assert d.plan({**ch, "ai_shipping_cost": 15}, FX, cfg).total == 415


def test_pickup_penalty_from_drive_time(monkeypatch):
    from fundus import delivery as d
    monkeypatch.setattr(d, "drive_minutes", lambda place, country, cfg: 180)
    p = d.plan({"platform": "kleinanzeigen", "price": 100, "currency": "EUR", "ai_shipping": "nein",
                "location": "80331 München"}, FX, HOME)
    assert p.penalty == 9 and "3 h 00 min Fahrt" in p.label


def test_new_price_caps_value_and_fills_missing_market():
    h = {"price": 100, "currency": "CHF", "ai_condition": "gut"}
    assert _score({**h, "price": 200}, 300, FX)[0] > _score_np({**h, "price": 200}, 300, 220)[0]  # Neupreis begrenzt
    score, reason, _ = _score_np({**h, "price": 200}, 300, 150)
    assert score <= 30 and "teurer als neu" in reason
    sid = db.save_search({"query": "bar"})
    l = Listing(platform="tutti", external_id="1", title="Bar", url="u1", price=60)
    db.upsert_listing(l, "neutral", [])
    db.add_hit(sid, l.key, False)
    db.update_hit_ai(sid, l.key, True, "x", "gut", "nein")
    db.set_new_price(sid, 200, "von Hand", True)
    scoring.rescore_search(sid)
    h = db.hits_for_search(sid)[0]
    assert h["ai_score"] is not None and "60 % vom Neupreis" in h["score_reason"]   # Markt ≈ 120 aus Neupreis


def _score_np(h, market, new_price):
    return scoring.score_hit({"platform": "tutti", "ai_shipping": "nein", **h}, market, FX, None, HOME, new_price)


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


def test_estimated_set_price_not_in_market():
    hits = [{"price": p, "ai_relevant": 1} for p in (100, 110, 120)]
    hits.append({"price": 1400, "single_price": 300, "single_estimated": 1, "ai_relevant": 1})
    assert scoring.market_price(hits, FX) == 110
