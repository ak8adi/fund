"""Neupreis einer Suche: was kostet das Produkt heute neu – mit Shop und Link?

1. Online suchen (DuckDuckGo, Region Schweiz): «<Suche> kaufen», Marktplätze ausgenommen.
2. Jede Shopseite öffnen und Produktname, Preis, Währung aus den strukturierten Produktdaten lesen
   (JSON-LD/schema.org bzw. Meta-Tags – dieselben Angaben, die Shops für Google bereitstellen).
3. Die KI prüft nur, welche Seiten genau das gesuchte Produkt neu anbieten (kein Zubehör, anderes Modell,
   Gebrauchtware) – sie schätzt keinen Preis. Neupreis = günstigster passender Shop in CHF.
Nichts gefunden → kein Neupreis (dann von Hand eintragen). Von Hand eingetragene Preise haben Vorrang.
Nebenbei schätzt die KI die typische Versandgrösse des Produkts (delivery.SIZES).
"""

from __future__ import annotations

import html
import json
import logging
import re
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
SKIP_DOMAINS = ("ricardo.", "tutti.", "anibis.", "kleinanzeigen.", "ebay.", "willhaben.", "blocket.", "markt.de",
                "youtube.", "facebook.", "instagram.", "reddit.", "pinterest.", "wikipedia.", "idealo.", "toppreise.",
                "google.", "amazon.", "aliexpress.", "temu.")

PROMPT = """Ich suche gebraucht: «{query}». Unten Produktseiten aus Online-Shops (Nr | Shop | Produktname | Preis | Zustand).
Welche bieten GENAU dieses Produkt NEU an? Passend, wenn Marke, Modellname und Modellnummer übereinstimmen
(Wortreihenfolge und Zusätze wie «Control Bar», Grössenangaben oder Jahrgang egal). Nicht passend: Zubehör, Ersatzteile, anderes Modell oder andere
Produktart, Sets mit weiteren Produkten, Gebraucht-/Vorführware, Miete, Spezialversionen, die nicht gesucht sind
(z.B. Foil-, Kinder-, Lite-Version). Verschiedene Grössen, Farben oder Modelljahre desselben Produkts passen.

{rows}

Antworte NUR mit JSON: {{"match": [Nr, ...], "product": "Modell, kurz", "size": "S"|"M"|"L"|"XL"|"fracht",
"weight_kg": number}}
size = Versandgrösse verpackt: S Päckchen bis 35×25×10 cm, M bis 60×30×15 cm, L bis 120×60×60 cm und 31 kg,
XL sperrig bis 200 cm lang, fracht = Möbel o.ä., nicht als Paket versendbar."""


def search_links(query: str, skip: list[str] | None = None) -> list[str]:
    page = fetch_html(SEARCH_URL.format(q=quote_plus(query)), "http")
    out: list[str] = []
    for raw in re.findall(r'uddg=([^&"]+)', page):
        url = unquote(raw)
        host = urlparse(url).netloc.lower()
        if not host or any(d in host for d in SKIP_DOMAINS) or url in out or url in (skip or []):
            continue
        out.append(url)
    return out[:MAX_SHOPS]


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


