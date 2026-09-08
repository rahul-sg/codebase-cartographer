"""
Blast radius: if I change this, what else is affected?

Crosses both layers, which is the whole point. A symbol graph stops at the repo
boundary and a service map knows nothing about functions; joined, a change to a
method can be followed out through its service's consumers.

Three rules govern every answer:
  * inferred edges are never presented as facts
  * contract surface (routes, events, schemas) is called out first, because
    that is what breaks other teams
  * invisible repos are stated, not silently omitted
"""
from __future__ import annotations

import os
from collections import defaultdict

from .. import ids

# Edges that mean "depends on the target".
INBOUND_SYMBOL = ("calls", "imports", "extends")
INBOUND_SERVICE = ("http", "event", "depends-on")

CONTRACT_KINDS = ("route", "topic")


def resolve(store, query, limit=25):
    """
    Find what the user meant. Returns a list of candidate nodes, best first.

    Accepts a symbol name, a Class.method, a file path or fragment, a service
    name, or a full node id.
    """
    q = (query or "").strip()
    if not q:
        return []
    exact = store.node(q)
    if exact:
        return [exact]

    out, seen = [], set()

    def add(rows):
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(dict(r))

    container = name = None
    if "." in q and "/" not in q and not q.endswith((".java", ".py", ".ts", ".go", ".kt")):
        container, _, name = q.rpartition(".")

    if container and name:
        add(store.conn.execute(
            "SELECT * FROM nodes WHERE name=? COLLATE NOCASE AND container=? COLLATE NOCASE",
            (name, container)))
    add(store.conn.execute(
        "SELECT * FROM nodes WHERE name=? COLLATE NOCASE "
        "ORDER BY CASE kind WHEN 'service' THEN 0 WHEN 'class' THEN 1 "
        "WHEN 'function' THEN 2 ELSE 3 END", (q,)))
    add(store.conn.execute(
        "SELECT * FROM nodes WHERE file=? OR file LIKE ?", (q, "%" + q)))
    if len(out) < limit:
        add(store.conn.execute(
            "SELECT * FROM nodes WHERE name LIKE ? COLLATE NOCASE "
            "OR file LIKE ? COLLATE NOCASE LIMIT ?",
            ("%" + q + "%", "%" + q + "%", limit)))
    # Prefer higher-ranked candidates when the query is ambiguous.
    ranks = {r["id"]: r["rank"] for r in store.conn.execute("SELECT id, rank FROM ranks")}
    out.sort(key=lambda n: -ranks.get(n["id"], 0.0))
    return out[:limit]


def _service_of(store, node):
    if node.get("service"):
        return node["service"]
    if node.get("kind") == "service":
        return node.get("name")
    if node.get("repo"):
        return node["repo"]
    return None


