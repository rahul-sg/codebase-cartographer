---
description: Deep-dive a single service and write a durable brief for it — purpose, entry points, key types, data owned, dependencies, invariants, gotchas. Use when the user is about to work in an unfamiliar service, asks what a service does, or wants to document one.
---

# Map one service

Do not try to map fifteen services at once. Map the one the user is about to touch, properly. Coverage earned through real work beats coverage guessed in a batch.

Arguments: `$ARGUMENTS` — the service name. If empty, list what's in `config/services.yaml` and ask.

## Method

1. **Orient before reading.** Start with the build file, the README, the directory layout, and the entry-point file. Five minutes here saves an hour of wandering.
2. **Find the seams.** Entry points (routes, consumers, jobs, CLI), exit points (clients, publishers, DB writes), and config (env vars, feature flags).
3. **Find the core domain types.** In an order management system these are the nouns that appear everywhere. Note which service *owns* each one versus merely reads it — ownership is the single most contested and least documented fact in a microservice system.
4. **Run `scripts/git-ownership.sh <repo-path>`** to learn who actually maintains this code and which files churn. Names here are who to ask, and who to tag on a PR.
5. **Hunt for the gotchas.** Read recent commit messages, `TODO`/`FIXME`/`HACK` comments, and any file with an unusual number of defensive checks. This is where the tribal knowledge lives.

## Output

Write `docs/services/<name>.md` using `templates/SERVICE.md`. Keep it under two pages — a brief nobody rereads is a wasted brief.

Then offer to write a `CLAUDE.md` into that service's repo (from `templates/CLAUDE.md.template`), so future Claude Code sessions in that directory start oriented. Ask before writing into a company repo — that's a file the team will see, and the user should decide whether to commit it or keep it local.

## Rules

- Distinguish **what the code does** from **what you think it's for**. Label inference as inference.
- Record what you could *not* figure out. That list is the user's agenda for their next conversation with the service's owner, and asking specific questions makes a new hire look sharp rather than lost.
- Note the service's failure behaviour: retries, timeouts, idempotency, dead-letter handling. For anything the user later wants to add an AI call to, this is the part that decides whether it's safe.
