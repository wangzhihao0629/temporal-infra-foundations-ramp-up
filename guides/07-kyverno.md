# Kyverno — Policy as Code for Kubernetes

**Why this matters.** When you own cell lifecycle across AWS, GCP, and Azure, you are shipping the same Kubernetes cluster hundreds of times and you need two guarantees that plain manifests cannot give you: that every cell comes up with the same baseline objects regardless of who created a namespace, and that nothing gets into a cell that violates the platform's invariants. Kyverno does both — `generate` rules are cell bootstrap (every new namespace gets a NetworkPolicy, a LimitRange, a pull secret), and `validate` rules are the guardrail that stops a tenant from shipping a `maxUnavailable: 0` PDB that wedges your Karpenter drift rollout. It is also a piece of infrastructure that, misconfigured, can prevent a cell from coming up at all, because it sits in the admission path of every API request. Understanding its failure modes is not optional.

Everything below was verified against primary sources on **2026-08-29**. Where I could not verify something, I say so.

> **Stale-content trap, read this first.** Kyverno is in the middle of the largest API transition in its history and **almost every tutorial, blog post, and LLM-generated snippet you will find describes the deprecated API.** The classic `apiVersion: kyverno.io/v1`, `kind: ClusterPolicy` with a `spec.rules[]` list of `validate`/`mutate`/`generate` rules is **deprecated as of v1.19 and scheduled for removal in v1.20**. The docs say it verbatim: *"ClusterPolicy, Policy, CleanupPolicy, and the legacy `kyverno.io` PolicyException are deprecated as of Kyverno v1.19 and will be removed in v1.20. The CEL-based policy types provide full feature parity as of v1.19. Plan your migration now."* ([Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/)). The replacement is a family of single-purpose CEL types at `policies.kyverno.io/v1`. Every example in this guide uses the new API, with the legacy form shown only where it still matters. Current release is **v1.19.0 (August 2026)**, supporting **Kubernetes v1.33–v1.35** ([Releases](https://kyverno.io/docs/installation/releases/)).

---

## The mental model

Hold five ideas.

**1. Kyverno is a pair of webhooks with a controller behind them, plus a reconciler that acts outside admission.** The admission controller answers `AdmissionReview` requests from the API server. But `generate`, `mutate-existing`, and background scanning all run *outside* the admission path, in separate controllers, against the live cluster. That split — synchronous gate vs. asynchronous reconciler — explains most of Kyverno's operational behavior, including why generate rules are eventually consistent and why the background controller needs extra RBAC.

**2. The policy language is now CEL, and each policy does exactly one thing.** The old model was one `ClusterPolicy` with an ordered list of heterogeneous rules. The new model is five separate kinds — `ValidatingPolicy`, `MutatingPolicy`, `GeneratingPolicy`, `DeletingPolicy`, `ImageValidatingPolicy` — each a superset of the corresponding *native* Kubernetes admission policy type, each written in CEL, each with cluster-scoped and `Namespaced*` variants ([Policy Types](https://kyverno.io/docs/policy-types/)).

**3. Kyverno's positioning is now "CEL, but with the parts Kubernetes left out."** A `ValidatingPolicy` *is* a `ValidatingAdmissionPolicy` plus external data lookups, background scans, reports, exceptions, auto-generation for pod controllers, CLI testing, and non-Kubernetes JSON payloads ([ValidatingPolicy](https://kyverno.io/docs/policy-types/validating-policy/)). It can even *compile itself down* to a native `ValidatingAdmissionPolicy` so the API server evaluates it with no webhook hop at all.

**4. Webhooks are the sharpest object in the box.** A validating webhook with `failurePolicy: Fail` that is unreachable blocks every matching API request cluster-wide. Kyverno defaults to **fail-closed** ([Security](https://kyverno.io/docs/guides/security/)) and dynamically narrows its webhook rules to only the resources your installed policies actually match, which is the mitigation. But the failure mode is real, and for a cell platform the question "can a cell bootstrap if Kyverno is down?" needs a deliberate answer.

**5. Policies are code, so test them like code.** The Kyverno CLI (`kyverno apply`, `kyverno test`) runs the real engine offline against manifest files. This is the single biggest reason a platform team picks Kyverno over hand-rolled webhooks: your guardrails get unit tests that run in CI before they can break a cell.

```text
                        kubectl / controller
                                │
                                ▼
                        ┌───────────────┐
                        │  kube-apiserver│
                        └───────┬───────┘
      Mutating phase            │            Validating phase
   ┌────────────────────────────┴──────────────────────────────┐
   │ 1. MutatingAdmissionWebhooks (Kyverno MutatingPolicy)     │
   │ 2. MutatingAdmissionPolicy (in-process CEL)               │
   │ 3. Object schema validation                               │
   │ 4. ValidatingAdmissionPolicy (in-process CEL)             │
   │ 5. ValidatingAdmissionWebhooks (Kyverno ValidatingPolicy) │
   └───────────────────────────────────────────────────────────┘
                                │ persist to etcd
                                ▼
   ── outside admission ──────────────────────────────────────────
   Background controller  → generate / mutate-existing (async)
   Reports controller     → background scans → PolicyReport (1h default)
   Cleanup controller     → DeletingPolicy via CronJobs
```

---

## Core concepts

### Admission control fundamentals, briefly

You know the shape; what matters operationally is the ordering and the failure semantics.

**Ordering.** Mutating webhooks run first, all of them, then the object is schema-validated, then validating webhooks run — all of them, and **in parallel**. Two consequences: a validating webhook can never modify an object, and *mutating* webhooks have no guaranteed order among themselves. Kubernetes' answer to mutation ordering is `reinvocationPolicy: IfNeeded`, which re-invokes mutating webhooks when an earlier one changed the object ([Kubernetes: Dynamic Admission Control](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/)). Kyverno's `MutatingPolicy` exposes exactly this field, defaulting to `Never` ([MutatingPolicy](https://kyverno.io/docs/policy-types/mutating-policy/)).

**failurePolicy.** `Fail` means an unreachable or erroring webhook **rejects the request**. `Ignore` means it is skipped. This is the single most consequential knob in the whole system.

**timeoutSeconds.** Per-webhook, 1–30 seconds. Kyverno's `spec.webhookConfiguration.timeoutSeconds` defaults to **10** with an allowed range of **1 to 30**, and Kyverno reflects it into the generated webhook configuration ([ValidatingPolicy — webhookConfiguration](https://kyverno.io/docs/policy-types/validating-policy/)).

**Why a broken webhook bricks a cluster.** If a `failurePolicy: Fail` webhook matches `*/*` and its backing pods are gone — evicted, on a drained node, or unschedulable — then *every* create and update in the cluster fails, including the ones that would fix the webhook. If it also matches `pods` in the namespace where it runs, it cannot even restart itself. This is a genuine circular dependency and it is why Kubernetes ships a `namespaceSelector` and why Kyverno excludes `kube-system` and its own namespace by default (below). The recovery is `kubectl delete validatingwebhookconfiguration <name>` from a credential that can still reach the API server — which means you must know the webhook names *before* the incident.

Kyverno creates these, by name ([Security](https://kyverno.io/docs/guides/security/)):

| Kind | Names |
|---|---|
| MutatingWebhookConfiguration | `kyverno-policy-mutating-webhook-cfg`, `kyverno-resource-mutating-webhook-cfg`, `kyverno-verify-mutating-webhook-cfg` |
| ValidatingWebhookConfiguration | `kyverno-policy-validating-webhook-cfg`, `kyverno-resource-validating-webhook-cfg`, `kyverno-cleanup-validating-webhook-cfg`, `kyverno-exception-validating-webhook-cfg` |

Put that table in your cell runbook.

### Kyverno's model, and how it compares

The pitch has always been "policy as Kubernetes YAML, no new language." That is now half true: the new types are CEL-based, so there *is* an expression language — but it is the same CEL the Kubernetes API server itself uses for `ValidatingAdmissionPolicy`, not a bespoke one.

| | Kyverno (CEL types) | OPA / Gatekeeper | ValidatingAdmissionPolicy (built-in) |
|---|---|---|---|
| Language | CEL, in Kubernetes-shaped YAML | Rego, embedded in a `ConstraintTemplate` | CEL |
| Install footprint | 4 Deployments + CRDs + webhooks | 2 Deployments + CRDs + webhooks | **None** — in the API server |
| Authoring model | One policy = one action | `ConstraintTemplate` (the code) + `Constraint` (the instance) — two objects per rule | One `ValidatingAdmissionPolicy` + one `Binding` |
| Validate | Yes | Yes | Yes |
| Mutate | Yes (`MutatingPolicy`) | Yes (separate `Assign`/`AssignMetadata` CRDs) | Only via `MutatingAdmissionPolicy`, a newer and separately-gated feature |
| Generate resources | **Yes** (`GeneratingPolicy`) | No | No |
| Delete on a schedule | **Yes** (`DeletingPolicy`) | No | No |
| Image signature verification | **Yes** (`ImageValidatingPolicy`) | Via external tooling | No |
| External data at eval time | Kubernetes API, HTTP, image registries, `GlobalContextEntry` cache | `data.inventory` replication, external data providers | **No** |
| Background scan of existing resources | Yes, ~1h default | Via audit controller | **No** |
| Reports | Policy WG `PolicyReport` CRs | `status` on Constraints + audit | **No** |
| Exceptions | `PolicyException` CRs, fine-grained | Constraint `excludedNamespaces`, or Rego logic | Binding-level only |
| Offline unit testing | `kyverno test` / `kyverno apply` | `opa test`, `gator` | Limited |
| Failure blast radius | Webhook outage → fail-closed by default | Webhook outage → per your config | **Cannot fail** — no network hop |
| Latency | Network hop per request | Network hop per request | In-process |
| Learning curve | Low → medium (CEL) | High (Rego is a genuinely different paradigm) | Medium (CEL, verbose bindings) |

The honest read in 2026: `ValidatingAdmissionPolicy` — **GA since Kubernetes 1.30** ([Kubernetes: Validating Admission Policy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)) — is strictly better for simple, self-contained, CEL-expressible checks, because it has no webhook to break and no pods to keep alive. It cannot do external data, reports, background scans, generation, or exceptions.

Kyverno's answer is not to compete but to **compile down**. `ValidatingPolicy.spec.autogen.validatingAdmissionPolicy.enabled: true` makes Kyverno emit a native `ValidatingAdmissionPolicy` from your policy, so the API server evaluates it in-process while you keep Kyverno's authoring, testing, and reporting. The docs describe the benefit as *"faster and more resilient execution during admission controls while leveraging all features of Kyverno"* ([ValidatingPolicy — autogen](https://kyverno.io/docs/policy-types/validating-policy/)). The catch is stated in a note: **pod-controller autogen and VAP generation are mutually exclusive** — configuring `spec.autogen.podControllers` makes Kyverno skip VAP generation and report why in the policy status, because a VAP is evaluated by the API server and cannot carry Kyverno-generated pod-controller rule variants.

`MutatingPolicy` has the mirror image via `spec.autogen.mutatingAdmissionPolicy.enabled`. Note that Kubernetes' `MutatingAdmissionPolicy` is substantially newer than VAP; check your control-plane version and feature gates before relying on it ([Kubernetes: Mutating Admission Policy](https://kubernetes.io/docs/reference/access-authn-authz/mutating-admission-policy/)).

### Current API version and major-version status

Verified 2026-08-29.

| Fact | Value | Source |
|---|---|---|
| Current release | **v1.19.0**, August 2026 | [Releases](https://kyverno.io/docs/installation/releases/) |
| Kubernetes support | **v1.33 – v1.35** | [Releases](https://kyverno.io/docs/installation/releases/) |
| Patch support model | **"main + 1"** since v1.18 — current release plus previous, ~3 months, critical/high CVEs only | [Announcing 1.18](https://kyverno.io/blog/2026/04/24/announcing-kyverno-release-1.18/) |
| Estimated EOL for 1.19 | v1.20 release, ~November 2026 | [Releases](https://kyverno.io/docs/installation/releases/) |
| Minor release cadence | ~every 3 months | [Releases](https://kyverno.io/docs/installation/releases/) |
| CNCF status | **Graduated**, March 2026 | [CNCF announcement](https://www.cncf.io/announcements/2026/03/24/cloud-native-computing-foundation-announces-kyvernos-graduation/) |

**The policy-type split, with dates:**

| Kind | Group/version | Introduced | Status in 1.19 |
|---|---|---|---|
| `ValidatingPolicy` / `NamespacedValidatingPolicy` | `policies.kyverno.io/v1` | v1.14 (Apr 2025) | Stable |
| `ImageValidatingPolicy` / `Namespaced…` | `policies.kyverno.io/v1` | v1.14 (Apr 2025) | Stable |
| `MutatingPolicy` / `Namespaced…` | `policies.kyverno.io/v1` | v1.15 (Jul 2025) | Stable |
| `GeneratingPolicy` / `Namespaced…` | `policies.kyverno.io/v1` | v1.15 (Jul 2025) | Stable |
| `DeletingPolicy` / `Namespaced…` | `policies.kyverno.io/v1` | v1.15 (Jul 2025) | Stable |
| `ClusterPolicy` / `Policy` | `kyverno.io/v1` | legacy | **Deprecated, removed in v1.20** |
| `CleanupPolicy` / `ClusterCleanupPolicy` | `kyverno.io/v2` | legacy | **Deprecated, removed in v1.20** |
| `PolicyReport` / `ClusterPolicyReport` | `wgpolicyk8s.io/v1alpha2` | — | Current |
| `PolicyException` (legacy `kyverno.io`) | `kyverno.io/v2` | legacy | **Deprecated, removed in v1.20** |

Three things to internalize from this table. First, **the `Namespaced*` variants are new and important for multi-tenancy**: `NamespacedValidatingPolicy` "allows namespace owners to manage validation policies without requiring cluster-admin permissions" ([ValidatingPolicy — policy scope](https://kyverno.io/docs/policy-types/validating-policy/)). For a cell platform where each cell may host tenants, this is the difference between "platform team writes every policy" and "platform team writes cluster invariants, tenants write their own." Second, **the cleanup story moved**: `CleanupPolicy` is deprecated in favor of `DeletingPolicy`, with the docs claiming full feature parity ([Cleanup Policy — deprecated](https://kyverno.io/docs/policy-types/cleanup-policy/)). Third, **background scanning still uses intermediary CRs** at `kyverno.io/v1alpha2` (`AdmissionReport`, `BackgroundScanReport` and their cluster-scoped forms) that roll up into the `wgpolicyk8s.io` reports ([Policy Reports](https://kyverno.io/docs/guides/reports/)).

> **Correction to a common mistake:** `kyverno migrate` is **not** a ClusterPolicy-to-CEL converter. Its documented synopsis is *"Migrate one or more resources to the stored version"* with the example `kyverno migrate --resource policyexceptions.kyverno.io` ([kyverno migrate](https://kyverno.io/docs/kyverno-cli/reference/kyverno_migrate/)). It is a CRD stored-version utility. The CEL migration is manual, guided by the field-mapping table in [Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/).

### Rule types, with real examples

#### `ValidatingPolicy` — the guardrail

```yaml
apiVersion: policies.kyverno.io/v1
kind: ValidatingPolicy
metadata:
  name: require-pdb-not-fully-blocking
  annotations:
    policies.kyverno.io/title: PDBs must not permanently block eviction
    policies.kyverno.io/category: Cell Safety
    policies.kyverno.io/severity: high
spec:
  # Deny | Audit | Warn. Start with Audit, promote to Deny.
  validationActions: [Deny]

  # failurePolicy applies to the generated webhook. Fail is Kyverno's default.
  failurePolicy: Fail

  webhookConfiguration:
    timeoutSeconds: 10        # default 10, allowed 1..30

  evaluation:
    admission:
      enabled: true           # gate at admission time
    background:
      enabled: true           # also scan pre-existing PDBs for reports
    mode: Kubernetes          # or JSON, for non-Kubernetes payloads

  # This is the Kubernetes ValidatingAdmissionPolicy matchConstraints shape.
  matchConstraints:
    resourceRules:
      - apiGroups:   ["policy"]
        apiVersions: ["v1"]
        operations:  ["CREATE", "UPDATE"]
        resources:   ["poddisruptionbudgets"]

  # CEL-based early exit. Cheaper than a validation that always evaluates.
  matchConditions:
    - name: not-platform-owned
      expression: >-
        !object.metadata.?labels['app.kubernetes.io/part-of'].orValue('')
          .startsWith('temporal-platform')

  # Named, reusable, lazily-evaluated CEL expressions.
  variables:
    - name: maxUnavailable
      expression: object.spec.?maxUnavailable.orValue(null)
    - name: minAvailable
      expression: object.spec.?minAvailable.orValue(null)

  validations:
    - expression: "variables.maxUnavailable != 0"
      messageExpression: >-
        "PodDisruptionBudget " + object.metadata.name +
        " sets maxUnavailable: 0, which permanently blocks node drain."
      message: "maxUnavailable: 0 is not permitted"
    - expression: "variables.minAvailable != '100%'"
      message: "minAvailable: 100% is not permitted; it blocks node drain forever"

  # Attach structured metadata to the PolicyReport entry.
  auditAnnotations:
    - key: owning-team
      valueExpression: >-
        object.metadata.?labels['team'].orValue('unknown')

  # Emit a native ValidatingAdmissionPolicy so the API server evaluates this
  # in-process. Mutually exclusive with autogen.podControllers.
  autogen:
    validatingAdmissionPolicy:
      enabled: true
```

Notes on the pieces:

- **`matchConditions` vs `validations`.** `matchConditions` decide *whether the policy applies*; `validations` decide *pass or fail*. Put cheap filters in `matchConditions` — they short-circuit.
- **`messageExpression` beats `message`.** If both are set, `messageExpression` wins; if it errors or produces an empty/whitespace/multi-line string, Kyverno falls back to `message` ([ValidatingPolicy — reporting details](https://kyverno.io/docs/policy-types/validating-policy/)). Always set both.
- **`variables` are lazily evaluated at most once, on first reference.** Free to declare, cheap if unused.
- **JSON mode is genuinely useful.** With `evaluation.mode: JSON`, the same engine validates a Terraform plan (the docs' own example checks that `aws_eks_cluster` resources don't expose a public endpoint) — which means your cell IaC and your cell runtime can share one policy engine and one CI harness ([ValidatingPolicy — JSON payloads](https://kyverno.io/docs/policy-types/validating-policy/)).

**`anyPattern`, `pattern`, and `foreach` — where they went.** The legacy `ClusterPolicy` had `validate.pattern` (an overlay-matching DSL), `validate.anyPattern` (a list of alternatives, any of which may match), `validate.deny` with `conditions`, and `validate.foreach` for iterating lists. In the CEL types these all collapse into `spec.validations` ([Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/)):

| Legacy | CEL replacement |
|---|---|
| `validate.pattern` | a `validations[].expression` |
| `validate.anyPattern` | one expression with `\|\|` |
| `validate.cel` | `spec.validations` directly |
| `validate.deny` + `conditions` | a `validations[].expression` with **inverted logic** |
| `validate.foreach` | CEL comprehensions: `all()`, `exists()`, `exists_one()` |
| `preconditions` / `celPreconditions` | `spec.matchConditions` |
| `context` (ConfigMap/API/registry lookups) | `spec.variables` using the CEL libraries |
| `failureAction` | `spec.validationActions` |
| `match` / `exclude` | `spec.matchConstraints` (`resourceRules`, `excludeResourceRules`, `namespaceSelector`) + `matchConditions` |

The `deny` inversion is the one that trips people: legacy `deny` fired when conditions were **true**; a CEL `validations[].expression` passes when it is **true**. Negate when porting.

The `foreach` → comprehension mapping in practice:

```yaml
  validations:
    # every container must set a memory limit
    - expression: >-
        object.spec.containers.all(c, has(c.resources) && has(c.resources.limits)
          && 'memory' in c.resources.limits)
      message: "all containers must set resources.limits.memory"
    # no container may run privileged
    - expression: >-
        !object.spec.containers.exists(c,
          c.?securityContext.?privileged.orValue(false) == true)
      message: "privileged containers are not permitted"
```

#### `MutatingPolicy` — defaults and normalization

Two patch styles. `ApplyConfiguration` is a merge-style CEL expression returning an `Object` — this is the one to reach for. `JSONPatch` is RFC 6902, for surgical edits.

```yaml
apiVersion: policies.kyverno.io/v1
kind: MutatingPolicy
metadata:
  name: default-cell-labels-and-tgp
spec:
  evaluation:
    admission:
      enabled: true
    mutateExisting:
      enabled: false
  reinvocationPolicy: IfNeeded     # Never (default) | IfNeeded
  matchConstraints:
    resourceRules:
      - apiGroups: ["karpenter.sh"]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["nodepools"]
  mutations:
    - patchType: ApplyConfiguration
      applyConfiguration:
        # Force a terminationGracePeriod on every NodePool so that
        # do-not-disrupt pods can never wedge a cell teardown.
        expression: >
          !has(object.spec.template.spec.terminationGracePeriod) ?
          Object{
            spec: Object.spec{
              template: Object.spec.template{
                spec: Object.spec.template.spec{
                  terminationGracePeriod: "4h"
                }
              }
            }
          } : Object{}
```

JSON Patch form, including the escaping helper for keys containing `/`:

```yaml
  mutations:
    - patchType: JSONPatch
      jsonPatch:
        expression: |
          [
            JSONPatch{
              op: "add",
              path: "/metadata/labels/" + jsonpatch.escapeKey("cell.example.com/managed-by"),
              value: "infra-foundation"
            },
            JSONPatch{
              op: "add",
              path: "/metadata/annotations/kyverno.io~1managed",
              value: "true"
            }
          ]
```

**Looping** uses CEL `map()` / `filter()` rather than a `foreach` block — the docs show adding `allowPrivilegeEscalation: false` to every container via `object.spec.containers.map(container, ...)`.

**Mutate-on-existing** is the powerful and dangerous one. With `evaluation.mutateExisting.enabled: true`, a trigger event causes Kyverno to fetch and mutate *all* matching existing resources, not just the triggering object. The docs are blunt about the two caveats ([MutatingPolicy — mutating existing resources](https://kyverno.io/docs/policy-types/mutating-policy/)):

1. **Asynchronous** — variable delay between trigger and mutation.
2. **Custom permissions almost always required**, because these mutations happen outside an `AdmissionReview` and Kyverno's background controller has no default RBAC for arbitrary kinds. Kyverno performs the permission check when the policy is installed.

The trigger/target split is expressed with `targetMatchConstraints` (which may also be a CEL `expression` resolving to an object, since 1.17) and filtered with `targetMatchConditions`. Evaluation is two-phase: trigger phase (match → lazily bind `variables` from `request.object`/`request.oldObject` → `matchConditions` → resolve targets), then target phase (per target: match target rules → `targetMatchConditions` where `object` is now the *target* → mutate with `Object` bound to the target). `variables` are the bridge carrying trigger data into the target phase.

**Ordering caveat, stated plainly in the docs:** mutations *within* one policy run in order, but **the order across multiple MutatingPolicies is not deterministic.** Do not build dependency chains across policies.

#### `GeneratingPolicy` — cell bootstrap

This is the rule type most directly relevant to your job. Two source modes.

**Data source** — the resource is defined in the policy. The YAML-template form (Beta in v1.19) is far more readable than the CEL-object form and is what I would ship:

```yaml
apiVersion: policies.kyverno.io/v1
kind: GeneratingPolicy
metadata:
  name: cell-namespace-baseline
spec:
  evaluation:
    synchronize:
      enabled: true              # keep downstream in sync with the policy
    generateExisting:
      enabled: true              # also apply to namespaces that already exist
    orphanDownstreamOnPolicyDelete:
      enabled: false             # default; delete downstream when policy is deleted
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["namespaces"]
  matchConditions:
    - name: not-system-namespace
      expression: >-
        !object.metadata.name.startsWith('kube-') &&
        object.metadata.name != 'kyverno'
  variables:
    - name: nsName
      expression: object.metadata.name
  generate:
    - template:
        interpolate: cel         # evaluate (( ... )) placeholders
        value: |
          apiVersion: networking.k8s.io/v1
          kind: NetworkPolicy
          metadata:
            name: default-deny-ingress
            namespace: (( variables.nsName ))
          spec:
            podSelector: {}
            policyTypes:
              - Ingress
          ---
          apiVersion: v1
          kind: LimitRange
          metadata:
            name: default-limits
            namespace: (( variables.nsName ))
          spec:
            limits:
              - type: Container
                default:
                  cpu: 500m
                  memory: 512Mi
                defaultRequest:
                  cpu: 100m
                  memory: 128Mi
```

Placeholder semantics are worth knowing precisely ([GeneratingPolicy — YAML templates](https://kyverno.io/docs/policy-types/generating-policy/)): interpolation is **structural, not textual**. A placeholder occupying an *entire value* is spliced natively and may be any CEL type, including maps and lists, with no `dyn()` conversion. A placeholder *embedded in a larger string* must evaluate to a scalar. Placeholders are **not supported in mapping keys** — use a whole-value placeholder on the parent (`labels: (( variables.labels ))`). Escape a literal `((` as `\((`. With the default `interpolate: none`, nothing is evaluated.

**Clone source** — copy an existing resource, the classic image-pull-secret distribution:

```yaml
apiVersion: policies.kyverno.io/v1
kind: GeneratingPolicy
metadata:
  name: distribute-registry-credentials
spec:
  evaluation:
    synchronize:
      enabled: true
    generateExisting:
      enabled: true
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["namespaces"]
  variables:
    - name: nsName
      expression: object.metadata.name
    - name: source
      expression: resource.Get("v1", "secrets", "platform-system", "regcred")
  generate:
    - expression: generator.Apply(variables.nsName, [variables.source])
```

`generator.Apply(namespace, [resources])` returns `true` on success, which is why it composes inside CEL comprehensions — the fan-out pattern is `variables.nsList.all(ns, generator.Apply(ns, variables.downstream))`. Use `resource.List(...)` instead of `resource.Get(...)` to clone every Secret in a namespace.

**Synchronization semantics are the part to get right.** With `synchronize.enabled: true` and a data source:

| Action | Sync | NoSync |
|---|---|---|
| Delete downstream | recreated | deleted |
| Delete policy, `orphanDownstreamOnPolicyDelete: true` | retained | retained |
| Delete policy, `orphanDownstreamOnPolicyDelete: false` (default) | **deleted** | retained |
| Delete trigger (the namespace) | deleted | none |
| Modify downstream | **reverted** | modified |
| Modify policy | synced | unmodified |

With a **clone** source the table differs in an important way: deleting the policy never deletes downstream resources, because their lifecycle is tied to the *source*, and deleting or modifying the **source** propagates to every clone ([GeneratingPolicy — synchronization](https://kyverno.io/docs/policy-types/generating-policy/)).

The row that surprises people: `orphanDownstreamOnPolicyDelete` defaults to **false**, so deleting a GeneratingPolicy with sync on will **delete every resource it generated**. On a cell platform that could mean removing every NetworkPolicy in every namespace. Set it to `true` unless you have deliberately decided otherwise.

**Namespace targeting** for templates comes from each rendered document's `metadata.namespace`; documents without one are treated as cluster-scoped. For a `NamespacedGeneratingPolicy`, missing namespace defaults to the policy's own and any other namespace is rejected. And note the explicit limitation: **fan-out to a runtime-computed set of namespaces is not expressible with templates** — use expression mode with `generator.Apply()` in a CEL loop.

#### `DeletingPolicy` — scheduled cleanup

```yaml
apiVersion: policies.kyverno.io/v1
kind: DeletingPolicy
metadata:
  name: reap-stale-cell-test-namespaces
spec:
  schedule: "0 3 * * *"          # standard cron; minimum granularity 1 minute
  matchPolicy: Equivalent        # Exact | Equivalent (Equivalent recommended)
  deletionPropagationPolicy: Foreground   # Foreground | Background | Orphan
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["*"]
        resources: ["namespaces"]
        scope: "Cluster"
  variables:
    - name: ttlHours
      expression: "72"
  conditions:
    - name: is-ephemeral
      expression: >-
        object.metadata.?labels['cell.example.com/ephemeral'].orValue('') == 'true'
    - name: is-expired
      expression: >-
        timestamp(object.metadata.creationTimestamp) <
          now() - duration('72h')
```

`deletionPropagationPolicy` matters: `Orphan` leaves dependents untouched, `Background` deletes the primary first and lets GC clean dependents asynchronously, `Foreground` is cascading and keeps the primary until dependents are gone ([DeletingPolicy](https://kyverno.io/docs/policy-types/deleting-policy/)). RBAC is the usual stumbling block — the cleanup controller needs `get, list, watch, delete` on the targeted kinds, and the docs note permissions may be missing even for core kinds, surfacing as `pods is forbidden: cannot list resource "pods"`.

The older TTL-label mechanism (`cleanup.kyverno.io/ttl` on individual resources) still exists on the deprecated Cleanup Policy page and is a nice lightweight alternative for one-off cases.

#### `ImageValidatingPolicy` — supply chain

```yaml
apiVersion: policies.kyverno.io/v1
kind: ImageValidatingPolicy
metadata:
  name: require-signed-platform-images
spec:
  validationActions: [Deny]
  failurePolicy: Fail
  webhookConfiguration:
    timeoutSeconds: 20            # registry round-trips are slow; budget for them
  evaluation:
    background:
      enabled: false              # do not re-hit the registry on every scan
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
  matchImageReferences:
    - glob: "ghcr.io/temporalio/*"
    # or: - expression: "image.registry == 'ghcr.io'"
  attestors:
    - name: ci
      cosign:
        keyless:                  # OIDC identity-based, no long-lived key
          identities:
            - issuer: "https://token.actions.githubusercontent.com"
              subjectRegExp: '^https://github\.com/temporalio/[^/]+/\.github/workflows/release\.yml@refs/tags/v.*$'
        ctlog:
          url: "https://rekor.sigstore.dev"
  attestations:
    - name: sbom
      referrer:
        type: sbom/cyclone-dx
    - name: provenance
      intoto:
        type: "https://slsa.dev/provenance/v1"
  validationConfigurations:
    mutateDigest: true            # rewrite tag -> digest, killing mutable tags
    verifyDigest: true
    required: true
  credentials:
    allowInsecureRegistry: false
    providers: ["default", "amazon", "azure", "google"]
    secrets: ["platform-regcred"]   # must live in the Kyverno namespace
  validations:
    - expression: >-
        images.containers.map(image,
          verifyImageSignatures(image, [attestors.ci])).all(e, e > 0)
      message: "image is not signed by the platform CI identity"
    - expression: >-
        images.containers.map(image,
          verifyAttestationSignatures(image, attestations.sbom, [attestors.ci]))
          .all(e, e > 0)
      message: "image has no verified SBOM attestation"
    - expression: >-
        images.containers.map(image,
          extractPayload(image, attestations.sbom).bomFormat == 'CycloneDX')
          .all(e, e)
      message: "SBOM is not CycloneDX"
```

Details that matter:

- `mutateDigest: true` is the highest-value single setting here. It rewrites tags to digests at admission, which eliminates the entire class of "the tag moved under us" problems — and it pairs naturally with the immutability guarantees a cell platform wants.
- `extractPayload()` **requires** prior verification via `verifyAttestationSignatures()`, or it errors.
- Attestation names with `-` must be accessed as `attestations["my-attes"]`; prefer camelCase.
- Notary is supported alongside cosign, with `certs` and `tsaCerts`.
- For a private Sigstore deployment, `keyless.trustedRoot.expression` accepts the full trusted-root JSON, typically read from a ConfigMap via `resource.get(...)`.
- **Registry lookups are network calls in the admission path.** Raise `timeoutSeconds`, and think hard before enabling `background`.

Source for all of the above: [ImageValidatingPolicy](https://kyverno.io/docs/policy-types/image-validating-policy/).

### Variables, context, and the CEL libraries

The legacy `context` block (ConfigMap lookups, API calls, image registry lookups) is now `spec.variables` plus a set of CEL libraries Kyverno adds on top of the Kubernetes CEL environment ([CEL Libraries](https://kyverno.io/docs/policy-types/cel-libraries/)):

| Library | Representative functions |
|---|---|
| Resource | `resource.Get(apiVersion, resource, namespace, name)`, `resource.List(...)`, `resource.Post(...)` (e.g. a live `SubjectAccessReview`) |
| HTTP | `http.Get(url)`, `http.Post(url, body, headers)`, `http.Client(caBundle)` |
| GlobalContext | `globalContext.Get(...)` — reads a cached `GlobalContextEntry` |
| Image | `image("nginx:latest")`, `isImage(...)`, then `.registry()`, `.repository()`, `.identifier()` |
| ImageData | `image.GetMetadata(...)` — OCI metadata: arch, OS, digests, tags, layers |
| User | `parseServiceAccount(request.userInfo.username)` → `.Name`, `.Namespace` |
| Hash | `md5()`, `sha1()`, `sha256()` |
| JSON / YAML | `json.unmarshal(...)`, `yaml.parse(...)` |
| Time | `time.now()`, `time.truncate(...)`, `time.toCron(...)` |
| X509 | `x509.decode(cert)` → issuer, subject, validity, key usage, `IsCA` |
| Math / Random / Transform / gzip | `math.round(v, precision)`, pattern-based random strings, `listObjToMap(...)`, gzip (added in 1.18) |

Note what is **not** there: there is no `quantity` CEL library documented on that page, so resource-quantity comparisons need care. Kubernetes' own CEL environment does provide `quantity()`, and Kyverno's own docs use `quantity(...)` in a `MutatingPolicy` `targetMatchConditions` example ([MutatingPolicy — mutating subresources](https://kyverno.io/docs/policy-types/mutating-policy/)), so it is available via the base environment rather than a Kyverno library. Verify against your version before relying on it.

**JMESPath is legacy.** The JMESPath expression language and Kyverno's custom filters (`{{ request.object.metadata.name }}`, `to_upper()`, `time_since()`, etc.) belong to the `ClusterPolicy` API and are documented under the deprecated section ([ClusterPolicy — JMESPath](https://kyverno.io/docs/policy-types/cluster-policy/jmespath/)). The `kyverno jp` CLI subcommands still exist for working with legacy policies. If you are writing new policy, you are writing CEL, and the mental translation is: `{{ }}` substitution → `variables`; JMESPath filters → CEL comprehensions.

**HTTP calls are a security surface, and were hardened in 1.18.** Loopback and metadata-service addresses are blocked by default, allow/block lists are configurable, and **HTTP calls from namespaced policies are disabled by default** and must be explicitly enabled. This closed [CVE-2026-4789](https://github.com/advisories/GHSA-rggm-jjmc-3394) (SSRF-style abuse). Separately, [CVE-2026-41323](https://github.com/kyverno/kyverno/security/advisories/GHSA-f9g8-6ppc-pqq4) fixed HTTP calls carrying a token that could impersonate Kyverno controllers; calls now carry a scoped token ([Announcing 1.18](https://kyverno.io/blog/2026/04/24/announcing-kyverno-release-1.18/)). If your policies call internal services, read both advisories before enabling the feature.

### Policy Reports, background scans, and the safe rollout path

Reports use the Kubernetes Policy WG API: **`wgpolicyk8s.io/v1alpha2`**, kinds `PolicyReport` (namespaced) and `ClusterPolicyReport` ([Policy Reports](https://kyverno.io/docs/guides/reports/)). Result values are `pass`, `skip`, `warn`, `error`, `fail`. `warn` comes from the `policies.kyverno.io/scored: "false"` annotation converting a `fail`; `error` means variable substitution failed outside preconditions — treat `error` as a **broken policy**, not a failing resource.

Reports reflect **current state only**; entries disappear when resources are deleted. They are not an audit log.

**Background scans** run on a period, **1 hour by default**, controlled by `--backgroundScanInterval` on the **reports** controller ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)). Confusingly, background *scans* are the reports controller's job; the *background controller* handles generate and mutate-existing. They are different Deployments.

**Audit vs Enforce.** In the CEL types this is `spec.validationActions: [Audit|Deny|Warn]`; in legacy ClusterPolicy it is `spec.rules[*].validate[*].failureAction`. The interaction with reports:

| `background: true` | New resource | Existing resource |
|---|---|---|
| Enforce / `Deny` | **Pass results only** (violations are blocked, never persisted) | Reported |
| Audit | Reported | Reported |

Background scan **never blocks existing resources, even in Enforce mode** ([Policy Reports](https://kyverno.io/docs/guides/reports/)). This asymmetry is exactly what makes the safe rollout path work.

**The safe rollout path — this is the operational discipline that makes policy-as-code survivable:**

1. **Write and unit-test offline.** `kyverno test tests/` in CI, against fixtures that assert both pass and fail. No cluster involved.
2. **Ship as `Audit` with `background.enabled: true`.** Deploy to one cell. Nothing is blocked.
3. **Read the reports.** `kubectl get polr -A` and `kubectl get cpolr`. Query for `result: fail` and count distinct owners.
4. **Fix the fleet or write exceptions.** A `PolicyException` grants a scoped bypass without weakening the policy ([Policy Exceptions](https://kyverno.io/docs/guides/exceptions/)). Note that the legacy `kyverno.io` PolicyException is deprecated; CEL policies use the newer form.
5. **Promote to `Deny` on one cell.** Watch admission failures for a full deploy cycle.
6. **Roll to the fleet**, cell by cell, with a kill switch — flipping back to `Audit` is a one-field change, and it is the fastest rollback you have.

```bash
# Step 3, concretely
kubectl get clusterpolicyreport -o json | \
  jq -r '.items[].results[] | select(.result=="fail") |
         [.policy, .rule, (.resources[0].kind), (.resources[0].name)] | @tsv'
```

Two other reporting levers: the label `reports.kyverno.io/disabled` on any policy suppresses all report generation for it (since 1.16), and `--allowedResults` (since 1.17) controls which result types get persisted at all — useful when `pass` results are drowning etcd.

**OpenReports** (`openreports.io/v1alpha1`) is available since 1.15 as an **alpha** alternative behind `--openreportsEnabled`, described as *"an initial step to eventually deprecate wgpolicyk8s and fully depend on openreports.io."* Do not build tooling on it yet, but know it is coming.

### Operational safety

This is the section to read twice, because Kyverno is in the critical path of cell bootstrap.

#### failurePolicy: `Ignore` vs `Fail`

**Kyverno is fail-closed by default.** From the security guide: *"Kyverno policies are configured to fail-closed by default. This setting can be tuned on a per policy basis"* ([Security](https://kyverno.io/docs/guides/security/)). There is a global override, `--forceFailurePolicyIgnore`, and per-policy control via `spec.failurePolicy` ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)).

The right choice is per-policy, not global:

| Policy purpose | failurePolicy | Reasoning |
|---|---|---|
| Security invariant (no privileged pods, image signing) | `Fail` | Bypassing the check is worse than an outage |
| Convenience mutation (default labels, sidecar injection) | `Ignore` | An unlabeled pod is a nuisance; a blocked deploy during an incident is not |
| Anything matching cluster-bootstrap resources | `Ignore` | Never let a policy block the thing that would fix the policy |

For a cell platform I would go further: **any policy whose failure could block cell bootstrap should be `Ignore`, with the invariant enforced by a background scan and an alert instead.** You lose synchronous enforcement; you gain the ability to bring a cell up when Kyverno is unhealthy.

#### Webhook resource scoping — the mitigation that actually works

Kyverno's most important safety property is that **it dynamically generates its webhook rules from the installed policy set**. Before any policy exists, `kyverno-resource-validating-webhook-cfg` is effectively empty; installing the first Pod-matching policy adds exactly one rule ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)). The blast radius is therefore proportional to what you actually asked for.

The corollary from the scaling docs is the thing to fear: *"any policies which match on a wildcard (`*`) will result in Kyverno being forced to process every operation (CREATE, UPDATE, DELETE, and CONNECT) on every resource in the cluster… only a single, simple policy written in such a manner and installed in a large cluster can and will have significant impact"* ([Scaling Kyverno](https://kyverno.io/docs/installation/scaling/)). One wildcard policy converts Kyverno from a targeted gate into a whole-cluster proxy. Ban wildcard `resources: ["*"]` in code review.

`--autoUpdateWebhooks` defaults to `true`. Setting it to `false` makes Kyverno create **default webhook configurations that match ALL resources** with `failurePolicy: Ignore`, and the ConfigMap webhook settings are ignored entirely. That is almost never what you want.

#### namespaceSelector exclusions

Configured through the `webhooks` key of the `kyverno` ConfigMap, which accepts a JSON `namespaceSelector` object. **The Kyverno and `kube-system` Namespaces are excluded by default** ([Configuring Kyverno — namespace selectors](https://kyverno.io/docs/installation/customization/)). The shipped default is:

```json
[{"namespaceSelector":{"matchExpressions":[
  {"key":"kubernetes.io/metadata.name","operator":"NotIn","values":["kube-system"]},
  {"key":"kubernetes.io/metadata.name","operator":"NotIn","values":["kyverno"]}
],"matchLabels":null}}]
```

Separately, `resourceFilters` is a second, engine-level exclusion layer applied *after* the webhook. Format is `[<Kind>,<Namespace>,<Name>]` with wildcards; the documented default includes `[*/*,kyverno,*] [Event,*,*] [*/*,kube-system,*] [*/*,kube-public,*] [*/*,kube-node-lease,*] [Node,*,*] [Node/*,*,*]` and more. Critically: **resource filters do not apply to background scanning** by default, because `--skipResourceFilters` defaults to `true` — so `[*/*,kube-system,*]` still produces background-scan report entries for kube-system. That is a common source of "why is my report full of things I excluded."

The full `kyverno` ConfigMap key set (13 keys) is: `defaultRegistry`, `enableDefaultRegistryMutation`, `excludeGroups` (default `system:serviceaccounts:kube-system,system:nodes`), `excludeUsernames` (default `'!system:kube-scheduler'`), `excludeRoles`, `excludeClusterRoles`, `generateSuccessEvents` (default `false`), `matchConditions`, `resourceFilters`, `updateRequestThreshold` (default `1000`), `webhooks`, `webhookAnnotations`, `webhookLabels`. The ConfigMap name is `kyverno`, overridable via the `INIT_CONFIG` env var, and changes are read dynamically at runtime ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)).

The `excludeGroups` default deserves attention: **`system:nodes` and kube-system service accounts bypass policy entirely.** That is correct for keeping kubelet and control-plane controllers unblocked, and it is also a bypass path a tenant with node-level access could exploit. Know it exists.

#### The HA deployment

Kyverno is **four Deployments**, each a different controller, with different HA characteristics ([High Availability](https://kyverno.io/docs/guides/high-availability/)):

| Controller | Leader election | Replicas buy you | Notes |
|---|---|---|---|
| **Admission** | Only for cert + webhook management | **Availability *and* scale** — inbound `AdmissionReview`s are distributed across all replicas | Required in every install. **Minimum 3 replicas for HA.** |
| **Reports** | Yes | **Availability only** — one replica processes reports | Stateful; vertical scaling helps only the leader |
| **Background** | Yes | **Availability only** — one replica does generate / mutate-existing | Tune `--genWorkers` (default 10) instead |
| **Cleanup** | Mixed (cert/webhook yes, cleanup handler no) | Both, but one replica per CronJob | Handles `CleanupPolicy` and `DeletingPolicy` |

The practical takeaway: **only the admission controller scales horizontally for throughput.** If generate rules are slow on a big cell, adding background-controller replicas does nothing — raise `--genWorkers` and CPU instead.

Ports: admission and cleanup expose **9443** (webhook) and **8000** (metrics); background and reports expose **8000** only and run no webhook. The API server needs ingress to 9443; if you run a default-deny NetworkPolicy in the Kyverno namespace (you should), you must allow it explicitly. Kyverno ships no NetworkPolicy by default; the Helm chart has `networkPolicy.enabled` ([Security](https://kyverno.io/docs/guides/security/)).

#### Resource consumption at scale

The scaling guide's core message is that **node and pod counts are the wrong sizing input**: *"a large production cluster hosting 60,000 Pods yet with no Kyverno policies installed which match on Pod has no bearing on the resources required by Kyverno"* ([Scaling Kyverno](https://kyverno.io/docs/installation/scaling/)). What drives cost is the *shape of the policy set* — what it matches, and how often that fires.

Two concrete recommendations from that page:

- **Do not set CPU limits** on Kyverno. Admission latency is on the critical path of every matching API call, and CPU throttling turns a fast policy into a timeout.
- **Watch API server memory** if you create thousands of policy resources: *"double check that the kube-apiserver pods have head room to increase its memory allocations, otherwise the cluster may crash entirely."*

Published v1.18.1 benchmarks for CEL policies (16 `ValidatingPolicy` resources mirroring the PSS restricted profile, k6 client-measured end-to-end latency including the API server hop): 1 replica at 50 VUs / 5,000 iterations → 57 ms avg, 103 ms p95; 3 replicas at 500 VUs / 10,000 iterations → 321 ms avg, 536 ms p95. The 500-VU single-replica case is flagged in the docs as degraded. Treat these as order-of-magnitude only and measure your own policy set; the runtime signal to watch is `kyverno_admission_review_duration_seconds`.

#### How a Kyverno outage affects a cell coming up

This is the question worth answering explicitly in a design doc.

**If Kyverno's webhooks are registered but its pods are down, with default fail-closed:** every API request matching a registered webhook rule fails. Since Kyverno narrows rules to what your policies match, the impact is scoped — but if you have a policy matching Pods, **no pod can be created anywhere except the excluded namespaces**. Including, potentially, Kyverno's own pods if it were not for the `kyverno` namespace exclusion. That exclusion is load-bearing.

**If Kyverno is not installed yet on a fresh cell:** nothing blocks, because no webhook configurations exist. Kyverno is safe to install *late* in cell bootstrap.

**If Kyverno's generate rules haven't run yet:** namespaces come up without their baseline NetworkPolicy or LimitRange, briefly. Generation is asynchronous. `generateExisting.enabled: true` backfills, but there is a window. If a cell's security posture depends on the default-deny NetworkPolicy existing, the correct design is a validating policy that *rejects pods in namespaces lacking one* — belt and braces — not just a generating policy.

**Design guidance for cells:**

1. Install Kyverno with **3 admission replicas** and a PodDisruptionBudget, on nodes that are not Karpenter-consolidation candidates (or with a generous `karpenter.sh/do-not-disrupt` duration).
2. Order cell bootstrap so Kyverno lands **before** tenant namespaces but **after** the CNI, CoreDNS, and anything Kyverno depends on.
3. Use `failurePolicy: Ignore` for every policy that touches bootstrap-critical resources.
4. Keep the webhook-configuration names in the break-glass runbook. `kubectl delete validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg` is the emergency unblock.
5. Alert on Kyverno admission-controller availability *as a cell health signal*, not just as an app.

*See also: [webhook certificate self-management](11-cert-manager-and-pki.md#webhook-certificate-self-management) for the other half of the fail-closed story — an expired or untrusted webhook serving certificate produces exactly the same cluster-wide outage as dead pods, and the recovery is different.*

### The Kyverno CLI — the reason a platform team picks Kyverno

Two commands.

**`kyverno apply`** runs policies against manifests or a live cluster and prints results. It has the flags you'd want for CI: `--resource`/`--resources`, `--cluster` (evaluate against the current context), `--policy-report` (emit a report), `--table`, `--audit-warn` (treat audit-policy failures as warnings not failures), `--context-file` (supply context data for CEL policies), `--json`/`--http-payload` for JSON-payload policies, `--continue-on-error` (default true), `--batch-size` (100), `--concurrent` (1), `--show-performance`, and `--generate-exceptions` with `--generated-exception-ttl` (default `720h`) to auto-author exceptions for current violations ([kyverno apply](https://kyverno.io/docs/kyverno-cli/reference/kyverno_apply/)).

That `--generate-exceptions` flag is worth calling out for a fleet migration: point it at a cell's existing resources, get a set of time-boxed exceptions, ship the policy in Enforce, and let the exceptions expire as teams remediate.

**`kyverno test`** asserts declared expectations. Given a directory containing a `kyverno-test.yaml`, it compares expected results to actual engine output:

```yaml
# tests/require-pdb/kyverno-test.yaml
name: require-pdb-not-fully-blocking
policies:
  - ../../policies/require-pdb-not-fully-blocking.yaml
resources:
  - resources.yaml
results:
  - policy: require-pdb-not-fully-blocking
    rule: require-pdb-not-fully-blocking
    resource: bad-pdb
    kind: PodDisruptionBudget
    result: fail
  - policy: require-pdb-not-fully-blocking
    rule: require-pdb-not-fully-blocking
    resource: good-pdb
    kind: PodDisruptionBudget
    result: pass
```

Useful flags: `--detailed-results`, `--fail-only`, `-o json|yaml|markdown|junit` (JUnit is what you want for CI reporting), `--require-tests`, and `-t/--test-case-selector` (default `"policy=*,rule=*,resource=*"`). `kyverno test` also accepts a git URL directly, e.g. `kyverno test https://github.com/kyverno/policies/pod-security --git-branch main` ([kyverno test](https://kyverno.io/docs/kyverno-cli/reference/kyverno_test/)). Scaffold a test file with `kyverno create test` ([kyverno create test](https://kyverno.io/docs/kyverno-cli/reference/kyverno_create_test/)).

> **Honest caveat:** I could not find a page in the v1.19 docs that lays out the complete `kyverno-test.yaml` schema. The field names above are confirmed indirectly by `--test-case-selector` (`policy`, `rule`, `resource`) and by `kyverno create test`'s expected-result tuple (`policy-name,rule-name,resource-name,resource-namespace,resource-kind`). Verify against [kyverno/policies](https://github.com/kyverno/policies), which contains hundreds of working test files.

**CI wiring** ([Testing Policies](https://kyverno.io/docs/guides/testing-policies/)):

```yaml
name: kyverno-policy-test
on: [pull_request, workflow_dispatch]
jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - name: Install Kyverno CLI
        uses: kyverno/action-install-cli@v0.2.0
        with:
          release: 'v1.19.0'
      - run: kyverno version
      # 1. New/changed manifests vs the current policy set
      - run: kyverno apply policies/ -r resources/
      # 2. Declared test cases
      - run: kyverno test tests/ -o junit > kyverno-junit.xml
```

The two use cases the docs name are exactly the two a platform team needs: gate *unknown resources* against *known policies* (a tenant's PR), and gate *known resources* against *changing policies* (your own PR, or a Kyverno version bump — pin `release:` to the next version to dry-run an upgrade). For end-to-end testing against a real cluster, [Chainsaw](https://kyverno.io/docs/subprojects/chainsaw/) is the sibling project.

### Pod Security Standards, and PSA

Kyverno ships a curated policy set covering all Pod Security Standards controls, installable as `kustomize build https://github.com/kyverno/policies/pod-security | kubectl apply -f -` or via Helm `kyverno/kyverno-policies` with `--set policyGroups=pod-security` ([Pod Security Standards](https://kyverno.io/docs/guides/pod-security/)).

The mechanism, in the **legacy** ClusterPolicy API, is the `validate.podSecurity` subrule, added in 1.8, which *"by integrating the same libraries as used in Kubernetes' Pod Security Admission"* applies whole profiles or individual controls ([ClusterPolicy — validate](https://kyverno.io/docs/policy-types/cluster-policy/validate/)):

```yaml
apiVersion: kyverno.io/v1     # LEGACY API — deprecated in 1.19
kind: ClusterPolicy
metadata:
  name: psa-baseline
spec:
  rules:
    - name: baseline
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        failureAction: Enforce
        podSecurity:
          level: baseline
          version: latest
```

**Here is the important, easily-missed fact: there is no CEL equivalent.** The migration guide states that `spec.rules.validate.podSecurity` is *"Not supported; use `spec.validations` for each pod security control"* ([Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/)). The `ValidatingPolicy` docs never mention `podSecurity`. Kyverno's own scaling benchmark describes a `vpol-pss` scenario as *"16 ValidatingPolicy resources mirroring the Pod Security Standards restricted profile"* — hand-written CEL, one policy per control.

So: if you plan to adopt PSS via Kyverno and you must be off `ClusterPolicy` before v1.20, budget for either 16-ish hand-written `ValidatingPolicy` resources or a shift to native PSA for the profile-level enforcement. **Verify this against the docs when you actually do it** — parity may land in v1.20.

**Kyverno vs built-in Pod Security Admission.** PSA is namespace-label-driven, baked into the API server since 1.25, and free. Kyverno's advantages, per its own docs: cluster-wide application without an `AdmissionConfiguration` file or control-plane changes; no namespace label required; **individual controls can be exempted from a profile**; container images can be exempted alongside a control; pod-controller enforcement is automatic; violations are visible via policy reports; and the whole thing is CI-testable.

The interop rule is the one operational fact you must know: **PSA and Kyverno are compatible, but pods blocked by PSA in `enforce` mode never generate an `AdmissionReview`**, so Kyverno cannot see or report on them ([ClusterPolicy — PSA interoperability](https://kyverno.io/docs/policy-types/cluster-policy/validate/)). The recommended pattern is PSA enforcing `baseline` cluster-wide as a cheap floor, with Kyverno handling `restricted` and the granular exemptions. For a cell platform that shape is right: PSA is the thing that still works when Kyverno is down.

### A starter policy set a platform team would actually ship

Ordered by value per line of YAML. Ship stages 1 and 2 to every cell; stage 3 where the tenancy model demands it.

**Stage 1 — cell bootstrap (GeneratingPolicy, ship first, low risk).**

1. Every non-system namespace gets a **default-deny-ingress NetworkPolicy**.
2. Every non-system namespace gets a **LimitRange** with sane container defaults.
3. Every non-system namespace gets the platform **image pull secret**, cloned from a source namespace with `synchronize.enabled: true` so credential rotation propagates automatically.
4. Every non-system namespace gets a **ResourceQuota** sized from a cell-tier label.

All four with `generateExisting.enabled: true` and `orphanDownstreamOnPolicyDelete.enabled: true`.

**Stage 2 — platform invariants (ValidatingPolicy, `Audit` → `Deny`).**

5. **No `PodDisruptionBudget` with `maxUnavailable: 0` or `minAvailable: 100%`.** Directly protects your Karpenter drift and consolidation paths from being permanently blocked.
6. **Every `karpenter.sh/v1` NodePool must set `terminationGracePeriod`** — prevents the documented `expireAfter` + `do-not-disrupt` deadlock and guarantees cell teardown terminates.
7. **Every `karpenter.sh/v1` NodePool must declare `disruption.budgets`** and must not use a floating AMI alias (`@latest`) in its referenced NodeClass.
8. **Every workload sets resource requests**, and containers do not run as root or privileged.
9. **Images come from allowed registries** and are **referenced by digest, not tag** — or let `ImageValidatingPolicy` `mutateDigest` fix it.
10. **No `hostNetwork`, `hostPID`, `hostIPC`, or `hostPath`** outside an explicit allowlist.
11. **Required ownership labels** on every namespace and workload — this is what makes reports actionable.

**Stage 3 — supply chain and hygiene.**

12. **`ImageValidatingPolicy`**: platform images must carry a valid cosign keyless signature from your CI identity, with `mutateDigest: true`.
13. **`MutatingPolicy`**: stamp cell identity labels (`cell.example.com/cell`, region, cloud) onto every namespace and workload, `failurePolicy: Ignore`.
14. **`DeletingPolicy`**: reap ephemeral/test namespaces past a TTL, and completed Jobs older than N days.

Every one of these gets a `kyverno test` fixture with a passing and a failing case, and the whole set runs in CI on every PR. Start from [kyverno/policies](https://github.com/kyverno/policies) rather than from scratch — but expect to port from `ClusterPolicy` to CEL, and check the repo's current state before assuming the samples are already migrated.

*See also: [disruption in depth](06-karpenter.md#disruption-in-depth) for the exact Karpenter behaviors rules 5–7 are defending, and [PDB, drain, and eviction semantics](04-managed-kubernetes-eks-gke-aks.md#lab-2--pdb-drain-and-eviction-semantics-kind-free) for the lab that shows you what a deadlocked PDB looks like from the drain side.*

---

## Hands-on

Everything here runs on a `kind` cluster. Unlike Karpenter, Kyverno needs no cloud at all — it is pure Kubernetes.

### Lab 0 — install

```bash
kind create cluster --name kyverno-lab --image kindest/node:v1.34.0

helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update

# HA-shaped install: 3 admission replicas, matching production.
helm install kyverno kyverno/kyverno \
  --namespace kyverno --create-namespace \
  --set admissionController.replicas=3 \
  --set backgroundController.replicas=1 \
  --set reportsController.replicas=1 \
  --set cleanupController.replicas=1

kubectl -n kyverno get deploy
kubectl get crd | grep -E 'kyverno|wgpolicy'

# See the dynamically-generated webhooks -- note how empty they are with no policies.
kubectl get validatingwebhookconfiguration | grep kyverno
kubectl get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg \
  -o jsonpath='{.webhooks[*].rules}' | jq .
```

Install the CLI too — you will use it more than the cluster:

```bash
# https://kyverno.io/docs/subprojects/kyverno-cli/
brew install kyverno jq yq   # jq and yq are used by later labs
kyverno version
```

### Lab 1 — a ValidatingPolicy, offline first

Set up a lab directory and write the policy from the PDB example above to `policies/require-pdb-not-fully-blocking.yaml`. Every later lab assumes you are sitting in `~/kyverno-lab`, so mind the `cd` at the end.

```bash
mkdir -p ~/kyverno-lab/policies && cd ~/kyverno-lab
# ... paste the ValidatingPolicy into policies/require-pdb-not-fully-blocking.yaml ...

mkdir -p tests/require-pdb && cd tests/require-pdb
cat > resources.yaml <<'EOF'
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: bad-pdb
  namespace: default
spec:
  maxUnavailable: 0
  selector: {matchLabels: {app: x}}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: good-pdb
  namespace: default
spec:
  maxUnavailable: 1
  selector: {matchLabels: {app: x}}
EOF

# Run the real engine offline. No cluster involved.
kyverno apply ../../policies/ -r resources.yaml --table
```

Then add the `kyverno-test.yaml` from earlier and run `kyverno test .`. **Deliberately break it** — flip an expected `fail` to `pass` — and confirm the command exits non-zero. A test harness you have never seen fail is not a test harness.

Then go back to the lab root: `cd ~/kyverno-lab`.

### Lab 2 — Audit → Deny, and read the reports

First edit the policy to start in `Audit` — the example above ships `validationActions: [Deny]`, and the whole point of this lab is to promote it. Then:

```bash
kubectl apply -f policies/require-pdb-not-fully-blocking.yaml   # validationActions: [Audit]

kubectl apply -f tests/require-pdb/resources.yaml   # both succeed; nothing is blocked

kubectl get clusterpolicyreport,policyreport -A
kubectl get policyreport -n default -o yaml | \
  yq '.items[].results[] | select(.result=="fail")'
```

Now flip `validationActions: [Deny]`, delete and re-apply `bad-pdb`, and read the rejection message carefully — confirm `messageExpression` produced the dynamic form with the object name in it. Then flip to `[Warn]` and observe a `kubectl` warning instead of a rejection.

Force a background scan cycle by lowering the interval:

```bash
kubectl -n kyverno set env deploy/kyverno-reports-controller \
  --containers=controller KYVERNO_EXPERIMENTAL= --overwrite   # no-op env poke to restart
# or set the flag directly:
kubectl -n kyverno patch deploy kyverno-reports-controller --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--backgroundScanInterval=1m"}
]'
```

### Lab 3 — a GeneratingPolicy, and the deletion trap

Apply the `cell-namespace-baseline` policy from above, then:

```bash
kubectl create ns tenant-a
kubectl -n tenant-a get networkpolicy,limitrange     # generated asynchronously

# Prove synchronization: tamper with the downstream, watch it revert.
kubectl -n tenant-a delete networkpolicy default-deny-ingress
sleep 5 && kubectl -n tenant-a get networkpolicy      # recreated

kubectl -n tenant-a patch networkpolicy default-deny-ingress --type=json \
  -p='[{"op":"add","path":"/spec/policyTypes/-","value":"Egress"}]'
sleep 5 && kubectl -n tenant-a get networkpolicy default-deny-ingress -o yaml   # reverted
```

**Now the trap.** With `orphanDownstreamOnPolicyDelete.enabled: false` (the default):

```bash
kubectl create ns tenant-b && sleep 5
kubectl delete generatingpolicy cell-namespace-baseline
sleep 10
kubectl -n tenant-a get networkpolicy    # GONE. Same for tenant-b.
```

Every generated resource across every namespace is deleted. Re-apply with `orphanDownstreamOnPolicyDelete.enabled: true` and repeat to see the safe behavior. Do this once on kind so you never do it in production.

Also try `generateExisting.enabled: true` on a cluster that already has ten namespaces and watch the one-time backfill.

### Lab 4 — break the cluster on purpose

This is the most valuable twenty minutes in the guide.

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: policies.kyverno.io/v1
kind: ValidatingPolicy
metadata:
  name: danger-wildcard
spec:
  validationActions: [Deny]
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: ["*"]
        apiVersions: ["*"]
        operations: ["CREATE", "UPDATE"]
        resources: ["*"]
  validations:
    - expression: "true"
      message: "never fires"
EOF

# Look at what you just did to the webhook configuration.
kubectl get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg \
  -o jsonpath='{.webhooks[*].rules}' | jq .

# Now take Kyverno down.
kubectl -n kyverno scale deploy kyverno-admission-controller --replicas=0

# And try to do anything.
kubectl create ns should-fail
kubectl -n default run test --image=nginx
```

Observe: the cluster is effectively read-only for most namespaces, **except** `kube-system` and `kyverno`, which are excluded by default. Then recover the two ways:

```bash
# Recovery A -- restore the service.
kubectl -n kyverno scale deploy kyverno-admission-controller --replicas=3

# Recovery B -- break glass. Know this command before you need it.
kubectl delete validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg
```

Repeat with `failurePolicy: Ignore` and confirm nothing breaks. This single experiment teaches the entire operational risk model.

Clean up before moving on, or every later lab pays the wildcard webhook's latency on every single API write:

```bash
kubectl delete validatingpolicy danger-wildcard
```

### Lab 5 — ImageValidatingPolicy against a real signature

```bash
# Kyverno's own images are cosign-signed with a published public key.
cat <<'EOF' | kubectl apply -f -
apiVersion: policies.kyverno.io/v1
kind: ImageValidatingPolicy
metadata:
  name: verify-kyverno-images
spec:
  validationActions: [Deny]
  webhookConfiguration:
    timeoutSeconds: 20
  evaluation:
    background:
      enabled: false
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
  matchImageReferences:
    - glob: "ghcr.io/kyverno/*"
    # Included so the lab has a guaranteed-unsigned counterexample below.
    - glob: "docker.io/*"
  attestors:
    - name: cosign
      cosign:
        keyless:
          identities:
            - issuer: "https://token.actions.githubusercontent.com"
              subjectRegExp: ".*github\\.com/kyverno/.*"
  validations:
    - expression: >-
        images.containers.map(image,
          verifyImageSignatures(image, [attestors.cosign])).all(e, e > 0)
      message: "image signature verification failed"
EOF

kubectl run signed   --image=ghcr.io/kyverno/kyverno:v1.19.0   # should pass
kubectl run unsigned --image=nginx:1.27                        # expect failure: no
                                                               # cosign signature from
                                                               # the kyverno GH identity
```

Time the admission latency with `kubectl --v=6` and compare against a plain `ValidatingPolicy`. The registry round-trip is real and it is the reason `timeoutSeconds` matters here.

### Lab 6 — VAP autogen

Add `autogen.validatingAdmissionPolicy.enabled: true` to the Lab 1 policy, then:

```bash
kubectl get validatingadmissionpolicy
kubectl get validatingadmissionpolicybinding
kubectl get validatingadmissionpolicy <generated-name> -o yaml
```

Scale the Kyverno admission controller to zero and confirm the generated VAP **still enforces**, because the API server evaluates it in-process. That is the resilience argument in one command. Then add `autogen.podControllers` alongside it and confirm the VAP is *not* generated and the policy status explains why.

Teardown: `kind delete cluster --name kyverno-lab`.

---

## Production gotchas

1. **The API you learned from the internet is being removed.** `ClusterPolicy`, `Policy`, `CleanupPolicy`, and the legacy `kyverno.io` `PolicyException` are **deprecated in v1.19 and removed in v1.20** ([Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/)). With a ~3-month minor cadence, v1.20 is close. Any policy you write today in `kyverno.io/v1` is technical debt with a known due date. Write CEL.

2. **`kyverno migrate` will not migrate your policies.** It migrates CRD *stored versions* ([kyverno migrate](https://kyverno.io/docs/kyverno-cli/reference/kyverno_migrate/)). The ClusterPolicy → CEL conversion is manual. Budget real engineering time, especially for `validate.podSecurity`, which has **no CEL equivalent** and must be rewritten as ~16 individual `ValidatingPolicy` expressions.

3. **A single wildcard policy turns Kyverno into a whole-cluster proxy.** *"Only a single, simple policy written in such a manner and installed in a large cluster can and will have significant impact"* ([Scaling Kyverno](https://kyverno.io/docs/installation/scaling/)). Ban `resources: ["*"]` and `apiGroups: ["*"]` in review, and add a `kyverno test` fixture that asserts your own policy set does not produce a wildcard webhook rule.

4. **Kyverno is fail-closed by default, and that is a deliberate footgun.** ([Security](https://kyverno.io/docs/guides/security/)) Choose `failurePolicy` per policy, not globally. Anything that could block cell bootstrap should be `Ignore` with the invariant backed by a background scan and an alert.

5. **`orphanDownstreamOnPolicyDelete` defaults to `false`.** Deleting a `GeneratingPolicy` with sync enabled **deletes every resource it ever generated**, fleet-wide ([GeneratingPolicy — synchronization](https://kyverno.io/docs/policy-types/generating-policy/)). Set it to `true` on every generating policy unless you have explicitly reasoned otherwise. Lab 3 reproduces this in under a minute; do it once.

6. **Generation is asynchronous, so there is always a window.** A namespace exists before its default-deny NetworkPolicy does. If the security posture depends on that object, pair the `GeneratingPolicy` with a `ValidatingPolicy` that rejects workloads in namespaces lacking it.

7. **Mutate-existing needs RBAC you do not have by default.** *"Custom permissions are almost always required. Because these mutations occur on existing resources and not during an AdmissionReview, Kyverno may need additional permissions which it does not have by default"* ([MutatingPolicy](https://kyverno.io/docs/policy-types/mutating-policy/)). Kyverno checks at install time, so the failure is loud — but it means the policy silently does nothing until you grant the aggregated ClusterRole. Same applies to `DeletingPolicy` and the cleanup controller.

8. **Mutation order across policies is not deterministic.** Within one `MutatingPolicy`, mutations run top to bottom; across policies, *"their execution order is not deterministic"* ([MutatingPolicy — mutate ordering](https://kyverno.io/docs/policy-types/mutating-policy/)). If mutation B depends on mutation A, they must be in the same policy. `reinvocationPolicy: IfNeeded` helps with interactions but does not give you ordering.

9. **Only the admission controller scales horizontally.** Reports and background are leader-elected: extra replicas buy availability, not throughput ([High Availability](https://kyverno.io/docs/guides/high-availability/)). Slow generate rules are fixed with `--genWorkers` and CPU, not replicas. Minimum for admission HA is **3**.

10. **Do not set CPU limits on Kyverno.** The scaling guide recommends this explicitly. Throttling the admission controller turns a 50 ms policy into a webhook timeout, which — fail-closed — becomes a rejected API request.

11. **`resourceFilters` does not apply to background scans.** `--skipResourceFilters` defaults to `true`, so *"anything defined in the resourceFilters will not be excluded in background reports"* ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)). Excluded namespaces still show up in `PolicyReport`s. Filter at query time, or flip the flag.

12. **`excludeGroups` gives `system:nodes` a policy bypass by default.** Default is `system:serviceaccounts:kube-system,system:nodes` ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)). Necessary to keep the kubelet unblocked; also a real bypass path. Know it, and do not rely on Kyverno as your only defense against node-level compromise.

13. **`--autoUpdateWebhooks=false` is worse than it sounds.** It makes Kyverno create default webhook configurations matching **all resources**, and the ConfigMap webhook settings are ignored ([Configuring Kyverno](https://kyverno.io/docs/installation/customization/)). Leave it `true`.

14. **PSA blocks before Kyverno sees anything.** *"Pods which are blocked by PSA in enforce mode do not result in an AdmissionReview request being sent to admission controllers"* ([ClusterPolicy — PSA interoperability](https://kyverno.io/docs/policy-types/cluster-policy/validate/)). If both are enforcing, your Kyverno reports will have a blind spot exactly where PSA is strictest.

15. **`result: error` in a report means a broken policy, not a failing resource.** It signals variable substitution failure outside preconditions ([Policy Reports](https://kyverno.io/docs/guides/reports/)). Alert on `error` separately from `fail`, and treat it as a page-worthy platform bug.

16. **`extractPayload()` errors without prior verification.** It requires a preceding `verifyAttestationSignatures()` call ([ImageValidatingPolicy](https://kyverno.io/docs/policy-types/image-validating-policy/)). And attestation names with hyphens need bracket access (`attestations["my-attes"]`) — prefer camelCase.

17. **Image verification puts a registry round-trip in the admission path.** Raise `webhookConfiguration.timeoutSeconds` (max 30) and think twice before enabling `background` for `ImageValidatingPolicy`, or every scan re-hits the registry. `credentials.secrets` must live in the **Kyverno namespace**, not the workload's.

18. **HTTP calls from policies are a real attack surface and were CVE-worthy.** Loopback and metadata addresses are blocked by default, and namespaced-policy HTTP calls are off by default since 1.18 ([CVE-2026-4789](https://github.com/advisories/GHSA-rggm-jjmc-3394), [CVE-2026-41323](https://github.com/kyverno/kyverno/security/advisories/GHSA-f9g8-6ppc-pqq4)). If you enable them, treat the allowlist as a security control with a review process.

19. **Support is only ~3 months.** "main + 1" since v1.18 ([Announcing 1.18](https://kyverno.io/blog/2026/04/24/announcing-kyverno-release-1.18/)). Across a fleet of cells that means Kyverno needs its own continuous upgrade pipeline, and `kyverno test` with the next version's CLI pinned is the cheapest dry-run you will ever get.

20. **Kubernetes support is a narrow window: v1.33–v1.35 for v1.19** ([Releases](https://kyverno.io/docs/installation/releases/)). Cell Kubernetes upgrades and Kyverno upgrades are coupled and must be sequenced together, exactly like Karpenter.

21. **Thousands of policy objects can take out the API server.** *"If you create several thousand Kyverno policy resources, double check that the kube-apiserver pods have head room to increase its memory allocations, otherwise the cluster may crash entirely"* ([Scaling Kyverno](https://kyverno.io/docs/installation/scaling/)). Namespaced policy types make it easy for tenants to create a lot of objects; quota them.

22. **Autogen modes are mutually exclusive.** Setting `spec.autogen.podControllers` silently disables `validatingAdmissionPolicy`/`mutatingAdmissionPolicy` generation, with the reason reported only in the policy status ([ValidatingPolicy — autogen](https://kyverno.io/docs/policy-types/validating-policy/)). If you enabled VAP generation for resilience and later added pod-controller autogen, you lost the resilience without an error.

---

## How this shows up in cell lifecycle

**Cell bootstrap ordering.** Kyverno's install position is a real design decision. It must land after the CNI and CoreDNS (it needs networking and DNS), after cert-manager if you use it for webhook certs, and before any tenant namespace exists — because generating policies only fire on namespace creation unless you enable `generateExisting`. A pragmatic order: cluster → CNI → CoreDNS → Karpenter → Kyverno (with generating policies) → platform namespaces → tenant namespaces. Because Kyverno registers no webhook rules until policies exist, installing the controller early and the *policies* late is a safe way to decouple these. That ordering is the L4–L8 slice of the full cell dependency DAG in [12-cell-lifecycle-synthesis.md](12-cell-lifecycle-synthesis.md#the-dependency-dag-of-bringing-up-a-cell).

**Kyverno as the cell's namespace-provisioning API.** This is the highest-leverage use. Rather than a bespoke controller that watches namespaces and creates baseline objects, a `GeneratingPolicy` with `synchronize` and `generateExisting` *is* that controller, declaratively, with drift correction built in. Changing the baseline is a one-file change that reconciles across every namespace in every cell. Two conditions make it safe: `orphanDownstreamOnPolicyDelete: true`, and the fan-out limitation (templates cannot target a runtime-computed namespace set) understood up front.

**Kyverno as the guard on the other guides' invariants.** Almost every gotcha in the [Karpenter guide](06-karpenter.md#production-gotchas) is expressible as a Kyverno policy, which is the honest argument for running it at all:

| Karpenter risk | Kyverno policy |
|---|---|
| `expireAfter` + `do-not-disrupt` deadlock | Deny NodePools lacking `terminationGracePeriod` |
| `alias: al2023@latest` rolls the fleet | Deny EC2NodeClass with a floating alias |
| Default 10% budget on a 200-node cell | Deny NodePools without explicit `disruption.budgets` |
| `maxUnavailable: 0` PDB immortalizes nodes | Deny such PDBs outside an allowlist |
| Labels consuming the 100-requirement ceiling | Validate label count on NodePool templates |

Each is ten lines of CEL and a test fixture, and each converts a tribal-knowledge incident into a compile-time error.

**Cell upgrade.** Kyverno is a coupled upgrade with two constraints: the Kubernetes support window (v1.33–v1.35 for v1.19) and the 3-month patch window. The mitigation is unusually cheap here: pin the CLI to the *target* version in CI and run `kyverno test` against your whole policy set. If the tests pass, the policy set is compatible. Do that before touching a cell.

**Cell teardown.** Two failure modes. First, generating policies with sync will *fight* your teardown if you delete namespaces before deleting the policies — Kyverno may recreate objects in a terminating namespace. Delete generating policies (with orphan enabled, or accept the cascade) before namespaces. Second, the webhook configurations are cluster-scoped and outlive a `helm uninstall` of the workload if the uninstall path is not clean; a fail-closed orphaned webhook pointed at a service that no longer exists is the classic bricked cluster. `kubectl get validatingwebhookconfiguration,mutatingwebhookconfiguration | grep kyverno` belongs in your teardown verification.

**Multi-cloud.** Kyverno is one of the few pieces of this stack that is genuinely cloud-agnostic — it is pure Kubernetes and behaves identically on EKS, GKE, and AKS. Two caveats worth checking in [Platform Notes](https://kyverno.io/docs/installation/platform-notes/): private GKE clusters require a firewall rule for the control plane to reach the webhook port, and some managed offerings have their own admission controllers that interact with yours.

**Helm-for-templating-only.** Kyverno's chart renders cleanly with `helm template`. CRDs are the usual caveat — render them into the manifest set explicitly rather than relying on Helm's CRD hook. This is the same discipline as the Karpenter CRDs, and the same pipeline stage can handle both.

---

## Learning path

**Day 1 — get the current API in your head, and only the current API.**

- Read [Policy Types — Overview](https://kyverno.io/docs/policy-types/) and [ValidatingPolicy](https://kyverno.io/docs/policy-types/validating-policy/) end to end. Ignore everything under "ClusterPolicy Deprecated" for now.
- Read [Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/) — even with no policies to migrate, its field-mapping table is the fastest way to recognize stale examples.
- Run Lab 0 and Lab 1. Get a policy passing offline in `kyverno apply` before you ever apply it to a cluster.

**Week 1 — build the operational instincts.**

- Run Labs 2, 3, and **4**. Lab 4 (breaking the cluster with a fail-closed wildcard policy, then break-glass recovery) is the highest-value exercise here; do not skip it.
- Read [High Availability](https://kyverno.io/docs/guides/high-availability/) and [Security](https://kyverno.io/docs/guides/security/). Write the seven webhook-configuration names into the cell runbook.
- Read [Configuring Kyverno](https://kyverno.io/docs/installation/customization/) with your finger on the `webhooks`, `resourceFilters`, and `excludeGroups` keys. Know what is excluded by default and why.
- Read [Scaling Kyverno](https://kyverno.io/docs/installation/scaling/) and internalize the wildcard warning.
- Wire `kyverno test` into a real CI pipeline with JUnit output. Watch it fail on a bad PR.
- Write and ship Stage 1 of the starter set (the four generating policies) to a kind cluster, with tests.

**Month 1 — own it.**

- Write and ship Stage 2, following the full audit → report → enforce path on one real cell, and document the promotion criteria.
- Read [GeneratingPolicy](https://kyverno.io/docs/policy-types/generating-policy/) and [MutatingPolicy](https://kyverno.io/docs/policy-types/mutating-policy/) completely, including the synchronization and evaluation-order tables. These are the two with the most surprising semantics.
- Run Lab 5 and Lab 6, and make a real call on VAP autogen: does your platform want in-process enforcement for the security-critical policies?
- Plan the `validate.podSecurity` migration explicitly, since there is no CEL equivalent today. Decide between hand-written CEL and native PSA, and re-verify the docs at decision time.
- Read [Policy Exceptions](https://kyverno.io/docs/guides/exceptions/) and design the exception workflow *before* you turn anything to `Deny` — who can create one, does it expire, is it reviewed. `kyverno apply --generate-exceptions --generated-exception-ttl` is a good migration primitive.
- Evaluate [Chainsaw](https://kyverno.io/docs/subprojects/chainsaw/) for end-to-end tests against a real cell, and [Policy Reporter](https://kyverno.io/docs/subprojects/policy-reporter/) for a fleet-wide reports UI.
- Read the [Gatekeeper Migration Guide](https://kyverno.io/docs/guides/gatekeeper/) and [Evaluating Policy Engines](https://kyverno.io/docs/guides/evaluating-policy-engines/) so you can defend the tool choice in a design review.

---

## References

1. [Kyverno Documentation (v1.19)](https://kyverno.io/docs/introduction/) — the canonical source; use the version selector in the header and never trust a version-less search result.
2. [Kyverno — Policy Types overview](https://kyverno.io/docs/policy-types/) — the map of the new CEL policy family and when each type was introduced.
3. [Kyverno — ValidatingPolicy](https://kyverno.io/docs/policy-types/validating-policy/) — full spec, VAP comparison table, `autogen`, `evaluation`, JSON payloads, `messageExpression`, `auditAnnotations`.
4. [Kyverno — MutatingPolicy](https://kyverno.io/docs/policy-types/mutating-policy/) — `ApplyConfiguration` vs `JSONPatch`, mutate-existing, target resolution phases, `reinvocationPolicy`, ordering caveats.
5. [Kyverno — GeneratingPolicy](https://kyverno.io/docs/policy-types/generating-policy/) — data vs clone source, YAML templates and placeholder semantics, synchronization tables, `generateExisting`, `orphanDownstreamOnPolicyDelete`.
6. [Kyverno — DeletingPolicy](https://kyverno.io/docs/policy-types/deleting-policy/) — schedules, `deletionPropagationPolicy`, `matchPolicy`, RBAC requirements.
7. [Kyverno — ImageValidatingPolicy](https://kyverno.io/docs/policy-types/image-validating-policy/) — cosign/Notary attestors, keyless identities, attestations, `validationConfigurations`, registry credentials.
8. [Kyverno — Cleanup Policy (deprecated)](https://kyverno.io/docs/policy-types/cleanup-policy/) — the deprecation notice and the `cleanup.kyverno.io/ttl` label mechanism.
9. [Kyverno — CEL Libraries](https://kyverno.io/docs/policy-types/cel-libraries/) — the 14 libraries Kyverno adds on top of Kubernetes CEL, with function names.
10. [Kyverno — Migrating to CEL Policies](https://kyverno.io/docs/guides/migration-to-cel/) — the deprecation notice, the complete legacy→CEL field mapping, and the list of unsupported features (`validate.podSecurity`, `validate.manifests`).
11. [Kyverno — Releases](https://kyverno.io/docs/installation/releases/) — current version, Kubernetes support window, the "main + 1" patch policy, release cadence.
12. [Kyverno — Installation](https://kyverno.io/docs/installation/installation/) — Helm install including the HA section.
13. [Kyverno — Configuring Kyverno](https://kyverno.io/docs/installation/customization/) — the 13 ConfigMap keys with defaults, every container flag, webhook management, namespace selectors, resource filters.
14. [Kyverno — Scaling Kyverno](https://kyverno.io/docs/installation/scaling/) — sizing guidance, the wildcard-policy warning, published CEL benchmarks, the API-server memory warning.
15. [Kyverno — High Availability](https://kyverno.io/docs/guides/high-availability/) — the four controllers, which use leader election, and what replicas actually buy you.
16. [Kyverno — Security](https://kyverno.io/docs/guides/security/) — fail-closed default, the seven webhook configuration names, ports, threat model and mitigations.
17. [Kyverno — Policy Reports](https://kyverno.io/docs/guides/reports/) — `wgpolicyk8s.io/v1alpha2`, intermediary report kinds, result values, the Audit/Enforce reporting matrix, OpenReports.
18. [Kyverno — Policy Exceptions](https://kyverno.io/docs/guides/exceptions/) — scoped bypasses, including the CEL-policy form.
19. [Kyverno — Testing Policies](https://kyverno.io/docs/guides/testing-policies/) — the two CI use cases and a working GitHub Actions workflow.
20. [Kyverno — Applying Policies](https://kyverno.io/docs/guides/applying-policies/) — how policies are evaluated in-cluster and via CLI.
21. [Kyverno — Pod Security Standards](https://kyverno.io/docs/guides/pod-security/) — the curated PSS policy set and how to install it (short page; the real content is in reference 22).
22. [Kyverno — ClusterPolicy validate rules](https://kyverno.io/docs/policy-types/cluster-policy/validate/) — the `validate.podSecurity` subrule, its advantages over PSA, and the critical PSA interoperability note. Legacy API, still the only PSS documentation.
23. [Kyverno — Kubernetes Admission Controllers](https://kyverno.io/docs/guides/admission-controllers/) — admission fundamentals in Kyverno's own words.
24. [Kyverno — Monitoring](https://kyverno.io/docs/guides/monitoring/) and [Metrics reference](https://kyverno.io/docs/reference/metrics/) — including `kyverno_admission_review_duration_seconds`.
25. [Kyverno — Troubleshooting](https://kyverno.io/docs/guides/troubleshooting/) — symptom-indexed debugging.
26. [Kyverno — Platform Notes](https://kyverno.io/docs/installation/platform-notes/) — per-provider caveats (private GKE firewall rules, managed admission interactions).
27. [Kyverno — Evaluating Policy Engines](https://kyverno.io/docs/guides/evaluating-policy-engines/) — Kyverno's own comparison framing; read critically, it is not neutral.
28. [Kyverno — Gatekeeper Migration Guide](https://kyverno.io/docs/guides/gatekeeper/) — concept-by-concept mapping from Rego/Constraints to Kyverno.
29. [Kyverno CLI — `kyverno apply`](https://kyverno.io/docs/kyverno-cli/reference/kyverno_apply/) — every flag, including `--generate-exceptions` and `--cluster`.
30. [Kyverno CLI — `kyverno test`](https://kyverno.io/docs/kyverno-cli/reference/kyverno_test/) — test invocation, output formats, and the test-case selector.
31. [Kyverno CLI — `kyverno create test`](https://kyverno.io/docs/kyverno-cli/reference/kyverno_create_test/) — scaffolds a `kyverno-test.yaml`; the closest thing to schema documentation.
32. [Kyverno CLI — `kyverno migrate`](https://kyverno.io/docs/kyverno-cli/reference/kyverno_migrate/) — proof that this is a stored-version tool, not a policy converter.
33. [Kyverno CLI subproject](https://kyverno.io/docs/subprojects/kyverno-cli/) — install methods and overall CLI docs.
34. [Announcing Kyverno 1.18](https://kyverno.io/blog/2026/04/24/announcing-kyverno-release-1.18/) — the "main + 1" support change, HTTP hardening, CLI expansion, and the ClusterPolicy deprecation reminder.
35. [Announcing Kyverno 1.17](https://kyverno.io/blog/2026/02/02/announcing-kyverno-release-1.17/) — CEL policy engine stabilization.
36. [kyverno/kyverno releases](https://github.com/kyverno/kyverno/releases) — authoritative version list and per-release notes.
37. [kyverno/policies](https://github.com/kyverno/policies) — hundreds of production sample policies with working `kyverno test` fixtures. Best starting point for a policy library.
38. [CNCF — Kyverno graduation announcement](https://www.cncf.io/announcements/2026/03/24/cloud-native-computing-foundation-announces-kyvernos-graduation/) — March 2026 graduation.
39. [CVE-2026-4789 advisory](https://github.com/advisories/GHSA-rggm-jjmc-3394) — SSRF via HTTP CEL calls; the reason for the 1.18 allowlist/blocklist.
40. [CVE-2026-41323 advisory](https://github.com/kyverno/kyverno/security/advisories/GHSA-f9g8-6ppc-pqq4) — token impersonation via HTTP calls; the reason for scoped tokens.
41. [Kubernetes — Validating Admission Policy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/) — the built-in CEL alternative, GA since Kubernetes 1.30.
42. [Kubernetes — Mutating Admission Policy](https://kubernetes.io/docs/reference/access-authn-authz/mutating-admission-policy/) — the newer mutating counterpart; check your control-plane version and feature gates.
43. [Kubernetes — Dynamic Admission Control](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/) — webhook ordering, `failurePolicy`, timeouts, `reinvocationPolicy`, and the "avoid deadlocking your cluster" section.
44. [Kubernetes — Pod Security Admission](https://kubernetes.io/docs/concepts/security/pod-security-admission/) — the built-in PSS enforcement Kyverno interoperates with.
45. [Open Policy Agent Gatekeeper](https://open-policy-agent.github.io/gatekeeper/website/docs/) — the Rego-based alternative, for the comparison table.
46. [Kyverno Chainsaw](https://kyverno.io/docs/subprojects/chainsaw/) — end-to-end policy testing against a live cluster, complementing `kyverno test`.
47. [Policy Reporter](https://kyverno.io/docs/subprojects/policy-reporter/) — UI and alerting over `PolicyReport` resources; the practical way to consume reports across a fleet.
48. [Kyverno Playground](https://playground.kyverno.io/) — browser-based policy evaluation; fastest way to iterate on a CEL expression.
