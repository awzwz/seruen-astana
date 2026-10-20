"""verifier: Celery worker (broker = Redis db 2). Live check of a place reported as closed.

We do not store 2GIS/Google data (their terms forbid it), so the check uses open signals:
the OSM object still exists and has no disused:/was: tags; the venue website still answers.
"""
from __future__ import annotations

import logging

import httpx
from celery import Celery

from seruen.common.config import settings

log = logging.getLogger("seruen")


def _broker() -> str:
    url = settings().redis_url or "redis://localhost:6379/0"
    base = url.rsplit("/", 1)[0] if url.count("/") > 2 else url
    return f"{base}/2"


app = Celery("seruen-verifier", broker=_broker(), backend=None)
app.conf.update(task_acks_late=True, worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True)

UA = {"User-Agent": "SeruenBot/0.1 (GDG Astana hackathon prototype)"}


@app.task(name="verify", autoretry_for=(httpx.HTTPError,), retry_backoff=30, max_retries=3)
def verify(poi_id: str) -> str:
    from seruen.workers.common import load_poi, upsert_pois

    p = load_poi(poi_id)
    if p is None:
        return "missing"
    verdict = "unknown"
    osm = p.external_ids.get("osm") or (poi_id[4:] if poi_id.startswith("osm:") else None)
    with httpx.Client(timeout=10, headers=UA, follow_redirects=True) as c:
        if osm and "/" in osm:
            r = c.get(f"https://api.openstreetmap.org/api/0.6/{osm}.json")
            if r.status_code == 410:
                verdict = "closed"
            elif r.status_code == 200:
                tags = (r.json().get("elements") or [{}])[0].get("tags", {})
                if any(k.startswith(("disused:", "was:", "abandoned:")) for k in tags):
                    verdict = "closed"
                else:
                    verdict = "exists"
        if verdict != "closed" and p.website:
            try:
                w = c.head(p.website)
                verdict = "exists" if w.status_code < 400 else verdict
            except httpx.HTTPError:
                pass
    if verdict == "closed":
        p.status = "closed"
    elif verdict == "exists":
        p.status = "unconfirmed"   # still unclear: keep it lower in ranking until a human or the venue confirms
    upsert_pois([p])
    log.info("verify %s → %s", poi_id, verdict)
    return verdict


def main() -> None:
    app.worker_main(["worker", "--loglevel=INFO", "--concurrency=1", "--pool=solo"])
