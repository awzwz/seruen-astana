"""Shared vocabulary of user intents → categories and walk tags, and rule-based query parsing (ru / kk / en)."""
from __future__ import annotations

import re

from seruen.common.models import Chips, Constraints

WANT_MAP: dict[str, tuple[set[str], set[str]]] = {
    "coffee":    ({"coffee", "cafe"}, {"coffee"}),
    "food":      ({"restaurant", "cafe", "fast_food", "coffee", "market"}, {"food"}),
    "museum":    ({"museum", "gallery"}, {"culture"}),
    "culture":   ({"museum", "gallery", "theatre", "concert", "landmark", "religious"}, {"culture"}),
    "park":      ({"park", "embankment", "viewpoint"}, {"outdoor"}),
    "view":      ({"viewpoint", "landmark", "embankment", "monument"}, {"view", "photo"}),
    "night":     ({"bar", "nightclub", "cinema", "concert", "theatre", "restaurant"}, {"night"}),
    "unusual":   ({"attraction", "gallery", "landmark", "viewpoint", "monument"}, {"unusual"}),
    "kids":      ({"playground", "attraction", "park", "museum", "mall"}, {"kids", "family"}),
    "quiet":     ({"coworking", "library", "coffee"}, {"quiet"}),
    "power":     ({"coworking", "library", "coffee", "cafe"}, {"power", "wifi"}),
    "shopping":  ({"mall", "market"}, set()),
    "events":    (set(), {"event"}),
    "landmark":  ({"landmark", "monument", "viewpoint", "religious"}, {"view"}),
    "attraction": ({"attraction"}, set()),
    "sport":     ({"sport"}, set()),
    "religious": ({"religious"}, set()),
    "accessible": (set(), {"accessible"}),
}
VOCAB = sorted(WANT_MAP)

_PATTERNS: list[tuple[str, str]] = [
    ("coffee", r"кофе|кофейн|капучин|латте|coffee|espresso|кофехана"),
    ("food", r"поесть|покушать|еда|голод|ресторан|обед|ужин|завтрак|тамақ|ас ішу|мейрамхана|eat|food|lunch|dinner|restaurant|breakfast"),
    ("museum", r"музе|мұражай|museum"),
    ("culture", r"выставк|галере|культур|театр|опер|көрме|мәдени|exhibit|gallery|cultur|theat|opera"),
    ("park", r"парк|сквер|набережн|прогул|погулять|природ|саябақ|серуен|жағалау|park|stroll|nature|river|embankment"),
    ("view", r"вид|смотров|панорам|фото|көрініс|сурет|view|panoram|photo|skyline"),
    ("night", r"ноч|поздн|вечерин|бар\b|бары|клуб|түн|night|late|party|\bbar|club"),
    ("unusual", r"необычн|интересн|удиви|ерекше|қызық|unusual|unique|hidden|quirky|interesting"),
    ("kids", r"ребён|ребен|дет|сын|доч|малыш|бала|kid|child|family|семь|отбас"),
    ("quiet", r"тих|спокойн|поработать|почитать|тыныш|quiet|calm|work|study"),
    ("power", r"розетк|ноутбук|зарядит|wi-?fi|вайфай|интернет|socket|laptop|charge|outlet"),
    ("shopping", r"торгов|магазин|шопинг|купить|сувенир|сауда|дүкен|mall|shop|souvenir"),
    ("events", r"событи|афиш|концерт|спектакл|фестивал|іс-шара|event|concert|show|festival"),
    ("landmark", r"достопримеч|главн|символ|байтерек|бәйтерек|көрікті|посмотреть|увидеть|көру|көрсет|landmark|sight|must.see|to see"),
    ("attraction", r"аттракц|развлеч|океанариум|аквапарк|ойын|attraction|aquarium|fun"),
    ("sport", r"спорт|каток|бассейн|sport|skating|pool|gym"),
    ("religious", r"мечет|храм|собор|церк|мешіт|mosque|church|cathedral"),
    ("accessible", r"без лестниц|пандус|лифт|доступн|ramp|step.free|accessible"),
]

