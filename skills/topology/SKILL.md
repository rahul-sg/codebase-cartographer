---
description: Build or refresh the cross-service topology map — which services exist, who calls whom, what events flow between them. Use when the user asks how services connect, what depends on a service, what the blast radius of a change is, or asks to refresh the service map.
---

# Cross-service topology

Goal: produce a map of the *territory between the repos*. In a 15+ service system this map usually exists nowhere, and it is the thing that makes every other question answerable.

Arguments: `$ARGUMENTS` — optionally a service name to focus on. Empty means map everything.

## Step 1 — Locate the services

Read `config/services.yaml` in the plugin's working repo. If it doesn't exist or is still the template, **stop and help the user fill it in first** — walk the sibling directories, list what look like service repos, and propose entries. Do not guess your way past this step; a wrong service inventory poisons everything downstream.

## Step 2 — Gather evidence (scripts, not reasoning)

Run `scripts/topology-scan.sh` against the configured roots. It greps for the places service-to-service edges leak into code and config:

- env vars matching `*_URL`, `*_HOST`, `*_ENDPOINT`, `*_BASE_URI`
- HTTP client construction and base URLs
- queue/topic/stream names (Kafka, SQS/SNS, RabbitMQ, Pub/Sub)
- service names in `docker-compose*.yml`, k8s manifests, Helm values, Terraform
- API gateway / ingress route definitions
- service-discovery or service-mesh identifiers

The script emits raw candidates. It does not decide anything.

## Step 3 — Interpret

This is your job, not the script's. From the candidates:

- Collapse aliases — `order-svc`, `orders`, `OrderService`, `ORDER_SERVICE_URL` are probably one node.
- Direct each edge. `A` holding `B_URL` means **A → B**. Getting direction wrong makes the map actively misleading.
- Separate **synchronous** (HTTP/gRPC) from **asynchronous** (events/queues) edges. They fail differently and matter differently; keep them visually distinct.
- Mark anything you are unsure about as `"confidence": "low"` rather than dropping it. A flagged guess is useful; a silent omission is not.

## Step 4 — Write the artifacts

Write **both**:

1. `context/services.json` — machine-readable, for other skills (including the user's own PR skill) to consume:

```json
{
  "generated": "<ISO date>",
  "services": [
    { "name": "...", "repo": "...", "purpose": "...", "owns_data": ["..."], "language": "..." }
  ],
  "edges": [
    { "from": "...", "to": "...", "kind": "http|event|db|unknown",
      "via": "POST /orders | topic:order.created", "evidence": "path:line",
      "confidence": "high|medium|low" }
  ]
}
```

2. `docs/TOPOLOGY.md` — human-readable, with a Mermaid diagram plus a short prose section per service. Keep sync and async edges as distinguishable arrow styles.

## Rules

- **Every edge carries `evidence` as `path:line`.** An unciteable edge is a guess and must be labelled `low`.
- Never invent a service that isn't in `config/services.yaml`.
- 85% correct and shipped beats 100% and unfinished — but say plainly in the doc which parts are shaky.
- On a refresh, diff against the existing `services.json` and summarise what changed. New edges appearing between services are exactly the kind of drift worth noticing.
