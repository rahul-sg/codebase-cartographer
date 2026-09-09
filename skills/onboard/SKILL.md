---
name: onboard
description: Set up or refresh the codebase map for this machine — discover repos, build the graph, and report what was found. Use when the user first installs cartographer, mentions the map is stale or empty, asks to scan or re-scan the codebase, or when another cartographer tool reports no graph.
---

# Set up the map

The graph is a local SQLite file built from the repos on this machine. Nothing
leaves the machine and nothing is sent anywhere.

## First run

1. **Find the repos.** Ask where the service repos live if it is not obvious
   (a directory that *contains* the repos, not one repo).
2. `cartographer init --root <dir>` — writes `cartographer.yaml` listing every
   repository it found.
3. **Fix the aliases.** This is the step that matters most and the one people
   skip. The same service is written many ways — `order-svc`, `OrderService`,
   `ORDER_SERVICE_URL`, `acme/order-svc:latest`. Every alias you add to
   `cartographer.yaml` sharpens the whole map. Read the config with the user
   and fill in `aliases`, `purpose` and `owns_data` for anything they know.
4. `cartographer scan` — builds the graph and writes the reports.
5. `cartographer doctor` if anything looks wrong.

## Runtime traces — ask about this

Ask whether the team runs an APM: Datadog, New Relic, Jaeger, Grafana Tempo,
Honeycomb, or an OpenTelemetry collector. If so, an exported service graph is
**the strongest evidence available** — measured traffic rather than inferred
config. It also reveals services whose repos are not on this machine.

Accepted exports: OTel `servicegraph` metrics, Jaeger/Zipkin dependencies JSON,
or a `source,target,calls` CSV. Feed them in with
`cartographer scan --trace <file>`, or list them under `traces:` in the config.

## Refreshing

`cartographer scan` is incremental in effect — each extractor replaces its own
contribution, so re-running is always safe. Re-scan after pulling a lot of new
code, or weekly.

## Reporting back

Summarise: repos and services found, how many edges and how many are `INFERRED`,
and the gap count. Then show the user `cartographer questions` — the things the
scan could not determine are the highest-value things to ask their team.

Do not present the map as complete. Say plainly which repos are on disk and
that services outside them can only appear through traces or config references.
