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
            n, e, _, sup, imp = symbols.parse_file(p, lang, "r", d, "s")
            self._last_imports = imp
            self._last_pending = sup
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
        # parse_file defers supertypes rather than emitting edges: whether a
        # parent is first-party or third-party is unknowable until the whole
        # index exists. resolve_supertypes turns these into edges.
        self._parse("A.java", "java",
                    "public class A extends B implements C, D { }")
        parents = {name for _src, name, _line in self._last_pending}
        self.assertEqual(parents, {"B", "C", "D"})

    def test_supertype_resolves_to_first_party_type(self):
        idx = {"Service": ["cart r java Svc.java`Service#"]}
        edges, gaps = symbols.resolve_supertypes(
            [("cart r java Impl.java`Impl#", "Service", 3, "Impl.java", "r")],
            idx, {"cart r java Svc.java`Service#": "r"})
        # Both directions: `extends` for the hierarchy, and the reciprocal
        # `implemented-by` so directed traversal can reach the implementation.
        by_kind = {e["kind"]: e for e in edges}
        self.assertEqual(set(by_kind), {"extends", "implemented-by"})
        self.assertEqual(by_kind["extends"]["src"], "cart r java Impl.java`Impl#")
        self.assertEqual(by_kind["extends"]["dst"], "cart r java Svc.java`Service#")
        self.assertEqual(by_kind["implemented-by"]["src"],
                         "cart r java Svc.java`Service#")
        self.assertEqual(by_kind["implemented-by"]["dst"],
                         "cart r java Impl.java`Impl#")
        self.assertTrue(by_kind["extends"]["extra"]["first_party"])
        self.assertEqual(gaps, [])

    def test_import_resolves_to_first_party_type_by_package(self):
        # A fully-qualified import matches on PACKAGE PATH, not just the simple
        # name, so a same-named type in another package is not picked.
        idx = {"OrderService": [
            "cart r java src/main/java/com/acme/agent/svc/OrderService.java`OrderService#",
            "cart r java src/main/java/com/other/pkg/OrderService.java`OrderService#"]}
        files = {
            "cart r java src/main/java/com/acme/agent/svc/OrderService.java`OrderService#":
                "src/main/java/com/acme/agent/svc/OrderService.java",
            "cart r java src/main/java/com/other/pkg/OrderService.java`OrderService#":
                "src/main/java/com/other/pkg/OrderService.java"}
        edges = symbols.resolve_imports(
            [("cart r java Ctl.java`", "com.acme.agent.svc.OrderService",
              7, "Ctl.java", "r")], idx, files)
        self.assertEqual(len(edges), 1)
        self.assertIn("com/acme/agent/svc", edges[0]["dst"])
        self.assertTrue(edges[0]["extra"]["first_party"])

    def test_import_of_third_party_is_not_resolved(self):
        edges = symbols.resolve_imports(
            [("cart r java Ctl.java`", "java.util.List", 3, "Ctl.java", "r")],
            {}, {})
        self.assertEqual(edges, [])

    def test_static_member_import_ignored(self):
        # `import static com.x.Foo.bar` ends in a lowercase member, not a type.
        edges = symbols.resolve_imports(
            [("cart r java Ctl.java`", "com.x.Foo.bar", 3, "Ctl.java", "r")],
            {"bar": ["whatever"]}, {"whatever": "x.java"})
        self.assertEqual(edges, [])

    def test_supertype_unknown_becomes_external_stub(self):
        edges, gaps = symbols.resolve_supertypes(
            [("cart r java Impl.java`Impl#", "RuntimeException", 3,
              "Impl.java", "r")], {}, {})
        # No reciprocal edge for a third-party parent: nothing here implements
        # it, and the stub is not a node anybody traverses from.
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["kind"], "extends")
        self.assertFalse(edges[0]["extra"]["first_party"])
        self.assertIn("external", edges[0]["dst"])

    def test_ambiguous_supertype_recorded_as_gap(self):
        idx = {"Service": ["cart a java A.java`Service#",
                           "cart b java B.java`Service#"]}
        by_repo = {"cart a java A.java`Service#": "a",
                   "cart b java B.java`Service#": "b"}
        edges, gaps = symbols.resolve_supertypes(
            [("cart c java Impl.java`Impl#", "Service", 3, "Impl.java", "c")],
            idx, by_repo)
        # Falls back to an external stub rather than guessing which one.
        self.assertFalse(edges[0]["extra"]["first_party"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0], "ambiguous-supertype")

    def test_unreadable_file_returns_empty(self):
        n, e, d, sup, imp = symbols.parse_file(
            "/nonexistent/x.java", "java", "r", "/", "s")
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
        self.assertIn("shop-db", details[0])

    def test_unconsumed_topic_flagged(self):
        details = " ".join(r["detail"] for r in self.st.conn.execute(
            "SELECT detail FROM gaps WHERE category='unconsumed-topic'"))
        self.assertIn("shop.order.cancelled", details)

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
        self.assertTrue(any("shop-db" in q for q in qs))


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
        names = {x["name"] for x in tools}
        # Assert on names, not a count: adding a tool should not fail the suite.
        self.assertTrue({"repo_map", "find_symbol", "blast_radius",
                         "service_topology", "who_owns", "hotspots",
                         "coupled_files", "shortest_path", "contracts",
                         "open_questions", "graph_stats", "schema_map",
                         "module_inventory", "secret_locations"} <= names,
                        sorted(names))
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
            "schema_map": {},
            "module_inventory": {},
            "secret_locations": {},
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




