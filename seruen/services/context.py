"""Context service: local time, holiday, sun, weather (Open-Meteo for the city centre only) → walking mode."""
from __future__ import annotations

import httpx

from seruen.common.clients import endpoint
from seruen.common.config import settings
from seruen.common.geo import geohash_decode, valid_geohash
from seruen.common.models import Simulation, Weather
from seruen.common.stores import jget, jset, kv
from seruen.engine.context_rules import build_context, now_local, parse_local, weather_desc, wind_chill

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


async def city_weather() -> Weather:
    """Weather for a fixed point in the city centre — the user's position is never sent to the weather API."""
    cached = jget(kv(1), "weather:astana")
    if cached:
        return Weather(**cached)
    s = settings()
    params = {"latitude": s.weather_lat, "longitude": s.weather_lon, "timezone": "Asia/Almaty",
              "current": "temperature_2m,apparent_temperature,wind_speed_10m,wind_gusts_10m,weather_code,precipitation",
              "hourly": "precipitation_probability", "forecast_hours": 3}
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(OPEN_METEO, params=params)
            r.raise_for_status()
            d = r.json()
        cur = d["current"]
        pp = (d.get("hourly", {}).get("precipitation_probability") or [None])[0]
        w = Weather(temp_c=cur.get("temperature_2m"), feels_c=cur.get("apparent_temperature"),
                    wind_kmh=cur.get("wind_speed_10m"), gust_kmh=cur.get("wind_gusts_10m"),
                    precip_prob=pp, code=cur.get("weather_code"))
        jset(kv(1), "weather:astana", w.model_dump(), ex=600)
        return w
    except Exception:
        return Weather(src="unavailable")


@endpoint("context", "/v1/context")
async def context(p: dict) -> dict:
    cell = p.get("cell", "")
    lang = p.get("lang", "ru")
    lat, lon = geohash_decode(cell) if valid_geohash(cell) else (51.128, 71.430)
    sim = Simulation(**p["sim"]) if p.get("sim") else None
    now = parse_local(sim.datetime) if sim and sim.datetime else now_local()
    w = await city_weather()
    simulated = bool(sim and (sim.datetime or sim.temp_c is not None or sim.wind_kmh is not None or sim.precip is not None))
    if sim:
        if sim.temp_c is not None:
            w.temp_c = sim.temp_c
        if sim.wind_kmh is not None:
            w.wind_kmh = sim.wind_kmh
            w.gust_kmh = round(sim.wind_kmh * 1.5, 1)
        if sim.temp_c is not None or sim.wind_kmh is not None:
            w.feels_c = wind_chill(w.temp_c if w.temp_c is not None else 0, w.wind_kmh or 0)
        if sim.precip is not None:
            w.precip_prob = 90 if sim.precip else 0
            w.code = (73 if (w.temp_c or 0) < 0 else 63) if sim.precip else 1
        w.simulated = simulated
    w.desc = weather_desc(w.code, lang)
    ctx = build_context(now, w, lat, lon, lang, simulated)
    return ctx.model_dump()
