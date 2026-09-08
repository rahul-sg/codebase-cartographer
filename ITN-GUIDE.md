# cartographer on the iTradeNetwork OMS

Everything below is tuned to the stack described in your reference doc. Read
[WORKFLOW.md](WORKFLOW.md) for the general workflow; this is the ONG-specific
layer on top of it.

---

## Setup, start to finish

```bash
# 1. get the tool onto the work machine
git clone <your-remote> ~/tools/cartographer
cd ~/tools/cartographer && python3 tests/test_all.py     # 96 tests, ~7s

# 2. use the pre-filled config (all 25 modules, schemas, aliases already in it)
cp cartographer.itn.yaml /Users/rsengupta/projects/ong/cartographer.yaml

# 3. cross-check it against the actual repos
cd /Users/rsengupta/projects/ong
~/tools/cartographer/bin/cartographer init --root . --dir /tmp/check
diff <(grep 'name:' cartographer.yaml) <(grep 'name:' /tmp/check/cartographer.yaml)
#    init reads the real repos. Anything it finds that the pre-filled config
#    lacks is drift since the reference was written -- trust init.

# 4. build the graph
cartographer scan

# 5. read the findings before the diagram
cartographer questions
cartographer secrets
cartographer schema
```

Add to Claude Code with `claude --plugin-dir ~/tools/cartographer`.

---

## The visual map

```bash
cd /Users/rsengupta/projects/ong
cartographer ui
```

For your estate specifically, these are the views that will earn their keep:

**Data** — the one to open first. `nexus` and `common` share `cmndev`, and
this is where you see which tables that actually means, and who writes them.
With no JPA anywhere there is no other way to get this picture.

**Map, at estate level** — around 25 backend modules plus three frontends,
their hosts, topics and schemas, on one readable screen. Drill into a module
to see its packages; drill again for files; click any edge for the
`file:line` behind it. The `logistics` and `interoperability` nodes carry an
"outside the build reactor" badge.

**Flow** — trace `order`. You will see the Angular proxy prefix, the composed
Spring route (`POST /order/api/v1/purchase-orders`, not `/{id}`), the tables,
then the Kafka hop resolved from the `KafkaConstants` enum rather than from a
string literal that does not exist.

**Coverage** — your honest boundary. It lists the services whose repos you do
not have, the ColdFusion pages whose source is in no repo, the tables whose
DDL lives in the external `database-scripts-repo`, and the topic constants
nothing on disk references. Every line there is a good question for a
teammate.

**Layers** — the 3D stack. Useful once, early, to feel the shape: three
frontends above, the module band in the middle, Kafka and the MySQL schemas
below, with the legacy ColdFusion pages hanging off to one side.

**Time** — plays the last twelve months of commits per module. Worth watching
before you pick what to work on: it shows you which parts of the estate are
actually alive.

---

## What it does that a generic tool would not

Each of these exists because your stack breaks a common assumption.

### Kafka topics are constants, not strings

Nothing in ONG names a topic inline. Names are computed at runtime:

```java
topicNameCreator.createTopicName(KafkaConstants.KafkaTopicName.ORDER_SUBMITTED)
```

A tool grepping for quoted topic names finds **nothing**, and the topic list in
`ong-devops/local/docker-compose.yml` is stale relative to the code — so the
naive answer is not merely incomplete, it is wrong.

cartographer builds a constant table from every `KafkaConstants`-style enum
(including `LogisticsKafkaConstants`), then classifies each reference as
produce or consume — SpEL inside `@KafkaListener` included. Direction it cannot
determine is recorded as unknown rather than guessed, because a backwards event
edge is worse than a missing one.

```bash
cartographer topology            # event edges appear alongside HTTP
```

### Two services outside the Maven reactor

The reference's most important correction: `logistics` and `interoperability`
are real, deployed services that are **not** in the parent POM's `<modules>`
list. cartographer finds every `pom.xml`, checks reactor membership separately,
and flags the difference:

```bash
cartographer questions | grep -i reactor
```

It also separates the seven shared libraries (`gcutil`, `cache`, `kafkautil`,
`framework`, `auth`, `elasticsearch`, `misc`) from deployable services, so they
do not clutter the service map.

### No ORM, so schema comes from SQL

Zero JPA, zero MyBatis, ~2,900 `.sql` files. cartographer reads **both**
migration systems — the legacy `database/<module>/patches/*.sql` manifests and
the Flyway `db/migration/{all,dev,uat,sqe,prd}/V*.sql` trees — and records
which system declared each table.

Then it reads SQL out of the `JdbcTemplate` DAOs, **merging string literals
joined by `+` first**, because DAO SQL is assembled by concatenation far more
often than written as one literal. Per-literal parsing sees `"SELECT a "` and
never learns the table.

That produces the map nobody has written down:

```bash
cartographer schema                    # every table, its owner, cross-service access
cartographer schema T_PURCHASE_ORDER   # one table: who declares, writes, reads
```

**This is the most valuable output for your estate.** Two services on one table
are coupled no matter what the API contracts say, and with no ORM there is no
other way to see it.

Known limit: `notification` and `report` build SQL through a `QueryBuilder`
abstraction rather than literals. Those queries will be under-reported — the
reference warns about this and the tool cannot fully solve it.

### The frontends declare dependencies in `proxy.config.json`

Your Angular apps never name a backend service in code. They call path prefixes
and the proxy maps each to a host — so `proxy.config.json` is the frontend's
actual dependency declaration, more authoritative than anything grep could
infer from TypeScript.

