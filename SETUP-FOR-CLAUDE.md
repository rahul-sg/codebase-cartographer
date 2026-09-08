# Setup runbook — for Claude

You are reading this because the user pointed you at this directory (or at a
zip containing it) and asked you to set up a codebase mapping tool. This file
is the complete procedure. Follow it in order; each step has a check.

**What this is:** `cartographer` — a Claude Code plugin and MCP server that
maps a large multi-repo, multi-service codebase and serves an interactive
visual map. Python standard library only: no `pip install`, no network calls,
nothing to get approved.

**Before you start, know these two things:**

1. **Never run `scan` against a directory the user has not pointed you at.**
   It walks every file. Ask where the repositories live if it is not obvious.
2. **The credential scanner records locations only, never values.** If you
   surface its findings, quote the file and line — never the secret. Do not
   read a flagged line aloud, into a summary, or into a commit message.

---

## Step 0 — locate and unpack

If the user gave you a zip:

```bash
mkdir -p ~/tools && cd ~/tools
unzip -o ~/Downloads/codebase-cartographer.zip
cd codebase-cartographer
```

Adjust the zip path to wherever they actually saved it — check `~/Downloads`,
`~/Desktop`, and the current directory. If you cannot find it, ask.

**Check:** `ls` shows `bin/`, `cartographer/`, `skills/`, `tests/`,
`install.sh`, `README.md`.

---

## Step 1 — verify it runs here

```bash
./install.sh
```

This checks Python 3.8+, checks git, fixes the executable bit (some transfers
drop it), and runs the full test suite against two synthetic codebases it
builds itself.

**Check:** the output ends with `Ran 116 tests` and `ok`.

**If Python is missing:** stop and tell the user. Do not try to install
Python; that is a machine-policy decision, not yours.

**If tests fail:** show the user the last 25 lines of
`/tmp/cartographer-tests.log` and stop. A failure here means the tool will
misbehave on their real code, and a wrong map is worse than none.

---

## Step 2 — find the workspace

The tool needs the directory that **contains** the repositories, not one
repository. A workspace usually looks like:

```
~/projects/something/
├── service-a/       <- its own git repo
├── service-b/       <- its own git repo
└── web-app/         <- its own git repo
```

Ask the user: *"Which directory holds the repos you want mapped?"* Do not
guess, and do not scan `~` or `/`.

**Check:** `ls <workspace>` shows several directories that look like repos.

---

## Step 3 — generate the config

```bash
cd <workspace>
~/tools/codebase-cartographer/bin/cartographer init --root .
```

This discovers repositories, build modules, container definitions and frontend
proxy configs, then writes `cartographer.yaml` with aliases pre-filled.

**Check:** it reports a count of repositories and services. Read
`cartographer.yaml` and show the user the `services:` list.

**Now do the one thing that matters most.** Aliases decide whether the tool
recognises that `order-svc`, `OrderService`, `ORDER_SERVICE_URL` and
`registry/org/order-svc:latest` are one thing. `init` guesses well but cannot
know internal codenames.

Ask the user: *"Are there internal names, legacy names or abbreviations for
any of these services that I should add as aliases?"* Add what they give you.

Also look for a commented-out block near the bottom of the file listing proxy
prefixes with no matching module. Those are usually real services whose repos
are not on this machine. Ask the user whether to uncomment them — naming a
service you cannot see is valuable, because edges pointing at it then resolve
instead of being silently dropped.

---

## Step 4 — build the graph

```bash
cartographer scan
```

Roughly a second per 200 source files. A 16,000-file estate takes about a
minute and a half.

**Check:** it prints a node and edge count and a gap count, and writes
`.cartographer/` in the workspace.

**If it warns about credentials:** report the count and tell the user to run
`cartographer secrets` for locations. Do not open those lines yourself.

**If the scan finds almost nothing:** the `roots:` in `cartographer.yaml` are
probably wrong. Check the paths exist and re-run `cartographer doctor`.

---

## Step 5 — sanity-check the result

Run these and read them with the user. This is where you find out whether the
extractors matched their actual conventions, which is the real risk.

