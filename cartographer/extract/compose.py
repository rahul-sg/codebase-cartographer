"""
docker-compose as the local service/schema manifest.

On a large multi-service estate this file is the single clearest statement of
which services run locally and which MySQL schema each one connects to, via
`db_uri` env vars. It is also where the "one service, one schema" assumption
breaks: `nexus` and `common` both point at `coredev`, which a per-module
reading would never reveal.

Parsed with the mini-YAML reader, then walked structurally rather than by
regex, so `environment:` in both list and mapping form works.
"""
from __future__ import annotations

import os
import re

from .. import ids, miniyaml
from ..config import SKIP_DIRS, prune, datastore_id

SOURCE = "compose"

COMPOSE_NAME = re.compile(r'^docker-compose.*\.ya?ml$|^compose\.ya?ml$', re.I)
JDBC = re.compile(
    r'jdbc:(\w+)://([^/\s:]+)(?::(\d+))?/([\w$]+)', re.I)
GENERIC_DSN = re.compile(
    r'\b(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|cassandra)://'
    r'(?:[^@\s/]+@)?([^/\s:]+)(?::(\d+))?(?:/([\w$]+))?', re.I)
# Scheme-less DSNs — `db_uri: mysqldb:3306/orgdev`. Common when the driver is
# configured elsewhere and the env var carries only host:port/schema. A bare
# `host:port/path` is far too generic to match on sight, so this is only
# trusted when the env key itself names a database URI (DB_URI_KEY below).
BARE_DSN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*):(\d{2,5})/([\w$]+)$')
DB_URI_KEY = re.compile(
    r'(?:db|database|datasource|jdbc)[\w-]*(?:uri|url|dsn)'
    r'|(?:uri|url|dsn)[\w-]*(?:db|database)', re.I)
# The engine is absent from a scheme-less DSN; infer it from the host name
# (`mysqldb` -> mysql) and stay honest with "unknown" when it is not evident.
ENGINE_BY_HOST = (("mysql", "mysql"), ("mariadb", "mysql"),
                  ("postgres", "postgres"), ("sqlserver", "mssql"),
                  ("mssql", "mssql"), ("oracle", "oracle"))


def _engine_from_host(host):
    h = (host or "").lower()
    for token, engine in ENGINE_BY_HOST:
        if token in h:
            return engine
    return "unknown"
# Substring, not word-boundary: real container names are `mysqldb`,
# `acme_mysql_db`, `kafka-broker-1`. A \b anchor matches none of those.
INFRA_TOKENS = ("mysql", "mariadb", "postgres", "redis", "kafka", "zookeeper",
                "elasticsearch", "opensearch", "rabbitmq", "mongo", "nginx",
                "vault", "consul", "localstack", "minio", "memcached",
                "cassandra", "clickhouse", "prometheus", "grafana", "jaeger",
                "schema-registry", "ldap", "sonarqube", "mailhog")


def _is_infra(container_name, image):
    hay = ("%s %s" % (container_name, image)).lower()
    return any(tok in hay for tok in INFRA_TOKENS)


def _find(root, follow=False):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
        prune(dirpath, dirnames)
        for fn in sorted(filenames):
            if COMPOSE_NAME.match(fn):
                yield os.path.join(dirpath, fn)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None, None
    try:
        import yaml
        return yaml.safe_load(text), text
    except ImportError:
        pass
    except Exception:
        return None, text
    try:
        return miniyaml.loads(text), text
    except Exception:
        return None, text


