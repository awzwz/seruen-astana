"""Overture Maps Places collector: DuckDB over the public S3 release, Astana bbox, confidence ≥ 0.6."""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from ..util import ASTANA_BBOX, USER_AGENT, cache_path, in_bbox, now_iso, raw_record
from .. import util
from ..anchors import is_anchor_name

log = logging.getLogger("seruen.ingest.overture")

DEFAULT_RELEASE = "2026-09-23.1"
MIN_CONF = 0.6

# categories that are never walk-relevant: dropped at collection time (keeps the catalog focused)
SKIP_CATS = {
    "real_estate_service", "fashion_and_apparel_store", "personal_or_beauty_service", "hardware_home_and_garden_store",
    "hospital", "event_or_party_service", "dental_clinic", "government_office", "place_of_learning",
    "food_and_beverage_store", "automotive_service", "professional_service", "bank_or_credit_union", "wellness_service",
    "education", "auto_dealer", "specialty_school", "embassy", "college_university", "health_care",
    "corporate_or_business_office", "preschool", "lodging", "flowers_and_gifts_store", "home_service",
    "sporting_goods_store", "social_or_community_service", "travel_service", "electronics_store",
    "personal_care_and_beauty_store", "outpatient_care_facility", "campus_building", "middle_school", "atm",
    "pharmacy_and_drug_store", "financial_service", "technical_service", "media_service", "vehicle_parts_store",
    "diagnostics_imaging_or_lab_service", "building_or_construction_service", "elementary_school", "gas_station",
    "family_service", "sport_league", "printing_service", "convenience_store", "shipping_or_delivery_service",
    "high_school", "b2b_service", "medical_service", "second_hand_store", "rental_service", "manufacturer",
    "b2b_office_and_professional_service", "design_service", "community_and_government", "police_station",
    "fueling_station", "laundry_service", "educational_service", "courthouse", "civic_organization",
    "animal_and_pet_store", "reproductive_perinatal_and_womens_care", "b2b_transportation_and_storage_service",
    "storage_facility", "supplier_or_distributor", "sport_team", "parking", "travel_and_transportation",
    "b2b_industrial_and_machine_service", "b2b_energy_and_utility_service", "behavioral_or_mental_health_clinic",
    "specialized_medical_facility", "senior_living_facility", "complementary_and_alternative_medicine",
    "legal_service", "vision_or_eye_care_clinic", "primary_care_or_general_clinic", "animal_or_pet_service",
    "radio_station", "public_transit_facility_or_service", "television_station", "surgery", "vehicle_service",
    "office_supply_store", "specialized_health_care", "hotel", "bed_and_breakfast", "resort", "campground",
    "adult_entertainment_venue", "casino", "youth_organization", "sport_court", "sport_field", "superstore",
    "discount_store", "warehouse_club_store", "specialty_store", "shopping", "arts_crafts_and_hobby_store",
    "toys_and_games_store", "jewelry_store", "river", "mountain", "gym", "fitness_studio",
}


def latest_release() -> str:
    """Latest release from the STAC catalog; falls back to the release measured in the research notes."""
    if util.OFFLINE:
        return DEFAULT_RELEASE
    try:
        r = httpx.get("https://stac.overturemaps.org/catalog.json", timeout=10, headers={"User-Agent": USER_AGENT})
        cat = r.json()
        rels = sorted(l["href"].split("/")[-2] for l in cat.get("links", []) if l.get("rel") == "child"
                      and re.match(r"\./?\d{4}-\d{2}-\d{2}", l.get("href", "")))
        return cat.get("latest") or (rels[-1] if rels else DEFAULT_RELEASE)
    except Exception:  # noqa: BLE001
        return DEFAULT_RELEASE



def _extract(release: str):
    pq = cache_path("overture", f"astana_{release}.parquet")
    if pq.exists():
        return pq
    if util.OFFLINE:
        raise FileNotFoundError(pq)
    import duckdb

    la0, lo0, la1, lo1 = ASTANA_BBOX
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
    path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    q = f"""COPY (SELECT * EXCLUDE (geometry), ST_X(geometry) AS lon, ST_Y(geometry) AS lat
        FROM read_parquet('{path}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {lo0} AND {lo1} AND bbox.ymin BETWEEN {la0} AND {la1})
        TO '{pq}.part' (FORMAT parquet);"""
    t = time.time()
    con.execute(q)
    (pq.parent / f"{pq.name}.part").rename(pq)
    log.info("overture %s extracted in %.1fs", release, time.time() - t)
    return pq


def collect() -> list[dict]:
    import duckdb

    fetched = now_iso()
    release = DEFAULT_RELEASE
    try:
        cands = sorted(cache_path("overture", "x").parent.glob("astana_*.parquet"))
        if util.OFFLINE and cands:
            release = cands[-1].stem.replace("astana_", "")
        else:
            release = latest_release()
        pq = _extract(release)
    except Exception as e:  # noqa: BLE001 — try the release from the notes before giving up
        log.warning("overture %s failed (%s); trying %s", release, type(e).__name__, DEFAULT_RELEASE)
        release = DEFAULT_RELEASE
        pq = _extract(release)
    con = duckdb.connect()
    rows = con.execute(f"""SELECT id, names, basic_category, taxonomy, confidence, websites, socials, phones, brand,
        addresses, sources, operating_status, lon, lat FROM read_parquet('{pq}') WHERE confidence >= {MIN_CONF}""")
    cols = [d[0] for d in rows.description]
    recs = []
    skipped = 0
    for row in rows.fetchall():
        d = dict(zip(cols, row))
        if not in_bbox(d["lat"], d["lon"]) or not (d.get("names") or {}).get("primary"):
            continue
        cat = d.get("basic_category")
        if (cat in SKIP_CATS or cat is None) and not is_anchor_name(d["names"]["primary"]):
            skipped += 1
            continue
        if (d.get("operating_status") or "") in ("permanently_closed",):
            continue
        d["release"] = release
        payload = json.loads(json.dumps(d, default=str))
        recs.append(raw_record("overture", d["id"], payload, "CDLA-Permissive-2.0 (Overture; per-source licences)",
                               f"https://explore.overturemaps.org/#/place/{d['id']}", fetched))
    log.info("overture %s: %d raw records (skipped %d non-walking categories)", release, len(recs), skipped)
    return recs
