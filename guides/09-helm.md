# Helm — As a Templating Engine

**Why this matters.** This guide assumes a specific, increasingly common constraint: *Helm for templating only, with no release state.* That means you render charts to YAML and hand the YAML to something else — a GitOps controller, a pipeline, `kubectl apply`. You are using roughly 40% of Helm and deliberately discarding the rest. This is a good decision and an increasingly common one, but it has sharp edges: half of what is written about Helm assumes release state exists, third-party charts assume hooks run, and the pruning problem Helm normally solves for you is now yours. You will be reading and writing charts on day one — some yours, many not — so you need to know both the templating engine in depth *and* the release model well enough to recognize when a chart you inherited quietly depends on it.

Everything below was verified against primary sources on **2026-08-29**. Where I could not verify something, I say so.

> **Stale-content trap, read this first.** Five things most Helm material gets wrong today:
>
> 1. **Helm 4 exists.** v4.0.0 shipped **2025-11-12** at KubeCon, the first major version in six years ([Helm 4 Released](https://helm.sh/blog/helm-4-released/)). Current releases as of today: **v4.2.4 (2026-08-13)** and **v3.21.4 (2026-08-14)** ([releases](https://github.com/helm/helm/releases)).
> 2. **Helm 3 is dying on a published schedule.** One final limited feature release on **2026-09-09**, security fixes originally scheduled through **November 2026, extended to February 10, 2027** ([Helm 3 End of Life](https://helm.sh/blog/helm-v3-end-of-life/)). Pin your CI to a Helm 4 binary now.
> 3. **There is no `helm render` command.** The Helm 4 overview page contains a typo that says `helm render --post-renderer`; the actual command list has no `render`, and `pkg/cmd/template.go` declares `Use: "template [NAME] [CHART]"` with no alias. It is still `helm template`.
> 4. **Helm 4 does server-side apply** — SSA is the default for *new* installs, while upgrades latch to whatever the release used before ([HIP-0023](https://helm.sh/community/hips/hip-0023)). This matters even in templating-only mode because it changes what the ecosystem assumes about field ownership.
> 5. **`kubectl apply --prune` is still alpha, and has been for a decade.** The Kubernetes docs say it plainly: *"This mode has existed since kubectl v1.5 but is still in alpha due to usability, correctness and performance issues with its design"* ([declarative config](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/#alternative-kubectl-apply-f-directory-prune)). Do not build your garbage collection on it.
>
> Several core helm.sh topic pages (`topics/charts`, `topics/registries`) still carry a banner reading *"This page has not yet been updated for Helm 4."* Read them with that in mind.

---

## The mental model

Hold five ideas.

**1. Helm is two products wearing one binary.** Product one is a *templating engine*: Go templates plus Sprig plus a values-merging algorithm, which turns a directory of files into a stream of Kubernetes manifests. Product two is a *release manager*: it applies that stream, records the result in a Secret in the cluster, and can diff, upgrade, roll back, and delete based on that record. This library assumes product one. Almost every Helm tutorial is about product two.

**2. `helm template` is a pure function.** Given a chart and a set of values, it emits YAML. It touches no cluster, keeps no record, and makes no decisions. Everything in Helm that requires knowing the current state of the world — `lookup`, `.Release.IsUpgrade`, hooks, three-way merge, `--wait`, rollback — either silently degrades or does nothing. Knowing *exactly which* features degrade, and how silently, is the practical core of working this way.

**3. The rendered manifest is the artifact.** In templating-only mode, the interesting object is not the chart and not the release — it is the YAML. It gets committed, diffed in a PR, scanned by policy, and applied by something else. The whole workflow reorganizes around making that artifact deterministic and reviewable. Akuity's framing is the best one-liner on why: *"Git contains the inputs to the desired state, not the desired state itself"* ([The Rendered Manifests Pattern](https://akuity.io/blog/the-rendered-manifests-pattern)).

**4. Without release state, nothing knows what to delete.** Helm's release record is also its inventory: on upgrade it diffs the old manifest against the new one and deletes what disappeared. Throw away the record and you have thrown away the inventory. Something else must own it — Argo CD's tracking annotation, Flux's `.status.inventory`, or ApplySet. This is the single largest thing you give up, and it is the one people discover in production.

**5. Field ownership replaces release ownership.** Server-side apply moves the question "who owns this field?" from Helm's release record into the API server's `managedFields`. In a templating-only world where multiple actors touch the same objects — your pipeline, a controller, an operator, a human with `kubectl edit` — SSA field management *is* your conflict model. Learn it.

```text
  ┌────────── product one: templating (what you use) ──────────┐
  │  Chart.yaml + values.yaml + templates/  ─┐                  │
  │  -f overrides, --set                    ─┤                  │
  │  --api-versions, --kube-version         ─┼─► helm template  │
  │  (no cluster contact)                    ┘        │         │
  └───────────────────────────────────────────────────┼─────────┘
                                                      ▼
                                              rendered YAML
                                                      │
      ┌───────────────────────────────────────────────┼──────────────┐
      │  Git commit ──► Argo CD / Flux ──► kubectl apply --server-side│
      │  (inventory, prune, health, waves live HERE now)             │
      └──────────────────────────────────────────────────────────────┘

  ┌────── product two: release management (what you gave up) ──────┐
  │  helm install/upgrade → Secret sh.helm.release.v1.<name>.vN    │
  │  → hooks, rollback, history, three-way merge, --wait, lookup   │
  └────────────────────────────────────────────────────────────────┘
```

---

## Core concepts

### Chart anatomy

```text
temporal-cell/
├── Chart.yaml            # name, version, apiVersion: v2, type, dependencies
├── Chart.lock            # resolved dependency digests (helm dependency update)
├── values.yaml           # defaults; the documented interface of the chart
├── values.schema.json    # JSON Schema; enforced on install/upgrade/lint/template
├── .helmignore           # excluded from `helm package`
├── charts/               # vendored subcharts (.tgz or directories)
├── crds/                 # plain YAML CRDs; special install rules, see below
└── templates/
    ├── NOTES.txt         # rendered post-install message — invisible to `helm template`
    ├── _helpers.tpl      # underscore prefix = partials, not manifests
    ├── deployment.yaml
    ├── service.yaml
    └── tests/
        └── connection.yaml   # helm.sh/hook: test
```

`Chart.yaml` essentials ([charts](https://helm.sh/docs/topics/charts/)):

```yaml
apiVersion: v2            # v2 = Helm 3+. v1 charts still render but are legacy.
name: temporal-cell
version: 4.2.0            # SemVer; the chart's own version
appVersion: "1.29.0"      # the app version; quoted, not used for resolution
type: application         # or `library` — library charts are not installable
kubeVersion: ">= 1.31.0-0"
dependencies:
  - name: temporal-server
    version: "~2.1.0"
    repository: "oci://registry.internal/temporal/charts"
    condition: temporalServer.enabled
    alias: server
```

Since Helm 3.3.2, unknown `Chart.yaml` fields are rejected — put custom metadata under `annotations`.

**Files starting with `_` are not manifests.** Verbatim: *"files whose name begins with an underscore (`_`) are assumed to not have a manifest inside. These files are not rendered to Kubernetes object definitions, but are available everywhere within other chart templates for use"* ([named templates](https://helm.sh/docs/chart_template_guide/named_templates/)). Note also that **template names are global across the chart and all subcharts** — always prefix with the chart name (`temporal-cell.labels`, not `labels`) or a subchart will silently shadow yours.

**`crds/` has rules that surprise everyone.** From [limitations on CRDs](https://helm.sh/docs/topics/charts/#limitations-on-crds):

> *"CRDs are never reinstalled… CRDs are never installed on upgrade or rollback. Helm will only create CRDs on installation operations. **CRDs are never deleted.** Deleting a CRD automatically deletes all of the CRD's contents across all namespaces in the cluster. Consequently, Helm will not delete CRDs."*
> *"CRD files cannot be templated. They must be plain YAML documents."*

In templating-only mode this whole mechanism is bypassed: `helm template` does **not** emit `crds/` unless you pass `--include-crds`. That is arguably an improvement — your GitOps tool now manages CRDs like any other resource, and *can* upgrade them — but you must remember the flag or your CRDs simply will not exist.

**`.helmignore`** is applied at `helm package` time, uses Go's `filepath.Match` (not `fnmatch`), and **does not support `**`**. Its docs page is internally inconsistent about whether `!` negation works — the differences list says *"There is no support for `!` as a special leading sequence."* Do not rely on it ([helmignore](https://helm.sh/docs/chart_template_guide/helm_ignore_file/)).

**`NOTES.txt` is invisible to you.** `helm template` never emits notes — Helm 4 deprecated `--hide-notes` and `--render-subchart-notes` on `helm template` precisely because *"These flags have no effect because template output never includes notes"* ([Helm 4 overview](https://helm.sh/docs/overview/)). If a chart puts important post-install instructions there, your users will never see them. Move anything load-bearing into a ConfigMap or your own docs.

### Go templates and Sprig, in the depth you need

Helm's template language is Go `text/template` plus the [Sprig](https://masterminds.github.io/sprig/) function library plus about a dozen Helm-specific functions ([function list](https://helm.sh/docs/chart_template_guide/function_list/)).

#### Actions, pipelines, and scoping

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "temporal-cell.fullname" . }}
  labels:
    {{- include "temporal-cell.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount | default 2 }}
  template:
    spec:
      containers:
        - name: frontend
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          {{- with .Values.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          env:
            {{- range $k, $v := .Values.env }}
            - name: {{ $k | quote }}
              value: {{ $v | quote }}
            {{- end }}
```

**Scoping is where people get lost.** `with` and `range` *rebind the dot*. Inside `{{- with .Values.resources }}`, `.` is the resources object, and `.Values` no longer resolves. Two escapes:

- **`$` is the root context**, always. `{{ $.Values.global.region }}` works anywhere, at any nesting depth.
- **Capture what you need before entering the block**: `{{- $fullName := include "temporal-cell.fullname" . -}}` at the top of the file.

Inside `range` with two variables, `$k` and `$v` are the key and value while `.` is *also* rebound to the value. Using both forms in one block is a readability trap; pick the named variables.

#### Whitespace control — `{{-` and `-}}`

`{{-` chomps all whitespace (including newlines) to the *left*; `-}}` chomps to the right. The rule that keeps YAML valid: **use `{{-` on control-flow lines so the action's own line disappears, and never use `-}}` before content you want on its own line.**

```yaml
# WRONG: leaves a blank line where the `if` was
metadata:
  annotations:
    {{ if .Values.annotations }}
    foo: bar
    {{ end }}

# RIGHT
metadata:
  annotations:
    {{- if .Values.annotations }}
    foo: bar
    {{- end }}
```

#### `include` vs `template` — always use `include`

This is not style. Verbatim from the docs: *"Because `template` is an action, and not a function, there is no way to pass the output of a `template` call to other functions; the data is simply inserted inline."* And: *"It is considered preferable to use `include` over `template`"* ([named templates](https://helm.sh/docs/chart_template_guide/named_templates/#the-include-function)).

Since you cannot pipe `template` output, you cannot indent it, which means `template` is unusable anywhere except column 0. Use `include` everywhere.

#### The `toYaml | nindent` bug — the single most common defect in charts

`toYaml` emits multi-line YAML starting at column 0 with **no leading newline**. `indent n` pads every line *including the first* — but the first line has already been glued to the preceding key because `{{-` ate the newline. `nindent n` prepends a newline and *then* indents, which is what you almost always want.

```yaml
# BROKEN — produces `resources:  limits:` on one line, then wrong indentation
resources:
  {{- toYaml .Values.resources | indent 2 }}

# CORRECT
resources:
  {{- toYaml .Values.resources | nindent 2 }}
```

The docs describe the intended pattern directly: *"Using the `{{- ... | nindent n }}` pattern makes it easier to read the `include` in context, because it chomps the whitespace to the left (including the previous newline), then the `nindent` re-adds the newline and indents the included content by the requested amount"* ([tips and tricks](https://helm.sh/docs/howto/charts_tips_and_tricks/)). Helm 4's own `helm create` scaffold uses `nindent` **exclusively** — `indent` appears zero times in [`pkg/chart/v2/util/create.go`](https://github.com/helm/helm/blob/main/pkg/chart/v2/util/create.go). Treat a bare `| indent` in a chart as a code smell worth checking.

The related trap: `{{- toYaml .Values.x | nindent 2 }}` when `.Values.x` is empty renders `resources:` followed by nothing, which YAML parses as `null`. Wrap in `{{- with }}` so the key disappears entirely when the value is absent — exactly what the Helm 4 scaffold does.

#### `tpl`, `required`, `default`

```yaml
# tpl renders a string from values as a template. Enables user-supplied
# templating — powerful, and a rendering-error surface you do not control.
annotations:
  external-dns.alpha.kubernetes.io/hostname: {{ tpl .Values.hostnameTemplate . }}

# required fails the render with your message. The correct way to make a
# value mandatory — far better than a runtime crash in a Pod.
image: {{ required "cell.image.repository is required" .Values.image.repository }}

# default supplies a fallback. Note: it treats "", 0, and false as empty,
# so `default 5 .Values.replicas` returns 5 when replicas is explicitly 0.
replicas: {{ .Values.replicas | default 3 }}
```

That `default` behavior is a real bug source — to allow an explicit `0` or `false`, test with `if hasKey` or use the `values.schema.json` default instead.

#### `lookup` — dead on arrival in templating-only mode

```yaml
{{- $existing := lookup "v1" "Secret" .Release.Namespace "cell-token" }}
{{- if $existing }}
token: {{ index $existing.data "token" }}
{{- else }}
token: {{ randAlphaNum 32 | b64enc }}
{{- end }}
```

Verbatim: *"`lookup` is used to look up resource in a running cluster. **When used with the `helm template` command it always returns an empty response**"* ([function list](https://helm.sh/docs/chart_template_guide/function_list/)). So the branch above always takes the `else` path and regenerates the secret on every render — which, committed to Git, means a new secret in every commit and a rolling restart every sync.

This is the most dangerous degradation in templating-only mode because it fails *silently and plausibly*. Audit every inherited chart for `lookup`. The chart-level fix is to stop generating secrets in templates; use an External Secrets Operator, a Vault sidecar, or a pre-created Secret referenced by name.

#### `Capabilities` and `.Release` — what they say without a cluster

`helm template` has no cluster, so it fabricates these. Two flags let you control the fabrication ([helm template](https://helm.sh/docs/helm/helm_template/)):

| Flag | Effect |
|---|---|
| `-a, --api-versions` | *"Kubernetes api versions used for Capabilities.APIVersions"* — repeatable or comma-separated |
| `--kube-version` | *"Kubernetes version used for Capabilities.KubeVersion"* |
| `--is-upgrade` | *"set .Release.IsUpgrade instead of .Release.IsInstall"* |

Without `--api-versions`, `.Capabilities.APIVersions.Has "monitoring.coreos.com/v1"` is **false**, so charts that conditionally emit a ServiceMonitor emit nothing. This is a silent-omission failure and it is extremely common with observability charts. Your render command must pass the real API surface of the target cluster:

```bash
helm template cell ./charts/temporal-cell \
  --kube-version "1.34.0" \
  --api-versions "monitoring.coreos.com/v1" \
  --api-versions "cert-manager.io/v1" \
  --api-versions "gateway.networking.k8s.io/v1"
```

For `.Release`: under plain `helm template`, **`.Release.IsInstall` is `true` and `.Release.IsUpgrade` is `false`**, and the release name is the hardcoded string `"release-name"` unless you pass one. Any chart logic branching on `IsUpgrade` — schema migration Jobs, one-time bootstrap — is permanently stuck on the install path.

### Values

#### Precedence

The documented order, verbatim: *"`values.yaml` is the default, which can be overridden by a parent chart's `values.yaml`, which can in turn be overridden by a user-supplied values file, which can in turn be overridden by `--set` parameters"* ([values files](https://helm.sh/docs/chart_template_guide/values_files/)). Within each family, last wins: *"You can specify the `--values`/`-f` flag multiple times. The priority will be given to the last (right-most) file specified."*

```text
chart values.yaml
  └► subchart values overridden by parent values.yaml
      └► -f file1.yaml
          └► -f file2.yaml            (later files win)
              └► --set / --set-string / --set-json / --set-file / --set-literal
```

The `--set*` family: `--set-string` forces string typing, `--set-file` reads a value from a file, `--set-json` (Helm 3.10+) takes raw JSON, `--set-literal` (3.12+) disables all escaping. Their relative merge order among themselves is real but undocumented — it lives in `MergeValues` in `pkg/cli/values/options.go`. Do not depend on it.

**Opinion for a platform team: ban `--set` from your render pipeline.** It is unreviewable, it silently coerces types (`--set version=1.10` becomes the number `1.1`), and it makes the render non-reproducible from Git alone. Use layered `-f` files — `values.yaml`, `values-<cloud>.yaml`, `values-<cell>.yaml` — all committed. `--set` is for humans debugging at a terminal.

Helm 4 adds **multi-document values**: *"Split complex values across multiple YAML files"* ([Helm 4 overview](https://helm.sh/docs/overview/)), where documents within a file merge in order. Useful for keeping per-cell overlays in one reviewable file.

#### `values.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["cell", "image"],
  "properties": {
    "cell": {
      "type": "object",
      "required": ["name", "region"],
      "properties": {
        "name":   { "type": "string", "pattern": "^[a-z0-9-]{3,32}$" },
        "region": { "type": "string" },
        "tier":   { "type": "string", "enum": ["standard", "dedicated", "isolated"], "default": "standard" }
      },
      "additionalProperties": false
    },
    "replicaCount": { "type": "integer", "minimum": 1, "maximum": 100 }
  }
}
```

Verbatim on enforcement: *"Validation occurs when any of the following commands are invoked: `helm install`, `helm upgrade`, `helm lint`, `helm template`"* — so it works in templating-only mode, which makes it your single best guardrail. Also: *"the final `.Values` object is checked against all subchart schemas."* Disable with `--skip-schema-validation` ([schema files](https://helm.sh/docs/topics/charts/#schema-files)).

Supported drafts are 4, 6, 7, 2019-09, and 2020-12 — Helm swapped to the `santhosh-tekuri/jsonschema/v6` validator in **3.18.5**, replacing an older library capped at draft-07. The docs only *show* draft-07 in their example and never state the list, so "Helm only supports draft-07" is a widely repeated but now-false claim. Set `$schema` explicitly.

`additionalProperties: false` is the highest-value line in the file: it turns a typo'd value key from a silent no-op into a render failure. That single change catches more real incidents than any linter.

#### Subcharts and `global`

The four rules ([subcharts and globals](https://helm.sh/docs/chart_template_guide/subcharts_and_globals/)):

- A subchart cannot access its parent's values.
- A parent can override a subchart's values, by nesting under the subchart name.
- `global` values are visible to all charts.
- *"Globals require explicit declaration. You can't use an existing non-global as if it were a global."*

And the direction rule: *"If a subchart declares a global variable, that global will be passed downward… but not upward to the parent chart… global variables of parent charts take precedence over the global variables from subcharts"* ([charts](https://helm.sh/docs/topics/charts/#global-values)).

```yaml
# parent values.yaml
global:
  cellName: cell-042
  imageRegistry: registry.internal

temporal-server:          # keyed by the subchart's name (or its `alias`)
  replicaCount: 5
  resources:
    limits: { cpu: "2" }
```

`import-values` in a dependency lets a parent pull a child's values upward, in two forms — `exports` (implicit; the parent key is not retained) and explicit `child`/`parent` paths ([importing child values](https://helm.sh/docs/topics/charts/#importing-child-values-via-dependencies)). It is powerful and it makes charts hard to reason about; prefer explicit wiring.

### Library charts and dependencies

A `type: library` chart contains only partials — `helm install` on one fails with `Error: library charts are not installable`. Two quirks most guides miss ([library charts](https://helm.sh/docs/topics/library_charts/)): *"The `.Files` object references the file paths on the parent chart"* and *"The `.Values` object is the same as the parent chart, in contrast to application [subcharts]."*

For a cell platform this is the right factoring: one library chart owning labels, annotations, naming, security contexts, and topology-spread defaults; many thin application charts consuming it.

```yaml
# library chart: templates/_container.tpl
{{- define "temporal-lib.securityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 65532
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}
```

```yaml
# application chart Chart.yaml
dependencies:
  - name: temporal-lib
    version: "1.4.0"
    repository: "oci://registry.internal/temporal/charts"
```

**OCI registries** are now the recommended distribution mechanism: *"It is recommended to use container registries with OCI support to store and share chart packages"* ([registries](https://helm.sh/docs/topics/registries/)). Classic `index.yaml` repos are **not deprecated** — the strongest wording anywhere is a soft steer: *"If you are considering creating a chart repository, you may want to consider using an OCI registry instead"* ([chart repository](https://helm.sh/docs/topics/chart_repository/)).

```bash
helm package ./charts/temporal-cell            # produces temporal-cell-4.2.0.tgz
helm push temporal-cell-4.2.0.tgz oci://registry.internal/temporal/charts
helm pull oci://registry.internal/temporal/charts/temporal-cell --version 4.2.0
```

Note `helm push` accepts only `.tgz`, and the OCI reference *"must not contain the basename or tag."* Helm 4 hardened digest support: *"Install charts by digest… Charts with non-matching digests are not installed"* — which is what you want for a reproducible pipeline. Also useful: `helm package` honors `SOURCE_DATE_EPOCH` for byte-reproducible archives ([tips and tricks](https://helm.sh/docs/howto/charts_tips_and_tricks/)).

Helm 4 registry login is now **domain-scoped only** — no scheme, no path. This breaks CI scripts that passed a full URL.

### The rendered-manifest / templating-only pattern

#### What it is

Render the chart, commit the YAML, let a controller apply it:

```bash
helm template cell-042 ./charts/temporal-cell \
  --namespace temporal-system \
  --kube-version 1.34.0 \
  --api-versions monitoring.coreos.com/v1 \
  -f values/base.yaml \
  -f values/aws.yaml \
  -f values/cells/cell-042.yaml \
  --include-crds \
  > rendered/aws/us-east-1/cell-042/manifests.yaml
```

The pattern was named and popularized by Akuity — first published in 2023 by Nicholas Morey, revised through July 2026 ([The Rendered Manifests Pattern](https://akuity.io/blog/the-rendered-manifests-pattern), [companion repo](https://github.com/akuity/rendered-manifest-pattern)). Its sharpest argument: *"Running Helm or Kustomize at sync time is the equivalent of running `apt-get install` inside a container's startup script."* Argo CD has since built it in as the [Source Hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/), *"Beta Feature (Since v3.5.0)"*, designed in the [manifest-hydrator proposal](https://github.com/argoproj/argo-cd/blob/master/docs/proposals/manifest-hydrator.md).

#### Why teams do it

- **The diff is the real diff.** A PR shows "this Deployment's memory limit goes from 4Gi to 8Gi," not "this values key changed and, three template layers down, something happens." At 300 cells this is the difference between reviewable and not.
- **Rendering happens once, not per sync.** Chart bugs surface in CI, not at 3 a.m. during a controller resync.
- **You get an auditable artifact.** What was deployed to cell-042 on 2026-08-14 is a Git object, not a reconstruction.
- **Policy and scanning get something concrete.** Conftest, Kyverno CLI, and kubeconform all run naturally over YAML.
- **It removes a runtime dependency.** The controller doesn't need Helm, chart repo credentials, or network access to a registry at sync time.
- **It sidesteps the whole release-state failure class** — no `another operation is in progress`, no `--atomic` rollback surprises, no drift between Helm's record and reality.

Argo CD does *not* use release state even when you point it at a chart. Verbatim: *"Helm is only used to inflate charts with `helm template`. The lifecycle of the application is handled by Argo CD instead of Helm"* ([Argo CD Helm docs](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/)), and *"It runs `helm template` and then deploys the resulting manifests… This means that you cannot use any Helm command to view/verify the application"* ([Argo CD FAQ](https://argo-cd.readthedocs.io/en/stable/faq/#after-deploying-my-helm-application-with-argo-cd-i-cannot-see-it-with-helm-ls-and-other-helm-commands)). So if you use Argo CD at all, you are already in templating-only mode — the rendered-manifest pattern only moves the render from sync time to CI time. As of Argo CD 3.5, *"The only Helm binary used to render charts in Argo CD is v4."*

Flux is the counterexample worth knowing: its `HelmRelease` controller performs *"reconciliation of Helm releases via Helm actions such as install, upgrade, test, uninstall, and rollback"* ([Flux HelmRelease](https://fluxcd.io/flux/components/helm/helmreleases/)) — real release state, real hooks (disabled via `.disableHooks`), real storage Secrets. Flux `Kustomization` is the templating-only path. Flux has shipped Helm 4 since [v2.8.0 (2026-02-24)](https://fluxcd.io/blog/2026/02/flux-v2.8.0/), with SSA and kstatus health checking as new defaults and a `UseHelm3Defaults` feature gate to revert.

#### What you lose, concretely

| Capability | Status under `helm template` | Mitigation |
|---|---|---|
| Hooks (`pre-install`, `post-upgrade`, …) | Manifests are **rendered into the output** but never executed | Argo CD sync waves/hooks; a real Job in the pipeline; Flux dependency ordering |
| `helm.sh/hook-delete-policy` | Meaningless — nothing manages hook lifecycle | `ttlSecondsAfterFinished` on the Job; Argo CD `hook-delete-policy` |
| `helm rollback` / `helm history` | Gone | `git revert` the rendered manifests. Arguably better: it is auditable |
| `helm list` / `helm status` | Gone | Argo CD app list, Flux `Kustomization` status |
| `lookup` | Always returns empty | Do not template cluster-dependent values |
| `.Release.IsUpgrade` | Always false (unless `--is-upgrade`) | Remove the branch, or pass the flag from a pipeline that knows |
| `Capabilities.APIVersions` | Empty unless `--api-versions` passed | Enumerate the target cluster's API surface in your render command |
| Three-way merge | N/A — Helm is not applying | Server-side apply, which is strictly better |
| `--wait`, `--timeout`, kstatus | Gone | Argo CD health assessment; Flux health checks; kstatus in your pipeline |
| Pruning of removed resources | **Nothing deletes anything** | See the next section — this is the big one |
| CRD auto-install from `crds/` | Not rendered without `--include-crds` | Always pass `--include-crds`, or manage CRDs separately |
| `NOTES.txt` | Never emitted | Move anything load-bearing elsewhere |

**Hooks deserve a precise statement**, because the docs never make one. `helm template` **renders hook manifests into stdout by default but never executes them**. `--no-hooks` *omits them from the output*; `--skip-tests` exists specifically because test hooks are included by default. This is deliberate design ([helm/helm#6443](https://github.com/helm/helm/issues/6443)), and verifiable in [`pkg/cmd/template.go`](https://github.com/helm/helm/blob/main/pkg/cmd/template.go), which appends hooks to the output buffer and has no cluster write path.

The practical consequence: a third-party chart's `pre-install` migration Job lands in your rendered output as an ordinary Job. Your GitOps tool applies it — possibly *concurrently* with the Deployment it was supposed to precede, and possibly *again* on every sync, since it is now just a resource. That is how a database migration runs twice.

Argo CD's mitigation is a documented mapping ([Argo CD Helm hooks](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/#helm-hooks)):

| Helm | Argo CD |
|---|---|
| `pre-install`, `pre-upgrade` | `PreSync` |
| `post-install`, `post-upgrade` | `PostSync` |
| `pre-delete` / `post-delete` | `PreDelete` / `PostDelete` |
| `helm.sh/hook-weight` | `argocd.argoproj.io/sync-wave` |
| `helm.sh/resource-policy: keep` | `argocd.argoproj.io/sync-options: Delete=false` |
| `pre-rollback`, `post-rollback`, `test-success`, `test-failure` | **Not supported** |

Two critical notes on that page: *"This is annotation compatibility, not a guarantee that hook lifecycle semantics are identical to Helm's"*, and — the one that bites — ***"If you define any Argo CD hooks, all Helm hooks will be ignored."*** Mixing the two annotation families in one application silently disables one of them.

**One more thing you lose, and it reaches beyond Helm: secrets in the render path.** If the rendered output is committed to git, then anything that resolves a secret *during* rendering writes plaintext into a commit. Argo CD's Source Hydrator docs state it without hedging: *"Do not use the source hydrator with any tool that injects secrets into your manifests as part of the hydration process (for example, Helm with SOPS or the Argo CD Vault Plugin). These secrets would be committed to git"* ([source hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/)). So the templating-only choice is not secrets-neutral: it structurally forces a *runtime* secrets operator — VSO, ESO, or the CSI driver — because the only thing your chart may render is a *pointer* to a secret, never the secret. Budget for that operator when you cost out this pattern. See [13-gitops-argocd-flux.md](13-gitops-argocd-flux.md#secrets-in-gitops) for the comparison and [10-vault.md](10-vault.md#kubernetes-integration--four-options) for the Vault-side options.

### Pruning and garbage collection

Delete a template from a chart and re-render: the object is simply absent from the output. Nothing tells the cluster to remove it. Your options, in descending order of quality:

| Mechanism | How it tracks | Verdict |
|---|---|---|
| **Argo CD prune** | `argocd.argoproj.io/tracking-id` annotation on every managed object | Best. Mature, per-app, with `Prune=confirm` for dangerous deletes |
| **Flux `Kustomization` prune** | Server-side inventory in `.status.inventory` | Excellent. Nothing written on the managed objects themselves |
| **ApplySet** (`kubectl apply --prune --applyset`) | `applyset.kubernetes.io/part-of` label + a parent Secret | **Still alpha**, unchanged since v1.27 |
| **`kubectl apply --prune -l`** (allowlist) | Label selector | Alpha since v1.5. Do not use |

Argo CD: enable with `spec.syncPolicy.automated.prune: true` ([auto sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/)), or `argocd app sync --prune`. Per-resource sync options are `Prune=false` and `Prune=confirm` — there is **no `Prune=true`** sync option, a very commonly copy-pasted mistake; the authoritative constant list is in [gitops-engine](https://github.com/argoproj/gitops-engine/blob/master/pkg/sync/common/types.go). Also available: `PruneLast=true` and `PrunePropagationPolicy=background|foreground|orphan` ([sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/)).

Resource tracking defaults changed: Argo CD 3.0 flipped the default from `label` to `annotation` ([3.0 upgrade notes](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/2.14-3.0/#use-annotation-based-tracking-by-default)). The old label method truncates at 63 characters and collides with other tools that write `app.kubernetes.io/instance`. If you inherit a cluster on label tracking, migrating is worth doing.

Flux: `.spec.prune` is required, and *"objects that were previously applied on the cluster but are missing from the current source revision, are removed"* ([prune](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune)). The inventory is *"the list of Kubernetes resource object references that have been successfully applied… recorded in `.status.inventory`"* ([inventory](https://fluxcd.io/flux/components/kustomize/kustomizations/#inventory)). Opt individual objects out with `kustomize.toolkit.fluxcd.io/prune: disabled`.

ApplySet is the eventual Kubernetes-native answer but is **not ready**: [KEP-3659](https://github.com/kubernetes/enhancements/blob/master/keps/sig-cli/3659-kubectl-apply-prune/kep.yaml) still reads `stage: alpha`, `latest-milestone: v1.27`, `beta: TBD`, and it is gated behind the env var `KUBECTL_APPLYSET=true`. Current Kubernetes is [v1.37 (2026-08-26)](https://kubernetes.io/releases/1.37/), so that is ten releases without graduating. Plan around a GitOps controller instead.

*See also: [Argo CD resource tracking, prune, and deletion](13-gitops-argocd-flux.md#argo-cd-resource-tracking-prune-and-deletion) and [Flux sources, Kustomization, and the inventory](13-gitops-argocd-flux.md#flux-sources-kustomization-and-the-inventory) for how each controller actually stores the inventory you are outsourcing pruning to — including the finalizer traps that make "prune" not mean "gone."*

### Server-side apply and field management

Once Helm is not applying, *your applier* owns fields — and so does everything else that touches those objects. SSA is how Kubernetes arbitrates that, and it has been GA since v1.22 ([SSA GA blog](https://kubernetes.io/blog/2021/08/06/server-side-apply-ga/)).

The model: each writer sends its full desired object with a `fieldManager` name. The API server records which manager owns which fields in `metadata.managedFields`. If you try to set a field another manager owns to a different value, you get a **conflict** — a 409 listing the fields and their owners.

```bash
kubectl apply --server-side --field-manager=temporal-cell-pipeline \
  -f rendered/aws/us-east-1/cell-042/manifests.yaml

# On conflict, either fix the other writer or take ownership deliberately:
kubectl apply --server-side --force-conflicts --field-manager=temporal-cell-pipeline -f ...
```

The docs are direct about when forcing is right: *"the applier should set the `force` query parameter to true (for `kubectl apply`, you use the `--force-conflicts` command line parameter)… This forces the operation to succeed… and removes the field from all other managers' entries in `managedFields`"*, and *"It is strongly recommended for controllers to always force conflicts on objects that they own and manage"* ([server-side apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/)).

For a GitOps pipeline that is the sole intended owner of rendered manifests, forcing conflicts is correct: your Git repo is the source of truth, and a conflict means someone edited out of band. But **choose a stable `--field-manager` name and never change it**, or every rename orphans a set of fields and you accumulate ghost managers.

Argo CD exposes this as `ServerSideApply=true`, which *"runs `kubectl apply --server-side --force-conflicts`"*. Two related options: `Replace=true` uses `kubectl replace`/`create` and **takes precedence over `ServerSideApply=true`**; `RespectIgnoreDifferences=true` applies your `ignoreDifferences` rules at sync time rather than only in the diff. Argo CD also ships `ClientSideApplyMigration=true` **on by default**, which migrates `managedFields` from `kubectl-client-side-apply` to `argocd-controller` ([sync options — server-side apply](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/#server-side-apply)).

One more concrete reason to use SSA: client-side apply stores the previous object in the `kubectl.kubernetes.io/last-applied-configuration` annotation, which is subject to the 262144-byte annotation limit. Argo CD's docs call this out — large CRDs simply cannot be applied client-side, and *"server-side apply can be used to avoid this issue as the annotation is not used in this case."*

Helm 4 also does SSA now: default for new installs, with upgrades **latching** to whatever the release used before, so every Helm 3-created release stays on client-side apply until explicitly opted in with `--server-side`. Field manager name is `"helm"` ([HIP-0023](https://helm.sh/community/hips/hip-0023)). Even in templating-only mode this matters: if any part of your estate still uses `helm upgrade`, you now have two managers with different apply semantics touching similar objects.

*See also: [server-side apply, field managers, and pruning](13-gitops-argocd-flux.md#server-side-apply-field-managers-and-pruning) for the field-manager names each GitOps controller actually writes, and the Argo `ignoreDifferences[].managedFieldsManagers` / Flux `spec.ignore` escape hatches for coexisting with another writer.*

### Chart testing and linting — the workflow you will build

This is the CI pipeline worth building on day one.

```bash
# 1. Structural lint. Also validates values.schema.json.
helm lint ./charts/temporal-cell --strict \
  -f values/base.yaml -f values/cells/cell-042.yaml

# 2. Unit tests over rendered output.
helm unittest ./charts/temporal-cell

# 3. Render deterministically, with the real cluster API surface.
helm template cell-042 ./charts/temporal-cell \
  --namespace temporal-system --kube-version 1.34.0 \
  --api-versions monitoring.coreos.com/v1 \
  -f values/base.yaml -f values/cells/cell-042.yaml \
  --include-crds > /tmp/rendered.yaml

# 4. Schema-validate against real Kubernetes OpenAPI + CRD schemas.
kubeconform -strict -summary -kubernetes-version 1.34.0 \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  /tmp/rendered.yaml

# 5. Policy over the rendered manifests.
conftest test --policy policy/ /tmp/rendered.yaml

# 6. Snapshot diff: the review artifact.
diff -u rendered/aws/us-east-1/cell-042/manifests.yaml /tmp/rendered.yaml
```

`helm lint` checks chart structure and values schema. It does **not** validate against Kubernetes OpenAPI schemas — that is step 4's job. Helm 4 adds a `crds` lint rule and moved `pkg/lint` to `pkg/chart/v2/lint`.

[`helm unittest`](https://github.com/helm-unittest/helm-unittest) (v1.1.2, 2026-07-24) is a plugin that asserts on rendered output without a cluster. It works with Helm 4 as of v1.1.0. Note Helm 4 verifies plugin provenance by default, so git-URL installs need `--verify=false`.

```yaml
# charts/temporal-cell/tests/deployment_test.yaml
suite: deployment
templates:
  - deployment.yaml
tests:
  - it: renders resource limits with correct indentation
    set:
      resources.limits.memory: 8Gi
    asserts:
      - equal:
          path: spec.template.spec.containers[0].resources.limits.memory
          value: 8Gi

  - it: omits the resources key entirely when unset
    set:
      resources: null
    asserts:
      - notExists:
          path: spec.template.spec.containers[0].resources

  - it: emits a ServiceMonitor only when the API is available
    capabilities:
      apiVersions: ["monitoring.coreos.com/v1"]
    templates:
      - servicemonitor.yaml
    asserts:
      - hasDocuments: { count: 1 }
```

That third test is the one that earns its keep in templating-only mode — it pins the `--api-versions` behavior so a silent omission becomes a failing test.

**The snapshot diff is the highest-value CI step you will build.** Commit rendered output per cell; in a PR, re-render and fail if the committed output differs from the freshly rendered output. Reviewers then see the Kubernetes diff, not the values diff. Concretely: a `make render` target regenerates everything, CI runs it and `git diff --exit-code`, and a PR that changes a chart *must* include the regenerated manifests. This gives you exactly the review property that makes the rendered-manifest pattern worth the trouble.

Tooling status worth knowing:

| Tool | Version | Helm 4? |
|---|---|---|
| [`helm unittest`](https://github.com/helm-unittest/helm-unittest) | v1.1.2 (2026-07-24) | Yes |
| [`chart-testing` (ct)](https://github.com/helm/chart-testing) | v3.14.0 (Oct 2025) | **No** — still vendors `helm.sh/helm/v3` |
| [`kubeconform`](https://github.com/yannh/kubeconform) | v0.8.0 | N/A |
| `kubeval` | — | **Unmaintained**; its README says *"a good replacement is kubeconform"* |
| [`helm diff`](https://github.com/databus23/helm-diff) | v3.15.8 (2026-06-06) | Yes; uses `--dry-run=server` on Helm 4 |

`ct` not supporting Helm 4 is a real gap; if your CI depends on it, either pin a Helm 3 binary for that step or build the equivalent out of `helm lint` + `helm unittest` + `kubeconform`, which is what I would do anyway since it is faster and the failure messages are better.

### Alternatives, honestly compared

| Tool | Model | Strength | Real cost | When I would pick it |
|---|---|---|---|---|
| **Helm (templating-only)** | Go text templates over YAML | Universal; every vendor ships a chart; the ecosystem is the moat | String templating YAML is fundamentally unsafe — indentation bugs, no types, `nindent` traps | Default. You must speak it regardless, because third-party charts are Helm |
| **[Kustomize](https://github.com/kubernetes-sigs/kustomize)** | Structural overlays, no templating | Merges typed YAML, not strings; built into `kubectl` (v1.37 embeds v5.8.1) | No variables, no conditionals *by design*; you trade template sprawl for overlay sprawl, and anything truly dynamic must leave Kustomize | Small, structural per-cell deltas over a common base |
| **[Tanka](https://github.com/grafana/tanka) / jsonnet** | Real config language | Functions, imports, actual abstraction; battle-tested at Grafana | Nobody knows Jsonnet; thin tooling; ecosystem ≈ Grafana and a few shops | Only if the team already knows Jsonnet |
| **[cdk8s](https://cdk8s.io/)** | TypeScript/Python/Go → manifests | Types, IDE completion, refactoring; real loops | CNCF Sandbox since 2020, never promoted; a Node/jsii toolchain in your deploy path; construct library trails upstream Kubernetes | Generating many cells programmatically, if the team is fluent |
| **[Timoni](https://timoni.sh/)** | CUE modules, OCI-distributed | CUE gives genuine schema validation and typed values — the thing Helm most lacks | v0.33.0, pre-1.0, de-facto single maintainer, not in CNCF | Watch it; do not bet a platform on it yet |
| **[KCL](https://kcl-lang.io/)** | Purpose-built config language | Schemas, LSP, constraint checking | CNCF Sandbox with **"Concerning"** health and no tagged GitHub release since v0.11.2 (Apr 2025) | Not today |
| **[Helmfile](https://github.com/helmfile/helmfile)** | Declarative orchestration over Helm | Manages N releases/environments; v1.7.4, very active, supports Helm 4 | Orchestration *on top of* Helm, not an escape — you keep every Go-template problem and add a second templated layer | If you need release orchestration and are not on Argo/Flux |
| **Plain Go templating** | Your own renderer | Total control; types from your own structs | You now maintain a chart format nobody else knows; no third-party charts | Never, for a whole platform |

My honest read for your situation: **Helm for anything third-party (non-negotiable — vendors ship charts), plus Kustomize overlays for per-cell structural deltas.** Kustomize is genuinely good at "same base, small typed differences per environment," which is precisely the many-cells shape, and its patches are structural rather than textual so they cannot produce the indentation class of bug. The combination `helm template | kustomize build` is well-trodden and both halves are in `kubectl`'s own orbit. Timoni is the one to watch, because CUE-typed values would eliminate an entire defect class — but pre-1.0 with one maintainer is not where a cell platform should live.

### Debugging

```bash
# Show the render even when the YAML is invalid. Without --debug you just get
# "Use --debug flag to render out invalid YAML".
helm template cell ./charts/temporal-cell --debug

# Render with real cluster contact — the only way to exercise `lookup`.
helm template cell ./charts/temporal-cell --dry-run=server

# Isolate one template.
helm template cell ./charts/temporal-cell -s templates/deployment.yaml

# See exactly what values Helm computed after all merging.
helm template cell ./charts/temporal-cell -f a.yaml -f b.yaml --debug 2>&1 \
  | sed -n '/^USER-SUPPLIED VALUES/,/^HOOKS/p'

# For anything that DOES have release state: what Helm believes is deployed.
helm get manifest my-release
helm get values my-release --all
helm get hooks my-release

# Compare rendered output across two chart versions, no cluster required.
# The helm-diff plugin only has release/revision/rollback/upgrade subcommands,
# all of which need release state, so for a pure local comparison use process
# substitution against two `helm template` runs.
diff -u <(helm template cell ./charts/v1) <(helm template cell ./charts/v2)
```

`--dry-run=server` arrived in **Helm 3.13.0** and *"also works on `helm template`"* ([Helm 3.13 blog](https://helm.sh/blog/helm-3.13/)). It is the only way to test a `lookup` and the only accurate simulation once SSA is in play — HIP-0023 notes *"`--dry-run=client` won't accurately simulate an install with SSA enabled… instead use `--dry-run=server`."*

`helm get manifest` returns *"what Helm's release storage says is deployed"*, not a re-render ([helm get manifest](https://helm.sh/docs/helm/helm_get_manifest/)). Diffing it against a fresh `helm template` is the canonical way to detect that a release has drifted from its chart — useful when migrating an existing Helm-managed estate into a rendered-manifest pipeline.

---

## Hands-on

**Cost: $0.** Everything runs on kind locally.

### Setup

```bash
brew install helm kind kubectl kubeconform conftest
helm plugin install https://github.com/helm-unittest/helm-unittest --verify=false
helm version          # expect v4.2.x
kind create cluster --name helm-lab
```

### Lab 1 — Build a chart and hit the `nindent` bug on purpose

```bash
mkdir -p ~/helm-lab && cd ~/helm-lab
helm create temporal-cell
rm -rf temporal-cell/templates/tests temporal-cell/templates/hpa.yaml \
       temporal-cell/templates/ingress.yaml temporal-cell/templates/serviceaccount.yaml
```

Now deliberately break it. Edit `temporal-cell/templates/deployment.yaml` and replace the resources block with the broken form:

```yaml
          # BROKEN ON PURPOSE
          resources:
            {{- toYaml .Values.resources | indent 12 }}
```

```yaml
# temporal-cell/values.yaml — give resources a real value
resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 128Mi
```

```bash
helm template cell ./temporal-cell
```

You will get a YAML parse error. Now look at the actual bytes:

```bash
helm template cell ./temporal-cell --debug 2>&1 | grep -A4 'resources:'
```

The first line of `toYaml` output is glued directly after `resources:` because `{{-` consumed the newline, and every subsequent line is indented 12 — so `limits:` ends up on the wrong line entirely. Fix it:

```yaml
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
```

Then produce the *second* variant of the same bug — set `resources: {}` in values and re-render with the fixed `nindent`. You get `resources:` followed by `{}`, which is fine, but try `resources: null`:

```bash
helm template cell ./temporal-cell --set resources=null | grep -A2 'resources:'
```

Now wrap it the way Helm 4's own scaffold does and confirm the key disappears entirely:

```yaml
          {{- with .Values.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
```

Doing this by hand once means you will spot it in code review forever.

### Lab 2 — Prove what dies without a cluster

Add a template that depends on all three degraded features:

```yaml
# temporal-cell/templates/degraded.yaml
{{- $existing := lookup "v1" "ConfigMap" .Release.Namespace "cell-state" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "temporal-cell.fullname" . }}-probe
data:
  lookupFoundExisting: {{ if $existing }}"yes"{{ else }}"no"{{ end }}
  isUpgrade: {{ .Release.IsUpgrade | quote }}
  isInstall: {{ .Release.IsInstall | quote }}
  releaseName: {{ .Release.Name | quote }}
  hasPrometheusCRD: {{ .Capabilities.APIVersions.Has "monitoring.coreos.com/v1" | quote }}
  kubeVersion: {{ .Capabilities.KubeVersion.Version | quote }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "temporal-cell.fullname" . }}-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": hook-succeeded
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: busybox:1.36
          command: ["sh", "-c", "echo migrating; sleep 2"]
```

```bash
# 1. Create the ConfigMap the lookup is looking for.
kubectl create namespace temporal-system
kubectl -n temporal-system create configmap cell-state --from-literal=x=1

# 2. Plain template — lookup is blind, IsUpgrade is false, no Prometheus CRD.
helm template cell ./temporal-cell -n temporal-system | grep -A8 'name: cell-temporal-cell-probe'

# 3. Now with a server dry-run — the lookup works.
helm template cell ./temporal-cell -n temporal-system --dry-run=server \
  | grep lookupFoundExisting

# 4. Feed in capabilities explicitly.
helm template cell ./temporal-cell -n temporal-system \
  --api-versions monitoring.coreos.com/v1 --kube-version 1.34.0 \
  | grep -E 'hasPrometheusCRD|kubeVersion'

# 5. Confirm the hook Job IS in the output but has no lifecycle.
helm template cell ./temporal-cell -n temporal-system | grep -c 'kind: Job'
helm template cell ./temporal-cell -n temporal-system --no-hooks | grep -c 'kind: Job'
```

Step 5 is the important one: the Job appears in normal output and disappears with `--no-hooks`. In a GitOps pipeline that Job is an ordinary resource that will be applied — and re-applied — with no ordering guarantee relative to the Deployment.

### Lab 3 — Schema-validate your values

Create `temporal-cell/values.schema.json` (JSON has no comments, so the path goes here rather than inside the file):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["replicaCount", "image"],
  "properties": {
    "replicaCount": { "type": "integer", "minimum": 1, "maximum": 50 },
    "image": {
      "type": "object",
      "required": ["repository"],
      "properties": {
        "repository": { "type": "string" },
        "tag":        { "type": "string" },
        "pullPolicy": { "type": "string", "enum": ["Always", "IfNotPresent", "Never"] }
      },
      "additionalProperties": false
    }
  }
}
```

```bash
helm template cell ./temporal-cell --set replicaCount=100          # fails: maximum
helm template cell ./temporal-cell --set image.pullPolicy=Sometimes # fails: enum
helm template cell ./temporal-cell --set image.repositry=nginx      # fails: typo caught
helm lint ./temporal-cell                                           # schema runs here too
helm template cell ./temporal-cell --set replicaCount=100 --skip-schema-validation  # escape hatch
```

That third command is the payoff: `additionalProperties: false` turns a misspelled values key from a silent no-op into a build failure.

### Lab 4 — Unit tests

```yaml
# temporal-cell/tests/deployment_test.yaml
suite: deployment
templates:
  - deployment.yaml
tests:
  - it: sets replicas from values
    set: { replicaCount: 7 }
    asserts:
      - equal: { path: spec.replicas, value: 7 }

  - it: omits resources when empty
    set: { resources: null }
    asserts:
      # Quote any path containing [] — unquoted it is a YAML flow sequence
      # inside a flow mapping, which fails to parse.
      - notExists: { path: "spec.template.spec.containers[0].resources" }

  - it: renders resources with correct nesting when set
    set:
      resources:
        limits: { memory: 8Gi }
    asserts:
      - equal:
          path: spec.template.spec.containers[0].resources.limits.memory
          value: 8Gi
```

```bash
helm unittest ./temporal-cell
helm unittest ./temporal-cell -f 'tests/*_test.yaml' --color
```

Break the `nindent` back to `indent` and watch the third test fail — that is your regression net for the bug class from Lab 1.

### Lab 5 — Convert to a rendered-manifest workflow with a CI diff gate

```bash
cd ~/helm-lab
mkdir -p values rendered/cell-001 rendered/cell-002 policy
```

```yaml
# values/base.yaml
image: { repository: nginx, tag: "1.27", pullPolicy: IfNotPresent }
replicaCount: 2
resources:
  limits:   { cpu: 500m, memory: 512Mi }
  requests: { cpu: 100m, memory: 128Mi }
```

```yaml
# values/cell-002.yaml
replicaCount: 5
resources:
  limits: { cpu: "2", memory: 4Gi }
```

```makefile
# Makefile
CELLS      := cell-001 cell-002
KUBE_VER   := 1.34.0
API_VERS   := --api-versions monitoring.coreos.com/v1 --api-versions cert-manager.io/v1

.PHONY: render verify test

render:
	@for c in $(CELLS); do \
	  extra=""; [ -f values/$$c.yaml ] && extra="-f values/$$c.yaml"; \
	  helm template $$c ./temporal-cell \
	    --namespace temporal-system \
	    --kube-version $(KUBE_VER) $(API_VERS) \
	    --include-crds \
	    -f values/base.yaml $$extra \
	    > rendered/$$c/manifests.yaml; \
	  echo "rendered $$c"; \
	done

verify: render
	@git diff --exit-code -- rendered/ \
	  || (echo "ERROR: rendered manifests are stale. Run 'make render' and commit."; exit 1)

test:
	helm lint ./temporal-cell --strict -f values/base.yaml
	helm unittest ./temporal-cell
	@for c in $(CELLS); do \
	  kubeconform -strict -summary -kubernetes-version $(KUBE_VER) \
	    -schema-location default rendered/$$c/manifests.yaml; \
	  conftest test --policy policy/ rendered/$$c/manifests.yaml; \
	done
```

```rego
# policy/security.rego
package main

deny contains msg if {
  input.kind == "Deployment"
  some c in input.spec.template.spec.containers
  not c.resources.limits.memory
  msg := sprintf("container %v has no memory limit", [c.name])
}

deny contains msg if {
  input.kind == "Deployment"
  some c in input.spec.template.spec.containers
  not c.securityContext.runAsNonRoot
  msg := sprintf("container %v must set runAsNonRoot", [c.name])
}
```

```bash
git init && git add -A && git commit -m "baseline"
make render && git add -A && git commit -m "rendered manifests"

# Expect this to FAIL on the runAsNonRoot rule: `helm create` scaffolds
# `securityContext: {}`, so the `{{- with }}` wrapper omits the key entirely.
# That is the gate doing its job. Set securityContext.runAsNonRoot: true in
# values/base.yaml to make it pass.
make test

# Now change a value and observe the review artifact.
sed -i '' 's/replicaCount: 5/replicaCount: 9/' values/cell-002.yaml
make verify        # FAILS: rendered output is stale
make render
git diff -- rendered/    # <-- this is what a reviewer sees: replicas 5 -> 9
```

That `git diff` is the entire point of the pattern. The reviewer sees a Kubernetes-level change, not a values-level one, and policy ran against the exact bytes that will be applied.

### Lab 6 — Apply with SSA and force a conflict

```bash
kubectl create namespace temporal-system --dry-run=client -o yaml | kubectl apply -f -
kubectl -n temporal-system apply --server-side \
  --field-manager=cell-pipeline -f rendered/cell-001/manifests.yaml

# Inspect who owns what.
kubectl -n temporal-system get deploy cell-001-temporal-cell \
  -o jsonpath='{.metadata.managedFields[*].manager}{"\n"}'

# Simulate a rogue actor taking a field.
kubectl -n temporal-system scale deploy/cell-001-temporal-cell --replicas=11

kubectl -n temporal-system get deploy cell-001-temporal-cell \
  -o json | jq '.metadata.managedFields[] | {manager, operation}'

# Re-apply — conflict, because kubectl-scale now owns spec.replicas.
kubectl -n temporal-system apply --server-side \
  --field-manager=cell-pipeline -f rendered/cell-001/manifests.yaml

# Take ownership back, the way a GitOps controller does.
kubectl -n temporal-system apply --server-side --force-conflicts \
  --field-manager=cell-pipeline -f rendered/cell-001/manifests.yaml
```

Read the conflict error carefully — it names the exact field paths and the competing manager. That message is what you will be reading when a controller and your pipeline fight over an annotation at 3 a.m.

---

## Production gotchas

1. **`lookup` silently returns empty and your chart takes the wrong branch.** *"When used with the `helm template` command it always returns an empty response"* ([function list](https://helm.sh/docs/chart_template_guide/function_list/)). The classic victim is `lookup`-then-`randAlphaNum` secret generation, which regenerates the secret on every render and restarts every Pod on every sync. Grep every inherited chart for `lookup`.

2. **`Capabilities.APIVersions` is empty without `--api-versions`, so conditional resources silently vanish.** No error, no warning — the ServiceMonitor just is not there. Enumerate the target cluster's API surface in your render command and pin it with a unit test ([helm template](https://helm.sh/docs/helm/helm_template/)).

3. **`.Release.IsUpgrade` is always false and the release name is the literal string `release-name`.** Verified in [`pkg/cmd/template.go`](https://github.com/helm/helm/blob/main/pkg/cmd/template.go). Pass a release name explicitly, and use `--is-upgrade` if a chart genuinely needs it — or better, delete the branch.

4. **Hooks are rendered but never run — and then applied as ordinary resources.** `helm template` writes hook manifests to stdout; `--no-hooks` removes them. Your GitOps tool will apply that `pre-install` migration Job with no ordering guarantee, and may re-run it every sync. Either strip hooks and re-express them as Argo sync waves, or map them ([Argo CD Helm hooks](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/#helm-hooks)).

5. **Mixing Argo CD hooks and Helm hooks disables the Helm ones entirely.** *"If you define any Argo CD hooks, all Helm hooks will be ignored"* (same page). One stray `argocd.argoproj.io/hook` annotation anywhere in a chart neutralizes every Helm hook in it.

6. **There is no `Prune=true` sync option in Argo CD.** Only `Prune=false` and `Prune=confirm` exist ([gitops-engine constants](https://github.com/argoproj/gitops-engine/blob/master/pkg/sync/common/types.go)). Automated pruning is `spec.syncPolicy.automated.prune: true`. Copy-pasting `Prune=true` produces a silently ignored annotation and no pruning.

7. **`crds/` is not rendered unless you pass `--include-crds`.** Forget the flag and your CRDs never reach the cluster, and everything that depends on them fails to apply. Bake it into your Makefile, not into someone's memory ([charts](https://helm.sh/docs/topics/charts/#custom-resource-definitions-crds)).

8. **`toYaml | indent n` is almost always wrong; you want `nindent`.** And a `toYaml` of an empty value renders the key as `null`. Wrap in `{{- with }}` so the key disappears, which is what Helm 4's own scaffold does ([named templates](https://helm.sh/docs/chart_template_guide/named_templates/)).

9. **`default` treats `0`, `""`, and `false` as empty.** `{{ .Values.replicas | default 3 }}` returns 3 when the user explicitly set 0. Use `hasKey` or a schema default when zero is meaningful.

10. **Template names are global across a chart and all subcharts.** An unprefixed `{{- define "labels" }}` will be silently overridden by whichever chart defines it last. Always prefix with the chart name ([tips and tricks](https://helm.sh/docs/howto/charts_tips_and_tricks/)).

11. **`NOTES.txt` never appears in `helm template` output** — Helm 4 deprecated the related flags because *"template output never includes notes"* ([Helm 4 overview](https://helm.sh/docs/overview/)). Post-install instructions in a chart you inherit are invisible in your workflow.

12. **Nothing prunes.** Delete a template and the object stays in the cluster forever until something with an inventory removes it. Argo CD's tracking annotation and Flux's `.status.inventory` are the two production answers; `kubectl apply --prune` is *"still in alpha due to usability, correctness and performance issues with its design"* ([declarative config](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/#alternative-kubectl-apply-f-directory-prune)).

13. **ApplySet has been alpha since Kubernetes 1.27 and has not moved.** [KEP-3659](https://github.com/kubernetes/enhancements/blob/master/keps/sig-cli/3659-kubectl-apply-prune/kep.yaml) still says `stage: alpha`, `beta: TBD`, gated on `KUBECTL_APPLYSET=true`. Kubernetes is now on v1.37. Do not plan around it.

14. **Changing your `--field-manager` name orphans every field it owned.** SSA tracks ownership by manager *name*. Pick one, put it in a constant, and treat renaming it as a migration with an explicit `--force-conflicts` pass ([server-side apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/)).

15. **Client-side apply hits the 262144-byte annotation limit on large CRDs.** SSA does not use `last-applied-configuration` at all, which is why Argo CD documents it as the fix ([sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/#server-side-apply)).

16. **Helm 4 latches the apply method per release.** New installs get SSA; anything created by Helm 3 stays on client-side apply after the binary upgrade unless you pass `--server-side` ([HIP-0023](https://helm.sh/community/hips/hip-0023)). Mixed estates therefore have two conflict models running side by side.

17. **`--dry-run=client` does not simulate SSA correctly.** HIP-0023 says so explicitly: use `--dry-run=server`. Since 3.13, that flag works on `helm template` too ([Helm 3.13](https://helm.sh/blog/helm-3.13/)).

18. **`helm registry login` in Helm 4 takes a domain, not a URL.** Scheme and path are rejected. CI scripts that pass `https://registry.internal/v2/` break on upgrade ([Helm 4 overview](https://helm.sh/docs/overview/)).

19. **`--post-renderer` now takes a plugin name, not an executable path.** Post-renderers became plugins in Helm 4 ([HIP-0026](https://helm.sh/community/hips/hip-0026)). Any kustomize-post-render script breaks on upgrade.

20. **`chart-testing` (ct) does not support Helm 4** — v3.14.0 still vendors `helm.sh/helm/v3`. If it is in your pipeline you need a pinned Helm 3 binary for that step, or a replacement built from `helm lint` + `helm unittest` + `kubeconform`.

21. **`kubeval` is dead.** Its README opens with *"NOTE: This project is no longer maintained, a good replacement is kubeconform."* Anything still calling `kubeval` is validating against a frozen schema set.

22. **`.helmignore` does not support `**` and its `!` negation is undocumented-to-contradictory.** It uses Go's `filepath.Match`, not `fnmatch` ([helmignore](https://helm.sh/docs/chart_template_guide/helm_ignore_file/)). Verify with `helm package` and `tar tzf`, do not assume gitignore semantics.

23. **Helm 3 support ends soon.** Final limited feature release 2026-09-09, security fixes through 2027-02-10 ([Helm 3 EOL](https://helm.sh/blog/helm-v3-end-of-life/)). Pin an explicit Helm 4 binary version in CI now, because "whatever the runner image ships" will change under you.

---

## How this shows up in cell lifecycle

**Cell provisioning.** Terraform builds the cluster and installs exactly one thing: the GitOps agent. Everything after that is rendered manifests. The cell's identity — name, cloud, region, tier, Temporal version — is a values file, and cell provisioning is `make render` for a new cell plus a commit. That means creating a cell is a reviewable PR whose diff is the full set of Kubernetes objects that will exist, which is a far better review surface than "we ran a script."

**The layering that works.** A `temporal-lib` library chart owns naming, labels, security contexts, topology spread, and PDB defaults. Component charts (frontend, history, matching, worker) depend on it. A `values/base.yaml` plus `values/<cloud>.yaml` plus `values/cells/<cell>.yaml` stack gives you three reviewable levels of override. Per-cell structural deltas that do not fit the values interface become Kustomize patches on the rendered output rather than more `if` branches in the chart — this is the discipline that keeps templates readable at 300 cells.

**Multi-cloud is a values-file problem, not a template problem.** Storage classes, load balancer annotations, node selectors, and IRSA/Workload Identity/Azure Workload Identity annotations differ across AWS, GCP, and Azure. Put them in `values/aws.yaml`, `values/gcp.yaml`, `values/azure.yaml`. Resist adding `{{ if eq .Values.cloud "aws" }}` to templates; the moment you have three of those in one file the chart is unreviewable. The values interface should be cloud-neutral, and the cloud-specific facts should be data.

**Upgrading a cell.** Bump a chart version or a values key, run `make render`, and the PR diff *is* the change plan. Reviewers see the actual container image, the actual resource limits, the actual rollout strategy. Roll it out cell by cell by regenerating a subset — which is exactly the "render only these cells" capability your Makefile should expose. Rollback is `git revert`, which is more auditable than `helm rollback` and does not depend on a Secret in a cluster that might be the thing that is broken.

**Tearing down a cell.** With rendered manifests, teardown is deleting the cell's directory and letting the GitOps controller prune. This *only* works if pruning is correctly configured — which is why the pruning section is not optional reading. Verify on a test cell that removing a directory actually removes the objects, and that `Prune=confirm` or an equivalent guard exists for anything stateful. A namespace deletion that hangs on a finalizer is the common failure, and it will hang your teardown workflow.

**Networking, when another team owns it.** Gateway/Ingress/Service objects are the interface. In a rendered-manifest world the natural split is: the networking team owns a chart or a set of templates, you own the values that parameterize it per cell, and the rendered output makes the boundary auditable. Two teams touching the same objects is exactly the SSA field-ownership scenario — agree on distinct field managers and distinct object sets, and use `ignoreDifferences` (Argo) rather than fighting over fields.

**Third-party charts are the hard part.** cert-manager, Kyverno, Prometheus, the CNI — all shipped as Helm charts, many assuming hooks and release state. For each one, render it, read the output for hook annotations and `lookup` calls, and decide: strip and re-express as sync waves, or carve it out and let Flux `HelmRelease` manage it with real release state. A hybrid is legitimate — "our charts are rendered, these four vendor charts get real Helm releases" — as long as the boundary is written down.

---

## Learning path

**Day 1 (3-5 hours).** Read the [Chart Template Guide](https://helm.sh/docs/chart_template_guide/) end to end — it is short and it is the actual reference. Run Labs 1 and 2; the `nindent` lab and the degraded-features lab together give you the two things you need immediately. Then `helm template` your team's real cell chart with the exact flags your pipeline uses and read the output — all of it. Grep it for `lookup`, `IsUpgrade`, `Capabilities`, and `helm.sh/hook` and ask what each one does in your pipeline. Find out which GitOps tool applies the output and whether pruning is on.

**Week 1.** Run Labs 3 through 6. Read the [values files](https://helm.sh/docs/chart_template_guide/values_files/) and [subcharts and globals](https://helm.sh/docs/chart_template_guide/subcharts_and_globals/) pages properly. Read the [Kubernetes server-side apply doc](https://kubernetes.io/docs/reference/using-api/server-side-apply/) in full — it is the field-ownership model you now live in. Add a `values.schema.json` with `additionalProperties: false` to one chart that lacks one, and write three `helm unittest` cases for it. Read the [Akuity rendered-manifests writeup](https://akuity.io/blog/the-rendered-manifests-pattern) and the [Argo CD manifest-hydrator proposal](https://github.com/argoproj/argo-cd/blob/master/docs/proposals/manifest-hydrator.md), then compare against what your team actually built and note the differences.

**Month 1.** Own the chart CI pipeline. Concretely: get `helm lint --strict` + `helm unittest` + `kubeconform` + `conftest` + a snapshot-diff gate running on every chart PR, and make the rendered-manifest staleness check blocking. Audit every third-party chart in the estate for hook and `lookup` dependence and write down the disposition of each. Verify pruning works by deleting a resource from a chart on a test cell and confirming it actually disappears — do not assume. Form an opinion on whether per-cell structural deltas should be more values keys or Kustomize patches, and write it down as a convention before the chart accumulates twenty conditionals. Then check whether `chart-testing`, `helm diff`, or any plugin in your pipeline is still pinned to Helm 3, and plan that migration ahead of the September 2026 final release.

---

## References

1. [Helm documentation](https://helm.sh/docs/) — canonical. Watch for the *"This page has not yet been updated for Helm 4"* banner on core topic pages.
2. [Helm 4 Overview](https://helm.sh/docs/overview/) — the authoritative list of breaking changes: flag renames, post-renderer plugins, domain-only registry login, SSA defaults, `helm template` flag deprecations. Full detail in the [Helm 4 Changelog](https://helm.sh/docs/changelog/).
3. [Helm 4 Released](https://helm.sh/blog/helm-4-released/) — the 2025-11-12 release: Wasm plugins, kstatus, OCI digests, multi-doc values.
4. [Helm 3 End of Life](https://helm.sh/blog/helm-v3-end-of-life/) — the extended timeline (final feature release 2026-09-09, security to 2027-02-10). Supersedes the dates in the Helm 4 release post. Patch versions at [helm/helm releases](https://github.com/helm/helm/releases).
5. [HIP-0023 — Server-Side Apply](https://helm.sh/community/hips/hip-0023) — the rationale, apply-method latching, conflict handling, and why `--dry-run=client` is inaccurate under SSA. The [HIP index](https://helm.sh/community/hips/) is the best source for *why* Helm 4 did what it did.
6. [Charts](https://helm.sh/docs/topics/charts/) — `Chart.yaml` field reference, chart types, dependencies, `crds/` and its verbatim limitations, `values.schema.json`, `import-values`.
7. [Chart Template Guide](https://helm.sh/docs/chart_template_guide/) — the primary learning resource; read it linearly once.
8. [Template function list](https://helm.sh/docs/chart_template_guide/function_list/) — every function including `lookup`, `required`, `toYaml`, `nindent`, and the "always returns an empty response" statement.
9. [Named templates](https://helm.sh/docs/chart_template_guide/named_templates/) — `include` vs `template`, the underscore-file rule, global template names, and the `nindent` correction example.
10. [Charts Tips and Tricks](https://helm.sh/docs/howto/charts_tips_and_tricks/) — the canonical `{{- ... | nindent n }}` explanation, `tpl`, `required`, and `SOURCE_DATE_EPOCH` for reproducible packaging.
11. [Values files](https://helm.sh/docs/chart_template_guide/values_files/) — the verbatim precedence order.
12. [Subcharts and globals](https://helm.sh/docs/chart_template_guide/subcharts_and_globals/) — the four rules, including "globals require explicit declaration."
13. [Built-in objects](https://helm.sh/docs/chart_template_guide/builtin_objects/) — `.Release`, `.Capabilities`, `.Chart`, `.Files`, `.Values`.
14. [`helm template` reference](https://helm.sh/docs/helm/helm_template/) — `--api-versions`, `--kube-version`, `--is-upgrade`, `--include-crds`, `--no-hooks`, `--skip-tests`. Regenerated 2026-08-14.
15. [Chart hooks](https://helm.sh/docs/topics/charts_hooks/) — the nine hook types, `hook-weight`, and `hook-delete-policy` with its `before-hook-creation` default.
16. [helm/helm#6443](https://github.com/helm/helm/issues/6443) — the design discussion establishing that `helm template` includes hooks in output but never executes them. The docs never say this outright; this is the citation.
17. [`pkg/cmd/template.go`](https://github.com/helm/helm/blob/main/pkg/cmd/template.go) — source of truth for `helm template`'s real behavior: no `render` alias, hardcoded `release-name`, `--is-upgrade`, no cluster write path.
18. [Library charts](https://helm.sh/docs/topics/library_charts/) — `type: library`, and the `.Files`/`.Values` scoping differences from application subcharts.
19. [OCI registries](https://helm.sh/docs/topics/registries/) — `helm push`, `oci://` dependencies, media types, digest references. Classic repos are covered at [chart repository](https://helm.sh/docs/topics/chart_repository/) and are *not* deprecated.
20. [Debugging templates](https://helm.sh/docs/chart_template_guide/debugging/) — `--debug`, `--dry-run=server`, and template isolation. The [Helm 3.13 blog](https://helm.sh/blog/helm-3.13/) is where `--dry-run=server` came from.
21. [`helm get manifest`](https://helm.sh/docs/helm/helm_get_manifest/) — reads Helm's release storage, not a re-render; the drift-detection tool for a mixed estate.
22. [The Rendered Manifests Pattern — Akuity](https://akuity.io/blog/the-rendered-manifests-pattern) — the canonical writeup (Nicholas Morey, Akuity; first published 2023, revised July 2026). Secondary source, but the definitive articulation. [Companion repo](https://github.com/akuity/rendered-manifest-pattern).
23. [Argo CD manifest-hydrator proposal](https://github.com/argoproj/argo-cd/blob/master/docs/proposals/manifest-hydrator.md) — the upstream design doc that turned the pattern into a feature; primary source.
24. [Argo CD Source Hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/) — the shipped feature, beta since v3.5.0.
25. [Argo CD Helm support](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/) — the *"Helm is only used to inflate charts with `helm template`"* statement and the full Helm→Argo hook mapping table.
26. [Argo CD FAQ — why `helm ls` shows nothing](https://argo-cd.readthedocs.io/en/stable/faq/#after-deploying-my-helm-application-with-argo-cd-i-cannot-see-it-with-helm-ls-and-other-helm-commands) — the clearest statement of what Argo CD does and does not use from Helm.
27. [Argo CD sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/) — `Prune=false`/`confirm`, `PruneLast`, `ServerSideApply`, `Replace`, `RespectIgnoreDifferences`, `ClientSideApplyMigration`. Automated pruning lives in [auto sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).
28. [Argo CD resource tracking](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_tracking/) and the [3.0 tracking default change](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/2.14-3.0/#use-annotation-based-tracking-by-default) — how pruning knows what it owns.
29. [Argo CD sync waves and phases](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/) — the replacement for Helm hook ordering; note the older `resource_hooks` URL is now a stub.
30. [gitops-engine sync types](https://github.com/argoproj/gitops-engine/blob/master/pkg/sync/common/types.go) — the authoritative list of valid sync options; settles the `Prune=true` myth.
31. [Flux HelmRelease](https://fluxcd.io/flux/components/helm/helmreleases/) — the counterexample: real Helm release state, real hooks, `.disableHooks`.
32. [Flux Kustomization prune](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune) and [inventory](https://fluxcd.io/flux/components/kustomize/kustomizations/#inventory) — server-side garbage collection with no annotations on managed objects.
33. [Flux v2.8.0 release](https://fluxcd.io/blog/2026/02/flux-v2.8.0/) — Helm 4, SSA and kstatus as new defaults, and the `UseHelm3Defaults` gate.
34. [Kubernetes — Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) — field managers, `managedFields`, conflicts, `--force-conflicts`, and migration from client-side apply. GA context in the [1.22 announcement](https://kubernetes.io/blog/2021/08/06/server-side-apply-ga/).
35. [Kubernetes — declarative config and `--prune`](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/#alternative-kubectl-apply-f-directory-prune) — the verbatim alpha warning on `kubectl apply --prune`.
36. [KEP-3659 — kubectl apply prune / ApplySet](https://github.com/kubernetes/enhancements/blob/master/keps/sig-cli/3659-kubectl-apply-prune/kep.yaml) — still `stage: alpha`, `beta: TBD`; the proof it has not moved since v1.27.
37. [helm-unittest](https://github.com/helm-unittest/helm-unittest) — snapshot and assertion testing of rendered output; Helm 4 compatible since v1.1.0.
38. [kubeconform](https://github.com/yannh/kubeconform) — fast schema validation of rendered manifests, with CRD schema locations. The successor to the unmaintained kubeval. See also [chart-testing (ct)](https://github.com/helm/chart-testing), which does *not* yet support Helm 4, and the [helm-diff plugin](https://github.com/databus23/helm-diff).
39. [Kustomize](https://github.com/kubernetes-sigs/kustomize) — structural overlays; embedded in `kubectl` (v5.8.1 in Kubernetes v1.37). The best complement to templating-only Helm. For the rest of the landscape: [Timoni](https://timoni.sh/), [Tanka](https://github.com/grafana/tanka), [cdk8s](https://cdk8s.io/), [KCL](https://kcl-lang.io/), [Helmfile](https://github.com/helmfile/helmfile).
40. [Kubernetes v1.37 release notes](https://kubernetes.io/releases/1.37/) — current Kubernetes, released 2026-08-26; use it to set `--kube-version` and `kubeconform -kubernetes-version` honestly.
