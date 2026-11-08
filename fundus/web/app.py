"""Weboberfläche: FastAPI + Jinja + HTMX. Läuft lokal, ein Nutzer, kein Login."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import ai, db, newprice, runner, scoring
from ..adapters import all_adapters, adapter_for_source
from ..adapters.base import BlockedError, GoneError, fetch_html
from ..adapters.extract import extract_description_from_page, shipping_from_page
from ..config import get_config, save_settings
from ..matching import excluded
from .. import notify
from ..notify import format_price, image_url, send_test, send_weekly
from ..scheduler import apply_schedule, next_runs, start_scheduler
from ..trust import LEVELS

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
state: dict = {"scheduler": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    db.init_db()
    if db.close_stale_runs():
        log.info("Abgebrochenen Lauf geschlossen")
    runner.available_sources(cfg)
    scoring.rescore_all()
    state["scheduler"] = start_scheduler(cfg)
    if cfg.get("ai", {}).get("gemini_api_key"):
        for sid in db.searches_with_unevaluated_hits():  # z.B. nach abgebrochenem Lauf liegengeblieben
            ai.start_background_evaluation(sid)
    yield
    if state["scheduler"]:
        state["scheduler"].shutdown(wait=False)


app = FastAPI(title="fundus", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
APP_MARKER = "FundusApp"   # steht im User-Agent der Android-App (android/), schaltet die App-Ansicht ein


@app.middleware("http")
async def fresh_pages(request: Request, call_next):
    """Seiten nie aus dem Cache: in der App (WebView) sähe man sonst alte Angebote. Bilder/CSS dürfen gecacht werden."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------- Jinja-Helfer ----------

def _tz() -> ZoneInfo:
    return ZoneInfo(get_config()["schedule"].get("timezone", "Europe/Zurich"))


def ago(value: str | datetime | None) -> str:
    if not value:
        return "–"
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return dt.astimezone(_tz()).strftime("%d.%m. %H:%M")
    if secs < 90:
        return "gerade eben"
    if secs < 3600:
        return f"vor {int(secs // 60)} min"
    if secs < 86400:
        return f"vor {int(secs // 3600)} h"
    if secs < 7 * 86400:
        return f"vor {int(secs // 86400)} T."
    return dt.astimezone(_tz()).strftime("%d.%m.%Y")


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def clock(value: datetime | None) -> str:
    if not value:
        return "–"
    local = value.astimezone(_tz())
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m. %H:%M}"


def left(value: str | datetime | None) -> str:
    """Restlaufzeit: «noch 3 h», «noch 2 T.», «endet heute 18:00»."""
    if not value:
        return ""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "abgelaufen"
    if secs < 3600:
        return f"noch {max(1, int(secs // 60))} min"
    if secs < 86400:
        return f"noch {int(secs // 3600)} h"
    return f"noch {int(secs // 86400)} T."


templates.env.filters["ago"] = ago
templates.env.filters["left"] = left
templates.env.filters["clock"] = clock
templates.env.globals["price"] = format_price
templates.env.globals["trust_labels"] = LEVELS


def _labels() -> dict[str, str]:
    return {a.name: a.label for a in all_adapters(get_config())}


def _source_label(source: str | None, platform: str) -> str:
    if source and source.startswith("EBAY_"):
        return "eBay " + source.split("_", 1)[1]
    return _labels().get(platform, platform)


templates.env.globals["source_label"] = _source_label


def _num(value: str | None) -> float | None:
    """Zahl aus einem Formularfeld; «1'200», «1200.-», «CHF 80» werden verstanden, Unlesbares ergibt None."""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.,]", "", value.replace("'", "")).replace(",", ".").rstrip(".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _ctx(request: Request, **kw) -> dict:
    cfg = get_config()
    evaluated, total = db.get_ai_progress()

    from .. import ai

    return {
        "request": request,
        "running": runner.is_running(),
        "scan": runner.progress(),
        "ai_running": ai.AI_RUNNING,
        "next_runs": next_runs(state["scheduler"]),
        "ntfy_ok": bool(cfg["ntfy"].get("topic")),
        "adapters": [(a.name, a.label) for a in all_adapters(cfg)],
        "cfg": cfg,
        "ai_progress": {"evaluated": evaluated, "total": total},
        "is_app": APP_MARKER in request.headers.get("user-agent", "") or request.cookies.get("fundus_app") == "1",
        **kw,
    }


# ---------- Seiten ----------

