"""
Symbol extraction: files, classes, functions, and the edges between them.

Container tracking is the part that matters. A method must be attached to its
class, otherwise every `validate` in the estate collides and the graph becomes
useless exactly where it should be most precise. Brace-delimited languages get
a brace-depth stack; indentation languages get an indent stack.
"""
from __future__ import annotations

import os

from .. import ids, langs
from ..config import SKIP_DIRS, GENERATED_HINTS, prune

SOURCE = "symbols"

INDENT_LANGS = {"python", "ruby"}
BRACE_LANGS = {"java", "kotlin", "scala", "groovy", "typescript", "javascript",
               "go", "csharp", "rust", "php", "swift", "c", "cpp", "proto"}


def _is_generated(path):
    low = path.replace("\\", "/").lower()
    return any(h in low for h in GENERATED_HINTS)


def iter_source_files(repo_root, max_bytes, follow_symlinks=False):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow_symlinks):
        prune(dirpath, dirnames)
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            lang = langs.lang_of(fn)
            if lang is None:
                continue
            if _is_generated(p):
                continue
            try:
                if os.path.getsize(p) > max_bytes:
                    continue
            except OSError:
                continue
            yield p, lang


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, UnicodeError):
        return None


class _Containers(object):
    """
    Tracks the enclosing class as we walk lines.

    Brace languages: a container's marker is the brace depth at the START of
    its declaration line; it is popped once depth falls back to that marker.
    Indentation languages: the marker is the declaration's indent.
    """

    def __init__(self, lang):
        self.stack = []          # (name, marker)
        self.brace = lang in BRACE_LANGS
        self.depth = 0

    def current(self):
        return self.stack[-1][0] if self.stack else None

    def marker_at_line_start(self, indent):
        """The marker a container declared on this line should carry."""
        return self.depth if self.brace else indent

    def advance(self, line, indent):
        """Consume a line, updating depth and popping closed containers."""
        if self.brace:
            self.depth = max(0, self.depth + line.count("{") - line.count("}"))
            while self.stack and self.depth <= self.stack[-1][1]:
                self.stack.pop()
        else:
            if line.strip():
                while self.stack and indent <= self.stack[-1][1]:
                    self.stack.pop()

    def push(self, name, marker):
        self.stack.append((name, marker))


def parse_file(path, lang, repo, repo_root, service):
    """Return (nodes, edges, defined) for one file. Never raises."""
    text = _read(path)
    if text is None:
        return [], [], {}
    clean = langs.strip_noise(text, lang)
    rel = os.path.relpath(path, repo_root).replace(os.sep, "/")

    fid = ids.file_id(repo, lang, rel)
    nodes = [{"id": fid, "kind": "file", "name": os.path.basename(rel),
              "file": rel, "line": 1, "repo": repo, "lang": lang,
              "service": service,
              "extra": {"loc": clean.count("\n") + 1,
                        "complexity": langs.complexity(clean, lang)}}]
    edges = []
    defined = {}

    def_patterns = langs.DEFS.get(lang, [])
    two_group = lang in langs.TWO_GROUP_DEFS
    imp_re = langs.IMPORTS.get(lang)
    ext_re = langs.EXTENDS.get(lang)
    containers = _Containers(lang)

    raw_lines = text.split("\n")
    lines = clean.split("\n")

    for i, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if len(line) > 2000:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        marker_here = containers.marker_at_line_start(indent)

        # One definition per line. Two on a line (`class X { void y() {} }`)
        # yields only the first -- accepted, because handling it properly needs
        # a real parser and real code is not written that way.
        matched = None
        for kind, pat in def_patterns:
            m = pat.match(line)
            if not m:
                continue
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            name = groups[-1] if (two_group and kind == "class" and len(groups) > 1) \
                else groups[0]
            if two_group and kind == "class" and len(m.groups()) > 1 and m.group(2):
                name = m.group(2)
            if not name or name in langs.KEYWORDS:
                continue
            matched = (kind, name)
            break

        # Pop containers this line closes before attaching, so a member on the
        # same line as a closing brace attaches to the right owner.
        containers.advance(line, indent)

        if matched:
            kind, name = matched
            container = containers.current()
            sid = ids.symbol_id(repo, lang, rel, name, kind, container)
            node = {"id": sid, "kind": kind, "name": name, "container": container,
                    "file": rel, "line": i, "repo": repo, "lang": lang,
                    "service": service}
            nodes.append(node)
            edges.append({"src": fid, "dst": sid, "kind": "defines",
                          "evidence": "%s:%d" % (rel, i),
                          "provenance": "EXTRACTED", "confidence": 1.0})
            defined.setdefault(name, []).append(sid)
            if kind in ("class", "interface", "struct", "trait", "enum"):
                containers.push(name, marker_here)

        if imp_re:
            m = imp_re.match(line) or (imp_re.search(line) if lang in
                                       ("typescript", "javascript") else None)
            if m:
                target = next((g for g in m.groups() if g), None)
                if target:
                    edges.append({"src": fid, "dst": ids.external_id(target),
                                  "kind": "imports",
                                  "evidence": "%s:%d" % (rel, i),
                                  "provenance": "EXTRACTED", "confidence": 1.0,
                                  "extra": {"module": target}})

        if ext_re:
            m = ext_re.search(line)
            if m and m.group(1):
                owner = containers.current()
                src = ids.symbol_id(repo, lang, rel, owner, "class") if owner else fid
                for parent in _split_parents(m.group(1)):
                    edges.append({"src": src, "dst": ids.external_id(parent),
                                  "kind": "extends",
                                  "evidence": "%s:%d" % (rel, i),
                                  "provenance": "EXTRACTED", "confidence": 0.9,
                                  "extra": {"parent": parent}})

    return nodes, edges, defined


