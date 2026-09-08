"""
Runtime topology from distributed tracing.

This is the highest-quality evidence available anywhere, and most estates
already have it. A trace records that service A actually called service B, with
request counts and error rates -- measured, not inferred from a config string.

Supported inputs (all local files you export from your APM):

  * OpenTelemetry Collector `servicegraph` connector metrics (Prometheus text
    or JSON)
  * Jaeger `/api/dependencies` JSON            [{parent, child, callCount}]
  * Zipkin dependencies JSON                   [{parent, child, callCount}]
  * Grafana Tempo service-graph metrics (Prometheus text)
  * A plain CSV: source,target,calls[,errors]

Nothing is fetched from the network. You export the file, this reads it.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re

from .. import ids

SOURCE = "traces"

# traces_service_graph_request_total{client="order-svc",server="pricing-svc"} 12345
PROM = re.compile(
    r'^(?P<metric>[a-zA-Z_:][\w:]*)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+\-]+)')
LABEL = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

CLIENT_KEYS = ("client", "source", "src", "parent", "caller", "from",
               "client_service", "source_service")
SERVER_KEYS = ("server", "target", "dst", "child", "callee", "to",
               "server_service", "target_service", "destination_service")
ERROR_HINT = ("failed", "error", "errors")


def parse_prometheus(text):
    """Return {(client, server): {'calls': n, 'errors': n}}."""
    out = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PROM.match(line)
        if not m:
            continue
        metric = m.group("metric").lower()
        if "service_graph" not in metric and "servicegraph" not in metric:
            continue
        labels = dict(LABEL.findall(m.group("labels")))
        client = next((labels[k] for k in CLIENT_KEYS if labels.get(k)), None)
        server = next((labels[k] for k in SERVER_KEYS if labels.get(k)), None)
        if not client or not server or client == server:
            continue
        try:
            val = float(m.group("value"))
        except ValueError:
            continue
        rec = out.setdefault((client, server), {"calls": 0.0, "errors": 0.0})
        if any(h in metric for h in ERROR_HINT):
            rec["errors"] += val
        elif metric.endswith(("_total", "_count")) or "request" in metric:
            rec["calls"] += val
    return out


def parse_json_deps(data):
    """Jaeger / Zipkin dependency lists, and a few common wrappers."""
    out = {}
    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("data", "dependencies", "links", "edges", "result"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if not items:
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        client = next((it[k] for k in CLIENT_KEYS if it.get(k)), None)
        server = next((it[k] for k in SERVER_KEYS if it.get(k)), None)
        if not client or not server or client == server:
            continue
        calls = it.get("callCount") or it.get("calls") or it.get("count") or 1
        errs = it.get("errorCount") or it.get("errors") or 0
        try:
            calls = float(calls)
        except (TypeError, ValueError):
            calls = 1.0
        try:
            errs = float(errs)
        except (TypeError, ValueError):
            errs = 0.0
        rec = out.setdefault((str(client), str(server)), {"calls": 0.0, "errors": 0.0})
        rec["calls"] += calls
        rec["errors"] += errs
    return out


def parse_csv(text):
    out = {}
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        return out
    lower = {(f or "").strip().lower(): f for f in rdr.fieldnames}
    ck = next((lower[k] for k in CLIENT_KEYS if k in lower), None)
    sk = next((lower[k] for k in SERVER_KEYS if k in lower), None)
    if not ck or not sk:
        return out
    call_k = next((lower[k] for k in ("calls", "callcount", "count", "requests")
                   if k in lower), None)
    err_k = next((lower[k] for k in ("errors", "errorcount", "failed")
                  if k in lower), None)
    for row in rdr:
        client, server = (row.get(ck) or "").strip(), (row.get(sk) or "").strip()
        if not client or not server or client == server:
            continue
        try:
            calls = float(row.get(call_k) or 1) if call_k else 1.0
        except ValueError:
            calls = 1.0
        try:
            errs = float(row.get(err_k) or 0) if err_k else 0.0
        except ValueError:
            errs = 0.0
        rec = out.setdefault((client, server), {"calls": 0.0, "errors": 0.0})
        rec["calls"] += calls
        rec["errors"] += errs
    return out


def load_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return parse_json_deps(json.loads(text))
        except ValueError:
            pass
    got = parse_prometheus(text)
    if got:
        return got
    return parse_csv(text)


def run(store, cfg, paths, progress=None):
    """paths: list of exported trace/metric files."""
    store.clear_source(SOURCE)
    merged = {}
    used = []
    for p in paths:
        p = os.path.expanduser(p)
        if not os.path.isfile(p):
            store.add_gap("trace-file-missing", "no such file: %s" % p,
                          "export it from your APM first", SOURCE)
            continue
        got = load_file(p)
        if not got:
            store.add_gap("trace-file-unreadable",
                          "%s contained no recognisable service-graph data" % p,
                          "supported: OTel servicegraph metrics, Jaeger/Zipkin "
                          "dependencies JSON, or a source,target,calls CSV",
                          SOURCE)
            continue
        used.append(p)
        for k, v in got.items():
            rec = merged.setdefault(k, {"calls": 0.0, "errors": 0.0})
            rec["calls"] += v["calls"]
            rec["errors"] += v["errors"]

    # Resolve aliases FIRST, then merge. Two exports may name the same service
    # differently (pricing-service vs pricing-svc); merging before resolution
    # would leave their traffic counts split across two edges.
    resolved = {}
    unmapped = set()
    for (client, server), rec in merged.items():
        c = cfg.resolve_service(client)
        s = cfg.resolve_service(server)
        if c is None:
            unmapped.add(client)
            c = client
        if s is None:
            unmapped.add(server)
            s = server
        if c == s:
            continue
        acc = resolved.setdefault((c, s), {"calls": 0.0, "errors": 0.0})
        acc["calls"] += rec["calls"]
        acc["errors"] += rec["errors"]

    nodes, edges = [], []
    for (c, s), rec in resolved.items():
        for n in (c, s):
            nodes.append({"id": ids.service_id(n), "kind": "service", "name": n,
                          "service": n})
        err_rate = (rec["errors"] / rec["calls"]) if rec["calls"] else 0.0
        edges.append({
            "src": ids.service_id(c), "dst": ids.service_id(s),
            "kind": "http", "evidence": "runtime traces",
            # Observed traffic is the strongest evidence there is.
            "provenance": "EXTRACTED", "confidence": 1.0,
            "weight": 1.0,
            "extra": {"via": "observed in traces", "runtime": True,
                      "calls": rec["calls"], "errors": rec["errors"],
                      "error_rate": round(err_rate, 4)}})
    for u in sorted(unmapped):
        store.add_gap("trace-service-unmapped",
                      "traces mention '%s', which is not in your service config" % u,
                      "add it to services -- you may not have its repo", SOURCE)
    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"files": len(used), "edges": len(edges),
            "services_seen": len({n["name"] for n in nodes}),
            "unmapped": sorted(unmapped)}