@app.post("/api/listing/{key}/edit_description")
def edit_listing_description(request: Request, key: str, description: str = Form("")):
    listing = db.get_listing(key)
    if not listing:
        return HTMLResponse("Nicht gefunden", status_code=404)

    db.update_listing_description(key, description)
    # Neue Beschreibung → Fakten in allen Suchen neu erheben
    db.reset_hit_ai(None, key)
    try:
        with db.connect() as c:
            for row in c.execute("SELECT search_id FROM hits WHERE listing_key = ?", (key,)).fetchall():
                ai.start_background_evaluation(row["search_id"])
    except Exception as e:
        log.error("Fehler beim Starten der KI-Neubewertung: %s", e)

    # Seite neu laden: der Treffer wird neu bewertet und rutscht an seinen neuen Platz
    return HTMLResponse("", headers={"HX-Refresh": "true"})


GO_FRESH_MINUTES = 10  # so frisch geprüft muss ein Inserat beim Klick nicht nochmals abgefragt werden


def _go_page(listing: dict, status: str) -> HTMLResponse:
    text = {"gone": "ist nicht mehr verfügbar – verkauft, gelöscht oder abgelaufen.",
            "reserved": "ist laut Plattform reserviert."}[status]
    from html import escape
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/static/style.css?v=17"><title>fundus</title>
<main class="wrap" style="max-width:560px;margin:12vh auto;padding:0 16px">
<div class="card"><h2 style="margin-top:0">{escape(listing['title'])}</h2>
<p>Dieses Inserat {text}</p>
<p class="muted small">fundus hat es eben auf der Plattform geprüft und in der Suche markiert.</p>
<p><a class="btn" href="javascript:history.back()">Zurück</a>
<a class="btn small" style="margin-left:8px" href="{escape(listing['url'])}" rel="noopener">Trotzdem öffnen</a></p></div></main>""")


@app.get("/go/{key:path}")
def go(key: str):
    """Klick auf ein Angebot: kurz prüfen, ob es noch zu haben ist (eine Abfrage, wie der Browser sie ohnehin macht),
    dann weiterleiten. Ricardo nicht – dort keine Inseratseiten, das Laufzeitende steht schon in der Suche."""
    listing = db.get_listing(key)
    if not listing:
        return RedirectResponse("/", status_code=303)
    if listing.get("gone_at"):
        return _go_page(listing, "gone")
    fresh = listing.get("checked_at") and (datetime.now(timezone.utc) - datetime.fromisoformat(listing["checked_at"])).total_seconds() < GO_FRESH_MINUTES * 60
    paused = db.platform_states().get(listing["platform"], {}).get("blocked_until")
    paused = paused and datetime.fromisoformat(paused) > datetime.now(timezone.utc)
    status = "reserved" if listing.get("reserved") else "ok"
    if listing["platform"] != "ricardo" and not fresh and not paused:
        try:
            status = runner.check_listing(all_adapters(get_config()), listing)
        except Exception as exc:  # Sperre, Zeitüberschreitung …: lieber direkt öffnen als blockieren
            log.info("Klick-Prüfung für %s übersprungen: %s", key, exc)
    if status in ("gone", "reserved"):
        return _go_page(listing, status)
    return RedirectResponse(listing["url"], status_code=302)


@app.get("/api/listing/{key}/description")
def get_listing_description(key: str):
    listing = db.get_listing(key)
    if not listing:
        return HTMLResponse("Nicht gefunden", status_code=404)
    if listing.get("description_full"):
        return HTMLResponse(listing["description_full"])
    if listing["platform"] == "ricardo":  # Ricardo sperrt Einzelabrufe sofort (429) – und damit die ganze Suche
        return HTMLResponse("Ricardo-Beschreibungen lädt fundus nicht einzeln (Ricardo sperrt solche Abrufe). "
                            "Das Inserat zeigt sie – oben auf das Angebot tippen.")
    try:
        if listing["platform"] == "ebay":
            runner.check_listing(all_adapters(get_config()), listing)
            listing = db.get_listing(key)
            if listing.get("gone_at"):
                return HTMLResponse("Inserat nicht mehr verfügbar (verkauft oder beendet).")
            return HTMLResponse(listing.get("description_full") or "Keine Beschreibung hinterlegt.")
        adapter = adapter_for_source(all_adapters(get_config()), listing["platform"])
        if adapter:
            fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
            page = fetch_html(listing["url"], fetcher)
            ai.apply_platform_shipping(key, shipping_from_page(page, listing["platform"]))
            desc = extract_description_from_page(page, listing["platform"])
            if desc:
                db.update_listing_description(key, desc)
                return HTMLResponse(desc)
    except GoneError:
        db.mark_listing_gone(key)
        return HTMLResponse("Inserat nicht mehr verfügbar (verkauft oder gelöscht).")
    except BlockedError as e:
        if listing["platform"] != "ebay":
            db.mark_platform_blocked(listing["platform"], e.retry_after)
        return HTMLResponse("Plattform lässt gerade keine Abfrage zu – fundus pausiert sie eine Weile.")
    except Exception as e:
        log.error("Fehler beim Laden der Beschreibung: %s", e)
    return HTMLResponse("Konnte Beschreibung nicht laden.")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Übersicht: pro Suche eine Zeile – Suchkarte, beste Angebote, Analytics (zum Wischen)."""
    from .. import views
    db.expire_ended_listings()
    searches = db.list_searches()  # eigene Reihenfolge (per Ziehen in der Übersicht)
    return templates.TemplateResponse(request, "index.html", _ctx(
        request,
        rows=[(s, views.build(s["id"])) for s in searches],
        runs=db.last_runs(1),
        health=_health(runner.available_sources(get_config())),
    ))


