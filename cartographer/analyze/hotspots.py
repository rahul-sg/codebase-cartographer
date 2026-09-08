"""
Hotspots: churn x complexity.

Tornhill's observation is that neither number alone tells you much. Complex
code nobody touches is harmless; simple code that changes constantly is fine.
The intersection -- complicated code under constant modification -- is where
defects concentrate and where your attention is worth most.

Adds bus-factor analysis, because "one person understands this" is the other
risk that only history reveals.
"""
from __future__ import annotations

import math


def compute(store):
    """Fill file_metrics.loc/complexity from the symbol graph, then score."""
    # Pull LOC and complexity recorded by the symbol extractor.
    rows = store.conn.execute(
        "SELECT repo, file, extra FROM nodes WHERE kind='file' AND extra IS NOT NULL"
    ).fetchall()
    import json
    updates = []
    for r in rows:
        try:
            ex = json.loads(r["extra"])
        except (ValueError, TypeError):
            continue
        updates.append((int(ex.get("loc") or 0), float(ex.get("complexity") or 0),
                        r["repo"], r["file"]))
    if updates:
        store.conn.executemany(
            "UPDATE file_metrics SET loc=?, complexity=? WHERE repo=? AND path=?",
            updates)

    # Normalise both axes to 0..1 across the estate, then multiply. Log-scaling
    # churn stops one runaway file from flattening everything else.
    mx = store.conn.execute(
        "SELECT MAX(revisions) r, MAX(complexity) c FROM file_metrics").fetchone()
    max_rev = float(mx["r"] or 1)
    max_cx = float(mx["c"] or 1)
    ln_max_rev = math.log1p(max_rev) or 1.0

    store.conn.execute(
        "UPDATE file_metrics SET hotspot = "
        "  (CASE WHEN complexity > 0 AND revisions > 0 "
        "        THEN (? * 1.0) * 0 + "
        "             ( (CAST(revisions AS REAL)) ) "
        "        ELSE 0 END)", (0,))
    # SQLite has no log(); compute in Python where it is clearer anyway.
    scored = []
    for r in store.conn.execute(
            "SELECT repo, path, revisions, complexity, authors, loc FROM file_metrics"):
        rev = float(r["revisions"] or 0)
        cx = float(r["complexity"] or 0)
        if rev <= 0 or cx <= 0:
            scored.append((0.0, r["repo"], r["path"]))
            continue
        churn_n = math.log1p(rev) / ln_max_rev
        cx_n = cx / max_cx if max_cx else 0.0
        scored.append((round(churn_n * cx_n, 6), r["repo"], r["path"]))
    store.conn.executemany(
        "UPDATE file_metrics SET hotspot=? WHERE repo=? AND path=?", scored)
    store.commit()
    return {"files_scored": len(scored), "max_revisions": int(max_rev),
            "max_complexity": int(max_cx)}


def top(store, limit=20, repo=None):
    sql = ("SELECT * FROM file_metrics WHERE hotspot > 0 %s "
           "ORDER BY hotspot DESC LIMIT ?" % ("AND repo=?" if repo else ""))
    args = ([repo, limit] if repo else [limit])
    return [dict(r) for r in store.conn.execute(sql, args)]


def bus_factor(store, limit=20, min_revisions=3, share=0.8):
    """
    Files where one author holds most of the history.

    A high share is not automatically a problem -- but combined with high churn
    it names the knowledge that leaves when that person does.
    """
    return [dict(r) for r in store.conn.execute(
        "SELECT repo, path, main_author, author_share, revisions, hotspot "
        "FROM file_metrics WHERE author_share >= ? AND revisions >= ? "
        "ORDER BY hotspot DESC, revisions DESC LIMIT ?",
        (share, min_revisions, limit))]


def knowledge_map(store):
    """Per-author footprint: how much of the estate each person has touched."""
    return [dict(r) for r in store.conn.execute(
        "SELECT author, COUNT(DISTINCT repo || '/' || path) files, "
        "       SUM(commits) commits, COUNT(DISTINCT repo) repos, MAX(last) last "
        "FROM ownership GROUP BY author ORDER BY commits DESC")]


def owners_for(store, repo, path, limit=5):
    return [dict(r) for r in store.conn.execute(
        "SELECT author, commits, added, last FROM ownership "
        "WHERE repo=? AND path=? ORDER BY commits DESC LIMIT ?",
        (repo, path, limit))]


def stale(store, limit=20, months=9):
    """Code with no recent commits: candidates for dead code, to be verified."""
    import datetime
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(days=30 * months)).strftime("%Y-%m-%d")
    return [dict(r) for r in store.conn.execute(
        "SELECT repo, path, revisions, last_change, main_author FROM file_metrics "
        "WHERE last_change IS NOT NULL AND last_change < ? "
        "ORDER BY last_change ASC LIMIT ?", (cutoff, limit))]
