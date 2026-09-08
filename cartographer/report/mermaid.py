"""Mermaid diagrams of the service topology."""
from __future__ import annotations

import json
import re


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name or "unknown")


def service_graph(store, include_runtime=True, max_edges=120):
    """
    Service topology. Solid arrows are synchronous calls, dashed are events;
    thick arrows are confirmed by runtime traces.
    """
    edges = {}
    for r in store.conn.execute(
            "SELECT src, dst, kind, provenance, confidence, extra FROM edges "
            "WHERE kind IN ('http','event','depends-on')"):
        src = _svc_name(r["src"])
        dst = _svc_name(r["dst"])
        if not src or not dst or src == dst:
            continue
        runtime = False
        via = ""
        if r["extra"]:
            try:
                x = json.loads(r["extra"])
                runtime = bool(x.get("runtime"))
                via = x.get("via") or ""
            except ValueError:
                pass
        key = (src, dst, r["kind"])
        cur = edges.setdefault(key, {"runtime": False, "via": set(),
                                     "conf": 0.0})
        cur["runtime"] = cur["runtime"] or runtime
        cur["conf"] = max(cur["conf"], float(r["confidence"] or 0))
        if via:
            cur["via"].add(via[:40])

    services = sorted({s for (s, _, _) in edges} | {d for (_, d, _) in edges})
    if not services:
        for r in store.conn.execute("SELECT name FROM nodes WHERE kind='service'"):
            services.append(r["name"])
        services = sorted(set(services))

    L = ["```mermaid", "graph LR"]
    for s in services:
        L.append('  %s["%s"]' % (_safe(s), s))
    n = 0
    for (src, dst, kind), meta in sorted(edges.items()):
        if n >= max_edges:
            L.append("  %% ... %d more edges omitted" % (len(edges) - n))
            break
        n += 1
        label = "event" if kind == "event" else ("dep" if kind == "depends-on" else "")
        if meta["runtime"]:
            label = (label + " ✓") if label else "✓"
        arrow = "-. %s .->" % label if kind == "event" else (
            "== %s ==>" % label if meta["runtime"] else
            ("-- %s -->" % label if label else "-->"))
        L.append("  %s %s %s" % (_safe(src), arrow, _safe(dst)))
    L.append("```")
    L.append("")
    L.append("Legend: solid = synchronous call, dashed = event/topic, "
             "thick + ✓ = confirmed by runtime traces.")
    return "\n".join(L)


def flow_sequence(steps, title="flow"):
    """Sequence diagram from [(from, to, label), ...]."""
    L = ["```mermaid", "sequenceDiagram", "  autonumber"]
    seen = []
    for a, b, _ in steps:
        for x in (a, b):
            if x not in seen:
                seen.append(x)
    for p in seen:
        L.append("  participant %s" % _safe(p))
    for a, b, label in steps:
        L.append("  %s->>%s: %s" % (_safe(a), _safe(b), (label or "").replace(":", " ")))
    L.append("```")
    return "\n".join(L)


def _svc_name(node_id):
    # "cart . service order-svc"
    parts = (node_id or "").split(" ", 3)
    return parts[3] if len(parts) == 4 and parts[2] == "service" else None
