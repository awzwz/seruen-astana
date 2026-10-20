"""Wikidata collector (SPARQL, items with coordinates ≤ 20 km from Baiterek) + Wikipedia intro extracts (ru, else en).

Structured data: CC0. Wikipedia extracts: CC BY-SA 4.0 (kept with the article URL for attribution).
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote, unquote

import httpx

from ..util import USER_AGENT, cache_path, clean_text, in_bbox, now_iso, raw_record
from .. import util

log = logging.getLogger("seruen.ingest.wikidata")

ENDPOINT = "https://query.wikidata.org/sparql"
RADIUS_KM = 20

SPARQL = """
SELECT ?item ?coord ?sitelinks
  (SAMPLE(?ruL) AS ?ru) (SAMPLE(?kkL) AS ?kk) (SAMPLE(?enL) AS ?en)
  (SAMPLE(?dRu) AS ?descRu) (SAMPLE(?dEn) AS ?descEn)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?type), "entity/"); separator="|") AS ?types)
  (GROUP_CONCAT(DISTINCT ?typeLabel; separator="|") AS ?typeLabels)
  (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
  (SAMPLE(?site) AS ?website) (SAMPLE(?ruArt) AS ?ruwiki) (SAMPLE(?enArt) AS ?enwiki) (SAMPLE(?kkArt) AS ?kkwiki)
  (SAMPLE(?osmw) AS ?osm_way) (SAMPLE(?osmn) AS ?osm_node) (SAMPLE(?osmr) AS ?osm_rel)
  (SAMPLE(?dissolved) AS ?dissolved) (SAMPLE(?inception) AS ?inception)
WHERE {
  SERVICE wikibase:around { ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point(71.4305 51.1283)"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "%d" . }
  ?item wikibase:sitelinks ?sitelinks .
  OPTIONAL { ?item rdfs:label ?ruL FILTER(LANG(?ruL) = "ru") }
  OPTIONAL { ?item rdfs:label ?kkL FILTER(LANG(?kkL) = "kk") }
  OPTIONAL { ?item rdfs:label ?enL FILTER(LANG(?enL) = "en") }
  OPTIONAL { ?item schema:description ?dRu FILTER(LANG(?dRu) = "ru") }
  OPTIONAL { ?item schema:description ?dEn FILTER(LANG(?dEn) = "en") }
  OPTIONAL { ?item skos:altLabel ?alias FILTER(LANG(?alias) IN ("ru", "kk", "en")) }
  OPTIONAL { ?item wdt:P31 ?type . OPTIONAL { ?type rdfs:label ?typeLabel FILTER(LANG(?typeLabel) = "en") } }
  OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?ruArt schema:about ?item ; schema:isPartOf <https://ru.wikipedia.org/> }
  OPTIONAL { ?enArt schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?kkArt schema:about ?item ; schema:isPartOf <https://kk.wikipedia.org/> }
  OPTIONAL { ?item wdt:P10689 ?osmw }
  OPTIONAL { ?item wdt:P11693 ?osmn }
  OPTIONAL { ?item wdt:P402 ?osmr }
  OPTIONAL { ?item wdt:P576 ?dissolved }
  OPTIONAL { ?item wdt:P571 ?inception }
}
GROUP BY ?item ?coord ?sitelinks
""" % RADIUS_KM


def _sparql() -> list[dict]:
    cp = cache_path("wikidata", "sparql.json")
    if cp.exists() and (util.OFFLINE or time.time() - cp.stat().st_mtime < 20 * 3600):
        return json.loads(cp.read_text())["results"]["bindings"]
    if util.OFFLINE:
        raise util.OfflineMiss("wikidata")
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(ENDPOINT, data={"query": SPARQL, "format": "json"},
                           headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
                           timeout=90)
            r.raise_for_status()
            cp.write_text(r.text)
            return r.json()["results"]["bindings"]
        except (httpx.HTTPError, ValueError) as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"wikidata SPARQL failed: {last}")


def _extracts(lang: str, titles: list[str]) -> dict[str, str]:
    """Intro plain-text extracts for article titles (batches of 20), cached."""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 20):
        batch = titles[i:i + 20]
        key = f"wikipedia/{lang}_{util.sha1('|'.join(batch))[:16]}.json"
        try:
            txt = util.polite_get(f"https://{lang}.wikipedia.org/w/api.php", key, min_interval=0.5,
                                  max_age_h=24 * 7, check_robots=False,
                                  params={"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                                          "exlimit": 20, "redirects": 1, "format": "json", "formatversion": 2,
                                          "titles": "|".join(batch)})
        except Exception as e:  # noqa: BLE001
            log.warning("wikipedia %s extracts failed: %s", lang, type(e).__name__)
            continue
        data = json.loads(txt)
        norm = {n["to"]: n["from"] for n in data.get("query", {}).get("normalized", [])}
        redir = {r["to"]: r["from"] for r in data.get("query", {}).get("redirects", [])}
        for p in data.get("query", {}).get("pages", []):
            t = p.get("title")
            ex = p.get("extract")
            if not t or not ex:
                continue
            orig = redir.get(t, t)
            orig = norm.get(orig, orig)
            out[orig] = ex
    return out


def _title(url: str | None) -> str | None:
    if not url:
        return None
    return unquote(url.rsplit("/wiki/", 1)[-1]).replace("_", " ")


def collect() -> list[dict]:
    fetched = now_iso()
    rows = _sparql()
    items: dict[str, dict] = {}
    for b in rows:
        v = {k: x.get("value") for k, x in b.items()}
        qid = v["item"].rsplit("/", 1)[-1]
        m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", v.get("coord", ""))
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        if not in_bbox(lat, lon) or qid in items:
            continue
        items[qid] = {
            "qid": qid, "lat": lat, "lon": lon, "sitelinks": int(v.get("sitelinks") or 0),
            "ru": v.get("ru"), "kk": v.get("kk"), "en": v.get("en"),
            "desc_ru": v.get("descRu"), "desc_en": v.get("descEn"),
            "types": [t for t in (v.get("types") or "").split("|") if t],
            "type_labels": [t for t in (v.get("typeLabels") or "").split("|") if t],
            "aliases": [a for a in (v.get("aliases") or "").split("|") if a][:8],
            "website": v.get("website"), "ruwiki": v.get("ruwiki"), "enwiki": v.get("enwiki"),
            "kkwiki": v.get("kkwiki"),
            "osm": ("way/" + v["osm_way"]) if v.get("osm_way") else ("node/" + v["osm_node"]) if v.get("osm_node")
            else ("relation/" + v["osm_rel"]) if v.get("osm_rel") else None,
            "dissolved": v.get("dissolved"), "inception": (v.get("inception") or "")[:10] or None,
        }
    # Wikipedia intros: ru first, en for the rest
    ru_titles = {qid: _title(it["ruwiki"]) for qid, it in items.items() if it["ruwiki"]}
    ex_ru = _extracts("ru", sorted(set(ru_titles.values()))) if ru_titles else {}
    en_titles = {qid: _title(it["enwiki"]) for qid, it in items.items() if it["enwiki"] and
                 not ex_ru.get(ru_titles.get(qid) or "")}
    ex_en = _extracts("en", sorted(set(en_titles.values()))) if en_titles else {}
    recs = []
    for qid, it in items.items():
        t = ru_titles.get(qid)
        if t and ex_ru.get(t):
            it["wiki_extract"] = clean_text(ex_ru[t], 600)
            it["wiki_lang"], it["wiki_url"] = "ru", it["ruwiki"]
        else:
            t = en_titles.get(qid)
            if t and ex_en.get(t):
                it["wiki_extract"] = clean_text(ex_en[t], 600)
                it["wiki_lang"], it["wiki_url"] = "en", it["enwiki"]
        recs.append(raw_record("wikidata", qid, it, "CC0 (Wikidata); Wikipedia text CC BY-SA 4.0",
                               f"https://www.wikidata.org/wiki/{qid}", fetched))
    log.info("wikidata: %d items, %d with Wikipedia intro", len(recs),
             sum(1 for r in recs if r["payload"].get("wiki_extract")))
    return recs
