"""Raw source records → POI / Event (seruen.common.models).

Category is decided from the *combination* of tags with an ordered rule table (first match wins), never from one
tag in isolation; key landmarks (Baiterek = historic=monument, Khan Shatyr = shop=mall, Ak Orda = building=office)
get their final category from anchors.json in quality.apply_anchors.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from seruen.common.models import CATEGORIES, POI, Attr, Description, Event, Hours, Names, Popularity, SourceRef

from . import anchors
from .util import (clean_text, has_cyrillic, is_kazakh, is_latin, norm_name, norm_phone, norm_site, translit)

log = logging.getLogger("seruen.ingest.normalize")
TZ = timezone(timedelta(hours=5))   # Asia/Almaty since 2024 (UTC+5)

# ======================================================================= OSM tag → category
# (predicate, category, extra walk tags). Ordered: more specific combinations first.
def _t(k, *vals):
    return lambda g: (g(k) in vals) if vals else bool(g(k))


OSM_RULES: list[tuple[Callable, str, list[str]]] = [
    (lambda g: g("amenity") == "place_of_worship", "religious", ["culture"]),
    (lambda g: g("tourism") in ("aquarium", "zoo", "theme_park") or g("leisure") == "water_park",
     "attraction", ["kids", "family", "unusual"]),
    (lambda g: g("tourism") == "museum" or g("amenity") == "planetarium" or g("museum"), "museum", ["culture"]),
    (lambda g: g("tourism") == "gallery" or g("amenity") == "arts_centre", "gallery", ["culture"]),
    (lambda g: g("amenity") == "theatre" and (g("theatre:type") == "concert_hall" or "concert" in (g("theatre:genre") or "")),
     "concert", ["culture"]),
    (_t("amenity", "theatre"), "theatre", ["culture"]),
    (_t("amenity", "cinema"), "cinema", []),
    (_t("amenity", "music_venue", "concert_hall", "events_venue"), "concert", ["culture"]),
    (_t("amenity", "exhibition_centre"), "gallery", ["culture"]),
    (_t("amenity", "library"), "library", ["quiet", "culture"]),
    (lambda g: g("amenity") == "coworking_space" or g("office") == "coworking", "coworking", ["quiet"]),
    (_t("amenity", "internet_cafe"), "attraction", ["wifi", "power"]),
    (_t("amenity", "nightclub"), "nightclub", ["night"]),
    (_t("amenity", "bar", "pub", "biergarten"), "bar", ["night", "food"]),
    (lambda g: g("amenity") == "cafe" and ("coffee" in (g("cuisine") or "") or g("cuisine") == "tea"),
     "coffee", ["coffee", "food"]),
    (_t("amenity", "cafe", "ice_cream"), "cafe", ["food"]),
    (_t("amenity", "restaurant"), "restaurant", ["food"]),
    (_t("amenity", "fast_food", "food_court"), "fast_food", ["food"]),
    (_t("shop", "mall", "department_store"), "mall", ["warm_stop", "family"]),
    (_t("amenity", "marketplace"), "market", []),
    (_t("leisure", "amusement_arcade", "escape_game", "bowling_alley", "trampoline_park", "miniature_golf",
        "adult_gaming_centre"), "attraction", ["unusual"]),
    (_t("leisure", "sports_centre", "stadium", "ice_rink", "sports_hall"), "sport", []),
    (_t("tourism", "viewpoint"), "viewpoint", ["view", "photo", "free"]),
    (lambda g: g("historic") in ("monument", "memorial") or g("memorial") in ("statue", "bust", "stele", "obelisk",
                                                                               "war_memorial", "sculpture"),
     "monument", ["culture", "photo", "free"]),
    (lambda g: g("tourism") == "artwork", "monument", ["photo", "unusual", "free"]),
    (lambda g: g("leisure") in ("park", "nature_reserve") or (g("leisure") == "garden" and g("name")),
     "park", ["free", "family"]),
    (lambda g: g("place") == "square" or (g("highway") in ("pedestrian", "footway") and g("name"))
     or g("man_made") == "embankment", "embankment", ["free", "photo"]),
    (_t("leisure", "playground"), "playground", ["kids", "family", "free"]),
    (lambda g: g("tourism") == "attraction" or (g("historic") and g("name")), "landmark", ["culture", "photo"]),
    (lambda g: g("amenity") == "fountain" and g("name"), "landmark", ["photo", "free"]),
    (lambda g: g("man_made") == "tower" and g("tourism"), "viewpoint", ["view"]),
    (_t("amenity", "community_centre"), "other", []),
    (lambda g: g("railway") == "station" or g("aeroway") == "aerodrome", "other", []),
    (lambda g: g("wikidata") and g("building") and g("name"), "other", []),   # only kept if an anchor picks it up
]
OSM_DROP = [
    lambda g: g("public_transport") in ("stop_position", "platform", "stop_area") or g("railway") in ("stop", "platform", "halt")
    or g("highway") == "bus_stop",
    lambda g: g("memorial") == "plaque" or g("historic") == "memorial" and g("memorial") == "plaque",
    lambda g: g("access") in ("private", "no"),
    lambda g: g("disused") == "yes" or g("abandoned") == "yes",
]
# objects that are useful without a name (the name is generic: "детская площадка в 200 м")
UNNAMED_OK = {"playground", "viewpoint", "park"}


def osm_category(tags: dict) -> tuple[Optional[str], list[str]]:
    g = tags.get
    if any(d(g) for d in OSM_DROP):
        return None, []
    for pred, cat, extra in OSM_RULES:
        try:
            if pred(g):
                return cat, list(extra)
        except Exception:  # noqa: BLE001
            continue
    return None, []


# ======================================================================= Overture basic_category → category
OVT_MAP = {
    "restaurant": "restaurant", "food_service": "restaurant", "casual_eatery": "cafe", "cafe": "cafe",
    "coffee_shop": "coffee", "fast_food_restaurant": "fast_food", "food_truck_stand": "fast_food",
    "food_and_drink": "cafe", "non_alcoholic_beverage_venue": "cafe", "bar": "bar", "lounge": "bar",
    "brewery": "bar", "winery": "bar", "dance_club": "nightclub", "museum": "museum", "art_gallery": "gallery",
    "movie_theater": "cinema", "theatre_venue": "theatre", "performing_arts_venue": "theatre",
    "event_venue": "concert", "cultural_center": "gallery", "arts_and_entertainment": "attraction",
    "amusement_park": "attraction", "amusement_attraction": "attraction", "arcade": "attraction",
    "animal_attraction": "attraction", "park": "park", "garden": "park", "nature_reserve": "park", "beach": "park",
    "recreational_trail_or_path": "park", "playground": "playground", "public_plaza": "embankment",
    "pier": "embankment", "marina": "embankment", "public_fountain": "landmark", "bridge": "landmark",
    "monument": "monument", "sculpture_statue": "monument", "historic_site": "landmark",
    "shopping_mall": "mall", "department_store": "mall", "farmers_market": "market", "library": "library",
    "muslim_place_of_worship": "religious", "christian_place_of_worship": "religious",
    "jewish_place_of_worship": "religious", "stadium_arena": "sport", "sports_and_recreation": "sport",
    "sport_or_fitness_facility": "sport", "skating_rink": "sport", "swimming_pool": "sport",
    "sport_or_recreation_club": "sport", "music_venue": "bar", "train_station": "other", "airport": "other",
}
_MONUMENT_RX = re.compile(r"памятник|монумент|ескерткіш|мемориал|monument|memorial|стела|statue|бюст", re.I)
_CONCERT_RX = re.compile(r"концерт|филармон|concert|hall|зал\b|арена|arena", re.I)

# ======================================================================= keyword rules (wikidata types, wikivoyage, visitastana)
KW_RULES: list[tuple[str, str]] = [
    (r"mosque|мечеть|мешіт|church|cathedral|собор|церк|храм|synagogue|синагог|place of worship|костёл|костел", "religious"),
    (r"oceanarium|aquarium|океанариум|zoo\b|зоопарк|amusement park|water park|аквапарк|theme park|ailand|duman|думан", "attraction"),
    (r"museum|музей|мұражай|planetarium|планетарий", "museum"),
    (r"art gallery|gallery|галере", "gallery"),
    (r"opera|опера|theat|театр", "theatre"),
    (r"concert hall|philharmonic|филармон|концертн|multi-purpose hall", "concert"),
    (r"movie theater|cinema|кинотеатр", "cinema"),
    (r"shopping|mall|\bтрц\b|\bтц\b|торгов|khan shatyr|хан шатыр|mega silk|keruen|керуен", "mall"),
    (r"botanical|ботанич|\bpark\b|парк|garden|\bсад\b|саябақ|protected area|miniature park", "park"),
    (r"monument|memorial|statue|sculpture|obelisk|arch\b|national symbol|памятник|монумент|арка|стела|мемориал|ескерткіш", "monument"),
    (r"observation tower|viewpoint|смотров", "viewpoint"),
    (r"square|площад|алаңы|boulevard|бульвар|embankment|набережн|жағалау|promenade", "embankment"),
    (r"stadium|arena|sports venue|ice rink|velodrome|football venue|стадион|арена|велотрек|ледов", "sport"),
    (r"library|библиотек|кітапхана", "library"),
    (r"coworking|коворкинг", "coworking"),
    (r"palace|château|official residence|architectural landmark|building complex|pyramid|tower|bridge|дворец|сарай|башн|мост|резиденц|байтерек|baiterek|bayterek", "landmark"),
    (r"railway station|international airport|аэропорт|вокзал", "other"),
]
_KW = [(re.compile(p, re.I), c) for p, c in KW_RULES]


def kw_category(text: str) -> Optional[str]:
    for rx, cat in _KW:
        if rx.search(text or ""):
            return cat
    return None


# ======================================================================= walk tags
BIG_INDOOR = {"mall", "museum", "theatre", "cinema", "concert", "library"}
CAT_TAGS = {
    "cafe": ["food"], "coffee": ["coffee", "food"], "restaurant": ["food"], "fast_food": ["food"],
    "bar": ["night", "food"], "nightclub": ["night"], "museum": ["culture", "family"], "gallery": ["culture"],
    "theatre": ["culture"], "concert": ["culture"], "landmark": ["culture", "photo"],
    "monument": ["culture", "photo", "free"], "viewpoint": ["view", "photo", "free"], "park": ["free", "family"],
    "embankment": ["free", "photo"], "playground": ["kids", "family", "free"], "mall": ["family"],
    "library": ["quiet", "culture"], "coworking": ["quiet", "wifi", "power"], "religious": ["culture"],
    "attraction": ["family"], "cinema": ["family"],
}
_NIGHT_RX = re.compile(r"24/7|-\s*(0[0-6]|[0-6]|24)[:.]\d\d|круглосуточ")
_WIFI_RX = re.compile(r"wi-?fi|вай-?фай|интернет", re.I)
_POWER_RX = re.compile(r"розетк|power socket|outlet", re.I)
_KIDS_RX = re.compile(r"детск|kids|children|балалар|для детей|семейн", re.I)


def base_walk_tags(cat: str, extra: list[str], hours_raw: Optional[str] = None) -> list[str]:
    tags = set(extra) | set(CAT_TAGS.get(cat, []))
    tags.add("indoor" if CATEGORIES.get(cat, CATEGORIES["other"])["indoor"] else "outdoor")
    if cat in BIG_INDOOR:
        tags.add("warm_stop")
    if hours_raw and _NIGHT_RX.search(hours_raw):
        tags.add("night")
    return tags  # type: ignore[return-value]


def finish_tags(tags: set, attrs: dict) -> list[str]:
    from seruen.common.models import WALK_TAGS

    w = attrs.get("wheelchair")
    if w and w.value == "yes":
        tags.add("accessible")
    if attrs.get("wifi") and attrs["wifi"].value is True:
        tags.add("wifi")
    if attrs.get("power_sockets") and attrs["power_sockets"].value is True:
        tags.add("power")
    if "indoor" in tags and "outdoor" in tags:
        tags.discard("outdoor")
    return [t for t in WALK_TAGS if t in tags]


# ======================================================================= names
def split_vals(s: Optional[str]) -> list[str]:
    return [x.strip() for x in (s or "").split(";") if x.strip()]


def build_names(primary: Optional[str], ru=None, kk=None, en=None, alts=()) -> Names:
    n = Names(ru=clean_text(ru, 160), kk=clean_text(kk, 160), en=clean_text(en, 160))
    p = clean_text(primary, 160)
    if p:
        if is_kazakh(p):
            n.kk = n.kk or p
        elif is_latin(p):
            n.en = n.en or p
        elif has_cyrillic(p):
            n.ru = n.ru or p
        else:
            n.ru = n.ru or p
    alt: list[str] = []
    for a in [p, *alts]:
        a = clean_text(a, 160)
        if a:
            alt.append(a)
    for x in (n.ru, n.kk, n.en, *list(alt)):
        t = translit(x)
        if t:
            alt.append(t)
    n.alt = dedupe_alt(alt, n)
    return n


def dedupe_alt(alts: list[str], n: Names, cap: int = 10) -> list[str]:
    seen = {x.lower() for x in (n.ru, n.kk, n.en) if x}
    out = []
    for a in alts:
        k = a.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out[:cap]


# ======================================================================= hours text → OSM syntax
_DAYS = [("пн", "Mo"), ("вт", "Tu"), ("ср", "We"), ("чт", "Th"), ("пт", "Fr"), ("сб", "Sa"), ("вс", "Su"),
         ("mon", "Mo"), ("tue", "Tu"), ("wed", "We"), ("thu", "Th"), ("fri", "Fr"), ("sat", "Sa"), ("sun", "Su"),
         ("понедельник", "Mo"), ("вторник", "Tu"), ("среда", "We"), ("четверг", "Th"), ("пятница", "Fr"),
         ("суббота", "Sa"), ("воскресенье", "Su")]
_DAY_RX = r"(?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье|пн|вт|ср|чт|пт|сб|вс|mon|tue|wed|thu|fri|sat|sun|mo|tu|we|th|fr|sa|su)[a-zа-я]*\.?"
_EN2 = {"mo": "Mo", "tu": "Tu", "we": "We", "th": "Th", "fr": "Fr", "sa": "Sa", "su": "Su"}


def _day(tok: str) -> Optional[str]:
    tok = tok.lower().rstrip(".")
    for k, v in _DAYS:
        if tok.startswith(k):
            return v
    return _EN2.get(tok[:2])


def valid_oh(s: str) -> bool:
    try:
        from opening_hours import OpeningHours

        OpeningHours(s)
        return True
    except Exception:  # noqa: BLE001
        return False


def hours_text_to_osm(text: Optional[str]) -> Optional[str]:
    """Best-effort conversion of free-text hours (ru/en) to OSM opening_hours; None if unsure."""
    if not text:
        return None
    t = text.lower().replace("–", "-").replace("—", "-").replace("‒", "-").replace(" до ", "-").replace(" to ", "-")
    t = re.sub(r"\bс\s+(?=\d)", "", t)
    t = t.replace("(", " ").replace(")", " ").replace("/", " ; ")
    if re.search(r"круглосуточ|24/7|24 hours|open 24", t):
        return "24/7"
    every = bool(re.search(r"ежедневно|daily|every day|без выходных|күн сайын", t))
    rules = []
    for m in re.finditer(r"(?:(%s)\s*(?:-\s*(%s))?\s*:?\s*)?(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})"
                         % (_DAY_RX, _DAY_RX), t):
        if m.group(1) and re.search(r"[-–]\s*$", t[:m.start(1)]):
            return None           # "S-Thu …": a day range we could not read — better no hours than wrong hours
        d1 = _day(m.group(1)) if m.group(1) else None
        d2 = _day(m.group(2)) if m.group(2) else None
        h1, m1, h2, m2 = int(m.group(3)), m.group(4), int(m.group(5)), m.group(6)
        if h1 > 24 or h2 > 24:
            continue
        span = f"{h1:02d}:{m1}-{h2:02d}:{m2}"
        days = f"{d1}-{d2}" if d1 and d2 else d1 or ("Mo-Su" if every or not rules else None)
        if not days:
            continue
        rules.append(f"{days} {span}")
    if not rules:
        return None
    m = re.search(r"(?:closed|выходной|выходные)\s*:?\s*(%s)" % _DAY_RX, t)
    if m and _day(m.group(1)):
        rules.append(f"{_day(m.group(1))} off")
    s = "; ".join(rules)
    return s if valid_oh(s) else None


# ======================================================================= helpers
def _date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(s))
    return m.group(1) if m else None


def _src(rec: dict, last_edit: Optional[str] = None, url: Optional[str] = None) -> SourceRef:
    return SourceRef(name=rec["source"], id=rec["source_id"], license=rec["license"], fetched_at=rec["fetched_at"],
                     last_edit=last_edit, url=url or rec.get("url"))


def _poi(rec: dict, pid: str, names: Names, lat: float, lon: float, cat: str, tags: set, *, attrs=None, hours=None,
         desc=None, ext=None, address=None, website=None, last_edit=None, pop=None) -> POI:
    attrs = attrs or {}
    ext = {k: v for k, v in (ext or {}).items() if v}
    if website and "website" not in ext:
        s = norm_site(website)
        if s:
            ext["website"] = s
    return POI(poi_id=pid, name=names, lat=round(lat, 7), lon=round(lon, 7), geo_src=rec["source"], category=cat,
               walk_tags=finish_tags(tags, attrs), description=desc, opening_hours=hours, attributes=attrs,
               visit_min=CATEGORIES.get(cat, CATEGORIES["other"])["visit_min"], popularity=pop or Popularity(),
               external_ids=ext, address=clean_text(address, 200), website=website,
               sources=[_src(rec, last_edit)], updated_at=rec["fetched_at"])


def _desc_flags(text: Optional[str], src: str, as_of: Optional[str], tags: set, attrs: dict) -> None:
    """Keyword facts from curated descriptions (wifi / sockets / kids) — recorded with their source."""
    if not text:
        return
    if _WIFI_RX.search(text) and "wifi" not in attrs:
        attrs["wifi"] = Attr(value=True, src=f"{src}:description", as_of=as_of)
    if _POWER_RX.search(text) and "power_sockets" not in attrs:
        attrs["power_sockets"] = Attr(value=True, src=f"{src}:description", as_of=as_of)
    if _KIDS_RX.search(text):
        tags.add("kids")


# ======================================================================= per source
def from_osm(rec: dict) -> Optional[POI]:
    p = rec["payload"]
    tags: dict = p["tags"]
    g = tags.get
    cat, extra = osm_category(tags)
    anchor_like = (g("wikidata") in anchors.qids() or rec["source_id"] in anchors.osm_ids()
                   or any(anchors.is_anchor_name(g(k)) for k in ("name", "name:ru", "name:en")))
    if not cat and anchor_like:
        cat, extra = "other", []
    if not cat or (cat == "other" and not anchor_like):
        return None
    name = g("name")
    if not name and cat not in UNNAMED_OK:
        return None
    if not name and cat == "park" and g("leisure") != "park":
        return None
    last_edit = _date(p.get("timestamp"))
    as_of = _date(g("check_date:opening_hours")) or _date(g("check_date")) or last_edit
    attrs: dict[str, Attr] = {}
    if g("wheelchair") in ("yes", "limited", "no"):
        attrs["wheelchair"] = Attr(value=g("wheelchair"), src="osm", as_of=_date(g("check_date")) or last_edit)
    ia = g("internet_access")
    if ia:
        attrs["wifi"] = Attr(value=ia in ("wlan", "yes", "wifi", "terminal;wlan", "wlan;terminal"), src="osm",
                             as_of=last_edit)
    sockets = [k for k in tags if k.startswith("socket:")] or ([1] if g("power_supply") == "yes" else [])
    if sockets:
        attrs["power_sockets"] = Attr(value=True, src="osm", as_of=last_edit)
    if g("cuisine"):
        attrs["cuisine"] = Attr(value=g("cuisine"), src="osm", as_of=last_edit)
    if g("fee") in ("yes", "no"):
        attrs["fee"] = Attr(value=g("fee") == "yes", src="osm", as_of=last_edit)
    if g("outdoor_seating") in ("yes", "no"):
        attrs["outdoor_seating"] = Attr(value=g("outdoor_seating") == "yes", src="osm", as_of=last_edit)
    hours = None
    oh = g("opening_hours")
    chk = _date(g("check_date:opening_hours")) or _date(g("check_date"))
    if oh and chk:
        attrs["hours_check_date"] = Attr(value=chk, src="osm", as_of=chk)
    if oh:
        if valid_oh(oh):
            hours = Hours(raw=oh, src="osm", as_of=as_of)
        else:
            attrs["hours_text"] = Attr(value=oh, src="osm", as_of=as_of)
    wt = base_walk_tags(cat, extra, oh)
    if g("fee") == "yes":
        wt.discard("free")
    elif g("fee") == "no":
        wt.add("free")
    if g("kids_area") == "yes" or g("playground"):
        wt.add("kids")
    if cat == "park" and g("leisure") == "garden" and g("garden:type") == "botanical":
        wt.add("unusual")
    if not name:
        cl = CATEGORIES[cat]
        names = Names(ru=cl["ru"], kk=cl["kk"], en=cl["en"])
    else:
        names = build_names(name, g("name:ru"), g("name:kk"), g("name:en"),
                            [*split_vals(g("alt_name")), *split_vals(g("old_name")), *split_vals(g("official_name")),
                             *split_vals(g("short_name")), *split_vals(g("int_name")), *split_vals(g("alt_name:ru")),
                             *split_vals(g("old_name:ru"))])
    addr = " ".join(x for x in (g("addr:street"), g("addr:housenumber")) if x) or None
    website = g("website") or g("contact:website") or g("url")
    phone = g("phone") or g("contact:phone") or g("contact:mobile")
    ext = {"osm": rec["source_id"], "wikidata": g("wikidata"), "phone": norm_phone(phone),
           "instagram": g("contact:instagram"), "brand_wikidata": g("brand:wikidata")}
    poi = _poi(rec, f"osm:{rec['source_id']}", names, p["lat"], p["lon"], cat, wt, attrs=attrs, hours=hours,
               ext=ext, address=addr, website=website, last_edit=last_edit)
    if not name:
        poi.attributes["unnamed"] = Attr(value=True, src="osm")
    return poi


def from_overture(rec: dict) -> Optional[POI]:
    p = rec["payload"]
    bc = p.get("basic_category")
    nm = (p.get("names") or {}).get("primary")
    cat = OVT_MAP.get(bc) or ("other" if anchors.is_anchor_name(nm) else None)
    if not cat or (cat == "other" and not anchors.is_anchor_name(nm)):
        return None
    if cat == "landmark" and _MONUMENT_RX.search(nm or ""):
        cat = "monument"
    if bc == "music_venue" and _CONCERT_RX.search(nm or ""):
        cat = "concert"
    if bc in ("historic_site",):
        cat = kw_category(nm or "") or "landmark"
    common = (p.get("names") or {}).get("common") or {}
    if isinstance(common, list):          # duckdb MAP → list of {key, value}
        common = {x.get("key"): x.get("value") for x in common if isinstance(x, dict)}
    names = build_names(nm, common.get("ru"), common.get("kk"), common.get("en"))
    conf = float(p.get("confidence") or 0)
    addrs = p.get("addresses") or []
    addr = addrs[0].get("freeform") if addrs and isinstance(addrs[0], dict) else None
    sites = p.get("websites") or []
    phones = p.get("phones") or []
    srcs = p.get("sources") or []
    datasets = sorted({s.get("dataset") for s in srcs if isinstance(s, dict) and s.get("dataset")} - {"Overture"})
    lic = sorted({s.get("license") for s in srcs if isinstance(s, dict) and s.get("license")})
    upd = max((s.get("update_time") or "" for s in srcs if isinstance(s, dict)), default="") or None
    tags = base_walk_tags(cat, [])
    brand_qid = ((p.get("brand") or {}).get("wikidata")) if isinstance(p.get("brand"), dict) else None
    ext = {"overture": p["id"], "phone": norm_phone(phones[0]) if phones else None, "brand_wikidata": brand_qid}
    poi = _poi(rec, f"ovt:{p['id']}", names, p["lat"], p["lon"], cat, tags, ext=ext, address=addr,
               website=sites[0] if sites else None, last_edit=_date(upd),
               pop=Popularity(overture_confidence=round(conf, 3)))
    poi.sources[0].license = "Overture Maps: " + ", ".join(lic or ["CDLA-Permissive-2.0"]) + \
        (f" (data: {', '.join(datasets)})" if datasets else "")
    poi.attributes["overture_category"] = Attr(value=bc, src="overture")
    return poi


def from_wikidata(rec: dict) -> Optional[POI]:
    p = rec["payload"]
    if p.get("dissolved"):
        return None
    labels = " | ".join(p.get("type_labels") or [])
    cat = kw_category(labels) or ("other" if p["qid"] in anchors.qids() else None)
    if not cat or not (p.get("ru") or p.get("kk") or p.get("en")):
        return None
    if cat == "other" and p["qid"] not in anchors.qids():
        return None
    names = build_names(None, p.get("ru"), p.get("kk"), p.get("en"), p.get("aliases") or [])
    tags = base_walk_tags(cat, [])
    desc = None
    if p.get("wiki_extract"):
        desc = Description(text=p["wiki_extract"], lang=p.get("wiki_lang") or "ru", src="wikipedia",
                           license="CC BY-SA 4.0", url=p.get("wiki_url"))
    ext = {"wikidata": p["qid"], "osm": p.get("osm")}
    poi = _poi(rec, f"wd:{p['qid']}", names, p["lat"], p["lon"], cat, tags, desc=desc, ext=ext,
               website=p.get("website"), pop=Popularity(wikidata_sitelinks=int(p.get("sitelinks") or 0)))
    poi.attributes["wikidata_types"] = Attr(value=(p.get("type_labels") or [])[:5], src="wikidata")
    if p.get("desc_ru") or p.get("desc_en"):
        poi.attributes["wikidata_description"] = Attr(value=clean_text(p.get("desc_ru") or p.get("desc_en"), 200),
                                                      src="wikidata")
    return poi


def _kw_text(*parts) -> str:
    return " ".join(x for x in parts if x)


def from_wikivoyage(rec: dict) -> Optional[POI]:
    p = rec["payload"]
    kind = p["kind"]
    text = _kw_text(p["name"], p.get("alt"))
    if kind in ("see", "do"):
        cat = kw_category(text) or kw_category((p.get("content") or "")[:160]) or ("landmark" if kind == "see" else "attraction")
    elif kind == "eat":
        cat = "coffee" if re.search(r"coffee|кофе", text, re.I) else \
            "fast_food" if re.search(r"burger|бургер|pizza|пицц|fast|kfc|doner|донер", text, re.I) else \
            "cafe" if re.search(r"cafe|кафе", text, re.I) else "restaurant"
    elif kind == "drink":
        cat = "coffee" if re.search(r"coffee|кофе|\btea\b|чай", text, re.I) else "bar"
    else:  # buy
        cat = "mall" if kw_category(text) == "mall" or re.search(r"plaza|center|centre|центр|mall", text, re.I) else \
            "market" if re.search(r"market|bazaar|базар|рынок", text, re.I) else None
    if not cat:
        return None
    alt = [p["alt"]] if p.get("alt") and not p["alt"].upper().startswith("IATA") else []
    lang = p["lang"]
    names = build_names(p["name"], alts=alt) if lang == "ru" else build_names(None, en=p["name"] if is_latin(p["name"]) else None,
                                                                               alts=[p["name"], *alt])
    tags = base_walk_tags(cat, [])
    attrs: dict[str, Attr] = {}
    hours = None
    le = _date(p.get("lastedit"))
    if p.get("hours"):
        raw = hours_text_to_osm(p["hours"])
        if raw:
            hours = Hours(raw=raw, src="wikivoyage", as_of=le)
        attrs["hours_text"] = Attr(value=p["hours"], src="wikivoyage", as_of=le)
    if p.get("price"):
        attrs["price"] = Attr(value=p["price"], src="wikivoyage", as_of=le)
    desc = None
    if p.get("content"):
        desc = Description(text=p["content"], lang=lang, src="wikivoyage", license="CC BY-SA 4.0", url=p["page_url"])
    _desc_flags(p.get("content"), "wikivoyage", le, tags, attrs)
    ext = {"wikidata": p.get("wikidata"), "phone": norm_phone(p.get("phone")), "wikivoyage": rec["source_id"]}
    located = p.get("lat") is not None
    poi = _poi(rec, f"wv:{rec['source_id']}", names, p["lat"] if located else 0.0, p["lon"] if located else 0.0, cat,
               tags, attrs=attrs, hours=hours, desc=desc, ext=ext, address=p.get("address"), website=p.get("url"),
               last_edit=le, pop=Popularity(curated_in=["wikivoyage"]))
    if not located:
        poi.geo_src = "none"
    return poi


_VA_ICON = {"food": "restaurant", "cafe": "cafe", "museum": "museum", "park": "park", "shop": "mall", "mall": "mall",
            "theatre": "theatre", "religion": "religious", "mosque": "religious"}


def from_visitastana(rec: dict) -> Optional[POI]:
    p = rec["payload"]
    sec = p["section"]
    nm = p.get("name")
    if not nm:
        return None
    text = _kw_text(nm, p.get("name_en"))
    if sec == "where-to-eat":
        cat = "coffee" if re.search(r"coffee|кофе", text, re.I) else "restaurant"
    elif sec == "coworking":
        cat = "coworking"
    elif sec == "exhibition-areas":
        cat = kw_category(text) or "gallery"
    else:
        cat = kw_category(text) or _VA_ICON.get(p.get("icon") or "") or \
            kw_category((p.get("description") or "")[:120]) or "landmark"
    names = build_names(nm, en=p.get("name_en"), kk=p.get("name_kk"))
    tags = base_walk_tags(cat, [])
    attrs: dict[str, Attr] = {}
    hours = None
    asof = rec["fetched_at"][:10]
    if p.get("hours"):
        raw = hours_text_to_osm(p["hours"])
        if raw:
            hours = Hours(raw=raw, src="visitastana", as_of=asof)
        attrs["hours_text"] = Attr(value=p["hours"], src="visitastana", as_of=asof)
    if p.get("price"):
        attrs["price"] = Attr(value=p["price"], src="visitastana", as_of=asof)
    desc = None
    if p.get("description"):
        desc = Description(text=p["description"], lang="ru", src="visitastana", license="© visitastana.kz",
                           url=p["page_url"])
    _desc_flags(p.get("description"), "visitastana", asof, tags, attrs)
    ext = {"visitastana": rec["source_id"], "phone": norm_phone(p.get("phone"))}
    located = p.get("lat") is not None
    poi = _poi(rec, f"va:{rec['source_id']}", names, p["lat"] if located else 0.0, p["lon"] if located else 0.0,
               cat, tags, attrs=attrs, hours=hours, desc=desc, ext=ext, address=p.get("address"),
               website=p.get("website"), pop=Popularity(curated_in=["visitastana"]))
    if not located:
        poi.geo_src = "none"
    return poi


CONVERTERS = {"osm": from_osm, "overture": from_overture, "wikidata": from_wikidata, "wikivoyage": from_wikivoyage,
              "visitastana": from_visitastana}


def normalize_records(raw: list[dict]) -> list[POI]:
    """Raw POI records (any source) → POI list. Records without coordinates get geo_src='none' (dedup attaches
    them to a located POI by Wikidata id / name, or drops them)."""
    out: list[POI] = []
    dropped: dict[str, int] = {}
    for rec in raw:
        fn = CONVERTERS.get(rec["source"])
        if not fn:
            continue
        try:
            poi = fn(rec)
        except Exception as e:  # noqa: BLE001 — one bad record must not stop the batch
            log.warning("normalize %s/%s failed: %s", rec["source"], rec["source_id"], e)
            poi = None
        if poi is None:
            dropped[rec["source"]] = dropped.get(rec["source"], 0) + 1
            continue
        out.append(poi)
    normalize_records.last_dropped = dropped  # type: ignore[attr-defined]
    log.info("normalize: %d POIs, dropped %s", len(out), dropped)
    return out


# ======================================================================= events
EVENT_CATS = [
    (r"stend|stand-?up", "Стендап"), (r"kontsert|concert", "Концерт"), (r"igry|kviz|quiz", "Квиз / игры"),
    (r"kino", "Кино"), (r"vystav|exhibit", "Выставка"), (r"festival", "Фестиваль"),
    (r"teatr|theatre|spektak", "Театр"), (r"vecherin|party", "Вечеринка"), (r"obrazov|educat|lekc", "Лекция / обучение"),
    (r"business", "Бизнес"), (r"tur|tour|ekskurs", "Экскурсия"), (r"deti|kids", "Детям"), (r"sport", "Спорт"),
]
_STREET_RX = re.compile(r"^(ул\.|улица|пр\.|проспект|пр-т|просп|шоссе|бульвар|б-р|мкр|микрорайон|көшесі|даңғылы|"
                        r"астана\b|г\.|место сбора)", re.I)


def _event_dt(s: Optional[str], end: bool = False) -> Optional[datetime]:
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = datetime.fromisoformat(s).replace(tzinfo=TZ)
            return d.replace(hour=23, minute=59) if end else d
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except ValueError:
        return None


def _split_venue(addr: str) -> tuple[Optional[str], Optional[str]]:
    a = re.sub(r"^\s*место сбора:\s*", "", addr or "", flags=re.I).strip()
    if not a:
        return None, None
    parts = re.split(r"\s*[,;]\s*", a, maxsplit=1)
    if len(parts) == 2 and not _STREET_RX.match(parts[0]):
        return clean_text(parts[0], 120), clean_text(parts[1], 200)
    if _STREET_RX.match(a):
        return None, clean_text(a, 200)
    return clean_text(a, 120), None


class VenueIndex:
    """Name index over POIs for venue matching (events) and for attaching geo-less records."""

    def __init__(self, pois: list[POI]):
        self.items = []
        for p in pois:
            if p.geo_src == "none" or p.attributes.get("unnamed"):
                continue
            keys = {norm_name(x) for x in (p.name.ru, p.name.kk, p.name.en, *p.name.alt) if x}
            keys = {k for k in keys if len(k) >= 3}
            if keys:
                self.items.append((p, keys, norm_name(p.address or "")))

    def match(self, name: Optional[str], address: Optional[str] = None, min_score: float = 90):
        from rapidfuzz import fuzz

        q = norm_name(name)
        if len(q) < 3:
            return None, 0.0
        qa = norm_name(address or "")
        scored = []
        for p, keys, paddr in self.items:
            s = max(max(fuzz.ratio(q, k), fuzz.token_set_ratio(q, k) - (0 if len(k) >= 5 else 15)) for k in keys)
            if s >= min_score:
                a = fuzz.token_set_ratio(qa, paddr) if qa and paddr else 0
                scored.append((s + 0.05 * a, s, p))
        if not scored:
            return None, 0.0
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        from seruen.common.geo import haversine_m

        # ambiguous: another equally good candidate far away (chain branches) → no confident match
        for tot, s, p in scored[1:]:
            if best[0] - tot < 3 and haversine_m(best[2].lat, best[2].lon, p.lat, p.lon) > 300:
                return None, 0.0
        return best[2], best[1]


def _catalog_pois() -> list[POI]:
    """Venue index fallback when the caller has no POI list (e.g. the normalizer worker): local catalog snapshot."""
    try:
        from seruen.common.config import settings

        f = settings().catalog_dir / "pois.jsonl"
        if f.exists():
            return [POI.model_validate_json(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
    except Exception as e:  # noqa: BLE001
        log.warning("venue index: catalog not loaded (%s)", type(e).__name__)
    return []


def normalize_events(raw: list[dict], pois: Optional[list[POI]] = None, now: Optional[datetime] = None) -> list[Event]:
    """sxodim Event JSON-LD records → Event list (past events dropped, venue → POI coordinates when confident)."""
    now = now or datetime.now(TZ)
    if pois is None:
        pois = _catalog_pois()
    idx = VenueIndex(pois)
    out: list[Event] = []
    seen_ids: set[str] = set()
    for rec in raw:
        if rec["source"] != "sxodim":
            continue
        ev = rec["payload"]
        title = clean_text(ev.get("name"), 200)
        start = _event_dt(ev.get("startDate"))
        end = _event_dt(ev.get("endDate"), end=True)
        if not title or not start:
            continue
        if (end or start) < now:
            continue
        loc = ev.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr_obj = loc.get("address") if isinstance(loc, dict) else None
        street = addr_obj.get("streetAddress") if isinstance(addr_obj, dict) else (addr_obj or "")
        venue, address = _split_venue(street or "")
        if not venue and isinstance(loc, dict) and loc.get("name") and loc.get("name") != ev.get("name"):
            venue = clean_text(loc["name"], 120)
        lat = lon = geo_src = None
        if venue:
            p, s = idx.match(venue, address)
            if p is not None:
                lat, lon, geo_src = p.lat, p.lon, f"venue_match:{p.poi_id}"
        offers = ev.get("offers") or {}
        if isinstance(offers, list):
            offers = min(offers, key=lambda o: float(o.get("price") or 0)) if offers else {}
        price = offers.get("price")
        price_kzt = None
        if price not in (None, ""):
            try:
                price_kzt = str(int(float(price)))
            except (TypeError, ValueError):
                price_kzt = clean_text(price, 40)
        seg = (offers.get("url") or "").split("/")
        cat_slug = seg[4] if len(seg) > 4 else ""
        category = next((c for rx, c in EVENT_CATS if re.search(rx, cat_slug + " " + title.lower())), "Событие")
        eid = f"sxodim:{rec['source_id']}"
        if eid in seen_ids:
            eid = f"{eid}@{start.date().isoformat()}"
        seen_ids.add(eid)
        out.append(Event(event_id=eid, title=title, start=start.isoformat(), end=end.isoformat() if end else None,
                         venue_name=venue, address=address, lat=lat, lon=lon, geo_src=geo_src, price_kzt=price_kzt,
                         category=category, url=ev.get("url") or rec.get("url"), src="sxodim",
                         fetched_at=rec["fetched_at"]))
    log.info("events: %d upcoming, %d with coordinates", len(out), sum(1 for e in out if e.lat is not None))
    return out
