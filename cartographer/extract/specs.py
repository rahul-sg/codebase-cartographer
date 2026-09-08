"""
Contracts as ground truth: OpenAPI, AsyncAPI, protobuf.

Where a spec exists it beats every inference this tool makes, because a spec is
the agreed interface rather than a guess about one. Spec-derived nodes are
marked authoritative so reports can prefer them.
"""
from __future__ import annotations

import json
import os
import re

from .. import ids, miniyaml
from ..config import SKIP_DIRS

SOURCE = "specs"

SPEC_EXT = (".yaml", ".yml", ".json")
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _load(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    if len(text) > 4_000_000:
        return None
    try:
        if path.endswith(".json"):
            return json.loads(text)
        try:
            import yaml
            return yaml.safe_load(text)
        except ImportError:
            return miniyaml.loads(text)
    except Exception:
        return None


def _candidates(repo_root, follow=False):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            low = fn.lower()
            if low.endswith(SPEC_EXT) or low.endswith(".proto"):
                yield os.path.join(dirpath, fn)


def _looks_openapi(d):
    return isinstance(d, dict) and ("openapi" in d or "swagger" in d) and "paths" in d


def _looks_asyncapi(d):
    return isinstance(d, dict) and "asyncapi" in d and "channels" in d


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    nodes, edges = [], []
    unowned = []
    n_open = n_async = n_proto = 0
    follow = cfg.defaults.get("follow_symlinks", False)

    # Specs often live outside any repo (a top-level openapi/ directory), so
    # scan the configured roots too.
    scan = [(r, root, s) for r, root, s in repos]
    for root in cfg.roots:
        if not any(os.path.commonpath([root, rr]) == rr for _, rr, _ in repos if os.path.isdir(rr)):
            scan.append((os.path.basename(root.rstrip("/")) or "root", root, None))

    seen_files = set()
    for repo_name, repo_root, svc in scan:
        if not os.path.isdir(repo_root):
            continue
        for path in _candidates(repo_root, follow):
            rp = os.path.realpath(path)
            if rp in seen_files:
                continue
            seen_files.add(rp)
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            ev = "%s/%s" % (repo_name, rel)

            if path.endswith(".proto"):
                got = _parse_proto(path, ev, svc or repo_name)
                if got:
                    n_proto += 1
                    nodes.extend(got[0])
                    edges.extend(got[1])
                continue

            d = _load(path)
            if not isinstance(d, dict):
                continue

            if _looks_openapi(d):
                n_open += 1
                owner = _spec_service(cfg, d, rel, svc or repo_name)
                if owner is None:
                    unowned.append(("openapi", ev))
                    continue
                nodes.append({"id": ids.service_id(owner), "kind": "service",
                              "name": owner, "service": owner})
                for p, ops in (d.get("paths") or {}).items():
                    if not isinstance(ops, dict):
                        continue
                    for method, op in ops.items():
                        if method.lower() not in HTTP_METHODS:
                            continue
                        rid = "cart . route %s %s %s" % (owner, method.upper(), p)
                        summary = ""
                        if isinstance(op, dict):
                            summary = (op.get("summary") or op.get("operationId") or "")
                        nodes.append({"id": rid, "kind": "route",
                                      "name": "%s %s" % (method.upper(), p),
                                      "service": owner,
                                      "extra": {"authoritative": True,
                                                "summary": summary,
                                                "spec": ev}})
                        edges.append({"src": ids.service_id(owner), "dst": rid,
                                      "kind": "exposes", "evidence": ev,
                                      "provenance": "EXTRACTED", "confidence": 1.0,
                                      "extra": {"authoritative": True}})

            elif _looks_asyncapi(d):
                n_async += 1
                owner = _spec_service(cfg, d, rel, svc or repo_name)
                if owner is None:
                    # Still record the channels: knowing the topics exist is
                    # useful even when the owning service is unclear.
                    unowned.append(("asyncapi", ev))
                    for chan in (d.get("channels") or {}):
                        nodes.append({"id": "cart . topic %s" % chan,
                                      "kind": "topic", "name": chan,
                                      "extra": {"authoritative": True, "spec": ev}})
                    continue
                nodes.append({"id": ids.service_id(owner), "kind": "service",
                              "name": owner, "service": owner})
                for chan, body in (d.get("channels") or {}).items():
                    tid = "cart . topic %s" % chan
                    nodes.append({"id": tid, "kind": "topic", "name": chan,
                                  "extra": {"authoritative": True, "spec": ev}})
                    if isinstance(body, dict):
                        # AsyncAPI 2.x: `publish` is what the app receives,
                        # `subscribe` is what it sends. This trips up almost
                        # everyone, so it is spelled out here.
                        if "publish" in body:
                            edges.append({"src": tid, "dst": ids.service_id(owner),
                                          "kind": "consumed-by", "evidence": ev,
                                          "provenance": "EXTRACTED", "confidence": 1.0,
                                          "extra": {"authoritative": True}})
                        if "subscribe" in body:
                            edges.append({"src": ids.service_id(owner), "dst": tid,
                                          "kind": "produces", "evidence": ev,
                                          "provenance": "EXTRACTED", "confidence": 1.0,
                                          "extra": {"authoritative": True}})

    for kind, ev in unowned:
        store.add_gap("spec-owner-unknown",
                      "%s spec %s does not name a service in your config" % (kind, ev),
                      "set info.title to the service name, or add the service "
                      "to your config, so its contract attaches to the map",
                      SOURCE)
    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"openapi": n_open, "asyncapi": n_async, "proto": n_proto,
            "routes": sum(1 for n in nodes if n["kind"] == "route"),
            "topics": sum(1 for n in nodes if n["kind"] == "topic")}


def _spec_service(cfg, doc, rel, fallback):
    """
    Which service does this spec describe?

    Returns None when it cannot be determined. Inventing an owner from the
    containing directory produces a phantom service (a spec in a top-level
    openapi/ folder would become a service called "openapi"), and a phantom
    service corrupts every downstream diagram and query.
    """
    title = ""
    if isinstance(doc.get("info"), dict):
        title = doc["info"].get("title") or ""
    for cand in (title, os.path.basename(rel).rsplit(".", 1)[0], fallback):
        r = cfg.resolve_service(cand)
        if r:
            return r
    return None


_PROTO_SVC = re.compile(r"^\s*service\s+(\w+)")
_PROTO_RPC = re.compile(r"^\s*rpc\s+(\w+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)")


def _parse_proto(path, ev, owner):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None
    nodes, edges, cur = [], [], None
    for i, line in enumerate(lines, 1):
        m = _PROTO_SVC.match(line)
        if m:
            cur = m.group(1)
            nodes.append({"id": "cart . grpcservice %s" % cur, "kind": "route",
                          "name": "grpc %s" % cur, "service": owner,
                          "extra": {"authoritative": True, "spec": ev}})
            continue
        m = _PROTO_RPC.match(line)
        if m and cur:
            rid = "cart . rpc %s.%s" % (cur, m.group(1))
            nodes.append({"id": rid, "kind": "route",
                          "name": "rpc %s.%s" % (cur, m.group(1)),
                          "service": owner,
                          "extra": {"authoritative": True, "spec": ev,
                                    "request": m.group(2), "response": m.group(3)}})
            edges.append({"src": ids.service_id(owner), "dst": rid,
                          "kind": "exposes", "evidence": "%s:%d" % (ev, i),
                          "provenance": "EXTRACTED", "confidence": 1.0,
                          "extra": {"authoritative": True}})
    return (nodes, edges) if nodes else None
