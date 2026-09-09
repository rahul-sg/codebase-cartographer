"""
Symbol extraction: files, classes, functions, and the edges between them.

Container tracking is the part that matters. A method must be attached to its
class, otherwise every `validate` in the estate collides and the graph becomes
useless exactly where it should be most precise. Brace-delimited languages get
a brace-depth stack; indentation languages get an indent stack.
"""
from __future__ import annotations

import hashlib
import json
import os

from .. import ids, langs
from ..config import SKIP_DIRS, GENERATED_HINTS, prune

SOURCE = "symbols"

# Bump this on ANY change to what parsing produces -- the payload's shape OR
# the parser's behaviour. The cache is keyed on file CONTENT, so improving the
# parser does not invalidate anything by itself: unchanged files keep being
# served from a cache built by the old logic, and the improvement silently
# never reaches the graph. Content hashing answers "did this file change", not
# "would we parse it differently now"; only this version answers the second.
CACHE_VERSION = 5

INDENT_LANGS = {"python", "ruby"}
BRACE_LANGS = {"java", "kotlin", "scala", "groovy", "typescript", "javascript",
               "go", "csharp", "rust", "php", "swift", "c", "cpp", "proto"}


# Directory names that mark the start of a module tree. `server/<module>/...`
# and `modules/<module>/...` are both common; the segment after one of these
# is the module, not the repo.
MODULE_ANCHORS = ("server", "modules", "services", "apps", "packages", "libs")


def module_owner(cfg, repo_root, path, fallback):
    """Attribute a file to its Maven/Gradle module rather than to its repo."""
    rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
    parts = rel.split("/")
    for anchor in MODULE_ANCHORS:
        if anchor in parts:
            i = parts.index(anchor)
            if i + 1 < len(parts) - 1:
                name = parts[i + 1]
                return cfg.resolve_service(name) or name
    return fallback


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