APP_CHECK_AFTER_RUN = 45   # Minuten nach Scan-Beginn: dann sind Suche und KI-Auswertung meist fertig


@app.get("/api/app/notices")
def app_notices(after: int = -1):
    """Für die Android-App: neue Meldungen seit `after`, ob Scan/Auswertung noch laufen und wann sie wieder fragen soll.
    after=-1 (erster Kontakt): keine alten Meldungen nachliefern, nur den Stand zurückgeben."""
    from fastapi.responses import JSONResponse
    db.kv_set("app_seen", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    busy = _busy()
    notices = [] if after < 0 or busy else db.app_notices_after(after, limit=100)
    last = notices[-1]["id"] if notices else (db.last_app_notice_id() if after < 0 else after)
    public = (get_config()["web"].get("public_url") or "").rstrip("/")
    digest = _digest(notices, public)
    jitter = int(get_config()["schedule"].get("jitter_minutes", 0))
    upcoming = next_runs(state["scheduler"])
    nxt = (upcoming[0] + timedelta(minutes=jitter + APP_CHECK_AFTER_RUN)) if upcoming else \
        datetime.now(timezone.utc) + timedelta(hours=12)
    return JSONResponse({"busy": busy, "last_id": last, "next_check": nxt.astimezone(timezone.utc).isoformat(timespec="seconds"),
                         "notices": [digest] if digest else []})


AI_SETTLE_HOURS = 2   # so lange nach einem Scan auf ausstehende KI-Bewertungen warten


def _busy() -> bool:
    """Läuft noch ein Scan oder die KI – oder warten nach dem letzten Scan noch Angebote auf die KI?
    (Die App meldet erst, wenn wirklich alles fertig ist; fällt Gemini aus, höchstens AI_SETTLE_HOURS warten.)"""
    if runner.is_running() or ai.AI_RUNNING:
        return True
    runs = db.last_runs(1)
    if not runs or not runs[0].get("finished_at") or not get_config().get("ai", {}).get("gemini_api_key"):
        return False
    if datetime.now(timezone.utc) - datetime.fromisoformat(runs[0]["finished_at"]) > timedelta(hours=AI_SETTLE_HOURS):
        return False
    evaluated, total = db.get_ai_progress()
    return evaluated < total


def _digest(notices: list[dict], public: str) -> dict | None:
    """Eine Meldung pro Durchgang: alle Suchen mit neuen Top-Angeboten zusammengefasst."""
    if not notices:
        return None
    if len(notices) == 1:
        n = notices[0]
        return {"id": n["id"], "title": n["title"], "body": n["body"], "url": n["url"] or f"{public}/"}
    by_search: dict = {}
    for n in notices:  # pro Suche nur die neueste Sammelmeldung
        by_search[n["search_id"]] = n
    lines = [f"{n['title']} · {n['body'].splitlines()[0]}" for n in by_search.values()]
    return {"id": notices[-1]["id"], "url": f"{public}/",
            "title": f"fundus: neue Top-Angebote in {len(by_search)} Suchen", "body": "\n".join(lines)}


@app.get("/app.apk")
def app_apk():
    """Android-App zum Installieren übers Tailnet (android/build-android.sh legt sie hier ab)."""
    from fastapi.responses import FileResponse
    apk = HERE.parent.parent / "android" / "fundus.apk"
    if not apk.exists():
        return HTMLResponse("Noch keine App gebaut – auf dem Mac android/build-android.sh ausführen.", status_code=404)
    return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="fundus.apk")


