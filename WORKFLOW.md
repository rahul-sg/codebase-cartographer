# Working with cartographer

Everything from getting the code onto your work laptop to using it well three
months in.

---

> A worked example for a large Java/Angular estate — Kafka constants,
> reactor-excluded modules, two migration systems, SPA proxy configs — is in
> [`examples/`](examples/), with a pre-filled config you can copy.

## Part 1 — Getting it onto your work machine

### What you are moving

A directory of plain files. Nothing is tied to a Claude account, so the fact
that your work Claude is a different account than your personal one does not
matter at all. Skills, agents, MCP config and Python source are just files.

### Option A — via a git remote (recommended)

On this laptop, push the repo somewhere you can reach from work — a personal
GitHub repo (private), or your company's Bitbucket if you are comfortable
having it there. It contains no company code, only tooling.

```bash
cd ~/Desktop/My-Documents/Programming/Personal\ Projects/codebase-cartographer
git remote add origin <your-remote>
git push -u origin main
```

Then on the work machine:

```bash
git clone <your-remote> ~/tools/codebase-cartographer
```

### Option B — no remote

Zip it and move it however you normally move files.

```bash
cd ~/Desktop/My-Documents/Programming/Personal\ Projects
zip -r cartographer.zip codebase-cartographer -x '*/.git/*' '*/__pycache__/*'
```

`claude --plugin-dir` accepts a `.zip` directly, so you do not even have to
unpack it to try it.

### First checks on the work machine

```bash
cd ~/tools/codebase-cartographer
python3 --version          # need 3.8+; every corporate Mac and Linux box has it
python3 tests/test_all.py  # 71 tests, ~6 seconds, proves the install is sound
./bin/cartographer doctor
```

If the tests pass, the tool works on that machine. That is the whole
verification story — no dependencies means nothing else can be missing.

---

## Part 2 — Loading it into Claude Code

### Try it for one session (start here)

```bash
claude --plugin-dir ~/tools/codebase-cartographer
```

Installs nothing, modifies nothing, disappears when you close the session.

### Make it permanent

```bash
cp -r ~/tools/codebase-cartographer ~/.claude/skills/cartographer
```

A plugin in your skills directory auto-loads on the next session as
`cartographer@skills-dir`. No marketplace, no install step.

### It will not disturb the skills you already have

This is worth being precise about, because it was your main worry.

Plugin skills are **namespaced**. From the Claude Code docs:

> Plugin skills are namespaced as `/plugin-name:skill-name`, so the original
> `/skill-name` and the plugin copy both remain available rather than one
> overriding the other.

So your PR skill stays `/pr`. Your working-contract skill stays where it is.
These arrive as `/cartographer:map`, `/cartographer:impact`, and so on. Delete
the directory and your setup is exactly as it was.

One genuine caveat: **agents** are the exception — your own `.claude/agents/`
definitions override same-named plugin agents. The agent here is called
`cartographer-explorer`, so a collision is very unlikely, but that is the one
place where order matters.

### Verifying it loaded

```
/help          → Custom commands tab lists /cartographer:*
/plugin        → manager UI; the Errors tab explains anything that failed
/context       → Custom Agents shows cartographer-explorer
```

The MCP server registers itself from `.mcp.json` using `${CLAUDE_PLUGIN_ROOT}`,
so its path resolves wherever you put the plugin. Ask Claude "what cartographer
tools do you have?" — you should get eleven.

---

## Part 3 — First real run against the OMS

### Step 1: point it at the repos

```bash
cd /projects/ong
cartographer init --root /projects/ong
```

This writes `cartographer.yaml` listing every repo it found.

### Step 2: fill in the aliases — do not skip this

**This is the highest-leverage twenty minutes you will spend on the whole
tool.** The map is only as good as its ability to recognise that `order-svc`,
`orders`, `OrderService`, `ORDER_SERVICE_URL` and `itn/order-svc:latest` all
name one thing.

```yaml
services:
  - name: order-svc
    repo: /projects/ong/order-svc
    aliases: [OrderService, ORDER_SVC, order-service, oms-order]
    purpose: "Owns the order lifecycle from submission to fulfillment."
    owns_data: [orders, order_lines]
```

The resolver already handles case, separators, camelCase, plurals, common
suffixes (`-svc`, `-service`, `-api`) and Docker image tags automatically. You
only need to add spellings it could not guess — internal codenames, legacy
names, abbreviations.

**Also add the services whose repos you do NOT have.** You said `/projects/ong`
holds four or five of fifteen-plus. Listing the others by name means edges
pointing at them resolve into real nodes instead of being dropped:

```yaml
  - name: ledger-svc
    aliases: [ledger, LEDGER_SERVICE]
    # no repo: we only see it from the outside
```

That turns "I can't see anything there" into "here is the boundary of what I
can see" — which is far more useful, and is a good, specific thing to ask a
teammate about.

### Step 3: scan

```bash
cartographer scan
```

