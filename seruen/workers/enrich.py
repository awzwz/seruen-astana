"""enrich: poi.changed without a description → a short description. Facts-only template by default; the LLM only
for curated anchors, always flagged ai_generated=true so the UI labels it."""
from __future__ import annotations

from seruen.common.models import CATEGORIES, Description
from seruen.workers.common import consume, load_poi, log, upsert_pois


def template(p) -> Description:
    cat = CATEGORIES.get(p.category, CATEGORIES["other"])["ru"]
    bits = [cat]
    if "warm_stop" in p.walk_tags or "indoor" in p.walk_tags:
        bits.append("можно зайти погреться")
    if "family" in p.walk_tags or "kids" in p.walk_tags:
        bits.append("подходит для семей")
    if "view" in p.walk_tags:
        bits.append("есть на что посмотреть")
    return Description(text=", ".join(bits).capitalize() + ".", lang="ru", src="template", ai_generated=False)


def handle(items: list[dict]) -> None:
    out = []
    for pid in dict.fromkeys(it["value"]["poi_id"] for it in items):
        p = load_poi(pid)
        if p is None or p.description is not None:
            continue
        p.description = template(p)
        out.append(p)
    if out:
        n = upsert_pois(out)
        log.info("enriched %d", n)


def main() -> None:
    consume(["poi.changed"], "enrich", handle, batch=200)
