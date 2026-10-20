"""Settings from environment variables (and .env in local mode)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

try:  # local dev: read .env next to the repo root; in the cluster env vars come from k8s
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:  # pragma: no cover
    pass


class Settings:
    def __init__(self) -> None:
        e = os.environ.get
        self.mode = e("SERUEN_MODE", "local")
        self.service = e("SERVICE", "orchestrator")
        self.data_dir = Path(e("DATA_DIR", str(ROOT / "data")))
        self.catalog_dir = Path(e("CATALOG_DIR", str(self.data_dir / "catalog")))
        self.openai_model = e("OPENAI_MODEL", "gpt-6-luna")
        self.embed_model = e("OPENAI_EMBED_MODEL", "text-embedding-3-large")
        self.embed_dim = int(e("EMBED_DIM", "1024"))
        self.llm_timeout = float(e("LLM_TIMEOUT_S", "8"))
        self.llm_daily_token_budget = int(e("LLM_DAILY_TOKEN_BUDGET", "3000000"))
        # voice input: best ru + kk accuracy in our test (20.10.2026); whisper-1 is the fallback
        self.stt_model = e("OPENAI_STT_MODEL", "gpt-4o-transcribe")
        self.stt_fallback_model = e("OPENAI_STT_FALLBACK_MODEL", "whisper-1")
        self.stt_timeout = float(e("STT_TIMEOUT_S", "15"))
        self.stt_daily_limit = int(e("STT_DAILY_LIMIT", "3000"))
        self.kafka = e("KAFKA_BOOTSTRAP", "")
        self.pg_host = e("PG_HOST", "")
        self.pg_db = e("PG_DB", "seruen")
        self.pg_user = e("PG_USER", "seruen")
        self.pg_password = e("PG_PASSWORD", "")
        self.redis_url = e("REDIS_URL", "")
        self.opensearch_url = e("OPENSEARCH_URL", "")
        self.clickhouse_url = e("CLICKHOUSE_URL", "")
        self.clickhouse_user = e("CLICKHOUSE_USER", "seruen")
        self.clickhouse_password = e("CLICKHOUSE_PASSWORD", "")
        self.minio_endpoint = e("MINIO_ENDPOINT", "")
        self.osrm_url = e("OSRM_URL", "")
        self.osrm_public = e("OSRM_PUBLIC_URL", "https://routing.openstreetmap.de/routed-foot")
        self.otel_endpoint = e("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        self.service_domain = e("SERVICE_DOMAIN", "seruen.svc.cluster.local")
        self.weather_lat = float(e("WEATHER_LAT", "51.1694"))
        self.weather_lon = float(e("WEATHER_LON", "71.4491"))

    @property
    def pg_dsn(self) -> str:
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:5432/{self.pg_db}"

    def service_url(self, name: str) -> str:
        override = os.environ.get("SERVICE_URL_" + name.upper().replace("-", "_"))
        return override or f"http://{name}.{self.service_domain}:8000"


@lru_cache
def settings() -> Settings:
    return Settings()