```bash
cartographer stats        # does the service count match what they expect?
cartographer topology     # are there service-to-service edges at all?
cartographer schema       # tables found? any shared by 2+ services?
cartographer questions    # what the scan could not determine
cartographer coverage 2>/dev/null || true
```

Interpret honestly:

- **No service edges** — the services may talk through a mechanism the scan
  cannot see (a mesh, a gateway, dynamic config). Say that; do not conclude
  the services are independent.
- **No tables** — either the codebase has no SQL on disk, or the DDL lives in
  a repo that was not scanned.
- **No topics** — if the user says they use Kafka, the topic names are
  probably built in a way the extractor did not recognise. Report it rather
  than concluding there is no messaging.
- **Everything empty** — check `cartographer doctor` before anything else.

---

## Step 6 — show them the map

```bash
cartographer ui
```

Opens `http://127.0.0.1:8787` — loopback only. Tell the user to start on the
**Coverage** tab, which states what the map cannot see, before relying on it.

---

## Step 7 — wire it into Claude Code

For one session, installing nothing:

```bash
claude --plugin-dir ~/tools/codebase-cartographer
```

Permanently, once it has earned its place:

```bash
~/tools/codebase-cartographer/install.sh --plugin
```

That copies it to `~/.claude/skills/cartographer`, where it auto-loads.

**Reassure the user about their existing setup:** plugin skills are
namespaced, so their own `/pr` or any other skill keeps working unchanged.
These arrive as `/cartographer:map`, `/cartographer:impact` and so on.
Removing the directory restores things exactly.

**Check inside a Claude Code session:** `/help` lists `/cartographer:*` under
custom commands, and asking for the available cartographer tools should return
14 MCP tools.

---

## Step 8 — hand over

Tell the user, briefly:

- what was found: repos, services, edges, and how many are inferred rather
  than extracted
- the two or three most interesting entries from `cartographer questions`
- that `scan` is manual — re-run it after a big pull; the UI and the MCP
  server both pick up the change on their own
- that `README.md` covers the commands and `WORKFLOW.md` the day-to-day use

---

## Reference

| Command | Purpose |
|---|---|
| `cartographer doctor` | Check the environment; run this first when anything is wrong |
| `cartographer init --root <dir>` | Discover repos and modules, write a config |
| `cartographer scan [--trace <file>]` | Build or refresh the graph |
| `cartographer ui` | Interactive visual map |
| `cartographer map "<task>"` | Ranked file map for a task, within a token budget |
| `cartographer impact <target>` | Blast radius |
| `cartographer schema [table]` | Tables, owners, cross-service access |
| `cartographer topology [service]` | Cross-service map |
| `cartographer questions` | What the scan could not determine |
| `cartographer secrets` | Credential locations (never values) |
| `cartographer hotspots --bus-factor` | Churn × complexity; single-author risk |
| `cartographer coupling --cross-repo` | Files that change together across repos |

**Runtime traces are the single biggest improvement available.** If the team
runs Datadog, New Relic, Jaeger, Tempo, Honeycomb or an OpenTelemetry
collector, an exported service graph is measured rather than inferred, and it
reveals services whose repos are not on this machine:

```bash
cartographer scan --trace <exported-file>
```

Accepts OpenTelemetry `servicegraph` metrics, Jaeger or Zipkin dependency
JSON, or a `source,target,calls` CSV. Everything is read from a local file;
the tool never contacts an APM.

---

## Ground rules when reporting results

The tool is careful about evidence and you should be too:

- Every edge carries a `file:line`. Cite it.
- `EXTRACTED` means read from source, config, a spec or a trace.
  `INFERRED` means name-matched — a lead, not a fact. Never restate an
  inferred edge as certain.
- **Absence of an edge is not proof of safety.** A dependent may live in a
  repo that is not here, or be reached through reflection, dependency
  injection, a service mesh, or configuration.
- When the user has only some of the repos, say "among the repos I can see".
  That distinction is the difference between a useful finding and a wrong
  claim in front of their team.
