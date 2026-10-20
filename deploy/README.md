# Seruen — deployment

Everything for the DigitalOcean Kubernetes cluster **`seruen`** (fra1, Kubernetes 1.35.9-do.1,
4 × s-4vcpu-8gb ≈ 6.26 GiB allocatable each, 25.1 GiB total). Built for the hackathon on 22 Oct 2026,
meant to live 2–3 days, then `scripts/teardown.sh --yes`.

```
deploy/
  platform/<component>/   Helm values (values.yaml) and raw manifests (*.yaml) of the platform
  apps/                   kustomize base of the Seruen application (namespace seruen)
  scripts/
    bootstrap.sh          installs / upgrades the whole platform (idempotent, re-runnable)
    lib.sh                helpers (secret generation, helm wrapper, LB host)
    sync-secrets.sh       creates seruen-db + seruen-secrets in namespace seruen
    render-hosts.sh       writes apps/patches/* from the LB IP
    gen-apps.sh           regenerates apps/services|workers|cronjobs/*.yaml
    argocd-repo.sh        registers the GitHub repo in Argo CD + applies Application seruen-apps
    teardown.sh           deletes cluster + its volumes/LB (dry run unless --yes)
  secrets/                generated passwords, certs, tokens (gitignored, chmod 600)
```

## URLs

Load balancer IP **129.212.254.180** (DigitalOcean LB `seruen-lb`, created by ingress-nginx).
Hostnames use sslip.io, TLS certificates from Let's Encrypt (cert-manager, HTTP-01).

| what | URL | login |
|---|---|---|
| Seruen app (after app deploy) | https://seruen.129-212-254-180.sslip.io (`/partner` → partner-portal) | – |
| Grafana | https://grafana.129-212-254-180.sslip.io | `admin` / `secrets/grafana.env` (k8s secret `observability/grafana-admin`) |
| Argo CD | https://argocd.129-212-254-180.sslip.io | `admin` / `secrets/argocd.env` (k8s secret `argocd/argocd-initial-admin-secret`) |
| Langfuse / Unleash | skipped (see below) | – |

Data stores are **never** exposed outside the cluster (ClusterIP only). For a local look use port-forward, e.g.
`kubectl -n data port-forward svc/opensearch 9200`, `kubectl -n data port-forward svc/minio 9001` (MinIO console),
`kubectl -n platform port-forward svc/vault 8200`.

## What is installed

