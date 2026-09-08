# cartographer

A Claude Code plugin and MCP server for mapping a large multi-service codebase
you did not write.

Built for the case where a codebase is too large to hold in your head: many
services, many repos, no single person who knows how it all connects. It
produces the cross-service map that usually exists nowhere, and exposes it to
Claude so every session starts oriented instead of rediscovering the system.

**No dependencies.** Python 3.8+ standard library only — no `pip install`,
nothing to get approved. **Fully local**: no network calls, ever. Nothing about
your code leaves the machine.

---

## Why two layers

No AST crosses a process boundary. A symbol graph of `order-svc` will never show
that it calls `pricing-svc`, because that edge exists only as the string
`PRICING_SERVICE_URL` in a YAML file. Conversely a service map knows nothing
about functions. Each layer is blind exactly where the other sees.

| Layer | What it maps | Built from |
|---|---|---|
| **Symbol** | classes, functions, `calls` / `imports` / `extends` | source, 9 languages |
| **Module** | services vs shared libraries, build-graph deps, reactor exclusions | Maven/Gradle/npm/go manifests |
| **Service** | who calls whom, events, contracts, hosts | config, proxy configs, specs, **runtime traces** |
| **Data** | tables, who declares them, who reads and writes them | migration SQL + SQL in DAOs |
| **Behavioural** | hotspots, change coupling, ownership | git history |

The layers are joined — every symbol carries its service — so `blast_radius`
follows a change from a method, through its repo, and out across a service
boundary.

---

## Install

```bash
unzip codebase-cartographer.zip -d ~/tools     # or: git clone <your-repo>
cd ~/tools/codebase-cartographer
./install.sh                                    # verify it runs here (~10s)
```

Nothing is fetched and nothing is installed with a package manager — the tool
is Python standard library only. `./install.sh --link` adds it to your PATH;
`./install.sh --plugin` installs it as an always-on Claude Code plugin.

For one session, installing nothing:

```bash
claude --plugin-dir ~/tools/codebase-cartographer
```

**Setting this up with Claude?** Point it at
[`SETUP-FOR-CLAUDE.md`](SETUP-FOR-CLAUDE.md) — an ordered runbook with a check
after every step.

Once it earns its place:

```bash
cp -r ~/tools/codebase-cartographer ~/.claude/skills/cartographer
```

Plugins in your skills directory auto-load on the next session. **Your existing
skills are untouched** — plugin skills are namespaced (`/cartographer:map`), so
your own `/pr` stays exactly `/pr`.

See **[WORKFLOW.md](WORKFLOW.md)** for the full day-one-to-month-three guide.

---

## The visual map

```bash
cartographer ui          # opens http://127.0.0.1:8787
```

A local, offline, clickable map of the whole estate, reading the same
`graph.db` the MCP server reads. Loopback only, no CDN, no framework — it
works behind a corporate proxy because it never leaves the machine.

| View | What it answers |
|---|---|
| **Map** | How does everything connect? Drill estate → service → package → file. Click a node for detail, click an **edge** for the exact `file:line` citations behind it. |
| **Layers** | What shape is this system? 3D stack: clients, services, messaging, data. |
| **Data** | Who owns which table, and which tables are touched by more than one service? |
| **Flow** | What happens end to end when an order is submitted? |
| **Impact** | What breaks if I change this? With a copy-for-Claude button. |
| **Time** | How did the estate get this way? Monthly activity, with a scrubber. |
| **Coverage** | What can this map *not* see? |

`scan` again in another terminal and the page updates itself.

### Why it is hierarchical

Your estate produces roughly 145,000 nodes. Drawn at once that is a grey
hairball — not slow, **illegible**. So the graph collapses to whichever level
you are looking at and rolls edges up with counts. Measured on a 39,796-node
graph: the estate view renders **26 readable nodes with 39,770 rolled up
inside them, in 0.39s**. Nothing is discarded — a service-to-service arrow
expands into the individual calls that justify it.

## Quick start

```bash
cartographer init --root /projects/ong    # discover repos, write config
$EDITOR cartographer.yaml                 # add aliases — this matters most
cartographer scan                         # build the graph (~seconds)
cartographer questions                    # what to ask your team
```

Then in Claude Code, just work — the MCP tools are used automatically.

---

## CLI