def _health(sources: list[dict]) -> list[dict]:
    """Plattform-Ampel für die Startseite: ok (liefert), zero (0 Treffer), bad (Fehler/Sperre), off."""
    out, seen = [], set()
    for s in sources:
        if not s["ready"]:  # z.B. eBay ohne Schlüssel: einmal pro Plattform statt pro Marktplatz
            if s["adapter"] not in seen:
                seen.add(s["adapter"])
                out.append({"label": s["label"].split(" ")[0], "state": "off", "short": "", "info": s["hint"]})
            continue
        err, count = s["last_error"], s["last_count"]
        if not s["enabled"]:
            st, short, info = "off", "aus", "ausgeschaltet"
        elif err:
            st, short, info = "bad", "pausiert" if err.startswith("Pausiert") else "Fehler", err
        elif not s["last_run"]:
            st, short, info = "off", "", "noch nicht abgefragt"
        else:
            st, short = ("ok", str(count)) if count else ("zero", "0")
            info = f"{count or 0} Treffer im letzten Lauf · {ago(s['last_run'])}"
        out.append({"label": s["label"], "state": st, "short": short, "info": info})
    return out


@app.post("/searches")
def create_search(
    query: str = Form(...),
    exclude: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    platforms: list[str] = Form([]),
    allow_sets: str | None = Form(None),
    require_shipping: str | None = Form(None),
    requirements: str = Form(""),
    new_price: str = Form(""),
    near: str | None = Form(None),
    near_km: str = Form(""),
    near_place: str = Form(""),
    route_to: str = Form(""),
    route_km: str = Form(""),
):
    sid = db.save_search({
        "query": query, "exclude": exclude, "min_price": _num(min_price),
        "max_price": _num(max_price), "platforms": platforms or None,
        "allow_sets": allow_sets == "on", "require_shipping": bool(require_shipping),
        "requirements": requirements, **_area_fields(near, near_km, near_place, route_to, route_km),
    })
    if _num(new_price):
        db.set_new_price(sid, _num(new_price), "von Hand", True)
    runner.run_in_background("neue Suche", [sid])  # Bestand sofort einlesen (ohne Meldungen), sucht auch den Neupreis
    return RedirectResponse(f"/search/{sid}", status_code=303)


def _area_fields(near, near_km: str, near_place: str, route_to: str, route_km: str) -> dict:
    """Formularfelder «In der Nähe»: nur gespeichert, wenn der Haken gesetzt ist. Orte werden gleich nachgeschlagen."""
    if not near:
        return {"near_km": None, "near_place": None, "route_to": None, "route_km": None}
    fields = {"near_km": _num(near_km) or 30, "near_place": near_place.strip() or None,
              "route_to": route_to.strip() or None, "route_km": _num(route_km) or None}
    try:
        from .. import area
        area.for_search(fields)  # Startort und Strecke einmal nachschlagen (danach aus dem Cache)
    except Exception as exc:
        log.warning("Gebiet nicht bestimmbar: %s", exc)
    return fields


def _mark_filtered(hits: list[dict], search: dict) -> None:
    """Für die Liste: ausgeschaltete Plattform bzw. ausserhalb des Gebiets («nur in der Nähe») → unter «Ausgefiltert»."""
    from .. import area
    region = area.for_search(search)
    for h in hits:
        if search.get("platforms") and h["platform"] not in search["platforms"]:
            h["_inactive_platform"] = True
        elif region and h.get("ai_relevant") != 0 and (why := area.verdict(search, region, h)):
            h["_outside"] = why


def _sort_hits(hits: list[dict], search: dict) -> None:
    """Deal-Score (Standard) oder stur nach Endpreis in CHF, günstigster zuerst; Inserate ohne Preis ans Ende.
    Noch nicht von der KI geprüfte Inserate kommen nach den geprüften – sonst stünde neues Zubehör für
    ein paar Franken bis zur Prüfung zuoberst."""
    if search.get("sort_by") == "price":
        fx = fx_rates.rates(get_config())
        def chf(h):
            p = scoring.pay_price_chf(h, fx)  # Endpreis «bis zu dir», wie auf der Karte
            return (p is None, p or 0)
        hits.sort(key=lambda x: (bool(x.get("gone_at")), x.get("ai_relevant") is None, *chf(x)))
    else:
        hits.sort(key=lambda x: (bool(x.get("gone_at")), x.get("ai_score") is None, -(x.get("ai_score") or 0)))


