#!/usr/bin/env bash
#
# Set up codebase-cartographer. Safe to re-run.
#
#   ./install.sh                 verify + report; installs nothing
#   ./install.sh --link          also add bin/ to PATH via your shell rc
#   ./install.sh --plugin        also copy into ~/.claude/skills (auto-loads)
#
# Nothing here reaches the network. Nothing is installed with a package
# manager. The tool is Python standard library only.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK=0; PLUGIN=0
for a in "$@"; do
  case "$a" in
    --link) LINK=1 ;;
    --plugin) PLUGIN=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; }

say "codebase-cartographer setup"
say "  location: $HERE"
say ""

# ---- 1. python ------------------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)'; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  bad "no Python 3.8+ on PATH"
  say "      Install Python 3.8 or newer, then re-run."
  exit 1
fi
ok "python: $("$PY" --version 2>&1)"

# ---- 2. git (optional but wanted) -----------------------------------------
if command -v git >/dev/null 2>&1; then
  ok "git: $(git --version | awk '{print $3}')"
else
  bad "git not found — hotspots, change coupling and ownership will be empty"
fi

# ---- 3. executable bit survives some transfers, not all -------------------
chmod +x "$HERE/bin/cartographer" 2>/dev/null || true
ok "bin/cartographer is executable"

# ---- 4. the real check ----------------------------------------------------
say ""
say "Running the test suite (builds two synthetic estates, ~10s)…"
if "$PY" "$HERE/tests/test_all.py" >/tmp/cartographer-tests.log 2>&1; then
  ok "$(tail -3 /tmp/cartographer-tests.log | grep -E '^Ran' || echo 'tests passed')"
else
  bad "tests failed — see /tmp/cartographer-tests.log"
  tail -25 /tmp/cartographer-tests.log
  exit 1
fi

# ---- 5. optional: PATH ----------------------------------------------------
if [ "$LINK" = "1" ]; then
  RC="$HOME/.bashrc"
  [ -n "${ZSH_VERSION:-}" ] && RC="$HOME/.zshrc"
  [ "$(basename "${SHELL:-}")" = "zsh" ] && RC="$HOME/.zshrc"
  LINE="export PATH=\"$HERE/bin:\$PATH\""
  if [ -f "$RC" ] && grep -Fq "$HERE/bin" "$RC"; then
    ok "PATH already set in $RC"
  else
    printf '\n# codebase-cartographer\n%s\n' "$LINE" >> "$RC"
    ok "added to $RC — run: source $RC"
  fi
fi

# ---- 6. optional: install as an always-on plugin ---------------------------
if [ "$PLUGIN" = "1" ]; then
  DEST="$HOME/.claude/skills/cartographer"
  mkdir -p "$HOME/.claude/skills"
  rm -rf "$DEST"
  cp -R "$HERE" "$DEST"
  rm -rf "$DEST/.git" "$DEST/.cartographer"
  chmod +x "$DEST/bin/cartographer" 2>/dev/null || true
  ok "installed to $DEST (loads automatically next session)"
fi

say ""
say "Ready. Next:"
say "  1. cd <the directory that CONTAINS your repos>"
say "  2. $HERE/bin/cartographer init --root ."
say "  3. review the aliases it wrote into cartographer.yaml"
say "  4. $HERE/bin/cartographer scan"
say "  5. $HERE/bin/cartographer ui"
say ""
say "To use it inside Claude Code for one session:"
say "  claude --plugin-dir $HERE"