| Command | Purpose |
|---|---|
| `init --root <dir>` | Discover repos, modules, containers and proxy prefixes; write a pre-filled config |
| `scan [--trace <file>]` | Build/refresh the graph and reports |
| `map "<task>"` | Ranked repo map for a task, within a token budget |
| `impact <target>` | Blast radius across both layers |
| `find <query>` | Locate a symbol, file, service, route or topic |
| `topology [service]` | Cross-service map (`--mermaid` for a diagram) |
| `contracts [service]` | Routes and events a service exposes |
| `coupling [--cross-repo]` | Files that change together, from git |
| `hotspots [--bus-factor]` | Churn × complexity; single-author risk |
| `owns <path>` | Who has actually worked on this code |
| `path <a> <b>` | How two things are connected |
| `schema [table]` | Tables, owners, and cross-service data access |
| `secrets` | Credential locations (values are never recorded) |
| `questions` | What the scan could not determine |
| `report` / `stats` | Regenerate reports / inspect the graph |
| `doctor` | Check the environment |
| `ui` | Open the interactive visual map in a browser |
| `serve-mcp` | Run the MCP stdio server |

## MCP tools

`repo_map` · `find_symbol` · `blast_radius` · `service_topology` · `who_owns` ·
`hotspots` · `coupled_files` · `shortest_path` · `contracts` · `schema_map` ·
`module_inventory` · `secret_locations` · `open_questions` · `graph_stats`

## Skills

`/cartographer:onboard` · `map` · `impact` · `pr-context` · `trace-flow` ·
`service-brief` · `ai-surface` · `journal`

---

## Outputs

Written to `.cartographer/`:

- `graph.db` — SQLite. Queryable with `sqlite3`, diffable, no server.
- `GRAPH_REPORT.md` — the human entry point, ending in questions to ask.
- `graph.html` — interactive force-directed graph. Self-contained; opens
  offline with no CDN.
- `topology.mmd` — Mermaid service diagram.
- `graph.json` — full export for your own scripts.

---

## Ground rules baked in

- **Every edge carries `file:line` evidence.** No exceptions.
- **`EXTRACTED` vs `INFERRED`.** Extracted is read from source, config, a spec,
  or a trace. Inferred is name-matched — a lead, never presented as a fact.
- **Gaps are stated, never filled with plausible guesses.** "I could not find
  the consumer" is a useful finding; an invented consumer is worse than nothing.
- **Absence of an edge is not proof of safety.** A dependent may live in a repo
  you do not have, or be reached through reflection, DI, a mesh, or config.

## Conventions it handles that trip up generic tools

- **Topic names as enum constants**, resolved at runtime rather than written as
  string literals — a literal-scanning regex finds none of them.
- **Modules excluded from the parent Maven reactor** that still build and
  deploy. A parent-POM module scan misses these; they are real services.
- **No ORM.** Schema is read from migration SQL (legacy patch-lists *and*
  Flyway), and data access from SQL strings in hand-written DAOs — with
  `+`-concatenated literals merged first, because that is how DAO SQL is
  actually written.
- **Frontend proxy configs** as the real dependency declaration, since SPAs
  call path prefixes rather than naming services.
- **Spring routes split across class-level and method-level annotations**,
  composed into full paths.
- **One schema shared by two services**, which reading either module alone
  cannot reveal.
- **Credentials in source**: flagged by location, never by value. A test
  asserts the values are absent from `graph.db`.

A worked example for a large Java/Angular microservice estate lives in
[`examples/`](examples/) — a pre-filled config and a day-to-day guide.

## Known limits

- The regex backend is ~70% recall versus a real parser, and `calls` edges are
  name-matched rather than scope-resolved — hence `INFERRED`.
- One definition per line; `class X { void y() {} }` yields only `X`.
- Ambiguous call targets are dropped rather than guessed, and recorded as gaps.
- Dynamic dispatch, reflection, DI wiring, and service meshes are invisible to
  static scanning. Runtime traces are the answer to all four — feed them in.

## Privacy

Local-only by construction: no network calls in any code path. But
`.cartographer/` and generated docs describe internal systems, so they are
gitignored by default. Keep the *tooling* portable and the *output* inside your
work environment. Ask before committing a generated `CLAUDE.md` to a team repo.

## Tests

```bash
python3 tests/test_all.py        # 96 tests; builds two synthetic estates
```

One fixture is a generic polyglot estate; the other mirrors a Maven
multi-module backend with constant-based Kafka topics, Angular proxy configs,
two migration systems and no ORM.

## Credit

The good ideas are borrowed and cited where used: personalised-PageRank repo
maps from [aider](https://aider.chat/2023/10/22/repomap.html); churn×complexity,
change coupling and knowledge maps from Adam Tornhill's
[code-maat](https://github.com/adamtornhill/code-maat); `EXTRACTED`/`INFERRED`
provenance and the report-plus-suggested-questions shape from
[Graphify](https://github.com/Graphify-Labs/graphify); stable cross-repo symbol
identifiers from [SCIP](https://scip-code.org/); runtime service graphs from the
OpenTelemetry `servicegraph` connector.
