# codebase-cartographer

A Claude Code plugin and MCP server that maps a large multi-repo, multi-service
codebase and serves an interactive visual map of it.

**If the user has just given you this directory or a zip of it and asked you to
set it up: read `SETUP-FOR-CLAUDE.md` and follow it.** That file is a complete,
ordered runbook with a check after every step.

## Two rules that apply whenever you work with this tool

1. **`scan` walks every file under the configured roots.** Never point it at a
   directory the user has not chosen. Ask where the repositories live.
2. **The credential scanner records locations, never values.** If you report
   its findings, give the file and line only. Do not read a flagged line into
   a summary, a prompt, or a commit message.

## Reporting results

Every edge in the graph carries a `file:line`. `EXTRACTED` means read from
source, config, a spec or a trace; `INFERRED` means name-matched — a lead, not
a fact, and it must never be restated as certain.

Absence of an edge is not proof of safety: reflection, dependency injection, a
service mesh and config-driven routing are all invisible to static scanning.
When only some repos are present, say "among the repos I can see".

## Layout

```
bin/cartographer      CLI entry point (no install needed)
cartographer/         the package: extract/ analyze/ report/ mcp/ ui/
skills/               eight Claude Code skills
agents/               a read-only explorer subagent
.mcp.json             registers the MCP server for the plugin
tests/test_all.py     116 tests; builds its own synthetic codebases
examples/             a worked config for one large estate (safe to delete)
install.sh            verify, optionally add to PATH or install as a plugin
```

`README.md` covers the commands, `WORKFLOW.md` the day-to-day use.
