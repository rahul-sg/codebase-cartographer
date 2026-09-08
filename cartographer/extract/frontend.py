"""
Frontend -> backend edges.

In this estate the frontends do not name backend services in code at all. They
call path prefixes (`/order/`, `/catalog/`) and a dev-server proxy maps each
prefix to a host. So `proxy.config.json` IS the frontend's dependency
declaration -- more authoritative than anything grep could infer from the
TypeScript.

Also handles:
  * React Native, which has no proxy: hardcoded base URLs per environment
  * legacy ColdFusion page references, a live surface that lives in no repo here
  * shared-component-library version drift between frontends
"""
from __future__ import annotations

import json
import os
import re

from .. import ids
from ..config import SKIP_DIRS

SOURCE = "frontend"

PROXY_NAMES = ("proxy.config.json", "proxy.conf.json", "proxy.config.js",
               "proxy.conf.js", "proxy.config.mjs")

# "/order/": { "target": "https://ongsqe.itradenetwork.net" }
PROXY_ENTRY = re.compile(
    r'"(?P<prefix>/[^"]*)"\s*:\s*\{(?P<body>[^{}]*)\}', re.S)
TARGET = re.compile(r'"target"\s*:\s*"([^"]+)"')
PATH_REWRITE = re.compile(r'"pathRewrite"\s*:\s*\{([^}]*)\}')

BASE_URL_CONST = re.compile(
    r'\b(?:const|let|var|export\s+const)\s+([A-Z][A-Z0-9_]{2,40})\s*=\s*'
    r'["\'](https?://[^"\']+)["\']')

CFM_REF = re.compile(r'["\'`]([\w/\-.]*\.cfml?)(?:\?[^"\'`]*)?["\'`]')

API_CALL = re.compile(
    r'''\.(?:get|post|put|delete|patch|request)\s*[<(]\s*[^)]*?["'`](/[a-z0-9\-_]+/)''',
    re.I)

SHARED_LIB_HINT = ("@itn/", "@itradenetwork/")


def _files(repo_root, follow=False, exts=None, names=None, cap_bytes=1_500_000):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if names and fn in names:
                yield os.path.join(dirpath, fn)
            elif exts and fn.endswith(exts):
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > cap_bytes:
                        continue
                except OSError:
                    continue
                yield p


