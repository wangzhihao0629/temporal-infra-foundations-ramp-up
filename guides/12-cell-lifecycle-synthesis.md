# The Cell as a System — How Fifteen Tools Become One Job

**Why this matters.** The other sixteen guides in this library each teach a tool, a system, or a signal. None of them teaches *the job*. The job is a single verb applied to a single noun: **converge a cell**. A cell is a self-contained unit of Temporal Cloud capacity, and cell-lifecycle work owns its whole life — bring it into existence in AWS, GCP, or Azure; keep it identical to its several hundred siblings; move it forward one version at a time without anyone noticing; and eventually delete it without leaving a load balancer, a disk, an ENI, a DNS record, an IAM role, or a KMS key behind. Every tool in guides 01–11 is a *stage in that pipeline*; [13](13-gitops-argocd-flux.md) and [14](14-observability-for-cells.md) are the loop and the gate that keep the pipeline running; [15](15-temporal-programming-model.md) and [16](16-temporal-server-internals.md) are the workload the whole thing exists to carry. And the interesting engineering is almost never inside a stage. It is in the ordering between them, in the six or seven places where the ordering is circular and someone had to break the cycle, and in the tests and gates that let a person ship a change to three hundred cells on a Tuesday afternoon without their pulse rising. This guide is the map. Read it first, and re-read it every time one of the other guides starts to feel like trivia.

**On sourcing.** Temporal has published a meaningful amount about how Temporal Cloud is built, and I quote it directly and link it. Everywhere I go beyond what Temporal has said publicly, I mark the claim as *industry pattern* or *inference* and say so in the sentence. Nothing in this guide is an internal Temporal detail; I do not have any. Verified against primary sources on **2026-08-29**.

---

## How to use this library

| # | Guide | What it is for | When you reach for it |
|---|---|---|---|
| 01 | [Go for Infrastructure Engineers](01-golang.md) | The language every control-plane component, controller, operator, and Temporal workflow is written in | Continuously. Reading Go is a bigger part of the job than writing it |
| 02 | [gRPC and Protocol Buffers](02-grpc.md) | Temporal's entire client/server contract, plus the LB, keepalive, and drain semantics that decide whether a cell upgrade is invisible | Any incident that looks like an application error but is really a load balancer |
| 03 | [AWS / GCP / Azure Foundations](03-multicloud-aws-gcp-azure.md) | The three-column comparison substrate: tenancy, identity, network, ingress, state, keys | Every time you write or review a per-cloud branch |
| 04 | [Managed Kubernetes: EKS / GKE / AKS](04-managed-kubernetes-eks-gke-aks.md) | The seam between Kubernetes and the cloud: version policy, node abstraction, upgrade mechanics, teardown ordering | Cell provisioning and the never-ending upgrade pipeline |
| 05 | [CNI and Host-Level Networking](05-cni-and-host-networking.md) | The datapath from kernel primitives up. IPAM, MTU, conntrack, kube-proxy modes, eBPF | The layer often co-owned with a networking team, and the one that produces the most confusing 3 a.m. pages |
| 06 | [Karpenter](06-karpenter.md) | What machines a cell is made of, and — more dangerously — when one gets deleted | Node image upgrades, capacity policy, and every "why did that node go away" question |
| 07 | [Kyverno](07-kyverno.md) | Policy as code: `generate` rules as cell bootstrap, `validate` rules as the guardrail on everything else | Encoding the invariants the other ten guides describe, so they become compile-time errors |
| 08 | [Terraform](08-terraform.md) | State layout, which is what actually determines blast radius across N cells | Anything below the Kubernetes API server |
| 09 | [Helm — As a Templating Engine](09-helm.md) | Rendering manifests, and what you owe yourself once you throw away release state | Anything above the Kubernetes API server |
| 10 | [HashiCorp Vault](10-vault.md) | Where a cell's secrets come from and where they go when the cell dies | Bootstrap ordering, and the "cell will not unseal" class of outage |
| 11 | [cert-manager and PKI](11-cert-manager-and-pki.md) | Issuance, trust distribution, and rotation — three separate disciplines | mTLS everywhere, and the one project that has no undo button |
| 12 | **This guide** | The composition. The dependency DAG, the bootstrap paradoxes, the upgrade and teardown stories, the questions to ask | First, and then repeatedly |
| 13 | [GitOps: Argo CD and Flux](13-gitops-argocd-flux.md) | The reconciler that owns everything above the API server: inventory, pruning, health assessment, and fanning one change across N cells | Every convergence question, and every "why is this cell OutOfSync" page |
| 14 | [Observability for Cells](14-observability-for-cells.md) | Prometheus, OTel, SLOs — and the cell health gate that decides whether a freshly built cell may take traffic | Defining "ready" and "healthy" as something a machine can evaluate |
| 15 | [Temporal: the Programming Model](15-temporal-programming-model.md) | The customer's seat: durable execution, determinism, versioning, workers and long polls | Understanding what your cells actually carry, and why a customer's retry policy is your capacity problem |
| 16 | [Temporal Server Internals](16-temporal-server-internals.md) | Code level: History shards and the range-ID fence, Matching partitions, persistence, membership | Sizing `numHistoryShards`, sequencing a rolling restart, and any shard-unavailability alert |
| 17 | [Cell-Based Architecture](17-cell-based-architecture.md) | The pattern itself: router, partition key, placement, migration, deployment waves, what may be shared between cells | Any design discussion about routing, tenant placement, failover, or blast radius — and design reviews |

**Suggested reading order.**

1. **This guide, end to end.** It will name things you have not learned yet. That is intentional; the names give the other guides somewhere to attach.
2. **[03](03-multicloud-aws-gcp-azure.md) and [04](04-managed-kubernetes-eks-gke-aks.md)** — the substrate. Read them as a pair; almost every fact in 04 is a consequence of a scoping rule in 03.
3. **[05](05-cni-and-host-networking.md)** — the deepest and most load-bearing guide in the set, and the one whose material you co-own with another team. Do its Lab 1 with your hands.
4. **[08](08-terraform.md) and [09](09-helm.md)** — the delivery mechanics, below and above the API server respectively. The boundary between them is a design decision your team has already made; find out where they drew it.
5. **[13](13-gitops-argocd-flux.md)** — immediately after 09, because 08 and 09 both stop mid-sentence and hand off to it. This is the component whose logs you will read most.
6. **[11](11-cert-manager-and-pki.md) then [10](10-vault.md)** — the trust and secret spine. In that order, because cert-manager gives you the vocabulary that makes Vault's PKI engine make sense.
7. **[06](06-karpenter.md) and [07](07-kyverno.md)** — the two cluster-level controllers that can, respectively, delete your cell's nodes and refuse to let your cell start.
8. **[14](14-observability-for-cells.md)** — once you know what a cell is made of, this is how you decide whether one is healthy. Its cell health gate is the thing the whole pipeline terminates in.
9. **[15](15-temporal-programming-model.md) then [16](16-temporal-server-internals.md)** — in that order, always. 16 is the deepest and most role-specific guide in the library, and it assumes the customer-side vocabulary that 15 supplies. Read 15 in an evening; give 16 a week.
10. **[01](01-golang.md) and [02](02-grpc.md)** — continuously, as reference, from day one.
11. **This guide again**, after the other fifteen. It will read completely differently.

---

## The mental model

Hold five ideas. Everything else in this guide is a consequence of one of them.

**1. A cell is a blast radius with a bill attached.** The whole point of a cell is that when something goes wrong inside it, the thing that went wrong is confined to it. AWS's [cell-based architecture whitepaper](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/what-is-a-cell-based-architecture.html) traces the idea to a ship's bulkhead: watertight compartments so a hull breach floods one section rather than the vessel. Translated: "If a workload uses 10 cells to service 100 requests, when a failure occurs in one cell, 90% of the overall requests would be unaffected." The cost of that property is that you now operate N copies of everything, and every operational task is multiplied by N. Which means the actual product of cell-lifecycle work is not a cell — it is **the automation that makes N cells cost about as much attention as one**.

