"""Versandangabe der Plattform («Nur Abholung») schlägt die KI-Schätzung – auf allen Plattformen gleich."""

from fundus import ai, db, delivery
from fundus.adapters.extract import shipping_from_page
from fundus.adapters.willhaben import WillhabenAdapter
from fundus.models import Listing

# Ausschnitte echter Inseratseiten (Oktober 2026)
KA_PAGE = '"labstest":"","Versand":"nein","posterid":"43683547","Verkaeufer":"privat"'
TUTTI_PAGE = ('<dl class="a"><dt class="b"><span class="c">Versand verfügbar</span></dt><dd class="d">'
              '<span class="MuiTypography-root e">Nein</span></dd></dl>')
WH_PAGE = ('<div data-testid="attribute-title" ><span >Übergabe</span></div>'
           '<div data-testid="attribute-value" >Selbstabholung</div>')


def test_shipping_from_detail_pages():
    assert shipping_from_page(KA_PAGE, "kleinanzeigen") == "nein"
    assert shipping_from_page(KA_PAGE.replace("nein", "ja"), "kleinanzeigen") == "ja"
    assert shipping_from_page(TUTTI_PAGE, "tutti") == "nein"
    assert shipping_from_page(TUTTI_PAGE.replace("Nein", "Ja"), "anibis") == "ja"
    assert shipping_from_page(WH_PAGE, "willhaben") == "nein"
    assert shipping_from_page(WH_PAGE.replace("Selbstabholung", "Selbstabholung, Versand"), "willhaben") == "ja"
    assert shipping_from_page("<p>Nur Abholung</p>", "tutti") is None  # Text ist Sache der KI


def test_shipping_from_search_cards():
    def wh(tree, p2p="false"):
        return {"attributes": {"attribute": [{"name": "categorytreeattributeids", "values": [tree]},
                                             {"name": "p2penabled", "values": [p2p]}]}}
    assert WillhabenAdapter.shipping(wh("[2535;2536, 21;23]")) == "nein"
    assert WillhabenAdapter.shipping(wh("[2535;2537, 2535;2536]")) == "ja"
    assert WillhabenAdapter.shipping(wh("[21;23]", "true")) == "ja"
    assert WillhabenAdapter.shipping(wh("[21;23]")) is None


def _hit(sid, require_shipping=True):
    l = Listing(platform="kleinanzeigen", external_id="3555803943", title="Windeleimer Magic Majestic",
                url="https://x/1", price=10, currency="EUR", location="27257 Sudwalde")
    db.upsert_listing(l, "ok", [])
    db.add_hit(sid, l.key, False)
    return l


def test_platform_pickup_only_overrides_ai():
    # Fall Windeleimer: Suche «Nur mit Versand», Text sagt nichts, Kleinanzeigen zeigt «Nur Abholung»
    sid = db.save_search({"query": "magic majestic windeleimer", "require_shipping": True})
    l = _hit(sid)
    db.update_hit_ai(sid, l.key, True, "Gesuchter Eimer", "gut", "unbekannt")
    assert ai.apply_platform_shipping(l.key, "nein") == {sid}
    h = db.hits_for_search(sid, include_hidden=True)[0]
    assert h["ai_relevant"] == 0 and h["ai_reason"] == db.NO_SHIPPING and h["ai_shipping"] == "nein"
    assert ai.apply_platform_shipping(l.key, "nein") == set()  # unverändert: nichts zu tun

    # Plattform sagt später doch «Versand möglich»: neu prüfen statt aussortiert lassen
    ai.apply_platform_shipping(l.key, "ja")
    assert db.hits_for_search(sid, include_hidden=True)[0]["ai_relevant"] is None


def test_platform_flag_used_by_ai_and_delivery():
    sid = db.save_search({"query": "magic majestic windeleimer", "require_shipping": True})
    l = _hit(sid)
    ai.apply_platform_shipping(l.key, "nein")  # noch unbewertet: nur gemerkt
    search = db.get_search(sid)
    ai._store_facts(search, {"key": l.key, "relevant": True, "reason": "Gesuchter Eimer", "shipping": "ja"})
    h = db.hits_for_search(sid, include_hidden=True)[0]
    assert h["ai_relevant"] == 0 and h["ai_shipping"] == "nein"

    # Ohne «Nur mit Versand»: bleibt drin, aber als Abholung gerechnet
    sid2 = db.save_search({"query": "windeleimer", "require_shipping": False})
    db.add_hit(sid2, l.key, False)
    ai._store_facts(db.get_search(sid2), {"key": l.key, "relevant": True, "reason": "Eimer", "shipping": "ja"})
    h2 = db.hits_for_search(sid2)[0]
    assert h2["ai_relevant"] == 1 and h2["ai_shipping"] == "nein"
    assert delivery.plan(h2, {"EUR": 0.94}, {}).label.startswith("Abholung")
