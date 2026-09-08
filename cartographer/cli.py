"""Command-line interface. One entry point for everything."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, config as cfgmod
from .store import Store
from .extract import (symbols, topology, history, specs, builddeps,
                      traces, maven, kafka, frontend, sqlschema,
                      compose, secrets)
from .analyze import pagerank, hotspots, impact, repomap
from .report import markdown as md_report, html as html_report, mermaid

IS_TTY = sys.stderr.isatty()


def note(msg=""):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def out(msg=""):
    sys.stdout.write(msg + "\n")


def _repos_for(cfg, only=None):
    """Resolve (repo_name, repo_root, service) triples from config."""
    roots = []
    for svc in cfg.services:
        if svc.get("repo") and os.path.isdir(svc["repo"]):
            roots.append(svc["repo"])
    discovered = cfgmod.discover_repos(cfg.roots) if cfg.roots else []
    for d in discovered:
        if d not in roots:
            roots.append(d)
    seen, triples = set(), []
    for r in roots:
        rp = os.path.realpath(r)
        if rp in seen:
            continue
        seen.add(rp)
        name = os.path.basename(r.rstrip("/")) or r
        svc = cfg.service_for_repo(name) or name
        if only and name not in only and svc not in only:
            continue
        triples.append((name, r, svc))

    # Content that sits in the workspace but inside no repository -- a
    # top-level database/ tree, a shared openapi/ folder, a parent POM above
    # sibling checkouts -- would otherwise never be scanned, silently.
    if not only:
        repo_paths = [x[1] for x in triples]
        for label, path in cfgmod.orphan_roots(cfg.roots, repo_paths):
            if os.path.realpath(path) in seen:
                continue
            seen.add(os.path.realpath(path))
            triples.append((label, path, cfg.service_for_repo(label) or label))
        # Every repo is scanned as its own target, so walking the workspace
        # root must skip them -- otherwise every file is discovered twice under
        # two different repo labels, doubling the graph and the runtime.
        cfgmod.set_pruned(repo_paths)
    return triples


def _open(cfg, create=True):
    path = cfg.db_path()
    if not create and not os.path.exists(path):
        note("No graph at %s. Run `cartographer scan` first." % path)
        raise SystemExit(2)
    cfg.ensure_out()
    return Store(path)


# ---------------------------------------------------------------- init

def cmd_init(args):
    """
    Discover the estate and write a config that is already mostly right.

    Repos alone are not the service inventory. In a Maven multi-module backend
    the services are the MODULES, and the aliases that matter come from
    docker-compose container names and frontend proxy prefixes. Gathering all
    of that here is what turns the first scan from noisy into useful.
    """
    base = os.path.abspath(args.dir or os.getcwd())
    roots = [os.path.abspath(os.path.expanduser(r)) for r in (args.root or [])]
    if not roots:
        note("No --root given; scanning the current directory.")
        roots = [base]
    found = cfgmod.discover_repos(roots)
    if not found:
        note("Found no repositories under: %s" % ", ".join(roots))
        note("Point --root at the directory that CONTAINS your service repos.")
        return 1

    note("Found %d repositories. Discovering services…" % len(found))
    triples = [(os.path.basename(r.rstrip("/")), r, os.path.basename(r.rstrip("/")))
               for r in found]
    empty = cfgmod.Config({}, None, base)

    # --- Maven modules -------------------------------------------------
    modules = {}          # name -> dict
    for repo_name, repo_root, _s in triples:
        for pom in maven.find_poms(repo_root):
            info = maven.parse_pom(pom)
            if not info:
                continue
            d = os.path.dirname(pom)
            if info["packaging"] == "pom" and info["modules"]:
                continue
            deploy = maven._deployable(d)
            sig = maven._java_signals(d)
            cfgfile = maven._has_app_config(d)
            is_svc = bool(deploy) or sig["app"] or cfgfile or \
                info["packaging"] == "war" or sig["controller"]
            modules[info["artifactId"]] = {
                "name": info["artifactId"], "repo": repo_name, "dir": d,
                "service": is_svc,
                "in_reactor": None, "schema": None, "aliases": set()}
    # mark reactor membership
    declared = set()
    for repo_name, repo_root, _s in triples:
        for pom in maven.find_poms(repo_root):
            info = maven.parse_pom(pom)
            if info and info["modules"]:
                for m in info["modules"]:
                    declared.add(os.path.normpath(
                        os.path.join(os.path.dirname(pom), m)))
    for m in modules.values():
        m["in_reactor"] = os.path.normpath(m["dir"]) in declared

    # --- docker-compose: container names and schemas --------------------
    compose_map = {}
    for repo_name, repo_root, _s in triples:
        for cpath in compose._find(repo_root):
            doc, _text = compose._load(cpath)
            if not isinstance(doc, dict):
                continue
            for cname, body in (doc.get("services") or {}).items():
                if not isinstance(body, dict):
                    continue
                image = str(body.get("image") or "")
                if compose._is_infra(str(cname), image):
                    continue
                env = compose._env_items(body.get("environment"))
                schema = None
                for val in env.values():
                    m = compose.JDBC.search(val or "")
                    if m:
                        schema = m.group(4)
                        break
                compose_map[str(cname)] = {"image": image, "schema": schema}

    # --- frontend proxy prefixes ----------------------------------------
    prefixes = set()
    for repo_name, repo_root, _s in triples:
        for path in frontend._files(repo_root, names=frontend.PROXY_NAMES):
            text = frontend._read(path)
            if not text:
                continue
            for m in frontend.PROXY_ENTRY.finditer(text):
                seg = [s for s in m.group("prefix").split("/") if s]
                if seg:
                    prefixes.add(seg[0])

    # --- fold discoveries into aliases ----------------------------------
    def norm(s):
        return cfgmod._norm_token(s)

    by_norm = {norm(k): k for k in modules}
    for cname, meta in compose_map.items():
        key = by_norm.get(norm(cname))
        if key:
            modules[key]["aliases"].add(cname)
            if meta["schema"]:
                modules[key]["schema"] = meta["schema"]
        else:
            tail = (meta["image"] or "").split("/")[-1].split(":")[0]
            key2 = by_norm.get(norm(tail))
            if key2:
                modules[key2]["aliases"].add(cname)
                if meta["schema"]:
                    modules[key2]["schema"] = meta["schema"]
            elif cname:
                modules[cname] = {"name": cname, "repo": "", "dir": "",
                                  "service": True, "in_reactor": None,
                                  "schema": meta["schema"], "aliases": set()}
                by_norm[norm(cname)] = cname

    def closest_module(token):
        """
        Suggest a module for an unmatched proxy prefix.

        `/omsenterprise/` and `order-enterprise` share no exact form but one
        clearly means the other, so fall back to substring containment on the
        normalised tokens and take the longest overlap.
        """
        nt = norm(token)
        best, best_len = None, 0
        for name in modules:
            nn = norm(name)
            if not nn or not nt:
                continue
            if nn in nt or nt in nn:
                overlap = min(len(nn), len(nt))
                if overlap > best_len and overlap >= 4:
                    best, best_len = name, overlap
        return best

    unmatched_prefixes = []
    for pre in sorted(prefixes):
        key = by_norm.get(norm(pre))
        if key:
            if norm(pre) != norm(key):
                modules[key]["aliases"].add(pre)
        else:
            guess = closest_module(pre)
            if guess:
                # Record it as an alias, but tell the user it was a guess.
                modules[guess]["aliases"].add(pre)
                unmatched_prefixes.append((pre, guess))
            else:
                unmatched_prefixes.append((pre, None))

    services = {k: v for k, v in modules.items() if v["service"]}
    libraries = sorted(k for k, v in modules.items() if not v["service"])

    # --- write it out ----------------------------------------------------
    L = ["# cartographer configuration",
         "# Generated %s by `cartographer init`. Your edits are never overwritten."
         % time.strftime("%Y-%m-%d"),
         "",
         "roots:"]
    L += ["  - %s" % r for r in roots]
    L += ["", "defaults:", "  history_months: 12", "  call_edges: true", ""]

    L += ["# Aliases are the highest-leverage thing in this file: the same",
          "# service is written many ways across code, config and image tags.",
          "# Those below were discovered automatically -- add any internal",
          "# codenames or legacy names you know of.",
          "services:"]
    for name in sorted(services):
        m = services[name]
        L.append("  - name: %s" % name)
        if m["dir"]:
            L.append("    repo: %s" % m["dir"])
        al = sorted(a for a in m["aliases"] if a != name)
        L.append("    aliases: [%s]" % ", ".join(al))
        if m["schema"]:
            L.append("    owns_data: [%s]   # schema from docker-compose" % m["schema"])
        else:
            L.append("    owns_data: []")
        if m["in_reactor"] is False and m["dir"]:
            L.append("    # NOTE: builds and deploys outside the parent reactor")
        L.append("    purpose: \"\"")

    for pre, guess in unmatched_prefixes:
        if guess:
            continue      # already added as an alias above
        L += ["  # A frontend proxies /%s/ but no module matched. If this is a" % pre,
              "  # real service whose repo you do not have, name it here so",
              "  # edges pointing at it resolve instead of being dropped.",
              "  # - name: %s" % pre,
              "  #   aliases: []"]

    # A repo whose modules are already listed is not itself a service; adding
    # it would double-count every finding inside it.
    module_dirs = [m["dir"] for m in modules.values() if m["dir"]]
    repo_by_name = dict((a, b) for a, b, _c in triples)
    for repo_name, repo_root, _s in triples:
        if norm(repo_name) in by_norm:
            continue
        if any(d.startswith(repo_root.rstrip("/") + os.sep) for d in module_dirs):
            L += ["  # %s contains the modules listed above; it is a container"
                  % repo_name,
                  "  # for them, not a service in its own right."]
            continue
        L += ["  - name: %s" % repo_name, "    repo: %s" % repo_by_name[repo_name],
              "    aliases: []", "    owns_data: []", "    purpose: \"\""]

    L += ["", "flows:", "  - buyer submits an order", "",
          "# Exported APM service graphs -- measured traffic beats inferred",
          "# config, and reveals services whose repos you do not have.",
          "traces: []", ""]

    dest = os.path.join(base, "cartographer.yaml")
    if os.path.exists(dest) and not args.force:
        note("%s already exists. Use --force to overwrite." % dest)
        return 1
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    note("")
    note("Wrote %s" % dest)
    note("  %d repositories" % len(found))
    note("  %d services%s" % (len(services),
                              (" (%d outside the Maven reactor)" %
                               sum(1 for m in services.values()
                                   if m["in_reactor"] is False))
                              if any(m["in_reactor"] is False
                                     for m in services.values()) else ""))
    if libraries:
        note("  %d shared libraries (not services): %s"
             % (len(libraries), ", ".join(libraries[:10])))
    if compose_map:
        note("  %d containers in docker-compose" % len(compose_map))
    guessed = [(p, g) for p, g in unmatched_prefixes if g]
    unknown = [p for p, g in unmatched_prefixes if not g]
    if guessed:
        note("  %d proxy prefixes matched by similarity (verify these):" % len(guessed))
        for pre, g in guessed:
            note("      /%s/  ->  %s" % (pre, g))
    if unknown:
        note("  %d proxy prefixes with no matching module: %s"
             % (len(unknown), ", ".join(unknown)))
        note("    (commented out in the config -- uncomment the real ones)")
    note("")
    note("Next: skim the aliases, then `cartographer scan`.")
    return 0


# ---------------------------------------------------------------- scan

def cmd_scan(args):
    cfg = cfgmod.load(args.config)
    if not cfg.path and not cfg.roots:
        note("No configuration found. Run `cartographer init --root <dir>` first.")
        return 2
    triples = _repos_for(cfg, args.only)
    if not triples:
        note("No repositories resolved. Check `roots:` and `services:` in %s"
             % (cfg.path or "your config"))
        return 2

    st = _open(cfg)
    t_start = time.time()
    note("Scanning %d repositories -> %s" % (len(triples), cfg.db_path()))
    for name, root, svc in triples:
        note("  %-22s %s" % (name, root))
    note("")

    stats = {}
    steps = []
    # maven first: it decides what counts as a service, which every later
    # extractor attributes findings to.
    steps.append(("maven", lambda: maven.run(st, cfg, triples, note)))
    steps.append(("compose", lambda: compose.run(st, cfg, triples, note)))
    if not args.skip_symbols:
        steps.append(("symbols", lambda: symbols.run(st, cfg, triples, note)))
    steps.append(("topology", lambda: topology.run(st, cfg, triples, note)))
    steps.append(("kafka", lambda: kafka.run(st, cfg, triples, note)))
    steps.append(("frontend", lambda: frontend.run(st, cfg, triples, note)))
    if not args.skip_sql:
        steps.append(("sqlschema", lambda: sqlschema.run(st, cfg, triples, note)))
    steps.append(("specs", lambda: specs.run(st, cfg, triples, note)))
    steps.append(("builddeps", lambda: builddeps.run(st, cfg, triples, note)))
    if not args.skip_secrets:
        steps.append(("secrets", lambda: secrets.run(st, cfg, triples, note)))
    if not args.skip_history:
        steps.append(("history", lambda: history.run(st, cfg, triples, note)))

    trace_files = list(args.trace or []) + list(cfg.raw.get("traces") or [])
    if trace_files:
        steps.append(("traces", lambda: traces.run(st, cfg, trace_files, note)))

    for name, fn in steps:
        t0 = time.time()
        try:
            stats[name] = fn()
        except Exception as exc:
            # One failing extractor must not lose the rest of the scan.
            note("  ! %s failed: %s" % (name, exc))
            if args.debug:
                import traceback
                traceback.print_exc()
            st.add_gap("extractor-failed", "%s: %s" % (name, exc),
                       "re-run with --debug for the traceback", name)
            stats[name] = {"error": str(exc)}
            continue
        note("  %-10s %.1fs  %s" % (name, time.time() - t0,
                                    _brief(stats[name])))

    note("")
    note("  analysing…")
    stats["hotspots"] = hotspots.compute(st)
    scores = pagerank.compute(st)
    stats["pagerank"] = {"ranked": len(scores)}

    st.set_meta("scanned_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    st.set_meta("version", __version__)
    st.set_meta("repos", json.dumps([t[0] for t in triples]))
    st.set_meta("stats", json.dumps(stats, default=str))
    st.commit()

    _write_reports(st, cfg)
    c = st.counts()
    sec = st.conn.execute(
        "SELECT COUNT(*) n FROM gaps WHERE category='credential-in-source'"
    ).fetchone()["n"]
    note("")
    if sec:
        note("  !! %d possible credential%s found in source. Values were NOT"
             % (sec, "" if sec == 1 else "s"))
        note("     recorded. Run `cartographer secrets` for the locations.")
        note("")
    note("Done in %.1fs." % (time.time() - t_start))
    note("  %d nodes, %d edges (%s)"
         % (c["nodes"], c["edges"],
            ", ".join("%s %d" % kv for kv in c["edges_by_provenance"].items())))
    note("  %d open gaps -- see the report" % c["gaps"])
    note("")
    note("  %s" % cfg.out("GRAPH_REPORT.md"))
    note("  %s" % cfg.out("graph.html"))
    note("")
    note("Next: `cartographer questions` for what to ask your team.")
    st.close()
    return 0


def _brief(d):
    if not isinstance(d, dict):
        return ""
    return " ".join("%s=%s" % (k, v) for k, v in d.items()
                    if isinstance(v, (int, float)) and v)


JSON_EXPORT_NODE_LIMIT = 20000


def _write_reports(st, cfg, force_json=False):
    cfg.ensure_out()
    with open(cfg.out("GRAPH_REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write(md_report.build(st, cfg))
    with open(cfg.out("graph.html"), "w", encoding="utf-8") as fh:
        fh.write(html_report.build(st))
    with open(cfg.out("topology.mmd"), "w", encoding="utf-8") as fh:
        fh.write(mermaid.service_graph(st))

    # The JSON export is for other tools and your own scripts. On a large
    # estate it runs to tens of megabytes and almost nobody reads it, so it is
    # skipped past a threshold -- graph.db holds the same data and is queryable.
    n = st.counts()["nodes"]
    if force_json or n <= JSON_EXPORT_NODE_LIMIT:
        data = {"nodes": [dict(r) for r in st.conn.execute("SELECT * FROM nodes")],
                "edges": [dict(r) for r in st.conn.execute("SELECT * FROM edges")]}
        with open(cfg.out("graph.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, default=str)
        return True
    note("  (skipped graph.json: %d nodes -- use `cartographer report --json` "
         "if you want it)" % n)
    return False


# ---------------------------------------------------------------- queries

def cmd_map(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    m = repomap.build(st, task=args.task, budget_tokens=args.budget)
    out(m["text"])
    note("")
    note("(%d tokens, %d/%d files, %s)"
         % (m["tokens"], m["files"], m.get("total_files", 0),
            "task-focused" if m["seeded"] else "global ranking"))
    st.close()
    return 0


def cmd_impact(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    cands = impact.resolve(st, args.target)
    if not cands:
        out("No node matches %r. Try `cartographer find %s`." % (args.target, args.target))
        st.close()
        return 1
    if len(cands) > 1 and not args.first:
        out("%d candidates matched %r:" % (len(cands), args.target))
        for i, c in enumerate(cands[:10], 1):
            out("  %d. %-9s %-28s %s" % (i, c["kind"], c["name"] or "",
                                         c["file"] or c["service"] or ""))
        out("")
        out("Analysing the highest-ranked. Use --first to skip this list,")
        out("or pass a more specific target such as Class.method.")
        out("")
    out(impact.render(impact.analyse(st, cands[0]["id"], depth=args.depth),
                      verbose=args.verbose))
    st.close()
    return 0


def cmd_find(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    rows = st.find_nodes(name=args.query, kind=args.kind, limit=args.limit)
    if not rows:
        out("No match for %r." % args.query)
        st.close()
        return 1
    for r in rows:
        owner = (r["container"] + ".") if r["container"] else ""
        loc = "%s:%s" % (r["file"], r["line"]) if r["file"] else "-"
        out("%-9s %-36s %-14s %s" % (r["kind"], (owner + (r["name"] or ""))[:36],
                                     r["service"] or r["repo"] or "-", loc))
    st.close()
    return 0


def cmd_topology(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    from .mcp.server import Server
    srv = Server(cfg.db_path(), cfg.path)
    srv._store = st
    srv._cfg = cfg
    out(mermaid.service_graph(st) if args.mermaid
        else srv._topology_text(st, args.service))
    st.close()
    return 0


def cmd_hotspots(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    rows = hotspots.top(st, args.limit, args.repo)
    if not rows:
        out("No hotspot data (needs git history).")
    else:
        out("%-8s %-6s %-6s %-4s %s" % ("SCORE", "REVS", "CX", "AUTH", "FILE"))
        for h in rows:
            out("%-8.4f %-6d %-6d %-4d %s/%s"
                % (h["hotspot"], h["revisions"], int(h["complexity"]),
                   h["authors"], h["repo"], h["path"]))
    if args.bus_factor:
        out("")
        out("Bus factor 1 (one author holds most of the history):")
        for b in hotspots.bus_factor(st, args.limit):
            out("  %-14s %-52s %s (%.0f%%)"
                % (b["repo"], b["path"][:52], b["main_author"], b["author_share"] * 100))
    st.close()
    return 0


def cmd_coupling(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    sql = "SELECT * FROM coupling WHERE 1=1"
    a = []
    if args.cross_repo:
        sql += " AND cross_repo=1"
    if args.path:
        sql += " AND (a LIKE ? OR b LIKE ?)"
        a += ["%" + args.path + "%"] * 2
    sql += " ORDER BY cross_repo DESC, degree DESC LIMIT ?"
    a.append(args.limit)
    rows = list(st.conn.execute(sql, a))
    if not rows:
        out("No coupling found.")
    for r in rows:
        out("%s shared=%-3d degree=%.2f" %
            ("CROSS-REPO" if r["cross_repo"] else "in-repo   ", r["shared"], r["degree"]))
        out("    %s" % r["a"])
        out("    %s" % r["b"])
    st.close()
    return 0


def cmd_owns(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    from .mcp.server import Server
    srv = Server(cfg.db_path(), cfg.path)
    srv._store = st
    srv._cfg = cfg
    out(srv.call("who_owns", {"path": args.path, "limit": args.limit}))
    st.close()
    return 0


def cmd_path(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    from .mcp.server import Server
    srv = Server(cfg.db_path(), cfg.path)
    srv._store = st
    srv._cfg = cfg
    out(srv._path(st, args.source, args.target, args.max_depth))
    st.close()
    return 0


def cmd_contracts(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    from .mcp.server import Server
    srv = Server(cfg.db_path(), cfg.path)
    srv._store = st
    srv._cfg = cfg
    out(srv._contracts(st, args.service))
    st.close()
    return 0


def cmd_secrets(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    rows = list(st.conn.execute(
        "SELECT detail, hint FROM gaps WHERE source='secrets' ORDER BY detail"))
    if not rows:
        out("No credential-shaped strings found.")
        out("")
        out("That is not a guarantee: this looks for common shapes only, and a "
            "secret in an unusual format will be missed.")
        st.close()
        return 0
    out("Possible credentials in source")
    out("")
    out("The VALUES were never read into the graph -- only these locations.")
    out("Open each line yourself to confirm. If real: rotate it, move it to a")
    out("secrets manager, and never paste the value into a prompt or a ticket.")
    out("")
    for r in rows:
        out("  %s" % r["detail"])
    st.close()
    return 0


def cmd_schema(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    if args.table:
        rows = list(st.conn.execute(
            "SELECT * FROM nodes WHERE kind='table' AND name LIKE ? COLLATE NOCASE",
            ("%" + args.table + "%",)))
        if not rows:
            out("No table matches %r." % args.table)
            st.close()
            return 1
        for n in rows[:20]:
            _print_table(st, n)
        st.close()
        return 0

    shared = list(st.conn.execute(
        "SELECT detail, hint FROM gaps WHERE category='shared-table' "
        "ORDER BY detail"))
    tables = st.conn.execute(
        "SELECT COUNT(*) n FROM nodes WHERE kind='table'").fetchone()["n"]
    out("%d tables in the graph." % tables)
    if shared:
        out("")
        out("Tables touched by more than one service -- coupling no API")
        out("contract documents:")
        out("")
        for r in shared:
            out("  %s" % r["detail"])
            out("      %s" % r["hint"])
    else:
        out("No cross-service table access detected.")
    st.close()
    return 0


def _print_table(st, n):
    import json as _json
    ex = _json.loads(n["extra"] or "{}")
    out("")
    out("%s" % n["name"])
    out("  declared: %s" % (ex.get("declared_at") or
                            "not found in the repos on disk"))
    if ex.get("migration_system"):
        out("  via:      %s migration" % ex["migration_system"])
    for kind, label in (("defines-table", "owned by"),
                        ("writes-table", "written by"),
                        ("reads-table", "read by")):
        rows = list(st.conn.execute(
            "SELECT src, evidence FROM edges WHERE dst=? AND kind=?",
            (n["id"], kind)))
        for r in rows:
            out("  %-9s %-16s %s" % (label, r["src"].split(" ", 3)[-1],
                                     r["evidence"] or ""))


def cmd_questions(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    out("Questions worth asking your team")
    out("(these are the things the code could not tell me)")
    out("")
    for q in md_report.suggest_questions(st):
        out("  - %s" % q)
    st.close()
    return 0


def cmd_report(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    wrote_json = _write_reports(st, cfg, force_json=args.json)
    note("Wrote:")
    files = ["GRAPH_REPORT.md", "graph.html", "topology.mmd"]
    if wrote_json:
        files.append("graph.json")
    for f in files:
        note("  %s" % cfg.out(f))
    st.close()
    return 0


def cmd_stats(args):
    cfg = cfgmod.load(args.config)
    st = _open(cfg, create=False)
    c = st.counts()
    meta = st.all_meta()
    out("graph:      %s" % cfg.db_path())
    out("built:      %s" % meta.get("scanned_at", "unknown"))
    out("version:    %s" % meta.get("version", "?"))
    out("repos:      %s" % ", ".join(c["repos"]))
    out("services:   %s" % ", ".join(x for x in c["services"] if x))
    out("languages:  %s" % c["languages"])
    out("nodes:      %d  %s" % (c["nodes"], c["nodes_by_kind"]))
    out("edges:      %d  %s" % (c["edges"], c["edges_by_kind"]))
    out("provenance: %s" % c["edges_by_provenance"])
    out("gaps:       %d" % c["gaps"])
    st.close()
    return 0


def cmd_serve(args):
    cfg = cfgmod.load(args.config)
    from .mcp.server import serve
    return serve(cfg.db_path(), cfg.path)


def cmd_doctor(args):
    """Check the environment before anything goes wrong at work."""
    ok = True
    out("cartographer %s" % __version__)
    out("python       %s" % sys.version.split()[0])
    if sys.version_info < (3, 8):
        out("  ! Python 3.8+ required")
        ok = False

    try:
        import yaml  # noqa: F401
        out("yaml         PyYAML present")
    except ImportError:
        out("yaml         using built-in mini parser (fine)")

    import shutil
    git = shutil.which("git")
    out("git          %s" % (git or "NOT FOUND -- history analysis disabled"))
    if not git:
        ok = False

    cfg = cfgmod.load(args.config)
    out("config       %s" % (cfg.path or "NONE -- run `cartographer init`"))
    if not cfg.path:
        ok = False
    else:
        out("roots        %s" % ", ".join(cfg.roots) or "(none)")
        for r in cfg.roots:
            if not os.path.isdir(r):
                out("  ! root does not exist: %s" % r)
                ok = False
        out("services     %d configured" % len(cfg.services))
        missing = [s["name"] for s in cfg.services
                   if s.get("repo") and not os.path.isdir(s["repo"])]
        if missing:
            out("  ! repo path missing for: %s" % ", ".join(missing))
        triples = _repos_for(cfg)
        out("resolved     %d repositories" % len(triples))
        for name, root, svc in triples[:20]:
            mark = " " if history.is_git_repo(root) else "!"
            out("  %s %-22s -> %-18s %s" % (mark, name, svc, root))
        if any(not history.is_git_repo(r) for _, r, _ in triples):
            out("  ! '!' means no git history: no hotspots, coupling or ownership")

    db = cfg.db_path()
    if os.path.exists(db):
        st = Store(db)
        c = st.counts()
        out("graph        %d nodes, %d edges, built %s"
            % (c["nodes"], c["edges"], st.get_meta("scanned_at", "?")))
        st.close()
    else:
        out("graph        not built yet -- run `cartographer scan`")

    out("")
    out("OK" if ok else "Problems found (see '!' lines above)")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="cartographer",
        description="Map a large multi-service codebase you did not write.")
    p.add_argument("--config", help="path to cartographer.yaml")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="discover repos and write a config")
    s.add_argument("--root", action="append", help="directory containing repos")
    s.add_argument("--dir", help="where to write the config (default: cwd)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("scan", help="build or refresh the graph")
    s.add_argument("--only", action="append", help="limit to these repos/services")
    s.add_argument("--trace", action="append", help="exported APM service-graph file")
    s.add_argument("--skip-history", action="store_true")
    s.add_argument("--skip-symbols", action="store_true")
    s.add_argument("--skip-sql", action="store_true",
                   help="skip schema/DDL and DAO SQL analysis")
    s.add_argument("--skip-secrets", action="store_true",
                   help="skip credential detection (locations only; values are "
                        "never recorded either way)")
    s.add_argument("--debug", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("map", help="ranked repo map for a task, within a token budget")
    s.add_argument("task", nargs="?", default=None)
    s.add_argument("--budget", type=int, default=2000)
    s.set_defaults(func=cmd_map)

    s = sub.add_parser("impact", help="blast radius of changing something")
    s.add_argument("target")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--verbose", "-v", action="store_true")
    s.add_argument("--first", action="store_true", help="skip the candidate list")
    s.set_defaults(func=cmd_impact)

    s = sub.add_parser("find", help="locate a symbol, file, service or route")
    s.add_argument("query")
    s.add_argument("--kind")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("topology", help="cross-service map")
    s.add_argument("service", nargs="?")
    s.add_argument("--mermaid", action="store_true")
    s.set_defaults(func=cmd_topology)

    s = sub.add_parser("hotspots", help="churn x complexity")
    s.add_argument("--repo")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--bus-factor", action="store_true")
    s.set_defaults(func=cmd_hotspots)

    s = sub.add_parser("coupling", help="files that change together")
    s.add_argument("--path")
    s.add_argument("--cross-repo", action="store_true")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_coupling)

    s = sub.add_parser("owns", help="who has worked on this code")
    s.add_argument("path")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(func=cmd_owns)

    s = sub.add_parser("path", help="how two things are connected")
    s.add_argument("source")
    s.add_argument("target")
    s.add_argument("--max-depth", type=int, default=6)
    s.set_defaults(func=cmd_path)

    s = sub.add_parser("contracts", help="routes and events a service exposes")
    s.add_argument("service", nargs="?")
    s.set_defaults(func=cmd_contracts)

    s = sub.add_parser("secrets", help="possible credentials in source (locations only)")
    s.set_defaults(func=cmd_secrets)

    s = sub.add_parser("schema", help="tables, owners, and cross-service access")
    s.add_argument("table", nargs="?", help="inspect one table")
    s.set_defaults(func=cmd_schema)

    s = sub.add_parser("questions", help="what to ask your team")
    s.set_defaults(func=cmd_questions)

    s = sub.add_parser("report", help="regenerate reports from the graph")
    s.add_argument("--json", action="store_true",
                   help="always write graph.json, however large")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("stats", help="what is in the graph")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("serve-mcp", help="run the MCP stdio server")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("doctor", help="check the environment")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        note("\ninterrupted")
        return 130
    except cfgmod.ConfigError as exc:
        note("config error: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
