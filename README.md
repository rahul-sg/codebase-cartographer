# cartographer

A Claude Code plugin and MCP server for mapping a large multi-service codebase
you did not write.

Built for ramping up on a 15+ microservice order management system. It produces
the cross-service map that usually exists nowhere, and exposes it to Claude so
every session starts oriented instead of rediscovering the system from scratch.

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
| **Service** | who calls whom, events, owned data, contracts | config, specs, **runtime traces** |
| **Behavioural** | hotspots, change coupling, ownership | git history |

The layers are joined — every symbol carries its service — so `blast_radius`
follows a change from a method, through its repo, and out across a service
boundary.

---

## Install

```bash
git clone <your-repo> ~/tools/cartographer
claude --plugin-dir ~/tools/cartographer      # one session, installs nothing
```

Once it earns its place:

```bash
cp -r ~/tools/cartographer ~/.claude/skills/cartographer
```

Plugins in your skills directory auto-load on the next session. **Your existing
skills are untouched** — plugin skills are namespaced (`/cartographer:map`), so
your own `/pr` stays exactly `/pr`.

See **[WORKFLOW.md](WORKFLOW.md)** for the full day-one-to-month-three guide.

---

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
| `init --root <dir>` | Discover repos, write `cartographer.yaml` |
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
| `questions` | What the scan could not determine |
| `report` / `stats` | Regenerate reports / inspect the graph |
| `doctor` | Check the environment |
| `serve-mcp` | Run the MCP stdio server |

## MCP tools

`repo_map` · `find_symbol` · `blast_radius` · `service_topology` · `who_owns` ·
`hotspots` · `coupled_files` · `shortest_path` · `contracts` ·
`open_questions` · `graph_stats`

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
python3 tests/test_all.py        # 71 tests, builds a synthetic estate
```

## Credit

The good ideas are borrowed and cited where used: personalised-PageRank repo
maps from [aider](https://aider.chat/2023/10/22/repomap.html); churn×complexity,
change coupling and knowledge maps from Adam Tornhill's
[code-maat](https://github.com/adamtornhill/code-maat); `EXTRACTED`/`INFERRED`
provenance and the report-plus-suggested-questions shape from
[Graphify](https://github.com/Graphify-Labs/graphify); stable cross-repo symbol
identifiers from [SCIP](https://scip-code.org/); runtime service graphs from the
OpenTelemetry `servicegraph` connector.
