"""
Cross-service topology: the layer no AST can reach.

A symbol graph stops at the process boundary. The fact that order-svc calls
pricing-svc exists only as a string in a config file, an annotation, or a topic
name -- so that is what we read.

Every edge carries file:line evidence and a provenance flag. Nothing here is
allowed to assert an edge it cannot cite.
"""
from __future__ import annotations

import os
import re

from .. import ids
from ..config import SKIP_DIRS, prune, datastore_id

SOURCE = "topology"

CONFIG_EXT = (".yml", ".yaml", ".properties", ".conf", ".cfg", ".ini", ".env",
              ".tf", ".tfvars", ".json", ".toml", ".xml", ".sh", ".dockerfile")
CONFIG_NAMES = ("dockerfile", "makefile", "procfile", ".env")
CODE_EXT = (".java", ".kt", ".scala", ".groovy", ".py", ".ts", ".tsx", ".js",
            ".jsx", ".go", ".cs", ".rb", ".rs", ".php")

MAX_BYTES = 1_500_000

# --- patterns -------------------------------------------------------------

# ORDER_SERVICE_URL / pricing.service.url / PRICING_SVC_HOST
ENV_URL = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_.\-]{2,60}?)[_.\-](?:URL|URI|HOST|HOSTNAME|ENDPOINT|"
    r"BASE_URL|BASE_URI|BASEURL|ADDR|ADDRESS|SERVER)\b", re.I)

# http://pricing-svc:8080  /  https://order-service.internal/api
URL_LITERAL = re.compile(
    r"https?://([A-Za-z0-9_.\-]+)(?::\d+)?(?:/[^\s\"'`,)]*)?", re.I)

# Spring Cloud / service discovery
ANNOTATION_CLIENT = re.compile(
    r"@(?:FeignClient|GrpcClient|RegisterRestClient|RibbonClient)\s*\(([^)]*)\)", re.I)
ANNOTATION_NAME = re.compile(r"""(?:name|value|serviceId|configKey)\s*=\s*["']([^"']+)["']""", re.I)
ANNOTATION_BARE = re.compile(r"""^\s*["']([^"']+)["']""")

# lb://order-service, service:order-service
DISCOVERY_URI = re.compile(r"\b(?:lb|service|discovery)://([A-Za-z0-9_.\-]+)", re.I)

# Messaging
PRODUCE = re.compile(
    r"""(?:kafkaTemplate|producer|template|bus|emitter|eventBus|publisher)?\s*\.?\s*"""
    r"""(?:send|publish|produce|emit|dispatch|sendMessage|publishEvent)\s*\(\s*["']([\w.\-/]{3,120})["']""",
    re.I)
CONSUME_ANN = re.compile(
    r"""@(?:KafkaListener|RabbitListener|StreamListener|JmsListener|SqsListener|"""
    r"""EventListener|Incoming|Consume)\s*\(([^)]*)\)""", re.I)
TOPIC_ATTR = re.compile(r"""(?:topics?|queues?|destination|value|channel)\s*=\s*\{?\s*["']([\w.\-/]{3,120})["']""", re.I)
SUBSCRIBE = re.compile(
    r"""(?:subscribe|consume|listen|on)\s*\(\s*["']([\w.\-/]{3,120})["']""", re.I)
# Consumer objects constructed with their topic inline -- the common Python and
# Node shape, which no annotation-based pattern would ever catch.
CONSUMER_CTOR = re.compile(
    r"""(?:Kafka|Pulsar|Rabbit|SQS|PubSub|Event|Message|Stream)?Consumer\s*\("""
    r"""(?:[^)]*?topics?\s*=\s*)?\[?\s*["']([\w.\-/]{3,120})["']""", re.I)
TOPIC_CONFIG = re.compile(
    r"""(?:^|[\s:=\"'])(?:topic|queue|channel|stream|subject)s?[\s:=]+["']?([\w][\w.\-]{2,120})["']?""", re.I)
