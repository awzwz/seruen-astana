"""Time of day, holidays, sun and weather → a walking mode with a stated reason.

Thresholds are our heuristics (named as such in the docs), not a standard:
  walk   — feels ≥ −5 °C, gusts < 40 km/h, precipitation < 50 %, ≥ 45 min of daylight left
  short  — feels −15…−5 °C, or gusts 40–60 km/h, or already dark → outdoor legs ≤ 15 min between indoor stops
  indoor — feels < −15 °C, gusts ≥ 60 km/h, snowstorm / freezing rain
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from seruen.common.models import Context, Weather

KZ_TZ = timezone(timedelta(hours=5))     # Kazakhstan lives on UTC+5 since 1 March 2024

HOLIDAY_NAMES = {
    "Қазақстан Республикасы күні": {"ru": "День Республики", "kk": "Республика күні", "en": "Republic Day"},
    "Тəуелсіздік күні": {"ru": "День Независимости", "kk": "Тәуелсіздік күні", "en": "Independence Day"},
}

SEVERE_CODES = {56, 57, 66, 67, 75, 86, 95, 96, 99}     # freezing drizzle/rain, heavy snow, storms
PRECIP_CODES = set(range(51, 100))

_MODE_REASON = {
    "walk": {"ru": "Погода позволяет гулять", "kk": "Ауа райы серуенге қолайлы", "en": "Good weather for a walk"},
    "short_cold": {"ru": "Холодно: короткие отрезки на улице между тёплыми местами",
                   "kk": "Суық: жылы орындар арасында көшеде аз жүреміз", "en": "Cold: short outdoor legs between warm places"},
    "short_wind": {"ru": "Сильный ветер: держимся ближе к помещениям", "kk": "Қатты жел: ғимараттарға жақын жүреміз",
                   "en": "Strong wind: staying close to indoor places"},
    "short_dark": {"ru": "Уже темно: выбираем освещённые места и короткие переходы",
                   "kk": "Қараңғы түсті: жарық орындар мен қысқа жолдар", "en": "It's dark: lit places and short walks"},
    "short_rain": {"ru": "Вероятны осадки: лучше места под крышей", "kk": "Жауын-шашын ықтимал: жабық орындар жақсы",
                   "en": "Precipitation likely: covered places are better"},
    "indoor": {"ru": "Опасная погода: только помещения, на длинные отрезки — такси",
               "kk": "Қауіпті ауа райы: тек ғимараттар, алыс жолға — такси", "en": "Harsh weather: indoor only, taxi for long legs"},
}


def now_local() -> datetime:
    return datetime.now(KZ_TZ)


def parse_local(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt.replace(tzinfo=KZ_TZ) if dt.tzinfo is None else dt.astimezone(KZ_TZ)


def holiday(d: date) -> tuple[bool, str | None]:
    try:
        import holidays

        kz = holidays.country_holidays("KZ", years=[d.year])
        name = kz.get(d)
        return (name is not None), name
    except Exception:
        return False, None


def sun_times(d: date, lat: float, lon: float) -> tuple[datetime, datetime]:
    """NOAA sunrise/sunset approximation (± 1–2 min), local UTC+5."""
    n = d.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                    - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    zen = math.radians(90.833)
    la = math.radians(lat)
    cos_ha = math.cos(zen) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    sunrise = base + timedelta(minutes=720 - 4 * (lon + ha) - eqt)
    sunset = base + timedelta(minutes=720 - 4 * (lon - ha) - eqt)
    return sunrise.astimezone(KZ_TZ), sunset.astimezone(KZ_TZ)


def wind_chill(temp_c: float, wind_kmh: float) -> float:
    if temp_c > 10 or wind_kmh < 4.8:
        return temp_c
    v = wind_kmh ** 0.16
    return round(13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v, 1)


def weather_desc(code: int | None, lang: str) -> str:
    if code is None:
        return ""
    table = [
        ({0}, ("Ясно", "Ашық", "Clear")), ({1, 2}, ("Переменная облачность", "Бұлтты", "Partly cloudy")),
        ({3}, ("Пасмурно", "Түнек", "Overcast")), ({45, 48}, ("Туман", "Тұман", "Fog")),
        (set(range(51, 68)), ("Дождь", "Жаңбыр", "Rain")), (set(range(71, 78)) | {85, 86}, ("Снег", "Қар", "Snow")),
        ({80, 81, 82}, ("Ливень", "Нөсер", "Showers")), ({95, 96, 99}, ("Гроза", "Найзағай", "Thunderstorm")),
    ]
    i = {"ru": 0, "kk": 1, "en": 2}.get(lang, 0)
    for codes, names in table:
        if code in codes:
            return names[i]
    return ""


def decide_mode(w: Weather, is_dark: bool, minutes_to_sunset: int | None, lang: str) -> tuple[str, str]:
    feels = w.feels_c if w.feels_c is not None else w.temp_c
    gust = w.gust_kmh if w.gust_kmh is not None else (w.wind_kmh or 0) * 1.5
    severe = (w.code in SEVERE_CODES) if w.code is not None else False
    if (feels is not None and feels < -15) or gust >= 60 or severe:
        return "indoor", _MODE_REASON["indoor"][lang]
    if feels is not None and feels < -5:
        return "short", _MODE_REASON["short_cold"][lang]
    if gust >= 40:
        return "short", _MODE_REASON["short_wind"][lang]
    if (w.precip_prob or 0) >= 50 or (w.code in PRECIP_CODES if w.code is not None else False):
        return "short", _MODE_REASON["short_rain"][lang]
    if is_dark or (minutes_to_sunset is not None and minutes_to_sunset < 45):
        return "short", _MODE_REASON["short_dark"][lang]
    return "walk", _MODE_REASON["walk"][lang]


def build_context(now: datetime, weather: Weather, lat: float, lon: float, lang: str, simulated: bool) -> Context:
    is_hol, hname = holiday(now.date())
    sunrise, sunset = sun_times(now.date(), lat, lon)
    is_dark = not (sunrise <= now <= sunset)
    mts = int((sunset - now).total_seconds() // 60) if now < sunset else None
    mode, reason = decide_mode(weather, is_dark, mts, lang)
    if hname:
        hname = HOLIDAY_NAMES.get(hname, {}).get(lang, hname)
    return Context(local_time=now.isoformat(timespec="minutes"), weekday=now.weekday(), is_holiday=is_hol,
                   holiday_name=hname, sunrise=sunrise.strftime("%H:%M"), sunset=sunset.strftime("%H:%M"),
                   is_dark=is_dark, minutes_to_sunset=mts, weather=weather, mode=mode, mode_reason=reason,
                   simulated=simulated)
