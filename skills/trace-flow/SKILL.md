---
name: trace-flow
description: Follow one entity or journey end to end across every service it touches — an order from submission to fulfillment, a cancellation, an invoice. Use when the user asks what happens when X, how a request flows, or wants to understand a business process across services.
---

# End-to-end flow trace

Following one object all the way through is the fastest way to learn a domain.
This produces a durable document, not a chat answer: pay for the investigation
once, read it free forever.

Arguments: the journey. If not given, list `flows:` from the config and ask.

## Method

1. **Get the shape first.** `mcp__cartographer__repo_map` with the journey as
   the task, then `mcp__cartographer__service_topology`. Now you know which
   services are involved before opening a file.
2. **Find the entry point** with `find_symbol` — a route, a queue consumer, a
   scheduled job. Name the handler with `file:line`.
3. **Follow forward, not backward from the database.** At each step record what
   validates, transforms, persists, emits.
4. **Cross boundaries explicitly.** Use `shortest_path` and `contracts` when
   control leaves a service. Boundary crossings are where the real complexity
   lives and where documentation always stops.
5. **Map failure paths.** Validation failure, timeout, partial write, retry,
   dead-letter. In an order management system this is where the business logic
   actually hides; the happy path is the easy part.
6. Use parallel subagents when the trace fans out across several services. Give
   each a specific question about a specific service.

## Output

Write `docs/flows/<slug>.md`:

- one-paragraph plain-language summary
- Mermaid sequence diagram of services and boundary crossings
- numbered step table: step | service | `file:line` | what happens | what it emits
- state transitions and terminal states
- failure paths, each with where it is handled
- **open questions** — anything the code did not answer

## Rules

- Every claim gets `file:line`, or is prefixed `assumption:`.
- Never smooth over a gap. "Control leaves here and I could not find the
  consumer" is a genuinely useful finding; an invented consumer is worse than
  nothing and will embarrass the user in front of their team.
- Prefer the handler over tests and docs: tests show intent, docs show what was
  once true, only the handler shows what runs.
