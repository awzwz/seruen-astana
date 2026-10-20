from datetime import datetime

import pytest

from seruen.common.geo import geohash_decode, geohash_encode, haversine_m
from seruen.common.models import POI, Chips, Hours, Names, Weather
from seruen.engine.context_rules import KZ_TZ, decide_mode, holiday, sun_times
from seruen.engine.hours import status_at
from seruen.engine.intents import parse_budget, parse_rules
from seruen.engine.route import Stop, best_route
from seruen.services.guard import problems


def test_geohash_roundtrip_within_cell():
    lat, lon = 51.1324, 71.4039
    gh = geohash_encode(lat, lon, 7)
    clat, clon = geohash_decode(gh)
    assert len(gh) == 7 and haversine_m(lat, lon, clat, clon) < 100


@pytest.mark.parametrize("text,minutes", [("у нас час", 60), ("на 1–2 часа", 120), ("30-60 минут", 60),
                                          ("бір жарым сағат", 90), ("2 hours before my flight", 120), ("полчаса", 30)])
def test_budget(text, minutes):
    assert parse_budget(text) == minutes


def test_rules_kids_airport():
    c = parse_rules("Мы с ребёнком, 2 часа до вылета", Chips(), "ru")
    assert c.kids == 1 and c.destination == "airport" and c.reserve_min == 60 and c.time_budget_min == 120


def test_chips_win():
    c = parse_rules("хочу на час", Chips(time_budget_min=30, wheelchair=True), "ru")
    assert c.time_budget_min == 30 and c.mobility == "wheelchair"


def test_mode_rules():
    assert decide_mode(Weather(temp_c=-20, feels_c=-28, wind_kmh=10), False, 200, "ru")[0] == "indoor"
    assert decide_mode(Weather(temp_c=-2, feels_c=-8, wind_kmh=20), False, 200, "ru")[0] == "short"
    assert decide_mode(Weather(temp_c=10, feels_c=9, wind_kmh=5, gust_kmh=8), False, 200, "ru")[0] == "walk"
    assert decide_mode(Weather(temp_c=10, feels_c=9, wind_kmh=5, gust_kmh=8), True, None, "ru")[0] == "short"


def test_holiday_and_sunset():
    assert holiday(datetime(2026, 10, 26).date())[0]
    _, sunset = sun_times(datetime(2026, 10, 22).date(), 51.17, 71.45)
    assert sunset.strftime("%H:%M") in ("17:06", "17:07", "17:08", "17:09")


def _poi(raw, level="medium", cat="cafe"):
    return POI(poi_id="t", name=Names(ru="T"), lat=51.13, lon=71.40, geo_src="t", category=cat,
               opening_hours=Hours(raw=raw, src="osm", as_of="2026-03-01", level=level) if raw else None)


def test_hours_status():
    t = datetime(2026, 10, 22, 21, 50, tzinfo=KZ_TZ)
    assert status_at(_poi("Mo-Su 10:00-22:00"), t, 30).status == "closes_soon"
    assert status_at(_poi("Mo-Su 10:00-22:00"), t.replace(hour=12), 30).status == "open"
    assert status_at(_poi(None), t, 30).status == "unknown"
    assert status_at(_poi(None, cat="park"), t, 30).status == "open_area"
    low = status_at(_poi("Mo-Su 10:00-23:00", level="low"), t.replace(hour=12), 30)
    assert not low.label.startswith("Открыто до")


def test_route_fits_budget_and_shortens_visits():
    stops = [Stop("a", 0.8, 60, True, lambda a, v: True), Stop("b", 0.5, 30, True, lambda a, v: True)]
    ids, obj, tl = best_route({"a": 5, "b": 6}, lambda x, y: 5, stops, datetime(2026, 10, 22, 12, 0), 60)
    assert ids and sum(x["visit_min"] for x in tl) + 5 + (5 if len(ids) == 2 else 0) <= 60


def test_guard_catches_digits_language_and_unknown_ids():
    ok = {"poi_id": "a", "why": "Тёплое место рядом.", "what_to_do": "Выпейте кофе."}
    assert problems(ok, {"a"}, "ru") == []
    assert "unknown_id" in problems({**ok, "poi_id": "x"}, {"a"}, "ru")
    assert "digits_in_why" in problems({**ok, "why": "Всего 5 минут"}, {"a"}, "ru")
    assert any(p.startswith("wrong_language") for p in problems(ok, {"a"}, "kk"))
    assert "safety_claim" in problems({**ok, "why": "Здесь безопасно."}, {"a"}, "ru")