# A specific kind of place that is not a walk intent: looked up in the whole 2GIS directory (Russian phrase for 2GIS).
PLACE_QUERIES: list[tuple[str, str]] = [
    (r"парикмахер|барбер|постричь|подстричь|стрижк|шаштараз|barber|hairdress|haircut", "парикмахерская"),
    (r"маникюр|педикюр|ногт[иея]|салон красоты|сұлулық салон|nail salon|manicure|beauty salon", "салон красоты"),
    (r"аптек|лекарств|дәріхана|pharmac|drugstore|chemist", "аптека"),
    (r"банкомат|снять (деньги|наличн)|\batm\b|cash machine", "банкомат"),
    (r"обменник|обмен валют|айырбастау|currency exchange|exchange office", "обмен валют"),
    (r"\bбанк(\b|[аеуи]\b|ов\b)|\bbank\b", "банк"),
    (r"заправк|\bазс\b|бензин|жанармай|gas station|petrol station|fuel", "АЗС"),
    (r"электрозаправ|ev charg", "электрозаправка"),
    (r"шиномонтаж|tyre service|tire service", "шиномонтаж"),
    (r"автомойк|car ?wash", "автомойка"),
    (r"автосервис|ремонт (авто|машин)|car (service|repair)", "автосервис"),
    (r"туалет|уборн|дәретхана|\bwc\b|toilet|restroom", "туалет"),
    (r"стоматолог|зубн|тіс дәрігер|dentist", "стоматология"),
    (r"травмпункт|больниц|поликлиник|аурухана|емхана|hospital|clinic|emergency room", "поликлиника"),
    (r"ветеринар|ветклиник|\bvet\b", "ветеринарная клиника"),
    (r"супермаркет|гастроном|продукт(ы|ов|овый)|азық-түлік|grocery|supermarket", "супермаркет"),
    (r"цветочн|букет|купить цвет|florist|flower shop", "цветы"),
    (r"почт(а|у|ы|ой|е)\b|почтов|казпочт|post office", "почта"),
    (r"\bцон\b|спеццон|халыққа қызмет|public service cent", "ЦОН"),
    (r"нотариус|notary", "нотариус"),
    (r"химчистк|dry clean", "химчистка"),
    (r"прачечн|laundr", "прачечная"),
    (r"ремонт (телефон|смартфон|ноутбук)|phone repair", "ремонт телефонов"),
    (r"сим-?карт|салон связи|sim card", "салон связи"),
    (r"ремонт обуви|shoe repair", "ремонт обуви"),
    (r"фото на документ|ксерокс|распечат|печать документ|копицентр|copy cent|printing", "копицентр"),
    (r"массаж|massage", "массаж"),
    (r"сауна|баня|хаммам|монша|sauna", "сауна"),
    (r"тренажёрн|тренажерн|фитнес|\bgym\b|fitness", "фитнес-клуб"),
    (r"гостиниц|отель|хостел|қонақ үй|hotel|hostel", "гостиница"),
    (r"парковк|паркинг|автотұрақ|parking", "парковка"),
]


def parse_place_query(text: str) -> str | None:
    t = (text or "").lower()
    for pat, q in PLACE_QUERIES:
        if re.search(pat, t):
            return q
    return None


_KK = set("әғқңөұүһі")


