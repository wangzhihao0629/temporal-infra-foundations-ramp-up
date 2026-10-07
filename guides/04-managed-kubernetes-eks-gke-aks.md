# Managed Kubernetes: EKS vs GKE vs AKS — Flavors, Intricacies, and Cluster Lifecycle

**Why this matters.** Cell lifecycle for Temporal Cloud means provisioning, upgrading, and tearing down isolated Kubernetes-based capacity units across AWS, GCP, and Azure. Temporal's own engineering writing describes Temporal Cloud as a cell-based architecture where each cell is a self-contained unit with its own account, VPC, and Kubernetes cluster, and says the same principles were carried to GCP using equivalent primitives ([Temporal blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). That means the unit of work you ship is *a cluster*, repeated hundreds of times, on three providers whose version policies, upgrade semantics, node abstractions, identity models, and teardown failure modes are all different. Almost none of that difference is Kubernetes — it is the seam between Kubernetes and the cloud. This guide is about that seam.

Everything below was verified against primary docs on **2026-08-29**. This is the fastest-churning area in cloud infrastructure; every version window and default here has a source link so you can re-verify. Where I could not verify something, I say so explicitly.

---

## The mental model

Hold four ideas.

**1. Managed Kubernetes is a contract about who holds the pager for `etcd`, not a product.** All three clouds run apiserver, etcd, scheduler, and controller-manager for you and hide the machines. What differs is the *edge* of that contract: which flags you can set, whether you can read etcd, what happens when the provider decides your version is too old, and what the provider does to your nodes without asking. Everything painful in fleet operations lives at that edge.

