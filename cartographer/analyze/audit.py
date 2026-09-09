"""
Recall audit: what the extractors MISSED.

Every other command reports what was found. None of them report what was not,
and that asymmetry is dangerous: a map that silently omits a third of the
endpoints looks exactly like a map of a codebase with a third fewer endpoints.

Two real bugs on this codebase were caught by luck rather than by the tool:

  * class-level `@RequestMapping(ApiPaths.API_BASEPATH_V1 + "/x")` was
    unreadable, so ~87% of controllers recorded their methods at the wrong
    path -- and the test suite stayed green because its fixture used a plain
    string literal
  * scheme-less DSNs (`db_uri: mysqldb:3306/orgdev`) matched neither DSN
    pattern, so compose-derived schema detection would have found zero schemas

Both were recall failures, and both were invisible in the output.

So each check here probes the raw source with a **deliberately independent**
broad pattern, then compares that against what the extractor produced. The
probes are intentionally cruder than the extractors: sharing code with them
would mean sharing their blind spots, which is the whole thing being tested.

A check reports a ratio and examples, never a pass/fail verdict on its own. A
gap can be correct -- an abstract controller declares no routes, a `CREATE
TABLE` may live in a repo that is not here. The point is to make the gap
visible so a human decides, instead of it being absorbed silently.
"""
from __future__ import annotations

import os
import re

from ..config import prune

MAX_BYTES = 1_500_000

# --- independent probes ---------------------------------------------------
# Crude on purpose. If these matched exactly what the extractors match, the
# audit would only ever confirm the extractors' own assumptions.

