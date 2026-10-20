"""visitastana.kz collector (official city tourism portal): curated lists + the interactive map.

robots.txt: `Allow: /` (checked on every run). 1 request / second, User-Agent SeruenBot. Pages cached for 20 h.
Content © visitastana.kz — we keep short excerpts with a link back to the page.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from ..util import clean_text, in_bbox, now_iso, polite_get, raw_record

log = logging.getLogger("seruen.ingest.visitastana")

BASE = "https://visitastana.kz"
LICENSE = "© visitastana.kz, link"
SECTIONS = [
    ("what-to-see", "/ru/about-city/what-to-see/"),
    ("where-to-eat", "/ru/about-city/where-to-eat/"),
    ("coworking", "/ru/coworking/"),
    ("exhibition-areas", "/ru/exhibition-areas/"),
]
MAX_PAGES = 5
DEFAULT_CENTER = (51.128351341624, 71.430721999043)


def _get(path: str) -> str:
    key = "visitastana/" + re.sub(r"[^A-Za-z0-9_.-]+", "_", path.strip("/")) + ".html"
    return polite_get(urljoin(BASE, path), key, min_interval=1.0)


def _strip_comments(h: str) -> str:
    return re.sub(r"<!--.*?-->", " ", h, flags=re.S)


def _first(h: str, patterns: list[str], cap: int = 600) -> str | None:
    for p in patterns:
        m = re.search(p, h, flags=re.S | re.I)
        if m:
            t = clean_text(m.group(1), cap)
            if t and t.lower() != "array":
                return t
    return None


def _coords(h: str) -> tuple[float | None, float | None]:
    for p, order in ((r"center:\s*\[\s*(5\d\.\d+)\s*,\s*(7\d\.\d+)\s*\]", "latlon"),
                     (r"[?&]m=(7\d\.\d+)%2C(5\d\.\d+)", "lonlat"),
                     (r"coords:\s*\[\s*(5\d\.\d+)\s*,\s*(7\d\.\d+)\s*\]", "latlon")):
        m = re.search(p, h)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            lat, lon = (a, b) if order == "latlon" else (b, a)
            if in_bbox(lat, lon):
                return lat, lon
    return None, None


def _listing(section: str, path: str) -> dict[str, dict]:
    """detail path → {card_desc, card_address, card_price} from the section listing (with pagination)."""
    html = _strip_comments(_get(path))
    pages = sorted({int(n) for n in re.findall(r"\?PAGEN_\d+=(\d+)", html)} - {0, 1})[:MAX_PAGES - 1]
    pkey = re.search(r"\?(PAGEN_\d+)=", html)
    htmls = [html]
    for n in pages:
        try:
            htmls.append(_strip_comments(_get(f"{path}?{pkey.group(1)}={n}")))
        except Exception as e:  # noqa: BLE001
            log.warning("visitastana %s page %s: %s", section, n, type(e).__name__)
    cards: dict[str, dict] = {}
    rx = re.compile(r'href="(%s[a-z0-9-]+/?)"' % re.escape(path))
    for h in htmls:
        for m in rx.finditer(h):
            link = m.group(1)
            if "/filter/" in link:
                continue
            link = link if link.endswith("/") else link + "/"
            card = cards.setdefault(link, {})
            # text of the card that follows the link (until the next card)
            tail = h[m.end(): m.end() + 4000]
            nxt = rx.search(tail[200:])
            seg = tail[: 200 + nxt.start()] if nxt else tail
            card.setdefault("card_desc", _first(seg, [r'class="museum__desc">(.*?)</div>',
                                                      r"<h4>.*?</h4>\s*<p>(.*?)</p>\s*<a"]))
            card.setdefault("card_address", _first(seg, [r'class="museum__address">(.*?)</div>']))
            card.setdefault("card_price", _first(seg, [r'class="rest__cost">.*?<span>.*?</span>\s*<span>(.*?)</span>']))
        # exhibition / coworking cards put the description *before* the "Подробнее" link
        for m in re.finditer(r"<h4>(.*?)</h4>\s*<p>(.*?)</p>\s*<a href=\"(%s[a-z0-9-]+/?)\"" % re.escape(path), h, re.S):
            link = m.group(3) if m.group(3).endswith("/") else m.group(3) + "/"
            cards.setdefault(link, {})["card_desc"] = clean_text(m.group(2), 600)
    return cards


def _detail(path: str) -> dict:
    h = _strip_comments(_get(path))
    name = _first(h, [r"<h1[^>]*>(.*?)</h1>", r'class="teatr_ban_title">(.*?)</div>',
                      r'<li class="is-current">\s*<span>(.*?)</span>'], 160)
    lat, lon = _coords(h)
    if (lat, lon) == DEFAULT_CENTER and "байтерек" not in (name or "").lower():
        lat = lon = None          # page template falls back to Baiterek's coordinates
    return {
        "name": name, "lat": lat, "lon": lon,
        "address": _first(h, [r'class="teatr_ban_info">(.*?)</div>',
                              r"Адрес\s*</h3>\s*<p[^>]*>(.*?)</p>",
                              r'banner--gastronomy-single-address">(.*?)</span>'], 200),
        "hours": _first(h, [r'banner--gastronomy-single-schedule">(.*?)</span>',
                            r"(?:Режим работы|Время работы|График работы|Часы работы)\s*:?\s*(?:</[^>]+>\s*<[^>]+>\s*)*([^<]{4,120})"],
                        200),
        "phone": _first(h, [r"Телефон\s*</h3>\s*<p[^>]*>(.*?)</p>", r'href="tel:([^"]+)"'], 60),
        "price": _first(h, [r"Средний чек:?\s*</span>\s*<span[^>]*>(.*?)</span>",
                            r'banner--gastronomy-single-price">(.*?)</div>'], 80),
        "detail_desc": _first(h, [r'class="center_text">(.*?)</div>',
                                  r'gastronomy-single__about-text-container">(.*?)</div>',
                                  r'banner--gastronomy-single-sec-info">\s*<p>(.*?)</p>',
                                  r'class="excurtions-single__text">(.*?)</div>'], 600),
    }


_FIELD = re.compile(r"(\w+):\s*'((?:[^'\\]|\\.)*)'")


def _lang_obj(s: str) -> dict:
    return {k: clean_text(v.replace("\\'", "'"), 600) for k, v in _FIELD.findall(s)}


def _map_points() -> list[dict]:
    h = _strip_comments(_get("/ru/map/"))
    i = h.find("const locations")
    j = h.find("];", i)
    if i < 0 or j < 0:
        return []
    block = h[i:j]
    out = []
    for m in re.finditer(r"title:\s*\{(.*?)\},\s*description:\s*\{(.*?)\},\s*address:\s*\{(.*?)\},\s*link:\s*\{(.*?)\},"
                         r".*?coords:\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\].*?icon:\s*'([^']*)'", block, re.S):
        icon = m.group(7).rsplit("/", 1)[-1].replace(".svg", "")
        lat, lon = float(m.group(5)), float(m.group(6))
        if icon == "hotel" or not in_bbox(lat, lon):
            continue
        out.append({"title": _lang_obj(m.group(1)), "description": _lang_obj(m.group(2)),
                    "address": _lang_obj(m.group(3)), "link": _lang_obj(m.group(4)).get("ru"),
                    "lat": lat, "lon": lon, "icon": icon})
    return out


def collect() -> list[dict]:
    fetched = now_iso()
    recs: list[dict] = []
    for section, path in SECTIONS:
        try:
            cards = _listing(section, path)
        except Exception as e:  # noqa: BLE001
            log.warning("visitastana %s listing failed: %s", section, type(e).__name__)
            continue
        for link, card in cards.items():
            try:
                d = _detail(link)
            except Exception as e:  # noqa: BLE001
                log.warning("visitastana %s: %s", link, type(e).__name__)
                d = {}
            if not d.get("name"):
                continue
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            payload = {"section": section, "name": d["name"], "lat": d.get("lat"), "lon": d.get("lon"),
                       "address": d.get("address") or card.get("card_address"),
                       "hours": d.get("hours"), "phone": d.get("phone"),
                       "price": d.get("price") or card.get("card_price"),
                       "description": card.get("card_desc") or d.get("detail_desc"),
                       "page_url": urljoin(BASE, link)}
            recs.append(raw_record("visitastana", f"{section}/{slug}", payload, LICENSE, payload["page_url"], fetched))
    try:
        pts = _map_points()
    except Exception as e:  # noqa: BLE001
        log.warning("visitastana map failed: %s", type(e).__name__)
        pts = []
    for k, p in enumerate(pts):
        en = (p["title"].get("en") or p["title"].get("ru") or str(k))
        slug = re.sub(r"[^a-z0-9]+", "-", en.lower()).strip("-")[:60]
        payload = {"section": "map", "name": p["title"].get("ru"), "name_kk": p["title"].get("kz"),
                   "name_en": p["title"].get("en"), "lat": p["lat"], "lon": p["lon"],
                   "address": p["address"].get("ru"), "website": p.get("link"), "icon": p["icon"],
                   "description": p["description"].get("ru"), "page_url": f"{BASE}/ru/map/"}
        recs.append(raw_record("visitastana", f"map/{slug}", payload, LICENSE, payload["page_url"], fetched))
    log.info("visitastana: %d records (%d map points)", len(recs), len(pts))
    return recs
