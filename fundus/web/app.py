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

from .. import db, runner
from ..adapters import all_adapters, adapter_for_source
from ..adapters.base import fetch_html
from ..adapters.extract import extract_description_from_page
from ..config import get_config, save_settings
from ..notify import format_price, send_test, send_weekly
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
    runner.available_sources(cfg)
    state["scheduler"] = start_scheduler(cfg)
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
    return {
        "request": request,
        "running": runner.is_running(),
        "next_runs": next_runs(state["scheduler"]),
        "ntfy_ok": bool(cfg["ntfy"].get("topic")),
        "adapters": [(a.name, a.label) for a in all_adapters(cfg)],
        "cfg": cfg,
        **kw,
    }


# ---------- Seiten ----------

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
    except Exception as e:
        log.error("Fehler beim Laden der Beschreibung: %s", e)
    return HTMLResponse("Konnte Beschreibung nicht laden.")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(
        request,
        searches=db.list_searches(),
        hits=db.recent_new_hits(40),
        runs=db.last_runs(1),
    ))


@app.post("/searches")
def create_search(
    query: str = Form(...),
    exclude: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    platforms: list[str] = Form([]),
):
    sid = db.save_search({
        "query": query, "exclude": exclude, "min_price": _num(min_price),
        "max_price": _num(max_price), "platforms": platforms or None,
    })
    runner.run_in_background("neue Suche", [sid])  # Bestand sofort einlesen (ohne Meldungen)
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.get("/search/{sid}", response_class=HTMLResponse)
def show_search(request: Request, sid: int):
    search = db.get_search(sid)
    if not search:
        return RedirectResponse("/", status_code=303)
    hits = db.hits_for_search(sid)
    hits.sort(key=lambda x: (x.get("ai_score") is None, -(x.get("ai_score") or 0)))
    resp = templates.TemplateResponse(request, "search.html", _ctx(request, search=search, hits=hits))
    db.mark_seen(sid)  # nach dem Rendern: «neu»-Markierung ist jetzt angeschaut
    return resp


@app.post("/search/{sid}")
def update_search(
    sid: int,
    query: str = Form(...),
    exclude: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    platforms: list[str] = Form([]),
):
    db.save_search({
        "query": query, "exclude": exclude, "min_price": _num(min_price),
        "max_price": _num(max_price), "platforms": platforms or None,
    }, sid)
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
