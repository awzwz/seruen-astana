"""Single entrypoint for the single image: `SERVICE=<name> python -m seruen.run`.

online services → uvicorn on :8000; workers → their consume loop; collect-* / initial-load → run once and exit.
SERUEN_MODE=local SERVICE=orchestrator runs the whole system in one process (modular monolith) for dev and demos.
"""
from __future__ import annotations

import importlib
import os
import sys

from seruen.common.config import settings
from seruen.services import SERVICES, load_all, load_service_module

WORKERS = {"normalizer", "dedup", "quality", "enrich", "embedder", "indexer", "verifier", "analytics"}
JOBS = {"collect-osm", "collect-overture", "collect-wikidata", "collect-wikivoyage", "collect-visitastana",
        "collect-sxodim", "initial-load", "schema"}


def build_app(service: str):
    from seruen.services.base import make_app

    if settings().mode == "local":
        load_all()
    mod = load_service_module(service)
    return make_app(service, getattr(mod, "extra", None))


def main() -> None:
    service = os.environ.get("SERVICE") or (sys.argv[1] if len(sys.argv) > 1 else "orchestrator")
    os.environ["SERVICE"] = service
    if service in SERVICES:
        import uvicorn

        app = build_app(service)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="warning",
                    proxy_headers=True, forwarded_allow_ips="*")
    elif service in WORKERS:
        importlib.import_module("seruen.workers." + service).main()
    elif service in JOBS:
        importlib.import_module("seruen.workers.jobs").main(service)
    else:
        raise SystemExit(f"unknown SERVICE={service}")


if __name__ == "__main__":
    main()