# ==========================================================================
# The Acme OMS shape: Maven multi-module backend, Kafka topics as
# enum constants, Angular proxy configs, two migration systems, no ORM.
# ==========================================================================

_MULTI = {}


class TestEnterpriseShape(unittest.TestCase):
    """
    Conventions that break naive tools, on a fixture shaped like a real
    enterprise estate: a Maven multi-module backend with services excluded
    from the reactor, Kafka topics as enum constants, SPA proxy configs, two
    migration systems side by side, and no ORM anywhere.
    """

    @classmethod
    def setUpClass(cls):
        if _MULTI:
            return
        import make_multiservice_fixture
        from cartographer.extract import (maven, kafka, frontend, sqlschema,
                                          compose, secrets)
        tmp = tempfile.mkdtemp(prefix="cart-multi-")
        root = os.path.join(tmp, "estate")
        make_multiservice_fixture.build(root)
        cfg_path = os.path.join(tmp, "cartographer.yaml")
        with open(cfg_path, "w") as fh:
            fh.write("roots:\n  - %s\n\nservices:\n" % root)
            for name, al in (
                    ("order", "[ord, orddev]"), ("catalog", "[ctlg, ctlgdev]"),
                    ("common", "[cmn, cmndev]"), ("company", "[cmny, cmnydev]"),
                    ("comment", "[cmt]"), ("notification", "[notif]"),
                    ("nexus", "[]"), ("agent", "[emailagentdev]"),
                    ("order-legacy", "[ome, legacyorders]"),
                    ("logistics", "[freight]"),
                    ("gateway", "[gw]"),
                    ("contract", "[contracts]"), ("inventory", "[inv]"),
                    ("web-repo", "[omsnextgen]"),
                    ("portal-repo", "[portal]"),
                    ("mobile-repo", "[]")):
                fh.write("  - name: %s\n    aliases: %s\n" % (name, al))
        cfg = C.load(cfg_path)
        triples = [(os.path.basename(r), r, cfg.service_for_repo(os.path.basename(r)))
                   for r in C.discover_repos(cfg.roots)]
        st = Store(os.path.join(tmp, "graph.db"))
        stats = {
            "maven": maven.run(st, cfg, triples),
            "compose": compose.run(st, cfg, triples),
            "symbols": symbols.run(st, cfg, triples),
            "topology": topology.run(st, cfg, triples),
            "kafka": kafka.run(st, cfg, triples),
            "frontend": frontend.run(st, cfg, triples),
            "sqlschema": sqlschema.run(st, cfg, triples),
            "secrets": secrets.run(st, cfg, triples),
            "history": history.run(st, cfg, triples),
        }
        hotspots.compute(st)
        pagerank.compute(st)
        st.commit()
        _MULTI.update(tmp=tmp, root=root, cfg=cfg, cfg_path=cfg_path, st=st,
                    stats=stats)

    @classmethod
    def tearDownClass(cls):
        try:
            _MULTI["st"].close()
        except Exception:
            pass
        shutil.rmtree(_MULTI.get("tmp", ""), ignore_errors=True)

    # ---- Maven ---------------------------------------------------------
    def test_services_separated_from_shared_libraries(self):
        st = _MULTI["st"]
        libs = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='library' AND extra LIKE '%artifactId%'")}
        svcs = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service'")}
        for lib in ("framework", "cache", "msglib", "corelib", "auth", "misc"):
            self.assertIn(lib, libs, "%s is a shared library, not a service" % lib)
        for svc in ("order", "catalog", "common", "agent"):
            self.assertIn(svc, svcs)

    def test_modules_outside_reactor_are_found(self):
        st = _MULTI["st"]
        details = " ".join(r["detail"] for r in st.conn.execute(
            "SELECT detail FROM gaps WHERE category IN "
            "('module-outside-reactor','module-standalone')"))
        self.assertIn("logistics", details)
        self.assertIn("gateway", details)

    def test_module_dependency_edges(self):
        st = _MULTI["st"]
        n = st.conn.execute(
            "SELECT COUNT(*) n FROM edges WHERE kind='depends-on'").fetchone()["n"]
        self.assertGreater(n, 10)

    # ---- Kafka via constants -------------------------------------------
    def test_topics_resolved_from_enum_constants(self):
        st = _MULTI["st"]
        topics = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='topic'")}
        self.assertIn("order.submitted", topics,
                      "topic must come from the enum value, not a literal")

    def test_listener_is_a_consumer_not_a_producer(self):
        st = _MULTI["st"]
        cons = {r["dst"].split(" ", 3)[-1] for r in st.conn.execute(
            "SELECT dst FROM edges WHERE kind='consumed-by'")}
        self.assertIn("notification", cons,
                      "@KafkaListener with a SpEL constant must read as consume")

    def test_event_edge_between_services(self):
        st = _MULTI["st"]
        pairs = {(r["src"].split()[-1], r["dst"].split()[-1])
                 for r in st.conn.execute(
                     "SELECT src, dst FROM edges WHERE kind='event'")}
        self.assertIn(("order", "notification"), pairs)

    def test_constants_file_does_not_reference_itself(self):
        st = _MULTI["st"]
        misc = [r for r in st.conn.execute(
            "SELECT src FROM edges WHERE kind IN ('produces','consumed-by') "
            "AND src LIKE '%misc%'")]
        self.assertEqual(misc, [])

    # ---- frontend ------------------------------------------------------
    def test_proxy_config_yields_frontend_to_backend_edges(self):
        st = _MULTI["st"]
        pairs = {(r["src"].split()[-1], r["dst"].split()[-1])
                 for r in st.conn.execute(
                     "SELECT src, dst FROM edges WHERE kind='http'")}
        for expected in (("web-repo", "order"), ("web-repo", "catalog"),
                         ("web-repo", "logistics"),
                         ("portal-repo", "order")):
            self.assertIn(expected, pairs, "missing %s" % (expected,))

    def test_separate_hosts_recorded(self):
        st = _MULTI["st"]
        hosts = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='host'")}
        self.assertIn("api.sqe.acme.test", hosts)
        self.assertIn("logistics.sqe.acme.test", hosts)

    def test_react_native_hardcoded_urls(self):
        st = _MULTI["st"]
        hosts = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='host'")}
        self.assertIn("www.example-prod.com", hosts)

    def test_coldfusion_surface_detected(self):
        st = _MULTI["st"]
        pages = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='legacy-page'")}
        self.assertTrue(any(p.endswith(".cfm") for p in pages), pages)

    def test_shared_library_version_drift(self):
        st = _MULTI["st"]
        d = " ".join(r["detail"] for r in st.conn.execute(
            "SELECT detail FROM gaps WHERE category='shared-library-version-drift'"))
        self.assertIn("ui-kit", d)

    # ---- schema --------------------------------------------------------
    def test_tables_from_both_migration_systems(self):
        st = _MULTI["st"]
        rows = {r["name"]: json.loads(r["extra"] or "{}")
                for r in st.conn.execute(
                    "SELECT name, extra FROM nodes WHERE kind='table'")}
        self.assertIn("T_PURCHASE_ORDER", rows)
        self.assertIn("T_PRODUCT", rows)
        systems = {v.get("migration_system") for v in rows.values()}
        self.assertTrue({"patchlist", "flyway"} <= systems, systems)

    def test_table_ownership(self):
        st = _MULTI["st"]
        owners = {r["name"]: r["service"] for r in st.conn.execute(
            "SELECT name, service FROM nodes WHERE kind='table'")}
        self.assertEqual(owners.get("T_PURCHASE_ORDER"), "order")
        self.assertEqual(owners.get("T_PRODUCT"), "catalog")

    def test_dao_sql_yields_table_access(self):
        st = _MULTI["st"]
        acc = {(r["src"].split()[-1], r["dst"].split(" ", 3)[-1], r["kind"])
               for r in st.conn.execute(
                   "SELECT src, dst, kind FROM edges "
                   "WHERE kind IN ('reads-table','writes-table')")}
        self.assertIn(("catalog", "T_PRODUCT", "reads-table"), acc)
        self.assertIn(("order", "T_PRODUCT", "reads-table"), acc)
        self.assertIn(("catalog", "T_PRODUCT_PRICE", "writes-table"), acc)

    def test_concatenated_sql_is_parsed(self):
        # ProductDaoImpl builds its SELECT across three string literals joined
        # by `+`; per-literal parsing would never see the JOIN target.
        st = _MULTI["st"]
        acc = {(r["src"].split()[-1], r["dst"].split(" ", 3)[-1])
               for r in st.conn.execute(
                   "SELECT src, dst FROM edges WHERE kind='reads-table'")}
        self.assertIn(("catalog", "T_PRODUCT_PRICE"), acc)

    def test_shared_table_flagged(self):
        st = _MULTI["st"]
        d = " ".join(r["detail"] for r in st.conn.execute(
            "SELECT detail FROM gaps WHERE category='shared-table'"))
        self.assertIn("T_PRODUCT", d)

    # ---- compose -------------------------------------------------------
    def test_shared_schema_detected(self):
        st = _MULTI["st"]
        d = " ".join(r["detail"] for r in st.conn.execute(
            "SELECT detail FROM gaps WHERE category='shared-schema'"))
        self.assertIn("cmndev", d)
        self.assertIn("common", d)
        self.assertIn("nexus", d)

    def test_infra_containers_not_services(self):
        st = _MULTI["st"]
        svcs = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='service'")}
        for infra in ("mysqldb", "redis", "kafka"):
            self.assertNotIn(infra, svcs)

    # ---- Spring routes -------------------------------------------------
    def test_class_and_method_mappings_compose(self):
        st = _MULTI["st"]
        routes = {r["name"] for r in st.conn.execute(
            "SELECT name FROM nodes WHERE kind='route'")}
        self.assertIn("POST /order/api/v1/purchase-orders", routes)
        self.assertIn("GET /order/api/v1/purchase-orders/{id}", routes)
        self.assertIn("DELETE /order/api/v1/purchase-orders/{id}", routes)
        self.assertIn("GET /catalog/api/products/{sku}", routes)

    # ---- secrets -------------------------------------------------------
    def test_credentials_flagged(self):
        st = _MULTI["st"]
        d = " ".join(r["detail"] for r in st.conn.execute(
            "SELECT detail FROM gaps WHERE category='credential-in-source'"))
        self.assertIn("application.properties", d)

    def test_secret_values_never_stored_anywhere(self):
        """The load-bearing guarantee: values must not reach the database."""
        st = _MULTI["st"]
        st.commit()
        with open(st.path, "rb") as fh:
            raw = fh.read()
        for needle in (b"s3cr3tValueThatMustNeverBeStored",
                       b"sk-proj-NOTAREALKEY", b"AAAAXXXXBBBBCCCC"):
            self.assertNotIn(needle, raw,
                             "secret value leaked into graph.db: %r" % needle)

    # ---- module attribution --------------------------------------------
    def test_findings_attributed_to_modules_not_the_repo(self):
        st = _MULTI["st"]
        owners = {r["src"].split()[-1] for r in st.conn.execute(
            "SELECT src FROM edges WHERE kind='uses-datastore'")}
        self.assertNotIn("backend-repo", owners,
                         "a multi-module repo must attribute per module")
        self.assertTrue({"order", "catalog"} & owners, owners)

    # ---- reports still render on this shape -----------------------------
    def test_report_includes_new_sections(self):
        md = md_report.build(_MULTI["st"], _MULTI["cfg"])
        for section in ("Module inventory", "Data ownership",
                        "Possible credentials in source"):
            self.assertIn(section, md)

    def test_questions_reference_real_findings(self):
        qs = " ".join(md_report.suggest_questions(_MULTI["st"]))
        self.assertTrue(qs.strip())


