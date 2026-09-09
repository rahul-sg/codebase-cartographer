"""
Frontend call site -> backend route.

`frontend.py` already records that a UI repo talks to a service, by reading the
dev-server proxy config. That is true but coarse: it answers "does the storefront
UI repo depend on order-svc" and stops there. What a reader actually needs when
chasing a bug is the opposite direction and one level finer -- *which file*
calls *which endpoint*, and therefore which controller answers it.

Without that edge the estate is two disconnected islands: a few thousand
declared backend routes on one side, several hundred URL literals on the
other, and nothing joining them. That is the shape that hides an integration
bug: the screen renders empty, the API returns null, and no path in the graph
leads from one to the other.

The match is mechanical once the pieces are lined up:

    frontend   ui/.../order-detail.component.ts
               `/order/v1/api/orders/${id}`
    strip      proxy prefix `/order/`            -> service `order`
    canonical  /v1/api/orders/{}
    declared   GET /v1/api/orders/{id:\\d+} -> /v1/api/orders/{}   svc order
    => EXTRACTED edge, file -> route, with file:line

Both sides are canonicalised the same way: every path parameter, however it is
written, collapses to `{}`. Spring's inline regex constraints (`{id:\\d+}`),
Angular template interpolation (`${id}`), and a bare numeric segment all become
the same token, so `/v1/api/orders/{}` compares equal from either side.

Unmatched API-looking URLs are recorded as gaps rather than dropped. A call
with no matching route is worth seeing: it is either dead frontend code, or an
endpoint served by a repo that is not on this machine -- and telling those two
apart is a question for a human, not a guess for an extractor.
"""
from __future__ import annotations

import os
import re

from .. import ids
from ..config import prune
from .frontend import _files, _read, _prefix_to_service

SOURCE = "httpcalls"

# Angular HttpClient / axios style: `.get<T>('/url')`, `.post('/url', body)`.
# The generic parameter is optional and skipped. Only the URL argument is read.
CALL = re.compile(
    r"""\.(?P<method>get|post|put|delete|patch)\s*"""
    r"""(?:<[^<>()]*>)?\s*\(\s*"""
    r"""(?P<quote>["'`])(?P<url>[^"'`\n]*)(?P=quote)""",
    re.I)

# `fetch('/url', {method: 'POST'})` -- method is not read; ANY is assumed.
FETCH = re.compile(
    r"""\bfetch\s*\(\s*(?P<quote>["'`])(?P<url>[^"'`\n]*)(?P=quote)""")

# Only paths that look like an API call. A bare `/assets/logo.png` or a router
# link is not one, and matching those would bury the real findings.
API_ISH = re.compile(r"^/[a-z0-9][a-z0-9\-_]*/", re.I)

STATIC_EXT = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".ico", ".css",
              ".woff", ".woff2", ".ttf", ".eot", ".map", ".webp", ".mp4",
              ".wav", ".html", ".htm")

FRONTEND_EXTS = (".ts", ".js", ".tsx", ".jsx")

# The frontend keeps its base paths in constants for exactly the same reason
# the backend does, so the same resolution is needed on this side:
#
#   export const ORDERS_API = '/order/v1/api/orders';
#   this.http.get(`${ORDERS_API}/${id}`)
#
# Without substituting the constant, `${ORDERS_API}` canonicalises to `{}`
# like any other interpolation and the URL becomes `/{}/{}` -- unresolvable,
# and silently so.
TS_CONST = re.compile(
    r"""(?:export\s+)?(?:const|let|var|(?:private\s+|public\s+|protected\s+)?"""
    r"""(?:readonly\s+)?)\s*(?P<name>[A-Za-z_]\w*)\s*(?::\s*string\s*)?=\s*"""
    r"""["'`](?P<val>/[^"'`\n${]*)["'`]\s*;?""")

# `${NAME}` where NAME is a known constant -- substituted before canonicalising.
INTERP_NAMED = re.compile(r"\$\{\s*([A-Za-z_][\w.]*)\s*\}")

# Angular `${x}`, ES concat leftovers, Spring `{id:\d+}` / `{id}`, and bare
# numeric ids all mean "a parameter goes here".
INTERP = re.compile(r"\$\{[^}]*\}")
BRACED = re.compile(r"\{[^}]*\}")
NUMERIC_SEG = re.compile(r"(?<=/)\d+(?=/|$)")


def ts_constants(text):
    """`NAME -> "/path"` for path-like string constants declared in one file."""
    out = {}
    for m in TS_CONST.finditer(text or ""):
        val = m.group("val")
        if val.startswith("/") and len(val) > 1:
            out[m.group("name")] = val
    return out


def _substitute(url, constants):
    """
    Replace `${CONST}` with its value before the path is canonicalised.

    Only named interpolations that resolve to a known path constant are
    substituted; `${id}` and friends are left alone for `_canon` to collapse
    into a parameter placeholder.
    """
    if not constants or "${" not in url:
        return url

    def sub(m):
        ref = m.group(1).split(".")[-1]
        val = constants.get(ref)
        return val if val else m.group(0)

    return INTERP_NAMED.sub(sub, url)


def _canon(path):
    """Collapse a URL path to its shape: every parameter becomes `{}`."""
    if not path:
        return ""
    p = path.split("?")[0].split("#")[0]
    p = INTERP.sub("{}", p)
    p = BRACED.sub("{}", p)          # covers {id}, {id:\d+}, {} already present
    p = NUMERIC_SEG.sub("{}", p)
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def _route_index(store):
    """
    (service, METHOD, canonical path) -> route node id.

    Also indexed under method ANY, because a `fetch()` call does not say which
    verb it uses and matching the path alone is still a real finding.
    """
    exact, anym = {}, {}
    rows = store.conn.execute(
        "SELECT id, name, service FROM nodes WHERE kind='route'")
    for r in rows:
        name = r["name"] or ""
        svc = r["service"]
        if not svc or " " not in name:
            continue
        method, _, raw = name.partition(" ")
        cp = _canon(raw)
        exact.setdefault((svc, method.upper(), cp), r["id"])
        anym.setdefault((svc, cp), r["id"])
    return exact, anym


