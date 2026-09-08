---
name: ai-surface
description: Map where AI/ML already lives in the codebase, where it could plausibly go, and what constrains it — latency, data availability, sensitivity, and evaluation. Use when the user asks where AI could be integrated, scopes an ML feature, or wants to know what AI already exists.
---

# AI/ML integration surface

The map only the AI/ML engineer on the team needs: what AI exists here, where
more could go, and what would stop it.

## Part 1 — what exists

Search all repos for model serving, inference calls, LLM SDK usage, vector
stores, embeddings, feature pipelines, training code, notebooks, model
registries, prompt templates, eval harnesses, golden datasets, shadow-mode or
A/B infrastructure.

Separate what is wired into production from what is abandoned. Use
`mcp__cartographer__hotspots` and `who_owns`: a model directory untouched for
two years is archaeology, not infrastructure.

## Part 2 — where AI could go

Look for **rule-based logic that is expensive to maintain**: long conditional
chains, hand-tuned thresholds, mapping tables, manual review queues, comments
admitting a heuristic. `mcp__cartographer__hotspots` finds these directly —
high churn plus high complexity is often exactly this.

Recurring candidates in an order management system: partner SKU and catalog
matching, document/EDI parsing and its exception queues, pricing and
substitution recommendations, demand and delivery forecasting, anomaly
detection on orders and invoices, routing exceptions to the right human.

For each: current implementation at `file:line`, why it is a candidate, and
what a realistic first version looks like.

## Part 3 — constraints (do this part carefully)

An integration point is worthless if you cannot ship there.

- **Latency budget** — call `blast_radius` to see whether it sits on a
  synchronous request path. A 2-second model call is fine in a queue consumer
  and fatal in an order-submission handler.
- **Failure tolerance** — what happens when the model is unavailable or wrong;
  is there a fallback to the existing logic?
- **Data availability** — what exists, where, at what volume, what is labelled
  or labellable from historical outcomes.
- **Data sensitivity** — customer, pricing and contract data carry
  restrictions. **Flag anything that looks regulated or contractually
  constrained as a question for the user to confirm with their team. Never
  assume data is safe to send to an external model.**
- **Eval story** — how would you know it works? If there is no way to measure
  it, that is the actual first task.

## Output

`docs/AI_SURFACE.md`, three parts as above, with a ranked shortlist at the top:
candidates ordered by value × feasibility, each with a one-line justification.

## Rules

- Be honest about feasibility. A candidate with no data and no eval path is a
  research project, not a roadmap item — say so.
- If no eval infrastructure exists anywhere, call that out prominently. It is
  usually the highest-value early contribution an AI/ML engineer can make, and
  it does not require deep domain tenure.