# Event-shaped literals: order.submitted, oms.order.created
EVENT_LITERAL = re.compile(
    r"""["']([a-z][\w\-]*(?:[.\-][\w\-]+){1,5}\.(?:created|updated|deleted|changed|"""
    r"""completed|failed|received|submitted|cancelled|shipped|placed|approved|"""
    r"""rejected|processed|synced|expired|requested|confirmed))["']""", re.I)

# Databases
DB_URL = re.compile(
    r"\b(?:jdbc:(\w+)://([^\s\"'?;]+)|(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|"
    r"mariadb|redis|cassandra|clickhouse|oracle|sqlserver)://([^\s\"'?]+))", re.I)

# Container / orchestration
COMPOSE_DEPENDS = re.compile(r"^\s*-\s+([A-Za-z0-9_.\-]+)\s*$")
IMAGE_LINE = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.I)
K8S_SERVICE_HOST = re.compile(
    r"\b([a-z0-9\-]{2,60})\.([a-z0-9\-]{2,40})\.svc(?:\.cluster\.local)?\b", re.I)

# HTTP routes (used to describe the contract surface of a service)
ROUTE_ANN = re.compile(
    r"""@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["']([^"']+)["']""", re.I)

# --- Spring controllers: the path is split across two annotations ----------
# A class carries the prefix and each method carries the rest, so reading only
# method annotations yields "/{id}" instead of "/order/api/v1/orders/{id}".
CONTROLLER_ANN = re.compile(r"@(?:Rest)?Controller\b")
CLASS_DECL = re.compile(r"^\s*(?:@\w+[^\n]*\s*)*(?:public\s+|final\s+|abstract\s+)*"
                        r"(?:class|interface)\s+(\w+)")
MAPPING_ANY = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b\s*(\((?P<args>[^)]*)\))?", re.I)
MAPPING_PATH = re.compile(r"""(?:^|[\s(,])(?:value|path)\s*=\s*\{?\s*["']([^"']*)["']""", re.I)
MAPPING_BARE = re.compile(r"""^\s*\{?\s*["']([^"']*)["']""")
MAPPING_METHOD = re.compile(r"RequestMethod\.(\w+)", re.I)

# Cache usage is annotation-mediated here, so direct RedisTemplate calls are
# rare and grepping for them finds almost nothing.
CACHE_ANN = re.compile(r"@(\w*Redis\w*Cacheable|Cacheable|CacheEvict|CachePut)\b")
# JNDI-resolved datasources: the connection string lives in a container, not
# in the repo, so the JNDI name is all we can see.
JNDI = re.compile(r"""["']?(java:/?(?:comp/env/)?jdbc/[\w.\-/]+)["']?""", re.I)
JNDI_PROP = re.compile(r"""(?:jndi[-_.]?name|jndiName)\s*[:=]\s*["']?([\w:/.\-]+)""", re.I)
ROUTE_DECOR = re.compile(
    r"""@(?:app|router|bp|api)\.(get|post|put|delete|patch|route)\s*\(\s*["']([^"']+)["']""", re.I)
ROUTE_EXPRESS = re.compile(
    r"""\b(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*["']([^"']+)["']""", re.I)

NOISE_HOSTS = frozenset("""
localhost 127.0.0.1 0.0.0.0 example.com example.org test.com foo.com bar.com
schemas.xmlsoap.org www.w3.org xmlns.jcp.org java.sun.com maven.apache.org
github.com gitlab.com bitbucket.org npmjs.com pypi.org docs.oracle.com
apache.org spring.io localhost.localdomain host.docker.internal
""".split())