cartographer reads all three frontends: proxy configs for the two Angular apps,
and hardcoded per-environment URLs in `src/utils/url.js` for React Native
(which has no proxy). It also records the separate hosts — `ongsqe`, `itlsqe`,
`icrsqe`, `imlsqe`, `iomsqe` — as deployment surfaces, and flags
`@itn/itn-library2` version drift (19.2609.1 vs 16.24.527) between repos.

Live ColdFusion page references are captured as `legacy-page` nodes, with a gap
noting their source is in none of the five repos.

### Schema sharing that per-module reading cannot see

`nexus` and `common` both point at `cmndev` in docker-compose. Reading either
module alone would never show it. cartographer parses compose, attributes each
schema, and flags the overlap — which is exactly the open item §2.6 asks you to
resolve.

### Credentials: flagged, never stored

Your reference deliberately never captured the value of the Confluent SASL
credential in `agent/src/main/resources/application.properties`. cartographer
follows the same rule **structurally**, not by good intentions: the matched
value is never written to the graph, never returned, never logged. Only file,
line, and secret *kind* are recorded, plus a salted fingerprint so repeat
findings are recognisable.

```bash
cartographer secrets
```

There is a test that reads `graph.db` as raw bytes and asserts the fixture
secrets are absent. If that ever regresses, the suite fails.

### Spring routes compose from two annotations

Your controllers put the prefix on the class and the rest on the method:

```java
@RestController
@RequestMapping("/order/api/v1/purchase-orders")
public class OrderController {
    @GetMapping("/{id}") ...
```

Reading method annotations alone yields `/{id}`. cartographer composes both
into `GET /order/api/v1/purchase-orders/{id}` across all ~434 controllers.

### Findings attributed per module, not per repo

`ong-server-repo` holds 25 Maven projects. Attributing everything to the repo
would make every schema look shared and every finding useless. Each file is
resolved to its owning module first.

---

## Suggested first week

**Day 1 — scan and read.** Run the setup above. Read `cartographer questions`.
Bring three or four to your onboarding buddy. Good candidates from your stack:

- Which service owns the tables in `cmndev`, given `nexus` and `common` share it?
- How are `logistics` and `interoperability` built, since the reactor skips them?
- Is the hardcoded Confluent credential in `agent` known, and is there a secrets manager?
- Where does the DDL for `nexus`, `integration`, `inventory`, `contract`, `transform` live? (§2.5 points at an external `database-scripts-repo` — if you can clone it, add it under `roots:` and the schema map completes.)

**Day 2 — ask about the APM.** Datadog, New Relic, Jaeger, Tempo, Honeycomb,
or a bare OTel collector. An exported service graph is measured truth, and it
will reveal the services whose repos you do not have. One Slack message,
probably the highest-value thing in this guide:

```bash
cartographer scan --trace ~/exports/servicegraph.prom
```

**Day 3–5 — trace one journey.** `/cartographer:trace-flow "buyer submits a
purchase order"`. Follow it through `order` → `catalog` → the Kafka event →
`notification`. This one document will teach you more than a week of reading,
and it is the artifact the next new hire will want.

**Then — resolve one open item from the reference.** `FlywayMigrationConfig.java`
(§2.5) settles the biggest unknown in the backend in a single file read. Doing
that and writing up the answer is a genuinely useful first contribution that
needs no domain tenure.

---

## Your AI/ML angle

Run `/cartographer:ai-surface` once the graph exists. On this codebase it will
find `agent` (OpenAI SDK, Vertex AI, Google ADK), the Spring AI MCP servers in
`company` and `misc`, and `nexus`'s rules engine.

The more useful half is where AI could go next. `cartographer hotspots` finds
churn × complexity, which is a good proxy for hand-maintained rule logic — the
EDI/exception paths in `transform` and `integration`, and the addendum handling
in `agent`, are the obvious candidates in an OMS.

Before proposing anything, use `blast_radius` to check whether the target sits
on a synchronous request path. A two-second model call is fine in a Kafka
consumer and fatal in order submission.

And note what the tool will tell you about data: `cartographer schema` shows
which tables hold customer, pricing and contract data, and which services touch
them. **Flag anything regulated or contractually constrained as a question for
your team — never assume data is safe to send to an external model.** With a
hardcoded external-Kafka credential already in the codebase, that caution is
warranted rather than theoretical.

If there is no eval infrastructure anywhere, say so early. That is the highest-
value early contribution an AI/ML engineer can make and it does not require
deep domain tenure.

---

## What it still will not know

Stated plainly so you never over-claim from its output:

- **ColdFusion source.** Referenced, live, in none of your repos.
- **The external `database-scripts-repo`.** Five modules' DDL lives there.
- **SQE/UAT/PRD config.** Only local `docker-compose` and source defaults are read; Helm and K8s overrides are not.
- **`QueryBuilder`-assembled SQL** in `notification` and `report` — under-reported.
- **Runtime behaviour** — reflection, DI wiring, the service mesh, the API gateway. Traces are the answer to all of these.
- **What `IML`, `pimi` and `pw` stand for.** Unresolved in the reference and unresolvable from code.

When the tool says "no consumer found", the honest phrasing is *"no consumer
among the repos I can see."* On an estate where you have five of fifteen-plus
repos, that distinction is the difference between a useful finding and a wrong
claim in front of your team.
