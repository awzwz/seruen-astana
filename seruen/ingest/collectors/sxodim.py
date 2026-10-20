"""sxodim.com events collector: schema.org Event JSON-LD from https://sxodim.com/astana/afisha (+ next pages).

robots.txt allows everything (checked on every run); 1 request / second; pages cached for 6 h.
Raw record payload = the Event JSON-LD object (cleaned later in normalize.normalize_events).
"""
from __future__ import annotations

import json
import logging
import re

from ..util import now_iso, polite_get, raw_record

log = logging.getLogger("seruen.ingest.sxodim")

BASE = "https://sxodim.com/astana/afisha"
MAX_PAGES = 8
LICENSE = "© sxodim.com (schema.org Event markup), link"


def _events(html: str) -> list[dict]:
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        items = d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []
        out.extend(x for x in items if isinstance(x, dict) and x.get("@type") == "Event")
    return out


def collect() -> list[dict]:
    fetched = now_iso()
    first = polite_get(BASE, "sxodim/afisha_1.html", min_interval=1.0, max_age_h=6)
    pages = sorted({int(n) for n in re.findall(r"afisha\?page=(\d+)", first)})
    last = min(max(pages, default=1), MAX_PAGES)
    htmls = [first]
    for n in range(2, last + 1):
        try:
            htmls.append(polite_get(f"{BASE}?page={n}", f"sxodim/afisha_{n}.html", min_interval=1.0, max_age_h=6))
        except Exception as e:  # noqa: BLE001
            log.warning("sxodim page %d: %s", n, type(e).__name__)
            break
    recs, seen = [], set()
    for h in htmls:
        for ev in _events(h):
            url = ev.get("url") or ""
            slug = url.rstrip("/").rsplit("/", 1)[-1] or re.sub(r"\W+", "-", ev.get("name", ""))[:60]
            key = (slug, ev.get("startDate"))
            if not slug or key in seen:
                continue
            seen.add(key)
            recs.append(raw_record("sxodim", slug, ev, LICENSE, url or BASE, fetched))
    log.info("sxodim: %d events from %d pages", len(recs), len(htmls))
    return recs
