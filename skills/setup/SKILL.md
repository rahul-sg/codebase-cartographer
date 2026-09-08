---
name: setup
description: Install and configure cartographer on this machine, from a zip or a fresh checkout, and build the first map. Use when the user says they have downloaded or unzipped a codebase mapping tool, asks to set cartographer up, or when another cartographer tool reports there is no graph yet.
---

# Set up cartographer

The full runbook is `SETUP-FOR-CLAUDE.md` at the root of the tool directory.
Read it and follow it — it has a check after every step. This skill is the
summary and the guardrails.

## Two rules that override anything else here

1. **Never run `scan` against a directory the user has not chosen.** It walks
   every file under the configured roots. If you are not certain where the
   repositories live, ask. Do not scan `~`, `/`, or a guess.
2. **The credential scanner records locations, never values.** When reporting
   its findings give the file and line only. Do not open a flagged line, do
   not read it into a summary, a prompt, a ticket, or a commit message.

## Procedure

1. **Find the tool.** If the user mentions a zip, look in `~/Downloads`,
   `~/Desktop` and the working directory. Unpack to `~/tools/`.
2. **Verify it runs:** `./install.sh` — checks Python 3.8+, checks git, fixes
   the executable bit, runs 120 tests against synthetic codebases it builds
   itself. If the tests fail, stop and show the user
   `/tmp/cartographer-tests.log`. A broken tool produces a wrong map, which is
   worse than no map.
3. **Ask where the repos live.** The tool wants the directory that *contains*
   the repositories, not one repository.
4. **`cartographer init --root .`** from that directory, then read the
   generated `cartographer.yaml` with the user. Ask them for internal
   codenames, legacy names and abbreviations to add as `aliases` — that single
   step decides how much of the map resolves. Ask about the commented-out
   proxy prefixes near the bottom; those are usually real services whose repos
   are not on this machine, and naming them is worth doing.
5. **`cartographer scan`** — about a second per 200 source files.
6. **Sanity-check** with `stats`, `topology`, `schema`, `questions`. Read the
   results with the user and interpret honestly: empty topology means the scan
   could not see how services talk, **not** that they are independent.
7. **`cartographer ui`** — open the visual map, and point the user at the
   Coverage tab first, which states what the map cannot see.
8. **Wire into Claude Code:** `claude --plugin-dir <dir>` for one session, or
   `./install.sh --plugin` to install it permanently. Reassure the user that
   plugin skills are namespaced, so their existing skills keep working exactly
   as before.

## Ask about runtime traces

This is the single biggest improvement available and it costs one question.
If the team runs Datadog, New Relic, Jaeger, Tempo, Honeycomb or an
OpenTelemetry collector, an exported service graph is measured rather than
inferred, and it reveals services whose repos are not on this machine:

```
cartographer scan --trace <exported-file>
```

Accepts OpenTelemetry `servicegraph` metrics, Jaeger or Zipkin dependency
JSON, or a `source,target,calls` CSV. Read from a local file — the tool never
contacts an APM.

## When you report what was found

- Give counts, then the two or three most interesting entries from
  `cartographer questions`.
- Say how many edges are `INFERRED` rather than `EXTRACTED`. Inferred means
  name-matched: a lead, never a fact.
- If the user has only some of the repos, say "among the repos I can see".
- Tell them `scan` is manual — re-run after a big pull; the UI and the MCP
  server both pick the change up on their own.
