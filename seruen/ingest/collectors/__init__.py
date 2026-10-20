"""One module per source; each exposes collect() -> list[raw record dict]
({source, source_id, payload, fetched_at, license, url})."""
from __future__ import annotations

from importlib import import_module
from typing import Callable

POI_SOURCES = ["osm", "overture", "wikidata", "wikivoyage", "visitastana"]
EVENT_SOURCES = ["sxodim"]
ALL_SOURCES = POI_SOURCES + EVENT_SOURCES


def get(name: str) -> Callable[[], list[dict]]:
    if name not in ALL_SOURCES:
        raise KeyError(name)
    return import_module(f"{__name__}.{name}").collect
