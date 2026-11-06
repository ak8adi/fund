"""Gebiet einer Suche: «nur in der Nähe» – Umkreis um einen Ort, optional zusätzlich entlang einer Strecke.

Beispiel: Umkreis 30 km um Winterthur, dazu entlang der Strecke nach Grabs ±8 km. Ein Angebot liegt im Gebiet,
wenn sein Ort im Umkreis liegt oder höchstens den Korridor-Abstand von der Autoroute entfernt ist.
Distanzen sind Luftlinie in km. Orte schlägt delivery.py über OpenStreetMap nach (Cache in der Tabelle geo),
die Route kommt von OSRM und wird in kv gespeichert.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass

import httpx

from . import db, delivery as deliv
from .config import get_config

log = logging.getLogger(__name__)
EARTH_KM = 6371.0
DEFAULT_ROUTE_KM = 8


@dataclass
class Area:
    center: tuple[float, float] | None
    radius_km: float
    line: list[tuple[float, float]]      # Autoroute als Punkte (lat, lon), leer = keine Strecke
    corridor_km: float
    label: str

    def contains(self, pos: tuple[float, float]) -> bool:
        if self.center and km(self.center, pos) <= self.radius_km:
            return True
        return bool(self.line) and km_to_line(pos, self.line) <= self.corridor_km


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Luftlinie (Haversine)."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def km_to_line(p: tuple[float, float], line: list[tuple[float, float]]) -> float:
    """Kürzester Abstand eines Punkts zu einer Linie (lokal flach gerechnet – auf Strecken von ein paar 100 km genau genug)."""
    if len(line) == 1:
        return km(p, line[0])
    k = math.cos(math.radians(p[0]))
    def xy(q):  # km relativ zu p
        return (q[1] - p[1]) * 111.32 * k, (q[0] - p[0]) * 110.57
    best = float("inf")
    for a, b in zip(line, line[1:]):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def coords(place: str | None, country: str | None, queue: bool = True) -> tuple[float, float] | None:
    """Koordinaten eines Orts aus dem Cache; Unbekanntes wird im Hintergrund nachgeschlagen (queue=True)."""
    place = (place or "").strip()
    if not place or "*" in place.split()[0]:  # eBay zeigt teils nur «37***» – nicht auffindbar
        return None
    row = db.geo_get(deliv._key(place, country or "CH"))
    if row is None:
        if queue:
            with deliv._lock:
                deliv._queue.add((place, country or "CH"))
        return None
    return (row["lat"], row["lon"]) if row["lat"] is not None else None


def locate(place: str, country: str = "CH") -> tuple[float, float] | None:
    """Ort einer Sucheinstellung sofort nachschlagen (beim Speichern der Suche) und merken."""
    pos = coords(place, country, queue=False)
    if pos or not place.strip():
        return pos
    try:
        pos = deliv._geocode(place, country)
    except Exception as exc:
        log.warning("Ort «%s» nicht gefunden: %s", place, exc)
        return None
    db.geo_put(deliv._key(place, country), *(pos or (None, None)), None)
    return pos


def route(start: str, target: str) -> list[tuple[float, float]]:
    """Autoroute von start nach target als Punktliste (OSRM, vereinfacht), gespeichert in kv."""
    key = f"route:{start.strip().lower()}|{target.strip().lower()}"
    cached = db.kv_get(key)
    if cached:
        return [tuple(p) for p in json.loads(cached)]
    a, b = locate(start), locate(target)
    if not (a and b):
        return []
    try:
        r = httpx.get(f"{deliv.OSRM}/{a[1]},{a[0]};{b[1]},{b[0]}",
                      params={"overview": "simplified", "geometries": "geojson"}, headers=deliv._HEADERS, timeout=20)
        r.raise_for_status()
        line = [(lat, lon) for lon, lat in r.json()["routes"][0]["geometry"]["coordinates"]]
    except Exception as exc:
        log.warning("Route %s → %s nicht gefunden: %s", start, target, exc)
        return [a, b]  # Notlösung: gerade Linie
    db.kv_set(key, json.dumps(line))
    return line


def for_search(search: dict) -> Area | None:
    """Gebiet einer Suche, oder None, wenn «nur in der Nähe» aus ist."""
    if not search.get("near_km"):
        return None
    start = (search.get("near_place") or "").strip() or get_config()["delivery"].get("home_place", "Winterthur")
    center = locate(start)
    target = (search.get("route_to") or "").strip()
    line = route(start, target) if target else []
    corridor = float(search.get("route_km") or DEFAULT_ROUTE_KM)
    label = f"{search['near_km']:g} km um {start}" + (f" und ±{corridor:g} km entlang der Strecke nach {target}" if target else "")
    return Area(center, float(search["near_km"]), line, corridor, label)


def inside(area: Area, h: dict) -> bool | None:
    """Liegt das Angebot im Gebiet? None = Ort (noch) unbekannt."""
    pos = coords(h.get("location"), deliv.listing_country(h))
    return None if pos is None else area.contains(pos)


def rejects_pickup(search: dict) -> bool:
    """Sortiert die Pipeline reine Abholangebote aus? Nur bei «nur Versand» ohne «oder in der Nähe» –
    sonst entscheidet verdict() mit dem Ort."""
    return bool(search.get("require_shipping")) and not search.get("near_km")


def ships(h: dict) -> bool:
    """Kann verschickt werden (laut Plattform, sonst KI)? Möbel/Fracht nie."""
    flag = h.get("ship_flag") or h.get("ai_shipping")
    return flag != "nein" and h.get("ai_size") != "fracht"


def verdict(search: dict, area: Area | None, h: dict) -> str | None:
    """Grund, warum ein Angebot wegen Versand/Ort nicht passt – None, wenn es passt.
    Regeln: nur Versand → muss versendbar sein; nur Nähe → muss im Gebiet liegen;
    beides → versendbar ODER im Gebiet (z.B. Abholung in Winterthur ist trotz «nur Versand» willkommen)."""
    if area is None:
        return None  # «nur Versand» ohne Nähe prüft die Pipeline selbst (ai.NO_SHIPPING)
    if search.get("require_shipping") and ships(h):
        return None
    where = inside(area, h)
    if where:
        return None
    return "Ort wird noch nachgeschlagen" if where is None else "ausserhalb des Gebiets"
