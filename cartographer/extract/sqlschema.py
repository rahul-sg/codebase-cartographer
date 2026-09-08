"""
Schema and data-access mapping.

There is no JPA anywhere in this estate, so there are no @Entity annotations to
scan. The schema lives in two places -- legacy `database/<module>/patches/*.sql`
manifests and Flyway `db/migration/**/V*.sql` trees -- and data ACCESS lives in
raw SQL strings inside hand-written JdbcTemplate DAOs.

That makes this the most valuable layer in the whole tool for a microservice
estate, because it answers the question nobody has written down: **which
services actually read and write which tables.** Two services touching one
table is a coupling no API contract documents and no import graph reveals.

Java SQL is assembled by concatenation far more often than written as one
literal, so string runs joined by `+` are merged before parsing. A naive
per-literal regex misses most real queries.
"""
from __future__ import annotations

import os
import re

from .. import ids
from ..config import SKIP_DIRS, prune

SOURCE = "sqlschema"

# ---- DDL -----------------------------------------------------------------
CREATE_TABLE = re.compile(
    r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+|LOCAL\s+)?(?:TEMPORARY\s+|TEMP\s+)?'
    r'TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?([A-Za-z_][\w$]*)[`"\]]?'
    r'(?:\.[`"\[]?([A-Za-z_][\w$]*)[`"\]]?)?', re.I)
ALTER_TABLE = re.compile(
    r'\bALTER\s+TABLE\s+[`"\[]?([A-Za-z_][\w$]*)[`"\]]?'
    r'(?:\.[`"\[]?([A-Za-z_][\w$]*)[`"\]]?)?', re.I)
CREATE_VIEW = re.compile(
    r'\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+[`"\[]?([A-Za-z_][\w$]*)[`"\]]?', re.I)
CREATE_INDEX = re.compile(r'\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+\w+\s+ON\s+[`"\[]?([A-Za-z_][\w$]*)', re.I)

# ---- DML in application code --------------------------------------------
READ_TABLE = re.compile(
    r'\b(?:FROM|JOIN)\s+[`"\[]?([A-Za-z_][\w$]*)[`"\]]?', re.I)
INSERT_TABLE = re.compile(
    r'\bINSERT\s+(?:IGNORE\s+)?INTO\s+[`"\[]?([A-Za-z_][\w$]*)[`"\]]?', re.I)
UPDATE_TABLE = re.compile(r'\bUPDATE\s+[`"\[]?([A-Za-z_][\w$]*)[`"\]]?', re.I)
DELETE_TABLE = re.compile(r'\bDELETE\s+FROM\s+[`"\[]?([A-Za-z_][\w$]*)[`"\]]?', re.I)
CALL_PROC = re.compile(r'\b(?:CALL|EXEC(?:UTE)?)\s+[`"\[]?([A-Za-z_][\w$]*)', re.I)

HAS_SQL = re.compile(r'\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CALL)\b', re.I)

# Java string literal, tolerating escapes.
JSTRING = re.compile(r'"((?:[^"\\]|\\.)*)"')

# Words that follow FROM/JOIN but are not tables.
NOT_A_TABLE = frozenset("""
select where and or on as set values dual join inner left right outer full cross
group order by having limit offset union all distinct case when then else end
""".split())

SQL_EXT = (".sql",)
CODE_EXT = (".java", ".kt", ".scala", ".groovy", ".py", ".xml")


def _read(p, limit=4_000_000):
    try:
        if os.path.getsize(p) > limit:
            return None
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _walk(repo_root, exts, follow=False):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        prune(dirpath, dirnames)
        for fn in sorted(filenames):
            if fn.endswith(exts):
                yield os.path.join(dirpath, fn)


def strip_sql_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    return text


def module_of(cfg, repo_root, path, fallback):
    """
    Attribute a file to its owning module.

    Handles both shapes seen here:
        server/<module>/src/main/...          -> <module>
        database/<module>/patches/*.sql       -> <module>
    """
    rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
    parts = rel.split("/")
    for anchor in ("server", "database", "modules", "services", "apps"):
        if anchor in parts:
            i = parts.index(anchor)
            if i + 1 < len(parts) - 1:
                name = parts[i + 1]
                return cfg.resolve_service(name) or name
    return fallback