def spring_routes(text):
    """
    Compose full HTTP paths from class-level + method-level annotations.

    Returns [(verb, path, line)]. A controller with no class-level mapping
    still works; the prefix is simply empty.
    """
    if not CONTROLLER_ANN.search(text):
        return []
    lines = text.split("\n")
    class_line = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*(?:public\s+|final\s+|abstract\s+)*(?:class|interface)\s+\w+", line):
            class_line = i
            break
    if class_line is None:
        return []

    prefix = ""
    for i in range(max(0, class_line - 12), class_line):
        m = MAPPING_ANY.search(lines[i])
        if m and m.group(1).lower() == "request":
            args = m.group("args") or ""
            pm = MAPPING_PATH.search(args) or MAPPING_BARE.match(args)
            if pm:
                prefix = pm.group(1)
            break

    out = []
    for i in range(class_line + 1, len(lines)):
        line = lines[i]
        m = MAPPING_ANY.search(line)
        if not m:
            continue
        kind = m.group(1).lower()
        args = m.group("args") or ""
        if kind == "request":
            mm = MAPPING_METHOD.search(args)
            verb = mm.group(1).upper() if mm else "ANY"
        else:
            verb = kind.upper()
        pm = MAPPING_PATH.search(args) or MAPPING_BARE.match(args)
        sub = pm.group(1) if pm else ""
        full = "/" + "/".join(
            seg for seg in (prefix + "/" + sub).split("/") if seg)
        out.append((verb, full or "/", i + 1))
    return out


def _relevant_files(repo_root, follow=False):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        prune(dirpath, dirnames)
        for fn in sorted(filenames):
            low = fn.lower()
            if low.endswith(CONFIG_EXT) or low.endswith(CODE_EXT) \
               or low in CONFIG_NAMES or low.startswith(".env") \
               or "docker-compose" in low:
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > MAX_BYTES:
                        continue
                except OSError:
                    continue
                yield p


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().split("\n")
    except (OSError, UnicodeError):
        return []


def _clean_host(h):
    h = (h or "").strip().strip("/").lower()
    if not h or h in NOISE_HOSTS:
        return None
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        return None
    if "${" in h or "{{" in h or h.startswith("$"):
        return None
    return h


