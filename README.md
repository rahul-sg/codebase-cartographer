# cartographer

Maps a large multi-repo, multi-service codebase you did not write — then hands
that map to Claude and to you, in a browser.

Built for the situation where a system is too large to hold in your head: many
services, several repositories, and no single person who knows how it all
connects. It derives the cross-service map that usually exists nowhere, and
keeps it honest about what it could not see.

**No dependencies.** Python 3.8+ standard library only — no `pip install`,
nothing to get approved. **Fully local**: no network calls in any code path.
Nothing about your code leaves the machine.

```bash
./install.sh                 # verify it runs here (~10s)
cartographer init --root .   # discover repos, modules, containers, proxies
cartographer scan            # build the graph
cartographer ui              # explore it
```

---

## Five layers, joined

The central problem is that no single technique sees a whole system.

**No AST crosses a process boundary.** A perfect symbol graph of `order-svc`
will never show that it calls `pricing-svc`, because that edge exists only as
the string `PRICING_SERVICE_URL` in a YAML file. Conversely a service map knows
nothing about functions, and neither knows which two services quietly write to
the same table.

| Layer | What it maps | Derived from |
|---|---|---|
| **Symbol** | classes, functions, `calls` / `imports` / `extends` | source, 9 languages |
| **Module** | services vs shared libraries, build deps, reactor exclusions | Maven / Gradle / npm / go manifests |
| **Service** | who calls whom, events, routes, hosts | config, SPA proxy configs, specs, **runtime traces** |
| **Data** | tables, who declares them, who reads and writes them | migration SQL + SQL inside DAOs |
| **Behavioural** | hotspots, change coupling, ownership, drift | git history |

Every symbol carries its service, so a change to a method can be followed out
through its repository and across a service boundary to the consumers. That
join is the point — the individual techniques are all borrowed.

---

## What it is actually good at

**Finding coupling nobody documented.** Two services writing one table. A
`CREATE TABLE` in one repo and the only writer in another. Files in different
repositories that change together in the same commits — which is a real
dependency that no import graph contains.

**Answering "what breaks if I change this?"** across layers, with the exact
`file:line` behind every claim, and reviewers drawn from who actually touched
the code rather than from an org chart.

**Being honest.** Every edge is `EXTRACTED` (read from source, config, a spec
or a trace) or `INFERRED` (name-matched — a lead, not a fact), and the
distinction is visible everywhere including the UI. A dedicated **Coverage**
view lists what the scan could *not* establish. That is a feature: a map that
hides its own gaps is worse than one that admits them, because you will
eventually repeat it in front of someone.

**Working where good tools usually cannot go.** Zero dependencies and no
server, because in regulated environments the reason tooling does not get
adopted is procurement, not capability.

---

## The visual map

```bash
cartographer ui        # http://127.0.0.1:8787, loopback only
```

Eight views over one graph. No CDN, no framework, no WebGL library — the force
simulation, the 3D projection and the diagram layout are all hand-rolled,
because an outbound request to a CDN simply fails on a locked-down machine. A
test asserts no asset references an external host.

| View | The question it answers |
|---|---|
| **Map** | How does everything connect? Drill estate → service → package → file. Click a node for detail; click an **edge** for the `file:line` citations behind it. |
| **Layers** | What shape is this system? A 3D stack — clients, services, messaging, data, libraries — with per-floor isolation and camera stepping. |
| **Arch** | What is the runtime architecture? The diagram teams draw by hand and let go stale, derived instead. Filters out the ~82% of inter-service edges that are compile-time structure. |
| **Data** | Who owns which table, and which are touched by more than one service? |
| **Flow** | What happens end to end when an order is submitted? |
| **Impact** | What breaks if I change this? |
| **Time** | How did this estate get this way? Monthly activity, with a scrubber. |
| **Coverage** | What can this map *not* see? |

Re-run `scan` in another terminal and the open page reloads itself.

### Why it is hierarchical

A real estate produces on the order of 145,000 nodes. Drawn at once that is a
grey hairball — not slow, **illegible**, and nobody learns anything from it. So
the graph collapses to whichever level you are looking at and rolls edges up
with counts. Measured on a 39,796-node graph: the estate view renders **26
readable nodes with 39,770 rolled up inside them, in 0.39s**. Nothing is
discarded — a service-to-service arrow expands into the individual calls that
justify it.