Seconds on a small estate, a couple of minutes on a large one. Re-running is
always safe: each extractor replaces its own contribution rather than adding to
it.

### Step 4: read what it found

```bash
cartographer questions
```

Start here, not with the diagram. These are the things the code could not tell
you — a shared database with no clear owner, an event nobody appears to consume,
a file only one person has ever touched. **This list is your agenda for your
first few conversations with the team**, and asking specific questions like
these is exactly what makes a new engineer look sharp rather than lost.

Then open `.cartographer/GRAPH_REPORT.md` and `.cartographer/graph.html`.

---

## Part 4 — The thing that will make the biggest difference

### Ask what APM the team runs

Datadog, New Relic, Jaeger, Grafana Tempo, Honeycomb, or a plain OpenTelemetry
collector — any of them already knows your true service topology, **measured
rather than inferred**. Every trace records that service A actually called
service B, with request counts and error rates.

Export the service dependency graph and feed it in:

```bash
cartographer scan --trace ~/exports/servicegraph.prom
```

Accepted formats: OpenTelemetry `servicegraph` metrics (Prometheus text),
Jaeger or Zipkin `/api/dependencies` JSON, or a `source,target,calls` CSV.
Everything is read from a local file — the tool never contacts your APM.

Why this matters so much for your situation:

- It is **ground truth**, not a guess from a config string.
- It reveals services **whose repos you do not have**. In testing, a service
  appearing only in traces still shows up correctly in the topology and in
  blast-radius results.
- It catches dynamic dispatch, service meshes and gateways — everything static
  scanning structurally cannot see.
- Where traces and config disagree, one of them is telling you something
  interesting: a dead config entry, or an undocumented call path.

This costs you one question in Slack and is probably the single highest-value
item in this whole document.

---

## Part 5 — Daily use

You mostly will not run the CLI. Claude uses the MCP tools directly. But
knowing what is available shapes what you ask for.

### Seeing it

```bash
cartographer ui
```

Keep this open in a tab while you work. It reads the same graph Claude does,
so what you see and what Claude answers from are the same thing — and when
Claude says "order-svc publishes `oms.order.submitted`, consumed by three
services", you can look at that edge and click through to the lines that
prove it.

The habit worth forming: **click the edge, not just the node.** Any arrow
expands into its citations. That is how you go from "the tool says these are
connected" to "I have read the line where they connect", which is the
difference between repeating a tool's output and knowing something.

Start on **Coverage** on day one. It tells you what the map cannot see, which
is the part you must not accidentally claim.

### Starting any unfamiliar task

Just say what you are doing:

> "I need to add a discount override to order submission. Where does that live?"

Claude calls `repo_map` with your task, gets the ranked files, and reads the
right ones. Compare that to grepping blindly across fifteen repos.

You can also run it yourself:

```bash
cartographer map "discount override on order submission" --budget 3000
```

### Before you edit anything

> "What breaks if I change `PricingEngine.computeTotal`?"

`blast_radius` returns, in order: the contract surface at risk, the services
affected with evidence, historical co-change including cross-repo, and
suggested reviewers from git history.

```bash
cartographer impact computeTotal -v
```

### While writing a PR

`/cartographer:pr-context` produces the impact block — services touched,
downstream consumers, contract changes, files that historically change with
your diff **but are not in it**, and reviewers with reasons.

That last one is the sleeper. "These two files have changed together in 5 of
the last 6 commits and you only changed one" is the kind of catch that makes
reviewers trust you.

This composes with your existing PR skill rather than replacing it: yours
writes the narrative, this supplies the analysis.

### When you do not understand something

```bash
cartographer owns path/to/File.java     # who to ask, with commit counts
cartographer path OrderService Ledger   # how are these two connected?
cartographer contracts pricing-svc      # what does it expose?
```

And `/cartographer:journal "why does cancellation write to two tables?"` logs
the question without breaking your flow. Batch-answer them weekly with
`/cartographer:journal`.

---

## Part 6 — A realistic first three months

### Week 1 — set up and observe

Scan. Read `questions`. Bring three or four of them to your onboarding buddy —
specific, evidence-backed questions, each citing a file and line. Ask about the
APM export. Do not try to understand everything.

### Weeks 2–4 — map what you touch

Every time real work takes you into an unfamiliar service, run
`/cartographer:service-brief <name>` and spend twenty minutes finishing the
document. Coverage earned through actual work is accurate; coverage guessed in
a batch rots immediately.

Trace one canonical journey properly with `/cartographer:trace-flow "buyer
submits an order"`. This single document will teach you more than a week of
reading, and it is the artifact most likely to be useful to the next person who
joins.

### Month 2 — become the person who knows the shape

You will now know things most of your team does not, because nobody has looked:
which services share a database, which events have no consumer, where the
cross-repo couplings are, which files have a bus factor of one.

Run `/cartographer:ai-surface`. That is your differentiator as the AI/ML
engineer: a ranked, evidence-backed view of where AI could plausibly go, with
honest constraints — latency budget, failure tolerance, data availability, data
sensitivity, and whether an eval story exists.

