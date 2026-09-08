"""
Declared dependencies from build files.

Build manifests are authoritative about what a service depends on -- more so
than grep -- and they also reveal in-estate dependencies (order-svc depending
on a shared oms-common library) that no network call would show.

Read statically. We never invoke a build tool: that would be slow, would need
credentials, and could execute arbitrary code from the repository.
"""
from __future__ import annotations

import json
import os
import re

from .. import ids
from ..config import SKIP_DIRS, prune

SOURCE = "builddeps"

MANIFESTS = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
             "package.json", "requirements.txt", "pyproject.toml", "Pipfile",
             "go.mod", "Cargo.toml", "composer.json", "Gemfile", "*.csproj")

MVN_DEP = re.compile(
    r"<dependency>(.*?)</dependency>", re.S | re.I)
MVN_FIELD = re.compile(r"<(groupId|artifactId|version)>\s*([^<]+?)\s*</\1>", re.I)
GRADLE_DEP = re.compile(
    r"""^\s*(?:api|implementation|compile|testImplementation|runtimeOnly|"""
    r"""compileOnly|annotationProcessor|kapt)\s*[\s(]\s*["']([^"']+)["']""", re.M)
GO_REQ = re.compile(r"^\s*([\w.\-]+/[\w.\-/]+)\s+v[\w.\-+]+", re.M)
PY_REQ = re.compile(r"^\s*([A-Za-z][\w.\-]*)\s*(?:[=<>~!\[]|$)", re.M)
TOML_DEP = re.compile(r'^\s*["\']?([A-Za-z][\w.\-]*)["\']?\s*=', re.M)


def _read(p, limit=2_000_000):
    try:
        if os.path.getsize(p) > limit:
            return None
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _manifests(repo_root, follow=False):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        prune(dirpath, dirnames)
        for fn in filenames:
            if fn in MANIFESTS or fn.endswith(".csproj"):
                yield os.path.join(dirpath, fn)


def parse(path, text):
    """Return a list of (coordinate, ecosystem)."""
    name = os.path.basename(path)
    out = []
    if name == "pom.xml":
        for block in MVN_DEP.findall(text):
            f = dict((k.lower(), v) for k, v in MVN_FIELD.findall(block))
            g, a = f.get("groupid"), f.get("artifactid")
            if a:
                out.append(("%s:%s" % (g, a) if g else a, "maven"))
    elif name.startswith("build.gradle") or name.startswith("settings.gradle"):
        for coord in GRADLE_DEP.findall(text):
            parts = coord.split(":")
            out.append((":".join(parts[:2]) if len(parts) >= 2 else coord, "maven"))
    elif name == "package.json":
        try:
            d = json.loads(text)
        except ValueError:
            return out
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            for dep in (d.get(key) or {}):
                out.append((dep, "npm"))
    elif name == "requirements.txt":
        for line in text.split("\n"):
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            m = PY_REQ.match(line)
            if m:
                out.append((m.group(1).lower(), "pypi"))
    elif name in ("pyproject.toml", "Cargo.toml", "Pipfile"):
        eco = "crates" if name == "Cargo.toml" else "pypi"
        section = None
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("["):
                section = s.strip("[]")
                continue
            if section and ("dependencies" in section.lower()
                            or "packages" in section.lower()):
                m = TOML_DEP.match(line)
                if m:
                    out.append((m.group(1).lower(), eco))
        for m in re.finditer(r'^\s*dependencies\s*=\s*\[(.*?)\]', text, re.S | re.M):
            for item in re.findall(r'["\']([^"\']+)["\']', m.group(1)):
                nm = PY_REQ.match(item)
                if nm:
                    out.append((nm.group(1).lower(), eco))
    elif name == "go.mod":
        for mod in GO_REQ.findall(text):
            out.append((mod, "go"))
    elif name.endswith(".csproj"):
        for m in re.finditer(r'<PackageReference\s+Include="([^"]+)"', text, re.I):
            out.append((m.group(1), "nuget"))
    elif name == "Gemfile":
        for m in re.finditer(r"""^\s*gem\s+["']([^"']+)["']""", text, re.M):
            out.append((m.group(1), "rubygems"))
    elif name == "composer.json":
        try:
            d = json.loads(text)
        except ValueError:
            return out
        for key in ("require", "require-dev"):
            for dep in (d.get(key) or {}):
                out.append((dep, "packagist"))
    # de-duplicate, preserve order
    seen, uniq = set(), []
    for c, e in out:
        if c and (c, e) not in seen:
            seen.add((c, e))
            uniq.append((c, e))
    return uniq


