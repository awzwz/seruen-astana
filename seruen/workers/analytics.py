"""analytics: reco.served + feedback → ClickHouse. Only an H3 cell of ~0.7 km², the hour, language and categories.
The view seruen.demand_by_cell publishes a cell only when it has ≥ 20 requests (k-anonymity threshold, our choice)."""
from __future__ import annotations

from datetime import datetime, timezone

from seruen.workers.common import consume, log
from seruen.workers.schema import clickhouse

_ch = None


def handle(items: list[dict]) -> None:
    global _ch
    _ch = _ch or clickhouse()
    rows = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for it in items:
        v = it["value"]
        if it["topic"] == "reco.served":
            for pid, cat in zip(v.get("poi_ids", []), v.get("categories", [])):
                rows.append([now, "reco", v.get("h3_r8") or "", int(v.get("hour") or 0), v.get("lang", ""),
                             v.get("mode", ""), cat, pid, int(bool(v.get("ai")))])
        elif it["topic"] == "feedback":
            rows.append([now, "fb_" + v.get("kind", ""), v.get("h3_r8") or "", now.hour, "", "", "", v.get("poi_id", ""), 0])
    if rows:
        _ch.insert("seruen.events", rows, column_names=["ts", "kind", "h3_r8", "hour", "lang", "mode", "category", "poi_id", "ai"])
        log.info("analytics +%d rows", len(rows))


def main() -> None:
    consume(["reco.served", "feedback"], "analytics", handle, batch=500)
