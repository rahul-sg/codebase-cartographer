"""
Maven multi-module discovery.

The lesson this encodes, learned the hard way on the real OMS: **never trust
the parent POM's <modules> list as the service inventory.** Real, deployed
services can be deliberately excluded from the reactor and built separately.
On ONG that is `logistics` and `interoperability` -- both live, both missed by
a naive parent-pom scan.

So: find every pom.xml, and mark whether each is in the reactor. A module with
its own Dockerfile or Helm chart is a deployable service regardless of what the
parent says.

Also builds the inter-module dependency graph from <dependency> entries whose
groupId matches the reactor, which is how shared libraries (framework, cache,
kafkautil) show up as real edges rather than invisible glue.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from .. import ids
from ..config import SKIP_DIRS, prune

SOURCE = "maven"

# Namespace-insensitive tag matching: POMs are inconsistently namespaced and
# ElementTree makes qualified lookups painful.
def _tag(el):
    t = el.tag
    return t.split("}", 1)[1] if "}" in t else t


def _text(parent, name):
    for child in parent:
        if _tag(child) == name:
            return (child.text or "").strip()
    return None


def _find(parent, name):
    for child in parent:
        if _tag(child) == name:
            return child
    return None


def find_poms(root, follow=False, max_depth=6):
    out = []
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
        if dirpath.rstrip(os.sep).count(os.sep) - base > max_depth:
            dirnames[:] = []
            continue
        prune(dirpath, dirnames)
        if "pom.xml" in filenames:
            out.append(os.path.join(dirpath, "pom.xml"))
    return sorted(out)


def parse_pom(path):
    """Return a dict describing one POM, or None if it will not parse."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    parent = _find(root, "parent")
    group = _text(root, "groupId") or (_text(parent, "groupId") if parent is not None else None)
    artifact = _text(root, "artifactId")
    if not artifact:
        return None

    modules = []
    mods = _find(root, "modules")
    if mods is not None:
        for m in mods:
            if _tag(m) == "module" and (m.text or "").strip():
                modules.append(m.text.strip())

    deps = []
    deps_el = _find(root, "dependencies")
    if deps_el is not None:
        for d in deps_el:
            if _tag(d) != "dependency":
                continue
            g, a = _text(d, "groupId"), _text(d, "artifactId")
            if a:
                deps.append((g, a))

    props = {}
    props_el = _find(root, "properties")
    if props_el is not None:
        for p in props_el:
            props[_tag(p)] = (p.text or "").strip()

    return {
        "path": path,
        "dir": os.path.dirname(path),
        "groupId": group,
        "artifactId": artifact,
        "packaging": _text(root, "packaging") or "jar",
        "parent_artifact": _text(parent, "artifactId") if parent is not None else None,
        "modules": modules,
        "dependencies": deps,
        "properties": props,
    }


DEPLOY_MARKERS = ("Dockerfile", "build.sh", "deployHelm", "Chart.yaml",
                  "helm", "k8s", "manifests", "Procfile")


def _deployable(d):
    """Signals that a module is actually shipped, whatever the reactor says."""
    hits = []
    try:
        entries = set(os.listdir(d))
    except OSError:
        return hits
    for m in DEPLOY_MARKERS:
        for e in entries:
            if e == m or e.startswith(m + "_") or e.startswith("Dockerfile"):
                hits.append(e)
                break
    return sorted(set(hits))


def _java_signals(d, cap=400):
    """
    Scan the module's Java for the two markers that separate a deployable
    service from a shared library: a Spring Boot entry point, and REST
    controllers. Capped so a 2,000-file module does not dominate the scan.
    """
    src = os.path.join(d, "src", "main", "java")
    found = {"app": False, "controller": False}
    if not os.path.isdir(src):
        return found
    seen = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [x for x in dirnames if not x.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".java"):
                continue
            seen += 1
            if seen > cap and found["controller"]:
                return found
            try:
                with open(os.path.join(dirpath, fn), "r", encoding="utf-8",
                          errors="replace") as fh:
                    head = fh.read(6000)
            except OSError:
                continue
            if "@SpringBootApplication" in head:
                found["app"] = True
            if "@RestController" in head or "@Controller" in head:
                found["controller"] = True
            if found["app"] and found["controller"]:
                return found
    return found