def _file_hash(path):
    """sha256 of a file's bytes, or None if it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(262144), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load_payload(raw):
    """A cache entry, or None if it is unusable for any reason."""
    try:
        got = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(got, dict) or "nodes" not in got:
        return None
    got.setdefault("edges", [])
    got.setdefault("defined", {})
    got.setdefault("fn_defs", [])
    got.setdefault("calls", [])
    got.setdefault("supertypes", [])
    got.setdefault("imports", [])
    return got


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
    """
    Return (nodes, edges, defined, pending_supertypes, pending_imports).

    The two `pending_*` lists await resolution against the global symbol index
    -- see `resolve_supertypes` and `resolve_imports`. Neither can be decided
    per file, because whether a name is first-party is a whole-estate question.
    Never raises.
    """
    text = _read(path)
    if text is None:
        return [], [], {}, [], []
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
    pending = []
    pending_imports = []

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
                    # Also queued for first-party resolution. The external
                    # edge above stays either way -- it is correct for a JDK
                    # or library import, and harmless alongside a resolved one.
                    pending_imports.append((fid, target, i))

        if ext_re:
            # A declaration is regularly split across lines when the implements
            # list is long:
            #
            #     public class OrderServiceImpl
            #             implements OrderService {
            #
            # so matching a single line finds no supertype at all. Join forward
            # to the opening brace -- bounded, and only from a line that starts
            # a type declaration, so this never runs over a method body.
            probe = line
            if matched and matched[0] in ("class", "interface", "enum",
                                          "record", "struct", "trait") \
                    and "{" not in line:
                for j in range(i, min(i + 4, len(lines))):
                    probe += " " + lines[j].strip()
                    if "{" in lines[j]:
                        break
            m = ext_re.search(probe)
            if m and m.group(1):
                owner = containers.current()
                src = ids.symbol_id(repo, lang, rel, owner, "class") if owner else fid
                for parent in _split_parents(m.group(1)):
                    # Recorded as PENDING, not emitted. Whether `FooService` is
                    # a type in this codebase or a third-party class cannot be
                    # known until the whole symbol index exists, and guessing
                    # "external" for all of them produced 7,389 edges pointing
                    # at nodes that were never created -- a type hierarchy that
                    # looked populated and joined to nothing.
                    pending.append((src, parent, i))

    return nodes, edges, defined, pending, pending_imports


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


def resolve_imports(pending, type_index, type_files):
    """
    Link `import com.acme.order.service.OrderService` to that interface's
    node, when it is defined in this estate.

    Imports were recorded only as external stubs, so a controller had no edge
    of any kind to the first-party types it depends on -- and since a call like
    `orderService.get()` resolves to hundreds of `get` definitions and is
    dropped as ambiguous, the import was the ONLY available link. Without it a
    controller and the service it uses sit in the same graph unconnected.

    A fully-qualified name resolves precisely: the package path must match the
    defining file's path, so `com.acme.order.service.OrderService` only
    matches a file ending `com/acme/order/service/OrderService.java`. That
    is a near-exact match, not the simple-name guessing used elsewhere -- so
    these edges are EXTRACTED at high confidence.

    Only first-party resolutions are returned; unresolved imports keep the
    external edge `parse_file` already emitted.
    """
    edges = []
    for fid, fqn, line, rel, _repo in pending:
        fqn = (fqn or "").strip()
        if "." not in fqn:
            continue
        simple = fqn.split(".")[-1]
        if not simple or not simple[:1].isupper():
            continue          # a package or static-member import, not a type
        cands = type_index.get(simple)
        if not cands:
            continue
        want = "/".join(fqn.split(".")) + "."
        chosen = None
        for cid in cands:
            path = (type_files.get(cid) or "").replace("\\", "/")
            if want in path or path.endswith("/".join(fqn.split(".")[-2:])):
                chosen = cid
                break
        if chosen is None and len(cands) == 1:
            # Name is unique estate-wide: safe even without a path match,
            # which covers a package layout that does not mirror the FQN.
            chosen = cands[0]
        if chosen is None:
            continue
        edges.append({"src": fid, "dst": chosen, "kind": "imports",
                      "evidence": "%s:%d" % (rel, line),
                      "provenance": "EXTRACTED", "confidence": 0.95,
                      "extra": {"module": fqn, "first_party": True}})
    return edges


def resolve_supertypes(pending, type_index, by_repo):
    """
    Turn `class Impl implements Service` into a real edge to that interface.

    `type_index` maps a simple type name to the ids of classes/interfaces
    DEFINED in this estate. A parent found there is a first-party type and the
    edge is EXTRACTED; a parent that is not (`RuntimeException`,
    `JpaRepository`) is genuinely external and gets an external stub, which is
    correct and honest for a third-party supertype.

    Resolution order matches `resolve_calls` so the two behave alike:
      1. exactly one definition anywhere      -> EXTRACTED, 0.95
      2. exactly one definition in this repo  -> EXTRACTED, 0.8
      3. ambiguous                            -> external, and a gap

    Returns (edges, gaps).
    """
    edges, gaps = [], []
    for src, parent, line, rel, repo in pending:
        simple = parent.split(".")[-1].split("<")[0].strip()
        if not simple:
            continue
        targets = type_index.get(simple)
        chosen, conf = None, None
        if targets:
            if len(targets) == 1:
                chosen, conf = targets[0], 0.95
            else:
                local = [t for t in targets if by_repo.get(t) == repo]
                if len(local) == 1:
                    chosen, conf = local[0], 0.8
                else:
                    gaps.append((
                        "ambiguous-supertype",
                        "%s:%d: supertype '%s' matches %d definitions"
                        % (rel, line, simple, len(targets)),
                        "two first-party types share this name; scope "
                        "resolution or an indexer would disambiguate"))
        if chosen:
            edges.append({"src": src, "dst": chosen, "kind": "extends",
                          "evidence": "%s:%d" % (rel, line),
                          "provenance": "EXTRACTED", "confidence": conf,
                          "extra": {"parent": simple, "first_party": True}})
            # The reciprocal edge, stated explicitly. Path traversal is
            # directed, and deliberately so -- making it bidirectional would
            # invent paths like "A imports X, C imports X, therefore A relates
            # to C". But a caller almost always holds the INTERFACE (via an
            # import or a field type) and needs to reach the implementation,
            # which is backwards along `extends`. "Who implements this?" is
            # also a first-class question in its own right.
            edges.append({"src": chosen, "dst": src, "kind": "implemented-by",
                          "evidence": "%s:%d" % (rel, line),
                          "provenance": "EXTRACTED", "confidence": conf,
                          "extra": {"parent": simple, "first_party": True}})
        else:
            # Not defined here: a third-party or JDK supertype. Still worth an
            # edge, but pointed at an external stub that IS created as a node
            # so the edge is not dangling.
            edges.append({"src": src, "dst": ids.external_id(simple),
                          "kind": "extends",
                          "evidence": "%s:%d" % (rel, line),
                          "provenance": "EXTRACTED", "confidence": 0.9,
                          "extra": {"parent": simple, "first_party": False}})
    return edges, gaps


def extract_calls(clean, lang=None):
    """
    Call-site names in one file, deduplicated, as [(name, line)].

    Split out of `resolve_calls` so it can be cached per file: which names a
    file calls depends only on that file, while *resolving* those names depends
    on the whole symbol index. Separating the two is what makes an incremental
    parse safe -- the cached half cannot go stale when a different file changes.

    Deduplicating by name here matches the previous behaviour exactly: the
    resolver kept a `seen` set and processed only a name's first occurrence, and
    a name absent from the index yields no edge on any occurrence.
    """
    out, seen = [], set()
    for i, line in enumerate((clean or "").split("\n"), start=1):
        if not line.strip() or len(line) > 2000:
            continue
        s = line.lstrip()
        if s.startswith(("import ", "from ", "using ", "package ", "#include")):
            continue
        for m in langs.CALL.finditer(line):
            name = m.group(1)
            if name in langs.CALL_NOISE or name in langs.KEYWORDS or name in seen:
                continue
            seen.add(name)
            out.append((name, i))
    return out


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
    for repo, lang, rel, fn_defs, calls in files:
        fid = ids.file_id(repo, lang, rel)
        seen = set()
        for name, i in calls:
            if name in seen:
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

    Parsing is cached per file by content hash. Call resolution is not: a call
    edge is resolved against the symbol index of the whole estate, so caching
    it would leave stale edges pointing at definitions another file just
    renamed. Parsing was measured at 96% of this extractor's cost and
    resolution at 4%, so caching only the safe half keeps nearly all the win.
    """
    max_bytes = int(cfg.defaults.get("max_file_bytes", 2_000_000))
    follow = bool(cfg.defaults.get("follow_symlinks", False))
    want_calls = bool(cfg.defaults.get("call_edges", True))

    cached = store.parse_cache_all()
    fresh_rows, live_keys = [], []
    all_nodes, all_edges = [], []
    index = {}         # bare name -> [symbol id]
    by_repo = {}       # symbol id -> repo
    call_files = []    # (repo, lang, rel, fn_defs, calls)
    super_pending = []  # (subtype_id, parent_name, line, rel, repo)
    type_index = {}     # simple type name -> [ids of first-party types]
    type_files = {}     # type id -> its file path, for package matching
    import_pending = [] # (file_id, imported FQN, line, rel, repo)
    n_files = n_hit = n_miss = 0
    by_lang = {}

    for repo_name, repo_root, service in repos:
        for path, lang in iter_source_files(repo_root, max_bytes, follow):
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            key = "v%d|%s|%s|%s" % (CACHE_VERSION, repo_name, lang, rel)
            live_keys.append(key)

            digest = _file_hash(path)
            hit = cached.get(key)
            payload = None
            if hit and digest and hit[0] == digest:
                payload = _load_payload(hit[1])

            if payload is None:
                # In a repo of Maven modules the repo name is far too coarse:
                # stamping every file with it invents a phantom service and
                # makes per-module questions unanswerable.
                owner = module_owner(cfg, repo_root, path, service)
                nodes, edges, defined, pending, pend_imp = parse_file(
                    path, lang, repo_name, repo_root, owner)
                if not nodes:
                    continue
                calls = []
                if want_calls:
                    text = _read(path)
                    if text is not None:
                        calls = extract_calls(langs.strip_noise(text, lang), lang)
                fn_defs = sorted((n["line"], n["id"]) for n in nodes
                                 if n["kind"] == "function" and n.get("line"))
                payload = {"nodes": nodes, "edges": edges, "defined": defined,
                           "fn_defs": fn_defs, "calls": calls,
                           "supertypes": pending, "imports": pend_imp}
                if digest:
                    fresh_rows.append((key, digest, json.dumps(payload)))
                n_miss += 1
            else:
                n_hit += 1

            n_files += 1
            by_lang[lang] = by_lang.get(lang, 0) + 1
            all_nodes.extend(payload["nodes"])
            all_edges.extend(payload["edges"])
            for name, sids in (payload["defined"] or {}).items():
                index.setdefault(name, []).extend(sids)
                for sid in sids:
                    by_repo[sid] = repo_name
            # A separate index of TYPES only. Resolving a supertype against
            # the all-symbols index would happily match a method of the same
            # name and produce a nonsense hierarchy.
            for n in payload["nodes"]:
                if n.get("kind") in ("class", "interface", "enum", "record",
                                     "struct", "trait"):
                    type_index.setdefault(n["name"], []).append(n["id"])
                    type_files[n["id"]] = n.get("file") or ""
            for row in (payload.get("supertypes") or []):
                if len(row) >= 3:
                    super_pending.append((row[0], row[1], row[2], rel,
                                          repo_name))
            for row in (payload.get("imports") or []):
                if len(row) >= 3:
                    import_pending.append((row[0], row[1], row[2], rel,
                                           repo_name))
            if want_calls and payload.get("calls"):
                call_files.append((repo_name, lang, rel,
                                   [tuple(x) for x in payload["fn_defs"]],
                                   [tuple(x) for x in payload["calls"]]))
            if progress and n_files % 2000 == 0:
                progress("  symbols: %d files (%d cached)…" % (n_files, n_hit))

    super_edges, super_gaps = [], []
    if super_pending:
        if progress:
            progress("  symbols: resolving %d supertype reference(s)…"
                     % len(super_pending))
        super_edges, super_gaps = resolve_supertypes(
            super_pending, type_index, by_repo)
        # External stubs must exist as nodes, or the edge dangles -- which is
        # exactly the bug this replaces.
        stubs = {}
        for e in super_edges:
            if not (e.get("extra") or {}).get("first_party"):
                stubs[e["dst"]] = {
                    "id": e["dst"], "kind": "library",
                    "name": (e["extra"] or {}).get("parent") or e["dst"],
                    "extra": {"external": True, "via": "supertype"}}
        all_nodes.extend(stubs.values())

    import_edges = []
    if import_pending:
        if progress:
            progress("  symbols: resolving %d import(s)…" % len(import_pending))
        import_edges = resolve_imports(import_pending, type_index, type_files)

    call_edges, gaps = [], []
    if want_calls and call_files:
        if progress:
            progress("  symbols: resolving calls across %d files…"
                     % len(call_files))
        call_edges, gaps = resolve_calls(call_files, index, by_repo)

    # Written only after a successful pass, so an interrupted scan does not
    # leave a cache that disagrees with the graph.
    store.clear_source(SOURCE)
    store.add_nodes(all_nodes, SOURCE)
    store.add_edges(all_edges, SOURCE)
    store.add_edges(call_edges, SOURCE)
    store.add_edges(super_edges, SOURCE)
    store.add_edges(import_edges, SOURCE)
    seen_gap = set()
    for cat, detail, hint in (list(super_gaps[:200]) + list(gaps[:500])):
        if detail in seen_gap:
            continue
        seen_gap.add(detail)
        store.add_gap(cat, detail, hint, SOURCE)
    if fresh_rows:
        store.parse_cache_put(fresh_rows)
    pruned = store.parse_cache_prune(live_keys)
    store.commit()

    return {"files": n_files, "by_language": by_lang,
            "cached": n_hit, "parsed": n_miss, "cache_pruned": pruned,
            "nodes": len(all_nodes), "edges": len(all_edges) + len(call_edges),
            "call_edges": len(call_edges), "ambiguous": len(gaps),
            "imports_first_party": len(import_edges),
            "supertypes": len(super_edges),
            "supertypes_first_party": sum(
                1 for e in super_edges
                if (e.get("extra") or {}).get("first_party"))}
