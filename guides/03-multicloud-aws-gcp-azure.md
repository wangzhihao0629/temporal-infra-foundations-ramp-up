# AWS / GCP / Azure Foundations for a Portable Cell

**Why this matters.** Cell lifecycle work — provisioning, upgrading, and tearing down isolated Kubernetes-based units of Temporal Cloud capacity — spans AWS, GCP, and Azure. That means you are not "an AWS engineer who also touches GCP." You are the person who has to answer, for every capability a cell needs, three questions at once: what is this called here, what is its scoping model, and what breaks when I assume it behaves like the other two. Temporal has already walked part of this road publicly: the expansion from AWS-only to AWS + GCP is documented in [Making Temporal Cloud a Multi-Cloud Platform](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability), which is explicit that Kubernetes and Terraform standardize *less* than people expect, that the load balancer resource graphs differ in *shape* and not just in naming, and that the visibility layer (OpenSearch on AWS) had no drop-in GCP equivalent. Azure is named in that post as the next target. This guide is the comparison substrate for that work.

Everything below is organized as: capability → three-column table → the deltas that actually bite.

---

## The mental model

Three framings make three clouds tractable simultaneously. Internalize these and most individual facts become derivable.

### Framing 1: every cloud answers the same six questions, differently

For any cell you provision, the platform must supply: (1) a **tenancy container** that bounds blast radius and quota, (2) a **principal** the cell's software runs as, (3) an **L3 fabric** with addressing and policy, (4) an **ingress surface** that terminates client traffic, (5) **durable state** (block, object, database), and (6) **key material** for encryption and TLS. Every cloud answers all six. The names are noise; the *scoping models* are signal. When you hit an unfamiliar service, ask "which of the six is this, and what is its scope?" rather than "what is the AWS equivalent?"

### Framing 2: the scope axis is the single biggest source of surprise

Each cloud has a different default scope for the same object. Memorize this table and half the multi-cloud bugs become predictable:

| Object | AWS scope | GCP scope | Azure scope |
|---|---|---|---|
| Virtual network | Regional (VPC) | **Global** (VPC network) | Regional (VNet) |
| Subnet | **Zonal** | **Regional** (spans all zones) | Regional (spans all zones) |
| Routes / route tables | Per-subnet route table, regional | Global routes on the VPC | Per-subnet UDR, regional |
| Firewall object | Attached to ENI (security group), regional | Attached to the **VPC**, targeted by tag/service account | Attached to subnet **and/or** NIC (NSG) |
| L7 load balancer | Regional (ALB) | Global **or** regional (ALB) | Regional (App Gateway) or global (Front Door) |
| Block volume | **Zonal** | Zonal, plus **regional (2-zone sync)** options | Zonal, plus **ZRS** options |
| Managed K8s control plane | Regional | Zonal or regional | Regional |
| Identity boundary | Account | Project (in an Organization) | **Entra tenant, separate from the ARM subscription** |
| Quota boundary | Account × region | Project × region | Subscription × region |

The three classics: GCP subnets are regional so a "subnet per AZ" loop produces one subnet, not three; GCP VPCs are global so cross-region connectivity needs no peering at all; Azure splits identity (Entra) from resource management (ARM) so "create the identity then grant it a role" is two different APIs with two different consistency models.

### Framing 3: abstract the verbs, never the nouns

The failure mode of multi-cloud is a `CloudLoadBalancer` type with a lowest-common-denominator field set. Temporal's own answer, published in the multi-cloud blog, is an interface of *operations* (`DeployPersistenceStore`, `DeployVisibilityPersistenceStore`) plus a factory that returns a provider-specific implementation, with cloud-agnostic parent workflows spawning cloud-specific child workflows. The interface is a list of verbs the cell lifecycle needs. The implementations are allowed to be wildly different — three Terraform resources on GCP versus two on AWS, a different vendor entirely for visibility. Adding a method to the interface forces you to implement it everywhere, which is exactly the feature-parity pressure you want.

Corollary: the thing you keep stable is the **cell contract** — what a finished cell exposes to the rest of the system (endpoints, identity, persistence handles, telemetry streams, teardown semantics) — not the resource graph that produces it.

---

## Core concepts

### Resource hierarchy and tenancy