@app.get("/search/{sid}", response_class=HTMLResponse)
def show_search(request: Request, sid: int):
    search = db.get_search(sid)
    if not search:
        return RedirectResponse("/", status_code=303)
    db.expire_ended_listings()  # abgelaufene Ricardo-/eBay-Inserate sofort als weg zeigen
    watching = runner.watch_in_background(sid)  # Angebote oben, die länger als 30 Min. ungeprüft sind
    hits = db.hits_for_search(sid)
    _mark_filtered(hits, search)
    _sort_hits(hits, search)
    img_done, img_total = db.image_progress(sid, search["image_prompt"]) if search.get("image_prompt") else (0, 0)
    from .. import views
    resp = templates.TemplateResponse(request, "search.html", _ctx(request, search=search, hits=hits, views=views.build(sid),
                                                                    watching=watching,
                                                                    img_progress={"done": img_done, "total": img_total}))
    db.mark_seen(sid)  # nach dem Rendern: «neu»-Markierung ist jetzt angeschaut
    return resp

@app.get("/search/{sid}/fresh")
def search_fresh(sid: int):
    """Für die Suchseite: läuft die Verfügbarkeitsprüfung noch, und welche Angebote stehen jetzt oben?"""
    from fastapi.responses import JSONResponse
    from .. import views
    offers = views.build(sid)["offers"]
    top = [o["key"] for o in offers if o["front"]] + [o["key"] for o in offers if not o["front"] and not o["ko"]][:6]
    return JSONResponse({"watching": runner.is_watching(sid), "top": top})


@app.get("/search/{sid}/results", response_class=HTMLResponse)

def search_results(request: Request, sid: int):

    search = db.get_search(sid)

    if not search:

        return ""

    hits = db.hits_for_search(sid)
    _mark_filtered(hits, search)
    _sort_hits(hits, search)
    return templates.TemplateResponse(request, "_search_results.html", _ctx(request, search=search, hits=hits))



@app.post("/search/{sid}")
def update_search(
    sid: int,
    query: str = Form(...),
    exclude: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    platforms: list[str] = Form([]),
    allow_sets: str | None = Form(None),
    require_shipping: str | None = Form(None),
    requirements: str = Form(""),
    new_price: str = Form(""),
    near: str | None = Form(None),
    near_km: str = Form(""),
    near_place: str = Form(""),
    route_to: str = Form(""),
    route_km: str = Form(""),
):
    old_search = db.get_search(sid)
    db.save_search({
        "query": query, "exclude": exclude, "min_price": _num(min_price),
        "max_price": _num(max_price), "platforms": platforms or None,
        "allow_sets": allow_sets == "on", "require_shipping": bool(require_shipping),
        "requirements": requirements, **_area_fields(near, near_km, near_place, route_to, route_km),
    }, sid)

    if old_search:
        manual = _num(new_price)
        if manual:
            if manual != old_search.get("new_price") or not old_search.get("new_price_manual"):
                db.set_new_price(sid, manual, "von Hand", True)
                scoring.rescore_search(sid)
            if old_search["query"] != query.strip():
                newprice.refresh_in_background(sid)  # Produktgrösse zur neuen Suche
        elif old_search.get("new_price_manual") or old_search["query"] != query.strip():
            db.set_new_price(sid, None, None, False)  # wieder automatisch
            newprice.refresh_in_background(sid)

    search = db.get_search(sid)
    if search:
        changed = _apply_search_changes(sid, old_search, search)
        if old_search and (old_search.get("requirements") or "") != search["requirements"]:
            db.reset_search_ai(sid)  # neue Anforderungen: passende Treffer neu von der KI prüfen lassen
            changed = True
        if old_search and platforms and (not old_search["platforms"] or set(platforms) - set(old_search["platforms"])):
            runner.run_in_background("neue Plattformen", [sid])  # bewertet danach auch mit der KI
        elif changed:
            ai.start_background_evaluation(sid)

    return RedirectResponse(f"/search/{sid}", status_code=303)


