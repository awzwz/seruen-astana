"""Orchestrator: the conductor of one request. Streams facts first (cards from code), then AI text, then the trace.

Budget: 8 s for the whole AI part; if the model is late or wrong, the template text is used ("simplified mode").
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import orjson
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from seruen.common.clients import ServiceError, call, endpoint
from seruen.common.config import ROOT, settings
from seruen.common.geo import SLOW_M_PER_MIN, WALK_M_PER_MIN, DETOUR, geohash_decode, h3_cell, valid_geohash, walk_estimate
from seruen.common.models import (CATEGORIES, POI, Attr, Card, Constraints, Context, Explanation, RecommendRequest,
                                  TraceStep)
from seruen.common.stores import publish
from seruen.engine.hours import SRC_LABEL, status_at
from seruen.engine.templates import reasons as code_reasons
from seruen.engine.templates import template_explanation

FALLBACK_LANDMARKS = [
    {"id": "baiterek", "name": {"ru": "Байтерек", "kk": "Бәйтерек", "en": "Baiterek"}, "lat": 51.12825, "lon": 71.43050},
    {"id": "khan-shatyr", "name": {"ru": "Хан Шатыр", "kk": "Хан Шатыр", "en": "Khan Shatyr"}, "lat": 51.13240, "lon": 71.40390},
    {"id": "mega", "name": {"ru": "Mega Silk Way", "kk": "Mega Silk Way", "en": "Mega Silk Way"}, "lat": 51.08900, "lon": 71.40800},
    {"id": "aitu", "name": {"ru": "Astana IT University", "kk": "Astana IT University", "en": "Astana IT University"}, "lat": 51.09070, "lon": 71.41830},
]


def _sse(event: str, data) -> bytes:
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


def _landmarks() -> list[dict]:
    p = ROOT / "seruen" / "common" / "landmarks.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return FALLBACK_LANDMARKS


def _radius(c: Constraints) -> float:
    budget = max(15, c.time_budget_min - c.reserve_min)
    one_way = min(35.0, max(8.0, budget * 0.35))
    speed = SLOW_M_PER_MIN if (c.kids or c.mobility != "walk") else WALK_M_PER_MIN
    return one_way * speed / DETOUR


def _sources(poi: POI) -> list[str]:
    seen = []
    for s in poi.sources:
        lbl = SRC_LABEL.get(s.name, s.name)
        if lbl not in seen:
            seen.append(lbl)
    return seen


def _event_label(e: dict, lang: str) -> str:
    t = e["start"][11:16]
    when = {"ru": "весь день", "kk": "күні бойы", "en": "all day"}[lang] if t in ("00:00", "") else \
        {"ru": f"начало в {t}", "kk": f"{t}-де басталады", "en": f"starts at {t}"}[lang]
    price = (e.get("price_kzt") or "").strip()
    if price and price.replace(" ", "").isdigit():
        price = {"ru": f"от {price} ₸", "kk": f"{price} ₸-ден", "en": f"from {price} ₸"}[lang]
    return when + (" · " + price if price else "") + " · sxodim.com"


def _company(c: Constraints, lang: str) -> str:
    parts = []
    if c.kids:
        parts.append({"ru": "с ребёнком", "kk": "баламен", "en": "with a child"}[lang])
    if c.adults > 1:
        parts.append({"ru": f"группа из {c.adults}", "kk": f"{c.adults} адамдық топ", "en": f"group of {c.adults}"}[lang])
    if c.mobility == "wheelchair":
        parts.append({"ru": "на инвалидной коляске", "kk": "мүгедектер арбасымен", "en": "in a wheelchair"}[lang])
    if c.mobility == "stroller":
        parts.append({"ru": "с коляской", "kk": "балалар арбасымен", "en": "with a stroller"}[lang])
    if c.destination == "airport":
        parts.append({"ru": "потом в аэропорт", "kk": "кейін әуежайға", "en": "then to the airport"}[lang])
    return ", ".join(parts)


async def _step(trace: list[TraceStep], name: str, service: str, coro, detail_fn=None):
    t = time.perf_counter()
    res = await coro
    ms = (time.perf_counter() - t) * 1000
    trace.append(TraceStep(name=name, service=service, ms=round(ms, 1), detail=detail_fn(res) if detail_fn else ""))
    return res


async def recommend_stream(req: RecommendRequest):
    rid = uuid.uuid4().hex[:12]
    trace: list[TraceStep] = []
    lang = req.lang
    t_start = time.perf_counter()
    if not valid_geohash(req.cell):
        yield _sse("error", {"message": "bad cell"})
        return
    lat, lon = geohash_decode(req.cell)
    trace.append(TraceStep(name={"ru": "Точка → клетка ~150 м (в браузере)", "kk": "Нүкте → ~150 м ұяшық (браузерде)",
                                 "en": "Point → ~150 m cell (in the browser)"}[lang], service="browser", ms=0,
                           detail=f"geohash-7 {req.cell}" + (f" · {req.origin_label}" if req.origin_label else "")))
    try:
        prev = None
        if req.session_id:
            prev = (await call("session", "/v1/session/get", {"session_id": req.session_id}, timeout=1.5)).get("constraints")
        nlu_p = {"text": req.text, "chips": req.chips.model_dump(), "lang": lang, "prev": prev, "no_ai": req.no_ai}
        ctx_p = {"cell": req.cell, "sim": req.sim.model_dump() if req.sim else None, "lang": lang}
        async def _embed():
            if not req.text.strip():
                return None
            try:
                r = await call("llm-gateway", "/v1/embed", {"texts": [req.text.strip()[:500]]}, timeout=4)
                return r["vectors"][0]
            except ServiceError:
                return None

        emb_task = asyncio.create_task(_embed())   # query embedding runs in parallel with NLU and context
        c_raw, ctx_raw = await asyncio.gather(
            _step(trace, "NLU: текст → условия", "nlu", call("nlu", "/v1/parse", nlu_p, timeout=7),
                  lambda r: f"{r['parsed_by']} · {r['time_budget_min']} мин · " + ", ".join(r["wants"] or ["—"])),
            _step(trace, "Контекст: время, закат, погода", "context", call("context", "/v1/context", ctx_p, timeout=5),
                  lambda r: f"{r['local_time'][11:16]} · {r['mode']} · {r['weather'].get('temp_c')}°C"))
        c = Constraints(**c_raw)
        ctx = Context(**ctx_raw)
        yield _sse("context", ctx.model_dump())
        yield _sse("constraints", c.model_dump())

        radius = _radius(c)
        qvec = await emb_task
        cand = await _step(trace, "Поиск: радиус + слова + смысл", "retrieval", call("retrieval", "/v1/candidates", {
            "origin": {"lat": lat, "lon": lon}, "radius_m": radius, "constraints": c.model_dump(),
            "context": ctx.model_dump(), "query_vector": qvec}, timeout=8),
            lambda r: f"{r['stats']['backend']} · радиус {r['stats']['radius_m']} м · подходят {r['stats']['kept']}"
                      + (" · исключены " + ", ".join(f"{k}: {v}" for k, v in r["stats"]["excluded"].items()) if r["stats"]["excluded"] else ""))
        if not cand["candidates"]:
            yield _sse("cards", {"items": [], "route": None, "origin": {"lat": lat, "lon": lon, "cell": req.cell},
                                 "empty_reason": "nothing_in_reach"})
            yield _sse("trace", {"steps": [s.model_dump() for s in trace], "sent_to_llm": [], "not_sent": [], "model": None, "mode": "template"})
            yield _sse("done", {"request_id": rid, "ai_generated": False, "fallback": True})
            return
        rank = await _step(trace, "Ранжирование: формула + разнообразие", "ranking", call("ranking", "/v1/rank", {
            "candidates": cand["candidates"], "constraints": c.model_dump(), "context": ctx.model_dump(), "k": 15}, timeout=5),
            lambda r: f"оценено {r['considered']} · веса " + " ".join(f"{k} {v}" for k, v in r["weights"].items()))
        by_id = {x["poi"]["poi_id"]: x for x in cand["candidates"]}
        ranked = rank["ranked"]
        if c.wants or c.kids:   # stops must match what the user asked for; fall back to everything if nothing does
            relevant = [r for r in ranked if r["matched"] or r["parts"]["relevance"] >= 0.3]
            ranked = relevant or ranked
        top_pois = [by_id[r["poi_id"]]["poi"] for r in ranked[:12]]
        rt = await _step(trace, "Маршрут: минуты пешком, часы работы, порядок", "routing", call("routing", "/v1/route", {
            "origin": {"lat": lat, "lon": lon}, "pois": top_pois, "ranked": ranked[:12], "constraints": c.model_dump(),
            "context": ctx.model_dump()}, timeout=8),
            lambda r: f"{r['method']} · бюджет {r['budget_min']} мин · " + (f"{len(r['route']['stops'])} остановки, {r['route']['total_min']} мин" if r.get("route") else "маршрут не собран"))
        route = rt.get("route")
        final_ids = list(route["stops"]) if route else []
        roles = {pid: ("route" if len(final_ids) > 1 else "pick") for pid in final_ids}
        if route and len(final_ids) < 3:   # add 1–2 backup options the user can choose instead
            start_alt = datetime.fromisoformat(ctx.local_time)
            for r in ranked:
                if r["poi_id"] in final_ids:
                    continue
                poi = POI(**by_id[r["poi_id"]]["poi"])
                st = status_at(poi, start_alt + timedelta(minutes=r["est_walk_min"]), poi.visit_min, lang, ctx.is_dark, ctx.is_holiday)
                fits = r["est_walk_min"] * (1 if c.destination else 2) + max(15, poi.visit_min // 2) \
                    + (rt.get("to_destination_min") or 0) <= rt.get("budget_min", c.time_budget_min)
                if st.status in ("open", "open_area") and r["score"] >= 0.35 and fits:
                    final_ids.append(poi.poi_id)
                    roles[poi.poi_id] = "alternative"
                if len(final_ids) == 3:
                    break
        if not final_ids:      # no feasible route → best 1–3 places that are open
            start = datetime.fromisoformat(ctx.local_time)
            for r in ranked:
                poi = POI(**by_id[r["poi_id"]]["poi"])
                st = status_at(poi, start + timedelta(minutes=r["est_walk_min"]), poi.visit_min, lang, ctx.is_dark, ctx.is_holiday)
                if st.status not in ("closed",):
                    final_ids.append(poi.poi_id)
                if len(final_ids) == 3:
                    break
            roles = {pid: "pick" for pid in final_ids}
        # fresh canonical cards from the catalog (source of truth); retrieval data as fallback
        try:
            cat = await _step(trace, "Каталог: актуальные карточки", "catalog-api",
                              call("catalog-api", "/v1/poi/get", {"ids": final_ids}, timeout=3),
                              lambda r: f"{r['source']} · {len(r['pois'])} карточки")
            fresh = cat["pois"]
        except ServiceError:
            fresh = {}
        timeline = {x["poi_id"]: x for x in (route or {}).get("timeline", [])}
        start = datetime.fromisoformat(ctx.local_time)
        slow = c.kids > 0 or c.mobility != "walk"
        cards: list[Card] = []
        matched_map, reasons_map = {}, {}
        rank_map = {r["poi_id"]: r for r in ranked}
        for pid in final_ids:
            poi = POI(**(fresh.get(pid) or by_id[pid]["poi"]))
            dist = by_id[pid]["distance_m"]
            wm = rt["walk"].get(pid, {}).get("minutes") or walk_estimate(dist, slow)[1]
            arrive = datetime.strptime(timeline[pid]["arrive"], "%H:%M").replace(
                year=start.year, month=start.month, day=start.day, tzinfo=start.tzinfo) if pid in timeline else start + timedelta(minutes=wm)
            if arrive < start:
                arrive += timedelta(days=1)
            hs = status_at(poi, arrive, poi.visit_min, lang, ctx.is_dark, ctx.is_holiday)
            cat_meta = CATEGORIES.get(poi.category, CATEGORIES["other"])
            r = rank_map.get(pid, {})
            card = Card(poi_id=pid, name=poi.name.best(lang), category=poi.category, category_label=cat_meta[lang],
                        lat=poi.lat, lon=poi.lon, distance_m=int(dist), walk_min=int(round(wm)),
                        visit_min=int(timeline[pid].get("visit_min", poi.visit_min)) if pid in timeline else poi.visit_min,
                        hours=hs, source_label=", ".join(_sources(poi)[:3]), sources=_sources(poi),
                        accessibility=poi.attributes.get("wheelchair"), tags=poi.walk_tags,
                        description=poi.description.text if poi.description else None,
                        description_ai=bool(poi.description and poi.description.ai_generated),
                        url=poi.website or (poi.description.url if poi.description else None),
                        score=r.get("score", 0.0), score_parts=r.get("parts", {}), status=poi.status,
                        role=roles.get(pid, "pick"))
            cards.append(card)
            matched_map[pid] = r.get("matched", [])
            reasons_map[pid] = code_reasons(card, c, ctx, matched_map[pid])
        # places the user named but that are beyond walking reach ("Думан" from EXPO) → honest card with a taxi hint
        for far in cand.get("named_far", [])[:1]:
            poi = POI(**far["poi"])
            if poi.poi_id in {x.poi_id for x in cards} or len(cards) >= 3:
                continue
            taxi_min = int(far["distance_m"] * 1.25 / 1000 / 55 * 60) + 5
            hs = status_at(poi, start + timedelta(minutes=taxi_min), poi.visit_min, lang, ctx.is_dark, ctx.is_holiday)
            far_lbl = {"ru": f" · далеко пешком, на такси ≈ {taxi_min} мин", "kk": f" · жаяу алыс, таксимен ≈ {taxi_min} мин",
                       "en": f" · too far to walk, ≈ {taxi_min} min by taxi"}[lang]
            cat_meta = CATEGORIES.get(poi.category, CATEGORIES["other"])
            cards.append(Card(poi_id=poi.poi_id, name=poi.name.best(lang), category=poi.category, category_label=cat_meta[lang],
                              lat=poi.lat, lon=poi.lon, distance_m=int(far["distance_m"]),
                              walk_min=walk_estimate(far["distance_m"], slow)[1], visit_min=poi.visit_min,
                              hours=hs.model_copy(update={"label": hs.label + far_lbl}), source_label=", ".join(_sources(poi)[:3]),
                              sources=_sources(poi), tags=poi.walk_tags, status=poi.status, role="alternative",
                              description=poi.description.text if poi.description else None))
            matched_map[poi.poi_id] = []
            reasons_map[poi.poi_id] = code_reasons(cards[-1], c, ctx, [])
        # events from the afisha (only when the user asked for events or a night out)
        if ("events" in c.wants or c.night) and cand.get("events"):
            for ev in cand["events"][: max(1, 3 - len(cards))]:
                e = ev["event"]
                cards.append(Card(poi_id=e["event_id"], kind="event", name=e["title"], category="concert",
                                  category_label={"ru": "Событие", "kk": "Іс-шара", "en": "Event"}[lang],
                                  lat=e["lat"], lon=e["lon"], distance_m=int(ev["distance_m"]),
                                  walk_min=walk_estimate(ev["distance_m"], slow)[1], visit_min=90,
                                  hours=status_at(POI(poi_id="x", name={"ru": "x"}, lat=e["lat"], lon=e["lon"], geo_src="x", category="concert"),
                                                  start, 90, lang)
                                  .model_copy(update={"status": "open", "label": _event_label(e, lang), "level": "medium"}),
                                  source_label="sxodim.com", sources=["sxodim.com"], url=e.get("url"),
                                  event={"title": e["title"], "start": e["start"], "end": e.get("end"), "price": e.get("price_kzt"),
                                         "venue": e.get("venue_name"), "url": e.get("url")}))
                matched_map[e["event_id"]] = ["events"]
                reasons_map[e["event_id"]] = code_reasons(cards[-1], c, ctx, ["events"])
        yield _sse("cards", {"items": [x.model_dump() for x in cards], "route": route,
                             "origin": {"lat": lat, "lon": lon, "cell": req.cell},
                             "to_destination_min": rt.get("to_destination_min")})

        # ---- AI part (call 2) with a hard time budget; templates otherwise
        mode = "template"
        model = None
        sent, not_sent = [], ["координаты и клетка", "ID сессии", "IP-адрес", "история запросов"]
        expl: dict[str, Explanation] = {}
        if not req.no_ai and cards:
            sent = ["текст запроса", "фильтры и теги", "режим погоды и время суток", f"факты о {len(cards)} выбранных местах"]
            ex_p = {"cards": [x.model_dump() for x in cards], "lang": lang, "query_text": c.query_text,
                    "company": _company(c, lang), "mode_reason": ctx.mode_reason,
                    "time_label": ctx.local_time[11:16] + (" (стемнело)" if ctx.is_dark else ""),
                    "reasons": reasons_map, "timeout": 8}
            try:
                ex = await _step(trace, "ИИ: объяснения по выбранным ID", "explain", call("explain", "/v1/explain", ex_p, timeout=10),
                                 lambda r: (f"{r.get('model')} · {r.get('latency_ms')} мс" + (" · кэш" if r.get("cached") else "")) if r.get("ok") else f"ошибка: {r.get('error')}")
            except ServiceError as e:
                ex = {"ok": False, "error": str(e)}
            if ex.get("ok"):
                model = ex.get("model")
                g = await _step(trace, "Guard: проверка ответа ИИ", "guard", call("guard", "/v1/check", {
                    "items": ex["items"], "allowed_ids": [x.poi_id for x in cards], "lang": lang}, timeout=3),
                    lambda r: "всё прошло" if r["ok"] else f"заменено шаблоном: {len(r['problems']) + len(r['missing'])}")
                for it in g["items"]:
                    expl[it["poi_id"]] = Explanation(**it)
                mode = "ai"
        for card in cards:
            e = expl.get(card.poi_id) or template_explanation(card, c, ctx, matched_map.get(card.poi_id, []))
            yield _sse("text", e.model_dump())
        if mode == "ai" and len(expl) < len(cards):
            mode = "ai+template"
        trace.append(TraceStep(name="Итого", service="orchestrator", ms=round((time.perf_counter() - t_start) * 1000, 1),
                               detail=f"{len(cards)} карточки · режим {mode}"))
        yield _sse("trace", {"steps": [s.model_dump() for s in trace], "sent_to_llm": sent, "not_sent": not_sent,
                             "model": model, "mode": mode, "radius_m": round(radius)})
        yield _sse("done", {"request_id": rid, "ai_generated": mode != "template", "fallback": mode != "ai"})
        publish("reco.served", rid, {"h3_r8": h3_cell(lat, lon, 8), "hour": ctx.local_time[11:13], "lang": lang,
                                     "mode": ctx.mode, "wants": c.wants, "kids": bool(c.kids), "poi_ids": [x.poi_id for x in cards],
                                     "categories": [x.category for x in cards], "ai": mode != "template"})
        if req.session_id:
            try:
                await call("session", "/v1/session/put", {"session_id": req.session_id, "constraints": c.model_dump()}, timeout=1.5)
            except ServiceError:
                pass
    except ServiceError as e:
        yield _sse("error", {"message": f"service unavailable: {e}"})
    except Exception as e:  # pragma: no cover
        yield _sse("error", {"message": type(e).__name__})


@endpoint("orchestrator", "/v1/landmarks")
async def landmarks(p: dict) -> dict:
    return {"items": _landmarks()}


def extra(app: FastAPI) -> None:
    @app.post("/api/recommend")
    async def recommend(req: RecommendRequest):
        return StreamingResponse(recommend_stream(req), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/landmarks")
    async def get_landmarks():
        return _landmarks()

    @app.post("/api/feedback")
    async def feedback(body: dict):
        try:
            return await call("feedback-api", "/v1/feedback", body, timeout=3)
        except ServiceError:
            return JSONResponse({"ok": False}, status_code=503)

    @app.delete("/api/session/{sid}")
    async def delete_session(sid: str):
        try:
            await call("session", "/v1/session/delete", {"session_id": sid}, timeout=2)
        except ServiceError:
            pass
        return {"ok": True, "deleted": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "mode": settings().mode}

    @app.get("/api/stats")
    async def stats():
        try:
            return await call("catalog-api", "/v1/poi/stats", {}, timeout=3)
        except ServiceError:
            return {}

    web = ROOT / "web"
    if web.exists():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
