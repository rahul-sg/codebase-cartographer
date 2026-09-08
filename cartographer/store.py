"""
The graph store: SQLite.

Chosen over a graph database on purpose. A .db file needs no server, no
install, no approval from anyone; it is queryable with a tool already on every
machine; and it diffs and ships like any other artifact. At a few hundred
thousand nodes -- far more than a 15-service estate produces -- plain indexed
SQL is quick enough that the difference is not observable.

Everything is idempotent: re-running a scan replaces a source's contribution
rather than duplicating it, so partial re-scans of one repo are safe.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA_VERSION = 4

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Every entity: files, classes, functions, services, external modules.
CREATE TABLE IF NOT EXISTS nodes (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    name      TEXT,
    container TEXT,
    file      TEXT,
    line      INTEGER,
    repo      TEXT,
    lang      TEXT,
    service   TEXT,
    source    TEXT NOT NULL DEFAULT 'unknown',
    extra     TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_name    ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_kind    ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_repo    ON nodes(repo);
CREATE INDEX IF NOT EXISTS idx_nodes_service ON nodes(service);
CREATE INDEX IF NOT EXISTS idx_nodes_file    ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_source  ON nodes(source);

-- Every relationship. `provenance` follows the EXTRACTED/INFERRED convention:
--   EXTRACTED  read directly from source, config, a spec, or a trace
--   INFERRED   derived by matching or heuristic; a lead, not a fact
CREATE TABLE IF NOT EXISTS edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    evidence   TEXT,
    provenance TEXT NOT NULL DEFAULT 'INFERRED',
    confidence REAL NOT NULL DEFAULT 0.5,
    weight     REAL NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL DEFAULT 'unknown',
    extra      TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_kind   ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_uniq
    ON edges(src, dst, kind, COALESCE(evidence,''));

-- Per-file behavioural metrics from version control (Code Maat style).
CREATE TABLE IF NOT EXISTS file_metrics (
    path        TEXT NOT NULL,
    repo        TEXT NOT NULL,
    revisions   INTEGER DEFAULT 0,
    authors     INTEGER DEFAULT 0,
    added       INTEGER DEFAULT 0,
    deleted     INTEGER DEFAULT 0,
    loc         INTEGER DEFAULT 0,
    complexity  REAL DEFAULT 0,
    hotspot     REAL DEFAULT 0,
    last_change TEXT,
    first_change TEXT,
    main_author TEXT,
    author_share REAL DEFAULT 0,
    PRIMARY KEY (repo, path)
);
CREATE INDEX IF NOT EXISTS idx_fm_hotspot ON file_metrics(hotspot DESC);

-- Temporal (logical) coupling: files that keep changing together.
CREATE TABLE IF NOT EXISTS coupling (
    a          TEXT NOT NULL,
    b          TEXT NOT NULL,
    a_repo     TEXT NOT NULL,
    b_repo     TEXT NOT NULL,
    shared     INTEGER NOT NULL,
    a_revs     INTEGER NOT NULL,
    b_revs     INTEGER NOT NULL,
    degree     REAL NOT NULL,
    cross_repo INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (a, b)
);
CREATE INDEX IF NOT EXISTS idx_coupling_degree ON coupling(degree DESC);
CREATE INDEX IF NOT EXISTS idx_coupling_cross  ON coupling(cross_repo);

-- Who knows what.
CREATE TABLE IF NOT EXISTS ownership (
    repo    TEXT NOT NULL,
    path    TEXT NOT NULL,
    author  TEXT NOT NULL,
    commits INTEGER NOT NULL,
    added   INTEGER DEFAULT 0,
    last    TEXT,
    PRIMARY KEY (repo, path, author)
);
CREATE INDEX IF NOT EXISTS idx_own_author ON ownership(author);

-- PageRank / centrality scores, recomputed on demand.
CREATE TABLE IF NOT EXISTS ranks (
    id    TEXT PRIMARY KEY,
    rank  REAL NOT NULL,
    kind  TEXT NOT NULL DEFAULT 'global'
);
CREATE INDEX IF NOT EXISTS idx_ranks_rank ON ranks(rank DESC);

-- Monthly activity per service, for the time-lapse view. Cheap to compute
-- from the log we already parse, and it is the only honest fourth dimension
-- available: how the estate actually got this way.
CREATE TABLE IF NOT EXISTS timeline (
    month    TEXT NOT NULL,       -- YYYY-MM
    service  TEXT NOT NULL,
    repo     TEXT NOT NULL,
    commits  INTEGER DEFAULT 0,
    authors  INTEGER DEFAULT 0,
    files    INTEGER DEFAULT 0,
    added    INTEGER DEFAULT 0,
    deleted  INTEGER DEFAULT 0,
    PRIMARY KEY (month, service, repo)
);
CREATE INDEX IF NOT EXISTS idx_timeline_month ON timeline(month);

-- Anything the scan could not resolve. Surfaced in the report, because a
-- known gap is useful and a silent one is dangerous.
CREATE TABLE IF NOT EXISTS gaps (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    detail   TEXT NOT NULL,
    hint     TEXT,
    source   TEXT
);
"""


