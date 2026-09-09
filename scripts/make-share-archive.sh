#!/usr/bin/env bash
#
# Build a shareable archive of this tool.
#
# The working tree accumulates things that must never leave the machine it was
# run on -- above all `.cartographer/graph.db`, which is a complete structural
# map of whatever codebase was last scanned. Exporting by hand is exactly the
# kind of task where one forgotten flag leaks everything, so it is a script
# with an enforced gate rather than a remembered procedure.
#
# The gate is the test suite's own organisation-identifier guard. If that test
# fails, no archive is produced. Refusing to build is the entire point.
#
#   ./scripts/make-share-archive.sh [output-path]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$HOME/Downloads/codebase-cartographer-$(date +%Y%m%d).zip}"
STAGE="$(mktemp -d)"
NAME="codebase-cartographer"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "==> Staging from $ROOT"
mkdir -p "$STAGE/$NAME"

# Exclusions, and why each one matters:
#   .git              history is forever; a single past commit of a graph or a
#                     config would travel with the archive
#   .cartographer     the graph database -- a full map of the scanned codebase
#   cartographer.yaml points at real local paths (the .example is kept)
#   examples/         organisation-specific configuration and walkthroughs
#   __pycache__/*.pyc build artefacts, and they embed absolute source paths
rsync -a \
  --exclude='.git' \
  --exclude='.gitignore' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.cartographer' \
  --exclude='node_modules' \
  --exclude='cartographer.yaml' \
  --exclude='examples/' \
  --exclude='*.zip' \
  "$ROOT/" "$STAGE/$NAME/"

# ---------------------------------------------------------------- gate 1
# The guard runs against the STAGED copy, not the working tree: what ships is
# what must be clean, and the two can differ.
echo "==> Gate 1: organisation-identifier guard (on the staged copy)"
if ! ( cd "$STAGE/$NAME/tests" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
        test_all -k organisation -q >/dev/null 2>&1 ); then
  echo "REFUSING TO BUILD: organisation identifiers found in the staged copy." >&2
  ( cd "$STAGE/$NAME/tests" && python3 -m unittest test_all -k organisation 2>&1 \
      | sed -n '/AssertionError/,/^$/p' ) >&2
  exit 1
fi
echo "    clean"

# ---------------------------------------------------------------- gate 2
echo "==> Gate 2: full test suite (on the staged copy)"
if ! ( cd "$STAGE/$NAME" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
        discover -s tests -q >/dev/null 2>&1 ); then
  echo "REFUSING TO BUILD: tests fail in the staged copy." >&2
  ( cd "$STAGE/$NAME" && python3 -m unittest discover -s tests 2>&1 | tail -30 ) >&2
  exit 1
fi
echo "    passing"

# Running the gates inside the staged copy regenerates bytecode there, after
# rsync already excluded it. PYTHONDONTWRITEBYTECODE covers the normal path;
# this covers anything that slips past it (a subprocess, a different
# interpreter). Order matters: purge BEFORE the sweep, or the sweep fails on
# artefacts the gates themselves created -- which is exactly what happened the
# first time this script ran.
find "$STAGE/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/$NAME" -name '*.pyc' -delete 2>/dev/null || true

# ---------------------------------------------------------------- gate 3
# Belt and braces: prove by inspection that no graph database, no compiled
# artefact and no absolute home path survived staging.
echo "==> Gate 3: artefact sweep"
LEAKS=0
while IFS= read -r f; do
  echo "    UNEXPECTED: $f" >&2; LEAKS=1
done < <(find "$STAGE/$NAME" \( -name '*.db' -o -name '*.pyc' -o -name '*.sqlite*' \
         -o -name 'cartographer.yaml' \) -print)
if grep -rIl "$HOME" "$STAGE/$NAME" 2>/dev/null | head -5 | grep -q .; then
  echo "    UNEXPECTED: absolute home paths remain:" >&2
  grep -rIl "$HOME" "$STAGE/$NAME" 2>/dev/null | sed 's|^|      |' >&2
  LEAKS=1
fi
[ "$LEAKS" -eq 0 ] || { echo "REFUSING TO BUILD." >&2; exit 1; }
echo "    clean"

# A .gitignore travels with the archive, so the first `git init` on the
# receiving machine cannot commit a graph before anyone thinks about it.
cat > "$STAGE/$NAME/.gitignore" <<'IGNORE'
.cartographer/
cartographer.yaml
__pycache__/
*.pyc
*.db
*.sqlite
*.sqlite3
.DS_Store
IGNORE

echo "==> Writing $OUT"
rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" "$NAME" )

echo
echo "    $(du -h "$OUT" | cut -f1)  $OUT"
echo "    $(find "$STAGE/$NAME" -type f | wc -l | tr -d ' ') files"
echo
echo "Next, on the receiving machine:"
echo "  unzip $(basename "$OUT") && cd $NAME && ./install.sh"
echo "  git init  # here, never in the working copy"
