---
name: impact
description: Blast radius — what breaks if this changes, which services are affected, what contract is at risk, and who should review. Use BEFORE editing unfamiliar code, when the user asks what depends on something, and when writing a PR description.
---

# Blast radius

Call `mcp__cartographer__blast_radius` with the symbol, `Class.method`, file
path or service name.

It crosses both layers, which is the point: a symbol graph stops at the repo
boundary and a service map knows nothing about functions. Joined, a change to a
method can be followed out to the services that consume its service.

## Read the output in this order

1. **Contract surface** — routes, events and schemas of affected services. This
   is what breaks *other teams*, so it leads.
2. **Services affected** — with the evidence for each. Edges marked
   `runtime-confirmed` come from real traffic and are the most trustworthy.
3. **Changes together** — historical co-change from git, especially
   `CROSS-REPO`. This finds coupling no static analysis can see; a cross-repo
   pair usually means an implicit shared contract.
4. **Suggested reviewers** — from git history, not the org chart.
5. **Limits of visibility** — say these out loud.

## Also worth calling

`mcp__cartographer__coupled_files` with `cross_repo_only: true` before assuming
a change is contained to one service.

## Rules

- **`?` marks an INFERRED edge.** Never restate one as fact. Say "possible
  caller — verify at `file:line`".
- **Absence of an edge is not proof of safety.** The dependent may live in a
  repo not on this machine, or be reached through reflection, dependency
  injection, a service mesh, or config. Say that when the result is empty.
- Over-report rather than under-report. A false positive costs a minute; a
  missed consumer costs an incident.
- When the user is writing a PR, offer a paste-ready summary: services touched,
  downstream consumers, contract changes, suggested reviewers.
