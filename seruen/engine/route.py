"""Mini-route = Orienteering Problem with Time Windows, solved by brute force on ≤ 12 candidates.

Measured on a synthetic Astana set: exhaustive search over 25 candidates finds the optimum in ~5 ms,
OR-Tools needed 100–300 ms and found worse solutions at this size. LLMs alone satisfy all constraints in
0.6 % of TravelPlanner tasks, so the order and timing are computed here, never by the model.

Visits are elastic: a stop may be shortened down to half of its usual duration (min 15 min) to fit the budget;
shortened visits cost a little in the objective so full visits win when they fit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import permutations
from typing import Callable

AIRPORT = (51.0222, 71.4669)
TRAIN_STATION = (51.1056, 71.5325)


@dataclass
class Stop:
    id: str
    score: float
    visit_min: int
    indoor: bool
    open_ok: Callable[[datetime, int], bool]   # (arrive, visit_min) -> can visit fully

    @property
    def vmin(self) -> int:
        return max(15, int(self.visit_min * 0.5))


def _try(perm, origin_min, pair_min, start, budget, short_mode, end_min):
    legs = []
    prev = None
    for s in perm:
        leg = origin_min[s.id] if prev is None else pair_min(prev.id, s.id)
        if short_mode and leg > 15 and not (prev is not None and prev.indoor and s.indoor and leg <= 20):
            return None
        legs.append(leg)
        prev = s
    tail = end_min.get(prev.id, 0) if (end_min is not None and prev is not None) else 0
    walk = sum(legs) + tail
    full = sum(s.visit_min for s in perm)
    avail = budget - walk
    if avail < sum(s.vmin for s in perm):
        return None
    if avail >= full:
        visits = [s.visit_min for s in perm]
    else:
        scale = avail / full
        visits = [max(s.vmin, int(s.visit_min * scale)) for s in perm]
        while sum(visits) > avail:
            i = max(range(len(visits)), key=lambda k: visits[k] - perm[k].vmin)
            if visits[i] <= perm[i].vmin:
                return None
            visits[i] -= 1
    t = start
    timeline, obj = [], 0.0
    for s, leg, v in zip(perm, legs, visits):
        t = t + timedelta(minutes=leg)
        if not s.open_ok(t, v):
            return None
        timeline.append({"poi_id": s.id, "arrive": t.strftime("%H:%M"), "leave": (t + timedelta(minutes=v)).strftime("%H:%M"),
                         "visit_min": v, "shortened": v < s.visit_min})
        t = t + timedelta(minutes=v)
        obj += s.score * (0.85 + 0.15 * v / s.visit_min)
    used = walk + sum(visits)
    obj += 0.05 * min(1.0, used / max(1, budget))
    return obj, timeline


def best_route(origin_min: dict[str, float], pair_min: Callable[[str, str], float], stops: list[Stop],
               start: datetime, budget_min: int, max_stops: int = 3, short_mode: bool = False,
               end_min: dict[str, float] | None = None) -> tuple[list[str], float, list[dict]]:
    """Return (ordered ids, objective, timeline). end_min: minutes from each stop to the final destination."""
    cand = stops[:12]
    best: tuple[list[str], float, list[dict]] = ([], -1.0, [])
    for n in range(min(max_stops, len(cand)), 0, -1):
        for perm in permutations(cand, n):
            r = _try(perm, origin_min, pair_min, start, budget_min, short_mode, end_min)
            if r and r[0] > best[1]:
                best = ([s.id for s in perm], r[0], r[1])
    return best