def _offer_price(offers) -> tuple[float, str, str] | None:
    """(Preis, Währung, Zustand) – bei mehreren Angeboten/Varianten das günstigste."""
    found = []
    for o in offers if isinstance(offers, list) else [offers]:
        if not isinstance(o, dict):
            continue
        price = o.get("price") or o.get("lowPrice")
        if price is None and isinstance(o.get("priceSpecification"), dict):
            price = o["priceSpecification"].get("price")
        try:
            value = float(str(price).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if value > 0:
            cond = str(o.get("itemCondition") or "").rsplit("/", 1)[-1]
            found.append((value, str(o.get("priceCurrency") or "").upper(), cond))
    return min(found) if found else None


def extract_offer(html: str) -> dict | None:
    """Hauptprodukt einer Shopseite: Name, Preis, Währung, Zustand."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for p in _products(data):
            offer = _offer_price(p.get("offers")) or _offer_price(
                [v.get("offers") for v in p.get("hasVariant", []) if isinstance(v, dict)])
            if offer:
                return {"name": str(p.get("name") or "")[:120], "price": offer[0], "currency": offer[1],
                        "condition": offer[2]}
    amount = re.search(r'<meta[^>]+(?:product:price:amount|og:price:amount)[^>]+content="([\d.,]+)"', html)
    currency = re.search(r'<meta[^>]+(?:product:price:currency|og:price:currency)[^>]+content="([A-Za-z]{3})"', html)
    title = re.search(r'<meta[^>]+og:title[^>]+content="([^"]+)"', html)
    if amount and currency:
        try:
            return {"name": (title.group(1) if title else "")[:120], "price": float(amount.group(1).replace(",", ".")),
                    "currency": currency.group(1).upper(), "condition": ""}
        except ValueError:
            return None
    return None


def find_offers(query: str, skip: list[str] | None = None) -> list[dict]:
    """Shopangebote mit Preis in CHF (noch ungeprüft)."""
    rates = fx_rates.rates(get_config())
    offers = []
    for url in search_links(query, skip):
        try:
            offer = extract_offer(fetch_html(url, "http"))
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
        offers.append({**offer, "currency": cur, "url": url, "shop": urlparse(url).netloc.removeprefix("www."),
                       "price_chf": round(offer["price"] * rates[cur], 2)})
    return offers


def lookup(query: str) -> dict | None:
    from .ai import _gemini_json
    api_key = get_config().get("ai", {}).get("gemini_api_key")
    if not api_key:
        return None
    offers = find_offers(f"{query} kaufen")
    if len(offers) < 6:  # wenig Shopseiten: zweite Suche mit anderem Wortlaut
        offers += find_offers(f"{query} preis neu", [o["url"] for o in offers])
    rows = "\n".join(f"{i} | {o['shop']} | {o['name']} | {o['price']:g} {o['currency']} | {o['condition'] or '-'}"
                     for i, o in enumerate(offers)) or "(keine Shopseiten gefunden)"
    r = _gemini_json(PROMPT.format(query=query, rows=rows),
                     "Du prüfst Produktseiten für einen Preisvergleich. Antworte nur mit JSON.", api_key,
                     ("gemini-3.7-flash", "gemini-3.5-flash-lite"))  # selten, aber heikel: stärkeres Modell zuerst
    r = r[0] if isinstance(r, list) and r else r
    if not isinstance(r, dict):
        return None
    picked = [offers[i] for i in r.get("match") or [] if isinstance(i, int) and 0 <= i < len(offers)]
    picked = [o for o in picked if "used" not in o["condition"].lower() and "refurb" not in o["condition"].lower()]
    picked.sort(key=lambda o: o["price_chf"])
    unique, seen = [], set()
    for o in picked:  # gleiche Seite in Farbvarianten oder doppelt gefunden: nur einmal
        sig = (o["shop"], o["name"].lower(), round(o["price_chf"]))
        if sig not in seen:
            seen.add(sig)
            unique.append(o)
    picked = unique
    size = r.get("size") if r.get("size") in ("S", "M", "L", "XL", "fracht") else None
    try:
        weight = float(r["weight_kg"]) if r.get("weight_kg") else None
    except (TypeError, ValueError):
        weight = None
    if not picked:
        return {"price": None, "note": "Kein Neupreis online gefunden – bei Bedarf von Hand eintragen",
                "offers": [], "size": size, "weight": weight}
    best = picked[0]  # günstigstes passendes Angebot
    price = round(best["price_chf"])
    product = str(r.get("product") or query)[:60]
    shops = len({o["shop"] for o in picked})
    note = f"{product} · günstigster bei {best['shop']}" + (f" (von {shops} Shops)" if shops > 1 else "")
    keep = [{k: o[k] for k in ("shop", "name", "price", "currency", "price_chf", "url")} for o in picked[:8]]
    return {"price": price, "note": note, "offers": keep, "size": size, "weight": weight}


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
