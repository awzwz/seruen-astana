#!/usr/bin/env bash
# Seruen platform bootstrap for the DOKS cluster "seruen".
#
#   deploy/scripts/bootstrap.sh              # all steps, in order
#   deploy/scripts/bootstrap.sh kafka osrm   # only the named steps
#   deploy/scripts/bootstrap.sh --list       # list steps
#
# Idempotent: every step uses `helm upgrade --install` / `kubectl apply` and only generates
# passwords when deploy/secrets/<name>.env does not exist yet. Secret values are never printed.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
P=$PLATFORM_DIR

# ---- pinned chart versions (checked 2026-10-20) ---------------------------------------------
V_INGRESS_NGINX=4.15.1          # controller 1.15.1
V_METRICS_SERVER=3.14.0         # 0.9.0
V_CERT_MANAGER=v1.21.2
V_STRIMZI=1.3.0
V_CNPG=0.29.1                   # operator 1.30.1
V_OPENSEARCH=3.9.0              # OpenSearch 3.9.0
V_KPS=92.10.1                   # kube-prometheus-stack
V_LOKI=7.3.0                    # Loki 3.6.x
V_TEMPO=1.24.4                  # Tempo 2.9.0
V_ALLOY=1.14.0                  # Alloy 1.21.0
V_KEDA=2.21.0
V_ARGOCD=10.10.5                # Argo CD 3.5.5
V_VAULT=0.34.1
V_LANGFUSE=2.1.6
V_UNLEASH=5.6.9
V_LINKERD=2026.10.1             # linkerd-edge charts, app edge-26.10.1

STEPS=(namespaces registry ingress metrics_server cert_manager kafka postgres opensearch stores osrm
       observability linkerd keda argocd vault)
# Not in the default run: "optional" (Langfuse + Unleash). Langfuse 4.63 web OOMs below ~1 GiB Node heap,
# which breaks the 1 GiB budget; it was uninstalled on 2026-10-20. Run explicitly: bootstrap.sh optional

repos() {
  helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null 2>&1 || true
  helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null 2>&1 || true
  helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
  helm repo add strimzi https://strimzi.io/charts/ >/dev/null 2>&1 || true
  helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
  helm repo add opensearch https://opensearch-project.github.io/helm-charts/ >/dev/null 2>&1 || true
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
  helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
  helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
  helm repo add langfuse https://langfuse.github.io/langfuse-k8s >/dev/null 2>&1 || true
  helm repo add unleash https://docs.getunleash.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null 2>&1
}

# ---- 1. namespaces ---------------------------------------------------------------------------
step_namespaces() {
  log "1. namespaces"
  kubectl apply -f "$P/namespaces/namespaces.yaml" >/dev/null
  info "namespaces: seruen (linkerd inject), data, platform, observability, argocd"
}

# ---- registry: let every namespace pull from registry.digitalocean.com/seruen ----------------
step_registry() {
  log "registry integration (registry.digitalocean.com/seruen)"
  doctl kubernetes cluster registry add "$CLUSTER" >/dev/null
  info "DOCR integration enabled (DOKS creates pull secret 'seruen' in every namespace + default SA)"
}

# ---- 2. ingress-nginx + DO load balancer -----------------------------------------------------
step_ingress() {
  log "2. ingress-nginx"
  local sm=false
  has_crd servicemonitors.monitoring.coreos.com && sm=true
  helm_up ingress-nginx ingress-nginx ingress-nginx/ingress-nginx "$V_INGRESS_NGINX" \
    "$P/ingress-nginx/values.yaml" --set controller.metrics.serviceMonitor.enabled=$sm
  local i
  for i in $(seq 1 60); do [[ -n "$(lb_ip)" ]] && break; sleep 10; done
  [[ -n "$(lb_ip)" ]] || die "DO load balancer has no IP after 10 minutes"
  info "LB IP $(lb_ip) -> hosts *.$(domain)"
  lb_ip > "$SECRETS_DIR/lb-ip.txt"
}

