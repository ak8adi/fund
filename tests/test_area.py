"""Gebiet einer Suche: Umkreis, Strecke, und wie es mit «nur Versand» zusammenspielt."""

from fundus import area, db, delivery

WINTERTHUR, ZUERICH, GRABS, BUCHS_SG, BERN = (47.4988, 8.7237), (47.3769, 8.5417), (47.1825, 9.4439), (47.1671, 9.4773), (46.948, 7.4474)


def _geo(place, pos, country="CH"):
    db.geo_put(delivery._key(place, country), *pos, None)


def test_distances():
    assert 18 < area.km(WINTERTHUR, ZUERICH) < 22
    line = [WINTERTHUR, (47.33, 9.10), GRABS]
    assert area.km_to_line(BUCHS_SG, line) < 5 and area.km_to_line(BERN, line) > 100


def _setup(monkeypatch):
    for p, pos in (("Winterthur", WINTERTHUR), ("8001 Zürich", ZUERICH), ("Grabs", GRABS), ("9470 Buchs SG", BUCHS_SG), ("3011 Bern", BERN)):
        _geo(p, pos)
    db.kv_set("route:winterthur|grabs", '[[47.4988, 8.7237], [47.33, 9.10], [47.1825, 9.4439]]')


def test_verdict_rules(monkeypatch):
    _setup(monkeypatch)
    near = {"near_km": 30, "near_place": "Winterthur"}
    pickup_zh = {"location": "8001 Zürich", "platform": "tutti", "ship_flag": "nein"}
    pickup_bern = {"location": "3011 Bern", "platform": "tutti", "ship_flag": "nein"}
    ship_bern = {"location": "3011 Bern", "platform": "tutti", "ship_flag": "ja"}
    on_route = {"location": "9470 Buchs SG", "platform": "tutti", "ship_flag": "nein"}
    # nur in der Nähe
    r = area.for_search(near)
    assert area.verdict(near, r, pickup_zh) is None
    assert area.verdict(near, r, ship_bern) == "ausserhalb des Gebiets"
    # Versand oder in der Nähe: Abholung in Zürich ja, Abholung in Bern nein, Versand aus Bern ja
    both = {**near, "require_shipping": 1}
    r = area.for_search(both)
    assert area.verdict(both, r, pickup_zh) is None and area.verdict(both, r, ship_bern) is None
    assert area.verdict(both, r, pickup_bern) == "ausserhalb des Gebiets"
    assert not area.rejects_pickup(both) and area.rejects_pickup({"require_shipping": 1})
    # entlang der Strecke nach Grabs
    route = {**near, "route_to": "Grabs", "route_km": 8}
    r = area.for_search(route)
    assert area.verdict(route, r, on_route) is None and area.verdict(near, area.for_search(near), on_route)
    # unbekannter Ort: wird nachgeschlagen, bis dahin nicht oben
    assert area.verdict(near, area.for_search(near), {"location": "Nirgendwo", "platform": "tutti"}) == "Ort wird noch nachgeschlagen"