def _lang_of(path):
    return "typescript" if path.endswith((".ts", ".tsx")) else "javascript"


def _looks_static(url):
    low = url.lower().split("?")[0]
    return low.endswith(STATIC_EXT)


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)

    exact, anym = _route_index(store)
    if not exact:
        # No routes in the graph yet: nothing to match against. Say so rather
        # than reporting zero matches as though the frontends call nothing.
        store.add_gap(
            "frontend-route-linking",
            "no backend route nodes in the graph, so frontend call sites could "
            "not be linked to endpoints",
            "run a full scan so the route extractor populates first, then "
            "re-run; if the backend repo is not on this machine, these edges "
            "cannot be built at all.", SOURCE)
        store.commit()
        return {"call_sites": 0, "linked": 0, "unmatched": 0}

    follow = cfg.defaults.get("follow_symlinks", False)
    edges = []
    n_sites = n_linked = 0
    unmatched = {}          # (svc-or-prefix, canonical path) -> evidence
    per_repo = {}

    # Pre-pass for path constants. They are exported from one module and used
    # in another (`ORDERS_API` lives in orders.api.service.ts), so a
    # per-file table would resolve almost nothing. Re-reading the frontend
    # files costs well under a second at this scale.
    consts = {}
    file_cache = {}
    for name, root, _svc in repos:
        if not os.path.isdir(root):
            continue
        for path in _files(root, follow=follow, exts=FRONTEND_EXTS):
            txt = _read(path)
            if not txt or "/" not in txt:
                continue
            file_cache[path] = (name, root, txt)
            for k, v in ts_constants(txt).items():
                # Same name with two different values is ambiguous; drop it
                # rather than pick one and mislabel every call that uses it.
                if k in consts and consts[k] != v:
                    consts[k] = None
                else:
                    consts.setdefault(k, v)
    consts = {k: v for k, v in consts.items() if v}

    for path, (name, root, txt) in sorted(file_cache.items()):
        rel = os.path.relpath(path, root)
        # Skip test/spec files: a URL in a spec is a fixture, not a
        # dependency the running app actually has.
        low = rel.lower()
        if ".spec." in low or ".test." in low or "/__tests__/" in low:
            continue

        hits = [(m.group("method").upper(), m.group("url"), m.start())
                for m in CALL.finditer(txt)]
        hits += [("ANY", m.group("url"), m.start())
                 for m in FETCH.finditer(txt)]
        if not hits:
            continue

        fid = ids.file_id(name, _lang_of(rel), rel)
        for method, raw_url, off in hits:
            # Substitute before the API test: a URL written as
            # `${ORDERS_API}/${id}` does not begin with "/" until its
            # base constant has been resolved, so testing first would discard
            # exactly the calls that matter most.
            url = _substitute(raw_url, consts)
            if not url or not API_ISH.match(url) or _looks_static(url):
                continue
            n_sites += 1
            line = txt.count("\n", 0, off) + 1
            ev = "%s/%s:%d" % (name, rel, line)

            svc, token = _prefix_to_service(cfg, url)
            # Strip the prefix segment the proxy routes on, leaving the
            # path the backend actually declares.
            seg = [s for s in url.split("/") if s]
            rest = "/" + "/".join(seg[1:]) if len(seg) > 1 else "/"
            cp = _canon(rest)

            rid = None
            if svc:
                rid = (exact.get((svc, method, cp))
                       or anym.get((svc, cp)))
                if rid is None and cp != "/":
                    # A concatenated URL (`'/v1/api/orders/' + id`)
                    # loses its final parameter, so try one back.
                    rid = (exact.get((svc, method, cp + "/{}"))
                           or anym.get((svc, cp + "/{}")))

            if rid:
                n_linked += 1
                per_repo[name] = per_repo.get(name, 0) + 1
                edges.append({
                    "src": fid, "dst": rid, "kind": "calls-route",
                    "evidence": ev, "provenance": "EXTRACTED",
                    "confidence": 0.95,
                    "extra": {"url": url, "source_url": raw_url,
                              "method": method,
                              "matched": cp, "service": svc},
                })
            else:
                key = (svc or (token and "/%s/" % token) or "?", cp)
                unmatched.setdefault(key, ev)

    store.add_edges(edges, SOURCE)

    # Cap the gap list: 400 near-identical entries is noise, and the count in
    # the summary already carries the magnitude.
    for (svc, cp), ev in sorted(unmatched.items())[:40]:
        store.add_gap(
            "unlinked-api-call",
            "%s calls %s but no declared route matches it (%s)" % (ev, cp, svc),
            "either dead frontend code, or an endpoint served by a repo that "
            "is not on this machine. Confirm which before treating the caller "
            "as unused.", SOURCE)
    if len(unmatched) > 40:
        store.add_gap(
            "unlinked-api-call",
            "%d further frontend API calls matched no declared route"
            % (len(unmatched) - 40),
            "run `cartographer contracts <service>` to compare a service's "
            "declared surface against what the frontends actually call.",
            SOURCE)

    store.commit()
    return {"call_sites": n_sites, "linked": n_linked,
            "unmatched": len(unmatched),
            "by_repo": per_repo}
