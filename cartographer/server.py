"""
Local web server for the visual map.

Stdlib only, no framework, no network: it opens on localhost, reads the same
`graph.db` the MCP server reads, and serves a self-contained UI. Nothing is
fetched from a CDN, which matters on a locked-down machine where outbound
requests to unpkg or jsdelivr simply fail.

It coexists with the MCP server rather than replacing it: Claude queries the
graph through MCP, you look at the same graph in a browser.
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .store import Store
from .analyze import hierarchy, impact, hotspots, repomap, pagerank
from .report import markdown as md_report

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


class GraphSource(object):
    """
    Opens the graph per request rather than holding one connection.

    SQLite handles are not safe to share across threads, and re-running `scan`
    replaces the file underneath us. Re-opening is cheap and means the page
    picks up a rescan without a restart.
    """

    def __init__(self, db_path, config_path=None):
        self.db_path = db_path
        self.config_path = config_path
        self._lock = threading.Lock()
        self._cfg = None

    def exists(self):
        return os.path.exists(self.db_path)

    def mtime(self):
        try:
            return os.path.getmtime(self.db_path)
        except OSError:
            return 0.0

    def open(self):
        return Store(self.db_path)

    def cfg(self):
        if self._cfg is None:
            from . import config as cfgmod
            self._cfg = (cfgmod.load(self.config_path) if self.config_path
                         else cfgmod.load(start=os.path.dirname(self.db_path)))
        return self._cfg


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    return str(o)


class Handler(BaseHTTPRequestHandler):
    server_version = "cartographer/" + __version__
    source = None          # set by serve()

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        pass                                   # quiet by default

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Local-only tool, but there is no reason to let a page in another tab
        # poke at it.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(self, obj):
        self._send(200, json.dumps(obj, default=_json_default))

    def _err(self, code, msg):
        self._send(code, json.dumps({"error": msg}))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        q = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api(path[5:], q)
            return self._static(path)
        except BrokenPipeError:
            pass
        except (ValueError, KeyError) as exc:
            # A bad query parameter is the caller's mistake, not a server
            # fault, and saying so is far more useful than a 500.
            self._err(400, "bad request: %s" % exc)
        except Exception as exc:              # never take the server down
            import traceback
            traceback.print_exc()
            self._err(500, "%s: %s" % (type(exc).__name__, exc))

    # -- static -----------------------------------------------------------

    def _static(self, path):
        if path in ("/", "/index.html"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        # Contain the path: never serve outside the ui directory.
        full = os.path.normpath(os.path.join(UI_DIR, rel))
        if not full.startswith(UI_DIR) or not os.path.isfile(full):
            return self._err(404, "not found: %s" % path)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- api --------------------------------------------------------------

    def _api(self, name, q):
        src = self.source
        one = lambda k, d=None: (q.get(k) or [d])[0]

        if name == "ping":
            return self._ok({"ok": True, "version": __version__,
                             "graph": src.exists(), "mtime": src.mtime()})

        if not src.exists():
            return self._err(503, "no graph at %s -- run `cartographer scan`"
                             % src.db_path)

        st = src.open()
        try:
            why = st.needs_rescan()
            if why and name not in ("ping",):
                return self._err(503, why)
            if name == "graph":
                include = one("include")
                inc = [x for x in include.split(",") if x] if include else None
                return self._ok(hierarchy.graph(
                    st, level=one("level", "estate"), focus=one("focus"),
                    package=one("package"), include=inc,
                    min_confidence=float(one("min_conf", "0") or 0),
                    limit=int(one("limit", "400"))))

            if name == "node":
                return self._ok(self._node_detail(st, one("id")))

            if name == "search":
                return self._ok(self._search(st, one("q", ""),
                                             int(one("limit", "40"))))

            if name == "edge":
                return self._ok(self._edge_detail(
                    st, one("source"), one("target"), one("kind"),
                    one("level", "estate"), one("focus")))

            if name == "impact":
                return self._ok(self._impact(st, one("target"),
                                             int(one("depth", "2"))))

            if name == "schema":
                return self._ok(self._schema(st))

            if name == "flow":
                return self._ok(self._flow(st, one("entity")))

            if name == "timeline":
                return self._ok(self._timeline(st))

            if name == "coverage":
                return self._ok(self._coverage(st))

            if name == "stats":
                c = st.counts()
                c["meta"] = st.all_meta()
                c["gaps_by_category"] = {
                    r["category"]: r["n"] for r in st.conn.execute(
                        "SELECT category, COUNT(*) n FROM gaps "
                        "GROUP BY category ORDER BY n DESC")}
                return self._ok(c)

            if name == "questions":
                return self._ok({"questions": md_report.suggest_questions(st)})

            if name == "hotspots":
                return self._ok({
                    "hotspots": hotspots.top(st, int(one("limit", "40"))),
                    "bus_factor": hotspots.bus_factor(st, 25),
                    "knowledge": hotspots.knowledge_map(st)})

            if name == "coupling":
                rows = [dict(r) for r in st.conn.execute(
                    "SELECT * FROM coupling ORDER BY cross_repo DESC, "
                    "degree DESC LIMIT ?", (int(one("limit", "80")),))]
                return self._ok({"coupling": rows})

            if name == "gaps":
                return self._ok({"gaps": [dict(r) for r in st.conn.execute(
                    "SELECT category, detail, hint, source FROM gaps "
                    "ORDER BY category, detail")]})

            if name == "repomap":
                m = repomap.build(st, task=one("task"),
                                  budget_tokens=int(one("budget", "2000")))
                return self._ok(m)

            return self._err(404, "unknown endpoint: %s" % name)
        finally:
            st.close()

    # -- api helpers ------------------------------------------------------

    def _node_detail(self, st, node_id):
        if not node_id:
            return {"error": "id required"}
        n = st.node(node_id)
        if not n:
            # Might be a synthetic group id from the hierarchy view.
            if node_id.startswith("pkg:"):
                _, svc, pkg = node_id.split(":", 2)
                return {"synthetic": True, "kind": "package", "label": pkg,
                        "service": svc,
                        "files": [dict(r) for r in st.conn.execute(
                            "SELECT id, name, file, line FROM nodes "
                            "WHERE kind='file' AND service=? AND file LIKE ? "
                            "ORDER BY file LIMIT 200", (svc, "%" + pkg + "%"))]}
            return {"error": "not found"}
        n = dict(n)
        try:
            n["meta"] = json.loads(n.pop("extra") or "{}")
        except (ValueError, TypeError):
            n["meta"] = {}
        rank = st.conn.execute("SELECT rank FROM ranks WHERE id=?",
                               (node_id,)).fetchone()
        n["rank"] = rank["rank"] if rank else 0.0

        def side(rows):
            out = []
            for r in rows:
                other = st.node(r["dst"] if r["src"] == node_id else r["src"])
                try:
                    ex = json.loads(r["extra"] or "{}")
                except (ValueError, TypeError):
                    ex = {}
                out.append({
                    "kind": r["kind"], "evidence": r["evidence"],
                    "provenance": r["provenance"], "confidence": r["confidence"],
                    "via": ex.get("via"), "runtime": bool(ex.get("runtime")),
                    "other_id": other["id"] if other else None,
                    "other_label": (other.get("name") if other else None)
                                   or (other.get("file") if other else None),
                    "other_kind": other["kind"] if other else None,
                    "other_service": other.get("service") if other else None})
            return out

        n["outgoing"] = side(st.out_edges(node_id))
        n["incoming"] = side(st.in_edges(node_id))

        if n.get("file") and n.get("repo"):
            fm = st.conn.execute(
                "SELECT * FROM file_metrics WHERE repo=? AND path=?",
                (n["repo"], n["file"])).fetchone()
            if fm:
                n["metrics"] = dict(fm)
            n["owners"] = hotspots.owners_for(st, n["repo"], n["file"])
            key = "%s/%s" % (n["repo"], n["file"])
            n["coupled"] = [dict(r) for r in st.conn.execute(
                "SELECT * FROM coupling WHERE a=? OR b=? "
                "ORDER BY degree DESC LIMIT 10", (key, key))]
        if n["kind"] in ("service", "library"):
            svc = n.get("name")
            n["contains"] = {r["kind"]: r["n"] for r in st.conn.execute(
                "SELECT kind, COUNT(*) n FROM nodes WHERE service=? "
                "GROUP BY kind", (svc,))}
            n["gaps"] = [dict(r) for r in st.conn.execute(
                "SELECT category, detail, hint FROM gaps WHERE detail LIKE ? "
                "LIMIT 12", ("%" + (svc or "") + "%",))]
        return n

    def _search(self, st, term, limit):
        term = (term or "").strip()
        if not term:
            return {"results": []}
        like = "%" + term + "%"
        rows = st.conn.execute(
            "SELECT n.id, n.kind, n.name, n.container, n.file, n.line, "
            "       n.service, COALESCE(r.rank,0) rank "
            "FROM nodes n LEFT JOIN ranks r ON r.id = n.id "
            "WHERE n.name LIKE ? COLLATE NOCASE OR n.file LIKE ? COLLATE NOCASE "
            "ORDER BY CASE WHEN n.name = ? COLLATE NOCASE THEN 0 ELSE 1 END, "
            "         rank DESC LIMIT ?", (like, like, term, limit))
        return {"results": [dict(r) for r in rows]}

    def _edge_detail(self, st, source, target, kind, level, focus):
        """Expand an aggregated edge back into the individual citations."""
        if not source or not target:
            return {"error": "source and target required"}
        nodes = {r["id"]: dict(r) for r in st.conn.execute("SELECT * FROM nodes")}
        member = {}
        for nid, n in nodes.items():
            if level == "estate":
                if n["kind"] in hierarchy.TOP_KINDS:
                    member[nid] = nid
                else:
                    svc = n.get("service") or n.get("repo")
                    member[nid] = "cart . service %s" % svc if svc else None
            else:
                if n["kind"] in hierarchy.TOP_KINDS:
                    member[nid] = nid
                elif (n.get("service") or n.get("repo")) == focus:
                    pkg = hierarchy._package_of(n, 2)
                    member[nid] = "pkg:%s:%s" % (focus, pkg) if pkg else nid
                else:
                    svc = n.get("service") or n.get("repo")
                    member[nid] = "cart . service %s" % svc if svc else None
        out = []
        for r in st.conn.execute("SELECT * FROM edges"):
            if kind and r["kind"] != kind:
                continue
            if member.get(r["src"]) != source or member.get(r["dst"]) != target:
                continue
            try:
                ex = json.loads(r["extra"] or "{}")
            except (ValueError, TypeError):
                ex = {}
            s, d = nodes.get(r["src"], {}), nodes.get(r["dst"], {})
            out.append({
                "kind": r["kind"], "evidence": r["evidence"],
                "provenance": r["provenance"], "confidence": r["confidence"],
                "via": ex.get("via"), "runtime": bool(ex.get("runtime")),
                "calls": ex.get("calls"),
                "src_id": r["src"], "dst_id": r["dst"],
                "src_label": s.get("name") or s.get("file"),
                "dst_label": d.get("name") or d.get("file"),
                "src_file": s.get("file"), "src_line": s.get("line"),
                "dst_file": d.get("file"), "dst_line": d.get("line")})
        out.sort(key=lambda e: (e["provenance"] != "EXTRACTED",
                                -(e["confidence"] or 0)))
        return {"source": source, "target": target, "kind": kind,
                "count": len(out), "edges": out[:300]}

    def _impact(self, st, target, depth):
        if not target:
            return {"error": "target required"}
        cands = impact.resolve(st, target)
        if not cands:
            return {"error": "no match for %r" % target, "candidates": []}
        res = impact.analyse(st, cands[0]["id"], depth=depth)
        return {
            "target": {"id": cands[0]["id"], "label": impact._label(cands[0]),
                       "kind": cands[0]["kind"], "file": cands[0].get("file"),
                       "line": cands[0].get("line"),
                       "service": cands[0].get("service")},
            "candidates": [{"id": c["id"], "label": impact._label(c),
                            "kind": c["kind"], "file": c.get("file")}
                           for c in cands[:10]],
            "services": {k: v for k, v in res["services"].items()},
            "direct": [{"label": impact._label(r["node"]), "id": r["node"]["id"],
                        "via": r["via"], "evidence": r["evidence"],
                        "provenance": r["provenance"],
                        "file": r["node"].get("file"),
                        "line": r["node"].get("line")}
                       for r in res["direct"]],
            "transitive": [{"label": impact._label(r["node"]),
                            "id": r["node"]["id"], "via": r["via"],
                            "evidence": r["evidence"],
                            "provenance": r["provenance"],
                            "distance": r["distance"]}
                           for r in res["transitive"][:80]],
            "contract": res["contract"], "coupling": res["coupling"],
            "owners": res["owners"], "invisible": res["invisible"],
            "notes": res["notes"],
            "text": impact.render(res, verbose=True)}

    def _schema(self, st):
        tables = []
        for r in st.conn.execute(
                "SELECT id, name, service, extra FROM nodes WHERE kind='table' "
                "ORDER BY name"):
            try:
                ex = json.loads(r["extra"] or "{}")
            except (ValueError, TypeError):
                ex = {}
            access = defaultdict(list)
            for e in st.conn.execute(
                    "SELECT src, kind, evidence FROM edges WHERE dst=? AND kind "
                    "IN ('defines-table','reads-table','writes-table')", (r["id"],)):
                access[e["kind"]].append({
                    "service": e["src"].split(" ", 3)[-1],
                    "evidence": e["evidence"]})
            readers = sorted({a["service"] for a in access["reads-table"]})
            writers = sorted({a["service"] for a in access["writes-table"]})
            owners = sorted({a["service"] for a in access["defines-table"]})
            tables.append({
                "id": r["id"], "name": r["name"], "owner": r["service"],
                "declared_at": ex.get("declared_at"),
                "migration": ex.get("migration_system"),
                "external": bool(ex.get("external")),
                "readers": readers, "writers": writers, "declared_by": owners,
                "touched_by": sorted(set(readers) | set(writers) | set(owners)),
                "access": {k: v for k, v in access.items()}})
        services = sorted({s for t in tables for s in t["touched_by"]})
        shared = [dict(r) for r in st.conn.execute(
            "SELECT detail, hint FROM gaps WHERE category='shared-table'")]
        return {"tables": tables, "services": services, "shared": shared,
                "datastores": [dict(r) for r in st.conn.execute(
                    "SELECT id, name FROM nodes WHERE kind='datastore' ORDER BY name")]}

    def _flow(self, st, entity):
        """
        A best-effort end-to-end path for one domain entity.

        Walks route -> service -> table -> topic -> consumer using only edges
        already in the graph, so every step is citable. Where a hop cannot be
        established it is reported as a break rather than bridged.
        """
        term = (entity or "order").strip()
        like = "%" + term + "%"
        steps, seen = [], set()

        routes = [dict(r) for r in st.conn.execute(
            "SELECT id, name, service FROM nodes WHERE kind='route' "
            "AND name LIKE ? COLLATE NOCASE ORDER BY name LIMIT 12", (like,))]
        for r in routes:
            steps.append({"stage": "entry", "kind": "route", "id": r["id"],
                          "label": r["name"], "service": r["service"]})
            seen.add(r["service"])

        frontends = [dict(r) for r in st.conn.execute(
            "SELECT DISTINCT e.src FROM edges e JOIN nodes n ON n.id=e.src "
            "WHERE e.kind='http' AND n.extra LIKE '%frontend%'")]
        for f in frontends:
            steps.insert(0, {"stage": "client", "kind": "service",
                             "id": f["src"], "label": f["src"].split(" ", 3)[-1],
                             "service": f["src"].split(" ", 3)[-1]})

        for svc in list(seen):
            for e in st.conn.execute(
                    "SELECT dst, kind, evidence FROM edges WHERE src=? AND kind "
                    "IN ('reads-table','writes-table')",
                    ("cart . service %s" % svc,)):
                steps.append({"stage": "data", "kind": "table", "id": e["dst"],
                              "label": e["dst"].split(" ", 3)[-1],
                              "service": svc, "access": e["kind"],
                              "evidence": e["evidence"]})
            for e in st.conn.execute(
                    "SELECT dst, evidence FROM edges WHERE src=? AND kind='produces'",
                    ("cart . service %s" % svc,)):
                topic = e["dst"].split(" ", 3)[-1]
                steps.append({"stage": "event", "kind": "topic", "id": e["dst"],
                              "label": topic, "service": svc,
                              "evidence": e["evidence"]})
                for c in st.conn.execute(
                        "SELECT dst, evidence FROM edges WHERE src=? "
                        "AND kind='consumed-by'", (e["dst"],)):
                    steps.append({"stage": "consumer", "kind": "service",
                                  "id": c["dst"],
                                  "label": c["dst"].split(" ", 3)[-1],
                                  "service": c["dst"].split(" ", 3)[-1],
                                  "topic": topic, "evidence": c["evidence"]})
        breaks = []
        for r in st.conn.execute(
                "SELECT detail FROM gaps WHERE category IN "
                "('unconsumed-topic','unproduced-topic') AND detail LIKE ?", (like,)):
            breaks.append(r["detail"])
        return {"entity": term, "steps": steps, "breaks": breaks}

    def _timeline(self, st):
        rows = [dict(r) for r in st.conn.execute(
            "SELECT month, service, SUM(commits) commits, SUM(files) files, "
            "SUM(added) added, SUM(deleted) deleted, MAX(authors) authors "
            "FROM timeline GROUP BY month, service ORDER BY month")]
        months = sorted({r["month"] for r in rows})
        services = sorted({r["service"] for r in rows})
        first_seen = {}
        for r in st.conn.execute(
                "SELECT repo, path, first_change FROM file_metrics "
                "WHERE first_change IS NOT NULL"):
            first_seen.setdefault(r["repo"], []).append(r["first_change"][:7])
        return {"months": months, "services": services, "rows": rows,
                "births": {k: min(v) for k, v in first_seen.items() if v}}

    def _coverage(self, st):
        """Be explicit about what is mapped, what is guessed, and what is blind."""
        c = st.counts()
        prov = c["edges_by_provenance"]
        runtime = st.conn.execute(
            "SELECT COUNT(*) n FROM edges WHERE extra LIKE '%\"runtime\": true%'"
        ).fetchone()["n"]
        blind = []
        for cat, why in (
            ("proxy-prefix-unmapped", "a frontend calls a service you have no repo for"),
            ("trace-service-unmapped", "traces mention a service missing from your config"),
            ("table-not-declared-locally", "a table is queried but its DDL is elsewhere"),
            ("unconsumed-topic", "an event is produced with no visible consumer"),
            ("unproduced-topic", "an event is consumed with no visible producer"),
            ("topic-constant-unreferenced", "a topic constant nothing on disk uses"),
            ("legacy-surface", "pages referenced whose source is in no repo here"),
            ("module-outside-reactor", "a service the parent build does not cover"),
            ("no-git-history", "a repo with no history, so no hotspots or ownership"),
            ("spec-owner-unknown", "a spec that names no configured service"),
        ):
            n = st.conn.execute("SELECT COUNT(*) n FROM gaps WHERE category=?",
                                (cat,)).fetchone()["n"]
            if n:
                blind.append({"category": cat, "count": n, "why": why,
                              "items": [r["detail"] for r in st.conn.execute(
                                  "SELECT detail FROM gaps WHERE category=? LIMIT 25",
                                  (cat,))]})
        services = [r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service' ORDER BY name")]
        norepo = [r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service' AND (repo IS NULL) "
            "ORDER BY name")]
        return {
            "extracted": prov.get("EXTRACTED", 0),
            "inferred": prov.get("INFERRED", 0),
            "runtime_confirmed": runtime,
            "services_total": len(services),
            "services_without_repo": norepo,
            "blind_spots": blind,
            "counts": c,
            "note": ("Absence of an edge is not proof of independence. "
                     "Dynamic dispatch, DI wiring, reflection, a service mesh "
                     "and config-driven routing are all invisible to static "
                     "scanning. Runtime traces close most of these gaps."),
        }


def serve(db_path, config_path=None, port=8787, host="127.0.0.1", open_browser=True):
    Handler.source = GraphSource(db_path, config_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/" % (host, port)
    print("cartographer UI  %s" % url)
    print("  graph: %s%s" % (db_path, "" if os.path.exists(db_path)
                             else "   (missing -- run `cartographer scan`)"))
    print("  Ctrl-C to stop")
    if open_browser:
        def _open():
            time.sleep(0.4)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
