"""Orchestrator: the conductor of one request. Streams facts first (cards from code), then AI text, then the trace.

Budget: 8 s for the whole AI part; if the model is late or wrong, the template text is used ("simplified mode").
"""
from __future__ import annotations

import asyncio
import base64
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
from seruen.common.geo import SLOW_M_PER_MIN, WALK_M_PER_MIN, DETOUR, geohash_decode, h3_cell, haversine_m, valid_geohash, walk_estimate
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


async def _where(lat: float, lon: float, cell: str) -> dict:
    try:
        return await call("places-live", "/v1/where", {"lat": lat, "lon": lon, "cell": cell}, timeout=3)
    except ServiceError:
        return {}


async def _moves(a: tuple[float, float], b: tuple[float, float]) -> tuple[dict | None, dict | None]:
    """Public transport and taxi options between two points (2GIS), in parallel."""
    async def one(path):
        try:
            return (await call("places-live", path, {"from": list(a), "to": list(b)}, timeout=6)).get("option")
        except ServiceError:
            return None
    tr, tx = await asyncio.gather(one("/v1/transit"), one("/v1/taxi"))
    return tr, tx


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



def _cat_label(poi: POI, lang: str) -> str:
    """The 2GIS rubric for places outside the walking categories («Парикмахерские», «Аптеки»)."""
    if poi.category == "other":
        rub = poi.attributes.get("rubrics")
        if rub and rub.value:
            return str(rub.value[0])
    return CATEGORIES.get(poi.category, CATEGORIES["other"])[lang]


