---
name: service-brief
description: Write a durable brief for one service — purpose, entry points, owned data, invariants, gotchas, hotspots, and who maintains it. Use when the user is about to work in an unfamiliar service or asks what a service does.
---

# Map one service

Map the service the user is about to touch, properly. Coverage earned through
real work beats coverage guessed in a batch — do not try to do all fifteen.

## Gather

- `mcp__cartographer__service_topology` with the service — its neighbours
- `mcp__cartographer__contracts` — what it exposes; `[from spec]` is authoritative
- `mcp__cartographer__hotspots` scoped to its repo — where the risk is
- `mcp__cartographer__who_owns` on the hot files — who to ask
- `mcp__cartographer__repo_map` with the service name — its most central code

## Then read

Orient before reading: build file, directory layout, entry points. Then read
the entry-point handlers and the core domain types.

**Distinguish what it owns from what it reads.** In a microservice estate this
is the most contested and least documented fact, and confusing the two is how
people ship cross-service bugs.

## Output

Write `docs/services/<name>.md`, under two pages:

purpose · owns (entity/store/notes) · entry points (kind/identifier/handler) ·
exit points · key types · invariants and business rules · failure behaviour
(retries, timeouts, idempotency, dead-letter) · gotchas · ownership · open
questions

Then offer to write a `CLAUDE.md` into that repo so future sessions start
oriented. **Ask before writing into a company repo** — the team will see it.

## Rules

- Label inference as inference.
- Record what you could not figure out; that list is the user's agenda for
  their next conversation with the service owner, and specific questions make a
  new hire look sharp rather than lost.
- Read failure behaviour carefully for anything the user may later add an AI
  call to — it decides whether that is safe.