| # | component | namespace | version | in-cluster address |
|---|---|---|---|---|
| 1 | namespaces `seruen` (Linkerd inject), `data`, `platform`, `observability`, `argocd` | – | – | – |
| – | DOCR integration | all | pull secret `seruen` in every namespace, default SAs patched | registry.digitalocean.com/seruen |
| 2 | ingress-nginx | ingress-nginx | chart 4.15.1 / controller 1.15.1 | LB 129.212.254.180 |
| – | metrics-server (for `kubectl top` + HPA; DOKS has none) | kube-system | chart 3.14.0 / 0.9.0 | – |
| 3 | cert-manager + ClusterIssuers `letsencrypt-prod`, `letsencrypt-staging` | cert-manager | v1.21.2 | – |
| 4 | Strimzi operator + Kafka `seruen` (KRaft, 1 node controller+broker, RF 1, 10Gi) + 10 KafkaTopics | platform / data | Strimzi 1.3.0, Kafka 4.3.1 | `seruen-kafka-bootstrap.data:9092` |
| 5 | CloudNativePG + Cluster `catalog` (PG 17 + PostGIS 3.5, `wal_level=logical`, 10Gi) | platform / data | operator 1.30.1, image `ghcr.io/cloudnative-pg/postgis:17-3.5` | `catalog-rw.data:5432`, db `seruen` |
| 6 | OpenSearch single node (security plugin off, k-NN bundled, heap 1g, 15Gi) | data | 3.9.0 | `opensearch.data:9200` |
| 7 | Redis (appendonly, volatile-lru, 1Gi) | data | redis:7.4.11-alpine | `redis.data:6379` |
| 7 | MinIO (10Gi) + Job creating buckets `raw`, `langfuse` | data | Chainguard build `cgr.dev/chainguard/minio@sha256:2915…` | `minio.data:9000` |
| 7 | ClickHouse (10Gi, user `seruen`, db `seruen`) | data | 26.9.22 | `clickhouse.data:8123` (HTTP), `:9000` (native) |
| 8 | OSRM foot, Astana bbox 71.20,50.98 → 71.75,51.30 (Job `osrm-prepare` + Deployment, MLD) | data | osrm-backend v26.10.0 | `osrm.data:5000` |
| 9 | kube-prometheus-stack (Prometheus 2d/10Gi, Alertmanager, Grafana, node-exporter, kube-state-metrics) | observability | chart 92.10.1, Prometheus 3.15, Grafana 13.2 | `kps-prometheus.observability:9090` |
| 9 | Loki single binary (filesystem, 48h, 10Gi) | observability | chart 7.3.0 / Loki 3.6 | `loki.observability:3100` |
| 9 | Tempo single binary (OTLP, 48h, 10Gi, metrics generator → Prometheus) | observability | chart 1.24.4 / Tempo 2.9.0 | `tempo.observability:4317` gRPC, `:4318` HTTP |
| 9 | Grafana Alloy DaemonSet (pod logs → Loki) | observability | chart 1.14.0 / Alloy 1.21.0 | – |
| 10 | Linkerd **edge** (CRDs + control plane, native sidecars, proxy request 20Mi) | linkerd | edge-26.10.1 (charts 2026.10.1) | – |
| 11 | KEDA | platform | 2.21.0 | – |
| 12 | Argo CD (no Dex / notifications, ApplicationSet scaled to 0) + Application `seruen-apps` (manual sync) | argocd | chart 10.10.5 / v3.5.5 | – |
| 13 | Vault **DEV MODE** (in-memory, auto-unsealed, lost on restart) | platform | chart 0.34.1 / Vault 2.0.4 | `vault.platform:8200` |
| 14 | ~~Langfuse~~ **skipped**: installed, but the 4.63 web pod dies with a Node heap OOM (~500 MB) within the ≤1 GiB budget, so it was uninstalled cleanly (release, secrets, cert, its PG/ClickHouse databases, Redis db 3). | – | chart 2.1.6 | – |
| 14 | ~~Unleash~~ **skipped** (not installed at wrap-up). Values ready in `platform/unleash/`. | – | chart 5.6.9 | – |

Both stay available via the explicit step `deploy/scripts/bootstrap.sh optional` (not part of the default run).
`platform/langfuse/values.yaml` already has the next attempt (heap 1024 MB, limit 1.5Gi), but it is untested.
The PG login roles `langfuse`/`unleash` remain declared in `platform/cnpg/catalog.yaml` (harmless, no databases).

Grafana datasources: Prometheus (`uid prometheus`), Loki (`uid loki`), Tempo (`uid tempo`, trace↔logs, service map).
Extra scrape targets: Kafka exporter (consumer lag), MinIO, ingress-nginx, Linkerd proxies in `seruen`
(`platform/linkerd/podmonitor.yaml`), app `/metrics` (`apps/monitoring.yaml`). Dashboards: put JSON files into
`platform/grafana-dashboards/`, list them in its `kustomization.yaml`, `kubectl apply -k deploy/platform/grafana-dashboards`
(folder "Seruen" in Grafana; a placeholder overview dashboard is already there).

### Decisions worth knowing
- **MinIO**: the official `minio/minio` / `quay.io/minio/minio` images no longer exist (MinIO stopped publishing community images in 2025); Chainguard's source build is used, pinned by digest.
- **Linkerd edge**: open-source Linkerd only ships images for edge releases. The brew CLI (`version-2.20`) renders images that do not exist, so the control plane is installed from the `linkerd-edge` Helm charts. Gateway API CRDs come with DOKS (Cilium), so `installGatewayAPI=false`. Trust anchor + issuer (1 year) are in `secrets/linkerd/`. Native sidecars are on, so Jobs/CronJobs in `seruen` complete normally (tested). linkerd-viz is **not** installed; proxy metrics go to our Prometheus instead.
- **ClickHouse 26.9** instead of 25.8 LTS: Langfuse 4.x migrations use the new full-text index syntax.
- **ingress-nginx** was retired upstream in March 2026 (no more fixes). It works fine for a 3-day demo; for anything longer move to Gateway API.
- Kafka `auto.create.topics.enable=true` (forgiving for the hackathon). Declare real topics in `platform/strimzi/topics.yaml` anyway. Retention 7 days everywhere (CONTRACTS gives none).
- Redis has no password and Kafka uses the plain listener. Both are ClusterIP-only. No NetworkPolicies yet.
- Vault runs in dev mode: demo only, never real secrets.