def _apply_search_changes(sid: int, old: dict | None, search: dict) -> int:
    """Geänderte Ausschlusswörter und Set-Einstellung auf bestehende Treffer anwenden.
    Gibt die Zahl der Treffer zurück, die die KI neu prüfen muss."""
    if not old:
        return 0
    from .. import area
    reset = 0
    was, now_ = area.rejects_pickup(old), area.rejects_pickup(search)
    if was != now_:  # «nur Versand» bzw. «oder in der Nähe» geändert: Abholangebote neu einordnen
        for h in db.hits_for_search(sid, limit=5000):
            if was and h.get("ai_reason") == db.NO_SHIPPING:
                db.reset_hit_ai(sid, h["key"])
                reset += 1
            elif now_ and h.get("ai_relevant") == 1 and (h.get("ship_flag") or h.get("ai_shipping")) == "nein":
                db.set_hit_relevant(sid, h["key"], False, db.NO_SHIPPING)
    sets_changed = old["allow_sets"] != search["allow_sets"]
    words_changed = old["exclude_words"] != search["exclude_words"]
    if not (sets_changed or words_changed):
        if reset or was != now_:
            scoring.rescore_search(sid)
        return reset
    for h in db.hits_for_search(sid, limit=5000):
        is_excluded = excluded(search["exclude_words"], h.get("title"))  # wie im Prüflauf
        reason = (h.get("ai_reason") or "").lower()
        if is_excluded:
            if h.get("ai_relevant") != 0:
                db.set_hit_relevant(sid, h["key"], False, "Ausgeschlossener Begriff")
        elif reason == "ausgeschlossener begriff":
            db.reset_hit_ai(sid, h["key"])  # Wort entfernt: wieder prüfen
            reset += 1
        elif sets_changed and h.get("ai_relevant") == 0 and search["allow_sets"] and ("set" in reason or "bundle" in reason):
            db.reset_hit_ai(sid, h["key"])  # Sets jetzt erlaubt
            reset += 1
        elif sets_changed and h.get("ai_relevant") == 1 and not search["allow_sets"] and h.get("ai_set_items"):
            db.reset_hit_ai(sid, h["key"])  # Sets nicht mehr erlaubt: KI prüft, ob einzeln verkauft
            reset += 1
    scoring.rescore_search(sid)
    return reset


@app.post("/search/{sid}/newprice")
def refresh_new_price(request: Request, sid: int):
    newprice.refresh_in_background(sid)
    back = request.headers.get("referer", "")
    return RedirectResponse(f"/search/{sid}/neu-kaufen" if back.endswith("/neu-kaufen") else f"/search/{sid}", status_code=303)


@app.get("/search/{sid}/analytics", response_class=HTMLResponse)
def analytics(request: Request, sid: int):
    """Marktlage, Lieferwege, Preis-Wert-Karte, Preisleiter und verpasste Angebote einer Suche."""
    from .. import views
    search = db.get_search(sid)
    if not search:
        return RedirectResponse("/", status_code=303)
    db.expire_ended_listings()
    return templates.TemplateResponse(request, "analytics.html", _ctx(request, search=search, views=views.build(sid)))


@app.get("/search/{sid}/neu-kaufen", response_class=HTMLResponse)
def buy_new(request: Request, sid: int):
    """Neu kaufen: passende Shopangebote mit Versand, Import und Endpreis bis zu dir."""
    from .. import views
    search = db.get_search(sid)
    if not search:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "neukaufen.html", _ctx(
        request, search=search, views=views.build(sid), options=newprice.buy_options(search)))


@app.post("/searches/order")
async def order_searches(request: Request):
    """Übersicht per Ziehen sortiert: {"ids": [oben … unten]}."""
    from fastapi.responses import JSONResponse
    ids = [int(i) for i in (await request.json()).get("ids", []) if str(i).isdigit()]
    known = {s["id"] for s in db.list_searches()}
    db.set_search_order([i for i in ids if i in known])
    return JSONResponse({"ok": True})


@app.get("/neue-suche", response_class=HTMLResponse)
def new_search_page(request: Request):
    return templates.TemplateResponse(request, "neue_suche.html", _ctx(request))


@app.post("/search/{sid}/sort")
def sort_search(sid: int, by: str = Form("score")):
    db.set_search_sort(sid, by)
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/toggle")
def toggle_search(sid: int):
    s = db.get_search(sid)
    if s:
        db.set_search_active(sid, not s["active"])
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/delete")
def remove_search(sid: int):
    db.delete_search(sid)
    return RedirectResponse("/", status_code=303)


