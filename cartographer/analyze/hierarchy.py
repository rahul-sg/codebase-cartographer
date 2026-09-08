"""
Hierarchical aggregation: the thing that makes a 145,000-node graph readable.

Rendering every node at once produces a grey hairball -- not slow, illegible.
Nobody learns anything from it. So the graph is collapsed to whichever level
you are looking at, and edges are rolled up with counts:

    estate   ~20-60 nodes   services, topics, datastores, hosts, infra
    service  ~10-40 nodes   one service's packages, plus its neighbours
    package  ~10-60 nodes   subpackages and files
    file     ~5-50 nodes    the symbols in one file, and what they touch

Every level is legible on its own, and drilling down never loses the edges --
they are aggregated, not discarded, so a service-to-service arrow can be
expanded into the exact `file:line` calls that justify it.
"""
from __future__ import annotations

import json
from collections import defaultdict

# Node kinds that are actors in their own right at estate level. Shared
# libraries are included deliberately: `framework` and `cache` are compiled
# into 17 modules, and collapsing them into their repo would both hide that
# and invent a phantom service named after the repository.
TOP_KINDS = ("service", "topic", "datastore", "host", "infra", "legacy-page",
             "library")
# Kinds that live inside a service and get rolled up into it.
INNER_KINDS = ("file", "class", "function", "interface", "route", "table")

LAYER = {
    "host": 0, "legacy-page": 0,
    "service": 1,
    "topic": 2, "infra": 2,
    "datastore": 3, "table": 3,
    "library": 4,
    "package": 1,
    "file": 1, "class": 1, "function": 1, "route": 1,
}

EDGE_STYLE = {
    "http": {"kind": "sync", "weight": 3},
    "event": {"kind": "async", "weight": 3},
    "produces": {"kind": "async", "weight": 2},
    "consumed-by": {"kind": "async", "weight": 2},
    "depends-on": {"kind": "build", "weight": 2},
    "calls": {"kind": "code", "weight": 2},
    "imports": {"kind": "code", "weight": 1},
    "extends": {"kind": "code", "weight": 1},
    "defines": {"kind": "structure", "weight": 1},
    "reads-table": {"kind": "data", "weight": 2},
    "writes-table": {"kind": "data", "weight": 3},
    "defines-table": {"kind": "data", "weight": 1},
    "uses-datastore": {"kind": "data", "weight": 2},
    "uses-cache": {"kind": "data", "weight": 1},
    "uses-library": {"kind": "build", "weight": 1},
    "exposes": {"kind": "contract", "weight": 1},
    "routes-to": {"kind": "deploy", "weight": 1},
    "links-to": {"kind": "legacy", "weight": 1},
}


def _extra(row):
    try:
        return json.loads(row["extra"]) if row["extra"] else {}
    except (ValueError, TypeError):
        return {}


def _all_nodes(store):
    return {r["id"]: dict(r) for r in store.conn.execute("SELECT * FROM nodes")}


def _ranks(store):
    return {r["id"]: r["rank"] for r in store.conn.execute(
        "SELECT id, rank FROM ranks")}


# --------------------------------------------------------------- grouping

def _service_of(n):
    if n["kind"] in TOP_KINDS:
        return n["id"]
    # Fall back to repo only when it is not a multi-module container; a file
    # with no service attributed is better left ungrouped than misattributed.
    return n.get("service") or n.get("repo")


def _package_of(n, depth):
    """
    Directory prefix `depth` levels deep, ignoring conventional Java scaffolding
    so `src/main/java/com/itn/order/dao/X.java` groups as `order/dao` rather
    than everything collapsing into `src`.
    """
    path = n.get("file") or ""
    if not path:
        return None
    parts = [p for p in path.split("/")[:-1] if p]
    skip = {"src", "main", "java", "kotlin", "scala", "resources", "com", "org",
            "net", "io", "app", "lib"}
    trimmed = [p for p in parts if p not in skip] or parts
    if not trimmed:
        return "(root)"
    return "/".join(trimmed[:depth])


# --------------------------------------------------------------- API

