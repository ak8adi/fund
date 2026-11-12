"""Auswertung einer Suche für Übersicht, Suchseite, Analytics und Neu kaufen (Darstellung: static/views.js).

Idee: erst hart aussortieren (K.-o.), dann jedes Angebot auf zwei Achsen stellen –
«was ich zahle» (Endpreis bis zu mir) und «was ich bekomme» (fairer Wert nach Zustand und Jahrgang).
Die besten Angebote sind die Pareto-Front: kein anderes Angebot ist zugleich billiger und mindestens so gut.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import area, db, delivery as deliv, fx as fx_rates, scoring
from .config import get_config
from .notify import image_url
from .trust import as_bid, bidding, buy_now, listing_price


def _when(h: dict) -> datetime | None:
    raw = h.get("posted_at") or h.get("hit_seen")
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _quartiles(values: list[float]) -> tuple[float, float, float] | None:
    if len(values) < 4:
        return None
    q = statistics.quantiles(values, n=4)
    return q[0], statistics.median(values), q[2]


def breakdown(h: dict, fx: dict, dcfg: dict, search: dict) -> str:
    """Kurz, woraus sich der Endpreis zusammensetzt: «365 EUR ≈ 342 CHF + Versand 10 + Paket4you 3»."""
    p = deliv.plan(h, fx, dcfg, search.get("product_size"), search.get("product_weight"))
    if not p or not p.parts:
        return ""
    cur = h.get("currency") or "CHF"
    amount = listing_price(h)
    head = f"{amount:,.0f} {cur}".replace(",", "'")
    if h.get("auction") and h.get("bid_price"):
        head = "Gebot " + head
    if cur != "CHF":
        head += f" ≈ {p.parts[0][1]:,.0f} CHF".replace(",", "'")
    extras = [f"{name} {value:,.0f}".replace(",", "'") for name, value in p.parts[1:] if round(value)]
    text = " + ".join([head] + extras) if extras or cur != "CHF" else ""
    if p.import_vat:  # nicht eingerechnet, nur zur Kenntnis
        text += (" · " if text else "") + f"Einfuhr-MWST {p.import_vat:,.0f} separat".replace(",", "'")
    return text


UNSEEN_HOURS = 20   # Ricardo: so lange nicht mehr in den Suchergebnissen → vermutlich verkauft


def _unseen(h: dict, last_run: str | None) -> bool:
    """Ricardo-Inserate prüft fundus nicht einzeln (Sperren). Taucht eines seit UNSEEN_HOURS in keinem Lauf mehr auf,
    obwohl die Suche seither lief, ist es meist verkauft – dann nie mehr oben zeigen."""
    if h.get("platform") != "ricardo" or not last_run or not h.get("last_seen"):
        return False
    return (datetime.fromisoformat(last_run) - datetime.fromisoformat(h["last_seen"])).total_seconds() > UNSEEN_HOURS * 3600


def ko_reason(h: dict, pay: float | None = None, reference: float | None = None, last_run: str | None = None,
              search: dict | None = None, region: "area.Area | None" = None) -> str | None:
    """Harte Ausschlüsse: was hier steht, kommt nie in die Auswahl der besten Angebote."""
    if search is not None and (where := area.verdict(search, region, h)):
        return where
    if _unseen(h, last_run):
        return "nicht mehr in den Suchergebnissen"
    if pay and reference and pay < scoring.SCAM_RATIO * reference and not bidding(h):  # tiefes Gebot ist normal
        return "unrealistisch billig"
    if h.get("ai_req") == "nein":
        return "Anforderung nicht erfüllt"
    if h.get("trust_level") == "warn":
        return "Verkäufer fragwürdig"
    if h.get("reserved"):
        return "reserviert"
    return None


FRONT_MIN_RATIO = 0.8   # deutlich überteuert (Wert unter 80 % des Preises) ist nie ein «bestes Angebot»


def pareto(offers: list[dict]) -> None:
    """Markiert front=True bei allen nicht dominierten Angeboten (ohne K.-o., ohne laufende Auktionen – deren
    Preis steht nicht fest). Überteuertes bleibt draussen, auch wenn es das «wertvollste» ist: sonst stünde der
    teuerste neuwertige Sessel als bestes Angebot da, nur weil kein anderer neuwertiger billiger ist."""
    pool = [o for o in offers if not o["ko"] and o["value"] is not None and not o.get("bidding")]
    pool = [o for o in pool if o["value"] / o["pay"] >= FRONT_MIN_RATIO] or pool
    for o in pool:
        o["front"] = not any(p is not o and p["pay"] <= o["pay"] and p["value"] >= o["value"]
                             and (p["pay"] < o["pay"] or p["value"] > o["value"]) for p in pool)


def _missed(hits: list[dict], fx: dict, reference: float | None, year: float | None, new_price: float | None,
            offers: list[dict]) -> dict | None:
    """Was ist schon weg? Günstigstes und bestes (Wert ÷ Preis) verschwundenes Angebot, Median der verschwundenen –
    zum Vergleich mit den besten aktuellen. «Weg» heisst meist verkauft, bei Auktionen auch ohne Gebot abgelaufen."""
    gone = []
    for h in hits:
        if h.get("ai_relevant") != 1 or not h.get("gone_at") or h.get("_dup") or bidding(h):
            continue  # reine Auktionen: das letzte gesehene Gebot ist nicht der Verkaufspreis
        pay = scoring.pay_price_chf(h, fx)
        if not pay or ko_reason(h, pay, reference) in ("unrealistisch billig", "Anforderung nicht erfüllt"):
            continue
        value = scoring.fair_value(h, reference, year, new_price) if reference else None
        gone.append({"key": h["key"], "title": h["title"], "pay": round(pay), "cond": h.get("ai_condition") or "unbekannt",
                     "year": h.get("ai_year"), "platform": h["platform"], "gone_at": h["gone_at"],
                     "thumb": image_url(h.get("image"), large=False),
                     "ratio": round(value / pay, 2) if value else None,
                     "unsold": bool(h.get("ends_at") and h["gone_at"] == h["ends_at"] and h.get("auction") and not h.get("bids"))})
    if not gone:
        return None
    best_now = max((o for o in offers if o["front"]), key=lambda o: o["ratio"] or 0, default=None)
    cheapest_now = min((o for o in offers if not o["ko"] and not o.get("bidding")), key=lambda o: o["pay"], default=None)
    return {
        "count": len(gone),
        "median": round(statistics.median(o["pay"] for o in gone)),
        "cheapest": min(gone, key=lambda o: o["pay"]),
        "best": max(gone, key=lambda o: o["ratio"] or 0) if any(o["ratio"] for o in gone) else None,
        "now_cheapest": cheapest_now and {"pay": cheapest_now["pay"], "title": cheapest_now["title"]},
        "now_best": best_now and {"pay": best_now["pay"], "ratio": best_now["ratio"], "title": best_now["title"]},
    }


DAILY_DAYS = 120    # bis so lange seit Suchbeginn: ein Balken pro Tag, danach pro Woche
MAX_WEEKS = 104     # länger zurück: nur die neusten zwei Jahre zeigen


def _timeline(search: dict, hits: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """Neue und verschwundene passende Angebote seit Beginn der Suche – nur echte Veränderungen: der Bestand beim
    ersten Abruf einer Plattform (baseline) zählt nicht als «neu». Dazu die letzten 7 Tage als Kennzahl."""
    new_at, gone_at = Counter(), Counter()
    for h in hits:
        if h.get("ai_relevant") != 1:
            continue
        if h.get("hit_seen") and not h.get("baseline"):
            new_at[h["hit_seen"][:10]] += 1
        if h.get("gone_at"):
            gone_at[h["gone_at"][:10]] += 1
    seen = [h["hit_seen"][:10] for h in hits if h.get("hit_seen")]
    first = min([d for d in [(search.get("created_at") or "")[:10]] + seen if d] or [now.date().isoformat()])
    start, today = datetime.fromisoformat(first).date(), now.date()
    span = (today - start).days + 1
    step = 1 if span <= DAILY_DAYS else 7
    if step == 7:
        start = today - timedelta(days=today.weekday() + 7 * (min(MAX_WEEKS, -(-span // 7)) - 1))  # Montage
    buckets = []
    day = start
    while day <= today:
        keys = [(day + timedelta(days=i)).isoformat() for i in range(step)]
        buckets.append({"day": day.isoformat(), "days": step,
                        "new": sum(new_at[k] for k in keys), "gone": sum(gone_at[k] for k in keys)})
        day += timedelta(days=step)
    recent = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    return buckets, {"new": sum(new_at[k] for k in recent), "gone": sum(gone_at[k] for k in recent)}


WORTH_CLEAR, WORTH, WORTH_TIGHT = 35, 20, 10   # % unter neu: klar lohnend / lohnend / knapp; darunter: lieber neu


def verdict(best: dict | None, new: dict | None, refurb: dict | None, successor: dict | None) -> dict | None:
    """Lohnt sich gebraucht? Vergleicht das beste Gebrauchtangebot (bestes Preis-Wert-Verhältnis, nicht das billigste –
    das ist oft das schlechteste) mit dem günstigsten lieferbaren Neuangebot, beide als Endpreis bis zu dir.
    Unter 10 % Abstand spricht alles für neu (Garantie, Rückgabe, kein Risiko), unter 20 % ist es knapp.
    Gibt es das Produkt nicht mehr neu, ist der Nachfolger der Massstab – dann nur als Einordnung, ohne Urteil."""
    if not best:
        return None
    pay = best["pay"]
    note = None
    if refurb and refurb["total"] <= pay * 1.15:
        note = (f"Erneuert mit Garantie: {refurb['total']} CHF bei {refurb['shop']} – "
                + ("günstiger (Ausstattung prüfen)." if refurb["total"] <= pay else "kaum teurer."))
    if new and new.get("total"):
        pct = round((new["total"] - pay) / new["total"] * 100)
        diff = round(new["total"] - pay)
        if pct >= WORTH_CLEAR:
            level, text = "klar", f"Gebraucht lohnt sich klar: {pct} % unter neu (−{diff} CHF)."
        elif pct >= WORTH:
            level, text = "ja", f"Gebraucht lohnt sich: {pct} % unter neu (−{diff} CHF)."
        elif pct >= WORTH_TIGHT:
            level, text = "knapp", f"Knapp: nur {pct} % unter neu (−{diff} CHF) – neu hat Garantie."
        elif pct > 0:
            level, text = "nein", f"Gebraucht lohnt sich kaum: neu kostet nur {diff} CHF mehr ({pct} %)."
        else:
            level, text = "nein", "Gebraucht lohnt sich nicht: neu kostet gleich viel oder weniger."
        return {"level": level, "text": text, "pct": pct, "note": note, "best": pay, "against": "neu"}
    if successor and successor.get("total"):
        pct = round((successor["total"] - pay) / successor["total"] * 100)
        return {"level": "info", "pct": pct, "note": note, "best": pay, "against": "nachfolger",
                "text": f"Nicht mehr neu. Nachfolger {successor['total']} CHF – gebraucht {pct} % darunter."}
    if note:
        return {"level": "info", "text": "Kein Neupreis zum Vergleich.", "pct": None, "note": note, "best": pay, "against": None}
    return None


def price_head(search: dict, offers: list[dict]) -> dict:
    """Ganz oben in der Suche: günstigstes passendes Gebrauchtangebot und günstigstes lieferbares Neuangebot –
    beide als Endpreis bis zu dir, mit Aufschlüsselung (Preis, Versand, Import)."""
    from . import newprice
    usable = [o for o in offers if not o["ko"] and not o.get("bidding")]  # ein Gebot ist kein «ab»-Preis
    used = min(usable, key=lambda o: o["pay"]) if usable else None
    options = newprice.buy_options(search) if search.get("new_price_offers") else []
    new = newprice.best_new(options, "neu")
    refurb = newprice.best_new(options, "erneuert")
    home = get_config().get("delivery", {}).get("home_country", "CH")
    local = newprice.best_new(options, "neu", home)   # günstigstes aus der Schweiz, wenn das günstigste importiert ist
    successor = newprice.best_new(options, "nachfolger") if not new else None
    best = max((o for o in offers if o["front"]), key=lambda o: o["ratio"] or 0, default=None)
    new_offers = [o for o in options if o.get("kind", "neu") == "neu"]

    def tile(o: dict | None) -> dict | None:
        return o and {"shop": o["shop"], "name": o["name"], "url": o["url"], "price": round(o["price_chf"]),
                      "ship": round(o["ship"]) if o.get("ship") is not None else None, "ship_known": o["ship_known"],
                      "fees": o["fees"], "total": o["total"], "available": o.get("available"), "approx": bool(o.get("approx"))}
    return {
        "used": used and {k: used[k] for k in ("key", "title", "pay", "breakdown", "img", "thumb", "platform", "cond")},
        "new": tile(new), "refurb": tile(refurb), "successor": tile(successor),
        "local": tile(local) if local and new and local is not new and new.get("country") != home else None,
        "best": best and {k: best[k] for k in ("key", "title", "pay", "cond")},
        "verdict": verdict(best, tile(new), tile(refurb), tile(successor)),
        "new_count": sum(o.get("available") is not False for o in new_offers),
        "soldout_count": sum(o.get("available") is False for o in new_offers),
        "saving": round(new["total"] - used["pay"]) if new and used else None,
        "saving_pct": round((new["total"] - used["pay"]) / new["total"] * 100) if new and used and new["total"] else None,
    }


def top_keys(offers: list[dict], rest: int = 6) -> list[str]:
    """Was oben auf der Suchseite steht: beste Angebote und die nächsten dahinter (ohne laufende Auktionen)."""
    return [o["key"] for o in offers if o["front"]] + \
           [o["key"] for o in offers if not o["front"] and not o["ko"] and not o.get("bidding")][:rest]


BUILD_MAX_AGE = 90   # Sekunden: Alter und Restzeiten in der Auswertung dürfen so lange stehen bleiben
_built: dict = {}


def build(search_id: int) -> dict:
    """Auswertung einer Suche – zwischengespeichert, solange sich an den Daten nichts geändert hat (db.version).
    Die Übersicht braucht alle Suchen auf einmal; ohne das dauert jeder Wechsel auf die Übersicht Sekunden."""
    import time
    stamp = db.version()
    hit = _built.get(search_id)
    if hit and hit[0] == stamp and time.monotonic() - hit[1] < BUILD_MAX_AGE:
        return hit[2]
    result = _build(search_id)
    _built[search_id] = (db.version(), time.monotonic(), result)
    return result


def _build(search_id: int) -> dict:
    search = db.get_search(search_id) or {}
    fx = fx_rates.rates(get_config())
    hits = db.hits_for_search(search_id, limit=5000)
    scoring.mark_duplicates(hits)
    market = search.get("market_price")
    new_price = search.get("new_price")
    reference = market or (new_price * scoring.USED_SHARE if new_price else None)
    year = scoring.typical_year(hits)
    now = datetime.now(timezone.utc)
    dcfg = get_config().get("delivery", {})
    region = area.for_search(search)

    offers = []
    for h in hits:
        if h.get("ai_relevant") != 1 or h.get("gone_at") or h.get("_dup") or h.get("hidden"):
            continue
        pay = scoring.pay_price_chf(h, fx)
        if not pay or scoring.out_of_range(h, search, fx):
            continue
        value = scoring.fair_value(h, reference, year, new_price) if reference else None
        when = _when(h)
        open_bid = bidding(h)
        limit = bid_pay = None
        # Stand beim aktuellen Gebot – auch neben einem Sofortkauf. Sets nicht: das Gebot gilt dem ganzen Set,
        # der Vergleichswert nur dem gesuchten Teil.
        if h.get("auction") and h.get("bid_price") and not h.get("single_price"):
            hb = as_bid(h)
            if p := deliv.plan(hb, fx, dcfg, search.get("product_size"), search.get("product_weight")):
                bid_pay = round(p.total)
                if reference:
                    limit = round(scoring.auction_limit(hb, p, reference, fx, year, new_price)[1])
        label = h.get("delivery") or ""
        offers.append({
            "key": h["key"], "title": h["title"], "platform": h["platform"],
            "img": image_url(h.get("image"), large=True), "thumb": image_url(h.get("image"), large=False),
            "location": h.get("location"), "pay": round(pay), "price": h.get("price"), "currency": h.get("currency"),
            "value": round(value) if value else None,
            "ratio": round(value / pay, 2) if value and not open_bid else None,
            # Auktion: bid_pay = Endpreis beim aktuellen Gebot, limit = höchstes lohnendes Gebot (CHF), under = Gebot
            # noch unter dem Wert. bidding = reine Auktion: dann ist auch pay nur dieser Zwischenstand. Mit Sofortkauf
            # ist pay der Sofortpreis, das Inserat steht in der normalen Rangliste UND im Auktions-Bereich.
            "bidding": open_bid, "bid_pay": bid_pay, "limit": limit, "under": bool(bid_pay and value and bid_pay < value),
            "buy_now": bool(buy_now(h)), "bid": h.get("bid_price"),
            "bid_chf": round(scoring.price_in_chf(h.get("bid_price"), h.get("currency") or "CHF", fx) or 0) if bid_pay else None,
            "score": h.get("ai_score"), "tier": h.get("ai_tier"), "cond": h.get("ai_condition") or "unbekannt",
            "year": h.get("ai_year"), "req": h.get("ai_req"), "req_note": h.get("ai_req_note"),
            "delivery": label, "pickup": label.startswith("Abholung"),
            "route": "pickup" if label.startswith("Abholung") else "import" if "Paket4you" in label else "ch",
            "age_h": round((now - when).total_seconds() / 3600, 1) if when else None,
            "auction": bool(h.get("auction")), "bids": h.get("bids"), "ends_at": h.get("ends_at"),
            "reserved": bool(h.get("reserved")), "trust": h.get("trust_level"),
            "drop": bool(h.get("first_price") and h.get("price") and h["price"] < h["first_price"]),
            "reason": h.get("score_reason"), "ko": ko_reason(h, pay, reference, search.get("last_run_at"), search, region), "front": False,
            "breakdown": breakdown(h, fx, dcfg, search), "new": bool(h.get("is_new")),
            "vat": round(p.import_vat) if (p := deliv.plan(h, fx, dcfg, search.get("product_size"), search.get("product_weight"))) and p.import_vat else None,
        })
    if deliv.pending():  # neue Orte (Gebiet, Fahrzeit) im Hintergrund nachschlagen
        deliv.resolve_pending(dcfg)
    pareto(offers)
    offers.sort(key=lambda o: (not o["front"], -(o["ratio"] or 0), o["pay"]))
    missed = _missed(hits, fx, reference, year, new_price, offers)
    head = price_head(search, offers)

    timeline, week = _timeline(search, hits, now)

    pays = [o["pay"] for o in offers if not o["bidding"]]
    auctions = sorted((o for o in offers if o["bid_pay"] and not o["ko"]), key=lambda o: o["ends_at"] or "9")
    return {
        "search": {"id": search_id, "query": search.get("query"), "market": market, "new_price": new_price,
                   "reference": round(reference) if reference else None, "typical_year": year,
                   "requirements": search.get("requirements")},
        "offers": offers,
        "auctions": [o["key"] for o in auctions],
        "missed": missed,
        "head": head,
        "band": _quartiles(pays),
        "timeline": timeline,
        "week": week,
        "area": region.label if region else None,
        "counts": {"all": len([h for h in hits if h.get("ai_relevant") == 1]), "open": len(offers),
                   "front": sum(o["front"] for o in offers), "ko": sum(bool(o["ko"]) for o in offers),
                   "auctions": len(auctions)},
    }


# ---------- Gruppen («entweder das eine oder das andere») und Export für Gemini ----------

def grouped(rows: list[tuple[dict, dict]]) -> list[dict]:
    """Übersicht in Blöcken: Suchen derselben Gruppe stehen beieinander (an der Stelle des ersten Mitglieds), mit einer
    gemeinsamen Bestenliste. rows: [(search, build(search))] in der Reihenfolge der Übersicht.
    Rückgabe: [{"name": Gruppe oder None, "rows": [...], "ids": [...], "best": [Angebote mit .search]}]."""
    blocks: list[dict] = []
    by_name: dict[str, dict] = {}
    for s, v in rows:
        name = (s.get("group_name") or "").strip() or None
        block = by_name.get(name.lower()) if name else None
        if block is None:
            block = {"name": name, "rows": [], "ids": [], "best": []}
            blocks.append(block)
            if name:
                by_name[name.lower()] = block
        block["rows"].append((s, v))
        block["ids"].append(s["id"])
    for b in blocks:
        if b["name"] and len(b["rows"]) > 1:
            # bester Deal über alle Suchen der Gruppe: Wert ÷ Preis ist über Produkte hinweg vergleichbar
            pool = [{**o, "search": s["query"], "search_id": s["id"]} for s, v in b["rows"] for o in v["offers"] if o["front"]]
            b["best"] = sorted(pool, key=lambda o: -(o["ratio"] or 0))[:6]
    return blocks


def gemini_prompt(search_ids: list[int], per_search: int = 40) -> str:
    """Text zum Einfügen in Gemini: alle passenden, verfügbaren Angebote der Suchen mit Endpreis, Zustand, Lieferweg,
    Link und Beschreibung – plus Neupreis und Anforderungen. Mehrere Suchen = Entweder-oder-Entscheid."""
    searches = [s for s in (db.get_search(i) for i in search_ids) if s]
    if not searches:
        return ""
    if len(searches) > 1:
        names = ", ".join(f"«{s['query']}»" for s in searches)
        text = (f"Ich will EINES dieser Produkte gebraucht kaufen (entweder–oder): {names}.\n"
                "Welches Angebot ist insgesamt der beste Deal? Vergleiche über die Produkte hinweg, nenne die Top 5 mit "
                "Begründung (Endpreis, Zustand, Jahrgang, Risiko, Abstand zum Neupreis) und sag, ob sich gebraucht lohnt.\n\n")
    else:
        text = (f"Analysiere diese Inserate für meine Suche nach «{searches[0]['query']}». Welche sind die besten Angebote? "
                "Erstelle eine Rangliste der besten 30 % mit Begründung (Endpreis, Zustand, Jahrgang, Risiko, Abstand zum "
                "Neupreis) und sag, ob sich gebraucht lohnt.\n\n")
    for s in searches:
        v = build(s["id"])
        text += f"=== {s['query']} ===\n"
        if (s.get("requirements") or "").strip():
            text += f"Muss erfüllen: {s['requirements'].strip()}\n"
        head = v["head"]
        if head.get("new"):
            text += f"Neu ab {head['new']['total']} CHF bis zu mir ({head['new']['shop']}: {head['new']['name']})\n"
        elif head.get("successor"):
            text += f"Nicht mehr neu erhältlich; Nachfolger neu ab {head['successor']['total']} CHF ({head['successor']['name']})\n"
        if head.get("refurb"):
            text += f"Vom Händler erneuert ab {head['refurb']['total']} CHF ({head['refurb']['shop']})\n"
        if v["search"].get("market"):
            text += f"Marktpreis gebraucht (Median der Angebote): {round(v['search']['market'])} CHF\n"
        offers = [o for o in v["offers"] if not o["ko"]][:per_search]
        text += f"{len(offers)} Angebote (Endpreis = inkl. Versand und Gebühren bis zu mir):\n\n"
        full = {h["key"]: h for h in db.hits_for_search(s["id"], limit=5000)}
        for o in offers:
            h = full.get(o["key"], {})
            price = (f"Auktion, aktuelles Gebot {o['bid_chf']} CHF, endet {str(o['ends_at'])[:16]}" if o.get("bidding")
                     else f"Endpreis {o['pay']} CHF")
            facts = " · ".join(x for x in (o["cond"] if o["cond"] != "unbekannt" else "", str(o["year"] or ""),
                                           o.get("req_note") or "", o["platform"], o.get("location") or "") if x)
            text += f"- {o['title']}\n  {price}" + (f" ({o['breakdown']})" if o.get("breakdown") else "") + f"\n  {facts}\n"
            if o.get("delivery"):
                text += f"  Lieferung: {o['delivery']}\n"
            if h.get("ai_reason"):
                text += f"  Einschätzung: {h['ai_reason']}\n"
            text += f"  Link: {h.get('url') or ''}\n"
            if h.get("description_full"):
                text += "  Beschreibung: " + " ".join(h["description_full"].split())[:700] + "\n"
            text += "\n"
    return text
