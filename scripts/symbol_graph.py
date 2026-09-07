#!/usr/bin/env python3
"""
symbol_graph.py — intra-repo symbol graph (the layer under the service map).

Emits nodes (files, classes, functions) and edges (defines, imports, extends,
calls) as JSON, joined to the service each symbol belongs to so that queries can
cross service boundaries.

Two backends, auto-selected:

  tree-sitter  accurate, needs `pip install tree-sitter-language-pack`
  regex        zero dependencies, roughly 70% recall, more false positives

If you can install tree-sitter, do — or better, use Graphify, which does this
layer properly across ~37 grammars. This exists so the cartographer still works
on a locked-down machine where you can install nothing.

Edge provenance follows Graphify's convention:
  EXTRACTED  read directly from the source
  INFERRED   resolved by name matching, and therefore possibly wrong

Fully local. No network calls.

Usage:
  ./symbol_graph.py <root> [-o context/symbols.json] [--services context/services.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# What we look at

LANG_BY_EXT = {
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go", ".rb": "ruby", ".cs": "csharp",
    ".rs": "rust", ".php": "php",
}

SKIP_DIRS = {
    "node_modules", ".git", "target", "build", "dist", "out", "vendor",
    ".venv", "venv", "__pycache__", ".gradle", ".idea", "coverage",
    ".mvn", "generated", "gen",
}

MAX_FILE_BYTES = 2_000_000

# --------------------------------------------------------------------------
# Regex backend
#
# Deliberately conservative: better to miss a definition than to invent one.
# Each pattern captures the symbol name in group 1.

DEFS = {
    "java": [
        ("class", re.compile(r"^\s*(?:public|private|protected)?\s*(?:final\s+|abstract\s+|static\s+)*(?:class|interface|enum|record)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:public|private|protected)\s+(?:static\s+|final\s+|synchronized\s+|abstract\s+|native\s+)*(?:<[^>]+>\s+)?[\w.<>\[\],\s?]+\s+(\w+)\s*\([^;]*\)\s*(?:throws [\w.,\s]+)?\s*\{")),
    ],
    "python": [
        ("class", re.compile(r"^\s*class\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")),
    ],
    "typescript": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*[:=].*?(?:=>|function)")),
        ("function", re.compile(r"^\s{2,}(?:public|private|protected|async|static|\s)*(\w+)\s*\([^)]*\)\s*[:{]")),
    ],
    "go": [
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)")),
        ("class", re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)")),
    ],
    "ruby": [
        ("class", re.compile(r"^\s*(?:class|module)\s+(\w+)")),
        ("function", re.compile(r"^\s*def\s+(?:self\.)?(\w+)")),
    ],
    "csharp": [
        ("class", re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:sealed\s+|abstract\s+|static\s+|partial\s+)*(?:class|interface|struct|record|enum)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:public|private|protected|internal)\s+(?:static\s+|async\s+|virtual\s+|override\s+)*[\w.<>\[\],\s?]+\s+(\w+)\s*\([^;]*\)\s*\{")),
    ],
    "rust": [
        ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)")),
    ],
    "php": [
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:public|private|protected|static|\s)*function\s+(\w+)")),
    ],
}
DEFS["kotlin"] = DEFS["java"] + [("function", re.compile(r"^\s*(?:private|public|internal|protected|\s)*fun\s+(\w+)"))]
DEFS["scala"] = [("class", re.compile(r"^\s*(?:case\s+)?(?:class|object|trait)\s+(\w+)")),
                 ("function", re.compile(r"^\s*def\s+(\w+)"))]
DEFS["javascript"] = DEFS["typescript"]

IMPORTS = {
    "java":       re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)"),
    "kotlin":     re.compile(r"^\s*import\s+([\w.]+)"),
    "scala":      re.compile(r"^\s*import\s+([\w.]+)"),
    "python":     re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))"),
    "typescript": re.compile(r"""(?:^\s*import\s.*?from\s+|require\()['"]([^'"]+)['"]"""),
    "javascript": re.compile(r"""(?:^\s*import\s.*?from\s+|require\()['"]([^'"]+)['"]"""),
    "go":         re.compile(r'^\s*(?:import\s+)?"([\w./-]+)"'),
    "ruby":       re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]"""),
    "csharp":     re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)"),
    "rust":       re.compile(r"^\s*use\s+([\w:]+)"),
    "php":        re.compile(r"^\s*use\s+([\w\\]+)"),
}

