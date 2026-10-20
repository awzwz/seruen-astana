# Seruen — contracts between parts

This file is the source of truth for names, ports, topics and data formats.
Python models live in `seruen/common/models.py`.

## Runtime modes

- `SERUEN_MODE=local` — every service is a Python module called in-process (dev, tests, the fallback demo on a laptop).
- `SERUEN_MODE=cluster` — every service is its own Deployment; calls go over HTTP/JSON to
  `http://<name>.seruen.svc.cluster.local:8000` (Linkerd adds mTLS, retries and metrics).
  gRPC is a roadmap item; HTTP/JSON keeps the build simple.

One container image for everything: `registry.digitalocean.com/seruen/seruen:<tag>`.
Entrypoint `python -m seruen.run`, role chosen by the env var `SERVICE` (service name, worker name or job name).

## Online services (namespace `seruen`, port 8000, all FastAPI)

| name | endpoints |
|---|---|
| `orchestrator` | `POST /api/recommend` (SSE), `POST /api/transcribe?lang=ru|kk|en` (raw audio body ≤ 1.9 MB → `{ok, text, model}`), `GET /api/where`, `GET /api/suggest`, `GET /api/landmarks`, `POST /api/feedback`, `DELETE /api/session/{id}`, `GET /api/health`, serves `web/` at `/` |
| `session` | `POST /v1/session/get`, `POST /v1/session/put`, `DELETE /v1/session/{id}` (Redis, TTL 30 min) |
| `context` | `POST /v1/context {cell, sim}` → `Context` |
| `nlu` | `POST /v1/parse {text, chips, lang, prev}` → `Constraints` |
| `retrieval` | `POST /v1/candidates` → candidates (POIs within radius + lexical + vector scores) and events |
| `ranking` | `POST /v1/rank` → ranked ids with score parts |
| `routing` | `POST /v1/route` → walking minutes per POI and the mini-route (≤ 3 stops) |
| `explain` | `POST /v1/explain` → `Explanation[]` (LLM call 2, enum of candidate ids) |
| `guard` | `POST /v1/check` → validated explanations or template fallback |
| `llm-gateway` | `POST /v1/complete`, `POST /v1/embed` (OpenAI; budget, retries, Redis cache), `POST /v1/transcribe {audio_b64, mime, lang, prompt}` (speech → text: `gpt-4o-transcribe`, fallback `whisper-1`; daily cap; the audio is never cached or stored) |
| `catalog-api` | `GET /v1/poi/{id}`, `GET /v1/poi?ids=` (PostgreSQL) |
| `events-api` | `GET /v1/events?lat=&lon=&radius_m=&from=&to=` |
| `feedback-api` | `POST /v1/feedback {poi_id, kind, cell}` → Kafka `feedback` |
| `partner-portal` | `GET /partner`, `POST /v1/partner/hours` (pending moderation, labelled) |
| `places-live` | 2GIS gateway, nothing stored except the geocoding cache: `POST /v1/nearby` (live places for the walk), `POST /v1/search {q, lat, lon}` (any kind of place, radius 1.5 → 4 → 10 km), `/v1/where` (address of the cell), `/v1/suggest` («Откуда?»), `/v1/walk`, `/v1/transit`, `/v1/taxi`, `/v1/keys` (masked key-pool status) |

## Workers and jobs (namespace `seruen`)

| name | kind | reads | writes |
|---|---|---|---|
| `collect-osm`, `collect-overture`, `collect-wikidata`, `collect-wikivoyage`, `collect-visitastana`, `collect-sxodim` | CronJob | the source | Kafka `poi.raw` / `events.raw`, then one `ingest.run` marker |
| `normalizer` | Deployment, KEDA on lag | `poi.raw`, `events.raw` | PG `staging_poi`, `event`; Kafka `poi.normalized` |
| `dedup` | Deployment | `ingest.run` | PG `poi` (merge + confidence), outbox → `poi.changed` |
| `quality` | Deployment | `feedback` | PG `poi.confidence/status`, Kafka `quality.alerts`, Celery tasks for `verifier` |
| `enrich` | Deployment | `poi.changed` (no description) | PG `poi.description` (`ai_generated=true`) |
| `embedder` | Deployment, KEDA | `poi.changed` | PG `poi_embedding`, Kafka `poi.embedded` |
| `indexer` | Deployment | `poi.embedded` | OpenSearch index `poi`, Redis cache invalidation |
| `verifier` | Celery worker (Redis broker) | tasks | PG `poi.status` |
| `analytics` | Deployment | `reco.served`, `feedback` | ClickHouse `seruen.events` (H3 r8, no ids, no coordinates) |

## Kafka topics (Strimzi, 1 broker, RF=1)

`poi.raw`, `events.raw`, `ingest.run`, `poi.normalized`, `poi.changed`, `poi.embedded`,
`feedback`, `reco.served`, `quality.alerts`, `dlq`. Keys = `poi_id` or `source:source_id`.
No topic ever carries exact coordinates of a user or a user id.

## Data stores (namespace `data`)

- PostgreSQL + PostGIS (CloudNativePG cluster `catalog`, service `catalog-rw.data:5432`, db `seruen`).
  Tables: `staging_poi`, `poi`, `poi_embedding`, `event`, `outbox`, `feedback_agg`, `partner_edit`.
- OpenSearch `opensearch.data:9200`, index `poi`: text fields, `location` geo_point, `embedding` knn_vector(1024, cosine), `data` (full POI JSON, not indexed).
- Redis `redis.data:6379` — sessions (db 0), caches (db 1), Celery broker (db 2).
- ClickHouse `clickhouse.data:8123`, MinIO `minio.data:9000` (raw dumps bucket `raw`), OSRM `osrm.data:5000` (foot, Astana extract).

## SSE contract of `POST /api/recommend`

Request body = `RecommendRequest` (`cell` is geohash-7 computed in the browser).
Response `text/event-stream`, events in this order:

| event | data |
|---|---|
| `context` | `Context` |
| `constraints` | `Constraints` |
| `cards` | `{items: Card[], route: Route \| null, origin: {lat, lon, cell}}` — facts computed by code, arrives in ~1–2 s |
| `text` | `Explanation` — one per card, may arrive later |
| `trace` | `{steps: TraceStep[], sent_to_llm: string[], not_sent: string[], model, mode: "ai" \| "template"}` |
| `done` | `{request_id, ai_generated: bool, fallback: bool}` |
| `error` | `{message}` |

The browser recomputes distances from its own exact position; server distances are from the cell centre (≤ ~90 m off).

## Local data files (`SERUEN_MODE=local` and the initial load)

- `data/catalog/pois.jsonl` — one `POI` per line.
- `data/catalog/events.jsonl` — one `Event` per line.
- `data/catalog/embeddings.npy` + `data/catalog/embedding_ids.json` — float32 matrix (N × 1024), row order = ids.
- `data/catalog/quality_report.json` — counts per source, dedup stats, field coverage.
