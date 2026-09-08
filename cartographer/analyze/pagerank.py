"""
Personalised PageRank over the code graph (the aider repo-map idea).

The problem it solves: your estate is far larger than any context window, so
something has to decide what an agent reads first. Ranking by graph centrality
answers that far better than file names or grep order -- a function that many
things depend on matters more than one nothing references.

"Personalised" means the random-surfer restart vector is biased toward nodes
you are actually working on, so the ranking is task-specific rather than a
fixed popularity list.

Pure-Python power iteration. No numpy, no scipy: a 15-service estate produces
tens of thousands of nodes, which converges in well under a second.
"""
from __future__ import annotations

from collections import defaultdict

# Edge kinds carry very different amounts of signal about importance.
KIND_WEIGHT = {
    "calls": 3.0,        # strongest: someone actually invokes this
    "imports": 1.0,
    "extends": 2.0,
    "defines": 0.5,      # structural, not a dependency
    "http": 4.0,         # cross-service: expensive to break
    "event": 3.5,
    "depends-on": 2.0,
    "produces": 2.0,
    "consumed-by": 2.0,
    "exposes": 1.5,
    "uses-datastore": 1.5,
    "uses-library": 0.2,  # everything uses libraries; near-zero signal
    "couples-with": 2.5,  # from history: co-change is real coupling
    # Data access is the strongest coupling signal in an estate with no JPA:
    # two services on one table are bound together whatever the API says.
    "writes-table": 3.0,
    "reads-table": 1.5,
    "defines-table": 1.0,
    "routes-to": 0.5,     # a host, not a dependency
    "links-to": 0.5,      # legacy page reference
    "uses-cache": 0.5,
}
DEFAULT_KIND_WEIGHT = 1.0

DAMPING = 0.85
MAX_ITER = 100
TOLERANCE = 1e-8


def build_adjacency(store, include_kinds=None, exclude_kinds=("uses-library",)):
    """
    Reverse the direction deliberately: rank flows toward things that are
    depended upon. If A calls B, importance should accrue to B.
    """
    out = defaultdict(list)
    nodes = set()
    q = "SELECT src, dst, kind, weight, confidence FROM edges"
    for r in store.conn.execute(q):
        kind = r["kind"]
        if include_kinds and kind not in include_kinds:
            continue
        if exclude_kinds and kind in exclude_kinds:
            continue
        w = KIND_WEIGHT.get(kind, DEFAULT_KIND_WEIGHT)
        w *= max(0.1, float(r["confidence"] or 0.5))
        w *= min(4.0, float(r["weight"] or 1.0))
        out[r["src"]].append((r["dst"], w))
        nodes.add(r["src"])
        nodes.add(r["dst"])
    return out, nodes


def pagerank(adjacency, nodes, personalization=None, damping=DAMPING,
             max_iter=MAX_ITER, tol=TOLERANCE):
    """
    Standard PageRank with a personalisation vector.

    Dangling nodes (no outgoing edges) redistribute their mass according to the
    personalisation vector rather than uniformly, which keeps a task-focused
    ranking from being diluted by every leaf in the graph.
    """
    n = len(nodes)
    if n == 0:
        return {}
    node_list = sorted(nodes)

    if personalization:
        total = float(sum(max(0.0, v) for v in personalization.values()))
        if total <= 0:
            personalization = None
    if personalization:
        p = {k: max(0.0, personalization.get(k, 0.0)) / total for k in node_list}
        # A small uniform floor keeps the walk from getting trapped in an
        # isolated component around the seeds.
        floor = 0.15 / n
        s = sum(p.values()) + floor * n
        p = {k: (p[k] + floor) / s for k in node_list}
    else:
        p = {k: 1.0 / n for k in node_list}

    rank = dict(p)
    # Pre-normalise outgoing weights once.
    norm = {}
    for src, targets in adjacency.items():
        tot = sum(w for _, w in targets)
        if tot > 0:
            norm[src] = [(d, w / tot) for d, w in targets]

    for _ in range(max_iter):
        nxt = {k: 0.0 for k in node_list}
        dangling = 0.0
        for node in node_list:
            r = rank[node]
            targets = norm.get(node)
            if not targets:
                dangling += r
                continue
            for dst, w in targets:
                nxt[dst] += r * w
        for k in node_list:
            nxt[k] = (1.0 - damping) * p[k] + damping * (nxt[k] + dangling * p[k])
        delta = sum(abs(nxt[k] - rank[k]) for k in node_list)
        rank = nxt
        if delta < tol:
            break
    return rank


def seed_from_terms(store, terms, boost=10.0):
    """
    Turn free text ("order submission", "PricingClient", a file path) into a
    personalisation vector.

    Matching is deliberately generous -- a seed that is slightly wrong still
    lands you in the right neighbourhood, whereas no seed at all gives you the
    generic popularity ranking.
    """
    vec = defaultdict(float)
    for term in terms:
        term = (term or "").strip()
        if not term:
            continue
        like = "%" + term + "%"
        for r in store.conn.execute(
                "SELECT id, name, file, kind FROM nodes "
                "WHERE name = ? COLLATE NOCASE", (term,)):
            vec[r["id"]] += boost * 3
        for r in store.conn.execute(
                "SELECT id FROM nodes WHERE (name LIKE ? COLLATE NOCASE "
                "   OR file LIKE ? COLLATE NOCASE OR container LIKE ? COLLATE NOCASE) "
                "LIMIT 400", (like, like, like)):
            vec[r["id"]] += boost
    return dict(vec)


def compute(store, seeds=None, kind="global"):
    """Compute and persist ranks. Returns the score dict."""
    adjacency, nodes = build_adjacency(store)
    if not nodes:
        return {}
    personalization = None
    if seeds:
        personalization = seeds if isinstance(seeds, dict) \
            else seed_from_terms(store, seeds)
        if not personalization:
            personalization = None
    scores = pagerank(adjacency, nodes, personalization)
    store.set_ranks(scores, kind=kind)
    store.commit()
    return scores


def top(store, scores=None, limit=40, kinds=None, exclude_kinds=("library",)):
    """Highest-ranked nodes, joined back to their metadata."""
    if scores is None:
        rows = store.conn.execute(
            "SELECT r.id, r.rank FROM ranks r ORDER BY r.rank DESC LIMIT ?",
            (limit * 8,)).fetchall()
        scores = {r["id"]: r["rank"] for r in rows}
    out = []
    for nid, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        n = store.node(nid)
        if not n:
            continue
        if kinds and n["kind"] not in kinds:
            continue
        if exclude_kinds and n["kind"] in exclude_kinds:
            continue
        n = dict(n)
        n["rank"] = score
        out.append(n)
        if len(out) >= limit:
            break
    return out