def detect_lang(text: str, default: str = "ru") -> str:
    low = text.lower()
    if any(c in _KK for c in low):
        return "kk"
    letters = [c for c in low if c.isalpha()]
    if letters and sum(c.isascii() for c in letters) / len(letters) > 0.8:
        return "en"
    return default if not letters else "ru"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_budget(text: str) -> int | None:
    t = text.lower()
    m = re.search(r"(\d+(?:[.,]\d)?)\s*[-–—]\s*(\d+(?:[.,]\d)?)\s*(час|ч\b|сағ|hour|hr|h\b)", t)
    if m:
        return int(round(_num(m.group(2)) * 60))
    m = re.search(r"(\d+)\s*[-–—]\s*(\d+)\s*(мин|min|минут)", t)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d+(?:[.,]\d)?)\s*(час|ч\b|сағ|hour|hr|h\b)", t)
    if m:
        return int(round(_num(m.group(1)) * 60))
    m = re.search(r"(\d+)\s*(мин|min|минут)", t)
    if m:
        return int(m.group(1))
    if re.search(r"полчаса|жарты сағат|half an hour", t):
        return 30
    if re.search(r"пар[уа] час|пару-тройку час|екі сағат|couple of hours|a few hours", t):
        return 120
    if re.search(r"полтора часа|бір жарым сағат|hour and a half", t):
        return 90
    if re.search(r"\bчас\b|\bсағат\b|an hour|one hour", t):
        return 60
    return None


def parse_rules(text: str, chips: Chips, lang: str) -> Constraints:
    t = (text or "").lower()
    wants: list[str] = []
    for key, pat in _PATTERNS:
        if re.search(pat, t):
            wants.append(key)
    c = Constraints(query_text=text or "", lang=lang, parsed_by="rules")
    b = parse_budget(t)
    if b:
        c.time_budget_min = max(15, min(b, 300))
    if re.search(r"вылет|рейс|аэропорт|самолет|самолёт|ұшақ|әуежай|рейс|flight|airport|plane|boarding", t):
        c.destination = "airport"
        c.reserve_min = 60
    if re.search(r"поезд|вокзал|нурлы жол|нұрлы жол|train|station", t):
        c.destination = "train_station"
        c.reserve_min = 30
    m = re.search(r"нас\s*(\d)|(\d)\s*(человек|чел\b|адам|people|of us)", t)
    if m:
        c.adults = int(next(g for g in m.groups() if g and g.isdigit()))
    elif re.search(r"друз|компани|достар|friends|group", t):
        c.adults = 4
    if "kids" in wants:
        c.kids = 1
    if re.search(r"инвалидн|wheelchair|мүгедек", t):
        c.mobility = "wheelchair"
    elif re.search(r"коляск|арба|stroller|pram", t):
        c.mobility = "stroller"
    c.night = "night" in wants
    if re.search(r"турист|впервые|первый раз|приезж|гост[ья] город|турис|tourist|first time|visiting|visitor|қонақ", t):
        c.tourist = True
        for w in ("landmark", "culture"):
            if w not in wants:
                wants.append(w)
    c.wants = [w for w in wants if w != "kids"] if c.kids else wants
    if c.destination == "airport" and not c.wants:
        c.tourist = True
        c.wants = ["landmark", "food"]
    pq = parse_place_query(t)
    if pq:   # «найди парикмахерскую»: a lookup in 2GIS, not a walk
        c.place_query = pq
        c.want_route = False
    walk_words = re.search(r"прогул|погуля|маршрут|пройт|серуен|walk|stroll|route|\bпешком", t)
    if c.wants and set(c.wants) <= {"coffee", "quiet", "power", "food"} and not walk_words:
        c.want_route = False
    return apply_chips(c, chips)


def apply_chips(c: Constraints, chips: Chips) -> Constraints:
    """Chips are explicit user choices — they always win over parsed text."""
    if chips.time_budget_min:
        c.time_budget_min = chips.time_budget_min
    if chips.with_kids:
        c.kids = max(c.kids, 1)
    if chips.wheelchair:
        c.mobility = "wheelchair"
    if chips.group_size and chips.group_size > 1:
        c.adults = chips.group_size
    for i in chips.interests:
        if i in WANT_MAP and i not in c.wants:
            c.wants.append(i)
    if "night" in c.wants:
        c.night = True
    return c
