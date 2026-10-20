#!/usr/bin/env bash
# Tears down the DOKS cluster "seruen" and the DigitalOcean resources it created:
#   - block-storage volumes of the PVCs (pvc-*, tagged k8s:<cluster-id>)
#   - the ingress load balancer (name seruen-lb / tagged k8s:<cluster-id>)
# Default = DRY RUN: only lists what would be deleted. Pass --yes to delete.
# Never touches the container registry registry.digitalocean.com/seruen or anything not linked to this cluster.
#   deploy/scripts/teardown.sh          # list
#   deploy/scripts/teardown.sh --yes    # delete
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
LB_NAME=${LB_NAME:-seruen-lb}
YES=0
case "${1:-}" in
  --yes) YES=1 ;;
  ""|--dry-run) ;;
  *) die "usage: $0 [--yes]" ;;
esac
command -v doctl >/dev/null && command -v jq >/dev/null || die "doctl and jq are required"

ID=$(doctl kubernetes cluster get "$CLUSTER" -o json 2>/dev/null | jq -r '.[0].id // empty' || true)
if [[ -n "$ID" ]]; then
  mkdir -p "$SECRETS_DIR"; echo "$ID" > "$SECRETS_DIR/cluster-id"
elif [[ -f "$SECRETS_DIR/cluster-id" ]]; then
  ID=$(cat "$SECRETS_DIR/cluster-id")
  warn "cluster $CLUSTER not found; using saved id $ID to look for leftovers"
else
  die "cluster $CLUSTER not found and no saved id in deploy/secrets/cluster-id"
fi
TAG="k8s:$ID"

leftover_volumes() {
  doctl compute volume list -o json \
    | jq -r --arg t "$TAG" '.[] | select((.tags // []) | index($t)) | select(.name | startswith("pvc-")) | "\(.id) \(.name) \(.size_gigabytes)GiB attached=\(.droplet_ids|length)"'
}
leftover_lbs() {
  doctl compute load-balancer list -o json \
    | jq -r --arg t "$TAG" --arg n "$LB_NAME" '.[] | select(.name == $n or ((.tags // []) | index($t))) | "\(.id) \(.name) \(.ip)"'
}

log "cluster"
doctl kubernetes cluster get "$CLUSTER" --format ID,Name,Region,Status,NodePools 2>/dev/null || info "(already deleted)"
log "volumes tagged $TAG (PVCs)"
leftover_volumes | sed 's/^/    /' || true
log "load balancers ($LB_NAME or tagged $TAG)"
leftover_lbs | sed 's/^/    /' || true
info "the registry registry.digitalocean.com/seruen is NOT deleted"

if [[ $YES -ne 1 ]]; then
  log "dry run only. Re-run with --yes to delete all of the above."
  exit 0
fi

if doctl kubernetes cluster get "$CLUSTER" >/dev/null 2>&1; then
  log "deleting cluster $CLUSTER with its associated volumes and load balancers (--dangerous)"
  doctl kubernetes cluster delete "$CLUSTER" --dangerous --force
fi

log "sweeping leftovers (volumes detach a minute or two after the nodes go away)"
for i in $(seq 1 20); do
  vols=$(leftover_volumes || true); lbs=$(leftover_lbs || true)
  [[ -z "$vols" && -z "$lbs" ]] && break
  while read -r id name size att; do
    [[ -z "$id" ]] && continue
    if [[ "$att" == "attached=0" ]]; then
      doctl compute volume delete "$id" --force && info "deleted volume $name ($size)"
    else
      info "volume $name still attached, waiting"
    fi
  done <<< "$vols"
  while read -r id name ip; do
    [[ -z "$id" ]] && continue
    doctl compute load-balancer delete "$id" --force && info "deleted load balancer $name ($ip)"
  done <<< "$lbs"
  sleep 20
done

vols=$(leftover_volumes || true); lbs=$(leftover_lbs || true)
if [[ -n "$vols$lbs" ]]; then
  warn "still present, re-run later: deploy/scripts/teardown.sh --yes"
  printf '%s\n%s\n' "$vols" "$lbs"
  exit 1
fi
log "teardown complete (registry kept)"
