"""
The runtime architecture endpoint.

The whole claim of this view is that it shows how services talk at RUN TIME
and leaves build-time coupling out. On a real estate that distinction is not
cosmetic: imports, extends and Maven dependencies were 82% of all
service-to-service edges, and including them turns the diagram back into the
hairball it exists to replace. So the exclusion is the thing worth testing --
not that the endpoint returns something.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cartographer.store import Store                                # noqa: E402
from cartographer.server import Handler                             # noqa: E402


class ArchitectureTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cart-arch-")
        self.st = Store(os.path.join(self.dir, "g.db"))
        self.st.add_nodes([
            {"id": "svc:a", "kind": "service", "name": "alpha", "repo": "r1"},
            {"id": "svc:b", "kind": "service", "name": "beta", "repo": "r2"},
            {"id": "svc:c", "kind": "service", "name": "gamma", "repo": "r2"},
            {"id": "tbl:shared", "kind": "table", "name": "T_SHARED"},
            {"id": "tbl:owned", "kind": "table", "name": "T_OWNED"},
            {"id": "tbl:lonely", "kind": "table", "name": "T_LONELY"},
            {"id": "topic:evt", "kind": "topic", "name": "orders.created"},
        ], "test")
        self.st.add_edges([
            # runtime: should all appear
            {"src": "svc:a", "dst": "svc:b", "kind": "http",
             "evidence": "A.java:10", "provenance": "EXTRACTED"},
            {"src": "svc:a", "dst": "svc:b", "kind": "http",
             "evidence": "A.java:20", "provenance": "EXTRACTED"},
            {"src": "svc:a", "dst": "topic:evt", "kind": "produces",
             "evidence": "A.java:30", "provenance": "EXTRACTED"},
            {"src": "topic:evt", "dst": "svc:c", "kind": "consumed-by",
             "evidence": "C.java:5", "provenance": "EXTRACTED"},
            # two writers, no owner: the finding this view should surface
            {"src": "svc:a", "dst": "tbl:shared", "kind": "writes-table",
             "evidence": "A.java:40", "provenance": "EXTRACTED"},
            {"src": "svc:b", "dst": "tbl:shared", "kind": "writes-table",
             "evidence": "B.java:40", "provenance": "EXTRACTED"},
            # one writer, one other reader: real but weaker
            {"src": "svc:a", "dst": "tbl:owned", "kind": "writes-table",
             "evidence": "A.java:50", "provenance": "EXTRACTED"},
            {"src": "svc:b", "dst": "tbl:owned", "kind": "reads-table",
             "evidence": "B.java:50", "provenance": "EXTRACTED"},
            # touched by a single service: not an integration point
            {"src": "svc:c", "dst": "tbl:lonely", "kind": "writes-table",
             "evidence": "C.java:60", "provenance": "EXTRACTED"},
            # build-time: must NOT appear
            {"src": "svc:a", "dst": "svc:c", "kind": "imports",
             "evidence": "A.java:1", "provenance": "EXTRACTED"},
            {"src": "svc:a", "dst": "svc:c", "kind": "depends-on",
             "evidence": "pom.xml:5", "provenance": "EXTRACTED"},
            {"src": "svc:b", "dst": "svc:c", "kind": "extends",
             "evidence": "B.java:2", "provenance": "EXTRACTED"},
        ], "test")

    def tearDown(self):
        self.st.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _arch(self):
        # The handler's method is pure with respect to the store; constructing
        # a real HTTP server to reach it would test the socket, not the logic.
        return Handler._architecture(Handler, self.st)

    def test_build_time_edges_are_excluded(self):
        d = self._arch()
        pairs = {(l["source"], l["target"]) for l in d["links"]}
        self.assertIn(("svc:a", "svc:b"), pairs, "runtime HTTP call missing")
        self.assertNotIn(("svc:a", "svc:c"), pairs,
                         "an imports/depends-on edge reached the diagram")
        self.assertNotIn(("svc:b", "svc:c"), pairs,
                         "an extends edge reached the diagram")

    def test_parallel_edges_collapse_to_one_arrow(self):
        d = self._arch()
        ab = [l for l in d["links"]
              if l["source"] == "svc:a" and l["target"] == "svc:b"]
        self.assertEqual(len(ab), 1, "two HTTP calls should be one arrow")
        self.assertEqual(ab[0]["count"], 2, "the arrow should carry the count")
        self.assertTrue(ab[0]["evidence"], "every arrow must keep a citation")

    def test_shared_resources_are_ranked_not_dumped(self):
        d = self._arch()
        by_id = {n["id"]: n for n in d["nodes"]}
        self.assertIn("tbl:shared", by_id, "a two-writer table must appear")
        self.assertTrue(by_id["tbl:shared"]["significant"],
                        "two writers with no owner is the headline finding")
        self.assertIn("tbl:owned", by_id, "writer + other reader is coupling")
        self.assertFalse(by_id["tbl:owned"]["significant"],
                         "one writer is not a multiple-writer finding")
        self.assertNotIn("tbl:lonely", by_id,
                         "a table only one service touches is not a waypoint")

    def test_queues_are_waypoints_not_direct_arrows(self):
        d = self._arch()
        pairs = {(l["source"], l["target"]) for l in d["links"]}
        self.assertIn("topic:evt", {n["id"] for n in d["nodes"]})
        self.assertIn(("svc:a", "topic:evt"), pairs)
        self.assertIn(("topic:evt", "svc:c"), pairs)
        # Collapsing producer->topic->consumer into producer->consumer would
        # assert a direct call that does not exist.
        self.assertNotIn(("svc:a", "svc:c"), pairs)

    def test_inferred_provenance_wins(self):
        self.st.add_edges([
            {"src": "svc:b", "dst": "svc:a", "kind": "http",
             "evidence": "B.java:9", "provenance": "EXTRACTED"},
            {"src": "svc:b", "dst": "svc:a", "kind": "http",
             "evidence": "B.java:11", "provenance": "INFERRED"},
        ], "test")
        d = self._arch()
        ba = [l for l in d["links"]
              if l["source"] == "svc:b" and l["target"] == "svc:a"][0]
        self.assertEqual(ba["provenance"], "INFERRED",
                         "an arrow is only as trustworthy as its weakest edge")

    def test_reports_what_it_is_hiding(self):
        d = self._arch()
        self.assertGreater(d["excluded"]["build_time_edges"], 0,
                           "a simplified view must declare its own filter")


if __name__ == "__main__":
    unittest.main()
