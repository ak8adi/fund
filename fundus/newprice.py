"""Neupreis einer Suche: was kostet das Produkt heute neu – mit Shop und Link?

1. Online suchen (DuckDuckGo, Region Schweiz): «<Suche> kaufen», Marktplätze ausgenommen; dazu die Produkt- bzw.
   Serienseite bei toppreise.ch (Preisvergleich Schweiz – führt auch Auslaufmodelle, solange ein Shop sie hat).
2. Jede Shopseite öffnen und Produktname, Preis, Währung aus den strukturierten Produktdaten lesen
   (JSON-LD/schema.org bzw. Meta-Tags – dieselben Angaben, die Shops für Google bereitstellen).
3. Die KI prüft nur, welche Seiten genau das gesuchte Produkt neu anbieten (kein Zubehör, anderes Modell,
   Gebrauchtware) – sie schätzt keinen Preis. Neupreis = günstigster passender Shop in CHF.
Neupreis = günstigster passender Shop, ausser er liegt weit unter allen anderen (Fehlpreis, falsche Variante).
Nichts gefunden → kein Neupreis (dann von Hand eintragen). Von Hand eingetragene Preise haben Vorrang.
Nebenbei schätzt die KI die typische Versandgrösse des Produkts (delivery.SIZES).
"""

from __future__ import annotations

import html
import json
import logging
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, unquote, urlparse

from . import db, fx as fx_rates
from .adapters.base import fetch_html
from .config import get_config

log = logging.getLogger(__name__)
SEARCH_URL = "https://html.duckduckgo.com/html/?kl=ch-de&q={q}"
MAX_AGE_DAYS = 30
MAX_SHOPS = 10
COMPARE_RE = re.compile(r"https://www\.toppreise\.ch/(?:preisvergleich|produktserie)/")
OUTLIER = 0.75   # günstigster Shop unter 75 % des zweitgünstigsten: nicht als Neupreis nehmen
SUSPICIOUS = 0.5  # unter 50 % des Medians aller passenden Shops: Fake-Shop oder Fehlpreis, weglassen
REFURB_QUERIES = ("{query} refurbished kaufen", "{query} refurbished mediamarkt")  # MediaMarkt: grösster CH-Anbieter
SKIP_DOMAINS = ("ricardo.", "tutti.", "anibis.", "kleinanzeigen.", "ebay.", "willhaben.", "blocket.", "markt.de",
                "youtube.", "facebook.", "instagram.", "reddit.", "pinterest.", "wikipedia.", "idealo.", "toppreise.",
                "google.", "amazon.", "aliexpress.", "temu.")

PROMPT = """Ich suche gebraucht: «{query}». Unten Produktseiten aus Online-Shops (Nr | Shop | Produktname | Preis | Zustand).
Welche Seiten zeigen GENAU dieses Produkt als Neuware (auch wenn «ausverkauft» – das zählt trotzdem)? Passend, wenn Marke, Modellname und Modellnummer übereinstimmen
(Wortreihenfolge und Zusätze wie «Control Bar», Grössenangaben oder Jahrgang egal). Nicht passend: Zubehör, Ersatzteile, anderes Modell oder andere
Produktart, Sets mit weiteren Produkten, Privat-Gebrauchtware, Miete, Spezialversionen, die nicht gesucht sind
(z.B. Foil-, Kinder-, Lite-Version). NENNT die Suche eine Generation, muss sie passen: «Pro 3» ≠ «Pro 4», Chip M1 ≠ M2 ≠ M4,
iPhone 13 ≠ 14. Nennt die Suche KEINE Generation, passt jede Generation des Produkts. Nennt die Suche eine Ausstattung
(z.B. 16GB), muss sie passen; fehlt die Angabe im Produktnamen, passt das Angebot trotzdem. Verschiedene Grössen,
Farben oder Modelljahre passen. Zeilen von toppreise.ch sind Preisvergleiche (Preis = günstigster Shop).
generic = true, wenn die Suche kein bestimmtes Produkt meint (z.B. «sessel», «tisch biedermeier», Antiquitäten) –
dann gibt es keinen sinnvollen Neupreis.

{rows}

Vom Händler erneuerte Geräte (Refurbished, «erneuert», generalüberholt, Vorführgerät mit Garantie) des gesuchten
Produkts sind interessant als Vergleich: deren Nummern in «refurb» statt in «match».

Antworte NUR mit JSON: {{"match": [Nr, ...], "refurb": [Nr, ...], "product": "Modell, kurz", "generic": bool,
"size": "S"|"M"|"L"|"XL"|"fracht", "weight_kg": number}}
size = Versandgrösse verpackt: S Päckchen bis 35×25×10 cm, M bis 60×30×15 cm, L bis 120×60×60 cm und 31 kg,
XL sperrig bis 200 cm lang, fracht = Möbel o.ä., nicht als Paket versendbar."""


