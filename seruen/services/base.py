"""Builds a FastAPI app for one service from its registered handlers, with /health, /metrics and tracing."""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from seruen.common.clients import REGISTRY
from seruen.common.config import settings

log = logging.getLogger("seruen")

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    REQS = Counter("seruen_requests_total", "Requests", ["service", "path", "status"])
    LAT = Histogram("seruen_request_seconds", "Latency", ["service", "path"],
                    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16))
except Exception:  # pragma: no cover
    REQS = LAT = None


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","svc":"'
                        + settings().service + '","msg":"%(message)s"}')


def setup_tracing(app: FastAPI, service: str) -> None:
    ep = settings().otel_endpoint
    if not ep:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": f"seruen-{service}"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ep.rstrip("/") + "/v1/traces")))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # pragma: no cover
        log.warning("tracing disabled: %s", type(e).__name__)


def make_app(service: str, extra=None) -> FastAPI:
    setup_logging()
    app = FastAPI(title=f"seruen-{service}", docs_url="/docs", redoc_url=None)

    @app.get("/health")
    async def health():
        return {"ok": True, "service": service, "mode": settings().mode}

    @app.get("/metrics")
    async def metrics():
        if REQS is None:
            return PlainTextResponse("")
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    for path, fn in REGISTRY.get(service, {}).items():
        def make(fn=fn, path=path):
            async def route(req: Request):
                t = time.perf_counter()
                status = "200"
                try:
                    body = await req.json()
                    res = fn(body)
                    if hasattr(res, "__await__"):
                        res = await res
                    return JSONResponse(res)
                except Exception as e:
                    status = "500"
                    log.exception("handler %s failed: %s", path, type(e).__name__)
                    return JSONResponse({"error": type(e).__name__}, status_code=500)
                finally:
                    if REQS is not None:
                        REQS.labels(service, path, status).inc()
                        LAT.labels(service, path).observe(time.perf_counter() - t)
            return route
        app.add_api_route(path, make(), methods=["POST"])
    if extra:
        extra(app)
    setup_tracing(app, service)
    return app
