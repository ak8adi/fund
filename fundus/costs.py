"""Endpreis: was kostet mich ein Angebot wirklich, bis es bei mir ist?

Preis (bzw. Einzelpreis aus Set) + Versand (laut Inserat oder geschätzt) + Schweizer Einfuhr-MWST,
wenn die Ware aus dem Ausland kommt und über der Wertfreigrenze liegt. Grob, aber vergleichbar:
ein Inserat für 400 € in Kiel und eines für 420 CHF in Winterthur stehen damit auf derselben Skala.
"""

from __future__ import annotations

from .trust import price_in_chf

PLATFORM_COUNTRY = {"ricardo": "CH", "tutti": "CH", "anibis": "CH", "kleinanzeigen": "DE", "markt": "DE",
                    "willhaben": "AT", "blocket": "SE"}


def listing_country(h: dict) -> str | None:
    return h.get("country") or PLATFORM_COUNTRY.get(h.get("platform", ""))


def total_cost_chf(h: dict, fx: dict, delivery: dict) -> tuple[float | None, list[str]]:
    """(Endpreis in CHF, Hinweise). None, wenn kein Preis bekannt ist."""
    currency = h.get("currency") or "CHF"
    price = price_in_chf(h.get("single_price") or h.get("price"), currency, fx)
    if price is None:
        return None, []
    home = delivery.get("home_country", "CH")
    country = listing_country(h) or home
    notes: list[str] = []

    if h.get("ai_shipping") == "nein":
        shipping = 0.0
        if country != home:
            notes.append(f"nur Abholung in {country}")
    elif h.get("ai_shipping_cost") is not None:
        shipping = price_in_chf(h["ai_shipping_cost"], currency, fx) or 0.0
        notes.append("inkl. Versand")
    else:
        shipping = float(delivery.get("shipping_estimate_chf", {}).get(country, 20))
        notes.append("inkl. Versand (geschätzt)")
    if country != home and country not in delivery.get("addresses", []) and h.get("ai_shipping") != "nein":
        notes.append(f"Versand aus {country} ins Ausland nötig")

    total = price + shipping
    if country != home and total > delivery.get("vat_free_limit_chf", 150):
        total += total * delivery.get("import_vat", 0.081)
        notes.append("inkl. Einfuhr-MWST")
    return round(total, 2), notes
