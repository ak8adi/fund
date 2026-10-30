"""Lieferung: wie kommt ein Angebot am besten zu mir, und was kostet es bis dahin?

Die KI schätzt pro Inserat die Versandgrösse (SIZES) und das Gewicht, dieses Modul rechnet daraus den Weg:

- CH-Inserat: Postversand (PostPac Economy) oder Abholung mit Fahrzeit ab Wohnort.
- DE-Inserat: DHL an die Paket4you-Adresse in Jestetten, dort abholen und selbst über die Grenze bringen.
  Bis 150 CHF pro Person und Tag ohne MWST (Wertfreigrenze im Reiseverkehr seit 1.1.2025), darüber 8,1 %
  auf den ganzen Betrag. Industriezölle gibt es seit 1.1.2024 keine mehr.
  Direkt per Post in die Schweiz lohnt sich kaum: internationales Porto plus Verzollungsgebühr der Post
  (ab 13 CHF + 3 % des Warenwerts) – teurer als die Fahrt nach Jestetten.
- Übriges Ausland: Versand an Paket4you (geschätzt) oder Abholung.
- Möbel und Sperriges («fracht»): nur Abholung.

Preise Stand Oktober 2026, Quellen: dhl.de (Privatkunden national), post.ch/de/pakete-versenden/pakete-inland,
lieferadresse-jestetten.ch (Preise bei Abholung), bazg.admin.ch (Wertfreigrenze 150 CHF).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from . import db
from .trust import price_in_chf

log = logging.getLogger(__name__)

PLATFORM_COUNTRY = {"ricardo": "CH", "tutti": "CH", "anibis": "CH", "kleinanzeigen": "DE", "markt": "DE",
                    "willhaben": "AT", "blocket": "SE"}

# Versandgrössen: S = Päckchen 35×25×10 cm, M = 60×30×15 cm, L = 120×60×60 cm bis 31,5 kg,
# XL = Sperrgut bis 200 cm Länge, fracht = Möbel o.ä., nicht als Paket versendbar
SIZES = ("S", "M", "L", "XL", "fracht")
DEFAULT_WEIGHT = {"S": 1, "M": 3, "L": 8, "XL": 15}

DHL_EUR = ((2, 6.19), (5, 7.69), (10, 10.49), (31.5, 23.99))   # Paket national, online frankiert
DHL_PAECKCHEN_S_EUR = 4.19
DHL_SPERRGUT_EUR = 28.99                                       # Zuschlag auf den Paketpreis
POST_CHF = ((2, 9.0), (10, 12.0), (30, 21.0))                  # PostPac Economy
POST_SPERRGUT_CHF = 31.0
P4Y_EUR = {"S": 2.5, "M": 3.5, "L": 6.0, "XL": 10.0}           # Gebühr bei Abholung in Jestetten
INTL_FACTOR = {"S": 0.7, "M": 1.0, "L": 1.6, "XL": 3.0}        # auf shipping_estimate_chf des Landes

PICKUP_PENALTY_PER_HOUR = 3   # Deal-Score-Abzug pro Stunde Fahrt (einfach), höchstens 15
PICKUP_UNKNOWN_PENALTY = 10   # Abholung im Ausland ohne bekannte Fahrzeit


@dataclass
class Plan:
    total: float                  # CHF bis zu mir, ohne Fahrtkosten
    label: str                    # kurz, für die Karte
    pickup_minutes: int | None = None
    penalty: int = 0
    notes: list[str] = field(default_factory=list)


def listing_country(h: dict) -> str | None:
    return h.get("country") or PLATFORM_COUNTRY.get(h.get("platform", ""))


def _tier(table, weight: float) -> float:
    for limit, price in table:
        if weight <= limit:
            return price
    return table[-1][1]


def dhl_eur(size: str, weight: float) -> float:
    if size == "S" and weight <= 2:
        return DHL_PAECKCHEN_S_EUR
    price = _tier(DHL_EUR, weight)
    return price + DHL_SPERRGUT_EUR if size == "XL" else price


def post_chf(size: str, weight: float) -> float:
    return POST_SPERRGUT_CHF if size == "XL" else _tier(POST_CHF, weight)


def fmt_minutes(m: int) -> str:
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def _num(v: float) -> str:
    return f"{v:,.0f}".replace(",", "'")


def plan(h: dict, fx: dict, delivery: dict, size: str | None = None, weight: float | None = None) -> Plan | None:
    """Beste Lieferart und Endpreis für einen Treffer. None, wenn kein Preis bekannt ist."""
    currency = h.get("currency") or "CHF"
    price = price_in_chf(h.get("single_price") or h.get("price"), currency, fx)
    if price is None:
        return None
    eur = fx.get("EUR", 0.94)
    size = h.get("ai_size") if h.get("ai_size") in SIZES else size if size in SIZES else "M"
    weight = h.get("ai_weight") or weight or DEFAULT_WEIGHT.get(size, 3)
    home = delivery.get("home_country", "CH")
    country = listing_country(h) or home
    vat_rate = delivery.get("import_vat", 0.081)
    vat_limit = delivery.get("vat_free_limit_chf", 150)
    place = (h.get("location") or "").strip()
    stated = price_in_chf(h["ai_shipping_cost"], currency, fx) if h.get("ai_shipping_cost") is not None else None

    def vat(value: float) -> tuple[float, str]:
        if country == home:
            return 0.0, ""
        if value > vat_limit:
            return value * vat_rate, f" · MWST {vat_rate * 100:.1f} % am Zoll"
        return 0.0, f" · ohne MWST (unter {vat_limit} CHF)"

    if h.get("ai_shipping") == "nein" or size == "fracht":
        minutes = drive_minutes(place, country, delivery) if place else None
        tax, tax_txt = vat(price)
        where = f"Abholung in {place}" if place else "Abholung"
        if size == "fracht" and h.get("ai_shipping") != "nein":
            where += " (zu gross für Paket)"
        penalty = (min(15, round(minutes / 60 * PICKUP_PENALTY_PER_HOUR)) if minutes is not None
                   else 0 if country == home else PICKUP_UNKNOWN_PENALTY)
        label = where + (f" · {fmt_minutes(minutes)} Fahrt" if minutes is not None else "") + tax_txt
        return Plan(round(price + tax, 2), label, minutes, penalty)

    if country == home:
        ship = stated if stated is not None else post_chf(size, weight)
        how = f"Versand {_num(ship)} CHF laut Inserat" if stated is not None else f"Postversand ≈ {_num(ship)} CHF"
        if size == "XL" and stated is None:
            how += " (Sperrgut – fragen, ob verschickt wird)"
        return Plan(round(price + ship, 2), how)

    # Ausland: an Paket4you Jestetten, dort abholen
    fee = P4Y_EUR.get(size, 3.5) * eur
    j_min = drive_minutes(delivery.get("forwarder_address", ""), "DE", delivery)
    if country in delivery.get("addresses", []):
        ship = stated if stated is not None else dhl_eur(size, weight) * eur
        how = (f"Versand {_num(ship)} CHF laut Inserat" if stated is not None
               else f"DHL ≈ {_num(ship / eur)} €") + f" an {delivery.get('forwarder', 'Paket4you')} + {_num(fee / eur)} € Gebühr"
    else:
        est = float(delivery.get("shipping_estimate_chf", {}).get(country, 30)) * INTL_FACTOR.get(size, 1.0)
        ship = stated if stated is not None else est
        how = f"Versand aus {country} ≈ {_num(ship)} CHF an {delivery.get('forwarder', 'Paket4you')} + {_num(fee / eur)} € Gebühr"
    if size == "XL" and stated is None:
        how += " (Sperrgut)"
    tax, tax_txt = vat(price + ship)
    if j_min is not None:
        how += f" · {fmt_minutes(j_min)} Fahrt"
    return Plan(round(price + ship + fee + tax, 2), how + tax_txt)


# ---------- Fahrzeit (OpenStreetMap: Nominatim zum Finden des Orts, OSRM für die Route) ----------

ENABLED = True
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving"
_HEADERS = {"User-Agent": "fundus/1.0 (private second-hand search tool)"}
_queue: set[tuple[str, str]] = set()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _key(place: str, country: str) -> str:
    return f"{place.strip().lower()}|{country.upper()}"


def drive_minutes(place: str, country: str, delivery: dict) -> int | None:
    """Fahrzeit mit dem Auto ab Wohnort, nur aus dem Cache; Unbekanntes wird im Hintergrund nachgeschlagen."""
    if not place:
        return None
    row = db.geo_get(_key(place, country))
    if row is not None:
        return row["minutes"]
    with _lock:
        _queue.add((place, country))
    return None


def pending() -> bool:
    with _lock:
        return bool(_queue)


def _geocode(place: str, country: str) -> tuple[float, float] | None:
    r = httpx.get(NOMINATIM, params={"q": place, "countrycodes": country.lower(), "format": "json", "limit": 1},
                  headers=_HEADERS, timeout=20)
    r.raise_for_status()
    hits = r.json()
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def _home(delivery: dict) -> tuple[float, float] | None:
    place, country = delivery.get("home_place", "Winterthur"), delivery.get("home_country", "CH")
    row = db.geo_get(_key(place, country))
    if row and row["lat"] is not None:
        return row["lat"], row["lon"]
    pos = _geocode(place, country)
    db.geo_put(_key(place, country), *(pos or (None, None)), 0 if pos else None)
    time.sleep(1.1)
    return pos


def resolve_pending(delivery: dict, on_done=None) -> None:
    """Offene Orte nachschlagen (Nominatim erlaubt 1 Anfrage pro Sekunde). Läuft in einem Hintergrund-Thread."""
    global _worker
    if not ENABLED:
        return
    with _lock:
        if not _queue or (_worker and _worker.is_alive()):
            return

        def work():
            found = 0
            try:
                home = _home(delivery)
                while home:
                    with _lock:
                        if not _queue:
                            break
                        place, country = _queue.pop()
                    if db.geo_get(_key(place, country)) is not None:
                        continue
                    minutes = lat = lon = None
                    try:
                        pos = _geocode(place, country)
                        time.sleep(1.1)
                        if pos:
                            lat, lon = pos
                            r = httpx.get(f"{OSRM}/{home[1]},{home[0]};{lon},{lat}", params={"overview": "false"},
                                          headers=_HEADERS, timeout=20)
                            r.raise_for_status()
                            minutes = round(r.json()["routes"][0]["duration"] / 60)
                            found += 1
                    except Exception as exc:
                        log.info("Fahrzeit für %s (%s) nicht gefunden: %s", place, country, exc)
                    db.geo_put(_key(place, country), lat, lon, minutes)
            except Exception as exc:
                log.warning("Fahrzeiten: %s", exc)
            if found and on_done:
                on_done()

        _worker = threading.Thread(target=work, daemon=True)
        _worker.start()