@app.post("/search/{sid}/run")
def run_one(sid: int):
    runner.run_in_background("manuell", [sid])
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/ai")
def run_ai(sid: int):
    ai.start_background_evaluation(sid)  # im Hintergrund – die Seite zeigt den Fortschritt
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/images")
def analyze_images(sid: int, image_prompt: str = Form("")):
    """Fotos aller passenden Angebote mit diesem Auftrag auswerten; bleibt danach für neue Angebote aktiv."""
    prompt = image_prompt.strip()[:500]
    if db.get_search(sid):
        db.set_image_analysis(sid, prompt, bool(prompt))
        if prompt:
            ai.start_background_evaluation(sid)  # erst offene Text-Bewertungen, dann die Fotos
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/images/off")
def images_off(sid: int):
    search = db.get_search(sid)
    if search:  # Auftrag und bisherige Befunde bleiben, nur neue Angebote werden nicht mehr automatisch geprüft
        db.set_image_analysis(sid, search.get("image_prompt") or "", False)
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/hide", response_class=HTMLResponse)
def hide(sid: int, key: str = Form(...)):
    db.hide_hit(sid, key)
    scoring.rescore_search(sid)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@app.post("/search/{sid}/restore", response_class=HTMLResponse)
def restore_hit(sid: int, key: str = Form(...)):
    db.set_hit_relevant(sid, key, True, "Manuell eingeblendet")
    scoring.rescore_search(sid)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@app.post("/run")
def run_all(request: Request):
    runner.run_in_background("manuell")
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_status.html", _ctx(request, running=True, runs=db.last_runs(1)))
    return RedirectResponse("/", status_code=303)


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    return templates.TemplateResponse(request, "_status.html", _ctx(request, runs=db.last_runs(1)))


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, test: str | None = None, saved: str | None = None):
    cfg = get_config()
    return templates.TemplateResponse(request, "settings.html", _ctx(
        request,
        sources=runner.available_sources(cfg),
        cfg=cfg,
        searches=db.list_searches(),
        weekdays=[("mon", "Montag"), ("tue", "Dienstag"), ("wed", "Mittwoch"), ("thu", "Donnerstag"),
                  ("fri", "Freitag"), ("sat", "Samstag"), ("sun", "Sonntag")],
        test=test,
        saved=saved,
        runs=db.last_runs(8),
        app_seen=db.kv_get("app_seen"),
        app_active=notify.app_active(),
    ))


@app.post("/platforms/{name}/toggle", response_class=HTMLResponse)
def toggle_platform(request: Request, name: str):
    states = db.platform_states()
    if name in states:
        db.set_platform_enabled(name, not states[name]["enabled"])
    src = next(s for s in runner.available_sources(get_config()) if s["name"] == name)
    return templates.TemplateResponse(request, "_source.html", {"request": request, "s": src})


@app.post("/ntfy/test")
def ntfy_test():
    ok = send_test(get_config())
    return RedirectResponse(f"/settings?test={'ok' if ok else 'fail'}", status_code=303)


@app.post("/settings/ntfy")
def save_ntfy(server: str = Form("https://ntfy.sh"), topic: str = Form(""), token: str = Form("")):
    save_settings("ntfy", {"server": server.strip() or "https://ntfy.sh", "topic": topic.strip(), "token": token.strip()})
    return RedirectResponse("/settings?saved=ntfy", status_code=303)


@app.post("/settings/ebay")
def save_ebay(app_id: str = Form(""), cert_id: str = Form("")):
    save_settings("ebay", {"app_id": app_id.strip(), "cert_id": cert_id.strip()})
    return RedirectResponse("/settings?saved=ebay", status_code=303)


@app.post("/settings/ai")
def save_ai(gemini_api_key: str = Form("")):
    save_settings("ai", {"gemini_api_key": gemini_api_key.strip()})
    return RedirectResponse("/settings?saved=ai", status_code=303)


@app.post("/settings/schedule")
def save_schedule(times: str = Form(""), jitter_minutes: str = Form("0"), pause_after_manual_hours: str = Form("3")):
    parts = [t for t in re.split(r"[,;\s]+", times.strip()) if t]
    valid = []
    for t in parts:
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", t)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            return RedirectResponse("/settings?saved=schedule-error", status_code=303)
        valid.append(f"{int(m[1]):02d}:{m[2]}")
    jitter = int(jitter_minutes) if jitter_minutes.strip().isdigit() else 0
    valid = sorted(set(valid))
    try:
        pause = min(max(float(pause_after_manual_hours.replace(",", ".")), 0.0), 48.0)
    except ValueError:
        pause = 3.0
    save_settings("schedule", {"times": valid, "jitter_minutes": min(jitter, 120), "pause_after_manual_hours": pause})
    apply_schedule(state["scheduler"], get_config())
    return RedirectResponse("/settings?saved=schedule", status_code=303)