def _known_modules(store):
    """
    Module directories the Maven/build pass already identified.

    Without this, `ong-ui-repo/ui/package.json` would create a service called
    "ui" and the parent `server/pom.xml` one called "server" -- directory
    names that are not modules at all.
    """
    import json as _json
    out = {}
    for r in store.conn.execute(
            "SELECT name, extra FROM nodes WHERE extra LIKE '%module_dir%'"):
        try:
            ex = _json.loads(r["extra"])
        except (ValueError, TypeError):
            continue
        d = ex.get("module_dir")
        if d:
            out[d] = r["name"]
        if ex.get("artifactId"):
            out.setdefault(ex["artifactId"], r["name"])
    return out


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    known = _known_modules(store)
    nodes, edges = [], []
    internal_hits = 0
    n_manifests = 0
    follow = cfg.defaults.get("follow_symlinks", False)

    for repo_name, repo_root, svc in repos:
        for path in _manifests(repo_root, follow):
            text = _read(path)
            if text is None:
                continue
            n_manifests += 1
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            # The directory holding the manifest IS the module. Using the repo
            # name instead invents a service for a multi-module container and
            # attributes every dependency in the repo to it.
            mdir = os.path.dirname(path)
            me = svc or repo_name
            if os.path.realpath(mdir) != os.path.realpath(repo_root):
                base = os.path.basename(mdir)
                if base in known:
                    me = known[base]
                else:
                    resolved = cfg.resolve_service(base)
                    me = resolved if resolved else (svc or repo_name)
            ev = "%s/%s" % (repo_name, rel)
            for coord, eco in parse(path, text):
                # An in-estate dependency is a real service edge; an external
                # one is just a library.
                tail = coord.split(":")[-1].split("/")[-1]
                target = cfg.resolve_service(tail) or cfg.resolve_service(coord)
                if target and target != me:
                    internal_hits += 1
                    nodes.append({"id": ids.service_id(target), "kind": "service",
                                  "name": target, "service": target})
                    edges.append({"src": ids.service_id(me),
                                  "dst": ids.service_id(target),
                                  "kind": "depends-on", "evidence": ev,
                                  "provenance": "EXTRACTED", "confidence": 1.0,
                                  "extra": {"via": "build dependency %s" % coord}})
                elif tail in known:
                    # An in-estate module referenced as a Maven coordinate is
                    # that module, not a third-party library.
                    edges.append({"src": ids.service_id(me),
                                  "dst": "cart . library maven:%s" % tail,
                                  "kind": "depends-on", "evidence": ev,
                                  "provenance": "EXTRACTED", "confidence": 1.0,
                                  "extra": {"via": "build dependency %s" % coord}})
                else:
                    lid = "cart . library %s:%s" % (eco, coord)
                    nodes.append({"id": lid, "kind": "library", "name": coord,
                                  "extra": {"ecosystem": eco}})
                    edges.append({"src": ids.service_id(me), "dst": lid,
                                  "kind": "uses-library", "evidence": ev,
                                  "provenance": "EXTRACTED", "confidence": 1.0})
            nodes.append({"id": ids.service_id(me), "kind": "service",
                          "name": me, "service": me})

    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"manifests": n_manifests,
            "libraries": len({n["id"] for n in nodes if n["kind"] == "library"}),
            "internal_deps": internal_hits}
