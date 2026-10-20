"""Curated anchors (anchors.json): loading + lookup helpers shared by collectors, normalize and quality."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .util import norm_name

PATH = Path(__file__).with_name("anchors.json")


@lru_cache
def load() -> list[dict]:
    return json.loads(PATH.read_text(encoding="utf-8"))["anchors"]


def all_names(a: dict) -> list[str]:
    return [x for x in (*a["names"].values(), *a.get("alt", [])) if x]


@lru_cache
def name_keys() -> frozenset[str]:
    """Normalised names of all anchors (used to keep otherwise-filtered records, e.g. a university building)."""
    return frozenset(k for a in load() for n in all_names(a) if len(k := norm_name(n)) >= 4)


@lru_cache
def qids() -> frozenset[str]:
    return frozenset(a["qid"] for a in load() if a.get("qid"))


@lru_cache
def osm_ids() -> frozenset[str]:
    return frozenset(a["osm"] for a in load() if a.get("osm"))


def is_anchor_name(name: str | None) -> bool:
    return bool(name) and norm_name(name) in name_keys()
