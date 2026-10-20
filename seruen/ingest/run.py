"""Seruen ingest pipeline: collect → normalize → dedup → anchors/quality → describe → events → embed.

    python -m seruen.ingest.run --out data/catalog [--sources osm,overture,wikidata,wikivoyage,visitastana,sxodim]
                                [--skip-embed] [--offline] [--llm-describe 40 | --no-llm] [--write-landmarks]

Every stage is also importable as a pure function (normalize_records, dedupe, apply_anchors, score,
template_descriptions, normalize_events, embed_pois) so Kafka workers can wrap them one by one.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from seruen.common.config import ROOT
from seruen.common.models import POI, Event

from . import util
from .collectors import ALL_SOURCES, EVENT_SOURCES, get as get_collector
from .dedup import dedupe
from .describe import llm_descriptions, template_descriptions
from .normalize import TZ, normalize_events, normalize_records
from .quality import apply_anchors, hours_level_counts, score

log = logging.getLogger("seruen.ingest.run")


def collect_all(sources: list[str], raw_dir: Path) -> tuple[dict[str, list[dict]], dict]:
    raw: dict[str, list[dict]] = {}
    info: dict[str, dict] = {}
    raw_dir.mkdir(parents=True, exist_ok=True)
    for s in sources:
        t = time.perf_counter()
        try:
            recs = get_collector(s)()
            raw[s] = recs
            with open(raw_dir / f"{s}.jsonl", "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            info[s] = {"raw": len(recs), "seconds": round(time.perf_counter() - t, 1), "ok": True}
        except Exception as e:  # noqa: BLE001 — one failing source must not stop the run
            log.error("collector %s failed: %s: %s", s, type(e).__name__, e)
            info[s] = {"raw": 0, "seconds": round(time.perf_counter() - t, 1), "ok": False,
                       "error": f"{type(e).__name__}: {str(e)[:200]}"}
            prev = raw_dir / f"{s}.jsonl"
            if prev.exists() and prev.stat().st_size > 0:      # keep the source: last good dump, marked stale
                recs = [json.loads(line) for line in prev.read_text(encoding="utf-8").splitlines() if line]
                raw[s] = recs
                info[s].update({"raw": len(recs), "stale_fallback": True,
                                "fallback_fetched_at": recs[0]["fetched_at"] if recs else None})
                log.warning("collector %s: using previous raw dump (%d records)", s, len(recs))
    return raw, info


def mark_event_venues(pois: list[POI], events: list[Event]) -> int:
    venue_ids = {e.geo_src.split(":", 1)[1] for e in events if e.geo_src and e.geo_src.startswith("venue_match:")}
    n = 0
    for p in pois:
        if p.poi_id in venue_ids and "event" not in p.walk_tags:
            p.walk_tags.append("event")
            n += 1
    return n


def coverage(pois: list[POI], now: datetime) -> dict:
    n = len(pois) or 1

    def pct(k: int) -> dict:
        return {"n": k, "pct": round(100 * k / n, 1)}

    named = [p for p in pois if not p.attributes.get("unnamed")]
    desc_src = Counter(p.description.src for p in pois if p.description)
    return {
        "pois": len(pois),
        "named": len(named),
        "hours_any": pct(sum(1 for p in pois if p.opening_hours)),
        "hours_levels": hours_level_counts(pois, now),
        "wheelchair_any": pct(sum(1 for p in pois if p.attributes.get("wheelchair"))),
        "wheelchair_yes": pct(sum(1 for p in pois if p.attributes.get("wheelchair") and
                                  p.attributes["wheelchair"].value == "yes")),
        "wifi_known": pct(sum(1 for p in pois if p.attributes.get("wifi"))),
        "description_real": pct(sum(1 for p in pois if p.description and p.description.src not in ("template",))),
        "description_by_src": dict(desc_src),
        "ai_generated_descriptions": sum(1 for p in pois if p.description and p.description.ai_generated),
        "name_ru": pct(sum(1 for p in pois if p.name.ru)),
        "name_kk": pct(sum(1 for p in pois if p.name.kk)),
        "name_en": pct(sum(1 for p in pois if p.name.en)),
        "website": pct(sum(1 for p in pois if p.website or p.external_ids.get("website"))),
        "phone": pct(sum(1 for p in pois if p.external_ids.get("phone"))),
        "address": pct(sum(1 for p in pois if p.address)),
        "multi_source": pct(sum(1 for p in pois if p.popularity.sources_confirming > 1)),
        "by_category": dict(Counter(p.category for p in pois).most_common()),
        "by_status": dict(Counter(p.status for p in pois)),
        "by_primary_source": dict(Counter(p.poi_id.split(":")[0] for p in pois)),
        "pois_with_source": dict(Counter(s.name for p in pois for s in {x.name: x for x in p.sources}.values())),
        "walk_tags": dict(Counter(t for p in pois for t in p.walk_tags).most_common()),
        "confidence_mean": {k: round(sum(getattr(p.confidence, k) for p in pois) / n, 3)
                            for k in ("exists", "location", "hours")},
    }


def write_landmarks(pois: list[POI], path: Path) -> int:
    from . import anchors

    by_anchor = {p.external_ids.get("anchor"): p for p in pois if p.external_ids.get("anchor")}
    out = []
    for a in anchors.load():
        if not a.get("origin"):
            continue
        p = by_anchor.get(a["id"])
        if not p:
            log.warning("landmark origin %s not in catalog", a["id"])
            continue
        out.append({"id": a["id"], "poi_id": p.poi_id,
                    "name": {"ru": a["names"]["ru"], "kk": a["names"]["kk"], "en": a["names"]["en"]},
                    "lat": round(p.lat, 6), "lon": round(p.lon, 6), "geo_src": p.geo_src})
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(out)


def run(out: Path, sources: list[str], skip_embed: bool = False, offline: bool = False, llm_limit: int = 40,
        landmarks_path: Path | None = None) -> dict:
    util.set_offline(offline)
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    now = datetime.now(timezone.utc)

    def lap(name: str, t: float) -> None:
        timings[name] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    raw, src_info = collect_all(sources, util.DATA_DIR / "raw")
    lap("collect", t)

    t = time.perf_counter()
    poi_raw = [r for s, recs in raw.items() if s not in EVENT_SOURCES for r in recs]
    pois = normalize_records(poi_raw)
    dropped = getattr(normalize_records, "last_dropped", {})
    norm_counts = Counter(p.sources[0].name for p in pois)
    for s in sources:
        if s in src_info:
            src_info[s]["normalized"] = norm_counts.get(s, 0)
            src_info[s]["dropped_not_walking"] = dropped.get(s, 0)
    lap("normalize", t)

    t = time.perf_counter()
    pois = dedupe(pois)
    dstats = getattr(dedupe, "last_stats", {})
    lap("dedup", t)

    t = time.perf_counter()
    pois = apply_anchors(pois)
    astats = getattr(apply_anchors, "last_stats", {})
    pois = score(pois, now, with_anchors=False)
    lap("quality", t)

    t = time.perf_counter()
    pois = template_descriptions(pois)
    n_llm = 0
    if llm_limit > 0 and not offline:
        try:
            n_llm = llm_descriptions(pois, llm_limit)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM descriptions failed: %s", e)
    elif llm_limit > 0:
        n_llm = llm_descriptions(pois, llm_limit) if (util.CACHE_DIR / "llm" / "descriptions.json").exists() else 0
    lap("describe", t)

    t = time.perf_counter()
    events: list[Event] = []
    ev_raw = [r for s in EVENT_SOURCES for r in raw.get(s, [])]
    if ev_raw:
        events = normalize_events(ev_raw, pois, datetime.now(TZ))
        mark_event_venues(pois, events)
        for s in EVENT_SOURCES:
            if s in src_info:
                src_info[s]["normalized"] = sum(1 for e in events if e.src == s)
                src_info[s]["dropped_past"] = len(raw.get(s, [])) - src_info[s]["normalized"]
    lap("events", t)

    pois.sort(key=lambda p: p.poi_id)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "pois.jsonl", "w", encoding="utf-8") as f:
        for p in pois:
            f.write(p.model_dump_json(exclude_none=False) + "\n")
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for e in sorted(events, key=lambda e: e.start):
            f.write(e.model_dump_json() + "\n")

    emb_stats: dict = {"skipped": True}
    t = time.perf_counter()
    if not skip_embed:
        from .embed import embed_pois

        try:
            emb_stats = embed_pois(pois, out)
        except Exception as e:  # noqa: BLE001
            log.error("embedding failed: %s: %s", type(e).__name__, e)
            emb_stats = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    lap("embed", t)

    n_land = write_landmarks(pois, landmarks_path) if landmarks_path else None
    timings["total"] = round(time.perf_counter() - t0, 2)
    report = {
        "generated_at": util.now_iso(),
        "offline": offline,
        "sources": src_info,
        "dedup": dstats,
        "anchors": astats,
        "llm_descriptions": n_llm,
        "coverage": coverage(pois, now),
        "events": {"total": len(events), "with_coordinates": sum(1 for e in events if e.lat is not None),
                   "by_category": dict(Counter(e.category for e in events).most_common()),
                   "raw": len(ev_raw)},
        "embeddings": emb_stats,
        "landmarks_written": n_land,
        "timings_s": timings,
        "licenses": {
            "osm": "© OpenStreetMap contributors, ODbL 1.0 (share-alike for derived databases)",
            "overture": "Overture Maps Foundation, CDLA-Permissive-2.0 (+ per-source notices, e.g. Foursquare Apache-2.0)",
            "wikidata": "CC0", "wikipedia/wikivoyage": "CC BY-SA 4.0 — attribute with link",
            "visitastana": "© visitastana.kz — short excerpts with link", "sxodim": "© sxodim.com — event facts with link",
        },
    }
    (out / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("done in %.1fs: %d POIs, %d events → %s", timings["total"], len(pois), len(events), out)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "data" / "catalog"))
    ap.add_argument("--sources", default=",".join(ALL_SOURCES))
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--offline", action="store_true", help="use only cached downloads (data/cache)")
    ap.add_argument("--llm-describe", type=int, default=40, help="max LLM-written descriptions (0 = off)")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--write-landmarks", nargs="?", const=str(ROOT / "seruen" / "common" / "landmarks.json"),
                    default=None, help="write UI origin points (default path seruen/common/landmarks.json)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    bad = [s for s in sources if s not in ALL_SOURCES]
    if bad:
        ap.error(f"unknown sources: {bad}; known: {ALL_SOURCES}")
    rep = run(Path(a.out), sources, skip_embed=a.skip_embed, offline=a.offline,
              llm_limit=0 if a.no_llm else a.llm_describe,
              landmarks_path=Path(a.write_landmarks) if a.write_landmarks else None)
    c = rep["coverage"]
    print(json.dumps({"pois": c["pois"], "events": rep["events"]["total"], "hours": c["hours_any"],
                      "description_real": c["description_real"], "timings_s": rep["timings_s"],
                      "embeddings": rep["embeddings"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