| | AWS | GCP | Azure |
|---|---|---|---|
| Top | Organization (management account) | Organization (tied to a Cloud Identity/Workspace domain) | Entra ID tenant |
| Grouping | Organizational Units, nestable [5 levels deep](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_reference_limits.html) | Folders, nestable [up to 10 levels](https://docs.cloud.google.com/resource-manager/docs/limits), max 300 folders per parent | Management groups, [6 levels of depth](https://learn.microsoft.com/en-us/azure/governance/management-groups/overview) excluding root and subscription |
| Isolation + quota unit | **Account** | **Project** | **Subscription** |
| Sub-container | none (region is not a container) | none | **Resource group** (has its own location; metadata lives there) |
| Guardrail policy | SCPs (identity-side) and [RCPs](https://aws.amazon.com/about-aws/whats-new/2024/11/resource-control-policies-restrict-access-aws-resources) (resource-side), introduced Nov 2024 | Organization Policy constraints (inherited down the hierarchy) + IAM deny policies | Azure Policy (+ deny assignments) at MG/subscription/RG |
| Default creation limit | [10 accounts by default](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_reference_limits.html), raisable via Service Quotas | Project-creation quota on the creating principal *and* the org | Subscription creation is a billing-agreement operation, not a quota bump |
| Deletion semantics | Account close, 90-day suspension before removal | Project delete → 30-day soft delete, ID **never reusable** | Subscription cancel; RG delete is synchronous-ish and cascades |

**Deltas that bite.**

- **Azure has a fourth level nobody else has.** The resource group is a real lifecycle container: deleting it cascades, and every resource must live in exactly one. That makes `rg-cell-<id>` an attractive cell boundary — one delete, whole cell gone — but resource groups are *not* a quota boundary and *not* a security boundary by default, and their `location` only stores metadata (a resource in `eastus2` can live in a `westus` RG, which produces confusing failure modes during regional outages). Do not confuse RG-per-cell with account-per-cell.
- **The quota boundary is the real cell boundary.** On all three, quota is enforced at account/project/subscription × region. If you put ten cells in one account, they share one EC2 vCPU quota and one throttling budget, and a runaway cell starves its neighbors. Cell-per-account (AWS), cell-per-project (GCP), cell-per-subscription (Azure) gives you hard quota isolation and a clean [blast-radius story](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html) — at the cost of N× the quota-increase paperwork, N× the org-policy attachment surface, and N× the "which account is this?" cognitive load.
- **Project IDs are permanently burned on GCP.** A deleted project's ID cannot be reused. If your cell naming scheme derives project IDs from cell IDs, a create-fail-destroy-retry loop permanently consumes names. Design cell IDs with a generation suffix from day one.
- **GCP projects are cheap; AWS accounts and Azure subscriptions are not.** Programmatic project creation is a normal API call bounded by quota. Programmatic AWS account creation via Organizations is possible but slow and rate-limited. Azure subscription creation depends on your agreement type (EA/MCA) and is the least ergonomic of the three. This asymmetry alone often pushes teams toward cell-per-project on GCP and cell-per-namespace-in-a-shared-account elsewhere — which then makes the three implementations structurally different, which is fine, as long as the cell contract hides it.

### Regions, zones, and what "multi-region" actually means

| | AWS | GCP | Azure |
|---|---|---|---|
| Failure domain inside a region | Availability Zone | Zone | Availability Zone (not present in every region) |
| Typical count | 3+ per region | 3+ per region | 3 where supported; some regions have none |
| Naming stability | AZ **names** are per-account aliases; **AZ IDs** (`use1-az1`) are stable | Zone names are globally stable | Logical zones 1/2/3 are mapped **per subscription** to physical zones ([docs](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-overview)) |
| Region pairing | None (you choose) | None (you choose) | Historically paired regions drive some replication defaults |
| Global services | IAM, Route 53, CloudFront, Organizations | IAM, Cloud DNS, global LB, **VPC networks** | Entra ID, Front Door, Traffic Manager, DNS |
| Resource that spans regions natively | None | **VPC network**, global LB, multi-region GCS buckets | None at the network layer |

Two consequences for cells.

First, **"deploy the cell across three zones" is not a portable instruction.** On AWS it means three subnets, three NAT gateways, three node groups (or one spanning group with three subnets), and a conscious cross-AZ traffic bill. On GCP it means one subnet and a regional node pool. On Azure it means one subnet, zone-spanning VM scale sets, and free inter-zone traffic — but you must first confirm the region *has* zones, because not all do, and a zone-redundant template deployed to a non-zonal region either fails or silently degrades to single-zone.

Second, **zone anti-affinity across cells is only meaningful if you can name physical zones.** If cell A and cell B live in different AWS accounts and both "spread across a, b, c," they may be spread across the *same* physical zones or completely different ones — the alias mapping is per account. The same applies across Azure subscriptions. Record AZ IDs and physical zone mappings in cell metadata at provision time; you cannot reconstruct them later from the names.

### Identity and authorization

| | AWS | GCP | Azure |
|---|---|---|---|
| Principal types | IAM users, IAM roles, service-linked roles | User accounts, groups, **service accounts**, federated principals | Users, groups, **service principals**, managed identities (system- or user-assigned) |
| Policy attachment | Identity policies + **resource policies** (S3, KMS, SQS, …) + role **trust policies** | [Allow policies bound to resources](https://docs.cloud.google.com/iam/docs/overview) at org/folder/project/resource; roles are collections of permissions | [Role assignments](https://learn.microsoft.com/en-us/azure/role-based-access-control/overview) = (principal, role definition, scope) as ARM resources |
| Inheritance | None down the hierarchy; SCPs only *subtract* | Allow policies **inherit** down the hierarchy and are additive | Role assignments **inherit** down MG → subscription → RG → resource |
| Deny | Explicit `Deny` in policy; SCP/RCP boundaries | IAM **deny policies** (separate object) + org policy constraints | Deny assignments (created by Azure Blueprints/managed apps, not directly authorable) |
| Assume/impersonate | `sts:AssumeRole` gated by the target role's **trust policy** | **Impersonate a service account** — you need `iam.serviceAccounts.getAccessToken` / `actAs` on the SA *resource*, and the SA needs roles on the target | No impersonation model; you *are* the managed identity, or you hold the SP's credential |
| Control-plane vs identity-plane | Single plane (IAM + STS) | Single plane (Cloud IAM + IAM Credentials API) | **Split**: Microsoft Graph for identity objects, ARM for role assignments |

**Why GCP's model genuinely differs from AWS assume-role.** In AWS, a role is a *container of permissions* whose trust policy says who may wear it; `AssumeRole` swaps your identity wholesale. In GCP, a service account is simultaneously a **principal** (it can hold IAM roles on other resources) and a **resource** (it has its own IAM policy saying who may act as it). Getting a token therefore requires a two-sided grant: someone must grant *you* `roles/iam.serviceAccountTokenCreator` **on the service account object**, and someone must grant *the service account* a role **on the thing you want to touch**. This is [service account impersonation](https://docs.cloud.google.com/iam/docs/service-account-impersonation), and it composes: SA-A can impersonate SA-B which can impersonate SA-C, producing delegation chains AWS models with role chaining (and AWS caps role chaining at one hour of session duration, which GCP does not have an analogue for). The practical consequence for cell automation: your GCP provisioning identity needs *two* grants per target, in two different projects potentially, and forgetting the resource-side grant produces a `PERMISSION_DENIED` that names the wrong thing.

**Why Azure's split plane bites.** Creating a user-assigned managed identity is an ARM operation. Creating an app registration + service principal is a **Microsoft Graph** operation against Entra ID. Granting either a role is an ARM operation. These have independent throttling, independent RBAC (Entra directory roles are *not* Azure RBAC roles — see [the comparison doc](https://learn.microsoft.com/en-us/azure/role-based-access-control/rbac-and-directory-admin-roles)), and — critically — **independent replication latency**. A freshly created service principal frequently is not yet visible to ARM when you immediately try to assign it a role, producing `PrincipalNotFound`. Every mature Azure provisioning workflow has a retry loop around role assignment. In Temporal terms this is exactly what activity retry policies are for; do not "fix" it with `sleep 30`.

**The same grant, three ways.** "Let the cell's provisioner read one bucket" looks like this:

```jsonc
// AWS — two documents: a trust policy on the role, an identity policy for the permission
// Trust policy (who may become this role):
{ "Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": { "StringEquals": {
      "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:temporal:provisioner",
      "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud": "sts.amazonaws.com" }}}]}
// Identity policy (what the role may do): s3:GetObject on arn:aws:s3:::cell-artifacts/*
```

```bash
# GCP — one binding if you use direct resource access; two if you impersonate a service account
gcloud storage buckets add-iam-policy-binding gs://cell-artifacts \
  --role=roles/storage.objectViewer \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/temporal/sa/provisioner"

# The older two-sided form, for comparison — note BOTH grants are required:
gcloud iam service-accounts add-iam-policy-binding provisioner@${PROJECT_ID}.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[temporal/provisioner]"   # grant ON the SA
gcloud storage buckets add-iam-policy-binding gs://cell-artifacts \
  --role=roles/storage.objectViewer \
  --member="serviceAccount:provisioner@${PROJECT_ID}.iam.gserviceaccount.com" # grant TO the SA
```

```bash
# Azure — the identity is a resource; the grant is a separate ARM resource; the federation is a third
az identity create -g rg-cell-0001 -n uami-provisioner
CID=$(az identity show -g rg-cell-0001 -n uami-provisioner --query clientId -o tsv)
PID=$(az identity show -g rg-cell-0001 -n uami-provisioner --query principalId -o tsv)
az role assignment create --assignee-object-id "$PID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/$SUB/resourceGroups/rg-cell-0001/providers/Microsoft.Storage/storageAccounts/cellartifacts"
az identity federated-credential create --name k8s-provisioner \
  --identity-name uami-provisioner -g rg-cell-0001 \
  --issuer "$AKS_OIDC_ISSUER" \
  --subject "system:serviceaccount:temporal:provisioner" \
  --audiences "api://AzureADTokenExchange"
```

Notice the shape difference: AWS expresses trust *inside the role*, GCP expresses it as a *principal string* in an ordinary IAM binding, and Azure expresses it as a *separate child resource* of the identity, subject to the 20-credential cap. There is no faithful common representation of these three, which is exactly why the abstraction belongs at the level of "bind this workload to this capability."

**Deltas summary.**

- AWS is the only one where a *resource* policy is a routine, first-class part of everyday access design (bucket policies, KMS key policies). On GCP and Azure, permissions almost always flow from the hierarchy. A KMS key policy on AWS can lock you out of your own key permanently; there is no GCP/Azure equivalent of that particular self-inflicted wound.
- GCP's inheritance is additive-only in the allow direction, so "grant at the folder to reduce toil" silently widens blast radius. Use deny policies and org policy constraints as the counterweight; [`iam.disableServiceAccountKeyCreation`](https://docs.cloud.google.com/organization-policy/restrict-service-accounts) should be on by default in every cell project, which forces every workload onto federation (good).
- Azure role assignments are ARM resources with their own GUIDs and a per-subscription cap. Cell-per-subscription keeps you far from that cap; shared-subscription-many-cells will eventually hit it.

### Workload identity: how a pod gets a cloud credential

This is the single most important portability surface for a Kubernetes-based cell, because *everything* in the cell — the CSI driver, external-dns, cert-manager, the Vault unsealer, the backup agent, the Temporal services themselves — needs a cloud credential and none of them should hold a static key.

| | AWS (IRSA) | AWS (EKS Pod Identity) | GCP (Workload Identity Federation for GKE) | Azure (Microsoft Entra Workload ID) |
|---|---|---|---|---|
| Trust anchor | Cluster's OIDC issuer registered as an [IAM OIDC provider](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html) | `pods.eks.amazonaws.com` service principal + [association API](https://docs.aws.amazon.com/eks/latest/userguide/pod-identity.html) | Cluster-bound workload identity pool `PROJECT_ID.svc.id.goog` | Cluster's [OIDC issuer](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview) + federated identity credential |
| Binding expressed as | Role trust policy condition on `sub` = `system:serviceaccount:NS:SA` | `CreatePodIdentityAssociation(cluster, namespace, sa, role)` | KSA annotation → GSA, **or** direct `principal://…/subject/ns/NS/sa/KSA` binding | KSA annotation `azure.workload.identity/client-id` + pod label `azure.workload.identity/use: "true"` |
| Delivery mechanism | Projected SA token file; SDK calls `AssumeRoleWithWebIdentity` | Node-local **Pod Identity Agent** DaemonSet serves credentials over a link-local endpoint | `gke-metadata-server` DaemonSet intercepts the metadata endpoint and exchanges the projected token via STS | Mutating webhook injects `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE`; SDK exchanges at the Entra token endpoint |
| Per-cluster setup cost | One IAM OIDC provider **per cluster** | One add-on install; trust policy is cluster-agnostic | Enable WI on cluster **and** set node pool metadata to `GKE_METADATA` | Enable OIDC issuer + workload identity add-on |
| Scales to N clusters | Poorly — every new cluster needs a new OIDC provider and every role's trust policy edited | Well — one role, many associations | Moderately — pool is per project; the subject encodes cluster only in the direct-binding form | Poorly by default — **max 20 federated identity credentials per identity** ([docs](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations)) |
| Cross-account/subscription | Role chaining | [Supported since June 2025](https://aws.amazon.com/about-aws/whats-new/2025/06/amazon-eks-pod-identity-cross-account-access) via target-account details on the association | Cross-project via granting the KSA principal roles in another project | Cross-subscription within a tenant is fine; cross-tenant needs multi-tenant app registration |
| Notable gotcha | Trust policy `sub` string is brittle; `aud` must be `sts.amazonaws.com` | Not available on Fargate (no DaemonSets) | Metadata server takes seconds to become ready — early auth **fails**, must retry | 20-FIC ceiling; [flexible FICs](https://learn.microsoft.com/en-us/entra/workload-id/workload-identities-flexible-federated-identity-credentials) with subject matching are the escape hatch |

**Deprecation you must know:** Microsoft Entra pod-managed identity (AAD Pod Identity) is dead. The open-source project is [archived](https://github.com/Azure/aad-pod-identity), and the AKS managed add-on was documented as patched and supported only through September 2025; the [official migration path](https://learn.microsoft.com/en-us/azure/aks/workload-identity-migrate-from-pod-identity) is Entra Workload ID. If you inherit any AKS estate with `AzureIdentity`/`AzureIdentityBinding` CRDs, that is unsupported technical debt, not a working system. On the AWS side, both IRSA and EKS Pod Identity are current; Pod Identity is the better choice for a fleet of cells because the role trust policy no longer names a specific cluster, which removes an IAM write from the cell-creation critical path.

**The portable shape.** All four mechanisms reduce to the same three steps: (1) the cluster issues a signed JWT for a Kubernetes ServiceAccount, (2) the cloud's STS verifies it against a registered issuer and a subject predicate, (3) the workload receives a short-lived cloud credential. Your cell provisioning interface therefore wants exactly one verb — `BindWorkloadIdentity(namespace, serviceAccount, capability)` — with three implementations. Do **not** expose "IAM role ARN" in the interface; expose "capability", and let each implementation decide whether that becomes a role, a GSA/principal binding, or a user-assigned managed identity plus FIC.

### Cross-cloud federation (control plane in cloud A provisioning cloud B)

This is the piece that makes a multi-cloud control plane possible without a secret-sprawl disaster. All three clouds accept an external OIDC issuer as a trust anchor.

| Direction | Mechanism | Key objects |
|---|---|---|
| → AWS | `AssumeRoleWithWebIdentity` against an [IAM OIDC identity provider](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html) | OIDC provider (issuer URL + audience), role trust policy with `sub`/`aud` conditions. Google's `accounts.google.com` is a built-in provider — no provider object needed |
| → GCP | [Workload Identity Federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation): pool + provider (AWS, OIDC, or SAML), attribute mapping, attribute condition; then either impersonate a service account or bind roles directly to the federated principal | Workload identity pool, provider, attribute mapping expressions |
| → Azure | [Federated identity credential](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations) on a user-assigned managed identity or app registration | FIC (issuer, subject, audience); hard cap of 20 per identity |

**AWS as the source is special.** GCP has a first-class [AWS provider type](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds) that does not use OIDC at all — it validates a signed `GetCallerIdentity` request, meaning an EC2/EKS workload's SigV4 identity is directly usable as a GCP principal. That is materially simpler than minting an OIDC token, and it is the right primitive if your Infra CP runs on AWS and provisions GCP.

**Design guidance for a multi-cloud control plane.**

- Pick one **home cloud** for the control plane and make its workload identity the root of all cross-cloud trust. Two-directional federation doubles the trust surface for no benefit.
- The subject predicate should encode *both* the cluster/namespace and the purpose, so a compromised sidecar cannot borrow the provisioner's identity. On GCP use attribute conditions, on AWS a `StringEquals` on `sub`, on Azure a precise FIC subject.
- Budget around Azure's 20-FIC ceiling early. One identity per *capability* (provisioner, DNS writer, KMS user) with flexible FICs beats one identity per cell, which hits the wall at 20 cells.
- Never provision cloud B with a stored key from cloud B. `iam.disableServiceAccountKeyCreation` and equivalent guardrails should make that impossible by policy rather than by convention.

### Networking: VPC vs VPC vs VNet

| | AWS VPC | GCP VPC network | Azure VNet |
|---|---|---|---|
| Scope | Regional | [Global](https://docs.cloud.google.com/vpc/docs/vpc) | Regional |
| Subnet scope | **One AZ** | [Regional — spans all zones](https://docs.cloud.google.com/vpc/docs/subnets) | Regional — spans all zones |
| Addressing | Primary CIDR + up to 4 secondary CIDRs | Subnet primary range + **secondary ranges** (used by GKE for pods/services) | Address spaces on the VNet; subnets carve them up |
| Expand later | Add secondary CIDRs; cannot resize a subnet | **Can expand a subnet's primary range in place** | Can add address spaces; subnet resize is constrained by neighbors |
| Routing | Route table per subnet; local route non-removable | Global routes with priorities and instance tags | UDR per subnet; system routes plus BGP-learned |
| Cross-region without peering | No | **Yes** — one VPC spans regions | No |
| Default connectivity within network | Subnets route to each other; SG default denies cross-SG | Subnets route to each other; firewall default denies ingress | Subnets route to each other; NSG default rule **`AllowVnetInBound` permits it** |

**The three deltas that reshape a cell blueprint.**

1. **Subnet-per-AZ is an AWS-only tax.** On AWS, an HA cell needs ≥3 subnets per tier (app, data, LB), each with its own route table entry and its own NAT gateway if you want AZ-independent egress. On GCP and Azure, one subnet per tier covers all zones. If your Terraform/CDK module is written as "for each AZ, create a subnet," the GCP implementation is not a port — it is a different module. This is precisely the shape difference Temporal's blog calls out for load balancers, generalized.
2. **GCP's global VPC eliminates a whole category of work.** Multi-region cells on GCP can share one VPC with regional subnets and no peering, no transit gateway, no route propagation. On AWS and Azure the same topology needs TGW/Virtual WAN or a peering mesh. Do not build an abstraction that forces GCP to pretend it needs peering; that throws away the platform's best property.
3. **Azure's default-open intra-VNet posture.** The built-in NSG rules allow inbound from `VirtualNetwork` by default ([NSG docs](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview)). AWS security groups default to "allow nothing inbound." If your security model is "cells are isolated," you must write explicit deny rules on Azure that have no AWS counterpart, and your policy-as-code needs a per-cloud baseline, not a shared one.

### Firewalling: security groups vs firewall rules vs NSGs

| | AWS | GCP | Azure |
|---|---|---|---|
| Primary object | Security group (stateful, allow-only) | [VPC firewall rule / firewall policy](https://docs.cloud.google.com/firewall/docs/firewalls) (stateful, allow **and** deny) | NSG (stateful, allow and deny, priority-ordered) |
| Attach point | ENI / instance | **The VPC**; selects targets by network tag or service account | Subnet and/or NIC (both can apply; both must pass) |
| Stateless layer | [Network ACLs](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-network-acls.html) — stateless, per-subnet, ordered | none | none |
| Reference other groups | Yes — SG can be a source | Indirectly, via service account or tag as source | Application Security Groups (ASGs) as source/destination |
| Hierarchical policy | Firewall Manager (add-on) | Hierarchical firewall policies at org/folder + global/regional network firewall policies | Azure Firewall / Azure Firewall Policy at hub; NSGs remain per-subnet |
| Connection tracking nuance | Stateful; SG changes apply to existing flows | Stateful; tracking entry is [active if ≥1 packet per 10 minutes](https://docs.cloud.google.com/firewall/docs/firewalls), and tracked-connection count is bounded by machine type | Stateful; flow-count limits are per-VM SKU |

**Deltas.** The mental switch that trips people is that GCP has **no per-instance firewall object**. You cannot "attach a security group"; you write a VPC-level rule whose target is a tag or a service account. That is more scalable (no SG-per-service explosion) and less discoverable (a VM's effective policy is a query, not a field). Using the node pool's *service account* as the firewall target — rather than tags — makes the firewall rule and the IAM identity the same key, which is a genuinely nice property for a cell: one identity per cell, and the network policy follows automatically.

AWS is the only cloud with a stateless layer you are likely to trip over; NACLs are per-subnet and require explicit ephemeral-port return rules. They are also the only place where "I allowed it and it still doesn't work" has a fourth possible cause. Most cell designs should leave NACLs at default-allow and do all policy in security groups, reserving NACLs for a coarse emergency kill-switch.

GCP's connection-tracking idle timeout deserves a specific note for Temporal: long-lived gRPC streams with sparse traffic can fall out of the tracking table if idle beyond the window, after which return packets are dropped. Configure gRPC keepalives well inside that window on GCP; the same config is harmless elsewhere.

### Private connectivity (this is how customers reach a cell)

| | AWS PrivateLink | GCP Private Service Connect | Azure Private Link |
|---|---|---|---|
| Producer object | VPC endpoint service fronted by an **NLB** or GWLB | [Service attachment](https://docs.cloud.google.com/vpc/docs/about-vpc-hosted-services) fronted by an internal LB, plus a **NAT subnet** | Private Link Service fronted by a **Standard internal LB**, plus a NAT subnet |
| Consumer object | Interface VPC endpoint (ENIs in consumer subnets) | PSC endpoint = forwarding rule with a consumer-side internal IP | Private Endpoint (NIC in consumer subnet) |
| Approval model | Whitelist consumer principals; auto/manual accept | Explicit consumer project allowlist; auto/manual accept | Auto/manual approval on connection request |
| Client IP visibility | PROXY protocol v2 on the NLB target group | [PROXY protocol v2](https://docs.cloud.google.com/vpc/docs/about-vpc-hosted-services), carrying `pscConnectionId` in a TLV | TCP PROXY v2 with a `LinkIdentifier` TLV |
| DNS | Private DNS name on the endpoint service (requires domain verification) | Consumer creates its own DNS, or uses the service directory / auto-DNS for published services | Private Endpoint DNS integration via `privatelink.*` zones |
| Direction | Unidirectional consumer → producer | Unidirectional, plus PSC **backends** for producer → consumer | Unidirectional consumer → producer |

**Deltas.** All three are structurally the same: producer publishes behind an internal LB, consumer materializes a local IP. The differences are in identity plumbing and in what the producer can see. Temporal's multi-cloud post specifically flags private connectivity as a feature-parity gap on GCP, citing PROXY protocol v2 header limitations — which is the exact class of problem you should expect: the mechanism exists everywhere, but the *tenant-identification* path differs, and per-customer private endpoints require you to map an opaque connection identifier back to a tenant on every cloud with different opaque identifiers.

For cell lifecycle this means: publishing a cell is not one operation. It is "create the internal LB, create the publish object, register the tenant's identity in the allowlist, produce the DNS name, and record the connection-ID→tenant mapping" — five steps whose *shape* is identical and whose *arguments* are entirely different per cloud. Perfect candidate for the verb-level interface.

### Peering, transit, and hub-and-spoke

| | AWS | GCP | Azure |
|---|---|---|---|
| Simple peering | VPC peering — non-transitive, no overlapping CIDRs | [VPC Network Peering](https://docs.cloud.google.com/vpc/docs/vpc-peering) — non-transitive, no overlapping subnets, per-VPC peering limits | VNet peering — non-transitive (but supports gateway transit) |
| Transit hub | [Transit Gateway](https://docs.aws.amazon.com/vpc/latest/tgw/what-is-transit-gateway.html) — regional, attachment + route tables, TGW peering across regions; Cloud WAN above it | [Network Connectivity Center](https://docs.cloud.google.com/network-connectivity/docs/network-connectivity-center/concepts/vpc-spokes-overview) hub with VPC spokes — gives transitivity without a mesh | [Virtual WAN](https://learn.microsoft.com/en-us/azure/virtual-wan/virtual-wan-about) with Microsoft-managed hubs, or DIY hub-and-spoke with Azure Firewall / Route Server |
| Cross-region | TGW peering (extra hop, extra cost) | Native — one global VPC, or NCC | vWAN hub-to-hub, or global VNet peering |
| Who owns routing | You, explicitly, per TGW route table | Mostly the platform; NCC exchanges routes | vWAN hub does it; DIY requires UDRs everywhere |

**Deltas.** GCP mostly does not need this layer for intra-organization traffic, because the VPC is already global — NCC exists for the multi-VPC case (shared services, partner networks). AWS gives the most explicit control and therefore the most rope: TGW route tables are a real routing design exercise, and TGW is regional so a multi-region cell fleet needs a peering topology. Azure Virtual WAN is the most managed and the least controllable; you gain built-in transitivity and lose the ability to insert arbitrary NVAs without using vWAN's specific integration points.

For cells specifically: prefer **not** connecting cells to each other at all. A cell should reach the control plane and its own dependencies, and nothing else. Transit topology is then a control-plane concern, not a cell concern, and stays out of the cell contract.

### Egress and NAT

| | AWS NAT Gateway | GCP Cloud NAT | Azure NAT Gateway |
|---|---|---|---|
| Nature | A **zonal resource** you deploy per AZ | [Not an instance](https://docs.cloud.google.com/nat/docs/overview) — SDN config on the Cloud Router, regional | A zonal or regional resource attached to subnets |
| HA | You build it: one NATGW per AZ, per-AZ route tables | Platform-managed | Zonal by default; zone-redundant via a zone-redundant public IP prefix |
| Port model | ~55,000 ports per unique destination tuple; `ErrorPortAllocation` on exhaustion | Min ports per VM (configurable), plus dynamic port allocation | Ports pre-allocated per public IP (64,512 per IP); scale by adding IPs |
| Pricing shape | Per-gateway-hour **+ per-GB processed** ([pricing](https://aws.amazon.com/vpc/pricing/)) | Per-VM-hour (capped) **+ per-GiB processed** ([pricing](https://cloud.google.com/nat/pricing)) | Per-gateway-hour **+ per-GB processed** ([pricing](https://azure.microsoft.com/en-us/pricing/details/azure-nat-gateway/)) |
| Default outbound if you do nothing | None — private subnet has no egress | None | **Being retired**: [default outbound access](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access) is going away; new VMs need an explicit method |

**Deltas.** The AWS design forces three NAT gateways per cell for AZ independence (or accepts a cross-AZ hop plus cross-AZ data charges). GCP's Cloud NAT is a single regional configuration — there is no per-zone object to forget. Azure sits in between, and additionally has a live migration story: Microsoft is retiring implicit outbound internet access for VMs, so any cell template that relied on "VMs just have internet" is on borrowed time and must declare a NAT Gateway, LB outbound rules, or a public IP.

Port exhaustion looks different on each and produces different symptoms: AWS surfaces `ErrorPortAllocation` CloudWatch metrics, GCP surfaces dropped-packet counters and `nat_allocation_failed`, Azure surfaces SNAT port exhaustion metrics. Any cell that pulls container images, calls S3/GCS/Blob over public endpoints, or scrapes external APIs will find this. The universal fix is the same everywhere and should be in every cell blueprint: **use private endpoints for cloud-provider services so that traffic never hits NAT at all.**

### IP address planning and exhaustion

| | AWS (EKS + VPC CNI) | GCP (GKE) | Azure (AKS) |
|---|---|---|---|
| Pod IP source | **Real VPC IPs** from the node's subnet | Secondary range ("alias IPs") on the subnet | Azure CNI: VNet IPs. **Azure CNI Overlay**: private overlay CIDR, not VNet |
| Density limiter | ENIs × IPs-per-ENI per instance type | Pod CIDR block per node (derived from max-pods-per-node) | Overlay: essentially unbounded; classic CNI: per-node IP reservation |
| Mitigations | [Prefix delegation](https://docs.aws.amazon.com/eks/latest/best-practices/ip-opt.html) (/28 per ENI), custom networking with secondary CIDRs (often 100.64.0.0/10) | Larger secondary ranges; [VPC-native clusters](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/alias-ips) with discontiguous ranges | [Azure CNI Overlay](https://learn.microsoft.com/en-us/azure/aks/azure-cni-overlay) — pods stop consuming VNet space entirely |
| Typical failure | Pods stuck `ContainerCreating`, CNI cannot allocate | Node pool cannot scale because the pod range is exhausted | Classic CNI: subnet exhausted at scale-out |

**Delta that determines your address plan.** AWS is the greediest: every pod burns a routable VPC address, and node scale-out reserves addresses in advance. Azure with overlay is the thriftiest. GCP is in the middle but at least isolates the burn in a secondary range you can size independently. **Plan the cell CIDR against AWS** — the worst case — and let the other two clouds have headroom, rather than sizing for GCP and discovering AWS cannot fit. Concretely: decide max nodes × max pods per node × safety factor, and reserve a per-cell block from a global IPAM that is identical across clouds so a cell's identity implies its address range everywhere. Reusing the same numbering scheme across clouds is one of the few genuinely safe abstractions here.

### DNS

| | AWS Route 53 | GCP Cloud DNS | Azure DNS |
|---|---|---|---|
| Public zones | Public hosted zones | Public managed zones | Azure DNS public zones |
| Private zones | [Private hosted zones](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/hosted-zones-private.html) associated with VPCs (cross-account association is a two-step CLI dance) | [Private managed zones](https://docs.cloud.google.com/dns/docs/zones/zones-overview) bound to VPC networks; **cross-project binding** available | Private DNS zones **linked** to VNets; only one link per VNet may have auto-registration ([docs](https://learn.microsoft.com/en-us/azure/dns/private-dns-virtual-network-links)) |
| Forwarding / hybrid | [Resolver inbound/outbound endpoints](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver.html) (ENIs, billed per endpoint-hour + queries), rules shareable via RAM | Forwarding zones, **DNS peering** (limited to a single transitive hop), response policies | Azure DNS Private Resolver (inbound/outbound endpoints, rulesets) |
| Split-horizon | PHZ overrides public for associated VPCs | Private zone shadows public within the bound network | Private zone shadows public within linked VNets |
| Service integration | VPC endpoint private DNS names | PSC auto-DNS / Service Directory | **`privatelink.*` zones** — the canonical Azure footgun |

**Deltas.** Route 53 and Cloud DNS both bind a zone to *networks*; Azure binds a zone to VNets via a separate `virtualNetworkLink` child resource, and the auto-registration restriction (one auto-registering link per VNet) surprises people building per-cell zones. GCP's DNS peering is the cleanest way to let a cell resolve control-plane names without full network peering, but note the single-transitive-hop limit — a chain of more than three VPCs will not resolve.

The Azure private-endpoint DNS pattern is worth memorizing because it has no AWS/GCP analogue: when you create a Private Endpoint for a PaaS service, the public name CNAMEs to a `privatelink.<service>.<suffix>` name that only resolves correctly if a private DNS zone with that exact name is linked to your VNet. Miss the link, and the name resolves to the public IP, and the traffic silently leaves the private path — a correctness *and* compliance bug that produces no error.

### Load balancing (and the gRPC problem)

| | AWS | GCP | Azure |
|---|---|---|---|
| L4 | NLB (zonal IPs; cross-zone **off by default**) | Internal/external **passthrough** Network Load Balancer | Standard Load Balancer (no TLS termination) |
| L7 regional | ALB | Regional external/internal Application Load Balancer | Application Gateway v2 (+ WAF) |
| L7 global | CloudFront / Global Accelerator (separate products) | **Global external Application Load Balancer** with anycast IP | Front Door (anycast) |
| Resource graph | LB → listener → target group | [Forwarding rule](https://docs.cloud.google.com/load-balancing/docs/forwarding-rule-concepts) → target proxy → URL map → [backend service](https://docs.cloud.google.com/load-balancing/docs/backend-service) → NEG/MIG | LB/AppGW → frontend IP → listener → rule → backend pool |
| gRPC end-to-end | **ALB with target group protocol version `GRPC`** ([docs](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html)); NLB passes it through at L4 | Backend service protocol `HTTP2` or `GRPC`; **gRPC backends require gRPC or TCP health checks**, not HTTP ([docs](https://docs.cloud.google.com/load-balancing/docs/health-check-concepts)) | App Gateway v2 talks **HTTP/1.1 to backends**; Front Door likewise ([FAQ](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-faq)) — so **no end-to-end gRPC**. [Application Gateway for Containers](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/application-gateway-for-containers-components) does gRPC over HTTP/2 to backends |
| Health check ownership | Target group | Standalone health check resource, referenced by backend service | Probe resource on the LB/AppGW |

**The gRPC problem, stated precisely.** Temporal's frontend is gRPC, i.e. long-lived HTTP/2 connections carrying many multiplexed streams. An L4 load balancer distributes *connections*, not *requests* — so a handful of clients with persistent connections produce badly skewed backend load, and a new frontend pod added by an HPA receives nothing until clients reconnect. You need either (a) an L7 proxy that does per-request HTTP/2 balancing, or (b) client-side load balancing with a resolver that sees all endpoints.

That constraint lands differently on each cloud:

- **AWS**: ALB with `ProtocolVersion=GRPC` does real per-request gRPC routing, including routing by package/service/method, and needs a gRPC-format health check path. This is the easy cloud.
- **GCP**: set the backend service protocol to `HTTP2` or `GRPC` and use a gRPC health check; the global ALB then does per-request balancing across regions on an anycast IP. Also easy, but the resource graph is four objects deep, and the health-check protocol mismatch is a common outage cause.
- **Azure**: the managed general-purpose L7s do **not** speak HTTP/2 to backends. Your options are Application Gateway for Containers (newer, ALB-controller-driven, Gateway API), or running your own Envoy/NGINX ingress behind a Standard Load Balancer. Practically, on Azure a Temporal-shaped cell will run its own L7 proxy inside the cluster, which is a structural difference from AWS/GCP — different failure modes, different upgrade story, different capacity model.

Two more asymmetries worth encoding in the cell contract: NLB **cross-zone load balancing is disabled by default** (deliberately, to avoid inter-AZ data-transfer charges) while ALB's is always on and not separately charged; and GCP's global LB is a genuinely different product class — a single anycast IP fronting backends in many regions with no DNS failover involved — that has no AWS/Azure equivalent at the load-balancer layer.

*See also: [the biggest infra gotcha: L4 load balancing pins gRPC](02-grpc.md#the-biggest-infra-gotcha-l4-load-balancing-pins-grpc) for the client-side half of this — `dns:///` plus `round_robin` over a headless Service, `MaxConnectionAge` to break connection stickiness, and the LB idle timeouts that silently kill long polls.*

### Compute for Kubernetes nodes

| | AWS EC2 | GCP Compute Engine | Azure VMs |
|---|---|---|---|
| Sizing model | Fixed instance types within families (m/c/r/i/g/p), Nitro platform | Predefined machine types **plus custom machine types** (arbitrary vCPU/memory within limits) | Fixed VM SKUs within series (D/E/F/L/N), several generations live at once |
| Interruptible | Spot Instances, per-AZ pricing, 2-minute notice | [Spot VMs](https://docs.cloud.google.com/compute/docs/instances/spot) — no max runtime; legacy [preemptible VMs](https://docs.cloud.google.com/compute/docs/instances/preemptible) **always stop at 24h** | Spot VMs with eviction policy and max price |
| Reservation | On-Demand Capacity Reservations; [Capacity Blocks for ML](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html) (GPU clusters, 1–64 instances, reservable in advance) | Reservations (specific or any-project), committed use discounts | Capacity reservations, reserved instances |
| Quota unit | **vCPU-based On-Demand limits, grouped into a handful of family classes** (Standard; F; G; P; X), plus separate Spot limits, per region ([docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-on-demand-instances.html)) | Regional `CPUS` quota plus **per-family** CPU quotas; Spot/preemptible have their own ([docs](https://docs.cloud.google.com/compute/resource-usage)) | **Two-tier**: Total Regional vCPUs *and* per-VM-family vCPUs; both must pass ([docs](https://learn.microsoft.com/en-us/azure/virtual-machines/quotas)) |
| Zone identity | AZ **names are per-account**; AZ **IDs** (`use1-az1`) are stable across accounts | Zone names are globally consistent (`us-central1-a` is the same everywhere) | Logical zones 1/2/3 are **mapped per subscription** to physical zones ([reliability docs](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-overview)) |

**Deltas.**

- **Quota shape drives your node-pool strategy.** Azure's two-tier model means a cell that mixes D-series and E-series nodes needs *three* quota increases (two families plus regional total). AWS's coarse "Standard family class" bucket means most general-purpose changes need no new request. GCP sits between, with per-family quotas on newer families. A multi-cloud capacity planner cannot use one data model for this; it needs three, behind one `EnsureCapacity(cell, shape)` verb.
- **Zone identity is not portable.** `us-east-1a` in account A and account B are different physical zones — you must use AZ IDs to correlate across accounts. Azure's logical-zone mapping is per subscription, and the physical mapping is exposed through the availability-zone mappings API. GCP is the only cloud where the zone name means the same thing everywhere. If your cell metadata records "zone a," it is meaningless on AWS and Azure across tenancy boundaries. Record the AZ ID / physical zone.
- **Preemptible vs Spot on GCP is a real trap.** Legacy preemptible VMs terminate at 24 hours unconditionally. If any node pool template still specifies `preemptible: true` rather than `provisioningModel: SPOT`, your nodes churn daily regardless of demand.
- **Custom machine types are GCP-only.** They are genuinely useful for right-sizing Temporal history/matching nodes, and they are exactly the kind of per-cloud advantage that a lowest-common-denominator abstraction destroys.

### Kubernetes as the cell substrate: EKS vs GKE vs AKS

The cell is a Kubernetes cluster, so the managed-Kubernetes deltas *are* cell lifecycle deltas.

| | EKS | GKE | AKS |
|---|---|---|---|
| Node abstraction | Managed node groups, self-managed groups, Karpenter, Fargate | Node pools; Standard and Autopilot modes; node auto-provisioning | Node pools backed by VM Scale Sets; **system** and **user** pool distinction |
| Default pod networking | AWS VPC CNI — pods get routable VPC IPs | Alias IP secondary ranges | Choice of Azure CNI (VNet IPs) or [Azure CNI Overlay](https://learn.microsoft.com/en-us/azure/aks/azure-cni-overlay) |
| LB provisioning from a Service | Requires the AWS Load Balancer Controller and **three annotations** for a public NLB | **One annotation** for a backend-service-based passthrough NLB | Built-in cloud provider; ingress requires your own controller or Application Gateway for Containers |
| Upgrade cadence | Standard support window per version, then a **more expensive extended-support** per-cluster rate ([pricing](https://aws.amazon.com/eks/pricing/)) | Release channels with automatic control-plane and node upgrades | Auto-upgrade channels; Free tier has no uptime SLA, Standard does ([tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)) |
| Control-plane cost | Per cluster-hour | Per cluster-hour, with a free-tier allowance | Free tier (no SLA) or Standard tier |
| Add-on model | EKS add-ons (VPC CNI, CoreDNS, kube-proxy, EBS CSI, Pod Identity Agent) | GKE-managed components, mostly not user-versioned | AKS add-ons and cluster extensions |

The three-annotations-versus-one-annotation contrast is not a trivia item — it is [the exact example Temporal used](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability) to explain why Kubernetes standardizes less than people assume. The Service object is portable; the annotations that make it produce a correctly configured load balancer are not, and neither are the IAM permissions the controller needs, nor the health-check semantics, nor the draining behavior on rolling update.

The upgrade model is the bigger operational delta. GKE's release channels will upgrade your control plane on Google's schedule unless you pin and manage maintenance windows and exclusions; EKS will not upgrade you but will start charging an extended-support premium when your version ages out; AKS sits in between with auto-upgrade channels you opt into. For a fleet of cells this means the *pacing* of upgrades is partly out of your hands on GCP and entirely in your hands (with a cost clock) on AWS. Your cell upgrade workflow must model "the platform may upgrade this cell for us" as a real state transition on GCP, and must model "this cell's version is aging into a billing cliff" on AWS.

Two data-plane facts worth pinning down before you design against them. **GKE Dataplane V2** (eBPF, Cilium-based, `anetd` DaemonSet) is *"enabled by default for all new Autopilot clusters"* — not for Standard clusters, where you opt in at creation time. The constraint that matters for cell provisioning: *"GKE Dataplane V2 can only be enabled when creating a new cluster. Existing clusters cannot be upgraded to use GKE Dataplane V2"* ([Dataplane V2](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2)), so the data-plane choice is a cell-creation decision you cannot revisit without a rebuild. On Azure, **kubenet retires on 31 March 2028**, with migration to Azure CNI Overlay required ([AKS legacy CNI](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni)) — also a rebuild rather than an in-place change.

*See also: [version and support windows: the big comparison](04-managed-kubernetes-eks-gke-aks.md#version-and-support-windows-the-big-comparison) for the exact months, current supported minors, and cost multipliers behind "the pacing of upgrades is partly out of your hands."*

### Storage: block and object

**Block:**

| | AWS EBS | GCP PD / Hyperdisk | Azure Managed Disks |
|---|---|---|---|
| Performance model | gp3 decouples IOPS/throughput from size; io2 for high IOPS | PD performance scales with **size and vCPU count**; [Hyperdisk](https://docs.cloud.google.com/compute/docs/disks/hyperdisk-performance) decouples IOPS/throughput from size | Premium SSD scales by disk tier; Premium SSD v2 decouples IOPS/throughput ([docs](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types)) |
| Attachment limit | Most Nitro instances: **28 attachments shared** across ENIs, EBS, and instance-store NVMe; newer generations have **dedicated** EBS limits from 32 up to 128 by size ([docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/volume_limits.html)) | Per-machine-type disk limits; aggregate performance capped by the VM's own limit regardless of per-disk provisioning | **Max data disks varies by VM size** — published per SKU in the VM size tables |
| Zonality | **Zonal only** | Zonal, plus **Regional PD (synchronous replication across 2 zones)** and Hyperdisk Balanced High Availability | Zonal, plus **ZRS disks** for some types/regions |
| Resize | Grow online, never shrink | Grow online, never shrink | Grow online (some types need a stop), never shrink |
| Snapshot scope | Regional, incremental | **Multi-regional / global** snapshots | Regional, incremental |

The delta that changes Temporal-shaped architecture: **AWS has no cross-zone block device.** GCP Regional PD and Azure ZRS disks let a stateful pod's volume survive a zone loss and reattach in a surviving zone. On AWS, a Cassandra or Postgres node's data is pinned to one AZ, and the answer must be replication at the database layer plus a rebuild path. If you write the cell's stateful-set recovery logic against GCP's regional disks and then port it, AWS will not have the primitive. Design for the AWS constraint (replicate at the data layer) and treat regional disks as an optimization, not a dependency.

Also note the AWS shared-attachment limit: on most Nitro instances, ENIs, EBS volumes, and instance-store devices all draw from the same pool of 28. A node with several ENIs (which the VPC CNI creates) has correspondingly fewer EBS slots. On a Kubernetes node running many PVC-backed pods, that is the actual pod-density ceiling and it is invisible until pods hang in `ContainerCreating`.

**Object:**

| | S3 | Cloud Storage | Azure Blob |
|---|---|---|---|
| Namespace | **Global** bucket names | **Global** bucket names | Names scoped to a **storage account**; the account name is global |
| Consistency | [Strong read-after-write since Dec 2020](https://aws.amazon.com/about-aws/whats-new/2020/12/amazon-s3-now-delivers-strong-read-after-write-consistency-automatically-for-all-applications) | Strongly consistent | Strongly consistent |
| Authorization | IAM + bucket policy + Block Public Access + optional ACLs | IAM (+ uniform bucket-level access, which disables ACLs) | Azure RBAC data-plane roles, **or** account keys, **or** SAS |
| Scoping unit for limits | Bucket / account request rates | Bucket | **Storage account** — the throughput and IOPS unit |

Two Azure-specific traps: (1) a storage **account** is the performance and limits unit, so "one container per cell" inside a shared account shares a throughput budget while "one account per cell" hits per-subscription account limits; (2) Azure RBAC **control-plane** roles like Contributor do not grant data access — you need explicit data-plane roles (`Storage Blob Data Contributor`) or you fall back to account keys, which is exactly the static-credential pattern you are trying to eliminate.

### Managed databases for Temporal persistence

Temporal Server persistence supports [Cassandra, MySQL, and PostgreSQL](https://docs.temporal.io/temporal-service/persistence), with advanced Visibility on MySQL 8.0.17+ / PostgreSQL 12+, and Elasticsearch-compatible search for the visibility store in larger deployments.

| Need | AWS | GCP | Azure |
|---|---|---|---|
| Managed PostgreSQL | RDS for PostgreSQL; Aurora PostgreSQL (separated storage, fast failover) | Cloud SQL for PostgreSQL; [AlloyDB](https://cloud.google.com/alloydb/docs/overview) for higher throughput | Azure Database for PostgreSQL **Flexible Server** — Single Server was [retired March 2025](https://learn.microsoft.com/en-us/azure/postgresql/migrate/whats-happening-to-postgresql-single-server) |
| Managed MySQL | RDS / Aurora MySQL | Cloud SQL for MySQL | Azure Database for MySQL Flexible Server |
| Managed Cassandra | Amazon Keyspaces (**API-compatible, not Cassandra**) | **No first-party managed Cassandra** | [Azure Managed Instance for Apache Cassandra](https://learn.microsoft.com/en-us/azure/managed-instance-apache-cassandra/introduction) — real OSS Cassandra |
| Self-managed Cassandra | EC2 + local NVMe | GCE + local SSD | VMs + Premium/Ultra disks |
| Visibility (ES-compatible) | Amazon OpenSearch Service | **No first-party ES-7-compatible service** — Temporal used a third-party vendor | Elastic Cloud on Azure / self-managed |
| Global SQL | Aurora Global Database | Spanner (different SQL dialect and semantics — not a Temporal target) | Cosmos DB (not a Temporal SQL target) |

**Deltas that matter.**

- **Amazon Keyspaces is not a drop-in Cassandra.** It has [documented functional differences](https://docs.aws.amazon.com/keyspaces/latest/devguide/functional-differences.html) — no materialized views, different secondary-index behavior, different capacity accounting for lightweight transactions (failed LWT condition checks still consume write units). Temporal leans on LWTs; validate against the actual workload before treating Keyspaces as an option rather than assuming CQL compatibility is sufficient.
- **Azure is the only cloud with a first-party managed *open-source* Cassandra.** That is a genuine Azure advantage for a Temporal-shaped workload and an example of why "pick the same database everywhere" is the wrong instinct.
- **The visibility store is the real portability gap**, and Temporal's own blog says so: OpenSearch on AWS had no directly ES-7-compatible GCP equivalent, forcing a different vendor and therefore a genuinely different deployment workflow. Expect the same class of problem on Azure. Encode this as an interface method (`DeployVisibilityPersistenceStore`) whose implementations are allowed to differ radically, including "call a third party's API."
- **HA semantics differ.** Aurora's storage-layer replication, Cloud SQL's regional HA with a standby, AlloyDB's cluster model, and Flexible Server's zone-redundant vs same-zone HA all have different failover times and different behavior under zone loss. The cell's SLO math is not portable; measure per cloud.

### KMS, secrets, and what they back

| | AWS | GCP | Azure |
|---|---|---|---|
| Key service | KMS (CMKs, key policies are authoritative) | Cloud KMS (key rings are **location-scoped**) | Key Vault (vault, or Managed HSM) |
| Envelope encryption | Generate data key → encrypt locally → store wrapped DEK | Same pattern; `encrypt`/`decrypt` on a CryptoKey | Same pattern; wrap/unwrap key operations |
| Rotation | [Automatic rotation, configurable 90–2560 days](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html); default ≈365 days; symmetric keys only | [Rotation schedule](https://cloud.google.com/kms/docs/key-rotation) creates a new primary version; old versions retained for decrypt | Rotation policy on keys; versions retained |
| Deletion | Scheduled deletion with a 7–30 day waiting period | **Key rings and keys cannot be deleted**; only key *versions* can be destroyed | **Soft delete is on by default**; purge protection can make deletion irreversible for the retention period |
| Secrets | Secrets Manager (rotation Lambdas) / SSM Parameter Store | Secret Manager (versions, replication policy: automatic or user-managed) | Key Vault secrets (same vault, same soft-delete semantics) |

**Deltas that hit cell teardown specifically.** All three clouds make key material deliberately hard to delete, and each does it differently. GCP key rings are permanent — a cell whose key ring is named after the cell can never fully disappear, and recreating a cell with the same name will collide with a live key ring. Azure Key Vault soft-delete means a vault name is reserved for the retention window (commonly 90 days) after deletion, so create-destroy-recreate cycles in CI fail on name collision unless you purge or use generation-suffixed names. AWS's pending-deletion window is the mildest but still means a same-named alias may conflict. **Every cell resource-naming scheme needs a generation counter, and the cell teardown workflow needs an explicit "soft-deleted residue" step** that either purges or records the tombstone.

**How these back the rest of the cell:**

- **Vault auto-unseal.** HashiCorp Vault's [seal configuration](https://developer.hashicorp.com/vault/docs/configuration/seal) supports `awskms`, `gcpckms`, and `azurekeyvault` stanzas. The Vault pods get their cloud credential from the workload-identity mechanism above, then Vault asks the KMS to decrypt its wrapped root key on every start. The portable verb is `ProvisionUnsealKey(cell) → seal-config`; the implementations differ in stanza name, in the identity binding, and in whether the key is deletable (see above).
- **cert-manager.** ACME DNS-01 solvers exist for [Route 53, Cloud DNS, and Azure DNS](https://cert-manager.io/docs/configuration/acme/dns01/). Each needs write permission on the zone, delivered through the same workload-identity path. Note that this is one of the few places where the cell needs *write* access to a shared, cross-cell resource (the DNS zone), which argues for a per-cell subdomain delegated to a per-cell zone rather than a shared zone with per-cell record permissions — record-level authorization is weak-to-absent on all three.

*See also: [auto-unseal with cloud KMS — and the bootstrap dependency it creates](10-vault.md#auto-unseal-with-cloud-kms--and-the-bootstrap-dependency-it-creates) for why a regional KMS outage seals every Vault in the region on restart, and why recovery keys are not unseal keys.*

### Observability plumbing

| | AWS | GCP | Azure |
|---|---|---|---|
| Logs | CloudWatch Logs (log groups, subscription filters) | Cloud Logging (`_Default`/`_Required` buckets, **Log Router sinks**) | Azure Monitor Logs → Log Analytics workspace, via **Data Collection Rules** |
| Metrics | CloudWatch Metrics; Managed Prometheus (AMP) | Cloud Monitoring; Managed Service for Prometheus | Azure Monitor Metrics; Azure Monitor managed Prometheus |
| Cost shape | Per-GB ingest + storage + per-metric ([pricing](https://aws.amazon.com/cloudwatch/pricing/)) | Per-GiB ingest with a monthly free allowance; storage included for a retention window ([pricing](https://cloud.google.com/stackdriver/pricing)) | Per-GB ingest with **table plans** — Analytics is materially pricier than Basic/Auxiliary ([pricing](https://azure.microsoft.com/en-us/pricing/details/monitor/)) |
| Why you cannot avoid it | Control-plane events (ASG, EKS, ELB health) exist only here | GKE/autoscaler/LB events exist only here | AKS/VMSS/LB events exist only here |

You will run your own metrics and logging pipeline for the cell's application telemetry. You will *also* be forced to integrate with each cloud's native stack, because platform-side events — node lifecycle, load balancer health transitions, quota denials, managed-database failovers — are only emitted there. Treat that as a fourth interface method (`ExportPlatformEvents`) rather than as "we don't use CloudWatch." The one architectural instruction: check Azure's per-GB default tier before you route pod logs into Log Analytics; the default Analytics plan is the most expensive of the three by a wide margin, and it is easy to accidentally 10× a cell's observability bill by using the same shipping config everywhere.

### Quotas, limits, and regional capacity

This is the operational tax nobody budgets for.

| | AWS | GCP | Azure |
|---|---|---|---|
| API | Service Quotas | Cloud Quotas (quota preferences) | Quota / Usages APIs, per provider |
| Granularity | Per account × region; some quotas not adjustable | Per project × region; some org-level | Per subscription × region; two-tier for vCPUs |
| Increase workflow | Automated for many, support case for the rest | Automated for many, support case for the rest | Automated for many, support case for the rest |
| Capacity ≠ quota | Yes — `InsufficientInstanceCapacity` even with quota | Yes — `ZONE_RESOURCE_POOL_EXHAUSTED` | Yes — `AllocationFailed` / `ZonalAllocationFailed` |

**The multi-cloud reality.** Your quota surface is (clouds) × (regions) × (tenancy units) × (quota families). A cell-per-account/project/subscription model multiplies the last factor by the number of cells. Three practices are non-negotiable:

1. **Quota provisioning is a step in the cell-creation workflow, not a prerequisite done by hand.** Request quota programmatically, poll for approval, and let the workflow wait — this is exactly the kind of long-running, human-in-the-loop step Temporal is good at.
2. **Monitor headroom, not usage.** Alert on "cell X is within 20% of its per-family vCPU quota in region Y," per cloud, continuously.
3. **Distinguish quota failures from capacity failures in your retry logic.** A quota error should page or open a request; a capacity error should retry in another zone or another instance shape. Conflating them produces either infinite retries or spurious pages. Each cloud's error taxonomy differs, so the classification lives in the per-cloud implementation and the *decision* lives in the shared workflow.

### Control-plane API behavior: consistency, idempotency, throttling

Cell lifecycle is mostly a long sequence of cloud API calls executed by workflows. The APIs themselves differ in ways that determine how you write activities.

| | AWS | GCP | Azure |
|---|---|---|---|
| Long-running operations | Mostly synchronous create + poll a describe call for state | Explicit **Operation** resources you poll by name | Explicit `Azure-AsyncOperation` / `Location` header polling; ARM deployments as first-class objects |
| Consistency | IAM and some other services are **eventually consistent**; a just-created role may not be usable for seconds | Generally read-your-writes within a project, but IAM propagation is not instant | Entra ID → ARM propagation is the slow path; role assignment right after principal creation commonly fails |
| Idempotency | Client tokens on some APIs (`ClientToken`, `Idempotency-Token`), not universal | `requestId` on many mutating calls | ARM deployments are declarative and re-appliable; raw resource PUTs are idempotent by URI |
| Throttling signal | `ThrottlingException` / `RequestLimitExceeded`, per-API token buckets | `RESOURCE_EXHAUSTED` with rate quotas separate from allocation quotas | HTTP 429 with `Retry-After`, per-resource-provider limits |
| Deletion | Mostly synchronous with dependency errors | Operations you poll; some resources are undeletable (KMS) | Cascading RG delete; soft-deleted services linger |

**What this means for workflow design.** Three rules that hold on all three clouds and are worth encoding once in your activity framework:

1. **Every create is "create-or-adopt," never "create."** Activities are retried; a retried create must find the existing resource and return it rather than failing on conflict. Where a native idempotency token exists, use it; where it does not (which is most of the time), implement adopt-by-name using a deterministic name derived from the cell ID and generation.
2. **Separate "not ready yet" from "will never be ready."** A `PrincipalNotFound` right after creating an Azure identity is transient; a `PrincipalNotFound` for an identity you never created is terminal. Retrying the second forever is how a cell-create workflow silently hangs for hours. Classify errors per cloud in the provider implementation and surface a single retryable/terminal decision to the shared workflow.
3. **Poll operations, do not sleep.** GCP and Azure hand you an operation handle; use it. On AWS, poll the describe call with backoff. Fixed sleeps are the single most common cause of both flaky provisioning and needlessly slow provisioning, and they are invisible in metrics because the workflow "succeeded."

The reason this matters more in multi-cloud than single-cloud: the *same* logical step has different failure vocabularies, so the classification logic cannot live in the shared workflow. If you find yourself writing `if err.Contains("PrincipalNotFound")` in a cloud-agnostic workflow, the abstraction boundary is in the wrong place.

### Cost model differences that change architecture

| | AWS | GCP | Azure |
|---|---|---|---|
| Cross-AZ (intra-region) VM↔VM | **Charged, both directions** (per-GB each way) — see [VPC pricing](https://aws.amazon.com/vpc/pricing/) | **Charged** inter-zone within a region; same-zone internal-IP traffic free ([network pricing](https://cloud.google.com/vpc/network-pricing)) | **Not charged** — Microsoft removed inter-AZ data transfer fees ([bandwidth pricing](https://azure.microsoft.com/en-us/pricing/details/bandwidth/)) |
| Cross-region | Charged | Charged, tier-dependent | Charged |
| Internet egress | Charged above a free monthly allowance | Charged; **Premium vs Standard network tier** changes both price and path | Charged above a free monthly allowance |
| NAT | Hourly + per-GB | Per-VM-hour (capped) + per-GiB | Hourly + per-GB |
| LB | LCU-based (dimension maxima) | Forwarding rules + data processing + tier egress | LB rules + processed data; App Gateway capacity units |
| K8s control plane | [Per cluster-hour](https://aws.amazon.com/eks/pricing/) | [Per cluster-hour](https://cloud.google.com/kubernetes-engine/pricing), with a free-tier allowance | [Free tier without an uptime SLA, or Standard tier with one](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers) |
| Log ingest | Moderate per-GB | Moderate per-GB, generous free allowance | Default Analytics plan is the priciest; Basic/Auxiliary plans much cheaper |

Do not quote exact figures from memory in design docs — link the pricing pages, which change. What *is* stable enough to design around:

- **Zone-aware routing is worth real money on AWS and GCP, and worth nothing on Azure.** Kubernetes topology-aware routing, NLB cross-zone left off, and zone-local Cassandra reads are AWS/GCP optimizations. Applying the same "keep traffic in-zone" pressure on Azure buys you availability-zone fragility for no financial return. This is a case where the *right* architecture genuinely differs per cloud, and forcing uniformity is the expensive choice.
- **NAT gateway shape differs enough to change topology.** AWS charges per gateway-hour and you need one per AZ; three idle NAT gateways per cell is a real fixed cost at fleet scale. GCP's per-VM-hour model caps out and has no per-AZ multiplication. Use private/VPC endpoints aggressively on AWS specifically.
- **Cell fixed cost is the number to watch.** Control plane hour + NAT + LB + KMS keys + DNS zones + log workspace = the floor cost of an empty cell. Compute it per cloud before you commit to a cell-per-customer model; the floor differs by more than people expect, and it determines your minimum viable cell size.

### Portability strategy: what to abstract, what to leave alone

This is the section that decides whether the codebase is maintainable in two years.

**Abstract the verbs. Do not abstract the nouns.** A verb is an operation the cell lifecycle needs — "give this cell durable persistence," "expose this cell's frontend," "bind this workload to this capability." A noun is a cloud resource — a load balancer, a subnet, a managed identity. Verbs have stable semantics across clouds. Nouns do not: AWS's load balancer *has* listeners, GCP's forwarding rule *is attached to* a proxy that references a URL map that references a backend service. There is no honest shared type.

| Layer | Abstract it? | Why |
|---|---|---|
| Cell contract (endpoints, identity, persistence handles, telemetry, teardown state) | **Yes** — this is the product | It is what everything else in the platform depends on; keep it stable and versioned |
| Provisioning verbs (`DeployPersistenceStore`, `BindWorkloadIdentity`, `PublishPrivateEndpoint`) | **Yes** | Same semantics everywhere; wildly different implementations |
| Naming, tagging/labeling, IPAM allocation, cell ID → resource name derivation | **Yes** | Genuinely cloud-independent, and the highest-value thing to make uniform |
| Error classification (retryable / terminal / capacity / quota) | **Interface yes, implementation no** | The decision is shared; the vocabulary is per-cloud |
| Conformance tests ("is this cell actually correct?") | **Yes** | One suite, three implementations of the probes; every difference it finds is a real design decision |
| Resource graphs (LB topology, network topology, node pool shapes) | **No** | Shapes differ structurally; forcing them together produces the worst of all three |
| IAM policy documents | **No** | Trust-inside-the-role vs principal-string vs child-resource are not the same object |
| Quota and capacity semantics | **No** | Three different data models; normalize *headroom* for alerting, not the model itself |
| Load balancer feature sets | **No** | Global anycast on GCP, gRPC-to-backend on AWS, in-cluster Envoy on Azure — these are different architectures |
| Disk performance and zonality | **No** | Regional PD and ZRS exist on two clouds and not the third; a shared type would erase them |
| DNS topology | **No** | Zone-to-network binding models differ enough that the "portable" version is wrong everywhere |
| Managed database selection | **No** | Managed OSS Cassandra exists on Azure, not GCP; Keyspaces is not Cassandra |

**The standard failure mode of lowest-common-denominator abstractions.** It goes like this, every time:

1. Someone builds `type CloudLoadBalancer struct { Scheme, Port, TargetGroup }` because all three clouds "have load balancers."
2. It works, because the first two use cases are simple.
3. A requirement arrives that only one cloud supports natively — end-to-end gRPC, or a global anycast IP, or a regional disk. It does not fit the struct.
4. An escape hatch appears: `ProviderSpecific map[string]string`, or an `if cloud == "azure"` inside the shared code.
5. Within a year the shared code has more conditionals than the three separate implementations would have had lines, and the "portable" struct is a lossy union that nobody can reason about.
6. During an incident, the abstraction is the thing standing between the on-call engineer and the actual cloud resource, and they bypass it — which means the abstraction is now also *wrong*, because manual changes have drifted from it.

The failure is not that the abstraction was leaky. It is that it abstracted at the wrong altitude: over nouns whose models genuinely differ, instead of over verbs whose *intent* is genuinely shared. Temporal's published approach avoids this by making the interface a list of lifecycle operations and letting each provider implementation be an entirely separate set of workflows — the AWS implementation is allowed to deploy OpenSearch while the GCP implementation calls a third-party vendor's API, and the cloud-agnostic parent workflow neither knows nor cares.

**Practical rules to hold the line.**

- **Adding a method to the provider interface must break compilation for every cloud.** That is the feature-parity forcing function. Never add a default implementation that silently no-ops.
- **Allow capability flags in the cell contract.** Some clouds will lack a feature for a while — that is normal and should be representable, not hidden. The user-facing control plane can then decline to offer the feature rather than failing at provision time.
- **Push cloud-specific richness *down*, not *out*.** If GCP's global load balancer lets you serve one anycast IP across regions, use it in the GCP implementation. Do not remove it because AWS cannot match it, and do not surface it into the shared contract as an optional field nobody else sets.
- **Write the conformance suite before the second cloud.** Otherwise "it works on GCP" means "it passes the tests we wrote while thinking about AWS."
- **Let Terraform/Pulumi be per-cloud.** IaC tools intentionally mirror each provider's resource model; trying to write one module for three clouds fights the tool. Share the *inputs* (cell ID, CIDR, sizing, tags) and the *outputs* (the cell contract), not the module body.

---

## Hands-on

These labs are deliberately cheap. **Read the cost notes before running anything.** The rule: networks, subnets, firewall rules, IAM, and federation configs are free on all three clouds; gateways, load balancers, managed control planes, and anything with an hourly rate are not.

### Setup

```bash
# AWS
aws --version
aws configure sso            # or aws configure
aws sts get-caller-identity

# GCP
gcloud version
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT

# Azure
az version
az login
az account set --subscription YOUR_SUBSCRIPTION_ID
```

**Cost: free.** Set up budget alerts before anything else:

```bash
# GCP: budgets are on the billing account
gcloud billing budgets list --billing-account=BILLING_ACCOUNT_ID
# Azure
az consumption budget list
# AWS: Budgets console, or
aws budgets describe-budgets --account-id "$(aws sts get-caller-identity --query Account --output text)"
```

### Lab 1 — Read the hierarchy three ways

```bash
# AWS: org tree
aws organizations list-roots
aws organizations list-organizational-units-for-parent --parent-id r-xxxx
aws organizations list-accounts

# GCP: org tree
gcloud organizations list
gcloud resource-manager folders list --organization=ORG_ID
gcloud projects list --filter="parent.id=FOLDER_ID"

# Azure: MG tree + subscriptions
az account management-group list -o table
az account management-group show --name MG_NAME --expand --recurse
az account list -o table
```

**Cost: free.** What to notice: AWS gives you accounts with no sub-container; GCP gives you projects with an immutable ID; Azure gives you subscriptions *plus* resource groups, and the resource group has a `location` that is not where its contents necessarily live.

### Lab 2 — The same network, three ways (the subnet-scope lesson)

```bash
# --- AWS: one VPC, THREE subnets (one per AZ) ---
VPC=$(aws ec2 create-vpc --cidr-block 10.10.0.0/16 \
  --query Vpc.VpcId --output text)
for i in 0 1 2; do
  AZ=$(aws ec2 describe-availability-zones \
        --query "AvailabilityZones[$i].ZoneName" --output text)
  aws ec2 create-subnet --vpc-id "$VPC" \
    --cidr-block "10.10.$i.0/24" --availability-zone "$AZ"
done
# Note the AZ *ID*, not the name — names are per-account aliases:
aws ec2 describe-availability-zones \
  --query "AvailabilityZones[].{Name:ZoneName,Id:ZoneId}" --output table
```

```bash
# --- GCP: one GLOBAL VPC, ONE regional subnet that spans all zones ---
gcloud compute networks create cell-lab --subnet-mode=custom
gcloud compute networks subnets create cell-lab-usc1 \
  --network=cell-lab --region=us-central1 --range=10.20.0.0/20 \
  --secondary-range=pods=10.24.0.0/14,services=10.28.0.0/20
gcloud compute networks subnets describe cell-lab-usc1 --region=us-central1
```

```bash
# --- Azure: one regional VNet, ONE subnet that spans all zones ---
az group create -n rg-cell-lab -l eastus2
az network vnet create -g rg-cell-lab -n vnet-cell-lab \
  --address-prefix 10.30.0.0/16 \
  --subnet-name snet-nodes --subnet-prefix 10.30.0.0/20
# Logical zone 1 is NOT the same physical zone across subscriptions:
az rest --method get --url \
  "https://management.azure.com/subscriptions/$(az account show --query id -o tsv)/locations?api-version=2022-12-01" \
  --query "value[?name=='eastus2'].availabilityZoneMappings"
```

**Cost: free** (VPCs, VNets, subnets, and resource groups carry no charge). **Do not** create a NAT gateway, load balancer, or managed cluster in this lab — those bill hourly.

**Teardown** — run this *after* Lab 3, which reuses `$VPC`, `cell-lab`, and `rg-cell-lab`. AWS refuses to delete a VPC while subnets or non-default security groups still reference it, so unwind in order:

```bash
# Subnets, then the security group Lab 3 created, then the VPC.
for S in $(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" \
             --query 'Subnets[].SubnetId' --output text); do
  aws ec2 delete-subnet --subnet-id "$S"
done
aws ec2 delete-security-group --group-name lab-sg --group-id \
  "$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC" \
       "Name=group-name,Values=lab-sg" --query 'SecurityGroups[0].GroupId' \
       --output text)" 2>/dev/null || true
aws ec2 delete-vpc --vpc-id "$VPC"

gcloud compute firewall-rules delete cell-lab-allow-internal -q 2>/dev/null || true
gcloud compute networks subnets delete cell-lab-usc1 --region=us-central1 -q
gcloud compute networks delete cell-lab -q
az group delete -n rg-cell-lab --yes --no-wait
```

### Lab 3 — Firewall semantics three ways

```bash
# AWS: stateful SG (allow-only) + stateless NACL
aws ec2 create-security-group --group-name lab-sg --description lab --vpc-id "$VPC"
aws ec2 describe-network-acls --filters "Name=vpc-id,Values=$VPC" \
  --query "NetworkAcls[].Entries"

# GCP: rule lives on the NETWORK, targets a service account
gcloud compute firewall-rules create cell-lab-allow-internal \
  --network=cell-lab --direction=INGRESS --action=ALLOW \
  --rules=tcp:7233,tcp:7235 \
  --target-service-accounts=NODE_SA@PROJECT.iam.gserviceaccount.com \
  --source-ranges=10.20.0.0/20

# Azure: NSG, and look at the DEFAULT rules
az network nsg create -g rg-cell-lab -n nsg-lab
az network nsg rule list -g rg-cell-lab --nsg-name nsg-lab --include-default \
  -o table
```

**Cost: free.** What to notice: the Azure default rule list includes `AllowVnetInBound` — Azure is default-open inside the VNet where AWS is default-closed between security groups. Your baseline policy cannot be shared.

### Lab 4 — Cross-cloud federation without a single stored key

This is the highest-value lab and it is entirely free (identity objects and STS calls carry no charge).

```bash
# GCP trusting AWS: workload identity pool with an AWS provider
gcloud iam workload-identity-pools create aws-cp-pool \
  --location=global --display-name="AWS control plane"
gcloud iam workload-identity-pools providers create-aws aws-provider \
  --location=global --workload-identity-pool=aws-cp-pool \
  --account-id=YOUR_AWS_ACCOUNT_ID \
  --attribute-condition="assertion.arn.startsWith('arn:aws:sts::YOUR_AWS_ACCOUNT_ID:assumed-role/InfraCP')"
# Then grant the federated principal a role directly:
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" \
  --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$(gcloud config get-value project)" \
  --role=roles/compute.networkViewer \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/aws-cp-pool/attribute.aws_role/arn:aws:sts::YOUR_AWS_ACCOUNT_ID:assumed-role/InfraCP"
```

```bash
# Azure trusting an external OIDC issuer (e.g. a GitHub Actions repo, free to test)
az identity create -g rg-cell-lab -n uami-cell-provisioner
az identity federated-credential create \
  --name gha-main --identity-name uami-cell-provisioner -g rg-cell-lab \
  --issuer "https://token.actions.githubusercontent.com" \
  --subject "repo:YOUR_ORG/YOUR_REPO:ref:refs/heads/main" \
  --audiences "api://AzureADTokenExchange"
# Now count them — the ceiling is 20 per identity:
az identity federated-credential list \
  --identity-name uami-cell-provisioner -g rg-cell-lab --query "length(@)"
```

```bash
# AWS trusting an external OIDC issuer
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
# Then a role whose trust policy conditions on sub/aud — inspect an existing one:
aws iam get-role --role-name SOME_ROLE --query 'Role.AssumeRolePolicyDocument'
```

**Cost: free.** Delete the identity pool/provider, the UAMI, and the OIDC provider afterward. Note that GCP workload identity pools are soft-deleted for 30 days and the name is reserved — a preview of the naming problem in Gotcha 13.

### Lab 5 — Quota introspection three ways

```bash
# AWS: the five On-Demand vCPU family buckets
aws service-quotas list-service-quotas --service-code ec2 \
  --query "Quotas[?contains(QuotaName, 'On-Demand')].{Name:QuotaName,Value:Value}" \
  --output table

# GCP: regional CPU + per-family quotas
gcloud compute regions describe us-central1 \
  --format="table(quotas.metric,quotas.limit,quotas.usage)"

# Azure: the two-tier model, visible in one call
az vm list-usage -l eastus2 -o table
```

**Cost: free.** What to notice: the three outputs are not the same data model. AWS buckets many instance families into one quota; Azure enforces both a regional total and a per-family limit; GCP has an aggregate plus per-family entries. Any "quota headroom" abstraction has to normalize three different shapes.

### Lab 6 — KMS envelope encryption three ways

```bash
# AWS  (~$1/key/month; keys cannot be deleted immediately)
KEYID=$(aws kms create-key --description lab --query KeyMetadata.KeyId --output text)
aws kms generate-data-key --key-id "$KEYID" --key-spec AES_256
aws kms schedule-key-deletion --key-id "$KEYID" --pending-window-in-days 7

# GCP  (billed per active key version; key rings can NEVER be deleted)
gcloud kms keyrings create lab-ring --location=us-central1
gcloud kms keys create lab-key --location=us-central1 \
  --keyring=lab-ring --purpose=encryption
echo "hello" | gcloud kms encrypt --location=us-central1 --keyring=lab-ring \
  --key=lab-key --plaintext-file=- --ciphertext-file=- | base64

# Azure  (per-operation; soft delete reserves the vault NAME)
az keyvault create -g rg-cell-lab -n kv-cell-lab-0001 -l eastus2
az keyvault key create --vault-name kv-cell-lab-0001 -n lab-key --kty RSA
az keyvault delete -n kv-cell-lab-0001            # soft delete
az keyvault list-deleted -o table                  # it is still holding the name
az keyvault purge -n kv-cell-lab-0001              # required to reuse the name
```

**Cost: small but nonzero.** AWS KMS keys carry a monthly charge and a mandatory deletion waiting period. GCP key rings are permanent — use a scratch project you are willing to abandon. Azure vault names are reserved by soft delete until purged; this lab is the cheapest way to feel Gotcha 13 in your hands.

### Lab 7 — Optional, and it costs money: one real cluster

If you want end-to-end workload identity, spin up exactly one cluster in one cloud, do the binding, and destroy it the same day.

```bash
# Cheapest of the three for a short-lived test is typically a single-zone GKE cluster
# (PROJECT_NUMBER is set in Lab 4; repeat it here if you skipped that lab.)
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" \
  --format='value(projectNumber)')
gcloud container clusters create-auto cell-lab --region=us-central1 \
  --workload-pool="$(gcloud config get-value project).svc.id.goog"
# Bind a KSA directly to a role (no intermediate service account):
gcloud projects add-iam-policy-binding "$(gcloud config get-value project)" \
  --role=roles/storage.objectViewer \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/$(gcloud config get-value project).svc.id.goog/subject/ns/default/sa/demo"
gcloud container clusters delete cell-lab --region=us-central1 -q
```

**Cost: real.** Managed control planes bill per cluster-hour on EKS and GKE (AKS has a free tier without an uptime SLA), plus nodes, plus any load balancer or NAT the cluster creates on your behalf. Set a calendar reminder to destroy it. The most common surprise bill from this lab is a leftover load balancer or public IP created by a `Service type=LoadBalancer` that outlived the cluster delete.

---

## Production gotchas

1. **GCP subnets are regional, so "one subnet per AZ" silently collapses.** A Terraform module that loops over zones creating subnets produces three subnets on AWS and either an error or three redundant subnets on GCP, where [one subnet already spans every zone in the region](https://docs.cloud.google.com/vpc/docs/subnets). The corollary bites harder: your AWS zonal-subnet routing, per-AZ NAT, and AZ-affinity logic have no GCP counterpart, and code that assumes "subnet implies zone" will mis-place workloads.

2. **AZ identity is not portable across tenancy boundaries.** `us-east-1a` is an account-local alias; use AZ IDs (`use1-az1`) to correlate zones across AWS accounts. Azure logical zones 1/2/3 are [mapped per subscription](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-overview) to physical zones. Only GCP zone names mean the same thing everywhere. A cell-per-account/subscription model with "spread across zones a, b, c" hard-coded will unknowingly co-locate cells in the same physical zone.

3. **Azure NSGs default to allowing intra-VNet inbound.** The built-in `AllowVnetInBound` rule ([NSG docs](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview)) means an Azure cell is open to its VNet neighbors unless you write explicit denies. Porting an AWS security-group baseline gives you a *less* secure Azure deployment than you think.

4. **Azure's identity and resource planes are separate and eventually consistent.** Creating a service principal via Microsoft Graph and immediately assigning it an ARM role frequently fails with `PrincipalNotFound`. This is expected behavior, not a transient bug; it needs a retry policy, and [Entra roles are not Azure RBAC roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/rbac-and-directory-admin-roles) so "I'm Global Administrator" does not imply you can create resources.

5. **GCP service accounts need a two-sided grant.** Permission to [impersonate](https://docs.cloud.google.com/iam/docs/service-account-impersonation) lives on the service account *resource*; permission to do the work lives on the *target* resource. Missing the first produces a denial that names the service account, not the operation you were attempting, and sends people debugging the wrong policy.

6. **The GKE metadata server is not ready the instant a pod starts.** Google documents that [Workload Identity Federation for GKE](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity) auth attempts in a pod's first seconds may fail and must be retried. Init containers and fast-failing entrypoints that authenticate immediately will flake at exactly the rate your cluster churns pods.

7. **Azure caps you at 20 federated identity credentials per identity.** [Documented limit](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations). A "one FIC per cell" design hits the ceiling at cell 21. Use one identity per *capability* with [flexible FICs](https://learn.microsoft.com/en-us/entra/workload-id/workload-identities-flexible-federated-identity-credentials) and subject matching, or one identity per cell (which then multiplies your Entra object count instead).

8. **AAD Pod Identity is dead; do not build on it.** The [OSS project is archived](https://github.com/Azure/aad-pod-identity) and the AKS managed add-on was supported only through September 2025. Any inherited `AzureIdentityBinding` CRD is a migration item, and the [documented path](https://learn.microsoft.com/en-us/azure/aks/workload-identity-migrate-from-pod-identity) is Entra Workload ID.

9. **IRSA requires one IAM OIDC provider per cluster.** Fleet-scale EKS with IRSA means editing role trust policies every time a cluster is created or replaced — an IAM write on the cell-creation critical path, subject to IAM's eventual consistency. [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identity.html) removes that coupling, and [gained cross-account support in June 2025](https://aws.amazon.com/about-aws/whats-new/2025/06/amazon-eks-pod-identity-cross-account-access), but its agent is a DaemonSet, so it does not work on Fargate.

10. **Azure's general-purpose L7 load balancers cannot do end-to-end gRPC.** Application Gateway v2 and Front Door speak HTTP/1.1 to backends ([Front Door FAQ](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-faq)). For a gRPC service like Temporal's frontend, Azure ingress is structurally different: [Application Gateway for Containers](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/application-gateway-for-containers-components) or your own Envoy. Do not discover this after committing to an ingress abstraction.

11. **GCP gRPC backends need gRPC or TCP health checks.** Google's [health check docs](https://docs.cloud.google.com/load-balancing/docs/health-check-concepts) are explicit: do not use HTTP(S) or HTTP/2 health checks against a `GRPC` backend service, and your server must implement the gRPC health checking protocol. An HTTP health check against a gRPC backend produces a permanently unhealthy backend service with no obvious error.

12. **NLB cross-zone load balancing is off by default and ALB's is on.** That default exists to avoid inter-AZ data transfer charges. If you flip it on for "better balancing," you have just opted every byte into cross-AZ pricing; if you leave it off, an AZ with fewer targets gets proportionally more load per target. Neither is wrong, but the choice must be deliberate, and it does not transfer to Azure where inter-AZ transfer is not charged.

13. **Key material and vault names outlive the cell.** GCP key rings and keys [cannot be deleted at all](https://cloud.google.com/kms/docs/key-rotation) — only versions can be destroyed. Azure Key Vault soft-delete reserves the vault name for the retention period unless you purge. AWS KMS enforces a 7–30 day [pending-deletion window](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html). Combined with GCP's permanently-burned project IDs, this means **cell names must carry a generation suffix** and teardown must have an explicit residue-handling step, or create-destroy-recreate will collide.

14. **Azure private endpoints fail silently without the `privatelink.*` DNS zone link.** If the zone is not linked to the VNet, the public name resolves to the public IP and traffic leaves the private path with no error, no metric, and no log entry that says "you are not private." Make the zone link an assertion in the cell's post-provision conformance test, not an assumption.

15. **Azure default outbound internet access is being retired.** Per [Microsoft's documentation](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access), new VMs will need an explicit outbound method — NAT Gateway, load balancer outbound rules, or an attached public IP. Cell templates that relied on implicit egress will break on new deployments while existing ones keep working, which is the worst possible failure mode: it only manifests on the next fresh cell.

16. **EKS burns real VPC IPs for every pod.** The VPC CNI gives each pod a routable address and pre-warms addresses per node. Without [prefix delegation or custom networking](https://docs.aws.amazon.com/eks/latest/best-practices/ip-opt.html) you will exhaust subnets long before you exhaust compute. Azure CNI Overlay avoids this entirely and GKE isolates it in a secondary range — so an address plan sized for GCP or Azure will not fit on AWS.

17. **Nitro instances share one attachment budget across ENIs, EBS, and instance store.** Most Nitro types allow [28 total attachments](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/volume_limits.html); newer generations have dedicated EBS limits from 32 to 128. Because the VPC CNI attaches ENIs, a busy node has fewer EBS slots than the raw number suggests, and PVC-heavy pods hang in `ContainerCreating` with a message that does not mention ENIs.

18. **Legacy GCP preemptible VMs stop at 24 hours, unconditionally.** [Spot VMs](https://docs.cloud.google.com/compute/docs/instances/spot) have no such cap. A node pool still specifying `preemptible` rather than the Spot provisioning model will cycle every node daily forever, which looks like a mysterious daily churn pattern rather than a config error.

19. **Amazon Keyspaces is CQL-compatible, not Cassandra.** The [functional differences page](https://docs.aws.amazon.com/keyspaces/latest/devguide/functional-differences.html) lists materialized views, secondary-index behavior, and lightweight-transaction capacity accounting (failed LWT condition checks still consume write units) among the divergences. Temporal's persistence layer leans on LWTs; treat Keyspaces as a candidate requiring validation, not a substitute.

20. **Azure Monitor's default log plan is materially more expensive per GB than CloudWatch or Cloud Logging.** Shipping the same pod-log volume to Log Analytics on the Analytics table plan, rather than Basic or Auxiliary, can multiply a cell's observability bill. Check the [pricing page](https://azure.microsoft.com/en-us/pricing/details/monitor/) and pick the table plan deliberately per data type.

21. **Quota is not capacity.** All three clouds will accept your quota increase and still refuse to launch: `InsufficientInstanceCapacity` (AWS), `ZONE_RESOURCE_POOL_EXHAUSTED` (GCP), `ZonalAllocationFailed` (Azure). A cell-creation workflow needs different handling for each class — retry-elsewhere for capacity, request-and-wait for quota — and the error taxonomies are cloud-specific, so classification belongs in the per-cloud implementation.

22. **Azure Storage control-plane roles do not grant data access.** `Contributor` on a storage account lets you delete it but not read a blob; you need data-plane roles like `Storage Blob Data Contributor`. Teams discover this, reach for account keys, and reintroduce the static credential they eliminated everywhere else.

23. **Not every Azure region has availability zones.** Zone support is region-dependent ([reliability docs](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-overview)), so a cell template that pins `zones: [1,2,3]` will either fail outright or, worse, be silently accepted in a degraded form depending on the resource type. Make "does this region have zones?" an explicit precondition of cell placement rather than a template assumption — it is the one input where AWS and GCP let you be lazy and Azure does not.

24. **GKE release channels will upgrade your control plane without you.** Enrolling a cluster in a channel means Google decides when the control plane moves, subject to your maintenance windows and exclusions. That is usually what you want at fleet scale, but a cell-upgrade workflow that assumes it is the only actor mutating cluster version will observe "impossible" state transitions. Model platform-initiated upgrades as a legitimate state change, and reconcile rather than assert.

25. **EKS version aging is a billing cliff, not an error.** When a Kubernetes version leaves standard support, the cluster keeps running and the per-cluster hourly rate goes up for extended support ([EKS pricing](https://aws.amazon.com/eks/pricing/)). Nothing breaks, no alarm fires, and the cost shows up a month later multiplied by the number of cells. Track version-age as a first-class fleet metric alongside health.

26. **A retried create must adopt, not fail — and a fixed sleep hides as success.** Cell provisioning activities get retried; a create that returns `AlreadyExists` on retry must resolve to the existing resource. Equally, replacing operation polling with `sleep 30` produces workflows that "succeed" while racing, then fail three steps later against a resource that was not actually ready. GCP and Azure hand you an operation handle; AWS gives you a describe call. Use them.

---

## How this shows up in cell lifecycle

**Provisioning.** A cell-create workflow is roughly: ensure tenancy container (account/project/subscription + resource group) → ensure quota → allocate CIDR from global IPAM → create network + subnets (1 on GCP/Azure, N on AWS) → create egress (Cloud NAT config vs N NAT gateways vs NAT Gateway resource) → create the managed K8s control plane → create node pools with per-cloud shapes and capacity strategy → enable the workload identity mechanism → provision KMS key and Vault seal config → provision persistence (managed Postgres or Cassandra, cloud-specific) → provision visibility store (cloud-specific, possibly third-party) → create the ingress surface (ALB / GCP forwarding-rule graph / in-cluster Envoy behind a Standard LB) → publish private connectivity → write DNS → register with the control plane. Every arrow in that chain has a different resource graph per cloud and the *same* verb. That is the interface.

Networking is often shared with a separate networking team precisely because steps 3–6 and 11–13 straddle the boundary: you cannot bring a cell up without addressing, egress, and an ingress surface, and none of those are cell-local decisions. The practical implication is that the cell contract must pin down exactly which network facts the cell *receives* (its CIDR, its DNS delegation, its private-link publication target) versus which it *creates*. Anything the cell creates that another team also touches is a future incident.

**Upgrading.** Cell upgrades exercise the parts of each cloud that are least uniform: control-plane version skew policies differ per cloud, node pool replacement mechanics differ (managed node groups vs node pools vs VMSS), load balancer target draining differs, and disk reattachment across zones is possible on GCP and Azure but not AWS. Surge capacity during an upgrade doubles the vCPU footprint temporarily, which means the upgrade workflow must check quota headroom — on three differently-shaped quota models — before it starts, not halfway through.

**Teardown.** This is where the cloud differences do the most damage, because deletion is the least-tested path. Soft-deleted Key Vaults, undeletable GCP key rings, permanently-burned project IDs, 30-day workload-identity-pool name reservations, load balancers and public IPs orphaned by Kubernetes services, DNS records left dangling, and private-endpoint connections left in a pending state on the consumer side. A correct teardown workflow enumerates residue explicitly per cloud and either purges it or records a tombstone that blocks name reuse. Test teardown as often as you test creation.

**Multi-cloud parity as a first-class concern.** Temporal's blog is candid that GCP shipped without private connectivity, API keys, export, Nexus, and multi-region namespaces at parity with AWS. That is the normal state of a multi-cloud platform: capability sets differ per cloud, and the control plane needs a feature-availability model, not an assumption of uniformity. Bake capability flags into the cell metadata so the user-facing control plane can refuse to offer a feature in a cloud where the cell cannot provide it, rather than failing at provision time.

---

## Learning path

**Day 1 — get oriented, do not go deep.**

- Read the three hierarchy docs back to back: [AWS Organizations concepts](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_getting-started_concepts.html), [GCP resource hierarchy](https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy), [Azure management groups](https://learn.microsoft.com/en-us/azure/governance/management-groups/overview). Draw all three on one page.
- Do Labs 1 and 2. The subnet-scope difference is the single highest-leverage fact in this guide.
- Read Temporal's [multi-cloud blog post](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability) end to end and note which of its problems are still open.
- Skim the three workload-identity docs ([IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html), [GKE WIF](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity), [Entra Workload ID](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview)) for shape, not detail.

**Week 1 — build the mental table.**

- Do Labs 3, 4, 5, and 6. Lab 4 (cross-cloud federation) is the one that changes how you think.
- For each of the twelve capability sections above, write your own three-column table from memory, then check it against this guide. The gaps are your study list.
- Read the load-balancing docs for all three properly, including [GCP's forwarding rule / backend service / health check triple](https://docs.cloud.google.com/load-balancing/docs/backend-service) — it is the least AWS-like model and the one most likely to trip you.
- Read the three quota docs and write a script that prints normalized headroom across all three clouds for one region each. This will be genuinely useful later.
- Read [AWS's cell-based architecture whitepaper](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html) and map its vocabulary onto Temporal's cells.

**Month 1 — earn opinions.**

- Trace one real cell end to end in each cloud in your team's codebase: which resources exist, in what order, owned by which workflow, torn down by what.
- Build (or read, if it exists) the conformance test suite: given a provisioned cell, assert every property in the cell contract — DNS resolves privately, workload identity mints a token, KMS decrypt works, the ingress does per-request gRPC balancing, egress goes through the intended NAT, quota headroom exceeds N%. Run it on all three clouds. Every difference it exposes is a portability decision someone made implicitly.
- Own one cross-cutting improvement: normalize quota headroom monitoring, or add generation suffixes to cell naming, or write the teardown residue enumerator. All three are real, all three are cloud-shaped, and all three teach you more than reading does.
- Read the pricing pages for NAT, load balancing, cross-AZ transfer, and log ingest in all three clouds, and compute your team's empty-cell floor cost per cloud. Then argue about it with someone.

---

## References

1. [Terminology and concepts for AWS Organizations — AWS](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_getting-started_concepts.html) — the account/OU/root model and how SCPs attach.
2. [Quotas and service limits for AWS Organizations — AWS](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_reference_limits.html) — OU nesting depth and the default account limit.
3. [Google Cloud resource hierarchy — Google](https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy) — organization/folder/project and policy inheritance.
4. [Resource Manager quotas and limits — Google](https://docs.cloud.google.com/resource-manager/docs/limits) — folder nesting depth, folders per parent, project creation quota.
5. [Organize your resources with management groups — Microsoft](https://learn.microsoft.com/en-us/azure/governance/management-groups/overview) — MG depth limit and subscription placement.
6. [IAM roles terms and concepts — AWS](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_terms-and-concepts.html) — trust policies and role assumption.
7. [IAM overview — Google](https://docs.cloud.google.com/iam/docs/overview) — allow policies bound to resources, inheritance semantics.
8. [Service account impersonation — Google](https://docs.cloud.google.com/iam/docs/service-account-impersonation) — why the two-sided grant exists.
9. [What is Azure RBAC — Microsoft](https://learn.microsoft.com/en-us/azure/role-based-access-control/overview) — role assignments as ARM resources at MG/sub/RG/resource scope.
10. [Microsoft Entra roles vs Azure roles — Microsoft](https://learn.microsoft.com/en-us/azure/role-based-access-control/rbac-and-directory-admin-roles) — the identity-plane/control-plane split, in one page.
11. [IAM roles for service accounts (IRSA) — AWS](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) — per-cluster OIDC provider and trust policy conditions.
12. [EKS Pod Identity — AWS](https://docs.aws.amazon.com/eks/latest/userguide/pod-identity.html) — association API, agent DaemonSet, cluster-agnostic trust policies.
13. [EKS Pod Identity cross-account access — AWS](https://aws.amazon.com/about-aws/whats-new/2025/06/amazon-eks-pod-identity-cross-account-access) — June 2025 announcement of cross-account associations.
14. [About Workload Identity Federation for GKE — Google](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity) — metadata server, STS exchange, startup timing caveat.
15. [Identities for workloads — Google](https://docs.cloud.google.com/iam/docs/workload-identities) — the `principal://` identifier syntax for direct resource access.
16. [Microsoft Entra Workload ID on AKS — Microsoft](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview) — OIDC issuer, mutating webhook, projected token flow.
17. [Migrate AKS pods from pod-managed identity to Workload ID — Microsoft](https://learn.microsoft.com/en-us/azure/aks/workload-identity-migrate-from-pod-identity) — the official deprecation and migration path.
18. [Workload Identity Federation — Google](https://docs.cloud.google.com/iam/docs/workload-identity-federation) — pools, providers, attribute mapping and conditions.
19. [Configure Workload Identity Federation with AWS or Azure — Google](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds) — the AWS provider type that validates a signed `GetCallerIdentity`.
20. [Create OIDC identity providers in IAM — AWS](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html) — the inbound federation primitive for AWS.
21. [Workload identity federation considerations — Microsoft](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations) — the 20-federated-credential ceiling and other limits.
22. [VPC networks — Google](https://docs.cloud.google.com/vpc/docs/vpc) — VPC networks are global resources.
23. [Subnets — Google](https://docs.cloud.google.com/vpc/docs/subnets) — subnets are regional and span all zones; secondary ranges.
24. [Configure subnets — AWS](https://docs.aws.amazon.com/vpc/latest/userguide/configure-subnets.html) — subnets are bound to a single Availability Zone.
25. [Azure virtual network overview — Microsoft](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-networks-overview) — VNet and subnet scoping.
26. [Security groups — AWS](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-security-groups.html) — stateful, allow-only, attached to ENIs.
27. [VPC firewall rules — Google](https://docs.cloud.google.com/firewall/docs/firewalls) — stateful connection tracking, the 10-minute idle rule, tag/service-account targeting.
28. [Network security groups — Microsoft](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview) — stateful NSGs and the default `AllowVnetInBound` rule.
29. [What is AWS PrivateLink — AWS](https://docs.aws.amazon.com/vpc/latest/privatelink/what-is-privatelink.html) — endpoint services, interface endpoints, the NLB requirement.
30. [Private Service Connect — Google](https://docs.cloud.google.com/vpc/docs/private-service-connect) — endpoints, backends, and the consumer/producer split.
31. [What is Azure Private Link — Microsoft](https://learn.microsoft.com/en-us/azure/private-link/private-link-overview) — Private Link Service, Private Endpoints, and DNS integration.
32. [What is a transit gateway — AWS](https://docs.aws.amazon.com/vpc/latest/tgw/what-is-transit-gateway.html) — regional hub, attachments, route tables, TGW peering.
33. [VPC Network Peering — Google](https://docs.cloud.google.com/vpc/docs/vpc-peering) — non-transitivity and CIDR overlap rules.
34. [VPC spokes overview (Network Connectivity Center) — Google](https://docs.cloud.google.com/network-connectivity/docs/network-connectivity-center/concepts/vpc-spokes-overview) — transitive VPC connectivity without a peering mesh.
35. [Azure Virtual WAN — Microsoft](https://learn.microsoft.com/en-us/azure/virtual-wan/virtual-wan-about) — managed hubs with built-in transit, versus DIY hub-and-spoke.
36. [Default outbound access — Microsoft](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access) — the retirement of implicit VM internet egress and the explicit alternatives.
37. [NAT gateways — AWS](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat-gateway.html) — zonal resource, port allocation, `ErrorPortAllocation`.
38. [Cloud NAT overview — Google](https://docs.cloud.google.com/nat/docs/overview) — SDN-based regional NAT with per-VM port allocation.
39. [What is Azure NAT Gateway — Microsoft](https://learn.microsoft.com/en-us/azure/nat-gateway/nat-overview) — SNAT port model and zone behavior.
40. [Optimizing IP address utilization — AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/ip-opt.html) — prefix delegation and custom networking for pod IP exhaustion.
41. [VPC-native clusters and alias IP ranges — Google](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/alias-ips) — how GKE consumes secondary ranges for pods and services.
42. [Azure CNI Overlay — Microsoft](https://learn.microsoft.com/en-us/azure/aks/azure-cni-overlay) — pod IPs outside the VNet address space.
43. [Private hosted zones — AWS](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/hosted-zones-private.html) — VPC association model, including cross-account.
44. [DNS zones overview — Google](https://docs.cloud.google.com/dns/docs/zones/zones-overview) — private zones, DNS peering (single transitive hop), cross-project binding.
45. [Virtual network links for Azure DNS private zones — Microsoft](https://learn.microsoft.com/en-us/azure/dns/private-dns-virtual-network-links) — the link model and the one-auto-registration-link rule.
46. [Target groups for Application Load Balancers — AWS](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html) — HTTP/1, HTTP/2, and gRPC protocol versions and gRPC health checks.
47. [Backend services overview — Google](https://docs.cloud.google.com/load-balancing/docs/backend-service) — backend service protocols including HTTP2 and GRPC, and NEG attachment.
48. [Health checks overview — Google](https://docs.cloud.google.com/load-balancing/docs/health-check-concepts) — why gRPC backends need gRPC or TCP health checks.
49. [Azure Front Door FAQ — Microsoft](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-faq) — HTTP/2 to clients, HTTP/1.1 to origins.
50. [Application Gateway for Containers components — Microsoft](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/application-gateway-for-containers-components) — the Azure L7 that does speak HTTP/2 to backends for gRPC.
51. [Purchasing On-Demand Instances — AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-on-demand-instances.html) — vCPU-based On-Demand limits grouped by instance family class.
52. [Compute Engine allocation quotas — Google](https://docs.cloud.google.com/compute/resource-usage) — regional CPU quota, per-family quotas, separate preemptible/Spot quota.
53. [Spot VMs — Google](https://docs.cloud.google.com/compute/docs/instances/spot) — Spot VMs have no maximum runtime; the 24-hour cap applies only to legacy preemptible VMs.
54. [vCPU quotas — Microsoft](https://learn.microsoft.com/en-us/azure/virtual-machines/quotas) — the two-tier regional-total plus per-family model.
55. [Availability zones overview — Microsoft](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-overview) — logical-to-physical zone mapping is per subscription.
56. [Amazon EBS volume limits for EC2 instances — AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/volume_limits.html) — shared 28-attachment budget on most Nitro types; dedicated limits on newer generations.
57. [Choose a disk type — Google](https://docs.cloud.google.com/compute/docs/disks) — Persistent Disk vs Hyperdisk, and regional (two-zone synchronous) options.
58. [Azure managed disk types — Microsoft](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types) — Premium SSD v2, Ultra, and zone-redundant storage options.
59. [S3 strong read-after-write consistency — AWS](https://aws.amazon.com/about-aws/whats-new/2020/12/amazon-s3-now-delivers-strong-read-after-write-consistency-automatically-for-all-applications) — the December 2020 change that ended the eventual-consistency era.
60. [Temporal Service persistence — Temporal](https://docs.temporal.io/temporal-service/persistence) — supported databases and minimum versions for advanced Visibility.
61. [Functional differences: Amazon Keyspaces vs Apache Cassandra — AWS](https://docs.aws.amazon.com/keyspaces/latest/devguide/functional-differences.html) — what CQL compatibility does and does not cover.
62. [Introduction to Azure Managed Instance for Apache Cassandra — Microsoft](https://learn.microsoft.com/en-us/azure/managed-instance-apache-cassandra/introduction) — managed open-source Cassandra, not an API emulation.
63. [What's happening to Azure Database for PostgreSQL Single Server — Microsoft](https://learn.microsoft.com/en-us/azure/postgresql/single-server/whats-happening-to-postgresql-single-server) — the March 2025 retirement and Flexible Server migration.
64. [Rotating AWS KMS keys — AWS](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html) — configurable automatic rotation (90–2560 days) and what is not rotatable.
65. [Cloud KMS key rotation — Google](https://cloud.google.com/kms/docs/key-rotation) — rotation schedules, primary versions, and what can and cannot be deleted.
66. [Azure Key Vault soft-delete overview — Microsoft](https://learn.microsoft.com/en-us/azure/key-vault/general/soft-delete-overview) — name reservation, purge, and purge protection.
67. [Vault seal configuration — HashiCorp](https://developer.hashicorp.com/vault/docs/configuration/seal) — the `awskms`, `gcpckms`, and `azurekeyvault` auto-unseal stanzas.
68. [cert-manager ACME DNS01 solvers — cert-manager](https://cert-manager.io/docs/configuration/acme/dns01/) — Route 53, Cloud DNS, and Azure DNS solver configuration and required permissions.
69. [Amazon CloudWatch pricing — AWS](https://aws.amazon.com/cloudwatch/pricing/) — per-GB log ingest and metric pricing.
70. [Google Cloud Observability pricing — Google](https://cloud.google.com/stackdriver/pricing) — Cloud Logging/Monitoring ingest pricing and free allowances.
71. [Azure Monitor pricing — Microsoft](https://azure.microsoft.com/en-us/pricing/details/monitor/) — the Analytics / Basic / Auxiliary table plans and their very different per-GB rates.
72. [Amazon VPC pricing — AWS](https://aws.amazon.com/vpc/pricing/) — NAT gateway hourly and per-GB charges; cross-AZ data transfer.
73. [All networking pricing — Google](https://cloud.google.com/vpc/network-pricing) — inter-zone and inter-region data transfer, Premium vs Standard tier.
74. [Bandwidth pricing — Microsoft](https://azure.microsoft.com/en-us/pricing/details/bandwidth/) — the authoritative page for Azure inter-AZ and inter-region transfer.
75. [AKS pricing tiers — Microsoft](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers) — the Free tier has no uptime SLA; Standard does. Compare against EKS and GKE per-cluster-hour pricing for the empty-cell floor cost.
76. [Reducing the Scope of Impact with Cell-Based Architecture — AWS Well-Architected](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html) — the vocabulary and math of cells, cell routers, and blast radius.
77. [Making Temporal Cloud a Multi-Cloud Platform — Temporal (vendor blog)](https://temporal.io/blog/multi-cloud-thats-one-small-step-for-temporal-one-giant-leap-for-reliability) — the Infra CP / User CP split, the provider-interface + factory pattern, and the concrete AWS→GCP parity gaps.