---

## Install

```bash
unzip codebase-cartographer.zip -d ~/tools     # or: git clone
cd ~/tools/codebase-cartographer
./install.sh                                    # verify (~10s)
```

`./install.sh --link` adds it to PATH. `./install.sh --plugin` installs it as
an always-on Claude Code plugin. For one session, installing nothing:

```bash
claude --plugin-dir ~/tools/codebase-cartographer
```

Plugin skills are namespaced, so any skills you already have keep working
unchanged; these arrive as `/cartographer:map` and so on.

**Setting this up with Claude?** Point it at
[`SETUP-FOR-CLAUDE.md`](SETUP-FOR-CLAUDE.md) — an ordered runbook with a check
after every step. `/cartographer:setup` does the same once the plugin is loaded.

---

## CLI

| Command | Purpose |
|---|---|
| `init --root <dir>` | Discover repos, modules, containers and proxy prefixes; write a pre-filled config |
| `scan [--trace <file>]` | Build or refresh the graph |
| `ui` | Interactive visual map |
| `map "<task>"` | Ranked file map for a task, within a token budget |
| `impact <target>` | Blast radius across every layer |
| `pr-check` | Architectural context for a change set: endpoints touched, frontend callers, multi-writer tables, reviewers |
| `audit` | What the extractors **missed** — raw source compared against what was extracted |
| `schema [table]` | Tables, owners, cross-service access |
| `topology [service]` | Cross-service map (`--mermaid` for a diagram) |
| `contracts [service]` | Routes and events a service exposes |
| `coupling [--cross-repo]` | Files that change together, from git |
| `hotspots [--bus-factor]` | Churn × complexity; single-author risk |
| `owns <path>` · `path <a> <b>` · `find <query>` | Ownership · connection · lookup |
| `diff` / `snapshots` | Architectural drift between scans |
| `gaps` / `ack` | Browse findings; accept one with a mandatory reason so it stops resurfacing |
| `secrets` | Credential **locations** — values are never recorded |
| `questions` | What the scan could not determine |
| `report` · `stats` · `doctor` · `serve-mcp` | Reports · graph contents · environment check · MCP server |

`audit` deserves a note: the two worst bugs found in this tool were silent
*recall* failures — extractors quietly missing things while reporting success.
A tool that maps a codebase must be able to check its own coverage.

## MCP tools

14, exposed to Claude over stdio:

`repo_map` · `find_symbol` · `blast_radius` · `service_topology` · `who_owns` ·
`hotspots` · `coupled_files` · `shortest_path` · `contracts` · `schema_map` ·
`module_inventory` · `secret_locations` · `open_questions` · `graph_stats`

## Skills

`/cartographer:` `setup` · `onboard` · `map` · `impact` · `pr-context` ·
`trace-flow` · `service-brief` · `ai-surface` · `journal`

---

## Conventions it handles that trip up generic tools

Each of these was a case where the naive answer was not merely incomplete but
**wrong**, which is worse:

- **Topic names as enum constants**, resolved at runtime rather than written as
  literals. A literal-scanning regex finds none of them, and the topic list in
  a local compose file is usually stale.
- **Modules excluded from the parent build reactor** that still ship. A
  parent-POM module scan misses them; they are real, deployed services.
- **No ORM.** Schema read from migration SQL — legacy patch-lists *and* Flyway —
  and data access from SQL strings inside hand-written DAOs, with
  `+`-concatenated literals merged first, because that is how DAO SQL is
  actually written.
- **SPA proxy configs** as the frontend's real dependency declaration, since a
  single-page app calls path prefixes rather than naming services.
- **Routes split across class-level and method-level annotations**, composed
  into full paths, including when the prefix comes from a constant.
- **One schema shared by two services**, which reading either module alone
  cannot reveal.
- **Credentials in source**: flagged by location, never by value. A test reads
  the database as raw bytes and asserts the values are absent.

---

## How this compares

