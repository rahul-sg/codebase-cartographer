---
name: pr-context
description: Produce the impact section for a pull request — services touched, downstream consumers, contract changes, and reviewers drawn from git history. Use when the user is opening a PR, writing a PR description, or asking who should review a change.
---

# PR impact section

This composes with whatever PR skill the user already has: that one writes the
narrative, this one supplies the analysis. Do not duplicate their format or
their conventions — produce the block and let their skill place it.

## Gather

1. `git diff --name-only` against the base branch for the changed files.
2. For each meaningful changed file or symbol, call
   `mcp__cartographer__blast_radius`.
3. `mcp__cartographer__coupled_files` with the changed paths — files that
   historically change together but are *not* in this diff are the most useful
   thing you can tell a reviewer, because that is what the author forgot.
4. `mcp__cartographer__who_owns` for the changed paths, for reviewers.

## Produce

```
**Services touched:** order-svc, pricing-svc

**Downstream impact**
- catalog-svc, ml-svc consume `oms.order.submitted` (payload shape changed)
- pricing-svc calls `POST /api/v1/orders` (runtime-confirmed, ~150k calls/day)

**Contract changes**
- `POST /api/v1/orders` request body gained a required field  ← breaking

**Historically changes with this diff but is NOT included**
- `pricing-svc/.../PricingEngine.java` (5 shared commits, cross-repo)

**Suggested reviewers:** Dana Reyes (owns PricingClient.java, 6 of 6 commits)
```

## Rules

- Lead with anything breaking. A reviewer who reads only the first two lines
  should still learn the risky part.
- Mark uncertainty explicitly. "Possible consumer, not verified" is honest and
  useful; a confident wrong claim damages the user's credibility with their team.
- Name reviewers with the reason ("owns this file, 6 of 6 commits"), never bare.
- If the diff touches a service whose consumers are not on this machine, say so
  rather than implying the blast radius is fully known.
