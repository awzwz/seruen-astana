"""Fast unit tests for the ingest pipeline (no network)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from seruen.common.models import POI, Hours, Names, SourceRef
from seruen.ingest.dedup import dedupe
from seruen.ingest.normalize import from_osm, hours_text_to_osm, normalize_records, osm_category
from seruen.ingest.quality import freshness, hours_level, score

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
FETCHED = "2026-10-20T06:00:00+00:00"


def osm_rec(oid: str, tags: dict, lat=51.13, lon=71.41, ts="2025-06-01T00:00:00Z") -> dict:
    t, i = oid.split("/")
    return {"source": "osm", "source_id": oid, "fetched_at": FETCHED, "license": "ODbL", "url": None,
            "payload": {"type": t, "id": int(i), "lat": lat, "lon": lon, "tags": tags, "timestamp": ts, "version": 3}}


def ovt_rec(oid: str, name: str, cat: str, lat=51.13, lon=71.41, conf=0.8) -> dict:
    return {"source": "overture", "source_id": oid, "fetched_at": FETCHED, "license": "CDLA", "url": None,
            "payload": {"id": oid, "names": {"primary": name}, "basic_category": cat, "confidence": conf,
                        "lat": lat, "lon": lon, "websites": None, "phones": None, "addresses": None,
                        "sources": [{"dataset": "meta", "license": "CDLA-Permissive-2.0",
                                     "update_time": "2026-09-14T00:00:00Z"}]}}


# ------------------------------------------------------------------ normalize
def test_category_mapping_uses_tag_combinations():
    assert osm_category({"historic": "monument", "building": "yes", "name": "Бәйтерек"})[0] == "monument"
    assert osm_category({"shop": "mall", "name": "Хан Шатыр"})[0] == "mall"
    assert osm_category({"amenity": "cafe", "cuisine": "coffee_shop"})[0] == "coffee"
    assert osm_category({"amenity": "cafe", "cuisine": "regional"})[0] == "cafe"
    # aquarium beats the generic tourism=attraction / building tags
    assert osm_category({"tourism": "aquarium", "name": "Ailand"})[0] == "attraction"
    assert osm_category({"amenity": "theatre", "theatre:type": "concert_hall"})[0] == "concert"
    assert osm_category({"memorial": "plaque", "historic": "memorial", "name": "x"})[0] is None
    assert osm_category({"amenity": "bench"})[0] is None


def test_names_kazakh_primary_and_translit():
    p = from_osm(osm_rec("way/1", {"tourism": "museum", "name": "Қазақстан Республикасының Ұлттық музейі",
                                   "name:ru": "Национальный музей", "wheelchair": "yes"}))
    assert p.name.kk == "Қазақстан Республикасының Ұлттық музейі"
    assert p.name.ru == "Национальный музей"
    assert any(a.startswith("Qazaqstan") for a in p.name.alt)          # Latin transliteration added
    assert "accessible" in p.walk_tags and "warm_stop" in p.walk_tags and "indoor" in p.walk_tags
    assert p.attributes["wheelchair"].value == "yes" and p.attributes["wheelchair"].src == "osm"
    assert p.poi_id == "osm:way/1" and p.sources[0].license == "ODbL"


def test_unnamed_cafe_dropped_but_playground_kept():
    pois = normalize_records([osm_rec("node/2", {"amenity": "cafe"}), osm_rec("node/3", {"leisure": "playground"})])
    assert [p.category for p in pois] == ["playground"]
    assert pois[0].name.ru == "Детская площадка" and "kids" in pois[0].walk_tags


def test_hours_text_conversion():
    assert hours_text_to_osm("Ежедневно с 12:00 до 24:00") == "Mo-Su 12:00-24:00"
    assert hours_text_to_osm("Пн — Чт 09:00–24:00 Пт — Сб 09:00–01:00") == "Mo-Th 09:00-24:00; Fr-Sa 09:00-01:00"
    assert hours_text_to_osm("Круглосуточно") == "24/7"
    assert hours_text_to_osm("S-Thu 10:00-21:00") is None                 # unreadable day range → no claim


# ------------------------------------------------------------------ dedup
def test_two_sources_merge_with_translit_and_generic_words():
    pois = normalize_records([
        osm_rec("way/10", {"shop": "mall", "name": "Мега Силк Вей", "opening_hours": "Mo-Su 10:00-22:00"},
                lat=51.08903, lon=71.40731),
        ovt_rec("ovt-1", "ТРЦ Mega Silk Way", "shopping_mall", lat=51.0892, lon=71.4075),
    ])
    out = dedupe(pois)
    assert len(out) == 1
    m = out[0]
    assert m.poi_id == "osm:way/10"                                         # OSM wins the id
    assert {s.name for s in m.sources} == {"osm", "overture"}
    assert m.popularity.sources_confirming == 2
    assert m.opening_hours and m.opening_hours.raw == "Mo-Su 10:00-22:00"


def test_same_name_far_apart_not_merged():
    pois = normalize_records([
        osm_rec("node/20", {"amenity": "cafe", "name": "Coffee Boom"}, lat=51.1280, lon=71.4300),
        ovt_rec("ovt-2", "Coffee Boom", "coffee_shop", lat=51.0900, lon=71.4100),   # ~4.5 km away
    ])
    assert len(dedupe(pois)) == 2


def test_different_names_same_building_not_merged():
    pois = normalize_records([
        osm_rec("node/30", {"amenity": "cafe", "name": "Coffee Boom"}, lat=51.1280, lon=71.4300),
        osm_rec("node/31", {"amenity": "cafe", "name": "Coffee Room"}, lat=51.12801, lon=71.43001),
    ])
    assert len(dedupe(pois)) == 2


# ------------------------------------------------------------------ quality
def _poi(cat: str, hours: Hours | None, last_edit: str | None = "2026-01-01") -> POI:
    return POI(poi_id="osm:node/1", name=Names(ru="X"), lat=51.1, lon=71.4, geo_src="osm", category=cat,
               opening_hours=hours,
               sources=[SourceRef(name="osm", id="node/1", license="ODbL", fetched_at=FETCHED, last_edit=last_edit)])


def test_freshness_buckets():
    d = lambda days: (NOW - timedelta(days=days)).date().isoformat()  # noqa: E731
    assert freshness(d(100), NOW) == 1.0
    assert freshness(d(800), NOW) == 0.8
    assert freshness(d(2000), NOW) == 0.5


def test_hours_levels():
    d = lambda days: (NOW - timedelta(days=days)).date().isoformat()  # noqa: E731
    assert hours_level(_poi("park", None), NOW) == "open_area"
    assert hours_level(_poi("cafe", None), NOW) == "unknown"
    assert hours_level(_poi("cafe", Hours(raw="Mo-Su 10:00-22:00", src="visitastana", as_of=d(1))), NOW) == "high"
    assert hours_level(_poi("cafe", Hours(raw="Mo-Su 10:00-22:00", src="osm", as_of=d(200))), NOW) == "medium"
    assert hours_level(_poi("cafe", Hours(raw="Mo-Su 10:00-22:00", src="osm", as_of=d(900))), NOW) == "low"


def test_score_confidence_and_possibly_closed():
    old = _poi("cafe", Hours(raw="Mo-Su 10:00-22:00", src="osm", as_of="2018-03-01"), last_edit="2018-03-01")
    fresh = _poi("museum", None, last_edit="2026-05-01")
    a, b = score([old, fresh], NOW, with_anchors=False)
    assert a.status == "possibly_closed"                                    # OSM-only business, not edited since 2019
    assert abs(a.confidence.hours - 0.4) < 1e-6                              # 0.8 × 0.5 (older than 3 years)
    assert b.status == "active" and abs(b.confidence.exists - 0.8) < 1e-6 and b.confidence.hours == 0.0
