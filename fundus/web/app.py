"""Weboberfläche: FastAPI + Jinja + HTMX. Läuft lokal, ein Nutzer, kein Login."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import ai, db, newprice, runner, scoring
from ..adapters import all_adapters, adapter_for_source
from ..adapters.base import GoneError, fetch_html
from ..adapters.extract import extract_description_from_page
from ..config import get_config, save_settings
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


templates.env.filters["ago"] = ago
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
    if value is None or not value.strip():
        return None
    return float(value.replace("'", "").replace(",", "."))


def _ctx(request: Request, **kw) -> dict:
    cfg = get_config()
    evaluated, total = db.get_ai_progress()

    from .. import ai

    return {
        "request": request,
        "running": runner.is_running(),
        "ai_running": ai.AI_RUNNING,
        "next_runs": next_runs(state["scheduler"]),
        "ntfy_ok": bool(cfg["ntfy"].get("topic")),
        "adapters": [(a.name, a.label) for a in all_adapters(cfg)],
        "cfg": cfg,
        "ai_progress": {"evaluated": evaluated, "total": total},
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

    # Re-fetch listing with new data
    h = db.get_listing(key)
    if not h:
        return HTMLResponse("", status_code=404)

    # Fetch hit info to inject into `h` like in `hits_for_search`
    with db.connect() as c:
        hit = c.execute("SELECT search_id, first_seen, is_new FROM hits WHERE listing_key = ?", (key,)).fetchone()
        if hit:
            h["search_id"] = hit["search_id"]
            h["hit_seen"] = hit["first_seen"]
            h["is_new"] = hit["is_new"]

            search = db.get_search(hit["search_id"])
            if search:
                h["search_query"] = search["query"]
                if search.get("platforms") and h["platform"] not in search["platforms"]:
                    h["_inactive_platform"] = True

    return templates.TemplateResponse(request, "_hit.html", _ctx(request, h=h))


@app.get("/api/listing/{key}/description")
def get_listing_description(key: str):
    listing = db.get_listing(key)
    if not listing:
        return HTMLResponse("Nicht gefunden", status_code=404)
    if listing.get("description_full"):
        return HTMLResponse(listing["description_full"])
    if listing["platform"] in ("ebay", "ricardo"):
        return HTMLResponse("Für diese Plattform nicht unterstützt.")
    try:
        adapter = adapter_for_source(all_adapters(get_config()), listing["platform"])
        if adapter:
            fetcher = adapter.platform_cfg().get("fetcher", "http") if hasattr(adapter, "platform_cfg") else "http"
            page = fetch_html(listing["url"], fetcher)
            desc = extract_description_from_page(page, listing["platform"])
            if desc:
                db.update_listing_description(key, desc)
                return HTMLResponse(desc)
    except GoneError:
        db.mark_listing_gone(key)
        return HTMLResponse("Inserat nicht mehr verfügbar (verkauft oder gelöscht).")
    except Exception as e:
        log.error("Fehler beim Laden der Beschreibung: %s", e)
    return HTMLResponse("Konnte Beschreibung nicht laden.")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(
        request,
        searches=db.list_searches(),
        hits=db.recent_new_hits(40),
        best=db.best_hits(1),
        runs=db.last_runs(1),
        health=_health(runner.available_sources(get_config())),
    ))


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
):
    sid = db.save_search({
        "query": query, "exclude": exclude, "min_price": _num(min_price),
        "max_price": _num(max_price), "platforms": platforms or None,
        "allow_sets": allow_sets == "on", "require_shipping": bool(require_shipping),
        "requirements": requirements,
    })
    if _num(new_price):
        db.set_new_price(sid, _num(new_price), "von Hand", True)
    runner.run_in_background("neue Suche", [sid])  # Bestand sofort einlesen (ohne Meldungen), sucht auch den Neupreis
    return RedirectResponse(f"/search/{sid}", status_code=303)


def _sort_hits(hits: list[dict], search: dict) -> None:
    """Deal-Score (Standard) oder stur nach Preis in CHF, günstigster zuerst; Inserate ohne Preis ans Ende.
    Noch nicht von der KI geprüfte Inserate kommen nach den geprüften – sonst stünde neues Zubehör für
    ein paar Franken bis zur Prüfung zuoberst."""
    if search.get("sort_by") == "price":
        fx = fx_rates.rates(get_config())
        def chf(h):
            p = scoring.effective_price_chf(h, fx)
            return (p is None, p or 0)
        hits.sort(key=lambda x: (bool(x.get("gone_at")), x.get("ai_relevant") is None, *chf(x)))
    else:
        hits.sort(key=lambda x: (bool(x.get("gone_at")), x.get("ai_score") is None, -(x.get("ai_score") or 0)))


@app.get("/search/{sid}", response_class=HTMLResponse)
def show_search(request: Request, sid: int):
    search = db.get_search(sid)
    if not search:
        return RedirectResponse("/", status_code=303)
    hits = db.hits_for_search(sid)

    for h in hits:
        if search.get("platforms") and h["platform"] not in search["platforms"]:
            h["_inactive_platform"] = True

    _sort_hits(hits, search)
    resp = templates.TemplateResponse(request, "search.html", _ctx(request, search=search, hits=hits))
    db.mark_seen(sid)  # nach dem Rendern: «neu»-Markierung ist jetzt angeschaut
    return resp

@app.get("/search/{sid}/results", response_class=HTMLResponse)

def search_results(request: Request, sid: int):

    search = db.get_search(sid)

    if not search:

        return ""

    hits = db.hits_for_search(sid)

    for h in hits:

        if search.get("platforms") and h["platform"] not in search["platforms"]:

            h["_inactive_platform"] = True

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
):
    old_search = db.get_search(sid)
    db.save_search({
        "query": query, "exclude": exclude, "min_price": _num(min_price),
        "max_price": _num(max_price), "platforms": platforms or None,
        "allow_sets": allow_sets == "on", "require_shipping": bool(require_shipping),
        "requirements": requirements,
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
    if search and old_search and (old_search.get("requirements") or "") != search["requirements"]:
        # Neue Anforderungen: alle Treffer neu von der KI prüfen lassen
        db.reset_search_ai(sid)
        from ..ai import start_background_evaluation
        start_background_evaluation(sid)
    elif search:
        hits = db.hits_for_search(sid, include_hidden=False)

        allow_sets_changed = old_search and (old_search["allow_sets"] != search["allow_sets"])

        for h in hits:
            # Re-evaluate due to allow_sets changing from False to True
            if allow_sets_changed and search["allow_sets"] and h.get("ai_relevant") is False and h.get("ai_reason"):
                reason = h["ai_reason"].lower()
                if "set" in reason or "bundle" in reason:
                    db.reset_hit_ai(sid, h["key"])
                    continue

            # Standard exclude words check
            if h.get("ai_relevant") is not False:
                txt = f"{h.get('title','')} {h.get('description_full','')}".lower()
                if any(w in txt for w in search["exclude_words"]):
                    db.set_hit_relevant(sid, h["key"], False, "Ausgeschlossener Begriff")
                # Sets not allowed anymore
                elif not search["allow_sets"] and h.get("ai_relevant") is not False:
                    # Check if ai_reason mentions set/bundle, or if title does
                    reason = (h.get("ai_reason") or "").lower()
                    if "set" in reason or "bundle" in reason or "komplett" in txt or "set" in txt or "bundle" in txt or "+" in txt:
                        db.reset_hit_ai(sid, h["key"])

        if old_search and platforms and (not old_search["platforms"] or set(platforms) - set(old_search["platforms"])):
            runner.run_in_background("neue Plattformen", [sid])

        if allow_sets_changed:
            from ..ai import start_background_evaluation
            start_background_evaluation(sid)

    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.post("/search/{sid}/newprice")
def refresh_new_price(sid: int):
    newprice.refresh_in_background(sid)
    return RedirectResponse(f"/search/{sid}", status_code=303)


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
    from ..ai import evaluate_listings
    try:
        evaluate_listings(sid)
    except Exception as e:
        log.error("AI Error: %s", e)
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
def save_schedule(times: str = Form(""), jitter_minutes: str = Form("0")):
    parts = [t for t in re.split(r"[,;\s]+", times.strip()) if t]
    valid = []
    for t in parts:
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", t)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            return RedirectResponse("/settings?saved=schedule-error", status_code=303)
        valid.append(f"{int(m[1]):02d}:{m[2]}")
    jitter = int(jitter_minutes) if jitter_minutes.strip().isdigit() else 0
    valid = sorted(set(valid))
    save_settings("schedule", {"times": valid, "jitter_minutes": min(jitter, 120)})
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
    hits = _enriched_hits_for_search(search_id) if "_enriched_hits_for_search" in globals() else db.hits_for_search(search_id)
    h = next((x for x in hits if x["key"] == key), None)
    if not h:
        return HTMLResponse("")
    h["search_id"] = search_id
    return templates.TemplateResponse(request, "_hit.html", _ctx(request, h=h))

@app.post("/api/listing/{key:path}/update_chat", response_class=HTMLResponse)
def api_update_chat(request: Request, key: str, search_id: int = Form(...), seller_chat: str = Form("")):
    db.update_listing_chat(key, seller_chat)
    ai.reevaluate_single_listing(key, search_id)
    hits = _enriched_hits_for_search(search_id) if "_enriched_hits_for_search" in globals() else db.hits_for_search(search_id)
    h = next((x for x in hits if x["key"] == key), None)
    if not h:
        return HTMLResponse("")
    h["search_id"] = search_id
    return templates.TemplateResponse(request, "_hit.html", _ctx(request, h=h))


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

    if cur == "CHF":
        chf_r = _round_chf_generous(val)
        return _Markup(f"{'≈ ' if approx else ''}{chf_r} CHF")

    rates = fx_rates.rates(get_config())
    rate = rates.get(cur, 0.94)
    chf_r = _round_chf_generous(val * rate)
    orig_r = int(round(val))
    return _Markup(f'{"≈ " if approx else "~"}{chf_r} CHF <span class="orig-price">({orig_r} {cur})</span>')

templates.env.globals['price'] = price_chf_html
templates.env.filters['img'] = lambda url: image_url(url, large=False)