# ==========================================================================
# The visual UI: hierarchical aggregation, the HTTP API, and the assets.
# ==========================================================================

class TestHierarchy(unittest.TestCase):
    def setUp(self):
        self.st = _STATE["st"]

    def test_estate_is_readable(self):
        from cartographer.analyze import hierarchy
        g = hierarchy.graph(self.st, "estate")
        # The whole point of the hierarchy: an estate view a human can read.
        self.assertLess(g["stats"]["nodes"], 200)
        self.assertGreater(g["stats"]["nodes"], 3)
        self.assertGreater(g["stats"]["rolled_up"], 0)

    def test_estate_has_no_inner_nodes(self):
        from cartographer.analyze import hierarchy
        g = hierarchy.graph(self.st, "estate")
        for n in g["nodes"]:
            self.assertIn(n["kind"], hierarchy.TOP_KINDS,
                          "%s should have been rolled up" % n["kind"])

    def test_edges_are_aggregated_with_counts(self):
        from cartographer.analyze import hierarchy
        g = hierarchy.graph(self.st, "estate")
        self.assertTrue(g["edges"])
        for e in g["edges"]:
            self.assertGreaterEqual(e["count"], 1)
            self.assertIn("style", e)
            self.assertIn(e["provenance"], ("EXTRACTED", "INFERRED"))
            self.assertIsInstance(e["samples"], list)

    def test_no_self_edges(self):
        from cartographer.analyze import hierarchy
        for level, kw in (("estate", {}), ("service", {"focus": "order-svc"})):
            g = hierarchy.graph(self.st, level, **kw)
            for e in g["edges"]:
                self.assertNotEqual(e["source"], e["target"])

    def test_every_edge_endpoint_is_present(self):
        from cartographer.analyze import hierarchy
        g = hierarchy.graph(self.st, "estate")
        ids = {n["id"] for n in g["nodes"]}
        for e in g["edges"]:
            self.assertIn(e["source"], ids)
            self.assertIn(e["target"], ids)

    def test_service_view_is_scoped(self):
        from cartographer.analyze import hierarchy
        svc = [n["label"] for n in hierarchy.graph(self.st, "estate")["nodes"]
               if n["kind"] == "service"]
        self.assertTrue(svc)
        g = hierarchy.graph(self.st, "service", focus=svc[0])
        self.assertLess(len(g["nodes"]), 120)
        self.assertEqual(g["breadcrumb"][-1]["label"], svc[0])

    def test_breadcrumbs_navigable(self):
        from cartographer.analyze import hierarchy
        g = hierarchy.graph(self.st, "service", focus="order-svc")
        self.assertEqual(g["breadcrumb"][0]["level"], "estate")

    def test_unknown_level_raises(self):
        from cartographer.analyze import hierarchy
        with self.assertRaises(ValueError):
            hierarchy.graph(self.st, "nonsense")


