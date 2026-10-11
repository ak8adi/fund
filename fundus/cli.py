"""Kommandozeile: fundus serve | run | probe | ntfy-test"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fundus", description="Gebrauchtwaren-Suche über mehrere Plattformen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Weboberfläche + Zeitplan starten")
    sub.add_parser("run", help="Alle aktiven Suchen einmal prüfen")
    p = sub.add_parser("probe", help="Eine Plattform testen und erkannte Inserate anzeigen")
    p.add_argument("platform", help="ricardo | tutti | anibis | ebay:EBAY_DE …")
    p.add_argument("query")
    p.add_argument("--save", action="store_true", help="Rohe Seite unter data/probe-<plattform>.html speichern")
    sub.add_parser("ntfy-test", help="Testmeldung an ntfy senden")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .config import get_config
    from . import db

    cfg = get_config()
    db.init_db()

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("fundus.web.app:app", host=cfg["web"]["host"], port=int(cfg["web"]["port"]), log_level="info")
        return 0

    if args.cmd == "run":
        from .runner import run
        print(run("cli"))
        return 0

    if args.cmd == "ntfy-test":
        from .notify import send_test
        ok = send_test(cfg)
        print("gesendet" if ok else "fehlgeschlagen (Topic gesetzt?)")
        return 0 if ok else 1

    if args.cmd == "probe":
        return _probe(cfg, args.platform, args.query, args.save)
    return 1


def _probe(cfg: dict, platform: str, query: str, save: bool) -> int:
    from .adapters import all_adapters, adapter_for_source
    from .adapters.base import fetch_html
    from .adapters.extract import json_blobs
    from .adapters.scraped import ScrapedAdapter
    from .notify import format_price

    adapters = all_adapters(cfg)
    try:
        adapter = adapter_for_source(adapters, platform)
    except KeyError:
        print(f"Unbekannte Plattform: {platform}")
        return 1
    source = platform if ":" in platform else adapter.sources()[0]

    if isinstance(adapter, ScrapedAdapter):
        url = adapter.search_url(query)
        print(f"URL: {url}")
        page = fetch_html(url, adapter.platform_cfg().get("fetcher", "http"))
        blobs = list(json_blobs(page))
        print(f"Seite: {len(page):,} Zeichen, {len(blobs)} JSON-Blöcke")
        if save:
            out = Path(cfg["data_dir"]) / f"probe-{adapter.name}.html"
            out.write_text(page, encoding="utf-8")
            print(f"gespeichert: {out}")
        listings = adapter.parse(page)
    else:
        listings = adapter.search(query, source, None, None)

    print(f"{len(listings)} Inserate erkannt\n")
    for l in listings[:25]:
        seller = l.seller
        extra = []
        if seller.name:
            extra.append(seller.name)
        if seller.rating_count is not None:
            extra.append(f"{seller.rating_count} Bew.")
        print(f"- {l.title[:70]}\n  {format_price(l.price, l.currency)} · {l.location or '–'} · {' · '.join(extra) or 'keine Verkäuferdaten'}\n  {l.url}")
    return 0 if listings else 2


if __name__ == "__main__":
    sys.exit(main())
