"""quality: user feedback loop. «Место закрыто» lowers confidence; 3 reports in 7 days → status possibly_closed,
an alert to `quality.alerts` and a live check task for the verifier (Celery)."""
from __future__ import annotations

from seruen.common.stores import pg_pool, publish
from seruen.workers.common import consume, load_poi, log, upsert_pois

THRESHOLD = 3


def handle(items: list[dict]) -> None:
    touched = []
    with pg_pool().connection() as conn:
        for it in items:
            v = it["value"]
            pid, kind = v.get("poi_id"), v.get("kind")
            if not pid or kind not in ("closed", "wrong", "useful", "not_useful"):
                continue
            row = conn.execute(
                "insert into feedback_agg(poi_id, kind, n, last_at) values (%s,%s,1,now()) on conflict (poi_id, kind) do update "
                "set n = case when feedback_agg.last_at < now() - interval '7 days' then 1 else feedback_agg.n + 1 end, last_at=now() "
                "returning n", (pid, kind)).fetchone()
            if kind in ("closed", "wrong"):
                touched.append((pid, kind, row[0]))
    changed = []
    for pid, kind, n in touched:
        p = load_poi(pid)
        if p is None:
            continue
        p.confidence.exists = round(max(0.1, p.confidence.exists * 0.8), 3)
        if kind == "closed" and n >= THRESHOLD and p.status == "active":
            p.status = "possibly_closed"
            publish("quality.alerts", pid, {"poi_id": pid, "reason": "user_reports_closed", "reports": n})
            try:
                from seruen.workers.verifier import verify

                verify.delay(pid)
            except Exception as e:
                log.warning("verifier enqueue failed: %s", type(e).__name__)
        changed.append(p)
    if changed:
        upsert_pois(changed)
        log.info("feedback applied to %d POIs", len(changed))


def main() -> None:
    consume(["feedback"], "quality", handle, batch=100)