Honest positioning, because overclaiming is the fastest way to lose an
argument with someone who knows the field.

**Almost every technique here is borrowed and cited.** Personalised-PageRank
repo maps are aider's. Churn × complexity, change coupling and knowledge maps
are Adam Tornhill's, productised in CodeScene. Service graphs from traces are
standard APM. `EXTRACTED`/`INFERRED` provenance came from Graphify. Stable
cross-repo symbol identifiers are SCIP's idea.

**It is not state of the art at any single layer.** The symbol extraction is
regex-based, roughly 70% recall with name-matched call edges. Sourcegraph's
SCIP indexers, Joern's code property graphs, and anything LSP-backed are far
more accurate. If the question is "what is the best code intelligence
available", the answer is not this.

**Where it is genuinely differentiated** is the combination, and three things
in particular:

1. **The multi-layer join.** Most tools own one layer well. Sourcegraph does
   symbols and knows nothing about services. Backstage does service catalogs
   but needs humans to write the metadata. APM knows runtime topology and
   nothing about code. CodeScene does behaviour and not structure. Joining
   them is integration work rather than research, but nobody else ships it.
2. **Data ownership derived from raw SQL.** Every tool that maps data assumes
   an ORM with annotations to read. A very large amount of enterprise code is
   hand-written JDBC with SQL in string literals, and for those, nothing tells
   you which services touch which tables.
3. **Uncertainty as a first-class feature.** Provenance on every edge, a
   Coverage view, and phrasing like "no consumer *among the repos I can see*".
   Most tools present a confident picture and let you find the holes by being
   wrong in front of a colleague.

---

## Known limits

- The regex symbol backend is ~70% recall versus a real parser, and `calls`
  edges are name-matched rather than scope-resolved — hence `INFERRED`.
- One definition per line; `class X { void y() {} }` yields only `X`.
- SQL assembled through a query-builder abstraction rather than as literals is
  under-reported.
- No incremental scanning across runs beyond the parse cache; a full rescan is
  the unit of work.
- Dynamic dispatch, reflection, DI wiring and service meshes are invisible to
  static scanning. **Runtime traces are the answer to all four** — export a
  service graph from your APM and pass `--trace`.
- Single machine, single user. No auth, no multi-tenancy.

**Absence of an edge is not proof of independence.** The tool says so
everywhere, and so should you.

---

## Privacy

Local-only by construction: no network calls in any code path. But generated
output describes internal systems, so `.cartographer/` and any local
`cartographer.yaml` are gitignored, and `scripts/install-git-hooks.sh` installs
a pre-commit hook that refuses to commit a graph database, a local config,
bytecode, or an organisation identifier.

`scripts/make-share-archive.sh` builds a distributable archive and **refuses**
unless three gates pass — the identifier guard, the full suite, and an artefact
sweep — all run against the staged copy rather than the working tree, because
those can differ.

---

## Tests

```bash
./install.sh                    # runs the suite
python3 tests/test_all.py       # 126 tests
node tests/render_smoke.js      # both renderers, ~1500 camera states
```

126 Python tests plus four standalone suites, and a renderer smoke test that
drives both engines against a **strict** canvas stub enforcing what browsers
enforce. Two synthetic codebases are built by the fixtures — a generic polyglot
estate and one shaped like an enterprise Java/SPA system.

One lesson worth repeating from building this: several assertions initially
passed against deliberately broken code, because they measured something
*adjacent* to the property they claimed to check. **A new assertion is worth
nothing until you have watched it fail.**

---

## Credit

Ideas taken and cited where used: personalised-PageRank repo maps from
[aider](https://aider.chat/2023/10/22/repomap.html); churn × complexity, change
coupling and knowledge maps from Adam Tornhill's
[code-maat](https://github.com/adamtornhill/code-maat);
`EXTRACTED`/`INFERRED` provenance and the report-plus-suggested-questions shape
from [Graphify](https://github.com/Graphify-Labs/graphify); stable cross-repo
symbol identifiers from [SCIP](https://scip-code.org/); runtime service graphs
from the OpenTelemetry `servicegraph` connector; Fruchterman-Reingold's
temperature cap, which is the piece the force layout was missing.