def _read(p, limit=2_000_000):
    try:
        if os.path.getsize(p) > limit:
            return None
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _prefix_to_service(cfg, prefix):
    """`/order/api/v1/` -> the `order` service, if we know it."""
    seg = [s for s in (prefix or "").split("/") if s]
    if not seg:
        return None, None
    for i in range(min(2, len(seg)), 0, -1):
        cand = "-".join(seg[:i]) if i > 1 else seg[0]
        got = cfg.resolve_service(cand)
        if got:
            return got, cand
    return None, seg[0]


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    follow = cfg.defaults.get("follow_symlinks", False)
    nodes, edges = [], []
    n_proxy = n_prefix = n_cfm = n_url = 0
    lib_versions = {}
    unmapped_prefixes = {}

    for repo_name, repo_root, svc_name in repos:
        fe = svc_name or repo_name

        # ---- 1. package.json: is this a frontend, and what does it pin? ----
        is_frontend = False
        for pkg in _files(repo_root, follow, names=("package.json",)):
            if "node_modules" in pkg:
                continue
            text = _read(pkg)
            if not text:
                continue
            try:
                d = json.loads(text)
            except ValueError:
                continue
            deps = {}
            for key in ("dependencies", "devDependencies"):
                deps.update(d.get(key) or {})
            if not deps:
                continue
            rel = os.path.relpath(pkg, repo_root).replace(os.sep, "/")
            framework = None
            for probe, label in (("@angular/core", "angular"),
                                 ("react-native", "react-native"),
                                 ("react", "react"), ("vue", "vue"),
                                 ("svelte", "svelte")):
                if probe in deps:
                    framework = "%s %s" % (label, deps[probe])
                    break
            if framework:
                is_frontend = True
                nodes.append({"id": ids.service_id(fe), "kind": "service",
                              "name": fe, "repo": repo_name, "service": fe,
                              "extra": {"frontend": True, "framework": framework,
                                        "package": d.get("name"),
                                        "version": d.get("version")}})
            for name, ver in deps.items():
                if name.startswith(SHARED_LIB_HINT):
                    lib_versions.setdefault(name, {})[fe] = (ver, "%s/%s" % (repo_name, rel))

        # ---- 2. proxy config: the real dependency declaration -------------
        for path in _files(repo_root, follow, names=PROXY_NAMES):
            text = _read(path)
            if not text:
                continue
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            n_proxy += 1
            nodes.append({"id": ids.service_id(fe), "kind": "service",
                          "name": fe, "repo": repo_name, "service": fe,
                          "extra": {"frontend": True}})
            line_of = _line_index(text)
            for m in PROXY_ENTRY.finditer(text):
                prefix = m.group("prefix")
                body = m.group("body")
                tm = TARGET.search(body)
                if not tm:
                    continue
                target = tm.group(1)
                host = re.sub(r"^https?://", "", target).split("/")[0].split(":")[0]
                ev = "%s/%s:%d" % (repo_name, rel, line_of(m.start()))
                n_prefix += 1

                svc, token = _prefix_to_service(cfg, prefix)
                # The host itself sometimes names the service (icrsqe -> icr).
                if not svc:
                    svc = cfg.resolve_service(host.split(".")[0])
                if svc:
                    nodes.append({"id": ids.service_id(svc), "kind": "service",
                                  "name": svc, "service": svc})
                    edges.append({
                        "src": ids.service_id(fe), "dst": ids.service_id(svc),
                        "kind": "http", "evidence": ev,
                        "provenance": "EXTRACTED", "confidence": 0.95,
                        "extra": {"via": "proxy %s -> %s" % (prefix, host),
                                  "prefix": prefix, "host": host,
                                  "rewrite": bool(PATH_REWRITE.search(body))}})
                else:
                    unmapped_prefixes.setdefault((prefix, host), ev)

                # Record the host as a deployment surface either way.
                hid = "cart . host %s" % host
                nodes.append({"id": hid, "kind": "host", "name": host,
                              "extra": {"prefix": prefix}})
                edges.append({"src": ids.service_id(fe), "dst": hid,
                              "kind": "routes-to", "evidence": ev,
                              "provenance": "EXTRACTED", "confidence": 1.0,
                              "extra": {"via": prefix}})

        # ---- 3. no proxy (React Native): hardcoded base URLs --------------
        if is_frontend and not any(True for _ in _files(repo_root, follow,
                                                        names=PROXY_NAMES)):
            for path in _files(repo_root, follow, exts=(".js", ".ts", ".jsx", ".tsx")):
                base = os.path.basename(path).lower()
                if not any(k in base for k in ("url", "config", "env", "constant",
                                               "endpoint", "api")):
                    continue
                text = _read(path)
                if not text:
                    continue
                rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
                for i, line in enumerate(text.split("\n"), start=1):
                    for m in BASE_URL_CONST.finditer(line):
                        const, url = m.group(1), m.group(2)
                        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
                        n_url += 1
                        ev = "%s/%s:%d" % (repo_name, rel, i)
                        hid = "cart . host %s" % host
                        nodes.append({"id": hid, "kind": "host", "name": host,
                                      "extra": {"constant": const}})
                        edges.append({"src": ids.service_id(fe), "dst": hid,
                                      "kind": "routes-to", "evidence": ev,
                                      "provenance": "EXTRACTED", "confidence": 0.9,
                                      "extra": {"via": "%s = %s" % (const, url)}})
                        svc = cfg.resolve_service(host.split(".")[0])
                        if svc:
                            edges.append({
                                "src": ids.service_id(fe), "dst": ids.service_id(svc),
                                "kind": "http", "evidence": ev,
                                "provenance": "EXTRACTED", "confidence": 0.7,
                                "extra": {"via": "hardcoded %s" % const}})

        # ---- 4. legacy ColdFusion surface ---------------------------------
        for path in _files(repo_root, follow, exts=(".ts", ".js", ".html", ".tsx")):
            text = _read(path)
            if not text or ".cfm" not in text:
                continue
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            for i, line in enumerate(text.split("\n"), start=1):
                for m in CFM_REF.finditer(line):
                    page = m.group(1)
                    if not page.endswith((".cfm", ".cfml")):
                        continue
                    n_cfm += 1
                    pid = "cart . legacypage %s" % page
                    nodes.append({"id": pid, "kind": "legacy-page", "name": page,
                                  "extra": {"technology": "ColdFusion"}})
                    edges.append({"src": ids.service_id(fe), "dst": pid,
                                  "kind": "links-to",
                                  "evidence": "%s/%s:%d" % (repo_name, rel, i),
                                  "provenance": "EXTRACTED", "confidence": 1.0,
                                  "extra": {"via": "legacy ColdFusion page"}})

    # ---- findings ------------------------------------------------------
    for (prefix, host), ev in sorted(unmapped_prefixes.items()):
        store.add_gap("proxy-prefix-unmapped",
                      "frontend proxies %s -> %s but no configured service "
                      "matches" % (prefix, host),
                      "add the service (with an alias) to your config; you may "
                      "not have its repo. Evidence: %s" % ev, SOURCE)

    for lib, per_repo in sorted(lib_versions.items()):
        versions = {v for v, _ in per_repo.values()}
        if len(versions) > 1:
            detail = ", ".join("%s pins %s" % (r, v)
                               for r, (v, _) in sorted(per_repo.items()))
            store.add_gap("shared-library-version-drift",
                          "%s: %s" % (lib, detail),
                          "a shared component library on different majors "
                          "across frontends means a fix in one does not reach "
                          "the other -- confirm this is deliberate", SOURCE)

    if n_cfm:
        store.add_gap("legacy-surface",
                      "%d ColdFusion page references found in frontend source"
                      % n_cfm,
                      "those pages are live but their source is in none of the "
                      "repos scanned -- a complete screen map needs that repo",
                      SOURCE)

    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"proxy_configs": n_proxy, "prefixes": n_prefix,
            "hardcoded_urls": n_url, "coldfusion_refs": n_cfm,
            "unmapped_prefixes": len(unmapped_prefixes),
            "shared_libs": len(lib_versions)}


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