**If there is no eval infrastructure anywhere, that is your opening.** It is
the highest-value early contribution an AI/ML engineer can make, it does not
require deep domain tenure, and you can propose it with evidence rather than
opinion.

### Month 3 — contribute the map back

By now the maps have proven useful to you. Offer them to the team: the flow
traces, the service briefs, the topology diagram. Propose committing a short
`CLAUDE.md` into repos your team owns so everyone's Claude sessions start
oriented. **Ask first** — those files are visible to everyone.

If it lands, package the plugin for the team through a plugin marketplace.

---

## Part 7 — Judgement: how to not get burned

The tool is careful, but you are the one whose credibility is on the line.

**Trust ranking, roughly highest to lowest:**

1. **Runtime traces** — observed traffic. As close to fact as you get.
2. **Specs** — OpenAPI/AsyncAPI. Marked `[from spec]`. The agreed contract.
3. **`EXTRACTED` edges** — read from a config file or annotation, with
   `file:line`. Reliable about what the code says.
4. **Git coupling** — real, but correlation. "These change together" is a
   question to investigate, not a conclusion.
5. **`INFERRED` edges** — name-matched, not scope-resolved. Marked `?`
   everywhere. **Always verify before repeating these to a person.**

**Four things to say out loud rather than gloss over:**

- *"No edge found" is not "safe to change."* The dependent may be in a repo you
  do not have, or reached by reflection, DI, a mesh, or config.
- *You can see 4–5 of 15+ repos.* Say "among the repos I can see". The
  difference matters and stating it builds trust rather than eroding it.
- *Complexity here is a branch-keyword proxy*, not real cyclomatic complexity.
  Fine for ranking hotspots, not a metric to quote at people.
- *Cross-repo coupling is inferred from shared ticket keys* and, more weakly,
  same-author-same-day. Worth investigating, not proof.

**And the general rule:** never let a tool output become something you assert
in a standup or a PR without having read the code it points at. The map tells
you *where*; only reading tells you *what*.

---

## Part 8 — Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no graph -- run cartographer scan` | Not scanned yet, or you are in a different directory. The config is found by walking up from cwd; set `CARTOGRAPHER_CONFIG` to pin it. |
| Claude does not see the tools | `/plugin` → Errors tab. Then `/reload-plugins`. Check `bin/cartographer` is executable. |
| No hotspots or coupling | Needs git history. `cartographer doctor` marks repos without git with `!`. Shallow clones have little history — `git fetch --unshallow`. |
| Services not merging (`order-svc` and `orders` both appear) | Add the alias to `cartographer.yaml`, re-scan. |
| A phantom service appeared | Usually an unaliased spec title or hostname. Add it to `services:` or an alias. |
| Scan is slow | `--skip-history` for a quick structural pass, or `defaults.call_edges: false`. |
| Too many `INFERRED` call edges | Expected with the regex backend. Set `call_edges: false` if the noise outweighs the value. |
| Wrong repos discovered | Point `--root` at the directory *containing* the repos, not at one repo. |

`cartographer doctor` checks all of this in one go — run it first.

---

## Part 8b — What to expect at your scale

Measured on a synthetic 15-service, 3,300-file Java estate (28 MB of source),
which is roughly the shape of an OMS:

| Operation | Time |
|---|---|
| Full `scan` (symbols, topology, specs, builds, history, PageRank) | ~17 s |
| `topology`, `questions`, `hotspots`, `find`, `impact` | under 0.3 s |
| `map "<task>"` (recomputes personalised PageRank) | ~3 s |
| Graph size | 39.6k nodes, 92k edges, 70 MB `graph.db` |

So: scan once in the morning or after a big pull, and every query after that is
effectively instant. The interactive HTML caps at the 1,200 most central nodes
so the browser stays responsive; `graph.json` is skipped above 20k nodes
(`cartographer report --json` forces it) because `graph.db` holds the same data
and is queryable with plain `sqlite3`.

If a scan is ever too slow: `--skip-history` for a quick structural pass, or
set `defaults.call_edges: false` to drop symbol-level call inference, which is
where most of the time goes.

---

## Part 9 — Extending it

The design is deliberately additive: each extractor writes into one SQLite
graph and can be replaced without touching the others.

Worth adding when you have a reason:

- **A better symbol layer.** If you can get tree-sitter or a SCIP indexer
  (`scip-java` for JVM) approved, it replaces the regex backend and turns those
  `INFERRED` call edges into resolved ones. The graph schema already carries
  SCIP-style identifiers for exactly this.
- **A Backstage `catalog-info.yaml` importer**, if your team adopts Backstage —
  service ownership metadata for free.
- **Data-flow analysis** (Joern-style) for the AI work specifically: tracing
  where customer and pricing data flows *before* it reaches a model call is a
  question you will be asked, and it deserves a rigorous answer.

Run the tests after any change: `python3 tests/test_all.py`.
