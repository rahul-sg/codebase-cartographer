#!/usr/bin/env python3
"""
End-to-end test suite. Stdlib unittest only.

Run:  python3 tests/test_all.py
      python3 tests/test_all.py -v

Builds a synthetic multi-service estate, runs the full pipeline over it, and
asserts on the results. Everything the user will hit on day one is covered here,
including the ugly cases: invalid UTF-8, empty files, huge files, symlink loops,
vendored and generated directories, repos without git, and a missing graph.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from cartographer import config as C, ids, langs, miniyaml           # noqa: E402
from cartographer.store import Store                                  # noqa: E402
from cartographer.extract import symbols, topology, history, specs, \
    builddeps, traces                                                 # noqa: E402
from cartographer.analyze import pagerank, hotspots, impact, repomap   # noqa: E402
from cartographer.report import markdown as md_report, html as html_report, \
    mermaid                                                           # noqa: E402
import make_fixture                                                   # noqa: E402

CART = os.path.join(ROOT, "bin", "cartographer")

_STATE = {}


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="cart-test-")
    estate = os.path.join(tmp, "estate")
    make_fixture.build(estate)

    cfg_path = os.path.join(tmp, "cartographer.yaml")
    with open(cfg_path, "w") as fh:
        fh.write("roots:\n  - %s\n\nservices:\n" % estate)
        for name, aliases in (("order-svc", "[OrderService, ORDER_SVC]"),
                              ("pricing-svc", "[pricing-service]"),
                              ("catalog-svc", "[]"),
                              ("inventory-api", "[]"),
                              ("ml-svc", "[]")):
            fh.write("  - name: %s\n    repo: %s/%s\n    aliases: %s\n"
                     % (name, estate, name, aliases))
        fh.write("  - name: notification-svc\n    aliases: []\n")

    # trace exports in all three supported shapes
    prom = os.path.join(tmp, "otel.prom")
    with open(prom, "w") as fh:
        fh.write(
            '# TYPE traces_service_graph_request_total counter\n'
            'traces_service_graph_request_total{client="order-svc",server="pricing-service"} 100000\n'
            'traces_service_graph_request_failed_total{client="order-svc",server="pricing-service"} 250\n'
            'traces_service_graph_request_total{client="order-svc",server="ledger-svc"} 42\n')
    jae = os.path.join(tmp, "jaeger.json")
    with open(jae, "w") as fh:
        json.dump({"data": [{"parent": "order-svc", "child": "pricing-svc",
                             "callCount": 5000}]}, fh)
    csvf = os.path.join(tmp, "deps.csv")
    with open(csvf, "w") as fh:
        fh.write("source,target,calls,errors\ninventory-api,order-svc,300,4\n")

    cfg = C.load(cfg_path)
    triples = [(os.path.basename(r), r, cfg.service_for_repo(os.path.basename(r)))
               for r in C.discover_repos(cfg.roots)]
    st = Store(os.path.join(tmp, "graph.db"))
    stats = {
        "symbols": symbols.run(st, cfg, triples),
        "topology": topology.run(st, cfg, triples),
        "specs": specs.run(st, cfg, triples),
        "builddeps": builddeps.run(st, cfg, triples),
        "history": history.run(st, cfg, triples),
        "traces": traces.run(st, cfg, [prom, jae, csvf]),
    }
    hotspots.compute(st)
    pagerank.compute(st)
    st.commit()
    _STATE.update(tmp=tmp, estate=estate, cfg=cfg, cfg_path=cfg_path, st=st,
                  triples=triples, stats=stats, prom=prom)


def tearDownModule():
    try:
        _STATE["st"].close()
    except Exception:
        pass
    shutil.rmtree(_STATE.get("tmp", ""), ignore_errors=True)


def edges_of(st, kind):
    return [dict(r) for r in st.conn.execute(
        "SELECT * FROM edges WHERE kind=?", (kind,))]


def svc_pairs(st, kinds=("http", "event")):
    out = set()
    q = "SELECT src,dst,kind FROM edges WHERE kind IN (%s)" % ",".join("?" * len(kinds))
    for r in st.conn.execute(q, kinds):
        s = r["src"].split(" ", 3)
        d = r["dst"].split(" ", 3)
        if len(s) == 4 and len(d) == 4 and s[2] == "service" and d[2] == "service":
            out.add((s[3], d[3], r["kind"]))
    return out


# ----------------------------------------------------------------- units

class TestIds(unittest.TestCase):
    def test_roundtrip(self):
        sid = ids.symbol_id("r", "java", "a/B.java", "run", "function", "B")
        p = ids.parse(sid)
        self.assertEqual((p["repo"], p["lang"], p["path"], p["container"],
                          p["name"], p["kind"]),
                         ("r", "java", "a/B.java", "B", "run", "function"))

    def test_distinct_across_repos(self):
        a = ids.symbol_id("r1", "java", "B.java", "run", "function", "B")
        b = ids.symbol_id("r2", "java", "B.java", "run", "function", "B")
        self.assertNotEqual(a, b)

    def test_method_and_class_distinct(self):
        self.assertNotEqual(ids.symbol_id("r", "j", "f", "X", "function"),
                            ids.symbol_id("r", "j", "f", "X", "class"))

    def test_malformed_never_raises(self):
        for bad in (None, "", "garbage", 123, "cart only two"):
            ids.parse(bad)


class TestMiniYaml(unittest.TestCase):
    def test_nested(self):
        d = miniyaml.loads("a:\n  b:\n    - 1\n    - two\nc: true\n")
        self.assertEqual(d, {"a": {"b": [1, "two"]}, "c": True})

    def test_list_of_maps(self):
        d = miniyaml.loads("s:\n  - name: x\n    v: 1\n  - name: y\n    v: 2\n")
        self.assertEqual(d["s"], [{"name": "x", "v": 1}, {"name": "y", "v": 2}])

    def test_comment_and_url(self):
        d = miniyaml.loads('u: http://x/?a=1&b=2   # note\nq: "a # b"\n')
        self.assertEqual(d["u"], "http://x/?a=1&b=2")
        self.assertEqual(d["q"], "a # b")

    def test_rejects_anchors(self):
        with self.assertRaises(miniyaml.MiniYamlError):
            miniyaml.loads("a: &x 1")

    def test_empty(self):
        self.assertEqual(miniyaml.loads(""), {})


class TestAliases(unittest.TestCase):
    def setUp(self):
        self.cfg = C.Config({"services": [
            {"name": "order-svc", "repo": "/x/order-svc", "aliases": ["OrderService"]},
            {"name": "catalog-service", "repo": "/x/catalog"}]}, None, "/tmp")

    def test_variants(self):
        for token in ("order-svc", "orders", "order", "OrderService",
                      "ORDER_SERVICE_URL", "order_service", "OrderSvc",
                      "oms/order-svc:latest", "reg.io/oms/order-svc:1.2"):
            self.assertEqual(self.cfg.resolve_service(token), "order-svc", token)

    def test_plural(self):
        self.assertEqual(self.cfg.resolve_service("catalogs"), "catalog-service")

    def test_unknown_is_none(self):
        for token in ("", None, "totally-unrelated"):
            self.assertIsNone(self.cfg.resolve_service(token))


class TestLangs(unittest.TestCase):
    def test_strip_preserves_geometry(self):
        src = 'class A {\n  // x\n  String s = "class B {}";\n}\n'
        got = langs.strip_noise(src, "java")
        self.assertEqual(len(src.split("\n")), len(got.split("\n")))
        for a, b in zip(src.split("\n"), got.split("\n")):
            self.assertEqual(len(a), len(b))

    def test_strip_hides_strings_and_comments(self):
        got = langs.strip_noise('a = "class Fake"; // realGone()', "java")
        self.assertNotIn("Fake", got)
        self.assertNotIn("realGone", got)

    def test_python_docstring(self):
        got = langs.strip_noise('def f():\n    """def hidden(): pass"""\n', "python")
        self.assertIn("def f", got)
        self.assertNotIn("hidden", got)


class TestSymbolExtraction(unittest.TestCase):
    def _parse(self, fn, lang, src):
        d = tempfile.mkdtemp()
        p = os.path.join(d, fn)
        with open(p, "w") as fh:
            fh.write(src)
        try:
            n, e, _ = symbols.parse_file(p, lang, "r", d, "s")
            return {x["name"] for x in n if x["kind"] != "file"}, n, e
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_control_flow_false_positives(self):
        names, _, _ = self._parse("A.java", "java", '''public class A {
    void m() {
        if (x) { y(); } else if (z) { w(); }
        for (int i=0;i<2;i++) { }
        while (c()) { }
        do { } while (c());
        switch (v) { case 1: break; }
        try { f(); } catch (E e) { } finally { }
        synchronized (l) { }
    }
    void real() { }
}''')
        self.assertEqual(names, {"A", "m", "real"})

    def test_container_attribution(self):
        _, nodes, _ = self._parse("A.java", "java", '''public class Outer {
    void a() { }
    public static class Inner {
        void b() { }
    }
    void c() { }
}''')
        by = {n["name"]: n.get("container") for n in nodes if n["kind"] != "file"}
        self.assertEqual(by["a"], "Outer")
        self.assertEqual(by["b"], "Inner")
        self.assertEqual(by["c"], "Outer", "must pop back to the outer class")

    def test_python_nesting(self):
        _, nodes, _ = self._parse("a.py", "python",
                                  "class A:\n    def m(self): pass\ndef top(): pass\n")
        by = {n["name"]: n.get("container") for n in nodes if n["kind"] != "file"}
        self.assertEqual(by["m"], "A")
        self.assertIsNone(by["top"])

    def test_common_names_kept_as_definitions(self):
        names, _, _ = self._parse("B.java", "java",
                                  "public class B {\n  public X build() { return null; }\n"
                                  "  public Y get() { return null; }\n}")
        self.assertIn("build", names)
        self.assertIn("get", names)

    def test_extends_excludes_keywords(self):
        _, _, edges = self._parse("A.java", "java",
                                  "public class A extends B implements C, D { }")
        parents = {e["extra"]["parent"] for e in edges if e["kind"] == "extends"}
        self.assertEqual(parents, {"B", "C", "D"})

    def test_unreadable_file_returns_empty(self):
        n, e, d = symbols.parse_file("/nonexistent/x.java", "java", "r", "/", "s")
        self.assertEqual((n, e, d), ([], [], {}))


# --------------------------------------------------------- pipeline

class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.st = _STATE["st"]
        self.cfg = _STATE["cfg"]

    def test_generated_and_vendored_excluded(self):
        files = [r["file"] for r in self.st.conn.execute(
            "SELECT file FROM nodes WHERE kind='file'")]
        joined = " ".join(files)
        for bad in ("node_modules", "vendor/", "target/generated", "generated/"):
            self.assertNotIn(bad, joined, "should have skipped %s" % bad)

    def test_bad_encoding_survived(self):
        got = self.st.conn.execute(
            "SELECT COUNT(*) n FROM nodes WHERE file LIKE '%badenc%'").fetchone()["n"]
        self.assertGreater(got, 0, "invalid-UTF-8 file should still parse")

    def test_all_languages_present(self):
        langs_found = self.st.counts()["languages"]
        for lang in ("java", "python", "go"):
            self.assertIn(lang, langs_found)

    def test_http_edges_have_evidence(self):
        rows = edges_of(self.st, "http")
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(r["evidence"], "every edge must cite its source")

    def test_known_service_edges(self):
        pairs = svc_pairs(self.st)
        for expected in (("order-svc", "pricing-svc", "http"),
                         ("order-svc", "catalog-svc", "http"),
                         ("catalog-svc", "inventory-api", "http"),
                         ("order-svc", "pricing-svc", "event"),
                         ("order-svc", "ml-svc", "event")):
            self.assertIn(expected, pairs, "missing %s" % (expected,))

    def test_alias_resolution_in_topology(self):
        # "pricing-service" in a FeignClient must fold into pricing-svc.
        names = {r["name"] for r in self.st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service'")}
        self.assertIn("pricing-svc", names)
        self.assertNotIn("pricing-service", names)

    def test_dependency_only_service_created(self):
        names = {r["name"] for r in self.st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service'")}
        self.assertIn("notification-svc", names)

    def test_shared_datastore_flagged(self):
        details = [r["detail"] for r in self.st.conn.execute(
            "SELECT detail FROM gaps WHERE category='shared-datastore'")]
        self.assertTrue(details)
        self.assertIn("oms-db", details[0])

    def test_unconsumed_topic_flagged(self):
        details = " ".join(r["detail"] for r in self.st.conn.execute(
            "SELECT detail FROM gaps WHERE category='unconsumed-topic'"))
        self.assertIn("oms.order.cancelled", details)

    def test_topic_consumers_found_across_languages(self):
        cons = {r["dst"].split(" ", 3)[-1] for r in self.st.conn.execute(
            "SELECT dst FROM edges WHERE kind='consumed-by'")}
        # Java @KafkaListener and two Python KafkaConsumer(...) constructions
        self.assertTrue({"pricing-svc", "catalog-svc", "ml-svc"} <= cons, cons)

    def test_routes_keep_http_verbs(self):
        names = {r["name"] for r in self.st.conn.execute(
            "SELECT name FROM nodes WHERE kind='route'")}
        self.assertIn("POST /api/v1/orders", names)
        self.assertIn("DELETE /api/v1/orders/{id}", names)
        self.assertIn("GET /api/v1/orders/{id}", names)

    def test_spec_routes_marked_authoritative(self):
        rows = [r for r in self.st.conn.execute(
            "SELECT extra FROM nodes WHERE kind='route' AND extra IS NOT NULL")]
        self.assertTrue(any("authoritative" in (r["extra"] or "") for r in rows))

    def test_no_phantom_service_from_spec_dir(self):
        names = {r["name"] for r in self.st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service'")}
        for phantom in ("estate", "openapi", "asyncapi"):
            self.assertNotIn(phantom, names)

    def test_traces_merge_after_alias_resolution(self):
        # 100000 (prom, "pricing-service") + 5000 (jaeger, "pricing-svc")
        found = None
        for r in self.st.conn.execute(
                "SELECT extra FROM edges WHERE source='traces' AND extra IS NOT NULL"):
            x = json.loads(r["extra"])
            if x.get("calls") == 105000:
                found = x
        self.assertIsNotNone(found, "trace counts must merge across name spellings")
        self.assertEqual(found["error_rate"], round(250 / 105000.0, 4))

    def test_trace_only_service_present(self):
        names = {r["name"] for r in self.st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service'")}
        self.assertIn("ledger-svc", names,
                      "a service seen only in traces must still appear")

    def test_history_metrics(self):
        row = self.st.conn.execute(
            "SELECT * FROM file_metrics WHERE path LIKE '%OrderService.java'").fetchone()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["revisions"], 5)
        self.assertIsNotNone(row["main_author"])

    def test_cross_repo_coupling_found(self):
        rows = [dict(r) for r in self.st.conn.execute(
            "SELECT * FROM coupling WHERE cross_repo=1")]
        self.assertTrue(rows, "cross-repo coupling via shared ticket keys")
        joined = " ".join(r["a"] + r["b"] for r in rows)
        self.assertIn("PricingClient.java", joined)
        self.assertIn("PricingEngine.java", joined)

    def test_boring_files_excluded_from_history(self):
        rows = [r["path"] for r in self.st.conn.execute(
            "SELECT path FROM file_metrics")]
        self.assertNotIn("CHANGELOG.md", rows)

    def test_hotspots_ranked(self):
        top = hotspots.top(self.st, 3)
        self.assertTrue(top)
        self.assertIn("api.py", top[0]["path"])

    def test_bus_factor(self):
        rows = hotspots.bus_factor(self.st, 10)
        self.assertTrue(rows)
        self.assertTrue(all(r["author_share"] >= 0.8 for r in rows))

    def test_pagerank_sane(self):
        scores = {r["id"]: r["rank"] for r in self.st.conn.execute(
            "SELECT id, rank FROM ranks")}
        self.assertTrue(scores)
        self.assertAlmostEqual(sum(scores.values()), 1.0, places=3)
        self.assertTrue(all(v >= 0 for v in scores.values()))

    def test_personalisation_changes_ranking(self):
        g = pagerank.compute(self.st, kind="global")
        p = pagerank.compute(self.st, seeds=["pricing"], kind="task")
        gtop = [n["name"] for n in pagerank.top(self.st, g, limit=5)]
        ptop = [n["name"] for n in pagerank.top(self.st, p, limit=5)]
        self.assertNotEqual(gtop, ptop)
        self.assertTrue(any("Pricing" in (x or "") or "pricing" in (x or "")
                            for x in ptop), ptop)
        pagerank.compute(self.st, kind="global")   # restore

    def test_repomap_respects_budget(self):
        for budget in (200, 500, 1500, 4000):
            m = repomap.build(self.st, task="order submission pricing",
                              budget_tokens=budget)
            self.assertLessEqual(m["tokens"], budget,
                                 "budget %d exceeded" % budget)
            self.assertGreaterEqual(m["files"], 1)

    def test_repomap_seeded_flag(self):
        m = repomap.build(self.st, task="pricing engine total")
        self.assertTrue(m["seeded"])
        self.assertIn("Pricing", m["text"])

    def test_repomap_handles_no_match(self):
        m = repomap.build(self.st, task="zzz nothing matches qqq")
        self.assertFalse(m["seeded"])
        self.assertTrue(m["text"])

    def test_impact_resolves_and_reports(self):
        cands = impact.resolve(self.st, "computeTotal")
        self.assertTrue(cands)
        res = impact.analyse(self.st, cands[0]["id"])
        self.assertIsNotNone(res)
        text = impact.render(res, verbose=True)
        self.assertIn("Blast radius", text)
        self.assertIn("order-svc", text)

    def test_impact_marks_inferred(self):
        cands = impact.resolve(self.st, "computeTotal")
        res = impact.analyse(self.st, cands[0]["id"])
        self.assertTrue(any(r["provenance"] == "INFERRED"
                            for r in res["direct"] + res["transitive"]))
        self.assertIn("INFERRED", impact.render(res))

    def test_impact_function_level_caller(self):
        cands = impact.resolve(self.st, "computeTotal")
        res = impact.analyse(self.st, cands[0]["id"])
        names = {impact._label(r["node"]) for r in res["direct"]}
        self.assertIn("OrderService.submitOrder", names,
                      "calls must attribute to the enclosing function")

    def test_impact_unknown_target(self):
        self.assertEqual(impact.resolve(self.st, "definitely-not-here-xyz"), [])

    def test_impact_names_reviewers(self):
        cands = impact.resolve(self.st, "PricingClient")
        res = impact.analyse(self.st, cands[0]["id"])
        self.assertTrue(res["owners"])

    def test_reports_render(self):
        md = md_report.build(self.st, self.cfg)
        for section in ("Service topology", "Hotspots", "change together",
                        "Contract surface", "Questions worth asking"):
            self.assertIn(section, md)
        self.assertIn("```mermaid", md)

    def test_html_selfcontained(self):
        h = html_report.build(self.st)
        self.assertIn("<canvas", h)
        for external in ("http://", "https://", "src=\"//"):
            self.assertNotIn(external, h, "page must not reference the network")

    def test_mermaid_valid_shape(self):
        m = mermaid.service_graph(self.st)
        self.assertIn("graph LR", m)
        self.assertNotIn("--  -->", m)

    def test_suggested_questions(self):
        qs = md_report.suggest_questions(self.st)
        self.assertTrue(qs)
        self.assertTrue(any("oms-db" in q for q in qs))


# --------------------------------------------------------- MCP + CLI

class TestMCP(unittest.TestCase):
    def _rpc(self, requests, db=None, cfg=None):
        from cartographer.mcp.server import Server
        srv = Server(db or _STATE["st"].path, cfg or _STATE["cfg_path"])
        srv._store = _STATE["st"] if db is None else None
        srv._cfg = _STATE["cfg"] if cfg is None else None
        return [srv.handle(r) for r in requests]

    def test_initialize_and_list(self):
        got = self._rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertEqual(got[0]["result"]["serverInfo"]["name"], "cartographer")
        tools = got[1]["result"]["tools"]
        self.assertEqual(len(tools), 11)
        for t in tools:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_notification_gets_no_response(self):
        got = self._rpc([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(got, [None])

    def test_unknown_method(self):
        got = self._rpc([{"jsonrpc": "2.0", "id": 9, "method": "nope"}])
        self.assertIn("error", got[0])

    def test_every_tool_returns_text(self):
        from cartographer.mcp.server import Server
        srv = Server(_STATE["st"].path, _STATE["cfg_path"])
        srv._store = _STATE["st"]
        srv._cfg = _STATE["cfg"]
        args = {
            "repo_map": {"task": "order pricing"},
            "find_symbol": {"query": "computeTotal"},
            "blast_radius": {"target": "computeTotal"},
            "service_topology": {},
            "who_owns": {"path": "OrderService"},
            "hotspots": {},
            "coupled_files": {},
            "shortest_path": {"source": "OrderService", "target": "PricingEngine"},
            "contracts": {},
            "open_questions": {},
            "graph_stats": {},
        }
        for tool in Server.TOOLS:
            name = tool["name"]
            text = srv.call(name, args.get(name, {}))
            self.assertIsInstance(text, str, name)
            self.assertTrue(text.strip(), "%s returned nothing" % name)

    def test_tool_handles_empty_query_gracefully(self):
        from cartographer.mcp.server import Server
        srv = Server(_STATE["st"].path, _STATE["cfg_path"])
        srv._store = _STATE["st"]
        srv._cfg = _STATE["cfg"]
        self.assertIn("No match", srv.call("find_symbol", {"query": "zzz-none"}))
        self.assertIn("No node matches",
                      srv.call("blast_radius", {"target": "zzz-none"}))

    def test_missing_graph_is_clear_error(self):
        from cartographer.mcp.server import Server
        srv = Server(os.path.join(_STATE["tmp"], "nope.db"))
        with self.assertRaises(RuntimeError) as cm:
            srv.store()
        self.assertIn("cartographer scan", str(cm.exception))

    def test_stdio_stream_is_pure_json(self):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "graph_stats", "arguments": {}}}]
        env = dict(os.environ, PYTHONPATH=ROOT)
        p = subprocess.run(
            [sys.executable, "-m", "cartographer.cli", "--config",
             _STATE["cfg_path"], "serve-mcp"],
            input="\n".join(json.dumps(r) for r in reqs) + "\ngarbage\n",
            capture_output=True, text=True, timeout=90, env=env,
            cwd=os.path.dirname(_STATE["st"].path))
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        lines = [l for l in p.stdout.strip().split("\n") if l.strip()]
        self.assertEqual(len(lines), 3)
        for l in lines:
            json.loads(l)


class TestCLI(unittest.TestCase):
    def _run(self, *args, **kw):
        env = dict(os.environ, PYTHONPATH=ROOT)
        return subprocess.run([sys.executable, "-m", "cartographer.cli"] + list(args),
                              capture_output=True, text=True, timeout=180,
                              env=env, cwd=kw.get("cwd", _STATE["tmp"]))

    def test_full_cli_lifecycle(self):
        work = tempfile.mkdtemp(prefix="cart-cli-")
        try:
            r = self._run("init", "--root", _STATE["estate"], "--dir", work, cwd=work)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(os.path.join(work, "cartographer.yaml")))

            r = self._run("scan", "--trace", _STATE["prom"], cwd=work)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            for f in ("graph.db", "GRAPH_REPORT.md", "graph.html",
                      "topology.mmd", "graph.json"):
                self.assertTrue(os.path.exists(
                    os.path.join(work, ".cartographer", f)), f)

            with open(os.path.join(work, ".cartographer", "graph.json")) as fh:
                g = json.load(fh)
            self.assertTrue(g["nodes"] and g["edges"])

            for cmd in (["stats"], ["questions"], ["topology"], ["hotspots"],
                        ["coupling"], ["contracts"], ["report"], ["doctor"],
                        ["map", "order pricing"], ["find", "computeTotal"],
                        ["impact", "computeTotal", "--first"],
                        ["owns", "OrderService"],
                        ["path", "OrderService", "PricingEngine"]):
                r = self._run(*cmd, cwd=work)
                self.assertEqual(r.returncode, 0,
                                 "%s failed: %s" % (cmd, r.stderr[-600:]))
                self.assertTrue((r.stdout + r.stderr).strip(), cmd)

            # a second scan must be idempotent, not additive
            before = self._run("stats", cwd=work).stdout
            self._run("scan", "--trace", _STATE["prom"], cwd=work)
            after = self._run("stats", cwd=work).stdout
            n_before = [l for l in before.split("\n") if l.startswith("nodes:")][0]
            n_after = [l for l in after.split("\n") if l.startswith("nodes:")][0]
            self.assertEqual(n_before, n_after, "re-scan must be idempotent")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_query_without_graph_exits_cleanly(self):
        empty = tempfile.mkdtemp(prefix="cart-empty-")
        try:
            with open(os.path.join(empty, "cartographer.yaml"), "w") as fh:
                fh.write("roots:\n  - %s\n" % empty)
            r = self._run("stats", cwd=empty)
            self.assertEqual(r.returncode, 2)
            self.assertIn("cartographer scan", r.stderr)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_init_on_empty_dir(self):
        empty = tempfile.mkdtemp(prefix="cart-none-")
        try:
            r = self._run("init", "--root", empty, "--dir", empty, cwd=empty)
            self.assertIn("no repositories", (r.stderr + r.stdout).lower())
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_help_and_version(self):
        self.assertEqual(self._run("--version").returncode, 0)
        self.assertEqual(self._run("--help").returncode, 0)


class TestRobustness(unittest.TestCase):
    def test_repo_without_git(self):
        d = tempfile.mkdtemp(prefix="cart-nogit-")
        try:
            os.makedirs(os.path.join(d, "svc"))
            with open(os.path.join(d, "svc", "A.java"), "w") as fh:
                fh.write("public class A { void m() {} }")
            cfg = C.Config({"roots": [d]}, None, d)
            st = Store(os.path.join(d, "g.db"))
            triples = [("svc", os.path.join(d, "svc"), "svc")]
            symbols.run(st, cfg, triples)
            res = history.run(st, cfg, triples)
            self.assertIn("svc", res["repos_without_git"])
            self.assertGreater(st.counts()["nodes"], 0)
            hotspots.compute(st)
            pagerank.compute(st)
            self.assertTrue(md_report.build(st, cfg))
            st.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_empty_estate(self):
        d = tempfile.mkdtemp(prefix="cart-empty2-")
        try:
            cfg = C.Config({"roots": [d]}, None, d)
            st = Store(os.path.join(d, "g.db"))
            symbols.run(st, cfg, [])
            topology.run(st, cfg, [])
            hotspots.compute(st)
            self.assertEqual(pagerank.compute(st), {})
            self.assertTrue(md_report.build(st, cfg))
            self.assertTrue(html_report.build(st))
            m = repomap.build(st, task="anything")
            self.assertIn("empty", m["text"].lower())
            st.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_store_idempotent(self):
        d = tempfile.mkdtemp()
        try:
            st = Store(os.path.join(d, "g.db"))
            for _ in range(3):
                st.add_nodes([{"id": "a", "kind": "file"}], "s")
                st.add_edges([{"src": "a", "dst": "b", "kind": "calls",
                               "evidence": "x:1"}], "s")
            st.commit()
            self.assertEqual(st.counts()["edges"], 1)
            st.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_traces_bad_input(self):
        d = tempfile.mkdtemp()
        try:
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as fh:
                fh.write("this is not anything")
            st = Store(os.path.join(d, "g.db"))
            res = traces.run(st, _STATE["cfg"], [bad, "/nope/missing.json"])
            self.assertEqual(res["edges"], 0)
            self.assertEqual(st.counts()["gaps"], 2)
            st.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_symlink_loop_terminates(self):
        # The fixture contains loop/self -> loop; discovery must not hang.
        found = C.discover_repos([_STATE["estate"]])
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
