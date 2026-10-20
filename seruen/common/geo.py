"""Geo helpers: geohash (the privacy cell), haversine, walking estimates, H3 for analytics."""
from __future__ import annotations

import math

_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DEC = {c: i for i, c in enumerate(_B32)}

WALK_M_PER_MIN = 75.0          # 4.5 km/h — median measured on OSRM foot in Astana
SLOW_M_PER_MIN = 58.0          # 3.5 km/h — kids, stroller, wheelchair (heuristic)
DETOUR = 1.3                   # walking / straight line, median 1.28 measured on 105 Astana pairs


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    lat_i, lon_i = [-90.0, 90.0], [-180.0, 180.0]
    out, bit, ch, even = [], 0, 0, True
    while len(out) < precision:
        rng, val = (lon_i, lon) if even else (lat_i, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            ch |= 1 << (4 - bit)
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_B32[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_decode(gh: str) -> tuple[float, float]:
    """Centre of the cell."""
    lat_i, lon_i = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for c in gh.lower():
        cd = _DEC[c]
        for mask in (16, 8, 4, 2, 1):
            rng = lon_i if even else lat_i
            mid = (rng[0] + rng[1]) / 2
            if cd & mask:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return (lat_i[0] + lat_i[1]) / 2, (lon_i[0] + lon_i[1]) / 2


def valid_geohash(gh: str) -> bool:
    return 5 <= len(gh) <= 9 and all(c in _DEC for c in gh.lower())


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_estimate(meters_straight: float, slow: bool = False) -> tuple[int, int]:
    """(walking meters, minutes) from a straight-line distance."""
    m = meters_straight * DETOUR
    speed = SLOW_M_PER_MIN if slow else WALK_M_PER_MIN
    return int(round(m)), max(1, int(math.ceil(m / speed)))


def h3_cell(lat: float, lon: float, res: int = 8) -> str:
    try:
        import h3

        return h3.latlng_to_cell(lat, lon, res)
    except Exception:  # pragma: no cover
        return geohash_encode(lat, lon, 6)


ASTANA_BBOX = (50.95, 71.15, 51.32, 71.80)   # lat_min, lon_min, lat_max, lon_max


def in_astana(lat: float, lon: float) -> bool:
    return ASTANA_BBOX[0] <= lat <= ASTANA_BBOX[2] and ASTANA_BBOX[1] <= lon <= ASTANA_BBOX[3]
