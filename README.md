# cartographer

A Claude Code plugin for mapping a large multi-service codebase you didn't write.

Built for ramping up on a 15+ microservice order management system: it produces
the cross-service map that usually exists nowhere, so that both you and Claude
Code start every session oriented instead of rediscovering the system each time.

Everything runs **fully local**. The scripts make no network calls, and no code
leaves the machine.

## What it does

| Skill | Purpose |
| --- | --- |
| `/cartographer:topology` | Cross-service map — who calls whom, what events flow. Emits `context/services.json` + a Mermaid diagram. |
| `/cartographer:map-service <name>` | Deep brief on one service: entry points, owned data, invariants, gotchas, who maintains it. |
| `/cartographer:trace-flow <journey>` | Follow one entity end to end across every service it touches. |
| `/cartographer:ai-surface` | Where AI/ML already lives, where it could go, and what the constraints are. |
| `/cartographer:impact <symbol>` | Blast radius across both layers — what breaks, which services, who to tag. |
| `/cartographer:journal [question]` | Log a ramp-up question, or batch-answer the backlog against the code. |

Plus a read-only `cartographer-service-explorer` subagent for parallel fan-out
across repos, and three dependency-free scripts:

- `scripts/git-ownership.sh` — who maintains what, what churns, what's stale, what changes together
- `scripts/topology-scan.sh` — raw evidence of service-to-service edges
- `scripts/symbol_graph.py` — intra-repo symbol graph (classes, functions, imports, calls)

## Two layers

| Layer | Maps | Built by |
| --- | --- | --- |
| **Symbol** | functions, classes, `calls` / `imports` / `extends` — inside a repo | `symbol_graph.py`, or [Graphify](https://github.com/Graphify-Labs/graphify) |
| **Service** | who calls whom across processes, events, owned data | `/cartographer:topology` |

The layers are joined: every symbol node carries the service it belongs to, so
`/cartographer:impact` can follow a change from a method through its repo and
out across a service boundary.

**Why both.** No AST crosses a process boundary. A symbol graph of `order-svc`
will never show that it calls `pricing-svc`, because that edge exists only as
the string `PRICING_SERVICE_URL` in a config file. Conversely the service map
knows nothing about functions. Each layer is blind exactly where the other sees.

### On Graphify

If you can get [Graphify](https://github.com/Graphify-Labs/graphify) approved,
use it for the symbol layer — it does tree-sitter parsing across ~37 grammars
and will beat `symbol_graph.py` comfortably. Its code path is local and
LLM-free (the API-key backends are only for docs/PDF/image extraction, which
you can skip). Note that `graphify install` writes `~/.claude/skills/graphify/`,
appends to `~/.claude/CLAUDE.md`, and installs `PreToolUse` hooks — additive,
but worth knowing before you run it, and worth raising with your security team
as a third-party dependency question rather than a data-egress one.

`symbol_graph.py` exists so this still works if the answer is no. It uses regex,
not AST: roughly 70% recall, and its `calls` edges are name-matched rather than
scope-resolved, so they're tagged `INFERRED`. Ambiguous names are dropped rather
than guessed.

Edge provenance follows Graphify's convention — `EXTRACTED` for what's read
directly from source, `INFERRED` for what's derived. Treat every `INFERRED` edge
as a lead to verify.

The scripts deliberately **decide nothing**. They gather cited candidates and
hand them to Claude to interpret. Dumb extraction plus smart interpretation
beats a clever script that silently drops edges.

---

## Installing at work

The plugin is just files, so it moves anywhere. Nothing is tied to a Claude
account.

### Option A — try it for one session

```bash
claude --plugin-dir /path/to/oms-cartographer
```

Loads for that session only. Nothing is installed, nothing is modified. Start here.

### Option B — always available (recommended once it earns its place)

```bash
cp -r oms-cartographer ~/.claude/skills/cartographer
```

A plugin placed in your skills directory auto-loads on the next session as
`cartographer@skills-dir`, with no marketplace and no install step.

### Option C — share it with the team

Push to a repo and distribute it as a plugin marketplace. Worth doing only once
the maps have proven useful to someone other than you.

### Useful commands

```bash
claude plugin validate ./oms-cartographer   # check structure before trusting it
/reload-plugins                             # pick up edits without restarting
/plugin                                     # manager UI; Errors tab if something won't load
/help                                       # Custom commands tab lists the skills
```

---

## How this coexists with skills you already have

**It does not touch them.** Plugin skills are namespaced, so:

- your existing `/pr` skill stays exactly `/pr`
- these arrive as `/cartographer:trace-flow`, `/cartographer:topology`, …

Per the Claude Code docs: *"Plugin skills are namespaced as
`/plugin-name:skill-name`, so the original `/skill-name` and the plugin copy
both remain available rather than one overriding the other."*

Uninstalling leaves your setup exactly as it was.

One caveat: **agents** are the exception — your own `.claude/agents/`
definitions override same-named plugin agents. The agent here is prefixed
`cartographer-service-explorer` so it can't collide.

### Composing with your own skills

Skills integrate through **shared files on disk**, not by calling each other.
That's the loose coupling that keeps this from becoming a tangle. Everything
here writes to one place:

```
context/services.json    # topology: who calls whom, who owns what
context/ownership.json   # from git history: who actually touches which code
docs/GLOSSARY.md         # domain vocabulary
```

Once those exist, point your existing skills at them. The highest-value one:

> **PR skill × topology map.** Add a line to your PR skill telling it to read
> `context/services.json` and `context/ownership.json` when present, and to
> include in the description: which services the change touches, which
> downstream services consume the endpoints or events it modifies, and who to
> tag as reviewer based on who has real history in those files.

That turns a description generator into a blast-radius analysis — and reviewer
selection stops depending on knowing the org chart.

Your working-contract skill should stay the single source of truth for how you
and Claude work together. These skills defer to it rather than restating your
preferences; two copies of a rule will drift.

---

## Getting started

1. Fill in `config/services.yaml` — repo roots and any services you already
   know. Five accurate entries beat fifteen guessed ones.
2. Run `/cartographer:topology` and correct what it gets wrong. It will get
   things wrong on the first pass; that's expected, and correcting it is itself
   a fast way to learn the system.
3. Build the symbol layer:
   ```bash
   python3 scripts/symbol_graph.py /projects/ong -o context/symbols.json
   ```
4. Run `/cartographer:trace-flow "buyer submits an order"`. This one document
   will teach you more than a week of reading.
5. From then on, run `/cartographer:map-service <name>` whenever real work
   takes you into an unfamiliar service. Coverage earned through actual work
   beats coverage guessed in a batch.
6. Log questions with `/cartographer:journal` as they come up. Batch-answer
   weekly.

---

## Ground rules baked into the skills

- **Every claim carries a `file:line`.** Uncited claims are marked as
  assumptions or dropped.
- **Gaps are stated, never filled with plausible guesses.** "I could not find
  the consumer" is a useful finding. An invented consumer is worse than nothing
  and will embarrass you in front of your team.
- **Never answer from general knowledge of how order management systems usually
  work.** Either the codebase says it, or it's tagged `#ask-team`.

## Privacy

- Scripts are local-only: no network calls, nothing transmitted.
- **Do not commit generated output to this repo if you keep it on a personal
  machine or a public host.** `context/`, `docs/services/`, `docs/flows/` and
  `journal/` are gitignored by default — they will contain real details about
  internal systems. Keep the *tooling* portable and the *output* inside your
  work environment.
- Confirm with your team before committing any `CLAUDE.md` into a shared repo.