step_metrics_server() {
  log "metrics-server (kubectl top, HPA)"
  helm_up metrics-server kube-system metrics-server/metrics-server "$V_METRICS_SERVER" \
    "$P/metrics-server/values.yaml"
}

# ---- 3. cert-manager + Let's Encrypt ---------------------------------------------------------
step_cert_manager() {
  log "3. cert-manager"
  helm_up cert-manager cert-manager jetstack/cert-manager "$V_CERT_MANAGER" "$P/cert-manager/values.yaml"
  kubectl apply -f "$P/cert-manager/clusterissuers.yaml" >/dev/null
  render "$P/cert-manager/certificates.tmpl.yaml" | kubectl apply -f - >/dev/null
  info "waiting for test certificate grafana-tls (HTTP-01)..."
  if kubectl -n observability wait --for=condition=Ready certificate/grafana-tls --timeout=300s >/dev/null; then
    info "certificate grafana-tls issued by letsencrypt-prod"
  else
    warn "grafana-tls not ready yet: kubectl -n observability describe certificate grafana-tls"
  fi
}

# ---- 4. Strimzi + Kafka (KRaft, 1 node) ------------------------------------------------------
step_kafka() {
  log "4. Strimzi operator + Kafka 'seruen'"
  helm_up strimzi platform strimzi/strimzi-kafka-operator "$V_STRIMZI" "$P/strimzi/values.yaml"
  kubectl apply -f "$P/strimzi/kafka.yaml" >/dev/null
  info "waiting for Kafka seruen to become Ready (first start takes 2-4 min)..."
  kubectl -n data wait kafka/seruen --for=condition=Ready --timeout=900s >/dev/null
  kubectl apply -f "$P/strimzi/topics.yaml" >/dev/null
  kubectl -n data wait kafkatopic --all --for=condition=Ready --timeout=300s >/dev/null
  info "Kafka ready at seruen-kafka-bootstrap.data:9092, $(kubectl -n data get kafkatopic -o name | wc -l | tr -d ' ') topics"
}

# ---- 5. CloudNativePG + PostGIS cluster "catalog" ---------------------------------------------
step_postgres() {
  log "5. CloudNativePG operator + Cluster 'catalog' (PostGIS)"
  helm_up cnpg platform cnpg/cloudnative-pg "$V_CNPG" "$P/cnpg/values.yaml"
  kubectl apply -f "$P/cnpg/catalog.yaml" >/dev/null
  info "waiting for cluster catalog (initdb + PostGIS, 2-4 min)..."
  kubectl -n data wait cluster.postgresql.cnpg.io/catalog --for=condition=Ready --timeout=900s >/dev/null
  info "PostgreSQL ready at catalog-rw.data:5432, db seruen, credentials in secret data/catalog-app"
}

# ---- 6. OpenSearch single node ---------------------------------------------------------------
step_opensearch() {
  log "6. OpenSearch (single node, security plugin disabled, internal only)"
  helm_up opensearch data opensearch/opensearch "$V_OPENSEARCH" "$P/opensearch/values.yaml"
  wait_pods data app.kubernetes.io/name=opensearch
  info "OpenSearch ready at opensearch.data:9200"
}

