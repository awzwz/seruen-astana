#!/usr/bin/env bash
# Registers the GitHub repo awzwz/seruen-astana in Argo CD (a token is only needed for a private repo) and applies the Application seruen-apps.
# The GitHub token comes from `gh auth token` and is piped straight into kubectl (never printed).
# Does nothing (exit 0) when the repo does not exist yet.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
REPO=${REPO:-awzwz/seruen-astana}

if ! gh repo view "$REPO" >/dev/null 2>&1; then
  warn "GitHub repo $REPO not found: Application seruen-apps NOT applied (file: deploy/platform/argocd/application.yaml)."
  warn "Create/push the repo, then re-run: deploy/scripts/argocd-repo.sh"
  exit 0
fi

kubectl -n argocd create secret generic repo-seruen \
  --from-literal=type=git \
  --from-literal=url="https://github.com/$REPO" \
  --from-literal=username="${REPO%%/*}" \
  --from-file=password=<(printf '%s' "$(gh auth token)") \
  --dry-run=client -o yaml \
  | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml \
  | kubectl apply -f - >/dev/null
info "Argo CD repository secret argocd/repo-seruen applied"
kubectl apply -f "$PLATFORM_DIR/argocd/application.yaml" >/dev/null
info "Application argocd/seruen-apps applied (automated sync disabled)"