@app.post("/settings/notify")
async def save_notify(request: Request):
    form = await request.form()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(form.get("weekly_time", "")).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return RedirectResponse("/settings?saved=notify-error", status_code=303)
    day = str(form.get("weekly_day", "mon"))
    save_settings("ntfy", {
        "on_new": "on_new" in form, "weekly": "weekly" in form,
        "weekly_day": day if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} else "mon",
        "weekly_time": f"{int(m[1]):02d}:{m[2]}",
    })
    for s in db.list_searches():
        db.set_search_notify(s["id"], f"new_{s['id']}" in form, f"weekly_{s['id']}" in form)
    apply_schedule(state["scheduler"], get_config())
    return RedirectResponse("/settings?saved=notify", status_code=303)


@app.post("/ntfy/weekly")
def weekly_now():
    ok = send_weekly(get_config(), db.weekly_summary())
    return RedirectResponse(f"/settings?test={'ok' if ok else 'fail'}", status_code=303)


@app.post("/api/listing/{key:path}/generate_question", response_class=HTMLResponse)
def api_generate_question(request: Request, key: str, search_id: int = Form(...)):
    ai.generate_seller_question(key, search_id)
    h = next((x for x in db.hits_for_search(search_id, include_hidden=True, limit=5000) if x["key"] == key), None)
    if not h:
        return HTMLResponse("")
    h["search_id"] = search_id
    return templates.TemplateResponse(request, "_hit.html", _ctx(request, h=h))

@app.post("/api/listing/{key:path}/update_chat", response_class=HTMLResponse)
def api_update_chat(request: Request, key: str, search_id: int = Form(...), seller_chat: str = Form("")):
    db.update_listing_chat(key, seller_chat)
    if ai.flags_scam(seller_chat):  # eigener Vermerk «Betrug»: sofort ausblenden, auch ohne KI
        db.set_hit_relevant(search_id, key, False, db.SCAM)
        scoring.rescore_search(search_id)
    ai.reevaluate_single_listing(key, search_id)
    # Neu laden statt nur die Karte zu ersetzen: Platz in der Liste, Ausblenden und Sortierung stimmen sofort
    return HTMLResponse("", headers={"HX-Refresh": "true"})


from markupsafe import Markup as _Markup

from .. import fx as fx_rates

def _round_chf_generous(val: float) -> int:
    if val >= 100:
        return int(round(val / 5.0) * 5)
    elif val >= 20:
        return int(round(val))
    return max(1, int(round(val)))

def price_chf_html(amount, currency: str | None = "CHF", approx: bool = False):
    """Preis in CHF; Fremdwährung mit «~» und Originalbetrag. approx: geschätzt, «≈» statt «~»."""
    if amount is None:
        return "VB / k.A."
    try:
        val = float(amount)
    except Exception:
        return str(amount)
    cur = (currency or "CHF").upper().strip()
    if cur in ("€", "EUR"): cur = "EUR"
    elif cur in ("KR", "KR.", "SEK"): cur = "SEK"
    elif cur in ("$", "USD"): cur = "USD"
    elif cur in ("£", "GBP"): cur = "GBP"
    elif cur in ("CHF", "FR", "FR.", "SFR"): cur = "CHF"

    def fmt(v: float) -> str:
        return f"{v:,.0f}".replace(",", "'")

    if cur == "CHF":  # Frankenpreise genau zeigen; gerundet wird nur Geschätztes
        return _Markup(f"≈ {fmt(_round_chf_generous(val))} CHF" if approx else f"{fmt(val)} CHF")

    rates = fx_rates.rates(get_config())
    rate = rates.get(cur, 0.94)
    chf_r = _round_chf_generous(val * rate)
    return _Markup(f'{"≈ " if approx else "~"}{fmt(chf_r)} CHF <span class="orig-price">({fmt(val)} {cur})</span>')

templates.env.globals['price'] = price_chf_html
templates.env.filters['img'] = lambda url: image_url(url, large=False)
