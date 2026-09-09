"""
GRAPH_REPORT.md -- the human entry point.

Ends with suggested questions (an idea worth borrowing from Graphify): the
report should not only describe the estate, it should tell you what to ask
next. For someone four weeks into a job, knowing which question to ask is the
scarce skill.
"""
from __future__ import annotations

from .. import ack

import json
import re
import time

from . import mermaid
from ..analyze import hotspots


def build(store, cfg, stats=None):
    c = store.counts()
    L = []
    L.append("# Codebase map")
    L.append("")
    L.append("Generated %s by cartographer." % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    L.append("")
    L.append("> Every claim below carries `file:line` evidence. Edges marked "
             "`INFERRED` are name-matched leads, not facts -- verify before "
             "relying on them.")
    L.append("")

    # ---- summary ----
    L.append("## What was scanned")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append("| Repos | %s |" % (", ".join(c["repos"]) or "none"))
    L.append("| Services | %d |" % len([s for s in c["services"] if s]))
    L.append("| Files parsed | %d |" % c["nodes_by_kind"].get("file", 0))
    L.append("| Symbols | %d |" % (c["nodes_by_kind"].get("function", 0)
                                   + c["nodes_by_kind"].get("class", 0)))
    L.append("| Edges | %d (%s) |" % (c["edges"], ", ".join(
        "%s %d" % (k, v) for k, v in sorted(c["edges_by_provenance"].items()))))
    L.append("| Languages | %s |" % ", ".join(
        "%s (%d)" % (k, v) for k, v in list(c["languages"].items())[:8]))
    L.append("")

    # ---- topology ----
    L.append("## Service topology")
    L.append("")
    L.append(mermaid.service_graph(store))
    L.append("")

    svc_rows = list(store.conn.execute(
        "SELECT n.name, "
        "  (SELECT COUNT(*) FROM edges e WHERE e.dst=n.id AND e.kind IN ('http','event')) inbound, "
        "  (SELECT COUNT(*) FROM edges e WHERE e.src=n.id AND e.kind IN ('http','event')) outbound, "
        "  (SELECT COUNT(*) FROM nodes f WHERE f.service=n.name AND f.kind='file') files, "
        "  COALESCE((SELECT rank FROM ranks r WHERE r.id=n.id),0) rank "
        "FROM nodes n WHERE n.kind='service' ORDER BY rank DESC"))
    if svc_rows:
        L.append("| Service | Files | Depends on | Depended on by | Centrality |")
        L.append("|---|---:|---:|---:|---:|")
        for r in svc_rows:
            L.append("| `%s` | %d | %d | %d | %.4f |" %
                     (r["name"], r["files"], r["outbound"], r["inbound"], r["rank"]))
        L.append("")

    # ---- most central code ----
    # Centrality only means something for a symbol something else depends on.
    # Without this filter, isolated nodes float up on the PageRank baseline and
    # the table fills with code nobody references.
    top_ranked = list(store.conn.execute(
        "SELECT n.name, n.kind, n.container, n.file, n.line, n.service, r.rank, "
        "  (SELECT COUNT(*) FROM edges e WHERE e.dst=n.id "
        "     AND e.kind IN ('calls','imports','extends')) inbound "
        "FROM ranks r JOIN nodes n ON n.id=r.id "
        "WHERE n.kind IN ('class','function') AND inbound > 0 "
        "ORDER BY r.rank DESC LIMIT 20"))
    if top_ranked:
        L.append("## Most central code")
        L.append("")
        L.append("Ranked by PageRank over the dependency graph -- what the rest "
                 "of the estate leans on most.")
        L.append("")
        L.append("| Symbol | Service | Dependents | Location |")
        L.append("|---|---|---:|---|")
        for r in top_ranked:
            owner = (r["container"] + ".") if r["container"] else ""
            L.append("| `%s%s` | %s | %d | `%s:%s` |" %
                     (owner, r["name"], r["service"] or "-", r["inbound"],
                      r["file"] or "-", r["line"] or "-"))
        L.append("")

    # ---- hotspots ----
    hs = hotspots.top(store, 15)
    if hs:
        L.append("## Hotspots (churn x complexity)")
        L.append("")
        L.append("Where defects concentrate: complicated code that changes "
                 "constantly. Neither number alone means much.")
        L.append("")
        L.append("| File | Revisions | Complexity | Authors | Main author |")
        L.append("|---|---:|---:|---:|---|")
        for h in hs:
            L.append("| `%s/%s` | %d | %d | %d | %s |" %
                     (h["repo"], h["path"], h["revisions"], int(h["complexity"]),
                      h["authors"], h["main_author"] or "-"))
        L.append("")

    # ---- coupling ----
    cross = list(store.conn.execute(
        "SELECT * FROM coupling WHERE cross_repo=1 ORDER BY degree DESC LIMIT 12"))
    within = list(store.conn.execute(
        "SELECT * FROM coupling WHERE cross_repo=0 ORDER BY degree DESC LIMIT 12"))
    if cross or within:
        L.append("## Files that change together")
        L.append("")
        L.append("From version-control history, not from the code. A coupling "
                 "here is real even when no import connects the files -- this "
                 "is the dependency static analysis cannot see.")
        L.append("")
        if cross:
            L.append("**Across repositories** (strongest signal in a "
                     "microservice estate):")
            L.append("")
            L.append("| A | B | Shared commits | Degree |")
            L.append("|---|---|---:|---:|")
            for r in cross:
                L.append("| `%s` | `%s` | %d | %.2f |" %
                         (r["a"], r["b"], r["shared"], r["degree"]))
            L.append("")
        if within:
            L.append("**Within a repository:**")
            L.append("")
            L.append("| A | B | Shared commits | Degree |")
            L.append("|---|---|---:|---:|")
            for r in within:
                L.append("| `%s` | `%s` | %d | %.2f |" %
                         (r["a"], r["b"], r["shared"], r["degree"]))
            L.append("")

    # ---- ownership ----
    km = hotspots.knowledge_map(store)
    if km:
        L.append("## Who knows what")
        L.append("")
        L.append("| Person | Files touched | Commits | Repos |")
        L.append("|---|---:|---:|---:|")
        for k in km[:15]:
            L.append("| %s | %d | %d | %d |" %
                     (k["author"], k["files"], k["commits"], k["repos"]))
        L.append("")
        bf = hotspots.bus_factor(store, 10)
        if bf:
            L.append("**Bus factor 1** -- one person holds most of the history. "
                     "Not automatically a problem, but this is the knowledge "
                     "that leaves when they do.")
            L.append("")
            for b in bf:
                L.append("- `%s/%s` -- %s (%.0f%% of %d commits)" %
                         (b["repo"], b["path"], b["main_author"],
                          b["author_share"] * 100, b["revisions"]))
            L.append("")

    # ---- modules ----
    mods = list(store.conn.execute(
        "SELECT name, kind, extra FROM nodes WHERE extra LIKE '%artifactId%' "
        "ORDER BY kind, name"))
    if mods:
        outside = []
        L.append("## Module inventory")
        L.append("")
        L.append("| Module | Kind | Notes |")
        L.append("|---|---|---|")
        for m in mods:
            try:
                ex = json.loads(m["extra"])
            except (ValueError, TypeError):
                continue
            tags = []
            if ex.get("in_reactor") is False:
                tags.append("**outside the reactor**")
                outside.append(m["name"])
            if ex.get("war"):
                tags.append("WAR")
            if ex.get("spring_boot_app"):
                tags.append("Boot app")
            if ex.get("has_controllers"):
                tags.append("controllers")
            L.append("| `%s` | %s | %s |" % (m["name"], m["kind"], ", ".join(tags)))
        L.append("")
        if outside:
            L.append("> **%d module(s) build and deploy outside the parent "
                     "reactor: %s.** They are real services. A build or module "
                     "scan driven by the parent POM alone will miss them."
                     % (len(outside), ", ".join("`%s`" % o for o in outside)))
            L.append("")

    # ---- schema ----
    tables = list(store.conn.execute(
        "SELECT name, service, extra FROM nodes WHERE kind='table' "
        "ORDER BY service, name"))
    shared_tables = list(store.conn.execute(
        "SELECT detail, hint FROM gaps WHERE category='shared-table' ORDER BY detail"))
    if tables:
        L.append("## Data ownership")
        L.append("")
        L.append("%d tables, read from migration SQL rather than from ORM "
                 "annotations." % len(tables))
        L.append("")
        if shared_tables:
            L.append("### Tables touched by more than one service")
            L.append("")
            L.append("The coupling no API contract documents. Two services on "
                     "one table are bound together whatever the interfaces say.")
            L.append("")
            for r in shared_tables:
                L.append("- %s" % r["detail"])
                L.append("  - *%s*" % r["hint"])
            L.append("")
        by_svc = {}
        for t_ in tables:
            by_svc.setdefault(t_["service"] or "(not declared locally)", []).append(t_["name"])
        L.append("### Tables by declaring service")
        L.append("")
        for svc, names in sorted(by_svc.items()):
            L.append("- **%s** (%d): %s" % (svc, len(names),
                                            ", ".join("`%s`" % n for n in names[:25])))
        L.append("")

    # ---- credentials ----
    secret_rows = list(store.conn.execute(
        "SELECT detail FROM gaps WHERE category='credential-in-source' ORDER BY detail"))
    if secret_rows:
        L.append("## Possible credentials in source")
        L.append("")
        L.append("**The values were never read into the graph** — only these "
                 "locations. Open each line yourself. If real: rotate it, move "
                 "it to a secrets manager, and never paste the value into a "
                 "prompt, a ticket, or a chat.")
        L.append("")
        for r in secret_rows:
            L.append("- %s" % r["detail"])
        L.append("")

    # ---- contracts ----
    routes = list(store.conn.execute(
        "SELECT name, service, extra FROM nodes WHERE kind='route' "
        "ORDER BY service, name LIMIT 80"))
    topics = list(store.conn.execute(
        "SELECT name, extra FROM nodes WHERE kind='topic' ORDER BY name"))
    if routes or topics:
        L.append("## Contract surface")
        L.append("")
        if routes:
            L.append("### HTTP / RPC endpoints")
            L.append("")
            cur = None
            for r in routes:
                if r["service"] != cur:
                    cur = r["service"]
                    L.append("")
                    L.append("**%s**" % (cur or "unknown"))
                auth = " `[spec]`" if r["extra"] and "authoritative" in r["extra"] else ""
                L.append("- `%s`%s" % (r["name"], auth))
            L.append("")
        if topics:
            L.append("### Events / topics")
            L.append("")
            for t in topics:
                prod = [x["src"].split(" ", 3)[-1] for x in store.conn.execute(
                    "SELECT src FROM edges WHERE dst=(SELECT id FROM nodes "
                    "WHERE kind='topic' AND name=?) AND kind='produces'", (t["name"],))]
                cons = [x["dst"].split(" ", 3)[-1] for x in store.conn.execute(
                    "SELECT dst FROM edges WHERE src=(SELECT id FROM nodes "
                    "WHERE kind='topic' AND name=?) AND kind='consumed-by'", (t["name"],))]
                L.append("- `%s` — produced by %s → consumed by %s" %
                         (t["name"], ", ".join(prod) or "**nobody visible**",
                          ", ".join(cons) or "**nobody visible**"))
            L.append("")

    # ---- gaps ----
    gaps = list(store.conn.execute(
        "SELECT category, detail, hint FROM gaps ORDER BY category LIMIT 60"))
    if gaps:
        L.append("## Gaps and open questions")
        L.append("")
        L.append("What the scan could **not** determine. These are the most "
                 "valuable things to ask a teammate, because they are exactly "
                 "the knowledge that is not written down anywhere.")
        L.append("")
        by_cat = {}
        for g in gaps:
            by_cat.setdefault(g["category"], []).append(g)
        for cat, items in sorted(by_cat.items()):
            L.append("### %s" % cat.replace("-", " "))
            for g in items[:12]:
                L.append("- %s" % g["detail"])
                if g["hint"]:
                    L.append("  - *%s*" % g["hint"])
            L.append("")

    # ---- suggested questions ----
    L.append("## Questions worth asking")
    L.append("")
    for q in suggest_questions(store):
        L.append("- %s" % q)
    L.append("")
    return "\n".join(L)


def _gaps(store, cats, acks, limit):
    """
    Gap details for one or more categories, minus anything acknowledged.

    Filtering happens here rather than in SQL because an acknowledgement is
    keyed on the finding's identity (category + location, line number
    stripped), which SQL cannot compute. The limit is applied after filtering,
    so acknowledging the top finding promotes the next one into view instead of
    leaving a shorter list.
    """
    if isinstance(cats, str):
        cats = [cats]
    marks = ",".join("?" * len(cats))
    rows = store.conn.execute(
        "SELECT category, detail FROM gaps WHERE category IN (%s) "
        "ORDER BY id" % marks, tuple(cats))
    out = []
    for r in rows:
        if acks and ack.key_for(r["category"], r["detail"]) in acks:
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def suggest_questions(store, acks=None):
    """
    Turn findings into questions. Specific questions make a new hire look
    sharp; vague ones make them look lost.

    `acks` hides findings already recorded in `.cartographer-ack.yaml`. Counts
    are left unfiltered on purpose: "108 credential-shaped strings" is a fact
    about the codebase, and quietly shrinking it because some were reviewed
    would misstate the scale.
    """
    qs = []
    # Ordered by how much a good answer is worth, not by category name.
    for r in _gaps(store, "shared-table", acks, 4):
        qs.append("%s — who is allowed to WRITE that table, and is the other "
                  "service's access deliberate or historical?" % r["detail"])
    for r in _gaps(store, ['module-outside-reactor', 'module-standalone'], acks, 3):
        qs.append("%s — how is it actually built and deployed, and does CI "
                  "cover it?" % r["detail"])
    for r in store.conn.execute(
            "SELECT COUNT(*) n FROM gaps WHERE category='credential-in-source'"):
        if r["n"]:
            qs.append("%d credential-shaped strings are hardcoded in source "
                      "(see `cartographer secrets` for locations) — is there a "
                      "secrets manager I should be moving these to, and who "
                      "owns rotating them?" % r["n"])
    for r in _gaps(store, "shared-schema", acks, 3):
        qs.append("%s — is that intentional shared ownership, or a "
                  "copy-paste in the compose file?" % r["detail"])
    for r in store.conn.execute(
            "SELECT COUNT(*) n FROM gaps WHERE category='table-not-declared-locally'"):
        if r["n"]:
            qs.append("%d tables are queried but have no CREATE statement in "
                      "any repo I can see — where does that DDL live?" % r["n"])
    for r in _gaps(store, "proxy-prefix-unmapped", acks, 3):
        qs.append("%s — what service is behind that prefix, and do I have "
                  "access to its repo?" % r["detail"])
    for r in _gaps(store, "topic-constant-unreferenced", acks, 2):
        qs.append("%s — is that event consumed by a service outside my "
                  "workspace, or is the constant dead?" % r["detail"])
    for r in _gaps(store, "shared-library-version-drift", acks, 2):
        qs.append("%s — is that drift deliberate, and does a fix in the library "
                  "need porting to both?" % r["detail"])
    for r in _gaps(store, "jndi-datasource", acks, 2):
        qs.append("%s — what host and schema does that JNDI name actually "
                  "resolve to in each environment?" % r["detail"])
    for r in _gaps(store, "shared-datastore", acks, 3):
        qs.append("%s — which service is the source of truth for that schema, "
                  "and is the other one allowed to write to it?" % r["detail"])
    for r in _gaps(store, "unconsumed-topic", acks, 3):
        qs.append("%s — is that consumer in a repo I don't have access to, or "
                  "is the event genuinely unused?" % r["detail"])
    for r in _gaps(store, "trace-service-unmapped", acks, 3):
        qs.append("%s — where does that service live and who owns it?" % r["detail"])
    for r in store.conn.execute(
            "SELECT a, b, shared FROM coupling WHERE cross_repo=1 "
            "ORDER BY degree DESC LIMIT 2"):
        qs.append("`%s` and `%s` changed together in %d commits across repos — "
                  "is that an intentional shared contract, and is there a way to "
                  "make the coupling explicit?" % (r["a"], r["b"], r["shared"]))
    for r in store.conn.execute(
            "SELECT repo, path, main_author, revisions FROM file_metrics "
            "WHERE author_share >= 0.9 AND revisions >= 5 "
            "ORDER BY hotspot DESC LIMIT 2"):
        qs.append("`%s/%s` has %d commits almost entirely from %s — worth asking "
                  "them to walk me through it before I touch it."
                  % (r["repo"], r["path"], r["revisions"], r["main_author"]))
    svc_no_edges = list(store.conn.execute(
        "SELECT n.name FROM nodes n WHERE n.kind='service' AND NOT EXISTS "
        "(SELECT 1 FROM edges e WHERE (e.src=n.id OR e.dst=n.id) "
        " AND e.kind IN ('http','event')) LIMIT 3"))
    for r in svc_no_edges:
        qs.append("`%s` shows no inbound or outbound service calls — is it "
                  "genuinely standalone, or is it reached in a way this scan "
                  "cannot see (service mesh, gateway, batch job)?" % r["name"])
    if not qs:
        qs.append("Nothing obviously unexplained — try `cartographer trace "
                  "\"buyer submits an order\"` and see whether the story holds "
                  "together end to end.")
    # Two categories can describe the same underlying fact (a shared schema is
    # reported by both the compose reader and the topology scan). Deduplicate
    # on the leading clause so the list stays worth reading.
    seen, uniq = set(), []
    for q in qs:
        key = re.sub(r"[^a-z0-9]", "", q.split("—")[0].lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(q)
    return uniq[:16]
