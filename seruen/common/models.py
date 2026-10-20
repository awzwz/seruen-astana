"""Shared data contracts for every Seruen service and worker.

One POI = one real place. Every field that came from somewhere records its source,
date and confidence level, so code (not the LLM) decides what we can claim.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- taxonomy
CATEGORIES: dict[str, dict] = {
    # key: {ru, kk, en, visit_min, indoor}
    "cafe":        {"ru": "Кафе", "kk": "Кафе", "en": "Cafe", "visit_min": 40, "indoor": True},
    "coffee":      {"ru": "Кофейня", "kk": "Кофехана", "en": "Coffee shop", "visit_min": 30, "indoor": True},
    "restaurant":  {"ru": "Ресторан", "kk": "Мейрамхана", "en": "Restaurant", "visit_min": 60, "indoor": True},
    "fast_food":   {"ru": "Фастфуд", "kk": "Фастфуд", "en": "Fast food", "visit_min": 25, "indoor": True},
    "bar":         {"ru": "Бар", "kk": "Бар", "en": "Bar", "visit_min": 60, "indoor": True},
    "nightclub":   {"ru": "Клуб", "kk": "Клуб", "en": "Night club", "visit_min": 90, "indoor": True},
    "museum":      {"ru": "Музей", "kk": "Мұражай", "en": "Museum", "visit_min": 60, "indoor": True},
    "gallery":     {"ru": "Галерея", "kk": "Галерея", "en": "Gallery", "visit_min": 40, "indoor": True},
    "theatre":     {"ru": "Театр", "kk": "Театр", "en": "Theatre", "visit_min": 120, "indoor": True},
    "cinema":      {"ru": "Кинотеатр", "kk": "Кинотеатр", "en": "Cinema", "visit_min": 120, "indoor": True},
    "concert":     {"ru": "Концертный зал", "kk": "Концерт залы", "en": "Concert hall", "visit_min": 120, "indoor": True},
    "landmark":    {"ru": "Достопримечательность", "kk": "Көрікті жер", "en": "Landmark", "visit_min": 30, "indoor": False},
    "monument":    {"ru": "Памятник", "kk": "Ескерткіш", "en": "Monument", "visit_min": 15, "indoor": False},
    "viewpoint":   {"ru": "Смотровая", "kk": "Көру алаңы", "en": "Viewpoint", "visit_min": 20, "indoor": False},
    "park":        {"ru": "Парк", "kk": "Саябақ", "en": "Park", "visit_min": 40, "indoor": False},
    "embankment":  {"ru": "Набережная, площадь", "kk": "Жағалау, алаң", "en": "Embankment, square", "visit_min": 30, "indoor": False},
    "playground":  {"ru": "Детская площадка", "kk": "Балалар алаңы", "en": "Playground", "visit_min": 30, "indoor": False},
    "attraction":  {"ru": "Развлечения", "kk": "Ойын-сауық", "en": "Attraction", "visit_min": 90, "indoor": True},
    "mall":        {"ru": "Торговый центр", "kk": "Сауда орталығы", "en": "Mall", "visit_min": 60, "indoor": True},
    "market":      {"ru": "Рынок", "kk": "Базар", "en": "Market", "visit_min": 40, "indoor": True},
    "library":     {"ru": "Библиотека", "kk": "Кітапхана", "en": "Library", "visit_min": 60, "indoor": True},
    "coworking":   {"ru": "Коворкинг", "kk": "Коворкинг", "en": "Coworking", "visit_min": 90, "indoor": True},
    "religious":   {"ru": "Мечеть, храм", "kk": "Мешіт, ғибадатхана", "en": "Mosque, church", "visit_min": 30, "indoor": True},
    "sport":       {"ru": "Спорт", "kk": "Спорт", "en": "Sport", "visit_min": 60, "indoor": True},
    "other":       {"ru": "Место", "kk": "Орын", "en": "Place", "visit_min": 30, "indoor": True},
}

# walk tags used by filters and ranking
WALK_TAGS = [
    "indoor", "outdoor", "warm_stop", "family", "kids", "food", "coffee", "wifi", "power",
    "quiet", "night", "view", "culture", "free", "accessible", "photo", "unusual", "event",
]

Level = Literal["high", "medium", "low", "unknown", "open_area"]
Status = Literal["active", "possibly_closed", "unconfirmed", "closed"]


class Names(BaseModel):
    ru: Optional[str] = None
    kk: Optional[str] = None
    en: Optional[str] = None
    alt: list[str] = Field(default_factory=list)

    def best(self, lang: str = "ru") -> str:
        return getattr(self, lang, None) or self.ru or self.kk or self.en or (self.alt[0] if self.alt else "?")


class Description(BaseModel):
    text: str
    lang: str = "ru"
    src: str                       # wikivoyage | wikipedia | visitastana | template | llm
    license: Optional[str] = None
    url: Optional[str] = None
    ai_generated: bool = False


class Hours(BaseModel):
    raw: str                       # OSM opening_hours syntax
    src: str
    as_of: Optional[str] = None    # ISO date of the last edit / check
    level: Level = "unknown"


class Attr(BaseModel):
    value: Optional[object] = None
    src: Optional[str] = None
    as_of: Optional[str] = None
    level: Level = "unknown"


class SourceRef(BaseModel):
    name: str                      # osm | overture | wikidata | wikivoyage | visitastana | sxodim
    id: Optional[str] = None
    license: str
    fetched_at: str
    last_edit: Optional[str] = None
    url: Optional[str] = None


class Popularity(BaseModel):
    wikidata_sitelinks: int = 0
    sources_confirming: int = 1
    curated_in: list[str] = Field(default_factory=list)
    overture_confidence: Optional[float] = None
    rating: Optional[float] = None       # 2GIS live rating (shown, never stored)
    reviews: int = 0


class Confidence(BaseModel):
    exists: float = 0.5
    location: float = 0.5
    hours: float = 0.0


class POI(BaseModel):
    poi_id: str                    # "osm:node/123" | "ovt:<id>" | "wd:Q123" | "va:<slug>"
    name: Names
    lat: float
    lon: float
    geo_src: str
    category: str                  # key of CATEGORIES
    walk_tags: list[str] = Field(default_factory=list)
    description: Optional[Description] = None
    opening_hours: Optional[Hours] = None
    attributes: dict[str, Attr] = Field(default_factory=dict)   # wheelchair, wifi, power_sockets, ...
    visit_min: int = 30
    popularity: Popularity = Field(default_factory=Popularity)
    external_ids: dict[str, str] = Field(default_factory=dict)  # osm, wikidata, overture, website, phone
    address: Optional[str] = None
    website: Optional[str] = None
    sources: list[SourceRef] = Field(default_factory=list)
    confidence: Confidence = Field(default_factory=Confidence)
    status: Status = "active"
    version: int = 1
    updated_at: Optional[str] = None

    def doc_text(self) -> str:
        """Multilingual text that is embedded and searched: names + translit + category + tags + description."""
        cat = CATEGORIES.get(self.category, CATEGORIES["other"])
        parts = [self.name.ru, self.name.kk, self.name.en, *self.name.alt, cat["ru"], cat["kk"], cat["en"],
                 " ".join(self.walk_tags)]
        if self.description:
            parts.append(self.description.text)
        return " | ".join(p for p in parts if p)


class Event(BaseModel):
    event_id: str                  # "sxodim:<slug>"
    title: str
    start: str                     # ISO with +05:00
    end: Optional[str] = None
    venue_name: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    geo_src: Optional[str] = None  # how coordinates were obtained (venue match / geocode)
    price_kzt: Optional[str] = None
    category: Optional[str] = None
    url: Optional[str] = None
    src: str = "sxodim"
    fetched_at: str


# ---------------------------------------------------------------- request side
Lang = Literal["ru", "kk", "en"]


class Chips(BaseModel):
    time_budget_min: Optional[int] = None
    with_kids: bool = False
    wheelchair: bool = False
    group_size: int = 1
    interests: list[str] = Field(default_factory=list)   # walk tags or categories


class Simulation(BaseModel):
    """Demo-only context override (the 'context simulator' sliders)."""
    datetime: Optional[str] = None  # ISO local time
    temp_c: Optional[float] = None
    wind_kmh: Optional[float] = None
    precip: Optional[bool] = None


class RecommendRequest(BaseModel):
    cell: str                      # geohash-7 computed in the browser; exact coordinates never leave it
    text: str = ""
    chips: Chips = Field(default_factory=Chips)
    lang: Lang = "ru"
    sim: Optional[Simulation] = None
    no_ai: bool = False            # "mode without AI" (KZ AI law, art. 16) — also the fallback path
    session_id: Optional[str] = None
    origin_label: Optional[str] = None   # landmark name when the user picked one instead of geolocation


class Constraints(BaseModel):
    time_budget_min: int = 60
    adults: int = 1
    kids: int = 0
    mobility: Literal["walk", "wheelchair", "stroller"] = "walk"
    wants: list[str] = Field(default_factory=list)      # categories / walk tags
    avoid: list[str] = Field(default_factory=list)
    want_route: bool = True
    max_stops: int = 3
    must_return: bool = False      # e.g. back to the start (airport, hotel)
    reserve_min: int = 0           # time to keep at the end (flight, meeting)
    destination: Optional[Literal["airport", "train_station"]] = None   # where the user must be at the end
    night: bool = False
    tourist: bool = False
    query_text: str = ""
    lang: Lang = "ru"
    parsed_by: Literal["llm", "rules"] = "rules"


class Weather(BaseModel):
    temp_c: Optional[float] = None
    feels_c: Optional[float] = None
    wind_kmh: Optional[float] = None
    gust_kmh: Optional[float] = None
    precip_prob: Optional[float] = None
    code: Optional[int] = None
    desc: Optional[str] = None
    src: str = "open-meteo"
    simulated: bool = False


class Context(BaseModel):
    local_time: str
    weekday: int
    is_holiday: bool = False
    holiday_name: Optional[str] = None
    sunrise: Optional[str] = None
    sunset: Optional[str] = None
    is_dark: bool = False
    minutes_to_sunset: Optional[int] = None
    weather: Weather = Field(default_factory=Weather)
    mode: Literal["walk", "short", "indoor"] = "walk"
    mode_reason: str = ""
    simulated: bool = False


class HoursStatus(BaseModel):
    status: Literal["open", "closed", "unknown", "open_area", "closes_soon"]
    label: str
    level: Level
    src: Optional[str] = None
    as_of: Optional[str] = None
    open_until: Optional[str] = None


class Card(BaseModel):
    poi_id: str
    kind: Literal["poi", "event"] = "poi"
    role: Literal["route", "alternative", "pick"] = "pick"   # stop of the mini-route, a backup option, or a single pick
    name: str
    category: str
    category_label: str
    lat: float
    lon: float
    distance_m: int
    walk_min: int
    visit_min: int
    hours: HoursStatus
    source_label: str
    sources: list[str]
    accessibility: Optional[Attr] = None
    tags: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    description_ai: bool = False
    url: Optional[str] = None
    event: Optional[dict] = None
    rating: Optional[float] = None
    reviews: int = 0
    transit: Optional[dict] = None       # 2GIS public transport option (bus, LRT …)
    taxi: Optional[dict] = None          # 2GIS car time with traffic + link to open the route in 2GIS
    score: float = 0.0
    score_parts: dict[str, float] = Field(default_factory=dict)
    status: Status = "active"


class Leg(BaseModel):
    from_id: str
    to_id: str
    meters: int
    minutes: int
    outdoor_min: int
    geometry: list[list[float]] = Field(default_factory=list)   # [[lon, lat], ...]
    approx: bool = False           # True when computed as straight line × 1.3


class Route(BaseModel):
    stops: list[str]
    legs: list[Leg]
    total_min: int
    timeline: list[dict] = Field(default_factory=list)   # [{poi_id, arrive, leave}]
    warm_stops: bool = False


class Explanation(BaseModel):
    poi_id: str
    why: str
    what_to_do: str
    source: Literal["poi_data", "ai_idea"] = "poi_data"
    template: bool = False


class TraceStep(BaseModel):
    name: str
    service: str
    ms: float
    detail: str = ""