## Memory (requests)

Measured after the full platform install (`kubectl get pods -A`, requests incl. native sidecars):

| namespace | requests | main consumers |
|---|---|---|
| data | 6016 Mi | OpenSearch 2Gi, Kafka 1.5Gi, ClickHouse 1Gi, Postgres 512Mi, OSRM 256Mi, MinIO 256Mi, Redis 128Mi, Strimzi entity operator + exporter |
| observability | 2528 Mi | Prometheus 1Gi, Loki 384Mi, Grafana 256Mi, Tempo 256Mi, Alloy 4×64Mi, node-exporter, KSM, operator, Alertmanager |
| kube-system | 2052 Mi | DOKS itself (Cilium, CoreDNS, CSI, agents) + metrics-server |
| argocd | 640 Mi | controller 256, repo-server 192, server 128, redis 64 |
| platform | 608 Mi | Strimzi operator 256, CNPG 96, KEDA 160, Vault dev 96 |
| linkerd | 220 Mi | destination, identity, proxy-injector (+ their proxies) |
| cert-manager / ingress-nginx | 160 / 128 Mi | |
| **platform total** | **12 352 Mi ≈ 12.1 GiB** of 25.1 GiB allocatable (48 %) | |
| seruen (apps, deployed by the app team at wrap-up) | 3 552 Mi | → cluster total 15.5 GiB (62 %), 9.5 GiB free |

`kubectl top nodes` at wrap-up: 84 % / 52 % / 65 % / 58 % actual memory use. OpenSearch (≈1.6 GiB used) sits on
`main-3b78oj`, which is the fullest node.

Budget for the apps (`deploy/apps`): 16 online pods (orchestrator and retrieval × 2) + 8 workers at 128Mi + 20Mi Linkerd proxy ≈ **3.5 GiB**;
autoscaling to max (HPA 2→4 ×2, KEDA 1→3 ×2) adds ≈ 1.2 GiB; a collector CronJob run adds ≈ 0.3 GiB while it runs.

## Secrets and passwords

Nothing secret is committed. Generated values live in `deploy/secrets/` (gitignored, 600) **and** in Kubernetes secrets:

| file in deploy/secrets | k8s secret(s) | content |
|---|---|---|
| `grafana.env` | `observability/grafana-admin` | Grafana admin |
| `argocd.env` | `argocd/argocd-initial-admin-secret` | Argo CD admin |
| `clickhouse.env` | `data/clickhouse-auth`, copied to `seruen/seruen-secrets` | ClickHouse user `seruen` |
| `minio.env` | `data/minio-auth`, copied to `seruen/seruen-secrets`, `platform/langfuse` | MinIO root |
| – (CNPG-generated) | `data/catalog-app`, copied to `seruen/seruen-db` | PG owner `seruen` of db `seruen` |
| `langfuse.env` | `data/pg-langfuse` | Langfuse (skipped): salt/keys/admin/PG role password |
| `unleash.env` | `data/pg-unleash` | Unleash (skipped): admin / PG role password |
| `vault.env`, `vault-values.yaml` | (helm values) | Vault dev root token |
| `linkerd/` | `linkerd/linkerd-identity-issuer` | trust anchor + issuer cert/key |
| `lb-ip.txt`, `cluster-id` | – | LB IP, cluster id (for teardown) |
| – | `argocd/repo-seruen` | GitHub token from `gh auth token` (piped, never printed) |

`OPENAI_API_KEY` is read from `seruen/.env` by `scripts/sync-secrets.sh` and stored only in `seruen/seruen-secrets`.