class TestUIServer(unittest.TestCase):
    """Drives the real HTTP server over a socket, not the handler in isolation."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from cartographer import server as srv
        srv.Handler.source = srv.GraphSource(_STATE["st"].path, _STATE["cfg_path"])
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        import urllib.request
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")

    def get_json(self, path):
        status, body, _ = self.get(path)
        return status, json.loads(body.decode("utf-8"))

    def test_index_and_assets(self):
        for path in ("/", "/app.css", "/app.js", "/graph2d.js", "/graph3d.js",
                     "/views.js"):
            status, body, ctype = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(len(body) > 200, path)

    def test_ui_references_no_external_hosts(self):
        """Offline guarantee: a CDN request would simply fail at work."""
        import re
        for path in ("/", "/app.css", "/app.js", "/graph2d.js", "/graph3d.js",
                     "/views.js"):
            _s, body, _c = self.get(path)
            text = body.decode("utf-8", "replace")
            # Ignore URLs that only appear inside comments or as sample data.
            for m in re.finditer(r'(src|href)\s*=\s*["\']([^"\']+)', text):
                url = m.group(2)
                self.assertFalse(url.startswith(("http://", "https://", "//")),
                                 "%s loads external resource %s" % (path, url))

    def test_path_traversal_blocked(self):
        import urllib.error
        for evil in ("/../../etc/passwd", "/..%2f..%2fetc%2fpasswd",
                     "/./../../cartographer/store.py"):
            try:
                status, _b, _c = self.get(evil)
                self.assertNotEqual(status, 200, evil)
            except urllib.error.HTTPError as e:
                self.assertIn(e.code, (403, 404), evil)

    def test_api_endpoints(self):
        for path in ("/api/ping", "/api/stats", "/api/graph?level=estate",
                     "/api/schema", "/api/coverage", "/api/timeline",
                     "/api/questions", "/api/gaps", "/api/hotspots",
                     "/api/coupling", "/api/flow?entity=order",
                     "/api/search?q=order"):
            status, d = self.get_json(path)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(d, dict, path)
            self.assertNotIn("error", d, path)

    def test_graph_levels(self):
        _s, estate = self.get_json("/api/graph?level=estate")
        svc = [n["label"] for n in estate["nodes"] if n["kind"] == "service"]
        self.assertTrue(svc)
        _s, one = self.get_json("/api/graph?level=service&focus=" + svc[0])
        self.assertEqual(one["level"], "service")
        self.assertEqual(one["focus"], svc[0])

    def test_node_detail_has_evidence(self):
        _s, res = self.get_json("/api/search?q=computeTotal")
        if not res["results"]:
            self.skipTest("fixture has no computeTotal")
        nid = res["results"][0]["id"]
        _s, n = self.get_json("/api/node?id=" + nid.replace(" ", "%20")
                              .replace("#", "%23").replace("`", "%60"))
        self.assertIn("outgoing", n)
        self.assertIn("incoming", n)

    def test_impact_endpoint(self):
        _s, d = self.get_json("/api/impact?target=computeTotal")
        if "error" in d:
            self.skipTest("no such symbol in this fixture")
        self.assertIn("services", d)
        self.assertIn("text", d)

    def test_schema_endpoint_shape(self):
        _s, d = self.get_json("/api/schema")
        self.assertIn("tables", d)
        for t in d["tables"]:
            for key in ("name", "readers", "writers", "touched_by"):
                self.assertIn(key, t)

    def test_coverage_reports_blind_spots(self):
        _s, d = self.get_json("/api/coverage")
        self.assertIn("blind_spots", d)
        self.assertIn("note", d)
        self.assertGreater(len(d["note"]), 40)

    def test_unknown_endpoint_is_404_json(self):
        import urllib.error
        try:
            self.get("/api/does-not-exist")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            json.loads(e.read().decode("utf-8"))

    def test_bad_params_do_not_500(self):
        import urllib.error
        for path in ("/api/graph?level=bogus", "/api/node?id=",
                     "/api/node?id=nope", "/api/impact?target=",
                     "/api/edge?source=a", "/api/search?q="):
            try:
                status, body, _c = self.get(path)
                self.assertIn(status, (200, 400, 404), path)
                json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as e:
                self.assertNotEqual(e.code, 500, path)
                json.loads(e.read().decode("utf-8"))


class TestUIMissingGraph(unittest.TestCase):
    def test_reports_missing_graph_cleanly(self):
        import threading, urllib.error, urllib.request
        from http.server import ThreadingHTTPServer
        from cartographer import server as srv
        tmp = tempfile.mkdtemp()
        srv.Handler.source = srv.GraphSource(os.path.join(tmp, "nope.db"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/ping" % port, timeout=10) as r:
                d = json.loads(r.read().decode())
            self.assertFalse(d["graph"])
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/stats" % port, timeout=10)
                self.fail("expected 503")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 503)
                self.assertIn("cartographer scan",
                              json.loads(e.read().decode())["error"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(tmp, ignore_errors=True)
            srv.Handler.source = srv.GraphSource(_STATE["st"].path,
                                                 _STATE["cfg_path"])


class TestPackagedSetup(unittest.TestCase):
    """The path a new machine actually takes: unpack, verify, init, scan."""

    def test_containers_are_not_services(self):
        """
        A directory that contains other scan targets -- the workspace root, or
        a repo whose modules are scanned individually -- must not appear as a
        service. It used to, and then collected every unattributed finding.
        """
        import subprocess
        work = tempfile.mkdtemp(prefix="cart-pkg-")
        try:
            import make_multiservice_fixture
            root = os.path.join(work, "ws")
            make_multiservice_fixture.build(root)
            env = dict(os.environ, PYTHONPATH=ROOT)
            run = lambda *a: subprocess.run(
                [sys.executable, "-m", "cartographer.cli"] + list(a),
                capture_output=True, text=True, timeout=180, env=env, cwd=root)

            r = run("init", "--root", ".")
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            r = run("scan")
            self.assertEqual(r.returncode, 0, r.stderr[-800:])

            st = Store(os.path.join(root, ".cartographer", "graph.db"))
            try:
                services = {x["name"] for x in st.conn.execute(
                    "SELECT name FROM nodes WHERE kind='service'")}
                # The workspace root and the multi-module backend repo are
                # containers, not services.
                self.assertNotIn(os.path.basename(root), services)
                self.assertNotIn("backend-repo", services)
                # Real modules survive.
                self.assertTrue({"order", "catalog"} <= services, services)
                # Shared libraries are libraries, not services.
                libs = {x["name"] for x in st.conn.execute(
                    "SELECT name FROM nodes WHERE kind='library' "
                    "AND extra LIKE '%artifactId%'")}
                self.assertTrue({"framework", "cache"} <= libs, libs)
                self.assertFalse(services & libs, "a name cannot be both")
            finally:
                st.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_installer_is_runnable(self):
        sh = os.path.join(ROOT, "install.sh")
        self.assertTrue(os.path.isfile(sh))
        self.assertTrue(os.access(sh, os.X_OK), "install.sh must be executable")

    def test_agent_runbook_present_and_ordered(self):
        """The runbook is what another Claude follows; keep its shape enforced."""
        with open(os.path.join(ROOT, "SETUP-FOR-CLAUDE.md")) as fh:
            text = fh.read()
        for marker in ("Step 0", "Step 1", "Step 2", "Step 3", "Step 4",
                       "Step 5", "Step 6", "Step 7"):
            self.assertIn(marker, text)
        # The two safety rules must survive any future edit.
        self.assertIn("never run `scan` against a directory".lower(),
                      text.lower())
        self.assertIn("locations only, never values", text.lower())

    def test_tool_carries_no_organisation_identifiers(self):
        """
        The tool must stay shareable. Anything company-specific belongs in
        examples/, which can be deleted without affecting it.
        """
        import re
        # Assembled from fragments so this file does not match its own
        # pattern and report itself as an offender.
        #
        # The original version of this guard checked only for the full company
        # name, a library name, five host prefixes and one username -- and
        # passed while real internal data sat in code COMMENTS and DOCSTRINGS:
        # the short company abbreviation, the internal codename, the Java
        # package root, real service and module names, the workspace path, and
        # measured statistics about the real estate's size. A guard that only
        # catches the obvious spellings gives false confidence, which is worse
        # than no guard, because it is trusted before sharing.
        parts = [
            # full names and hosts
            "itrade" + "network", "itn" + "-library",
            # Split after the second letter, not the third: the standalone
            # codename rule below would otherwise match this list itself.
            "on" + "gsqe", "iom" + "sqe", "itl" + "sqe", "icr" + "sqe",
            "iml" + "sqe", "rsen" + "gupta",
            # package roots and abbreviations, bounded so ordinary words
            # containing these letters do not match
            r"\bcom\.i" + r"tn\b", r"\bi" + r"tn[-_/.]", r"[-_/]i" + r"tn\b",
            # The codename appears lowercased inside compound identifiers
            # (image names, hostnames, schema names) where \b does not help,
            # because "_" is a word character. An earlier version of this
            # guard was also case-SENSITIVE, and that combination is exactly
            # how `o<codename>_mysql_db` survived a scrub and a review.
            r"\bo" + r"ng\b", r"\bo" + r"ng[-_/]", r"[-_/]o" + r"ng\b",
            # the workspace path and repo names
            r"/projects/o" + r"ng\b",
            r"\bo" + r"ng-(?:server|ui|devops)-repo\b",
            r"\bom-angular-repo\b", r"\bbp-react-repo\b",
            r"\bdatabase-scripts-repo\b",
            # real module / service names used as examples
            r"\border-enterprise\b", r"\binteroperability\b",
            r"\bomsenterprise\b", r"\bkafkautil\b", r"\bgcutil\b",
            r"\bApiConstant\b",
        ]
        # Case-insensitive: the codename and abbreviation appear in both
        # cases across configs, image names and prose, and a case-sensitive
        # guard silently passes half of them.
        bad = re.compile("|".join(parts), re.I)
        offenders = []
        # Root-level docs are shipped too. Leaving them unscanned is how the
        # workspace path and the real node counts reached README and WORKFLOW.
        roots = [os.path.join(ROOT, s) for s in
                 ("cartographer", "tests", "bin", "skills", "agents",
                  "templates", "hooks")]
        # Every shippable root-level file, not just prose. The example config
        # is the first thing a new user opens and it carried a real workspace
        # path for exactly as long as this list said ".md" and ".txt" only --
        # the third hole found in this guard, all of the same kind: the
        # scanned SET was narrower than the shipped set.
        loose = [os.path.join(ROOT, f) for f in os.listdir(ROOT)
                 if f.endswith((".md", ".txt", ".yaml", ".yml", ".json",
                                ".example", ".sh", ".cfg", ".toml")) and
                 os.path.isfile(os.path.join(ROOT, f))]
        for base in roots:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    if fn.endswith((".pyc",)):
                        continue
                    loose.append(os.path.join(dirpath, fn))
        for path in loose:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    hit = bad.search(fh.read())
                if hit:
                    offenders.append("%s (%s)" % (
                        os.path.relpath(path, ROOT), hit.group(0)))
            except OSError:
                pass
        self.assertEqual(sorted(offenders), [],
                         "organisation identifiers leaked into shippable files")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