def graph(store, level="estate", focus=None, package=None, limit=400,
          include=None, min_confidence=0.0):
    """
    Return {nodes, edges, breadcrumb, stats, level, focus} for one view.

    `include` optionally restricts edge kinds (["http","event"] for a pure
    service-interaction view).
    """
    nodes = _all_nodes(store)
    ranks = _ranks(store)

    if level == "estate":
        return _estate(store, nodes, ranks, limit, include, min_confidence)
    if level == "service":
        return _service(store, nodes, ranks, focus, limit, include, min_confidence)
    if level == "package":
        return _package(store, nodes, ranks, focus, package, limit, include,
                        min_confidence)
    if level == "file":
        return _file(store, nodes, ranks, focus, limit, include, min_confidence)
    raise ValueError("unknown level: %s" % level)


def _rollup(store, nodes, member_of, keep, include, min_confidence):
    """Aggregate every edge into the group its endpoints belong to."""
    agg = {}
    for r in store.conn.execute("SELECT * FROM edges"):
        if include and r["kind"] not in include:
            continue
        if (r["confidence"] or 0) < min_confidence:
            continue
        a = member_of.get(r["src"])
        b = member_of.get(r["dst"])
        if a is None or b is None or a == b:
            continue
        if a not in keep or b not in keep:
            continue
        key = (a, b, r["kind"])
        rec = agg.get(key)
        if rec is None:
            rec = agg[key] = {
                "source": a, "target": b, "kind": r["kind"],
                "style": EDGE_STYLE.get(r["kind"], {"kind": "other", "weight": 1}),
                "count": 0, "provenance": r["provenance"],
                "confidence": r["confidence"] or 0, "samples": [],
                "runtime": False, "calls": 0}
        rec["count"] += 1
        rec["confidence"] = max(rec["confidence"], r["confidence"] or 0)
        if r["provenance"] == "EXTRACTED":
            rec["provenance"] = "EXTRACTED"
        ex = _extra(r)
        if ex.get("runtime"):
            rec["runtime"] = True
            rec["calls"] = max(rec["calls"], ex.get("calls") or 0)
        if len(rec["samples"]) < 6 and r["evidence"]:
            rec["samples"].append({
                "evidence": r["evidence"], "via": ex.get("via"),
                "src": r["src"], "dst": r["dst"],
                "provenance": r["provenance"]})
    return list(agg.values())


def _pack(n, ranks, extra=None):
    ex = _extra(n)
    out = {
        "id": n["id"], "kind": n["kind"],
        "label": n.get("name") or (n.get("file") or "").split("/")[-1] or n["id"],
        "service": n.get("service"), "repo": n.get("repo"),
        "file": n.get("file"), "line": n.get("line"),
        "container": n.get("container"), "lang": n.get("lang"),
        "rank": round(ranks.get(n["id"], 0.0), 6),
        "layer": LAYER.get(n["kind"], 2),
        "meta": ex,
    }
    if extra:
        out.update(extra)
    return out


def _estate(store, nodes, ranks, limit, include, min_confidence):
    """Top level: the actors, with everything else rolled into its service."""
    member_of = {}
    tops = {}
    for nid, n in nodes.items():
        if n["kind"] in TOP_KINDS:
            tops[nid] = n
            member_of[nid] = nid
        elif n["kind"] == "table":
            # Tables are rolled into their declaring service here; the data
            # view shows them individually.
            svc = n.get("service")
            member_of[nid] = ("cart . service %s" % svc) if svc else None
        else:
            svc = _service_of(n)
            member_of[nid] = ("cart . service %s" % svc) if svc else None

    # Count what each service contains, so size means something.
    contents = defaultdict(lambda: defaultdict(int))
    for nid, n in nodes.items():
        g = member_of.get(nid)
        if g and g != nid:
            contents[g][n["kind"]] += 1

    keep = set(tops)
    out_nodes = []
    for nid, n in tops.items():
        c = contents.get(nid, {})
        out_nodes.append(_pack(n, ranks, {
            "contents": dict(c),
            "size": sum(c.values()),
            "drillable": bool(c),
        }))
    edges = _rollup(store, nodes, member_of, keep, include, min_confidence)
    return {"level": "estate", "focus": None, "nodes": out_nodes,
            "edges": edges, "breadcrumb": [{"label": "estate", "level": "estate"}],
            "stats": {"nodes": len(out_nodes), "edges": len(edges),
                      "rolled_up": len(nodes) - len(out_nodes)}}


