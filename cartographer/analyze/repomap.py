"""
Token-budgeted repository map (aider's ranked-tags idea).

Given a task, produce the most useful possible summary of the estate that fits
in N tokens. Rank everything by personalised PageRank, group by file, then
binary-search the cut-off so the rendered map lands just under budget.

This is what makes a 15-service estate tractable: instead of an agent grepping
blindly, it starts from a ranked map of the places that actually matter for the
task at hand.
"""
from __future__ import annotations

from collections import defaultdict

from . import pagerank

# Rough but stable. Real tokenisers vary by a few percent; we target ~90% of
# budget so the small error never overruns.
CHARS_PER_TOKEN = 3.6


def estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _gather(store, scores, max_symbols_per_file=12):
    by_file = defaultdict(lambda: {"score": 0.0, "symbols": [], "meta": None})
    services = []
    for nid, score in scores.items():
        n = store.node(nid)
        if not n:
            continue
        kind = n["kind"]
        if kind in ("library",):
            continue
        if kind == "service":
            services.append((score, n))
            continue
        if kind in ("file",):
            key = (n["repo"], n["file"])
            by_file[key]["score"] += score * 0.5
            by_file[key]["meta"] = n
            continue
        if kind in ("class", "function", "interface", "struct", "trait", "enum"):
            if not n["file"]:
                continue
            key = (n["repo"], n["file"])
            by_file[key]["score"] += score
            by_file[key]["symbols"].append((score, n))
            if by_file[key]["meta"] is None:
                by_file[key]["meta"] = n
    for v in by_file.values():
        v["symbols"].sort(key=lambda t: -t[0])
        v["symbols"] = v["symbols"][:max_symbols_per_file]
    ranked = sorted(by_file.items(), key=lambda kv: -kv[1]["score"])
    services.sort(key=lambda t: -t[0])
    return ranked, services


def _render(ranked, services, n_files, show_lines=True, header=None):
    out = []
    if header:
        out.append(header)
    if services:
        out.append("## Services by centrality")
        for score, n in services[:15]:
            out.append("  %-24s %s" % (n["name"], _fmt_rank(score)))
        out.append("")
    out.append("## Ranked files (%d shown)" % min(n_files, len(ranked)))
    for (repo, path), v in ranked[:n_files]:
        meta = v["meta"] or {}
        svc = meta.get("service") or repo or ""
        out.append("")
        out.append("%s/%s   [%s]" % (repo or "?", path, svc))
        for score, s in v["symbols"]:
            owner = (s["container"] + ".") if s["container"] else ""
            loc = ":%s" % s["line"] if (show_lines and s["line"]) else ""
            out.append("    %-9s %s%s%s" % (s["kind"], owner, s["name"], loc))
    return "\n".join(out)


def _fmt_rank(x):
    return "%.4f" % x


def build(store, task=None, seeds=None, budget_tokens=2000,
          max_symbols_per_file=12):
    """
    Returns {"text", "tokens", "files", "seeded"}.

    `task` is free text; seeds are extracted from it. Falls back to the global
    ranking when nothing matches, which is still far better than nothing.
    """
    terms = []
    if seeds:
        terms.extend(seeds)
    if task:
        terms.extend(_terms_from_task(task))

    personalization = pagerank.seed_from_terms(store, terms) if terms else None
    seeded = bool(personalization)
    adjacency, nodes = pagerank.build_adjacency(store)
    if not nodes:
        return {"text": "(graph is empty -- run a scan first)", "tokens": 0,
                "files": 0, "seeded": False}
    scores = pagerank.pagerank(adjacency, nodes, personalization)
    ranked, services = _gather(store, scores, max_symbols_per_file)
    if not ranked:
        return {"text": "(no files in graph)", "tokens": 0, "files": 0,
                "seeded": seeded}

    header = None
    if task:
        header = "# Repo map for: %s\n" % task
    elif seeded:
        header = "# Repo map (focused)\n"
    else:
        header = "# Repo map (global ranking)\n"

    # Binary-search the file count that fits the budget.
    lo, hi, best = 1, len(ranked), None
    while lo <= hi:
        mid = (lo + hi) // 2
        text = _render(ranked, services, mid, header=header)
        tok = estimate_tokens(text)
        if tok <= budget_tokens:
            best = (text, tok, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        text = _render(ranked, services, 1, header=header)
        best = (text, estimate_tokens(text), 1)
    return {"text": best[0], "tokens": best[1], "files": best[2],
            "seeded": seeded, "total_files": len(ranked)}


_STOP = frozenset("""
the a an and or of to in for on with how what where when does do is are be
i we you it this that these those from by at as if then than so but not
can could should would please show me tell about work works working code
add fix change update implement why which who whom
""".split())


def _terms_from_task(task):
    import re
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task or "")
    out = []
    for w in words:
        if w.lower() in _STOP:
            continue
        out.append(w)
        # split camelCase so "OrderService" also seeds "Order" and "Service"
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", w)
        if len(parts) > 1:
            out.extend(p for p in parts if len(p) > 2 and p.lower() not in _STOP)
    seen, uniq = set(), []
    for w in out:
        if w.lower() not in seen:
            seen.add(w.lower())
            uniq.append(w)
    return uniq[:20]
