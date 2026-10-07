# Temporal Cloud Infrastructure Ramp-Up

A seventeen-guide library I wrote to ramp up for a new infrastructure role at Temporal:
how Temporal Cloud's cells are built, run, upgraded, and torn down across AWS, GCP, and
Azure, and the open-source tools underneath.

It is also a record of how I prepare for a new position: map the surface area the job is
likely to touch, go deep on each technology from primary sources, build small labs to
check my understanding, and finish with a concrete first-90-days plan.

> **Disclaimer.** I wrote this library before I joined Temporal, to ramp up on the
> areas my new role would likely touch. It contains no internal information — nothing
> here was sourced from Temporal's internal knowledge. Temporal is open source, so the
> programming model and server internals covered here come from public code and
> documentation, and every statement about Temporal Cloud comes from Temporal's public
> docs and engineering blog, linked inline. Most of the library is general infrastructure
> technology — Go, gRPC, Kubernetes, Karpenter, Terraform, Helm, Vault, cert-manager,
> GitOps, observability — that has nothing to do with any one company. Where a guide goes
> beyond public sources, it says so and labels the claim as inference.

Written 2026-08-29. Every version number, default, limit, and deprecation date was
verified against live primary sources on that date and carries an inline link.
This stack churns fast — re-check anything load-bearing before you build on it.
Claims that could not be verified are explicitly marked as such in the text.

## Read this first

**[guides/12-cell-lifecycle-synthesis.md](guides/12-cell-lifecycle-synthesis.md)** is the map.
It defines the cell, gives the exact dependency order for bringing one up, names the
bootstrap paradoxes between these tools, and lays out a 30/60/90 plan that points back
into the other sixteen guides. The rest are deep dives; its **How to use this library**
section gives a suggested order if you want one.

## Layout

```
temporal-infra-foundations-ramp-up/
├── index.html          <- open this: searchable hub, cards, cross-links
├── style.css           <- shared stylesheet (light/dark, print-friendly)
├── build.py            <- regenerates html/ from guides/
├── guides/*.md         <- the source of truth; edit these
└── html/*.html         <- rendered, generated; do not edit by hand
```

## The guides

| # | Guide | Focus |
|---|-------|-------|
| 01 | [Go](guides/01-golang.md) | Language, concurrency, `context`, runtime in containers, controller-runtime idioms |
| 02 | [gRPC and Protobuf](guides/02-grpc.md) | Wire compat, HTTP/2, load balancing, keepalives, deadlines, Temporal's own protocol |
| 03 | [Multi-Cloud](guides/03-multicloud-aws-gcp-azure.md) | AWS/GCP/Azure side by side: hierarchy, IAM, workload identity, networking, quotas |
| 04 | [Managed Kubernetes](guides/04-managed-kubernetes-eks-gke-aks.md) | EKS/GKE/AKS deltas, version windows, upgrades, fleet management, teardown |
| 05 | [CNI and Host Networking](guides/05-cni-and-host-networking.md) | Namespaces up through eBPF, kube-proxy, DNS, cloud CNIs, a 3am debugging runbook |
| 06 | [Karpenter](guides/06-karpenter.md) | v1 API, disruption and consolidation, spot, multi-cloud provider reality |
| 07 | [Kyverno](guides/07-kyverno.md) | Admission control, the CEL migration, generate rules for cell bootstrap, safe rollout |
| 08 | [Terraform](guides/08-terraform.md) | State and blast radius, module design, N-cells patterns, where to stop and hand off |
| 09 | [Helm](guides/09-helm.md) | Templating-only: rendered manifests, what you give up, pruning, server-side apply |
| 10 | [Vault](guides/10-vault.md) | Seal/unseal, auth methods, dynamic credentials, K8s integration, disaster recovery |
| 11 | [cert-manager and PKI](guides/11-cert-manager-and-pki.md) | Issuers, trust distribution, root rotation, designing PKI for cells |
| 12 | [Cell Lifecycle](guides/12-cell-lifecycle-synthesis.md) | The synthesis: dependency DAG, bootstrap paradoxes, upgrades, 90-day plan |
| 13 | [GitOps: Argo CD and Flux](guides/13-gitops-argocd-flux.md) | Reconcilers, ApplicationSets across N cells, inventory and pruning, secrets in GitOps |
| 14 | [Observability for Cells](guides/14-observability-for-cells.md) | Prometheus, cardinality, OTel, SLOs and burn-rate alerts, the cell health gate |
| 15 | [Temporal: Programming Model](guides/15-temporal-programming-model.md) | The customer's seat: durable execution, determinism, versioning, workers and long polls |
| 16 | [Temporal Server Internals](guides/16-temporal-server-internals.md) | Code level: History shards and the range-ID fence, Matching, persistence, membership |
| 17 | [Cell-Based Architecture](guides/17-cell-based-architecture.md) | The pattern Temporal Cloud is built on: routing, partition keys, placement, migration, waves, shared dependencies |

## Rebuilding the HTML

Edit the Markdown, then:

```bash
pip install markdown "pygments==2.11.2"   # pinned: newer Pygments rewrites all code-block markup
python3 build.py
```

Each page gets a sidebar, an on-page table of contents, copy buttons on code blocks,
and a theme toggle. `index.html` searches all 410 headings across the library.
