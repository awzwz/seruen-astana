"""Guard: the model never checks itself. Validates every explanation; anything wrong → template text for that card."""
from __future__ import annotations

import re

from seruen.common.clients import endpoint

KK = set("әғқңөұүһі")
INJECTION = re.compile(r"ignore (all|previous)|system prompt|игнорир|забудь инструкц|<\/?poi_data>|```", re.I)
UNSAFE = re.compile(r"\bбезопасн|\bsafe\b|қауіпсіз", re.I)


def lang_ok(text: str, lang: str) -> bool:
    low = text.lower()
    letters = [ch for ch in low if ch.isalpha()]
    if not letters:
        return False
    latin = sum(ch.isascii() for ch in letters) / len(letters)
    if lang == "en":
        return latin > 0.8
    if lang == "kk":
        return latin < 0.3 and any(ch in KK for ch in low)
    return latin < 0.3


def problems(item: dict, allowed: set[str], lang: str) -> list[str]:
    out = []
    if item.get("poi_id") not in allowed:
        out.append("unknown_id")
    for f in ("why", "what_to_do"):
        tx = (item.get(f) or "").strip()
        if not tx:
            out.append(f"empty_{f}")
            continue
        if re.search(r"\d", tx):
            out.append(f"digits_in_{f}")
        if len(tx) > 320:
            out.append(f"too_long_{f}")
        if INJECTION.search(tx):
            out.append("injection_marker")
        if UNSAFE.search(tx):
            out.append("safety_claim")
        if not lang_ok(tx, lang):
            out.append(f"wrong_language_{f}")
    return out


@endpoint("guard", "/v1/check")
async def check(p: dict) -> dict:
    allowed = set(p["allowed_ids"])
    lang = p.get("lang", "ru")
    seen = set()
    good, bad = [], {}
    for it in p.get("items", []):
        pr = problems(it, allowed, lang)
        if it.get("poi_id") in seen:
            pr.append("duplicate")
        if pr:
            bad[it.get("poi_id", "?")] = pr
        else:
            good.append(it)
            seen.add(it["poi_id"])
    missing = sorted(allowed - seen)
    return {"ok": not bad and not missing, "items": good, "problems": bad, "missing": missing}
