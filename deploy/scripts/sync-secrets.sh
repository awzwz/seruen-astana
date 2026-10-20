#!/usr/bin/env bash
# Creates/updates the application secrets in namespace seruen. Never prints values.
#   seruen-db       PG_USER, PG_PASSWORD, DATABASE_URL   <- copied from data/catalog-app (CloudNativePG)
#                   (Kubernetes cannot reference a secret in another namespace, hence the copy)
#   seruen-secrets  OPENAI_API_KEY                        <- seruen/.env (ENV_FILE to override)
#                   CLICKHOUSE_USER, CLICKHOUSE_PASSWORD  <- deploy/secrets/clickhouse.env
#                   MINIO_ACCESS_KEY, MINIO_SECRET_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY <- deploy/secrets/minio.env
# Re-run after rotating any of them (pods pick up new values on restart:
#   kubectl -n seruen rollout restart deploy).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
ENV_FILE=${ENV_FILE:-$DEPLOY_DIR/../.env}
NS=${NS:-seruen}

TMP=$(mktemp -d); chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
umask 077

dotenv_get() { # strips optional surrounding quotes
  grep -E "^$2=" "$1" | tail -1 | cut -d= -f2- | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}
b64d() { base64 -d 2>/dev/null || base64 -D; }

# ---- seruen-db --------------------------------------------------------------------------------
kubectl -n data get secret catalog-app >/dev/null 2>&1 || die "secret data/catalog-app not found (bootstrap step postgres)"
PGU=$(kubectl -n data get secret catalog-app -o jsonpath='{.data.username}' | b64d)
PGP=$(kubectl -n data get secret catalog-app -o jsonpath='{.data.password}' | b64d)
PGP_URI=$(jq -rn --arg p "$PGP" '$p|@uri')
{
  printf 'PG_USER=%s\n' "$PGU"
  printf 'PG_PASSWORD=%s\n' "$PGP"
  printf 'DATABASE_URL=postgresql://%s:%s@catalog-rw.data.svc.cluster.local:5432/seruen\n' "$PGU" "$PGP_URI"
} > "$TMP/db.env"
unset PGP PGP_URI
secret_from_env_file "$NS" seruen-db "$TMP/db.env"

# ---- seruen-secrets ---------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found"
KEY=$(dotenv_get "$ENV_FILE" OPENAI_API_KEY || true)
[[ -n "$KEY" ]] || warn "OPENAI_API_KEY is empty in $ENV_FILE (template fallback only)"
{
  printf 'OPENAI_API_KEY=%s\n' "$KEY"
  DG=$(dotenv_get "$ENV_FILE" DGIS_API_KEY || true)
  [[ -n "$DG" ]] && printf 'DGIS_API_KEY=%s\n' "$DG"
  DGS=$(dotenv_get "$ENV_FILE" DGIS_API_KEYS || true)
  [[ -n "$DGS" ]] && printf 'DGIS_API_KEYS=%s\n' "$DGS"
  unset DG DGS
  if [[ -f "$SECRETS_DIR/clickhouse.env" ]]; then
    printf 'CLICKHOUSE_USER=%s\n' "$(env_get "$SECRETS_DIR/clickhouse.env" CLICKHOUSE_USER)"
    printf 'CLICKHOUSE_PASSWORD=%s\n' "$(env_get "$SECRETS_DIR/clickhouse.env" CLICKHOUSE_PASSWORD)"
  fi
  if [[ -f "$SECRETS_DIR/minio.env" ]]; then
    MU=$(env_get "$SECRETS_DIR/minio.env" MINIO_ROOT_USER); MP=$(env_get "$SECRETS_DIR/minio.env" MINIO_ROOT_PASSWORD)
    printf 'MINIO_ACCESS_KEY=%s\nMINIO_SECRET_KEY=%s\nAWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' "$MU" "$MP" "$MU" "$MP"
    unset MU MP
  fi
} > "$TMP/app.env"
unset KEY
secret_from_env_file "$NS" seruen-secrets "$TMP/app.env"
info "keys: seruen-db [$(kubectl -n "$NS" get secret seruen-db -o json | jq -r '.data|keys|join(", ")')]"
info "keys: seruen-secrets [$(kubectl -n "$NS" get secret seruen-secrets -o json | jq -r '.data|keys|join(", ")')]"
