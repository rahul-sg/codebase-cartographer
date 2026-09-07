---
description: Blast radius for a change — what depends on this symbol, file, or service, across both the symbol graph and service topology. Use when the user asks what breaks if they change X, what depends on X, who to tag on a PR, or is writing a PR description for a change with cross-service reach.
---

# Blast radius

Answers "if I change this, what else is affected?" — across **both** layers, which
is the point. A symbol-level graph stops at the repo boundary; a service map
doesn't know about functions. Joined, they cross the boundary.

Arguments: `$ARGUMENTS` — a symbol name, file path, service name, or nothing
(then use the current git diff).

## Inputs

- `context/symbols.json` — from `scripts/symbol_graph.py`
- `context/services.json` — from `/cartographer:topology`

If either is missing, say which and offer to generate it. You can still answer
partially with one layer; be explicit about which half of the picture is absent,
because a blast radius that silently omits cross-service reach is the dangerous
kind of wrong.

## Method

**1. Resolve the target.** Match against node `id`, `name`, and `file`. If the
name is ambiguous, list the candidates and ask — do not pick one.

**2. Walk inbound edges (symbol layer).** Who `calls`, `imports`, or `extends`
the target, transitively. Depth 2 is usually the useful limit; go deeper only if
asked. Separate `EXTRACTED` (read from source) from `INFERRED` (name-matched)
findings and label them — inferred edges are leads to verify, not facts.

**3. Cross the boundary (service layer).** Take the services owning the affected
symbols and look up their inbound edges in `services.json`. This is the step
that catches what an AST-only tool cannot:

> "You changed `OrderValidator.validate` in order-svc. That's fine internally —
> but order-svc publishes `order.submitted`, and pricing-svc and audit-svc both
> consume it."

**4. Check the contract surface.** Flag it loudly if the change touches an HTTP
handler signature, an event payload shape, a shared DB schema, or a proto
definition. These are the changes that break other teams, and they're what a PR
description most needs to call out.

**5. Attribute owners.** Run `scripts/git-ownership.sh` on the affected repos to
name who to tag for review.

## Output

Answer in the conversation, concise:

- **Direct dependents** — with `file:line`.
- **Cross-service reach** — which services, via what mechanism.
- **Contract surface** — anything that could break a consumer, called out first.
- **Suggested reviewers** — from git history, with why.
- **Confidence** — say plainly what the regex backend may have missed.

When the user is writing a PR, offer this as a paste-ready block for the
description. That's the composition with their existing PR skill: this produces
the impact analysis, their skill produces the narrative.

## Rules

- **Never present an `INFERRED` edge as certain.** Say "possible caller, verify".
- **State what you can't see.** If `/projects/ong` holds 5 of 15+ services, a
  consumer may live in a repo not on disk. "No known consumers *among the repos
  I can see*" is the honest phrasing, and the difference matters.
- Under-reporting blast radius is far worse than over-reporting it. When
  uncertain, include it and mark it uncertain.