def _env_items(env):
    """`environment:` is legal as a list of KEY=VAL or a mapping. Handle both."""
    out = {}
    if isinstance(env, dict):
        for k, v in env.items():
            out[str(k)] = "" if v is None else str(v)
    elif isinstance(env, list):
        for item in env:
            if not isinstance(item, str):
                continue
            k, _sep, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    follow = cfg.defaults.get("follow_symlinks", False)
    nodes, edges = [], []
    schema_users = {}
    n_files = n_services = 0

    roots = [(r, root) for r, root, _s in repos]
    for root in cfg.roots:
        if not any(os.path.commonpath([root, rr]) == rr
                   for _r, rr in roots if os.path.isdir(rr)):
            roots.append((os.path.basename(root.rstrip("/")) or "root", root))

    seen_files = set()
    for repo_name, repo_root in roots:
        if not os.path.isdir(repo_root):
            continue
        for path in _find(repo_root, follow):
            rp = os.path.realpath(path)
            if rp in seen_files:
                continue
            seen_files.add(rp)
            doc, text = _load(path)
            if not isinstance(doc, dict):
                continue
            svcs = doc.get("services")
            if not isinstance(svcs, dict):
                continue
            n_files += 1
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            line_of = _line_index(text or "")
            base_ev = "%s/%s" % (repo_name, rel)

            for cname, body in svcs.items():
                if not isinstance(body, dict):
                    continue
                n_services += 1
                image = str(body.get("image") or "")
                is_infra = _is_infra(str(cname), image)
                canon = (cfg.resolve_service(cname)
                         or cfg.resolve_service(image)
                         or str(cname))
                ev = base_ev
                if text:
                    idx = text.find("\n  %s:" % cname)
                    if idx >= 0:
                        ev = "%s:%d" % (base_ev, line_of(idx + 1))

                if is_infra:
                    nid = "cart . infra %s" % cname
                    nodes.append({"id": nid, "kind": "infra", "name": str(cname),
                                  "extra": {"image": image, "compose": ev}})
                else:
                    nodes.append({"id": ids.service_id(canon), "kind": "service",
                                  "name": canon, "service": canon,
                                  "extra": {"image": image, "compose": ev,
                                            "container_name": str(cname),
                                            "runs_locally": True}})

                env = _env_items(body.get("environment"))
                for key, val in env.items():
                    for m in JDBC.finditer(val):
                        engine, host, port, schema = m.groups()
                        _record(nodes, edges, schema_users, canon, engine, host,
                                port, schema, ev, key, is_infra)
                    for m in GENERIC_DSN.finditer(val):
                        engine, host, port, schema = m.groups()
                        if schema:
                            _record(nodes, edges, schema_users, canon, engine,
                                    host, port, schema, ev, key, is_infra)
                    # Scheme-less `host:port/schema`, only for db-URI keys and
                    # only when no schemed DSN already matched, so a value like
                    # `jdbc:mysql://h:3306/db` is never recorded twice.
                    if (DB_URI_KEY.search(key)
                            and not JDBC.search(val)
                            and not GENERIC_DSN.search(val)):
                        m = BARE_DSN.match(val.strip())
                        if m:
                            host, port, schema = m.groups()
                            _record(nodes, edges, schema_users, canon,
                                    _engine_from_host(host), host, port,
                                    schema, ev, key, is_infra)

                for dep in (body.get("depends_on") or []):
                    if isinstance(dep, dict):
                        continue
                    target = cfg.resolve_service(dep)
                    if target and target != canon and not is_infra:
                        nodes.append({"id": ids.service_id(target),
                                      "kind": "service", "name": target,
                                      "service": target})
                        edges.append({"src": ids.service_id(canon),
                                      "dst": ids.service_id(target),
                                      "kind": "http", "evidence": ev,
                                      "provenance": "EXTRACTED",
                                      "confidence": 0.6,
                                      "extra": {"via": "compose depends_on"}})

    for dsn, users in sorted(schema_users.items()):
        if len(users) > 1:
            store.add_gap(
                "shared-schema",
                "schema %s is configured for %d services: %s"
                % (dsn, len(users), ", ".join(sorted(users))),
                "one schema per service is the usual assumption and it does not "
                "hold here. Confirm which service owns the DDL and whether the "
                "others are permitted to write.", SOURCE)

    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"compose_files": n_files, "containers": n_services,
            "schemas": len(schema_users),
            "shared_schemas": sum(1 for u in schema_users.values() if len(u) > 1)}


def _record(nodes, edges, schema_users, svc, engine, host, port, schema, ev,
            key, is_infra):
    dsn = datastore_id(engine, host, schema)
    did = "cart . datastore %s" % dsn
    nodes.append({"id": did, "kind": "datastore", "name": dsn,
                  "extra": {"engine": engine, "host": host, "port": port,
                            "schema": schema}})
    if is_infra:
        return
    schema_users.setdefault(dsn, set()).add(svc)
    nodes.append({"id": ids.service_id(svc), "kind": "service", "name": svc,
                  "service": svc})
    edges.append({"src": ids.service_id(svc), "dst": did,
                  "kind": "uses-datastore", "evidence": ev,
                  "provenance": "EXTRACTED", "confidence": 1.0,
                  "extra": {"via": "%s" % key, "schema": schema}})


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