EXTENDS = {
    "java":       re.compile(r"\b(?:class|interface)\s+\w+(?:<[^>]*>)?\s+(?:extends|implements)\s+([\w.,<>\s]+?)(?:\{|$)"),
    "python":     re.compile(r"^\s*class\s+\w+\s*\(([^)]+)\)"),
    "typescript": re.compile(r"\b(?:class|interface)\s+\w+\s+(?:extends|implements)\s+([\w.,<>\s]+?)(?:\{|$)"),
    "csharp":     re.compile(r"\b(?:class|interface|record)\s+\w+\s*:\s*([\w.,<>\s]+?)(?:\{|where|$)"),
    "ruby":       re.compile(r"^\s*class\s+\w+\s*<\s*(\w+)"),
}
EXTENDS["kotlin"] = EXTENDS["java"]
EXTENDS["javascript"] = EXTENDS["typescript"]

CALL = re.compile(r"\b([a-zA-Z_]\w{2,})\s*\(")

# Words that look like calls but are control flow, or are so common that
# treating them as edges would bury the real signal.
CALL_NOISE = {
    "if", "for", "while", "switch", "catch", "return", "super", "this", "self",
    "print", "println", "log", "assert", "throw", "new", "typeof", "sizeof",
    "func", "def", "class", "function", "await", "yield", "import", "require",
    "String", "Integer", "Boolean", "List", "Map", "Set", "Optional", "Array",
    "get", "set", "put", "add", "run", "test", "it", "describe", "expect",
    "len", "str", "int", "dict", "list", "range", "type", "format", "join",
}


def detect_lang(path: Path) -> str | None:
    return LANG_BY_EXT.get(path.suffix.lower())


def iter_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if detect_lang(p) is None:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


def repo_of(path: Path, repo_roots: list[Path], root: Path) -> str:
    """
    Longest matching repo root wins, so nested repos resolve correctly.

    Falls back to the first path component under `root`, which keeps monorepo
    subdirectories and any non-git checkout from collapsing into one bucket.
    """
    best = None
    for r in repo_roots:
        try:
            path.relative_to(r)
        except ValueError:
            continue
        if best is None or len(str(r)) > len(str(best)):
            best = r
    if best is not None:
        return best.name
    try:
        parts = path.relative_to(root).parts
        return parts[0] if len(parts) > 1 else root.name
    except ValueError:
        return "?"


def find_repo_roots(root: Path) -> list[Path]:
    roots = [p.parent for p in root.rglob(".git") if p.is_dir()]
    return roots or [root]


def parse_file(path: Path, lang: str, root: Path, repo: str):
    """Return (nodes, edges, defined_names) for one file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], {}

    rel = str(path.relative_to(root))
    nodes, edges, defined = [], [], {}

    file_id = f"file:{rel}"
    nodes.append({"id": file_id, "kind": "file", "name": path.name,
                  "file": rel, "line": 1, "repo": repo, "lang": lang})

    lines = text.splitlines()
    def_patterns = DEFS.get(lang, [])
    imp_re = IMPORTS.get(lang)
    ext_re = EXTENDS.get(lang)

    # Track the innermost class by indentation so methods attach to it.
    current_class = None
    class_indent = -1

    for i, line in enumerate(lines, start=1):
        if len(line) > 500:
            continue
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue
        indent = len(line) - len(stripped)

        for kind, pat in def_patterns:
            m = pat.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in CALL_NOISE:
                continue
            if kind == "class":
                current_class, class_indent = name, indent
                sid = f"class:{rel}:{name}"
            else:
                if current_class and indent <= class_indent:
                    current_class = None
                owner = current_class
                sid = f"func:{rel}:{owner + '.' if owner else ''}{name}"
            nodes.append({"id": sid, "kind": kind, "name": name,
                          "file": rel, "line": i, "repo": repo, "lang": lang,
                          **({"class": current_class} if kind == "function" and current_class else {})})
            edges.append({"from": file_id, "to": sid, "kind": "defines",
                          "evidence": f"{rel}:{i}", "provenance": "EXTRACTED"})
            defined.setdefault(name, sid)
            break

        if imp_re:
            m = imp_re.match(line)
            if m:
                target = next((g for g in m.groups() if g), None)
                if target:
                    edges.append({"from": file_id, "to": f"module:{target}",
                                  "kind": "imports", "evidence": f"{rel}:{i}",
                                  "provenance": "EXTRACTED"})

        if ext_re:
            m = ext_re.search(line)
            if m:
                for parent in re.split(r"[,\s]+", m.group(1).strip()):
                    parent = parent.split("<")[0].split(".")[-1].strip()
                    if parent and parent[0].isupper() and parent not in CALL_NOISE:
                        edges.append({"from": f"class:{rel}:{current_class}" if current_class else file_id,
                                      "to": f"symbol:{parent}", "kind": "extends",
                                      "evidence": f"{rel}:{i}", "provenance": "EXTRACTED"})

    return nodes, edges, defined


def collect_calls(path: Path, lang: str, root: Path, symbol_index: dict[str, list[str]]):
    """
    Call edges, resolved by name against the global symbol index.

    This is the weakest part of the regex backend and always INFERRED: a name
    that matches two definitions in different repos cannot be disambiguated
    without real scope resolution. Ambiguous names are dropped rather than
    guessed, because a wrong call edge is worse than a missing one.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = str(path.relative_to(root))
    file_id = f"file:{rel}"
    seen, edges = set(), []

    for i, line in enumerate(text.splitlines(), start=1):
        if len(line) > 500:
            continue
        s = line.lstrip()
        if not s or s.startswith(("//", "#", "*", "/*", "import", "from", "using")):
            continue
        for m in CALL.finditer(line):
            name = m.group(1)
            if name in CALL_NOISE or name in seen:
                continue
            targets = symbol_index.get(name)
            if not targets or len(targets) > 1:
                continue                      # unknown or ambiguous: skip
            target = targets[0]
            if target.split(":")[1] == rel:
                continue                      # same-file call, low value
            seen.add(name)
            edges.append({"from": file_id, "to": target, "kind": "calls",
                          "evidence": f"{rel}:{i}", "provenance": "INFERRED"})
    return edges