def run(store, cfg, repos, progress=None):
    """repos: list of (repo_name, repo_root, service_name)."""
    store.clear_source(SOURCE)

    services = {}          # canonical name -> node dict
    edges = []
    cache_use = {}         # service -> {annotation: evidence}
    jndi = {}              # service -> {jndi name: evidence}
    produces = {}          # topic -> set(service)
    consumes = {}          # topic -> set(service)
    topic_evidence = {}    # (service, topic, role) -> "path:line"
    db_users = {}          # dsn-ish key -> set((service, evidence))
    routes = []
    gaps = []

    def ensure_service(name, repo=None):
        if not name:
            return None
        if name not in services:
            services[name] = {"id": ids.service_id(name), "kind": "service",
                              "name": name, "repo": repo, "service": name}
        elif repo and not services[name].get("repo"):
            services[name]["repo"] = repo
        return services[name]["id"]

    for repo_name, repo_root, svc_name in repos:
        # Deliberately NOT ensure_service(repo_name) here: a repo that holds
        # many modules is a container, not a service, and creating a node for
        # it produces a phantom that collects every unattributed finding.

        for path in _relevant_files(repo_root, cfg.defaults.get("follow_symlinks", False)):
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            low_name = os.path.basename(path).lower()
            is_compose = "docker-compose" in low_name
            lines = _read_lines(path)
            # In a repo of Maven modules the repo name is far too coarse: every
            # finding would be attributed to `ong-server-repo` and every schema
            # would look shared. Resolve the owning module per file.
            me = _module_owner(cfg, repo_root, path, svc_name or repo_name)
            ensure_service(me, repo_name)

            # Java controllers need whole-file context to compose paths.
            if path.endswith((".java", ".kt")) and "Mapping" in "\n".join(lines[:400]):
                for verb, full, ln in spring_routes("\n".join(lines)):
                    routes.append((me, verb, full,
                                   "%s/%s:%d" % (repo_name, rel, ln)))

            for i, line in enumerate(lines, start=1):
                if not line.strip() or len(line) > 4000:
                    continue
                ev = "%s/%s:%d" % (repo_name, rel, i)
                stripped = line.strip()
                if stripped.startswith(("#", "//", "*", "<!--")):
                    continue

                # 1. env-var style service references
                for m in ENV_URL.finditer(line):
                    target = cfg.resolve_service(m.group(1))
                    if target and target != me:
                        ensure_service(target)
                        edges.append(_edge(me, target, "http", ev,
                                           "EXTRACTED", 0.85,
                                           "env var %s" % m.group(0)))

                # 2. literal URLs
                for m in URL_LITERAL.finditer(line):
                    host = _clean_host(m.group(1))
                    if not host:
                        continue
                    base = host.split(".")[0]
                    target = cfg.resolve_service(host) or cfg.resolve_service(base)
                    if target and target != me:
                        ensure_service(target)
                        edges.append(_edge(me, target, "http", ev,
                                           "EXTRACTED", 0.9, m.group(0)[:120]))
                    elif "." not in host and host not in NOISE_HOSTS:
                        gaps.append(("unknown-host",
                                     "%s references host '%s' which is not a "
                                     "configured service" % (ev, host),
                                     "add it to services in the config, or note "
                                     "it as external"))

                # 3. declarative clients and service discovery
                for m in ANNOTATION_CLIENT.finditer(line):
                    body = m.group(1)
                    nm = ANNOTATION_NAME.search(body) or ANNOTATION_BARE.search(body)
                    if nm:
                        target = cfg.resolve_service(nm.group(1))
                        if target and target != me:
                            ensure_service(target)
                            edges.append(_edge(me, target, "http", ev,
                                               "EXTRACTED", 0.95,
                                               "declarative client %s" % nm.group(1)))
                for m in DISCOVERY_URI.finditer(line):
                    target = cfg.resolve_service(m.group(1))
                    if target and target != me:
                        ensure_service(target)
                        edges.append(_edge(me, target, "http", ev, "EXTRACTED",
                                           0.95, m.group(0)))
                for m in K8S_SERVICE_HOST.finditer(line):
                    target = cfg.resolve_service(m.group(1))
                    if target and target != me:
                        ensure_service(target)
                        edges.append(_edge(me, target, "http", ev, "EXTRACTED",
                                           0.95, "k8s dns %s" % m.group(0)))

                # 4. messaging
                for m in PRODUCE.finditer(line):
                    t = _topic(m.group(1))
                    if t:
                        produces.setdefault(t, set()).add(me)
                        topic_evidence.setdefault((me, t, "produce"), ev)
                for m in CONSUME_ANN.finditer(line):
                    for tm in TOPIC_ATTR.finditer(m.group(1)):
                        t = _topic(tm.group(1))
                        if t:
                            consumes.setdefault(t, set()).add(me)
                            topic_evidence.setdefault((me, t, "consume"), ev)
                for m in SUBSCRIBE.finditer(line):
                    t = _topic(m.group(1))
                    if t:
                        consumes.setdefault(t, set()).add(me)
                        topic_evidence.setdefault((me, t, "consume"), ev)
                for m in CONSUMER_CTOR.finditer(line):
                    t = _topic(m.group(1))
                    if t:
                        consumes.setdefault(t, set()).add(me)
                        topic_evidence.setdefault((me, t, "consume"), ev)
                for m in EVENT_LITERAL.finditer(line):
                    t = _topic(m.group(1))
                    if not t:
                        continue
                    # Direction unknown from a bare literal; record as seen so
                    # the topic exists as a node even if we cannot orient it.
                    topic_evidence.setdefault((me, t, "mention"), ev)

                # 5. databases (shared schema is a hidden coupling).
                # Skipped inside compose files: those describe other services,
                # and extract/compose.py attributes them correctly.
                for m in (() if is_compose else DB_URL.finditer(line)):
                    dsn = (m.group(2) or m.group(4) or "").strip().rstrip("/")
                    engine = (m.group(1) or m.group(3) or "db").lower()
                    if not dsn or "${" in dsn:
                        continue
                    dsn = dsn.split("?")[0]
                    hostpart, _, schema = dsn.partition("/")
                    key = datastore_id(engine, hostpart, schema)
                    db_users.setdefault(key, set()).add((me, ev))

                # 6. compose / k8s
                if is_compose:
                    im = IMAGE_LINE.match(line)
                    if im:
                        target = cfg.resolve_service(im.group(1))
                        if target:
                            ensure_service(target)
                    dm = COMPOSE_DEPENDS.match(line)
                    if dm:
                        target = cfg.resolve_service(dm.group(1))
                        if target and target != me:
                            ensure_service(target)
                            edges.append(_edge(me, target, "http", ev,
                                               "EXTRACTED", 0.7,
                                               "compose depends_on"))

                # 7. non-Java routes (Python decorators, Express)
                for m in ROUTE_DECOR.finditer(line):
                    verb = m.group(1).upper()
                    routes.append((me, "ANY" if verb == "ROUTE" else verb,
                                   m.group(2), ev))
                for m in ROUTE_EXPRESS.finditer(line):
                    routes.append((me, m.group(1).upper(), m.group(2), ev))

                # 8. cache annotations and JNDI datasources
                cm = CACHE_ANN.search(line)
                if cm:
                    cache_use.setdefault(me, {}).setdefault(cm.group(1), ev)
                for m in JNDI.finditer(line):
                    jndi.setdefault(me, {}).setdefault(m.group(1), ev)
                for m in JNDI_PROP.finditer(line):
                    jndi.setdefault(me, {}).setdefault(m.group(1), ev)

    # -- topics become nodes; producer->consumer becomes an event edge -----
    topic_nodes = {}
    all_topics = set(produces) | set(consumes)
    for (svc, t, role), ev in topic_evidence.items():
        all_topics.add(t)
    for t in all_topics:
        tid = "cart . topic %s" % t
        topic_nodes[t] = {"id": tid, "kind": "topic", "name": t}

    for t in all_topics:
        tid = topic_nodes[t]["id"]
        for p in produces.get(t, ()):
            ensure_service(p)
            edges.append(_edge_raw(ids.service_id(p), tid, "produces",
                                   topic_evidence.get((p, t, "produce")),
                                   "EXTRACTED", 0.9, t))
        for c in consumes.get(t, ()):
            ensure_service(c)
            edges.append(_edge_raw(tid, ids.service_id(c), "consumed-by",
                                   topic_evidence.get((c, t, "consume")),
                                   "EXTRACTED", 0.9, t))
        for p in produces.get(t, ()):
            for c in consumes.get(t, ()):
                if p == c:
                    continue
                edges.append(_edge(p, c, "event",
                                   topic_evidence.get((p, t, "produce"))
                                   or topic_evidence.get((c, t, "consume")),
                                   "EXTRACTED", 0.85, "topic %s" % t))
        if t in produces and t not in consumes:
            gaps.append(("unconsumed-topic",
                         "topic '%s' is produced by %s but no consumer was found"
                         % (t, ", ".join(sorted(produces[t]))),
                         "the consumer may live in a repo you cannot see"))
        if t in consumes and t not in produces:
            gaps.append(("unproduced-topic",
                         "topic '%s' is consumed by %s but no producer was found"
                         % (t, ", ".join(sorted(consumes[t]))),
                         "the producer may live in a repo you cannot see"))

    # -- shared databases --------------------------------------------------
    db_nodes = []
    for dsn, users in db_users.items():
        names = sorted({u for u, _ in users})
        did = "cart . datastore %s" % dsn
        db_nodes.append({"id": did, "kind": "datastore", "name": dsn})
        for svc, ev in users:
            ensure_service(svc)
            edges.append(_edge_raw(ids.service_id(svc), did, "uses-datastore",
                                   ev, "EXTRACTED", 0.9, dsn))
        if len(names) > 1:
            gaps.append(("shared-datastore",
                         "%s is used by %d services: %s"
                         % (dsn, len(names), ", ".join(names)),
                         "a shared schema is a coupling that no API contract "
                         "documents; confirm which service owns it"))

    # -- route nodes -------------------------------------------------------
    route_nodes = []
    seen_routes = set()
    for svc, method, pattern, ev in routes:
        key = (svc, method, pattern)
        if key in seen_routes:
            continue
        seen_routes.add(key)
        rid = "cart . route %s %s %s" % (svc, method or "ANY", pattern)
        route_nodes.append({"id": rid, "kind": "route", "name":
                            ("%s %s" % (method or "ANY", pattern)).strip(),
                            "service": svc})
        edges.append(_edge_raw(ids.service_id(svc), rid, "exposes", ev,
                               "EXTRACTED", 0.95, pattern))

    # cache + JNDI as first-class infrastructure edges
    for svc, anns in cache_use.items():
        ensure_service(svc)
        cid = "cart . infra cache"
        db_nodes.append({"id": cid, "kind": "infra", "name": "cache (Redis)"})
        for ann, ev in list(anns.items())[:1]:
            edges.append(_edge_raw(ids.service_id(svc), cid, "uses-cache", ev,
                                   "EXTRACTED", 0.95,
                                   "@%s (%d annotated sites)" % (ann, len(anns))))
    for svc, names in jndi.items():
        ensure_service(svc)
        for name, ev in names.items():
            did = "cart . datastore jndi:%s" % name
            db_nodes.append({"id": did, "kind": "datastore", "name": name,
                             "extra": {"jndi": True}})
            edges.append(_edge_raw(ids.service_id(svc), did, "uses-datastore",
                                   ev, "EXTRACTED", 0.9,
                                   "JNDI %s (connection defined outside the repo)" % name))
            gaps.append(("jndi-datasource",
                         "%s resolves a datasource through JNDI (%s)" % (svc, name),
                         "the actual host and schema live in the container or "
                         "app-server config, not in this repo -- check the "
                         "deployment descriptor to learn what it points at"))

    nodes = list(services.values()) + list(topic_nodes.values()) \
        + db_nodes + route_nodes
    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    seen = set()
    for cat, detail, hint in gaps:
        if detail in seen:
            continue
        seen.add(detail)
        store.add_gap(cat, detail, hint, SOURCE)
    store.commit()

    svc_edges = [e for e in edges if e["kind"] in ("http", "event")]
    return {"services": len(services), "topics": len(topic_nodes),
            "datastores": len(db_nodes), "routes": len(route_nodes),
            "service_edges": len(svc_edges), "gaps": len(seen)}


