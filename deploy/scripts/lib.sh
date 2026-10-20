# Shared helpers for the deploy scripts. Source it, do not run it.
# Nothing here prints secret values: secrets go file -> kubectl, output is discarded.

DEPLOY_DIR=${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
PLATFORM_DIR=$DEPLOY_DIR/platform
SECRETS_DIR=$DEPLOY_DIR/secrets
CLUSTER=${CLUSTER:-seruen}

if [[ -z "${DIGITALOCEAN_ACCESS_TOKEN:-}" && -f "$HOME/.do_token" ]]; then
  DIGITALOCEAN_ACCESS_TOKEN=$(cat "$HOME/.do_token")
  export DIGITALOCEAN_ACCESS_TOKEN
fi

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail] %s\033[0m\n' "$*" >&2; exit 1; }

# helm_up <release> <namespace> <chart> <version> <values-file> [extra helm args...]
helm_up() {
  local rel=$1 ns=$2 chart=$3 ver=$4 values=$5; shift 5
  helm upgrade --install "$rel" "$chart" -n "$ns" --create-namespace \
    --version "$ver" -f "$values" --wait --timeout "${HELM_TIMEOUT:-15m}" "$@" >/dev/null
  info "helm release $ns/$rel ($chart $ver) deployed"
}

rand() { openssl rand -hex "${1:-20}"; }

# ensure_env_file <file> KEY=value|@rand|@hex64 ...  -> creates the file once (chmod 600), never overwrites.
ensure_env_file() {
  local f=$1; shift
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
  [[ -f "$f" ]] && return 0
  umask 077
  : > "$f"
  local kv k v
  for kv in "$@"; do
    k=${kv%%=*}; v=${kv#*=}
    [[ "$v" == "@rand" ]] && v=$(rand 20)
    [[ "$v" == "@hex64" ]] && v=$(rand 32)
    printf '%s=%s\n' "$k" "$v" >> "$f"
  done
  chmod 600 "$f"
  info "generated $(basename "$f") (kept in deploy/secrets/, gitignored)"
}

# env_get <file> <KEY>  -> value (for piping into other files only, never echo it to the terminal)
env_get() { grep -E "^$2=" "$1" | head -1 | cut -d= -f2-; }

# secret_from_env_file <namespace> <name> <file>
secret_from_env_file() {
  kubectl -n "$1" create secret generic "$2" --from-env-file="$3" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  info "secret $1/$2 applied"
}

lb_ip() {
  kubectl -n ingress-nginx get svc ingress-nginx-controller \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null
}

# DOMAIN = <ip-with-dashes>.sslip.io
domain() {
  local ip; ip=$(lb_ip)
  [[ -n "$ip" ]] || die "LoadBalancer IP not assigned yet (run step ingress first)"
  echo "${ip//./-}.sslip.io"
}

render() { sed "s/__DOMAIN__/$(domain)/g" "$1"; }

# wait_pods <namespace> <label-selector> [timeout]
wait_pods() {
  local ns=$1 sel=$2 t=${3:-600s} i
  for i in $(seq 1 60); do
    [[ -n "$(kubectl -n "$ns" get pods -l "$sel" -o name 2>/dev/null)" ]] && break
    sleep 5
  done
  kubectl -n "$ns" wait --for=condition=Ready pod -l "$sel" --timeout="$t" >/dev/null
  info "pods ready: $ns $sel"
}

has_crd() { kubectl get crd "$1" >/dev/null 2>&1; }