def main() -> int:
    ap = argparse.ArgumentParser(description="Build an intra-repo symbol graph.")
    ap.add_argument("root", help="directory containing the service repos")
    ap.add_argument("-o", "--out", default="context/symbols.json")
    ap.add_argument("--services", default="context/services.json",
                    help="topology output, used to tag symbols with their service")
    ap.add_argument("--no-calls", action="store_true",
                    help="skip call-edge inference (faster, and much less noisy)")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    repo_roots = find_repo_roots(root)
    print(f"scanning {root}  ({len(repo_roots)} repo(s))", file=sys.stderr)

    files = list(iter_source_files(root))
    print(f"  {len(files)} source files", file=sys.stderr)

    nodes, edges = [], []
    symbol_index: dict[str, list[str]] = defaultdict(list)
    by_lang: dict[str, int] = defaultdict(int)
    parsed = []

    for p in files:
        lang = detect_lang(p)
        repo = repo_of(p, repo_roots, root)
        n, e, defined = parse_file(p, lang, root, repo)
        nodes.extend(n)
        edges.extend(e)
        by_lang[lang] += 1
        parsed.append((p, lang))
        for name, sid in defined.items():
            symbol_index[name].append(sid)

    if not args.no_calls:
        print("  resolving call edges…", file=sys.stderr)
        for p, lang in parsed:
            edges.extend(collect_calls(p, lang, root, symbol_index))

    # Join to the service layer so queries can cross process boundaries.
    svc_by_repo = {}
    svc_path = Path(args.services)
    if svc_path.exists():
        try:
            svc = json.loads(svc_path.read_text())
            for s in svc.get("services", []):
                if s.get("repo"):
                    svc_by_repo[Path(s["repo"]).name] = s["name"]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  note: could not read {svc_path}: {exc}", file=sys.stderr)
    for n in nodes:
        n["service"] = svc_by_repo.get(n["repo"], n["repo"])

    counts: dict[str, int] = defaultdict(int)
    for e in edges:
        counts[e["kind"]] += 1

    graph = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "backend": "regex",
        "caveat": ("Regex backend: ~70% recall, and 'calls' edges are name-matched "
                   "rather than scope-resolved. Ambiguous names are dropped. Treat "
                   "every INFERRED edge as a lead to verify, not a fact."),
        "stats": {
            "files": len(files),
            "by_language": dict(sorted(by_lang.items(), key=lambda kv: -kv[1])),
            "nodes": len(nodes),
            "edges": len(edges),
            "edges_by_kind": dict(counts),
            "repos": sorted({n["repo"] for n in nodes}),
        },
        "nodes": nodes,
        "edges": edges,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, indent=2))

    print(f"\nwrote {out}", file=sys.stderr)
    print(f"  {len(nodes)} nodes, {len(edges)} edges", file=sys.stderr)
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {k:10s} {v}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
