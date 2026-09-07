---
description: Trace one entity or user journey end to end across every service it touches — e.g. an order from submission to fulfillment. Use when the user asks what happens when X, how a request flows, where something gets created/validated/persisted, or wants to follow a domain object across service boundaries.
---

# End-to-end flow trace

Following one object all the way through a system is the fastest way to learn a domain. This skill produces a durable trace document, not a chat answer — you pay for the investigation once and read it for free forever after.

Arguments: `$ARGUMENTS` — the journey to trace (e.g. "buyer submits an order", "order cancellation", "invoice generation"). If empty, ask which one; do not pick for the user.

## Before you start

Read `context/services.json` if it exists — it tells you which services to look in and saves a great deal of blind searching. If it doesn't exist, say so and suggest running `/cartographer:topology` first, but proceed anyway if the user wants.

## Method

Work **forward from the entry point**, not backward from the database.

1. **Find the entry point.** An HTTP route, a queue consumer, a scheduled job, a UI action. Name the exact handler with `file:line`.
2. **Follow the call.** At each step record: what validates, what transforms, what persists, what emits.
3. **Cross the boundary explicitly.** When control leaves a service, say how — HTTP call, event published, shared DB table, batch file. Boundary crossings are where the real complexity lives and where documentation always stops.
4. **Continue in the next service.** Find the consumer and repeat. Recurse until you reach terminal states.
5. **Map the failure paths too.** What happens on validation failure, timeout, partial write? For an OMS this matters more than the happy path — the happy path is obvious, the exception handling is where the business logic hides.

Use parallel `cartographer-service-explorer` subagents when the trace fans out across several services at once. Give each one a specific question about a specific service, not a vague brief.

## Output

Write to `docs/flows/<slug>.md`:

- **One-paragraph summary** — the whole journey in plain language, no jargon.
- **Sequence diagram** (Mermaid) of services and boundary crossings.
- **Numbered step table**: step | service | file:line | what happens | what it emits.
- **State transitions** the entity goes through, and the terminal states.
- **Failure and edge paths**, each with where it's handled.
- **Open questions** — things you could not determine from the code. Append these to `journal/questions.md` so they become the user's list to ask a teammate.

## Rules

- Every claim gets a `file:line`. If you cannot cite it, mark it **"assumption:"** explicitly.
- Do not smooth over gaps. A trace that says "control leaves here and I could not find the consumer" is genuinely useful; one that invents a plausible consumer is worse than nothing and will embarrass the user in front of their team.
- Prefer reading actual handler code over reading tests or docs — tests show intent, docs show what was true once, only the handler shows what runs.
- Note the domain vocabulary you encounter as you go and add unfamiliar terms to `docs/GLOSSARY.md`.