**2. Provisioning a cell is a distributed transaction with no rollback, so it is modelled as a workflow.** Temporal has said this publicly and specifically: control-plane tasks "involve complex long-running processes with many interdependent steps," and each cell has an *entity workflow* that "manages its lifecycle, from provisioning to upgrades" ([Building durable cloud control systems with Temporal](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). The engineering consequence is that every step in the cell DAG must be an **idempotent, retryable, individually-timed activity**, and the DAG itself must be **deterministic and resumable from any point**. This is not a stylistic preference; it is what makes a half-failed cell recoverable instead of garbage.

**3. Level-triggered beats edge-triggered, everywhere, always.** "Create a cell" is a tempting way to think about provisioning and it is wrong. The right frame is: there is a **desired state** for cell `usw2-07`, there is an **observed state**, and something converges the difference — repeatedly, forever, tolerating being interrupted at any instant. Provisioning is the first pass of the convergence loop. Upgrade is a change to the desired state. Teardown is setting the desired state to empty. One loop, three names. Every tool in this library that works well works this way (Kubernetes controllers, Karpenter, GitOps agents, Terraform's plan/apply); every tool that works badly is one where somebody bolted an imperative script on the side.

**4. The interesting part is the ordering, and the ordering has cycles in it.** You cannot install the autoscaler without a node; you cannot get a node without the autoscaler. You cannot issue a certificate without a CA; the CA needs a certificate. You cannot run the policy engine without a certificate; the certificate issuer must pass the policy engine. Each of these is a real cycle in the dependency graph, and each one is broken by a specific, deliberate, slightly ugly trick. Knowing those tricks — and knowing that they exist and are *supposed* to be there — is most of what separates someone who can debug a stuck cell from someone who cannot. There is a whole section on this below.

**5. Cross-cell dependencies destroy the property you paid for.** AWS says it plainly in [Cell design](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-design.html): "Cells should have no dependency on each other at all (that is, no cross-cell API calls, no shared resources like databases or S3 buckets.) Even the use of separate AWS accounts is encouraged." Every shared thing you add — a shared Vault, a shared observability backend, a shared registry, a shared DNS zone — is a correlated failure domain that spans every cell in the fleet. Some of these are unavoidable; the discipline is to *name each one out loud*, decide whether it is on the request path or only on the control path, and make sure the cell keeps serving when the shared thing is down.

---

## Core concepts

### What a cell is, and why a multi-tenant SaaS converges on one

Start from the constraint, not the pattern. A managed service has to choose a tenancy model. Temporal's public account of that decision: single-tenancy — a dedicated cluster per customer — "offers simplicity and isolation," but "customers end up paying for unused capacity, and providers shoulder higher operational costs," so "multi-tenancy, though harder to implement, emerged as the clear winner" ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)).

Having chosen multi-tenancy, you have imported a problem: every failure is now potentially everyone's failure. A bad deploy, a poison-pill request, a hot tenant, a corrupted index — all of them are, by default, global. Cells are the answer: you take the multi-tenant system and instantiate it N times, each instance complete and independent, and you route each tenant to exactly one instance.

The AWS whitepaper decomposes that into three parts, and it is worth memorizing the decomposition because it is the vocabulary everyone uses:

- **Cell** — "A complete workload, with everything needed to operate independently."
- **Cell router** — "the *thinnest possible layer*, with the responsibility of routing requests to the right cell, and only that."
- **Control plane** — "Responsible for administration tasks, such as provisioning cells, de-provisioning cells, and migrating cell customers."

The router is deliberately dumb because it is the one component that is *not* cellular — it sees all traffic, so its blast radius is global, so it must be simple enough to be nearly bug-free. That principle generalizes: any component that spans cells must be radically simpler than the components inside cells.

The **partition key** is the other half of the router's job. AWS: "The overall workload is partitioned by a partition key. This key needs to align with the *grain* of the service, or the natural way that a service's workload can be subdivided with minimal cross-cell interactions." For Temporal Cloud, the natural grain is publicly visible in the product: the Namespace. Temporal's namespace-provisioning workflow begins by "selecting a suitable cell within the chosen region" ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). A [Namespace](https://docs.temporal.io/cloud/namespaces) is described in the docs as "a unit of isolation within Temporal Cloud, providing security boundaries, Workflow management, unique identifiers, and gRPC endpoints." That is a partition key with the grain built in.

### Cell vs shard vs availability zone vs region

These four words get used interchangeably by people who have not thought about it, and precisely by people who have. Be in the second group.

| Concept | What it partitions | Who chose the boundary | Failure semantics | Can you add more? |
|---|---|---|---|---|
| **Availability zone** | Physical infrastructure — power, cooling, network | The cloud provider | Correlated failure inside, independent across (mostly). Temporal Cloud replicates standard Namespace state across **three AZs** before acknowledging a write ([HA docs](https://docs.temporal.io/cloud/high-availability)) | No. Fixed per region |
| **Region** | Geography, jurisdiction, data residency | The cloud provider | Independent, but with real latency and cost between them | Only by launching in a new one |
| **Cell** | A complete, independent instance of your workload | **You** | Independent by construction; you own how independent | Yes — that is the point. Provisioning a cell is the scaling primitive |
| **Shard** | Data within one system, for throughput | You, usually at install time | *Not* a fault boundary by default. A shard loss is a partial outage of one cell | Often not. Temporal's History Shard count "cannot be changed" after the database is integrated ([Temporal Server docs](https://docs.temporal.io/temporal-service/temporal-server)) |

The distinction that matters most for cell work: **a shard is a throughput unit, a cell is a fault unit.** Temporal's History Shards are the clearest example in your own stack. Each History Shard "maps to a single persistence partition" and "represents the number of concurrent database operations that can occur"; Temporal recommends "1 History Service process for every 500 History Shards," and the total is fixed for the life of the service. Shards buy you concurrency. They buy you nothing in blast radius — a wedged shard degrades every namespace whose workflows hash into it, within that cell. The cell is what stops that from spreading.

A fourth relationship worth holding: **cells nest inside regions, and both nest inside a cloud.** Temporal Cloud publicly operates in [multiple AWS and GCP regions](https://docs.temporal.io/cloud/regions), and the [High Availability docs](https://docs.temporal.io/cloud/high-availability) describe Multi-region Replication, Multi-cloud Replication, and — most relevant here — **Same-region Replication**, where "Temporal operates a 'cell architecture' and will replicate the Namespace across multiple cells in that region." That last sentence is the single most direct public confirmation that cells are a first-class, customer-visible construct, and that cell-to-cell failover within a region is a thing that happens automatically.

### Cell sizing: the three-way tension

A cell has a maximum size, and choosing it is a real design decision rather than a capacity-planning afterthought. AWS names [three opposing forces](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-sizing.html):

- Big enough to fit the largest single workload you will ever accept.
- Small enough to **test at full scale**, and to stay below account and region quotas.
- Big enough to get economies of scale.

The second one is the one people underweight and the one a senior engineer should defend. If a cell is too large to load-test to its own stated limit, then the cell's limit is a guess, and you will discover it during an incident. AWS's framing of what a cell's capacity *is*: "How many transactions per second can a cell handle? How many customers or tenants does it support? How many GB of transfer per second or stored capacity does it support?" If your team cannot answer those three questions with measured numbers for each cloud, that is a gap worth owning.

The smaller-vs-larger trade table from the whitepaper is worth internalizing: smaller cells mean a smaller scope of impact, less chance of hitting a quota cliff, and cheaper full-scale testing, at the cost of more cells to operate and more idle headroom. Larger cells mean better utilization and fewer things to operate, at the cost of a bigger blast radius and a higher chance of hitting a limit that nobody knew existed. Note that "more cells to operate" is only expensive if your automation is weak — which is the argument that your team's automation quality is what actually sets the optimal cell size.

### What Temporal Cloud is publicly known to run

Everything in this subsection is from Temporal's own published material. Where I extrapolate, I say so.

**The unit.** From the Temporal engineering blog: "Each cell operates as a self-contained unit with its own AWS account, VPC, EKS cluster, and supporting infrastructure. While this approach is framed within the context of AWS, we have applied the same principles to Google Cloud Platform (GCP), leveraging its equivalent primitives." Each cell includes:

- **Compute pods** running "Temporal services and infrastructure tools for observability, ingress management, and certificate handling."
- **Databases** — "both primary databases and Elasticsearch for enhanced visibility."
- **Additional components** — "Load balancers, private connectivity endpoints, and other supporting infrastructure."

That maps one-to-one onto this library: EKS/GKE/AKS is guide [04](04-managed-kubernetes-eks-gke-aks.md), the VPC is guide [05](05-cni-and-host-networking.md), the account boundary is guide [03](03-multicloud-aws-gcp-azure.md), "certificate handling" is guide [11](11-cert-manager-and-pki.md), and "ingress management" is the seam usually shared with a networking team.

**The application inside the cell.** The Temporal Server is [four independently scalable services](https://docs.temporal.io/temporal-service/temporal-server):

- **Frontend** — stateless gateway; rate limiting, authorizing, validating, routing. gRPC on 7233, membership on 6933. Scales differently from the rest "because it has no sharding or partitioning; it is just stateless."
- **History** — persists Workflow Execution state to Event History; owns mutable state, timers, and the internal Transfer / Timer / Replicator / Visibility task queues. 7234 / 6934.
- **Matching** — hosts user-facing Task Queues and matches Workers to Tasks. 7235 / 6935.
- **Worker** — internal background workflows and the replication queue. Membership on 6939.

Services find each other through a membership protocol via [Ringpop](https://github.com/temporalio/ringpop-go). The docs' own example of a production shape: "5 Frontend, 15 History, 17 Matching, and 3 Worker Services per Temporal Service." Two consequences for you: the internal traffic is **gRPC over a hash ring**, not over a load balancer, which is why the guide [02](02-grpc.md) material on client-side round-robin over headless Services matters; and **History is the stateful one**, which is why drain and PDB policy for History is a different conversation from drain policy for Frontend.

**Persistence.** [Persistence docs](https://docs.temporal.io/temporal-service/persistence): the required dependency is a persistence database (Cassandra, PostgreSQL, or MySQL), with a separate **visibility** store. Elasticsearch is recommended for production visibility; SQL-based advanced visibility is available from Server 1.20. Temporal's public multi-cloud writeup is specific about the AWS shape: "Temporal depends on a visibility layer, for which we use AWS OpenSearch with no clear equivalent in Google Cloud directly compatible with ElasticSearch 7. This required us to work with another vendor to deploy our visibility layer in Google Cloud" ([multi-cloud blog](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability)). This is the canonical example of *the abstraction leaking on purpose*: same verb (`DeployVisibilityPersistenceStore`), completely different implementation, and the interface is what forces you to notice.

**The control planes.** Also public: there are two. "One that is the user-facing control plane (User CP), which handles resources logically, and another that is the infrastructure control plane (Infra CP), which handles resources 'physically'... Those two control planes are actually Temporal Namespaces with separate workers." The Infra CP talks to the cloud providers; the User CP does not. The published Go shape is a `TemporalClusterProvider` interface plus a `GetTemporalClusterProvider` factory, with cloud-agnostic parent workflows spawning cloud-specific child workflows.

*Inference, clearly marked:* cell infrastructure work naturally sits underneath or beside the Infra CP. The published interface has methods like `DeployPersistenceStore`; the cell-level equivalents (network, cluster, node capacity, PKI, policy) are the natural siblings. Treat that as a plausible shape to ask about early, not as a fact.

**The ingress and identity story.** Publicly: connections are gRPC on port 7233; each Namespace gets a [Namespace endpoint](https://docs.temporal.io/cloud/namespaces) (`<namespace>.<account>.tmprl.cloud:7233`) and each region a regional endpoint (`<region>.<cloud>.api.temporal.io:7233`). Authentication is [mTLS](https://docs.temporal.io/cloud/certificates) with a customer-supplied CA certificate, or API keys. All traffic is TLS 1.3. When using a regional endpoint with mTLS, the client must set `server_name` to the Namespace endpoint, "since the request to the regional endpoint is redirected to the specific Namespace" — which tells you SNI-based routing exists in the ingress path. Private connectivity is AWS PrivateLink endpoint services and GCP Private Service Connect service attachments, both enumerated per region in the [regions doc](https://docs.temporal.io/cloud/regions).

**The rollout model.** Publicly described, and this is the part most relevant to "upgrade N cells safely": cells are organized into **deployment rings**. "Ring 0: Synthetic traffic only, no customer impact. Changes are monitored here for at least a week. Ring 1: Low-priority traffic namespaces... Higher Rings: Gradually expanding to critical, high-priority traffic customers. Within each ring, updates are applied in batches, with pauses between batches to observe for potential issues like memory leaks or race conditions." Note the specific failure classes named: memory leaks and race conditions. Those are *soak-time* bugs. They do not show up in a canary that runs for an hour.

### The namespace-to-cell mapping problem

This is the single most interesting design problem visible from outside, and it is worth having thought about before someone asks you.

Publicly, the mapping exists ("selecting a suitable cell within the chosen region") and the endpoint abstraction hides it ("A Temporal Client that uses a Namespace endpoint doesn't have to be aware of which region the Namespace is in"). Publicly, it is mutable — the AWS whitepaper lists "migrating cell customers" as a control-plane responsibility, and Temporal's Same-region Replication feature replicates a Namespace "across multiple cells in that region" with failovers "always managed automatically by Temporal."

The general problem, which every cell-based SaaS has (*industry pattern, not a Temporal-specific claim*):

- **Placement.** When a new tenant arrives, which cell? Naive round-robin ignores capacity. Least-loaded creates hot spots as load changes after placement. Bin-packing maximizes utilization and minimizes headroom for spikes. Most mature systems end up with a placement service that reads live cell capacity, honours affinity and anti-affinity constraints, and refuses to place when no cell has headroom — which means **"no capacity" must be a first-class, user-visible outcome**, not a provisioning timeout.
- **Lookup.** The router needs partition-key → cell in single-digit milliseconds, for every request, with a cache that fails safe. This is the "thinnest possible layer."
- **Rebalancing.** Tenants grow. A cell that was 40% full becomes 95% full because one customer 10x'd. Moving a tenant between cells means moving its data, which for Temporal means moving workflow state — hence replication-based migration rather than copy-and-cutover.
- **Draining.** When you need a cell gone (a bad node image, a failing AZ, an EOL cluster), you need every tenant off it, in an order you control, without dropping in-flight work.

Shopify's published [pods architecture](https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale) is the clearest external illustration of all four: a "Sorting Hat" in the load balancers matches every request to a pod via a rule list and stamps a header; a "Pod Mover" relocates a whole pod to its recovery data center "in a minute without dropping requests or jobs"; and "evacuating a whole data center is nothing more than evacuating each pod active there one at a time." Read it — it is three minutes long and it is the pattern in miniature.

### The dependency DAG of bringing up a cell

This is the heart of the guide. Everything below the horizontal rules in the other eleven guides is a node in this graph.

The graph is drawn for a generic Kubernetes-based cell on any of the three clouds. It is *the industry-standard ordering*, assembled from the primary docs cited throughout this library — it is not a claim about Temporal's internal pipeline.

```text
L0   CLOUD TENANCY        account / project / subscription  ->  quota grants
  |                       org policy / SCP / Azure Policy attached
  v
L1   IDENTITY             cloud roles for the provisioner  ->  OIDC provider registered
  |                       workload identity federation (IRSA / GKE WIF / Entra WID)
  v
L2   NETWORK              [often shared with a networking team]
  |                       IPAM allocation (pod CIDR, service CIDR, node subnets)
  |                         ->  VPC / VNet  ->  subnets  ->  route tables  ->  egress NAT
  |                         ->  private endpoints: KMS, registry, object store, metadata
  |                         ->  DNS zones + delegation
  v
L3   K8S CONTROL PLANE    EKS / GKE / AKS API server + etcd, private endpoint
  |                       cluster OIDC issuer published  ->  authn binding for CI
  v
L4   IN-CLUSTER DATAPATH  [nodes stay NotReady until this lands]
  |                       CNI DaemonSet (hostNetwork, tolerates the not-ready taint)
  |                         ->  conflist in /etc/cni/net.d  ->  nodes Ready
  |                         ->  CoreDNS  ->  cloud-controller-manager  ->  CSI driver
  v
L5   CAPACITY             bootstrap node group / Fargate profile / system pool
  |                         ->  Karpenter CRDs  ->  controller  ->  NodeClass Ready
  |                         ->  NodePool Ready  ->  workload nodes
  v
L6   PKI                  [nothing mTLS works before this]
  |                       region intermediate CA already exists (created ahead of demand)
  |                         ->  cert-manager  ->  cell issuer chained to the region CA
  |                         ->  trust-manager  ->  CA bundle in every namespace
  v
L7   SECRETS              cloud KMS key + key policy  (needs L1 identity, L2 network)
  |                         ->  Vault  ->  auto-unseal  ->  init  ->  audit devices
  |                         ->  Kubernetes auth  ->  DB engine, PKI mount, transit
  v
L8   POLICY + TELEMETRY   Kyverno controller (webhook cert from L6)
  |                         ->  generating policies  ->  baseline objects per namespace
  |                         ->  metrics / logs / traces agents  ->  fleet backend
  v
L9   DATA STORES          persistence (Cassandra or SQL)  +  visibility (ES / SQL)
  |                       credentials from L7, TLS from L6, disks from L4
  |                         ->  schema created at the target version
  v
L10  TEMPORAL SERVICES    history  ->  matching  ->  frontend  ->  worker
  |                       Ringpop membership converges; internal mTLS from L6
  v
L11  TRAFFIC REGISTRATION [networking team owns the far side]
  |                       LB / ingress  ->  mTLS termination  ->  health targets green
  |                         ->  DNS record  ->  private link publication  ->  router registry
  v
L12  HEALTH GATE          synthetic namespace  ->  workflow start  ->  complete,
                          through the REAL external endpoint with a REAL client cert
                            ->  cell marked ready for placement
```

Now the edges. For each, why it must come first and what breaks when it does not.

| Edge | Why this order | What fails if you get it wrong |
|---|---|---|
| L0 → L1 | Roles and policies are scoped to a tenancy container that must exist first | Role creation silently lands in the wrong account/project; you find out at teardown when the sweeper cannot see it |
| L0 → all | Quota is per account × region (AWS), project × region (GCP), subscription × region (Azure) — see [03](03-multicloud-aws-gcp-azure.md) | Provision proceeds to 80% and then fails on an ENI, IP, or vCPU quota, leaving a half-cell that costs money and holds a CIDR |
| L1 → L2 | Creating network objects requires a principal with permission; workload identity federation must be registered before anything in-cluster can assume a role | Pods come up and get `AccessDenied` from the cloud SDK an hour later, in a component you were not watching |
| L2 (IPAM first) | Pod and service CIDRs are effectively immutable once workloads land; max-pods-per-node is immutable per node pool — see [05](05-cni-and-host-networking.md) | Overlapping CIDRs across cells make Cluster Mesh, peering, and migration impossible forever. This is the one mistake with no fix short of rebuilding the cell |
| L2 → L3 | The managed control plane is created *into* subnets and must be able to reach nodes | Cluster created in the wrong subnets; private endpoint unreachable from CI; you rebuild |
| L2 (egress before L4) | Nodes pull container images and reach the cloud API before anything works | Nodes join, then every pod is `ImagePullBackOff` and the failure looks like a registry problem |
| L3 → L4 | The CNI DaemonSet is a Kubernetes object; the API server must exist to accept it | — |
| L4: CNI before node Ready | The kubelet reports `NotReady` with `cni plugin not initialized` until a conflist appears in `/etc/cni/net.d`. The CNI must therefore be `hostNetwork: true` and tolerate the not-ready taint | A CNI manifest missing the toleration produces a cell that provisions cleanly and never converges. Health checks that run before this settles report false failures |
| L4: CoreDNS after CNI | CoreDNS runs on the pod network | CoreDNS pends forever; every subsequent component's DNS-based discovery fails in a way that looks like the component is broken |
| L4: CCM before LoadBalancer Services | The cloud controller manager is what turns a `Service type=LoadBalancer` into a cloud LB | Services sit `<pending>` and the ingress step at L11 never completes |
| L4: CSI before StatefulSets | PVCs bind through the CSI driver; use `WaitForFirstConsumer` so the volume lands in the same zone as the pod | `Immediate` binding puts a zonal disk in a zone with no capacity; the pod is unschedulable forever |
| L4 → L5 | Karpenter is a pod; it needs a node, DNS, and the API server | See "the Karpenter chicken-and-egg" below |
| L5 → L6 | cert-manager is a pod; it needs capacity | — |
| L6 before L7 | Vault's TLS, and any in-cluster CA-signed client cert, come from cert-manager. The region intermediate must pre-exist because signing it is a human ceremony — see [11](11-cert-manager-and-pki.md) | A missing region intermediate blocks the cell on a human, at 2 a.m., with no automated path forward |
| L6 trust before use | "Distribute trust before you use it. Remove trust after you stop using it. Never in the other order." | Rotate first and every client that has not yet received the new bundle rejects every connection. This is the classic internal-PKI outage |
| L1+L2 → L7 | Vault auto-unseal calls cloud KMS, which needs an identity and a network path | See "the unseal loop" below. Symptom: Vault sealed. Root cause: three layers down |
| L7 → L9 | Database credentials are dynamic, issued by Vault | Static credentials in a values file, which then never rotate and end up in git |
| L6 → L8 | Kyverno's admission webhook needs a serving certificate the API server trusts | See "the webhook deadlock" below |
| L8 before tenant namespaces | Kyverno `generate` rules fire on namespace creation; without `generateExisting`, namespaces created earlier get nothing | Half your namespaces have a NetworkPolicy and half do not, and the difference is creation order |
| L8 observability before L9 | You need to be able to *see* the database bootstrap fail — see [14](14-observability-for-cells.md#the-prometheus-operator-and-how-a-cell-gets-monitoring-at-bring-up) | Otherwise you debug a silent cell with `kubectl logs` and guesswork |
| L9 schema before L10 | Temporal will not run against an older schema: "Not supported: running newer binaries with an older schema" ([Helm chart README](https://github.com/temporalio/helm-charts)) | Server pods crash-loop with a schema version error, which reads like an application bug |
| L10 internal order | History owns state; Matching dispatches; Frontend fans in. Ringpop membership needs the peers to be discoverable | Frontend accepts traffic before History has claimed its shards, and the first requests fail |
| L10 → L11 | You register a cell with the router only once it can serve | Register early and the router sends real traffic into a cell that is still converging |
| L11 → L12 | "Cell is ready" means an external client completed a real RPC, not that pods are `Running` — see [02](02-grpc.md), and [14](14-observability-for-cells.md#the-cell-health-gate) for the gate that evaluates it | A cell that passes readiness on pod status and fails on the first customer connection, because of SNI, a missing SAN, an idle timeout, or an L4 LB pinning every request to one frontend |

Two structural notes on the DAG.

**The Terraform/Kubernetes boundary sits between L3 and L4.** Terraform's rule is explicit: "You can use expressions to configure provider arguments, but you can only reference values that Terraform knows before it applies your configuration" ([provider block docs](https://developer.hashicorp.com/terraform/language/block/provider)). A cluster endpoint is unknown until the cluster exists, so you cannot create a cluster and manage Kubernetes resources against it in one apply. This is not a style choice — it is why L0–L3 is one or more Terraform applies and L4–L12 is something else. See [08](08-terraform.md).

**Everything from L4 down is level-triggered; everything above is transactional.** Cloud resources are created once and then drift; Kubernetes objects are continuously reconciled. That difference is why the drift-detection story is different on the two sides of the line, and why your teardown has to handle both a "delete these objects" path and a "sweep the cloud by tag" path. The reconciler that does the level-triggered half — and owns the inventory, the pruning, and the health assessment for L4–L12 — is guide [13](13-gitops-argocd-flux.md).

### The bootstrap paradoxes

Six real cycles in that graph. Each has a name, a cause, and a standard resolution. Learn them as a set; they are the highest-signal thing you can know about a cell platform.

**1. The Karpenter chicken-and-egg — the autoscaler needs a node to run on.**

Karpenter is a Deployment. Deployments need nodes. Karpenter is what creates nodes. Standard resolutions, in rough order of preference:

- A small **bootstrap node group** (EKS managed node group, GKE default pool, AKS system pool) with 2–3 nodes, sized only for the platform add-ons, tainted so workloads never land there. Karpenter, CoreDNS, cert-manager, and the GitOps agent live there.
- On AWS specifically, a **Fargate profile** for the `karpenter` namespace, so Karpenter runs with no EC2 instance at all.
- On AKS/GKE, the provider's **system node pool** is exactly this by design.

The trap: the bootstrap pool is now a thing you must also upgrade, and because it is small, a single unavailable node is 33% of your platform capacity. Give it its own PDB story and its own upgrade step. See [06](06-karpenter.md).

**2. The self-signed root — cert-manager cannot issue a certificate to the CA it needs in order to issue certificates.**

A `CA` issuer needs a Secret containing a CA key pair. Something has to make that key pair. cert-manager's answer is the `SelfSigned` issuer: a special issuer that signs a certificate with its own key. You use it exactly once, to mint a root, and then you never use it again:

```text
SelfSigned issuer  →  root Certificate (isCA: true)  →  CA ClusterIssuer (root)
                            →  intermediate Certificate (isCA: true)
                                     →  CA ClusterIssuer (cell issuer)
                                              →  every leaf certificate
```

In production the root is not self-signed in-cluster at all — it is an offline root, or a cloud private CA, or a Vault PKI mount whose key was signed by a region CA in a human ceremony. The self-signed bootstrap is the *lab* resolution and the *day-zero* resolution; the production resolution is "the CA already exists, created ahead of demand." See [11](11-cert-manager-and-pki.md).

**3. The webhook deadlock — Kyverno needs a certificate from cert-manager, and cert-manager's pods must pass Kyverno's admission webhook.**

A validating webhook with `failurePolicy: Fail` that is unreachable blocks every matching API request cluster-wide. So: cert-manager issues Kyverno's webhook cert. Kyverno validates every pod, including cert-manager's. Kill both and neither can start.

Four standard resolutions, usually combined:

- **Namespace exclusions.** Exclude `kube-system`, `cert-manager`, and the policy engine's own namespace from the webhook's `namespaceSelector`. Kyverno excludes several by default; know which, and check [Kyverno's configuration docs](https://kyverno.io/docs/installation/customization/) rather than assuming.
- **Self-signed webhook certs.** Kyverno generates and rotates its own webhook certificate by default, precisely so it does not depend on cert-manager. Using cert-manager for it is an *option*, and taking that option creates this cycle. Consider not taking it.
- **Ordering.** Install the policy engine *controller* early and the *policies* late. Kyverno registers no webhook rules until policies exist, which decouples the two cleanly.
- **A break-glass runbook.** `kubectl delete validatingwebhookconfiguration <name>` is the recovery, and it must be a documented, rehearsed, permissioned procedure — not something someone invents during an outage. A fail-closed orphaned webhook pointing at a Service that no longer exists is the classic bricked cluster. See [07](07-kyverno.md).

**4. The unseal loop — Vault's auto-unseal needs cloud KMS, which needs an identity, which needs a network path.**

Vault starts sealed: it has the ciphertext but not the key ([seal/unseal concepts](https://developer.hashicorp.com/vault/docs/concepts/seal)). Auto-unseal asks a cloud KMS to decrypt the root key. That call needs: a working pod network (L4), DNS (L4), an egress path or private endpoint to the KMS API (L2), a workload identity binding (L1), and a key policy granting Decrypt (L0/L1). Five dependencies, and the symptom for all five is identical: **Vault is sealed**.

The resolution is not architectural — it is diagnostic. Build, and keep, a single diagnostic that distinguishes the five cases: can the pod resolve the KMS hostname, can it reach the endpoint, does it have a token, does the token map to the expected principal, does the principal have Decrypt. Guide [10](10-vault.md) says it well: "If it does not, you have a networking or IAM problem, not a Vault problem — check in that order." Write that check once and every future cell bootstrap failure of this class costs five minutes instead of two hours.

The second-order version of this paradox: **anything that needs Vault to start cannot be something Vault needs to start.** Vault's own TLS certificate is the trap. If Vault's serving cert comes from cert-manager, and cert-manager's issuer is Vault PKI, you have built a cycle. Break it by giving Vault a certificate from a different, simpler issuer (the cell's cert-manager CA issuer, chained to the region intermediate) — never from the Vault PKI mount that Vault itself hosts.

**5. The GitOps bootstrap — the agent that deploys everything cannot deploy itself.**

If Argo CD or Flux applies every manifest in the cell, something has to apply Argo CD or Flux. Standard resolutions:

- **Terraform installs exactly one thing** — the agent — in a separate apply after the cluster exists, and then stops. Everything downstream is the agent's problem. This keeps Terraform's state small and keeps the "reachable cluster required for destroy" problem contained. See [08](08-terraform.md).
- **App-of-apps / self-management.** The agent's first Application points at the agent's own manifests, so subsequent upgrades of the agent flow through the same pipeline as everything else. Elegant, and it has a sharp edge: a bad agent manifest can make the agent unable to fix itself, so keep the bootstrap apply path alive and tested as the recovery route.
- **Rendered manifests make this less scary.** Because this library assumes Helm for templating only ([09](09-helm.md)), the artifact is plain YAML in git. Worst case, `kubectl apply --server-side -f rendered/` from a laptop is a valid recovery path. Preserve that property deliberately; it is worth more than it looks.

**6. The Terraform provider paradox — you cannot plan Kubernetes resources against a cluster that does not exist yet.**

Covered above as a DAG note, but it belongs in this list because it is the same shape as the others. The resolution is *split the apply*, and the consequence is that "provision a cell" is inherently a multi-step orchestration rather than a single command — which is precisely the argument for a durable workflow driving it.

**Two honourable mentions.**

- **The CNI/not-ready paradox.** Nodes are `NotReady` until the CNI initializes, and the scheduler will not place pods on `NotReady` nodes — except the CNI DaemonSet itself, which runs `hostNetwork: true` and tolerates the not-ready taint. Every CNI ships this configuration; the failure mode is a homegrown or modified manifest that drops the toleration. See [05](05-cni-and-host-networking.md).
- **The observability paradox.** The tooling you would use to debug a failing bootstrap is installed *during* the bootstrap. Resolution: the cell provisioning workflow itself must emit rich, structured progress from *outside* the cell — activity-level events with the layer name, the assertion that failed, and the last successful gate. If your only view into a stuck cell is `kubectl` from a laptop, that is a real gap and a very good first-90-days project.

### The steady-state loop

Once a cell exists, nothing about it is "done." The loop:

```text
   change to desired state (git commit)
              │
              ▼
   RENDER      helm template + kustomize  →  rendered/<cell>/*.yaml
              │   pure function: chart + values → YAML, no cluster contact
              ▼
   REVIEW      PR diff IS the change plan (real images, limits, replicas)
              │   policy gate: conftest / kyverno CLI / kubeconform
              ▼
   APPLY       GitOps agent  →  kubectl apply --server-side
              │   field manager owns fields; prune removes what left the set
              ▼
   CONVERGE    controllers reconcile; Karpenter provisions; certs issue
              │
              ▼
   VERIFY      health gate re-run  →  conformance suite  →  fleet metrics
              │
              └──────────► drift detector ──► back to the top
```

**Where drift comes from,** in descending order of frequency: humans (`kubectl edit` during an incident, never reverted); other controllers writing a field your pipeline also writes, which surfaces as a server-side-apply field-ownership conflict and is *good* — see the [SSA docs](https://kubernetes.io/docs/reference/using-api/server-side-apply/) and guide [09](09-helm.md); the cloud itself (an auto-upgraded control plane, a provider-rotated node image, a security group another team's automation touched); time (a renewed certificate, a rotated token, a consolidated node — not real drift, but a naive detector cannot tell); and someone clicking in a console, which only `terraform plan` will ever see.

**How drift is detected,** at three layers:

- **Kubernetes objects** — the GitOps agent's own `OutOfSync` status, per-application, exported as a fleet metric with a cell label. See [13](13-gitops-argocd-flux.md#drift-emergency-changes-and-break-glass) for how each tool detects and reports it, and what happens to a hand-edit during an incident.
- **Cloud resources** — nightly `terraform plan -detailed-exitcode` per cell state, ticketing on exit code 2. Cheapest high-value job you can build.
- **Semantic conformance** — the layer most teams skip and the one with the most value. It is the same shape as the cell health gate in [14](14-observability-for-cells.md#the-cell-health-gate), run continuously rather than once at bring-up. A per-cell suite that asserts the *properties* the cell is supposed to have, not the manifests: DNS resolves privately, workload identity mints a token, KMS decrypt works, the ingress does per-request gRPC balancing, egress leaves via the intended NAT, kube-proxy mode is explicitly set, no `Immediate` block StorageClass, no `maxUnavailable: 0` PDB, every APIService healthy, kubelet skew under 2, days-to-EOL above threshold. Run it continuously and publish "how many cells are non-conformant and why" as one number.

The important framing, from the rendered-manifests literature: "Git contains the inputs to the desired state, not the desired state itself" ([Akuity](https://akuity.io/blog/the-rendered-manifests-pattern)). Rendering into git converts that into "git contains the desired state," which is what makes the PR diff a real review surface and what makes `git revert` a real rollback.

### Cell upgrade

Upgrading one cell is a pipeline with a fixed order. Upgrading three hundred is a scheduling problem layered on top of it. Do not conflate them.

**The per-cell order, and why.**

1. **Pre-flight.** Deprecated API scan, PDB lint, add-on compatibility matrix, quota headroom check (surge capacity temporarily doubles vCPU), APIService health, and — for Temporal specifically — a schema compatibility check. Every one of these is cheaper than a rollback.
2. **Cloud infrastructure.** Anything below the cluster that the new version needs: a new subnet, a new security group rule, an IAM permission the new add-on version requires.
3. **Kubernetes control plane.** One minor version at a time. The [version skew policy](https://kubernetes.io/releases/version-skew-policy/) permits kubelet to be up to three minors behind the API server but never ahead, which is exactly why the control plane goes first and nodes follow.
4. **Cluster add-ons that gate the datapath.** CNI, CoreDNS, kube-proxy, CSI. The CNI in particular has its own compatibility matrix against the Kubernetes version and against the node kernel.
5. **Karpenter itself,** before the control plane in some matrices — check [Karpenter's compatibility page](https://karpenter.sh/docs/upgrading/compatibility/) rather than assuming, because the constraint runs the other direction from what people expect.
6. **Node images.** With Karpenter this is a declarative bump of the AMI/image selector, which marks every NodeClaim `Drifted` and rolls them, paced by a disruption budget. Elegant, and only safe with a pinned image (not `@latest`), an explicit budget, and `terminationGracePeriod` set.
7. **Policy.** Kyverno's Kubernetes support window is narrow; pin the CLI to the target version and run `kyverno test` against the whole policy set in CI before touching a cell.
8. **Database schema, then application.** For Temporal this order is *load-bearing* and publicly documented: "Not supported: running newer binaries with an older schema. Supported: downgrading binaries — running older binaries with a newer schema" ([Helm chart README](https://github.com/temporalio/helm-charts)). Schema first is also what makes rollback possible.
9. **Temporal Server, one minor at a time.** From the [upgrade docs](https://docs.temporal.io/self-hosted-guide/upgrade-server): "Temporal Server should be upgraded sequentially, one minor version at a time. Before bumping to the next minor version, first upgrade to the highest available patch version of your current minor version." Skipping versions can make old data formats unreadable. And a number worth putting in your runbook: "each upgrade requires the History Service to load all Shards and update the Shard metadata, so allow approximately 10 minutes on each version."

**The fleet problem: upgrade N cells safely.**

Temporal's published model is deployment rings — Ring 0 synthetic-only, monitored for at least a week; Ring 1 low-priority namespaces; higher rings progressively more critical; batches within each ring with pauses between them. Generalizing that into the pieces you would build (*industry pattern*):

- **Canary cells that carry real but low-stakes traffic.** A cell with only synthetic traffic will not find the bug that only appears under a real workload mix. Temporal's model has both, which is the right answer.
- **Soak time measured in days, not minutes.** The failure classes Temporal names — memory leaks and race conditions — are time-dependent. A one-hour canary cannot see them. This is the single most common way a wave-based rollout is quietly useless.
- **A wave schedule with explicit gates.** 1 cell → 5 cells → 10% → 50% → rest, with a defined pass criterion between waves. The criterion must be a *metric comparison against unupgraded siblings*, not "no pages fired." Cells give you a natural control group; use it.
- **Concurrency limits and a pause button.** A human must be able to stop the fleet mid-rollout, and a stopped rollout must be resumable rather than restartable. Terraform gives you a diff and a lock but "has no notion of rollout" — that logic belongs in the orchestrator, and a durable workflow with a signal-based approval gate is the natural shape. See [08](08-terraform.md).
- **Rollback that is honest about what cannot roll back.** Schema migrations, CRD version bumps, and anything that rewrote data are one-way. Know which steps in your pipeline are one-way *before* you start, and put the reversible steps first.

**PDBs, drain, and long-running Temporal workloads.**

This is where a generic upgrade pipeline meets a specific application, and it is worth being precise.

- A [PodDisruptionBudget](https://kubernetes.io/docs/tasks/run-application/configure-pdb/) constrains *voluntary* disruption — the [Eviction API](https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/) that `kubectl drain` and Karpenter use. It does nothing about node failure, and `maxUnavailable: 0` makes a node undrainable forever, which is how a drift rollout wedges.
- Temporal Frontend is stateless and drains like any gRPC server: `GracefulStop`, a `terminationGracePeriodSeconds` longer than your longest RPC, a `preStop` sleep long enough for endpoint removal to propagate, and `MaxConnectionAge` already spreading reconnections so the herd is not synchronized. Done wrong you get a wall of `UNAVAILABLE` and a synchronized re-poll stampede. See [02](02-grpc.md).
- **Worker long polls are the specific hazard.** Temporal SDK workers hold long-poll RPCs open (tens of seconds) against Matching. Any proxy or LB idle timeout shorter than the poll expiration turns a normal rollout into a burst of errors. Assert `route_timeout > long_poll_expiration + margin` in CI, per cloud, and treat it as a cell invariant rather than a tuning detail.
- **History is stateful in the way that matters.** Shard ownership moves when a History pod goes away; that movement is normal and fast, but a rollout that replaces History pods faster than shards can be reclaimed produces a latency spike that looks like a database problem. Roll History slowly, and watch shard-ownership metrics rather than pod readiness.

### Cell teardown

Teardown is the least-tested path in every infrastructure system, and it is the one that silently costs money forever. Design it as a first-class, continuously-exercised code path, not as an afterthought.

**The reverse DAG.**

1. **Stop placement.** Mark the cell ineligible in the router registry so no new tenant lands on it. Nothing else can proceed while new work is arriving.
2. **Drain tenants.** Migrate or expire every namespace. Verify zero in-flight work rather than "no traffic for N seconds" — for gRPC, channelz gives you actual in-flight stream counts, which is a much better gate ([02](02-grpc.md)).
3. **Deregister from traffic.** Remove DNS records, remove LB targets, tear down private-link publications. Do this before deleting anything the LB points at, or you leave the router pointing at a black hole.
4. **Stop the reconcilers.** Delete the GitOps Application, or the agent will faithfully recreate everything you are about to delete. This is the step people forget, and the symptom is a teardown that appears to loop.
5. **Delete workloads, then Kubernetes-owned cloud objects.** `LoadBalancer` Services and bound PVCs create cloud resources through the CCM and CSI. Delete the Kubernetes objects and *verify* zero remaining LoadBalancer Services and zero bound PVCs before deleting the cluster. Deleting the cluster first orphans them permanently.
6. **Revoke secrets before destroying the secret store.** Vault leases must be revoked (`vault lease revoke -prefix` per mount) so external systems — databases, cloud IAM — actually clean up. Destroy Vault first and you leave orphaned IAM users and database roles that nobody will ever find. See [10](10-vault.md).
7. **Delete node pools, then the cluster.**
8. **Delete the network,** then the identity objects, then the tenancy container.
9. **Sweep by tag** and assert zero remaining objects.
10. **Tombstone the name and the CIDR** so neither is reused before the cloud's own reservation windows expire.

**Finalizer traps.** [Finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/) are the mechanism that makes ordered teardown possible and the mechanism that wedges it. The specific ones to know:

- A namespace stuck `Terminating` because an APIService backing a CRD in it is unavailable — the "ghost APIService" problem. The API server cannot enumerate resources it cannot reach, so it waits forever.
- A Karpenter `NodeClaim` held by a `do-not-disrupt` pod, a `maxUnavailable: 0` PDB, or a stuck `VolumeAttachment`. `terminationGracePeriod` on the NodePool is the bound that makes teardown actually terminate; argue for making it mandatory.
- A Kyverno generating policy with `synchronize: true` recreating objects inside a terminating namespace. Delete the policies before the namespaces.
- `prevent_destroy` on a Terraform resource, which blocks `terraform destroy` entirely and needs a documented, reviewed lift procedure rather than an ad-hoc edit.

**The orphan inventory.** Enumerate it explicitly, per cloud, and make the sweeper's assertions match. The usual suspects: load balancers and target groups; public IPs; block volumes and their snapshots; **ENIs left attached-or-detached-but-not-deleted on AWS** (these consume subnet IPs that the next cell needs); security groups; DNS records; IAM roles, policies, and instance profiles; OIDC providers; KMS keys (with their pending-deletion windows); soft-deleted Azure Key Vaults; GCP key rings that cannot be deleted at all; permanently-burned GCP project IDs; workload-identity-pool name reservations; private-endpoint connections left pending on the consumer side; and container images or artifacts tagged with the cell ID.

**How to verify a cell is actually gone.** Three independent checks, because each can lie:

1. **Kubernetes says so** — the cluster does not exist. Weakest signal.
2. **The cloud API says so** — enumerate every resource type by `cell-id` tag in every region the cell touched, and assert zero. This must be a real, tested code path with a dry-run mode that runs on *every* teardown, not just the ones you are worried about.
3. **The bill says so** — a per-cell cost query that returns zero for the following month. This is the only check that catches resource types your sweeper does not know about, which is exactly the class of orphan that matters.

### Failure modes and blast radius

**What a single-cell failure looks like.** By construction: a subset of tenants, in one region, on one cloud, sees elevated errors or latency. Everyone else is fine. The dashboards that matter are therefore **per-cell**, and the alerting question is not "is the service down" but "how many cells are unhealthy and which tenants are on them." If your fleet dashboard cannot answer that in one glance, the cell architecture is not paying for itself.

**Gray failure is the hard case.** Microsoft's [gray failure paper](https://www.microsoft.com/en-us/research/wp-content/uploads/2017/06/paper-1.pdf), which Slack cites in its cellular writeup, defines it precisely: "different components have different views of the availability of the system." Slack's [account of the 2021-06-30 incident](https://slack.engineering/slacks-migration-to-a-cellular-architecture/) is the best short description of why this defeats automatic remediation — systems inside the affected zone saw local backends as healthy and remote ones as down; systems outside saw the reverse; even two clients in the same zone disagreed depending on which network path they took. No health-checking algorithm resolves that cleanly.

Slack's conclusion is the one to internalize: **stop trying to automate the diagnosis, and build a button instead.** Their design goals for that button are worth copying almost verbatim:

1. Remove as much traffic as possible from a cell within 5 minutes.
2. Drains must not cause user-visible errors — so a drain is a *generic mitigation* an operator can try during an incident and undo if it does not help.
3. Drains must be incremental, down to 1% granularity, so you can test a recovery.
4. **The draining mechanism must not depend on resources in the cell being drained.** "It's not OK to activate a drain by just SSHing to every server."

Point 4 is the one that gets designed wrong. If the drain control lives in the cell, the drain does not work in exactly the situation you built it for.

**Cell evacuation and migration.** Two different operations with two different costs:

- **Evacuation** moves *traffic* away, leaving the data where it is. This is fast, reversible, and the right first response to a suspected cell problem. It requires the tenant's state to be reachable from another cell — which for Temporal is what replication provides. Temporal's publicly documented Same-region Replication does exactly this: replicate a Namespace "across multiple cells in that region," with cell-to-cell failovers "always managed automatically."
- **Migration** moves *state* to another cell. Slow, one-way in practice, and the tool you use for capacity rebalancing rather than incident response. The AWS whitepaper lists "migrating cell customers" as a control-plane responsibility for exactly this reason.

**The cell-level circuit breaker.** *Industry pattern.* The router decides, per cell, whether to keep sending traffic to it. Three properties make it safe:

- The decision is made **outside** the cell, from signals the cell cannot suppress (external synthetic probes, error rates measured at the router, not health endpoints the cell serves about itself).
- It is **partial and weighted**, not binary — Slack drains by reweighting per-cell clusters at the edge, which lets you take a cell to 10% and watch, rather than 0% and pray.
- It has **anti-flap and a floor**: if enough cells trip that the survivors would be overloaded, tripping more makes the outage worse. A circuit breaker that can take down the whole fleet is not a safety device.

**Why cells make some incidents boring.** This is the actual payoff and it is worth saying explicitly. A bad deploy is caught in Ring 0 against synthetic traffic. A poison-pill request corrupts one cell's cache, not the fleet's. A hot tenant saturates one cell's database, and the blast radius is the tenants sharing it. A failing AZ or a bad node image is a drain, then a leisurely investigation. A quota cliff is hit by one cell that fails to scale — an alert, not a fleet-wide outage. Each of those is a page that turns into a ticket. The cost is that provisioning, upgrading, and deleting a cell must be *routine*, because you will do all three constantly. That cost is your team's job, and it is the reason the job exists.

### Where senior engineers add leverage

An honest section, because the honest version is more useful than the flattering one. The Senior version of this job — understand the eleven tools deeply, ship reliable changes, debug hard problems fast — is a lot, and it is table stakes here rather than the differentiator. The senior version is about **leverage**, and for cell lifecycle across three clouds, the leverage is concentrated in four places.

**1. The abstractions between clouds.** Temporal has already published its answer — an interface of verbs (`DeployPersistenceStore`, `DeployVisibilityPersistenceStore`) with per-cloud implementations behind a factory, and cloud-agnostic parent workflows spawning cloud-specific child workflows. The senior work is not writing that pattern; it is *policing the boundary*. Specifically: which facts are genuinely per-cloud (IPAM model, max-pods math, LB provisioning, egress NAT and its port limits, MTU, teardown ordering, version support windows) versus which are accidental divergence that crept in because someone was in a hurry. The second list should shrink every quarter, and someone has to be the person who keeps a written record of both. Adding a method to the provider interface "will remind us to add it for other cloud providers" — that forcing function only works if someone defends it.

**2. The bootstrap ordering contract.** The DAG above is currently, in most organizations, distributed across a workflow definition, a few Terraform modules, a GitOps sync-wave annotation, and three people's heads. Making it an *explicit, tested, single-source artifact* — with named gates, per-layer assertions, and a diagnostic that tells you which layer failed and why — is a genuinely senior-scoped deliverable, and it pays off every single time a cell gets stuck. The measurable outcome is mean-time-to-diagnose for a stuck provision.

**3. The upgrade safety story.** This is where a fleet either compounds or accumulates debt. Concretely: the wave schedule, the soak criteria, the automated comparison against unupgraded siblings, the pause and resume semantics, the pre-flight checks that make a wave abort before it starts rather than halfway through, and the honest catalogue of which steps are irreversible. If your team can upgrade every cell on three clouds through a Kubernetes minor bump without anyone staying up late, that is a senior-level artifact — and if it takes a quarter and three incidents, that is the gap.

**4. The tests and gates that let people ship without fear.** The highest-leverage code on a platform team is usually a test. Rendered-manifest snapshot diffs. `terraform test` on the cell modules. `kyverno test` on the policy set, pinned to the target Kubernetes version. CI assertions that coupled values agree across templates (gRPC keepalive versus LB idle timeout versus long-poll expiration versus termination grace). A conformance suite that runs against every live cell. A teardown sweeper with a dry-run mode that runs on every teardown. None of these is glamorous; all of them convert tribal knowledge into a compile-time error, which is the definition of scaling a team.

**What to look at, question, and measure in the first 90 days.**

- **Look at:** the cell entity workflow (or whatever drives provisioning) end to end, in all three clouds; one real teardown with cloud audit logs open; the fleet inventory, whatever it is called; the last three cell-related incidents' write-ups.
- **Question:** every per-cloud branch (is it a real difference or accidental?); every shared dependency (what happens to a cell when it is down?); the cell size limit (is it measured or assumed?); the placement algorithm (what happens when no cell has room?); the soak criteria (what metric, compared against what?).
- **Measure:** time to provision a cell, p50 and p99, per cloud; time to tear one down; number of manual interventions per 10 cell operations; number of non-conformant cells and why; orphaned-resource cost per month; mean time to diagnose a stuck provision.

That last list is the one to bring to your manager in week two. Numbers you can move are how a senior engineer picks what to work on.

---

## Hands-on

**Build a miniature cell on your laptop.** This is the single most valuable exercise in the library, because it is the only one that exercises the *composition*. Everything here mirrors a layer of the real DAG, in the real order, and every failure you hit is a scale model of a failure you will hit at work.

What you build: a `kind` cluster with the default CNI and kube-proxy deliberately removed, Cilium as the datapath, a two-level PKI with trust distribution, Vault as the secret store, Kyverno as the policy gate, a rendered-manifest pipeline with server-side apply, a Postgres persistence layer, and a real Temporal Server on top — brought up in dependency order, with a health gate at the end that proves an external client can complete a workflow.

Budget: 3–4 hours the first time. Do it in one sitting.

**Prerequisites.** A running Docker (or Podman/Colima with a Docker-compatible socket) with **at least 8 GB of memory and 4 CPUs** allocated — the three-node kind cluster plus Cilium, cert-manager, Vault, Kyverno, Postgres, and four Temporal services will not fit in less. Plus the CLIs in Step 0, outbound internet for Helm repos and image pulls, and roughly 5 GB of image downloads. Everything runs locally; there is no cloud account and no cost.

### Step 0 — Tools and a sanity check

```bash
# Required. Docker must already be running — kind builds its nodes as containers.
brew install kind kubectl helm cilium-cli jq yq step temporal
docker info >/dev/null && echo "docker ok"
# or the equivalent for your platform — see kind.sigs.k8s.io, docs.cilium.io,
# smallstep.com/docs/step-cli, and docs.temporal.io/cli for install instructions.

kind version && kubectl version --client && helm version --short && temporal --version
```

Before building anything, see what "working" looks like. The Temporal CLI ships a full in-process dev server:

```bash
temporal server start-dev --db-filename /tmp/temporal-lab.db --ui-port 8080
```

In a second shell:

```bash
temporal operator cluster health
temporal operator namespace create --namespace lab
temporal workflow start --type NoopWorkflow --task-queue lab-tq \
  --workflow-id smoke-1 --namespace lab
temporal workflow list --namespace lab
```

The workflow will sit `Running` because there is no worker — that is fine and it is the point. It proves Frontend accepted the request, History persisted it, and Visibility indexed it. Open `http://localhost:8080`, look at the Event History, then `Ctrl-C`. You now know what the far end of the DAG looks like.

Make a working directory:

```bash
mkdir -p ~/cell-lab/{charts,values,rendered,manifests} && cd ~/cell-lab
```

### Step 1 — L0–L3: the "cloud account" and the Kubernetes control plane

`kind` collapses layers 0 through 3 into one command, but you can still make the CNI edge visible by refusing the defaults.

```yaml
# ~/cell-lab/kind-cell.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: cell-lab
networking:
  disableDefaultCNI: true      # no kindnet — we install our own datapath
  kubeProxyMode: "none"        # Cilium will replace kube-proxy
  podSubnet: "10.244.0.0/16"   # the IPAM decision, made once, immutable
  serviceSubnet: "10.96.0.0/16"
nodes:
  - role: control-plane
  - role: worker
  - role: worker
```

```bash
kind create cluster --config kind-cell.yaml
```

Now look at what you have. This is L3 with no L4:

```bash
kubectl get nodes -o wide
# STATUS: NotReady   — every node

kubectl get pods -n kube-system
# coredns-*  Pending   — it needs pod networking that does not exist yet

kubectl describe node cell-lab-worker | grep -A3 'Ready '
# KubeletNotReady ... container runtime network not ready:
#   NetworkReady=false ... cni plugin not initialized
```

**Stop and read that.** This is the exact message every real cell emits between L3 and L4. If your provisioning health check runs here, it reports a false failure. Encode gates, not sleeps.

### Step 2 — L4: the CNI, and nodes becoming Ready

```bash
# The API server address the CNI needs, because there is no kube-proxy
# to give it a working ClusterIP for the kubernetes Service.
API_SERVER_IP=$(docker inspect cell-lab-control-plane \
  -f '{{ .NetworkSettings.Networks.kind.IPAddress }}')
echo "$API_SERVER_IP"

helm repo add cilium https://helm.cilium.io/
helm repo update

# Pin the version explicitly. Never render from a floating chart.
CILIUM_VERSION=$(helm search repo cilium/cilium -o json | jq -r '.[0].version')
echo "pinning cilium chart ${CILIUM_VERSION}"

helm template cilium cilium/cilium \
  --version "${CILIUM_VERSION}" \
  --namespace kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="${API_SERVER_IP}" \
  --set k8sServicePort=6443 \
  --set operator.replicas=1 \
  > rendered/10-cilium.yaml

kubectl apply --server-side --force-conflicts \
  --field-manager=cell-pipeline -f rendered/10-cilium.yaml

cilium status --wait
kubectl get nodes          # Ready
kubectl get pods -n kube-system   # coredns now Running
```

Note what just happened in DAG terms: **the CNI made the nodes Ready, and node-Ready made CoreDNS schedulable.** Two edges, one command. Everything from here on depends on both.

Prove the datapath works before trusting it:

```bash
kubectl create deployment web --image=nginx --replicas=3
kubectl expose deployment web --port=80
kubectl run probe --rm -it --image=nicolaka/netshoot --restart=Never -- \
  sh -c 'nslookup web.default.svc.cluster.local && curl -s -o /dev/null -w "%{http_code}\n" http://web'
# 200
kubectl delete deployment web && kubectl delete svc web
```

If DNS resolves and the ClusterIP answers, L4 is green. See [05](05-cni-and-host-networking.md) for what each of those two facts actually required.

### Step 3 — The render pipeline (Helm as templating only)

Before installing anything else, set up the pattern your team actually uses. Everything downstream goes through this.

```bash
cat > apply.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# Apply rendered YAML with a stable field manager.
# Field ownership is what replaces Helm's release state.
for f in "$@"; do
  echo "== $f  ($(grep -c '^kind:' "$f") objects)"
  kubectl diff --server-side --field-manager=cell-pipeline -f "$f" || true
  kubectl apply --server-side --force-conflicts \
    --field-manager=cell-pipeline -f "$f"
done
EOF
chmod +x apply.sh
```

`kubectl diff` before every apply is the habit worth building: in a templating-only world, the diff *is* the change plan. See [09](09-helm.md).

### Step 4 — L6: PKI, the two-level hierarchy, and trust distribution

Install cert-manager. Note `crds.enabled=true` — in a rendered-manifest world you own CRD lifecycle explicitly.

```bash
helm repo add jetstack https://charts.jetstack.io && helm repo update
CM_VERSION=$(helm search repo jetstack/cert-manager -o json | jq -r '.[0].version')
echo "pinning cert-manager ${CM_VERSION}"

kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -

helm template cert-manager jetstack/cert-manager \
  --version "${CM_VERSION}" \
  --namespace cert-manager \
  --set crds.enabled=true \
  > rendered/20-cert-manager.yaml

./apply.sh rendered/20-cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=180s
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s
```

Now break the self-signed-root paradox, deliberately and visibly:

```yaml
# ~/cell-lab/manifests/30-pki.yaml
---
# The ONLY use of SelfSigned. Breaks the "CA needs a certificate" cycle.
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-bootstrap
spec:
  selfSigned: {}
---
# Level 1: the root. In production this is offline / a cloud private CA /
# a Vault PKI mount signed in a human ceremony. Never this.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cell-root
  namespace: cert-manager
spec:
  isCA: true
  commonName: cell-lab-root
  secretName: cell-root-ca
  duration: 87600h        # 10y
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: selfsigned-bootstrap
    kind: ClusterIssuer
    group: cert-manager.io
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cell-root-issuer
spec:
  ca:
    secretName: cell-root-ca
---
# Level 2: the intermediate. This is the one that rotates routinely,
# because rotating it needs no trust redistribution.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cell-intermediate
  namespace: cert-manager
spec:
  isCA: true
  commonName: cell-lab-intermediate
  secretName: cell-intermediate-ca
  duration: 8760h         # 1y
  renewBefore: 720h       # 30d
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: cell-root-issuer
    kind: ClusterIssuer
    group: cert-manager.io
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cell-issuer
spec:
  ca:
    secretName: cell-intermediate-ca
```

```bash
./apply.sh manifests/30-pki.yaml
kubectl -n cert-manager get certificate
kubectl get clusterissuer

# Read the chain you just built.
kubectl -n cert-manager get secret cell-intermediate-ca \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | step certificate inspect --short
```

Trust distribution — the half with no automation by default:

```bash
kubectl label namespace default cell.lab/trust=true --overwrite

helm upgrade --install trust-manager \
  oci://quay.io/jetstack/charts/trust-manager \
  --namespace cert-manager \
  --set app.trust.namespace=cert-manager \
  --wait
```

```yaml
# ~/cell-lab/manifests/35-trust.yaml
apiVersion: trust.cert-manager.io/v1alpha1
kind: Bundle
metadata:
  name: cell-trust-bundle
spec:
  sources:
    - useDefaultCAs: true          # public roots, for egress to the internet
    - secret:
        name: cell-root-ca         # our root, for everything inside the cell
        key: "ca.crt"
  target:
    configMap:
      key: "trust-bundle.pem"
    namespaceSelector:
      matchLabels:
        cell.lab/trust: "true"
```

```bash
./apply.sh manifests/35-trust.yaml
kubectl get bundle cell-trust-bundle
kubectl -n default get configmap cell-trust-bundle -o jsonpath='{.data.trust-bundle\.pem}' \
  | step certificate inspect --short --bundle | head -20
```

**The lesson to take away:** issuing the intermediate took two seconds. Getting the root into one namespace's trust store took a separate tool, a label, and a second reconcile loop. That asymmetry is the entire reason root rotation is a quarter-long project. See [11](11-cert-manager-and-pki.md).

### Step 5 — L7: the secret store

Vault in dev mode is auto-unsealed, which is the lab's stand-in for cloud-KMS auto-unseal. The dependency shape is identical; only the unseal mechanism differs.

```bash
helm repo add hashicorp https://helm.releases.hashicorp.com && helm repo update
VAULT_VERSION=$(helm search repo hashicorp/vault -o json | jq -r '.[0].version')

kubectl create namespace vault --dry-run=client -o yaml | kubectl apply -f -

helm template vault hashicorp/vault \
  --version "${VAULT_VERSION}" \
  --namespace vault \
  --set "server.dev.enabled=true" \
  --set "server.dev.devRootToken=root" \
  --set "injector.enabled=false" \
  > rendered/40-vault.yaml

./apply.sh rendered/40-vault.yaml
# vault-helm sets updateStrategyType: OnDelete, and `kubectl rollout status`
# errors out on any StatefulSet that is not RollingUpdate. Wait on the pod.
kubectl -n vault wait --for=condition=Ready pod/vault-0 --timeout=180s

# Kubernetes auth: the cell's workloads authenticate by their ServiceAccount
# token, not by a shared secret.
kubectl -n vault exec vault-0 -- sh -c '
  export VAULT_TOKEN=root
  vault auth enable kubernetes || true
  vault write auth/kubernetes/config \
    kubernetes_host="https://$KUBERNETES_PORT_443_TCP_ADDR:443"
  vault secrets enable -path=cell-lab kv-v2 || true
  vault kv put cell-lab/postgres username=temporal password=temporal-lab-pw
  vault kv get cell-lab/postgres
'
```

**Exercise (10 minutes, high value).** Prove that Vault fails closed. From guide [10](10-vault.md)'s Lab 5: enable a file audit device pointing at a path Vault cannot write to, then watch every write fail. It is the most embarrassing Vault outage and it takes ten minutes to inoculate yourself against it.

### Step 6 — L8: policy, and the webhook deadlock

```bash
helm repo add kyverno https://kyverno.github.io/kyverno/ && helm repo update
KYVERNO_VERSION=$(helm search repo kyverno/kyverno -o json | jq -r '.[0].version')

kubectl create namespace kyverno --dry-run=client -o yaml | kubectl apply -f -

helm template kyverno kyverno/kyverno \
  --version "${KYVERNO_VERSION}" \
  --namespace kyverno \
  --include-crds \
  > rendered/50-kyverno.yaml

./apply.sh rendered/50-kyverno.yaml
kubectl -n kyverno rollout status deploy/kyverno-admission-controller --timeout=300s

# Which namespaces are excluded by default? Read it, do not assume.
kubectl -n kyverno get configmap kyverno -o yaml | grep -A5 resourceFilters
```

A cell invariant as a policy. Check your Kyverno version's API first — the CEL types under `policies.kyverno.io/v1` are current, and the legacy `kyverno.io/v1 ClusterPolicy` is deprecated:

```bash
kubectl api-resources | grep -i kyverno
```

```yaml
# ~/cell-lab/manifests/55-policy.yaml
apiVersion: policies.kyverno.io/v1
kind: ValidatingPolicy
metadata:
  name: require-cell-component-label
spec:
  validationActions: [Deny]
  matchConstraints:
    # Scope this to namespaces you own. A cluster-wide match here would deny
    # every Deployment in the Temporal chart in Step 8, because a third-party
    # chart does not carry your labels — which is the real-world lesson.
    namespaceSelector:
      matchLabels:
        cell.lab/policy: "enforce"
    resourceRules:
      - apiGroups: ["apps"]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["deployments", "statefulsets"]
  validations:
    - expression: >-
        has(object.spec.template.metadata) &&
        has(object.spec.template.metadata.labels) &&
        'cell.lab/component' in object.spec.template.metadata.labels
      message: "every workload must carry cell.lab/component"
```

```bash
./apply.sh manifests/55-policy.yaml
kubectl label namespace default cell.lab/policy=enforce --overwrite

# Should be denied:
kubectl create deployment naughty --image=nginx
# Should be accepted:
kubectl create deployment nice --image=nginx --dry-run=client -o yaml \
  | yq '.spec.template.metadata.labels."cell.lab/component" = "demo"' \
  | kubectl apply -f -
kubectl delete deployment nice --ignore-not-found
```

**Now break the cell on purpose.** This is the most instructive twenty minutes in the lab.

```bash
# Make the webhook fail-closed and then remove the thing behind it.
kubectl get validatingwebhookconfiguration | grep kyverno
kubectl scale -n kyverno deploy/kyverno-admission-controller --replicas=0

# Try to do anything. Watch it hang and then fail.
kubectl create deployment canary --image=nginx
#   Error: failed calling webhook ... connection refused

# Break glass. This is the runbook step. Practise it now, not at 3am.
kubectl delete validatingwebhookconfiguration \
  $(kubectl get validatingwebhookconfiguration -o name | grep kyverno | head -1 | cut -d/ -f2)

kubectl scale -n kyverno deploy/kyverno-admission-controller --replicas=1
# Kyverno re-registers its webhooks on startup. Confirm:
kubectl get validatingwebhookconfiguration | grep kyverno
```

Ask yourself the question that matters: **if this cell were bootstrapping right now, could it?** A fail-closed webhook that matches `Pods` cluster-wide, installed before cert-manager, is a cell that never comes up. That is why the ordering in the DAG puts the policy *controller* early and the *policies* late.

### Step 7 — L9: the data store

```yaml
# ~/cell-lab/manifests/60-postgres.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: data
  labels:
    cell.lab/trust: "true"
    cell.lab/policy: "enforce"   # opt this namespace into the Step 6 policy
---
apiVersion: v1
kind: Secret
metadata:
  name: postgres-creds
  namespace: data
stringData:
  POSTGRES_USER: temporal
  POSTGRES_PASSWORD: temporal-lab-pw
  POSTGRES_DB: postgres
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: data
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels: { app: postgres }
  template:
    metadata:
      labels:
        app: postgres
        cell.lab/component: persistence
    spec:
      containers:
        - name: postgres
          image: postgres:16
          envFrom:
            - secretRef: { name: postgres-creds }
          ports: [{ containerPort: 5432 }]
          readinessProbe:
            exec: { command: ["pg_isready", "-U", "temporal"] }
            initialDelaySeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: data
spec:
  selector: { app: postgres }
  ports: [{ port: 5432, targetPort: 5432 }]
```

```bash
./apply.sh manifests/60-postgres.yaml
kubectl -n data rollout status statefulset/postgres --timeout=180s
```

Note that the StatefulSet carries `cell.lab/component` — your own policy from step 6 is now enforcing a cell invariant on the cell's own data layer. That is the point of policy-as-bootstrap.

### Step 8 — L10: the Temporal services

Read the chart's values before you use it. Do this for every third-party chart, always:

```bash
helm show values temporal --repo https://go.temporal.io/helm-charts \
  > values/temporal-reference.yaml
wc -l values/temporal-reference.yaml
grep -n 'useHelmHooks\|numHistoryShards\|manageSchema' values/temporal-reference.yaml
```

The chart's schema Job uses Helm hooks by default. In a rendered-manifest pipeline with no release state, hooks do not run — this is exactly the "third-party charts assume release state" problem from guide [09](09-helm.md), and the chart gives you the escape hatch:

```yaml
# ~/cell-lab/values/temporal-cell.yaml
useHelmHooks: false          # we own ordering, not Helm

server:
  replicaCount: 1
  config:
    numHistoryShards: 4      # tiny. In production this is fixed forever.
    persistence:
      defaultStore: default
      visibilityStore: visibility
      datastores:
        default:
          sql:
            createDatabase: true
            manageSchema: true
            pluginName: postgres12
            driverName: postgres12
            databaseName: temporal
            connectAddr: "postgres.data.svc.cluster.local:5432"
            connectProtocol: tcp
            user: temporal
            password: temporal-lab-pw
        visibility:
          sql:
            createDatabase: true
            manageSchema: true
            pluginName: postgres12
            driverName: postgres12
            databaseName: temporal_visibility
            connectAddr: "postgres.data.svc.cluster.local:5432"
            connectProtocol: tcp
            user: temporal
            password: temporal-lab-pw
```

```bash
kubectl create namespace temporal --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace temporal cell.lab/trust=true --overwrite
# Deliberately NOT labelled cell.lab/policy=enforce: the upstream chart does not
# stamp cell.lab/component, so enforcing here would deny the whole install. In a
# real cell you would either patch the labels in the render step or scope the
# policy the way we just did. Decide which, on purpose.

helm template temporal temporal \
  --repo https://go.temporal.io/helm-charts \
  --namespace temporal \
  -f values/temporal-cell.yaml \
  > rendered/70-temporal.yaml

# Look at what you are about to apply. This is the review surface.
grep -c '^kind:' rendered/70-temporal.yaml
grep -n 'helm.sh/hook' rendered/70-temporal.yaml || echo "no hooks left — good"
grep -n 'kind: Job' rendered/70-temporal.yaml

./apply.sh rendered/70-temporal.yaml
```

Watch the ordering happen, and notice that it happens in the right order **because the objects retry**, not because you sequenced them:

```bash
kubectl -n temporal get pods -w
# schema jobs run, then history/matching/frontend/worker come up.
# Any pod that starts before the schema is ready crash-loops and recovers.
# That is level-triggered convergence doing its job.

kubectl -n temporal get jobs
kubectl -n temporal logs job/temporal-schema-setup --tail=20 2>/dev/null || true
kubectl -n temporal rollout status deploy/temporal-frontend --timeout=300s
```

If the schema job did not run, that is the hook problem — apply it explicitly first, wait, then apply the rest. Either way, *you have now personally hit the L9 → L10 edge from the DAG table.*

### Step 9 — L11–L12: the health gate

```bash
kubectl -n temporal get svc
kubectl -n temporal port-forward svc/temporal-frontend-headless 7233:7233 &
sleep 3

export TEMPORAL_ADDRESS=127.0.0.1:7233

# L12 gate step 1: the service reports itself healthy
temporal operator cluster health
temporal operator cluster system

# L12 gate step 2: control-plane operations work
temporal operator namespace create --namespace cell-smoke --retention 24h
temporal operator namespace describe --namespace cell-smoke

# L12 gate step 3: the full write path — frontend -> history -> persistence
temporal workflow start --type SmokeWorkflow --task-queue smoke-tq \
  --workflow-id "smoke-$(date +%s)" --namespace cell-smoke

# L12 gate step 4: the visibility path
temporal workflow list --namespace cell-smoke
```

Four assertions, four different subsystems. **This is the difference between "pods are Running" and "the cell is ready."** A real gate adds two more that the lab cannot show: the request goes through the real external endpoint (not a port-forward), and it presents a real client certificate against the real mTLS terminator. Those two are where SNI mismatches, missing SANs, and L4 load-balancer pinning are caught.

Write the gate down as a script, because that is the actual artifact:

```bash
cat > gate.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
fail() { echo "GATE FAIL: $*" >&2; exit 1; }
ready() { kubectl -n "$1" get "$2" "$3" -o json | jq -e '.status.readyReplicas >= 1' >/dev/null; }

kubectl get nodes -o json | jq -e '[.items[].status.conditions[]
  | select(.type=="Ready" and .status=="True")] | length >= 3' >/dev/null \
  || fail "L4: not all nodes Ready"
ready kube-system deploy coredns             || fail "L4: CoreDNS not ready"
kubectl -n cert-manager get certificate cell-intermediate -o json \
  | jq -e '[.status.conditions[] | select(.type=="Ready" and .status=="True")] | length == 1' \
  >/dev/null                                  || fail "L6: intermediate CA not Ready"
kubectl -n temporal get configmap cell-trust-bundle >/dev/null 2>&1 \
  || fail "L6: trust bundle missing in the temporal namespace"
kubectl get validatingwebhookconfiguration -o name | grep -q kyverno \
  || fail "L8: kyverno webhooks not registered"
ready temporal deploy temporal-frontend      || fail "L10: frontend not ready"
temporal operator cluster health >/dev/null  || fail "L12: cluster health check failed"
echo "GATE PASS"
EOF
chmod +x gate.sh && ./gate.sh
```

### Step 10 — Break things on purpose

Each of these is a scale model of a real incident. Do all four.

```bash
# 1. Kill the datapath. Watch what still works and what does not.
kubectl -n kube-system delete pod -l k8s-app=cilium
kubectl run probe --rm -it --image=nicolaka/netshoot --restart=Never -- \
  sh -c 'nslookup temporal-frontend-headless.temporal.svc.cluster.local'
# New pods cannot get an IP. Existing pods keep their existing connections.
# That asymmetry is why "the cell looks fine" during a CNI outage.

# 2. Rotate the root the WRONG way. Time the outage.
kubectl -n cert-manager delete secret cell-root-ca
kubectl -n cert-manager delete certificate cell-root
./apply.sh manifests/30-pki.yaml
# New leaves chain to a new root. The trust bundle has not caught up yet.
# In production, that window is your outage. Measure it.

# 3. Wedge a namespace with a finalizer.
kubectl create namespace stuck
kubectl create -n stuck configmap victim --from-literal=a=b
kubectl patch configmap victim -n stuck --type=merge \
  -p '{"metadata":{"finalizers":["cell.lab/never-removed"]}}'
kubectl delete namespace stuck --timeout=30s   # hangs
kubectl get namespace stuck -o json | jq '.status.conditions'
# Fix it the way you would in production: remove the finalizer from the
# OBJECT, not from the namespace.
kubectl patch configmap victim -n stuck --type=merge \
  -p '{"metadata":{"finalizers":null}}'
kubectl get namespace stuck 2>/dev/null || echo "namespace gone"

# 4. Simulate a node upgrade with a bad PDB.
kubectl -n temporal create poddisruptionbudget frontend-pdb \
  --selector=app.kubernetes.io/component=frontend --max-unavailable=0
kubectl drain cell-lab-worker --ignore-daemonsets --delete-emptydir-data --timeout=60s
# Cannot evict. This is exactly how a Karpenter drift rollout wedges a cell.
kubectl -n temporal delete pdb frontend-pdb
kubectl uncordon cell-lab-worker
```

### Step 11 — Teardown, in reverse, with verification

```bash
# Reverse DAG order. Note step 1: stop the reconcilers first.
kubectl delete -f manifests/55-policy.yaml --ignore-not-found       # policies before namespaces
kubectl delete -f rendered/70-temporal.yaml --ignore-not-found
kubectl delete -f manifests/60-postgres.yaml --ignore-not-found
kubectl delete -f rendered/50-kyverno.yaml --ignore-not-found
kubectl delete -f rendered/40-vault.yaml --ignore-not-found
kubectl delete -f manifests/35-trust.yaml --ignore-not-found
helm uninstall trust-manager -n cert-manager --ignore-not-found   # installed by helm, not rendered
kubectl delete -f manifests/30-pki.yaml --ignore-not-found
kubectl delete -f rendered/20-cert-manager.yaml --ignore-not-found

# Verify: the residue enumerator. This is the sweeper in miniature.
echo "--- namespaces stuck terminating"
kubectl get ns -o json | jq -r '.items[] | select(.status.phase=="Terminating") | .metadata.name'
echo "--- orphaned cluster-scoped webhooks (the bricked-cluster class)"
kubectl get validatingwebhookconfiguration,mutatingwebhookconfiguration -o name
echo "--- orphaned CRDs"
kubectl get crd -o name | grep -E 'cert-manager|kyverno|trust' || echo "none"
echo "--- unbound PVs"
kubectl get pv -o json | jq -r '.items[] | select(.status.phase!="Bound") | .metadata.name'

kind delete cluster --name cell-lab
```

**The exercise that makes this stick:** run the residue enumerator *before* `kind delete cluster` and write down everything it finds. In a real cell, every one of those lines is a cloud resource that costs money forever. Your teardown workflow's job is to make that list empty, and to prove it independently by querying the cloud API by tag rather than trusting Kubernetes to have cleaned up.

---

## Production gotchas

These are the composition-level failures — the ones that do not belong to any single tool, and so appear in no single tool's documentation.

**1. "Ready" is defined at the wrong layer.** A cell whose readiness gate checks pod status will be marked ready before it can serve. The gate must be an end-to-end request through the real ingress with real credentials. Everything else is a proxy metric that will eventually lie to you.

**2. Sleeps instead of gates.** Every `sleep 60` in a provisioning pipeline is a bug with a timer on it. It is too short on a slow day and wastes minutes on every other day. Poll a condition. Guide [06](06-karpenter.md) makes the specific case: poll `NodePool.status.conditions[Ready]`, not the clock.

**3. Coupled numbers in uncoupled files.** gRPC server `keepAliveEnforcementPolicy.minTime` versus client keepalive interval; keepalive interval versus LB idle timeout; proxy route timeout versus long-poll expiration; `MaxConnectionAge` versus `terminationGracePeriodSeconds`; pod CIDR size versus max-pods-per-node versus expected pod count. Each pair lives in different YAML that nothing forces to agree. Write CI assertions. This is a cheap, high-visibility first-month contribution.

**4. The shared dependency nobody drew on the diagram.** A shared container registry, a shared Vault, a shared observability backend, a shared DNS zone, a shared CI runner pool. Each is a correlated failure domain spanning every cell. The question to answer for each: *is it on the request path, or only on the control path?* Control-path-only is usually acceptable — the cell keeps serving, you just cannot change it. Request-path shared dependencies quietly convert your cellular architecture back into a monolith.

**5. Teardown is tested less than creation, so it fails more.** Every team creates cells constantly and deletes them rarely. Fix this structurally: create and destroy a scratch cell on a schedule, in CI, on each cloud, and fail the build if the residue enumerator finds anything. The specific residue that bites hardest is leaked ENIs and IPs on AWS — interfaces left behind when nodes go away abruptly consume subnet addresses, and the *next* cell into that VPC fails IPAM for reasons that have nothing to do with it. Sweep by tag; alarm on subnet free-IP percentage.

**6. Provider auto-upgrade fighting your pipeline.** GKE release channels and AKS auto-upgrade will move your cells without asking. You want to control timing, not the provider: on GKE, the Extended channel or aggressive maintenance exclusions; on AKS, channel `none` plus your own driver; on EKS, staying comfortably inside standard support. See [04](04-managed-kubernetes-eks-gke-aks.md).

**7. A canary that cannot catch the bug class you fear.** Memory leaks and race conditions are the failure modes Temporal names publicly for its ring rollouts. Neither is visible in a one-hour synthetic canary. Soak time is a feature, not a delay.

**8. Rollout logic built out of Terraform.** Terraform "is a *convergence* tool with no notion of rollout" — no canary, no wave, no health gate that reverts. Trying to build waves out of `count` and `-target` produces something unreadable and unresumable. The rollout belongs in an orchestrator.

**9. Per-cell state you cannot destroy.** If Kubernetes or Helm resources live in the cell's Terraform state, `terraform destroy` needs a *reachable* cluster. If the control plane is already gone the plan itself fails, and you are doing state surgery during a teardown. Stop Terraform at the cluster boundary.

**10. Trust and certificate lifetimes running backwards.** The one-way rule: distribute trust before you use it, remove it after you stop. Violating the second half is subtler than violating the first and produces the same outage a week later, when the last straggler reconnects. The mirror image: a torn-down cell whose leaves are still valid is a set of live credentials for infrastructure that no longer exists — short lifetimes make that self-correcting within a day.

**11. A routing layer that cannot say "no."** If placement cannot report "no cell in this region has room," it will instead pick one and fail during provisioning, or pick one and overload it. Capacity exhaustion must be an explicit, monitored, user-visible state. Related, and more dangerous: the drain control living inside the thing being drained — Slack's fourth design goal, and the one most likely to be violated by accident. Test it. Can you drain a cell whose Kubernetes API server is unreachable?

**12. Quota checked at the wrong time.** Surge capacity during an upgrade temporarily doubles the vCPU footprint. Check headroom before the wave starts, on three differently-shaped quota models — not halfway through, when you have already drained half the nodes.

---

## Cell readiness checklist

A cell may serve production traffic when all of the following are true. Each line should be an automated assertion, not a human judgment.

**L0–L2 — tenancy, identity, network**

- [ ] Account / project / subscription exists, tagged with the cell ID, enrolled in the org policy set.
- [ ] Quota headroom exceeds the cell's maximum size plus upgrade surge, in every relevant dimension.
- [ ] Workload identity federation configured, and a pod mints a cloud token end to end.
- [ ] Pod CIDR, service CIDR, and node subnets allocated from the fleet IPAM registry, non-overlapping with every other cell, recorded durably.
- [ ] Egress leaves via the intended NAT, verified from inside a pod; MTU correct end to end.
- [ ] Private endpoints to KMS, registry, and object storage resolve and are reachable; DNS delegation resolves privately.

**L3–L5 — cluster, datapath, capacity**

- [ ] All nodes `Ready`; kubelet skew within policy; node OS image within N days of latest.
- [ ] kube-proxy mode explicitly set (not defaulted) and supported by the node kernel; CoreDNS healthy for in-cluster and external names.
- [ ] CCM provisions a `LoadBalancer` Service; CSI provisions and mounts a volume with `WaitForFirstConsumer`.
- [ ] NetworkPolicy actually *enforced*, not merely accepted by the API server; every APIService healthy.
- [ ] Karpenter NodeClass `status` resolved, NodePool `Ready`, budgets set per reason, `terminationGracePeriod` set, image pinned (not a floating alias).
- [ ] No `maxUnavailable: 0` PDB outside a reviewed allowlist.

**L6–L8 — PKI, secrets, policy, observability**

- [ ] Cell issuer chains to the region intermediate, verified with `openssl verify`; a throwaway certificate issues.
- [ ] Trust bundle present in every namespace that needs it, *before* any workload started.
- [ ] A wire probe confirms the certificate served on each port matches the Secret; every certificate in the fleet inventory with issuer, notAfter, SANs.
- [ ] Vault unsealed with the seal mechanism verified (not just "the pod is Running"); two audit devices enabled before any workload authenticated; root token revoked; recovery keys escrowed.
- [ ] Database credentials dynamic; `rotate-root` has run.
- [ ] Policy engine healthy, webhooks registered and pointing at a live Service; baseline objects generated in every namespace.
- [ ] Metrics, logs, and traces arriving at the fleet backend with the correct cell label; cell visible on the fleet dashboard.

**L9–L10 — data and application**

- [ ] Persistence and visibility stores reachable, at the correct schema version.
- [ ] History shard count set correctly and recorded (it is immutable).
- [ ] Backup and restore verified — a restore drill, not just a backup job.
- [ ] All four Temporal services ready; Ringpop membership converged to the expected counts.

**L11–L12 — traffic and gate**

- [ ] Load balancer targets healthy through the real health check, not a TCP dial; mTLS terminates with matching SANs and expected SNI behavior; private connectivity published where applicable.
- [ ] **End-to-end synthetic:** an external client with a real certificate creates a namespace, starts a workflow, and lists it, through the public endpoint.
- [ ] Conformance suite passes; teardown dry-run executes cleanly against this cell.
- [ ] Cell registered in the router as eligible for placement — **and this is the last step, not an earlier one.**

---

## The first 90 days

A schedule that synthesizes the eleven guides' learning paths. Weekday hours are assumed scarce; the heavy labs are weekend work.

### Days 1–30 — Build the map, get your hands dirty, ship something small

**Week 1 — orientation and the two labs that change how you see everything.**

- Read this guide end to end. Then read the [Temporal cell architecture blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) and the [multi-cloud blog](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability), and map every noun in them onto the DAG above.
- Do **the Hands-on lab in this guide**, all eleven steps, in one sitting. Nothing else this month teaches as much per hour.
- [05 CNI](05-cni-and-host-networking.md): **Lab 1** (build a two-namespace network by hand) and **Lab 5** (break MTU and watch the black hole). These two are 80% of the datapath intuition.
- [04 Managed Kubernetes](04-managed-kubernetes-eks-gke-aks.md): **Lab 2** (PDB, drain, eviction). One hour, highest value in that guide.
- [01 Go](01-golang.md): Tour of Go where unfamiliar, Effective Go, and the `context` package docs. Do Hands-on steps 1–4.
- Read your team's actual cell provisioning workflow or pipeline end to end, and draw it as a DAG. Compare against the DAG in this guide and list every difference. This list is your week-two conversation.

**Week 2 — the delivery mechanics and the first PR.**

- [08 Terraform](08-terraform.md): **Labs 1 and 3** (the `count` index-shift disaster; `moved`/`import`/`removed`). Read the state docs end to end. Find out where your team splits state and where the Terraform-to-GitOps boundary sits.
- [09 Helm](09-helm.md): **Labs 1 and 2** (the `nindent` bug; what dies without a cluster). Then `helm template` your team's real cell chart with the exact pipeline flags and read all of the output. Grep it for `lookup`, `IsUpgrade`, and `helm.sh/hook`.
- [02 gRPC](02-grpc.md): **Labs 1, 2, 4**. Then read [`temporalio/api`'s `service.proto`](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto) and find the poll RPCs.
- Ship one small, reviewed change to a production cell module. The point is to see the whole pipeline — plan in PR, policy gate, approval, apply — not the change.

**Week 3 — trust and secrets.**

- [11 cert-manager and PKI](11-cert-manager-and-pki.md): **Labs 1–4**. Lab 4 (a full root rotation) is the one that matters; do the wrong-way version too and time the outage.
- [10 Vault](10-vault.md): **Labs 0, 2, and 5**. Lab 5 (wedge the audit device) is ten minutes and inoculates you against the most embarrassing Vault outage.
- Find out where your fleet's region intermediates live, how they were created, and what the rotation runbook says. If there is no runbook, you have found your 60-day project.

**Week 4 — the cluster-level controllers, and your first fleet-wide finding.**

- [06 Karpenter](06-karpenter.md): **Lab 1**, exercises 1–6. Exercise 5 (the `do-not-disrupt` deadlock) is the highest-value hour.
- [07 Kyverno](07-kyverno.md): **Labs 0, 1, and 4**. Lab 4 (break the cluster with a fail-closed policy, then break-glass) is mandatory.
- Do one audit that produces a real finding: every cell's kube-proxy mode explicitly set; or every NodePool's `terminationGracePeriod` present; or every PDB checked for `maxUnavailable: 0`. Write it up with numbers.

**By day 30 you should be able to:** draw the cell DAG from memory; name all six bootstrap paradoxes and their resolutions; explain where your team's Terraform stops and GitOps starts; and have shipped one small change and one fleet-wide finding.

### Days 31–60 — Go deep where the cells actually live, own something

- [03 Multi-cloud](03-multicloud-aws-gcp-azure.md): **Labs 1–6**. Lab 4 (cross-cloud federation with no stored key) is the one that changes how you think. Then write the twelve capability tables from memory and check them.
- [04](04-managed-kubernetes-eks-gke-aks.md): **Labs 1, 3, 4** plus the costed cloud labs A, C, E. Watch a real upgrade on each cloud and time it.
- [05](05-cni-and-host-networking.md): **Labs 2, 3, 4**. Then compute, for each real cell shape on each cloud, the max-pods number and IP consumption at full scale. Write it down; this exercise finds latent exhaustion reliably.
- [08](08-terraform.md): **Labs 4, 5, 6**. Write one `terraform test` for a module that has none.
- [09](09-helm.md): **Labs 3–6**. Add a `values.schema.json` with `additionalProperties: false` to one chart that lacks one.
- [10](10-vault.md): **Labs 1, 3, 4**. Write the Terraform module that configures one cell's Vault, apply it twice with different cell IDs, confirm nothing secret is in state.
- [11](11-cert-manager-and-pki.md): **Labs 5 and 6**. Set up Vault PKI behind cert-manager — that is the production shape.
- [07](07-kyverno.md): **Labs 2, 3, 5, 6**. Ship the generating-policy starter set to a real cell with tests in CI.
- **Trace one real cell teardown end to end**, with cloud audit logs open, and enumerate every resource that survived cluster deletion. Turn that list into the sweeper's test fixture. This is the single highest-value thing you can do in this window.
- **Own one cross-cutting improvement and land it.** Candidates, all real: the CI assertions for coupled timeout values across templates; the nightly per-cell `terraform plan -detailed-exitcode` drift job; the skew/EOL fleet reporter; the certificate inventory; the teardown residue enumerator.

**By day 60 you should be able to:** debug a stuck cell provision at any layer without asking; state which per-cloud branches are real and which are accidental; and point at one shipped artifact that made the fleet safer.

### Days 61–90 — Earn and write down opinions

- **Write the upgrade plan for the nearest version cliff on each cloud**, including the wave schedule, the soak criteria, the metric comparison against unupgraded siblings, and the rollback story for each step (naming which steps are one-way).
- **Write the root rotation runbook for the real fleet** and execute it in a scratch cell. Measure trust propagation time empirically rather than guessing it.
- **Build or improve the conformance suite** — one test per line of the readiness checklist above, running continuously against every cell, with "how many cells are non-conformant and why" as one number on a dashboard.
- **Form and write down the fleet dataplane opinion.** One CNI everywhere versus the cloud-supported one per cloud, with the tradeoffs from guide [05](05-cni-and-host-networking.md) spelled out. This is the highest-leverage architectural question your team will answer, and being the person who can argue it from first principles is the senior-level deliverable.
- **Read Temporal server code for real.** Trace one request path end to end through the Frontend service; understand the `fx` wiring. See [01](01-golang.md)'s Month 1 path.
- **Read the Karpenter cloud provider interface** (`pkg/cloudprovider/types.go`) and the disruption controller source. The nine-method interface makes the multi-cloud story legible.
- **Do one seal migration in the lab** (Shamir to auto-unseal, and auto-unseal to a different KMS key). You will need this eventually and you do not want the first attempt to be an incident.
- **Measure the six numbers** from the leverage section — provision time p50/p99 per cloud, teardown time, manual interventions per 10 operations, non-conformant cell count, orphaned-resource cost, MTTD for a stuck provision — and bring them to your manager with a proposal for which one to move.

---

## Questions to ask the team

These are architecture questions. None of them is answerable by reading a README, and each one's answer will teach you more than a week of code reading.

**Cell definition and boundaries**

1. What exactly is inside a cell versus shared across cells, and which of the shared things are on the *request* path rather than only the *control* path?
2. What is the maximum size of a cell — in tenants, in actions per second, in storage — was that number measured or assumed, and have we ever load-tested a cell to it on all three clouds?
3. What is the cell's identity — a name, a generation counter, a UUID? If we rebuild a cell in place, is the name reusable?
4. Where does the fleet inventory live, and is it the source of truth or a cache of one?

**Placement and routing**

5. How is a namespace assigned to a cell? What happens when no cell in the requested region has capacity — is that a user-visible outcome or a provisioning timeout?
6. Can we move a namespace between cells today? What does that cost, and how often do we do it?
7. What does the routing layer actually consist of, and what is its blast radius? Where is its state, and what happens if that state is stale?
8. Can we drain a cell? How fast, at what granularity, and — the important one — does the drain control depend on anything inside the cell being drained?

**Bootstrap and ordering**

9. Where is the canonical, authoritative description of the cell bootstrap order? Is it one artifact, or is it spread across a workflow, some Terraform, and sync-wave annotations?
10. Which of the bootstrap paradoxes have we hit, and how did we resolve each one? Specifically: what runs Karpenter, where does the root CA live, does Kyverno's webhook cert come from cert-manager, and what unseals Vault?
11. When a cell provision fails at 3 a.m., what tells us which layer failed? Is there a diagnostic, or does someone read logs from the top?
12. Where exactly does Terraform stop and GitOps start, and why there?
13. Can we bring up a cell if the shared observability backend is down? If the shared registry is down? If the central Vault is down?

**Multi-cloud**

14. Which per-cloud differences in the cell definition are genuinely irreducible, and which are accidental divergence we have not gotten around to removing?
15. What does the provider interface look like today, and what happened the last time someone added a method to it?
16. Where is Azure relative to AWS and GCP right now, and what is the feature-parity model — capability flags in cell metadata, or do we discover gaps at provision time?
17. Which cloud do we test least, and what do we do about that?

**Upgrade**

18. What does a Kubernetes minor version upgrade across the whole fleet actually look like, start to finish? How long does it take and how many humans does it involve?
19. What are the ring and wave definitions, what is the soak time per wave, and what metric do we compare against unupgraded cells to decide a wave passed?
20. Which steps in a cell upgrade are irreversible, does everyone know which ones, and what happens when a wave is paused halfway — resume, or restart?
21. How do we suppress the cloud providers' own auto-upgrade so our pipeline stays in control?
22. What is the drain story for History specifically, given shard ownership, versus Frontend, given long polls?

**Teardown**

23. When did we last delete a cell, and how did we verify it was actually gone — by Kubernetes, by cloud API, or by the bill?
24. Do we create-and-destroy a scratch cell on a schedule in CI? If not, what keeps the delete path working?
25. What is on the orphan list per cloud, who owns the sweeper, and what is our monthly spend on resources belonging to cells that no longer exist?

**Failure, blast radius, and the team**

26. What was the last incident contained to one cell, and the last one that was not? What made the difference?
27. Is there a per-cell circuit breaker? What signal trips it, is that signal produced outside the cell, and what stops it from tripping enough cells to overload the survivors?
28. How do we detect gray failure — a cell that is up, healthy by its own report, and serving errors to half its clients?
29. What is the largest correlated failure domain we have, and is it drawn on any diagram?
30. What is the thing about cell lifecycle that everybody knows and nobody has written down? Which part of the pipeline do people dread touching? If I could fix one thing in 90 days, what would you pick?

---

## References

**Temporal — primary sources**

1. [Building durable cloud control systems with Temporal](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) — Sergey Bykov, Jan 2025. The cell definition (own AWS account, VPC, EKS cluster), what a cell contains, namespace provisioning steps, deployment rings, entity workflows.
2. [Making Temporal Cloud a Multi-Cloud Platform](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability) — Raphaël Beamonte, Oct 2024. User CP vs Infra CP, the `TemporalClusterProvider` interface and factory, the OpenSearch/GCP visibility gap, the Azure plan.
3. [Durable Execution in the Control Plane](https://www.infoq.com/presentations/durable-execution-control-plane/) — QCon talk referenced from (1). *Secondary recording of a primary talk.*
4. [Temporal Server](https://docs.temporal.io/temporal-service/temporal-server) — the four services, ports, History Shards, Ringpop membership, retention.
5. [Persistence](https://docs.temporal.io/temporal-service/persistence) — supported databases and versions, visibility store.
6. [Visibility](https://docs.temporal.io/temporal-service/visibility) — standard vs advanced visibility.
7. [Temporal Cloud — High Availability](https://docs.temporal.io/cloud/high-availability) — 3-AZ replication, multi-region and multi-cloud replication, request forwarding, and the explicit "Temporal operates a 'cell architecture'" statement under Same-region Replication.
8. [Temporal Cloud — Service regions](https://docs.temporal.io/cloud/regions) — per-region endpoints, PrivateLink endpoint services, Private Service Connect attachments, replication pairs.
9. [Temporal Cloud — Service availability](https://docs.temporal.io/cloud/service-availability) — throughput model, p99 latency SLO.
10. [Temporal Cloud — Namespaces](https://docs.temporal.io/cloud/namespaces) — namespace as unit of isolation, Namespace vs regional endpoints, SNI behavior, deletion protection.
11. [Temporal Cloud — Certificates](https://docs.temporal.io/cloud/certificates) — mTLS CA requirements and certificate filters.
12. [Upgrade the Temporal Server](https://docs.temporal.io/self-hosted-guide/upgrade-server) — sequential minor upgrades, schema-first ordering, the ~10-minute shard reload per version.
13. [temporalio/helm-charts](https://github.com/temporalio/helm-charts) — the V3 chart, `useHelmHooks`, `numHistoryShards`, and the schema/binary compatibility rule.
14. [temporalio/docker-compose](https://github.com/temporalio/docker-compose) — the `auto-setup` image and its environment contract.
15. [temporalio/temporal](https://github.com/temporalio/temporal) — the server source.
16. [temporalio/api — `service.proto`](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto) — the gRPC contract.
17. [temporalio/ringpop-go](https://github.com/temporalio/ringpop-go) — the membership protocol.
18. [Temporal CLI](https://docs.temporal.io/cli) — `temporal server start-dev`, `operator cluster health`.
19. [Temporal Cloud SLA](https://docs.temporal.io/cloud/sla) and [RPO/RTO](https://docs.temporal.io/cloud/rpo-rto).
20. [status.temporal.io](https://status.temporal.io) — public per-region status.

**Cell-based architecture — primary literature**

21. [Reducing the Scope of Impact with Cell-Based Architecture](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html) — AWS Well-Architected, Sept 2023. The whitepaper.
22. [What is a cell-based architecture?](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/what-is-a-cell-based-architecture.html) — bulkheads, partition key, cell router, control plane.
23. [Cell design](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-design.html) — cell independence, no cross-cell dependencies, separate accounts.
24. [Cell sizing](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-sizing.html) — the three opposing forces and the smaller-vs-larger trade table.
25. [AWS Fault Isolation Boundaries](https://docs.aws.amazon.com/whitepapers/latest/aws-fault-isolation-boundaries/abstract-and-introduction.html) — AZs, Regions, control planes, data planes as isolation boundaries.
26. [REL01-BP01 Aware of service quotas and constraints](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_manage_service_limits_aware_quotas_and_constraints.html) — the quota discipline cells depend on.
27. [Use bulkhead architectures to limit scope of impact](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_fault_isolation_use_bulkhead.html) — the Well-Architected best practice the whitepaper expands.
28. [Workload isolation using shuffle sharding](https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/) — the complementary technique when full cells are too coarse.
29. [Guidance for Cell-Based Architecture on AWS](https://aws.amazon.com/solutions/guidance/cell-based-architecture-on-aws/) — reference implementation.
30. [AWS re:Invent — cell-based architecture](https://www.youtube.com/watch?v=6IknqRZMFic) — the talk cited by the whitepaper. *Secondary recording.*

**Industry cell writeups**

31. [Slack's Migration to a Cellular Architecture](https://slack.engineering/slacks-migration-to-a-cellular-architecture/) — Cooper Bethea, Aug 2023. AZ-as-cell, siloing, the four drain design goals, Envoy/xDS weighted clusters and RTDS.
32. [A Pods Architecture To Allow Shopify To Scale](https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale) — Sorting Hat routing, Pod Mover, per-pod disaster recovery, evacuating a data center pod by pod.
33. [Gray Failure: The Achilles' Heel of Cloud-Scale Systems](https://www.microsoft.com/en-us/research/wp-content/uploads/2017/06/paper-1.pdf) — Microsoft Research. The definition Slack works from.
34. [Generic mitigations](https://www.oreilly.com/content/generic-mitigations/) — why a drain button beats a diagnosis. *Secondary.*
35. [Scaling Datastores at Slack with Vitess](https://slack.engineering/scaling-datastores-at-slack-with-vitess/) — the strongly-consistent-shard constraint that shaped Slack's cells. *Secondary context.*

**Kubernetes and tooling — primary sources**

36. [Kubernetes version skew policy](https://kubernetes.io/releases/version-skew-policy/) — why control plane upgrades precede nodes.
37. [Specifying a Disruption Budget](https://kubernetes.io/docs/tasks/run-application/configure-pdb/) and [API-initiated eviction](https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/).
38. [Finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/) — the ordered-teardown mechanism and its traps.
39. [Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) — field ownership, which replaces Helm release ownership.
40. [Cluster Networking](https://kubernetes.io/docs/concepts/cluster-administration/networking/) — the four-point network model contract.
41. [kind — Quick Start](https://kind.sigs.k8s.io/docs/user/quick-start/) — the lab substrate, including `disableDefaultCNI` and `kubeProxyMode`.
42. [Cilium — Installation on kind](https://docs.cilium.io/en/stable/installation/kind/) — kube-proxy replacement and `k8sServiceHost`.
43. [cert-manager Concepts](https://cert-manager.io/docs/concepts/) and [trust-manager](https://cert-manager.io/docs/trust/trust-manager/) — issuance and trust distribution as separate disciplines.
44. [Kyverno — Policy Types](https://kyverno.io/docs/policy-types/) and [Configuring Kyverno](https://kyverno.io/docs/installation/customization/) — the CEL policy family and the default resource filters.
45. [Vault — Seal/Unseal](https://developer.hashicorp.com/vault/docs/concepts/seal) — why a cell that cannot unseal is a cell that cannot boot.
46. [Karpenter — Disruption](https://karpenter.sh/docs/concepts/disruption/) and [Compatibility](https://karpenter.sh/docs/upgrading/compatibility/) — graceful vs forceful disruption; the version matrix.
47. [Terraform — provider block](https://developer.hashicorp.com/terraform/language/block/provider) — "you can only reference values that Terraform knows before it applies your configuration."
48. [Helm — `helm template`](https://helm.sh/docs/helm/helm_template/) — the pure function at the centre of a rendered-manifest pipeline.
49. [The Rendered Manifests Pattern](https://akuity.io/blog/the-rendered-manifests-pattern) — "Git contains the inputs to the desired state, not the desired state itself." *Vendor blog, but the canonical statement of the pattern.*
50. [Argo CD — Sync waves and phases](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/) — how ordering is expressed once Helm hooks are gone.
