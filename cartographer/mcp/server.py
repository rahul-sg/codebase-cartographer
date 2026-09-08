"""
MCP stdio server for the cartographer graph.

Implements the JSON-RPC subset MCP needs (initialize, tools/list, tools/call)
directly over stdin/stdout. No SDK, no dependencies: this has to start on a
machine where you may not be able to pip install anything.

Protocol notes that matter in practice:
  * one JSON object per line on stdout, nothing else ever
  * every diagnostic goes to stderr, because a stray print on stdout corrupts
    the stream and the client silently disconnects
  * notifications (no "id") get no response
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from .. import __version__, config as cfgmod, ids
from ..store import Store
from ..analyze import pagerank, repomap, impact, hotspots
from ..report import markdown as md_report, mermaid

PROTOCOL_VERSION = "2024-11-05"


def _log(msg):
    sys.stderr.write("[cartographer-mcp] %s\n" % msg)
    sys.stderr.flush()


class Server(object):
    def __init__(self, db_path, config_path=None):
        self.db_path = db_path
        self.config_path = config_path
        self._store = None
        self._cfg = None

    # -- lazy resources so a missing graph is a tool error, not a crash ----

    def store(self):
        if self._store is None:
            if not os.path.exists(self.db_path):
                raise RuntimeError(
                    "no graph at %s -- run `cartographer scan` first"
                    % self.db_path)
            self._store = Store(self.db_path)
        return self._store

    def cfg(self):
        if self._cfg is None:
            self._cfg = cfgmod.load(self.config_path) if self.config_path \
                else cfgmod.load(start=os.path.dirname(self.db_path))
        return self._cfg

    # -- tool definitions --------------------------------------------------

    TOOLS = [
        {"name": "repo_map",
         "description": "Ranked map of the estate for a task, fitted to a token "
                        "budget. Use this FIRST on any unfamiliar area: it tells "
                        "you which files matter before you read any of them.",
         "inputSchema": {"type": "object", "properties": {
             "task": {"type": "string", "description": "what you are trying to do"},
             "budget_tokens": {"type": "integer", "default": 2000}},
             "required": ["task"]}},

        {"name": "find_symbol",
         "description": "Locate a class, function, file, service, route or topic "
                        "by name or fragment. Returns file:line for each match.",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "kind": {"type": "string", "description":
                      "optional: class|function|file|service|route|topic|datastore"},
             "limit": {"type": "integer", "default": 20}},
             "required": ["query"]}},

        {"name": "blast_radius",
         "description": "What breaks if this changes. Crosses both the symbol "
                        "graph and the service topology, names the contract "
                        "surface at risk, and suggests reviewers from git "
                        "history. Use before editing anything unfamiliar, and "
                        "when writing a PR description.",
         "inputSchema": {"type": "object", "properties": {
             "target": {"type": "string", "description":
                        "symbol, Class.method, file path, or service name"},
             "depth": {"type": "integer", "default": 2}},
             "required": ["target"]}},

        {"name": "service_topology",
         "description": "The cross-service map: who calls whom, over HTTP and "
                        "events, with evidence. Optionally scoped to one service.",
         "inputSchema": {"type": "object", "properties": {
             "service": {"type": "string"},
             "format": {"type": "string", "enum": ["text", "mermaid"],
                        "default": "text"}}}},

        {"name": "who_owns",
         "description": "Who has actually worked on this code, from git history. "
                        "Use to pick reviewers or decide who to ask.",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "file path or fragment"},
             "limit": {"type": "integer", "default": 8}},
             "required": ["path"]}},

        {"name": "hotspots",
         "description": "Files ranked by churn x complexity -- where defects "
                        "concentrate. Optionally scoped to one repo.",
         "inputSchema": {"type": "object", "properties": {
             "repo": {"type": "string"}, "limit": {"type": "integer", "default": 15}}}},

        {"name": "coupled_files",
         "description": "Files that historically change together, including "
                        "ACROSS repositories. This finds dependencies no static "
                        "analysis can see. Check before assuming a change is "
                        "contained to one service.",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "optional: scope to one file"},
             "cross_repo_only": {"type": "boolean", "default": False},
             "limit": {"type": "integer", "default": 20}}}},

        {"name": "shortest_path",
         "description": "How two things are connected: the dependency chain "
                        "between two symbols or services, if one exists.",
         "inputSchema": {"type": "object", "properties": {
             "source": {"type": "string"}, "target": {"type": "string"},
             "max_depth": {"type": "integer", "default": 6}},
             "required": ["source", "target"]}},

        {"name": "contracts",
         "description": "HTTP routes, RPCs and event topics a service exposes, "
                        "and who consumes them. Spec-derived entries are marked "
                        "authoritative.",
         "inputSchema": {"type": "object", "properties": {
             "service": {"type": "string"}}}},

        {"name": "open_questions",
         "description": "What the scan could NOT determine, phrased as questions "
                        "to ask a teammate. These are the highest-value things "
                        "to ask, because they are the knowledge nobody wrote down.",
         "inputSchema": {"type": "object", "properties": {}}},

        {"name": "graph_stats",
         "description": "What is in the graph and when it was built. Call this "
                        "if results look stale or empty.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]

    # -- dispatch ----------------------------------------------------------

    def handle(self, req):
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cartographer", "version": __version__}})
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._ok(rid, {})
        if method == "tools/list":
            return self._ok(rid, {"tools": self.TOOLS})
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                text = self.call(name, args)
            except Exception as exc:                       # tool-level failure
                _log("tool %s failed: %s" % (name, traceback.format_exc()))
                return self._ok(rid, {"isError": True, "content":
                                      [{"type": "text", "text": "Error: %s" % exc}]})
            return self._ok(rid, {"content": [{"type": "text", "text": text}]})
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            return self._ok(rid, {key: []})
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found: %s" % method}}

    @staticmethod
    def _ok(rid, result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    # -- tools -------------------------------------------------------------

    def call(self, name, a):
        st = self.store()
        if name == "repo_map":
            m = repomap.build(st, task=a.get("task"),
                              budget_tokens=int(a.get("budget_tokens") or 2000))
            return m["text"] + (
                "\n\n(%d tokens, %d of %d files; %s)"
                % (m["tokens"], m["files"], m.get("total_files", 0),
                   "focused on your task" if m["seeded"] else
                   "global ranking -- no match for your task terms"))

        if name == "find_symbol":
            rows = st.find_nodes(name=a.get("query"), kind=a.get("kind"),
                                 limit=int(a.get("limit") or 20))
            if not rows:
                return ("No match for %r.\nTry a fragment, or call graph_stats "
                        "to check the graph is populated." % a.get("query"))
            out = ["%d match(es) for %r:" % (len(rows), a.get("query")), ""]
            for r in rows:
                owner = (r["container"] + ".") if r["container"] else ""
                loc = "%s:%s" % (r["file"], r["line"]) if r["file"] else "-"
                out.append("  %-9s %-34s %-14s %s"
                           % (r["kind"], (owner + (r["name"] or ""))[:34],
                              r["service"] or r["repo"] or "-", loc))
            return "\n".join(out)

        if name == "blast_radius":
            cands = impact.resolve(st, a.get("target"))
            if not cands:
                return "No node matches %r." % a.get("target")
            body = impact.render(impact.analyse(
                st, cands[0]["id"], depth=int(a.get("depth") or 2)), verbose=True)
            if len(cands) > 1:
                body += "\n\nNote: %d nodes matched; analysed the highest-ranked." \
                        "\nOthers: %s" % (
                            len(cands),
                            ", ".join("%s (%s)" % (c["name"], c["file"] or c["kind"])
                                      for c in cands[1:5]))
            return body

        if name == "service_topology":
            if (a.get("format") or "text") == "mermaid":
                return mermaid.service_graph(st)
            return self._topology_text(st, a.get("service"))

        if name == "who_owns":
            q = a.get("path") or ""
            rows = list(st.conn.execute(
                "SELECT repo, path, main_author, author_share, revisions, authors "
                "FROM file_metrics WHERE path LIKE ? ORDER BY revisions DESC LIMIT 10",
                ("%" + q + "%",)))
            if not rows:
                return ("No version-control history for %r. The repo may not be "
                        "a git checkout, or the path may be wrong." % q)
            out = []
            for r in rows:
                out.append("%s/%s  (%d revisions, %d authors)"
                           % (r["repo"], r["path"], r["revisions"], r["authors"]))
                for o in hotspots.owners_for(st, r["repo"], r["path"],
                                             int(a.get("limit") or 8)):
                    out.append("    %-22s %d commits, last %s"
                               % (o["author"], o["commits"], (o["last"] or "")[:10]))
            return "\n".join(out)

        if name == "hotspots":
            rows = hotspots.top(st, int(a.get("limit") or 15), a.get("repo"))
            if not rows:
                return "No hotspot data. Run a scan with git history available."
            out = ["Churn x complexity. High score = complicated code that "
                   "changes constantly.", ""]
            for h in rows:
                out.append("  %.4f  rev=%-4d cx=%-5d authors=%-2d  %s/%s"
                           % (h["hotspot"], h["revisions"], int(h["complexity"]),
                              h["authors"], h["repo"], h["path"]))
            return "\n".join(out)

        if name == "coupled_files":
            sql = "SELECT * FROM coupling WHERE 1=1"
            args = []
            if a.get("cross_repo_only"):
                sql += " AND cross_repo=1"
            if a.get("path"):
                sql += " AND (a LIKE ? OR b LIKE ?)"
                args += ["%" + a["path"] + "%"] * 2
            sql += " ORDER BY cross_repo DESC, degree DESC LIMIT ?"
            args.append(int(a.get("limit") or 20))
            rows = list(st.conn.execute(sql, args))
            if not rows:
                return ("No coupling found for that query. Either the files do "
                        "not co-change, or history is too short.")
            out = ["Files that change together (from git history, not code):", ""]
            for r in rows:
                out.append("  %s  shared=%-3d degree=%.2f\n    %s\n    %s"
                           % ("CROSS-REPO" if r["cross_repo"] else "in-repo   ",
                              r["shared"], r["degree"], r["a"], r["b"]))
            return "\n".join(out)

        if name == "shortest_path":
            return self._path(st, a.get("source"), a.get("target"),
                              int(a.get("max_depth") or 6))

        if name == "contracts":
            return self._contracts(st, a.get("service"))

        if name == "open_questions":
            qs = md_report.suggest_questions(st)
            return "\n".join("- %s" % q for q in qs)

        if name == "graph_stats":
            c = st.counts()
            meta = st.all_meta()
            out = ["Graph: %s" % self.db_path,
                   "Built: %s" % meta.get("scanned_at", "unknown"),
                   "Repos: %s" % (", ".join(c["repos"]) or "none"),
                   "Services: %s" % (", ".join(x for x in c["services"] if x) or "none"),
                   "Nodes: %d %s" % (c["nodes"], c["nodes_by_kind"]),
                   "Edges: %d %s" % (c["edges"], c["edges_by_kind"]),
                   "Provenance: %s" % c["edges_by_provenance"],
                   "Languages: %s" % c["languages"],
                   "Open gaps: %d" % c["gaps"]]
            return "\n".join(out)

        return "Unknown tool: %s" % name

    # -- helpers -----------------------------------------------------------

    def _topology_text(self, st, service=None):
        cfg = self.cfg()
        canon = cfg.resolve_service(service) if service else None
        if service and not canon:
            canon = service
        out = []
        rows = list(st.conn.execute(
            "SELECT src, dst, kind, evidence, provenance, confidence, extra "
            "FROM edges WHERE kind IN ('http','event','depends-on')"))
        seen = {}
        for r in rows:
            s = _svc(r["src"])
            d = _svc(r["dst"])
            if not s or not d:
                continue
            if canon and canon not in (s, d):
                continue
            key = (s, d, r["kind"])
            rec = seen.setdefault(key, {"ev": [], "runtime": False, "conf": 0})
            rec["conf"] = max(rec["conf"], r["confidence"] or 0)
            if r["extra"]:
                try:
                    x = json.loads(r["extra"])
                    if x.get("runtime"):
                        rec["runtime"] = True
                        rec["calls"] = x.get("calls")
                    if x.get("via"):
                        rec["ev"].append("%s (%s)" % (x["via"], r["evidence"] or ""))
                except ValueError:
                    pass
            elif r["evidence"]:
                rec["ev"].append(r["evidence"])
        if not seen:
            return ("No service edges%s. Either the scan found none, or this "
                    "service is reached in a way the scan cannot see (mesh, "
                    "gateway, batch)." % (" for %s" % canon if canon else ""))
        header = "Service topology" + (" for %s" % canon if canon else "")
        out.append(header)
        out.append("")
        for (s, d, kind), rec in sorted(seen.items()):
            tag = "  [runtime-confirmed, %.0f calls]" % rec["calls"] \
                if rec.get("runtime") and rec.get("calls") else ""
            out.append("  %-6s %-16s -> %-16s conf=%.2f%s"
                       % (kind, s, d, rec["conf"], tag))
            for e in rec["ev"][:3]:
                out.append("         %s" % e[:110])
        out.append("")
        out.append("Note: an edge to a service whose repo is not on disk is "
                   "still real. Absence of an edge is not proof of independence.")
        return "\n".join(out)

    def _contracts(self, st, service=None):
        cfg = self.cfg()
        canon = cfg.resolve_service(service) if service else None
        out = []
        sql = "SELECT name, service, extra FROM nodes WHERE kind='route'"
        args = []
        if canon:
            sql += " AND service=?"
            args.append(canon)
        sql += " ORDER BY service, name"
        rows = list(st.conn.execute(sql, args))
        if rows:
            out.append("## Endpoints")
            cur = None
            for r in rows:
                if r["service"] != cur:
                    cur = r["service"]
                    out.append("")
                    out.append("%s:" % (cur or "unknown"))
                auth = "  [from spec]" if r["extra"] and "authoritative" in r["extra"] else ""
                out.append("  %s%s" % (r["name"], auth))
        trows = list(st.conn.execute("SELECT id, name FROM nodes WHERE kind='topic' ORDER BY name"))
        if trows:
            out.append("")
            out.append("## Events")
            for t in trows:
                prod = [_svc(x["src"]) for x in st.conn.execute(
                    "SELECT src FROM edges WHERE dst=? AND kind='produces'", (t["id"],))]
                cons = [_svc(x["dst"]) for x in st.conn.execute(
                    "SELECT dst FROM edges WHERE src=? AND kind='consumed-by'", (t["id"],))]
                prod = [p for p in prod if p]
                cons = [c for c in cons if c]
                if canon and canon not in prod and canon not in cons:
                    continue
                out.append("  %-34s produced by %-24s consumed by %s"
                           % (t["name"], ", ".join(prod) or "(nobody visible)",
                              ", ".join(cons) or "(nobody visible)"))
        return "\n".join(out) if out else "No contracts recorded."

    def _path(self, st, source, target, max_depth):
        src = impact.resolve(st, source)
        dst = impact.resolve(st, target)
        if not src:
            return "No node matches source %r." % source
        if not dst:
            return "No node matches target %r." % target
        def expand(node):
            """A symbol and the file that defines it are the same place."""
            out = {node["id"]}
            if node.get("kind") in ("class", "function") and node.get("file"):
                for r in st.conn.execute(
                        "SELECT id FROM nodes WHERE kind='file' AND file=? AND repo=?",
                        (node["file"], node["repo"])):
                    out.add(r["id"])
            elif node.get("kind") == "file":
                for r in st.conn.execute(
                        "SELECT id FROM nodes WHERE file=? AND repo=? "
                        "AND kind IN ('class','function')",
                        (node["file"], node["repo"])):
                    out.add(r["id"])
            return out

        goal = set()
        for d in dst[:5]:
            goal |= expand(d)
        from collections import deque
        starts = expand(src[0])
        start = src[0]["id"]
        prev = {s: None for s in starts}
        q = deque([(s, 0) for s in starts])
        found = None
        while q:
            nid, depth = q.popleft()
            if nid in goal and nid not in starts:
                found = nid
                break
            if depth >= max_depth:
                continue
            for e in st.out_edges(nid):
                if e["dst"] not in prev:
                    prev[e["dst"]] = (nid, e["kind"], e["evidence"], e["provenance"])
                    q.append((e["dst"], depth + 1))
        if not found:
            return ("No path from %r to %r within %d hops.\nThat is not proof "
                    "they are unrelated -- the link may be dynamic, or in a "
                    "repo not on disk." % (source, target, max_depth))
        chain = []
        cur = found
        while prev.get(cur):
            parent, kind, ev, prov = prev[cur]
            chain.append((parent, kind, cur, ev, prov))
            cur = parent
        chain.reverse()
        out = ["Path from %s to %s (%d hops):"
               % (impact._label(src[0]), impact._label(st.node(found)), len(chain)), ""]
        for parent, kind, child, ev, prov in chain:
            mark = "?" if prov == "INFERRED" else " "
            out.append("  %s %-32s --%s--> %-32s   %s"
                       % (mark, impact._label(st.node(parent))[:32], kind,
                          impact._label(st.node(child))[:32], ev or ""))
        out.append("")
        out.append("'?' marks an INFERRED edge -- verify it.")
        return "\n".join(out)


def _svc(node_id):
    parts = (node_id or "").split(" ", 3)
    return parts[3] if len(parts) == 4 and parts[2] == "service" else None


def serve(db_path, config_path=None):
    srv = Server(db_path, config_path)
    _log("ready (db=%s)" % db_path)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _log("dropped non-JSON line")
            continue
        try:
            resp = srv.handle(req)
        except Exception:
            _log(traceback.format_exc())
            rid = req.get("id") if isinstance(req, dict) else None
            if rid is None:
                continue
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": "internal error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    _log("stdin closed, exiting")
    return 0
