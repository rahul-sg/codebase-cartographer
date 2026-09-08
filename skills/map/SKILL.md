---
name: map
description: Get oriented in an unfamiliar part of the codebase before reading any files. Use at the START of any task touching code the user has not worked in — bug investigations, feature work, code review, "where is X handled", "how does Y work".
---

# Orient before reading

Reading files at random in a 15-service estate wastes the context window and
usually misses the file that mattered. Start with the ranked map.

## Do this first

Call `mcp__cartographer__repo_map` with the user's task as `task`. It returns
the files that matter for *this* task, ranked by PageRank over the dependency
graph and fitted to a token budget. Read from the top of that list.

If the tool reports no graph, invoke the `onboard` skill instead.

## Then narrow

- `mcp__cartographer__find_symbol` — exact location of a class, function, route
  or topic, with `file:line`.
- `mcp__cartographer__service_topology` — which services are involved and how
  they talk. Runtime-confirmed edges are marked; trust those most.
- `mcp__cartographer__shortest_path` — "how does A reach B", when the
  connection is not obvious.
- `mcp__cartographer__contracts` — the routes and events a service exposes.
  Entries marked `[from spec]` come from an OpenAPI/AsyncAPI file and are
  authoritative.

## Then read the actual code

The map tells you *where*; it never substitutes for reading the handler. Open
the files it ranked highest and read them properly.

## Rules

- **Never present a map entry as an explanation of behaviour.** The graph knows
  structure, not semantics.
- Edges marked `INFERRED` are name-matched, not scope-resolved. Say "possible"
  and verify by reading the code.
- If the map is empty for a task, say so rather than guessing. An empty result
  usually means the relevant repo is not on this machine — which is itself
  worth telling the user.