def _module_owner(cfg, repo_root, path, fallback):
    """A multi-module repo means the repo name is too coarse to be the owner."""
    rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
    parts = rel.split("/")
    for anchor in ("server", "modules", "services", "apps"):
        if anchor in parts:
            i = parts.index(anchor)
            if i + 1 < len(parts) - 1:
                name = parts[i + 1]
                return cfg.resolve_service(name) or name
    return fallback


def _topic(raw):
    t = (raw or "").strip().strip("/")
    if not t or len(t) < 3 or len(t) > 120:
        return None
    if "${" in t or "{{" in t or t.startswith("$") or " " in t:
        return None
    if not re.match(r"^[\w][\w.\-/]*$", t):
        return None
    if t.lower() in ("true", "false", "null", "none", "default"):
        return None
    # Needs at least one separator, else it is probably a variable name.
    if not re.search(r"[.\-/_]", t):
        return None
    return t


def _edge(src_svc, dst_svc, kind, evidence, provenance, confidence, via):
    return _edge_raw(ids.service_id(src_svc), ids.service_id(dst_svc), kind,
                     evidence, provenance, confidence, via)


def _edge_raw(src, dst, kind, evidence, provenance, confidence, via):
    return {"src": src, "dst": dst, "kind": kind, "evidence": evidence,
            "provenance": provenance, "confidence": confidence,
            "extra": {"via": via} if via else None}
