---
description: Map the AI/ML surface area of the codebase — existing models, prompts, evals, feature pipelines, plus where AI could plausibly be integrated and what the constraints are. Use when the user asks where AI/ML lives, where an AI feature could go, what data is available, or is scoping an AI integration.
---

# AI/ML integration surface

This is the map only the AI/ML engineer on the team needs. It answers: what AI already exists here, where could more of it go, and what would stop me.

Arguments: `$ARGUMENTS` — optionally a service or capability to focus on.

## Part 1 — What already exists

Search across all configured repos for:

- model serving, inference calls, LLM SDK usage, vector stores, embedding generation
- prompt templates and anywhere prompts are assembled from user or record data
- feature pipelines, training code, notebooks, model registries
- eval harnesses, golden datasets, offline scoring, A/B or shadow-mode infrastructure

Report what's actually wired into production versus what's abandoned or experimental. Check `git log` recency — a model directory untouched for two years is archaeology, not infrastructure.

## Part 2 — Where AI could go

Look for **rule-based logic that is expensive to maintain**: long conditional chains, hand-tuned thresholds, mapping tables, manual review queues, anything with a `# TODO: this is a heuristic` energy. In an order management system the recurring candidates are:

- order/product matching and catalog normalization across trading partners
- document and EDI parsing, and the exception queues that catch failures
- pricing, substitution, and shortage recommendations
- demand and delivery forecasting
- anomaly detection on orders, invoices, and settlements
- routing and triage of exceptions to the right human

For each candidate record: current implementation (`file:line`), why it's a candidate, and what a realistic first version would be.

## Part 3 — Constraints (do this part carefully)

An integration point is worthless if you can't actually ship there. For each candidate note:

- **Latency budget** — is this on a synchronous request path or a background job? A 2-second model call is fine in a queue consumer and fatal in an order-submission handler.
- **Failure tolerance** — what happens if the model is unavailable or wrong? Is there a fallback to the existing logic?
- **Data availability** — what data exists, where, at what volume, with what retention. What is labelled, or labellable from historical outcomes.
- **Data sensitivity** — customer, pricing, and contract data all carry restrictions. Flag anything that looks regulated or contractually constrained. **Do not assume something is safe to send to an external model; flag it as a question for the user to confirm with their team.**
- **Eval story** — how would you know it works? If there's no way to measure it, that's the actual first task.

## Output

Write `docs/AI_SURFACE.md`, structured as the three parts above, with a ranked shortlist at the top: candidates ordered by (value × feasibility), each with a one-line justification.

## Rules

- Be honest about feasibility. A candidate with no data and no eval path is a research project, not a roadmap item — say so.
- If no eval infrastructure exists anywhere, call that out prominently. It is usually the highest-value early contribution an AI/ML engineer can make, and it's a contribution that doesn't require deep domain tenure.
- Keep this document current — it's the user's argument for what to work on next, and stale evidence undermines it.