def analyse(store, target_id, depth=2, include_coupling=True):
    """Full blast-radius record for one node."""
    node = store.node(target_id)
    if not node:
        return None

    result = {
        "target": node,
        "direct": [],          # immediate dependents (symbol layer)
        "transitive": [],      # further out, same layer
        "services": {},        # service -> why it is affected
        "contract": [],        # routes/topics that could break consumers
        "coupling": [],        # historical co-change
        "owners": [],          # who to ask / tag
        "invisible": [],       # boundaries of what we can see
        "notes": [],
    }

    # ---- symbol layer: walk inbound edges ----
    seen = {target_id}
    frontier = [target_id]
    level = 0
    while frontier and level < depth:
        nxt = []
        for nid in frontier:
            for e in store.in_edges(nid, INBOUND_SYMBOL):
                src = e["src"]
                if src in seen:
                    continue
                seen.add(src)
                n = store.node(src)
                if not n:
                    continue
                rec = {"node": n, "via": e["kind"], "evidence": e["evidence"],
                       "provenance": e["provenance"],
                       "confidence": e["confidence"], "distance": level + 1}
                (result["direct"] if level == 0 else result["transitive"]).append(rec)
                nxt.append(src)
        frontier = nxt
        level += 1

    # ---- which services are implicated ----
    origin = _service_of(store, node)
    if origin:
        result["services"][origin] = [{"reason": "the change itself lives here",
                                       "kind": "origin"}]
    for rec in result["direct"] + result["transitive"]:
        svc = _service_of(store, rec["node"])
        if not svc:
            continue
        result["services"].setdefault(svc, []).append({
            "reason": "%s %s" % (rec["via"], _label(rec["node"])),
            "kind": "symbol", "evidence": rec["evidence"],
            "provenance": rec["provenance"]})

    # ---- service layer: who consumes the affected services ----
    for svc in list(result["services"].keys()):
        sid = ids.service_id(svc)
        for e in store.in_edges(sid, INBOUND_SERVICE):
            caller = store.node(e["src"])
            if not caller or caller.get("name") == svc:
                continue
            import json
            via = ""
            if e["extra"]:
                try:
                    x = json.loads(e["extra"])
                    via = x.get("via") or ""
                    if x.get("runtime"):
                        via += " (observed in traces: %.0f calls)" % x.get("calls", 0)
                except ValueError:
                    pass
            result["services"].setdefault(caller["name"], []).append({
                "reason": "calls %s via %s" % (svc, via or e["kind"]),
                "kind": "service", "evidence": e["evidence"],
                "provenance": e["provenance"], "confidence": e["confidence"]})

    # ---- contract surface ----
    for svc in result["services"]:
        sid = ids.service_id(svc)
        for e in store.out_edges(sid, ("exposes", "produces")):
            n = store.node(e["dst"])
            if n and n["kind"] in CONTRACT_KINDS:
                result["contract"].append({
                    "service": svc, "kind": n["kind"], "name": n["name"],
                    "evidence": e["evidence"],
                    "authoritative": "authoritative" in (e["extra"] or "")})

    # ---- historical coupling ----
    if include_coupling and node.get("file") and node.get("repo"):
        key = "%s/%s" % (node["repo"], node["file"])
        for r in store.conn.execute(
                "SELECT * FROM coupling WHERE a=? OR b=? ORDER BY degree DESC LIMIT 12",
                (key, key)):
            other = r["b"] if r["a"] == key else r["a"]
            result["coupling"].append({
                "path": other, "shared": r["shared"], "degree": r["degree"],
                "cross_repo": bool(r["cross_repo"])})

    # ---- owners ----
    repos = {node.get("repo")} | {rec["node"].get("repo")
                                  for rec in result["direct"] + result["transitive"]}
    repos.discard(None)
    counts = defaultdict(int)
    for repo in repos:
        for r in store.conn.execute(
                "SELECT author, SUM(commits) c FROM ownership WHERE repo=? "
                "GROUP BY author ORDER BY c DESC LIMIT 5", (repo,)):
            counts[r["author"]] += r["c"]
    if node.get("repo") and node.get("file"):
        for r in store.conn.execute(
                "SELECT author, commits FROM ownership WHERE repo=? AND path=? "
                "ORDER BY commits DESC LIMIT 5", (node["repo"], node["file"])):
            counts[r["author"]] += r["commits"] * 5   # file-level history wins
    result["owners"] = [{"author": a, "score": c}
                        for a, c in sorted(counts.items(), key=lambda kv: -kv[1])[:5]]

    # ---- what we cannot see ----
    for r in store.conn.execute(
            "SELECT detail FROM gaps WHERE category IN "
            "('unconsumed-topic','trace-service-unmapped','unknown-host') LIMIT 20"):
        for svc in result["services"]:
            if svc in r["detail"]:
                result["invisible"].append(r["detail"])
                break
    result["invisible"] = sorted(set(result["invisible"]))

    inferred = sum(1 for rec in result["direct"] + result["transitive"]
                   if rec["provenance"] == "INFERRED")
    if inferred:
        result["notes"].append(
            "%d of %d symbol dependents are INFERRED (name-matched, not "
            "scope-resolved). Verify before relying on them."
            % (inferred, len(result["direct"]) + len(result["transitive"])))
    if not result["direct"]:
        result["notes"].append(
            "No inbound symbol edges found. Either nothing depends on this, or "
            "the dependent lives in a repo not on disk, or it is reached "
            "dynamically (reflection, DI, config). Do not read this as 'safe'.")
    return result