**2. The version treadmill is the product.** Kubernetes ships roughly three minor releases a year ([Kubernetes releases](https://kubernetes.io/releases/)), each supported for about a year upstream. Every managed offering wraps that with its own support window, its own escape hatch (paid extended support), and its own forcing function. If you own N clusters on three clouds, you are running a continuous, never-finished upgrade pipeline. Design for that, not for "we'll upgrade in Q3."

**3. A cell is cattle, and the hard part is not `create`, it is `converge` and `delete`.** Creating a cluster is a single API call plus twenty minutes. Making a cluster *identical* to 300 siblings, and deleting one without leaving orphaned load balancers, disks, ENIs, DNS records, and IAM roles behind, is the actual engineering. Finalizers and cloud-controller-created resources are where teardown goes wrong.

**4. Three clouds means three failure vocabularies, one abstraction.** Your job is to build one cell lifecycle API whose implementation branches only where the clouds genuinely differ, and to *know* exactly where those branches are. The list of genuine differences is shorter than it looks — version policy, node abstraction, IP model, identity federation, and teardown ordering. Everything else can be one code path.

A useful frame for the whole guide:

```text
                 things all three clouds do the same
  ┌───────────────────────────────────────────────────────────┐
  │ Kubernetes API, RBAC, PDBs, drain/eviction, StatefulSets, │
  │ CSI, CRDs, controllers, kubelet, containerd               │
  └───────────────────────────────────────────────────────────┘
                 things that MUST branch per cloud
  ┌───────────────────────────────────────────────────────────┐
  │ version + support window       auto-upgrade semantics     │
  │ node abstraction + OS image    pod IP model               │
  │ workload identity federation   human/CI authn             │
  │ default StorageClass + AZ      add-on lifecycle           │
  │ quota model                    teardown ordering          │
  └───────────────────────────────────────────────────────────┘
```

---

## Core concepts

### The control plane, from an operator's point of view

You know what these components do. What matters when you *operate* clusters is what each one costs you when it degrades, and who can touch it.

- **kube-apiserver** — the only component anything else talks to. It is stateless and horizontally scalable, and it is the first thing that browns out under fleet-wide load (a badly written controller doing `LIST` on every pod every 5 seconds will take it down). All three clouds scale it for you, and none let you set arbitrary flags.
- **etcd** — the consistent store. It is the thing that actually cannot be lost, and the thing you cannot see on any managed offering. Upstream guidance sets a default 8 GiB storage limit, and EKS documents that limit explicitly and tells you to watch `etcd_db_total_size_in_bytes` ([EKS known limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html), [etcd limits](https://etcd.io/docs/v3.5/dev-guide/limit/)). GKE surfaces it as a first-class quota of 6 GB ([GKE quotas](https://docs.cloud.google.com/kubernetes-engine/quotas)).
- **kube-scheduler** and **kube-controller-manager** — leader-elected, managed, invisible. You mostly interact with them through symptoms: pods pending forever (scheduler, or more likely quota/taints), or PVs not binding (controller-manager plus CSI).
- **cloud-controller-manager** — the component that turns `Service type=LoadBalancer` into a real load balancer and node objects into real VMs. This one matters enormously for teardown, because it is the thing that creates cloud resources on your behalf and must be alive to delete them.
- **kubelet** — on your nodes, running your version. The version skew policy allows kubelet to be up to **three** minor versions older than the apiserver from Kubernetes 1.25 onward (two before that), and kubelet must never be *newer* than the apiserver ([version skew policy](https://kubernetes.io/releases/version-skew-policy/)). This single rule is why every managed upgrade is control-plane-first, and why you can defer node upgrades for a while — but not forever.

#### What "managed" means, component by component

| Component | EKS | GKE | AKS |
|---|---|---|---|
| apiserver hosting | AWS-owned account, exposed via ENIs in your VPC | Google-owned project, peered/PSC to your VPC | Microsoft-owned; optionally projected into a delegated subnet of your VNet via [API Server VNet Integration](https://learn.microsoft.com/en-us/azure/aks/api-server-vnet-integration) |
| apiserver flags | Not settable. Narrow allowlist of features via cluster config (authn mode, logging, encryption) | Not settable. Feature toggles via cluster config | Not settable. Feature toggles via cluster config |
| Admission plugins | Fixed set; you add webhooks | Fixed set; you add webhooks (plus GKE-forced ones on Autopilot) | Fixed set; you add webhooks |
| etcd access | None. Backups are AWS's problem, not yours; no restore-to-point-in-time knob exposed | None. 6 GB quota is visible ([GKE quotas](https://docs.cloud.google.com/kubernetes-engine/quotas)) | None. Control-plane scaling differs by tier ([AKS tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)) |
| etcd size limit | 8 GiB (upstream default), documented by AWS ([EKS limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html)) | 6 GB quota ([GKE quotas](https://docs.cloud.google.com/kubernetes-engine/quotas)) | Not published as a per-cluster number; tier-dependent ([AKS tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)) |
| Control-plane SLA | Paid tier only concept does not apply; single tier | Regional clusters carry a higher SLA than zonal ([GKE SLA](https://cloud.google.com/kubernetes-engine/sla)) | Free tier has **no** financially backed SLA; Standard/Premium do ([AKS tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)) |
| Control-plane cost | ~$0.10/cluster/hour standard, **$0.60/cluster/hour in extended support** ([AWS pricing blog](https://aws.amazon.com/blogs/containers/amazon-eks-extended-support-for-kubernetes-versions-pricing/)) | Cluster management fee per cluster; see [GKE pricing](https://cloud.google.com/kubernetes-engine/pricing) (I did not re-verify the exact rate in this pass) | Free $0, Standard ~$0.10/cluster/hour, Premium ~$0.60/cluster/hour ([AKS tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)) |

The operational punchline: **you cannot debug etcd on any of them.** Your only levers are (a) keep object count and object size down, (b) watch the size metric where exposed, and (c) treat "cluster is full" as a cell-splitting signal rather than a tuning problem. For a cell architecture that is actually good news — it pushes you toward more, smaller cells, which is what you want anyway.

---

### Version and support windows: the big comparison

This is the table to internalize. Dates verified 2026-08-29.

| Dimension | EKS | GKE | AKS |
|---|---|---|---|
| Standard support per minor | **14 months** from EKS release ([EKS extended support docs](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html)) | ~**14 months** from Regular-channel availability ([GKE versioning](https://docs.cloud.google.com/kubernetes-engine/versioning)) | **12 months** from AKS GA; N-2 GA minors supported ([AKS supported versions](https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions)) |
| Extended support | **+12 months** (26 total) ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html)) | **+~10 months** via the **Extended channel** (~24 total) ([GKE versioning](https://docs.cloud.google.com/kubernetes-engine/versioning)) | **LTS**: +12 months (~24 total) ([AKS LTS](https://learn.microsoft.com/en-us/azure/aks/long-term-support)) |
| Extended support cost | $0.60/cluster/hr vs $0.10 — **6x** ([AWS blog](https://aws.amazon.com/blogs/containers/amazon-eks-extended-support-for-kubernetes-versions-pricing/)) | Channel change, no separate per-cluster surcharge documented on the versioning page | Requires **Premium tier** (~$0.60/cluster/hr), billed only once the minor exits community support ([AKS LTS](https://learn.microsoft.com/en-us/azure/aks/long-term-support)) |
| Extended support opt-in | Cluster `upgradePolicy` field: `STANDARD` or `EXTENDED` ([UpgradePolicy](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-eks-cluster-upgradepolicy.html)) | Enroll cluster in **Extended** release channel | Set cluster tier to Premium **and** select the LTS support plan |
| Release channels | None. You pick an exact minor | **Rapid / Regular / Stable / Extended** ([release channels](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/release-channels)) | Auto-upgrade channels: `patch`, `stable`, `rapid`, `node-image`, `none` ([AKS auto-upgrade](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster)) |
| Minor auto-upgrade by default | **No** for in-support versions. **Yes** as a forced upgrade after extended support ends | **Yes** — every channel auto-upgrades minors except Extended, which auto-upgrades patches only ([release channels](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/release-channels)) | Only if you set an auto-upgrade channel; `none` is a valid (and dangerous) choice |
| Patch auto-upgrade | Control-plane patches applied by AWS | Yes, per channel | Only with `patch`/`stable`/`rapid` channel |
| Node OS image auto-update | No, you drive AMI updates | Yes, node auto-upgrade tied to channel | Separate `NodeImage`/`SecurityPatch` channel ([node OS auto-upgrade](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-node-os-image)) |
| Maintenance windows | Not a first-class concept for version upgrades | **Maintenance windows + exclusions**, first-class ([windows and exclusions](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/maintenance-windows-and-exclusions)) | **Planned Maintenance**: `aksManagedAutoUpgradeSchedule` and `aksManagedNodeOSUpgradeSchedule`; 4h+ windows recommended ([auto-upgrade](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster)) |
| What happens at true EOL | AWS force-upgrades the **control plane** (not nodes) to the oldest supported version, with **no advance notification** ([EKS extended support](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html)) | GKE auto-upgrades the cluster to keep it operable ([release schedule footnote 3](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule)) | Cluster becomes unsupported; upgrades are your responsibility |
| Skip minors? | No. One minor at a time | No. One minor at a time | No. One minor at a time |

#### Where each cloud actually is, today (2026-08-29)

| | EKS | GKE | AKS |
|---|---|---|---|
| Minors on standard support | **1.34, 1.35, 1.36** ([EKS standard support notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)) | 1.32 (EOS 2026-04-27, now extended-only), **1.33** (EOS 2026-08-12), **1.34**, **1.35**, **1.36** ([GKE release schedule](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule)) | **1.33** (EOL ~July 2026, LTS-eligible), **1.34**, **1.35**; 1.36 rolling out — verify on the [supported versions page](https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions) |
| Newest minor generally available | 1.36 | 1.36 (Rapid 2026-04-28, Regular 2026-06-09) | Verify; AKS trails upstream by ~2 months at GA |
| Nearest cliff you should care about | EKS 1.33 standard support ended 2026-07-29 | GKE 1.33 end of standard support **2026-08-12** (17 days ago) | AKS 1.33 community support ended ~July 2026 |

Two things fall out of that table immediately.

**GKE publishes exact future dates and you should build your roadmap on them.** The [GKE release schedule](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule) gives per-minor availability, auto-upgrade start, end of standard support, and end of extended support for every channel, and it is updated monthly. Nothing equivalent exists on EKS or AKS with that fidelity. Scrape it.

**The three clouds will never be on the same minor at the same time.** GKE Rapid gets 1.36 on 2026-04-28; EKS gets it later; AKS later still. If your cell software depends on any Kubernetes feature gate, your minimum supported version is set by AKS, and your maximum by GKE Rapid. Pick a target *band* (e.g. "all cells on N or N-1, where N is the newest minor available on all three") and enforce it in CI.

#### The surprise-upgrade risk, ranked

1. **GKE, by a mile.** Every channel except Extended auto-upgrades your *minor version* on a schedule Google publishes but you do not control. Rollouts are multi-day (typically four or more days), paused on weekends and holidays, and the exact day depends on your region, your maintenance windows, your exposure to deprecated APIs, and your rollout-sequencing config ([release schedule](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule)). Maintenance exclusions are the brake — and for clusters *not* in a channel, exclusions cap out at 30 days.
2. **AKS, if you set a channel.** `rapid` will move you to the newest minor. `stable` moves you to N-1. Either can fire inside your maintenance window without a human. `none` avoids it but then nothing patches either.
3. **EKS, only at the cliff.** EKS will not move your minor while it is supported. But once extended support ends, AWS force-upgrades the control plane "at any time after the end of extended support date" with **no notification**, and does *not* upgrade your nodes or add-ons ([EKS extended support](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html)). That is the worst possible shape of surprise: your apiserver jumps and your kubelets and add-ons do not.

One more GKE-specific landmine: **"No channel" (formerly "Static") is deprecated and will be removed on 2027-06-14**, after which GKE enrolls all remaining clusters in the Stable channel ([release schedule](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule)). If any of your GKE cells were created channel-less to avoid auto-upgrades, you have a hard deadline.

---

### Upgrade mechanics

The shape is identical everywhere and worth stating once:

```text
1. Pre-flight        detect removed APIs, unsupported add-on versions, PDB deadlocks
2. Control plane     provider-driven, ~10-40 min, apiserver moves to N+1
3. Add-ons           CNI, CoreDNS, kube-proxy, CSI must be within compat matrix for N+1
4. Nodes             rolling / surge / blue-green replacement, drains workloads
5. Post-flight       verify DaemonSets, verify PVs re-attached, verify PDBs healthy
```

The version skew policy is what makes step 2 safe to do before step 4: kubelet may be up to three minors behind the apiserver ([skew policy](https://kubernetes.io/releases/version-skew-policy/)). It is also what makes the reverse fatal — never let a node run a kubelet newer than the control plane.

#### Node replacement semantics side by side

| Behavior | EKS managed node groups | GKE node pools | AKS node pools |
|---|---|---|---|
| Strategy knobs | `maxUnavailable` **or** `maxUnavailablePercentage`; `updateStrategy` of `DEFAULT` (respects PDBs) or `FORCE` ([NodegroupUpdateConfig](https://docs.aws.amazon.com/eks/latest/APIReference/API_NodegroupUpdateConfig.html)) | `maxSurge` + `maxUnavailable` (surge strategy) **or** blue/green ([node upgrade strategies](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies)) | `maxSurge`, `drainTimeoutInMinutes`, `nodeSoakDurationInMinutes` ([rolling upgrades](https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-node-pools-rolling)) |
| Defaults | 1 node unavailable | `maxSurge=1, maxUnavailable=0` — surge-first, never dips below capacity ([GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies)) | `maxSurge=1`, drain timeout **30 min**, soak **0 min** ([AKS](https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-node-pools-rolling)) |
| Drain timeout | **15 minutes** per node; without `FORCE`, the update fails if pods do not leave ([EKS update behavior](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-update-behavior.html)) | Governed by pod `terminationGracePeriodSeconds` and PDBs; blue/green adds a soak phase | Configurable **5 min – 24 h**, default 30 min |
| Blue/green node pool | Not built in — you create a second node group and shift | **Yes**, first-class blue/green upgrade strategy with batched drain and soak ([GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies)) | Not built in as an upgrade strategy — you create a second pool |
| Ignores PDBs? | Only with `FORCE` | Surge upgrades respect eviction; blue/green batches respect it too | Drain timeout expiry effectively forces past a stuck PDB |
| Extra capacity needed | Yes — new nodes launch before old drain | `maxSurge>0` needs Compute Engine quota; blue/green **temporarily doubles** the pool ([GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies)) | Yes — AKS explicitly warns upgrades consume extra subnet IPs and vCPU quota ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)) |

Three consequences for a fleet:

- **Quota is an upgrade dependency.** Surge and blue/green need headroom in vCPU quota *and* subnet IP space. AKS calls this out directly: "When you upgrade an AKS cluster, extra resources are temporarily consumed... If you don't have the available IP address space or vCPU quota to handle these temporary resources, the cluster upgrade process fails" ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)). If you sized cell subnets exactly to steady state, your upgrades will fail at 3am.
- **PDBs are the thing that stalls you, and the fix is not `--force`.** A `PodDisruptionBudget` with `minAvailable` equal to the replica count makes the pod permanently un-evictable ([disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)). A single-replica Deployment with `maxUnavailable: 0` does the same. Across 300 cells, one bad PDB in one tenant namespace stalls one upgrade — which is exactly why you want a fleet-wide PDB linter, not a fleet-wide `FORCE`.
- **`terminationGracePeriodSeconds` multiplies.** Drain sends SIGTERM, waits up to the grace period, then SIGKILL ([pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination)). A Temporal worker with a 300s grace period, on a node with 40 such pods draining serially behind a PDB, will blow through EKS's 15-minute node drain timeout. Measure real drain time per node class before you set fleet upgrade concurrency.

#### StatefulSets make all of this worse

A StatefulSet updates pods in strict reverse-ordinal order, one at a time, waiting for each to become Ready ([StatefulSet docs](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)). During a node upgrade, each replacement pod must (a) get scheduled, (b) get its PV detached from the old node and attached to the new one, and (c) pass readiness. If the PV is a **zonal** disk and the new node lands in another AZ, step (b) never completes and the pod is stuck `Pending` forever with a volume node affinity conflict. This is the single most common stateful-upgrade outage on all three clouds.

Kubernetes 1.35 added `maxUnavailable` for StatefulSets as beta, which lets stateful rollouts go parallel ([EKS 1.35 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)). Useful, but it does not fix the AZ problem — see the storage section.

#### The API deprecation problem

Every Kubernetes minor removes APIs that were deprecated several releases earlier ([deprecation guide](https://kubernetes.io/docs/reference/using-api/deprecation-guide/)). At fleet scale you will not find these by reading manifests, because the offender is usually a controller, an operator, or a Helm chart three layers down.

Detection tooling, in order of usefulness:

| Tool | Cloud | How it detects | Blind spot |
|---|---|---|---|
| [EKS Cluster Insights / Upgrade Insights](https://docs.aws.amazon.com/eks/latest/userguide/cluster-insights.html) | EKS | AWS-curated findings surfaced via API and console | Only checks things AWS models; not a full manifest scan |
| [GKE deprecation insights](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations) | GKE | **Observed apiserver calls** from real user agents | Catches only what actually called recently — a monthly cronjob may be missed |
| [`kubent` / kube-no-trouble](https://github.com/doitintl/kube-no-trouble) | Any | Scans live cluster objects, Helm release manifests, and files | Cannot see a controller that will call a removed API but has no stored object |
| Static CI scan of your own charts | Any | Pre-merge | Does not cover third-party charts you install at bootstrap |

Run **all** of them. The GKE model and the `kubent` model are complementary: one catches runtime callers with no stored objects, the other catches stored objects with no recent callers. Neither alone is sufficient.

Deprecations currently live on your roadmap, from primary sources:

- **1.35**: cgroup v1 support removed — kubelet refuses to start on cgroup v1 nodes by default; containerd 1.x support ends (you must be on containerd 2.0+ before the next minor); IPVS mode of kube-proxy deprecated ([EKS 1.35 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)).
- **1.36**: `gitRepo` volumes permanently disabled ([KEP-5040](https://github.com/kubernetes/enhancements/issues/5040)); `StrictIPCIDRValidation` on by default, so `10.0.0.5/24`-style non-canonical CIDRs and leading-zero IPs are **rejected on create/update** ([KEP-4858](https://github.com/kubernetes/enhancements/issues/4858)); SELinux volume labeling GA; Service `.spec.externalIPs` deprecated ([KEP-5707](https://github.com/kubernetes/enhancements/issues/5707)).
- **Ecosystem, not Kubernetes**: Ingress NGINX was retired by the upstream project in March 2026 — no further bug fixes or security patches ([Kubernetes statement](https://kubernetes.io/blog/2026/01/29/ingress-nginx-statement/)). If any cell bootstraps it, that is a live security debt with no drop-in replacement.

The `StrictIPCIDRValidation` one is worth calling out specifically for a cell platform: your cell templates are full of CIDRs. A single `192.168.0.5/24` in a NetworkPolicy or a Helm value that was silently accepted for years will start failing on create at 1.36. Grep your templates now.

---

### Node management: the compute menu

This is where the three clouds diverge most in *concepts*, not just names.

| | EKS | GKE | AKS |
|---|---|---|---|
| Fully self-managed | Self-managed node groups / raw ASGs — you own the AMI, bootstrap, and lifecycle hooks | Not really a thing; node pools are the floor | VM node pools; you own less than on EKS |
| Provider-managed groups | **Managed node groups** — AWS handles ASG, drain, AMI rollout | **Node pools** (Standard mode) | **Node pools** (agent pools) |
| Serverless per-pod | **Fargate** — one microVM per pod, no nodes. Note it still runs cgroup v1 as of 1.35 ([EKS 1.35 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)) | Autopilot's per-pod billing model is the analogue | **Virtual nodes** (ACI-backed) |
| Fully-managed node layer | **EKS Auto Mode** — AWS runs Karpenter off-cluster plus managed networking, LB, and block storage ([AWS announcement](https://aws.amazon.com/blogs/aws/streamline-kubernetes-cluster-management-with-new-amazon-eks-auto-mode)) | **Autopilot** — Google owns nodes entirely; plus **Autopilot ComputeClasses in Standard clusters** since Sept 2025 ([GKE docs](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-autopilot-mode-standard-clusters)) | **Node Auto-Provisioning (NAP)** — managed Karpenter addon, GA ([AKS NAP](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning)); the exact GA date is not stated on any primary Microsoft page, so treat "mid-2025" as approximate |
| Just-in-time provisioner | Karpenter (self-run) or Auto Mode (AWS-run) | **Node auto-provisioning** creates node pools on demand ([GKE NAP](https://cloud.google.com/kubernetes-engine/docs/how-to/node-auto-provisioning)) | NAP, built on [Azure/karpenter-provider-azure](https://github.com/Azure/karpenter-provider-azure) |
| Max nodes per pool/group | Bounded by ASG + EC2 quota | **1,000 per zone** (2,000 for TPU nodes) ([GKE quotas](https://docs.cloud.google.com/kubernetes-engine/quotas)) | **1,000 per node pool**, **100 pools per cluster** ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)) |

Two of the three clouds have now converged on **Karpenter as the managed node autoscaler**: EKS Auto Mode runs Karpenter off-cluster where you never see the pods or the Helm chart, and AKS NAP is the AKS Karpenter provider shipped as a managed addon. GKE's node auto-provisioning predates Karpenter and works differently — it creates and deletes *node pools*, not individual nodes. For a cell platform this convergence is real but narrower than it looks: `karpenter.sh/v1` `NodePool` genuinely is portable, while the *NodeClass* is not — `EC2NodeClass` is at `karpenter.k8s.aws/v1` and `AKSNodeClass` is still at `karpenter.azure.com/v1beta1`, so a cross-cloud abstraction has to carry an API-version skew. See the [Karpenter guide](06-karpenter.md#multi-cloud-status--verified-not-assumed) for the verified per-provider status, and [disruption in depth](06-karpenter.md#disruption-in-depth) for the disruption budgets, consolidation, and drift semantics that matter during upgrades.

A word of caution on the "fully managed node layer" options for *your* use case. Auto Mode and Autopilot both restrict what you can do to nodes: privileged DaemonSets, custom kernel parameters, host networking, and node-level agents are constrained or forbidden. A cell running Temporal's data plane will almost certainly want node-level control (sysctls for connection-heavy services, custom eBPF observability, specific instance families for latency). Treat Auto Mode/Autopilot as strong candidates for *control-plane-adjacent* cells and support tooling, and expect Standard/managed-node-group for the cells that carry customer workflow traffic. Verify this against your actual DaemonSet inventory before committing.

#### Base images, and why the choice matters

| | EKS | GKE | AKS |
|---|---|---|---|
| Default Linux | **AL2023** (AL2 is gone — see below) | **Container-Optimized OS (COS)** | **Ubuntu**; **Azure Linux** opt-in |
| Container-hardened option | **Bottlerocket** — immutable, minimal, API-driven, separate OS/data volumes | COS *is* the hardened option | **Azure Linux** (Microsoft's minimal distro) |
| General-purpose option | AL2023 | **Ubuntu** node images | Ubuntu |
| Current defaults, verified | AL2 EKS-optimized AMIs reached **end of support 2025-11-26**; 1.32 was the last minor with AL2 AMIs; no AL2 AMI for 1.34 ([AWS FAQ](https://docs.aws.amazon.com/eks/latest/userguide/eks-ami-deprecation-faqs.html), [eks-ami#2545](https://github.com/awslabs/amazon-eks-ami/issues/2545)) | COS default; verify per-minor in release notes | **Ubuntu 24.04 becomes the default OS SKU at Kubernetes 1.35**; `--os-sku AzureLinux` defaults to **Azure Linux 3.0 at 1.32+** ([AKS node images](https://learn.microsoft.com/en-us/azure/aks/node-images), [upgrade OS version](https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version)) |

Why the base image is a lifecycle decision, not a taste decision:

- **It sets your patch cadence.** Node OS CVEs land far more often than Kubernetes minors. On AKS the `NodeImage` / `SecurityPatch` auto-upgrade channel is separate from the cluster channel precisely because of this ([node OS auto-upgrade](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-node-os-image)). On EKS, node AMI updates are entirely your pipeline.
- **It determines whether you can SSH in.** Bottlerocket and COS are immutable and have no package manager; debugging is via a separate admin/toolbox container. That is a security win and an incident-response tax. Decide once, fleet-wide, and build the debug path deliberately.
- **It silently changes underneath you at upgrade time.** The AKS case is the sharpest: upgrading a cluster to 1.35 with the Ubuntu OS SKU **automatically moves nodes from Ubuntu 22.04 to 24.04** ([AKS upgrade OS version](https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version)). That is a distro major-version bump — new glibc, new systemd, new default kernel — riding along with a Kubernetes minor upgrade. If any DaemonSet compiles against host headers or depends on kernel modules, test it explicitly.
- **AL2 is a live migration for anyone with old cells.** Kubernetes 1.33 onward is AL2023/Bottlerocket only. The AL2023 cutover changes bootstrap (the `nodeadm`/NodeConfig format replaces the old `bootstrap.sh` args), cgroup version, and IMDS defaults (IMDSv2-only) ([AL2023 upgrade](https://docs.aws.amazon.com/eks/latest/userguide/al2023.html)).

---

### Networking per cloud

Keep this section as a routing table; the deep material belongs in the [CNI and host networking guide](05-cni-and-host-networking.md#cloud-cni-comparison).

| | EKS | GKE | AKS |
|---|---|---|---|
| Default pod networking | **VPC CNI** — every pod gets a real VPC IP from an ENI | **VPC-native / alias IPs** — pods get IPs from a *secondary* subnet range ([alias IPs](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/alias-ips)) | **Azure CNI Overlay** (recommended default), Azure CNI Pod Subnet, legacy Azure CNI node subnet, legacy kubenet ([overlay](https://learn.microsoft.com/en-us/azure/aks/concepts-network-azure-cni-overlay)) |
| Pod IPs routable in the VPC? | Yes | Yes (alias ranges are VPC routes) | Overlay: **no**. Pod Subnet / node subnet: yes |
| Primary IP-exhaustion failure | ENI/IP limits per instance type cap pods per node; large clusters exhaust the subnet ([IP optimization](https://aws.github.io/aws-eks-best-practices/networking/ip-optimization-strategies/)) | The **/24-per-node** default burns the secondary range fast ([flexible pod CIDR](https://cloud.google.com/kubernetes-engine/docs/how-to/flexible-pod-cidr)) | Node-subnet modes exhaust the VNet; overlay largely removes the problem |
| Main mitigation | **Prefix delegation** (/28 prefixes per ENI) plus custom networking on secondary CIDRs ([prefix mode](https://aws.github.io/aws-eks-best-practices/networking/prefix-mode/index_linux/)) | Lower `--max-pods-per-node` so GKE hands out a smaller per-node CIDR; size the secondary range from node count | Use overlay |
| Legacy mode with a deadline | — | — | **kubenet retires 2028-03-31** ([legacy CNI](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni)) |

The GKE arithmetic is worth memorizing because it bites during cell sizing. GKE assigns each node an alias range sized from `max-pods-per-node`: the default 110 pods yields a **/24 per node**, so a secondary range of /21 supports only 2^(24-21) = **8 nodes**. Drop `max-pods-per-node` to 8 and each node takes a /28, so the same /21 supports **128 nodes** ([flexible pod CIDR](https://cloud.google.com/kubernetes-engine/docs/how-to/flexible-pod-cidr)). And note it is not a clean power of two in usable pods: with a /24 you get 110 pods, not 256, because GKE reserves headroom so pods do not become unschedulable during IP churn.

Standard GKE now supports up to **256 pods per node** on 1.23.5-gke.1300+; Autopilot sets the value dynamically between 8 and 256 ([GKE quotas](https://docs.cloud.google.com/kubernetes-engine/quotas)). AKS allows a max of **250 pods per node** on either networking plugin, with defaults of 110 (CLI/ARM) or 30 (portal) ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)) — that portal-vs-CLI default split is a real source of "why is this cluster different."

#### Control-plane endpoint access

| Model | EKS | GKE | AKS |
|---|---|---|---|
| Public + IP allowlist | Public endpoint with CIDR restrictions | Public endpoint + **authorized networks** | **Authorized IP ranges** |
| Private only | Private endpoint; all API traffic must originate in-VPC or a connected network | Private cluster; control plane reachable via peering/PSC | **Private cluster** (Private Link) or **API Server VNet Integration** (endpoint projected into a delegated subnet, no tunnel) ([VNet integration](https://learn.microsoft.com/en-us/azure/aks/api-server-vnet-integration)) |
| Both simultaneously | Yes | Yes | Yes (VNet integration supports hybrid) |
| Fleet implication | Your control-plane automation needs network reachability to N private endpoints | Same | Same |

For a cell fleet, private-only endpoints are the right default and immediately create a bootstrap problem: **your provisioning automation has to be inside the network it is provisioning.** The usual answers are a per-region bastion/runner subnet, a management cluster with peering to every cell VPC/VNet, or a pull-based agent in the cell (Flux/Argo/Arc/Connect Gateway) that reaches out. Pick one and standardize; mixing them across clouds is how you end up with three different break-glass paths.

---

### Identity

#### Workload identity (pods to cloud APIs)

The four mechanisms — IRSA, EKS Pod Identity, Workload Identity Federation for GKE, and Microsoft Entra Workload ID — all reduce to the same three steps: the cluster issues a signed JWT for a ServiceAccount, the cloud's STS verifies it against a registered issuer and a subject predicate, and the workload gets a short-lived cloud credential. Trust anchors, binding syntax, and delivery paths are compared in full in [03-multicloud-aws-gcp-azure.md](03-multicloud-aws-gcp-azure.md#workload-identity-how-a-pod-gets-a-cloud-credential). What follows is only the part that is a *managed-Kubernetes* decision: what each cluster costs you to enable, and where it stops scaling.

| | EKS | GKE | AKS |
|---|---|---|---|
| Per-cluster setup | IRSA: an IAM OIDC provider **per cluster**. Pod Identity: none ([AWS blog](https://aws.amazon.com/blogs/containers/amazon-eks-pod-identity-a-new-way-for-applications-on-eks-to-obtain-iam-credentials/)) | Enable WIF on cluster + node pools | Enable OIDC issuer + workload identity on cluster |
| Scaling pain | **IRSA does not scale to hundreds of clusters.** Each new cluster requires editing the trust policy of every shared IAM role, and trust policy length is capped at **2,048 characters** with **100 OIDC providers per account** ([EKS limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html)) | Bindings are per Google SA, not per cluster — scales better | Federated credentials are per managed identity + issuer + subject, and **a maximum of 20 federated identity credentials can be added to an application or user-assigned managed identity** ([FIC considerations](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations)) — a one-FIC-per-cell design hits the wall at cell 21 |
| Fleet recommendation | **Use Pod Identity for new cells.** It replaces N trust-policy edits with N association API calls | WIF, no alternative needed | Workload ID (pod-managed identity is superseded) |

The IRSA scaling limits are the most important single fact in this section for a hundreds-of-cells fleet, and they are documented as hard AWS quotas: **100 OIDC providers per account** caps IRSA-enabled clusters per account at 100, and a **2,048-character trust policy** caps how many cluster issuers one role can trust. Both are why AWS built Pod Identity. If your cell model is one-AWS-account-per-cell you dodge the OIDC-provider cap but multiply account management; if you pack many cells into an account, you hit it. Know which trade you made.

#### Human and CI access to the API

| | EKS | GKE | AKS |
|---|---|---|---|
| Mechanism | **Access entries** (API) — the modern path; `aws-auth` ConfigMap is deprecated ([migration](https://docs.aws.amazon.com/eks/latest/userguide/migrating-access-entries.html), [ConfigMap docs](https://docs.aws.amazon.com/eks/latest/userguide/auth-configmap.html)) | Google IAM roles map to cluster permissions, composed with Kubernetes RBAC | **Entra ID** integration; optionally **Azure RBAC for Kubernetes authorization** |
| Auth modes | `CONFIG_MAP`, `API_AND_CONFIG_MAP`, `API`. Access entries win when both define a principal | IAM + RBAC always both evaluated (union of permissions) | Entra groups → K8s groups → RBAC, or Azure RBAC role assignments |
| Failure mode to fear | Corrupting `aws-auth` locks **everyone** out of the cluster — historically the classic EKS self-inflicted outage | Over-broad IAM project roles silently granting cluster-admin | Local accounts left enabled alongside Entra, bypassing conditional access |
| Fleet recommendation | Set new cells to `API` mode only. Never ship a cell with a writable `aws-auth` | Grant cluster access through groups, never individuals | Disable local accounts; use Entra + Azure RBAC |

For a cell platform the design goal is: **no human ever needs a per-cluster credential.** Access should be a group membership evaluated by the cloud IAM, so onboarding an engineer is one change, not 300.

---

### Storage

| | EKS | GKE | AKS |
|---|---|---|---|
| Block CSI | EBS CSI driver (an EKS add-on) ([docs](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html)) | Compute Engine PD CSI driver (managed) | Azure Disk CSI driver (managed) |
| Shared-file CSI | EFS CSI, FSx CSI | Filestore CSI | Azure Files CSI, Azure NetApp Files |
| Default StorageClass | `gp2` historically; **from EKS 1.30 the EBS CSI add-on ships a gp3-backed default** ([AWS blueprints notes](https://github.com/awslabs/cdk-eks-blueprints/blob/main/docs/addons/ebs-csi-default-storage-class.md)) | `standard-rwo` (PD-balanced) | `managed-csi` / `managed-csi-premium` |
| Volume expansion | Supported; `allowVolumeExpansion: true` | Supported | Supported |
| Regional/multi-zone volume | No native multi-AZ EBS. Use replication at the app layer | **Regional PD** replicates across two zones in a region ([GKE PVs](https://cloud.google.com/kubernetes-engine/docs/concepts/persistent-volumes)) | **ZRS disks** replicate across zones (SKU-dependent) |
| Failover helper | — | **Stateful HA Operator** automates StatefulSet failover with regional PD ([docs](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/stateful-ha)) | — |
| VolumeAttributesClass | GA at 1.34; AWS patches beta-API sidecars only until **EKS 1.33 standard support ended 2026-07-29** ([EKS 1.34 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)) | — | — |

**The StatefulSet-across-AZ trap, stated precisely.** A zonal block volume can only attach to a node in its own zone. If a StatefulSet pod is rescheduled to another zone — because its node was drained during an upgrade, or an autoscaler consolidated the zone away, or the zone had no capacity — the PVC cannot follow. The pod goes `Pending` with a volume node affinity conflict and stays there.

There is exactly one correct default and two mitigations:

- **Default: `volumeBindingMode: WaitForFirstConsumer` on every block StorageClass.** This delays PV provisioning until the pod is scheduled, so the disk is created in the zone the pod actually landed in. Both AWS's example EBS StorageClass and GKE's guidance use it ([GKE PVs](https://cloud.google.com/kubernetes-engine/docs/concepts/persistent-volumes)). Verify it on every cell — an inherited `Immediate` StorageClass is a latent zone-pinning bug.
- **Mitigation A: regional volumes** where available (GKE regional PD, Azure ZRS disks). Costs more, halves the failure class.
- **Mitigation B: per-zone node pools plus zone-aware anti-affinity**, so each StatefulSet ordinal is pinned to a zone that always has capacity. This is what most people actually do on EKS, since EBS has no regional flavor.

At upgrade time this trap is *systematically* triggered, because upgrades drain every node in the cluster by design. Any stateful workload with a zonal PV and no zone pinning will eventually get hit. This is not an edge case; it is a certainty at fleet scale.

---

### Add-on management

| | EKS | GKE | AKS |
|---|---|---|---|
| Model | **EKS add-ons** — versioned, AWS-tested, installed/upgraded via the EKS API ([add-ons](https://docs.aws.amazon.com/eks/latest/userguide/eks-add-ons.html)) | **Managed add-ons** — Google owns them; you toggle features, not versions | **Add-ons** (AKS-managed, e.g. monitoring, Key Vault CSI) and **cluster extensions** (Arc-based) |
| CNI | VPC CNI as an add-on. You choose the version | Fully managed | Fully managed |
| CoreDNS | Add-on. Version and replica count are yours | Managed, autoscaled by GKE | Managed |
| kube-proxy | Add-on; must track the control-plane minor | Managed | Managed |
| metrics-server | **You install it** | Included | Included |
| CSI drivers | EBS/EFS as add-ons (opt-in) | Included | Included |
| Practical burden | Highest — you own a version matrix per cluster | Lowest | Low |
| Practical control | Highest — you can pin, stage, canary | Lowest — you get what Google ships | Medium |

For a fleet the tension is exact: **EKS add-ons give you the control you need to canary a CNI change across 5 cells before 300, and the burden of maintaining a compatibility matrix per minor version.** GKE removes the burden and the control together. Neither is wrong; be deliberate.

Two operational rules regardless of cloud:

- **CoreDNS is the most common cluster-wide brownout you will cause.** AWS documents that Route 53 resolvers cap at 1,024 packets per second, and funnelling a large cluster's DNS through too few CoreDNS replicas causes lookup timeouts ([EKS limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html)). Size CoreDNS to node count, use NodeLocal DNSCache, and set `ndots` sensibly.
- **Add-on upgrades must be sequenced *between* control plane and nodes.** kube-proxy in particular must not lag the apiserver across a minor boundary.

---

### Scale limits and what actually breaks first

Documented per-cluster limits, verified:

| Limit | EKS | GKE Standard | GKE Autopilot | AKS |
|---|---|---|---|---|
| Nodes per cluster | No published hard cap; bounded by EC2/VPC quotas | **65,000** (tiered — >5,000 needs specific infra and Customer Care) | **5,000** | **5,000** across all node pools (VMSS + Standard LB) |
| Nodes per pool/group | ASG-bounded | **1,000 per zone** | n/a | **1,000** |
| Pools per cluster | — | — | — | **100** |
| Pods per node | Instance-type/ENI bound; higher with prefix delegation | **256** (110 before 1.23.5-gke.1300) | dynamic **8–256** | **250** max; defaults 110 (CLI) / 30 (portal) |
| Pods per cluster | Not published | **200,000** | **200,000** | Not published |
| Containers per cluster | Not published | **400,000** | **400,000** | Not published |
| etcd DB size | **8 GiB** | **6 GB** | **6 GB** | Not published; tier-dependent |
| Concurrent cluster operations | Not published | **100** | **100** | RP throttling: `PUT ManagedCluster` bucket 20, refill 1/min |
| Clusters per account/sub/region | Soft quota | Zonal/regional cluster quotas per project | same | **5,000 per subscription globally**; per-region default **100** (EA) with self-service to **1,000** |
| LoadBalancer Services | ELB quotas apply (50 NLBs/region default) | — | — | **300 per cluster** with Standard LB |

Sources: [EKS known limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html), [GKE quotas](https://docs.cloud.google.com/kubernetes-engine/quotas), [AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions).

**What breaks first is almost never the documented cluster limit.** Ranked by how often it actually bites, in a fleet:

1. **Account/subscription/project quotas, not cluster quotas.** The default **100 AKS clusters per subscription per region** for Enterprise Agreement subscriptions is the sharpest example — a 300-cell Azure footprint needs quota increases *by region*, and above 1,000 needs a support ticket ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)). On AWS, **5 VPCs per region** and **5,000 network interfaces per region** by default will stop a cell rollout long before any EKS limit does.
2. **Cloud API throttling during fleet operations.** AKS publishes exact token-bucket limits: `PUT ManagedCluster` refills at **1 request per minute** per cluster, `LIST ManagedClusters` at 1/sec per subscription with a 500 burst, returning HTTP 429 with `Retry-After` ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)). If your reconciler polls 300 clusters in a tight loop, you throttle yourself. On AWS the equivalent is EC2 ENI/IP API throttling (`CreateNetworkInterface`, `AssignPrivateIpAddresses`, `DescribeInstances`), which is exactly the API surface the VPC CNI hammers during scale-out ([EKS limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html)).
3. **IAM object counts.** 1,000 IAM roles per account and 100 OIDC providers per account cap how many IRSA-enabled clusters fit in an AWS account.
4. **etcd size and object churn.** Long before you reach 200,000 pods, a chatty operator writing status subresources every second will grow etcd and slow the apiserver. Watch the size metric.
5. **CoreDNS and the DNS packet-per-second ceiling.**
6. **Only then**, nodes per cluster.

For a cell architecture, this ranking is a gift: it says **more small cells is operationally safer than fewer big ones**, and the binding constraint is your cloud *account* topology, not Kubernetes.

---

### Multi-tenancy and isolation for a cell model

The isolation ladder, weakest to strongest:

| Boundary | Blast radius contained | Cost / overhead | Leaks |
|---|---|---|---|
| Namespace + RBAC + quota + NetworkPolicy | Application bugs, RBAC mistakes | Near zero | Shared apiserver, shared etcd, shared CoreDNS, shared node kernel, shared cluster version |
| Namespace + dedicated node pool + taints | Adds kernel/noisy-neighbour isolation | Idle node cost per tenant | Still one control plane and one upgrade schedule |
| **Cluster** | Control-plane load, upgrade blast radius, CRD/version conflicts, admission-webhook failures | Control-plane fee + per-cluster add-ons + per-cluster ops | Shared account quotas, shared VPC, shared IAM |
| **Cluster + dedicated account/project/subscription** | Quota contention, IAM blast radius, billing, compromise containment | Account management, networking complexity | Region-level and provider-level failures |

Google's own multi-tenancy guidance walks the same ladder ([GKE multi-tenancy](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/multitenancy-overview)).

**Why "cell = cluster (or a few)" wins for a platform like Temporal Cloud.** The three things that a namespace boundary cannot contain are precisely the three things that hurt most at scale:

1. **The upgrade is cluster-wide.** One control-plane version, one maintenance window, one rollback. If a hundred tenants share a cluster, an upgrade regression hits all hundred. Cells let you canary version N+1 on cells 1–3 and hold 297 at N.
2. **Cluster-scoped objects are shared.** CRDs, admission webhooks, mutating policies, PriorityClasses, StorageClasses, and cluster RBAC are global. A tenant needing CRD `v1` while another needs `v2` is unsolvable in one cluster.
3. **The apiserver and etcd are shared failure domains.** One tenant's runaway controller degrades everyone's control plane. There is no per-namespace apiserver quota that fully prevents this (API Priority and Fairness helps, it does not solve it).

The costs you accept in exchange: a control-plane fee per cell (~$0.10/cluster/hour on EKS/AKS Standard is ~$876/year — negligible next to node cost, meaningful times 300 only if cells are small), N× add-on footprint (CoreDNS, CNI, CSI, metrics-server, and observability agents on every cell — this is the *real* overhead, often 1-2 nodes' worth per cell), and N× upgrade work, which is why the automation in this guide exists.

The practical shape most platforms land on: **cell = one cluster + one account/project/subscription + one VPC/VNet**, with tenants as namespaces *within* a cell, and cell assignment as a placement decision made at tenant onboarding. That gives you a two-level isolation model where the expensive boundary is used sparingly and the cheap boundary carries the tenant count.

---

### Fleet management: how teams actually run hundreds of clusters

Three families, and most mature platforms use two of them together.

**1. Infrastructure-as-code, imperatively driven (Terraform / Pulumi / CDK).** One module per cell, invoked N times, state in a remote backend. Strengths: it is the only approach that natively models *everything around* the cluster (VPC, IAM, DNS, databases, KMS) in the same graph. Weaknesses: state files are a scaling and blast-radius problem at N=300, drift detection is a `plan` you have to run, and there is no continuous reconciliation — nothing fixes a cluster that drifts at 4am.

**2. Cluster API (CAPI) — Kubernetes-native cluster lifecycle.** A management cluster runs controllers that reconcile `Cluster`, `MachineDeployment`, and provider-specific resources into real clusters ([Cluster API book](https://cluster-api.sigs.k8s.io/)).

| Provider | Repo/book | Managed control plane support |
|---|---|---|
| **CAPA** (AWS) | [cluster-api-aws](https://cluster-api-aws.sigs.k8s.io/) | `AWSManagedControlPlane` represents an EKS cluster as a CAPI control plane |
| **CAPZ** (Azure) | [capz.sigs.k8s.io](https://capz.sigs.k8s.io/managed/managed) | Manages both self-managed and AKS clusters from one management plane; **supports ClusterClass for AKS** |
| **CAPG** (GCP) | [cluster-api-provider-gcp](https://github.com/kubernetes-sigs/cluster-api-provider-gcp) | GKE support via managed control plane types |

The strength of CAPI is **continuous reconciliation and a uniform API across clouds** — one `Cluster` CRD shape, three infrastructure providers, and a controller that keeps converging. That is exactly the "cattle" property you want. The known rough edge, documented in the CAPI project's own [managed Kubernetes proposal](https://github.com/kubernetes-sigs/cluster-api/blob/main/docs/proposals/20220725-managed-kubernetes.md), is that CAPI's model assumes infrastructure and control plane are two separate resources, while managed offerings fuse them — CAPA modelled EKS as a control plane, which then collided with ClusterClass's expectations. Provider maturity for managed control planes is genuinely uneven; evaluate per-cloud rather than assuming parity.

**3. Fleet/registry layers.** These do not create clusters; they give you a fleet-wide view and multi-cluster placement.

| Layer | Scope | Notes |
|---|---|---|
| [GKE fleet management](https://cloud.google.com/kubernetes-engine/fleet-management/docs) | GKE-first, with attached clusters | Fleets, Config Sync, Policy Controller, multi-cluster Services/Ingress |
| [Azure Kubernetes Fleet Manager](https://learn.microsoft.com/en-us/azure/kubernetes-fleet/overview) | AKS + Arc-attached | Hub-spoke; joins up to **1,000** clusters; can include non-AKS via Arc |
| [Azure Arc-enabled Kubernetes](https://learn.microsoft.com/en-us/azure/azure-arc/kubernetes/overview) | Any conformant cluster | Projects any cluster into Azure RM for policy, GitOps, and inventory |
| GitOps (Argo CD / Flux) | Any | The workhorse for cluster *contents* regardless of who created the cluster |

**The pattern that works, stated plainly.** Cluster *creation* via IaC or CAPI; cluster *contents* via GitOps, always. The moment you have more than a dozen cells, hand-applied manifests and `helm install` from a laptop become the dominant source of drift. If you use Helm for templating only, with no release state (the setup [09](09-helm.md) assumes), this maps cleanly: render charts to manifests, commit or serve them, and let a reconciler own actual state. That is arguably a *better* fit than Helm-with-Tiller-style state, because your source of truth is the rendered manifest in git, not a secret in a namespace.

**The bootstrap ordering problem, in one sentence:** the tooling that manages a cluster's contents has to be installed *into* the cluster, and it needs credentials, networking, and a functioning CNI before it can run — so something outside the cluster must do the first N steps. Which leads directly to the next section.

*See also: [ApplicationSet generators — the heart of a many-cells fleet](13-gitops-argocd-flux.md#applicationset-generators--the-heart-of-a-many-cells-fleet) for how the "cluster contents via GitOps" half of that pattern is actually wired across N cells.*

---

### Bootstrap ordering for a fresh cell

This is the core of the job. The dependency graph is real, and getting it wrong produces a cluster that looks up but is subtly broken.

```text
   cloud account / project / subscription   (quotas, IAM baseline, logging sinks)
                    │
   network          │  VPC/VNet, subnets, NAT, routes, DNS, private endpoints
                    ▼
   control plane       cluster created; apiserver reachable from the runner
                    │
   ┌────────────────┴─────────────────────────────────────────────┐
   │ 1. CNI                    nodes stay NotReady until this runs │
   │ 2. kube-proxy             (managed on GKE/AKS)                │
   │ 3. first nodes join       node pool / Karpenter's own nodes    │
   │ 4. CoreDNS ready          nothing that resolves names works    │
   │      ▼                      before this                        │
   │ 5. cloud-controller        LoadBalancer + node lifecycle       │
   │ 6. CSI drivers             PVCs bind                           │
   │ 7. metrics-server          HPA + kubectl top                   │
   │ 8. cert-manager            webhooks need certs                 │
   │ 9. Karpenter / autoscaler  needs identity + a place to run     │
   │10. policy (Kyverno/Gatekeeper/PSA)  must precede workloads     │
   │11. observability agents    DaemonSets                          │
   │12. GitOps agent            takes over from here                │
   │13. platform workloads      Temporal services, ingress, etc.    │
   └───────────────────────────────────────────────────────────────┘
```

The non-obvious constraints, which are where real bootstrap bugs live:

- **CNI before anything schedulable.** Nodes report `NotReady` with `NetworkPluginNotReady` until the CNI DaemonSet lands. On EKS the VPC CNI is installed by default, but if you are replacing it (Cilium, custom config) you must do so before the first node group scales, or you get a race between the default CNI and yours.
- **CoreDNS before any component that resolves a Service name.** cert-manager's webhook, Karpenter's apiserver client, and most operators will crash-loop confusingly if DNS is not up. They recover, but your bootstrap looks broken for minutes.
- **Karpenter has a chicken-and-egg problem.** It provisions nodes, so it needs a node to run on. The standard answer is a small managed node group (or GKE/AKS system pool) that hosts only system components, with Karpenter's own NodePools excluded from managing it. On EKS Auto Mode and AKS NAP this disappears because the provisioner runs off-cluster or as a managed addon — a genuine operational simplification worth weighing.
- **cert-manager before any admission webhook that needs a CA.** And note the reverse hazard: cert-manager itself installs a webhook, so if a policy engine with a `failurePolicy: Fail` webhook is installed first and cannot serve, cert-manager's own resources fail to admit. **Order policy after cert-manager, and never install a `Fail`-policy webhook whose backend is not yet running.**
- **Policy before workloads, but with a bypass for system namespaces.** A Kyverno/Gatekeeper policy that requires resource limits will block your own observability DaemonSet if applied first without exclusions.
- **Identity before anything that calls a cloud API.** IRSA/Pod Identity/WIF/Workload ID must be wired before the CSI driver, external-dns, cert-manager's DNS solver, or the LB controller start, or they fail with permission errors that look like bugs.
- **Idempotency is mandatory, not nice-to-have.** Bootstrap will be re-run — on retry, on drift correction, on version bump. Every step must converge, not just create.

A practical structuring: **phase 0 (out-of-cluster, IaC)** = account, network, cluster, identity, first node group. **Phase 1 (in-cluster, imperative one-shot)** = CNI config, CSI, cert-manager, GitOps agent. **Phase 2 (in-cluster, GitOps)** = everything else. Keep phase 1 as thin as you can bear; every component there is a component your provisioning code must version, upgrade, and reconcile by hand.

---

### Teardown: how to actually delete a cell

Deleting is harder than creating because Kubernetes owns cloud resources it did not tell your IaC about.

**The finalizer problem.** A namespace in `Terminating` is almost always blocked by a finalizer or an unreachable API ([finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/)). The nastiest variant: if an `APIService` (an aggregated API like `v1beta1.custom.metrics.k8s.io`) is registered but its backing workload is gone, the namespace controller cannot enumerate resources to clean up — and **no namespace anywhere in the cluster will finish terminating** until that APIService is healthy or removed ([GKE troubleshooting](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)). One dead metrics adapter blocks every deletion in the cluster.

**Do not reflexively strip finalizers.** Force-removing the `kubernetes` finalizer from a namespace lets the object disappear and leaves the resources it contained orphaned — cloud load balancers, external DNS records, cloud-managed secrets, operator-managed database instances ([GKE troubleshooting](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)). At fleet scale, orphans are a slow-motion cost and security problem: you will eventually find load balancers with public IPs pointing at nothing, for cells you deleted a year ago.

**Orphan classes to hunt, per cloud:**

| Orphan | Created by | EKS | GKE | AKS |
|---|---|---|---|---|
| Load balancers | `Service type=LoadBalancer` / Ingress | CLB/NLB/ALB + target groups + security groups | Forwarding rules, backend services, health checks | Load balancer rules, public IPs |
| Persistent disks | PVC with `Retain` reclaim policy | EBS volumes | Persistent disks | Managed disks |
| Network interfaces | VPC CNI | ENIs (count against a 5,000/region quota) | — | — |
| Snapshots | VolumeSnapshot | EBS snapshots | PD snapshots | Disk snapshots |
| DNS records | external-dns | Route 53 records | Cloud DNS records | Azure DNS records |
| IAM objects | IRSA setup | OIDC provider + roles (100 provider cap) | Service accounts, bindings | Managed identities, federated credentials |
| Node-created resources | Karpenter | Instances, launch templates | — | VMs, NICs, disks |

**A teardown order that works:**

1. **Stop the reconcilers first.** Suspend the GitOps agent and delete Karpenter/autoscaler *controllers*, otherwise they will fight the teardown by recreating things.
2. **Delete workload namespaces and wait**, so the cloud-controller-manager and CSI drivers get a chance to delete the cloud resources they created. This is the step people skip, and it is the step that prevents orphans.
3. **Delete anything with a `Retain` reclaim policy explicitly** — Kubernetes will not.
4. **Verify zero remaining `Service type=LoadBalancer` and zero bound PVCs** before touching the cluster object.
5. **Then delete node pools, then the cluster.**
6. **Then sweep the cloud account by tag.** Every cell resource should carry a `cell-id` tag/label from birth so a sweep is a single tag query per resource type. Run the sweep as an assertion in CI, not as a manual cleanup.
7. **Finally, reclaim identity objects** (OIDC providers, roles, federated credentials) — the ones nobody remembers and that hit hard quotas.

Give yourself a **deletion dry-run** that reports what step 6 would find. Run it on every teardown, alert if non-empty, and you will catch a whole class of bug that otherwise only surfaces on the invoice.

*See also: [Argo CD resource tracking, prune, and deletion](13-gitops-argocd-flux.md#argo-cd-resource-tracking-prune-and-deletion) for step 1 and step 2 in practice — suspending the reconciler, prune propagation policies, and the namespace-stuck-in-Terminating failure that stalls this list at step 2.*

---

### Cost and cross-AZ traffic

| Cost driver | Detail |
|---|---|
| Control plane | ~$0.10/cluster/hour on EKS and AKS Standard; **6x that** in EKS extended support or AKS Premium ([AWS](https://aws.amazon.com/blogs/containers/amazon-eks-extended-support-for-kubernetes-versions-pricing/), [AKS](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)). At 300 cells, $0.10/hr is ~$263k/year, and extended support turns that into ~$1.6M/year |
| Cross-AZ traffic | AWS charges **$0.01/GB in each direction** between AZs in a region — $0.02/GB round trip. Same-AZ private-IP traffic is free ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)) |
| Add-on footprint | CoreDNS + CNI + CSI + metrics-server + logging + metrics agents on every cell. Often 1–2 nodes' worth per cell — the largest *hidden* cell overhead |
| Idle headroom | Surge/blue-green upgrades need spare capacity or quota; blue/green temporarily doubles a node pool ([GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies)) |
| Spare IP space | Sized for peak-during-upgrade, not steady state |

Cross-AZ cost drives real topology decisions for a cell:

- **A multi-AZ cell is the right default for availability and costs you AZ-crossing traffic on every internal hop.** For a Temporal-shaped workload — frontend to history to matching to persistence — a single request can cross AZ boundaries several times. `trafficDistribution: PreferSameZone` (and `PreferSameNode`, stable in 1.35 per the [EKS 1.35 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)) reduces this materially for stateless hops.
- **A single-AZ cell is cheaper and simpler for stateful components** (no cross-AZ replication traffic, no zonal-disk trap) but concentrates failure. Some platforms run zonal cells and rely on cell-level redundancy, which is a legitimate and often underrated design — it converts an availability problem into a placement problem.
- **Regional vs zonal control plane matters too.** GKE regional clusters replicate the control plane across zones with a higher SLA than zonal ([GKE SLA](https://cloud.google.com/kubernetes-engine/sla)); AKS availability zones move the API server SLA from 99.9% to 99.95% ([AKS tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)).

---

## Hands-on

### Lab 0 — Prerequisites (free)

```bash
# macOS
brew install kind kubectl helm kustomize
brew install derailed/k9s/k9s          # optional but excellent for drain-watching

# kube-no-trouble, for the deprecated-API lab
brew install kube-no-trouble           # or: go install github.com/doitintl/kube-no-trouble/cmd/kubent@latest

kind version && kubectl version --client
```

All `kind` labs below run entirely on your laptop and cost **$0**. They cover everything that is generic Kubernetes: version skew, drain/eviction, PDB deadlock, finalizer traps, deprecated APIs. The cloud-specific behavior (managed upgrade orchestration, node image swaps, IP exhaustion) needs a cloud lab, priced at the end.

---

### Lab 1 — Simulate a control-plane-then-node upgrade (`kind`, free)

`kind` lets you pin node images to specific Kubernetes versions, so you can reproduce the exact skew a managed upgrade produces.

```yaml
# kind-skew.yaml — control plane at 1.33, workers at 1.33 to start
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: skewlab
nodes:
  - role: control-plane
    image: kindest/node:v1.33.0
  - role: worker
    image: kindest/node:v1.33.0
  - role: worker
    image: kindest/node:v1.33.0
```

```bash
kind create cluster --config kind-skew.yaml
kubectl get nodes -o custom-columns=NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion
```

Now simulate the managed-upgrade order. `kind` cannot upgrade in place, so model it the way a real fleet does — **replace nodes**, do not mutate them:

```bash
# 1. "Upgrade the control plane": recreate the cluster with a newer CP image but
#    keep the old worker image, reproducing kubelet-behind-apiserver skew.
kind delete cluster --name skewlab
cat > kind-skew2.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: skewlab
nodes:
  - role: control-plane
    image: kindest/node:v1.35.0
  - role: worker
    image: kindest/node:v1.33.0     # 2 minors behind: legal
  - role: worker
    image: kindest/node:v1.33.0
EOF
kind create cluster --config kind-skew2.yaml
kubectl get nodes -o wide
```

What to observe and write down:

- The cluster works. Two-minor skew is legal ([skew policy](https://kubernetes.io/releases/version-skew-policy/)) — this is *exactly* the state a real cluster sits in between step 2 and step 4 of an upgrade.
- Try a control plane at 1.33 with a worker at 1.35 (kubelet newer than apiserver). Note what breaks and how confusingly.
- Note that nothing in the API tells you the skew is dangerous. Fleet tooling has to compute it.

**Exercise:** write a script that, for every context in your kubeconfig, prints apiserver minor, min kubelet minor, and the skew, and flags anything at 3 or over. That is a real tool you will want.

---

### Lab 2 — PDB, drain, and eviction semantics (`kind`, free)

This is the lab that teaches you why upgrades stall.

```bash
# Workers are required: a bare `kind create cluster` gives you a single
# control-plane node, and the `grep worker` below would come back empty.
cat > kind-pdblab.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: pdblab
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
kind create cluster --config kind-pdblab.yaml

kubectl create deploy web --image=registry.k8s.io/pause:3.9 --replicas=3
kubectl rollout status deploy/web
```

Create a PDB that is satisfiable, then one that is not:

```yaml
# pdb-ok.yaml — 3 replicas, allow 1 down
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web-ok
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: web
```

```yaml
# pdb-deadlock.yaml — 3 replicas, require all 3: nothing can ever be evicted
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web-deadlock
spec:
  minAvailable: 3
  selector:
    matchLabels:
      app: web
```

```bash
kubectl apply -f pdb-ok.yaml
kubectl get pdb
NODE=$(kubectl get nodes -o name | grep worker | head -1 | cut -d/ -f2)

# Drain respects the PDB and completes
kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data
kubectl uncordon "$NODE"

# Now the deadlock
kubectl delete pdb web-ok
kubectl apply -f pdb-deadlock.yaml
kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=60s
# -> "Cannot evict pod as it would violate the pod's disruption budget." Blocks forever.
```

Then measure grace-period cost:

```yaml
# slow.yaml — a pod that takes its full grace period to exit
apiVersion: v1
kind: Pod
metadata:
  name: slow
  labels: {app: slow}
spec:
  terminationGracePeriodSeconds: 120
  containers:
    - name: sleeper
      image: registry.k8s.io/e2e-test-images/agnhost:2.47
      command: ["/bin/sh","-c","trap 'sleep 120' TERM; sleep 100000"]
```

```bash
kubectl apply -f slow.yaml
kubectl wait --for=condition=Ready pod/slow
time kubectl delete pod slow          # ~120s: the grace period is real wall-clock
```

What to take away:

- A PDB does not fail fast; it blocks. That is why EKS caps node drain at 15 minutes and fails the node-group update ([EKS update behavior](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-update-behavior.html)) and why AKS defaults `drainTimeoutInMinutes` to 30 ([AKS rolling upgrades](https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-node-pools-rolling)).
- Grace period times pods-per-node times PDB serialization is your real per-node drain time. Compute it before choosing upgrade concurrency.

**Exercise:** write a fleet linter that flags any PDB where `minAvailable >= replicas` or `maxUnavailable == 0`. Run it in admission, not just in CI.

---

### Lab 3 — Finalizer and namespace-deletion traps (`kind`, free)

Reproduce the two failure modes that will eat your teardown automation.

**3a. An object-level finalizer that never clears:**

```bash
kubectl create ns finaltest
kubectl -n finaltest create configmap stuck --from-literal=k=v
kubectl -n finaltest patch configmap stuck --type=merge \
  -p '{"metadata":{"finalizers":["example.com/never-removed"]}}'

kubectl -n finaltest delete configmap stuck --timeout=20s   # hangs
kubectl delete ns finaltest --timeout=30s                    # namespace stuck Terminating
kubectl get ns finaltest -o jsonpath='{.status.conditions}' | jq .
```

The `status.conditions` on the namespace tell you exactly what is blocking. Learn to read them — that is your teardown debugger.

```bash
# Correct fix: clear the finalizer on the OBJECT (a controller would normally do this)
kubectl -n finaltest patch configmap stuck --type=merge -p '{"metadata":{"finalizers":null}}'
kubectl get ns finaltest        # now completes
```

**3b. The dead APIService that blocks every namespace in the cluster:**

```yaml
# ghost-apiservice.yaml — registers an aggregated API with no backend
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.ghost.example.com
spec:
  group: ghost.example.com
  version: v1beta1
  groupPriorityMinimum: 100
  versionPriority: 10
  service:
    name: does-not-exist
    namespace: default
    port: 443
  insecureSkipTLSVerify: true
```

```bash
kubectl apply -f ghost-apiservice.yaml
kubectl get apiservices | grep ghost      # False (ServiceNotFound)

kubectl create ns victim
kubectl -n victim create configmap x --from-literal=a=b
kubectl delete ns victim --timeout=60s    # hangs — and so will EVERY other namespace

kubectl delete apiservice v1beta1.ghost.example.com
kubectl get ns victim                     # completes immediately
```

This is the exact mechanism GKE documents ([terminating namespaces](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)), and it is worth burning into memory: **one broken aggregated API is a cluster-wide deletion outage.** Add "all APIServices Available" to your pre-teardown assertion list.

---

### Lab 4 — Detect removed APIs before an upgrade (`kind`, free)

```bash
kind create cluster --name kubentlab --image kindest/node:v1.33.0

# Install something with a chart, so kubent has a Helm release to scan
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
helm install ms metrics-server/metrics-server -n kube-system \
  --set 'args={--kubelet-insecure-tls}'

kubent                                   # scan live objects + helm releases
kubent -t 1.36.0                          # what breaks if I target 1.36?
kubent --helm3=false --cluster            # cluster-objects only
```

Then feed it a manifest with a known-removed API to see a real finding:

```bash
cat > old.yaml <<'EOF'
apiVersion: policy/v1beta1
kind: PodDisruptionBudget
metadata: {name: legacy-pdb}
spec:
  minAvailable: 1
  selector: {matchLabels: {app: nope}}
EOF
kubent -f old.yaml
```

**Exercise:** wire `kubent -t <next-minor> -e` (exit non-zero on findings) into a cron that runs against every cell and opens a ticket per finding. That plus [EKS Cluster Insights](https://docs.aws.amazon.com/eks/latest/userguide/cluster-insights.html) and [GKE deprecation insights](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations) is a complete detection story.

---

### Cloud labs (real money — read the costs first)

These teach the things `kind` cannot: managed upgrade orchestration, node image replacement, IP exhaustion, identity federation. **Delete everything the same day.** All figures are rough, on-demand, us-east-1-ish, and should be re-checked against current pricing pages.

| Lab | What it teaches | Rough cost | Teardown gotcha |
|---|---|---|---|
| **A. EKS: 2-node cluster, upgrade 1.34 to 1.35** | Real control-plane-then-node ordering; managed node group `maxUnavailable`; add-on version matrix | ~$0.10/hr control plane + 2 × m5.large (~$0.19/hr) + NAT (~$0.045/hr + data) ≈ **$0.35/hr**; a 4-hour lab ≈ **$1.50** | `eksctl delete cluster` leaves orphans if any `Service type=LoadBalancer` exists. Delete Services first |
| **B. EKS: force IP exhaustion** | VPC CNI ENI limits, prefix delegation | Same as A, plus a small instance type (t3.small caps pods low) | Same |
| **C. GKE: Standard cluster, set `--max-pods-per-node 16`, observe alias range** | The /24-per-node math, secondary range sizing | Cluster management fee + 2 × e2-standard-2 (~$0.13/hr) ≈ **$0.25/hr**; 3-hour lab ≈ **$0.80** | Delete the cluster *and* check for leftover forwarding rules and disks |
| **D. GKE: blue/green node pool upgrade** | Batched drain + soak; watch pool size double | As C, but node cost temporarily doubles | Blue/green leaves the old pool until it completes — do not cancel midway |
| **E. AKS: cluster with `maxSurge=33%`, `drainTimeout=5`, `nodeSoak=2`** | Surge sizing, drain timeout expiry behavior, forced eviction past a PDB | Free tier control plane ($0) + 2 × Standard_D2s_v5 (~$0.19/hr) ≈ **$0.19/hr**; 3-hour lab ≈ **$0.60** | Delete the **resource group**, not just the cluster — AKS creates a second `MC_*` resource group that must go too |
| **F. AKS: enable NAP, delete a node pool, watch Karpenter provision** | Managed Karpenter behavior vs self-run | As E | Same `MC_*` gotcha |
| **G. Identity: IRSA vs Pod Identity on EKS; WIF on GKE; Workload ID on AKS** | Token exchange flows end-to-end | Minimal beyond the cluster | Delete OIDC providers and federated credentials — they count against quotas |

Run A, C, and E at minimum. Together they are under $5 and they are the only way to feel the difference in upgrade orchestration.

A tiny discipline that will save you real money: tag or label every lab resource with `owner=<you>` and `ttl=<date>`, and run a nightly sweeper. That is the same pattern you need for cell teardown, so you may as well practise it.

---

## Production gotchas

1. **EKS force-upgrades your control plane after extended support ends, with no notification, and does not touch your nodes.** AWS documents this explicitly: automatic updates can happen at any time after the end-of-extended-support date, you get no advance notice, and you must manually update add-ons and EC2 nodes afterwards ([EKS extended support](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html)). The failure shape is an apiserver two minors ahead of your kubelets. Alarm on days-until-EOL per cell and never let a cell approach the cliff.

2. **EKS extended support costs 6x, and it is per cluster per hour.** $0.60/hr vs $0.10/hr ([AWS blog](https://aws.amazon.com/blogs/containers/amazon-eks-extended-support-for-kubernetes-versions-pricing/)). At 300 cells that is roughly $1.3M/year of pure procrastination tax. Model this as a line item in your upgrade business case; it is usually the single most persuasive number you have.

3. **GKE "No channel" clusters are on a hard deadline of 2027-06-14**, after which Google enrols them in Stable ([GKE release schedule](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule)). Any GKE cell created channel-less to dodge auto-upgrades will start auto-upgrading. Inventory this now.

4. **GKE maintenance exclusions cap at 30 days for channel-less clusters.** You cannot indefinitely freeze a GKE cluster. Plan upgrades on Google's calendar, not yours, and scrape the release schedule into your roadmap.

5. **AKS upgrades fail on quota, not on Kubernetes.** Microsoft states plainly that upgrades temporarily consume extra IP addresses and vCPU quota, and that the upgrade process fails without headroom ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)). Size cell subnets and vCPU quota for peak-during-upgrade — roughly steady state plus `maxSurge` — not for steady state.

6. **The default AKS clusters-per-subscription-per-region quota is 100 for EA subscriptions**, with self-service to 1,000 and support tickets beyond ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)). A 300-cell Azure footprint hits this per region. Raise quotas as part of region onboarding, before the first cell.

7. **IRSA does not scale past 100 clusters per AWS account** — the IAM OIDC-providers-per-account quota is 100, and role trust policies cap at 2,048 characters ([EKS known limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html)). Use [EKS Pod Identity](https://aws.amazon.com/blogs/containers/amazon-eks-pod-identity-a-new-way-for-applications-on-eks-to-obtain-iam-credentials/) for new cells; it replaces per-cluster trust-policy edits with an association API call.

8. **`aws-auth` is deprecated and is a single-object cluster lockout risk.** Set new EKS cells to `API` authentication mode (access entries only) ([migration guide](https://docs.aws.amazon.com/eks/latest/userguide/migrating-access-entries.html)). A malformed `aws-auth` edit has locked more teams out of production EKS clusters than any other single mistake.

9. **One dead APIService blocks namespace deletion cluster-wide.** If an aggregated API is registered without a healthy backend, the namespace controller cannot enumerate resources and *no* namespace finishes terminating ([GKE troubleshooting](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)). Make "all APIServices Available" a pre-teardown gate, and alert on it continuously.

10. **Never force-remove the `kubernetes` finalizer from a namespace.** It orphans cloud load balancers, disks, DNS records, and operator-managed external resources ([GKE troubleshooting](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)). Fix the blocking controller instead, and if you must force, run the cloud-resource sweep immediately afterwards.

11. **Zonal block volumes plus node draining equals guaranteed stuck StatefulSets, eventually.** Set `volumeBindingMode: WaitForFirstConsumer` on every block StorageClass, and either use regional volumes (GKE regional PD, Azure ZRS) or pin StatefulSet ordinals to zones ([GKE PVs](https://cloud.google.com/kubernetes-engine/docs/concepts/persistent-volumes)). Audit inherited StorageClasses on every cell; `Immediate` binding is a latent outage.

12. **cgroup v1 is gone as of Kubernetes 1.35 — kubelet refuses to start by default.** AL2023 and Bottlerocket are fine, but Bottlerocket sets `failCgroupV1: false` for compatibility and EKS Fargate still uses cgroup v1 ([EKS 1.35 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)). If any cell runs a custom AMI with a manual cgroup v1 configuration, 1.35 will not boot it.

13. **containerd 1.x support ends with Kubernetes 1.35.** You must be on containerd 2.0+ before the next minor ([EKS 1.35 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)). This is a node-image concern, so it is your problem on EKS and mostly the provider's on GKE/AKS — but verify, do not assume.

14. **Kubernetes 1.36 rejects non-canonical CIDRs and leading-zero IPs on create/update.** `StrictIPCIDRValidation` is on by default; `192.168.0.5/24` and `010.0.0.5` are refused, with ratcheting only for already-stored objects ([KEP-4858](https://github.com/kubernetes/enhancements/issues/4858), [EKS 1.36 notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html)). Cell templates are full of CIDRs. Grep and fix them before 1.36 lands.

15. **Amazon Linux 2 EKS AMIs are end-of-support since 2025-11-26 and 1.32 was the last minor to get them** ([AWS FAQ](https://docs.aws.amazon.com/eks/latest/userguide/eks-ami-deprecation-faqs.html)). The AL2023 move changes node bootstrap format, cgroup version, and defaults to IMDSv2-only ([AL2023 upgrade](https://docs.aws.amazon.com/eks/latest/userguide/al2023.html)) — an SDK in a pod that still uses IMDSv1 will start failing.

16. **Upgrading an AKS cluster to 1.35 with the Ubuntu OS SKU silently moves nodes from Ubuntu 22.04 to 24.04** ([AKS upgrade OS version](https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version)). A distro major bump riding along with a Kubernetes minor upgrade is a genuine risk for anything touching the host kernel or glibc. Test DaemonSets explicitly on the new image before the fleet-wide rollout.

17. **AKS kubenet retires 2028-03-31** ([legacy CNI](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni)). Migrating a live cluster's network plugin is generally a rebuild, not an in-place change, so this is a cell-recycle project with a fixed deadline. Start the inventory now.

18. **GKE's default 110 pods per node consumes a /24 of your secondary range per node.** A /21 secondary range supports 8 nodes at the default ([flexible pod CIDR](https://cloud.google.com/kubernetes-engine/docs/how-to/flexible-pod-cidr)). If your GKE cell template copies the AWS subnet sizing, it will run out of nodes at a number that looks absurd. Set `--max-pods-per-node` deliberately per cell profile, and note it is fixed at node-pool creation.

19. **Your own reconciler will throttle you.** AKS publishes `PUT ManagedCluster` at 1 request per minute per cluster and returns HTTP 429 with `Retry-After` ([AKS quotas](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)); AWS throttles the exact EC2 ENI/IP APIs the VPC CNI depends on during scale-out ([EKS limits](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html)). Fleet reconcilers need jittered backoff and per-cloud concurrency limits, or a cell rollout becomes a self-inflicted throttling incident.

20. **Ingress NGINX was retired by upstream Kubernetes in March 2026** — no more bug fixes or security patches, and no drop-in replacement ([Kubernetes statement](https://kubernetes.io/blog/2026/01/29/ingress-nginx-statement/)). If any cell bootstraps it, that is unpatched internet-facing software in your dependency graph. Plan the Gateway API migration as a funded project, not a chore.

21. **A control plane at N with add-ons pinned at N-2 is a silent time bomb.** kube-proxy, CNI, and CSI all have compatibility matrices tied to the control-plane minor. On EKS you own that matrix. Encode it in your cell template and validate it in pre-flight, because the failure mode is subtle networking or volume-attach breakage weeks later, not a clean error at upgrade time.

---

## How this shows up in cell lifecycle

Mapping the above onto the four verbs of cell lifecycle.

**Provision.** A cell definition is a version-pinned, cloud-branching template. Concretely: minor version (chosen from the intersection of what all three clouds support), release channel or auto-upgrade policy (Extended/none/none — you want to control timing, not Google), node abstraction (managed node group + Karpenter on AWS, node pool + NAP on Azure, node pool + NAP on GKE), OS image, pod IP model and CIDR sizing, workload identity mechanism, StorageClass with `WaitForFirstConsumer`, and the bootstrap ordering from the section above. The branching points are exactly the rows of the tables in this guide; everything else is one code path. Helm-for-templating fits this well: render per-cloud values into manifests, let GitOps own applied state.

**Upgrade.** This is a *pipeline*, not an event. Pre-flight (deprecated APIs via `kubent` + provider insights, PDB lint, add-on matrix check, quota headroom check, APIService health). Then a wave schedule: canary cells, then a percentage, then the rest, with soak time between waves. Then per-cell: control plane, add-ons, nodes, verify. The per-cloud differences that matter are the node-replacement knobs (`maxUnavailable` and `updateStrategy` on EKS, `maxSurge`/blue-green on GKE, `maxSurge`/`drainTimeout`/`nodeSoak` on AKS) and the auto-upgrade policy you have to *suppress* so your pipeline stays in control. On GKE, that means Extended channel or aggressive maintenance exclusions; on AKS, channel `none` plus your own driver; on EKS, staying comfortably inside standard support.

**Converge.** Drift is the steady-state enemy. GitOps for cluster contents, plus a continuous conformance check per cell: correct add-on versions, correct StorageClass binding mode, no `Immediate` block classes, no deadlock PDBs, all APIServices healthy, node OS image within N days of latest, kubelet skew under 2, days-until-EOL above threshold. Emit these as fleet metrics with a per-cell label. Your dashboard should answer "how many cells are non-conformant and why" in one glance.

**Tear down.** The ordered sequence from the teardown section, wrapped in assertions: stop reconcilers, drain workload namespaces, verify zero LoadBalancer Services and zero bound PVCs, delete node pools, delete cluster, sweep by `cell-id` tag, reclaim identity objects. The sweep must be a first-class, tested code path with a dry-run mode that runs on every teardown — orphaned cloud resources are the failure mode that never pages you and always shows up on the invoice.

**Cross-cutting.** When networking is owned by a separate team, be precise about the seam. A typical split: you own pod CIDR sizing, CNI choice and version, node subnet allocation, and the control-plane endpoint model; they own ingress, load balancers, service mesh, and cross-cell routing. The IP-exhaustion class of failure sits exactly on that boundary — you size the ranges, they consume them with LoadBalancer Services and NAT. Write the contract down.

---

## Learning path

### Day 1 (about 3 hours)

- Read [GKE versioning and support](https://docs.cloud.google.com/kubernetes-engine/versioning) and the [GKE release schedule](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule) end to end. GKE has the clearest published model of the three; understanding it gives you the vocabulary for the other two.
- Read the [EKS extended support page](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html) and the [AKS supported versions page](https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions). Fill in a copy of the big comparison table from memory afterwards.
- Read the [Kubernetes version skew policy](https://kubernetes.io/releases/version-skew-policy/). It is short and it explains why every upgrade is ordered the way it is.
- Run **Lab 2** (PDB and drain). This is the highest-value hour in the guide.

### Week 1

- Run Labs 1, 3, and 4. Lab 3 in particular — the ghost APIService trick is something you will use in a real incident.
- Read the three quota pages side by side ([EKS](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html), [GKE](https://docs.cloud.google.com/kubernetes-engine/quotas), [AKS](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)) and write down, for each cloud, the first three quotas a 300-cell fleet would hit.
- Read the node upgrade docs for all three ([EKS](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-update-behavior.html), [GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies), [AKS](https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-node-pools-rolling)) and build a one-page cheat sheet of the knobs and defaults.
- Run cloud Labs A, C, and E (under $5 total). Watch a real upgrade on each cloud and time it.
- Read the identity docs for all three ([Pod Identity](https://aws.amazon.com/blogs/containers/amazon-eks-pod-identity-a-new-way-for-applications-on-eks-to-obtain-iam-credentials/), [GKE WIF](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity), [Entra Workload ID](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview)) and draw the token-exchange flow for each from memory.

### Month 1

- Read your team's actual cell template top to bottom and annotate every place it branches per cloud. Compare against the branch list in this guide; anything extra is either a real difference you should learn or accidental divergence you should propose removing.
- Build the skew/EOL reporter from Lab 1's exercise against your real fleet. Ship it as a dashboard.
- Build the PDB linter from Lab 2's exercise and propose it as an admission policy.
- Trace one real cell teardown end to end, with cloud audit logs open, and enumerate every resource that survived the cluster deletion. Turn that list into the sweeper's test fixture.
- Read the [Cluster API book](https://cluster-api.sigs.k8s.io/) and the [managed Kubernetes proposal](https://github.com/kubernetes-sigs/cluster-api/blob/main/docs/proposals/20220725-managed-kubernetes.md) so you can hold an informed opinion on whether CAPI belongs in your stack. Then read your team's existing IaC and form a view on the trade you already made.
- Pick the nearest version cliff on each cloud and write the upgrade plan for it, including the wave schedule and the rollback story.

---

## References

1. [Review release notes for Kubernetes versions on standard support — AWS](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html) — authoritative per-minor breaking changes for EKS 1.34/1.35/1.36, including cgroup v1 removal and strict CIDR validation.
2. [Review release notes for Kubernetes versions on extended support — AWS](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html) — the 14+12 month model, forced-upgrade behavior, AL2 AMI deprecation notice.
3. [Amazon EKS extended support pricing — AWS Containers Blog](https://aws.amazon.com/blogs/containers/amazon-eks-extended-support-for-kubernetes-versions-pricing/) — the $0.10 vs $0.60 per cluster-hour numbers.
4. [AWS::EKS::Cluster UpgradePolicy — AWS](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-eks-cluster-upgradepolicy.html) — the `STANDARD` vs `EXTENDED` cluster field that decides what happens at end of standard support.
5. [Known limits and service quotas — Amazon EKS Best Practices](https://docs.aws.amazon.com/eks/latest/best-practices/known_limits_and_service_quotas.html) — the single best page on what actually breaks at EKS scale: IAM OIDC provider cap, trust policy length, ENI quotas, EC2 API throttling, 8 GiB etcd limit, Route 53 PPS.
6. [View and manage Amazon EKS service quotas — AWS](https://docs.aws.amazon.com/eks/latest/userguide/service-quotas.html) — where to check and raise EKS-specific quotas.
7. [Managed node group update behavior — AWS](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-update-behavior.html) — the 15-minute drain timeout and PDB-respecting vs force update strategies.
8. [NodegroupUpdateConfig API reference — AWS](https://docs.aws.amazon.com/eks/latest/APIReference/API_NodegroupUpdateConfig.html) — `maxUnavailable`, `maxUnavailablePercentage`, `updateStrategy`.
9. [Migrating from aws-auth to access entries — AWS](https://docs.aws.amazon.com/eks/latest/userguide/migrating-access-entries.html) — the three authentication modes and precedence rules.
10. [Grant IAM users access with a ConfigMap — AWS](https://docs.aws.amazon.com/eks/latest/userguide/auth-configmap.html) — the deprecated `aws-auth` path, for when you inherit one.
11. [Amazon EKS Pod Identity — AWS Containers Blog](https://aws.amazon.com/blogs/containers/amazon-eks-pod-identity-a-new-way-for-applications-on-eks-to-obtain-iam-credentials/) — why Pod Identity exists and how it removes the per-cluster OIDC provider.
12. [Streamline cluster management with EKS Auto Mode — AWS News Blog](https://aws.amazon.com/blogs/aws/streamline-kubernetes-cluster-management-with-new-amazon-eks-auto-mode) — what AWS takes over in Auto Mode.
13. [EKS AL2 and AL2-accelerated AMI transition FAQ — AWS](https://docs.aws.amazon.com/eks/latest/userguide/eks-ami-deprecation-faqs.html) — the 2025-11-26 AL2 end-of-support date and what replaces it.
14. [NOTICE: Amazon Linux 2 AMI end of support — awslabs/amazon-eks-ami#2545](https://github.com/awslabs/amazon-eks-ami/issues/2545) — the upstream issue with the concrete final-AMI timeline.
15. [Upgrade from Amazon Linux 2 to Amazon Linux 2023 — AWS](https://docs.aws.amazon.com/eks/latest/userguide/al2023.html) — bootstrap format, cgroup, and IMDS changes in the AL2023 migration.
16. [Use Kubernetes volume storage with Amazon EBS — AWS](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html) — EBS CSI driver as an EKS add-on.
17. [Amazon EKS add-ons — AWS](https://docs.aws.amazon.com/eks/latest/userguide/eks-add-ons.html) — the add-on model, versioning, and which components it covers.
18. [Cluster insights — Amazon EKS](https://docs.aws.amazon.com/eks/latest/userguide/cluster-insights.html) — AWS's built-in upgrade blocker detection.
19. [Optimizing IP address utilization — EKS Best Practices](https://aws.github.io/aws-eks-best-practices/networking/ip-optimization-strategies/) — the practical playbook for VPC CNI IP exhaustion.
20. [Prefix mode for Linux — EKS Best Practices](https://aws.github.io/aws-eks-best-practices/networking/prefix-mode/index_linux/) — prefix delegation, the main pods-per-node lever on EKS.
21. [Best practices for cluster upgrades — EKS Best Practices](https://aws.github.io/aws-eks-best-practices/upgrades/) — AWS's own upgrade runbook, worth stealing structure from.
22. [Amazon EC2 on-demand pricing — AWS](https://aws.amazon.com/ec2/pricing/on-demand/) — source for the $0.01/GB per direction cross-AZ data transfer charge.
23. [GKE versioning and support — Google](https://docs.cloud.google.com/kubernetes-engine/versioning) — the minor version lifecycle, ~14 months standard and up to 24 months with Extended.
24. [GKE release schedule — Google](https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule) — the most useful single page in this guide: per-minor availability, auto-upgrade, and end-of-support dates per channel, plus the No-channel removal date.
25. [About release channels — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/release-channels) — Rapid/Regular/Stable/Extended semantics and what auto-upgrades in each.
26. [Use release channels — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/release-channels) — how to enrol and change channels on existing clusters.
27. [Announcing GKE extended support — Google Cloud Blog](https://cloud.google.com/blog/products/containers-kubernetes/announcing-gke-extended-support) — vendor announcement; useful for the rationale, secondary to the versioning docs.
28. [Quotas and limits — GKE](https://docs.cloud.google.com/kubernetes-engine/quotas) — 65,000 nodes Standard / 5,000 Autopilot, 256 pods per node, 200,000 pods, 400,000 containers, 6 GB etcd, 100 concurrent operations.
29. [Best practices for planning large GKE clusters — Google](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/planning-large-clusters) — the tiering requirements behind the 5k/15k/65k node thresholds.
30. [Node upgrade strategies — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies) — surge defaults (`maxSurge=1`, `maxUnavailable=0`) and blue/green mechanics.
31. [Configure node upgrade strategies — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/node-pool-upgrade-strategies) — the actual flags, batch sizes, and soak configuration.
32. [Maintenance windows and exclusions — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/maintenance-windows-and-exclusions) — your only real brake on GKE auto-upgrades.
33. [VPC-native clusters and alias IPs — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/alias-ips) — how GKE pod IPs come from secondary subnet ranges.
34. [Configure maximum Pods per node — GKE](https://cloud.google.com/kubernetes-engine/docs/how-to/flexible-pod-cidr) — the /24-per-node default and the arithmetic for sizing secondary ranges.
35. [About Workload Identity Federation for GKE — Google](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity) — the metadata-server interception and STS token exchange flow.
36. [Persistent volumes and dynamic provisioning — GKE](https://cloud.google.com/kubernetes-engine/docs/concepts/persistent-volumes) — regional PD, `WaitForFirstConsumer`, and `allowedTopologies` behavior.
37. [Stateful HA Operator — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/stateful-ha) — automated StatefulSet failover with regional PD.
38. [Kubernetes deprecations in GKE — Google](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations) — deprecation insights generated from observed apiserver calls.
39. [Troubleshoot namespace stuck in Terminating — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces) — the definitive writeup of the dead-APIService trap and why not to strip finalizers.
40. [About Autopilot mode workloads in GKE Standard — Google](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-autopilot-mode-standard-clusters) — the `autopilot` and `autopilot-spot` ComputeClasses added to Standard clusters.
41. [Node auto-provisioning — GKE](https://cloud.google.com/kubernetes-engine/docs/how-to/node-auto-provisioning) — GKE's pool-creating autoscaler, conceptually different from Karpenter.
42. [Cluster multi-tenancy — GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/multitenancy-overview) — Google's isolation ladder, useful framing for cell sizing.
43. [Fleet management — GKE](https://cloud.google.com/kubernetes-engine/fleet-management/docs) — fleets, Config Sync, Policy Controller, multi-cluster services.
44. [Container-optimized compute for Autopilot — Google Cloud Blog](https://cloud.google.com/blog/products/containers-kubernetes/container-optimized-compute-delivers-autoscaling-for-autopilot) — vendor blog; context on the Autopilot scheduling changes. Secondary source.
45. [Supported Kubernetes versions in AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions) — the N-2 policy, 12-month window, and the per-version GA/EOL table.
46. [Long-term support for AKS versions — Microsoft](https://learn.microsoft.com/en-us/azure/aks/long-term-support) — 24-month total support, Premium tier requirement, two-patch limitation.
47. [AKS Long Term Support announcement — AKS Engineering Blog](https://blog.aks.azure.com/2025/07/25/aks-lts-announcement) — vendor blog explaining LTS for every version. Secondary source.
48. [Quotas, VM size restrictions, and region availability in AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions) — 5,000 nodes per cluster, 1,000 per pool, 100 pools, 250 pods per node, RP throttling buckets, cluster-per-subscription quotas, and the upgrade-consumes-extra-quota warning.
49. [Automatically upgrade an AKS cluster — Microsoft](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster) — the `patch`/`stable`/`rapid`/`none` channels and `aksManagedAutoUpgradeSchedule`.
50. [Auto-upgrade node OS images — Microsoft](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-node-os-image) — the separate node-image channel and `aksManagedNodeOSUpgradeSchedule`.
51. [Configure rolling upgrades for AKS node pools — Microsoft](https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-node-pools-rolling) — `maxSurge`, `drainTimeoutInMinutes` (default 30, range 5 min to 24 h), `nodeSoakDurationInMinutes`.
52. [Upgrade options and recommendations for AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/upgrade-options) — the overall AKS upgrade decision tree.
53. [Node auto-provisioning in AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning) — managed Karpenter as an AKS addon.
54. [Azure/karpenter-provider-azure — GitHub](https://github.com/Azure/karpenter-provider-azure) — the provider behind AKS NAP; read this if you run Karpenter self-hosted on Azure.
55. [Azure CNI Overlay overview — Microsoft](https://learn.microsoft.com/en-us/azure/aks/concepts-network-azure-cni-overlay) — the recommended AKS pod networking model and its IP characteristics.
56. [AKS legacy container networking interfaces — Microsoft](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni) — kubenet's 2028-03-31 retirement and the node-subnet legacy mode.
57. [API Server VNet Integration — Microsoft](https://learn.microsoft.com/en-us/azure/aks/api-server-vnet-integration) — projecting the AKS API server into a delegated subnet without a tunnel.
58. [AKS Free, Standard, and Premium pricing tiers — Microsoft](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers) — tier costs, SLA differences, and control-plane scaling per tier.
59. [Microsoft Entra Workload ID on AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview) — OIDC issuer plus federated identity credentials.
60. [Node images in AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/node-images) — the OS SKU defaults per Kubernetes version.
61. [Upgrade OS version in AKS clusters — Microsoft](https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version) — the Ubuntu 22.04 to 24.04 auto-migration at Kubernetes 1.35.
62. [Best practices for large AKS clusters — Microsoft](https://learn.microsoft.com/en-us/azure/aks/best-practices-performance-scale-large) — what to do before pushing past a few thousand nodes.
63. [Azure Kubernetes Fleet Manager overview — Microsoft](https://learn.microsoft.com/en-us/azure/kubernetes-fleet/overview) — hub-spoke fleet management, up to 1,000 member clusters.
64. [Azure Arc-enabled Kubernetes overview — Microsoft](https://learn.microsoft.com/en-us/azure/azure-arc/kubernetes/overview) — projecting any conformant cluster into Azure Resource Manager.
65. [Version skew policy — Kubernetes](https://kubernetes.io/releases/version-skew-policy/) — kubelet may trail the apiserver by three minors from 1.25; the rule that makes control-plane-first upgrades safe.
66. [Kubernetes releases — Kubernetes](https://kubernetes.io/releases/) — upstream release cadence and support windows.
67. [Disruptions and PodDisruptionBudgets — Kubernetes](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/) — the semantics that stall every managed node upgrade.
68. [Safely drain a node — Kubernetes](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/) — the eviction API and what `kubectl drain` actually does.
69. [Pod lifecycle: termination — Kubernetes](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination) — where `terminationGracePeriodSeconds` fits into drain wall-clock time.
70. [Finalizers — Kubernetes](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/) — the mechanism behind every stuck deletion.
71. [StatefulSets — Kubernetes](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) — ordinal update ordering and why stateful upgrades are slow.
72. [Deprecated API migration guide — Kubernetes](https://kubernetes.io/docs/reference/using-api/deprecation-guide/) — the canonical list of what was removed in which minor.
73. [kube-no-trouble (kubent) — GitHub](https://github.com/doitintl/kube-no-trouble) — scans live objects, Helm releases, and files for APIs removed in a target version.
74. [Ingress NGINX retirement statement — Kubernetes Blog](https://kubernetes.io/blog/2026/01/29/ingress-nginx-statement/) — official notice of the March 2026 retirement.
75. [KEP-4858: Strict IP/CIDR validation — Kubernetes Enhancements](https://github.com/kubernetes/enhancements/issues/4858) — the 1.36 change that rejects non-canonical CIDRs.
76. [KEP-5040: gitRepo volume removal — Kubernetes Enhancements](https://github.com/kubernetes/enhancements/issues/5040) — permanently disabled in 1.36.
77. [etcd storage size limit — etcd docs](https://etcd.io/docs/v3.5/dev-guide/limit/) — the 8 GiB default behind the EKS documented limit.
78. [The Cluster API Book — Kubernetes SIGs](https://cluster-api.sigs.k8s.io/) — the canonical reference for declarative cluster lifecycle.
79. [Managed Kubernetes in CAPI proposal — kubernetes-sigs/cluster-api](https://github.com/kubernetes-sigs/cluster-api/blob/main/docs/proposals/20220725-managed-kubernetes.md) — the honest writeup of why managed control planes are awkward in CAPI's model.
80. [Cluster API Provider AWS book — Kubernetes SIGs](https://cluster-api-aws.sigs.k8s.io/) — CAPA, including `AWSManagedControlPlane` for EKS.
81. [Managed Clusters (AKS) — Cluster API Provider Azure book](https://capz.sigs.k8s.io/managed/managed) — CAPZ's managed AKS support, including ClusterClass.
82. [cluster-api-provider-gcp — GitHub](https://github.com/kubernetes-sigs/cluster-api-provider-gcp) — CAPG, for GKE and GCE-based clusters.
83. [kind — Kubernetes SIGs](https://kind.sigs.k8s.io/) — the local cluster tool used for every free lab in this guide.
84. [How Temporal builds durable cloud control systems — Temporal Blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) — Temporal's own description of the cell architecture (account + VPC + EKS cluster per cell) and its extension to GCP.
85. [Temporal Cloud overview — Temporal Docs](https://docs.temporal.io/cloud/overview) — Namespaces as the tenant isolation unit inside a cell.
86. [Multi-tenant application patterns — Temporal Docs](https://docs.temporal.io/production-deployment/multi-tenant-patterns) — the namespace-per-tenant vs shared-namespace trade, directly relevant to cell packing.