def _pick_depth(nodes, svc, want_min=4, want_max=45):
    """
    Choose how deep to slice package paths for this service.

    A fixed depth is wrong in both directions: a service whose files all sit in
    one flat `src/` collapses to a single node (nothing to see), while a deep
    package tree explodes into hundreds. Try successive depths and keep the
    first that yields a readable number of groups.
    """
    owned = [n for n in nodes.values()
             if (n.get("service") or n.get("repo")) == svc and n.get("file")]
    if not owned:
        return 2
    best, best_depth = 0, 2
    for depth in (2, 3, 4, 5):
        groups = {_package_of(n, depth) for n in owned}
        groups.discard(None)
        k = len(groups)
        if want_min <= k <= want_max:
            return depth
        if k > best and k <= want_max:
            best, best_depth = k, depth
        if k > want_max:
            break
    return best_depth


def _service(store, nodes, ranks, focus, limit, include, min_confidence):
    """One service: its packages, plus the neighbours it talks to."""
    svc = focus
    depth = _pick_depth(nodes, svc)
    member_of = {}
    groups = {}
    for nid, n in nodes.items():
        owner = _service_of(n)
        if n["kind"] in TOP_KINDS:
            if nid == "cart . service %s" % svc:
                continue                       # do not draw the service itself
            member_of[nid] = nid
            groups[nid] = _pack(n, ranks, {"boundary": True})
            continue
        if owner != svc:
            member_of[nid] = ("cart . service %s" % owner) if owner else None
            continue
        if n["kind"] == "table":
            member_of[nid] = nid
            groups[nid] = _pack(n, ranks, {"boundary": False})
            continue
        pkg = _package_of(n, depth)
        if pkg is None:
            member_of[nid] = None
            continue
        gid = "pkg:%s:%s" % (svc, pkg)
        member_of[nid] = gid
        g = groups.get(gid)
        if g is None:
            g = groups[gid] = {"id": gid, "kind": "package", "label": pkg,
                               "service": svc, "layer": 1, "rank": 0.0,
                               "contents": defaultdict(int), "size": 0,
                               "drillable": True, "package": pkg,
                               "meta": {"depth": depth}}
        g["contents"][n["kind"]] += 1
        g["size"] += 1
        g["rank"] = max(g["rank"], ranks.get(nid, 0.0))

    for g in groups.values():
        if isinstance(g.get("contents"), defaultdict):
            g["contents"] = dict(g["contents"])
    keep = set(groups)
    edges = _rollup(store, nodes, member_of, keep, include, min_confidence)

    # Keep only what this service actually touches. Without this the view fills
    # with every other service in the estate whether or not it is related, and
    # with edges between two neighbours that have nothing to do with the focus.
    own = {gid for gid in groups if gid.startswith("pkg:%s:" % svc)}
    own |= {gid for gid, g in groups.items()
            if not g.get("boundary") and g.get("service") == svc}
    edges = [e for e in edges if e["source"] in own or e["target"] in own]
    linked = own | {e["source"] for e in edges} | {e["target"] for e in edges}
    groups = {k: v for k, v in groups.items() if k in linked}

    return {"level": "service", "focus": svc,
            "nodes": sorted(groups.values(), key=lambda x: -x.get("size", 0))[:limit],
            "edges": edges,
            "breadcrumb": [{"label": "estate", "level": "estate"},
                           {"label": svc, "level": "service", "focus": svc}],
            "stats": {"nodes": len(groups), "edges": len(edges)}}