_PARENT_KEYWORDS = {"implements", "extends", "with", "where", "by", "public",
                    "private", "protected", "internal", "open", "final"}


def _split_parents(blob):
    import re
    out = []
    for part in re.split(r"[,\s]+", blob.strip()):
        if part.lower() in _PARENT_KEYWORDS:
            continue
        part = part.split("<")[0].split("[")[0].split("(")[0]
        part = part.replace("\\", ".").split(".")[-1].split(":")[-1].strip()
        if part and part[0].isalpha() and part not in langs.CALL_NOISE:
            out.append(part)
    return out[:8]


def _enclosing(defs, line):
    """
    Innermost function definition starting at or before `line`.

    An approximation -- it does not track block ends -- but attributing a call
    to the function it sits in is far more useful than attributing it to the
    file, and being off at a file's tail is a small price for that precision.
    """
    best = None
    for dline, sid in defs:
        if dline <= line:
            best = sid
        else:
            break
    return best


def resolve_calls(files, index, by_repo):
    """
    Call edges, resolved against the symbol index.

    Resolution order, most to least trustworthy:
      1. exactly one definition anywhere      -> confidence 0.65
      2. exactly one definition in this repo  -> confidence 0.55
      3. ambiguous                            -> dropped, recorded as a gap

    Always INFERRED. Without scope resolution a name match is a lead, and this
    tool's contract is that it never presents a lead as a fact.
    """
    edges, gaps = [], []
    for path, lang, repo, repo_root, service, clean, rel, fn_defs in files:
        fid = ids.file_id(repo, lang, rel)
        seen = set()
        for i, line in enumerate(clean.split("\n"), start=1):
            if not line.strip() or len(line) > 2000:
                continue
            s = line.lstrip()
            if s.startswith(("import ", "from ", "using ", "package ", "#include")):
                continue
            for m in langs.CALL.finditer(line):
                name = m.group(1)
                if name in langs.CALL_NOISE or name in langs.KEYWORDS or name in seen:
                    continue
                targets = index.get(name)
                if not targets:
                    continue
                conf, chosen = None, None
                if len(targets) == 1:
                    chosen, conf = targets[0], 0.65
                else:
                    local = [t for t in targets if by_repo.get(t) == repo]
                    if len(local) == 1:
                        chosen, conf = local[0], 0.55
                    else:
                        gaps.append(("ambiguous-call",
                                     "%s: '%s' matches %d definitions"
                                     % (rel, name, len(targets)),
                                     "add scope resolution, or a SCIP indexer, "
                                     "to disambiguate"))
                        seen.add(name)
                        continue
                if ids.parse(chosen).get("path") == rel:
                    seen.add(name)
                    continue            # same-file call: low information
                seen.add(name)
                caller = _enclosing(fn_defs, i) or fid
                edges.append({"src": caller, "dst": chosen, "kind": "calls",
                              "evidence": "%s:%d" % (rel, i),
                              "provenance": "INFERRED", "confidence": conf})
                if caller != fid:
                    # Keep a file-level edge too, so file-granularity queries
                    # and the repo map still see the dependency.
                    edges.append({"src": fid, "dst": chosen, "kind": "calls",
                                  "evidence": "%s:%d" % (rel, i),
                                  "provenance": "INFERRED",
                                  "confidence": conf * 0.9})
    return edges, gaps


def run(store, cfg, repos, progress=None):
    """
    repos: list of (repo_name, repo_root, service_name)
    Returns a stats dict.
    """
    store.clear_source(SOURCE)
    max_bytes = int(cfg.defaults.get("max_file_bytes", 2_000_000))
    follow = bool(cfg.defaults.get("follow_symlinks", False))

    all_nodes, all_edges = [], []
    index = {}         # bare name -> [symbol id]
    by_repo = {}       # symbol id -> repo
    file_cache = []
    n_files = 0
    by_lang = {}

    for repo_name, repo_root, service in repos:
        for path, lang in iter_source_files(repo_root, max_bytes, follow):
            nodes, edges, defined = parse_file(path, lang, repo_name, repo_root, service)
            if not nodes:
                continue
            n_files += 1
            by_lang[lang] = by_lang.get(lang, 0) + 1
            all_nodes.extend(nodes)
            all_edges.extend(edges)
            for name, sids in defined.items():
                index.setdefault(name, []).extend(sids)
                for s in sids:
                    by_repo[s] = repo_name
            if cfg.defaults.get("call_edges", True):
                text = _read(path)
                if text is not None:
                    rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
                    fn_defs = sorted(
                        (n["line"], n["id"]) for n in nodes
                        if n["kind"] == "function" and n.get("line"))
                    file_cache.append((path, lang, repo_name, repo_root, service,
                                       langs.strip_noise(text, lang), rel, fn_defs))
            if progress and n_files % 500 == 0:
                progress("  symbols: %d files…" % n_files)

    call_edges, gaps = [], []
    if cfg.defaults.get("call_edges", True) and file_cache:
        if progress:
            progress("  symbols: resolving calls across %d files…" % len(file_cache))
        call_edges, gaps = resolve_calls(file_cache, index, by_repo)

    store.add_nodes(all_nodes, SOURCE)
    store.add_edges(all_edges, SOURCE)
    store.add_edges(call_edges, SOURCE)
    seen_gap = set()
    for cat, detail, hint in gaps[:500]:
        if detail in seen_gap:
            continue
        seen_gap.add(detail)
        store.add_gap(cat, detail, hint, SOURCE)
    store.commit()

    return {"files": n_files, "by_language": by_lang,
            "nodes": len(all_nodes), "edges": len(all_edges) + len(call_edges),
            "call_edges": len(call_edges), "ambiguous": len(gaps)}
