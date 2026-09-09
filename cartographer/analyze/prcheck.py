"""
Architectural review of a change set.

The map is only useful if it is consulted, and the reliable moment to consult
it is when a diff already exists. So this takes a git range and answers the
questions a reviewer would ask if they had the whole estate in their head:

  * does this touch a table that more than one service writes?
  * does it change an endpoint somebody's frontend calls?
  * which services other than the obvious one are downstream?
  * who has actually worked on these files, and should therefore review it?

It reads the graph and `git diff --name-only`. It does not read the diff
content, does not judge the code, and does not gate anything -- the output is
context for a human review, not a verdict.

Everything is scoped to "among the repos I can see". A change can have
consequences in a repository that is not on this machine, and this cannot know
that.
"""
from __future__ import annotations

import os
import subprocess


def _git(repo, args, timeout=30):
    try:
        r = subprocess.run(["git"] + args, cwd=repo, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


def default_range(repo):
    """
    A sensible base..HEAD for this checkout.

    Tries the upstream of the current branch, then the repo's default branch,
    then the merge-base with it. Returns None when nothing can be determined,
    rather than inventing a range and reporting a diff nobody asked about.
    """
    up = _git(repo, ["rev-parse", "--abbrev-ref", "@{upstream}"])
    if up and up.strip():
        return "%s..HEAD" % up.strip()
    for base in ("origin/develop", "origin/main", "origin/master"):
        if _git(repo, ["rev-parse", "--verify", "--quiet", base]):
            mb = _git(repo, ["merge-base", base, "HEAD"])
            if mb and mb.strip():
                return "%s..HEAD" % mb.strip()
    return None


def _three_dot(rng):
    """
    Turn `base..HEAD` into `base...HEAD`.

    `git diff a..b` compares the two endpoints, so once `develop` has been
    merged into a branch it reports every file that differs between them --
    hundreds of files the author never touched. `a...b` diffs from the merge
    base, which is the change set actually under review.
    """
    if not rng or "..." in rng:
        return rng
    if ".." in rng:
        return rng.replace("..", "...", 1)
    return rng


def changed_files(repos, rng=None):
    """
    `[(unit, repo_root, relative path)]` for every changed file.

    Each git repository is diffed once, then the result attributed to whichever
    scanned unit owns each path -- a Maven checkout holds many units, and
    diffing per unit would run the same command 26 times.
    """
    seen_roots = {}
    for name, root, _svc in repos:
        cur = os.path.abspath(root)
        while True:
            if os.path.isdir(os.path.join(cur, ".git")):
                break
            nxt = os.path.dirname(cur)
            if nxt == cur:
                cur = None
                break
            cur = nxt
        if cur:
            seen_roots.setdefault(cur, []).append((name, os.path.abspath(root)))

    out = []
    for git_root, units in sorted(seen_roots.items()):
        use = rng or default_range(git_root)
        if not use:
            continue
        raw = _git(git_root, ["diff", "--name-only", _three_dot(use)])
        if raw is None:
            raw = _git(git_root, ["diff", "--name-only", "HEAD"]) or ""
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            abs_path = os.path.join(git_root, line)
            # Longest matching unit root wins, so `server/agent/...` is
            # attributed to `agent` rather than to the outer checkout.
            best = None
            for unit, uroot in units:
                if abs_path.startswith(uroot + os.sep) or abs_path == uroot:
                    if best is None or len(uroot) > len(best[1]):
                        best = (unit, uroot)
            if best:
                out.append((best[0], best[1],
                            os.path.relpath(abs_path, best[1]).replace(os.sep, "/")))
            else:
                out.append((os.path.basename(git_root), git_root, line))
    return out


def analyse(store, files):
    """Architectural consequences of a set of changed files."""
    res = {"files": len(files), "matched": 0, "unmatched": [],
           "services": {}, "tables": [], "routes": [], "callers": [],
           "reviewers": [], "multi_writer": []}
    if not files:
        return res

    node_ids = []
    for unit, _root, rel in files:
        row = store.conn.execute(
            "SELECT id, service, repo FROM nodes WHERE kind='file' "
            "AND repo=? AND file=?", (unit, rel)).fetchone()
        if not row:
            res["unmatched"].append("%s/%s" % (unit, rel))
            continue
        res["matched"] += 1
        node_ids.append(row["id"])
        svc = row["service"] or row["repo"]
        res["services"][svc] = res["services"].get(svc, 0) + 1

    if not node_ids:
        return res
    marks = ",".join("?" * len(node_ids))

    # --- tables these files' services touch, with writer counts ------------
    svcs = sorted(res["services"])
    if svcs:
        smarks = ",".join("?" * len(svcs))
        rows = store.conn.execute(
            "SELECT d.name AS tbl, e.kind AS kind, s.name AS svc FROM edges e "
            "JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
            "WHERE e.kind IN ('writes-table','reads-table') "
            "AND s.name IN (%s)" % smarks, tuple(svcs))
        touched = {}
        for r in rows:
            touched.setdefault(r["tbl"], set()).add(r["svc"])
        if touched:
            tmarks = ",".join("?" * len(touched))
            writers = {}
            for r in store.conn.execute(
                    "SELECT d.name AS tbl, s.name AS svc FROM edges e "
                    "JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
                    "WHERE e.kind='writes-table' AND d.name IN (%s)" % tmarks,
                    tuple(touched)):
                writers.setdefault(r["tbl"], set()).add(r["svc"])
            for tbl in sorted(touched):
                w = sorted(writers.get(tbl, []))
                if len(w) > 1:
                    res["multi_writer"].append((tbl, w))
            res["tables"] = sorted(touched)[:40]

    # --- endpoints these files serve, and who calls them ------------------
    for r in store.conn.execute(
            "SELECT s.name AS route, s.service AS svc FROM edges e "
            "JOIN nodes s ON s.id=e.src "
            "WHERE e.kind='handled-by' AND e.dst IN (%s)" % marks,
            tuple(node_ids)):
        res["routes"].append("%s  (%s)" % (r["route"], r["svc"]))
    if res["routes"]:
        rmarks = ",".join("?" * len(res["routes"]))
        # Frontend call sites for the routes these files serve: a changed
        # controller with a live caller is a contract change, not a local one.
        for r in store.conn.execute(
                "SELECT DISTINCT e.evidence AS ev, d.name AS route FROM edges e "
                "JOIN nodes d ON d.id=e.dst JOIN nodes h ON h.id=e.dst "
                "WHERE e.kind='calls-route' AND d.id IN ("
                "  SELECT e2.src FROM edges e2 WHERE e2.kind='handled-by' "
                "  AND e2.dst IN (%s))" % marks, tuple(node_ids)):
            res["callers"].append("%s -> %s" % (r["ev"], r["route"]))

    # --- reviewers, from who has actually touched these paths -------------
    # `ownership.path` is relative to the GIT ROOT (`server/agent/src/...`)
    # while a change set is relative to the build unit (`src/...`), so an
    # equality match silently finds nobody. Suffix matching, still scoped by
    # unit, bridges the two without needing both paths threaded through.
    tally = {}
    for unit, _root, rel in files:
        for r in store.conn.execute(
                "SELECT author, commits FROM ownership "
                "WHERE repo=? AND (path=? OR path LIKE ?)",
                (unit, rel, "%/" + rel)):
            tally[r["author"]] = tally.get(r["author"], 0) + (r["commits"] or 0)
    res["reviewers"] = sorted(tally.items(), key=lambda t: -t[1])[:6]
    return res
