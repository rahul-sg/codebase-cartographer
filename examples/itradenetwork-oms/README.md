# Example: a large Java/Angular microservice estate

A worked configuration for one specific workspace — a Maven multi-module
Spring backend plus three frontends. Useful as a template even if it is not
your estate, because the shapes it handles are common:

- services deliberately excluded from the parent Maven reactor
- Kafka topic names computed at runtime from enum constants
- SPA `proxy.config.json` as the frontend's real dependency declaration
- two migration systems side by side (legacy patch-lists and Flyway)
- one schema shared by two services
- hand-written JDBC DAOs, no ORM anywhere
- a live legacy application whose source is in none of the repos

**This directory contains internal names for one organisation.** If you fork
or share this tool, delete it — nothing outside this folder depends on it.

`GUIDE.md` is the day-to-day workflow for that estate.
`cartographer.yaml` is a pre-filled config to copy to the workspace root.