# ---- 7. Redis, MinIO, ClickHouse -------------------------------------------------------------
step_stores() {
  log "7. Redis, MinIO, ClickHouse"
  kubectl apply -f "$P/redis/redis.yaml" >/dev/null

  ensure_env_file "$SECRETS_DIR/minio.env" MINIO_ROOT_USER=seruen-admin MINIO_ROOT_PASSWORD=@rand
  secret_from_env_file data minio-auth "$SECRETS_DIR/minio.env"
  kubectl apply -f "$P/minio/minio.yaml" >/dev/null

  ensure_env_file "$SECRETS_DIR/clickhouse.env" CLICKHOUSE_USER=seruen CLICKHOUSE_PASSWORD=@rand CLICKHOUSE_DB=seruen
  secret_from_env_file data clickhouse-auth "$SECRETS_DIR/clickhouse.env"
  kubectl apply -f "$P/clickhouse/clickhouse.yaml" >/dev/null

  wait_pods data app.kubernetes.io/name=redis
  wait_pods data app.kubernetes.io/name=minio
  kubectl -n data delete job minio-create-buckets --ignore-not-found >/dev/null
  kubectl apply -f "$P/minio/buckets-job.yaml" >/dev/null
  kubectl -n data wait job/minio-create-buckets --for=condition=Complete --timeout=300s >/dev/null
  info "MinIO buckets: raw, langfuse"
  wait_pods data app.kubernetes.io/name=clickhouse
  info "redis.data:6379  minio.data:9000  clickhouse.data:8123 ready"
}

# ---- 8. OSRM (foot, Astana) ------------------------------------------------------------------
step_osrm() {
  log "8. OSRM foot routing (Astana extract)"
  kubectl apply -f "$P/osrm/osrm.yaml" >/dev/null
  local done_
  done_=$(kubectl -n data get job osrm-prepare -o jsonpath='{.status.succeeded}' 2>/dev/null || true)
  if [[ "$done_" != "1" || "${FORCE_OSRM:-0}" == "1" ]]; then
    kubectl -n data scale deploy/osrm --replicas=0 >/dev/null   # release the RWO volume
    kubectl -n data delete job osrm-prepare --ignore-not-found --wait >/dev/null
    kubectl apply -f "$P/osrm/osrm-prepare.yaml" >/dev/null
    info "osrm-prepare: download Kazakhstan (~225 MB), clip, extract, partition, customize (3-8 min)..."
    kubectl -n data wait job/osrm-prepare --for=condition=Complete --timeout=1500s >/dev/null \
      || die "osrm-prepare failed: kubectl -n data logs job/osrm-prepare --all-containers"
  else
    info "osrm-prepare already completed (FORCE_OSRM=1 to rebuild)"
  fi
  kubectl -n data scale deploy/osrm --replicas=1 >/dev/null
  kubectl -n data rollout status deploy/osrm --timeout=300s >/dev/null
  # Khan Shatyr -> Baiterek
  local code
  code=$(kubectl -n data run osrm-check --rm -i --restart=Never --quiet --image=curlimages/curl:8.17.0 -- \
    -s "http://osrm.data:5000/route/v1/foot/71.4039,51.1324;71.4305,51.1283?overview=false" 2>/dev/null \
    | grep -o '"code":"[A-Za-z]*"' | head -1 || true)
  info "OSRM route Khan Shatyr -> Baiterek: ${code:-no answer}"
}

# ---- 9. Observability: kube-prometheus-stack, Loki, Tempo, Alloy -----------------------------
step_observability() {
  log "9. observability (Prometheus, Grafana, Loki, Tempo, Alloy)"
  local d; d=$(domain)
  ensure_env_file "$SECRETS_DIR/grafana.env" admin-user=admin admin-password=@rand
  secret_from_env_file observability grafana-admin "$SECRETS_DIR/grafana.env"
  render "$P/cert-manager/certificates.tmpl.yaml" | kubectl apply -f - >/dev/null
  helm_up kps observability prometheus-community/kube-prometheus-stack "$V_KPS" \
    "$P/kube-prometheus-stack/values.yaml" \
    --set "grafana.ingress.hosts[0]=grafana.$d" \
    --set "grafana.ingress.tls[0].secretName=grafana-tls" \
    --set "grafana.ingress.tls[0].hosts[0]=grafana.$d" \
    --set "grafana.grafana\.ini.server.root_url=https://grafana.$d"
  helm_up loki observability grafana/loki "$V_LOKI" "$P/loki/values.yaml"
  helm_up tempo observability grafana/tempo "$V_TEMPO" "$P/tempo/values.yaml"
  helm_up alloy observability grafana/alloy "$V_ALLOY" "$P/alloy/values.yaml"
  kubectl apply -f "$P/kube-prometheus-stack/monitors.yaml" >/dev/null
  kubectl apply -k "$P/grafana-dashboards" >/dev/null
  # now that the ServiceMonitor CRD exists, let ingress-nginx publish its ServiceMonitor
  helm_up ingress-nginx ingress-nginx ingress-nginx/ingress-nginx "$V_INGRESS_NGINX" \
    "$P/ingress-nginx/values.yaml" --set controller.metrics.serviceMonitor.enabled=true
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://grafana.$d/api/health" || true)
  info "Grafana https://grafana.$d -> HTTP $code (user/password: secret observability/grafana-admin, deploy/secrets/grafana.env)"
}

