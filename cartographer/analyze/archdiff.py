"""
Architecture drift: what changed between two scans.

A map tells you what the estate looks like now. It does not tell you that this
morning's pull added a second writer to a shared table, introduced a new
service-to-service call, or quietly deleted an endpoint the UI still calls.
Those are the changes worth a conversation, and they are invisible in a
snapshot of the present.

So each scan writes a small record of the architectural surface -- services,
routes, topics, tables and who writes them, cross-service edges -- and `diff`
compares two of them. The record deliberately excludes the symbol graph: 175k
functions churn constantly and none of that movement is architecture. What
belongs here is the shape a reviewer would draw on a whiteboard.

The findings this is built for, in rough order of how much they matter:

  * a table gained a writer         -- two services now write it, owner unclear
  * a new service-to-service edge   -- a dependency that did not exist before
  * a route disappeared             -- something may still be calling it
  * a service appeared or vanished

Nothing here is a verdict. A new edge is often exactly the intended change;
the point is that it should be a decision rather than a surprise.
"""
from __future__ import annotations

import json
import os
import time

SNAP_DIR = "snapshots"
KEEP = 30          # a month of daily scans; older ones are rarely compared


def snapshot(store):
    """The architectural surface of the current graph, as plain data."""
    c = store.conn

    services = sorted(
        "%s" % r["name"] for r in
        c.execute("SELECT name FROM nodes WHERE kind='service'"))

    routes = sorted(
        "%s %s" % (r["service"] or "?", r["name"]) for r in
        c.execute("SELECT service, name FROM nodes WHERE kind='route'"))

    topics = sorted(
        r["name"] for r in
        c.execute("SELECT name FROM nodes WHERE kind='topic'"))

    tables = {}
    for kind, field in (("writes-table", "writers"),
                        ("reads-table", "readers"),
                        ("defines-table", "declared_by")):
        rows = c.execute(
            "SELECT s.name AS svc, d.name AS tbl FROM edges e "
            "JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst "
            "WHERE e.kind = ?", (kind,))
        for r in rows:
            t = tables.setdefault(r["tbl"], {"writers": [], "readers": [],
                                             "declared_by": []})
            if r["svc"] not in t[field]:
                t[field].append(r["svc"])
    for t in tables.values():
        for k in t:
            t[k] = sorted(t[k])

    svc_edges = sorted({
        "%s -%s-> %s" % (r["src"], r["kind"], r["dst"]) for r in
        c.execute(
            "SELECT s.name AS src, d.name AS dst, e.kind AS kind FROM edges e "
            "JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst "
            "WHERE s.kind='service' AND d.kind='service' AND s.name <> d.name")
    })

    return {
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "services": services,
        "routes": routes,
        "topics": topics,
        "tables": tables,
        "service_edges": svc_edges,
        "counts": {
            "services": len(services), "routes": len(routes),
            "topics": len(topics), "tables": len(tables),
            "service_edges": len(svc_edges),
            "multi_writer": sum(1 for t in tables.values()
                                if len(t["writers"]) > 1),
        },
    }


def snap_dir(cfg):
    return os.path.join(os.path.dirname(cfg.db_path()), SNAP_DIR)


def write_snapshot(cfg, snap):
    """Store a snapshot and prune old ones. Never fatal to a scan."""
    try:
        d = snap_dir(cfg)
        os.makedirs(d, exist_ok=True)
        name = snap["taken_at"].replace(":", "").replace("-", "")
        path = os.path.join(d, "%s.json" % name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, sort_keys=True)
        existing = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        for stale in existing[:-KEEP]:
            try:
                os.remove(os.path.join(d, stale))
            except OSError:
                pass
        return path
    except (OSError, TypeError, ValueError):
        return None


def list_snapshots(cfg):
    d = snap_dir(cfg)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f)
            for f in sorted(f for f in os.listdir(d) if f.endswith(".json"))]


def load_snapshot(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def _added_removed(a, b):
    sa, sb = set(a or []), set(b or [])
    return sorted(sb - sa), sorted(sa - sb)


def diff(old, new):
    """
    Compare two snapshots.

    Table writer changes are reported per table rather than as a count, because
    "INVOICE gained a writer: integration" is a question somebody can answer
    and "multi-writer count went 65 -> 66" is not.
    """
    d = {"from": old.get("taken_at"), "to": new.get("taken_at")}

    d["services_added"], d["services_removed"] = _added_removed(
        old.get("services"), new.get("services"))
    d["routes_added"], d["routes_removed"] = _added_removed(
        old.get("routes"), new.get("routes"))
    d["topics_added"], d["topics_removed"] = _added_removed(
        old.get("topics"), new.get("topics"))
    d["edges_added"], d["edges_removed"] = _added_removed(
        old.get("service_edges"), new.get("service_edges"))

    ot, nt = old.get("tables") or {}, new.get("tables") or {}
    d["tables_added"] = sorted(set(nt) - set(ot))
    d["tables_removed"] = sorted(set(ot) - set(nt))

    gained, lost, became_multi = [], [], []
    for name in sorted(set(ot) & set(nt)):
        ow = set((ot[name] or {}).get("writers") or [])
        nw = set((nt[name] or {}).get("writers") or [])
        if nw - ow:
            gained.append((name, sorted(nw - ow), sorted(nw)))
            if len(ow) <= 1 < len(nw):
                became_multi.append(name)
        if ow - nw:
            lost.append((name, sorted(ow - nw), sorted(nw)))
    d["writers_gained"] = gained
    d["writers_lost"] = lost
    d["became_multi_writer"] = became_multi

    d["counts_before"] = old.get("counts") or {}
    d["counts_after"] = new.get("counts") or {}
    d["changed"] = any(
        d[k] for k in ("services_added", "services_removed", "routes_added",
                       "routes_removed", "topics_added", "topics_removed",
                       "edges_added", "edges_removed", "tables_added",
                       "tables_removed", "writers_gained", "writers_lost"))
    return d