def _has_app_config(d):
    """
    application.properties / application.yml under src/main/resources.

    On ONG this is the cleanest library-vs-service discriminator: the shared
    libraries (gcutil, cache, kafkautil, framework, auth, elasticsearch, misc)
    have no Spring Boot config of their own.
    """
    res = os.path.join(d, "src", "main", "resources")
    if not os.path.isdir(res):
        return False
    try:
        for fn in os.listdir(res):
            low = fn.lower()
            if low.startswith("application") and low.endswith(
                    (".properties", ".yml", ".yaml")):
                return True
    except OSError:
        pass
    return False


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    nodes, edges = [], []
    reactors = {}          # parent artifactId -> set(declared module dirs)
    poms = []

    for repo_name, repo_root, svc in repos:
        for path in find_poms(repo_root, cfg.defaults.get("follow_symlinks", False)):
            p = parse_pom(path)
            if p:
                p["repo"] = repo_name
                poms.append(p)

    if not poms:
        return {"poms": 0}

    by_artifact = {p["artifactId"]: p for p in poms}
    group_ids = {p["groupId"] for p in poms if p["groupId"]}

    # Which directories did a parent POM actually declare?
    declared_dirs = set()
    for p in poms:
        for m in p["modules"]:
            declared_dirs.add(os.path.normpath(os.path.join(p["dir"], m)))
            reactors.setdefault(p["artifactId"], set()).add(m)

    n_services = n_libs = n_excluded = 0
    for p in poms:
        if p["packaging"] == "pom" and p["modules"]:
            continue                                   # the aggregator itself

        d = p["dir"]
        name = p["artifactId"]
        in_reactor = os.path.normpath(d) in declared_dirs
        deploy = _deployable(d)
        sig = _java_signals(d)
        app = sig["app"]
        has_cfg = _has_app_config(d)
        # A WAR is a deployable artifact by definition -- order-enterprise and
        # interoperability ship as WARs onto WildFly rather than as fat jars.
        is_war = p["packaging"] == "war"
        is_service = bool(deploy) or app or has_cfg or is_war or sig["controller"]

        canon = cfg.resolve_service(name) or cfg.resolve_service(os.path.basename(d)) or name
        kind = "service" if is_service else "library"
        if is_service:
            n_services += 1
        else:
            n_libs += 1

        node = {"id": ids.service_id(canon) if is_service
                else "cart . library maven:%s" % name,
                "kind": kind, "name": canon if is_service else name,
                "repo": p["repo"], "lang": "java",
                "service": canon if is_service else None,
                "extra": {"artifactId": name, "groupId": p["groupId"],
                          "module_dir": os.path.basename(d),
                          "in_reactor": in_reactor,
                          "packaging": p["packaging"],
                          "deploy_markers": deploy,
                          "spring_boot_app": app,
                          "has_app_config": has_cfg,
                          "has_controllers": sig["controller"],
                          "war": is_war}}
        nodes.append(node)

        # The important finding: shipped but outside the reactor.
        if is_service and not in_reactor and p["parent_artifact"] is not None:
            n_excluded += 1
            store.add_gap(
                "module-outside-reactor",
                "%s declares <parent>%s</parent> but is NOT in that parent's "
                "<modules> list, yet ships (%s)"
                % (name, p["parent_artifact"], ", ".join(deploy) or "no markers"),
                "it is built and deployed independently -- do not assume the "
                "top-level build covers it", SOURCE)
        elif is_service and not in_reactor and p["parent_artifact"] is None:
            n_excluded += 1
            store.add_gap(
                "module-standalone",
                "%s is a fully standalone Maven project (no <parent>) that "
                "ships (%s)" % (name, ", ".join(deploy) or "no markers"),
                "independently versioned and deployed; check its own build.sh "
                "and Dockerfile rather than assuming the reactor flow",
                SOURCE)

    # ---- inter-module dependency edges --------------------------------
    for p in poms:
        if p["packaging"] == "pom" and p["modules"]:
            continue
        src_name = p["artifactId"]
        src_is_service = any(
            n for n in nodes
            if n.get("extra", {}).get("artifactId") == src_name and n["kind"] == "service")
        src_canon = cfg.resolve_service(src_name) or src_name
        src_id = (ids.service_id(src_canon) if src_is_service
                  else "cart . library maven:%s" % src_name)
        ev = "%s/%s" % (p["repo"], os.path.relpath(p["path"], p["dir"]) if False
                        else os.path.basename(p["dir"]) + "/pom.xml")
        for g, a in p["dependencies"]:
            if a not in by_artifact:
                continue                     # external library, not in-estate
            if g and group_ids and g not in group_ids:
                continue
            dst_is_service = any(
                n for n in nodes
                if n.get("extra", {}).get("artifactId") == a and n["kind"] == "service")
            dst_canon = cfg.resolve_service(a) or a
            dst_id = (ids.service_id(dst_canon) if dst_is_service
                      else "cart . library maven:%s" % a)
            if dst_id == src_id:
                continue
            edges.append({"src": src_id, "dst": dst_id, "kind": "depends-on",
                          "evidence": ev, "provenance": "EXTRACTED",
                          "confidence": 1.0,
                          "extra": {"via": "maven dependency %s" % a}})

    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"poms": len(poms), "services": n_services, "libraries": n_libs,
            "outside_reactor": n_excluded, "module_edges": len(edges)}
