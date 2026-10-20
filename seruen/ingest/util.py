"""Shared helpers for the ingest pipeline: paths, HTTP with cache + politeness, text cleaning, transliteration."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from seruen.common.config import ROOT

log = logging.getLogger("seruen.ingest")

USER_AGENT = "SeruenBot/0.1 (GDG Astana hackathon prototype)"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
CACHE_DIR = DATA_DIR / "cache"

# Astana: OSM relation 3087155 (area 3603087155); bbox of the relation, measured 2026-10-20
ASTANA_REL = 3087155
ASTANA_AREA = 3600000000 + ASTANA_REL
ASTANA_BBOX = (50.8576, 71.2180, 51.3511, 71.7852)      # lat_min, lon_min, lat_max, lon_max
ASTANA_CENTER = (51.1283, 71.4305)                      # Baiterek

# global switch, set by run.py --offline: only cached files are used
OFFLINE = os.environ.get("SERUEN_OFFLINE", "") == "1"


def set_offline(flag: bool) -> None:
    global OFFLINE
    OFFLINE = flag


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def cache_path(*parts: str) -> Path:
    p = CACHE_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def raw_record(source: str, source_id: str, payload: dict, license: str, url: Optional[str] = None,
               fetched_at: Optional[str] = None) -> dict:
    return {"source": source, "source_id": str(source_id), "payload": payload,
            "fetched_at": fetched_at or now_iso(), "license": license, "url": url}


# ------------------------------------------------------------------ HTTP
class OfflineMiss(RuntimeError):
    pass


_robots: dict[str, RobotFileParser] = {}
_last_hit: dict[str, float] = {}


def _robots_ok(url: str, client: httpx.Client) -> bool:
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    rp = _robots.get(base)
    if rp is None:
        rp = RobotFileParser()
        try:
            r = client.get(base + "/robots.txt", timeout=15)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        _robots[base] = rp
    return rp.can_fetch(USER_AGENT, url)


def polite_get(url: str, cache_key: str, *, min_interval: float = 1.0, timeout: float = 30.0,
               max_age_h: float = 20.0, check_robots: bool = True, params: Optional[dict] = None,
               headers: Optional[dict] = None) -> str:
    """GET with a file cache (data/cache/<cache_key>), robots.txt check and ≥ min_interval between hits per host."""
    p = cache_path(cache_key)
    if p.exists() and (OFFLINE or (time.time() - p.stat().st_mtime) < max_age_h * 3600):
        return p.read_text(encoding="utf-8")
    if OFFLINE:
        raise OfflineMiss(cache_key)
    host = urlparse(url).netloc
    h = {"User-Agent": USER_AGENT, "Accept-Language": "ru,kk;q=0.8,en;q=0.6"}
    h.update(headers or {})
    with httpx.Client(headers=h, follow_redirects=True, timeout=timeout) as c:
        if check_robots and not _robots_ok(url, c):
            raise PermissionError(f"robots.txt disallows {url}")
        wait = min_interval - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        r = c.get(url, params=params)
        _last_hit[host] = time.time()
        r.raise_for_status()
        text = r.text
    p.write_text(text, encoding="utf-8")
    return text


def load_json_cache(key: str, max_age_h: float = 1e9) -> Any:
    p = CACHE_DIR / key
    if p.exists() and (OFFLINE or (time.time() - p.stat().st_mtime) < max_age_h * 3600):
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_json_cache(key: str, obj: Any) -> None:
    cache_path(key).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ text hygiene (all downloaded text is untrusted data)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏  ﻿]")


def clean_text(s: Any, cap: int = 600) -> Optional[str]:
    """Strip HTML, decode entities, drop control chars, collapse whitespace, cap length at a word boundary."""
    if s is None:
        return None
    s = str(s)
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(html.unescape(s))
    s = _CTRL.sub(" ", s)
    s = unicodedata.normalize("NFC", s)
    s = _WS.sub(" ", s).strip()
    if not s:
        return None
    if cap and len(s) > cap:
        cut = s[:cap]
        dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        s = cut[:dot + 1] if dot > cap * 0.5 else cut[:cut.rfind(" ")].rstrip(",;:—- ") + "…"
    return s


# ------------------------------------------------------------------ transliteration
_KK_LAT = {
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "ң": "n", "о": "o",
    "ө": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u", "ү": "u", "ф": "f", "х": "kh",
    "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "і": "i", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}
KK_LETTERS = set("әғқңөұүһі")


def translit(s: Optional[str]) -> Optional[str]:
    """Cyrillic (Russian or Kazakh) → simple Latin, keeping case of the first letter of each word."""
    if not s:
        return None
    out = []
    for ch in s:
        lo = ch.lower()
        if lo in _KK_LAT:
            t = _KK_LAT[lo]
            out.append(t.capitalize() if ch != lo and t else t)
        else:
            out.append(ch)
    r = "".join(out)
    return r if r != s else None


def has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[Ѐ-ӿ]", s or ""))


def is_kazakh(s: str) -> bool:
    return any(c in KK_LETTERS for c in (s or "").lower())


def is_latin(s: str) -> bool:
    letters = [c for c in s or "" if c.isalpha()]
    return bool(letters) and all("a" <= c.lower() <= "z" for c in letters)


# generic words removed before name matching (ru / kk / en, after transliteration)
GENERIC = {
    "kafe", "cafe", "coffee", "kofe", "kofeinya", "kofeynya", "kofekhana", "restoran", "restaurant", "meiramkhana",
    "bar", "pub", "lounge", "trts", "trk", "tts", "tsentr", "center", "centre", "mall", "the", "and", "i",
    "mechet", "meshit", "mosque", "muzei", "muzey", "museum", "murazhai", "park", "sayabak", "sayabaq", "saiabaq",
    "teatr", "theatre", "theater", "shop", "store", "magazin", "kz", "astana", "astane", "astany",
    "fast", "food", "fastfud", "pizzeria", "bistro", "grill", "kitchen", "house", "cafe-bar", "kafe-bar",
    "torgovyi", "torgovo", "razvlekatelnyi", "sauda", "oiyn", "sauyq", "ortalygy", "ortalyq", "respubliki",
    "kazakhstan", "qazaqstan", "respublikasy", "gorod", "g", "ul", "pr", "im", "imeni",
}


def norm_name(s: Optional[str]) -> str:
    """Lower-case, transliterate, drop quotes / punctuation / generic words. Used for matching only."""
    if not s:
        return ""
    s = s.lower().replace("ё", "е")
    t = translit(s) or s
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    toks = re.findall(r"[a-z0-9]+", t)
    kept = [x for x in toks if x not in GENERIC]
    return " ".join(kept or toks)


def norm_phone(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    d = re.sub(r"\D", "", s.split(";")[0])
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return d if len(d) == 11 else None


def norm_site(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.split(";")[0].strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("?")[0].rstrip("/")
    if not s or any(x in s for x in ("instagram.com", "facebook.com", "2gis", "t.me", "wa.me", "vk.com", "linktr.ee",
                                     "booking.com", "tripadvisor", "yandex", "google.")):
        return None
    return s.split("/")[0]


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def in_bbox(lat: Optional[float], lon: Optional[float], bbox=ASTANA_BBOX) -> bool:
    return lat is not None and lon is not None and bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]
