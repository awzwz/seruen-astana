"""Pool of 2GIS API keys with per-key, per-service accounting, failover and automatic reset.

- Order of use: key 1 until it is exhausted, then key 2 (and back again after key 1 resets).
- Every 2GIS API has its own quota, so accounting is per (key, service): places, geocoder, suggest, routing, transit.
- Soft limit: DGIS_LIMIT_PER_KEY requests per service per DGIS_LIMIT_PERIOD ("month" for demo keys, or "day").
- Hard signal: when 2GIS answers 401/403/429 the key is blocked for that service and the call is retried with the next key.
  * "limit / quota / exceeded / too many requests" → blocked until the next local midnight (Asia/Almaty), then probed again;
    this catches both daily and monthly real resets without knowing which one 2GIS uses.
  * invalid / expired key → blocked for 6 hours, then probed again.
- Raw keys are never stored in Redis or logs: a key is identified by a short hash; status pages show a masked key.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from seruen.common.stores import jget, jset, kv

KZ = timezone(timedelta(hours=5))
SERVICES = ("places", "geocoder", "suggest", "routing", "transit")


def keys() -> list[str]:
    raw = os.environ.get("DGIS_API_KEYS") or os.environ.get("DGIS_API_KEY") or ""
    out = []
    for k in raw.replace(";", ",").split(","):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def kid(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def mask(key: str) -> str:
    return key[:4] + "…" + key[-3:]


def _period() -> str:
    return os.environ.get("DGIS_LIMIT_PERIOD", "month")


def _limit() -> int:
    return int(os.environ.get("DGIS_LIMIT_PER_KEY", "1000"))


def _stamp(now: datetime) -> tuple[str, str]:
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def _next_midnight(now: datetime) -> float:
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def usage(key: str, service: str) -> tuple[int, int]:
    day, month = _stamp(datetime.now(KZ))
    k = kid(key)
    return int(kv(1).get(f"dgis:u:{k}:{service}:{day}") or 0), int(kv(1).get(f"dgis:u:{k}:{service}:{month}") or 0)


def _count(key: str, service: str) -> None:
    day, month = _stamp(datetime.now(KZ))
    k = kid(key)
    kv(1).incrby(f"dgis:u:{k}:{service}:{day}", 1, ex=3 * 86400)
    kv(1).incrby(f"dgis:u:{k}:{service}:{month}", 1, ex=40 * 86400)


def blocked(key: str, service: str) -> dict | None:
    b = jget(kv(1), f"dgis:b:{kid(key)}:{service}")
    if b and b.get("until", 0) > time.time():
        return b
    return None


def _block(key: str, service: str, reason: str, until: float) -> None:
    jset(kv(1), f"dgis:b:{kid(key)}:{service}", {"reason": reason, "until": until,
                                                  "at": datetime.now(KZ).isoformat(timespec="seconds")},
         ex=max(60, int(until - time.time())))


def _soft_exhausted(key: str, service: str) -> bool:
    day_used, month_used = usage(key, service)
    used = month_used if _period() == "month" else day_used
    return used >= _limit()


def order(service: str) -> list[str]:
    """Keys to try, in order: not blocked and under the soft limit first; soft-exhausted ones as a last resort."""
    ks = keys()
    fresh = [k for k in ks if not blocked(k, service) and not _soft_exhausted(k, service)]
    soft = [k for k in ks if not blocked(k, service) and _soft_exhausted(k, service)]
    return fresh + soft


def classify(status: int, body) -> str | None:
    """None if the key is fine, else 'limit' | 'invalid' | 'rate'."""
    code, text = status, ""
    if isinstance(body, dict):
        meta = body.get("meta") or {}
        if meta.get("code"):
            code = int(meta["code"])
            text = str((meta.get("error") or {}).get("message", "")) + " " + str((meta.get("error") or {}).get("type", ""))
        else:
            text = str(body.get("message", "")) + " " + str(body.get("type", ""))
    t = text.lower()
    if code == 429 or "too many" in t or "rate" in t:
        return "rate" if "limit" not in t and "quota" not in t else "limit"
    if code in (401, 402, 403):
        if any(w in t for w in ("limit", "quota", "exceed", "закончил", "превышен")):
            return "limit"
        return "invalid"
    return None


async def request(service: str, method: str, url: str, *, params: dict | None = None, json: dict | None = None,
                  timeout: float = 4.0, client: httpx.AsyncClient | None = None):
    """Call 2GIS with failover across keys. Returns (status, body) or (None, None) when no key could serve."""
    tried = 0
    for key in order(service):
        tried += 1
        p = dict(params or {})
        p["key"] = key
        try:
            if client is not None:
                r = await client.request(method, url, params=p, json=json, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.request(method, url, params=p, json=json)
        except httpx.HTTPError:
            continue
        _count(key, service)
        try:
            body = r.json()
        except Exception:
            body = None
        verdict = classify(r.status_code, body)
        if verdict is None:
            return r.status_code, body
        now = datetime.now(KZ)
        until = {"limit": _next_midnight(now), "invalid": time.time() + 6 * 3600, "rate": time.time() + 60}[verdict]
        _block(key, service, verdict, until)
    return None, None


def status() -> dict:
    """Masked overview for the operator: usage per key and service, blocks and their reset time."""
    out = []
    for i, k in enumerate(keys(), 1):
        row = {"key": f"#{i} {mask(k)}", "services": {}}
        for s in SERVICES:
            day_used, month_used = usage(k, s)
            b = blocked(k, s)
            row["services"][s] = {
                "today": day_used, "this_month": month_used, "soft_limit": f"{_limit()} / {_period()}",
                "state": ("blocked: " + b["reason"]) if b else ("soft-limit reached" if _soft_exhausted(k, s) else "ok"),
                "resets_at": datetime.fromtimestamp(b["until"], KZ).isoformat(timespec="minutes") if b else None,
            }
        out.append(row)
    return {"keys": out, "period": _period(), "rule": "key #1 first; on limit → next key; blocked keys are retried after local midnight"}
