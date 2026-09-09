"""
Behavioural analysis of version-control history (Code Maat family).

Static analysis tells you how the code is written. History tells you how it is
actually worked on, which is different and often more useful:

  * hotspots      churn x complexity -- where the defects concentrate
  * coupling      files that keep changing together, including across repos,
                  a dependency no parser can see
  * ownership     who to ask, and who to put on a review
  * knowledge     what has a bus factor of one

Cross-repo coupling is the part that matters most in a microservice estate.
Two repos have no shared commits, so we correlate on the ticket key in the
commit message first (reliable) and fall back to same-author-same-day.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict

from ..config import unit_to_git_map

SOURCE = "history"

TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
MERGE_SUBJECT = re.compile(r"^(Merge|Revert)\b", re.I)

# Files whose churn says nothing about design.
BORING = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Gemfile\.lock|"
    r"poetry\.lock|Cargo\.lock|go\.sum|composer\.lock|\.min\.(js|css)|"
    r"CHANGELOG(\.md)?|\.snap)$", re.I)


def _git(repo, args, timeout=180):
    try:
        r = subprocess.run(["git"] + args, cwd=repo, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_git_repo(path):
    return _git(path, ["rev-parse", "--git-dir"]) is not None


def read_log(repo, months=12, max_commits=20000):
    """
    Parse `git log --numstat` into commit records.

    Merge commits are excluded: they attribute every file in the merge to
    whoever pressed the button, which destroys both ownership and coupling.
    """
    since = "%d months ago" % months
    fmt = "@@%H|%an|%aI|%s"
    out = _git(repo, ["log", "--no-merges", "-M", "-C",
                      "--since=" + since, "--numstat",
                      "--format=" + fmt, "--max-count=%d" % max_commits])
    if out is None:
        return []
    commits = []
    cur = None
    for line in out.split("\n"):
        if line.startswith("@@"):
            if cur and cur["files"]:
                commits.append(cur)
            parts = line[2:].split("|", 3)
            if len(parts) < 4:
                cur = None
                continue
            sha, author, date, subject = parts
            cur = {"sha": sha, "author": author.strip(), "date": date,
                   "subject": subject, "files": [],
                   "tickets": set(TICKET.findall(subject))}
            continue
        if cur is None or not line.strip():
            continue
        bits = line.split("\t")
        if len(bits) != 3:
            continue
        added, deleted, path = bits
        # rename form: "old => new" or "dir/{a => b}/file"
        if "=>" in path:
            path = _resolve_rename(path)
        if BORING.search(path):
            continue
        try:
            a = int(added)
        except ValueError:
            a = 0        # binary files show as "-"
        try:
            d = int(deleted)
        except ValueError:
            d = 0
        cur["files"].append((path, a, d))
    if cur and cur["files"]:
        commits.append(cur)
    return commits


def _resolve_rename(path):
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", path)
    if m:
        return (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
    if " => " in path:
        return path.split(" => ")[-1]
    return path


def analyse_repo(repo_root, repo_name, months, max_files_per_commit):
    """Per-file metrics, ownership rows, and in-repo co-change counts."""
    commits = read_log(repo_root, months)
    metrics = defaultdict(lambda: {"revisions": 0, "added": 0, "deleted": 0,
                                   "authors": set(), "first": None, "last": None})
    own = defaultdict(int)
    own_added = defaultdict(int)
    own_last = {}
    pairs = defaultdict(int)
    ticket_files = defaultdict(set)
    author_day_files = defaultdict(set)

    for c in commits:
        paths = [f[0] for f in c["files"]]
        if MERGE_SUBJECT.match(c["subject"]):
            continue
        for path, a, d in c["files"]:
            m = metrics[path]
            m["revisions"] += 1
            m["added"] += a
            m["deleted"] += d
            m["authors"].add(c["author"])
            if m["first"] is None or c["date"] < m["first"]:
                m["first"] = c["date"]
            if m["last"] is None or c["date"] > m["last"]:
                m["last"] = c["date"]
            own[(path, c["author"])] += 1
            own_added[(path, c["author"])] += a
            prev = own_last.get((path, c["author"]))
            if prev is None or c["date"] > prev:
                own_last[(path, c["author"])] = c["date"]

        # Co-change. A commit touching a very large number of files is a
        # formatting sweep or a dependency bump; counting it would couple
        # everything to everything.
        if 1 < len(paths) <= max_files_per_commit:
            uniq = sorted(set(paths))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    pairs[(uniq[i], uniq[j])] += 1

        day = c["date"][:10]
        for t in c["tickets"]:
            ticket_files[t].update((repo_name, p) for p in paths)
        author_day_files[(c["author"], day)].update((repo_name, p) for p in paths)

    # monthly rollup for the time-lapse
    months = defaultdict(lambda: {"commits": 0, "authors": set(),
                                  "files": set(), "added": 0, "deleted": 0})
    for c in commits:
        mk = c["date"][:7]
        rec = months[mk]
        rec["commits"] += 1
        rec["authors"].add(c["author"])
        for path, a, d in c["files"]:
            rec["files"].add(path)
            rec["added"] += a
            rec["deleted"] += d

    return {"metrics": metrics, "ownership": own, "own_added": own_added,
            "own_last": own_last, "pairs": pairs, "commits": len(commits),
            "ticket_files": ticket_files, "author_day_files": author_day_files,
            "months": months}


def run(store, cfg, repos, progress=None):
    # A pair of files is only genuinely CROSS-REPO when the two build
    # units live in different git repositories. Treating every module as a
    # repo marks coupling inside one checkout as cross-repo and inflates
    # the figure by orders of magnitude.
    _gmap = unit_to_git_map(repos)

    months = int(cfg.defaults.get("history_months", 12))
    max_fpc = int(cfg.defaults.get("max_commit_files", 40))
    min_shared = int(cfg.defaults.get("min_coupling_shared", 3))
    min_degree = float(cfg.defaults.get("min_coupling_degree", 0.30))

    store.conn.execute("DELETE FROM file_metrics")
    store.conn.execute("DELETE FROM coupling")
    store.conn.execute("DELETE FROM ownership")
    store.conn.execute("DELETE FROM timeline")

    all_tickets = defaultdict(set)
    all_author_day = defaultdict(set)
    repo_revs = {}
    total_commits = 0
    no_git = []

    for repo_name, repo_root, service in repos:
        if not is_git_repo(repo_root):
            no_git.append(repo_name)
            continue
        if progress:
            progress("  history: %s…" % repo_name)
        res = analyse_repo(repo_root, repo_name, months, max_fpc)
        total_commits += res["commits"]

        rows = []
        for path, m in res["metrics"].items():
            rows.append((repo_name, path, m["revisions"], len(m["authors"]),
                         m["added"], m["deleted"], m["last"], m["first"]))
            repo_revs[(repo_name, path)] = m["revisions"]
        store.conn.executemany(
            "INSERT INTO file_metrics(repo,path,revisions,authors,added,deleted,"
            "last_change,first_change) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo,path) DO UPDATE SET revisions=excluded.revisions,"
            "authors=excluded.authors, added=excluded.added,"
            "deleted=excluded.deleted, last_change=excluded.last_change,"
            "first_change=excluded.first_change", rows)

        store.conn.executemany(
            "INSERT INTO ownership(repo,path,author,commits,added,last) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(repo,path,author) DO UPDATE SET "
            "commits=excluded.commits, added=excluded.added, last=excluded.last",
            [(repo_name, p, a, n, res["own_added"].get((p, a), 0),
              res["own_last"].get((p, a)))
             for (p, a), n in res["ownership"].items()])

        # in-repo coupling
        crows = []
        for (a, b), shared in res["pairs"].items():
            ra = res["metrics"][a]["revisions"]
            rb = res["metrics"][b]["revisions"]
            avg = (ra + rb) / 2.0
            if avg <= 0:
                continue
            degree = shared / avg
            if shared >= min_shared and degree >= min_degree:
                crows.append((repo_name + "/" + a, repo_name + "/" + b,
                              repo_name, repo_name, shared, ra, rb,
                              round(degree, 4), 0))
        if crows:
            store.conn.executemany(
                "INSERT OR REPLACE INTO coupling(a,b,a_repo,b_repo,shared,"
                "a_revs,b_revs,degree,cross_repo) VALUES(?,?,?,?,?,?,?,?,?)", crows)

        store.conn.executemany(
            "INSERT OR REPLACE INTO timeline(month,service,repo,commits,authors,"
            "files,added,deleted) VALUES(?,?,?,?,?,?,?,?)",
            [(mk, service or repo_name, repo_name, r["commits"],
              len(r["authors"]), len(r["files"]), r["added"], r["deleted"])
             for mk, r in res["months"].items()])

        for t, files in res["ticket_files"].items():
            all_tickets[t].update(files)
        for k, files in res["author_day_files"].items():
            all_author_day[k].update(files)

    # ---- cross-repo coupling ------------------------------------------
    # Shared ticket key is the reliable signal; same-author-same-day is a
    # weaker fallback and is recorded with a lower degree so it can never
    # outrank real evidence.
    cross = defaultdict(int)
    cross_weak = defaultdict(int)
    for t, files in all_tickets.items():
        repos_touched = {r for r, _ in files}
        if len(repos_touched) < 2 or len(files) > max_fpc * 2:
            continue
        items = sorted(files)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ra, pa), (rb, pb) = items[i], items[j]
                if ra == rb:
                    continue
                cross[((ra, pa), (rb, pb))] += 1
    for (author, day), files in all_author_day.items():
        repos_touched = {r for r, _ in files}
        if len(repos_touched) < 2 or len(files) > max_fpc * 2:
            continue
        items = sorted(files)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ra, pa), (rb, pb) = items[i], items[j]
                if ra == rb:
                    continue
                cross_weak[((ra, pa), (rb, pb))] += 1

    crows = []
    for (ka, kb), shared in cross.items():
        if shared < max(2, min_shared - 1):
            continue
        ra, pa = ka
        rb, pb = kb
        va = repo_revs.get((ra, pa), shared)
        vb = repo_revs.get((rb, pb), shared)
        degree = shared / max(1.0, (va + vb) / 2.0)
        xr = 1 if _gmap.get(ra, ra) != _gmap.get(rb, rb) else 0
        crows.append(("%s/%s" % (ra, pa), "%s/%s" % (rb, pb), ra, rb,
                      shared, va, vb, round(min(degree, 1.0), 4), xr))
    for (ka, kb), shared in cross_weak.items():
        if ((ka, kb)) in cross or shared < max(3, min_shared):
            continue
        ra, pa = ka
        rb, pb = kb
        va = repo_revs.get((ra, pa), shared)
        vb = repo_revs.get((rb, pb), shared)
        degree = 0.5 * shared / max(1.0, (va + vb) / 2.0)
        xr = 1 if _gmap.get(ra, ra) != _gmap.get(rb, rb) else 0
        crows.append(("%s/%s" % (ra, pa), "%s/%s" % (rb, pb), ra, rb,
                      shared, va, vb, round(min(degree, 1.0), 4), xr))
    if crows:
        store.conn.executemany(
            "INSERT OR REPLACE INTO coupling(a,b,a_repo,b_repo,shared,a_revs,"
            "b_revs,degree,cross_repo) VALUES(?,?,?,?,?,?,?,?,?)", crows)

    # ---- main author per file ------------------------------------------
    store.conn.execute("""
        UPDATE file_metrics SET
          main_author = (SELECT author FROM ownership o
                         WHERE o.repo=file_metrics.repo AND o.path=file_metrics.path
                         ORDER BY o.commits DESC, o.author LIMIT 1),
          author_share = COALESCE((SELECT CAST(MAX(o.commits) AS REAL)/
                          NULLIF((SELECT SUM(o2.commits) FROM ownership o2
                                  WHERE o2.repo=file_metrics.repo
                                    AND o2.path=file_metrics.path),0)
                         FROM ownership o
                         WHERE o.repo=file_metrics.repo AND o.path=file_metrics.path),0)
    """)

    for r in no_git:
        store.add_gap("no-git-history",
                      "%s is not a git repository, so it has no behavioural data" % r,
                      "hotspots, coupling and ownership will be missing for it",
                      SOURCE)
    store.commit()

    # Only pairs actually flagged cross_repo=1 count; the rest are cross-module
    # inside a single checkout, which is a different and far less notable thing.
    n_cross = sum(1 for r in crows if r[-1]) if crows else 0
    n_files = store.conn.execute("SELECT COUNT(*) n FROM file_metrics").fetchone()["n"]
    n_coup = store.conn.execute("SELECT COUNT(*) n FROM coupling").fetchone()["n"]
    n_months = store.conn.execute(
        "SELECT COUNT(DISTINCT month) n FROM timeline").fetchone()["n"]
    return {"commits": total_commits, "files_with_history": n_files,
            "months": n_months,
            "coupling_pairs": n_coup, "cross_repo_pairs": n_cross,
            "repos_without_git": no_git}