class Store:
    """Thin, explicit wrapper. No ORM, no magic."""

    def __init__(self, path):
        self.path = str(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def _migrate(self):
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return
        try:
            have = int(row["value"])
        except (TypeError, ValueError):
            have = 0
        if have == SCHEMA_VERSION:
            return
        # Forward-only. The graph is a derived artifact: rebuilding it is cheap
        # and always correct, whereas a half-migrated graph is silently wrong.
        for t in ("nodes", "edges", "file_metrics", "coupling",
                  "ownership", "ranks", "gaps", "timeline"):
            self.conn.execute("DELETE FROM {}".format(t))
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.set_meta("migrated_at", _now())
        # Wiping is correct -- the graph is derived and rebuilding is cheap --
        # but doing it silently leaves every query answering "nothing found",
        # which reads as a broken tool rather than a stale file.
        self.set_meta("needs_rescan",
                      "schema %d -> %d: the graph was cleared, run "
                      "`cartographer scan`" % (have, SCHEMA_VERSION))

    def close(self):
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- meta --------------------------------------------------------------

    def needs_rescan(self):
        """Non-empty when the stored graph was cleared by a schema upgrade."""
        if self.conn.execute(
                "SELECT COUNT(*) n FROM nodes").fetchone()["n"]:
            return None
        return self.get_meta("needs_rescan")

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def all_meta(self):
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT * FROM meta")}

    # -- writes ------------------------------------------------------------

    def clear_source(self, source):
        """Drop everything a given extractor contributed, so re-runs are clean."""
        self.conn.execute("DELETE FROM nodes WHERE source=?", (source,))
        self.conn.execute("DELETE FROM edges WHERE source=?", (source,))
        self.conn.execute("DELETE FROM gaps  WHERE source=?", (source,))

    def add_nodes(self, rows, source):
        """
        rows: iterable of dicts with at least id and kind.

        Conflict policy: a node already present keeps its richer fields rather
        than being blanked by a later, thinner sighting. Two extractors often
        see the same file -- the one that parsed it knows more than the one
        that only saw its name in a config.
        """
        payload = []
        for n in rows:
            if not n.get("id") or not n.get("kind"):
                continue
            extra = n.get("extra")
            payload.append((
                n["id"], n["kind"], n.get("name"), n.get("container"),
                n.get("file"), n.get("line"), n.get("repo"), n.get("lang"),
                n.get("service"), source,
                json.dumps(extra, sort_keys=True) if extra else None,
            ))
        if not payload:
            return 0
        self.conn.executemany(
            "INSERT INTO nodes(id,kind,name,container,file,line,repo,lang,service,source,extra) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  name=COALESCE(excluded.name, nodes.name), "
            "  container=COALESCE(excluded.container, nodes.container), "
            "  file=COALESCE(excluded.file, nodes.file), "
            "  line=COALESCE(excluded.line, nodes.line), "
            "  repo=COALESCE(excluded.repo, nodes.repo), "
            "  lang=COALESCE(excluded.lang, nodes.lang), "
            "  service=COALESCE(excluded.service, nodes.service), "
            "  extra=COALESCE(excluded.extra, nodes.extra)",
            payload)
        return len(payload)

    def add_edges(self, rows, source):
        payload = []
        for e in rows:
            if not e.get("src") or not e.get("dst") or not e.get("kind"):
                continue
            if e["src"] == e["dst"]:
                continue  # self-loops carry no information here
            extra = e.get("extra")
            payload.append((
                e["src"], e["dst"], e["kind"], e.get("evidence"),
                e.get("provenance", "INFERRED"),
                float(e.get("confidence", 0.5)),
                float(e.get("weight", 1.0)), source,
                json.dumps(extra, sort_keys=True) if extra else None,
            ))
        if not payload:
            return 0
        # A repeated sighting of the same edge strengthens it rather than
        # duplicating it.
        self.conn.executemany(
            "INSERT INTO edges(src,dst,kind,evidence,provenance,confidence,weight,source,extra) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(src,dst,kind,COALESCE(evidence,'')) DO UPDATE SET "
            "  weight=edges.weight+excluded.weight, "
            "  confidence=MAX(edges.confidence, excluded.confidence), "
            "  provenance=CASE WHEN edges.provenance='EXTRACTED' "
            "                  OR excluded.provenance='EXTRACTED' "
            "             THEN 'EXTRACTED' ELSE edges.provenance END",
            payload)
        return len(payload)

    def add_gap(self, category, detail, hint=None, source=None):
        self.conn.execute(
            "INSERT INTO gaps(category,detail,hint,source) VALUES(?,?,?,?)",
            (category, detail, hint, source))

    def set_ranks(self, scores, kind="global"):
        self.conn.execute("DELETE FROM ranks WHERE kind=?", (kind,))
        self.conn.executemany(
            "INSERT INTO ranks(id,rank,kind) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET rank=excluded.rank, kind=excluded.kind",
            [(k, float(v), kind) for k, v in scores.items()])

    # -- reads -------------------------------------------------------------

    def node(self, node_id):
        r = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(r) if r else None

    def find_nodes(self, name=None, kind=None, repo=None, service=None,
                   path_like=None, limit=50):
        sql = "SELECT * FROM nodes WHERE 1=1"
        args = []
        if name:
            sql += " AND (name=? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE)"
            args += [name, "%" + name + "%"]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if repo:
            sql += " AND repo=?"
            args.append(repo)
        if service:
            sql += " AND service=?"
            args.append(service)
        if path_like:
            sql += " AND file LIKE ?"
            args.append("%" + path_like + "%")
        # Exact matches first, then by rank, so the useful answer is at the top.
        sql += (" ORDER BY CASE WHEN name=? COLLATE NOCASE THEN 0 ELSE 1 END, "
                " COALESCE((SELECT rank FROM ranks WHERE ranks.id=nodes.id),0) DESC, "
                " name LIMIT ?")
        args += [name or "", int(limit)]
        return [dict(r) for r in self.conn.execute(sql, args)]

    def out_edges(self, node_id, kinds=None):
        sql = "SELECT * FROM edges WHERE src=?"
        args = [node_id]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += list(kinds)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def in_edges(self, node_id, kinds=None):
        sql = "SELECT * FROM edges WHERE dst=?"
        args = [node_id]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += list(kinds)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def counts(self):
        c = {}
        for t in ("nodes", "edges", "file_metrics", "coupling", "ownership",
                  "gaps", "timeline"):
            c[t] = self.conn.execute("SELECT COUNT(*) n FROM %s" % t).fetchone()["n"]
        c["nodes_by_kind"] = {r["kind"]: r["n"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) n FROM nodes GROUP BY kind ORDER BY n DESC")}
        c["edges_by_kind"] = {r["kind"]: r["n"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) n FROM edges GROUP BY kind ORDER BY n DESC")}
        c["edges_by_provenance"] = {r["provenance"]: r["n"] for r in self.conn.execute(
            "SELECT provenance, COUNT(*) n FROM edges GROUP BY provenance")}
        c["repos"] = [r["repo"] for r in self.conn.execute(
            "SELECT DISTINCT repo FROM nodes WHERE repo IS NOT NULL ORDER BY repo")]
        c["services"] = [r["service"] for r in self.conn.execute(
            "SELECT DISTINCT service FROM nodes WHERE service IS NOT NULL ORDER BY service")]
        c["languages"] = {r["lang"]: r["n"] for r in self.conn.execute(
            "SELECT lang, COUNT(*) n FROM nodes WHERE lang IS NOT NULL "
            "GROUP BY lang ORDER BY n DESC")}
        return c

    def commit(self):
        self.conn.commit()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
