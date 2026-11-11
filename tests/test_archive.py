"""Langzeit-Archiv: Tagesstand mit allen Treffern und Verlaufszeile pro Suche."""

import csv
import gzip
import json

from fundus import archive, db
from fundus.models import Listing


def test_snapshot_keeps_everything():
    sid = db.save_search({"query": "Duotone Trust Bar"})
    for ext, price, relevant in (("1", 300, True), ("2", 20, False)):
        l = Listing(platform="tutti", external_id=ext, title=f"Trust Bar {ext}", url=f"https://x/{ext}", price=price,
                    description_full="Sehr gut erhalten, 24 m Leinen")
        db.upsert_listing(l, "ok", [])
        db.add_hit(sid, l.key, False)
        db.update_hit_ai(sid, l.key, relevant, "Bar" if relevant else "Zubehör", "gut")
    db.mark_listing_gone("tutti:1")
    day = archive.snapshot(sid)
    assert day and day.parent.name == f"{sid:03d}-duotone-trust-bar"
    rows = [json.loads(line) for line in gzip.open(day, "rt", encoding="utf-8")]
    assert {r["key"] for r in rows} == {"tutti:1", "tutti:2"}                  # auch Ausgefilterte und Verkaufte
    one = next(r for r in rows if r["key"] == "tutti:1")
    assert one["gone_at"] and one["description_full"].startswith("Sehr gut") and one["ai_relevant"] == 1
    assert one["preisverlauf"][0][1] == 300
    archive.snapshot(sid)                                                          # zweiter Lauf am selben Tag
    with (day.parent / "verlauf.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f, delimiter=";"))) == 2
    assert json.loads((day.parent / "suche.json").read_text())["query"] == "Duotone Trust Bar"
