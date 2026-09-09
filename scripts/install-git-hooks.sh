#!/usr/bin/env bash
#
# Install the repository's git hooks.
#
# Run this immediately after `git init`, before the first commit -- the hook
# only protects commits made after it exists, and the first commit is the one
# most likely to sweep in a graph database that was sitting in the working
# tree.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not a git repository: $ROOT" >&2
  echo "Run 'git init' first, then re-run this script." >&2
  exit 1
fi

HOOKS="$(git -C "$ROOT" rev-parse --git-path hooks)"
case "$HOOKS" in /*) ;; *) HOOKS="$ROOT/$HOOKS" ;; esac
mkdir -p "$HOOKS"

for h in "$ROOT"/scripts/hooks/*; do
  [ -f "$h" ] || continue
  name="$(basename "$h")"
  if [ -e "$HOOKS/$name" ] && ! cmp -s "$h" "$HOOKS/$name"; then
    cp "$HOOKS/$name" "$HOOKS/$name.backup-$(date +%s)"
    echo "  existing $name backed up"
  fi
  cp "$h" "$HOOKS/$name"
  chmod +x "$HOOKS/$name"
  echo "  installed $name"
done

echo
echo "Hooks installed into $HOOKS"
echo "Verify with:  git commit --allow-empty -m 'hook check'"