def _search_explanation(card: Card, c: Constraints, lang: str, found_by: list[str] | None = None) -> Explanation:
    """Template text for a lookup («парикмахерская»): distance, hours and rating — facts only."""
    q = c.place_query or ""
    hours = (card.hours.label or "").split(" · ")[0]
    hours = hours[:1].lower() + hours[1:] if hours else ""
    rating = f"{card.rating:.1f}".replace(".", ",") if card.rating else None
    facts = ", ".join(x[:1].lower() + x[1:] for x in found_by[:2]) if found_by else ""
    if facts:
        hours = (hours + ", " if hours else "") + facts
    if lang == "kk":
        why = f"{card.name} — жаяу {card.walk_min} мин" + (f", {hours}" if hours else "") + (f", 2GIS бағасы {rating}" if rating else "") + "."
        todo = "Шығар алдында 2GIS-те жұмыс уақытын тексеріңіз — сол жерден қоңырау шалуға болады."
    elif lang == "en":
        why = f"{card.name} — {card.walk_min} min on foot" + (f", {hours}" if hours else "") + (f", rated {rating} on 2GIS" if rating else "") + "."
        todo = "Check the hours in 2GIS before you go — you can call them from there."
    else:
        why = f"{card.name} — {card.walk_min} мин пешком" + (f", {hours}" if hours else "") + (f", рейтинг {rating} в 2GIS" if rating else "") + "."
        todo = "Перед выходом проверьте часы работы в 2GIS — там же можно позвонить."
    return Explanation(poi_id=card.poi_id, why=why, what_to_do=todo, source="poi_data", template=True)


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
        where_task = asyncio.create_task(_where(lat, lon, req.cell))
        c_raw, ctx_raw = await asyncio.gather(
            _step(trace, "NLU: текст → условия", "nlu", call("nlu", "/v1/parse", nlu_p, timeout=7),
                  lambda r: f"{r['parsed_by']} · {r['time_budget_min']} мин · " + ", ".join(r["wants"] or ["—"])),
            _step(trace, "Контекст: время, закат, погода", "context", call("context", "/v1/context", ctx_p, timeout=5),
                  lambda r: f"{r['local_time'][11:16]} · {r['mode']} · {r['weather'].get('temp_c')}°C"))
        c = Constraints(**c_raw)
        ctx = Context(**ctx_raw)
        yield _sse("context", ctx.model_dump())
        yield _sse("constraints", c.model_dump())

        found_map: dict[str, list[str]] = {}
        if c.place_query:
            # ---- «найди парикмахерскую»: a specific kind of place, looked up in the whole 2GIS directory nearby
            emb_task.cancel()
            found = await _step(trace, {"ru": f"Поиск в 2GIS: «{c.place_query}»", "kk": f"2GIS-тен іздеу: «{c.place_query}»",
                                        "en": f"2GIS search: “{c.place_query}”"}[lang], "places-live",
                                call("places-live", "/v1/search", {"q": c.place_query, "lat": lat, "lon": lon}, timeout=9),
                                lambda r: f"{len(r.get('pois') or [])} мест · радиус {r.get('radius_m') or '—'} м" + (f" · отброшено как не по теме: {r['dropped']}" if r.get("dropped") else ""))
            radius = float(found.get("radius_m") or 1500)
            start = datetime.fromisoformat(ctx.local_time)
            slow = c.kids > 0 or c.mobility != "walk"
            options = []
            for pj in found.get("pois") or []:
                poi = POI(**pj)
                dist = haversine_m(lat, lon, poi.lat, poi.lon)
                wm = walk_estimate(dist, slow)[1]
                options.append((poi, dist, wm, status_at(poi, start + timedelta(minutes=wm), 15, lang, ctx.is_dark, ctx.is_holiday)))
            # open on arrival first, then unknown hours, then closed (shown honestly when nothing is open); nearest first
            order = {"open": 0, "open_area": 0, "closes_soon": 1, "unknown": 2, "closed": 3}
            options.sort(key=lambda o: (order.get(o[3].status, 2), o[1]))
            options = options[:3]   # one place + two backups, like every answer
            cand = {"stats": {"live": {"enabled": True, "queries": [c.place_query], "items": len(options), "merged": 0}}}
            rt, route, to_dest = {}, None, None
            cards, matched_map, reasons_map = [], {}, {}
            if not options:
                where = await where_task
                yield _sse("cards", {"items": [], "route": None, "empty_reason": "search_nothing",
                                     "origin": {"lat": lat, "lon": lon, "cell": req.cell, "address": where.get("address"),
                                                "building": where.get("building"), "district": where.get("district")}})
                trace.append(TraceStep(name="Итого", service="orchestrator", ms=round((time.perf_counter() - t_start) * 1000, 1),
                                       detail="0 карточек · в 2GIS ничего не нашлось"))
                yield _sse("trace", {"steps": [s.model_dump() for s in trace], "sent_to_llm": [], "not_sent": [],
                                     "sent_to_2gis": f"в 2GIS: центр клетки ~150 м и запрос «{c.place_query}»", "model": None, "mode": "template"})
                yield _sse("done", {"request_id": rid, "ai_generated": False, "fallback": False})
                return
            best = options[0]
            if best[2] <= 30:   # walkable: a real 2GIS walking path to the nearest one
                try:
                    legs = (await call("places-live", "/v1/walk", {"points": [[lat, lon], [best[0].lat, best[0].lon]]}, timeout=5)).get("legs")
                except ServiceError:
                    legs = None
                leg = (legs or [None])[0]
                m = leg["minutes"] if leg else best[2]
                arrive = start + timedelta(minutes=m)
                route = {"stops": [best[0].poi_id], "total_min": m, "warm_stops": False,
                         "legs": [{"from_id": "origin", "to_id": best[0].poi_id, "minutes": m, "outdoor_min": m,
                                   "meters": leg["meters"] if leg else int(best[1] * DETOUR),
                                   "geometry": leg["geometry"] if leg else [], "approx": not leg}],
                         "timeline": [{"poi_id": best[0].poi_id, "arrive": arrive.strftime("%H:%M"), "leave": arrive.strftime("%H:%M"), "visit_min": 0}]}
            for k, (poi, dist, wm, hs) in enumerate(options):
                if k == 0 and route:
                    wm = route["total_min"]
                closeness = max(0.0, 1 - dist / 3000)
                hours_v = {"open": 1.0, "open_area": 1.0, "closes_soon": 0.6, "unknown": 0.4, "closed": 0.0}.get(hs.status, 0.4)
                pop = min(1.0, (poi.popularity.rating or 0) / 5)
                parts = {"relevance": 1.0, "closeness": round(closeness, 2), "hours": hours_v, "popularity": round(pop, 2)}
                card = Card(poi_id=poi.poi_id, name=poi.name.best(lang), category=poi.category, category_label=_cat_label(poi, lang),
                            lat=poi.lat, lon=poi.lon, distance_m=int(dist), walk_min=int(wm), visit_min=0, hours=hs,
                            source_label=", ".join(_sources(poi)[:3]), sources=_sources(poi),
                            accessibility=poi.attributes.get("wheelchair"), tags=poi.walk_tags,
                            description=poi.description.text if poi.description else None, address=poi.address,
                            rating=poi.popularity.rating, reviews=poi.popularity.reviews,
                            score=round(0.35 + 0.25 * closeness + 0.25 * hours_v + 0.15 * pop, 3), score_parts=parts,
                            status=poi.status, role="pick" if k == 0 else "alternative")
                if k == 0 and wm > 30:   # too far to walk: taxi and public transport from 2GIS
                    tr_opt, tx_opt = await _moves((lat, lon), (poi.lat, poi.lon))
                    card = card.model_copy(update={"transit": tr_opt, "taxi": tx_opt})
                cards.append(card)
                matched_map[poi.poi_id] = []
                reasons_map[poi.poi_id] = code_reasons(card, c, ctx, [])
                fb = poi.attributes.get("found_by")
                if fb and fb.value:
                    found_map[poi.poi_id] = list(fb.value)
        else:
            radius = _radius(c)
            qvec = await emb_task
            cand = await _step(trace, "Поиск: радиус + слова + смысл", "retrieval", call("retrieval", "/v1/candidates", {
                "origin": {"lat": lat, "lon": lon}, "radius_m": radius, "constraints": c.model_dump(),
                "context": ctx.model_dump(), "query_vector": qvec}, timeout=8),
                lambda r: f"{r['stats']['backend']} + 2GIS live ({r['stats']['live']['items']} мест, склеено {r['stats']['live']['merged']}) · радиус {r['stats']['radius_m']} м · подходят {r['stats']['kept']}"
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
                    late_unknown = st.status == "unknown" and ctx.is_dark and poi.category not in ("bar", "nightclub")
                    if st.status not in ("closed", "closes_soon") and not late_unknown:
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
                cand_poi = by_id[pid]["poi"]
                live_data = any(s.get("name") == "2gis" for s in cand_poi.get("sources", []))
                poi = POI(**(cand_poi if live_data or pid not in fresh else fresh[pid]))
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
                            url=poi.website or (poi.description.url if poi.description else None), address=poi.address,
                            rating=poi.popularity.rating, reviews=poi.popularity.reviews,
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
                tr_opt, tx_opt = await _moves((lat, lon), (poi.lat, poi.lon))
                taxi_min = tx_opt["minutes"] if tx_opt else int(far["distance_m"] * 1.25 / 1000 / 55 * 60) + 5
                hs = status_at(poi, start + timedelta(minutes=taxi_min), poi.visit_min, lang, ctx.is_dark, ctx.is_holiday)
                far_lbl = {"ru": f" · далеко пешком, на такси ≈ {taxi_min} мин", "kk": f" · жаяу алыс, таксимен ≈ {taxi_min} мин",
                           "en": f" · too far to walk, ≈ {taxi_min} min by taxi"}[lang]
                cat_meta = CATEGORIES.get(poi.category, CATEGORIES["other"])
                cards.append(Card(poi_id=poi.poi_id, name=poi.name.best(lang), category=poi.category, category_label=cat_meta[lang],
                                  lat=poi.lat, lon=poi.lon, distance_m=int(far["distance_m"]),
                                  walk_min=walk_estimate(far["distance_m"], slow)[1], visit_min=poi.visit_min,
                                  hours=hs.model_copy(update={"label": hs.label + far_lbl}), source_label=", ".join(_sources(poi)[:3]),
                                  sources=_sources(poi), tags=poi.walk_tags, status=poi.status, role="alternative",
                                  description=poi.description.text if poi.description else None, transit=tr_opt, taxi=tx_opt))
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
            to_dest = None
            if c.destination:
                from seruen.engine.route import AIRPORT, TRAIN_STATION

                dest_pt = AIRPORT if c.destination == "airport" else TRAIN_STATION
                last = next((x for x in reversed(cards) if x.role in ("route", "pick") and x.kind == "poi"), None)
                frm = (last.lat, last.lon) if last else (lat, lon)
                tr_opt, tx_opt = await _moves(frm, dest_pt)
                to_dest = {"name": {"airport": {"ru": "аэропорт", "kk": "әуежай", "en": "the airport"},
                                    "train_station": {"ru": "вокзал Нұрлы Жол", "kk": "Нұрлы Жол вокзалы", "en": "Nurly Zhol station"}}[c.destination][lang],
                           "from": last.name if last else None, "transit": tr_opt, "taxi": tx_opt}
                trace.append(TraceStep(name="Дорога дальше: такси и транспорт", service="places-live",
                                       ms=0, detail=("такси ≈ " + str(tx_opt["minutes"]) + " мин" if tx_opt else "такси —") +
                                       (" · транспорт ≈ " + str(tr_opt["minutes"]) + " мин" if tr_opt else " · транспорт —") + " · 2GIS"))
        where = await where_task
        yield _sse("cards", {"items": [x.model_dump() for x in cards], "route": route,
                             "origin": {"lat": lat, "lon": lon, "cell": req.cell, "address": where.get("address"),
                                        "building": where.get("building"), "district": where.get("district")},
                             "to_destination_min": (to_dest["taxi"]["minutes"] if to_dest and to_dest.get("taxi") else rt.get("to_destination_min")),
                             "to_destination": to_dest})

        # ---- AI part (call 2) with a hard time budget; templates otherwise
        mode = "template"
        model = None
        sent, not_sent = [], ["координаты и клетка", "ID сессии", "IP-адрес", "история запросов"]
        live_st = cand["stats"].get("live", {})
        if live_st.get("enabled") and live_st.get("queries"):
            sent_2gis = "в 2GIS: центр клетки ~150 м и тип мест (" + ", ".join(live_st["queries"]) + ")"
        else:
            sent_2gis = None
        expl: dict[str, Explanation] = {}
        if not req.no_ai and cards and not c.place_query:   # a lookup answers with facts only: distance, hours, rating
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
            e = expl.get(card.poi_id) or (_search_explanation(card, c, lang, found_map.get(card.poi_id)) if c.place_query
                                          else template_explanation(card, c, ctx, matched_map.get(card.poi_id, [])))
            yield _sse("text", e.model_dump())
        if mode == "ai" and len(expl) < len(cards):
            mode = "ai+template"
        trace.append(TraceStep(name="Итого", service="orchestrator", ms=round((time.perf_counter() - t_start) * 1000, 1),
                               detail=f"{len(cards)} карточки · режим {mode}"))
        yield _sse("trace", {"steps": [s.model_dump() for s in trace], "sent_to_llm": sent, "not_sent": not_sent,
                             "sent_to_2gis": sent_2gis, "model": model, "mode": mode, "radius_m": round(radius)})
        yield _sse("done", {"request_id": rid, "ai_generated": mode != "template", "fallback": mode != "ai"})
        publish("reco.served", rid, {"h3_r8": h3_cell(lat, lon, 8), "hour": ctx.local_time[11:13], "lang": lang,
                                     "mode": ctx.mode, "wants": c.wants, "kids": bool(c.kids),
                                     # live 2GIS ids are not kept anywhere (their terms forbid storing)
                                     "poi_ids": [x.poi_id for x in cards if not x.poi_id.startswith("2gis:")],
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


# vocabulary hints for speech-to-text: place names the model should spell the way Astana does
VOICE_HINT = {
    "ru": "Прогулка по Астане: Хан Шатыр, Байтерек, Керуен, ЭКСПО, Мега Силк Уэй, Нурлы Жол, Астана Опера, кофе, с ребёнком, до вылета.",
    "kk": "Астанада серуен: Хан Шатыр, Бәйтерек, Керуен, ЭКСПО, Мега Силк Уэй, Нұрлы Жол, кофе, баламен, ұшар алдында.",
    "en": "A walk in Astana: Khan Shatyr, Baiterek, Keruen, EXPO, Mega Silk Way, Nurly Zhol, Astana Opera, coffee, with kids, before my flight.",
}
MAX_VOICE_BYTES = 7_500_000        # up to 2 min of speech at any browser bitrate; the ingress allows 8 MB


def extra(app: FastAPI) -> None:
    @app.post("/api/recommend")
    async def recommend(req: RecommendRequest):
        return StreamingResponse(recommend_stream(req), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/transcribe")
    async def api_transcribe(request: Request, lang: str = "ru"):
        """Voice → text for the search field. The audio goes to OpenAI only to be transcribed and is not kept."""
        body = await request.body()
        if not body:
            return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
        if len(body) > MAX_VOICE_BYTES:
            return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
        lang = lang if lang in VOICE_HINT else "ru"
        payload = {"audio_b64": base64.b64encode(body).decode(), "mime": request.headers.get("content-type", "audio/webm"),
                   "lang": lang, "prompt": VOICE_HINT[lang]}
        try:
            r = await call("llm-gateway", "/v1/transcribe", payload, timeout=45)
        except ServiceError:
            return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
        if not r.get("ok"):
            return JSONResponse({"ok": False, "error": r.get("error", "failed")}, status_code=502)
        return {"ok": True, "text": (r.get("text") or "")[:400], "model": r.get("model"), "latency_ms": r.get("latency_ms")}

    @app.get("/api/where")
    async def api_where(cell: str):
        if not valid_geohash(cell):
            return {"address": None}
        la, lo = geohash_decode(cell)
        return await _where(la, lo, cell)

    @app.get("/api/suggest")
    async def api_suggest(q: str, cell: str = ""):
        la, lo = geohash_decode(cell) if valid_geohash(cell) else (51.128, 71.43)
        try:
            return await call("places-live", "/v1/suggest", {"q": q, "lat": la, "lon": lo}, timeout=4)
        except ServiceError:
            return {"items": []}

    @app.get("/api/status/2gis")
    async def api_dgis_status():
        """Masked usage of the 2GIS key pool (per key and service, blocks and reset times)."""
        try:
            return await call("places-live", "/v1/keys", {}, timeout=3)
        except ServiceError:
            return {"keys": []}

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
