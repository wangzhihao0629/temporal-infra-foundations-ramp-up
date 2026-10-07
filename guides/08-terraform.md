# Terraform — Platform Scale, Multi-Cloud, Many Cells

**Why this matters.** Cell lifecycle is, mechanically, the problem of applying the same infrastructure definition several hundred times across AWS, GCP, and Azure, with per-cell variation, and then being able to upgrade or delete any one of them without touching the other 299. Terraform is the tool most platform teams reach for, and the thing that actually determines whether that works is not HCL — it is state layout. State is where identity lives, state is what locks, state is what determines blast radius, and state is what you will be doing surgery on at 2 a.m. when a cell provision half-failed. You will be reading and writing modules on day one. The modules are the easy part. Read the state sections twice.

Everything below was verified against primary sources on **2026-08-29**. Where I could not verify something, I say so.

> **Stale-content trap, read this first.** Four things that most tutorials and most LLM-generated Terraform get wrong as of today:
>
> 1. **CDK for Terraform is dead.** HashiCorp's own page says it verbatim: *"The Cloud Development Kit for Terraform is deprecated as of December 10, 2025. HashiCorp no longer supports or maintains the Cloud Development Kit for Terraform"* ([CDKTF docs](https://developer.hashicorp.com/terraform/cdktf)). The repo was [archived on the same date](https://github.com/hashicorp/terraform-cdk). Do not start anything on it.
> 2. **S3 backend state locking no longer needs DynamoDB.** `use_lockfile` landed in Terraform [1.10.0](https://github.com/hashicorp/terraform/releases/tag/v1.10.0) and `dynamodb_table` is deprecated as of [1.11.0](https://github.com/hashicorp/terraform/releases/tag/v1.11.0). Every blog post that tells you to create a lock table is out of date ([S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)).
> 3. **Terraform Stacks is GA but is not an open-source feature.** There is no `terraform stacks plan` and no `terraform stacks apply` in the CLI — plan and apply execute in HCP Terraform or Terraform Enterprise 2.0+ ([`terraform stacks` command reference](https://developer.hashicorp.com/terraform/cli/commands/stacks)). If your org is not on HCP/TFE, Stacks is not on the menu.
> 4. **`tfsec` is retired.** It was folded into Trivy in 2023; the maintainers wrote *"tfsec will continue to remain available for the time being, although our engineering attention will be directed at Trivy going forward"* ([tfsec discussion #1994](https://github.com/aquasecurity/tfsec/discussions/1994)). Use `trivy config`.
>
> Current stable is **Terraform 1.16.0, released 2026-08-26** ([release tag](https://github.com/hashicorp/terraform/releases/tag/v1.16.0)). Current OpenTofu stable is **1.12.6, released 2026-08-19** ([release tag](https://github.com/opentofu/opentofu/releases/tag/v1.12.6)).

---

## The mental model

Hold six ideas.

**1. Terraform is a graph compiler with a persistent symbol table.** Configuration is not a script. Terraform parses every `.tf` file in the directory, builds a DAG from the references between blocks, and walks it. Ordering in the file is meaningless; `depends_on` and expression references are the only ordering primitives. The "symbol table" — the mapping from configuration address (`module.cell.aws_eks_cluster.this`) to real-world object ID (`arn:aws:eks:...`) — is the state file. Everything hard about Terraform is a consequence of that table being persistent, shared, and mutable.

**2. State is the source of truth for *identity*, not for *reality*.** State says "the address `aws_eks_cluster.this` corresponds to object X." It also caches X's attributes, but that cache is refreshed at plan time and is not authoritative. When you delete a resource from configuration, Terraform does not "forget" it — it looks up the address in state, finds an object, and plans a destroy. This is why removing a `for_each` key deletes infrastructure, why `count` index shifts are catastrophic, and why `moved`/`import`/`removed` blocks exist at all: they are edits to the identity map that produce no infrastructure change.

**3. The plan is a value, and it can be persisted.** `terraform plan -out=tfplan` writes an opaque artifact containing the config, the prior state, and the resolved diff. `terraform apply tfplan` executes exactly that, with no re-planning and no confirmation prompt ([apply — saved plan mode](https://developer.hashicorp.com/terraform/cli/commands/apply#saved-plan-mode)). This is the entire basis of plan-in-PR/apply-on-merge. It is also why plan files are sensitive artifacts: HashiCorp says *"You should therefore treat any saved plan files as potentially-sensitive artifacts"* ([plan `-out`](https://developer.hashicorp.com/terraform/cli/commands/plan#out-filename)).

**4. Providers are plugins with their own configuration lifecycle, and provider configuration cannot depend on unknown values.** The rule, verbatim: *"You can use expressions to configure provider arguments, but you can only reference values that Terraform knows before it applies your configuration"* ([provider block](https://developer.hashicorp.com/terraform/language/block/provider#provider-specific-arguments)). This single sentence is why you cannot create a Kubernetes cluster and manage its resources in one apply, and it dictates the entire shape of a cell pipeline.

**5. Blast radius equals state file.** A `terraform apply` can only damage what is in the state it holds. One state per cell means a bad module change breaks one cell at a time. One state for all cells means a bad module change is a fleet-wide outage and a 40-minute plan. Choose deliberately, and choose before you have 300 cells, because splitting state later is manual surgery.

**6. Terraform is a *convergence* tool with no notion of rollout.** There is no canary, no wave, no progressive delivery, no health check that reverts. If you need "upgrade 5 cells, watch, then 50," that logic lives in your orchestrator — a pipeline, a Temporal workflow, Terragrunt, Stacks deployment groups — not in Terraform. Recognizing that boundary early saves you from trying to build rollout semantics out of `count` and `-target`.

```text
   .tf files ──► parse ──► build DAG ──► for each node:
                              │            ┌─────────────────────────────┐
                              │            │ state entry?                │
                              │            │  no  → CREATE               │
   ┌──────────────┐           │            │  yes → refresh from provider│
   │ state (S3/…) ├───────────┤            │        diff vs config       │
   │  addr → id   │           │            │        → NOOP/UPDATE/REPLACE│
   └──────┬───────┘           │            │ config gone, state present  │
          │                   │            │       → DESTROY             │
          │                   ▼            └─────────────────────────────┘
          │              plan artifact  ──────► apply ──► provider CRUD
          │                                                    │
          └────────────────── write new state ◄────────────────┘
                    (lock held for the whole apply)
```

---

## Core concepts

### The plan/apply cycle, precisely

`terraform init` resolves modules and providers, writes `.terraform/`, and creates or verifies `.terraform.lock.hcl`. `terraform plan` acquires a state lock (read), refreshes each managed resource against its provider, computes the diff, and releases. `terraform apply` acquires a write lock, walks the diff graph with a default concurrency of **10** (`-parallelism=n`, [documented default 10](https://developer.hashicorp.com/terraform/cli/commands/apply#parallelism-n)), and persists new state after each resource completes — not at the end. That last detail is why a killed apply leaves partially-applied state rather than nothing, and why the lock matters more than people think.

Three plan modes matter operationally:

| Mode | Invocation | What it does |
|---|---|---|
| Normal | `terraform plan` | Refresh, then diff config vs refreshed state |
| Refresh-only | `terraform plan -refresh-only` | Diff *state* vs reality; propose state updates only. Added in v0.15.4 ([planning modes](https://developer.hashicorp.com/terraform/cli/commands/plan#planning-modes)) |
| Destroy | `terraform plan -destroy` | Plan destruction of everything in state |

`terraform refresh` still exists but is deprecated: *"This command is deprecated. Instead, add the `-refresh-only` flag to `terraform apply` and `terraform plan` commands."* The docs also spell out why, and it is a genuinely important failure mode: *"If you have misconfigured credentials for one or more providers, Terraform may be misled into thinking that all of the managed objects have been deleted, causing it to remove all of the tracked objects without any confirmation prompt"* ([refresh](https://developer.hashicorp.com/terraform/cli/commands/refresh)).

For automation, `-detailed-exitcode` is the contract: **0** = success, empty diff; **1** = error; **2** = success, non-empty diff ([plan `-detailed-exitcode`](https://developer.hashicorp.com/terraform/cli/commands/plan#detailed-exitcode)). Note it is documented on `plan` only.

### HCL, in the depth you actually need

#### Resources, data sources, locals, outputs

```hcl
# A resource is a managed object: Terraform creates, updates, and destroys it.
resource "aws_eks_cluster" "this" {
  name     = local.cell_name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = false
  }
}

# A data source is a read. It is refreshed on every plan and is a plan-time
# dependency — if it cannot be read, the plan fails.
data "aws_caller_identity" "current" {}

# Locals are named expressions, evaluated lazily, scoped to the module.
locals {
  account_id = data.aws_caller_identity.current.account_id
  cell_name  = "${var.cell_prefix}-${var.cloud}-${var.region}-${var.cell_index}"

  # Locals are the right place for the cross-cutting policy of a module.
  common_tags = merge(var.extra_tags, {
    "example.com/cell"        = local.cell_name
    "example.com/managed-by"  = "terraform"
    "example.com/cell-tier"   = var.cell_tier
  })
}

# Outputs are the module's public API. Type them (Terraform 1.15+) and mark
# anything secret.
output "cluster_endpoint" {
  description = "EKS API server endpoint for this cell."
  value       = aws_eks_cluster.this.endpoint
  type        = string
}
```

Typed `output` blocks and `deprecated` markers on variables and outputs both landed in **1.15.0** ([v1.15 changelog](https://github.com/hashicorp/terraform/blob/v1.15/CHANGELOG.md)). `deprecated` is genuinely useful for a platform team: you can retire a module input across 300 callers with a warning instead of a breakage.

#### Variables, validation, and the type system

```hcl
variable "cell" {
  description = "Full specification of one cell."

  type = object({
    name   = string
    cloud  = string
    region = string

    # optional() with a default is the single most useful type-system feature
    # for module interfaces. Terraform 1.3+.
    kubernetes_version = optional(string, "1.34")
    node_pools = optional(map(object({
      instance_type = string
      min_size      = optional(number, 1)
      max_size      = optional(number, 10)
      spot          = optional(bool, false)
      taints        = optional(list(object({
        key    = string
        value  = string
        effect = string
      })), [])
    })), {})

    # Nested optional objects need a default at every level you want to omit.
    networking = optional(object({
      pod_cidr      = optional(string, "10.244.0.0/16")
      service_cidr  = optional(string, "10.96.0.0/12")
      enable_ipv6   = optional(bool, false)
    }), {})
  })

  validation {
    condition     = contains(["aws", "gcp", "azure"], var.cell.cloud)
    error_message = "cell.cloud must be one of aws, gcp, azure."
  }

  validation {
    condition     = can(regex("^[a-z0-9-]{3,32}$", var.cell.name))
    error_message = "cell.name must be 3-32 chars of [a-z0-9-]."
  }

  # Terraform 1.9+ lets a validation reference other variables and data
  # sources, which is how you express cross-field invariants.
  validation {
    condition = !var.cell.networking.enable_ipv6 || var.cell.cloud != "azure"
    error_message = "IPv6 cells are not supported on Azure yet."
  }
}
```

Version floors worth memorizing, all from the [validation requirements table](https://developer.hashicorp.com/terraform/language/validate#requirements):

| Feature | Since | Notes |
|---|---|---|
| `variable` `validation` blocks | 0.13.0 | Single-variable only until 1.9 |
| Cross-variable / data-source validation | 1.9.0 | Lets you validate `var.a` against `var.b` |
| `precondition` / `postcondition` | 1.2.0 | Inside `lifecycle` on a resource, or in `output` |
| `check` blocks with `assert` | 1.5.0 | Non-blocking assertions; produce warnings, not errors ([check](https://developer.hashicorp.com/terraform/language/block/check)) |
| `optional()` attributes with defaults | 1.3.0 | [type constraints](https://developer.hashicorp.com/terraform/language/expressions/type-constraints#optional-object-type-attributes) |
| `nullable = false` on a variable | 1.1.0 | Rejects an explicit `null`; distinct from having a default |

**Where the type system bites in module interfaces.** Three specific traps:

- **Object types are structural and strict.** If a caller passes an extra attribute not in your `object({...})`, the plan fails. That is usually what you want, but it means adding an attribute to a shared module's input object is a *non-breaking* change while removing one is breaking — the reverse of what people assume.
- **`optional()` defaults do not propagate into nested objects unless you also default the nesting level.** In the example above, omitting `networking` entirely works only because `networking` itself has `optional(..., {})`. Without that outer default, omitting it yields `null` and `var.cell.networking.pod_cidr` explodes.
- **`map` vs `object` in a `for_each` source.** `for_each` requires a map or a set of strings. It cannot take a list, and it *does not implicitly convert* a list or tuple to a set — you must wrap with `toset()` ([`for_each`](https://developer.hashicorp.com/terraform/language/meta-arguments/for_each)). It also cannot take unknown or sensitive values, which is the root of the most common "Invalid for_each argument" failure.

#### `for_each` vs `count` — and the index-shift disaster

The current docs state the choice mildly: *"Use `for_each` when some instance arguments must have distinct values that can't be directly derived from an integer. Use the `count` argument when you want to create nearly identical instances"* ([`for_each`](https://developer.hashicorp.com/terraform/language/meta-arguments/for_each), [`count`](https://developer.hashicorp.com/terraform/language/meta-arguments/count)).

That undersells it. The reason `for_each` is almost always right is identity. `count` addresses instances by *position* — `aws_subnet.this[0]`, `[1]`, `[2]`. `for_each` addresses them by *key* — `aws_subnet.this["us-east-1a"]`. When the input list changes shape, position-addressed resources get reassigned to different real-world objects.

HashiCorp documented this explicitly, though the section was removed from the current docs and now survives only at a pinned version URL: *"If an element was removed from the middle of the list, every instance after that element would see its `subnet_id` value change, resulting in more remote object changes than intended"* ([v1.11.x count docs — When to Use `for_each` Instead of `count`](https://developer.hashicorp.com/terraform/language/v1.11.x/meta-arguments/count#when-to-use-for_each-instead-of-count)). Since a changed `subnet_id` on most resources forces replacement, "more remote object changes than intended" means, in practice, "Terraform destroys and recreates every node pool after the one you removed."

```hcl
# WRONG for anything with identity. Removing "cell-b" from the list renumbers
# cell-c into index 1 and Terraform plans to replace it.
variable "cells" { type = list(string) }

resource "aws_eks_cluster" "bad" {
  count = length(var.cells)
  name  = var.cells[count.index]
}

# RIGHT. Removing "cell-b" destroys exactly one cluster.
resource "aws_eks_cluster" "good" {
  for_each = toset(var.cells)
  name     = each.key
}

# For richer per-instance config, key on a stable identifier and carry the
# object as the value.
variable "cell_specs" {
  type = map(object({
    region = string
    tier   = string
  }))
}

resource "aws_eks_cluster" "fleet" {
  for_each = var.cell_specs
  name     = each.key
  tags     = { tier = each.value.tier }
}
```

The one case where `count` is genuinely correct is the boolean toggle — `count = var.enabled ? 1 : 0` — because there is only ever index 0 and its identity never shifts. Even there, be aware you have committed to `aws_thing.this[0]` as the address forever.

**`for_each` keys must be known at plan time.** If your keys come from another resource's computed attributes, the plan fails with "The `for_each` value depends on resource attributes that cannot be determined until apply." The fix is either to key on something static (a variable) or to split the apply into two stages. Terraform has an experimental "deferred actions" feature that would relax this — `-allow-deferral` on plan, allowing unknown `count`/`for_each` — but it is explicitly listed under `EXPERIMENTS` and is **not in stable 1.16** ([main CHANGELOG](https://raw.githubusercontent.com/hashicorp/terraform/main/CHANGELOG.md)). It *is* GA inside Terraform Stacks as "deferred changes" ([Stacks runs](https://developer.hashicorp.com/terraform/cloud-docs/stacks/deploy/runs#deferred-changes)), which is one of the few concrete reasons to care about Stacks.

#### `dynamic` blocks

`dynamic` generates repeatable *nested blocks* (not resources) from a collection.

```hcl
resource "aws_security_group" "cell" {
  name   = local.cell_name
  vpc_id = var.vpc_id

  dynamic "ingress" {
    for_each = var.ingress_rules   # map(object({...}))
    content {
      description = ingress.value.description
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = "tcp"
      cidr_blocks = ingress.value.cidrs
    }
  }
}
```

HashiCorp's own caution is worth quoting because it is routinely ignored: *"Overuse of `dynamic` blocks can make configuration hard to read and maintain, so we recommend using them only when you need to hide details in order to build a clean user interface for a re-usable module. Always write nested blocks out literally where possible"* ([dynamic blocks](https://developer.hashicorp.com/terraform/language/expressions/dynamic-blocks#best-practices-for-dynamic-blocks)). In a cell module the honest test is: is this block varying *per cell*, or am I just avoiding typing? If it does not vary, write it out.

#### `moved`, `import`, `removed` — state edits as code

These three blocks replace the CLI state commands for the common cases, and they are reviewable, plannable, and idempotent. Version floors: `moved` in **1.1**, `import` in **1.5**, `removed` in **1.7**, `import` with `for_each` in **1.7**, and `import` blocks *inside modules* only in **1.16**.

```hcl
# Refactoring: you renamed a resource or moved it into a submodule.
# Terraform updates the state address; no infrastructure changes.
moved {
  from = aws_eks_cluster.this
  to   = module.control_plane.aws_eks_cluster.this
}

# Adopting existing infrastructure. -generate-config-out will write the
# resource block for you from the live object.
import {
  to = aws_eks_cluster.legacy_cell
  id = "temporal-cell-042"
}

# Bulk import, 1.7+.
import {
  for_each = var.adopted_cells   # map(string) of address key -> real id
  to       = aws_eks_cluster.fleet[each.key]
  id       = each.value
}

# Stop managing something without destroying it.
removed {
  from = aws_iam_role.old_cluster_role
  lifecycle { destroy = false }
}
```

Generate config for an import with:

```bash
terraform plan -generate-config-out=generated.tf
```

The docs now actively steer you here: *"Instead of using the `terraform state rm` command, you can use `removed` blocks to remove resources"* ([`terraform state rm`](https://developer.hashicorp.com/terraform/cli/commands/state/rm)). Notably, the `terraform state mv` page does **not** carry the equivalent steer toward `moved`, and `terraform state replace-provider` has no block equivalent at all.

#### `lifecycle`

The full v1.16 argument set is `create_before_destroy`, `prevent_destroy`, `ignore_changes`, `replace_triggered_by`, `precondition`, `postcondition`, `action_trigger`, and `destroy` ([lifecycle](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle)).

```hcl
resource "aws_eks_cluster" "this" {
  # ...

  lifecycle {
    # Never let a plan destroy this. Terraform errors instead of planning.
    prevent_destroy = true

    # Do not diff on fields mutated by controllers or by humans out of band.
    ignore_changes = [
      tags["kubernetes.io/cluster/autoscaler"],
      vpc_config[0].security_group_ids,
    ]

    # Force replacement when an upstream marker changes, even though no
    # attribute of this resource changed.
    replace_triggered_by = [terraform_data.cell_generation]

    precondition {
      condition     = length(var.private_subnet_ids) >= 2
      error_message = "EKS requires subnets in at least two AZs."
    }
  }
}
```

Two caveats the docs call out and people miss:

- *"All `lifecycle` settings affect how Terraform constructs and traverses the dependency graph. As a result, only literal values can be used"* — you cannot compute `prevent_destroy` from a variable in Terraform (OpenTofu 1.12 added dynamic `prevent_destroy`; Terraform has not).
- *"Except for `create_before_destroy`, Terraform does not explicitly record a resource's `lifecycle` rule to state."* So removing `prevent_destroy` from config and re-planning immediately allows the destroy — it is a guard on the plan, not a property of the object.

`create_before_destroy` is the one that will bite you at cell scale: it is *contagious*. If resource A has it, every resource A depends on must effectively also create-before-destroy, and Terraform propagates it through the graph. Turning it on for a node group can silently change the replacement order of the whole VPC subtree. Name-collision failures (`AlreadyExists`) are the usual symptom, so pair it with a `random_id`/`name_prefix` naming scheme.

`destroy = false` is new in **1.16.0**: *"Set to `false` to remove a resource from state without destroying the actual infrastructure resource."* OpenTofu shipped the same thing in 1.12 (2026-05-14), so this is parity, not a Terraform first.

#### Provisioners, and why to avoid them

The current docs say *"You should exhaust all alternatives before using provisioners in your configurations"* and *"Terraform is primarily designed for immutable infrastructure operations, so we strongly recommend using purpose-built solutions to perform post-apply operations"* ([provisioners](https://developer.hashicorp.com/terraform/language/provisioners)). The famous "Provisioners are a Last Resort" heading was removed in 1.16 but the classic phrasing lives on at [the v1.11.x URL](https://developer.hashicorp.com/terraform/language/v1.11.x/resources/provisioners/syntax#provisioners-are-a-last-resort).

The engineering reason is sharper than "they're discouraged": **a provisioner's effect is not in state.** Terraform records that the resource exists; it records nothing about whether your `local-exec` succeeded or what it did. So a provisioner is invisible to plan, cannot be diffed, cannot be reconciled, and on failure taints the resource — forcing a full replace of the underlying infrastructure to retry a shell command. In a cell pipeline that means a failed `kubectl apply` provisioner replaces your EKS cluster.

The alternatives, in preference order: a real provider resource; an out-of-band step in your pipeline that runs *after* Terraform; a `terraform_data` resource with a `triggers_replace` to sequence things without shelling out; and only then a provisioner. Note the docs now point at [`terraform_data`](https://developer.hashicorp.com/terraform/language/resources/terraform-data) rather than `null_resource` for the "provisioner without a resource" pattern — `null_resource` is no longer referenced in 1.16 docs at all.

#### `templatefile` and encoding functions

```hcl
# Prefer this:
locals {
  cell_config = yamlencode({
    cellName = local.cell_name
    region   = var.region
    services = [for s in var.services : { name = s.name, replicas = s.replicas }]
  })
}

# Over hand-rolled string templating of structured data.
resource "local_file" "cell_config" {
  filename = "${path.module}/out/${local.cell_name}.yaml"
  content  = local.cell_config
}
```

HashiCorp's guidance on `templatefile` says the quiet part out loud: *"If the string you want to generate will be in JSON or YAML syntax, it's often tricky and tedious to write a template that will generate valid JSON or YAML that will be interpreted correctly when using lots of individual interpolation sequences and directives."* Instead: *"you can write a template that consists only of a single interpolated call to either `jsonencode` or `yamlencode`"* ([templatefile](https://developer.hashicorp.com/terraform/language/functions/templatefile#generating-json-or-yaml-from-a-template)). The recommended template filename extension is `.tftpl`.

`templatefile` is still the right tool for genuinely textual output — cloud-init, an nginx config, a shell bootstrap script:

```hcl
user_data = templatefile("${path.module}/templates/node-bootstrap.sh.tftpl", {
  cluster_name     = aws_eks_cluster.this.name
  cluster_endpoint = aws_eks_cluster.this.endpoint
  cluster_ca       = aws_eks_cluster.this.certificate_authority[0].data
  extra_kubelet_args = join(" ", var.kubelet_args)
})
```

Note the difference from `file()`: `templatefile` reads and renders at plan time, so the file must exist in the module. There is no way to template a file that Terraform itself will generate later in the same apply — another instance of the "known at plan time" rule.

### State management at scale

This is the section that determines whether a 300-cell platform works.

#### Backends and locking

| Backend | Locking mechanism | Extra infrastructure | Notes |
|---|---|---|---|
| `s3` | `use_lockfile = true` writes `<key>.tflock` in the same bucket | **None** since TF 1.10 | `dynamodb_table` deprecated in 1.11.0; both can be set simultaneously for migration ([s3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)) |
| `gcs` | Lock object `<prefix>/<name>.tflock`, created with a `DoesNotExist` precondition | None | Docs: *"This backend supports state locking"* ([gcs backend](https://developer.hashicorp.com/terraform/language/backend/gcs)) |
| `azurerm` | Blob lease (`LeaseDuration: -1`), lock info in blob metadata `terraformlockid` | None | *"supports state locking and consistency checking with Azure Blob Storage native capabilities"* ([azurerm backend](https://developer.hashicorp.com/terraform/language/backend/azurerm)) |
| `http` | Optional, via `lock_address`/`unlock_address` | Your server | The escape hatch for a bespoke state service |
| `remote` / `cloud` | Server-side | HCP Terraform / TFE | Also gives run history, RBAC, drift detection |

Note all three hyperscaler backends now do locking natively, in-bucket. The S3 lockfile needs `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject` on the `.tflock` key.

```hcl
terraform {
  required_version = "~> 1.16"

  backend "s3" {
    bucket       = "temporal-cloud-tfstate-us-east-1"
    key          = "cells/aws/us-east-1/cell-042/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    kms_key_id   = "arn:aws:kms:us-east-1:111122223333:key/…"
    use_lockfile = true

    # Cross-account: the state bucket lives in the platform account,
    # the cell lives elsewhere.
    assume_role = {
      role_arn = "arn:aws:iam::111122223333:role/TerraformStateAccess"
    }
  }

  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.62" }
  }
}
```

Note the `key` layout. Encode cloud, region, and cell identity into the state key path so that one bucket can hold the whole fleet with an obvious naming convention and IAM prefix policies per environment.

**Backend configuration cannot use variables in Terraform.** This is the single most annoying limitation for per-cell root modules and the reason `-backend-config` exists:

```bash
terraform init -backend-config=backends/aws-us-east-1-cell-042.hcl
```

OpenTofu removed this limitation in 1.8 — variables and locals work in backend config ([OpenTofu backend configuration](https://opentofu.org/docs/language/settings/backends/configuration/#variables-and-locals)). It is one of the few genuine reasons a platform team switches.

#### Workspaces vs directories

CLI workspaces give you multiple states behind one backend and one configuration. HashiCorp is direct about the limit: *"CLI workspaces within a working directory use the same backend, so they are not a suitable isolation mechanism for this scenario"* and *"Workspaces alone are not a suitable tool for system decomposition because each subsystem should have its own separate configuration and backend"* ([when not to use multiple workspaces](https://developer.hashicorp.com/terraform/cli/workspaces#when-not-to-use-multiple-workspaces)). The state docs add: *"Workspaces are not appropriate for system decomposition or deployments requiring separate credentials and access controls"* ([workspaces](https://developer.hashicorp.com/terraform/language/state/workspaces)).

For a multi-cloud cell fleet this is disqualifying. One backend means one bucket, one set of credentials, and one blast-radius domain across AWS, GCP, and Azure. Use directories (or generated root modules) per cell, with a distinct backend config each. Workspaces are fine for ephemeral dev/test variants of a single component.

#### `terraform_remote_state` vs data sources

`terraform_remote_state` reads another configuration's outputs from its state. It works, and it is a coupling you should minimize:

> *"Although `terraform_remote_state` only exposes output values, its user must have access to the entire state snapshot, which often includes some sensitive information."*
> *"…any user or server which has enough access to read the root module output values will also always have access to the full state snapshot data by direct network requests."*
> *"When possible, we recommend explicitly publishing data for external consumption to a separate location instead of accessing it via remote state."*

— [remote state data source](https://developer.hashicorp.com/terraform/language/state/remote-state-data)

For a cell platform the "separate location" is usually SSM Parameter Store / GCP Secret Manager / Azure App Configuration, or a small "cell registry" table. Layer N writes its outputs there; layer N+1 reads with a normal data source. You get per-key IAM, you decouple the state layout from the consumption contract, and you can reshape state later without breaking readers.

```hcl
# Publish, in the network layer.
resource "aws_ssm_parameter" "cell_vpc" {
  name = "/temporal/cells/${var.cell_name}/vpc"
  type = "String"
  value = jsonencode({
    vpc_id             = aws_vpc.this.id
    private_subnet_ids = aws_subnet.private[*].id
  })
}

# Consume, in the cluster layer — no state access required.
data "aws_ssm_parameter" "cell_vpc" {
  name = "/temporal/cells/${var.cell_name}/vpc"
}

locals {
  vpc = jsondecode(data.aws_ssm_parameter.cell_vpc.value)
}
```

#### State surgery

You will still need the CLI sometimes. The commands, with their docs:

```bash
# Inspect. -json landed in 1.16.
terraform state list
terraform state show -json aws_eks_cluster.this

# Move an address (prefer a `moved` block when the change is in config).
terraform state mv 'aws_eks_cluster.this' 'module.cp.aws_eks_cluster.this'

# Forget an object (prefer a `removed` block).
terraform state rm 'aws_iam_role.old'

# Repoint a provider after a fork/rename — no block equivalent exists.
terraform state replace-provider hashicorp/aws registry.opentofu.org/hashicorp/aws

# Pull/push for offline surgery. Always keep the original.
terraform state pull > backup-$(date +%s).tfstate
terraform state push fixed.tfstate
```

Rules I would enforce on a platform team:

1. Never run `state mv`/`rm` against shared state without a `state pull` backup committed to a ticket first.
2. Never run them from a laptop against production. Run them through the same pipeline that holds the lock.
3. Prefer `moved`/`removed`/`import` blocks so the change is reviewed in a PR and reproducible, then delete the blocks in a follow-up once every state has converged. Leaving `moved` blocks in place forever is harmless but noisy; leaving `import` blocks is worse, because they re-plan an import every run.

#### Blast radius and state splitting for hundreds of cells

The decision is a spectrum:

| Layout | Plan time | Blast radius | Cross-cell change cost | Verdict |
|---|---|---|---|---|
| One state, all cells | Minutes-to-hours; unusable past ~50 cells | Entire fleet | One PR, one apply | Never |
| One state per cloud/region | Tens of seconds | Every cell in the region | One apply per region | Acceptable for shared regional infra only |
| **One state per cell** | Seconds | One cell | 300 applies, orchestrated | **This is the answer** |
| One state per cell per layer (network / cluster / addons) | Seconds | One layer of one cell | 900 applies | Right when layers have different change cadence or different owners |

For cell lifecycle, per-cell state is not a preference, it is a requirement: provision, upgrade, and teardown are per-cell operations, and they must be independently lockable so two cells can be upgraded concurrently. The layered split matters when networking is shared with another team — separate state means separate ownership boundaries and separate IAM, and the interface between them becomes an explicit published contract rather than a shared state file.

The second-order consequence: with 300 states you now need an orchestrator that knows which cells exist, what version each is on, and in what order to touch them. That inventory is not Terraform's job. Options are Terragrunt, HCP Stacks, a hand-rolled matrix in CI, or — given where you work — a Temporal workflow driving `terraform` per cell with retries, concurrency limits, and human approval gates. That last option is the one worth arguing for internally, because rollout orchestration with durable state and human-in-the-loop is exactly the problem Temporal solves and exactly the problem Terraform does not.

### Module design

#### Composition over inheritance, thin root modules

The rule from [module composition](https://developer.hashicorp.com/terraform/language/modules/develop/composition): modules should be flat and composed by the root, not deeply nested and parameterized. A root module for a cell should read like a wiring diagram:

```hcl
# cells/aws/us-east-1/cell-042/main.tf  — a thin root module
module "network" {
  source = "git::ssh://git@github.com/temporalio/tf-modules.git//aws/cell-network?ref=v4.2.0"

  cell_name   = local.cell.name
  region      = local.cell.region
  cidr_block  = local.cell.networking.vpc_cidr
}

module "cluster" {
  source = "git::ssh://git@github.com/temporalio/tf-modules.git//aws/cell-cluster?ref=v4.2.0"

  cell_name          = local.cell.name
  kubernetes_version = local.cell.kubernetes_version
  private_subnet_ids = module.network.private_subnet_ids
  vpc_id             = module.network.vpc_id
}
```

Three properties to hold:

- **The root module contains no `resource` blocks**, only `module`, `locals`, `provider`, `terraform`, and `output`. If a resource creeps into a root module it will not exist in any other cell, and you have just created a snowflake.
- **Modules do not call `terraform_remote_state`.** Data crosses module boundaries as inputs and outputs, resolved by the root. A module that reads remote state cannot be tested and cannot be reused in a different state layout.
- **Nesting depth of two, maximum.** Root → component module → (optionally) a small shared helper. Beyond that, changing a leaf input means threading a variable through four `variables.tf` files, and nobody does it correctly under time pressure.

#### Providers in modules — the `configuration_aliases` rule

This is the rule that most often surprises people writing their first shared module. Verbatim:

> *"A module intended to be called by one or more other modules must not contain any `provider` blocks."*
> *"Provider configurations can be defined only in a root Terraform module."*

— [providers within modules](https://developer.hashicorp.com/terraform/language/modules/develop/providers)

The reason is destruction ordering: if a module declares its own provider, Terraform cannot destroy the module's resources after the module is removed from configuration, because the provider configuration went away with it. So the module would be un-removable.

A module that needs *multiple* configurations of the same provider — the classic multi-region or multi-account case — declares `configuration_aliases` (added in 0.15.0) and the root passes them in:

```hcl
# modules/aws/cell-replicated/versions.tf
terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 6.62"
      configuration_aliases = [aws.primary, aws.replica]
    }
  }
}

# Inside the module, select explicitly.
resource "aws_s3_bucket" "primary" {
  provider = aws.primary
  bucket   = "${var.cell_name}-primary"
}

resource "aws_s3_bucket" "replica" {
  provider = aws.replica
  bucket   = "${var.cell_name}-replica"
}
```

```hcl
# Root module wires them.
provider "aws" {
  alias  = "use1"
  region = "us-east-1"
  assume_role { role_arn = var.cell_role_arn }
}

provider "aws" {
  alias  = "usw2"
  region = "us-west-2"
  assume_role { role_arn = var.cell_role_arn }
}

module "cell" {
  source = "../../modules/aws/cell-replicated"
  providers = {
    aws.primary = aws.use1
    aws.replica = aws.usw2
  }
}
```

Two consequences for a many-cell platform. First, **provider configurations do not `for_each`** in Terraform — you cannot generate N provider blocks for N regions from a map. You must write them out, or generate the root module file. (OpenTofu added provider `for_each` in 1.9, which is arguably its most valuable divergence for exactly this use case: [provider `for_each`](https://opentofu.org/docs/language/providers/configuration/#for_each-multiple-instances-of-a-provider-configuration).) Second, this is precisely the gap Terragrunt and Stacks fill by generating provider blocks or supporting `for_each` on components.

#### Versioning and registries

Pin module versions. For a private setup you have three options: the HCP Terraform private registry, a Git ref, or an OCI artifact (OpenTofu only). Git refs are the pragmatic default for an internal platform:

```hcl
source = "git::ssh://git@github.com/temporalio/tf-modules.git//aws/cell-cluster?ref=v4.2.0"
```

Note the `//` separating repo from subdirectory and the `?ref=` pinning to a tag. **The `version` argument works only for registry modules**, not Git or local sources — a common source of "why isn't my version constraint doing anything."

Also note: `.terraform.lock.hcl` **tracks providers only, not module versions** ([dependency lock file](https://developer.hashicorp.com/terraform/language/files/dependency-lock)). Module reproducibility is entirely on your `ref=` discipline, so use immutable tags, never branches.

Terraform 1.15 added variables and locals in `source` and `version`, which finally lets you parameterize the module version per cell — very useful for staged rollouts of a module upgrade:

```hcl
module "cluster" {
  source  = "app.terraform.io/temporal/cell-cluster/aws"
  version = var.cell.module_versions.cluster   # 1.15+
  # ...
}
```

#### Testing

`terraform test` went GA in **1.6.0**, with provider and module mocking in **1.7.0**. Tests live in `.tftest.hcl` files, by default in a `tests/` directory ([tests](https://developer.hashicorp.com/terraform/language/tests)).

```hcl
# tests/naming.tftest.hcl — a plan-only unit test, no cloud calls
mock_provider "aws" {}

variables {
  cell = {
    name   = "cell-042"
    cloud  = "aws"
    region = "us-east-1"
  }
}

run "cell_name_is_propagated_to_tags" {
  command = plan

  assert {
    condition     = aws_eks_cluster.this.tags["example.com/cell"] == "cell-042"
    error_message = "cell tag not propagated"
  }
}

run "rejects_bad_cloud" {
  command = plan
  variables {
    cell = { name = "cell-042", cloud = "digitalocean", region = "nyc3" }
  }
  expect_failures = [var.cell]
}
```

By default each `run` block executes `command = apply` and runs sequentially. For a module you will want `command = plan` plus `mock_provider` for the fast unit tests, and a much smaller set of real-apply integration tests gated behind a nightly job with real credentials.

Where Terratest still earns its place: it is Go, so it can assert on things outside Terraform's world — hit the cluster API after apply, run `kubectl`, poll for a healthy Temporal frontend, then destroy ([Terratest](https://terratest.gruntwork.io/)). For a cell module, the right split is `terraform test` for configuration logic (naming, tagging, validation, conditional resources) and Terratest for "does a provisioned cell actually work."

### Multi-cloud, multi-region, multi-account

#### Cross-account and cross-project authentication

```hcl
# AWS: assume-role, including role chaining (multiple blocks, in order).
provider "aws" {
  alias  = "cell"
  region = var.region

  assume_role {
    role_arn     = "arn:aws:iam::${var.cell_account_id}:role/CellProvisioner"
    session_name = "tf-${var.cell_name}"
    external_id  = var.external_id
  }

  # Guardrail: fail fast if credentials resolve to the wrong account.
  allowed_account_ids = [var.cell_account_id]

  default_tags { tags = local.common_tags }
}

# AWS from CI with OIDC, no long-lived keys.
provider "aws" {
  alias  = "cell_oidc"
  region = var.region
  assume_role_with_web_identity {
    role_arn                = var.ci_role_arn
    web_identity_token_file = "/var/run/secrets/oidc/token"
    session_name            = "tf-${var.cell_name}"
  }
}

# GCP: service account impersonation. Requires
# roles/iam.serviceAccountTokenCreator on the target SA.
provider "google" {
  alias                       = "cell"
  project                     = var.gcp_project_id
  region                      = var.region
  impersonate_service_account = "cell-provisioner@${var.gcp_project_id}.iam.gserviceaccount.com"
}

# Azure: one provider per subscription; OIDC from CI.
provider "azurerm" {
  alias           = "cell"
  subscription_id = var.azure_subscription_id
  tenant_id       = var.azure_tenant_id
  use_oidc        = true
  features {}
}
```

References: [AWS provider `assume_role`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs#assume_role-configuration-block) (current v6.62.0), [Google provider reference](https://registry.terraform.io/providers/hashicorp/google/latest/docs/guides/provider_reference) (v8.0.0), [AzureRM provider](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs) (v5.3.0). AzureRM notes that `subscription_id` *"is required when performing a plan or apply operation, but is not required to run `terraform validate`"* — useful for CI validation stages without credentials. Azure has no dedicated multi-subscription guide; provider aliases are the documented approach, plus `auxiliary_tenant_ids` (max 3) for cross-tenant.

#### Generating per-cell root modules

With no provider `for_each` and no variables in backend config, the honest answer for N nearly-identical cells is that *something* generates the root module. Your realistic options:

**1. Generate `.tf` files from a cell inventory.** A small program (Go, since you're a Go shop) reads a YAML/JSON fleet registry and emits `cells/<cloud>/<region>/<cell>/main.tf` + `backend.hcl`. Committed, reviewable, greppable. Downside: generated code in Git, and a second tool to maintain.

**2. Terragrunt.** Its entire reason for existing is DRY root modules — it generates the backend block and provider blocks and calls one module per "unit." Current version **1.1.4 (2026-08-27)**; it hit 1.0 on 2026-03-30 with an explicit backwards-compatibility guarantee ([Terragrunt 1.0](https://www.gruntwork.io/blog/terragrunt-1-0-released)). Positioning has shifted: *"Terragrunt is a flexible orchestration tool that allows Infrastructure as Code written in OpenTofu/Terraform to scale."* Two things to know: it now defaults to **OpenTofu** if `tofu` is on `PATH`, and it has its own Stacks concept — `terragrunt.stack.hcl` with `unit` and `stack` blocks, stable since v0.78.0 ([Terragrunt Stacks](https://docs.terragrunt.com/features/stacks/explicit/)). Docs moved to `docs.terragrunt.com`.

**3. HCP Terraform Stacks.** GA since 2025-09-25. A `.tfcomponent.hcl` file declares `component` blocks (which *do* support `for_each`, and whose providers *do* support `for_each`), and a `.tfdeploy.hcl` declares `deployment` blocks — one per cell. Deferred changes are GA here, so a cluster and its Kubernetes resources genuinely can live in one Stack. **But**: it requires HCP Terraform or TFE 2.0+, the `orchestrate` block was deprecated at GA in favor of `deployment_group`, and there are hard limits of 500 deployments, 100 components, and 10,000 resources per Stack ([Stacks overview](https://developer.hashicorp.com/terraform/language/stacks), [beta→GA changes](https://developer.hashicorp.com/terraform/language/stacks/update-GA)). 500 deployments is a real ceiling for a cell fleet.

**4. Your CI matrix.** A single parameterized root module, `terraform init -backend-config=...` per cell, driven by a job matrix. Zero extra tools. Loses you a dependency graph across cells and any notion of ordering.

#### The tooling landscape, honestly

| Tool | Model | Best at | Real cost | Verdict for cell lifecycle |
|---|---|---|---|---|
| **Terraform (plain)** | HCL, per-directory state | Everything; universal knowledge | No fleet orchestration, no provider `for_each`, no vars in backend config | The substrate. You will use it regardless |
| **OpenTofu** | Fork of Terraform | State encryption, provider `for_each`, vars in backend config, `-exclude`, OCI registries | Provider registry redirection; smaller (but real) ecosystem; some org-level license politics | Strong candidate. The provider `for_each` + backend-vars combination directly removes two of your biggest per-cell pain points |
| **Terragrunt** | Wrapper that generates + orchestrates | DRY root modules, dependency graphs across units, `run-all` | A second DSL; error messages get one layer removed from Terraform's; commercial "Scale" tier for the good CI bits | The pragmatic answer for N cells if you are not on HCP |
| **HCP Terraform Stacks** | First-class multi-component + multi-deployment | `for_each` on components *and* providers, deferred changes, one plan across a dependency graph | **HCP/TFE only**, 500-deployment ceiling, vendor lock, GA-broke-beta-configs precedent | Only if you are already on HCP and under 500 cells |
| **CDKTF** | TypeScript/Python → HCL JSON | — | **Deprecated and archived 2025-12-10** | Do not use |
| **Pulumi** | Real languages, own engine, own state | Programmatic generation of N stacks; genuine loops and abstractions; `pulumi package add hcl module` runs TF modules | Whole-ecosystem switch; your providers are TF providers bridged anyway; team retraining | Not worth it if the team knows HCL; genuinely good if the fleet logic is complex enough to want a real language ([Pulumi](https://www.pulumi.com/docs/)) |
| **Crossplane** | Kubernetes controllers reconciling cloud resources | Continuous reconciliation, no state file, self-service APIs via XRs | Cloud infra now depends on a management cluster; v2 removed claims and native patch-and-transform, so v1 material is misleading | Complementary, not a replacement. A management cluster provisioning cells is a real pattern; but you need Terraform to build the management cluster ([Crossplane v2](https://docs.crossplane.io/latest/whats-new/), [CNCF graduated 2025-10-28](https://www.cncf.io/projects/crossplane/)) |

One note on Crossplane since it comes up constantly: **v2 is a big break from everything written before August 2025.** Composite resources are namespaced by default, *claims are gone* (*"The new namespaced and cluster scoped XRs in Crossplane v2 don't support claims"*), and native patch-and-transform composition was removed in favor of function pipelines. Current release is v2.4.0 (2026-08-20). Any tutorial predating v2.0 (2025-08-12) teaches an API that no longer exists.

*See also: [resource hierarchy and tenancy](03-multicloud-aws-gcp-azure.md#resource-hierarchy-and-tenancy) and [quotas, limits, and regional capacity](03-multicloud-aws-gcp-azure.md#quotas-limits-and-regional-capacity) — the account/project/subscription boundary you pick there is what fixes your state split here, and the per-region quotas are what a fleet apply actually fails on.*

### Kubernetes and Terraform: where to stop

This is the most consequential architectural decision in the guide, and I will be opinionated: **stop Terraform at the cluster boundary.**

The mechanical reason is the provider-configuration rule. Terraform's `kubernetes` provider needs an endpoint and credentials; if those come from a cluster resource in the same configuration, they are unknown at plan time. HashiCorp's own provider docs contain the warning in capital letters:

> **"WARNING** When using interpolation to pass credentials to the Kubernetes provider from other resources, these resources SHOULD NOT be created in the same Terraform module where Kubernetes provider resources are also used."
>
> *"The most reliable way to configure the Kubernetes provider is to ensure that the cluster itself and the Kubernetes provider resources can be managed with separate `apply` operations."*

— [hashicorp/kubernetes provider](https://registry.terraform.io/providers/hashicorp/kubernetes/latest/docs)

`kubernetes_manifest` makes it worse, because it reads the cluster's OpenAPI schema during planning: *"This resource requires API access during planning time. This means the cluster has to be accessible at plan time and thus cannot be created in the same apply operation"* ([kubernetes_manifest](https://registry.terraform.io/providers/hashicorp/kubernetes/latest/docs/resources/manifest)). A destroyed or unreachable cell therefore cannot even be *planned*, which turns teardown into a puzzle.

The design reason is stronger than the mechanical one. Kubernetes objects want continuous reconciliation, drift correction, pruning by ownership, and rollout semantics with health gates. Terraform gives you point-in-time convergence with a lock, no health awareness, and a destroy path that deletes things in dependency order and then waits. Those are different tools for different jobs. GitOps controllers (Argo CD, Flux) do the second job natively.

The line I would draw for a cell:

```text
Terraform owns:                          GitOps owns:
  VPC / subnets / peering                  every namespace
  IAM roles, workload identity             every Deployment / StatefulSet
  the managed cluster itself               CRDs and their controllers
  node pools (or Karpenter's IAM)          cert-manager, Kyverno, the mesh
  KMS keys, buckets, databases             Temporal server components
  DNS zones, load balancer prereqs         HPA/PDB/NetworkPolicy
  the *bootstrap* GitOps agent  ───────►   ...and then everything else
```

The one Kubernetes object Terraform should create is the bootstrap: install the Argo CD or Flux agent and register the cell with the fleet's config repo. After that, Terraform's job is done and reconciliation takes over. This keeps the `kubernetes` provider usage to a handful of objects created immediately after cluster creation, in a *separate* apply stage from the cluster itself.

If you must manage more Kubernetes from Terraform, know the provider landscape as of today:

| Provider | Version | Notes |
|---|---|---|
| `hashicorp/kubernetes` | v3.2.1 (2026-07-01) | v3.0.0 (2025-12-03) moved to protocol v6, requires Terraform ≥1.0, deprecated all non-`_v1` resources. `kubernetes_manifest` uses SSA. `kubernetes_resource`/`kubernetes_resources` are **data sources only** |
| `hashicorp/helm` | v3.2.0 (2026-06-04) | **Embeds Helm 3.18.5, not Helm 4** (verified in `go.mod`). v3 reshaped `kubernetes {}` → `kubernetes = {}`, `registry {}` → `registries = [...]`, and `set` into a list of objects. `set_wo` write-only support added in 3.0.0 |
| `alekc/kubectl` | 2.4.1 stable, 3.0.0-beta3 | The maintained fork; v3 adds an ephemeral `kubectl_manifest` and `lazy_load` to sidestep plan-time cluster access |
| `gavinbunney/kubectl` | 1.19.0 (2025-01-10) | Dormant (not archived). Do not start new work here |

The provider-level `ignore_annotations` / `ignore_labels` regex settings on the `kubernetes` provider are worth knowing — they suppress diffs on metadata that controllers mutate, which is otherwise a permanent source of phantom plans.

*See also: [bootstrap: the chicken-and-egg](13-gitops-argocd-flux.md#bootstrap-the-chicken-and-egg) for the mechanics on the other side of that handoff — `flux bootstrap` self-managing by construction, Argo CD's app-of-apps-on-itself, and why keeping the bootstrap apply path alive is the recovery route.*

### CI/CD

The canonical shape:

```yaml
# .github/workflows/cell.yml (abridged)
jobs:
  plan:
    permissions: { id-token: write, contents: read, pull-requests: write }
    strategy:
      matrix: { cell: ${{ fromJSON(needs.discover.outputs.cells) }} }
      max-parallel: 10
    steps:
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init -backend-config=cells/${{ matrix.cell }}/backend.hcl
      - run: terraform validate
      - id: plan
        run: |
          terraform plan -out=tfplan -detailed-exitcode -lock-timeout=5m
          echo "exit=$?" >> "$GITHUB_OUTPUT"
        continue-on-error: true
      - run: terraform show -json tfplan > tfplan.json
      # Policy gate on the machine-readable plan, not on the HCL.
      - run: conftest test --policy policy/ tfplan.json
      - run: trivy config --exit-code 1 --severity HIGH,CRITICAL .
      - uses: actions/upload-artifact@v4
        with: { name: tfplan-${{ matrix.cell }}, path: tfplan }

  apply:
    needs: plan
    if: github.ref == 'refs/heads/main'
    environment: production        # human approval gate
    steps:
      - uses: actions/download-artifact@v4
      - run: terraform init -backend-config=cells/${{ matrix.cell }}/backend.hcl
      - run: terraform apply -lock-timeout=10m tfplan
```

Points that matter:

- **Apply the saved plan, not a fresh one.** *"When you pass a saved plan file to `terraform apply`, Terraform performs the operations in the saved plan without prompting you for confirmation"* ([apply](https://developer.hashicorp.com/terraform/cli/commands/apply#saved-plan-mode)). This is what makes the PR review meaningful. Store the artifact with the same access controls as state — it contains input variables in cleartext.
- **Gate on plan JSON, not HCL.** `terraform show -json tfplan` produces the documented [JSON output format](https://developer.hashicorp.com/terraform/internals/json-format). Policy over the *plan* catches "this apply will delete a production cell," which no static HCL scanner can see. Careful with `format_version`: the docs prose says `"1.0"` but current source declares `FormatVersion = "1.2"` — read the field, don't hardcode a comparison.
- **`-refresh=false` is a scale optimization with teeth.** It skips the provider read for every resource, which on a 500-resource cell is the difference between 90 seconds and 8. The docs warn: *"setting `refresh=false` causes Terraform to ignore external changes, which could result in an incomplete or incorrect plan."* My rule: `-refresh=false` for the fast PR plan, full refresh on the apply-path plan and on the scheduled drift job.
- **`-target` is a smell.** *"Use `-target=ADDRESS` in exceptional circumstances only, such as recovering from mistakes or working around Terraform limitations"* ([plan `-target`](https://developer.hashicorp.com/terraform/cli/commands/plan#target-address)). The longer form names the real cost: *"this can lead to undetected configuration drift and confusion about how the true state of resources relates to configuration."* If your pipeline routinely uses `-target`, you have a state-splitting problem, not a targeting need. Split the state.
- **`taint` is deprecated** in favor of `-replace=ADDRESS` on plan/apply ([taint](https://developer.hashicorp.com/terraform/cli/commands/taint)).
- **Drift on a schedule.** A nightly job per cell: `terraform plan -detailed-exitcode -lock-timeout=0`; exit 2 opens a ticket with the plan attached. HCP Terraform has this built in as health assessments, available on **Standard and Premium** editions, requiring remote/agent execution and at least one successful apply ([workspace health](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/health#drift-detection)). Note their caveat: *"Configuration drift differs from state drift. Drift detection does not detect state drift."*

**Policy-as-code options today:**

| Tool | Where it runs | Input | Notes |
|---|---|---|---|
| [Sentinel](https://developer.hashicorp.com/sentinel) | HCP Terraform / TFE runs | Plan, config, state, run data | Enforcement is an HCP/TFE feature; the standalone CLI only tests against mocks. Free tier: one policy set, five policies |
| [Terraform Policy](https://developer.hashicorp.com/terraform/policy) (**beta**) | `tfpolicy` CLI locally + HCP at run time | Provider-aware HCL policy | HashiCorp's stated direction: *"We recommend using Terraform policy to enforce governance in Terraform workflows."* Requires Terraform 1.16+. Beta — do not put it in a production gate yet |
| [Conftest](https://www.conftest.dev/) / OPA | Anywhere, including your own CI | `tfplan.json` | v0.69.0 (2026-08-03). The portable choice; works identically for Terraform, Kubernetes, and Helm output |
| [Checkov](https://www.checkov.io/) | CI | HCL *and* plan JSON | v3.3.15 (2026-08-27). Bridgecrew, now Palo Alto / Prisma Cloud. Large built-in rule set |
| [Trivy](https://trivy.dev/docs/latest/scanner/misconfiguration/) | CI | HCL | The successor to tfsec; `trivy config <dir>` |

For a multi-cloud platform I would run Conftest over plan JSON as the *blocking* gate (because your invariants are org-specific: cell naming, mandatory tags, no public endpoints, no destroy of tier-1 cells) and Trivy or Checkov as an advisory scan for CVE-class misconfigurations.

### The OpenTofu fork

**What happened.** HashiCorp relicensed from MPL 2.0 to the Business Source License 1.1 on 2023-08-10 ([announcement](https://www.hashicorp.com/en/blog/hashicorp-adopts-business-source-license)). Terraform 1.6.0 was the first BUSL release; the [LICENSE file](https://github.com/hashicorp/terraform/blob/main/LICENSE) covers *"Terraform Version 1.6.0 or later"*, with a four-year Change Date after which each version reverts to MPL 2.0. The Licensor is now **International Business Machines Corporation** — IBM completed its acquisition of HashiCorp on 2025-02-27, and hashicorp.com now brands as "HashiCorp, an IBM company." No further licensing change has occurred since.

**What BUSL means for you.** It forbids offering the software as a competing commercial hosted service. Internal use by a company building its own product is fine. But: your legal team will ask, your vendors will ask, and if Temporal Cloud ever bundles infrastructure-provisioning-as-a-service the question stops being academic. Read [the license FAQ](https://www.hashicorp.com/license-faq) rather than trusting a summary.

**OpenTofu today.** Latest stable **1.12.6 (2026-08-19)**, with 1.13 in beta. Governed under the Linux Foundation since [2023-09-20](https://www.linuxfoundation.org/press/announcing-opentofu). It remains a drop-in replacement — same HCL, same providers, state compatible.

**Feature divergence, verified:**

| Capability | OpenTofu | Terraform 1.16 | Why a cell platform cares |
|---|---|---|---|
| [Client-side state encryption](https://opentofu.org/docs/language/state/encryption/) | **1.7** (external key providers in 1.10) | Not available | Directly solves the plaintext-secrets-in-state problem, with your own KMS |
| [Provider `for_each`](https://opentofu.org/docs/language/providers/configuration/#for_each-multiple-instances-of-a-provider-configuration) | **1.9** | Not available | Generate N region/account provider configs from a map — exactly the N-cells problem |
| Variables/locals in **backend** config | **1.8** | Not available | Removes the `-backend-config` file generation step per cell |
| Variables/locals in module `source`/`version` | 1.8 | **1.15** — parity | — |
| [`-exclude` flag](https://opentofu.org/docs/v1.9/intro/whats-new/#the-exclude-flag) (+ `-target-file`/`-exclude-file` in 1.10) | **1.9** | Not available | "Apply everything except this one broken resource" — the inverse of `-target`, and less dangerous |
| [OCI registries for providers and modules](https://opentofu.org/docs/cli/oci_registries/) | **1.10** | Not available | One artifact store for charts, images, and modules |
| `lifecycle { enabled = ... }`, dynamic `prevent_destroy` | 1.11 / 1.12 | Not available | Per-cell protection driven by cell tier |
| `mock_provider` / `override_*` in tests | 1.8 | **1.7** — Terraform first | Not an OpenTofu differentiator; the common claim is backwards |
| `deprecated` on variables/outputs | 1.10 | **1.15** — parity | — |
| `lifecycle { destroy = false }` | 1.12 | **1.16** — parity | — |
| Stacks | Not available | Terraform + **HCP/TFE only** | Not usable with the OSS CLI either way |

**What a platform team should actually conclude.** The divergence is now real but narrow, and it runs in OpenTofu's favor on the specific axes a many-cell platform cares about: state encryption, provider `for_each`, and backend-config variables. Terraform's counterweights are Stacks (HCP-gated), the enormous default-choice gravity of the registry and every tutorial, and IBM's investment in the ecosystem. Migration is genuinely low-risk today — `tofu` reads Terraform state, the module code is unchanged, and the main operational work is repointing the provider registry and updating CI. The thing that will actually decide it at your company is not features: it is whether legal has an opinion about BUSL and whether your vendors' modules are published to the Terraform registry, the OpenTofu registry, or both.

### Performance at scale

At 300 cells the numbers that matter are per-cell plan time and total wall-clock across the fleet.

- **`-parallelism`** defaults to 10. Raising it helps CPU-light, API-latency-bound plans; it also raises your cloud API rate-limit exposure, and rate-limit errors during apply are far worse than a slow apply. Measure before raising.
- **Provider plugin cache.** Set `TF_PLUGIN_CACHE_DIR` or `plugin_cache_dir` in `.terraformrc` so `terraform init` hardlinks instead of re-downloading 600 MB of AWS provider per cell. Two documented gotchas: *"This directory must already exist before Terraform will cache plugins; Terraform will not create the directory itself"* and *"The plugin cache directory is not guaranteed to be concurrency safe"* ([provider plugin cache](https://developer.hashicorp.com/terraform/cli/config/config-file#provider-plugin-cache)). That second one matters if your CI runs 20 cells in parallel on one runner with a shared cache volume — use per-job caches or a mirror instead.
- **Provider mirrors.** For hermetic, fast, egress-free builds, run a `network_mirror` or bake a `filesystem_mirror` into your CI image ([provider installation](https://developer.hashicorp.com/terraform/cli/config/config-file#provider-installation)). Build it with `terraform providers mirror -platform=linux_amd64 ./mirror`.
- **Lock file, all platforms.** If engineers are on macOS ARM and CI is Linux AMD64, `terraform init` on one platform writes hashes only for that platform and CI then fails. Fix once: `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64`.
- **Huge-plan slowness is a state-size problem.** Plan time scales with the number of resources in state, because every one is refreshed. The mitigations, in order of effectiveness: split state (biggest win by far), `-refresh=false` on non-authoritative plans, and reducing resource count per cell (one `aws_iam_role_policy` with a document beats twelve `aws_iam_role_policy_attachment`s).
- **`terraform init` is the hidden cost in CI.** Module downloads over SSH plus provider resolution can exceed plan time. Cache `.terraform/` keyed on a hash of `.terraform.lock.hcl` plus module refs.

### Secrets in Terraform

The uncomfortable truth, stated by HashiCorp:

> *"If you are developing with Terraform locally, Terraform stores your state in a plaintext file, which includes any secret values you defined in your configuration."*
> *"Terraform stores values with the `sensitive` argument in both state and plan files, and anyone who can access those files can access your sensitive values."*

— [Manage sensitive data](https://developer.hashicorp.com/terraform/language/manage-sensitive-data)

`sensitive = true` redacts from CLI output only. The docs are explicit for both directions: *"Terraform still stores the values of sensitive variables in your state"* and *"If you use the `terraform output` CLI command with the `-json` or `-raw` flags, Terraform displays sensitive outputs in plain text."*

The mitigation stack, in order:

1. **Encrypt the state store and lock down access.** SSE-KMS on the S3 bucket with a CMK, bucket policy denying unencrypted `PutObject`, versioning on, access logging on, and IAM that grants state access only to the pipeline role. This is table stakes and it is not sufficient.
2. **Do not put secrets in state at all.** Prefer resources that generate secrets in the cloud and expose only ARNs — an `aws_secretsmanager_secret` whose value is written by a separate process, a `google_secret_manager_secret` version created out of band.
3. **Ephemeral values and write-only arguments.** These are the real fix and they are recent. **Terraform 1.10.0** added ephemeral resources (the [`ephemeral` block](https://developer.hashicorp.com/terraform/language/block/ephemeral)) and `ephemeral = true` on variables and outputs — values that are re-read each phase and *never persisted to state*. **Terraform 1.11.0** added [write-only arguments](https://developer.hashicorp.com/terraform/language/manage-sensitive-data/write-only) (`*_wo` with a `*_wo_version` companion), which send a value to the provider without storing it.

```hcl
# Fetch a secret at apply time, never persist it.
ephemeral "vault_kv_secret_v2" "cell_db" {
  mount = "secret"
  name  = "cells/${var.cell_name}/database"
}

resource "aws_db_instance" "cell" {
  identifier = local.cell_name
  # Write-only: sent to the provider, not stored in state (Terraform 1.11+).
  password_wo         = ephemeral.vault_kv_secret_v2.cell_db.data.password
  password_wo_version = var.db_password_version   # bump to trigger a rotation
}
```

The `_wo_version` companion exists because *"Terraform does not store write-only arguments in state files, so Terraform has no way of knowing if a write-only argument value has changed"* — you increment the version to signal a change. The [Vault provider](https://registry.terraform.io/providers/hashicorp/vault/latest/docs) (v5.11.0, 2026-08-14) ships **24 ephemeral resources** including `vault_kv_secret_v2`, `vault_aws_access_credentials`, `vault_database_secret`, and `vault_kubernetes_credentials`. Note that `vault_kubernetes_service_account_token` is a *data source*, not ephemeral — the ephemeral equivalent is `vault_kubernetes_credentials`.

4. **If you need encryption at rest that you control, that is OpenTofu's state encryption**, which is the strongest single argument for the fork on a security-conscious platform team.

*See also: [Terraform's Vault provider and the state problem](10-vault.md#terraforms-vault-provider-and-the-state-problem) for the Vault-side view of the same hazard, and why "configure Vault with Terraform" and "read secrets with Terraform" are different decisions with different blast radii.*

---

## Hands-on

**Cost.** Labs 1 through 5 use only the `local`, `random`, `null`, and `terraform` built-in providers plus LocalStack, and cost **$0**. Lab 6 uses AWS free-tier-eligible resources (S3, DynamoDB-free since we use native locking, IAM) and should cost **under $0.10/month** if you destroy afterward — but S3 requests and KMS keys are not free, so run `terraform destroy` and verify. I flag the costed step explicitly.

### Setup

```bash
# macOS. Terraform is not in homebrew-core -- it was disabled there when the
# licence changed to BUSL -- so it comes from HashiCorp's own tap.
brew install hashicorp/tap/terraform
brew install opentofu tflint

# Verify — you should see 1.16.x
terraform version
tofu version

mkdir -p ~/tf-lab && cd ~/tf-lab
```

### Lab 1 — Reproduce the `count` index-shift disaster

This is the single most valuable 15 minutes in the guide. You will watch Terraform destroy and recreate resources it had no business touching.

```bash
mkdir -p 01-index-shift && cd 01-index-shift
```

```hcl
# main.tf
terraform {
  required_providers {
    local  = { source = "hashicorp/local",  version = "~> 2.5" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

variable "cells" {
  type    = list(string)
  default = ["cell-a", "cell-b", "cell-c", "cell-d"]
}

# The wrong way: positional identity.
resource "random_pet" "by_count" {
  count  = length(var.cells)
  prefix = var.cells[count.index]
}

# The right way: keyed identity.
resource "random_pet" "by_foreach" {
  for_each = toset(var.cells)
  prefix   = each.key
}

output "count_ids"   { value = [for r in random_pet.by_count : r.id] }
output "foreach_ids" { value = { for k, r in random_pet.by_foreach : k => r.id } }
```

```bash
terraform init && terraform apply -auto-approve
terraform state list      # note the addresses: [0]..[3] vs ["cell-a"]..["cell-d"]

# Now remove cell-b from the middle.
terraform plan -var='cells=["cell-a","cell-c","cell-d"]'
```

Read the plan carefully. The `for_each` resources show **1 to destroy**. The `count` resources show **2 to change (replace), 1 to destroy** — because `by_count[1]` must become `cell-c` and `by_count[2]` must become `cell-d`. Now imagine those are EKS clusters.

```bash
# Prove the fix is a state edit, not an infrastructure change:
terraform apply -var='cells=["cell-a","cell-c","cell-d"]' -auto-approve
terraform state list
```

Extension: add a `moved` block converting a `count` resource to `for_each` and confirm the plan becomes a no-op.

### Lab 2 — Module interfaces, `optional()`, and validation

```bash
cd ~/tf-lab && mkdir -p 02-modules/modules/cell && cd 02-modules
```

```hcl
# modules/cell/main.tf
terraform {
  required_providers { local = { source = "hashicorp/local", version = "~> 2.5" } }
}

variable "cell" {
  type = object({
    name    = string
    tier    = optional(string, "standard")
    regions = optional(list(string), ["us-east-1"])
    limits  = optional(object({
      max_namespaces = optional(number, 100)
      max_pods       = optional(number, 5000)
    }), {})
  })
  validation {
    condition     = contains(["standard", "dedicated", "isolated"], var.cell.tier)
    error_message = "cell.tier must be standard, dedicated, or isolated."
  }
}

resource "local_file" "manifest" {
  filename = "${path.root}/out/${var.cell.name}.yaml"
  content  = yamlencode({
    cell = var.cell.name, tier = var.cell.tier,
    limits = var.cell.limits, regions = var.cell.regions
  })
}
```

```hcl
# main.tf (root)
locals {
  fleet = {
    "cell-001" = { name = "cell-001" }                                # all defaults
    "cell-002" = { name = "cell-002", tier = "dedicated" }
    "cell-003" = { name = "cell-003", tier = "isolated",
                   limits = { max_namespaces = 10 } }                 # partial nested
  }
}

module "cells" {
  source   = "./modules/cell"
  for_each = local.fleet
  cell     = each.value
}
```

```bash
terraform init && terraform apply -auto-approve
cat out/*.yaml
```

Now break it on purpose — set `tier = "gold"` and confirm the validation error, then omit the `limits` default in the module and observe the `null` deref. Understanding *why* the nested default is required is the whole point.

### Lab 3 — `moved`, `import`, `removed`

```bash
cd ~/tf-lab && mkdir -p 03-state-blocks && cd 03-state-blocks
```

```hcl
# main.tf
terraform {
  required_providers { local = { source = "hashicorp/local", version = "~> 2.5" } }
}

resource "local_file" "config" {
  filename = "${path.module}/cell.conf"
  content  = "cell=042\n"
}
```

```bash
terraform init && terraform apply -auto-approve

# 1. Rename with a `moved` block: rename the resource to local_file.cell_config
#    and add:
#      moved { from = local_file.config, to = local_file.cell_config }
terraform plan     # should be "No changes" plus a move notice

# 2. `removed`: comment out the resource, add a removed block with
#    lifecycle { destroy = false }, apply, then confirm the file still exists.
ls -la cell.conf
terraform state list    # empty

# 3. `import` it back:
#      import { to = local_file.cell_config, id = "./cell.conf" }
terraform plan -generate-config-out=generated.tf
cat generated.tf
terraform apply -auto-approve
```

That loop — move, forget, adopt — is exactly what you do when refactoring a shared cell module across 300 states, and doing it once locally is worth more than reading about it three times.

### Lab 4 — Remote state, native S3 locking, and a lock collision (LocalStack, free)

```bash
cd ~/tf-lab && mkdir -p 04-backend && cd 04-backend
# Needs docker, and the AWS CLI for the bucket call: brew install awscli
docker run -d --name localstack -p 4566:4566 localstack/localstack
until curl -sf http://localhost:4566/_localstack/health >/dev/null; do sleep 1; done

export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
aws --endpoint-url=http://localhost:4566 s3 mb s3://tfstate
```

```hcl
# main.tf
terraform {
  backend "s3" {
    bucket       = "tfstate"
    key          = "cells/cell-042/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true          # native S3 locking, Terraform 1.10+

    # LocalStack wiring
    access_key                  = "test"
    secret_key                  = "test"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    use_path_style              = true
    endpoints = { s3 = "http://localhost:4566" }
  }
  required_providers {
    time = { source = "hashicorp/time", version = "~> 0.12" }
  }
}

# A slow apply, so you can race two terminals against the lock.
resource "time_sleep" "slow" { create_duration = "60s" }
```

```bash
terraform init
terraform apply -auto-approve &      # terminal 1
sleep 5
terraform apply -auto-approve        # terminal 2 → "Error acquiring the state lock"

# Inspect the lock object itself.
aws --endpoint-url=http://localhost:4566 s3 ls s3://tfstate/cells/cell-042/
# → terraform.tfstate.tflock while held

# Practice recovery (only ever with the ID from the error message):
# terraform force-unlock <LOCK_ID>
```

Do the `force-unlock` at least once in the lab so that you have muscle memory before you need it in production, and internalize that the correct response to a stuck lock is *find out who holds it*, not unlock reflexively.

### Lab 5 — `terraform test` with mocked providers

```bash
cd ~/tf-lab/02-modules && mkdir -p modules/cell/tests
```

```hcl
# modules/cell/tests/defaults.tftest.hcl
mock_provider "local" {}

variables {
  cell = { name = "cell-999" }
}

run "defaults_applied" {
  command = plan
  assert {
    condition     = strcontains(local_file.manifest.content, "tier: standard")
    error_message = "tier default was not applied"
  }
}

run "rejects_unknown_tier" {
  command   = plan
  variables { cell = { name = "cell-999", tier = "gold" } }
  expect_failures = [var.cell]
}
```

```bash
cd modules/cell && terraform init && terraform test
```

Add a failing assertion and confirm the output tells you which `run` block and which assertion failed. This is the harness you will actually build for the cell module.

### Lab 6 — Plan JSON and a policy gate

```bash
cd ~/tf-lab/01-index-shift
terraform plan -out=tfplan
terraform show -json tfplan | jq '.format_version, (.resource_changes | length)'
terraform show -json tfplan | jq -r '
  .resource_changes[] | select(.change.actions | index("delete"))
  | "DELETE \(.address)"'
```

```rego
# policy/cells.rego
package main

deny contains msg if {
  some rc in input.resource_changes
  "delete" in rc.change.actions
  startswith(rc.address, "random_pet.by_count")
  msg := sprintf("refusing to delete tier-1 resource %v", [rc.address])
}
```

```bash
brew install conftest
mkdir -p policy    # then save the rego above as policy/cells.rego
terraform show -json tfplan > tfplan.json
conftest test --policy policy/ tfplan.json
```

That is the whole CI gate in miniature: plan to a file, render to JSON, evaluate policy over the *diff*, then apply the saved plan.

### Lab 7 — costed, optional

If you want a real backend: create an S3 bucket with versioning and SSE-KMS in your own account, set `use_lockfile = true`, and run Lab 3 against it. **Cost: an AWS KMS CMK is $1/month prorated, plus per-request charges.** Use SSE-S3 instead of a CMK to stay at effectively $0, and `terraform destroy` plus manual bucket deletion when done. Do not use a company account.

---

## Production gotchas

1. **Removing a `for_each` key destroys infrastructure; there is no "unmanage" default.** The safe removal sequence is: add a `removed` block with `lifecycle { destroy = false }`, apply, *then* delete the config. Getting this backwards is the most common way to accidentally delete a cell. ([`removed` block](https://developer.hashicorp.com/terraform/language/block/removed))

2. **`prevent_destroy` is not recorded in state.** *"Except for `create_before_destroy`, Terraform does not explicitly record a resource's `lifecycle` rule to state"* ([lifecycle](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle)). Deleting the `lifecycle` block and applying in the same PR bypasses the guard silently. Real protection is IAM plus a plan-JSON policy that rejects deletes on tier-1 addresses.

3. **`create_before_destroy` is contagious through the dependency graph.** Terraform propagates it to dependencies, silently reordering replacements far from where you set it. Always pair it with `name_prefix` or a `random_id` suffix, or you will hit `AlreadyExists` on the create half.

4. **Provider configuration cannot depend on unknown values, and no amount of `depends_on` fixes it.** *"You can use expressions to configure provider arguments, but you can only reference values that Terraform knows before it applies your configuration"* ([provider block](https://developer.hashicorp.com/terraform/language/block/provider#provider-specific-arguments)). This is a hard architectural constraint, not a bug to work around.

5. **`kubernetes_manifest` requires cluster reachability at *plan* time.** *"This resource requires API access during planning time"* ([kubernetes_manifest](https://registry.terraform.io/providers/hashicorp/kubernetes/latest/docs/resources/manifest)). A cell whose control plane is down or already deleted cannot be planned, which turns disaster recovery and teardown into state surgery.

6. **The Terraform `helm` provider embeds Helm 3, not Helm 4** — v3.2.0's `go.mod` pins `helm.sh/helm/v3 v3.18.5`. If your charts start using Helm 4 features, the provider will not render them. Verified against [the provider's go.mod](https://raw.githubusercontent.com/hashicorp/terraform-provider-helm/v3.2.0/go.mod).

7. **Never run `terraform apply` and expect atomicity.** State is written incrementally as each resource completes. A killed apply leaves a partially-created cell and a stale lock. Your orchestrator must handle "resume from partial" as a first-class case, not an exception.

8. **`terraform refresh` can delete everything.** *"If you have misconfigured credentials for one or more providers, Terraform may be misled into thinking that all of the managed objects have been deleted, causing it to remove all of the tracked objects without any confirmation prompt"* ([refresh](https://developer.hashicorp.com/terraform/cli/commands/refresh)). Use `plan -refresh-only` and review, never `apply -refresh-only -auto-approve` in automation.

9. **The plugin cache is not concurrency-safe.** *"The plugin cache directory is not guaranteed to be concurrency safe"* ([CLI config](https://developer.hashicorp.com/terraform/cli/config/config-file#provider-plugin-cache)). A CI runner doing 20 parallel cell inits against one cache volume will corrupt it. Use a filesystem mirror baked into the image instead.

10. **The lock file is per-platform unless you say otherwise.** `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64` once, or CI will fail with checksum errors the first time a Mac user runs `init` ([providers lock](https://developer.hashicorp.com/terraform/cli/commands/providers/lock#platform-os_arch)).

11. **`.terraform.lock.hcl` does not lock module versions.** It covers providers only ([dependency lock file](https://developer.hashicorp.com/terraform/language/files/dependency-lock)). Pin module sources to immutable tags; a `ref=main` module source means your 300 cells are not running the same code.

12. **Plan files contain secrets in cleartext.** *"You should therefore treat any saved plan files as potentially-sensitive artifacts"* ([plan `-out`](https://developer.hashicorp.com/terraform/cli/commands/plan#out-filename)). CI artifact stores are usually more readable than state buckets. Encrypt them or use short retention.

13. **`sensitive = true` does not keep anything out of state.** *"Terraform still stores the values of sensitive variables in your state"*, and `terraform output -json` prints them in the clear ([manage sensitive data](https://developer.hashicorp.com/terraform/language/manage-sensitive-data)). Use ephemeral resources (1.10) and write-only arguments (1.11), or OpenTofu state encryption.

14. **`terraform_remote_state` grants full state read, not just outputs.** *"any user or server which has enough access to read the root module output values will also always have access to the full state snapshot data by direct network requests"* ([remote state](https://developer.hashicorp.com/terraform/language/state/remote-state-data)). Publish to a parameter store instead.

15. **Workspaces share a backend and are therefore not an isolation boundary.** *"CLI workspaces within a working directory use the same backend, so they are not a suitable isolation mechanism"* ([workspaces](https://developer.hashicorp.com/terraform/cli/workspaces#when-not-to-use-multiple-workspaces)). Do not use them for prod/staging or for cells.

16. **Routine `-target` use means your state is too big.** *"Use `-target=ADDRESS` in exceptional circumstances only"* ([plan](https://developer.hashicorp.com/terraform/cli/commands/plan#target-address)). Treat every `-target` in a runbook as a state-splitting ticket.

17. **Plan JSON's `format_version` is not what the docs say.** The prose says `"1.0"`; current source declares `FormatVersion = "1.2"` in [`internal/command/jsonplan/plan.go`](https://raw.githubusercontent.com/hashicorp/terraform/main/internal/command/jsonplan/plan.go). Parse the field, do not assert equality with a hardcoded string.

18. **Terraform Stacks will not run on the open-source CLI.** The `terraform stacks` subcommands are `init`, `validate`, `create`, `fmt`, `list`, `version`, `providers-lock`, and deployment-management groups. There is no `plan` and no `apply` ([stacks CLI](https://developer.hashicorp.com/terraform/cli/commands/stacks)). Anyone proposing Stacks is implicitly proposing HCP Terraform or TFE 2.0+.

19. **Stacks has a 500-deployment ceiling** ([Stacks constraints](https://developer.hashicorp.com/terraform/language/stacks)), and its GA release discarded beta configurations and state history ([beta→GA](https://developer.hashicorp.com/terraform/language/stacks/update-GA)). For a fleet that might exceed 500 cells, that is a design-time disqualifier, and the beta precedent is a reason to be cautious about future migrations.

20. **Provisioner failures taint the resource.** A failed `local-exec` marks the whole resource for replacement, so a transient script error causes Terraform to plan the destruction of real infrastructure. Combined with the fact that provisioner effects are invisible to state, this is why the docs say *"You should exhaust all alternatives before using provisioners"* ([provisioners](https://developer.hashicorp.com/terraform/language/provisioners)).

21. **`for_each` cannot take unknown or sensitive values, and does not convert lists to sets.** The error "The `for_each` value depends on resource attributes that cannot be determined until apply" is not fixable with `depends_on`; it is a signal to split the apply or key on static input ([`for_each`](https://developer.hashicorp.com/terraform/language/meta-arguments/for_each)).

22. **CDKTF is archived.** If you inherit a CDKTF codebase, budget for a migration; there will be no upstream fixes ([CDKTF](https://developer.hashicorp.com/terraform/cdktf)).

---

## How this shows up in cell lifecycle

**Provisioning a cell.** A cell provision is a sequence of Terraform applies against a per-cell state, ordered because provider configuration cannot depend on unknown values: (1) accounts/projects and IAM, (2) network — often owned by a separate networking team, so it is its own state and its own module, (3) the managed cluster (EKS/GKE/AKS), (4) a *separate* apply for the GitOps bootstrap that installs the Argo/Flux agent and registers the cell. Steps 3 and 4 must be separate applies. Everything after step 4 is not Terraform's problem.

**Upgrading a cell.** Terraform gives you a diff and a lock; it does not give you a rollout. Cell upgrades — a Kubernetes minor bump, a node image roll — need ordering, concurrency limits, health gates between waves, and the ability to pause. Encode the cell's desired module version in the fleet inventory (Terraform 1.15's variables-in-`version` makes this natural), and let an orchestrator advance cells through it. At a company that builds Temporal, a Temporal workflow driving per-cell applies with retries, heartbeats, and a signal-based approval gate is the natural fit, and it is a defensible platform design rather than a cute use of the home product.

**Tearing down a cell.** This is where per-cell state pays for itself: `terraform destroy` against one state, with no chance of touching a neighbor. Two traps. First, `prevent_destroy` on tier-1 resources will block the destroy — so the teardown runbook needs a documented, reviewed way to lift it, not an ad-hoc edit. Second, if you kept `kubernetes` or `helm` resources in the cell's state, destroy requires a *reachable* cluster; if the control plane is already gone the plan itself fails. That is the concrete operational cost of not stopping at the cluster boundary.

**Multi-cloud.** Three clouds means three provider ecosystems with genuinely different resource models, not a single abstraction. Do not build a "cloud-agnostic cell module" with a `var.cloud` switch — you will end up with a module where two-thirds of the resources are `count = var.cloud == "aws" ? 1 : 0`, which is unreadable and untestable. Build `modules/aws/cell`, `modules/gcp/cell`, `modules/azure/cell` with a *common input object schema and a common output contract*, enforced by `terraform test` in CI. Same interface, three implementations. That is the composition the docs are describing.

**Networking as shared ownership.** Split state at the ownership boundary. If a networking team owns the network state, you consume its outputs through a published contract — parameter store keys, not `terraform_remote_state`. This gives both teams independent apply cadence, independent locks, and a review surface that is an explicit interface change rather than "someone touched a shared state file."

**The inventory is the real system.** With 300 per-cell states, the highest-leverage artifact is not any module — it is the fleet registry: which cells exist, on which cloud/region/account, at what module version, at what Kubernetes version, in what lifecycle phase. Terraform consumes it; it does not own it. Build it deliberately.

---

## Learning path

**Day 1 (4-6 hours).** Install Terraform 1.16 and OpenTofu side by side. Read [the state docs](https://developer.hashicorp.com/terraform/language/state) end to end — it is short and it is the whole game. Run Labs 1 and 3; the index-shift lab and the moved/import/removed lab together give you the correct mental model of state as an identity map. Then read your team's cell module top to bottom and write down every question; specifically, find out where they split state, whether they use workspaces, and where the Terraform-to-GitOps boundary sits. Skim [module composition](https://developer.hashicorp.com/terraform/language/modules/develop/composition) and [providers within modules](https://developer.hashicorp.com/terraform/language/modules/develop/providers).

**Week 1.** Run Labs 2, 4, 5, and 6. Read the whole [`lifecycle`](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle) page and the [manage sensitive data](https://developer.hashicorp.com/terraform/language/manage-sensitive-data) page. Do a real, reviewed, small change to a production cell module end to end so you see the team's actual pipeline: what the plan output looks like in a PR, what the policy gate rejects, who approves, how apply is triggered. Write one `terraform test` file for an existing module that has none. Find out which of Terragrunt / Stacks / a home-grown generator your team uses for N cells, and read its config as carefully as you read the HCL. Ask what happens today when an apply dies halfway.

**Month 1.** Own a state-layout decision. Concretely: audit whether any cell state is large enough that plan time is hurting, and either split it or document why not. Build or improve the drift-detection job (`plan -detailed-exitcode` per cell, nightly, ticket on exit 2) — this is high-value, low-risk, and immediately visible. Read the full [json-format spec](https://developer.hashicorp.com/terraform/internals/json-format) and write one Conftest policy that encodes a real platform invariant, then get it into the blocking gate. Form and write down an opinion on OpenTofu with the specific gaps named (state encryption, provider `for_each`, backend-config variables) so the conversation is about tradeoffs rather than vibes. Finally, sketch the cell-upgrade orchestration you would build — waves, health gates, resume-from-partial — because that is the senior-level gap Terraform structurally does not fill.

---

## References

1. [Terraform documentation](https://developer.hashicorp.com/terraform) — canonical docs. Use the version selector; version-less search results are frequently stale.
2. [Terraform v1.16.0 release](https://github.com/hashicorp/terraform/releases/tag/v1.16.0) — current stable, 2026-08-26. `import` in modules, `lifecycle { destroy = false }`, `state show -json`, `graph -format=mermaid`. Cite the tag, not the branch CHANGELOG, which still says "Unreleased."
3. [Terraform CHANGELOG (main)](https://raw.githubusercontent.com/hashicorp/terraform/main/CHANGELOG.md) — the only reliable source for what shipped vs. what is still an experiment (see "deferred actions"). Per-version history at [v1.15](https://github.com/hashicorp/terraform/blob/v1.15/CHANGELOG.md) and [v1.14](https://github.com/hashicorp/terraform/blob/v1.14/CHANGELOG.md).
4. [`for_each` meta-argument](https://developer.hashicorp.com/terraform/language/meta-arguments/for_each) — keying rules, the unknown/sensitive restrictions, no implicit list→set conversion.
5. [`count` — When to Use `for_each` Instead (v1.11.x)](https://developer.hashicorp.com/terraform/language/v1.11.x/meta-arguments/count#when-to-use-for_each-instead-of-count) — the index-shift explanation, removed from current docs; cite the pinned URL.
6. [`lifecycle` meta-argument](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle) — all eight arguments, the literal-values-only rule, and the "not recorded to state" caveat.
7. [`moved` block](https://developer.hashicorp.com/terraform/language/block/moved) / [`removed` block](https://developer.hashicorp.com/terraform/language/block/removed) / [`import` block](https://developer.hashicorp.com/terraform/language/import) — config-driven state edits; added in 1.1, 1.7, and 1.5 respectively.
8. [Provisioners](https://developer.hashicorp.com/terraform/language/provisioners) — the current "exhaust all alternatives" wording; the classic "last resort" phrasing is at [the v1.11.x URL](https://developer.hashicorp.com/terraform/language/v1.11.x/resources/provisioners/syntax#provisioners-are-a-last-resort).
9. [Type constraints and `optional()`](https://developer.hashicorp.com/terraform/language/expressions/type-constraints#optional-object-type-attributes) — optional object attributes with defaults, 1.3+.
10. [Validation overview](https://developer.hashicorp.com/terraform/language/validate) — the one-stop version table for `validation`, preconditions/postconditions, and [`check` blocks](https://developer.hashicorp.com/terraform/language/block/check).
11. [`dynamic` blocks](https://developer.hashicorp.com/terraform/language/expressions/dynamic-blocks#best-practices-for-dynamic-blocks) — including the explicit "always write nested blocks out literally where possible."
12. [`templatefile`](https://developer.hashicorp.com/terraform/language/functions/templatefile#generating-json-or-yaml-from-a-template) — and the guidance to use `jsonencode`/`yamlencode` for structured output.
13. [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3) — `use_lockfile`, the DynamoDB deprecation, cross-account `assume_role` for state access.
14. [GCS backend](https://developer.hashicorp.com/terraform/language/backend/gcs) and [AzureRM backend](https://developer.hashicorp.com/terraform/language/backend/azurerm) — native locking on the other two clouds, plus `use_azuread_auth`.
15. [CLI workspaces — when not to use them](https://developer.hashicorp.com/terraform/cli/workspaces#when-not-to-use-multiple-workspaces) — the shared-backend argument against workspaces-as-environments; see also [state workspaces](https://developer.hashicorp.com/terraform/language/state/workspaces).
16. [Remote state data source](https://developer.hashicorp.com/terraform/language/state/remote-state-data) — the full-state-access caveat and the recommendation to publish data explicitly.
17. [`terraform state rm`](https://developer.hashicorp.com/terraform/cli/commands/state/rm) and [state removal](https://developer.hashicorp.com/terraform/language/state/remove) — the docs' own steer toward `removed` blocks. See also [`state mv`](https://developer.hashicorp.com/terraform/cli/commands/state/mv).
18. [Module composition](https://developer.hashicorp.com/terraform/language/modules/develop/composition) and [standard module structure](https://developer.hashicorp.com/terraform/language/modules/develop/structure) — the flat-composition argument and the conventional file layout.
19. [Providers within modules](https://developer.hashicorp.com/terraform/language/modules/develop/providers) — the "must not contain any `provider` blocks" rule and `configuration_aliases`; the [`provider` block reference](https://developer.hashicorp.com/terraform/language/block/provider#alias) covers aliases.
20. [Terraform tests](https://developer.hashicorp.com/terraform/language/tests) and [test mocking](https://developer.hashicorp.com/terraform/language/tests/mocking) — `.tftest.hcl`, `run` blocks, `mock_provider`, `override_*`. GA in 1.6, mocking in 1.7.
21. [Terratest](https://terratest.gruntwork.io/) — Go-based integration testing (Gruntwork, third-party); the right tool for post-apply assertions against a live cell.
22. [Terraform Stacks overview](https://developer.hashicorp.com/terraform/language/stacks) and [`terraform stacks` CLI](https://developer.hashicorp.com/terraform/cli/commands/stacks) — read together, these prove Stacks needs HCP/TFE and show the 500-deployment limit. [Beta→GA changes](https://developer.hashicorp.com/terraform/language/stacks/update-GA) cover the file extensions and the `orchestrate` deprecation.
23. [OpenTofu documentation](https://opentofu.org/docs/) — start here; [state encryption](https://opentofu.org/docs/language/state/encryption/) and [provider `for_each`](https://opentofu.org/docs/language/providers/configuration/#for_each-multiple-instances-of-a-provider-configuration) are the two pages that matter most for a cell platform.
24. [OpenTofu v1.12.6 release](https://github.com/opentofu/opentofu/releases/tag/v1.12.6) — current stable, 2026-08-19. Governance: [Linux Foundation launch announcement](https://www.linuxfoundation.org/press/announcing-opentofu).
25. [HashiCorp adopts BUSL](https://www.hashicorp.com/en/blog/hashicorp-adopts-business-source-license), the [license FAQ](https://www.hashicorp.com/license-faq), and the [Terraform LICENSE](https://github.com/hashicorp/terraform/blob/main/LICENSE) — BUSL 1.1, covers 1.6.0+, Licensor now IBM, Change License MPL 2.0.
26. [Terragrunt documentation](https://docs.terragrunt.com/) and [Terragrunt Stacks](https://docs.terragrunt.com/features/stacks/explicit/) — the DRY-root-module answer for N cells; note it defaults to the `tofu` binary.
27. [CDKTF deprecation notice](https://developer.hashicorp.com/terraform/cdktf) — the verbatim deprecation, 2025-12-10. Secondary confirmation: [the archived repo](https://github.com/hashicorp/terraform-cdk).
28. [Crossplane — what's new](https://docs.crossplane.io/latest/whats-new/) and [CNCF project page](https://www.cncf.io/projects/crossplane/) — v2's namespaced XRs, removal of claims, function-only composition; graduated 2025-10-28.
29. [Pulumi documentation](https://www.pulumi.com/docs/) — the alternative engine; see also [running Terraform modules from Pulumi](https://www.pulumi.com/docs/iac/guides/building-extending/using-existing-tools/use-terraform-module/).
30. [hashicorp/kubernetes provider](https://registry.terraform.io/providers/hashicorp/kubernetes/latest/docs) — the capitalized WARNING about stacking cluster creation with Kubernetes resources; the single best citation for "stop at the cluster boundary." The [`kubernetes_manifest` resource](https://registry.terraform.io/providers/hashicorp/kubernetes/latest/docs/resources/manifest) documents the plan-time API access requirement verbatim.
31. [hashicorp/helm provider](https://registry.terraform.io/providers/hashicorp/helm/latest/docs) and its [v3 upgrade guide](https://registry.terraform.io/providers/hashicorp/helm/latest/docs/guides/v3-upgrade-guide) — the block→attribute syntax break; confirm the embedded Helm version in `go.mod` before assuming Helm 4 support.
32. [`terraform plan`](https://developer.hashicorp.com/terraform/cli/commands/plan) and [`terraform apply`](https://developer.hashicorp.com/terraform/cli/commands/apply) — `-out`, `-refresh=false`, `-target`, `-replace`, `-detailed-exitcode`, saved-plan mode, and the `-parallelism` default of 10, all with the official warnings.
33. [JSON output format](https://developer.hashicorp.com/terraform/internals/json-format) — the plan/state JSON schema for policy gates; cross-check `format_version` against [the source constant](https://raw.githubusercontent.com/hashicorp/terraform/main/internal/command/jsonplan/plan.go).
34. [CLI configuration file](https://developer.hashicorp.com/terraform/cli/config/config-file) — `TF_PLUGIN_CACHE_DIR`, its concurrency warning, and `provider_installation` mirrors.
35. [Dependency lock file](https://developer.hashicorp.com/terraform/language/files/dependency-lock) and [`terraform providers lock`](https://developer.hashicorp.com/terraform/cli/commands/providers/lock#platform-os_arch) — providers only, and the multi-platform hash problem.
36. [Manage sensitive data](https://developer.hashicorp.com/terraform/language/manage-sensitive-data) — the plaintext-state statement and `sensitive` limits; [write-only arguments](https://developer.hashicorp.com/terraform/language/manage-sensitive-data/write-only) (1.11) and [ephemeral resources](https://developer.hashicorp.com/terraform/language/block/ephemeral) (1.10) are the only in-Terraform fix.
37. [hashicorp/vault provider](https://registry.terraform.io/providers/hashicorp/vault/latest/docs) — 24 ephemeral resources; the practical partner to write-only arguments.
38. [`terraform refresh`](https://developer.hashicorp.com/terraform/cli/commands/refresh) — deprecated, plus the "may remove all tracked objects without confirmation" warning worth quoting in a runbook. Fleet drift: [HCP Terraform workspace health](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/health#drift-detection).
39. [tfsec → Trivy announcement](https://github.com/aquasecurity/tfsec/discussions/1994) and [Trivy misconfiguration scanning](https://trivy.dev/docs/latest/scanner/misconfiguration/) — the retirement, and where the rules went. Alternatives: [Conftest](https://www.conftest.dev/), [Checkov](https://www.checkov.io/), [Sentinel](https://developer.hashicorp.com/sentinel), and the beta [Terraform Policy](https://developer.hashicorp.com/terraform/policy).
40. [AWS provider `assume_role`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs#assume_role-configuration-block), [Google provider reference](https://registry.terraform.io/providers/hashicorp/google/latest/docs/guides/provider_reference), [AzureRM provider](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs) — cross-account, impersonation, and multi-subscription/OIDC configuration for the three clouds.
