# Cell-Based Architecture — The Pattern Temporal Cloud Is Built On

**Why this matters.** Temporal Cloud is not one big Temporal cluster. It is many complete, independent copies of Temporal — *cells* — with a thin layer in front that sends each Namespace to exactly one of them, and a control plane beside them that creates, fills, moves, and deletes them. Temporal says so in its own docs: "Temporal Cloud uses a cell-based architecture to achieve isolation and scalability," and "Cells are failure domains. When infrastructure inside a cell degrades, only the Namespaces in that cell are affected, and cells scale independently of each other" ([Temporal Cloud overview](https://docs.temporal.io/cloud/overview)). If you work on cell infrastructure, this pattern is not background reading — it is the shape of everything you will build. Guide [12](12-cell-lifecycle-synthesis.md) is about the *lifecycle* of one cell: the dependency DAG, the bootstrap paradoxes, upgrade, teardown. This guide steps back to the *architecture*: why cells, how traffic finds the right one, how tenants are placed and moved, how you deploy to N of them, what you are allowed to share between them, and the specific ways cell-based systems fail. It is also the vocabulary for any design review about routing, placement, or failover.

**On sourcing.** Every quote about Temporal Cloud comes from Temporal's public docs and engineering blog, linked inline. Where I go beyond what Temporal has published, I label the claim *inference* or *industry pattern*. I have no internal Temporal information. Verified against primary sources on **2026-10-04**.

---

## The mental model

Six ideas. Every section below unpacks one of them.

**1. A cell is a complete copy of the product, not a slice of it.** A shard splits *data* inside one system. A cell copies the *whole system* — compute, database, search index, load balancers, certificates, observability — and gives each copy a subset of the tenants. Microsoft's name for the same pattern says it well: "Deploy multiple independent copies of application components, including data stores, as a single group of resources. Each copy is called a *stamp*, or sometimes a *service unit*, *scale unit*, or *cell*" ([Azure Deployment Stamps](https://learn.microsoft.com/en-us/azure/architecture/patterns/deployment-stamp)). The test is simple: if you deleted every other cell, would this one still serve its tenants? If yes, it is a cell.

**2. You buy blast-radius reduction and pay for it in operational multiplication.** With N equally loaded cells, a failure that takes out one cell hits about 1/N of tenants. The price is that every task — deploy, patch, rotate a cert, upgrade Kubernetes — now happens N times. A cell architecture is only a good deal if the per-cell cost of those tasks goes toward zero. That is what a cell-infrastructure team actually ships: **automation that makes N cells cost about as much attention as one.**

**3. The router is the one part you could not make cellular, so it has to be boring.** Something has to see every request to send it to the right cell. That component's blast radius is global. AWS's rule is to keep it "as simple and horizontally scalable as possible, which necessitates avoiding complex business logic within this layer" ([AWS — Cell routing](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-routing.html)). Rule of thumb: **anything that spans cells must be much simpler than anything inside a cell.**

**4. The data plane must keep working when the control plane is down.** The control plane makes changes: create a cell, place a Namespace, move it, delete it. The data plane runs Workflows. AWS calls the required property *static stability*: "In a statically stable design, the overall system keeps working even when a dependency becomes impaired" ([Amazon Builders' Library — Static stability](https://aws.amazon.com/builders-library/static-stability-using-availability-zones/)). If your control plane has an outage, nobody should be able to tell — except that new Namespaces wait.

**5. Placement and migration are the real product.** Splitting tenants across cells is easy on day one. The hard part is everything after: deciding where a new tenant goes, noticing a cell is getting full, moving a tenant that outgrew its cell, emptying a cell you need to retire — all while that tenant's Workflows keep running. AWS: "Stateful cell-based architectures will almost certainly require online cell migration" ([AWS — Cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html)).

**6. Every shared thing quietly undoes the pattern.** A shared database, a shared CA, a shared container registry, a shared DNS zone, a shared deploy pipeline that pushes to every cell at once — each is a failure domain wider than a cell. Some are unavoidable. The discipline is to list them, decide for each whether it sits on the *request path* or only the *control path*, and make sure cells keep serving when it is down.

---

## Core concepts

### The three parts, and what Temporal Cloud maps to each

AWS's whitepaper splits every cell-based system into three parts ([What is a cell-based architecture?](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/what-is-a-cell-based-architecture.html)). Here they are, lined up with what Temporal has said publicly.

| Part | AWS definition | Temporal Cloud (public) | Source |
|---|---|---|---|
| **Cell** | "A complete workload, with everything needed to operate independently" | "Dedicated cloud account and VPC; Kubernetes cluster running Temporal services; Primary database with synchronous replication across three availability zones; Elasticsearch for Workflow visibility and search; Load balancers and ingress management; Observability and certificate infrastructure" | [Cloud overview](https://docs.temporal.io/cloud/overview) |
| **Cell router** | "The thinnest possible layer, with the responsibility of routing requests to the right cell, and only that" | Namespace endpoints (`<namespace>.<account>.tmprl.cloud:7233`) and regional endpoints; on failover, "Temporal Cloud ... updates the CNAME to point at the new active region. The DNS TTL is 15 seconds" | [Namespaces](https://docs.temporal.io/cloud/namespaces), [HA — how it works](https://docs.temporal.io/cloud/high-availability/how-it-works) |
| **Control plane** | "Provisioning cells, de-provisioning cells, and migrating cell customers" | "Handles provisioning, configuration, and lifecycle operations." When you create a Namespace "it selects a cell, provisions resources, generates certificates, and configures ingress routes." And: "The control plane runs those steps on Temporal itself." | [Cloud overview](https://docs.temporal.io/cloud/overview) |

Three things are worth noticing.

- **The cell boundary is a cloud account.** Not a Kubernetes namespace, not a node pool — an account, with its own VPC and its own cluster ([Building durable cloud control systems with Temporal](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal), Jan 2025). That is the strongest isolation the clouds offer. It separates IAM, API rate limits, service quotas, and billing per cell. The cost is that every cell needs its own account vending, its own guardrails, and its own teardown story — which is guide [03](03-multicloud-aws-gcp-azure.md) and guide [08](08-terraform.md) territory.
- **Inside the cell, Temporal itself is still sharded.** History shards, Matching partitions, and the Ringpop membership ring all live *inside* one cell (guide [16](16-temporal-server-internals.md)). Shards give you throughput; the cell gives you isolation. Do not confuse the two (guide [12](12-cell-lifecycle-synthesis.md) has a full comparison table).
- **The control plane is built from Temporal.** Each cell has "an entity workflow that manages its lifecycle, from provisioning to upgrades," and there are two control planes — a User CP that handles resources "logically" and an Infra CP that handles them "physically" — both themselves Temporal Namespaces ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). *Inference:* those control-plane Namespaces must live somewhere, which means some cell (or a dedicated, specially-protected deployment) hosts the system that manages all cells. Where that lives, and what happens to it during a bad deploy, is a very good week-one question.

### Where the pattern comes from

The idea is old; the names are new. Knowing the lineage helps in design discussions, because people often reach for one of these as the reference point.

| Who | What they call it | What a cell is there | Why they did it | Source |
|---|---|---|---|---|
| AWS | Cell-based architecture | A full copy of a service, behind a thin router | Limit the scope of impact of any single failure or deploy | [AWS whitepaper](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/what-is-a-cell-based-architecture.html) |
| Microsoft Azure | Deployment Stamps | "Stamp, service unit, scale unit, or cell" | Scale limits, tenant separation, staged updates, data residency | [Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/patterns/deployment-stamp) |
| Shopify | Pods | A full copy of the stack for a set of shops, with its own MySQL | Scale past one database; move a pod to another data center "in a minute" | [Shopify Engineering](https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale) |
| Slack | Cellular architecture | **One availability zone**: "each service only communicates with services within its AZ" | A June 30, 2021 AZ network problem caused errors everywhere; they wanted to be able to drain one AZ | [Slack Engineering](https://slack.engineering/slacks-migration-to-a-cellular-architecture/) |
| Roblox | Cells | About 1,400 machines; "more than 70 percent of our back-end service traffic" served from cells at peak (Dec 2023) | After a 73-hour outage in October 2021 | [Roblox](https://about.roblox.com/newsroom/2023/12/making-robloxs-infrastructure-efficient-resilient) |
| Temporal Cloud | Cells | Per-cloud-account copy of Temporal plus its databases | Multi-tenancy without shared fate | [Temporal Cloud overview](https://docs.temporal.io/cloud/overview) |

Note the important split: **Slack's cell is an AZ; Temporal's cell spans AZs.** Temporal's docs say "components within a cell are distributed across at least three Availability Zones" ([RPO/RTO](https://docs.temporal.io/cloud/rpo-rto)), and the database replicates synchronously across three AZs. So in Temporal Cloud, an AZ failure is handled *inside* a cell with no failover, and a cell failure is handled by *failing over to another cell*. AWS's whitepaper calls these two choices "AZ independence" vs. "non-AZ independence" for cells. Be ready to explain why Temporal picked the second: Temporal's correctness depends on a strongly consistent database for History (guide [16](16-temporal-server-internals.md)), and one AZ cannot hold that database's quorum by itself.

### Choosing the partition key

The router needs a *partition key* in every request to decide which cell it goes to. AWS: keys "must be chosen to match the *grain* of the service, or the natural ways that a service's workload can be subdivided with minimal cross-grain interactions. A good partition key is one that is easily accessible in most API calls" ([AWS — Cell partition](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-partition.html)).

For Temporal, the Namespace is an almost perfect key:

- **It is in every call.** Every Temporal API request carries a Namespace, and the Namespace endpoint even puts it in the hostname — so the router can decide before it decodes a single byte of gRPC (more on SNI below).
- **Almost nothing crosses it.** Workflows, Task Queues, Schedules, and Visibility queries all live inside one Namespace. Cross-Namespace calls exist ([Nexus](https://docs.temporal.io/nexus) is the explicit, supported way to make them), but they are a small fraction of traffic and go through a defined endpoint rather than a database join.
- **It is already the isolation unit customers think in.** Temporal's docs describe a Namespace as "a unit of isolation within Temporal Cloud" ([Namespaces](https://docs.temporal.io/cloud/namespaces)).

The weak spot AWS warns about is the *very large tenant*: "if you choose the `CustomerID` for your partition key and a single customer of yours becomes so big that it doesn't fit into a single cell anymore," you need a second dimension. For Temporal, the natural answer is that the customer splits their work across multiple Namespaces, and one Namespace has a hard ceiling set by its cell. *Inference:* that ceiling is why Temporal Cloud enforces per-Namespace limits — a Namespace must always fit inside one cell's measured capacity.

**Cross-grain operations** — work that has to touch many cells — are unavoidable: billing roll-ups, fleet-wide usage reports, "list all my Namespaces," and admin search. AWS's advice: "instead of letting the cells talk directly to each other, any cross-cell calls have to go back through the normal cell router." The Azure version: have every stamp publish into a central warehouse and query that, instead of querying each stamp live. Either way, the rule is: **no cell ever calls another cell directly.**

### Mapping keys to cells

Once you have a key, you need a function from key to cell. AWS lists four ([cell partition](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-partition.html)):

| Algorithm | How it works | Good at | Bad at |
|---|---|---|---|
| **Full mapping** | A table: every key → its cell | Total control. Move any one key. Put a heavy tenant on a quiet cell | A table you must read on every request and write on every placement. AWS: "a critical read and write dependency on the mapping table, a read-your-writes consistency requirement, and a large amount of state" |
| **Prefix / range** | Key ranges → cells | Compact; easy to split a hot range | Hot ranges; keys must have useful ordering |
| **Naive modulo** | `hash(key) % N` | No state at all | Adding one cell remaps almost every key — a mass migration |
| **Consistent hashing** | Keys and cells on a ring | Adding a cell moves only about 1/N of keys | You cannot place one specific tenant where you want it |

AWS adds a warning that applies to all of them: "Regardless of partition mapping approach, it's important to also use an override table to force specific keys to specific cells ... useful for testing, quarantining, and special-case routing for particularly heavy partition keys" ([AWS — warning for all mapping approaches](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/a-warning-for-all-mapping-approaches.html)).

**What Temporal Cloud appears to use is full mapping, stored in DNS.** *Inference from public behavior:* each Namespace gets its own hostname, the control plane chooses its cell when the Namespace is created, and failover works by "updat[ing] the CNAME." A per-Namespace CNAME *is* a full-mapping table — one row per key — and DNS is its globally replicated, heavily cached, read-optimized storage. This is a clever choice for three reasons:

1. **Reads never touch your own infrastructure.** Resolution happens in the client's resolver and in DNS caches. The "critical read dependency" AWS warns about is outsourced to the most battle-tested distributed cache on the internet.
2. **It is statically stable by default.** If the control plane that writes records is down, every existing record keeps resolving. Only *changes* stop.
3. **Moving a tenant is a single record change.** That is the "redirect" step of a migration.

The cost is also clear: DNS gives you eventual consistency bounded by TTL (15 seconds here, per [the docs](https://docs.temporal.io/cloud/high-availability/how-it-works)), and you cannot fully control clients that cache longer than the TTL. Long-lived gRPC connections are also not re-resolved just because DNS changed — an already-open HTTP/2 connection to the old cell stays open until something closes it. That is why failover is described as clients converging "within about 30 seconds" rather than instantly, and why the old active side has to actively push clients away rather than just wait.

### The cell router: design choices

AWS lists the router properties that matter: "Be simple as possible, but not simpler. Have request dispatching isolation between cells. Minimize the amount of business logic in this layer. Abstract underlying cellular implementation and complexity from clients. Fast and reliable. Continue operating normally in other cells even when one cell is unreachable" ([AWS — Cell routing](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-routing.html)).

There are four common ways to build one. Real systems often stack two.

| Router style | How it routes | Strengths | Weaknesses | Where Temporal fits |
|---|---|---|---|---|
| **DNS** | Hostname per tenant (or per cell) resolves to the cell's load balancer | No hop in the data path; scales without limit; statically stable | TTL-bounded changes; clients that ignore TTL; no per-request logic | Namespace endpoints |
| **L7 proxy** (Envoy, API gateway) | Proxy reads a header or path, looks up the cell, forwards | Instant changes; per-request decisions; can drain by weight | A hop in every request; the proxy fleet is itself a global failure domain | Slack drains AZs this way through Envoy ([Slack](https://slack.engineering/slacks-migration-to-a-cellular-architecture/)) |
| **L4 / SNI routing** | TLS ClientHello's SNI picks the backend; the router never decrypts | No TLS termination in the router; works for any protocol over TLS, including gRPC | Only sees the hostname; mTLS must be terminated by the cell | *Inference:* the regional endpoint docs say clients must set `server_name` to the Namespace endpoint "since the request to the regional endpoint is redirected to the specific Namespace" — SNI is the key ([Namespaces](https://docs.temporal.io/cloud/namespaces)) |
| **Client-side** | A smart client asks "where is tenant X?" once, then talks to the cell directly | No hop; the client can retry another cell | You ship router logic into every SDK in every language; very hard to change | Not used for Temporal's customer path, as far as public docs show |

The last row is why Temporal's approach matters: Temporal has SDKs in many languages, and none of them needs to know cells exist. AWS lists that as a router goal — "abstract underlying cellular implementation and complexity from clients" — and DNS plus SNI achieves it with no SDK code at all.

**The router has its own blast radius, so make it cellular too.** AWS: "the only component that has the shared state of all cells is the cell router. It presents itself as a single point of failure. Therefore, it is essential that it be built with maximum reliability and also as a cellular component" ([AWS — router resilience](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/about-resilience-of-the-cell-router.html)). In practice that means:

- **Isolate dispatch per cell (bulkheads).** If cell 7 becomes slow, requests to it pile up. A router with one shared worker pool will fill that pool with cell-7 requests and starve every other cell. Give each cell its own concurrency budget and fail fast when it is full. The hands-on lab below shows exactly this.
- **Serve from last-known-good.** The router loads its mapping, validates it, and keeps the previous one if the new one is missing, empty, unparseable, or *older*. Never let a bad mapping push replace a good one.
- **Deploy the router in waves, separately from cells.** A router bug is the one bug that can take down every cell at once. It deserves the slowest, most careful rollout in the company.

### Control plane, data plane, and static stability

The Builders' Library definitions: the control plane is "the machinery involved in making changes to a system — adding resources, deleting resources, modifying resources," and the data plane is "the daily business of those resources, that is, what it takes for them to function" ([Static stability](https://aws.amazon.com/builders-library/static-stability-using-availability-zones/)). Temporal's version: the data plane is "where your Workflows run," and the control plane "handles provisioning, configuration, and lifecycle operations" ([Cloud overview](https://docs.temporal.io/cloud/overview)).

Static stability is a set of design rules, not a slogan. Check each one against a cell:

| Rule | What it means for a Temporal cell | How it breaks |
|---|---|---|
| **Pre-provision; do not react** | Each cell runs with enough headroom to absorb its own AZ loss without launching anything. AWS's example: with three AZs "we overprovision by 50 percent," so each AZ runs at about 66% | Relying on Karpenter (guide [06](06-karpenter.md)) to add nodes *during* an AZ outage — when the cloud's EC2 API is most likely to be degraded |
| **No control-plane call on the request path** | Starting a Workflow must never wait on the control plane, the account system, or a cloud API | A Frontend that checks a central "is this Namespace allowed?" service on each request |
| **Cache what you got, keep using it** | Certificates, mapping, config, and secrets are loaded and held; refresh failures are logged, not fatal | A sidecar that crashes the pod when Vault (guide [10](10-vault.md)) is unreachable at refresh time |
| **Expiry is the hidden dependency** | Anything with a TTL — a cert, a token, a DNS record, a lease — turns "the control plane is down" into "the data plane will be down in N hours" | Short-lived certs whose issuer lives outside the cell (guide [11](11-cert-manager-and-pki.md)) |
| **The data plane must not need the control plane to restart** | A pod that restarts during a control-plane outage must come back with what is already in the cell | An image pulled only from a registry outside the cell, with no in-cell mirror or node cache |

The fourth row is the one experienced people still miss. Write down, for every credential and lease in a cell, *how long the cell survives if the thing that refreshes it disappears.* That number is the cell's real independence, and it should be days, not minutes.

### Blast radius math: cells, replicas, and shuffle sharding

**Plain cells.** With N equal cells and each tenant in exactly one, a whole-cell failure affects 1/N of tenants. With 20 cells in a region, that is 5%. That is the whole pitch of the pattern, and it is a big improvement over 100%.

**Cells with replicas.** Temporal's High Availability Namespaces add a *replica* in a second place. With Same-region Replication, "Temporal operates a 'cell architecture' and will replicate the Namespace across multiple cells in that region" ([HA docs](https://docs.temporal.io/cloud/high-availability)). Now a tenant lives on a *pair* of cells (active + replica). A single cell failure no longer causes an outage for HA Namespaces; they fail over, with Temporal publishing "Under 1 minute" RPO and "Under 20 minutes" RTO for a cell outage ([RPO/RTO](https://docs.temporal.io/cloud/rpo-rto)). The new risk is losing *both* cells of a pair at once. If pairs are spread evenly over N cells, there are C(N,2) possible pairs — 190 for N = 20 — so a double failure of two particular cells hits only the tenants on that one pair: about 1/190, or roughly 0.5%.

**Shuffle sharding.** This is the same idea taken further. Instead of one cell per tenant, give each tenant a random *combination* of k workers out of n. Colm MacCárthaigh's original post compares dealing hands from a deck of cards: with 8 workers and hands of 2, there are far more distinct hands than there are plain shards, so a "poison" tenant that kills its 2 workers almost never takes out another tenant's entire hand ([AWS Architecture Blog, 2014](https://aws.amazon.com/blogs/architecture/shuffle-sharding-massive-and-magical-fault-isolation/)). Do the math yourself, because the numbers are the argument:

- n = 100 workers, k = 5 per tenant gives C(100,5) = **75,287,520** distinct hands.
- If one tenant destroys all 5 of its workers, the chance that a random other tenant had the *same* 5 is about **1 in 75 million**.
- 77% of other tenants share **zero** workers with it; 21% share exactly one; fewer than 2% share two or more. With retries across the hand, a tenant that keeps even one healthy worker stays up.
- Route 53 gives each hosted zone four name servers. If those were drawn from a pool of 2,048, C(2048,4) ≈ **7.3 × 10¹¹** combinations — which is why per-customer DNS isolation is practical at that scale.

**How these relate.** Cells are *hard* isolation: separate accounts, separate databases. Shuffle sharding is *statistical* isolation on shared machines. They stack: you can shuffle-shard *inside* a cell (for example, which Matching hosts serve which Task Queue partitions) while the cell is the hard wall around it. In a design review, the strong answer is: "Cells cap the worst case at 1/N. Shuffle sharding makes the *expected* overlap between two tenants close to zero. Use cells for the faults you cannot reason about — bad deploys, data corruption, quota exhaustion — and shuffle sharding for noisy neighbors on shared fleets."

### Cell sizing, revisited as a capacity vector

Guide [12](12-cell-lifecycle-synthesis.md) covers AWS's three forces: big enough for the largest tenant, small enough to test at full scale, big enough for economies of scale. One more thing to add: **a cell's capacity is a vector, not a number.** A Temporal cell runs out of different things for different tenants:

| Dimension | What exhausts it | Where it is covered |
|---|---|---|
| History shard throughput | Many workflows with heavy event histories | Guide [16](16-temporal-server-internals.md) |
| Persistence IOPS / write bandwidth | Write-heavy tenants; large payloads | Guide [16](16-temporal-server-internals.md) |
| Visibility indexing | High workflow start/close rate; custom search attributes | Guide [16](16-temporal-server-internals.md) |
| Long-poll connections | Many Workers per Task Queue | Guide [15](15-temporal-programming-model.md), guide [02](02-grpc.md) |
| Pod IPs / subnet space | Cell growth, surge during upgrades | Guide [05](05-cni-and-host-networking.md) |
| Cloud quotas in the cell's account | Instances, load balancers, endpoints, KMS calls | Guide [03](03-multicloud-aws-gcp-azure.md) |

The placement engine must consider all of them. A cell that is 30% full on CPU and 95% full on persistence IOPS is *full*. *Industry pattern:* the usual approach is to express each tenant's expected load and each cell's measured limit along the same dimensions, then place against the tightest one — a multi-dimensional bin-packing problem with a headroom reserve held back for spikes and for failover.

### Placement and capacity management

Placement is the control-plane decision "which cell does this new Namespace go to?" Temporal's public description of Namespace creation starts with exactly that: "selecting a suitable cell within the chosen region" ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). A production-grade placement service, as an *industry pattern*, handles:

- **Hard constraints first.** Region and cloud (the customer chose them). Compliance or tier requirements. For HA Namespaces, **anti-affinity**: the replica must be in a different cell — and for Multi-region or Multi-cloud Replication, a different region or cloud. Temporal's docs add that the multi-region replica "must be on the same continent as the primary region" ([HA docs](https://docs.temporal.io/cloud/high-availability)).
- **Headroom, not just free space.** A cell that hosts replicas must be able to take *their* active load after a failover. If cell A holds 40 replicas whose actives are spread across 10 other cells, losing one of those cells moves about 4 tenants' load onto A. Plan for that, or failover becomes the second outage.
- **High- and low-water marks.** Stop placing new tenants on a cell at, say, 70% of its tightest dimension; start moving tenants away at 85%. The exact numbers are yours to measure, not copy.
- **"No capacity" is a valid answer.** If no cell in the region has room, the system must say so clearly and trigger a new cell. It must not hand back a slow timeout. Provisioning a new cell takes time (guide [12](12-cell-lifecycle-synthesis.md) walks the whole DAG), so cell creation has to be driven by a **forecast**, not by the first failed placement.
- **Dedicated cells.** Azure's guidance covers this case: "Some large customers might need their own independent instances of your solution." The same placement machinery can put one tenant alone on a cell. The cell is the same; only the placement policy differs.

### Migration: moving a tenant between cells

AWS gives the four generic phases ([Cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html)):

1. **Clone** "the data from the current location into the new location, as a non-authoritative copy."
2. **Flip** "the new location copy to be *authoritative*."
3. **Redirect** "from old location to new location."
4. **Forget** "the data from the old location."

For Temporal, the striking thing is that **the High Availability feature already contains all four steps.** *Inference, but a strong one:* adding a replica in another cell is *Clone* — "Workflow Executions are asynchronously replicated from an active Namespace to a replica" ([Temporal blog, Mar 2025](https://temporal.io/blog/expanding-temporal-clouds-high-availability-offerings)). A failover is *Flip* — "The replica becomes active, and the former active becomes a replica." The CNAME update is *Redirect*. Removing the old replica is *Forget*. In the open-source server this is the multi-cluster replication path of global Namespaces: the active/standby task executors and version histories described in guide [16](16-temporal-server-internals.md). So the same machinery serves customer-facing HA, cell draining, rebalancing, and moving a tenant to a dedicated cell. That is a big deal for an infra team: **migration is not a special project; it is a routine operation built on a product feature** that already gets tested every day.

What makes migration hard, in any cell system:

- **The in-between state.** During migration, some requests may arrive at the old cell. AWS: this "may involve cross-cell redirects, performing multiple iterations of the mapping algorithm when necessary, or both." Decide ahead of time which side is authoritative at every moment, and make the other side refuse writes or forward them.
- **Replication lag at flip time.** A *graceful* flip waits for the replica to catch up before switching. A *forced* flip during an outage switches anyway and accepts that the last few seconds may be missing — that is what "Under 1 minute" RPO means. Know which kind you are running before you press the button.
- **Old connections.** As covered above, open gRPC connections do not move just because DNS changed. The old side must close or reject them, and clients must retry.
- **Forget is the dangerous step.** It is the only irreversible one. Gate it on verification (record counts, last event IDs, a bake period), never on a timer alone.

**Draining a whole cell** is then just "migrate every tenant off it, in a controlled order." Shopify's version: "evacuating a whole data center is nothing more than evacuating each pod active there one at a time" ([Shopify](https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale)). Draining is how you retire a cell, rebuild one from scratch, or move away from a cell you no longer trust after a gray failure (guide [12](12-cell-lifecycle-synthesis.md) has the gray-failure discussion and Slack's "build a button" lesson).

### Deploying to many cells

AWS: "the important point here is to deploy in waves, cell by cell or set of cells" ([Cell deployment](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-deployment.html)). Temporal's public version is deployment rings: "Ring 0: Synthetic traffic only, no customer impact. Changes are monitored here for at least a week. Ring 1: Low-priority traffic namespaces ... Higher Rings: Gradually expanding to critical, high-priority traffic customers. Within each ring, updates are applied in batches, with pauses between batches" ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)).

The design rules that turn "rings" from a slide into something safe:

- **Each wave must be able to fail on its own.** If wave 1 has three cells in the same region on the same cloud, a region-specific bug shows up in all three at once. Mix regions and clouds early so a wave catches *different kinds* of bugs.
- **Bake time comes from the bug class, not from patience.** Temporal names "memory leaks or race conditions." A leak that adds 1% per hour will not show up in a one-hour canary. Set the soak time from the slowest failure you expect to catch.
- **Promote automatically; stop automatically.** A wave moves forward only when the cell health gate (guide [14](14-observability-for-cells.md)) is green for the whole bake time, and halts the moment any cell in the wave goes red. A person should be needed to *override* the pipeline, not to *drive* it.
- **Config is a deploy.** A config change pushed to every cell at once is a global change that skips every ring. Feature flags, rate limits, and mapping data all need the same waves as code. Config pushes are a classic cause of global outages precisely because they feel too small to need a rollout.
- **Infra changes go through rings too.** Kubernetes version, node image, CNI version, and Terraform module changes (guides [04](04-managed-kubernetes-eks-gke-aks.md), [05](05-cni-and-host-networking.md), [08](08-terraform.md)) are as dangerous as server code, and they are exactly what a cell-infrastructure team ships. GitOps tools fan one change out to N cells (guide [13](13-gitops-argocd-flux.md)), so *the wave structure has to live in the GitOps layout*, not in someone's head.
- **Plan for version skew.** During a rollout, different cells run different versions for days or weeks. Anything that crosses cells — the router, the control plane, the replication stream between an active and its replica in another cell — must work across at least two adjacent versions.

### Observability across cells

AWS: "Your entire observability stack needs to be cell-aware ... It is important to be able to track each request and identify which cell it is destined for" ([Cell observability](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-observability.html)). Guide [14](14-observability-for-cells.md) covers the tools; the cell-specific rules are:

- **`cell` is a label on everything** — every metric, log line, and trace — from the moment the cell exists. Retrofitting it later is painful.
- **Look at the worst cell, not the average.** A fleet average of 99.95% can hide one cell at 97%. Alert per cell; build fleet dashboards that sort by the worst cell.
- **Compare cells to each other.** Cells are supposed to be identical, so an outlier is a signal by itself. A cell whose p99 latency is 3× its siblings' on the same version is sick even if it is within its SLO.
- **The observability backend is a shared dependency.** If every cell ships telemetry to one central system, that system's outage blinds you to all cells at once — but must not *hurt* any of them. Keep a short in-cell buffer so a cell's health gate can still be evaluated locally.

### What you are allowed to share

AWS's ideal: "Cells should have no dependency on each other at all (that is, no cross-cell API calls, no shared resources like databases or S3 buckets.)" ([Cell design](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-design.html)). Real systems share some things anyway. Classify each one:

| Shared thing | On request path? | If it is down, cells should... | Common mistake |
|---|---|---|---|
| Router / DNS for endpoints | **Yes** | Keep serving from cached records | Short TTLs everywhere "for agility," so caches empty quickly during a DNS-provider incident |
| Control plane (User CP, Infra CP) | No | Keep serving; only changes stop | Frontend checks the control plane per request |
| Identity / API-key validation | Often | Validate locally from cached keys or signed tokens | Calling a central auth service for every RPC |
| Root CA / PKI | No (if done right) | Keep serving with already-issued certs until they expire | Issuer outside the cell plus very short cert lifetimes (guide [11](11-cert-manager-and-pki.md)) |
| Secrets store | No (if cached) | Keep using already-fetched secrets | Pods that fail to start or crash on refresh failure (guide [10](10-vault.md)) |
| Container registry | No | Restart pods from node cache or an in-cell mirror | Every restart pulls from a single remote registry |
| Observability backend | No | Buffer locally; keep serving | Telemetry exporter backpressure blocking the app |
| Deploy pipeline / GitOps | No | Keep the last applied state | A pipeline that pushes to all cells at once |
| Billing / usage metering | No | Record locally, ship later | Metering call made inline on each request |

Rule: **for every row, write down the cell's survival time when that thing is down.** "Indefinitely" is the goal for request-path items. For control-path items, the answer should be "until the next change," never "until the next cert expiry in two hours."

### Failure modes that are specific to cells

Cells remove some failure modes and add new ones. These are the ones to know.

1. **Router or mapping bug — global.** One bad mapping push sends everyone to the wrong cell, or nowhere. Defenses: validation, refusing older or near-empty mappings, last-known-good, and slow waves for router and mapping changes.
2. **Correlated deploy — global, delayed.** A bug that only shows up after 48 hours, shipped through rings with 24-hour bakes, reaches every cell before it fires. Defenses: bake times tied to bug classes, mixing cloud and region early, and the ability to halt *and* roll back across many cells at once.
3. **Replication spreads poison.** If a "poison" workflow or a corrupted history crashes the active cell, async replication may carry the same data to the replica, and a failover then crashes the replica too. *Industry pattern:* the replica is safe from infrastructure faults, not from data faults. The defense is the ability to quarantine one tenant (the override table) and per-tenant circuit breakers inside the cell.
4. **Failover overload.** All HA tenants from a failed cell fail over at once to their replica cells. If those cells have no headroom, the failover causes a second outage. Defense: reserve headroom for replicas and spread a cell's replicas across many other cells, not one.
5. **Split routing.** During a mapping change, some clients reach the old cell and some reach the new one, and both accept writes. Defense: exactly one authoritative side at every moment, enforced *in the cells*, not by the router. In Temporal this is what Namespace failover versions do (guide [16](16-temporal-server-internals.md)).
6. **Quota cliffs.** A cell is its own cloud account, so it has its own limits. Growth stops at a cloud limit nobody measured, and raising limits can take days. Defense: track quotas as a capacity dimension, and test new cells to their stated limit.
7. **Shared dependency failure.** Covered above. The tell-tale sign is "every cell broke at the same minute" — which, in a cell architecture, should be impossible unless something is shared.
8. **Gray failure in one cell.** The cell looks healthy to its own health checks but is failing for customers. Guide [12](12-cell-lifecycle-synthesis.md) covers this in depth; in short, build a fast, safe, *manual* drain button, since automatic detection is unreliable.

### Cells compared with the alternatives

| Approach | Isolation | Cost | Operations | When it wins |
|---|---|---|---|---|
| **One big multi-tenant cluster** | None between tenants | Cheapest | One thing to run | Early stage; small scale |
| **Single-tenant (cluster per customer)** | Total | Most expensive; idle capacity per customer | N = number of customers | Strict compliance; very large customers. Temporal: "customers end up paying for unused capacity" ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)) |
| **Sharding only** | Throughput, not faults | Cheap | One system, more partitions | Scaling data, not reducing blast radius |
| **Cells** | Strong, at cell level | Medium; some headroom per cell | N cells, with automation | Multi-tenant SaaS at scale — Temporal's choice |
| **Geodes** (every instance serves every user) | Regional | High; data everywhere | Complex replication | Global read-heavy apps. Azure: geodes are "typically more complex to design and build" |

Microsoft's guidance against the pattern is worth remembering too — don't use stamps when "You can scale your system out or up within a single instance" or "You only need to scale some components and not others" ([Azure](https://learn.microsoft.com/en-us/azure/architecture/patterns/deployment-stamp)). And deploy at least two from the start: "If you deploy only a single stamp, you can easily hard-code assumptions into your code or configuration that don't apply when you scale out."

---

## Hands-on

### Lab 1 — A thin, statically stable cell router (Go, ~20 minutes, free)

You will build a router in front of two fake cells and watch four properties: routing by partition key, migration by mapping change, static stability when the mapping source is broken, and per-cell bulkheads when one cell gets slow. The code was tested with Go 1.26 on macOS.

```bash
mkdir -p ~/cell-lab && cd ~/cell-lab
go mod init cell-lab
```

Create `mapping.json`. This is the full-mapping table: partition key → cell, plus cell → address. The `version` field only ever goes up.

```json
{
  "version": 1,
  "cells":   { "cell-a": "http://127.0.0.1:9001", "cell-b": "http://127.0.0.1:9002" },
  "tenants": { "acme": "cell-a", "globex": "cell-b", "initech": "cell-b" }
}
```

Create `main.go`:

```go
// cell-lab: two in-process "cells" behind a thin, statically stable cell router.
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Mapping is the router's entire world view: partition key -> cell, plus cell -> address.
type Mapping struct {
	Version int               `json:"version"`
	Cells   map[string]string `json:"cells"`   // cell name -> base URL
	Tenants map[string]string `json:"tenants"` // tenant (partition key) -> cell name
}

type router struct {
	current   atomic.Pointer[Mapping] // last known good
	loadedAt  atomic.Int64
	lastError atomic.Value // string
	mu        sync.Mutex
	bulkheads map[string]chan struct{} // per-cell in-flight limit
	perCell   int
}

func (r *router) reload(path string) {
	b, err := os.ReadFile(path)
	var m Mapping
	if err == nil {
		err = json.Unmarshal(b, &m)
	}
	if err == nil && (len(m.Cells) == 0 || m.Version == 0) {
		err = fmt.Errorf("refusing suspicious mapping: version=%d cells=%d", m.Version, len(m.Cells))
	}
	if err == nil {
		if cur := r.current.Load(); cur != nil && m.Version < cur.Version {
			err = fmt.Errorf("refusing rollback from version %d to %d", cur.Version, m.Version)
		}
	}
	if err != nil {
		// Static stability: keep serving the last known good mapping.
		r.lastError.Store(err.Error())
		return
	}
	r.lastError.Store("")
	if cur := r.current.Load(); cur == nil || cur.Version != m.Version {
		log.Printf("router: loaded mapping version %d (%d tenants)", m.Version, len(m.Tenants))
	}
	r.current.Store(&m)
	r.loadedAt.Store(time.Now().Unix())
}

func (r *router) bulkhead(cell string) chan struct{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.bulkheads[cell] == nil {
		r.bulkheads[cell] = make(chan struct{}, r.perCell)
	}
	return r.bulkheads[cell]
}

func (r *router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.URL.Path == "/debug/mapping" {
		m := r.current.Load()
		age := time.Now().Unix() - r.loadedAt.Load()
		fmt.Fprintf(w, "version=%d age=%ds lastError=%q\n", m.Version, age, r.lastError.Load())
		return
	}
	// Partition key is the first path segment: /t/<tenant>/...
	parts := strings.SplitN(strings.TrimPrefix(req.URL.Path, "/t/"), "/", 2)
	tenant := parts[0]
	m := r.current.Load()
	cell, ok := m.Tenants[tenant]
	if !ok {
		http.Error(w, "unknown tenant "+tenant, http.StatusNotFound)
		return
	}
	target, err := url.Parse(m.Cells[cell])
	if err != nil || m.Cells[cell] == "" {
		http.Error(w, "no address for cell "+cell, http.StatusBadGateway)
		return
	}
	// Bulkhead: a slow cell may exhaust its own slots, never another cell's.
	slots := r.bulkhead(cell)
	select {
	case slots <- struct{}{}:
		defer func() { <-slots }()
	default:
		http.Error(w, "cell "+cell+" saturated", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("X-Cell", cell)
	httputil.NewSingleHostReverseProxy(target).ServeHTTP(w, req)
}

// startCell runs a fake cell. POST /admin/slow on it makes every request take 2s.
func startCell(name, addr string) {
	var slow atomic.Bool
	mux := http.NewServeMux()
	mux.HandleFunc("/admin/slow", func(w http.ResponseWriter, _ *http.Request) {
		slow.Store(!slow.Load())
		fmt.Fprintf(w, "%s slow=%v\n", name, slow.Load())
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, req *http.Request) {
		if slow.Load() {
			time.Sleep(2 * time.Second)
		}
		fmt.Fprintf(w, "served by %s: %s\n", name, req.URL.Path)
	})
	go func() { log.Fatal(http.ListenAndServe(addr, mux)) }()
}

func main() {
	startCell("cell-a", "127.0.0.1:9001")
	startCell("cell-b", "127.0.0.1:9002")

	r := &router{bulkheads: map[string]chan struct{}{}, perCell: 8}
	r.reload("mapping.json")
	if r.current.Load() == nil {
		log.Fatalf("no valid mapping at startup: %v", r.lastError.Load())
	}
	go func() {
		for range time.Tick(2 * time.Second) {
			r.reload("mapping.json")
		}
	}()
	log.Println("router listening on 127.0.0.1:8080")
	log.Fatal(http.ListenAndServe("127.0.0.1:8080", r))
}
```

Run it in one terminal:

```bash
cp mapping.json mapping.v1.json   # keep a copy for later
go run .
```

**Step 1 — routing by partition key.** In a second terminal:

```bash
curl -si localhost:8080/t/acme/hello | grep -E 'X-Cell|served'
# X-Cell: cell-a
# served by cell-a: /t/acme/hello
curl -s localhost:8080/t/globex/hello      # served by cell-b
curl -s localhost:8080/t/nobody/x          # unknown tenant nobody
```

The unknown tenant gets a 404, not a guess. A router that "defaults" unknown keys to some cell will put data in the wrong place the first time the control plane is late registering a tenant.

**Step 2 — migration as a mapping change.** Move `initech` to `cell-a` and bump the version. This is the *Redirect* step; in a real system *Clone* and *Flip* happen in the cells first.

```bash
sed -e 's/"version": 1/"version": 2/' \
    -e 's/"initech": "cell-b"/"initech": "cell-a"/' mapping.v1.json > mapping.json
sleep 3
curl -s localhost:8080/t/initech/x          # served by cell-a
```

Note the 2-second reload interval: during that window, the router still sends `initech` to `cell-b`. That window is your TTL, and the old cell must handle it — by forwarding, or by refusing writes so the client retries.

**Step 3 — static stability.** Break the mapping source, as if the control plane pushed garbage or the config store went away:

```bash
echo '{ not json' > mapping.json
sleep 3
curl -s localhost:8080/debug/mapping
# version=2 age=4s lastError="invalid character 'n' looking for beginning of object key string"
curl -s localhost:8080/t/initech/x          # still served by cell-a
```

Then try to "roll back" to an older mapping:

```bash
cp mapping.v1.json mapping.json
sleep 3
curl -s localhost:8080/debug/mapping
# version=2 ... lastError="refusing rollback from version 2 to 1"
```

The data plane kept working through both bad pushes. Note that `age` keeps growing — **that number is what you alert on.** A router serving a stale mapping is fine for minutes and dangerous after days.

**Step 4 — bulkheads.** Make `cell-b` slow, flood it, and check that `cell-a` tenants don't notice:

```bash
curl -s -X POST 127.0.0.1:9002/admin/slow      # cell-b slow=true
for i in $(seq 1 30); do
  curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/t/globex/x &
done > flood.txt
sleep 0.3
time curl -s localhost:8080/t/acme/x           # fast: ~20 ms
sleep 3; sort flood.txt | uniq -c
#    8 200
#   22 503
```

Eight requests got `cell-b`'s eight slots and waited; the other 22 were refused immediately; `acme` on `cell-a` never waited. Now set `perCell` to a very large number, rebuild, and repeat. In this toy the router's goroutines are cheap, so `acme` will still be fast — in a real proxy with a fixed worker pool or connection pool it would not be, and that is the outage the bulkhead prevents. Say this out loud in a design review: *a router without per-cell isolation turns one slow cell into a global outage.*

**Exercises.**

1. Add an `overrides` map that takes priority over `tenants` (AWS's "override table"), and use it to quarantine one tenant to a cell named `cell-quarantine`.
2. Replace the full mapping for unknown tenants with consistent hashing over the cells. Add a third cell and count how many tenants move. Then compare with `hash % N`.
3. Add a `max_age` check: if the mapping is older than some limit, keep serving but return a warning header and log loudly. Decide what the limit should be and why.

### Lab 2 — Blast-radius calculator (Python, 5 minutes)

Check the numbers from the blast-radius section yourself, then change them to match a real fleet.

```python
from math import comb

def shuffle_shard_overlap(n: int, k: int) -> None:
    total = comb(n, k)
    print(f"n={n} k={k}: {total:,} distinct hands")
    for j in range(k + 1):
        p = comb(k, j) * comb(n - k, k - j) / total
        print(f"  share exactly {j} of {k} workers with a given tenant: {p:.6%}")

def replica_pairs(cells: int) -> None:
    pairs = comb(cells, 2)
    print(f"{cells} cells: single-cell outage hits {1/cells:.1%} of non-HA tenants; "
          f"{pairs} active/replica pairs, so a specific double failure hits ~{1/pairs:.2%}")

shuffle_shard_overlap(100, 5)
shuffle_shard_overlap(8, 2)
for n in (10, 20, 40):
    replica_pairs(n)
```

Expected highlights: C(100,5) = 75,287,520; 76.96% of tenants share zero workers; with 20 cells there are 190 pairs. Then ask the more useful question: *what assumption makes these numbers lie?* (Answer: they assume failures are independent and placement is uniform. A shared dependency or a correlated deploy breaks both assumptions — which is why the sharing table above matters more than the math.)

---

## Production gotchas

1. **The router is the most dangerous thing you own.** It is the only component whose bug reaches every cell. Give router and mapping changes the slowest rollout in the company, and test the mapping validator as hard as the router.
2. **Config pushes skip your rings.** A flag flipped "everywhere" is a global deploy. Put config, rate limits, and mapping data through the same waves as code.
3. **TTL does not move open connections.** gRPC clients keep long-lived HTTP/2 connections. A CNAME change only affects *new* connections. The old side must actively close or reject.
4. **Clients cache DNS longer than you think.** Some runtimes and resolvers have their own cache settings independent of the record TTL. Measure real convergence time in a failover drill; don't compute it from the TTL.
5. **Expiring things are hidden dependencies.** Certs, tokens, leases, and signed URLs turn a control-plane outage into a delayed data-plane outage. List each with its lifetime and where its refresher lives.
6. **Replicas need headroom.** A cell full of replicas must still have room to make them all active. Spread replicas so one failure lands on many cells, not one.
7. **Replication copies data faults too.** A replica protects against infrastructure loss, not against a poison workflow or bad data. You still need per-tenant quarantine.
8. **Cell capacity is multi-dimensional.** CPU, persistence IOPS, visibility indexing, connections, IP space, and cloud quotas all run out separately. Place against the tightest one.
9. **"Cell full" must be a clean answer.** A placement that times out is worse than one that says "no capacity in this region." And because new cells take a long time to build, create them from a forecast.
10. **The first cell lies.** Code written against one cell hard-codes assumptions. Azure's advice: run at least two from day one. Run every test against the second cell, not just the first.
11. **"Forget" is the only irreversible migration step.** Gate deletion of the old copy on verification and a bake period, not on a timer.
12. **Fleet averages hide sick cells.** Alert per cell; build dashboards around the worst cell and around outliers among identical cells.
13. **Everything that spans cells must be simpler than anything inside a cell.** If a cross-cell component is starting to grow business logic, push that logic down into the cells or into the control plane.

---

## How this shows up in cell lifecycle

- **Provisioning (guide [12](12-cell-lifecycle-synthesis.md))** builds one unit of this architecture. The cell DAG ends at the health gate (guide [14](14-observability-for-cells.md)) because a cell must not join the placement pool until it is proven healthy — the gate is the line between "a cell that exists" and "a cell the router may send traffic to."
- **The account boundary (guide [03](03-multicloud-aws-gcp-azure.md))** is the cell's outer wall. Per-cell accounts are why quotas are a capacity dimension and why teardown must leave nothing behind.
- **Terraform state layout (guide [08](08-terraform.md))** is blast radius for infrastructure changes. One state per cell keeps a bad apply inside one cell; one state for many cells turns your IaC tool into a shared dependency.
- **GitOps (guide [13](13-gitops-argocd-flux.md))** is where deployment waves actually live. The directory or ApplicationSet structure *is* the ring structure.
- **PKI and secrets (guides [10](10-vault.md), [11](11-cert-manager-and-pki.md))** decide how long a cell survives a control-plane outage. That number is the real measure of the cell's independence.
- **Temporal internals (guide [16](16-temporal-server-internals.md))** supply the migration and failover mechanics: replication tasks, active/standby executors, and version histories are what make *Clone* and *Flip* safe.
- **The control plane runs on Temporal (guide [15](15-temporal-programming-model.md)).** Cell lifecycle operations are long-running, retryable workflows — an entity workflow per cell — which is what makes "drain cell 7 and rebuild it" a safe, resumable operation rather than a runbook.

---

## Explaining cells in a design review

The questions below come up when the system under discussion is multi-tenant infrastructure. Each answer is a starting point; be ready to go two levels deeper.

**"Why not just run one big cluster and scale it?"** Because scaling fixes throughput, not blast radius. In one big cluster, a bad deploy, a poison tenant, or a corrupted index hits everyone. Cells cap any single failure at 1/N of tenants, let you deploy in waves, and keep each copy small enough to load-test to its limit. The cost is N copies to operate, which is fine only if automation makes that cheap.

**"Why not one cluster per customer?"** Idle capacity and N = number of customers. Temporal explicitly rejected it: "customers end up paying for unused capacity, and providers shoulder higher operational costs." Cells are the middle path, and you can still give a very large tenant a dedicated cell through placement policy.

**"How does a request find its cell?"** A partition key that is in every request — the Namespace — and a thin router. For Temporal, a per-Namespace DNS name is effectively a full-mapping table stored in DNS, with SNI routing for regional endpoints. Then discuss the trade-off: no data-path hop and strong static stability, against TTL-bounded changes and connections that don't move on their own.

**"What happens when the control plane is down?"** Nothing visible to running workloads; only changes stop — new Namespaces, migrations, config. Then prove it: no control-plane calls on the request path, cached credentials, and cert lifetimes long enough to survive a long control-plane outage.

**"How do you move a tenant?"** Clone, Flip, Redirect, Forget — and for Temporal, the HA replication path already does all four. Talk about graceful versus forced flip (RPO), the in-between state, old connections, and gating Forget on verification.

**"How do you roll out a change to 300 cells?"** Rings and waves: synthetic-only first with a long soak, then low-priority tenants, then batches with pauses; automatic promotion on a green health gate and automatic halt on red; mixed clouds and regions early; config through the same pipeline; tolerate version skew.

**"What is still shared, and what happens when it breaks?"** Walk the sharing table: router/DNS, control plane, identity, PKI, secrets, registry, observability, pipelines. For each, say whether it is on the request path and how long a cell survives without it.

**"Cells or shuffle sharding?"** Both. Cells are hard walls for faults you cannot predict; shuffle sharding is statistical isolation for noisy neighbors on shared fleets. Quote the math: 5 of 100 gives 75 million distinct hands.

---

## Learning path

### Day 1 (about 3 hours)

- Read the AWS whitepaper end to end: [what is a cell](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/what-is-a-cell-based-architecture.html), cell design, partition, routing, migration, deployment, observability. It is short and it is the shared vocabulary.
- Read Temporal's [Cloud overview](https://docs.temporal.io/cloud/overview), [RPO/RTO](https://docs.temporal.io/cloud/rpo-rto), and [High Availability](https://docs.temporal.io/cloud/high-availability) pages. Write down every sentence that mentions cells.
- Do Lab 1 and Lab 2.

### Week 1

- Read [Building durable cloud control systems with Temporal](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) twice: once for the architecture, once for the workflow design.
- Read the Builders' Library articles on [static stability](https://aws.amazon.com/builders-library/static-stability-using-availability-zones/) and [constant work](https://aws.amazon.com/builders-library/reliability-and-constant-work/), and the [original shuffle sharding post](https://aws.amazon.com/blogs/architecture/shuffle-sharding-massive-and-magical-fault-isolation/).
- Read [Slack](https://slack.engineering/slacks-migration-to-a-cellular-architecture/), [Shopify](https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale), and [Roblox](https://about.roblox.com/newsroom/2023/12/making-robloxs-infrastructure-efficient-resilient). For each, write one line: what is their cell, and what failure made them build it?
- Do the three Lab 1 exercises.

### Month 1 (on the team)

- Draw the real sharing table for Temporal Cloud cells, with a survival time for every row. Find the row with the shortest survival time; that is your first project candidate.
- Find out where the control-plane Namespaces themselves run, and what protects them from a bad deploy.
- Run, or shadow, a cell drain and a failover drill. Measure real client convergence time and compare it with the TTL.
- Learn how placement decides "this cell is full," along which dimensions, and how far ahead new cells are forecast.

---

## References

1. [AWS Well-Architected — What is a cell-based architecture?](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/what-is-a-cell-based-architecture.html) — the cell, router, and control-plane decomposition; the bulkhead analogy.
2. [AWS Well-Architected — Cell design](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-design.html) — "no dependency on each other at all"; separate accounts encouraged.
3. [AWS Well-Architected — Cell partition](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-partition.html) — partition keys, grain, the large-tenant problem, cross-grain calls through the router.
4. [AWS Well-Architected — Full mapping](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/full-mapping.html) — trade-offs of mapping every key explicitly.
5. [AWS Well-Architected — A warning for all mapping approaches](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/a-warning-for-all-mapping-approaches.html) — the override table.
6. [AWS Well-Architected — Cell routing](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-routing.html) — router properties and design options.
7. [AWS Well-Architected — About resilience of the cell router](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/about-resilience-of-the-cell-router.html) — the router as the one shared component; make it cellular.
8. [AWS Well-Architected — Cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html) — Clone, Flip, Redirect, Forget.
9. [AWS Well-Architected — Cell deployment](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-deployment.html) — deploy in waves, cell by cell.
10. [AWS Well-Architected — Cell observability](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-observability.html) — a cell-aware observability stack.
11. [AWS Well-Architected — Cell sizing](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-sizing.html) — the three forces on cell size.
12. [Amazon Builders' Library — Static stability using Availability Zones](https://aws.amazon.com/builders-library/static-stability-using-availability-zones/) — control plane vs data plane; overprovisioning by 50% across three AZs.
13. [Amazon Builders' Library — Reliability, constant work, and a good cup of coffee](https://aws.amazon.com/builders-library/reliability-and-constant-work/) — why simple, constant-work designs make reliable routers.
14. [AWS Architecture Blog — Shuffle Sharding: Massive and Magical Fault Isolation (2014)](https://aws.amazon.com/blogs/architecture/shuffle-sharding-massive-and-magical-fault-isolation/) — the original shuffle sharding explanation.
15. [Amazon Builders' Library — Workload isolation using shuffle sharding](https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/) — the longer treatment, including Route 53 (now redirects to AWS Builder Center).
16. [Microsoft Azure Architecture Center — Deployment Stamps pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/deployment-stamp) — stamps, scale units, routing, when not to use the pattern, run at least two.
17. [Temporal Cloud — Overview](https://docs.temporal.io/cloud/overview) — "Temporal Cloud uses a cell-based architecture"; what a cell contains; data vs control plane.
18. [Temporal Cloud — Outages and recovery objectives (RPO/RTO)](https://docs.temporal.io/cloud/rpo-rto) — cells across at least three AZs; RPO/RTO by failure scope.
19. [Temporal Cloud — High Availability](https://docs.temporal.io/cloud/high-availability) — Same-region, Multi-region, and Multi-cloud Replication; "replicate the Namespace across multiple cells in that region."
20. [Temporal Cloud — High Availability: how it works](https://docs.temporal.io/cloud/high-availability/how-it-works) — CNAME update on failover; 15-second TTL; clients converge in about 30 seconds.
21. [Temporal Cloud — Namespaces](https://docs.temporal.io/cloud/namespaces) — Namespace as a unit of isolation; Namespace and regional endpoints; `server_name` for regional endpoints.
22. [Temporal blog — Building durable cloud control systems with Temporal (Sergey Bykov, Jan 16, 2025)](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) — cells as per-account units; User CP and Infra CP; entity workflow per cell; deployment rings.
23. [Temporal blog — Temporal Cloud expands High Availability (Nikitha Suryadevara, Mar 4, 2025)](https://temporal.io/blog/expanding-temporal-clouds-high-availability-offerings) — "Each cell ... acts as a failure domain"; asynchronous replication to a replica.
24. [Temporal blog — Multi-cloud: one small step for Temporal](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability) — how the cell abstraction carried over from AWS to GCP.
25. [Temporal docs — Nexus](https://docs.temporal.io/nexus) — the supported way to call across Namespaces.
26. [Slack Engineering — Slack's Migration to a Cellular Architecture](https://slack.engineering/slacks-migration-to-a-cellular-architecture/) — AZ-as-cell; draining through Envoy weights in seconds.
27. [Shopify Engineering — A Pods Architecture to Allow Shopify to Scale](https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale) — Sorting Hat routing and the Pod Mover.
28. [Roblox — How We're Making Roblox's Infrastructure More Efficient and Resilient (Dec 7, 2023)](https://about.roblox.com/newsroom/2023/12/making-robloxs-infrastructure-efficient-resilient) — ~1,400-machine cells; >70% of back-end traffic in cells; the 73-hour outage that motivated them.