def _package(store, nodes, ranks, focus, package, limit, include, min_confidence):
    """Inside one package: its files, plus boundary nodes for what they touch."""
    svc = focus
    member_of, groups = {}, {}
    for nid, n in nodes.items():
        owner = _service_of(n)
        if n["kind"] in TOP_KINDS or n["kind"] == "table":
            member_of[nid] = nid
            if nid != "cart . service %s" % svc:
                groups.setdefault(nid, _pack(n, ranks, {"boundary": True}))
            continue
        if owner != svc:
            member_of[nid] = ("cart . service %s" % owner) if owner else None
            continue
        pkg = _package_of(n, depth)
        if pkg != package:
            member_of[nid] = "pkg:%s:%s" % (svc, pkg) if pkg else None
            if pkg:
                groups.setdefault("pkg:%s:%s" % (svc, pkg),
                                  {"id": "pkg:%s:%s" % (svc, pkg),
                                   "kind": "package", "label": pkg,
                                   "service": svc, "layer": 1, "rank": 0.0,
                                   "boundary": True, "drillable": True,
                                   "package": pkg, "size": 0, "meta": {}})
            continue
        if n["kind"] == "file":
            member_of[nid] = nid
            groups[nid] = _pack(n, ranks, {"drillable": True})
        else:
            # symbols roll into their file
            fid = None
            if n.get("file") and n.get("repo"):
                for cand, cn in nodes.items():
                    if (cn["kind"] == "file" and cn.get("file") == n["file"]
                            and cn.get("repo") == n["repo"]):
                        fid = cand
                        break
            member_of[nid] = fid
    keep = set(groups)
    edges = _rollup(store, nodes, member_of, keep, include, min_confidence)
    own = {gid for gid, g in groups.items() if not g.get("boundary")}
    edges = [e for e in edges if e["source"] in own or e["target"] in own]
    linked = own | {e["source"] for e in edges} | {e["target"] for e in edges}
    groups = {k: v for k, v in groups.items() if k in linked}
    ordered = sorted(groups.values(),
                     key=lambda x: (bool(x.get("boundary")), -x.get("rank", 0)))
    truncated = max(0, len(ordered) - limit)
    return {"level": "package", "focus": svc, "package": package,
            "truncated": truncated,
            "nodes": ordered[:limit],
            "edges": edges,
            "breadcrumb": [{"label": "estate", "level": "estate"},
                           {"label": svc, "level": "service", "focus": svc},
                           {"label": package, "level": "package", "focus": svc,
                            "package": package}],
            "stats": {"nodes": len(groups), "edges": len(edges)}}


def _file(store, nodes, ranks, focus, limit, include, min_confidence):
    """The symbols in one file, and the things they reach."""
    fnode = nodes.get(focus)
    if not fnode:
        return {"level": "file", "focus": focus, "nodes": [], "edges": [],
                "breadcrumb": [], "stats": {}}
    path, repo = fnode.get("file"), fnode.get("repo")
    member_of, groups = {}, {}
    for nid, n in nodes.items():
        if nid == focus or (n.get("file") == path and n.get("repo") == repo):
            member_of[nid] = nid
            groups[nid] = _pack(n, ranks, {})
        elif n["kind"] in TOP_KINDS or n["kind"] == "table":
            member_of[nid] = nid
        elif n["kind"] in ("class", "function") and n.get("file"):
            member_of[nid] = nid
        else:
            member_of[nid] = nid

    # Only keep outside nodes that are actually connected to this file.
    connected = set(groups)
    for r in store.conn.execute(
            "SELECT src, dst FROM edges WHERE src IN (%s) OR dst IN (%s)"
            % (",".join("?" * len(groups)), ",".join("?" * len(groups))),
            list(groups) * 2):
        connected.add(r["src"])
        connected.add(r["dst"])
    for nid in connected - set(groups):
        n = nodes.get(nid)
        if n:
            groups[nid] = _pack(n, ranks, {"boundary": True})
    keep = set(groups)
    edges = _rollup(store, nodes, member_of, keep, include, min_confidence)
    svc = fnode.get("service") or fnode.get("repo")
    pkg = _package_of(fnode, _pick_depth(nodes, svc))
    return {"level": "file", "focus": focus,
            "nodes": list(groups.values())[:limit], "edges": edges,
            "breadcrumb": [{"label": "estate", "level": "estate"},
                           {"label": svc, "level": "service", "focus": svc},
                           {"label": pkg, "level": "package", "focus": svc,
                            "package": pkg},
                           {"label": (path or "").split("/")[-1],
                            "level": "file", "focus": focus}],
            "stats": {"nodes": len(groups), "edges": len(edges)}}
