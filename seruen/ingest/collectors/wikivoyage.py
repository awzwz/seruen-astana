"""Wikivoyage collector: listings (see / do / eat / drink / buy) from the en and ru "Astana" pages. CC BY-SA 4.0."""
from __future__ import annotations

import json
import logging
import re

from ..util import clean_text, in_bbox, now_iso, polite_get, raw_record

log = logging.getLogger("seruen.ingest.wikivoyage")

PAGES = [("en", "Astana"), ("ru", "Астана")]
KEEP = {"see", "do", "eat", "drink", "buy"}
LICENSE = "CC BY-SA 4.0 (Wikivoyage contributors)"


def _wikitext(lang: str, page: str) -> tuple[str, str | None]:
    txt = polite_get(f"https://{lang}.wikivoyage.org/w/api.php", f"wikivoyage/{lang}_{page}.json",
                     check_robots=False, min_interval=0.5,
                     params={"action": "parse", "page": page, "prop": "wikitext|revid", "format": "json",
                             "formatversion": 2, "redirects": 1})
    d = json.loads(txt)["parse"]
    return d["wikitext"], str(d.get("revid") or "")


def _strip_wiki(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)          # [[a|b]] → b
    s = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", s)              # [url text] → text
    s = re.sub(r"\{\{[^{}]*\}\}", " ", s)                               # nested small templates
    s = re.sub(r"'{2,}", "", s)
    return clean_text(s, 600)


def _float(s) -> float | None:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def collect() -> list[dict]:
    import mwparserfromhell as mw

    fetched = now_iso()
    recs: list[dict] = []
    for lang, page in PAGES:
        try:
            text, revid = _wikitext(lang, page)
        except Exception as e:  # noqa: BLE001
            log.warning("wikivoyage %s failed: %s", lang, type(e).__name__)
            continue
        code = mw.parse(text)
        section = None
        n = 0
        for tpl in code.filter_templates(recursive=False):
            name = str(tpl.name).strip().lower()
            p = {str(x.name).strip().lower(): str(x.value).strip() for x in tpl.params}
            kind = p.get("type", "").lower() if name in ("listing", "marker") else name
            if kind not in KEEP:
                continue
            lat, lon = _float(p.get("lat")), _float(p.get("long") or p.get("lon"))
            nm = _strip_wiki(p.get("name"))
            if not nm:
                continue
            content = _strip_wiki(p.get("content") or p.get("description"))
            n += 1
            sid = f"{lang}:{re.sub(r'[^0-9a-zа-яёәғқңөұүһі]+', '-', nm.lower()).strip('-')}"
            payload = {
                "lang": lang, "kind": kind, "name": nm, "alt": _strip_wiki(p.get("alt")),
                "lat": lat if in_bbox(lat, lon) else None, "lon": lon if in_bbox(lat, lon) else None,
                "address": _strip_wiki(p.get("address")), "url": (p.get("url") or "").strip() or None,
                "phone": (p.get("phone") or "").strip() or None, "hours": _strip_wiki(p.get("hours")),
                "price": _strip_wiki(p.get("price")), "content": content,
                "wikidata": (p.get("wikidata") or p.get("wdid") or "").strip() or None,
                "lastedit": (p.get("lastedit") or "").strip() or None, "revid": revid,
                "page_url": f"https://{lang}.wikivoyage.org/wiki/{page}",
            }
            recs.append(raw_record("wikivoyage", sid, payload, LICENSE, payload["page_url"], fetched))
        log.info("wikivoyage %s: %d listings", lang, n)
    return recs