## Re-running the bootstrap

```bash
export DIGITALOCEAN_ACCESS_TOKEN=$(cat ~/.do_token)       # lib.sh does this automatically
doctl kubernetes cluster kubeconfig save seruen
deploy/scripts/bootstrap.sh --list                         # steps
deploy/scripts/bootstrap.sh                                # everything (≈10–15 min on a fresh cluster)
deploy/scripts/bootstrap.sh observability argocd           # just some steps
FORCE_OSRM=1    deploy/scripts/bootstrap.sh osrm           # rebuild the OSRM graph from fresh Geofabrik data
FORCE_LINKERD=1 deploy/scripts/bootstrap.sh linkerd        # upgrade Linkerd (re-generates its webhook certs)
```
Steps: `namespaces registry ingress metrics_server cert_manager kafka postgres opensearch stores osrm observability linkerd keda argocd vault` (+ `optional`, explicit only).
Passwords are generated only once; deleting a file in `deploy/secrets/` makes the next run generate a new value
(not possible for the CNPG/Langfuse DB passwords without also updating the database role, which CNPG does on its own).

## Deploying the application

The app is one image, role chosen by `SERVICE` (see `docs/CONTRACTS.md`, `seruen/run.py`).
`apps/configmap.yaml` carries exactly the names from `seruen/common/config.py`
(`SERUEN_MODE=cluster`, `SERVICE_DOMAIN=seruen.svc.cluster.local`, `KAFKA_BOOTSTRAP`, `PG_HOST`, `PG_DB`, `REDIS_URL`,
`OPENSEARCH_URL`, `CLICKHOUSE_URL`, `MINIO_ENDPOINT`, `OSRM_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OPENAI_MODEL`,
`OPENAI_EMBED_MODEL`, `EMBED_DIM`); `seruen-db` gives `PG_USER`/`PG_PASSWORD`; `seruen-secrets` gives `OPENAI_API_KEY`,
`CLICKHOUSE_USER`/`CLICKHOUSE_PASSWORD`, `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`. Workers also get `KAFKA_GROUP_ID=<name>`
(the KEDA ScaledObjects watch groups `normalizer` and `embedder`).

```bash
# 1. build for the nodes' architecture (the Mac is arm64, nodes are amd64) and push
doctl registry login
docker buildx build --platform linux/amd64 -t registry.digitalocean.com/seruen/seruen:latest --push .
# 2. secrets + hostnames (both idempotent)
deploy/scripts/sync-secrets.sh
deploy/scripts/render-hosts.sh            # commit deploy/apps/patches/* afterwards
# 3. deploy: either directly ...
kubectl apply -k deploy/apps
# ... or via Argo CD after pushing deploy/ to github.com/awzwz/seruen (branch main):
#    UI -> seruen-apps -> Sync, or: kubectl -n argocd patch application seruen-apps --type merge \
#      -p '{"operation":{"sync":{"revision":"main"}}}'
# 4. schema + initial data (runs ensure_all + loads data/catalog baked into the image)
kubectl -n seruen create -f deploy/apps/jobs/initial-load.yaml
kubectl -n seruen logs -f -l app.kubernetes.io/name=initial-load
# 5. check
kubectl -n seruen get pods; curl -s https://seruen.129-212-254-180.sslip.io/api/health
```
To roll out a new image with the same `:latest` tag: `kubectl -n seruen rollout restart deploy` (pods use `imagePullPolicy: Always`).
Prefer immutable tags: `cd deploy/apps && kustomize edit set image registry.digitalocean.com/seruen/seruen=registry.digitalocean.com/seruen/seruen:<sha>`.

Validate without deploying: `kubectl apply -k deploy/apps --dry-run=server` (49 objects, passes).

## Teardown

```bash
deploy/scripts/teardown.sh          # dry run: lists cluster, PVC volumes (tag k8s:<cluster-id>), LB seruen-lb
deploy/scripts/teardown.sh --yes    # deletes cluster (--dangerous: with its volumes + LB), then sweeps leftovers
```
The container registry `registry.digitalocean.com/seruen` is kept.
