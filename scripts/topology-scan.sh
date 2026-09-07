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
#
# Usage: ./topology-scan.sh <root-dir> [output-file]
#   root-dir  directory containing the service repos (as siblings)

set -euo pipefail

ROOT="${1:-.}"
OUT="${2:-/dev/stdout}"

if command -v rg >/dev/null 2>&1; then
  SEARCH() { rg --no-heading --line-number --color never -i "$@" 2>/dev/null || true; }
else
  echo "note: ripgrep (rg) not found, falling back to grep -r (slower)" >&2
  SEARCH() {
    local pat="$1"; shift
    grep -rniE --line-number "$pat" "$@" 2>/dev/null || true
  }
fi

# Directories that are never interesting and are always enormous.
EXCLUDES=(
  --glob '!**/node_modules/**' --glob '!**/.git/**'    --glob '!**/target/**'
  --glob '!**/build/**'        --glob '!**/dist/**'    --glob '!**/vendor/**'
  --glob '!**/.venv/**'        --glob '!**/__pycache__/**'
  --glob '!**/*.min.js'        --glob '!**/*.lock'     --glob '!**/*.sum'
)
if ! command -v rg >/dev/null 2>&1; then EXCLUDES=(); fi

section() { echo; echo "=== $1 ==="; echo "# $2"; }

{
echo "# topology-scan  root=$ROOT  generated=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "# Raw candidates only. Direction, aliasing and validity are Claude's job."

section "SERVICE_URL_ENV_VARS" \
  "A holding B_URL implies A -> B. The single most reliable sync-edge signal."
SEARCH '[A-Z][A-Z0-9_]*_(URL|URI|HOST|ENDPOINT|BASE_URL|BASE_URI|ADDR|ADDRESS)\b' \
  "${EXCLUDES[@]}" "$ROOT" | head -400

section "HTTP_CLIENT_CONSTRUCTION" \
  "Where outbound calls are actually made."
SEARCH '(WebClient|RestTemplate|HttpClient|FeignClient|OkHttpClient|axios\.create|fetch\(|requests\.(get|post|put|delete)|http\.(Get|Post|NewRequest)|urllib|HttpURLConnection)' \
  "${EXCLUDES[@]}" "$ROOT" | head -400

section "SERVICE_ANNOTATIONS" \
  "Declarative client bindings name the target service directly."
SEARCH '@(FeignClient|RibbonClient|LoadBalanced|GrpcClient|RegisterRestClient)' \
  "${EXCLUDES[@]}" "$ROOT" | head -200

section "MESSAGING_TOPICS" \
  "Async edges. Producer and consumer of the same topic are connected."
SEARCH '(KafkaTemplate|@KafkaListener|@StreamListener|@RabbitListener|SendMessage|sns\.publish|sqs\.|PubSub|EventBridge|kafka\.(producer|consumer)|subscribe\(|publish\()' \
  "${EXCLUDES[@]}" "$ROOT" | head -400

section "TOPIC_QUEUE_NAMES" \
  "String literals that look like topic/queue names."
SEARCH '["'"'"'][a-z0-9]+([._-][a-z0-9]+){1,4}\.(created|updated|deleted|changed|completed|failed|received|submitted|cancelled)["'"'"']' \
  "${EXCLUDES[@]}" "$ROOT" | head -300

section "GRPC_PROTO_SERVICES" \
  "Proto service definitions and their RPCs."
SEARCH '^\s*(service|rpc)\s+[A-Z]' --glob '*.proto' "$ROOT" | head -200

section "CONTAINER_ORCHESTRATION" \
  "Compose/k8s/Helm name services and their wiring explicitly. High-signal."
SEARCH '^\s*(image|container_name|serviceName|host|depends_on|- name):' \
  --glob '*docker-compose*.y*ml' --glob '*.k8s.y*ml' \
  --glob '**/k8s/**/*.y*ml' --glob '**/helm/**/values*.y*ml' \
  --glob '**/charts/**/*.y*ml' --glob '**/manifests/**/*.y*ml' \
  "$ROOT" | head -300

section "GATEWAY_ROUTES" \
  "API gateway / ingress route tables map public paths to services."
SEARCH '(spring\.cloud\.gateway|zuul\.routes|ingress|serviceName|upstream|proxy_pass|routes:)' \
  --glob '*.y*ml' --glob '*.properties' --glob '*.conf' --glob '*.tf' \
  "$ROOT" | head -300

section "DB_CONNECTIONS" \
  "Two services on one schema is a hidden coupling worth flagging."
SEARCH '(jdbc:|mongodb(\+srv)?://|postgres(ql)?://|mysql://|redis://|DATABASE_URL|datasource\.url)' \
  "${EXCLUDES[@]}" "$ROOT" | head -300

section "REPO_INVENTORY" \
  "What repos exist under root, and what each looks like."
find "$ROOT" -maxdepth 2 -name '.git' -type d 2>/dev/null \
  | while read -r g; do
      d="$(dirname "$g")"
      printf '%s\n' "  repo: $d"
      for f in pom.xml build.gradle build.gradle.kts package.json \
               pyproject.toml requirements.txt go.mod Cargo.toml \
               Dockerfile *.csproj; do
        [ -e "$d/$f" ] && printf '%s\n' "        marker: $f"
      done
    done

echo
echo "# end of scan"
} > "$OUT"
