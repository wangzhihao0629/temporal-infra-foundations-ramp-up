# GitOps — Argo CD and Flux, at Fleet Scale

**Why this matters.** Every other guide in this library hands off to GitOps and none of them teaches it. [08-terraform.md](08-terraform.md) stops at "Terraform installs the agent and gets out of the way." [09-helm.md](09-helm.md)'s entire templating-only argument depends on something else owning the inventory, the pruning, and the health assessment that Helm's release record would otherwise provide. [12-cell-lifecycle-synthesis.md](12-cell-lifecycle-synthesis.md)'s steady-state loop *is* a GitOps loop. That "something else" is the reconciler, and on a cell-lifecycle team it is the single most load-bearing component you own: it is what turns "provision a cell" from a script into a commit, what makes "upgrade 300 cells" a scheduling problem rather than 300 scripts, and what makes teardown a directory deletion. You will be reading its logs at 3 a.m. more than any other component's.

Everything below was verified against primary sources on **2026-08-29**. Where I could not verify something, I say so.

> **Stale-content trap, read this first.** Six things most GitOps material gets wrong today:
>
> 1. **`argo-cd.readthedocs.io/en/stable/` is not current.** Read the Docs builds the `stable` slug from the git branch named `stable`, and that branch's `VERSION` file currently reads **3.4.5** ([VERSION on stable](https://raw.githubusercontent.com/argoproj/argo-cd/stable/VERSION)) while the latest release is **v3.5.2, published 2026-08-27** ([releases](https://github.com/argoproj/argo-cd/releases/tag/v3.5.2)). For anything 3.5-specific, cite `/en/release-3.5/`.
> 2. **`Prune=true` is not a sync option and never was.** The only pruning constants are `Prune=false` and `Prune=confirm` ([gitops-engine types](https://github.com/argoproj/gitops-engine/blob/master/pkg/sync/common/types.go)). In Argo CD 3.5's in-tree engine fork the constant is split into `SyncOptionPrune = "Prune"` with legal values `"false"` and `"confirm"` — so a literal `Prune=true` parses, matches neither branch, and is a **silent no-op** ([release-3.5 types](https://github.com/argoproj/argo-cd/blob/release-3.5/gitops-engine/pkg/sync/common/types.go)). Pruning is `spec.syncPolicy.automated.prune: true`.
> 3. **Progressive Syncs and the Source Hydrator are Beta now, not alpha** — Progressive Syncs since v3.3.0, Source Hydrator since v3.5.0 ([Progressive Syncs](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Progressive-Syncs/), [Source Hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/)). Progressive Syncs is still off by default and behind an env var, so both halves of the answer changed.
> 4. **Flux is on v2.9.** Latest is **v2.9.4 (2026-08-07)**; v2.9.0 GA'd 2026-06-30 and v2.8.0 GA'd 2026-02-24 with Helm 4 ([flux2 releases](https://github.com/fluxcd/flux2/releases), [v2.8 announcement](https://fluxcd.io/blog/2026/02/flux-v2.8.0/)). GitHub's `/releases/latest` API returned a stale v2.8.8 during research — check the releases page, not the API.
> 5. **The OpenGitOps principles never say "Git."** Principle 3 is *"Software agents automatically pull the desired state declarations from **the source**"* ([PRINCIPLES.md v1.0.0](https://github.com/open-gitops/documents/blob/v1.0.0/PRINCIPLES.md)). An OCI registry satisfies them. This matters because OCI-artifact GitOps is not a deviation.
> 6. **The vendor that coined "GitOps" is gone.** Weaveworks shut down in February 2024; the CNCF GitOps Working Group merged into OpenGitOps in March 2024 and `cncf/tag-app-delivery` was archived 2025-09-09 ([WG README](https://github.com/cncf/tag-app-delivery/blob/main/gitops-wg/README.md)). Both surviving implementations are CNCF-graduated. GitOps is a spec now, not a product.

---

## The mental model

Hold six ideas.

**1. GitOps is a control loop, not a pipeline.** A pipeline is edge-triggered: something happens, steps run, the pipeline ends. A reconciler is level-triggered: it compares desired to actual, forever, and has no concept of "done." That single difference explains almost every surprising behavior — why your `kubectl edit` gets reverted, why a stuck resource retries forever instead of failing the build, why there is no natural place to hang "run this migration, then wait, then do the next thing."

**2. Git is the state store, not the mechanism.** The four OpenGitOps principles are declarative, versioned-and-immutable, pulled-automatically, continuously-reconciled. Git is the dominant implementation of principle 2, not the principle. Say this precisely and you will never be confused by OCI-based GitOps, or by someone claiming a CI job that runs `kubectl apply` is GitOps (it satisfies principle 1 and maybe 2, and neither 3 nor 4).

**3. Something must own the inventory.** In [09-helm.md](09-helm.md) you gave up Helm's release record. The inventory question — *what did I put here last time, so I know what to delete now* — does not disappear, it relocates. Argo CD answers it with a per-object tracking annotation. Flux answers it with a `.status.inventory` list on the Kustomization. These are genuinely different designs with different failure modes, and picking one is picking a garbage-collection strategy.

**4. Field ownership is the conflict model.** Both tools apply with Kubernetes server-side apply. That means the API server's `managedFields` — not the tool — is the arbiter of who owns what. Argo CD's manager is `argocd-controller`; Flux's are `kustomize-controller` and `helm-controller`. When your reconciler and an operator both write the same field, SSA is what surfaces it, and knowing that turns a mystery into a two-minute diagnosis.

**5. The unit of fan-out is the interesting design decision.** For one cluster, Argo CD and Flux are close to interchangeable. For three hundred cells, the question becomes: what generates the N×M objects, where does that generation run, and what is the blast radius of getting it wrong? Argo CD answers with `ApplicationSet` generators evaluated in a central hub. Flux answers with an agent per cluster and a thin per-cluster directory. Those are opposite answers, and they scale differently.

**6. GitOps is bad at ordered imperative steps, and pretending otherwise is the classic mistake.** "Create the database, wait for it, run the schema migration, then start the servers" is a workflow, not a desired state. Both tools bolt on approximations — Argo CD sync waves, Flux `dependsOn` — and both approximations are level-triggered retry loops wearing a sequencing costume. They work. They are also why a cell bootstrap that genuinely needs transactional ordering wants a durable workflow driving *around* the reconciler, not inside it.

```text
   git (or OCI): declared state ──► poll every N, or webhook
            │
            ▼
   ┌──────────────────────────────────────────────┐
   │  RECONCILER                                  │
   │   fetch source ──► render (helm/kustomize)   │
   │   diff desired vs live  ◄──── watch cluster  │
   │   apply (server-side) ──► field manager owns │
   │   prune what left the inventory              │
   │   assess health ──► status ──► metric        │
   └──────────────────────────────────────────────┘
            │                        ▲
            └── loop forever ────────┘

   What it does NOT do: ordered imperative steps, human approval gates,
   "build then deploy", anything that needs to run exactly once.
```

---

## Core concepts

### What GitOps buys, and what it does not

The four principles, verbatim from [OpenGitOps v1.0.0](https://github.com/open-gitops/documents/blob/v1.0.0/PRINCIPLES.md): desired state is **Declarative**; **Versioned and Immutable**; **Pulled Automatically** by software agents from the source; and **Continuously Reconciled** by agents that observe actual state and attempt to apply the desired state.

What you actually get:

| You get | Because |
|---|---|
| An audit trail that is the change mechanism, not a side effect | Every change is a commit with an author and a reviewer |
| Rollback that does not depend on the cluster | `git revert` needs no Secret in a possibly-broken cluster, unlike `helm rollback` |
| Drift detection for free | The diff runs on every reconcile whether you asked or not |
| Disaster recovery that is "point a new cluster at the repo" | The repo is the full description of the cell |
| A review surface for fleet-wide change | The PR diff is the change plan, if you render (see the rendered-manifest section) |

What you do not get, and should say out loud in a design review:

- **It is not a deploy pipeline.** There is no build, no artifact promotion, no test gate, no approval step. Those live in CI and in a promotion tool. The reconciler is the last mile only.
- **It is bad at ordered imperative steps.** Sync waves and `dependsOn` sequence *convergence*, not *operations*. A wave that never becomes healthy stalls the sync forever rather than failing cleanly ([sync waves](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-waves/)).
- **It cannot run something exactly once.** Everything is idempotent-or-broken. A migration Job re-applied on every sync is the single most common self-inflicted outage. And it does not solve secrets: every option in that section is a trade, none is free.
- **It moves the bottleneck rather than removing it.** At 300 cells the interesting failures are reconciler capacity, git provider rate limits, and repo-server CPU — not Kubernetes.

### Argo CD: the component split

Argo CD is a hub. One installation talks to many clusters ([architecture](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/architecture/), [components](https://argo-cd.readthedocs.io/en/release-3.5/developer-guide/architecture/components/)).

| Component | Job | Scales with |
|---|---|---|
| `argocd-server` (API server) | gRPC/REST for UI, CLI, CI; RBAC; SSO delegation; git webhook listener | User and UI traffic; stateless, run 3+ replicas and set `ARGOCD_API_SERVER_REPLICAS` |
| `argocd-repo-server` | Clones repos, runs `helm template` / `kustomize build`, returns manifests | Number of repos, manifest generation cost. The usual CPU/memory hotspot |
| `argocd-application-controller` | Watches clusters, computes diff, applies, prunes, assesses health, runs hooks | Number of **clusters** and objects. Sharded by cluster |
| `argocd-applicationset-controller` | Reconciles `ApplicationSet` into `Application` objects | Number of generated Applications |
| `argocd-redis` | Disposable cache in front of the Kube API and git | Object count. Explicitly rebuildable without service disruption |
| `argocd-dex-server` | OIDC federation (optional; in-memory DB, so multiple replicas can disagree) | — |
| `argocd-notifications-controller` | Triggers and templates for alerting ([notifications](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/notifications/)) | — |

The HA manifests (`manifests/ha/install.yaml`) run Redis in HA mode and **require at least three distinct nodes** because of pod anti-affinity; IPv6-only clusters are unsupported ([high availability](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/)).

### Argo CD: Application and AppProject

The `Application` is the unit of sync. The fields that matter in a cell fleet:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: cell-042-temporal
  namespace: argocd
  finalizers:
    # Without this, deleting the Application orphans every object it created.
    - resources-finalizer.argocd.argoproj.io
spec:
  project: cells                       # AppProject = the guardrail
  source:
    repoURL: https://github.com/example/cells-gitops.git
    targetRevision: refs/heads/main    # fully-qualified: short refs are CPU-expensive to resolve
    path: rendered/cell-042/temporal
  destination:
    server: https://cell-042.eks.internal
    namespace: temporal
  syncPolicy:
    automated:
      enabled: true                    # newer, cleaner than presence/absence of `automated`
      prune: true                      # default false; the safety mechanism
      selfHeal: true                   # default false; reverts live edits
      allowEmpty: false                # default; refuses a sync that would empty the app
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
      - PrunePropagationPolicy=foreground
      - PruneLast=true
    retry:
      limit: 5
      backoff: { duration: 5s, factor: 2, maxDuration: 3m }
  ignoreDifferences:
    - group: apps
      kind: Deployment
      managedFieldsManagers: ["kube-controller-manager"]
  revisionHistoryLimit: 10
```

`AppProject` is the tenancy boundary and the thing that stops an ApplicationSet bug from deploying to the wrong cell. It restricts `sourceRepos`, `destinations` (server + namespace, wildcards allowed), `clusterResourceWhitelist`/`Blacklist`, `namespaceResourceWhitelist`/`Blacklist`, carries `roles` with scoped JWT tokens, and holds `syncWindows` ([declarative setup](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/declarative-setup/)). On a fleet, put every cell Application in a project whose `destinations` are exactly the cells that project may touch. It is the cheapest blast-radius control you will ever add.

### Argo CD: sync waves, hooks, and phases

Ordering within one Application's sync is `argocd.argoproj.io/sync-wave` — an integer string, default **0**, negatives allowed ([sync waves](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-waves/)).

The documented precedence is: **phase, then wave (lower first), then Kubernetes kind, then name.** Phases are `PreSync`, `Sync`, `PostSync`, plus `SyncFail` on failure and `Skip` to suppress a manifest entirely; `PreDelete` and `PostDelete` (the latter since v2.10) run on Application deletion, handled as deletion finalizers rather than by the sync phase machinery.

Two behaviors people get wrong:

- **Argo CD waits for health between waves.** It applies the lowest wave containing anything out-of-sync or unhealthy, waits, and repeats. A wave that never becomes healthy stalls the sync indefinitely — the docs say so plainly: *"it may be that the app can never get to healthy."* Objects with no health assessment (a `Secret`) auto-succeed.
- **There is a fixed inter-wave delay of 2 seconds**, so other controllers can react. It is `ARGOCD_SYNC_WAVE_DELAY`, parsed with `strconv.Atoi` as an integer number of seconds — a Go duration string like `"5s"` is silently ignored and you get the default — and it is skipped after the final wave ([controller/sync.go](https://github.com/argoproj/argo-cd/blob/release-3.5/controller/sync.go)). At 12 waves that is 22 seconds of pure sleep per sync, per Application. Multiply by 300 cells.

Pruning reverses wave order: higher waves are pruned first, and a prune failure in one wave stops lower waves from being processed.

Hook cleanup is `argocd.argoproj.io/hook-delete-policy` with exactly three values — `HookSucceeded`, `HookFailed`, `BeforeHookCreation` — and **`BeforeHookCreation` is the default** when unspecified.

### Argo CD: sync options, in full

Set them app-wide in `spec.syncPolicy.syncOptions`, or per-resource with the `argocd.argoproj.io/sync-options` annotation (comma-separated, whitespace trimmed). Not every option works in both places ([sync options](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-options/)).

| Option | Default | Where | Note |
|---|---|---|---|
| `Validate=false` | validation on | both | Required when supplying a partial manifest with SSA |
| `CreateNamespace=true` | off | app only | Prerequisite for `managedNamespaceMetadata` |
| `PrunePropagationPolicy=` | `foreground` | app only | `background`, `foreground`, `orphan` |
| `PruneLast=true` | off | both | Prunes as an implicit final wave, after everything is healthy |
| `ApplyOutOfSyncOnly=true` | off | app only | Hooks still run, unlike selective sync |
| `ServerSideApply=true` | off (client-side) | both | `ServerSideApply=false` opts one resource out |
| `RespectIgnoreDifferences=true` | off | app only | Makes `ignoreDifferences` apply at sync, not just diff. No effect on first create |
| `Replace=true` | off | both | `kubectl replace`/`create`. Destructive. Takes precedence over SSA |
| `SkipDryRunOnMissingResource=true` | off | both | For CRs whose CRD is created in the same sync |
| `Delete=false` / `Delete=confirm` | delete on cascade | both | `confirm` needs the `argocd.argoproj.io/deletion-approved` annotation |
| `Prune=false` / `Prune=confirm` | — | both | Resource-level **always overrides** the app-level policy |
| `FailOnSharedResource=true` | off | app only | Fail if another Application already owns the object |
| `ClientSideApplyMigration=false` | migration **enabled** | app only | Migrates ownership away from `kubectl-client-side-apply` |
| `Force=true` | off | resource only | Delete-and-recreate. Pair with `Replace=true` |

Note that `ServerSideDiff` is **not** in this list — it is a *compare* option, set via `argocd.argoproj.io/compare-options`, and it has been Stable since v3.1.0 ([diff strategies](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/diff-strategies/)). It runs an SSA dry-run per resource and compares the API server's predicted result, which means admission webhooks and defaulted fields participate in the diff — a genuinely large quality-of-life improvement over the legacy three-way diff.

### Argo CD: health assessment and custom checks

Argo CD ships health assessment for the common kinds and rolls per-resource health up to the Application. Everything about wave ordering depends on it. For CRDs it does not know, you write Lua in `argocd-cm` under `resource.customizations.health.<group>_<kind>` ([health](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/health/)):

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-cm
  namespace: argocd
data:
  # Group and kind joined by an underscore. Note: wildcards do NOT work in this
  # key form -- ConfigMap keys cannot contain '*'. Use the flat
  # `resource.customizations` key if you need `*.example.io/*`.
  resource.customizations.health.example.com_TemporalCluster: |
    hs = { status = "Progressing", message = "waiting for status" }
    if obj.status == nil then return hs end
    -- Guard on observedGeneration or health flaps while the controller catches up.
    if obj.status.observedGeneration ~= nil and
       obj.status.observedGeneration ~= obj.metadata.generation then
      hs.message = "spec changed, controller has not observed it yet"
      return hs
    end
    for i, c in ipairs(obj.status.conditions or {}) do
      if c.type == "Ready" and c.status == "False" then
        hs.status = "Degraded"; hs.message = c.message; return hs
      elseif c.type == "Ready" and c.status == "True" then
        hs.status = "Healthy"; hs.message = "cluster ready"; return hs
      end
    end
    return hs

  # Lua standard libraries are disabled by default as a security measure.
  resource.customizations.useOpenLibs.example.com_TemporalCluster: "true"
```

Write these for every CRD in the cell that a wave depends on. A CRD with no health check reports `Healthy` the moment it is applied, which means your wave ordering silently does nothing.

### Argo CD: resource tracking, prune, and deletion

Since **3.0 the default tracking method is annotation-based**, not label-based ([2.14→3.0 upgrade](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/upgrading/2.14-3.0/)). The annotation is:

```text
argocd.argoproj.io/tracking-id: my-app:apps/Deployment:default/my-deployment
                                <app>:<group>/<kind>:<namespace>/<name>
```

The reason for the change is the reason you care: labels are **truncated at 63 characters**, so two Applications sharing a 63-character prefix become indistinguishable to the pruner ([resource tracking](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/resource_tracking/)). In a fleet where names are `cell-<region>-<cloud>-<nnn>-<component>`, that is not hypothetical. Annotation tracking also encodes the group/kind/namespace/name, so a resource copied elsewhere (by HNC, say) is correctly *not* claimed. If several Argo CD instances manage one cluster, set `installationID` in `argocd-cm` and each object additionally carries `argocd.argoproj.io/installation-id`.

Pruning only considers objects Argo CD tracks; untracked objects are "orphaned" and never pruned. Cascading delete of an Application depends on the `resources-finalizer.argocd.argoproj.io` finalizer being present — omit it and deleting the Application leaves everything running.

### App of apps, and why ApplicationSet replaced it

The app-of-apps pattern is one Application whose source contains only other `Application` manifests. It still works, and Argo CD's own bootstrapping doc now presents it as the *alternative* — the recommendation is ApplicationSet, specifically the cluster generator ([cluster bootstrapping](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/cluster-bootstrapping/)). The docs also carry an explicit warning that it is an admin-level capability: whoever can push to the parent repo can create an Application in any project, and a project with access to the Argo CD namespace is effectively admin.

For a cell fleet, app-of-apps is the wrong shape anyway: it makes you write N×M YAML files. ApplicationSet makes you write one.

### ApplicationSet: the controller

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: cell-components
  namespace: argocd
spec:
  goTemplate: true
  goTemplateOptions: ["missingkey=error"]   # NOT the default; turn it on
  syncPolicy:
    applicationsSync: create-update          # needs --enable-policy-override on the controller
    preserveResourcesOnDeletion: false
  ignoreApplicationDifferences:
    - jsonPointers:
        - /spec/syncPolicy                   # lets you hand-disable autosync on one cell
  generators: [...]
  template: {...}
```

Facts worth memorizing ([controlling resource modification](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Controlling-Resource-Modification/), [GoTemplate](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/GoTemplate/)):

- The controller flag `--policy` takes `sync` (**default**), `create-only`, `create-update`, `create-delete`. The per-ApplicationSet `spec.syncPolicy.applicationsSync` is **ignored unless** `--enable-policy-override` is set (default `false`), and the flag wins when both are set.
- `preserveResourcesOnDeletion: true` works by *not adding* the `resources-finalizer.argocd.argoproj.io` finalizer to generated Applications. `kubectl delete applicationset --cascade=orphan` does not help: the orphans still carry the finalizer.
- `goTemplate: true` switches from fasttemplate to Go `text/template` plus Sprig, adds `normalize`, `slugify`, `toYaml`/`fromYaml`, and changes every variable reference (`{{path}}` becomes `{{.path.path}}`, `{{metadata.labels.x}}` becomes `{{index .metadata.labels "x"}}`). **Set `goTemplateOptions: ["missingkey=error"]`** — without it a typo'd variable renders as empty and you get 300 Applications pointing at the wrong path. Guard genuinely-optional params with Sprig `dig`.
- Templates apply to **string fields only**. You cannot template a boolean or an object. For those, use `spec.templatePatch` (which requires `goTemplate: true`).
- `ignoreApplicationDifferences` is applied via a **MergePatch**, and "existing lists will be completely replaced by new lists" — so ignoring one entry inside `spec.sources` holds only until anything else in that list changes.
- The refresh annotation is `argocd.argoproj.io/application-set-refresh` (hyphenated). The single-word spelling does not exist and is a no-op.

### ApplicationSet generators — the heart of a many-cells fleet

A generator produces **parameter sets**; each set renders `spec.template` once, producing one Application. Argo CD 3.5 ships nine: List, Cluster, Git, Matrix, Merge, Cluster Decision Resource, SCM Provider, Pull Request, Plugin ([Generators](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators/)). Every one of them accepts a sibling `selector:` field that post-filters the generated parameter sets with a standard Kubernetes label selector.

**List** — a literal list. Any key/value pairs since v0.2.0, not just `cluster`/`url`. `elementsYaml` lets a JSON/YAML string (typically from a Git file generator inside a matrix) expand into elements.

**Cluster** — one parameter set per cluster registered with Argo CD, read from the cluster Secrets labelled `argocd.argoproj.io/secret-type: cluster` ([Generators-Cluster](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Cluster/)). This is the one that matters for cells. Exposed fields:

| Parameter | Meaning |
|---|---|
| `name` | The Argo CD cluster name |
| `nameNormalized` | Lowercased to alphanumerics, `-`, `.` — use this in resource names |
| `server` | Cluster API URL |
| `project` | The Secret's `project` field, or `''` |
| `metadata.labels.<key>` | One per label on the cluster Secret |
| `metadata.annotations.<key>` | One per annotation on the cluster Secret |

Two behaviors to internalize. First, **the default local cluster has no Secret**, so it carries no `argocd.argoproj.io/secret-type` label, so *any selector on that label automatically excludes the hub itself* — which is the idiomatic "remote clusters only" filter. Second, the label selector supports `matchExpressions`, so the cluster Secret's labels become your fleet query language. This is where your cell taxonomy lives:

```yaml
metadata:
  name: cell-042
  labels:
    argocd.argoproj.io/secret-type: cluster
    cloud: aws
    region: us-west-2
    tier: production
    ring: "1"                 # rollout wave
    temporal-version: "1.27"
```

**Git** — two subtypes. `directories` gives one parameter set per matching directory, exposing `.path.path`, `.path.basename`, `.path.basenameNormalized`, `index .path.segments n`. **Exclude rules always beat include rules regardless of order**, and directories starting with `.` are auto-excluded. `files` gives one parameter set per matching JSON/YAML file, with the *file contents* as parameters — a nested object under `goTemplate: true`, a flat dotted map without it — plus `.path.filename` and `.path.filenameNormalized`. Note the default globbing is documented as "very greedy": `cluster-charts/*/*/values.yaml` behaves like `cluster-charts/**/values.yaml`. Stricter doublestar globbing is opt-in via `--enable-new-git-file-globbing` ([file globbing](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Git-File-Globbing/)). Both subtypes poll every **3 minutes** by default (`requeueAfterSeconds`, or globally `ARGOCD_APPLICATIONSET_CONTROLLER_REQUEUE_AFTER`), and the ApplicationSet controller runs its **own** webhook server at `/api/webhook`, separate from the API server's.

**Matrix** — the cartesian product of exactly **two** child generators, merging each pair of parameter sets ([Generators-Matrix](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Matrix/)). The restrictions are all load-bearing: only two children; one generator per array entry; child-level `template` overrides are not processed; matrix/merge may be nested only one level deep; a consumer child must be listed after its producer; circular consumption is invalid. Duplicate keys are allowed and treated as overrides — **except** that matrix fails outright when children produce identical keys with differing values, which is exactly what happens when both children are Git generators, since both auto-populate `path*`. Set `pathParamPrefix` on at least one.

**Merge** — an override join, not a product. Parameter sets from the base (first) generator are matched against later generators on `mergeKeys`; **non-matching sets from later generators are discarded**, and precedence runs bottom-to-top ([Generators-Merge](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Merge/)). The trap: *"merging on nested values while using `goTemplate: true` is currently not supported"*, so a `mergeKeys: [values.selector]` silently fails to match under goTemplate.

**Cluster Decision Resource** duck-types an arbitrary CR's status list (Open Cluster Management `PlacementRule` is the canonical case) via a ConfigMap declaring `apiVersion`/`kind`/`statusListKey`/`matchKey`. **SCM Provider** discovers repos in an org. **Pull Request** creates one Application per open PR (`requeueAfterSeconds` default **1800**), which is how you get per-PR preview environments. **Plugin** POSTs to your own HTTP service at `POST /api/v1/getparams.execute` with a bearer token and gets back a list of parameter objects — the escape hatch when your cell inventory lives in a database rather than in git.

**The one that matters: one Application per cell per component.**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: cell-components
  namespace: argocd
spec:
  goTemplate: true
  goTemplateOptions: ["missingkey=error"]
  generators:
    - matrix:
        generators:
          # Child 1: one parameter set per registered cell.
          # Selecting on secret-type excludes the hub cluster itself.
          - clusters:
              selector:
                matchLabels:
                  argocd.argoproj.io/secret-type: cluster
                matchExpressions:
                  - key: tier
                    operator: In
                    values: ["production"]
          # Child 2: one parameter set per platform component.
          - list:
              elements:
                - component: cert-manager
                  wave: "10"
                  namespace: cert-manager
                - component: vault-agent
                  wave: "20"
                  namespace: vault
                - component: kyverno-policies
                  wave: "30"
                  namespace: kyverno
                - component: temporal
                  wave: "40"
                  namespace: temporal
  template:
    metadata:
      name: '{{.nameNormalized}}-{{.component}}'
      labels:
        cell: '{{.nameNormalized}}'
        component: '{{.component}}'
        wave: '{{.wave}}'                              # selected by RollingSync, below
        ring: '{{index .metadata.labels "ring"}}'      # drives progressive rollout
    spec:
      project: cells
      source:
        repoURL: https://github.com/example/cells-gitops.git
        targetRevision: refs/heads/main
        # The rendered-manifest layout: plain YAML, per cell, per component.
        path: 'rendered/{{.nameNormalized}}/{{.component}}'
      destination:
        server: '{{.server}}'
        namespace: '{{.namespace}}'
      syncPolicy:
        automated: { enabled: true, prune: true, selfHeal: true }
        syncOptions:
          - CreateNamespace=true
          - ServerSideApply=true
          - PruneLast=true
```

Note what the `wave` label is **not**: it is not `argocd.argoproj.io/sync-wave`. Sync waves order resources *inside a single Application's sync*, and these Applications are created directly by the applicationset-controller, not as children of a parent Application's sync — so a sync-wave annotation stamped on them is inert. Cross-Application ordering here comes from a progressive-sync strategy that selects on that label:

```yaml
  strategy:
    type: RollingSync
    rollingSync:
      steps:
        - matchExpressions: [{ key: wave, operator: In, values: ["10"] }]
        - matchExpressions: [{ key: wave, operator: In, values: ["20"] }]
        - matchExpressions: [{ key: wave, operator: In, values: ["30"] }]
        - matchExpressions: [{ key: wave, operator: In, values: ["40"] }]
```

Progressive syncs have been Beta since Argo CD v3.3.0 and must be enabled on the applicationset-controller (`--enable-progressive-syncs`, or `ARGOCD_APPLICATIONSET_CONTROLLER_ENABLE_PROGRESSIVE_SYNCS=true`). The price is real: `RollingSync` forces automated sync off on the generated Applications, because the controller has to drive sync order itself. If you are not willing to pay that, the honest statement is that this ApplicationSet provides *no* cross-component ordering and you are relying on each component's own retry loop to converge.

With 300 production cells and 4 components that is 1,200 Applications from one file. The two children share no parameter names, so no `pathParamPrefix` is needed. Notice that the *cell taxonomy lives in cluster Secret labels* and the *component taxonomy lives in the list generator* — which means adding a cell is registering a cluster, and adding a component is a four-line diff.

If your cell inventory is richer than labels can express (per-cell Temporal version, per-cell shard count, per-cell storage class), replace the cluster child with a Git **files** generator over `cells/*/config.yaml` and use the file contents as parameters. That is the pattern that scales, because a cell's identity becomes a reviewable file rather than a `kubectl label` someone ran once.

### Sharding the application controller

This is the fleet-scale question, and the answer surprises people: **the shard key is the cluster, not the Application** ([high availability](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/)).

Set `replicas` on the `argocd-application-controller` StatefulSet **and repeat the same number** in `ARGOCD_CONTROLLER_REPLICAS` — the env var is how each pod learns the denominator:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: argocd-application-controller
spec:
  replicas: 6
  template:
    spec:
      containers:
        - name: argocd-application-controller
          env:
            - name: ARGOCD_CONTROLLER_REPLICAS
              value: "6"
```

Algorithms are `legacy` (**default**, UID-based, non-uniform), `round-robin`, and `consistent-hashing`, set via `controller.sharding.algorithm` in `argocd-cmd-params-cm`, the `--sharding-method` flag, or `ARGOCD_CONTROLLER_SHARDING_ALGORITHM`. **Both non-legacy algorithms are still labelled experimental**, and round-robin reshuffles every cluster if the rank-0 cluster is removed. You can pin a cluster to a shard by setting `shard` in its cluster Secret.

Dynamic cluster distribution (alpha since v2.9) avoids the restart-on-rescale problem: enable `ARGOCD_ENABLE_DYNAMIC_CLUSTER_DISTRIBUTION=true`, run the controller as a Deployment, and shards coordinate through a ConfigMap named `argocd-app-controller-shard-cm` with a heartbeat (default **10 seconds**, `ARGOCD_CONTROLLER_HEARTBEAT_TIME`); a pod whose entry is stale for more than 3× that is marked unready ([dynamic cluster distribution](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/dynamic-cluster-distribution/)). In this mode the shard count comes from the Deployment's `replicas`, not the env var.

The practical consequence for a cell fleet is good news: **cluster count is nearly free, Application count is not.** The CNOE/AWS/Intuit/Red Hat benchmark measured, at 10 shards on one m5.2xlarge with 10k apps, 100 clusters in 9 minutes, 250 in 9 minutes, 500 in 11 minutes — while app scaling went 20k in 12 min, 30k in 19 min, 50k in 22 min ([CNOE, secondary](https://cnoe.io/blog/argo-cd-application-scalability)). The same benchmark found shard count gains flatten at 9 (3 shards 75 min, 6 shards 37 min, 9 shards 21 min, no gain beyond), and that raising kube client QPS/burst from 50/100 to 100/200 cut full sync time ~52%. It also found that status and operation processors had **no effect at all** until QPS/burst was raised — the classic "you tuned the wrong knob" trap. For a single oversized cell, sharding cannot help you at all; use `--status-processors` (default **20**) and `--operation-processors` (default **10**), which the docs suggest raising to 50/25 for 1000 applications.

### Flux: the controller set

Flux is an agent. It runs *in* the cluster it manages ([components](https://fluxcd.io/flux/components/)).

| Controller | CRDs | Default? |
|---|---|---|
| source-controller | GitRepository, OCIRepository, HelmRepository, HelmChart, Bucket, ExternalArtifact | yes, required |
| kustomize-controller | Kustomization | yes, required |
| helm-controller | HelmRelease | yes |
| notification-controller | Provider, Alert, Receiver | yes |
| image-reflector-controller | ImageRepository, ImagePolicy | `--components-extra` |
| image-automation-controller | ImageUpdateAutomation | `--components-extra` |
| source-watcher | ArtifactGenerator | `--components-extra` |

The bootstrap minimum is source-controller plus kustomize-controller ([optional components](https://fluxcd.io/flux/installation/configuration/optional-components/)).

### Flux: sources, Kustomization, and the inventory

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: cell-config
  namespace: flux-system
spec:
  interval: 5m              # REQUIRED; there is no default
  timeout: 60s              # default
  url: https://github.com/example/cells-gitops.git
  ref:
    name: refs/heads/main   # takes precedence over branch/tag/semver.
                            # If you omit `ref` entirely you get branch `master`.
  secretRef: { name: cell-config-auth }
  # provider: generic | aws | azure | github   -- note: NO gcp value on GitRepository
  sparseCheckout: ["rendered/cell-042"]        # do not ship 300 cells to every cell
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: temporal
  namespace: flux-system
spec:
  interval: 10m
  # timeout defaults to interval-30s (floored at 30s), NOT to interval as the prose says
  retryInterval: 1m         # defaults to .spec.interval
  path: ./rendered/cell-042/temporal
  prune: true               # REQUIRED field
  sourceRef: { kind: GitRepository, name: cell-config }
  dependsOn:
    - name: infra-pki
    - name: infra-secrets
  wait: true                # health-check ALL resources; this IGNORES .spec.healthChecks
  deletionPolicy: MirrorPrune   # MirrorPrune (default) | Delete | WaitForTermination | Orphan
  postBuild:
    substitute: { CELL_ID: "cell-042", REGION: "us-west-2" }
    substituteFrom:
      - kind: ConfigMap
        name: cell-vars
```

Flux's garbage collection is **inventory-based**: `.status.inventory.entries[]` holds `{id, v}` where `id` is `<namespace>_<name>_<group>_<kind>`, and pruning diffs the current build output against the recorded inventory ([prune](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune)). Correcting a common claim: Flux is not entirely label-free on managed objects — it stamps `kustomize.toolkit.fluxcd.io/name` and `/namespace` for attribution — but the inventory, not those labels, is what drives deletion.

The per-resource controls are annotations:

| Annotation | Values | Effect |
|---|---|---|
| `kustomize.toolkit.fluxcd.io/prune` | `disabled` | Never GC this object (Namespaces, PVCs) |
| `kustomize.toolkit.fluxcd.io/ssa` | `Override` (default), `Merge`, `IfNotPresent`, `Ignore` | How SSA treats other writers |
| `kustomize.toolkit.fluxcd.io/force` | `enabled` | Delete-and-recreate on immutable-field change. Docs warn of StatefulSet data loss |
| `kustomize.toolkit.fluxcd.io/reconcile` | `disabled` | Stop applying **and** stop pruning this one object |

Note the fourth `ssa` value is **`IfNotPresent`**, not `IgnoreDifferences` — a widely repeated error. `Merge` preserves non-overlapping fields other tools added; atomic list fields are still reverted, per SSA semantics.

### Flux: HelmRelease, and the crucial difference from Argo CD

`HelmRelease` (`helm.toolkit.fluxcd.io/v2`) uses the Helm SDK for real. It creates real Helm releases with real release state in Secrets, readable by the stock `helm get -n <storageNamespace>` client ([HelmRelease](https://fluxcd.io/flux/components/helm/helmreleases/)). That means hooks run, rollback works, `helm history` works — all the things [09-helm.md](09-helm.md) says you gave up.

Defaults worth knowing: `.spec.timeout` **5m0s**; `.spec.maxHistory` **5**; `.spec.install.crds` defaults **`Create`** while `.spec.upgrade.crds` defaults **`Skip`** (deliberately asymmetric); `.spec.upgrade.remediation.strategy` **`rollback`**; `.spec.driftDetection.mode` **`disabled`** (options `warn`, `enabled`); `.spec.persistentClient` **`true`**. Since Flux **v2.8.0** helm-controller ships **Helm v4**, making server-side apply and kstatus health checks the defaults for *new* releases; existing releases keep client-side apply because Helm persists the apply method in release storage, and the `UseHelm3Defaults` feature gate (default `false`) restores the old behavior.

**This is the fork in the road.** The constraint this library assumes is "Helm for templating only, no release state." Argo CD enforces that constraint structurally — it only ever runs `helm template` ([Argo CD Helm](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/)). Flux `HelmRelease` does the opposite. Flux `Kustomization` over pre-rendered YAML matches your constraint; Flux `HelmRelease` violates it. A hybrid is legitimate — rendered manifests for your charts, `HelmRelease` for four vendor charts that genuinely need hooks — as long as the boundary is written down.

### Flux: dependsOn, health, and multi-tenancy

`dependsOn` gates a Kustomization or HelmRelease on its dependencies reaching `Ready == True`. The subtlety that bites everyone: **a bare `Ready=True` means "applied successfully," not "the workloads are healthy."** For `dependsOn` to gate on real readiness, the *dependency itself* must set `wait: true` or `healthChecks` so that its own `Ready` is kstatus-gated ([dependencies](https://fluxcd.io/flux/components/kustomize/kustomizations/#dependencies)). And `wait: true` **ignores** `healthChecks` — they are mutually exclusive, not additive. Circular dependencies are fatal: the objects simply never reconcile.

For CRDs kstatus does not understand, `healthCheckExprs` takes CEL — `current` (required), `inProgress`, `failed`, evaluated in the order inProgress → failed → current. Added for Kustomization in Flux 2.5, extended to HelmRelease in 2.8.0, and since 2.9.0 `kind` may be omitted to apply one expression to a whole API group ([CEL cheatsheet](https://fluxcd.io/flux/cheatsheets/cel-healthchecks/)).

Flux's multi-tenancy model is Kubernetes RBAC plus impersonation: controllers impersonate `.spec.serviceAccountName`, so a tenant's own RBAC bounds what Flux will do for them. The lockdown flags are `--no-cross-namespace-refs=true` (all five reconcilers), `--no-remote-bases=true` (kustomize-controller only), and `--default-service-account=default` ([multi-tenancy](https://fluxcd.io/flux/installation/configuration/multitenancy/)). This is genuinely stronger than Argo CD's AppProject model, because it is enforced by the API server rather than by the GitOps tool.

### Argo CD vs Flux — the honest comparison

| Dimension | Argo CD 3.5 | Flux 2.9 |
|---|---|---|
| Topology | Hub-and-spoke: one control plane, N clusters via kubeconfig | Agent per cluster; no hub |
| UI | First-class, and a real operational asset | None in core; UIs are vendor add-ons (Flux Operator, Weave GitOps successors) |
| RBAC | Its own RBAC model + SSO, layered over `AppProject` | Native Kubernetes RBAC via ServiceAccount impersonation |
| Multi-tenancy | AppProject guardrails, enforced by Argo CD | API-server-enforced impersonation. Structurally stronger |
| Fleet scale | Documented sharding, published benchmarks, known hub bottleneck | Scales by construction; no hub to size. Sharding exists for tens of thousands of objects *per cluster* |
| Helm | `helm template` only, always. No release state, ever | Real Helm releases with real state (`HelmRelease`), or plain YAML (`Kustomization`) |
| Inventory | Per-object `tracking-id` annotation | `.status.inventory` on the Kustomization |
| Drift semantics | `selfHeal` reverts within ~5s of a live change; diff is whole-object | SSA dry-run diff against fields Flux owns; other managers' fields are not drift |
| Push vs pull | Pull from git, **push** to remote clusters' API servers | Pure pull; the cell needs no inbound path from a hub |
| Fan-out | ApplicationSet generators — declarative, centralized, expressive | One thin `clusters/<name>/` directory per cluster, bootstrapped per cell |
| Progressive rollout | ApplicationSet Progressive Syncs (Beta, flag-gated) | `dependsOn` chains, or an external orchestrator |
| Bootstrap | Terraform installs it; app-of-apps for self-management | `flux bootstrap` self-manages by construction |
| Operational burden | More components to run and size; you own the hub's availability | Distributed: 300 cells means 300 small installs to keep current |

**Recommendation framing rather than a verdict**, because the honest answer is that it depends on one question: *does a hub outage mean a fleet outage you can tolerate?*

- **Choose Argo CD** when a human-operated fleet view matters, when you want fan-out expressed as data in one place, when your organization already federates identity into one console, and when you can accept that the hub is a tier-0 service you now operate. For a team whose job is to provision, upgrade, and tear down cells, the ApplicationSet cluster generator is close to a purpose-built tool, and the UI is worth more during an incident than engineers admit.
- **Choose Flux** when cell isolation is the dominant value — no shared control plane, no inbound path from a hub into a customer-adjacent cell, no single component whose failure freezes 300 cells. Choose it when you want real Helm release state for vendor charts. Choose it when your regulatory story benefits from "each cell reconciles itself from its own repo path with its own credentials."
- **Both is a legitimate answer** and more common than blog posts suggest: Flux inside each cell for the cell's own bootstrap layer (L4–L8 of the [DAG](12-cell-lifecycle-synthesis.md)), Argo CD as the fleet view and the driver of application-layer rollouts. The cost is two mental models and two sets of gotchas, which is real.

The comparison people get wrong is scale. Argo CD has published fleet numbers *because it has a bottleneck worth measuring*; Flux has almost none because there is nothing central to size. Absence of Flux benchmarks is not evidence of Flux limits.

### How each handles Helm, and the rendered-manifest pattern

Argo CD is unambiguous: it only inflates charts with `helm template`, which is why `helm ls` shows nothing for an Argo-managed release ([FAQ](https://argo-cd.readthedocs.io/en/stable/faq/)). Helm hooks are mapped onto Argo hooks, and — a trap worth repeating from [09-helm.md](09-helm.md) — defining *any* Argo CD hook in a chart causes *all* Helm hooks in it to be ignored.

Flux gives you the choice: `HelmRelease` for real release state, or render in CI and let a `Kustomization` apply plain YAML.

**The rendered-manifest pattern** is the third option, and it is the one that fits that constraint best. The framing sentence is Akuity's: *"Git contains the inputs to the desired state, not the desired state itself"* ([The Rendered Manifests Pattern](https://akuity.io/blog/the-rendered-manifests-pattern)). The mechanics:

1. `main` holds the dry source — charts, values, overlays. Nobody hand-edits anything downstream.
2. CI runs `helm template` / `kustomize build` per cell per component on every push to `main`.
3. Output is committed to an environment branch (`environments/cell-042`) or a path (`rendered/cell-042/`).
4. The Argo CD Application or Flux Kustomization points at the rendered location with **no** Helm or Kustomize config at all.
5. The PR diff is the literal set of Kubernetes objects that will exist. `git revert` is a real rollback.

Argo CD has productized this as the **Source Hydrator**, Beta since v3.5.0 ([source hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/), design in [manifest-hydrator.md](https://github.com/argoproj/argo-cd/blob/master/docs/proposals/manifest-hydrator.md)). Enable `hydrator.enabled: "true"` in `argocd-cmd-params-cm`, run the commit server, and give it a push credential labelled `argocd.argoproj.io/secret-type: repository-write`:

```yaml
spec:
  sourceHydrator:                    # mutually exclusive with `source` and `sources`
    drySource:
      repoURL: https://github.com/example/cells-gitops.git
      path: charts/temporal-cell
      targetRevision: HEAD
    syncSource:
      targetBranch: environments/cell-042
      path: temporal                 # required; MUST NOT be the repo root
    hydrateTo:
      targetBranch: environments/cell-042-next   # push-to-stage; a PR promotes it
```

Two constraints that will decide whether you can use it. First, **hydration must be deterministic** — no unpinned chart dependencies, no unpinned Kustomize remote bases, no `randAlphaNum`, no `lookup`; the hydrator deliberately does not set `ARGOCD_APP_NAME`, `KUBE_VERSION`, or `KUBE_API_VERSIONS`. Second, and this is the hard one, the docs state it plainly: *"Do not use the source hydrator with any tool that injects secrets into your manifests as part of the hydration process (for example, Helm with SOPS or the Argo CD Vault Plugin). These secrets would be committed to git."* **The rendered-manifest pattern structurally forces you off SOPS and onto a runtime secrets operator.** That is not a footnote; it is a design constraint on your whole secrets story.

### Server-side apply, field managers, and pruning

Both tools apply with SSA, so the API server's `managedFields` is the ownership record ([Kubernetes SSA](https://kubernetes.io/docs/reference/using-api/server-side-apply/), GA since 1.22).

| | Argo CD | Flux |
|---|---|---|
| Field manager | `argocd-controller` (constant `ArgoCDSSAManager`; **not** flag-configurable in 3.5) | `kustomize-controller`, `helm-controller` |
| Enabling SSA | Opt-in: `ServerSideApply=true` sync option | Always on |
| Conflicts | `--server-side --force-conflicts` — Argo CD takes ownership | `ssa: Override` (default) takes ownership; `Merge` preserves non-overlapping fields |
| Ignoring another writer | `ignoreDifferences[].managedFieldsManagers` | `spec.ignore` with Strip/Adopt strategies (new in 2.9) |
| Escape hatch for humans | `argocd.argoproj.io/compare-options: IgnoreExtraneous` | Edits made under field manager `flux-client-side-apply` are preserved |
| Inventory | `argocd.argoproj.io/tracking-id` annotation on each object | `.status.inventory` on the Kustomization |
| Migrating from client-side apply | `ClientSideApplyMigration` (enabled by default), source manager `kubectl-client-side-apply` | — |

Flux 2.9's `spec.ignore` is worth understanding because it is more nuanced than Argo's equivalent ([ignore rules](https://fluxcd.io/flux/components/kustomize/kustomizations/#ignore-rules)). It resolves two ways: **Strip**, when another Apply-type manager owns the field, removes it from Flux's apply payload and relinquishes ownership; **Adopt**, when Flux is the sole Apply-type manager, copies the live value into the payload so `kubectl patch` edits survive while Flux keeps ownership. The warning in the docs matters — *omitting the `target` selector makes the rule match every object the Kustomization manages*.

**Pruning and the resources that refuse to die.** Deleting the manifest is only the start. Argo CD's `PrunePropagationPolicy` defaults to `foreground`, meaning dependents are deleted first and the owner lingers with a `deletionTimestamp` until they are gone. `PruneLast=true` defers all pruning to an implicit final wave. Flux's `deletionPolicy` gives you `MirrorPrune` (default — honor `prune`), `Delete`, `WaitForTermination` (bounded by `.spec.timeout`), and `Orphan`.

Then there are finalizers. The failure you will actually hit is a **namespace stuck in `Terminating`**, and the cause is almost never the namespace. `kubectl describe ns <name>` and read the Conditions — `NamespaceDeletionDiscoveryFailure`, `NamespaceContentRemaining`, `NamespaceFinalizersRemaining` name the failing API group or the exact stuck finalizer with instance counts. The critical mechanism: the namespace controller must enumerate *every registered API resource* to know what to delete, so **one unavailable aggregated APIService breaks discovery cluster-wide and no namespace anywhere can finish terminating** ([namespace deletion controller](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/namespace/deletion/namespaced_resources_deleter.go), [GKE troubleshooting](https://cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)). Fix the APIService or remove the specific finalizer from the specific object. Do **not** blank `spec.finalizers` via the `/finalize` subresource: the namespace vanishes from storage while its contents were never enumerated, leaving unreachable objects in etcd that reappear when a namespace of the same name is recreated. Kubernetes' own docs say *"avoid manually removing finalizers"* ([finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/)). And note the widely-copied `kubectl patch ns X -p '{"metadata":{"finalizers":[]}}'` one-liner patches the **wrong field** entirely.

### Bootstrap: the chicken-and-egg

The agent that deploys everything cannot deploy itself. [12-cell-lifecycle-synthesis.md](12-cell-lifecycle-synthesis.md) lists this as bootstrap paradox #5; here is the mechanical resolution.

**Terraform installs exactly one thing.** L0–L3 of the DAG is Terraform ([08-terraform.md](08-terraform.md)); the boundary sits between L3 and L4 because you cannot plan Kubernetes resources against a cluster that does not exist. After the cluster endpoint is known, a second apply installs the agent and stops. Everything from L4 down is the agent's problem. Keep Terraform's Kubernetes footprint at exactly one resource and the "destroy needs a reachable cluster" problem stays contained.

**Flux self-manages by construction.** `flux bootstrap github --owner=… --repository=… --path=clusters/cell-042` clones or creates the repo, generates an SSH deploy key stored as the `flux-system` Secret, commits `<path>/flux-system/{gotk-components.yaml, gotk-sync.yaml, kustomization.yaml}`, applies them, and creates a `GitRepository` **and** `Kustomization` both named `flux-system` pointing at that same path — so Flux reconciles its own manifests ([bootstrap](https://fluxcd.io/flux/installation/bootstrap/)). Defaults: `--branch main`, `--private true`, `--personal false`, `--network-policy true`, `--interval 1m`, `-n flux-system`. Upgrading is then a commit that bumps `gotk-components.yaml`, or re-running bootstrap. For Terraform-driven cells, `fluxcd/terraform-provider-flux`'s `flux_bootstrap_git` resource does the same thing inside an apply.

**Argo CD self-management is app-of-apps on itself.** Terraform applies the Argo CD manifests once; Argo CD's first Application points at those same manifests, so subsequent upgrades flow through the pipeline. The sharp edge: a bad Argo CD manifest can leave Argo CD unable to fix itself. Keep the bootstrap apply path alive and tested as the recovery route. This is where the rendered-manifest property pays for itself — worst case, `kubectl apply --server-side -f rendered/argocd/` from a laptop is a valid recovery path, and preserving that property is worth more than it looks.

One ordering constraint from the DAG: the agent is a pod, so it needs L4 (CNI, DNS) and L5 (a node) first. On the bootstrap node group, alongside CoreDNS and cert-manager, tainted so workloads never land there ([06-karpenter.md](06-karpenter.md)). And if you install the agent *before* the policy engine ([07-kyverno.md](07-kyverno.md)), remember the agent's own namespace needs to be in the webhook exclusion list or you have built a new deadlock.

### Secrets in GitOps

Neither tool solves this; both punt to an ecosystem. Argo CD's own secret-management page is worth reading in full because it *strongly cautions against* manifest-generation-time injection (the Argo CD Vault Plugin pattern) for a specific reason: **Argo CD stores generated manifests in plaintext in Redis** and serves them over repo-server gRPC ([secret management](https://argo-cd.readthedocs.io/en/stable/operator-manual/secret-management/)).

| | Git holds | Trust root | Rotation | Blast radius if root leaks | Air-gapped? | Op cost |
|---|---|---|---|---|---|---|
| **SOPS** v3.13.1 | ciphertext | KMS key, Vault transit, or age/PGP private key | None built in; re-encrypt and commit | Everything ever encrypted, **including git history**, irreversibly | Yes with age/PGP; no with KMS | Lowest infra; highest human |
| **Sealed Secrets** v0.39.1 | ciphertext | RSA keypair *inside* the cluster | Sealing key auto-renews every **30 days**; old keys kept forever | All SealedSecrets for that key — maintainers say rotation cannot fix it | Yes, best-in-class | One controller; **you own key backup forever** |
| **ESO** v2.9.0 | a pointer | External provider + workload identity | Poll, `refreshInterval` default **1h0m0s** | Scoped to the credential; `ClusterSecretStore` widens it cluster-wide | No | Controller + IAM; **only the newest minor is supported** |
| **VSO** v1.4.0 | a pointer | Vault + auth role | `refreshAfter`, PKI `ttl`, lease renewal, `rolloutRestartTargets` | Scoped by Vault policy; leases shorten exposure | No | Cheap operator, but you run Vault |
| **CSI Driver** v1.6.0 | a pointer | External store, resolved per mount | **Alpha, off by default**; `--rotation-poll-interval` default `2m` | Narrowest resting footprint — no Secret unless `secretObjects` | No | DaemonSet driver + provider on every node |

Notes that change decisions:

- **Sealed Secrets moved orgs** in June 2026: `bitnami-labs/sealed-secrets` → `bitnami/sealed-secrets`, and the old GitHub Pages Helm repo URL **404s with no redirect** ([migration issue](https://github.com/bitnami/sealed-secrets/issues/1982)). Check your `HelmRepository`/`repoURL` today.
- **ESO v1.x is already EOL**; the current major is v2.x, and the project supports only the most recent minor, EOL'd the day the next ships — a real ~2-6 week upgrade treadmill ([stability and support](https://external-secrets.io/latest/introduction/stability-support/)). ESO is CNCF **Sandbox**, not incubating.
- **CSI driver rotation changed shape in v1.6.0**: it now rides the CSI `RequiresRepublish` mechanism, the dedicated rotation controller and its privileged RBAC were removed, and `--rotation-poll-interval` became a *minimum cache duration* rather than a poll timer ([auto rotation](https://secrets-store-csi-driver.sigs.k8s.io/topics/secret-auto-rotation)).
- **Argo CD has no native SOPS.** Flux does (`spec.decryption.provider: sops`, with `sops.asc` for GPG, a `.agekey`-suffixed key for age, or an IAM binding on kustomize-controller for cloud KMS — [SOPS guide](https://fluxcd.io/flux/guides/mozilla-sops/)).

**For the setup this library assumes**, the synthesis is short: you use Vault ([10-vault.md](10-vault.md)), you want rendered manifests, and rendered manifests are incompatible with encrypt-in-git. That points at VSO or ESO with Vault as the provider, with the CSI driver reserved for the handful of workloads where a Secret at rest in etcd is unacceptable. Write down which one and why, because the hybrid estate is where this gets expensive.

### Progressive delivery, and rolling a change across a fleet

Two distinct problems that get conflated.

**Within one cell**, progressive delivery means canary or blue/green on a workload. **Argo Rollouts** v1.9.1 (2026-07-17, a security release for CVE-2026-35469) replaces `Deployment` with a `Rollout` whose `spec.strategy` is `canary` (an ordered `steps` list of `setWeight` / `pause` / `setCanaryScale` / `analysis`) or `blueGreen` (`activeService` + `previewService` + a promotion gate). Automated judgement is `AnalysisTemplate` / `ClusterAnalysisTemplate` → `AnalysisRun`, with providers including Prometheus, Datadog, CloudWatch, Web, and Job ([Argo Rollouts](https://argo-rollouts.readthedocs.io/en/stable/)). Without `trafficRouting`, canary weight is approximated by replica counts. **Flagger** v1.44.0 is the Flux-family equivalent: a `Canary` CRD wrapping an existing Deployment, with `analysis.interval` (default 60s), `stepWeight`/`maxWeight` for canary, `iterations` alone for blue/green, `iterations` plus `match` for A/B, `MetricTemplate` for custom metrics, and webhooks for conformance/load-test gating ([Flagger](https://fluxcd.io/flagger/)). Both are per-cluster and neither orchestrates across clusters.

**Across the fleet**, wave-by-wave rollout is a different mechanism. In Argo CD it is ApplicationSet **Progressive Syncs** — Beta since v3.3.0 but still off by default behind `--enable-progressive-syncs` / `ARGOCD_APPLICATIONSET_CONTROLLER_ENABLE_PROGRESSIVE_SYNCS` / `applicationsetcontroller.enable.progressive.syncs`:

```yaml
spec:
  strategy:
    type: RollingSync            # or AllAtOnce (default)
    deletionOrder: Reverse       # optional; requires RollingSync + steps
    rollingSync:
      steps:
        - matchExpressions:
            - { key: ring, operator: In, values: ["0"] }   # canary cells
        - matchExpressions:
            - { key: ring, operator: In, values: ["1"] }
          maxUpdate: 0                                     # 0 = manual gate
        - matchExpressions:
            - { key: ring, operator: In, values: ["2"] }
          maxUpdate: 10%                                   # rounds down, floored at 1
```

Steps match on labels of the **generated Applications** (which is why the ApplicationSet template above stamps `ring` from the cluster Secret). Every Application in a step must be `Healthy` before the next begins. Three behaviors to plan around: **RollingSync forces auto-sync off** on every generated Application, and logs warnings for templates that declare one; Applications matching **no** step are skipped entirely and need a manual sync; and syncs are triggered by setting `operation` exactly as the UI does, so sync windows and per-Application retry settings are respected.

In Flux there is no equivalent; you chain `dependsOn` between per-ring Kustomizations, or promote by PR between rendered branches. The third option — often the best one for a cell fleet — is **promotion by PR between rendered environments**: ring 0 renders from `main`, and promoting is a PR that fast-forwards ring 1's rendered branch to ring 0's commit. It is slower, entirely auditable, needs no feature flag, and the rollback is a revert.

Do not confuse sync waves with progressive syncs. Sync waves order **resources inside one Application's sync**. Progressive syncs order **Applications owned by one ApplicationSet**, gating on Application health, across clusters. They compose; they do not substitute.

### Repo structure for N cells

Be opinionated here, because the default of "we'll figure it out" produces a 300-cell repo nobody can review.

Flux's guide names four patterns — monorepo, repo per environment, repo per team, repo per app — and for many clusters recommends one thin directory per cluster under `clusters/`, holding only entrypoint Kustomizations that point at shared `apps/` and `infrastructure/` overlays ([repository structure](https://fluxcd.io/flux/guides/repository-structure/), [flux2-kustomize-helm-example](https://github.com/fluxcd/flux2-kustomize-helm-example)).

For a cell-lifecycle team, here is what I would build:

```text
cells-gitops/                          # ONE repo. Per-cell repos are a mistake at 300.
├── charts/                            # dry source: library chart + component charts
│   ├── temporal-lib/                  # naming, labels, PDBs, topology spread
│   └── temporal-cell/
├── values/
│   ├── base.yaml
│   ├── clouds/{aws,gcp,azure}.yaml     # storage classes, LB annotations, workload identity
│   ├── tiers/{production,staging}.yaml
│   └── cells/
│       ├── cell-042.yaml               # THE cell's identity. ~20 lines. The review surface.
│       └── cell-043.yaml
├── rendered/                          # GENERATED. CI writes it. Humans never edit it.
│   ├── cell-042/{cert-manager,vault-agent,kyverno-policies,temporal}/
│   └── cell-043/...
├── fleet/
│   ├── appsets/cell-components.yaml    # the ApplicationSet above
│   ├── projects/cells.yaml             # AppProject guardrails
│   └── clusters/cell-042.yaml          # cluster Secret WITHOUT credentials (ESO fills them)
└── .github/workflows/render.yml
```

The opinions behind it:

1. **One repo, not 300.** Per-cell repos multiply your git provider API pressure by 300, make a fleet-wide change 300 PRs, and give you no way to diff cells against each other. Use directories.
2. **A cell's identity is one small values file.** Adding a cell should be a ~20-line PR a reviewer can fully understand. If it touches five files, the abstraction is wrong.
3. **`rendered/` is generated and never hand-edited**, enforced by CODEOWNERS and a CI staleness check that fails the build if re-rendering produces a diff. This is the single most important guardrail; without it the pattern rots within a month.
4. **Keep the PR reviewable by scoping the diff.** A change to `values/base.yaml` legitimately regenerates 300 directories, which no human reviews. Have CI post a *summary* — N cells affected, the set of distinct diffs (usually one), the full diff for a representative cell — and expand per-cell diffs only where they differ. "300 cells, 1 unique diff, shown below" is reviewable; 300 identical diffs are not.
5. **Environment promotion is directory-scoped rendering, not branches.** Render ring 0 from `main` on every merge; promote by a PR that advances ring 1's rendered inputs. The hydrator proposal makes the matching argument: directories are the write interface, branches are the read interface.
6. **Cluster registration is itself GitOps'd**, with the credential injected by ESO rather than committed. A cell that exists in Terraform but not in `fleet/clusters/` is invisible to the reconciler — exactly the gap your teardown sweeper should detect.

### Drift, emergency changes, and break-glass

Someone will `kubectl edit` during an incident. With `selfHeal: true` Argo CD reverts it in about **5 seconds** (`--self-heal-timeout-seconds`). That is correct behavior and a terrible surprise at 3 a.m., so make the escape hatch a rehearsed runbook rather than something invented under pressure.

The Argo CD break-glass ladder, least to most invasive:

1. `argocd app terminate-op <app>` — stop the sync running *right now*.
2. `spec.syncPolicy.automated.enabled: false` (or `argocd app set <app> --sync-policy manual`; `none` is an alias) — stop future auto-syncs while keeping `prune`/`selfHeal` config intact. This is the cleanest toggle.
3. `selfHeal: false` — allow hand-patching without a 5-second revert. Note there is no `--no-self-heal` flag; edit the manifest.
4. An `AppProject` `deny` sync window with `manualSync: true` — a fleet-wide freeze with an operator escape hatch. Deny windows override allow windows, and they affect **both** manual and automated syncs unless `manualSync` is set ([sync windows](https://argo-cd.readthedocs.io/en/stable/user-guide/sync_windows/)).
5. `argocd.argoproj.io/skip-reconcile: "true"` — a total stop for one Application. It is **Alpha since v2.7**, and the docs list the disaster-recovery use case under "Alternative Use Cases" and then say those are "generally not recommended." Know it exists; prefer 2 or 3.

The Flux equivalent: `flux suspend kustomization <name>` / `flux suspend helmrelease <name>` / `flux suspend source git <name>` (note there is no `flux suspend gitrepository`), or the per-object annotation `kustomize.toolkit.fluxcd.io/reconcile: disabled` which stops both apply and prune for one resource. **The trap is documented and vicious**: setting `spec.suspend: true` in the cluster gets reconciled away by Git unless you also change the Git manifest — *"the manually applied patch would be overwritten by the declared state in Git."* Suspend the parent Kustomization first, or change Git.

Three things to build before you need them:

- **An ApplicationSet-level ignore for the toggle you will actually flip.** `ignoreApplicationDifferences` on `/spec/syncPolicy` means you can disable auto-sync on one cell without the ApplicationSet controller putting it straight back.
- **A drift metric with a cell label**, exported from `argocd_app_info` (note that 3.0 removed `argocd_app_sync_status`, `argocd_app_health_status`, and `argocd_app_created_time`, folding them into labels on `argocd_app_info`) or from Flux's `gotk_*` metrics. "How many cells are out of sync and for how long" should be one number on one dashboard.
- **A written expiry on every break-glass action.** The real failure is not the emergency edit; it is the emergency edit that is still there four months later because reconciliation was suspended and nobody remembered. Suspension should page someone after N hours.

### Scale and operational limits

Real numbers, labelled by source type.

**Argo CD, documented.** Polling default is `timeout.reconciliation: 120s` plus `timeout.reconciliation.jitter: 60s`, so 120-180s — the widely quoted "every three minutes" is the *ceiling*, and Argo CD's own docs contradict themselves on this. Caches: `--repo-cache-expiration` **24h**, `--default-cache-expiration` **24h**, `--revision-cache-expiration` **3m**, `--app-state-cache-expiration` **1h**, `--repo-server-timeout-seconds` **60**. `--parallelismlimit` on the repo-server has **no default — it is unlimited**, which is how repo-servers get OOM-killed. Webhooks let you raise the reconciliation timeout to `15m` or `1h` instead of polling everything every three minutes; the endpoint is `/api/webhook`, GitHub must be set to `application/json` content type, and `webhook.maxPayloadSizeMB` defaults to **50** with no rate limiting on the endpoint.

**The monorepo problem, documented.** The repo-server keeps one local clone per repository, and if manifest generation must modify files in that clone, **only one concurrent generation per repo-server instance is allowed** — the docs say this "might significantly slow down Argo CD if you have a monorepo with multiple applications (50+)" ([monorepo scaling](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/#monorepo-scaling-considerations)). Since v3.0 multiple Helm apps in the same directory generate in parallel by default; plugin apps need an `.argocd-allow-concurrency` marker or a sidecar CMP; **multiple Kustomize apps with parameter overrides have no workaround**. Worse, the manifest cache is keyed by commit SHA, so *any* commit invalidates the cache for *every* app in the repo. The fix is the `argocd.argoproj.io/manifest-generate-paths` annotation, which since v2.11 works **without** a webhook. Measure whether it is helping with `argocd_webhook_store_cache_attempts_total{successful="true"}`. Also: use `refs/heads/main`, not `main` — resolving a short ref loads and iterates every branch and tag.

**Published fleet numbers, secondary.** Intuit reported **12,000 applications, 370 clusters, 3,000 git repositories** at ArgoCon '21, and a separate 200+ cluster addon fleet framed as "either 20 of 200 clusters or all 200" ([ArgoCon '21](https://argoproj.github.io/argocon21/)). Akuity's load test reached 1,000 clusters and 50,000 apps, and reports the default config handling "a dozen mid-size clusters," comfortable at hundreds of apps, "a little slower at ~3,000" and "starts to struggle beyond 5,000" — with the root cause of UI/API degradation named as missing server-side pagination ([Akuity, secondary](https://akuity.io/blog/argo-cd-ultimate-scalability)). The CNCF 2025 Argo CD survey found 42% running >500 apps per instance and 25% connecting >20 clusters. **The UI is the first casualty** at a few thousand Applications, well before the controller is.

**Git provider limits, documented.** GitHub REST is 5,000 requests/hour authenticated; GitHub App installation tokens get a minimum of 5,000/hr, 15,000/hr on GitHub Enterprise Cloud, and outside GHEC scale +50/hr per repo beyond 20 repos, capped at 12,500/hr. At 300 cells polling every 3 minutes you are at 6,000 requests/hour from polling alone. Webhooks are not an optimization; at fleet scale they are a requirement. Argo CD caches GitHub App credentials for 60 minutes.

**Flux, documented.** Sharding guidance triggers at "tens of thousands of applications" and is static and label-based: `sharding.fluxcd.io/key` plus `--watch-label-selector=sharding.fluxcd.io/key=shard1` on shard controllers and `--watch-label-selector=!sharding.fluxcd.io/key` on the default ones ([sharding](https://fluxcd.io/flux/installation/configuration/sharding/)). notification-controller and source-watcher do not support it. Vertical scaling guidance starts at "hundreds of applications": `--concurrent=10`, `--requeue-dependency=5s`, limits 2000m CPU / 2Gi. Source defaults verified in code: `--concurrent` is 2 for source-controller and 4 for kustomize-controller, `--concurrent-ssa` 4, `--kube-api-qps` 50, `--kube-api-burst` 300 — **but if the API server has Priority and Fairness enabled, Flux sets QPS/burst to -1 and those flags become no-ops.**

**The shard failure mode nobody plans for**, from a public load test: when an application controller starts OOM-killing, its shard "got stuck, did not recover cleanly, and other controllers did not take over" — the Applications on that shard silently stay OutOfSync ([ITNEXT, secondary](https://itnext.io/how-we-load-test-argo-cd-at-scale-1-000-vclusters-with-gitops-on-kubernetes-d8ea2a8935b6)). Alert on per-shard reconcile staleness, not just on controller restarts.

### Policy integration: CI versus admission

Both matter and they catch different things. [07-kyverno.md](07-kyverno.md) covers admission; here is the CI half and the seam between them.

The rendered-manifest pattern makes CI policy trivially good, because the artifact CI validates is byte-identical to what the cluster receives. A cell PR gate should run, in order:

```bash
# 1. Render deterministically. Same flags as production, pinned Helm binary.
make render CELLS=all

# 2. Fail if rendered/ is stale relative to the dry source. THE critical gate.
git diff --exit-code -- rendered/ || { echo "rendered/ is stale; run make render"; exit 1; }

# 3. Schema-validate against the real Kubernetes version + CRD schemas.
#    kubeconform v0.8.0 (2026-06-04). The CRDs-catalog template is the standard trick.
kubeconform -strict -summary \
  -kubernetes-version 1.37.0 \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  rendered/

# 4. Run the SAME policies that admission will run, before admission sees them.
kyverno apply policies/ --resource rendered/ --detailed-results

# 5. Rego for the things Kyverno is awkward at.
conftest test --policy policy/ rendered/

# 6. Diff against live, as a comment on the PR.
argocd app diff cell-042-temporal --local rendered/cell-042/temporal --server-side-generate
```

Points that matter:

- **Run the same policy set in CI and at admission.** Kyverno's CLI (`kyverno apply`, `kyverno test`) exists precisely for this, and Kyverno **graduated CNCF on 2026-03-16**. Note that `ClusterPolicy` and `CleanupPolicy` are now marked deprecated in favor of the CEL-based `ValidatingPolicy`/`MutatingPolicy` family — plan that migration.
- **CI is not a substitute for admission.** CI validates what you intended to apply; admission validates what actually arrives, including things no pipeline produced. Keep both. The value of CI is that a policy violation becomes a red PR check instead of a stuck sync that a human has to diagnose from a controller log.
- **Diff-in-PR is not a shipped feature of either tool.** `argocd app diff` (exit codes 0 no-diff / 1 diff / 2 error; **Secrets are ignored from the diff**) and `flux diff kustomization` (0 / 1 / >1, and it is a server-side dry-run so it needs cluster access) are the first-party primitives. The PR-comment layer is community: `argocd-diff-action/argocd-diff-action` is the maintained fork — the original `quizlet/argocd-diff-action` was archived 2025-11-24, and `allenporter/flux-local` is also archived.
- **`kubeval` is dead and `datreeio/datree` was archived 2024-06-06** with its backing company closed. The `datreeio/CRDs-catalog` repo is still live and still the schema source kubeconform points at — but it lives in a dormant org, so vendor or pin it.
- **Pre-commit catches the cheap class.** `check-yaml`, `yamllint`, and a `helm lint` hook cost nothing and keep the CI signal meaningful. There is no first-party kubeconform pre-commit hook, despite frequent claims otherwise.

---

## Hands-on

Six labs on kind. Roughly three hours end to end. The point is not to learn the CLIs; it is to *see* the reconcile loop fight you, and to break pruning on purpose in a place where it does not matter.

### Prerequisites

```bash
# Required. Install per each project's own docs; do not trust versions from memory.
kind version        # >= 0.30
kubectl version --client
helm version        # v4.x -- see 09-helm.md on the Helm 3 EOL timeline
argocd version --client
flux --version
git --version
jq --version

# You need a git repo the clusters can reach; a public GitHub repo is simplest.
export GH_USER="your-github-user"   # substitute yours; unquoted <angle brackets>
export GH_REPO="gitops-lab"         # are shell redirections, not placeholders
```

Create three clusters. One is the hub; the other two are stand-ins for cells.

```bash
for c in hub cell-a cell-b; do kind create cluster --name "$c"; done
kubectl config get-contexts | grep kind-
```

### Lab 1 — Install Argo CD and deploy from git

```bash
kubectl config use-context kind-hub
kubectl create namespace argocd

# Pin the version. "stable" is a moving target, and the `stable` docs branch
# is currently behind the `stable` manifest tag.
ARGOCD_VERSION=v3.5.2
kubectl apply -n argocd -f \
  "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
kubectl -n argocd rollout status deploy/argocd-server --timeout=5m

# Read what you just installed. Name every pod and say what it does.
kubectl -n argocd get deploy,sts

kubectl -n argocd port-forward svc/argocd-server 8080:443 >/dev/null 2>&1 &
argocd admin initial-password -n argocd
argocd login localhost:8080 --username admin --insecure
```

Create an Application against the upstream example repo:

```bash
argocd app create guestbook \
  --repo https://github.com/argoproj/argocd-example-apps.git \
  --path guestbook \
  --dest-server https://kubernetes.default.svc \
  --dest-namespace default \
  --sync-policy automated --auto-prune --self-heal

argocd app sync guestbook
argocd app get guestbook

# Now look at what Argo CD stamped on the objects. This is the inventory.
kubectl get deploy guestbook-ui -o jsonpath='{.metadata.annotations}' | jq .
# Expect: argocd.argoproj.io/tracking-id: guestbook:apps/Deployment:default/guestbook-ui
# Note there is NO app.kubernetes.io/instance label -- 3.0 defaults to annotations.
```

### Lab 2 — Break drift on purpose

```bash
# 1. Scale it out of band, the way a human would during an incident.
kubectl scale deploy guestbook-ui --replicas=5
watch -n1 'kubectl get deploy guestbook-ui -o jsonpath="{.spec.replicas}{\"\n\"}"'
# Reverts in ~5s. That is --self-heal-timeout-seconds, default 5.

# 2. Disable automated sync entirely and try again. This is break-glass rung 2.
# (Rung 3 -- selfHeal: false while keeping automated sync -- has no CLI flag;
#  you have to edit spec.syncPolicy.automated.selfHeal on the Application.)
argocd app set guestbook --sync-policy manual
kubectl scale deploy guestbook-ui --replicas=5
argocd app get guestbook            # OutOfSync, and it STAYS out of sync
argocd app diff guestbook; echo "exit=$?"   # 1 == differences found

# 3. Add a resource Argo CD does not know about and confirm it is NOT pruned.
kubectl create configmap orphan --from-literal=k=v
argocd app sync guestbook --prune
kubectl get configmap orphan        # still there: untracked objects are never pruned

# 4. Now make it tracked, remove it from the desired set, and watch prune work.
kubectl annotate configmap orphan \
  'argocd.argoproj.io/tracking-id=guestbook:/ConfigMap:default/orphan'
argocd app sync guestbook --prune
kubectl get configmap orphan        # gone

# 5. Prove Prune=true is a no-op. This is the single most common myth.
argocd app set guestbook --sync-option Prune=true
argocd app get guestbook -o json | jq '.spec.syncPolicy.syncOptions'
# It is accepted, stored, and does nothing. Remove it: !Prune=true
argocd app set guestbook --sync-option '!Prune=true'
argocd app set guestbook --sync-policy automated --auto-prune --self-heal
```

### Lab 3 — ApplicationSet fanning one component across three cells

Register the two cell clusters with the hub. The kind API server addresses are container-internal, so use the docker network address.

```bash
kubectl config use-context kind-hub
for c in cell-a cell-b; do
  SRV=$(docker inspect "${c}-control-plane" \
        --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
  # `argocd cluster add` has no --server-url flag; rewrite the kubeconfig entry
  # so the address it reads is already the docker-network one.
  kubectl config set-cluster "kind-${c}" --server="https://${SRV}:6443"
  argocd cluster add "kind-${c}" --name "$c" --yes
done

# Label the cluster Secrets. This is your cell taxonomy.
kubectl -n argocd label secret -l argocd.argoproj.io/secret-type=cluster tier=production
kubectl -n argocd label secret cluster-cell-a ring=0 --overwrite 2>/dev/null || true
kubectl -n argocd label secret cluster-cell-b ring=1 --overwrite 2>/dev/null || true
kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=cluster --show-labels
```

Now the ApplicationSet. Three "cells": the two remote clusters plus the hub's own in-cluster destination, added via the list generator so you can see both generator styles side by side.

```yaml
# appset.yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: fleet-guestbook
  namespace: argocd
spec:
  goTemplate: true
  goTemplateOptions: ["missingkey=error"]
  generators:
    - matrix:
        generators:
          # Selecting on secret-type deliberately EXCLUDES the local hub cluster,
          # because the default in-cluster entry has no Secret and thus no label.
          - clusters:
              selector:
                matchLabels:
                  argocd.argoproj.io/secret-type: cluster
          - list:
              elements:
                - component: guestbook
                  path: guestbook
  template:
    metadata:
      name: '{{.nameNormalized}}-{{.component}}'
      labels:
        cell: '{{.nameNormalized}}'
        ring: '{{index .metadata.labels "ring"}}'
    spec:
      project: default
      source:
        repoURL: https://github.com/argoproj/argocd-example-apps.git
        targetRevision: HEAD
        path: '{{.path}}'
      destination:
        server: '{{.server}}'
        namespace: '{{.component}}'
      syncPolicy:
        automated: { prune: true, selfHeal: true }
        syncOptions: ["CreateNamespace=true"]
```

```bash
kubectl apply -f appset.yaml
argocd app list        # two Applications, one per cell, from one file

# Prove missingkey=error earns its keep: remove a label and watch it FAIL LOUDLY
# instead of silently generating an Application with an empty ring label.
kubectl -n argocd label secret cluster-cell-a ring-
kubectl -n argocd logs deploy/argocd-applicationset-controller --tail=20 | grep -i 'map has no entry'
kubectl -n argocd label secret cluster-cell-a ring=0

# Now prove the fleet query language works: add
#   matchExpressions: [{key: ring, operator: In, values: ["0"]}]
# to the selector, re-apply, and watch cell-b's Application be DELETED. That is
# --policy sync (the default). Use applicationsSync: create-update plus
# --enable-policy-override if you want retargeting to be non-destructive.
```

### Lab 4 — Waves, health, and a resource that refuses to die

```bash
# Watch the inter-wave delay cost you real time; re-sync something multi-wave
# and time it, then put it back. Note "10s" would be silently ignored.
kubectl -n argocd set env sts/argocd-application-controller ARGOCD_SYNC_WAVE_DELAY=10
kubectl -n argocd rollout status sts/argocd-application-controller
kubectl -n argocd set env sts/argocd-application-controller ARGOCD_SYNC_WAVE_DELAY-

# Build the finalizer trap that will one day wedge a real teardown.
kubectl config use-context kind-cell-a
kubectl create namespace doomed
kubectl -n doomed create configmap stuck --from-literal=a=b
kubectl -n doomed patch configmap stuck -p '{"metadata":{"finalizers":["example.com/never"]}}'
kubectl delete namespace doomed --wait=false

# Diagnose it the RIGHT way. Read the conditions, do not reach for the hack.
kubectl describe namespace doomed | sed -n '/Conditions/,$p'
# Expect NamespaceFinalizersRemaining / SomeFinalizersRemain naming the exact finalizer.

# Fix it correctly: remove the finalizer from the OBJECT, not from the namespace.
kubectl -n doomed patch configmap stuck --type=merge -p '{"metadata":{"finalizers":null}}'
kubectl get namespace doomed        # terminates

# Read, do not run: the dangerous version is a PUT of an emptied spec.finalizers
# to /api/v1/namespaces/doomed/finalize. It deletes the namespace object while
# leaving its contents unreachable in etcd. Kubernetes' own docs advise against it.
```

### Lab 5 — The same thing with Flux

```bash
kubectl config use-context kind-cell-a
export GITHUB_TOKEN="ghp_..."   # a PAT with repo scope

flux check --pre
flux bootstrap github \
  --owner="$GH_USER" --repository="$GH_REPO" \
  --branch=main --path=clusters/cell-a --personal

# Read what bootstrap did. This is the self-management property.
kubectl -n flux-system get gitrepository,kustomization
git clone "https://github.com/$GH_USER/$GH_REPO" /tmp/$GH_REPO && cd /tmp/$GH_REPO
ls clusters/cell-a/flux-system/    # gotk-components.yaml gotk-sync.yaml kustomization.yaml
```

Add a two-stage dependency chain, so you can see `dependsOn` do — and fail to do — what people expect:

```bash
mkdir -p apps/base infra/base
cat > clusters/cell-a/apps.yaml <<'YAML'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: infra
  namespace: flux-system
spec:
  interval: 1m
  path: ./infra/base     # a DIFFERENT path: two Kustomizations over one path
  prune: true            # means two inventories claiming the same objects, and
                         # pruning either one deletes the other's resources
  wait: true             # WITHOUT this, Ready=True means "applied", not "healthy"
  sourceRef: { kind: GitRepository, name: flux-system }
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  interval: 1m
  path: ./apps/base
  prune: true
  dependsOn: [{ name: infra }]
  sourceRef: { kind: GitRepository, name: flux-system }
YAML

cat > infra/base/kustomization.yaml <<'YAML'
resources: [namespace.yaml]
YAML
cat > infra/base/namespace.yaml <<'YAML'
apiVersion: v1
kind: Namespace
metadata: { name: demo-infra }
YAML

cat > apps/base/kustomization.yaml <<'YAML'
resources: [configmap.yaml]
YAML
cat > apps/base/configmap.yaml <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata: { name: demo, namespace: default }
data: { key: original }
YAML

git add -A && git commit -m "add apps" && git push
flux reconcile kustomization flux-system --with-source
flux get kustomizations
flux tree kustomization apps          # the inventory, rendered

# Inspect the inventory directly -- this is Flux's answer to tracking-id.
kubectl -n flux-system get kustomization apps -o jsonpath='{.status.inventory}' | jq .
# Entries look like: default_demo__ConfigMap

# Drift: Flux uses SSA, so watch the field manager, not just the value.
kubectl -n default patch configmap demo --type=merge -p '{"data":{"key":"tampered"}}'
kubectl -n default get configmap demo --show-managed-fields -o yaml | grep -A3 manager:
flux reconcile kustomization apps     # reverted; ssa: Override is the default

# Exempt it, the Flux way, and confirm the revert stops.
kubectl -n default annotate configmap demo kustomize.toolkit.fluxcd.io/ssa=Merge
kubectl -n default patch configmap demo --type=merge -p '{"data":{"extra":"kept"}}'
flux reconcile kustomization apps
kubectl -n default get configmap demo -o jsonpath='{.data}' ; echo

# Break-glass, and the trap: an in-cluster suspend only survives while Git is
# silent on the field. Declare `suspend: false` in the committed manifest and the
# same command is reconciled away within one interval. Test both; know which you have.
flux suspend kustomization apps
kubectl -n flux-system get kustomization apps -o jsonpath='{.spec.suspend}'; echo
flux resume kustomization apps

# Prune: delete the manifest, confirm the object goes.
git rm apps/base/configmap.yaml
sed -i.bak 's/resources: \[configmap.yaml\]/resources: []/' apps/base/kustomization.yaml
git add -A && git commit -m "remove configmap" && git push
flux reconcile kustomization flux-system --with-source
kubectl -n default get configmap demo   # NotFound
```

### Lab 6 — Make the two tools fight over one object

This is the lab that teaches the thing you will actually debug.

```bash
# On cell-a, Flux already owns the `demo` ConfigMap. Point Argo CD at it too.
# Re-create it via Flux, then apply a conflicting value as a different manager.
kubectl config use-context kind-cell-a

kubectl -n default create configmap contested --from-literal=owner=flux
kubectl -n default annotate configmap contested \
  kustomize.toolkit.fluxcd.io/ssa=Override

# Simulate the GitOps controller taking a field under its own manager.
kubectl -n default apply --server-side --field-manager=kustomize-controller -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata: { name: contested, namespace: default }
data: { owner: flux }
YAML

# Now a second writer with a different manager name -- exactly what happens when
# Argo CD and Flux, or a controller and your pipeline, both claim one field.
kubectl -n default apply --server-side --field-manager=argocd-controller -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata: { name: contested, namespace: default }
data: { owner: argocd }
YAML
# Expect: Apply failed with 1 conflict: conflict with "kustomize-controller"

# Read the ownership record. This is the source of truth, not either tool's UI.
kubectl -n default get configmap contested --show-managed-fields -o json \
  | jq '.metadata.managedFields[] | {manager, operation, fields: .fieldsV1}'

# Take ownership deliberately -- which is exactly what ServerSideApply=true does,
# because Argo CD runs `kubectl apply --server-side --force-conflicts`.
kubectl -n default apply --server-side --force-conflicts \
  --field-manager=argocd-controller -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata: { name: contested, namespace: default }
data: { owner: argocd }
YAML
kubectl -n default get configmap contested --show-managed-fields -o json \
  | jq '[.metadata.managedFields[].manager]'
# kustomize-controller has lost the field. In production this is an infinite
# revert war between two reconcilers, at whatever the shorter interval is.
```

Cleanup:

```bash
for c in hub cell-a cell-b; do kind delete cluster --name "$c"; done
```

---

## Production gotchas

1. **`Prune=true` is not a sync option.** Only `Prune=false` and `Prune=confirm` exist; in 3.5's in-tree engine the constant is `SyncOptionPrune = "Prune"` with values `"false"` and `"confirm"`, so `Prune=true` parses and silently does nothing. Pruning is `spec.syncPolicy.automated.prune: true` ([gitops-engine types](https://github.com/argoproj/gitops-engine/blob/master/pkg/sync/common/types.go), [auto sync](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/auto_sync/)).

2. **Deleting an Argo CD Application without the `resources-finalizer.argocd.argoproj.io` finalizer orphans everything it created.** ApplicationSet only adds that finalizer when `preserveResourcesOnDeletion` is `false`, and `--cascade=orphan` does not remove it from already-generated Applications ([application deletion](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Application-Deletion/)).

3. **`goTemplateOptions: ["missingkey=error"]` is not the default**, kept off for backwards compatibility. Without it a typo'd variable renders as empty and you generate N wrong Applications with no error anywhere ([GoTemplate](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/GoTemplate/)).

4. **The ApplicationSet cluster generator silently excludes the hub.** The default local cluster has no Secret, so it carries no `argocd.argoproj.io/secret-type` label, so any selector on that label drops it. Usually what you want, occasionally a two-hour mystery ([Generators-Cluster](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Cluster/)).

5. **Matrix supports exactly two children and fails on duplicate `path*` keys.** If both children are Git generators, one must set `pathParamPrefix` or the matrix errors at generation time — after passing API validation ([Generators-Matrix](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Matrix/)).

6. **Merge on nested values does not work under `goTemplate: true`.** The docs state it: *"Merging on nested values while using goTemplate: true is currently not supported."* A `mergeKeys: [values.selector]` fails silently to match ([Generators-Merge](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Merge/)).

7. **`ARGOCD_SYNC_WAVE_DELAY` is parsed with `strconv.Atoi`.** Setting it to `"5s"` is silently ignored and you get the 2-second default. 12 waves means 11 inter-wave delays (there is no delay after the last), so 22 s per Application; at 300 Applications that is ~1.8 hours of *aggregate* controller sleep, and divided by `--operation-processors` (default 10) still roughly 11 minutes of pure sleep added to a full fleet sync ([controller/sync.go](https://github.com/argoproj/argo-cd/blob/release-3.5/controller/sync.go)).

8. **Argo CD sharding is by cluster, not by Application.** All Applications targeting one cluster live on one shard, so adding replicas cannot relieve a single oversized cell — use `--status-processors` (default 20) and `--operation-processors` (default 10) instead ([high availability](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/)).

9. **`legacy` is still the default sharding algorithm and both alternatives are experimental.** `round-robin` reshuffles every cluster if the rank-0 cluster is removed. Consistent hashing with bounded loads reduced reassignments from 75 to 15 in the CNOE 10→9-shard benchmark, but the docs still want community feedback before calling it production-ready.

10. **`--parallelismlimit` on the repo-server has no default — it is unlimited.** Combine it with `GOMEMLIMIT` at 80-90% of the container limit, or a burst of concurrent Kustomize invocations will OOM-kill the repo-server and take every Application's manifest generation with it.

11. **A monorepo invalidates the manifest cache for every app on every commit**, because the cache key is the commit SHA. `argocd.argoproj.io/manifest-generate-paths` is the fix and it has worked **without webhooks since v2.11** — but a shallow clone (`depth: "1"`) disables the non-webhook path ([monorepo scaling](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/#monorepo-scaling-considerations)).

12. **Use `refs/heads/main`, not `main`.** Resolving a short ref loads and iterates every branch and tag; the docs call it CPU and memory intensive on repos with many refs. Fleet repos accumulate refs.

13. **`argocd app diff` ignores Secrets.** *"Kubernetes Secrets are ignored from this diff."* A PR gate built on it will never show a secret change ([argocd app diff](https://argo-cd.readthedocs.io/en/stable/user-guide/commands/argocd_app_diff/)).

14. **The ApplicationSet refresh annotation is `argocd.argoproj.io/application-set-refresh`.** The single-word spelling that appears in many blog posts does not exist in the docs or in `common/common.go` and is a no-op.

15. **`ignoreApplicationDifferences` is applied as a MergePatch, so lists are replaced wholesale.** Ignoring one entry of `spec.sources` holds only until anything else in that list changes ([controlling resource modification](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Controlling-Resource-Modification/)).

16. **`RollingSync` forces auto-sync off on every generated Application**, and Applications matching no step are skipped entirely and require a manual sync. Enabling progressive syncs changes the operating model of the whole ApplicationSet, not just its ordering ([Progressive Syncs](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Progressive-Syncs/)).

17. **Flux `wait: true` ignores `.spec.healthChecks`.** They are mutually exclusive, not additive — setting both and expecting the specific checks to run is a common misreading ([wait](https://fluxcd.io/flux/components/kustomize/kustomizations/#wait)).

18. **Flux `dependsOn` only gates on `Ready=True`, which by default means "applied," not "healthy."** The *dependency* must set `wait` or `healthChecks` for the gate to mean anything. This is how a bootstrap chain appears to work in a lab and races in production ([dependencies](https://fluxcd.io/flux/components/kustomize/kustomizations/#dependencies)).

19. **Flux Kustomization `.spec.timeout` defaults to `interval - 30s` (floored at 30s), not to `interval`** as the prose says — verified in `GetTimeout()`. A 1-minute interval gives you a 30-second budget for build + apply + health checks.

20. **The `kustomize.toolkit.fluxcd.io/ssa` annotation's fourth value is `IfNotPresent`, not `IgnoreDifferences`.** The valid set is `Override` (default), `Merge`, `IfNotPresent`, `Ignore` ([annotations](https://fluxcd.io/flux/components/kustomize/kustomizations/#kustomizetoolkitfluxcdiossa)).

21. **A `spec.suspend: true` applied in-cluster gets reconciled away by Git.** The docs say it directly: *"the manually applied patch would be overwritten by the declared state in Git."* Your incident-time suspend evaporates on the next reconcile unless you also change Git or suspend the parent.

22. **Flux sets `--kube-api-qps`/`--kube-api-burst` to -1 when the API server has Priority and Fairness enabled**, disabling client-side throttling entirely and making those flags no-ops. Tuning them on a modern cluster does nothing ([fluxcd/pkg client](https://github.com/fluxcd/pkg/blob/main/runtime/client/client.go)).

23. **Changing your SSA field manager name orphans every field it owned.** Argo CD's is fixed at `argocd-controller` and is not flag-configurable in 3.5; Flux's are `kustomize-controller` and `helm-controller`. Do not rename the field manager on your own pipeline's `kubectl apply` without treating it as a migration ([Kubernetes SSA](https://kubernetes.io/docs/reference/using-api/server-side-apply/)).

24. **One unavailable aggregated APIService stops every namespace in the cluster from terminating**, because the namespace controller must enumerate all API resources to know what to delete. Diagnose with `kubectl get apiservice | grep False` before touching any finalizer ([GKE troubleshooting](https://cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces)).

25. **The rendered-manifest pattern is incompatible with SOPS and the Argo CD Vault Plugin.** The Source Hydrator docs are explicit: those secrets would be committed to git. Choosing rendered manifests is choosing a runtime secrets operator ([source hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/)).

26. **Argo CD stores generated manifests in plaintext in Redis.** This is why its own secret-management page cautions against generation-time secret injection, and why "Redis is just a cache" is true operationally but not true from a security review's perspective ([secret management](https://argo-cd.readthedocs.io/en/stable/operator-manual/secret-management/)).

27. **ESO supports only the newest minor version and EOLs the previous one the day a new one ships.** v1.x is already dead; the current major is v2.x. Budget for a continuous upgrade, not an annual one ([stability and support](https://external-secrets.io/latest/introduction/stability-support/)).

28. **Sealed Secrets moved GitHub orgs and the old Helm repo URL 404s with no redirect** (`bitnami-labs.github.io/sealed-secrets` → `bitnami.github.io/sealed-secrets`). Git clones still redirect, which is why this breaks CI weeks after it breaks charts ([migration issue](https://github.com/bitnami/sealed-secrets/issues/1982)).

29. **An OOM-killed Argo CD controller shard does not fail over.** A public load test found the shard "got stuck, did not recover cleanly, and other controllers did not take over," leaving its Applications silently OutOfSync. Alert on per-shard reconcile staleness, not on pod restarts ([ITNEXT, secondary](https://itnext.io/how-we-load-test-argo-cd-at-scale-1-000-vclusters-with-gitops-on-kubernetes-d8ea2a8935b6)).

30. **300 cells polling every 3 minutes is 6,000 GitHub API requests per hour** against a 5,000/hour authenticated limit. Webhooks plus a raised `timeout.reconciliation` are a scale requirement, not an optimization ([GitHub rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)).

---

## How this shows up in cell lifecycle

**Provisioning.** Terraform does L0-L3 and installs exactly one thing at L4: the agent ([08-terraform.md](08-terraform.md)). Everything below is a commit. Creating a cell becomes: write `values/cells/cell-311.yaml`, run `make render`, register the cluster Secret, merge. The ApplicationSet cluster generator does the rest, and the PR diff is the complete set of Kubernetes objects the cell will contain. That is a dramatically better review surface than "we ran the script," and it is the single strongest argument for this whole architecture.

**The bootstrap DAG, concretely.** The reconciler sits at L4/L5 and drives L6-L10 of the [cell lifecycle DAG](12-cell-lifecycle-synthesis.md). Sync waves or `dependsOn` encode that DAG: PKI before secrets ([11-cert-manager-and-pki.md](11-cert-manager-and-pki.md), [10-vault.md](10-vault.md)), secrets before data stores, policy controller before policies ([07-kyverno.md](07-kyverno.md)), schema before Temporal servers. Two cautions. First, waves gate on *health*, so every CRD in that chain needs a custom health check or the ordering is decorative. Second, the reconciler is level-triggered, so a wave that cannot converge stalls forever rather than failing — which is the right behavior for a cell that is merely slow, and the wrong behavior for a cell that is genuinely broken. Put a timeout *outside* the reconciler, in the workflow driving the provision.

**Upgrading.** One cell is a pipeline; 300 cells is a scheduling problem. Bump a chart version, render, and the diff is the change plan. Roll it with a `ring` label on cluster Secrets plus `RollingSync` steps, or with promotion PRs between rendered environments if you would rather not give up auto-sync. Either way, the property that matters is that ring 0 and ring 5 are running byte-identical manifests modulo their values files, and you can prove it with a `git diff`.

**Teardown.** Delete the cell's values file and its rendered directory; the ApplicationSet stops generating; prune removes the objects; `deletionPolicy`/`PrunePropagationPolicy` decides how aggressively. This only works if pruning is genuinely configured and rehearsed, which is why Lab 4 exists. The reliable failure is a namespace wedged on a finalizer — and note that teardown must also **stop the reconciler first**, or it will happily recreate what your sweeper deletes. Reverse-DAG order starts with "suspend the agent."

**Drift and conformance.** The reconciler gives you Kubernetes-object drift for free, as a per-Application status you can export with a cell label. It gives you nothing for cloud resources (that is nightly `terraform plan -detailed-exitcode`) and nothing for semantic conformance (that is a per-cell suite asserting DNS resolves privately, workload identity mints a token, the ingress does per-request gRPC balancing). Three layers, three mechanisms. Publishing "how many cells are non-conformant and why" as one number is a genuinely high-value first-90-days project.

**Multi-cloud.** The reconciler is the same on EKS, GKE, and AKS ([04-managed-kubernetes-eks-gke-aks.md](04-managed-kubernetes-eks-gke-aks.md)); what differs is the values files and the identity binding the agent uses to talk to git and to cloud APIs. Resist per-cloud branching in the ApplicationSet template. The cloud belongs in a cluster Secret label and a values file, exactly as [09-helm.md](09-helm.md) argues for charts.

**Where the hub becomes tier-0.** If you choose Argo CD, the hub can now stop 300 cells from converging. That means it needs the same treatment as any tier-0 service: HA manifests on ≥3 nodes, sharded controllers with per-shard staleness alerts, a tested restore path, and a rehearsed answer to "the hub is down and we need to change cell-042 right now." The answer, in a rendered-manifest world, is `kubectl apply --server-side -f rendered/cell-042/` from a laptop — which is a property worth deliberately preserving.

---

## Learning path

**Day 1 (3-5 hours).** Read the [OpenGitOps principles](https://github.com/open-gitops/documents/blob/v1.0.0/PRINCIPLES.md) — they are one page, and being able to state them precisely separates people who have used GitOps from people who understand it. Then read Argo CD's [architecture](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/architecture/), [sync waves](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-waves/), and [sync options](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-options/) pages end to end. Run Labs 1 and 2. Then find out, for your team's actual estate: which tool, which version, is pruning on, is self-heal on, is SSA on, and what generates the per-cell Applications. Write the answers down; that is your map.

**Week 1.** Run Labs 3 through 6. Read the [ApplicationSet generators](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators/) pages properly — all of them, they are short — and the [Kubernetes server-side apply doc](https://kubernetes.io/docs/reference/using-api/server-side-apply/) in full, because field ownership is the model you now live in. Read the [Akuity rendered-manifests writeup](https://akuity.io/blog/the-rendered-manifests-pattern) and the [manifest-hydrator proposal](https://github.com/argoproj/argo-cd/blob/master/docs/proposals/manifest-hydrator.md), then compare against what your team actually built and write down the differences. If your team uses Flux, substitute the [Kustomization](https://fluxcd.io/flux/components/kustomize/kustomizations/) and [HelmRelease](https://fluxcd.io/flux/components/helm/helmreleases/) specs and the [repository structure guide](https://fluxcd.io/flux/guides/repository-structure/). Finally, take one real cell and trace a change end to end: commit, render, PR, sync, converge — and time each stage.

**Month 1.** Own the fleet reconciler's operability. Concretely, in rough priority order: (a) get a per-cell drift metric and a per-shard reconcile-staleness alert onto one dashboard, because you cannot reason about 300 cells without them; (b) verify pruning actually works by deleting a resource from a test cell's rendered output and confirming it disappears — do not assume; (c) make the rendered-manifest staleness check a blocking CI gate, and make the PR diff summary reviewable at 300 cells; (d) write and *rehearse* the break-glass runbook, including the expiry rule, and put it where someone at 3 a.m. will find it; (e) audit your secrets story against the rendered-manifest constraint and write down which operator you standardize on and why; (f) form and document an opinion on progressive rollout — RollingSync versus promotion PRs — before the first fleet-wide incident forces the choice. Then read the [high availability](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/) doc or the Flux [sharding](https://fluxcd.io/flux/installation/configuration/sharding/) and [vertical scaling](https://fluxcd.io/flux/installation/configuration/vertical-scaling/) pages with your real fleet numbers in hand, and size deliberately rather than by default.

---

## References

1. [OpenGitOps Principles v1.0.0](https://github.com/open-gitops/documents/blob/v1.0.0/PRINCIPLES.md) — the four principles, verbatim and vendor-neutral. Note they never say "Git." Companion [glossary](https://github.com/open-gitops/documents/blob/v1.0.0/GLOSSARY.md); site at [opengitops.dev](https://opengitops.dev/). The [CNCF GitOps WG](https://github.com/cncf/tag-app-delivery/blob/main/gitops-wg/README.md) merged into OpenGitOps in March 2024 and its parent repo was archived 2025-09-09.
2. [Argo CD documentation](https://argo-cd.readthedocs.io/en/release-3.5/) — cite the `release-3.5` slug, not `stable`; the `stable` git branch is pinned at [3.4.5](https://raw.githubusercontent.com/argoproj/argo-cd/stable/VERSION).
3. [Argo CD releases](https://github.com/argoproj/argo-cd/releases) and [release process and cadence](https://argo-cd.readthedocs.io/en/stable/developer-guide/release-process-and-cadence/) — v3.5.2 (2026-08-27); four minors a year on the first Tuesday of Feb/May/Aug/Nov, RC1 seven weeks earlier, only the three most recent minors get patches.
4. [Argo CD v2.14 → 3.0 upgrade](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/upgrading/2.14-3.0/) — annotation tracking becomes the default, RBAC sub-resource inheritance changes, three `argocd_app_*` metrics removed. The single most useful "what changed" page.
5. [Argo CD architecture](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/architecture/) and [components](https://argo-cd.readthedocs.io/en/release-3.5/developer-guide/architecture/components/) — what each process actually does and which layer may depend on which.
6. [Argo CD declarative setup](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/declarative-setup/) — the full `Application` and `AppProject` field reference, cluster Secrets, repository Secrets.
7. [Argo CD sync waves and hooks](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-waves/) — the phase→wave→kind→name ordering, the wait-for-health rule, `ARGOCD_SYNC_WAVE_DELAY`, reverse-order pruning. The older `resource_hooks` URL is now a stub.
8. [Argo CD sync options](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync-options/) and [auto sync](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/auto_sync/) — the complete option list, `allowEmpty`, and the `automated.enabled` toggle.
9. [gitops-engine sync types](https://github.com/argoproj/gitops-engine/blob/master/pkg/sync/common/types.go) and Argo CD 3.5's [in-tree fork](https://github.com/argoproj/argo-cd/blob/release-3.5/gitops-engine/pkg/sync/common/types.go) — the authoritative constant list. Settles the `Prune=true` myth definitively.
10. [Argo CD resource tracking](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/resource_tracking/) — annotation vs label, the `tracking-id` format, the 63-character label limit, `installationID`.
11. [Argo CD health assessment](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/health/) — built-in checks, custom Lua under `resource.customizations.health.<group>_<kind>`, `useOpenLibs`, and why wildcards only work in the flat key form.
12. [Argo CD diffing](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/diffing/) and [diff strategies](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/diff-strategies/) — `ignoreDifferences`, `managedFieldsManagers`, and Server-Side Diff (Stable since v3.1.0).
13. [Argo CD cluster bootstrapping](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/cluster-bootstrapping/) — app-of-apps, and the docs' own recommendation to prefer ApplicationSet instead.
14. [ApplicationSet overview](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/) and [Generators index](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators/) — all nine generators and the universal post-`selector`.
15. [ApplicationSet Cluster generator](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Cluster/) — the exposed fields, `nameNormalized`, `values`, `flatList`, label-selector filtering, and the local-cluster exclusion. The most important single page for a cell fleet.
16. [ApplicationSet Git generator](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Git/) and [file globbing](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Git-File-Globbing/) — directories vs files, exclusion precedence, `pathParamPrefix`, and the opt-in strict globbing.
17. [ApplicationSet Matrix](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Matrix/), [Merge](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-Merge/), and [List](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Generators-List/) generators — the composition primitives and every restriction on them.
18. [ApplicationSet Go templating](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/GoTemplate/) — the syntax migration, `missingkey=error`, Sprig availability, and the string-fields-only limitation.
19. [ApplicationSet controlling resource modification](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Controlling-Resource-Modification/) — `--policy`, `applicationsSync`, `preserveResourcesOnDeletion`, `ignoreApplicationDifferences` and its MergePatch caveat.
20. [ApplicationSet Progressive Syncs](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/applicationset/Progressive-Syncs/) — Beta since v3.3.0, still flag-gated; `RollingSync`, `maxUpdate`, `deletionOrder`, and the auto-sync-disable side effect.
21. [Argo CD high availability](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/high_availability/) — sharding, processors, the monorepo section, `manifest-generate-paths`, cache warming, and the three-node HA requirement. Read this one twice.
22. [Argo CD dynamic cluster distribution](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/dynamic-cluster-distribution/) — the alpha Deployment-mode shard coordination and its heartbeat semantics.
23. [argocd-cm reference](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/argocd-cm-yaml/) and [argocd-cmd-params-cm reference](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/argocd-cmd-params-cm-yaml/) — every default in one place. The fastest way to answer "what is the default for X."
24. [Argo CD webhook configuration](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/webhook/) — providers, the JSON content-type gotcha, secret keys, payload size cap, and OCI registry webhooks.
25. [Argo CD Helm support](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/) and the [`helm ls` FAQ entry](https://argo-cd.readthedocs.io/en/stable/faq/) — the "only `helm template`, ever" statement and the Helm→Argo hook mapping table.
26. [Argo CD Source Hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/) and the [manifest-hydrator proposal](https://github.com/argoproj/argo-cd/blob/master/docs/proposals/manifest-hydrator.md) — the shipped feature (Beta since 3.5.0) and the design reasoning, including why branches for hydrated and directories for dry.
27. [The Rendered Manifests Pattern — Akuity](https://akuity.io/blog/the-rendered-manifests-pattern) — secondary, vendor blog, and still the definitive articulation. Companion [example repo](https://github.com/akuity/rendered-manifest-pattern).
28. [Argo CD secret management](https://argo-cd.readthedocs.io/en/stable/operator-manual/secret-management/) — the official option list and the explicit caution against generation-time injection, with the plaintext-in-Redis rationale.
29. [Argo CD sync windows](https://argo-cd.readthedocs.io/en/stable/user-guide/sync_windows/), [skip-reconcile](https://argo-cd.readthedocs.io/en/stable/user-guide/skip_reconcile/), and [`argocd app diff`](https://argo-cd.readthedocs.io/en/stable/user-guide/commands/argocd_app_diff/) — the break-glass surface, including the alpha status of skip-reconcile and the fact that diff ignores Secrets.
30. [Argo Rollouts](https://argo-rollouts.readthedocs.io/en/stable/) — v1.9.1 (2026-07-17). Canary steps, blue/green, `AnalysisTemplate`, and the metric and traffic provider lists. [Releases](https://github.com/argoproj/argo-rollouts/releases).
31. [Flux documentation](https://fluxcd.io/flux/) and [components](https://fluxcd.io/flux/components/) — the controller set, which are optional, which CRDs each owns. Version state lives at [Flux releases](https://fluxcd.io/flux/releases/) and [GitHub releases](https://github.com/fluxcd/flux2/releases) — v2.9.4 (2026-08-07); last three minors supported, Kubernetes N-2. The [v2.8 announcement](https://fluxcd.io/blog/2026/02/flux-v2.8.0/) is where Helm 4 landed.
32. [Flux GitRepository](https://fluxcd.io/flux/components/source/gitrepositories/) and [OCIRepository](https://fluxcd.io/flux/components/source/ocirepositories/) — required `interval`, the `master`-branch default, provider values (no `gcp` on GitRepository), sparse checkout, cosign/notation verification.
33. [Flux Kustomization](https://fluxcd.io/flux/components/kustomize/kustomizations/) — the full spec including prune, inventory, `dependsOn`, `wait`, `deletionPolicy`, `postBuild`, and the v2.9 [`spec.ignore` rules](https://fluxcd.io/blog/2026/08/ignore-rules-drift-detection/) with Strip/Adopt.
34. [Flux HelmRelease](https://fluxcd.io/flux/components/helm/helmreleases/) — real release state, install/upgrade CRD policy asymmetry, `driftDetection`, and the `UseHelm3Defaults` gate. The counterexample to templating-only Helm.
35. [Flux bootstrap](https://fluxcd.io/flux/installation/bootstrap/) and [terraform-provider-flux](https://github.com/fluxcd/terraform-provider-flux) — what bootstrap actually commits, the self-management property, and the Terraform handoff.
36. [Flux multi-tenancy lockdown](https://fluxcd.io/flux/installation/configuration/multitenancy/) and [flux2-multi-tenancy](https://github.com/fluxcd/flux2-multi-tenancy) — the three lockdown flags, ServiceAccount impersonation, and a working fleet-repo layout.
37. [Flux sharding](https://fluxcd.io/flux/installation/configuration/sharding/) and [vertical scaling](https://fluxcd.io/flux/installation/configuration/vertical-scaling/) — `sharding.fluxcd.io/key`, `--watch-label-selector`, concurrency knobs, and which controllers cannot be sharded.
38. [Flux repository structure](https://fluxcd.io/flux/guides/repository-structure/) and [flux2-kustomize-helm-example](https://github.com/fluxcd/flux2-kustomize-helm-example) — the four patterns and the thin `clusters/<name>/` convention for many clusters.
39. [Flux SOPS guide](https://fluxcd.io/flux/guides/mozilla-sops/) and [CEL health-check cheatsheet](https://fluxcd.io/flux/cheatsheets/cel-healthchecks/) — native decryption (the URL still says mozilla; the project is [getsops/sops](https://github.com/getsops/sops)) and ready-made expressions for common CRDs.
40. [Flagger](https://fluxcd.io/flagger/) — v1.44.0 (2026-07-21). The `Canary` CRD, the three strategies and how `iterations` vs `match` selects between them, `MetricTemplate`, and gating webhooks.
41. [Kubernetes Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) — field managers, `managedFields`, conflicts, `--force-conflicts`, ownership transfer. Stable since 1.22.
42. [Kubernetes finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/) and [GKE terminating-namespaces troubleshooting](https://cloud.google.com/kubernetes-engine/docs/troubleshooting/terminating-namespaces) — the official warning against manual removal, and the best public writeup of the APIService-breaks-discovery cause.
43. [SOPS](https://getsops.io/docs/), [Sealed Secrets](https://github.com/bitnami/sealed-secrets), [External Secrets Operator](https://external-secrets.io/), [Vault Secrets Operator](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/), [Secrets Store CSI Driver](https://secrets-store-csi-driver.sigs.k8s.io/) — the five options in the comparison table. HashiCorp's [VSO vs Agent Injector comparison](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/comparisons) is the clearest first-party trade-off table anywhere in this space.
44. [kubeconform](https://github.com/yannh/kubeconform), [Kyverno CLI](https://kyverno.io/docs/kyverno-cli/), and [conftest](https://www.conftest.dev/) — the CI validation stack. Kyverno graduated CNCF 2026-03-16 and is migrating from `ClusterPolicy` to CEL-based [`ValidatingPolicy`/`MutatingPolicy`](https://kyverno.io/docs/policy-types/overview/). `kubeval` and `datree` are both dead.
45. [Argo CD at scale — Akuity](https://akuity.io/blog/argo-cd-ultimate-scalability), [CNOE Argo CD application scalability](https://cnoe.io/blog/argo-cd-application-scalability), and [ArgoCon '21 abstracts](https://argoproj.github.io/argocon21/) — all secondary, all with real numbers: the 1,000-cluster/50,000-app load tests, the QPS/burst and shard-count measurements, and Intuit's 12,000 apps across 370 clusters.
