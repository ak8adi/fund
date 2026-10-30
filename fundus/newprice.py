"""Neupreis einer Suche: was kostet das Produkt heute neu in der Schweiz?

1. Toppreise.ch durchsuchen (Schweizer Preisvergleich, günstigstes Angebot inkl. Versand).
2. Die KI wählt aus den Treffern das eigentliche Produkt (kein Zubehör) – oder schätzt den Neupreis aus ihrem
   Wissen, wenn Toppreise nichts Passendes hat (z.B. Kite-Material). Antiquitäten/Unikate: kein Neupreis.
Dabei schätzt sie auch die typische Versandgrösse des Produkts (delivery.SIZES).

Von Hand eingetragene Neupreise haben Vorrang und werden nicht überschrieben.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from . import db
from .adapters.base import fetch_html
from .config import get_config

log = logging.getLogger(__name__)
TOPPREISE = "https://www.toppreise.ch/produktsuche?q={q}"
MAX_AGE_DAYS = 30

PROMPT = """Ich suche gebraucht: «{query}». Wie viel kostet das Produkt heute NEU in der Schweiz?

Treffer vom Schweizer Preisvergleich Toppreise.ch (Name | Anzahl Angebote | günstigster Preis CHF inkl. Versand):
{candidates}

Wähle den Treffer, der genau das gesuchte Produkt ist (kein Zubehör, Ersatzteil, anderes Modell). Bei mehreren
Varianten (Farbe, Grösse) den typischen Preis. Passt keiner, schätze den aktuellen Schweizer Neupreis aus deinem
Wissen (aktuelles Modell). Antiquitäten, Unikate, Vintage ohne Nachfolger: price_chf = null.

Antworte NUR mit JSON:
{{"price_chf": number oder null, "product": "Modell, kurz", "from_list": true wenn aus den Treffern,
 "size": "S"|"M"|"L"|"XL"|"fracht", "weight_kg": number}}
size = Versandgrösse verpackt: S Päckchen bis 35×25×10 cm, M bis 60×30×15 cm, L bis 120×60×60 cm und 31 kg,
XL sperrig bis 200 cm lang, fracht = Möbel o.ä., nicht als Paket versendbar."""


def toppreise(query: str) -> list[tuple[str, int, float]]:
    """(Name, Anzahl Angebote, ab-Preis CHF inkl. Versand) der Suchtreffer."""
    page = fetch_html(TOPPREISE.format(q=quote_plus(query)), "http")
    text = re.sub(r"(\s*\|\s*)+", " | ", re.sub(r"<[^>]+>", " | ", page))
    out = []
    for m in re.finditer(r"\| ([^|]{3,80}(?: \| [^|]{2,80})?) \| (?:Produkt bewerten|\(\d+\)|[^|]{0,20}Bewertung[^|]*) \| "
                         r"(?:[^|]{0,40} \| ){0,3}?(\d+) Angebote? ab \| CHF \| ([\d'’]+(?:\.\d+)?) \| inkl", text):
        out.append((m.group(1).replace(" | ", " ").strip(), int(m.group(2)),
                    float(m.group(3).replace("'", "").replace("’", ""))))
    return out[:30]


def lookup(query: str) -> dict | None:
    from .ai import _gemini_json
    api_key = get_config().get("ai", {}).get("gemini_api_key")
    if not api_key:
        return None
    try:
        cands = toppreise(query)
    except Exception as exc:
        log.info("Toppreise nicht erreichbar: %s", exc)
        cands = []
    lines = "\n".join(f"{n} | {c} | {p:.2f}" for n, c, p in cands) or "(keine Treffer)"
    r = _gemini_json(PROMPT.format(query=query, candidates=lines),
                     "Du bist ein Einkaufsberater für den Schweizer Markt. Antworte nur mit JSON.", api_key)
    r = r[0] if isinstance(r, list) and r else r
    if not isinstance(r, dict):
        return None
    try:
        price = round(float(r["price_chf"])) if r.get("price_chf") else None
    except (TypeError, ValueError):
        price = None
    product = str(r.get("product") or query)[:60]
    if price and r.get("from_list"):
        note = f"{product} · Toppreise.ch"
    elif price:
        note = f"{product} · KI-Schätzung"
    else:
        note = "kein Neupreis (z.B. Antiquität)"
    size = r.get("size") if r.get("size") in ("S", "M", "L", "XL", "fracht") else None
    try:
        weight = float(r["weight_kg"]) if r.get("weight_kg") else None
    except (TypeError, ValueError):
        weight = None
    return {"price": price, "note": note, "size": size, "weight": weight}


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
        db.set_new_price(search_id, s["new_price"], s["new_price_note"], True, r["size"], r["weight"])
    else:
        db.set_new_price(search_id, r["price"], r["note"], False, r["size"], r["weight"])
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