def _label(n):
    if n["kind"] == "file":
        return n.get("file") or n.get("name") or "?"
    owner = (n.get("container") + ".") if n.get("container") else ""
    return "%s%s" % (owner, n.get("name") or "?")


def render(result, verbose=False):
    """Human-readable report."""
    if not result:
        return "not found"
    n = result["target"]
    L = []
    where = n.get("file") or ""
    if n.get("line"):
        where += ":%d" % n["line"]
    L.append("# Blast radius: %s" % _label(n))
    L.append("  kind=%s  service=%s  %s" % (n["kind"], n.get("service") or "-", where))
    L.append("")

    if result["contract"]:
        L.append("## Contract surface of affected services  (what breaks other teams)")
        seen = set()
        for c in result["contract"]:
            k = (c["service"], c["name"])
            if k in seen:
                continue
            seen.add(k)
            star = " [spec]" if c["authoritative"] else ""
            L.append("  %-14s %-6s %s%s" % (c["service"], c["kind"], c["name"], star))
        L.append("")

    L.append("## Services affected (%d)" % len(result["services"]))
    for svc, reasons in sorted(result["services"].items(),
                               key=lambda kv: (kv[1][0].get("kind") != "origin", kv[0])):
        head = reasons[0]
        tag = "ORIGIN" if head.get("kind") == "origin" else ""
        L.append("  %-16s %s" % (svc, tag))
        for r in reasons[:4 if not verbose else 20]:
            if r.get("kind") == "origin":
                continue
            prov = r.get("provenance", "")
            mark = "?" if prov == "INFERRED" else " "
            L.append("      %s %s   %s" % (mark, r["reason"][:78],
                                           r.get("evidence") or ""))
    L.append("")

    if result["direct"]:
        L.append("## Direct dependents (%d)" % len(result["direct"]))
        for rec in result["direct"][:20 if not verbose else 200]:
            mark = "?" if rec["provenance"] == "INFERRED" else " "
            L.append("  %s %-8s %-34s %s" % (mark, rec["via"],
                                             _label(rec["node"])[:34],
                                             rec["evidence"] or ""))
        L.append("")

    if result["transitive"] and verbose:
        L.append("## Transitive dependents (%d)" % len(result["transitive"]))
        for rec in result["transitive"][:60]:
            mark = "?" if rec["provenance"] == "INFERRED" else " "
            L.append("  %s d=%d %-8s %-30s %s" % (mark, rec["distance"], rec["via"],
                                                  _label(rec["node"])[:30],
                                                  rec["evidence"] or ""))
        L.append("")

    if result["coupling"]:
        L.append("## Historically changes together  (from git, not from code)")
        for c in result["coupling"][:8]:
            tag = "CROSS-REPO" if c["cross_repo"] else ""
            L.append("  %-58s %d commits, degree %.2f %s"
                     % (c["path"][:58], c["shared"], c["degree"], tag))
        L.append("")

    if result["owners"]:
        L.append("## Suggested reviewers  (git history, most relevant first)")
        for o in result["owners"]:
            L.append("  %s" % o["author"])
        L.append("")

    if result["invisible"]:
        L.append("## Limits of visibility")
        for i in result["invisible"][:6]:
            L.append("  - %s" % i)
        L.append("")

    if result["notes"]:
        L.append("## Caveats")
        for note in result["notes"]:
            L.append("  ! %s" % note)
    L.append("")
    L.append("Legend: '?' marks an INFERRED edge -- a lead to verify, not a fact.")
    return "\n".join(L)
