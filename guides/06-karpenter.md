# Karpenter — Node Autoscaling and Node Lifecycle

**Why this matters.** Cell lifecycle work spans AWS, GCP, and Azure — provisioning, upgrading, and tearing down isolated Kubernetes units of Temporal Cloud capacity. Karpenter is the component that decides *what machines a cell is made of*, and — more dangerously — the component that decides **when to delete a machine that is currently serving traffic**. On a fleet of hundreds of cells running long-lived history hosts, matching engines, and frontends, the interesting Karpenter surface is not "scale up when pods pend." It is disruption: consolidation, drift, expiry, spot interruption, and the budgets and annotations that keep all of that from taking a cell down. Karpenter is also where the multi-cloud story gets uncomfortable, because the three clouds are at three completely different maturity levels.

Everything below was verified against primary sources on **2026-08-29**. Where I could not verify something, I say so.

> **Stale-content trap, read this first.** A large majority of Karpenter content on the internet — blog posts, Stack Overflow answers, internal wikis, and LLM training data — describes the **v1alpha5** API (`Provisioner`, `AWSNodeTemplate`, `Machine`) or the **v1beta1** API. Both are gone. The v1alpha5 kinds were renamed in v1beta1 (`Provisioner` → `NodePool`, `Machine` → `NodeClaim`, `AWSNodeTemplate` → `EC2NodeClass`) ([v1beta1 API design](https://github.com/aws/karpenter-provider-aws/blob/main/designs/v1beta1-api.md)), and **Karpenter 1.1.0 dropped support for the v1beta1 APIs entirely** ([v1 Migration guide](https://karpenter.sh/v1.0/upgrading/v1-migration/)). If a snippet you find says `apiVersion: karpenter.sh/v1alpha5` or `v1beta1`, or puts `kubelet:` under the NodePool, it is describing software that is several years and fourteen minor versions out of date. The current stable line is **v1.14.1**, released 2026-08-21 ([core release](https://github.com/kubernetes-sigs/karpenter/releases/tag/v1.14.1), [AWS provider release](https://github.com/aws/karpenter-provider-aws/releases/tag/v1.14.1)), and the AWS v1.14.1 release is tagged as a **Long-Term Support release supported until July 2027**.

---

## The mental model

Hold five ideas.

**1. Karpenter is a scheduler that can buy hardware.** Cluster Autoscaler asks "which of my pre-defined node groups, if I made it bigger, would let this pod schedule?" Karpenter asks "what is the cheapest single machine in the cloud's entire catalogue that satisfies the union of these pending pods' requirements?" It runs a real scheduling simulation over the pending pod set, bin-packs them, and then launches an instance shaped to fit. There is no group. There is no `desired` count to reconcile. The abstraction is one level lower and one level more powerful.

**2. Every node is owned by a NodeClaim, and every NodeClaim is finalizer-protected.** Karpenter sets a finalizer on each Node and NodeClaim it creates. Deleting either cascades to the other, and the finalizer blocks API deletion while the Termination Controller taints, drains, and waits ([Disruption](https://karpenter.sh/docs/concepts/disruption/)). This is why `kubectl delete node` on a Karpenter node behaves *correctly* (the instance is actually terminated) where on a non-Karpenter node it orphans the VM.

**3. There are two disruption modes and the difference is everything.** *Graceful* methods (consolidation, drift) pre-spin a replacement, wait for it to be ready, then drain — and they are rate-limited by NodePool disruption budgets. *Forceful* methods (expiration, interruption, node repair) start draining immediately, do not pre-spin, and **cannot be rate-limited by disruption budgets** ([Disruption](https://karpenter.sh/docs/concepts/disruption/)). Almost every "Karpenter took my cell down" incident is a forceful method meeting a workload that assumed graceful.

**4. Configuration drift is a feature, not a bug.** Karpenter hashes the NodePool's `NodeClaimTemplateSpec` and the NodeClass spec and compares it against each NodeClaim. Change the AMI selector, and every node in the pool is marked `Drifted` and rolled. This is the mechanism you will use for cell node-image upgrades — and it is also the mechanism that will roll your entire fleet at 3 a.m. if you `kubectl apply` a NodeClass change without a budget in place.

**5. Karpenter is a core library plus per-cloud providers at wildly different maturity.** `kubernetes-sigs/karpenter` is a Go **library**, not a binary — the only `package main` in the repo is the KWOK test provider. AWS is mature and LTS-tagged. Azure is production but on a `v1beta1` NodeClass and effectively AKS-only. GCP has **no official provider at all**. Plan accordingly.

```text
                    pending pods
                         │
                         ▼
   ┌─────────────────────────────────────────────────┐
   │  Provisioning Controller                        │
   │  batch (1s idle / 10s max) → simulate schedule  │
   │  → pick instance types → create NodeClaim(s)    │
   └─────────────────────────────────────────────────┘
                         │ CloudProvider.Create()
                         ▼
   NodeClaim ──owns──▶ Node ──backed by──▶ EC2 / VMSS / GCE instance
        ▲                                       │
        │                                       │
   ┌────┴────────────────────────────┐     ┌────┴──────────────┐
   │ Disruption Controller            │     │ Termination Ctrl  │
   │  1. Drift    (graceful, budgeted)│     │  taint → evict →  │
   │  2. Consolidation (graceful, bud)│     │  wait volumes →   │
   │  ---- not budgeted -------------- │     │  terminate → rm   │
   │  Expiration / Interruption /      │     │  finalizer        │
   │  Node Repair (forceful)           │     └───────────────────┘
   └──────────────────────────────────┘
```

---

## Core concepts

### Why Karpenter exists: the Cluster Autoscaler comparison

Cluster Autoscaler (CA) is a *node group* autoscaler. It knows about ASGs, MIGs, and VMSSes, it simulates whether adding one node **shaped like the existing nodes in that group** would let a pending pod schedule, and it changes the group's desired count ([kubernetes/autoscaler FAQ](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md)). Every design consequence flows from that.

| Dimension | Cluster Autoscaler | Karpenter |
|---|---|---|
| Unit of scaling | Node group (ASG / MIG / VMSS); you pre-create one per shape | None. Instance type is chosen per launch from the whole catalogue |
| Instance-type selection | You encode it in the group; CA picks *a group* | Karpenter picks the instance type from a `NodePool`'s requirements at launch time |
| Shape diversity | Requires N groups for N shapes; CA assumes nodes in a group are identical | One `NodePool` can span hundreds of instance types |
| Bin-packing | CA does not bin-pack; the kube-scheduler does, after the node exists | Karpenter simulates the pod→node assignment before choosing a shape |
| Provisioning path | Cloud autoscaling group API | Direct instance API (`ec2:CreateFleet` on AWS) |
| Multi-AZ | Groups are usually per-AZ; balancing is a CA feature flag | Zone is just another requirement key (`topology.kubernetes.io/zone`) |
| Scale-down | Utilization threshold + unneeded-time, per node | Consolidation: delete-if-fits-elsewhere, or *replace with a cheaper node* |
| Consolidation by replacement | Not supported | First-class (`Replace` mechanism) |
| Cost awareness | None built in | Prices instance-type offerings; consolidation is explicitly a cost decision |
| Node expiry / image rolling | External tooling | Built in (`expireAfter`, drift) |
| Spot interruption handling | Separate component (e.g. AWS Node Termination Handler) | Built in via SQS interruption queue |
| Rate limiting | `--max-empty-bulk-delete`, etc. | NodePool disruption budgets with cron windows and per-reason scoping |

Two things CA does that Karpenter deliberately does not: CA works on any cloud with a node-group API (it has ~25 providers), and CA's blast radius is bounded by the groups you defined. Karpenter's power is exactly its danger — a NodePool with `requirements: []` is "any instance type this cloud sells."

The migration story is documented end-to-end at [Migrating from Cluster Autoscaler](https://karpenter.sh/docs/getting-started/migrating-from-cas/), and the summary is: scale CA to zero, shrink the managed node group to a minimum that hosts Karpenter itself, and let Karpenter take the rest.

### The v1 API: three kinds

Karpenter's API is three resources across two API groups.

| Kind | Group / version | Scope | Owned by | Purpose |
|---|---|---|---|---|
| `NodePool` | `karpenter.sh/v1` | Cluster | You | Constraints + disruption policy. The template. |
| `NodeClaim` | `karpenter.sh/v1` | Cluster | Karpenter | One requested machine. You never write these. |
| `EC2NodeClass` | `karpenter.k8s.aws/v1` | Cluster | You | AWS-specific: subnets, SGs, AMI, IAM role, kubelet, disks |
| `AKSNodeClass` | `karpenter.azure.com/v1beta1` | Cluster | You | Azure equivalent — note the **v1beta1** |
| `GCENodeClass` | `karpenter.k8s.gcp/v1alpha1` | Cluster | You | Third-party GCP provider — **v1alpha1** |

The split is deliberate: `NodePool`/`NodeClaim` are the provider-agnostic core (`sigs.k8s.io/karpenter`), and the NodeClass is the provider's escape hatch. A `NodePool` points at exactly one NodeClass via `nodeClassRef`, which in v1 requires `group`, `kind`, and `name` — the `apiVersion` field was replaced by `group` because only a single version is served ([NodePools](https://karpenter.sh/docs/concepts/nodepools/)).

#### The v1beta1 → v1 migration, and why the internet is wrong about it

The upgrade path was: Karpenter **1.0** shipped CRDs with both `v1beta1` and `v1` versions plus **conversion webhooks** (from the core project and from each cloud provider, for NodeClass changes), so existing objects were converted at runtime. Karpenter **1.1.0 dropped v1beta1 support** ([v1 Migration](https://karpenter.sh/v1.0/upgrading/v1-migration/)). If you inherit a cluster pinned below 1.1, you must land on 1.0.x with webhooks healthy *before* going forward.

The single field move that breaks the most copy-pasted YAML: **`kubelet` moved from `NodePool.spec.template.spec` to `EC2NodeClass.spec`**. The migration guide calls this out as hard to handle via conversion webhooks because NodePool→NodeClass is many-to-one. The NodePool docs now carry the note verbatim: *"Objects for setting Kubelet features have been moved from the NodePool spec to the EC2NodeClasses spec, to not require other Karpenter providers to support those features"* ([NodePools](https://karpenter.sh/docs/concepts/nodepools/)).

The other v1 change worth internalizing: **`Drift` was promoted to stable and its feature gate removed.** You can no longer turn drift off with a flag. The documented replacement is to suppress it with a disruption budget scoped to `reasons: [Drifted]` ([Settings — feature gates](https://karpenter.sh/docs/reference/settings/#feature-gates)).

#### A production-shaped NodePool

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: temporal-services
spec:
  # `template` is a NodeClaim template. Karpenter treats it as the MINIMUM
  # constraint set and further narrows it with each pending pod's requirements.
  template:
    metadata:
      labels:
        cell.example.com/pool: services
      annotations:
        example.com/owner: infra-foundation
    spec:
      # v1 requires group+kind+name. There is no apiVersion field here.
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: temporal-services

      # Applied to every node. Pods must tolerate these to land here.
      taints:
        - key: cell.example.com/dedicated
          value: services
          effect: NoSchedule

      # Applied to the node at launch, but pods do NOT need to tolerate them.
      # Karpenter assumes some other agent removes them. If you get this list
      # wrong, Karpenter will provision infinitely -- see gotcha #4.
      startupTaints:
        - key: node.cilium.io/agent-not-ready
          value: "true"
          effect: NoExecute

      # Maximum node lifetime. FORCEFUL: budgets do not gate expiration.
      # Default is 720h (30 days). "Never" disables it.
      expireAfter: 336h

      # Hard cap on how long a node may spend draining before Karpenter
      # force-deletes remaining pods and terminates the instance.
      # Setting this also lets DRIFT bypass blocking PDBs and do-not-disrupt.
      terminationGracePeriod: 4h

      # Requirements narrow the instance-type search space. Operators:
      # In, NotIn, Exists, DoesNotExist, Gt, Lt, Gte, Lte.
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: kubernetes.io/os
          operator: In
          values: ["linux"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
          # minValues forces the scheduler to keep at least N distinct values
          # in play, which is how you buy capacity-pool diversity.
          minValues: 2
        - key: karpenter.k8s.aws/instance-generation
          operator: Gte
          values: ["6"]
        - key: node.kubernetes.io/instance-type
          operator: Exists
          minValues: 10
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["us-west-2a", "us-west-2b", "us-west-2c"]

  disruption:
    # WhenEmpty | Balanced | WhenEmptyOrUnderutilized
    consolidationPolicy: WhenEmptyOrUnderutilized
    # Stability timer: reset every time a pod is added to or removed from
    # the node. "Never" disables consolidation for this pool.
    consolidateAfter: 5m
    budgets:
      # Baseline ceiling for everything.
      - nodes: "10%"
      # Absolute ceiling regardless of pool size.
      - nodes: "5"
      # No voluntary churn during business hours, UTC only.
      - nodes: "0"
        schedule: "0 14 * * mon-fri"
        duration: 10h

  # Hard ceiling on total pool size. Eventually consistent -- see gotcha #9.
  limits:
    cpu: "2000"
    memory: 4000Gi
    nodes: "80"

  # Higher weight wins when multiple NodePools match a pod.
  weight: 50
```

#### Requirements, well-known keys, and the 100-key ceiling

Karpenter respects the Kubernetes well-known labels plus provider-specific ones. The AWS set includes `karpenter.k8s.aws/instance-family`, `instance-category`, `instance-generation`, `instance-cpu`, `instance-hypervisor`, `instance-tenancy`, and `instance-capability-flex`; the core set includes `kubernetes.io/arch`, `kubernetes.io/os`, `topology.kubernetes.io/zone`, `node.kubernetes.io/instance-type`, and `karpenter.sh/capacity-type` ([NodePools](https://karpenter.sh/docs/concepts/nodepools/)).

Three sharp edges:

- **`karpenter.sh/capacity-type` now has three values: `reserved`, `spot`, `on-demand`.** `reserved` means on-demand capacity reservations and capacity blocks, *not* Reserved Instances. When a NodePool allows several, Karpenter prioritizes `reserved` → `spot` → `on-demand`. This is gated by `ReservedCapacity`, which has been **Beta and on by default since v1.6** ([Settings](https://karpenter.sh/docs/reference/settings/)).
- **There is a hard limit of 100 requirements across a NodePool and its NodeClaim**, and `spec.template.metadata.labels` are propagated as requirements too — so labels count against the same 100 ([NodePools](https://karpenter.sh/docs/concepts/nodepools/)).
- **`minValues` behavior is policy-driven.** `MIN_VALUES_POLICY` / `--min-values-policy` defaults to `Strict`, which fails the scheduling loop for that NodePool if the minimum flexibility cannot be met (falling back to another NodePool, or leaving the pod pending). `BestEffort` relaxes `minValues` instead ([Settings](https://karpenter.sh/docs/reference/settings/)). If multiple `minValues` are given for the same key, the maximum wins.

#### Weights and multiple NodePools

`spec.weight` is a scheduling preference between NodePools, analogous to affinity weights; unspecified is equivalent to 0, and the highest weight wins when several match ([NodePools](https://karpenter.sh/docs/concepts/nodepools/)). The docs are explicit that you should *prefer mutually exclusive NodePools* and use weight only as a tie-break. The canonical weighted pattern is: a high-weight `reserved`/`spot` pool and a low-weight `on-demand` fallback pool with otherwise identical requirements.

Note the interaction with drift: `spec.weight`, `spec.limits`, and everything under `spec.disruption.*` are classified as **behavioral fields and are explicitly not considered for drift** ([Disruption — behavioral fields](https://karpenter.sh/docs/concepts/disruption/)). Changing a budget does not roll your fleet. Changing `spec.template.spec.requirements` may.

#### EC2NodeClass — the AWS-specific half

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: temporal-services
spec:
  # kubelet lives HERE in v1, not on the NodePool.
  kubelet:
    maxPods: 110
    systemReserved:
      cpu: 200m
      memory: 512Mi
      ephemeral-storage: 2Gi
    kubeReserved:
      cpu: 200m
      memory: 1Gi
    evictionHard:
      memory.available: 5%
      nodefs.available: 10%
    imageGCHighThresholdPercent: 85
    imageGCLowThresholdPercent: 80

  # amiFamily drives UserData generation and default block device mappings.
  # May be omitted when using an `alias` amiSelectorTerm.
  amiFamily: AL2023

  # Discovery is tag-based. Terms are OR'd; conditions within a term are AND'd.
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "${CLUSTER_NAME}"
        cell.example.com/tier: private
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "${CLUSTER_NAME}"

  # Pin the image. `alias: al2023@latest` is a moving target and WILL drift
  # your whole fleet whenever AWS publishes a new AMI.
  amiSelectorTerms:
    - alias: al2023@v20260812

  # One of role / instanceProfile is required.
  role: "KarpenterNodeRole-${CLUSTER_NAME}"

  tags:
    cell.example.com/cell: "${CELL_ID}"
    team: infra-foundation

  metadataOptions:
    httpEndpoint: enabled
    httpTokens: required        # IMDSv2 only
    httpPutResponseHopLimit: 1  # blocks IMDS from non-hostNetwork containers

  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 200Gi
        volumeType: gp3
        iops: 6000
        throughput: 250
        encrypted: true
        kmsKeyID: "arn:aws:kms:us-west-2:111122223333:key/..."
        deleteOnTermination: true

  # Use NVMe instance storage for ephemeral-storage instead of EBS.
  instanceStorePolicy: RAID0

  detailedMonitoring: true
```

`status` on an EC2NodeClass is worth watching in CI: it publishes the *resolved* `subnets` (with zones), `securityGroups`, and `amis` (with per-architecture requirements). A cell that provisions nothing is very often an EC2NodeClass whose `status.subnets` is empty because the discovery tag was never applied.

Two v1 additions relevant to cells: `capacityReservationSelectorTerms` (for ODCRs/capacity blocks, paired with `capacity-type: reserved`) and `placementGroupSelector` ([NodeClasses](https://karpenter.sh/docs/concepts/nodeclasses/)).

### Disruption in depth

This is the part that matters for long-lived workloads. Read the control flow literally:

1. Pick a disruption method — **Drift first, then Consolidation** — and build a prioritized candidate list.
2. For each candidate, check the NodePool's disruption budget, then run a scheduling simulation to determine what replacements are needed.
3. Taint the node(s) with `karpenter.sh/disrupted:NoSchedule`.
4. **Pre-spin replacements and wait for them to become ready.** If a replacement fails to initialize, un-taint and restart from step 1.
5. Delete the node(s); the Termination Controller drains them.

([Disruption](https://karpenter.sh/docs/concepts/disruption/))

Step 4 is the property you want and CA does not give you: capacity exists before capacity is removed.

#### Consolidation policies

| Policy | Nodes considered | Use when |
|---|---|---|
| `WhenEmpty` | Only nodes whose remaining pods have zero disruption cost (DaemonSets, annotated-cheap pods) | Maximum conservatism. Nothing running gets evicted for cost. |
| `Balanced` | Same set as `WhenEmptyOrUnderutilized`, but each action is *scored* — savings vs. weighted pod disruption — and skipped when the savings do not justify the churn | You want most of the savings without marginal churn |
| `WhenEmptyOrUnderutilized` | Any node that can be removed or replaced to reduce cost | Lowest cost, most churn |

Defaults if you write nothing: `consolidationPolicy: WhenEmptyOrUnderutilized`, `consolidateAfter: 0s` ([Disruption — defaults](https://karpenter.sh/docs/concepts/disruption/)). That default is aggressive. `consolidateAfter: 0s` means a node becomes a consolidation candidate the instant its pod set stops changing.

`Balanced` is the newer, quieter option. Every pod contributes equal weight by default (so "disruption" is effectively pod count), with higher-priority pods weighted more. Decisions emit a `ConsolidationApproved` event carrying the score and the savings/disruption percentages, and are exported as `karpenter_consolidation_score` and `karpenter_consolidation_moves_total`, labeled by decision, NodePool, and policy ([Disruption — balanced consolidation](https://karpenter.sh/docs/concepts/disruption/)).

Consolidation runs three mechanisms **in this order**:

1. **Empty node consolidation** — delete all fully-empty nodes in parallel.
2. **Multi-node consolidation** — delete two or more nodes together, possibly launching one cheaper replacement. Heuristic, because the search space is exponential.
3. **Single-node consolidation** — delete or replace one node at a time. Every node is evaluated individually.

When several nodes are candidates, Karpenter prefers to terminate nodes running **fewer pods**, nodes that will **expire soon**, and nodes with **lower-priority pods**.

#### Disruption budgets — syntax and semantics

```yaml
spec:
  disruption:
    budgets:
      # Applies only to Empty and Drifted reasons.
      - nodes: "20%"
        reasons: ["Empty", "Drifted"]
      # No `reasons` => applies to ALL reasons. Acts as a global ceiling.
      - nodes: "5"
      # Time-windowed. schedule + duration must be set together.
      # Cron is 5-field; macros (@daily, @hourly) supported. UTC only.
      - nodes: "0"
        schedule: "@daily"
        duration: 10m
        reasons: ["Underutilized"]
```

Semantics that bite:

- **The default, if you specify nothing, is a single budget of `nodes: 10%`** ([Disruption — NodePool disruption budgets](https://karpenter.sh/docs/concepts/disruption/)).
- Valid `reasons` are `Drifted`, `Underutilized`, and `Empty`. **A budget with no `reasons` applies to all of them.** When computing the allowance for a given reason, Karpenter takes the **minimum** across every budget that lists that reason or omits `reasons` entirely.
- The percentage formula is `allowed_disruptions = roundup(total * percentage) - total_deleting - total_notready`. Non-percentage values use the static ceiling minus the same subtractions. **`NotReady` nodes consume budget.** A cell with unhealthy nodes has less headroom for voluntary disruption, which is usually what you want but occasionally deadlocks you.
- **Budgets do not prevent Karpenter from terminating expired nodes**, and do not apply to interruption or node repair. This is stated explicitly in the docs and it is the single most common misreading of the feature.
- `duration` accepts hours and minutes only (`10h5m`, `30m`, `160h`) — cron has no sub-minute granularity. **Timezones are not supported; schedules are always UTC.**
- `budgets: [{nodes: "0"}]` is the documented way to disable all voluntary disruption for a NodePool.

#### Drift

Karpenter annotates the owning NodePool and NodeClass with a hash of the `NodeClaimTemplateSpec`/`EC2NodeClassSpec`, exactly like `deployment.spec.template` → pods. A NodeClaim whose values no longer match is given the `Drifted` status condition.

Special cases where drift is *resolved* rather than hashed:

| Resource | Fields |
|---|---|
| NodePool | `spec.template.spec.requirements` |
| EC2NodeClass | `spec.subnetSelectorTerms`, `spec.securityGroupSelectorTerms`, `spec.amiSelectorTerms` |

The asymmetry is instructive: widening `instance-type In [m5.large]` to `In [m5.large, m5.2xlarge]` does **not** drift an existing `m5.large` node, because its value is still compatible. But publishing a new AMI that `amiSelectorTerms` resolves to **does** drift every node, with no CRD change at all ([Disruption — drift](https://karpenter.sh/docs/concepts/disruption/)). That is the trap in `alias: al2023@latest`.

#### Expiration and terminationGracePeriod

`expireAfter` lives on `spec.template.spec` and is persisted to each NodeClaim. **Default 720h (30 days)**, or the literal string `Never`. Changing it on the NodePool does not update existing NodeClaims — it *drifts* them, and replacements get the new value.

The docs are careful about a distinction people get wrong: `expireAfter` is a **maximum** lifetime, not a minimum. Drift, consolidation, or emptiness can end a node much earlier. To get a *true* maximum you must combine `expireAfter` with budgets that suppress the other reasons.

`terminationGracePeriod` (`spec.template.spec.terminationGracePeriod`, persisted to `NodeClaim.spec.terminationGracePeriod`) is the hard cap on draining. Two consequences that matter enormously for Temporal-shaped workloads:

- **Setting it changes drift eligibility.** With `terminationGracePeriod` configured, a node may be disrupted via drift *even if* it hosts pods with blocking PDBs or the `karpenter.sh/do-not-disrupt` annotation. This is deliberate — it lets an operator guarantee CVE-driven AMI rollouts cannot be blocked by an application team.
- **Pods are preemptively deleted so their own `terminationGracePeriodSeconds` fits inside the node's window.** A pod with a 5-minute grace period on a node with a 1-hour `terminationGracePeriod` gets deleted at the 55-minute mark. And if a pod's grace period *exceeds* the node's, the node wins and the pod is deleted as soon as draining starts, never receiving its full grace period.

Maximum node lifetime is therefore `expireAfter + terminationGracePeriod`.

The docs carry an explicit warning worth repeating: **do not set `expireAfter` without `terminationGracePeriod` if any pod carries `karpenter.sh/do-not-disrupt`**, because expiration is forceful, `do-not-disrupt` blocks draining, and you end up with partially-drained nodes stuck in the cluster forever, requiring manual intervention.

#### `karpenter.sh/do-not-disrupt`

Two formats on **pods**:

| Format | Example | Behavior |
|---|---|---|
| Boolean | `karpenter.sh/do-not-disrupt: "true"` | Permanent protection |
| Go duration | `karpenter.sh/do-not-disrupt: "30m"` | Protection for that long after the pod starts running |

The duration format is the newer and, for a job-shaped workload, the better one: it protects a long activity without permanently pinning the node. An invalid duration string is ignored and an event is emitted on the pod.

Semantics: treat it as a **single-pod blocking PDB**. Nodes hosting active `do-not-disrupt` pods are excluded from consolidation, and *conditionally* excluded from drift (excluded unless `terminationGracePeriod` is set). The Termination Controller will not gracefully evict such pods, so node termination cannot complete until the annotation lapses, the pod reaches a terminal phase, or `terminationGracePeriod` elapses.

It also exists at **node** level (`karpenter.sh/do-not-disrupt: "true"` on the Node object), which blocks voluntary disruption of that node. Neither form protects against expiration, interruption, node repair, or manual `kubectl delete node`.

#### Interruption

Karpenter watches an **SQS queue** fed by EventBridge rules for: Spot Interruption Warnings, Scheduled Change Health Events, Instance Terminating/Stopping events, and Instance Status Check Failures. You enable it with `--interruption-queue` / `INTERRUPTION_QUEUE`; if unset, interruption handling is **disabled** ([Disruption — interruption](https://karpenter.sh/docs/concepts/disruption/), [Settings](https://karpenter.sh/docs/reference/settings/)). The CloudFormation template in the getting-started guide provisions the queue and rules.

Spot gives a **2-minute notice**. On receipt Karpenter provisions a replacement in parallel with draining, and the docs claim average startup time is usually enough to have the replacement ready first. Notably, Karpenter publishes events for **Spot Rebalance Recommendations** but **does not act on them** — if you want rebalance handling you must run AWS Node Termination Handler alongside, accepting more churn.

Separately, Karpenter polls **EC2 `DescribeInstanceStatus`** for System Status, Instance Status, and Scheduled Maintenance Events. This path needs no SQS queue, only the IAM permission.

#### Node Auto Repair

`NodeRepair=true` is **alpha, off by default, since v1.1** ([Settings — feature gates](https://karpenter.sh/docs/reference/settings/)). It requires a node diagnostic agent (EKS Node Monitoring Agent or Node Problem Detector) that writes status conditions. Karpenter then force-terminates nodes past a toleration duration, **bypassing drain and grace period entirely**.

| Condition | Status | Toleration |
|---|---|---|
| `Ready` | `False` | 30m |
| `Ready` | `Unknown` | 30m |
| `AcceleratedHardwareReady` | `False` | 10m |
| `StorageReady` / `NetworkingReady` / `KernelReady` / `ContainerRuntimeReady` | `False` | 30m |

Safety valve: **Karpenter will not repair if more than 20% of nodes in a NodePool are unhealthy** (for standalone NodeClaims, the threshold is evaluated against all nodes in the cluster). That is a good circuit breaker against a bad AMI or a broken CNI rollout turning into fleet-wide force-termination.

### Spot

Three mechanisms, all worth knowing precisely.

**Allocation strategy.** Karpenter selects lower-priced spot instance types using the **`price-capacity-optimized`** strategy ([Disruption — spot consolidation](https://karpenter.sh/docs/concepts/disruption/), [AWS: price-capacity-optimized](https://aws.amazon.com/blogs/compute/introducing-price-capacity-optimized-allocation-strategy-for-ec2-spot-instances/)). This trades a little price for a lot of interruption-rate improvement versus `lowest-price`. Karpenter does not expose a knob to change it.

**Spot-to-spot consolidation.** For spot nodes, **deletion** consolidation is on by default, but **replacement** consolidation requires the `SpotToSpotConsolidation` feature gate, which is **alpha and off by default since v0.34** ([Settings](https://karpenter.sh/docs/reference/settings/)). When enabled, Karpenter uses "instance type flexibility" — the number of instance types available at a price lower than the current node — as a heuristic, and **requires a minimum of 15 instance types** for a single-node (1→1) spot-to-spot consolidation. Multi-node (many→1) consolidations have no such requirement, because they cannot produce a race to the bottom. The stated goals: don't consolidate down to the cheapest, highest-interruption instance, and always land with enough diversity that the replacement has comparable availability.

**Diversification.** This is on you. The `minValues` mechanism is the lever: set `minValues` on `instance-family` and `node.kubernetes.io/instance-type` so the scheduler keeps a wide pool in play. The docs' own spot example asks for at least 2 instance categories, 5 families, and 10 instance types.

For Temporal history hosts, my opinion: spot is defensible for stateless frontend and worker tiers with generous replica counts, and is not defensible for anything holding shard ownership, because a 2-minute notice against a shard-rebalance window is a bad trade even with pre-spun replacements.

### How Karpenter interacts with PDBs, StatefulSets, and long-lived workloads

**PDBs.** The Termination Controller evicts via the Kubernetes Eviction API, so PDBs are honored — but the composition rule is harsher than people expect. From the docs: if a pod matches **multiple** PDBs, **all** of them must allow disruption; when different pods on the same node belong to different PDBs, **all** must simultaneously permit eviction; and **a single blocking PDB prevents the entire node from being voluntarily disrupted** ([Disruption — pod-level controls](https://karpenter.sh/docs/concepts/disruption/)). The docs' own example: Pod A matching both `maxUnavailable: 0` and `maxUnavailable: 1` is blocked by the stricter one. The practical corollary is that dense nodes with many distinct PDBs become effectively immortal.

Static pods, pods tolerating `karpenter.sh/disrupted:NoSchedule`, and Succeeded/Failed pods are ignored during drain.

**StatefulSets.** Karpenter has no special StatefulSet handling. What it does have is **VolumeAttachment verification**: step 3 of the termination flow explicitly verifies that all `VolumeAttachment` resources for drain-able pods are deleted before terminating the instance. This is what prevents the classic "new pod can't attach the EBS volume because the old node still holds it" stall. For a StatefulSet the practical controls are: a PDB with `maxUnavailable: 1`, a `terminationGracePeriod` long enough for your longest legitimate shutdown, and — for genuinely non-relocatable state — `karpenter.sh/do-not-disrupt` with a duration.

**Long-running workloads.** The combination I would ship for a Temporal cell:

```yaml
# On the workload
apiVersion: apps/v1
kind: StatefulSet
spec:
  template:
    metadata:
      annotations:
        # Cheap-to-move? Omit. Expensive? Use a duration, not "true".
        karpenter.sh/do-not-disrupt: "45m"
    spec:
      terminationGracePeriodSeconds: 900
---
apiVersion: policy/v1
kind: PodDisruptionBudget
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: temporal-history
```

paired with a NodePool that sets `terminationGracePeriod: 4h` (so drift can still land AMI CVE fixes), `consolidateAfter: 15m` (so normal shard rebalancing does not look like an idle node), and a budget of `nodes: "1"` for `reasons: [Drifted]` so image rollouts are strictly serial.

*See also: [upgrade mechanics](04-managed-kubernetes-eks-gke-aks.md#upgrade-mechanics) for the baseline PDB, drain, and eviction semantics the managed node upgraders enforce — Karpenter's eviction path is the same Eviction API with harsher composition rules on top.*

### Scheduling inputs Karpenter respects — and the ones it handles poorly

Karpenter runs a scheduling simulation, so it must reimplement a meaningful subset of kube-scheduler. What it consumes ([Scheduling](https://karpenter.sh/docs/concepts/scheduling/)):

| Input | Handling |
|---|---|
| Resource requests | First-class. This is the bin-packing input. **Limits are ignored** for sizing. |
| `nodeSelector` | First-class; intersected with NodePool requirements |
| `requiredDuringScheduling` node affinity | First-class |
| Taints / tolerations | First-class; a NodePool whose taints a pod does not tolerate is skipped |
| `topologySpreadConstraints` (`DoNotSchedule`) | Supported; zone, hostname, and `karpenter.sh/capacity-type` are usable topology keys |
| Pod affinity / anti-affinity (required) | Supported, with topology-key caveats |
| Persistent volume topology | Supported — PVC zone constraints narrow the launchable zones |
| DaemonSets | Accounted for in capacity math (`karpenter_nodes_total_daemon_requests`) |
| **Preferences** (`preferredDuringScheduling`, `ScheduleAnyway`) | Handled, but this is the weak spot |
| **DRA (Dynamic Resource Allocation)** | Not formally supported; `IGNORE_DRA_REQUESTS` exists as a stopgap |

The preference problem is real and documented in a warning box:

> *"Using preferred anti-affinity and topology spreads can reduce the effectiveness of consolidation. At node launch, Karpenter attempts to satisfy affinity and topology spread preferences. In order to reduce node churn, consolidation must also attempt to satisfy these constraints to avoid immediately consolidating nodes after they launch. This means that consolidation may not disrupt nodes in order to avoid violating preferences, even if kube-scheduler can fit the host pods elsewhere."* ([Disruption](https://karpenter.sh/docs/concepts/disruption/))

Karpenter logs these, e.g. `pod default/inflate-anti-self-55894c5d8b-522jd has a preferred Anti-Affinity which can prevent consolidation`. The global escape hatch is `PREFERENCE_POLICY` / `--preference-policy`, which accepts `Respect` (default) or `Ignore` and covers preferred node/pod affinities and `ScheduleAnyway` topology spreads ([Settings](https://karpenter.sh/docs/reference/settings/)). Setting it to `Ignore` makes bin-packing tighter and consolidation more effective, at the cost of ignoring soft spreading intent — a reasonable trade for a homogeneous cell, a bad one for a multi-tenant cluster.

**DRA is the honest gap.** `IGNORE_DRA_REQUESTS` is documented as *"NOTE: This flag will be removed once formal DRA support is GA in Karpenter"* ([Settings](https://karpenter.sh/docs/reference/settings/)). If your cells move to DRA-based device allocation, verify Karpenter's status at that time rather than assuming.

**Batching.** Karpenter batches pending pods before deciding. `BATCH_IDLE_DURATION` defaults to **1s** (each new pending pod extends the window) and `BATCH_MAX_DURATION` caps the window at **10s**. Larger values yield fewer, larger nodes; smaller values yield faster, smaller launches.

**Memory overhead.** `VM_MEMORY_OVERHEAD_PERCENT` defaults to **0.075** — 7.5% subtracted from advertised memory when cached allocatable data is unavailable. Get this wrong and you either waste capacity or Karpenter launches nodes that turn out too small and immediately provisions again.

### Newer capabilities worth knowing exist

| Feature | Gate | Stage / since | What it does |
|---|---|---|---|
| `ReservedCapacity` | on by default | Beta since **v1.6** | `capacity-type: reserved` for ODCRs and capacity blocks |
| `SpotToSpotConsolidation` | off | Alpha since **v0.34** | Replacement consolidation between spot nodes |
| `NodeRepair` | off | Alpha since **v1.1** | Auto-replace unhealthy nodes |
| `NodeOverlay` | off | Alpha since **v1.7** | Adjust instance-type capacity/price data Karpenter reasons over |
| `StaticCapacity` | off | Alpha since **v1.8** | `NodePool.spec.replicas` — a fixed-size pool |
| `CapacityBuffer` | off | Alpha since **v1.13** | Headroom / over-provisioning |

Source: [Settings — feature gates](https://karpenter.sh/docs/reference/settings/).

**Static NodePools** deserve a note for cell work, because they are the closest thing to a node group Karpenter has. Setting `spec.replicas` maintains a fixed node count regardless of pod demand. Constraints: cannot be removed once set (no static↔dynamic switching), only `limits.nodes` is allowed in `limits`, `weight` cannot be set, **nodes are not considered for consolidation**, and scale operations **bypass node disruption budgets but still respect PodDisruptionBudgets**. You scale with `kubectl scale nodepool <name> --replicas=<n>` ([NodePools — spec.replicas](https://karpenter.sh/docs/concepts/nodepools/)). This is a plausible answer to "where do the cell's own control-plane-adjacent pods run."

### Multi-cloud status — verified, not assumed

This is the section where you should trust nothing you read elsewhere.

| | AWS | Azure | GCP |
|---|---|---|---|
| Repo | [`aws/karpenter-provider-aws`](https://github.com/aws/karpenter-provider-aws) | [`Azure/karpenter-provider-azure`](https://github.com/Azure/karpenter-provider-azure) | [`cloudpilot-ai/karpenter-provider-gcp`](https://github.com/cloudpilot-ai/karpenter-provider-gcp) — **third party** |
| Governance | AWS-maintained, CNCF-adjacent via kubernetes-sigs core | Microsoft-maintained | Vendor-maintained |
| Version (2026-08) | v1.14.1, LTS to Jul 2027 | v1.14.2 | v0.6.0 |
| NodeClass kind | `EC2NodeClass` | `AKSNodeClass` | `GCENodeClass` |
| NodeClass group/version | `karpenter.k8s.aws/**v1**` | `karpenter.azure.com/**v1beta1**` | `karpenter.k8s.gcp/**v1alpha1**` |
| Managed offering | none (you run it) | **AKS Node Auto Provisioning (NAP)** — GA | none |
| Runs outside the managed service? | Yes, any Kubernetes on EC2 | **No** — requires an AKS cluster | **No** — requires a GKE Standard node pool |
| Interruption handling | SQS + EventBridge | provider-specific | limited |

**Core.** [`kubernetes-sigs/karpenter`](https://github.com/kubernetes-sigs/karpenter) is a Go library (`module sigs.k8s.io/karpenter`), not a runnable controller. Its `CloudProvider` interface is nine methods — `Create`, `Delete`, `Get`, `List`, `GetInstanceTypes`, `IsDrifted`, `RepairPolicies`, `Name`, `GetSupportedNodeClasses` ([types.go](https://github.com/kubernetes-sigs/karpenter/blob/main/pkg/cloudprovider/types.go)). Every provider vendors the library and ships its own binary. Core and the AWS provider version in lockstep; both released v1.14.1 on 2026-08-21 within minutes of each other.

**Azure.** The provider is real and production-grade, but two facts constrain how you'd use it. First, **`AKSNodeClass` is still `karpenter.azure.com/v1beta1`** — the `v1beta1` package carries the `+kubebuilder:storageversion` marker and there is no `v1` package in the repo, and both the repo README and Microsoft Learn use `v1beta1` in every example ([AKSNodeClass on Microsoft Learn](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning-aksnodeclass)). So a multi-cloud abstraction over NodeClasses must handle a version skew between clouds. Second, **AKS Node Auto Provisioning *is* this provider**, run as a managed addon: NAP "automatically deploys, configures, and manages Karpenter on your AKS clusters and is based on the open-source Karpenter and AKS Karpenter provider projects" ([AKS NAP](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning)), enabled with `az aks create/update --node-provisioning-mode Auto`. The repo README explicitly recommends NAP over self-hosting and notes that Microsoft support channels cover NAP only, with self-hosted supported best-effort via GitHub issues. **Self-hosted still requires AKS** — the provider's own install script calls `az aks show` to read the node resource group, network profile, and kubelet identity. NAP is GA; I could not verify the exact GA date from a primary source (the Azure Updates page would not render), so treat "mid-2025" as approximate. Key `AKSNodeClass` fields: `imageFamily` (`Ubuntu` | `Ubuntu2204` | `Ubuntu2404` | `AzureLinux`), `osDiskSizeGB` (30–2048, default 128), `maxPods` (10–250), `vnetSubnetID`, `kubelet`, `fipsMode`, `tags`.

**GCP.** There is **no official Karpenter provider for GCP** — nothing under `kubernetes-sigs`, nothing under `google`. The `kubernetes-sigs/karpenter` README's GCP entry points at the third-party CloudPilot AI repo. That provider is at v0.6.0, defines `GCENodeClass` at `karpenter.k8s.gcp/v1alpha1`, and requires **GKE Standard** with an existing node pool — architecturally, because it reads bootstrap metadata (instance templates and `kube-env`) from that node pool. GKE's native answers are Cluster Autoscaler, [node auto-provisioning](https://cloud.google.com/kubernetes-engine/docs/concepts/node-auto-provisioning) (described as "an extension of the GKE cluster autoscaler," creating and deleting whole node pools), and [custom ComputeClasses](https://cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes), whose `spec.priorities` hierarchy is the closest native analogue to a Karpenter NodePool's fallback behavior.

**Practical consequence for a three-cloud cell platform:** you cannot write "one NodePool YAML, three clouds." You can write one `NodePool` (`karpenter.sh/v1` is genuinely portable) and three NodeClass templates at three API versions, and on GCP you should probably not use Karpenter at all — use ComputeClasses and accept that GCP cells have a different node-lifecycle implementation. Be honest about that in design docs rather than pretending the abstraction is uniform.

There is also [`kubernetes-sigs/karpenter-provider-cluster-api`](https://github.com/kubernetes-sigs/karpenter-provider-cluster-api), which drives CAPI `MachineDeployment`s. Its README calls it "an experimental proof of concept," it has exactly one release (v0.2.0, Oct 2025), and drift detection, consolidation, and cost integration are all unimplemented. Interesting as a design reference; not a production path today.

### Installation, IAM, and the bootstrap chicken-and-egg

The AWS install is a Helm chart from `oci://public.ecr.aws/karpenter/karpenter`, into the **`kube-system`** namespace in the current guide, with `settings.clusterName`, `settings.interruptionQueue`, and an IRSA role annotation ([Migrating from CAS](https://karpenter.sh/docs/getting-started/migrating-from-cas/)).

**Two IAM roles, not one:**

- **`KarpenterNodeRole-<cluster>`** — assumed by EC2, attached policies `AmazonEKSWorkerNodePolicy`, `AmazonEKS_CNI_Policy`, `AmazonEC2ContainerRegistryPullOnly`, `AmazonSSMManagedInstanceCore`. Referenced from `EC2NodeClass.spec.role`.
- **`KarpenterControllerRole-<cluster>`** — assumed by the controller's ServiceAccount via IRSA (OIDC trust scoped to `system:serviceaccount:kube-system:karpenter`).

The controller policy is worth reading rather than copying blindly, because it is a real privilege-escalation surface. Broad `Resource: "*"` actions include `ec2:RunInstances`, `ec2:CreateFleet`, `ec2:CreateLaunchTemplate`, `ec2:CreateTags`, the `Describe*` family, `ssm:GetParameter`, and `pricing:GetProducts`. Then the scoped parts:

- `ec2:TerminateInstances` is conditioned on `StringLike: {"ec2:ResourceTag/karpenter.sh/nodepool": "*"}` — Karpenter can only terminate instances it tagged.
- `iam:PassRole` is scoped to exactly the node role ARN.
- Instance-profile create/tag/modify actions are conditioned on `aws:RequestTag`/`aws:ResourceTag` for `kubernetes.io/cluster/<name>: owned`, `topology.kubernetes.io/region`, and `karpenter.k8s.aws/ec2nodeclass`.

**Note what this means: `ec2:RunInstances` is unscoped.** Karpenter can launch any instance type in the account. Your cost control is `NodePool.spec.limits`, not IAM. For a cell platform, one cell per account (which Temporal's published cell architecture already implies) makes that blast radius acceptable; a shared account makes it a real risk.

**The bootstrap chicken-and-egg.** Karpenter provisions nodes, so something must provision the node Karpenter runs on. Karpenter cannot run on a node it owns — if it did, consolidating that node would kill the controller mid-flight, and a cold cluster with zero nodes could never start it. The documented answers:

1. **A small managed node group** (EKS MNG / AKS system pool) sized for Karpenter plus other cluster-critical pods. The migration guide pins Karpenter to it with node affinity:

   ```yaml
   affinity:
     nodeAffinity:
       requiredDuringSchedulingIgnoredDuringExecution:
         nodeSelectorTerms:
           - matchExpressions:
               - key: karpenter.sh/nodepool
                 operator: DoesNotExist     # never on a Karpenter node
               - key: eks.amazonaws.com/nodegroup
                 operator: In
                 values: ["${NODEGROUP}"]
     podAntiAffinity:
       requiredDuringSchedulingIgnoredDuringExecution:
         # The labelSelector is load-bearing: a PodAffinityTerm with a null
         # selector matches no pods, so without it this term does nothing.
         - topologyKey: "kubernetes.io/hostname"
           labelSelector:
             matchLabels:
               app.kubernetes.io/instance: karpenter
   ```

   The guide recommends a **minimum of 2 instances for a single multi-AZ node group, or 1 per group for single-AZ groups**, and suggests doing the same for CoreDNS and metrics-server.

2. **EKS Fargate** for the Karpenter deployment — no node group at all. Not covered in the migration guide, but it is the other common production answer and removes the "who upgrades the bootstrap node group" question.

3. **AKS NAP** dodges it entirely: the addon runs on the AKS-managed system pool.

For cell lifecycle, this is a real design decision, because the bootstrap node group is *another thing to upgrade* on every cell, with its own AMI and its own drain semantics — and it is not managed by Karpenter's drift mechanism.

**Upgrade path.** Karpenter follows semver; breaking changes appear in minor releases with a permanent "upgrading to x.y.z+" section in the release notes ([Compatibility](https://karpenter.sh/docs/upgrading/compatibility/)). The Kubernetes compatibility matrix as of v1.14:

| Kubernetes | 1.30 | 1.31 | 1.32 | 1.33 | 1.34 | 1.35 | 1.36 |
|---|---|---|---|---|---|---|---|
| Minimum Karpenter | >= 0.37 | >= 1.0.5 | >= 1.2 | >= 1.5 | >= 1.6 | >= 1.9 | >= 1.13 |

Three release channels: **stable** (semver-tagged, the only one for production), **release candidates** (`x.y.z-rc.N`), and **snapshot** (per-commit, published to a private ECR at `021119463062.dkr.ecr.us-east-1.amazonaws.com`, **removed after 90 days**, never for production).

The upgrade procedure that people skip: **CRDs are not upgraded by `helm upgrade` on the main chart.** The migration guide applies them explicitly from raw GitHub URLs (`karpenter.sh_nodepools.yaml`, `karpenter.sh_nodeclaims.yaml`, `karpenter.k8s.aws_ec2nodeclasses.yaml`). If you use Helm for templating only, with no release state, this is actually *easier* — render CRDs as part of the manifest set and apply them with everything else — but it must be explicit.

*See also: [workload identity: how a pod gets a cloud credential](03-multicloud-aws-gcp-azure.md#workload-identity-how-a-pod-gets-a-cloud-credential) for the IRSA-versus-Pod-Identity trade behind that controller ServiceAccount annotation, and why a fleet of cells wants the latter.*

### Observability: metrics, events, logs

Metrics are Prometheus format on `:8080/metrics` (`METRICS_PORT`), default service address `karpenter.kube-system.svc.cluster.local:8080` ([Metrics](https://karpenter.sh/docs/reference/metrics/)). Karpenter labels each metric with a stability level (STABLE / BETA / ALPHA / DEPRECATED) — build dashboards on STABLE and BETA, and expect ALPHA names to move.

The dashboard I would build for a cell fleet:

**"Is Karpenter healthy at all?"**

- `karpenter_cluster_state_synced` — 1 if Karpenter's in-memory state matches the API server (STABLE)
- `karpenter_cluster_state_unsynced_time_seconds` — alert if this is ever non-trivially positive (STABLE)
- `karpenter_build_info` — version, for fleet-wide upgrade tracking (STABLE)
- `leader_election_master_status`, `controller_runtime_reconcile_errors_total`, `controller_runtime_reconcile_panics_total`

**"Why didn't it scale up?"**

- `karpenter_scheduler_unschedulable_pods_count` — pods Karpenter itself deems unschedulable (ALPHA)
- `karpenter_scheduler_ignored_pods_count` — pods it skipped entirely (ALPHA)
- `karpenter_scheduler_queue_depth`, `karpenter_scheduler_scheduling_duration_seconds` (STABLE)
- `karpenter_scheduler_pending_pods_by_effective_zone_count` — dimensioned by the *intersection* of pod zone signals, PVC zones, and topology constraints; the value `none` means "no valid zone intersection exists," which is the single most useful signal for a stuck StatefulSet (ALPHA)
- `karpenter_cloudprovider_instance_launch_failures_total` — by AZ, zone ID, capacity type, and **launch failure reason**; this is where `InsufficientInstanceCapacity` shows up (BETA)
- `karpenter_cloudprovider_instance_type_offering_available` (BETA)
- `karpenter_nodepools_limit` vs `karpenter_nodepools_usage` (ALPHA) — did you hit `spec.limits`?
- `karpenter_nodeclaims_created_total` is labeled with **whether minValues was relaxed** for that claim (STABLE)

**"Why did it kill my node?"**

- `karpenter_voluntary_disruption_decisions_total` — labeled by decision, **reason**, and consolidation type (STABLE). This is the first query to run in a postmortem.
- `karpenter_voluntary_disruption_eligible_nodes` by reason (BETA)
- `karpenter_nodepools_allowed_disruptions` and `karpenter_nodepools_nodes_consuming_budgets` — is the budget doing anything? (ALPHA)
- `karpenter_nodeclaims_disrupted_total` by reason and NodePool (ALPHA)
- `karpenter_interruption_received_messages_total` by message type and actionability (STABLE) — separates "spot took it" from "we consolidated it"
- `karpenter_nodeclaims_unhealthy_disrupted_total` — labeled by node condition and **image ID**, which is exactly what you want when a bad AMI is the cause (ALPHA)
- `karpenter_pods_drained_total` and `karpenter_pods_eviction_requests_total` **by response code** (ALPHA) — 429s here mean PDBs are blocking

**Status conditions.** NodePools carry `NodeClassReady`, `ValidationSucceeded`, `NodeRegistrationHealthy`, and a rolled-up `Ready`. `NodeRegistrationHealthy` is the one that catches the ugliest class of bug — *"Indicates whether a misconfiguration is preventing launched nodes from registering successfully and requires manual investigation"* — and it is **informational and does not affect the top-level `Ready`** ([NodePools — status.conditions](https://karpenter.sh/docs/concepts/nodepools/)). Alert on it separately; a NodePool that is `Ready=True` but `NodeRegistrationHealthy=False` is launching instances that never join the cluster, i.e. burning money silently. A NodePool that is not `Ready` is not considered for scheduling at all.

**Events.** `kubectl describe node` on a node you expected to be consolidated is the fastest debug loop:

```text
Events:
  Type     Reason             Age                From        Message
  Normal   Unconsolidatable   66s                karpenter   pdb default/inflate-pdb prevents pod evictions
  Normal   Unconsolidatable   33s (x3 over 30m)  karpenter   can't replace with a lower-priced node
```

`ConsolidationApproved` events (on Node/NodeClaim for single-node actions, on the NodePool for multi-node) carry the `Balanced` score and savings/disruption percentages.

**Logs.** `LOG_LEVEL` accepts `debug`, `info` (default), `error`. Balanced-consolidation scoring decisions are logged at `debug`, as are the "pod X has a preferred Anti-Affinity which can prevent consolidation" warnings. `DISABLE_CLUSTER_STATE_OBSERVABILITY` turns off cluster state metrics and events — useful at extreme scale, terrible for debugging.

**A concrete debug ladder for "why didn't it scale up":**

1. `kubectl get nodepool -o wide` — is `Ready` true? Is `NodeClassReady` true?
2. `kubectl get ec2nodeclass <name> -o jsonpath='{.status}'` — are `subnets`, `securityGroups`, `amis` all populated? Empty means a discovery tag is missing.
3. `kubectl describe pod <pending>` — does its requirement set actually intersect the NodePool's? A `nodeSelector` for an instance type outside the NodePool's list means Karpenter will never launch.
4. `karpenter_nodepools_usage` vs `karpenter_nodepools_limit`.
5. Controller logs — look for `InsufficientInstanceCapacity`, or the min-values relaxation message.
6. Remember offering-unavailability is **cached for 3 minutes** per instance-type/zone ([NodePools — capacity type](https://karpenter.sh/docs/concepts/nodepools/)), so a fix may take that long to take effect.

*See also: [what to actually monitor in a cell](14-observability-for-cells.md#what-to-actually-monitor-in-a-cell) for where these metrics belong in the cell's alert set, and why `karpenter_nodepools_usage` against `_limit` is a fleet metric rather than a dashboard panel.*

### Cost implications and instance-type selection strategy

Karpenter's cost model is: the cheaper the instance mix that satisfies the pods, the better. Levers, roughly in order of impact:

1. **Requirement breadth.** The single biggest lever. `requirements: []` gives Karpenter the whole catalogue; a five-instance-type allowlist gives it almost nothing. The docs' recommendation is to constrain *"only in ways that are absolutely necessary,"* with a suggested baseline of arch, os, capacity-type, `instance-category In [c, m, r]`, and `instance-generation Gte 3`. For Temporal cells I would add a generation floor much higher than 3 (Graviton-era instances are meaningfully cheaper per unit of throughput) and would allow both `amd64` and `arm64` only if the images are genuinely multi-arch.
2. **Capacity type mix.** `reserved` → `spot` → `on-demand` priority means adding `reserved` to a NodePool's allowed types transparently soaks up ODCR/capacity-block capacity you have already paid for before spending on-demand.
3. **Consolidation policy.** `WhenEmptyOrUnderutilized` saves the most and churns the most; `Balanced` is the right default for stateful cells; `WhenEmpty` is nearly free of churn and leaves real money on the table.
4. **`consolidateAfter`.** Short values chase every transient dip. For workloads with bursty pod turnover, a longer value is both cheaper (fewer wasted launches) and safer.
5. **Node overhead.** `VM_MEMORY_OVERHEAD_PERCENT` (0.075) and the `kubelet.systemReserved`/`kubeReserved` block on the NodeClass directly determine allocatable, and therefore how many nodes you buy.
6. **`karpenter_nodepools_cost_total`** exists (ALPHA) — *"Total cost of the nodepool from Karpenter's perspective. Units are determined by the cloud provider. Not an authoritative source for billing."* Good for relative comparison across cells; do not reconcile it against an invoice.

An honest caveat: Karpenter's pricing data comes from the AWS on-demand pricing endpoint plus spot price history. In an isolated VPC (`ISOLATED_VPC=true`), the on-demand pricing lookup is **disabled** ([Settings](https://karpenter.sh/docs/reference/settings/)), so cost-based consolidation degrades to whatever static data is compiled in. If your cells run in isolated VPCs — plausible for a security-conscious multi-tenant cloud — validate that consolidation is still making sensible choices.

---

## Hands-on

### Honest framing

**Karpenter cannot be fully exercised on `kind`.** Its whole job is calling a cloud provider's instance API. What you *can* do on kind is exercise the entire core: the scheduling simulator, NodePool/NodeClaim lifecycle, consolidation, drift, disruption budgets, and the termination flow — using the **KWOK provider**, which is a fake cloud provider that fabricates instance types and lets [KWOK](https://kwok.sigs.k8s.io/) simulate the nodes. That is genuinely most of the learning, and it is free.

For anything provider-specific — EC2NodeClass discovery, AMI drift, spot interruption via SQS, IAM — you need a real cloud. Lab 3 is costed.

### Lab 1 — Karpenter on kind with the KWOK provider (free)

This is the reference test harness the Karpenter maintainers themselves use.

```bash
# Prereqs: docker, kind, kubectl, go, make, helm
git clone https://github.com/kubernetes-sigs/karpenter.git
cd karpenter

kind create cluster --name karpenter-lab

# Build the KWOK provider image into the kind cluster directly.
export KWOK_REPO=kind.local
export KIND_CLUSTER_NAME=karpenter-lab

make install-kwok     # installs KWOK itself
make apply            # builds + deploys the KWOK-backed Karpenter; re-run to redeploy

# Push real workloads off the kind control-plane node so they land on kwok nodes.
kubectl taint nodes karpenter-lab-control-plane CriticalAddonsOnly=true:NoSchedule
```

Source: [`kwok/README.md`](https://github.com/kubernetes-sigs/karpenter/blob/main/kwok/README.md). Note the fake labels it introduces — `karpenter.kwok.sh/instance-type`, `instance-size`, `instance-family`, `instance-cpu`, and `karpenter.sh/instance-memory` — which the README warns *"will not work with a real Karpenter installation."*

Now create a NodePool against the KWOK NodeClass:

```yaml
apiVersion: karpenter.kwok.sh/v1alpha1
kind: KWOKNodeClass
metadata:
  name: default
spec: {}
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: default
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.kwok.sh
        kind: KWOKNodeClass
        name: default
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
      expireAfter: 1h
      terminationGracePeriod: 5m
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 30s
    budgets:
      - nodes: "1"
  limits:
    cpu: "200"
```

**Exercise 1 — provisioning and bin-packing.**

```bash
kubectl create deployment inflate --image=public.ecr.aws/eks-distro/kubernetes/pause:3.7 --replicas=0
kubectl set resources deployment inflate --requests=cpu=1,memory=1Gi
kubectl scale deployment inflate --replicas=12
kubectl get nodeclaims -w
```
Watch how *many* nodes appear and how large they are. Then set `BATCH_MAX_DURATION=1s` on the Karpenter deployment and repeat — you should get more, smaller nodes.

**Exercise 2 — consolidation.**

```bash
kubectl scale deployment inflate --replicas=2
kubectl get nodeclaims -w
# then, once it settles:
kubectl describe node <a node that survived> | sed -n '/Events/,$p'
```
Then flip `consolidationPolicy` to `WhenEmpty` and repeat. Then try `Balanced` and look for `ConsolidationApproved` events and the `karpenter_consolidation_score` metric.

**Exercise 3 — a PDB blocks consolidation.**

```bash
kubectl create poddisruptionbudget inflate-pdb --selector=app=inflate --max-unavailable=0
kubectl scale deployment inflate --replicas=2
kubectl describe node <underutilized node> | grep Unconsolidatable
```
You should see `pdb default/inflate-pdb prevents pod evictions`. Now set `max-unavailable=1` and watch it unblock.

**Exercise 4 — drift.** Edit `NodePool.spec.template.spec.requirements` to exclude an instance family currently in use. Every incompatible NodeClaim should get the `Drifted` status condition. Now set `budgets: [{nodes: "0", reasons: ["Drifted"]}]` and confirm the drift is detected but *not acted on* — this is the exact mechanism you'd use to freeze image rollouts during a change freeze.

```bash
kubectl get nodeclaims -o custom-columns=\
'NAME:.metadata.name,DRIFTED:.status.conditions[?(@.type=="Drifted")].status'
```

**Exercise 5 — `do-not-disrupt` and the expiry deadlock.** Annotate the deployment with `karpenter.sh/do-not-disrupt: "true"`, set `expireAfter: 2m`, and **remove** `terminationGracePeriod`. Watch a node get stuck mid-drain forever. This reproduces the documented failure mode in about three minutes, and it is the single most valuable thing on this list to have seen with your own eyes. Then add `terminationGracePeriod: 5m` and watch it resolve. Then change the annotation to `"30s"` and watch the duration form expire cleanly.

**Exercise 6 — budgets under load.** Set `budgets: [{nodes: "1"}]`, create 10 nodes, then drift all of them at once. Confirm the rollout is strictly serial, and watch `karpenter_nodepools_allowed_disruptions` and `karpenter_nodepools_nodes_consuming_budgets`.

Teardown:

```bash
make delete
make uninstall-kwok
kind delete cluster --name karpenter-lab
```

### Lab 2 — the AWS KWOK provider (free, more realistic)

[`aws/karpenter-provider-aws/kwok`](https://github.com/aws/karpenter-provider-aws/tree/main/kwok) is a *separate* harness that fakes the EC2 API while running the real AWS controllers. Its make targets live in `kwok/Makefile`, not the repo root. This is the right place to exercise EC2NodeClass-shaped behavior without spending money, and it is what AWS uses for scale testing.

### Lab 3 — a real EKS cluster (costed)

Do this once, deliberately, and tear it down the same day.

**Rough cost, `us-west-2`, ~3 hours:**

| Item | Rate | 3h |
|---|---|---|
| EKS control plane | $0.10/hr | $0.30 |
| 2 × `m5.large` bootstrap node group (on-demand) | ~$0.096/hr each | ~$0.58 |
| Karpenter-provisioned nodes (say 3 × `c6g.xlarge` on-demand, intermittent) | ~$0.136/hr each | ~$1.20 |
| NAT gateway (1) | $0.045/hr + data | ~$0.15 |
| EBS gp3 (5 × 100 GiB, partial hour) | $0.08/GiB-month | ~$0.16 |
| SQS interruption queue | effectively free at this volume | ~$0.00 |
| **Total** | | **~$2.50** |

Prices are indicative; check the [AWS pricing pages](https://aws.amazon.com/ec2/pricing/on-demand/) for current rates and your region. The dominant risk is not the hourly rate, it is *forgetting to delete*. Set a budget alarm before you start.

Follow [Getting Started with Karpenter](https://karpenter.sh/docs/getting-started/getting-started-with-karpenter/), which uses `eksctl` and a CloudFormation template that creates the interruption queue and EventBridge rules for you. Then run the exercises that only work for real:

1. **AMI drift.** Set `amiSelectorTerms: [{alias: al2023@latest}]`, then pin it to a specific version, then unpin it. Confirm that unpinning drifts every node with no other change.
2. **Spot interruption.** Create a spot NodePool, then use [AWS FIS](https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html) `aws:ec2:send-spot-instance-interruptions` to inject a real interruption. Watch `karpenter_interruption_received_messages_total` increment and a replacement pre-spin.
3. **Spot diversification.** Create a NodePool with `instance-type In [<one type>]` and spot capacity, and watch launch failures under `karpenter_cloudprovider_instance_launch_failures_total`. Then add `minValues: 10` on `instance-type` and see the difference.
4. **Delete the whole thing.** `kubectl delete nodepool --all` and watch the cascading NodeClaim → Node → instance teardown via owner references. This is your cell-teardown dry run, and it is worth timing.

```bash
eksctl delete cluster --name <name>
# then verify no orphaned instances remain:
aws ec2 describe-instances \
  --filters "Name=tag-key,Values=karpenter.sh/nodepool" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId'
```

---

## Production gotchas

1. **Every internet example you find is for a dead API.** `Provisioner`, `AWSNodeTemplate`, `Machine`, `apiVersion: karpenter.sh/v1beta1`, and `kubelet:` under the NodePool are all removed. Karpenter 1.1.0 dropped v1beta1 support outright ([v1 Migration](https://karpenter.sh/v1.0/upgrading/v1-migration/)). Before trusting any snippet, check for `apiVersion: karpenter.sh/v1` and `nodeClassRef.group`. Pin your docs reading to [karpenter.sh/docs](https://karpenter.sh/docs/) (currently v1.14), not a version-less Google result.

2. **The default disruption config is aggressive, and silence means you accepted it.** Omitting `spec.disruption` gives you `consolidationPolicy: WhenEmptyOrUnderutilized` and `consolidateAfter: 0s` ([Disruption — defaults](https://karpenter.sh/docs/concepts/disruption/)). Omitting `budgets` gives you a single `nodes: 10%`. On a 10-node cell that is one node at a time, which is probably fine; on a 200-node cell it is 20 nodes churning simultaneously. Always write budgets explicitly.

3. **Disruption budgets do not stop expiration, interruption, or node repair.** The docs state this in two separate places. A team that sets `budgets: [{nodes: "0"}]` believing they have frozen all node churn, and then hits `expireAfter: 720h` on a fleet built the same week, will get a synchronized fleet-wide expiry. Stagger `expireAfter` across cells, or set it to `Never` and roll exclusively via drift, which *is* budgeted.

4. **A missing `startupTaint` causes runaway provisioning.** From the NodePool docs: *"Failure to provide accurate `startupTaints` can result in Karpenter continually provisioning new nodes. When the new node joins and the startup taint that Karpenter is unaware of is added, Karpenter now considers the pending pod to be unschedulable to this node. Karpenter will attempt to provision yet another new node."* Any CNI, security agent, or node-init DaemonSet that taints on boot — Cilium's `node.cilium.io/agent-not-ready` is the canonical case — must be declared in `startupTaints`. This bug looks like a cost incident, not a config error.

5. **`alias: al2023@latest` will roll your entire fleet without you touching anything.** Drift on `amiSelectorTerms` is *resolved*, not hashed: a new AMI publication drifts every node ([Disruption — special cases on drift](https://karpenter.sh/docs/concepts/disruption/)), and `AMI_REFRESH_INTERVAL` defaults to **1 minute**. Pin the alias to a version and bump it as a deliberate, budgeted change. This is the correct primitive for cell node-image upgrades — but only if you drive it.

6. **You cannot turn drift off any more.** The `Drift` feature gate was removed when drift went stable in v1 ([Settings — feature gates](https://karpenter.sh/docs/reference/settings/)). The only control is a disruption budget with `reasons: [Drifted]`. Write that budget before you write your first NodeClass.

7. **PDBs compose by AND, and one blocking PDB immortalizes a whole node.** *"If a pod matches multiple PDBs (via label selectors), ALL of these PDBs must allow for disruption... A single blocking PDB can prevent the entire node from being voluntary disrupted"* ([Disruption — pod-level controls](https://karpenter.sh/docs/concepts/disruption/)). A team shipping `maxUnavailable: 0` "temporarily" can silently pin nodes for months. Audit for `maxUnavailable: 0` and `minAvailable: 100%` across the fleet; they are almost always mistakes.

8. **`expireAfter` + `do-not-disrupt` without `terminationGracePeriod` is a documented deadlock.** Expiration begins a forceful drain; `do-not-disrupt` blocks eviction; nothing resolves it. The docs say the result is *"partially drained nodes stuck in the cluster, driving up cluster cost and potentially requiring manual intervention."* If you allow the annotation anywhere in your platform, mandate `terminationGracePeriod` on every NodePool. Enforce it with policy (see the [Kyverno guide](07-kyverno.md#a-starter-policy-set-a-platform-team-would-actually-ship), which ships this exact rule).

9. **`spec.limits` is eventually consistent and can be overrun.** *"Karpenter provisioning is highly parallel. Because of this, limit checking is eventually consistent, which can result in overrun during rapid scale outs"* ([NodePools — spec.limits](https://karpenter.sh/docs/concepts/nodepools/)). Do not treat limits as a hard safety boundary against a runaway workload; treat them as a guardrail and back them with cloud-account quotas. Also: use string quantities (`cpu: "1000"`) — the docs warn the API coerces CPU to a string and integers cause GitOps diff churn.

10. **The 100-requirement ceiling counts your labels.** `spec.template.metadata.labels` are propagated as NodeClaim requirements, so labels plus requirements share one budget of 100 ([NodePools](https://karpenter.sh/docs/concepts/nodepools/)). A platform that stamps 30 cell-metadata labels onto every node has just spent a third of the budget.

11. **`NodeRegistrationHealthy: False` does not make a NodePool `NotReady`.** It is explicitly informational ([NodePools — status.conditions](https://karpenter.sh/docs/concepts/nodepools/)). A NodePool that launches instances which never register looks green in a naive dashboard while burning money. Alert on this condition directly.

12. **Preferred affinities and `ScheduleAnyway` spreads quietly suppress consolidation.** Karpenter honors preferences at launch *and* at consolidation, so a soft anti-affinity can make a node permanently unconsolidatable ([Disruption](https://karpenter.sh/docs/concepts/disruption/)). Grep the logs for `has a preferred Anti-Affinity which can prevent consolidation`. `PREFERENCE_POLICY=Ignore` is the blunt fix.

13. **Spot-to-spot *replacement* consolidation is off by default and needs 15 instance types.** `SpotToSpotConsolidation` is alpha and disabled ([Settings](https://karpenter.sh/docs/reference/settings/)); enabling it imposes a minimum instance-type flexibility of 15 on single-node consolidations ([Disruption — spot consolidation](https://karpenter.sh/docs/concepts/disruption/)). A narrow spot NodePool will simply never spot-to-spot consolidate, and you will conclude the feature is broken when it is working exactly as designed.

14. **Karpenter cannot run on a node it manages.** Pin it with `karpenter.sh/nodepool DoesNotExist` node affinity plus hostname anti-affinity, on a bootstrap node group or Fargate ([Migrating from CAS](https://karpenter.sh/docs/getting-started/migrating-from-cas/)). That bootstrap group becomes a second, un-Karpenter-managed lifecycle you must upgrade per cell. Design for it explicitly; do not discover it during a cell upgrade.

15. **CRDs are not managed by the Helm chart upgrade path.** The documented flow applies the three CRD YAMLs separately from raw GitHub URLs. With Helm-for-templating-only this is straightforward, but it must be a deliberate step in the cell pipeline — a Karpenter binary running against stale CRDs fails in confusing, partially-functional ways.

16. **`ISOLATED_VPC=true` disables on-demand pricing lookups.** Cost-based consolidation then reasons over compiled-in data ([Settings](https://karpenter.sh/docs/reference/settings/)). If cells run in isolated VPCs, either accept degraded cost decisions or budget for a pricing VPC endpoint.

17. **Snapshot releases are deleted after 90 days.** If anyone pins a cell to a snapshot tag to get an unreleased fix, that image will vanish and the cell will fail to pull on the next node launch ([Compatibility — snapshot releases](https://karpenter.sh/docs/upgrading/compatibility/)). Only stable tags belong in a cell manifest.

18. **The Azure NodeClass is `v1beta1` while the AWS one is `v1`.** Any code, CRD-validation, or templating layer that assumes a single NodeClass API version across clouds is wrong ([AKSNodeClass](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning-aksnodeclass)). Encode the version per provider.

19. **There is no supported GCP path.** The only GCP provider is third-party, at `v1alpha1`, and requires an existing GKE Standard node pool to read bootstrap metadata. Do not put "Karpenter everywhere" in a design doc without stating this. GKE's ComputeClasses are the native answer.

20. **Capacity unavailability is cached for 3 minutes.** When a provider API reports no capacity for an instance-type/zone, Karpenter caches that across all provisioning attempts for 3 minutes ([NodePools — capacity type](https://karpenter.sh/docs/concepts/nodepools/)). During a capacity crunch this makes recovery look slower than it is; do not chase a phantom bug for the first 3 minutes of an incident.

---

## How this shows up in cell lifecycle

**Cell provisioning.** Karpenter is part of the cell's bootstrap graph, and its ordering constraints are real: cluster exists → bootstrap node group / Fargate profile exists → IAM roles and OIDC trust exist → subnets and security groups carry `karpenter.sh/discovery` tags → CRDs applied → Karpenter deployment healthy → NodeClass `status` resolved → NodePool `Ready` → workload NodePools created. A cell provisioning workflow (which, at Temporal, is plausibly itself a Temporal workflow) should have an activity that polls `NodePool.status.conditions[Ready]` and `NodeClassReady` rather than sleeping. The discovery-tag step is the one most likely to be forgotten, and it fails silently as an EC2NodeClass with an empty `status.subnets`.

**Cell upgrade — the two axes.** Kubernetes control-plane upgrades and node-image upgrades are separate problems with separate mechanisms:

- *Control plane*: check the [compatibility matrix](https://karpenter.sh/docs/upgrading/compatibility/) first. Moving a cell to Kubernetes 1.36 requires Karpenter ≥ 1.13. Karpenter itself upgrades before the control plane.
- *Nodes*: bump `amiSelectorTerms` (or `imageFamily`/image version on Azure), which drifts every NodeClaim, and let a `reasons: [Drifted]` budget pace the rollout. This is genuinely elegant — a declarative image bump plus a rate limit is the whole node upgrade — but it is *only* safe if the budget is written, the AMI is pinned rather than `@latest`, and `terminationGracePeriod` is set so a stuck pod cannot wedge the rollout.

For a fleet, the right shape is: budget of `nodes: "1"` for `Drifted` on stateful pools, a wider budget on stateless pools, and a cell-level workflow that bumps the pin, watches `karpenter_nodeclaims_disrupted_total{reason="drifted"}` climb, and gates the next cell on the previous one finishing clean. A drift rollout with no observer is a fleet-wide change with no rollback.

**Cell teardown.** The finalizer + owner-reference chain is your friend: deleting a NodePool cascades to NodeClaims, which cascade to Nodes and instances ([Disruption — manual methods](https://karpenter.sh/docs/concepts/disruption/)). But teardown is exactly when things wedge: a `do-not-disrupt` pod, a `maxUnavailable: 0` PDB, or a stuck VolumeAttachment will hold the finalizer indefinitely. `terminationGracePeriod` is the bound that makes teardown *terminate*, which is why I'd argue it should be mandatory on every NodePool a cell platform ships, not optional. Teardown verification should independently query the cloud for instances tagged `karpenter.sh/nodepool` belonging to the cell — trusting Kubernetes to have cleaned up is how you accumulate orphaned instances across hundreds of cells.

**Capacity and cost per cell.** `NodePool.spec.limits` is your per-cell blast radius against a runaway tenant, backed by account-level quotas because limits are eventually consistent. `karpenter_nodepools_cost_total` and `karpenter_cluster_utilization_percent` give you a per-cell efficiency signal that is comparable across the fleet even if it is not billing-accurate.

**Networking, when another team owns it.** Karpenter's node-side surface touches theirs in three specific places worth naming in a design review: `subnetSelectorTerms` and `securityGroupSelectorTerms` determine which subnets and SGs new nodes land in, so a subnet-tagging change is a networking change; `startupTaints` must match whatever CNI the networking team runs, or provisioning runs away; and `EC2NodeClass.spec.connectionTracking` (TCP/UDP idle timeouts) and `networkInterfaces` are node-level networking knobs that live in a CRD you own but have networking-team consequences. `RESERVED_ENIS` matters if VPC CNI custom networking is in use, because it changes max-pods math.

**Helm-for-templating-only.** Karpenter's chart is straightforward to render with `helm template` and apply — the migration guide literally does this (`helm template ... > karpenter.yaml`). The only thing to handle deliberately is CRDs, which are applied separately. This suits your model well.

---

## Learning path

**Day 1 — get the model right, and unlearn the internet.**

- Read [Concepts](https://karpenter.sh/docs/concepts/) end to end: NodePools, NodeClasses, NodeClaims, Scheduling, Disruption. Ninety minutes, and it replaces a week of blog posts.
- Read [Disruption](https://karpenter.sh/docs/concepts/disruption/) a **second** time, specifically the graceful/forceful split and the budget semantics. This is the part that causes incidents.
- Skim [Settings](https://karpenter.sh/docs/reference/settings/) for the feature-gate table so you know what is alpha in your version.
- Run Lab 1 exercises 1–3. Getting a NodeClaim to appear on a kind cluster in under an hour is very good for morale.

**Week 1 — build the muscle memory for disruption.**

- Lab 1 exercises 4–6: drift, the `do-not-disrupt` deadlock, and serial budget rollouts. Exercise 5 is the highest-value hour on this list.
- Read the [v1 Migration guide](https://karpenter.sh/v1.0/upgrading/v1-migration/) even though you will never do that migration, because it enumerates every field that moved and inoculates you against stale YAML.
- Read the controller IAM policy in [Migrating from Cluster Autoscaler](https://karpenter.sh/docs/getting-started/migrating-from-cas/) line by line and be able to explain why `ec2:TerminateInstances` is tag-scoped but `ec2:RunInstances` is not.
- Skim the [Metrics reference](https://karpenter.sh/docs/reference/metrics/) and draft the three dashboards above. Write the PromQL, even without data.
- Run Lab 3 once. Spend the $2.50. Inject a spot interruption with FIS.
- Read Temporal's own [cell architecture writeup](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) and map "cell" onto "cluster + NodePools" concretely.

**Month 1 — operate it like you own it.**

- Read [`kubernetes-sigs/karpenter/pkg/cloudprovider/types.go`](https://github.com/kubernetes-sigs/karpenter/blob/main/pkg/cloudprovider/types.go) and the disruption controller source. The nine-method interface makes the whole multi-cloud story legible, and it tells you exactly what a provider can and cannot customize.
- Read the [v1 API design doc](https://github.com/kubernetes-sigs/karpenter/blob/main/designs/v1-api.md) and the [drift design](https://github.com/aws/karpenter-core/blob/main/designs/drift.md) for the reasoning behind the current shape.
- Stand up the [`Azure/karpenter-provider-azure`](https://github.com/Azure/karpenter-provider-azure) provider on an AKS cluster via NAP and diff `AKSNodeClass` against `EC2NodeClass` field by field. Write down what your cell abstraction must branch on.
- Write the team's NodePool standard: mandatory `terminationGracePeriod`, pinned AMI aliases, explicit budgets per reason, `expireAfter` staggered across cells, and a `NodeRegistrationHealthy` alert. Then enforce it with admission policy.
- Build the "why did Karpenter kill this node" runbook from `karpenter_voluntary_disruption_decisions_total` + node events + interruption metrics, and test it against a real consolidation.
- Read the [Threat Model](https://karpenter.sh/docs/reference/threat-model/) before any conversation about running Karpenter in a multi-tenant account.

---

## References

1. [Karpenter documentation (v1.14, current)](https://karpenter.sh/docs/) — the only documentation you should trust; note the version selector in the header.
2. [Karpenter — Disruption](https://karpenter.sh/docs/concepts/disruption/) — control flow, consolidation policies, drift, expiration, interruption, node repair, budgets, `do-not-disrupt`. The single most important page.
3. [Karpenter — NodePools](https://karpenter.sh/docs/concepts/nodepools/) — full annotated NodePool spec, well-known labels, `minValues`, weights, limits, static NodePools, status conditions.
4. [Karpenter — NodeClasses](https://karpenter.sh/docs/concepts/nodeclasses/) — full `EC2NodeClass` v1 spec including `kubelet`, selector terms, block devices, IMDS, capacity reservations.
5. [Karpenter — NodeClaims](https://karpenter.sh/docs/concepts/nodeclaims/) — the object Karpenter creates for each machine.
6. [Karpenter — Scheduling](https://karpenter.sh/docs/concepts/scheduling/) — which pod scheduling constructs Karpenter simulates, topology spread, PV topology, weighted NodePools.
7. [Karpenter — Settings reference](https://karpenter.sh/docs/reference/settings/) — every env var and CLI flag, plus the feature-gate table with stages and versions.
8. [Karpenter — Metrics reference](https://karpenter.sh/docs/reference/metrics/) — every metric with its stability level; the source for the dashboards above.
9. [Karpenter — Compatibility](https://karpenter.sh/docs/upgrading/compatibility/) — Kubernetes/Karpenter version matrix, breaking-change policy, release channels.
10. [Karpenter — Upgrade Guide](https://karpenter.sh/docs/upgrading/upgrade-guide/) — per-version upgrade notes.
11. [Karpenter — v1 Migration guide](https://karpenter.sh/v1.0/upgrading/v1-migration/) — what moved between v1beta1 and v1; read it as a stale-YAML detector.
12. [Karpenter — v1beta1 Migration guide](https://karpenter.sh/v1.0/upgrading/v1beta1-migration/) — the earlier alpha→beta rename (`Provisioner`→`NodePool`, etc.).
13. [Karpenter — Migrating from Cluster Autoscaler](https://karpenter.sh/docs/getting-started/migrating-from-cas/) — the complete IAM policy, Helm install, node-affinity pinning, and CAS decommission steps.
14. [Karpenter — Getting Started](https://karpenter.sh/docs/getting-started/getting-started-with-karpenter/) — eksctl + CloudFormation path that creates the interruption queue.
15. [Karpenter — Troubleshooting](https://karpenter.sh/docs/troubleshooting/) — the symptom-indexed debugging page; bookmark it.
16. [Karpenter — Threat Model](https://karpenter.sh/docs/reference/threat-model/) — required reading before running Karpenter in a shared account.
17. [Karpenter — Instance Types reference](https://karpenter.sh/docs/reference/instance-types/) — the AWS catalogue Karpenter reasons over, with resources per type.
18. [Karpenter — Managing AMIs](https://karpenter.sh/docs/tasks/managing-amis/) — pinning strategy and the mechanics of image-driven drift.
19. [kubernetes-sigs/karpenter](https://github.com/kubernetes-sigs/karpenter) — the provider-agnostic core library; the README lists every known cloud provider implementation.
20. [kubernetes-sigs/karpenter — `pkg/cloudprovider/types.go`](https://github.com/kubernetes-sigs/karpenter/blob/main/pkg/cloudprovider/types.go) — the nine-method `CloudProvider` interface every provider implements.
21. [kubernetes-sigs/karpenter — v1 API design](https://github.com/kubernetes-sigs/karpenter/blob/main/designs/v1-api.md) — the reasoning behind the v1 shape.
22. [kubernetes-sigs/karpenter — KWOK provider README](https://github.com/kubernetes-sigs/karpenter/blob/main/kwok/README.md) — the exact kind-cluster commands for Lab 1.
23. [kubernetes-sigs/karpenter — release v1.14.1](https://github.com/kubernetes-sigs/karpenter/releases/tag/v1.14.1) — current core release, 2026-08-21.
24. [aws/karpenter-provider-aws](https://github.com/aws/karpenter-provider-aws) — the AWS provider; also hosts the karpenter.sh website source.
25. [aws/karpenter-provider-aws — release v1.14.1](https://github.com/aws/karpenter-provider-aws/releases/tag/v1.14.1) — current AWS release, tagged LTS through July 2027.
26. [aws/karpenter-provider-aws — v1beta1 API design](https://github.com/aws/karpenter-provider-aws/blob/main/designs/v1beta1-api.md) — where the `Provisioner`→`NodePool` renames are enumerated.
27. [aws/karpenter-provider-aws — AWS KWOK provider](https://github.com/aws/karpenter-provider-aws/tree/main/kwok) — fake-EC2 harness for Lab 2; make targets live in `kwok/Makefile`.
28. [aws/karpenter-provider-aws — v1 examples](https://github.com/aws/karpenter-provider-aws/tree/main/examples/v1) — canonical, current NodePool and EC2NodeClass YAML.
29. [AWS — price-capacity-optimized allocation strategy](https://aws.amazon.com/blogs/compute/introducing-price-capacity-optimized-allocation-strategy-for-ec2-spot-instances/) — the spot strategy Karpenter uses; explains the price/interruption trade.
30. [AWS EKS Best Practices — Karpenter](https://aws.github.io/aws-eks-best-practices/karpenter/) — AWS's own opinionated guidance; secondary but high quality.
31. [AWS FIS actions reference](https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html) — `aws:ec2:send-spot-instance-interruptions` for injecting real spot interruptions in Lab 3.
32. [Azure/karpenter-provider-azure](https://github.com/Azure/karpenter-provider-azure) — the AKS provider; README documents NAP vs self-hosted and the AKS-only constraint.
33. [Microsoft Learn — AKS Node Auto Provisioning](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning) — NAP as managed Karpenter; enablement via `--node-provisioning-mode Auto`.
34. [Microsoft Learn — Configure AKSNodeClass](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning-aksnodeclass) — the `karpenter.azure.com/v1beta1` NodeClass fields and defaults.
35. [cloudpilot-ai/karpenter-provider-gcp](https://github.com/cloudpilot-ai/karpenter-provider-gcp) — the only GCP provider; third-party, `GCENodeClass` at `v1alpha1`, GKE Standard only.
36. [Google Cloud — Custom compute classes](https://cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes) — GKE's native Karpenter-NodePool analogue, with a priority hierarchy and fallback.
37. [Google Cloud — Node auto-provisioning](https://cloud.google.com/kubernetes-engine/docs/concepts/node-auto-provisioning) — GKE's node-pool-creating extension of Cluster Autoscaler.
38. [kubernetes/autoscaler — Cluster Autoscaler FAQ](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md) — the authoritative source for CA's node-group model, scale-down thresholds, and eviction annotations.
39. [kubernetes-sigs/karpenter-provider-cluster-api](https://github.com/kubernetes-sigs/karpenter-provider-cluster-api) — experimental CAPI-backed provider; useful design reference, not a production path.
40. [KWOK — Kubernetes WithOut Kubelet](https://kwok.sigs.k8s.io/) — the node simulator underneath Lab 1; also useful on its own for scheduler testing.
