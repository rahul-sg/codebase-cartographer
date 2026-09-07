#!/usr/bin/env bash
# git-ownership.sh — who actually maintains this code, and what churns.
#
# Pure git. No language assumptions, no network, nothing leaves the machine.
#
# Usage: ./git-ownership.sh <repo-path> [months] [top-n]
#   repo-path  path to a git repository
#   months     history window (default 12)
#   top-n      directories to report (default 25)

set -euo pipefail

REPO="${1:-.}"
MONTHS="${2:-12}"
TOPN="${3:-25}"
SINCE="${MONTHS} months ago"

if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: '$REPO' is not a git repository" >&2
  exit 1
fi

REPO_NAME="$(basename "$(git -C "$REPO" rev-parse --show-toplevel)")"

echo "# Ownership & churn: ${REPO_NAME}"
echo "# window: last ${MONTHS} months"
echo

echo "## Top contributors"
git -C "$REPO" log --since="$SINCE" --format='%aN' \
  | sort | uniq -c | sort -rn | head -15 \
  | awk '{c=$1; $1=""; sub(/^ /,""); printf "  %5d commits  %s\n", c, $0}'
echo

echo "## Hottest directories (commit touches)"
git -C "$REPO" log --since="$SINCE" --name-only --format='' \
  | grep -v '^$' \
  | awk -F/ 'NF>1 {print $1"/"$2; next} {print "."}' \
  | sort | uniq -c | sort -rn | head -"$TOPN" \
  | awk '{c=$1; $1=""; sub(/^ /,""); printf "  %5d  %s\n", c, $0}'
echo

echo "## Primary author per hot directory"
echo "# who to ask, and who to tag on a PR"
git -C "$REPO" log --since="$SINCE" --name-only --format='@%aN' \
  | awk '
      /^@/ { author = substr($0,2); next }
      NF   { n = split($0, p, "/")
             d = (n > 1) ? p[1]"/"p[2] : "."
             key = d "\t" author
             count[key]++
             dirtotal[d]++ }
      END  { for (k in count) {
               split(k, kv, "\t")
               if (count[k] > best[kv[1]]) { best[kv[1]] = count[k]; who[kv[1]] = kv[2] }
             }
             for (d in dirtotal)
               printf "%6d\t%s\t%s\t%d\n", dirtotal[d], d, who[d], best[d] }' \
  | sort -rn | head -"$TOPN" \
  | awk -F'\t' '{ printf "  %-40s %s (%d of %d touches)\n", $2, $3, $4, $1 }'
echo

echo "## Files that change together"
echo "# strong co-change across repos usually means a hidden coupling"
git -C "$REPO" log --since="$SINCE" --name-only --format='---' \
  | awk '
      /^---$/ { if (n > 1 && n <= 20)
                  for (i = 1; i < n; i++)
                    for (j = i+1; j <= n; j++) {
                      a = f[i]; b = f[j]
                      if (a > b) { t = a; a = b; b = t }
                      pair[a " + " b]++
                    }
                n = 0; next }
      NF      { f[++n] = $0 }' \
  | sort | uniq -c | sort -rn | head -15 \
  | awk '{c=$1; $1=""; sub(/^ /,""); printf "  %4d  %s\n", c, $0}'
echo

echo "## Stale areas (no commits in window)"
echo "# candidates for dead code — verify before believing"
git -C "$REPO" ls-files \
  | awk -F/ 'NF>1 {print $1"/"$2; next} {print "."}' \
  | sort -u > /tmp/.cart_all_dirs.$$
git -C "$REPO" log --since="$SINCE" --name-only --format='' \
  | grep -v '^$' \
  | awk -F/ 'NF>1 {print $1"/"$2; next} {print "."}' \
  | sort -u > /tmp/.cart_hot_dirs.$$
comm -23 /tmp/.cart_all_dirs.$$ /tmp/.cart_hot_dirs.$$ | head -20 | sed 's/^/  /'
rm -f /tmp/.cart_all_dirs.$$ /tmp/.cart_hot_dirs.$$