CONTROLLER = re.compile(r"@(?:Rest)?Controller\b")
CLASS_MAPPING = re.compile(r"@RequestMapping\s*\(([^)]*)\)")
METHOD_MAPPING = re.compile(r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\b")
CREATE_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([\w.$]+)", re.I)
API_URL = re.compile(r"""["'`](/[a-z][a-z0-9\-_]*/[a-z0-9\-_{}$/.]*)["'`]""", re.I)
KAFKA_SIGNAL = re.compile(
    r"@KafkaListener\b|kafkaTemplate\s*\.\s*send|\.send\s*\(\s*[\"'][\w.\-]{3,}",
    re.I)
DB_URI_LINE = re.compile(r"\bdb_uri\w*\s*[:=]", re.I)


def _files(root, exts, follow=False):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
        prune(dirpath, dirnames)
        for fn in sorted(filenames):
            if not fn.endswith(exts):
                continue
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > MAX_BYTES:
                    continue
            except OSError:
                continue
            yield p


def _read(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _check(name, probed, produced, examples, note, unit="items"):
    ratio = (float(produced) / probed) if probed else None
    return {"name": name, "probed": probed, "produced": produced,
            "ratio": ratio, "examples": examples[:8], "note": note,
            "unit": unit}


# --- checks ---------------------------------------------------------------

def _controllers_with_routes(store, repos):
    """Every Spring controller should contribute at least one route."""
    have = set()
    for r in store.conn.execute(
            "SELECT DISTINCT d.file AS f FROM edges e "
            "JOIN nodes d ON d.id = e.dst WHERE e.kind='handled-by'"):
        if r["f"]:
            have.add(os.path.basename(r["f"]))

    probed, missing = 0, []
    for _name, root, _svc in repos:
        for p in _files(root, (".java", ".kt")):
            txt = _read(p)
            if not txt or not CONTROLLER.search(txt):
                continue
            if not METHOD_MAPPING.search(txt):
                continue          # a controller with no mapped method is fine
            probed += 1
            if os.path.basename(p) not in have:
                missing.append(os.path.relpath(p, root))
    return _check(
        "spring controllers -> routes", probed, probed - len(missing), missing,
        "A controller with mapped methods but no route in the graph means the "
        "annotation shape was not understood. Abstract or commented-out "
        "controllers are legitimate misses.",
        unit="controllers")


def _base_path_applied(store, repos):
    """
    A class-level @RequestMapping prefix should appear in that controller's
    route paths.

    This is the check that would have caught the constant-expression bug: the
    routes existed, so a presence check passed, but every path was missing its
    base. Consistency, not existence, is what was broken.
    """
    routes_by_file = {}
    for r in store.conn.execute(
            "SELECT d.file AS f, s.name AS route FROM edges e "
            "JOIN nodes d ON d.id = e.dst JOIN nodes s ON s.id = e.src "
            "WHERE e.kind='handled-by'"):
        if r["f"]:
            routes_by_file.setdefault(os.path.basename(r["f"]), []).append(
                r["route"] or "")

    probed, bad = 0, []
    for _name, root, _svc in repos:
        for p in _files(root, (".java", ".kt")):
            txt = _read(p)
            if not txt or not CONTROLLER.search(txt):
                continue
            m = CLASS_MAPPING.search(txt)
            if not m:
                continue
            # Any quoted literal inside the class mapping is a segment the
            # composed paths must contain. Constant references are skipped:
            # their value is unknown to this probe by design.
            lits = re.findall(r'"([^"]+)"', m.group(1))
            seg = next((l.strip("/") for l in lits if l.strip("/")), None)
            if not seg:
                continue
            rts = routes_by_file.get(os.path.basename(p))
            if not rts:
                continue
            probed += 1
            if not any(seg in (r or "") for r in rts):
                bad.append("%s (expected '%s' in its paths, got e.g. '%s')"
                           % (os.path.relpath(p, root), seg,
                              (rts[0] or "")[:48]))
    return _check(
        "class @RequestMapping applied", probed, probed - len(bad), bad,
        "The class-level prefix is missing from the composed paths. Every "
        "route in that controller is recorded at the wrong URL, which no "
        "presence check would reveal.",
        unit="controllers")


def _ddl_captured(store, repos):
    """CREATE TABLE statements on disk should become declared tables."""
    declared = {r["name"].upper() for r in store.conn.execute(
        "SELECT DISTINCT d.name AS name FROM edges e "
        "JOIN nodes d ON d.id = e.dst WHERE e.kind='defines-table'")
        if r["name"]}
    seen, missing = set(), []
    for _name, root, _svc in repos:
        for p in _files(root, (".sql",)):
            txt = _read(p)
            if not txt:
                continue
            for m in CREATE_TABLE.finditer(txt):
                t = m.group(1).split(".")[-1].strip('`"[]').upper()
                if not t or t in seen:
                    continue
                seen.add(t)
                if t not in declared:
                    missing.append("%s (%s)" % (t, os.path.relpath(p, root)))
    return _check(
        "CREATE TABLE -> declared tables", len(seen), len(seen) - len(missing),
        missing,
        "A table created on disk but not declared in the graph means the DDL "
        "parser skipped that statement shape.",
        unit="tables")


def _frontend_calls_accounted(store, repos):
    """
    Every API-ish URL in frontend source should be either linked to a route or
    explicitly recorded as unlinked -- never silently dropped.
    """
    linked = store.conn.execute(
        "SELECT COUNT(*) n FROM edges WHERE kind='calls-route'").fetchone()["n"]
    unlinked = store.conn.execute(
        "SELECT COUNT(*) n FROM gaps WHERE category='unlinked-api-call'"
    ).fetchone()["n"]

    probed, examples = 0, []
    for name, root, _svc in repos:
        # Frontend repos only; a URL literal in Java is usually an outbound
        # call, which is the topology extractor's business, not this one's.
        if not os.path.exists(os.path.join(root, "package.json")) and \
           not any(os.path.exists(os.path.join(root, d, "package.json"))
                   for d in ("ui", "OrderAndStock", "src")):
            continue
        for p in _files(root, (".ts", ".js", ".tsx", ".jsx")):
            low = p.lower()
            if ".spec." in low or ".test." in low:
                continue
            txt = _read(p)
            if not txt or "http" not in txt:
                continue
            for m in API_URL.finditer(txt):
                url = m.group(1)
                if url.startswith(("/assets/", "/static/", "/images/")):
                    continue
                if len(url.split("/")) < 3:
                    continue
                probed += 1
                if probed <= 40:
                    examples.append("%s  %s" % (os.path.basename(p), url))
    return _check(
        "frontend API URLs accounted for", probed, linked + unlinked, [],
        "Probed with a deliberately loose URL pattern, so it over-counts "
        "(router paths, i18n keys). A number well BELOW the probe is expected; "
        "one near zero would mean call sites are being dropped, not linked.",
        unit="URL literals")


def _compose_schemas(store, repos):
    """Every db_uri-ish line in compose should yield a datastore."""
    stores = store.conn.execute(
        "SELECT COUNT(*) n FROM nodes WHERE kind='datastore'").fetchone()["n"]
    probed = 0
    for _name, root, _svc in repos:
        for p in _files(root, (".yml", ".yaml")):
            if "compose" not in os.path.basename(p).lower():
                continue
            txt = _read(p) or ""
            probed += len(DB_URI_LINE.findall(txt))
    return _check(
        "compose db_uri -> datastores", probed, stores, [],
        "Read-only replicas and repeated URIs collapse into one datastore, so "
        "produced is legitimately lower. Zero datastores against a non-zero "
        "probe is the failure this catches.",
        unit="db_uri lines")


def _kafka_signals(store, repos):
    """
    Services whose code touches Kafka should appear on a topic edge.

    Counted per SERVICE, not per file and not against an edge total: a service
    with 40 Kafka files still needs only one edge to be represented, so
    comparing files to edges produced a meaningless ratio above 100%.
    """
    on_edge = set()
    for r in store.conn.execute(
            "SELECT s.name AS a, d.name AS b, s.kind AS ak, d.kind AS bk "
            "FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
            "WHERE e.kind IN ('produces','consumed-by','event')"):
        if r["ak"] == "service" and r["a"]:
            on_edge.add(r["a"])
        if r["bk"] == "service" and r["b"]:
            on_edge.add(r["b"])

    signalling, missing = set(), []
    for name, root, _svc in repos:
        for p in _files(root, (".java", ".kt", ".ts", ".js", ".py")):
            txt = _read(p)
            if txt and KAFKA_SIGNAL.search(txt):
                signalling.add(name)
                break
    for name in sorted(signalling):
        if name not in on_edge:
            missing.append(name)
    return _check(
        "services touching Kafka -> topic edges",
        len(signalling), len(signalling) - len(missing), missing,
        "A service whose code sends or listens but appears on no topic edge "
        "means its topic name was built in a way the extractor could not "
        "resolve -- commonly a runtime-composed constant.",
        unit="services")


CHECKS = (
    _controllers_with_routes,
    _base_path_applied,
    _ddl_captured,
    _frontend_calls_accounted,
    _compose_schemas,
    _kafka_signals,
)


def run(store, repos, only=None):
    out = []
    for fn in CHECKS:
        res = fn(store, repos)
        if only and only not in res["name"]:
            continue
        out.append(res)
    return out