# ---- 10. Linkerd (edge) ----------------------------------------------------------------------
linkerd_certs() {
  local L=$SECRETS_DIR/linkerd
  [[ -f "$L/issuer.key" ]] && return 0
  command -v step >/dev/null || die "step CLI missing: brew install step"
  mkdir -p "$L"; chmod 700 "$L"
  step certificate create root.linkerd.cluster.local "$L/ca.crt" "$L/ca.key" \
    --profile root-ca --no-password --insecure --not-after=8760h >/dev/null 2>&1
  step certificate create identity.linkerd.cluster.local "$L/issuer.crt" "$L/issuer.key" \
    --profile intermediate-ca --not-after=8760h --no-password --insecure \
    --ca "$L/ca.crt" --ca-key "$L/ca.key" >/dev/null 2>&1
  chmod 600 "$L"/*
  info "generated Linkerd trust anchor + issuer in deploy/secrets/linkerd/ (1 year)"
}

step_linkerd() {
  log "10. Linkerd edge (CRDs + control plane)"
  helm repo add linkerd-edge https://helm.linkerd.io/edge >/dev/null 2>&1 || true
  helm repo update linkerd-edge >/dev/null 2>&1
  linkerd_certs
  local L=$SECRETS_DIR/linkerd
  helm_up linkerd-crds linkerd linkerd-edge/linkerd-crds "$V_LINKERD" "$P/linkerd/crds-values.yaml"
  # The chart regenerates its webhook certificates on every upgrade; only upgrade on request.
  if helm -n linkerd status linkerd-control-plane >/dev/null 2>&1 && [[ "${FORCE_LINKERD:-0}" != "1" ]]; then
    info "linkerd-control-plane already installed (FORCE_LINKERD=1 to upgrade)"
  else
    helm_up linkerd-control-plane linkerd linkerd-edge/linkerd-control-plane "$V_LINKERD" "$P/linkerd/values.yaml" \
      --set-file identityTrustAnchorsPEM="$L/ca.crt" \
      --set-file identity.issuer.tls.crtPEM="$L/issuer.crt" \
      --set-file identity.issuer.tls.keyPEM="$L/issuer.key"
  fi
  kubectl -n linkerd rollout status deploy/linkerd-destination --timeout=300s >/dev/null
  kubectl -n linkerd rollout status deploy/linkerd-identity --timeout=300s >/dev/null
  kubectl -n linkerd rollout status deploy/linkerd-proxy-injector --timeout=300s >/dev/null
  kubectl annotate ns seruen linkerd.io/inject=enabled --overwrite >/dev/null
  info "Linkerd ready; namespace seruen has linkerd.io/inject=enabled"
}

# ---- 11. KEDA --------------------------------------------------------------------------------
step_keda() {
  log "11. KEDA"
  helm_up keda platform kedacore/keda "$V_KEDA" "$P/keda/values.yaml"
}

# ---- 12. Argo CD -----------------------------------------------------------------------------
step_argocd() {
  log "12. Argo CD (slim)"
  local d; d=$(domain)
  render "$P/cert-manager/certificates.tmpl.yaml" | kubectl apply -f - >/dev/null
  helm_up argocd argocd argo/argo-cd "$V_ARGOCD" "$P/argocd/values.yaml" \
    --set "server.ingress.hostname=argocd.$d" \
    --set "server.ingress.extraTls[0].hosts[0]=argocd.$d" \
    --set "server.ingress.extraTls[0].secretName=argocd-tls" \
    --set "configs.cm.url=https://argocd.$d"
  if [[ ! -f "$SECRETS_DIR/argocd.env" ]] && kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then
    umask 077
    { printf 'ARGOCD_USER=admin\nARGOCD_PASSWORD='
      kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
      printf '\n'; } > "$SECRETS_DIR/argocd.env"
    chmod 600 "$SECRETS_DIR/argocd.env"
    info "Argo CD admin password saved to deploy/secrets/argocd.env"
  fi
  "$SCRIPT_DIR/argocd-repo.sh"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://argocd.$d/healthz" || true)
  info "Argo CD https://argocd.$d -> HTTP $code"
}

# ---- 13. Vault (DEV MODE) --------------------------------------------------------------------
step_vault() {
  log "13. Vault in DEV MODE (in-memory, demo only)"
  ensure_env_file "$SECRETS_DIR/vault.env" VAULT_DEV_ROOT_TOKEN=@rand
  umask 077
  printf 'server:\n  dev:\n    devRootToken: "%s"\n' "$(env_get "$SECRETS_DIR/vault.env" VAULT_DEV_ROOT_TOKEN)" \
    > "$SECRETS_DIR/vault-values.yaml"
  chmod 600 "$SECRETS_DIR/vault-values.yaml"
  helm_up vault platform hashicorp/vault "$V_VAULT" "$P/vault/values.yaml" -f "$SECRETS_DIR/vault-values.yaml"
  wait_pods platform app.kubernetes.io/name=vault
  info "Vault (DEV MODE) at vault.platform:8200, root token in deploy/secrets/vault.env"
}

# ---- 14. optional: Langfuse + Unleash on our own Postgres/ClickHouse/Redis/MinIO --------------
# basic_auth_secret <ns> <name> <username> <password-file-key> <env-file>
basic_auth_secret() {
  kubectl -n "$1" create secret generic "$2" --type=kubernetes.io/basic-auth \
    --from-literal=username="$3" --from-file=password=<(printf '%s' "$(env_get "$5" "$4")") \
    --dry-run=client -o yaml \
    | kubectl label --local -f - cnpg.io/reload=true -o yaml | kubectl apply -f - >/dev/null
  info "secret $1/$2 applied"
}

step_optional() {
  log "14. optional: Langfuse + Unleash"
  local d; d=$(domain)
  local LF=$SECRETS_DIR/langfuse.env UL=$SECRETS_DIR/unleash.env
  ensure_env_file "$LF" PG_PASSWORD=@rand SALT=@rand ENCRYPTION_KEY=@hex64 NEXTAUTH_SECRET=@rand \
    INIT_USER_EMAIL=admin@seruen.local INIT_USER_PASSWORD=@rand \
    PUBLIC_KEY="pk-lf-$(rand 16)" SECRET_KEY="sk-lf-$(rand 16)"
  ensure_env_file "$UL" PG_PASSWORD=@rand ADMIN_PASSWORD=@rand

  # Postgres roles (declared in catalog.yaml) + databases
  basic_auth_secret data pg-langfuse langfuse PG_PASSWORD "$LF"
  basic_auth_secret data pg-unleash unleash PG_PASSWORD "$UL"
  kubectl apply -f "$P/cnpg/catalog.yaml" -f "$P/cnpg/optional-dbs.yaml" >/dev/null
  kubectl -n data wait database/langfuse database/unleash --for=jsonpath='{.status.applied}'=true --timeout=180s >/dev/null
  info "PostgreSQL databases langfuse, unleash ready"
  # ClickHouse database for Langfuse (credentials are read inside the pod from its own env)
  kubectl -n data exec clickhouse-0 -- sh -c \
    'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "CREATE DATABASE IF NOT EXISTS langfuse"'
  info "ClickHouse database langfuse ready"

  # platform/langfuse secret
  local T; T=$(mktemp -d); chmod 700 "$T"
  ( umask 077
    { printf 'salt=%s\n' "$(env_get "$LF" SALT)"
      printf 'encryption-key=%s\n' "$(env_get "$LF" ENCRYPTION_KEY)"
      printf 'nextauth-secret=%s\n' "$(env_get "$LF" NEXTAUTH_SECRET)"
      printf 'pg-password=%s\n' "$(env_get "$LF" PG_PASSWORD)"
      printf 'clickhouse-password=%s\n' "$(env_get "$SECRETS_DIR/clickhouse.env" CLICKHOUSE_PASSWORD)"
      printf 's3-access-key=%s\n' "$(env_get "$SECRETS_DIR/minio.env" MINIO_ROOT_USER)"
      printf 's3-secret-key=%s\n' "$(env_get "$SECRETS_DIR/minio.env" MINIO_ROOT_PASSWORD)"
      printf 'init-user-email=%s\n' "$(env_get "$LF" INIT_USER_EMAIL)"
      printf 'init-user-password=%s\n' "$(env_get "$LF" INIT_USER_PASSWORD)"
      printf 'public-key=%s\n' "$(env_get "$LF" PUBLIC_KEY)"
      printf 'secret-key=%s\n' "$(env_get "$LF" SECRET_KEY)"
    } > "$T/langfuse.env"
    { printf 'db-password=%s\n' "$(env_get "$UL" PG_PASSWORD)"
      printf 'admin-password=%s\n' "$(env_get "$UL" ADMIN_PASSWORD)"
    } > "$T/unleash.env" )
  secret_from_env_file platform langfuse "$T/langfuse.env"
  secret_from_env_file platform unleash "$T/unleash.env"
  rm -rf "$T"

  helm_up langfuse platform langfuse/langfuse "$V_LANGFUSE" "$P/langfuse/values.yaml" \
    --set "langfuse.nextauth.url=https://langfuse.$d" \
    --set "langfuse.ingress.hosts[0].host=langfuse.$d" \
    --set "langfuse.ingress.hosts[0].paths[0].path=/" \
    --set "langfuse.ingress.hosts[0].paths[0].pathType=Prefix"
  helm_up unleash platform unleash/unleash "$V_UNLEASH" "$P/unleash/values.yaml" \
    --set "ingress.hosts[0].host=unleash.$d" \
    --set "ingress.hosts[0].paths[0].path=/" \
    --set "ingress.hosts[0].paths[0].pathType=Prefix" \
    --set "ingress.tls[0].secretName=unleash-tls" \
    --set "ingress.tls[0].hosts[0]=unleash.$d"
  info "Langfuse https://langfuse.$d (admin: deploy/secrets/langfuse.env), Unleash https://unleash.$d (admin / deploy/secrets/unleash.env)"
}

# ---- main ------------------------------------------------------------------------------------
main() {
  if [[ "${1:-}" == "--list" ]]; then printf '%s\n' "${STEPS[@]}" "optional (explicit only)"; exit 0; fi
  command -v kubectl >/dev/null && command -v helm >/dev/null || die "kubectl and helm are required"
  kubectl config current-context | grep -q "seruen" || die "kubectl context is not the seruen cluster"
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
  repos
  local steps=("$@"); [[ ${#steps[@]} -eq 0 ]] && steps=("${STEPS[@]}")
  local s
  for s in "${steps[@]}"; do
    declare -F "step_$s" >/dev/null || die "unknown step: $s (see --list)"
    "step_$s"
  done
  log "done: ${steps[*]}"
}
main "$@"
