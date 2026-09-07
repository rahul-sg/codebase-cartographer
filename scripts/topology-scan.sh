#!/usr/bin/env bash
# topology-scan.sh — gather raw evidence of service-to-service edges.
#
# This script DECIDES NOTHING. It collects candidate signals with file:line
# citations and hands them to Claude to interpret. Keeping extraction dumb and
# interpretation smart is deliberate: the grep patterns will be wrong for some
# stacks, and a wrong-but-visible candidate is easy to discard, while a clever
# script that silently drops edges is not.
#
# Fully local. No network calls. Nothing leaves the machine.
# Works with ripgrep if present, plain grep otherwise. bash 3.2 compatible.
#
# Usage: ./topology-scan.sh <root-dir> [output-file]
#   root-dir  directory containing the service repos (as siblings)

set -euo pipefail

ROOT="${1:-.}"
OUT="${2:-/dev/stdout}"
CAP="${CARTOGRAPHER_CAP:-400}"   # max hits per section

if [ ! -d "$ROOT" ]; then
  echo "error: '$ROOT' is not a directory" >&2
  exit 1
fi

if command -v rg >/dev/null 2>&1; then RG=1; else RG=0; fi

# Never interesting, always enormous.
EXCL_DIRS="node_modules .git target build dist out vendor .venv venv __pycache__ .gradle .idea .mvn coverage"

# search <pattern> [file-glob ...]
#   With no globs, searches everything minus EXCL_DIRS.
#   With globs, restricts to them (grep fallback uses the basename only).
search() {
  pat="$1"; shift
  if [ "$RG" -eq 1 ]; then
    set -- --no-heading --line-number --color never -i \
           $(for d in $EXCL_DIRS; do printf -- "--glob\n!**/%s/**\n" "$d"; done) \
           --glob '!**/*.min.js' --glob '!**/*.lock' --glob '!**/*.sum' \
           "$@" -e "$pat" "$ROOT"
    rg "$@" 2>/dev/null || true
  else
    globs=""
    for g in "$@"; do globs="$globs --include=$(basename "$g")"; done
    # shellcheck disable=SC2086
    grep -rniE $(for d in $EXCL_DIRS; do printf -- "--exclude-dir=%s " "$d"; done) \
         $globs -e "$pat" "$ROOT" 2>/dev/null || true
  fi
}

section() { echo; echo "=== $1 ==="; echo "# $2"; }

{
echo "# topology-scan  root=$ROOT  generated=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "# searcher=$([ "$RG" -eq 1 ] && echo ripgrep || echo grep)  cap=$CAP hits/section"
echo "# Raw candidates only. Direction, aliasing and validity are Claude's job."

section "SERVICE_URL_ENV_VARS" \
  "A holding B_URL implies A -> B. The single most reliable sync-edge signal."
search '[A-Z][A-Z0-9_]*_(URL|URI|HOST|ENDPOINT|BASE_URL|BASE_URI|ADDR|ADDRESS)' | head -"$CAP"

section "HTTP_CLIENT_CONSTRUCTION" \
  "Where outbound calls are actually made."
search '(WebClient|RestTemplate|HttpClient|FeignClient|OkHttpClient|axios\.create|fetch\(|requests\.(get|post|put|delete)|http\.(Get|Post|NewRequest)|HttpURLConnection)' | head -"$CAP"

section "SERVICE_ANNOTATIONS" \
  "Declarative client bindings name the target service directly."
search '@(FeignClient|RibbonClient|LoadBalanced|GrpcClient|RegisterRestClient)' | head -200

section "MESSAGING" \
  "Async edges. Producer and consumer of the same topic are connected."
search '(KafkaTemplate|@KafkaListener|@StreamListener|@RabbitListener|sns\.publish|sqs\.|PubSub|EventBridge|kafka\.(producer|consumer))' | head -"$CAP"

section "TOPIC_QUEUE_NAMES" \
  "String literals shaped like topic/queue names."
search '[a-z0-9]+([._-][a-z0-9]+){0,4}\.(created|updated|deleted|changed|completed|failed|received|submitted|cancelled|shipped)' | head -300

section "GRPC_PROTO_SERVICES" \
  "Proto service definitions and their RPCs."
search '^[[:space:]]*(service|rpc)[[:space:]]+[A-Z]' '*.proto' | head -200

section "CONTAINER_ORCHESTRATION" \
  "Compose/k8s/Helm name services and their wiring explicitly. High-signal."
search '^[[:space:]]*(image|container_name|serviceName|depends_on|- name):' \
  '*.yml' '*.yaml' | head -300

section "GATEWAY_ROUTES" \
  "API gateway / ingress route tables map public paths to services."
search '(spring\.cloud\.gateway|zuul\.routes|ingress|upstream|proxy_pass|routes:)' \
  '*.yml' '*.yaml' '*.properties' '*.conf' '*.tf' | head -300

section "DB_CONNECTIONS" \
  "Two services on one schema is a hidden coupling worth flagging."
search '(jdbc:|mongodb(\+srv)?://|postgres(ql)?://|mysql://|redis://|DATABASE_URL|datasource\.url)' | head -300

section "REPO_INVENTORY" \
  "What repos exist under root, and what each is built with."
find "$ROOT" -maxdepth 3 -name '.git' -type d 2>/dev/null \
  | while read -r g; do
      d="$(dirname "$g")"
      echo "  repo: $d"
      for f in pom.xml build.gradle build.gradle.kts settings.gradle package.json \
               pyproject.toml requirements.txt go.mod Cargo.toml Dockerfile; do
        # `|| true` is load-bearing: under `set -e` a false test as the last
        # command in the loop body kills the whole enclosing while-read.
        { [ -e "$d/$f" ] && echo "        marker: $f"; } || true
      done
      # a monorepo of services shows up as nested build files
      find "$d" -mindepth 2 -maxdepth 3 \( -name 'pom.xml' -o -name 'package.json' \) \
        2>/dev/null | head -10 | sed 's|^|        submodule: |' || true
    done

echo
echo "# end of scan"
} > "$OUT"