def search_links(query: str, skip: list[str] | None = None, compare: bool = False) -> list[str]:
    """Shopseiten aus der Websuche; compare=True: nur Produkt-/Serienseiten von toppreise.ch."""
    page = fetch_html(SEARCH_URL.format(q=quote_plus(query)), "http")
    out: list[str] = []
    for raw in re.findall(r'uddg=([^&"]+)', page):
        url = unquote(raw).split("#")[0]
        host = urlparse(url).netloc.lower()
        if not host or url in out or url in (skip or []):
            continue
        if compare != bool(COMPARE_RE.match(url)) or (not compare and any(d in host for d in SKIP_DOMAINS)):
            continue
        out.append(url)
    return out[:2 if compare else MAX_SHOPS]


def _products(node):
    """Alle schema.org-Produkte in einem JSON-LD-Baum."""
    if isinstance(node, list):
        for n in node:
            yield from _products(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types or "ProductGroup" in types:
            yield node
        for key in ("@graph", "mainEntity", "hasVariant"):
            if key in node:
                yield from _products(node[key])


AVAILABLE = ("InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly", "in stock", "instock")
UNAVAILABLE = ("OutOfStock", "SoldOut", "Discontinued", "out of stock", "oos", "outofstock")


def _availability(value) -> bool | None:
    """schema.org-Verfügbarkeit: True lieferbar, False ausverkauft, None unbekannt (BackOrder, PreOrder, fehlt)."""
    v = str(value or "").rsplit("/", 1)[-1].strip()
    if any(v.lower() == a.lower() for a in AVAILABLE):
        return True
    if any(v.lower() == a.lower() for a in UNAVAILABLE):
        return False
    return None


def _shipping(offer: dict) -> tuple[float, str] | None:
    """Versandkosten aus schema.org shippingDetails – die Rate für die Schweiz, sonst die erste angegebene."""
    details = offer.get("shippingDetails")
    rates = []
    for d in details if isinstance(details, list) else [details]:
        if not isinstance(d, dict):
            continue
        rate = d.get("shippingRate")
        dest = d.get("shippingDestination") or {}
        country = (dest.get("addressCountry") if isinstance(dest, dict) else "") or ""
        if isinstance(rate, dict) and rate.get("value") is not None:
            try:
                rates.append((country.upper() == "CH", float(str(rate["value"]).replace(",", ".")),
                              str(rate.get("currency") or offer.get("priceCurrency") or "").upper()))
            except ValueError:
                continue
    if not rates:
        return None
    rates.sort(key=lambda r: not r[0])
    return rates[0][1], rates[0][2]


def _offer_price(offers, page_url: str | None = None) -> tuple | None:
    """(Preis, Währung, Zustand, lieferbar) des Angebots, das die Seite zeigt: das mit der Seiten-URL, sonst das
    günstigste lieferbare, sonst das günstigste. «lowPrice» (ab-Preis über Varianten) nur als letzte Wahl."""
    found = []
    for o in offers if isinstance(offers, list) else [offers]:
        if not isinstance(o, dict):
            continue
        price = o.get("price")
        if price is None and isinstance(o.get("priceSpecification"), dict):
            price = o["priceSpecification"].get("price")
        low = price is None
        price = price if price is not None else o.get("lowPrice")
        try:
            value = float(str(price).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if value > 0:
            cond = str(o.get("itemCondition") or "").rsplit("/", 1)[-1]
            same_page = bool(page_url and o.get("url") and str(o["url"]).split("?")[0].rstrip("/") == page_url.split("?")[0].rstrip("/"))
            found.append({"value": value, "cur": str(o.get("priceCurrency") or "").upper(), "cond": cond,
                          "avail": _availability(o.get("availability")), "same": same_page, "low": low,
                          "ship": _shipping(o)})
    if not found:
        return None
    best = (next((f for f in found if f["same"]), None)
            or min((f for f in found if f["avail"] is not False and not f["low"]), key=lambda f: f["value"], default=None)
            or min(found, key=lambda f: (f["low"], f["value"])))
    return best["value"], best["cur"], best["cond"], best["avail"], best["ship"]


def extract_offer(html: str, url: str | None = None) -> dict | None:
    """Hauptprodukt einer Shopseite: Name, Preis, Währung, Zustand, lieferbar.
    Seiten mit mehreren verschiedenen Produkten (Kategorien, Suchtreffer) gelten nicht als Produktseite –
    sonst landet der Link auf einer Liste und der Preis gehört zu irgendeinem anderen Produkt."""
    products = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for p in _products(data):
            offer = _offer_price(p.get("offers"), url) or _offer_price(
                [v.get("offers") for v in p.get("hasVariant", []) if isinstance(v, dict)], url)
            if offer:
                products.append((str(p.get("name") or "")[:120], offer))
    names = {n.lower() for n, _ in products if n}
    if len(names) > 3 and not (url and COMPARE_RE.match(url)):
        return None  # Liste mehrerer Produkte
    if products:
        name, offer = products[0]
        out = {"name": name, "price": offer[0], "currency": offer[1], "condition": offer[2], "available": offer[3]}
        if offer[4]:
            out["shipping"], out["shipping_currency"] = offer[4]
        return out
    amount = re.search(r'<meta[^>]+(?:product:price:amount|og:price:amount)[^>]+content="([\d.,]+)"', html)
    currency = re.search(r'<meta[^>]+(?:product:price:currency|og:price:currency)[^>]+content="([A-Za-z]{3})"', html)
    title = re.search(r'<meta[^>]+og:title[^>]+content="([^"]+)"', html)
    avail = re.search(r'<meta[^>]+(?:product:availability|og:availability)[^>]+content="([^"]+)"', html)
    if amount and currency:
        try:
            return {"name": (title.group(1) if title else "")[:120], "price": float(amount.group(1).replace(",", ".")),
                    "currency": currency.group(1).upper(), "condition": "",
                    "available": _availability(avail.group(1).replace(" ", "")) if avail else None}
        except ValueError:
            return None
    return None


def find_offers(query: str, skip: list[str] | None = None, compare: bool = False) -> list[dict]:
    """Shopangebote mit Preis in CHF (noch ungeprüft)."""
    rates = fx_rates.rates(get_config())
    offers = []
    for url in search_links(query, skip, compare):
        try:
            offer = extract_offer(fetch_html(url, "http"), url)
        except Exception as exc:
            log.info("Shopseite %s nicht lesbar: %s", url, exc)
            continue
        finally:
            time.sleep(0.5)
        if not offer or not offer["name"] or offer["price"] < 5:
            continue
        offer["name"] = html.unescape(offer["name"]).strip()
        cur = offer["currency"] or "CHF"
        if cur not in rates:
            continue
        ship_cur = offer.get("shipping_currency") or cur
        offers.append({**offer, "currency": cur, "url": url, "shop": urlparse(url).netloc.removeprefix("www."),
                       "price_chf": round(offer["price"] * rates[cur], 2),
                       "shipping_chf": round(offer["shipping"] * rates[ship_cur], 2)
                       if offer.get("shipping") is not None and ship_cur in rates else None})
    return offers


def lookup(query: str) -> dict | None:
    from .ai import _gemini_json
    api_key = get_config().get("ai", {}).get("gemini_api_key")
    if not api_key:
        return None
    offers = find_offers(f"{query} kaufen")
    if len(offers) < 6:  # wenig Shopseiten: weitere Suchen mit anderem Wortlaut
        offers += find_offers(f"{query} preis neu", [o["url"] for o in offers])
    if len(offers) < 8:
        offers += find_offers(f"{query} online shop", [o["url"] for o in offers])
    for wording in REFURB_QUERIES:  # erneuerte Geräte von Händlern als Vergleich (allgemeine Suchen liefern oft nur Listen)
        try:
            offers += find_offers(wording.format(query=query), [o["url"] for o in offers])
        except Exception as exc:
            log.info("Refurbished-Suche «%s» fehlgeschlagen: %s", wording.format(query=query), exc)
    try:
        offers += find_offers(f"{query} toppreise", [o["url"] for o in offers], compare=True)
    except Exception as exc:
        log.info("toppreise für «%s» nicht lesbar: %s", query, exc)
    rows = "\n".join(f"{i} | {o['shop']} | {o['name']} | {o['price']:g} {o['currency']} | {o['condition'] or '-'}"
                     f"{' | ausverkauft' if o.get('available') is False else ''}"
                     for i, o in enumerate(offers)) or "(keine Shopseiten gefunden)"
    r = _gemini_json(PROMPT.format(query=query, rows=rows),
                     "Du prüfst Produktseiten für einen Preisvergleich. Antworte nur mit JSON.", api_key,
                     ("gemini-3.7-flash", "gemini-3.5-flash-lite"))  # selten, aber heikel: stärkeres Modell zuerst
    r = r[0] if isinstance(r, list) and r else r
    if not isinstance(r, dict):
        return None
    def chosen(field: str) -> list[dict]:
        out, seen = [], set()
        for i in r.get(field) or []:
            if not (isinstance(i, int) and 0 <= i < len(offers)):
                continue
            o = offers[i]
            sig = (o["shop"], o["name"].lower(), round(o["price_chf"]))  # Farbvarianten / doppelt gefunden
            if sig not in seen:
                seen.add(sig)
                out.append(o)
        return sorted(out, key=lambda o: o["price_chf"])
    refurb_words = ("refurb", "erneuert", "generalüberholt", "renewed", "used")
    picked = [o for o in chosen("match") if not any(w in (o["condition"] + " " + o["name"]).lower() for w in refurb_words)]
    refurb = [o for o in chosen("match") + chosen("refurb") if o not in picked]
    # nur unter sich vergleichen: Neupreise eines Auslaufmodells sind oft Restbestand zum Höchstpreis
    refurb = [o for o in plausible(refurb, refurb) if o.get("available") is not False][:4]
    size = r.get("size") if r.get("size") in ("S", "M", "L", "XL", "fracht") else None
    try:
        weight = float(r["weight_kg"]) if r.get("weight_kg") else None
    except (TypeError, ValueError):
        weight = None
    if r.get("generic") is True:
        return {"price": None, "note": "Kein bestimmtes Produkt – Neupreis nicht sinnvoll (bei Bedarf von Hand eintragen)",
                "offers": [], "size": size, "weight": weight}
    def kept(o: dict, kind: str) -> dict:
        return {**{k: o[k] for k in ("shop", "name", "price", "currency", "price_chf", "url")},
                "available": o.get("available"), "shipping_chf": o.get("shipping_chf"), "kind": kind}
    if not picked:
        note = "Kein Neupreis online gefunden – vermutlich nicht mehr neu erhältlich; bei Bedarf von Hand eintragen"
        if refurb:
            note += f" · erneuert ab {round(refurb[0]['price_chf'])} CHF bei {refurb[0]['shop']}"
        return {"price": None, "note": note, "offers": [kept(o, "erneuert") for o in refurb], "size": size, "weight": weight}
    picked = plausible(picked, picked)
    in_stock = [o for o in picked if o.get("available") is not False]
    sold_out = [o for o in picked if o.get("available") is False]
    best = choose_best(in_stock) if in_stock else sold_out[0]  # Neupreis = günstigstes lieferbares Angebot
    price = round(best["price_chf"])
    product = str(r.get("product") or query)[:60]
    shops = len({o["shop"] for o in in_stock})
    if in_stock:
        note = f"{product} · günstigster lieferbar bei {best['shop']}" + (f" (von {shops} Shops)" if shops > 1 else "")
        if sold_out and sold_out[0]["price_chf"] < best["price_chf"]:
            note += f" · ausverkauft ab {round(sold_out[0]['price_chf'])} CHF bei {sold_out[0]['shop']}"
    else:
        note = f"{product} · nur ausverkauft gefunden, zuletzt {price} CHF bei {best['shop']}"
    if refurb:
        note += f" · erneuert ab {round(refurb[0]['price_chf'])} CHF bei {refurb[0]['shop']}"
    keep = [kept(o, "neu") for o in (in_stock + sold_out)[:12]] + [kept(o, "erneuert") for o in refurb]
    return {"price": price, "note": note, "offers": keep, "size": size, "weight": weight}


def plausible(offers: list[dict], all_matched: list[dict]) -> list[dict]:
    """Ohne Angebote weit unter dem Median aller passenden (auch ausverkaufter): Fehlpreis, falsche Variante oder
    Fake-Shop. Braucht mindestens 3 Vergleichspreise."""
    if len(all_matched) < 3:
        return offers
    median = statistics.median(o["price_chf"] for o in all_matched)
    return [o for o in offers if o["price_chf"] >= SUSPICIOUS * median]


SHOP_COUNTRY = {"ch": "CH", "li": "CH", "de": "DE", "at": "AT", "it": "IT", "fr": "FR", "nl": "NL", "es": "ES",
                "be": "BE", "dk": "DK", "se": "SE", "pl": "PL", "uk": "GB"}


def shop_country(url: str, currency: str | None) -> str:
    """Land des Shops aus der Domain, sonst aus der Währung (EUR-Shops liefern an die deutsche Grenzadresse)."""
    tld = urlparse(url).netloc.rsplit(".", 1)[-1].lower()
    return SHOP_COUNTRY.get(tld) or {"CHF": "CH", "GBP": "GB", "SEK": "SE"}.get((currency or "").upper(), "DE")


def buy_options(search: dict) -> list[dict]:
    """Neu kaufen: alle passenden Shopangebote mit Versand, Import und Endpreis bis zu dir (wie bei Gebrauchten),
    lieferbare zuerst, dann nach Endpreis. Versand aus den Shopdaten, sonst geschätzt (≈)."""
    from . import delivery as deliv
    cfg = get_config()
    rates = fx_rates.rates(cfg)
    out = []
    for o in search.get("new_price_offers") or []:
        country = shop_country(o["url"], o.get("currency"))
        pseudo = {"price": o["price"], "currency": o.get("currency") or "CHF", "country": country, "platform": "",
                  "ai_shipping_cost": (o["shipping_chf"] / rates.get(o.get("currency") or "CHF", 1))
                  if o.get("shipping_chf") is not None else None,
                  "ai_size": search.get("product_size"), "ai_weight": search.get("product_weight"), "ai_shipping": "ja"}
        p = deliv.plan(pseudo, rates, cfg.get("delivery", {}), search.get("product_size"), search.get("product_weight"))
        if not p:
            continue
        parts = dict(p.parts)
        out.append({**o, "country": country, "total": round(p.total), "route": p.label,
                    "ship": parts.get("Versand"), "fees": round(parts.get("Paket4you", 0)),
                    "ship_known": o.get("shipping_chf") is not None})
    out.sort(key=lambda o: (o.get("available") is False, o["total"]))
    return out


def best_new(options: list[dict], kind: str = "neu") -> dict | None:
    """Günstigstes lieferbares Angebot einer Art («neu» oder «erneuert») aus buy_options."""
    return next((o for o in options if o.get("kind", "neu") == kind and o.get("available") is not False), None)


def choose_best(picked: list[dict]) -> dict:
    """Günstigstes passendes Angebot – ausser es liegt weit unter dem zweitgünstigsten (Fehlpreis, falsche Variante,
    Aktion mit Haken): dann das zweitgünstigste. picked ist nach price_chf sortiert."""
    if len(picked) >= 3 and picked[0]["price_chf"] < OUTLIER * picked[1]["price_chf"]:
        return picked[1]
    return picked[0]


def refresh(search_id: int) -> bool:
    """Neupreis und Produktgrösse neu bestimmen; ein von Hand gesetzter Preis bleibt."""
    s = db.get_search(search_id)
    if not s:
        return False
    try:
        r = lookup(s["query"])
    except Exception as exc:
        log.warning("Neupreis für «%s» nicht bestimmt: %s", s["query"], exc)
        return False
    if not r:
        return False
    if s.get("new_price_manual"):
        db.set_new_price(search_id, s["new_price"], s["new_price_note"], True, r["size"], r["weight"], r["offers"])
    else:
        db.set_new_price(search_id, r["price"], r["note"], False, r["size"], r["weight"], r["offers"])
    from . import scoring
    scoring.rescore_search(search_id)
    log.info("Neupreis «%s»: %s (%s)", s["query"], r["price"], r["note"])
    return True


def is_stale(s: dict) -> bool:
    if not s.get("new_price_at"):
        return True
    at = datetime.fromisoformat(s["new_price_at"])
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at > timedelta(days=MAX_AGE_DAYS)


def refresh_in_background(search_id: int) -> None:
    threading.Thread(target=refresh, args=(search_id,), daemon=True).start()