def merged_java_strings(text):
    """
    Yield (merged_string, line_number) for runs of literals joined by `+`.

    `"SELECT a " + "FROM T " + "WHERE x"` becomes one string, which is how most
    real DAO SQL is written. Parsing literals individually finds "SELECT a" and
    never learns which table it came from.
    """
    out = []
    pos = 0
    n = len(text)
    line = 1
    line_at = {}
    for i, ch in enumerate(text):
        if ch == "\n":
            line += 1
        line_at[i] = line
    while pos < n:
        m = JSTRING.search(text, pos)
        if not m:
            break
        start_line = line_at.get(m.start(), 1)
        parts = [m.group(1)]
        end = m.end()
        while True:
            j = end
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] == "+":
                j += 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                nxt = JSTRING.match(text, j)
                if nxt:
                    parts.append(nxt.group(1))
                    end = nxt.end()
                    continue
            break
        merged = "".join(parts)
        if len(merged) > 6:
            out.append((merged, start_line))
        pos = end
    return out


def tables_in(sql):
    """Return (reads, writes) table-name sets for one SQL statement."""
    reads, writes = set(), set()
    s = " " + re.sub(r"\s+", " ", sql) + " "

    def add(target, pat):
        for m in pat.finditer(s):
            name = m.group(1)
            if not name or name.lower() in NOT_A_TABLE or len(name) < 2:
                continue
            if name.startswith(":") or "$" in name:
                continue
            target.add(name.upper())

    add(writes, INSERT_TABLE)
    add(writes, DELETE_TABLE)
    # UPDATE t SET ... : only treat as a write when SET follows, so that
    # "FOR UPDATE" and similar do not invent a table.
    for m in UPDATE_TABLE.finditer(s):
        tail = s[m.end():m.end() + 200]
        if re.match(r"\s+(?:\w+\s+)?SET\b", tail, re.I):
            name = m.group(1)
            if name and name.lower() not in NOT_A_TABLE:
                writes.add(name.upper())
    add(reads, READ_TABLE)
    reads -= writes
    return reads, writes


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    follow = cfg.defaults.get("follow_symlinks", False)

    tables = {}          # TABLE -> {"owner":module, "evidence":str, "kind":str}
    access = {}          # (service, TABLE) -> {"read":ev, "write":ev}
    n_ddl = n_dml = n_files = 0

    # ---- 1. DDL: the schema itself ------------------------------------
    for repo_name, repo_root, svc in repos:
        for path in _walk(repo_root, SQL_EXT, follow):
            text = _read(path)
            if text is None:
                continue
            n_files += 1
            body = strip_sql_comments(text)
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            owner = module_of(cfg, repo_root, path, svc or repo_name)
            flavour = ("flyway" if "/db/migration/" in "/" + rel
                       else "patchlist" if "/patches/" in "/" + rel else "sql")
            line_of = _line_index(text)
            for pat, kind in ((CREATE_TABLE, "table"), (CREATE_VIEW, "view")):
                for m in pat.finditer(body):
                    groups = [g for g in m.groups() if g]
                    name = groups[-1].upper()
                    if name.lower() in NOT_A_TABLE:
                        continue
                    n_ddl += 1
                    ev = "%s/%s:%d" % (repo_name, rel, line_of(m.start()))
                    prev = tables.get(name)
                    if prev is None:
                        tables[name] = {"owner": owner, "evidence": ev,
                                        "kind": kind, "migration": flavour,
                                        "created_by": {owner}}
                    else:
                        prev["created_by"].add(owner)
            for m in ALTER_TABLE.finditer(body):
                groups = [g for g in m.groups() if g]
                name = groups[-1].upper()
                ev = "%s/%s:%d" % (repo_name, rel, line_of(m.start()))
                rec = tables.setdefault(name, {
                    "owner": owner, "evidence": ev, "kind": "table",
                    "migration": flavour, "created_by": set()})
                rec.setdefault("altered_by", set()).add(owner)

    if progress:
        progress("  sqlschema: %d tables from %d .sql files" % (len(tables), n_files))

    # ---- 2. DML: which service touches which table ---------------------
    for repo_name, repo_root, svc in repos:
        for path in _walk(repo_root, CODE_EXT, follow):
            text = _read(path)
            if text is None or not HAS_SQL.search(text):
                continue
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            owner = module_of(cfg, repo_root, path, svc or repo_name)
            if path.endswith(".xml"):
                candidates = [(text, 1)]
            else:
                candidates = merged_java_strings(text)
            for sql, line in candidates:
                if not HAS_SQL.search(sql):
                    continue
                reads, writes = tables_in(sql)
                if not reads and not writes:
                    continue
                n_dml += 1
                ev = "%s/%s:%d" % (repo_name, rel, line)
                for t in reads:
                    access.setdefault((owner, t), {}).setdefault("read", ev)
                for t in writes:
                    access.setdefault((owner, t), {}).setdefault("write", ev)

    # ---- 3. build the graph -------------------------------------------
    nodes, edges = [], []
    for name, rec in tables.items():
        nodes.append({"id": "cart . table %s" % name, "kind": "table",
                      "name": name, "service": rec["owner"],
                      "extra": {"declared_at": rec["evidence"],
                                "migration_system": rec["migration"],
                                "object": rec["kind"]}})
        if rec["owner"]:
            nodes.append({"id": ids.service_id(rec["owner"]), "kind": "service",
                          "name": rec["owner"], "service": rec["owner"]})
            edges.append({"src": ids.service_id(rec["owner"]),
                          "dst": "cart . table %s" % name,
                          "kind": "defines-table", "evidence": rec["evidence"],
                          "provenance": "EXTRACTED", "confidence": 1.0})

    touched = {}
    for (owner, table), modes in access.items():
        tid = "cart . table %s" % table
        if table not in tables:
            # Referenced but never declared in a repo we can see.
            nodes.append({"id": tid, "kind": "table", "name": table,
                          "extra": {"declared_at": None, "external": True}})
        nodes.append({"id": ids.service_id(owner), "kind": "service",
                      "name": owner, "service": owner})
        for mode, ev in modes.items():
            edges.append({"src": ids.service_id(owner), "dst": tid,
                          "kind": "writes-table" if mode == "write" else "reads-table",
                          "evidence": ev, "provenance": "EXTRACTED",
                          "confidence": 0.8,
                          "extra": {"via": "SQL in DAO"}})
        touched.setdefault(table, {})[owner] = set(modes)

    # ---- 4. the findings that matter -----------------------------------
    shared = 0
    for table, owners in sorted(touched.items()):
        if len(owners) < 2:
            continue
        shared += 1
        writers = sorted(o for o, m in owners.items() if "write" in m)
        readers = sorted(o for o, m in owners.items() if "write" not in m)
        declared = tables.get(table, {}).get("owner")
        detail = "table %s is accessed by %d services (%s)" % (
            table, len(owners), ", ".join(sorted(owners)))
        if declared:
            detail += "; its DDL lives in %s" % declared
        hint = "a shared table is a coupling no API contract documents. "
        if len(writers) > 1:
            hint += "MULTIPLE WRITERS (%s) -- confirm who owns it." % ", ".join(writers)
        elif writers:
            hint += "%s writes; %s only read." % (writers[0], ", ".join(readers) or "none")
        else:
            hint += "read-only from every service seen here."
        store.add_gap("shared-table", detail, hint, SOURCE)

    for name, rec in tables.items():
        creators = rec.get("created_by") or set()
        if len(creators) > 1:
            store.add_gap("table-declared-twice",
                          "table %s has CREATE statements under %d modules (%s)"
                          % (name, len(creators), ", ".join(sorted(creators))),
                          "either a shared schema or duplicated DDL -- confirm "
                          "which is authoritative", SOURCE)

    undeclared = sorted({t for (_o, t) in access} - set(tables))
    for t in undeclared[:40]:
        store.add_gap("table-not-declared-locally",
                      "table %s is queried but no CREATE statement was found in "
                      "the repos on disk" % t,
                      "its DDL likely lives in an external schema repo",
                      SOURCE)

    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"sql_files": n_files, "tables": len(tables),
            "ddl_statements": n_ddl, "queries": n_dml,
            "table_access_edges": len(edges),
            "shared_tables": shared, "undeclared_tables": len(undeclared)}


def _line_index(text):
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)

    def line_of(pos):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1
    return line_of
