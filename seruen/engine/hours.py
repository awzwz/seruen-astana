"""Opening-hours status with an honest confidence level.

The code decides what may be claimed; the LLM only receives the finished label.
Rule: the word "open" is shown only when the hours level is medium or high AND the place stays open
for at least 15 minutes after the planned visit.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional

from seruen.common.models import CATEGORIES, POI, HoursStatus

OPEN_AREA = {"park", "embankment", "monument", "viewpoint", "landmark", "playground"}

_T = {
    "open_until": {"ru": "Открыто до {t}", "kk": "{t} дейін ашық", "en": "Open until {t}"},
    "closes_soon": {"ru": "Закрывается в {t}, успеть впритык", "kk": "{t}-де жабылады, үлгеру қиын", "en": "Closes at {t}, tight"},
    "closed": {"ru": "Будет закрыто к приходу", "kk": "Барғанда жабық болады", "en": "Closed when you arrive"},
    "unknown": {"ru": "Часы неизвестны — не обещаем, что открыто", "kk": "Жұмыс уақыты белгісіз — ашық деп уәде бермейміз",
                "en": "Hours unknown — we can't promise it's open"},
    "open_area": {"ru": "Открытая территория", "kk": "Ашық аумақ", "en": "Open area"},
    "open_area_night": {"ru": "Открытая территория; ночью освещённость не гарантирована",
                        "kk": "Ашық аумақ; түнде жарық болатынына кепілдік жоқ",
                        "en": "Open area; lighting at night is not guaranteed"},
    "low": {"ru": "Обычно {raw}, данные могли устареть", "kk": "Әдетте {raw}, деректер ескірген болуы мүмкін",
            "en": "Usually {raw}, data may be outdated"},
    "medium_src": {"ru": "по данным {src} ({d}) — уточните", "kk": "{src} деректері бойынша ({d}) — нақтылаңыз",
                   "en": "per {src} ({d}) — please check"},
    "high_src": {"ru": "проверено {d}", "kk": "{d} тексерілген", "en": "checked {d}"},
    "holiday": {"ru": "праздник: часы могут отличаться", "kk": "мереке: уақыты өзгеруі мүмкін", "en": "public holiday: hours may differ"},
}

SRC_LABEL = {"osm": "OpenStreetMap", "overture": "Overture Maps", "wikidata": "Wikidata", "wikivoyage": "Wikivoyage",
             "visitastana": "visitastana.kz", "sxodim": "sxodim.com", "seruen-curated": "Seruen", "partner": "заведение", "2gis": "2GIS"}


def t(key: str, lang: str, **kw) -> str:
    return _T[key].get(lang, _T[key]["ru"]).format(**kw)


@lru_cache(maxsize=4096)
def _oh(raw: str, lat: float, lon: float):
    from opening_hours import OpeningHours

    try:
        return OpeningHours(raw, country="KZ", coords=(lat, lon), timezone="Asia/Almaty")
    except TypeError:
        return OpeningHours(raw)
    except Exception:
        return None


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso[:10]).strftime("%m.%Y")
    except Exception:
        return iso[:7]


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


def status_at(poi: POI, arrive: datetime, visit_min: int, lang: str = "ru", is_dark: bool = False,
              is_holiday: bool = False) -> HoursStatus:
    h = poi.opening_hours
    if not h or not h.raw:
        if poi.category in OPEN_AREA or (CATEGORIES.get(poi.category, {}).get("indoor") is False):
            return HoursStatus(status="open_area", label=t("open_area_night" if is_dark else "open_area", lang),
                               level="open_area")
        return HoursStatus(status="unknown", label=t("unknown", lang), level="unknown")

    level = h.level if h.level != "open_area" else "medium"
    if is_holiday and "PH" not in h.raw and level in ("high", "medium"):
        level = "medium" if level == "high" else "low"
    oh = _oh(h.raw, round(poi.lat, 3), round(poi.lon, 3))
    src = SRC_LABEL.get(h.src, h.src)
    if oh is None:
        return HoursStatus(status="unknown", label=t("low", lang, raw=h.raw[:40]), level="low", src=src, as_of=h.as_of)
    a = _naive(arrive)
    leave = a + timedelta(minutes=visit_min)
    try:
        open_now = oh.is_open(a)
        nxt = oh.next_change(a)
    except Exception:
        return HoursStatus(status="unknown", label=t("low", lang, raw=h.raw[:40]), level="low", src=src, as_of=h.as_of)
    nxt = _naive(nxt) if isinstance(nxt, datetime) else None
    if not open_now:
        return HoursStatus(status="closed", label=t("closed", lang), level=level, src=src, as_of=h.as_of)
    until = nxt.strftime("%H:%M") if nxt and nxt - a < timedelta(hours=24) else None
    if nxt and nxt < leave + timedelta(minutes=15):
        st, label = "closes_soon", t("closes_soon", lang, t=until or "?")
    elif level in ("high", "medium"):
        st = "open"
        label = t("open_until", lang, t=until) if until else {"ru": "Открыто", "kk": "Ашық", "en": "Open"}[lang]
    else:
        st = "open"
        usual = {"ru": "Обычно открыто до {t}", "kk": "Әдетте {t} дейін ашық", "en": "Usually open until {t}"}[lang]
        stale = {"ru": "данные {src} ({d}) могли устареть", "kk": "{src} деректері ({d}) ескірген болуы мүмкін",
                 "en": "{src} data ({d}) may be outdated"}[lang]
        if until:
            head = usual.format(t=until)
        elif "24/7" in h.raw:
            head = {"ru": "Обычно круглосуточно", "kk": "Әдетте тәулік бойы", "en": "Usually open 24/7"}[lang]
        else:
            head = {"ru": "Обычно открыто в это время", "kk": "Әдетте бұл уақытта ашық", "en": "Usually open at this time"}[lang]
        label = head + " · " + stale.format(src=src, d=_fmt_date(h.as_of))
    if level == "high" and h.src == "2gis":
        label += " · " + {"ru": "по данным 2GIS", "kk": "2GIS деректері", "en": "per 2GIS"}[lang]
    elif level == "high":
        label += " · " + t("high_src", lang, d=_fmt_date(h.as_of))
    elif level == "medium":
        label += " · " + t("medium_src", lang, src=src, d=_fmt_date(h.as_of))
    if is_holiday and "PH" not in h.raw:
        label += " · " + t("holiday", lang)
    return HoursStatus(status=st, label=label, level=level, src=src, as_of=h.as_of, open_until=until)
