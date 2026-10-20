"""Session service: remembers only the hard constraints of a dialogue for 30 minutes. No coordinates, no name."""
from __future__ import annotations

from seruen.common.clients import endpoint
from seruen.common.stores import jget, jset, kv

TTL = 30 * 60


@endpoint("session", "/v1/session/get")
async def get(p: dict) -> dict:
    sid = p.get("session_id")
    return {"constraints": jget(kv(0), f"s:{sid}") if sid else None}


@endpoint("session", "/v1/session/put")
async def put(p: dict) -> dict:
    sid, c = p.get("session_id"), p.get("constraints")
    if sid and c:
        keep = {k: c.get(k) for k in ("kids", "mobility", "avoid", "adults", "lang")}
        jset(kv(0), f"s:{sid}", keep, ex=TTL)
    return {"ok": True}


@endpoint("session", "/v1/session/delete")
async def delete(p: dict) -> dict:
    sid = p.get("session_id")
    if sid:
        kv(0).delete(f"s:{sid}")
    return {"ok": True, "deleted": bool(sid)}
