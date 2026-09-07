# <service-name>

> Last mapped: <YYYY-MM-DD> · Repo: `<path>` · Language: `<lang>`

## Purpose

One paragraph. What this service is responsible for, in plain language.

## Owns

Data this service is the source of truth for. Be precise — "reads orders" and
"owns orders" are very different claims, and confusing them is how people ship
bugs across service boundaries.

| Entity | Store | Notes |
| --- | --- | --- |
|  |  |  |

## Entry points

How work arrives here.

| Kind | Identifier | Handler | Notes |
| --- | --- | --- | --- |
| HTTP | `POST /...` | `path:line` |  |
| Event | `topic.name` | `path:line` |  |
| Job | schedule | `path:line` |  |

## Exit points

What this service calls, and what it emits.

| Target | Kind | Where | Notes |
| --- | --- | --- | --- |
|  |  | `path:line` |  |

## Key types

The domain nouns you must understand to read this code.

| Type | Defined at | What it represents |
| --- | --- | --- |
|  | `path:line` |  |

## Invariants & business rules

Rules the code enforces that are not obvious from the type names. This is the
section that saves you from shipping a subtle bug.

## Failure behaviour

Retries, timeouts, idempotency, dead-letter handling, partial-failure semantics.
Read this before adding anything on a synchronous path — especially a model call.

## Gotchas

Surprises, traps, misleading names, dead code that looks alive. Recent commit
messages and `TODO`/`HACK` comments are the richest source.

## Ownership

From `scripts/git-ownership.sh`. Who to ask, and who to tag on a PR.

| Area | Primary | Also active |
| --- | --- | --- |
|  |  |  |

## Open questions

Things the code did not answer. Tag `#ask-team`. Specific questions make you
look sharp; vague ones make you look lost.

- [ ]
