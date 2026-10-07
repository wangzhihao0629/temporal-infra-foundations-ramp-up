# HashiCorp Vault for a Platform Team

**Why this matters.** Every cell you provision needs secrets before it can serve traffic: database credentials for the persistence layer, TLS material for the frontend, cloud credentials for the operators running inside it, and encryption keys for anything at rest. Cell lifecycle owns the moment those secrets come into existence and the moment they are destroyed, across AWS, GCP, and Azure. Vault is the piece of infrastructure that decides whether "provision a cell" is a 20-minute automated operation or a ticket to a security team. It is also the piece with the nastiest failure modes: a Vault that cannot unseal is a cell that cannot boot, and a Vault whose audit devices are all wedged is a Vault that refuses every write. You need the operational model, not the CLI surface.

---

## The mental model

Vault is **a small encrypted key-value store with an authorization layer and a plugin system bolted on top**. Almost everything confusing about Vault falls out of that sentence.

The store is called the **barrier**. Everything Vault persists goes through the barrier, encrypted with AES-256-GCM. The barrier is the entire security boundary: the storage backend (Raft, Consul, S3, DynamoDB) sees only ciphertext and is explicitly untrusted. Vault's [security model](https://developer.hashicorp.com/vault/docs/internals/security) says this plainly — the storage backend is not part of the trusted computing base.

Because the barrier is encrypted, Vault starts up *sealed*: it has the ciphertext but not the key. **Unsealing** is the process of reconstructing the key that decrypts the barrier. Once unsealed, Vault is a normal HTTP server. Sealed, it answers exactly three things: health, seal status, and unseal.

The authorization layer is **tokens + policies**. Every request carries a token. Every token maps to a set of policies. Every policy is a list of `path` blocks with `capabilities`. There is no RBAC object model, no roles-that-contain-roles, no inheritance. Just paths and capabilities. This is why Vault multi-tenancy is fundamentally a *path namespacing* exercise.

The plugin system is **mounts**. Auth methods mount under `auth/`, secrets engines mount under a path you choose. A mount is a self-contained plugin with its own storage prefix inside the barrier. `vault secrets enable -path=cell-usw2-db database` creates a new database engine whose data lives under a private prefix, isolated from every other mount. Mounts are how you get per-cell isolation without namespaces.

Layered on top: **leases**. Anything Vault *generates* (as opposed to stores) gets a lease — an ID, a TTL, and a revocation hook. Dynamic database credentials, cloud credentials, and tokens are all leased. Vault tracks every live lease in its own storage and revokes them when they expire. This is the feature that makes Vault worth running, and also the single most common source of Vault performance incidents.

Four things to hold in your head simultaneously:

1. **Vault is stateful and its state is small but hot.** The lease table and the token store dominate write traffic.
2. **Vault is a single-writer system.** One active node handles all writes; standbys forward. Adding nodes does not add write throughput.
3. **Vault is a bootstrap dependency.** Anything that needs Vault to start cannot be something Vault needs to start. Auto-unseal makes this circular in a subtle way (see below).
4. **Vault fails closed.** Sealed, quorum-lost, or audit-wedged, it stops serving rather than degrading.

---

## Core concepts

### The barrier, seal, and unseal

There are three keys, and conflating them is the number one source of confusion.

| Key | Also called | What it does | Where it lives |
|---|---|---|---|
| Encryption key / barrier key | "the keyring" | Actually encrypts data in the barrier (AES-256-GCM) | Stored *inside* the barrier, encrypted by the root key |
| Root key | "master key" (legacy name) | Encrypts the encryption key | Never persisted in plaintext; protected by the seal |
| Unseal key shares | "Shamir shares" | Reconstruct the root key at startup | Split across humans/HSMs; never stored by Vault |

The [seal/unseal concept doc](https://developer.hashicorp.com/vault/docs/concepts/seal) is the primary source. The important consequence: **rotating the encryption key (`vault operator rotate`) is cheap and online; rekeying the unseal shares (`vault operator rekey`) is a ceremony**. They are different operations and people mix them up.

With the default **Shamir seal**, `vault operator init` produces N key shares with threshold T (default 5 and 3). Startup requires T humans to each submit a share. This is fine for one Vault. It is unworkable for one Vault per cell across three clouds.

```bash
# Shamir init — the ceremony you do NOT want in cell provisioning
vault operator init -key-shares=5 -key-threshold=3
# Unseal key 1: <base64>
# ...
# Initial Root Token: hvs....

vault operator unseal <share-1>
vault operator unseal <share-2>
vault operator unseal <share-3>   # now unsealed
```

### Auto-unseal with cloud KMS — and the bootstrap dependency it creates

Auto-unseal replaces the Shamir step: Vault stores the root key wrapped by a **cloud KMS key**, and on startup calls `Decrypt` against KMS to recover it. This is what a cell actually uses, because provisioning must be unattended.

```hcl
# AWS — https://developer.hashicorp.com/vault/docs/configuration/seal/awskms
seal "awskms" {
  region     = "us-west-2"
  kms_key_id = "arn:aws:kms:us-west-2:111122223333:key/1a2b3c4d-..."
  # Credentials come from the instance/IRSA role. Do NOT put static keys here.
}
```

```hcl
# GCP — https://developer.hashicorp.com/vault/docs/configuration/seal/gcpckms
seal "gcpckms" {
  project    = "temporal-cell-prod"
  region     = "global"
  key_ring   = "vault-unseal"
  crypto_key = "vault-cell-usc1"
}
```

```hcl
# Azure — https://developer.hashicorp.com/vault/docs/configuration/seal/azurekeyvault
seal "azurekeyvault" {
  tenant_id  = "..."
  vault_name = "cell-kv-weu"
  key_name   = "vault-unseal"
  # Prefer managed identity over client_id/client_secret.
}
```

**The bootstrap dependency.** Auto-unseal moves the secret-zero problem from "humans holding shares" to "this node's cloud identity can call KMS Decrypt." That is a much better trade, but it creates a hard dependency graph that you must design around explicitly:

- Vault cannot start until it can reach the KMS endpoint. In a private-networking cell, that means a VPC endpoint / Private Service Connect / Private Link must be provisioned *before* Vault. If your networking is torn down or misconfigured, Vault does not come back — and the networking team owns half of that path.
- Vault cannot start if the node's identity is not yet bound. On Kubernetes with IRSA or Workload Identity, the OIDC provider and role trust policy must exist before the pod starts.
- **KMS is a cross-region single point of failure.** An AWS KMS regional outage seals every Vault in that region on restart. Existing unsealed Vaults keep running (the root key is in memory), which is why "everything was fine until we did a rolling restart" is the classic shape of this incident.
- With auto-unseal, `vault operator init` produces **recovery keys**, not unseal keys. Recovery keys cannot unseal Vault. They authorize a small set of privileged operations: generating a new root token, rekeying the recovery shares, and (with `seal-migration`) changing seals. **Losing them does not brick Vault, but it does mean you can never generate a new root token.** Store them like unseal keys anyway.

Vault 2.0 also added [AWS KMS multi-region keys](https://developer.hashicorp.com/vault/docs/secrets/key-management/awskms#multi-region-keys) as managed keys, which helps DR designs where a replica in a second region must unseal against a replicated key. OpenBao took a different path and made auto-unseal mechanisms [external plugins as of 2.6.0](https://openbao.org/community/release-notes/2-6-0/), deprecating the built-in `awskms`/`gcpckms`/`azurekeyvault`/`pkcs11` seals for removal in 2.7.0.

Set **`seal_wrap = true`** on sensitive mounts if you have Enterprise/HSM: it wraps values with the seal in addition to barrier encryption, so a compromised barrier key alone is insufficient. See [seal wrap](https://developer.hashicorp.com/vault/docs/enterprise/sealwrap).

*See also: [KMS, secrets, and what they back](03-multicloud-aws-gcp-azure.md#kms-secrets-and-what-they-back) for the per-cloud key-scoping and residency rules that decide where this dependency can even be satisfied, and [workload identity](03-multicloud-aws-gcp-azure.md#workload-identity-how-a-pod-gets-a-cloud-credential) for how the pod gets the identity that calls Decrypt.*

### Storage backends: Integrated Storage (Raft) vs Consul

For anything new, the answer is Integrated Storage. Consul storage exists because Vault predates Raft-in-Vault, and it is now a liability: you are operating a second consensus system whose failure modes you must also understand.

| | Integrated Storage (Raft) | Consul |
|---|---|---|
| Consensus | Inside Vault, `raft` stanza | External Consul cluster |
| Operational surface | One system | Two systems, two upgrade cadences |
| Snapshots | `vault operator raft snapshot save` — a real Vault-consistent backup | Consul snapshots + separate Vault state reasoning |
| Failure domain | Vault node loss = Vault degradation | Consul quorum loss = Vault outage, and you debug Consul |
| Data size limit | Practical ceiling from Raft log + snapshot size; HashiCorp guidance is to keep it modest | Consul KV limits apply |
| Recommended today | Yes | Legacy only |

The [Integrated Storage internals doc](https://developer.hashicorp.com/vault/docs/internals/integrated-storage) is the reference. Configuration:

```hcl
storage "raft" {
  path    = "/vault/data"
  node_id = "vault-usw2-a"

  retry_join {
    leader_api_addr = "https://vault-0.vault-internal:8200"
  }
  retry_join {
    leader_api_addr = "https://vault-1.vault-internal:8200"
  }
  retry_join {
    leader_api_addr = "https://vault-2.vault-internal:8200"
  }
}

cluster_addr = "https://vault-usw2-a.vault-internal:8201"
api_addr     = "https://vault.cell-usw2.internal:8200"
```

`api_addr` and `cluster_addr` are the two settings people get wrong. `api_addr` is what standbys advertise for client redirects; `cluster_addr` is the node-to-node request-forwarding and Raft port (8201). If `api_addr` points at a load balancer that round-robins, redirect loops happen.

Note OpenBao 2.6.0 added `auto_join` via DNS SRV records, which is nicer than a static `retry_join` list for autoscaled cells.

### HA, standbys, performance standbys, replication, DR

Vault HA is **active/standby with request forwarding**, not active/active. One node holds the lock; the rest are standbys. A standby either redirects the client to the active node (`api_addr`) or transparently forwards the request over the cluster port. Writes always land on one node.

**Node counts.** HashiCorp's [Raft reference architecture](https://developer.hashicorp.com/vault/tutorials/day-one-raft/raft-reference-architecture) recommends **3 or 5 nodes**. Three tolerates one failure; five tolerates two. Do not run even numbers — you get the cost of the extra node with none of the fault tolerance. Do not run more than five without a reason; every voter adds Raft write latency.

**Autopilot** ([docs](https://developer.hashicorp.com/vault/docs/concepts/integrated-storage/autopilot)) manages voter promotion: a joining node starts as a non-voter, syncs the Raft index, and is promoted only after a stability threshold. It has been on by default since Vault 1.7, but **dead server cleanup is not enabled by default** — which means a cell whose node was replaced by the autoscaler leaves a phantom voter in the quorum calculation until you clean it up.

```bash
vault operator raft autopilot set-config \
  -cleanup-dead-servers=true \
  -dead-server-last-contact-threshold=24h \
  -min-quorum=3

vault operator raft autopilot state
vault operator raft list-peers
```

Note the threshold: `24h` is the default, and HashiCorp's docs warn explicitly against lowering it, because the value must exceed the time a joining node takes to load a Raft snapshot. Set it to `10m` and a new cell node restoring a large snapshot gets pruned mid-join — the exact scenario you turned cleanup on to fix. Lower it only after you have measured your own snapshot-load time.

**Performance standbys** (Enterprise) let standby nodes serve read-only requests locally instead of forwarding, and forward only writes. This is the only way to scale reads within a cluster. OpenBao shipped a community equivalent — standby nodes serving reads — in [2.5.0](https://openbao.org/community/release-notes/2-5-0/).

**Replication** (Enterprise) comes in two flavors and they are not interchangeable:

- **Performance replication**: secondary clusters serve reads and handle their own local tokens/leases; writes forward to the primary. Used for latency and read scale across regions.
- **Disaster recovery replication**: a warm standby cluster that mirrors *everything* including tokens and leases, but serves no client traffic until promoted. Used for RPO/RTO.

Both are Enterprise-only, which is a real budget decision for a multi-cloud fleet.

### Cell-local Vault vs central Vault — an honest recommendation

This is the architectural call, so treat it as one.

| | Cell-local Vault (one Vault per cell) | Central Vault (one regional/global Vault, cells are clients) |
|---|---|---|
| Blast radius of compromise | One cell's secrets | Everything, unless namespaces/policy are perfect |
| Blast radius of outage | One cell | Every cell provision, renewal, and dynamic cred in scope |
| Cell teardown | Delete the Vault, done | Must reliably garbage-collect mounts, roles, policies, leases |
| Cross-cloud story | Each cell uses its own cloud's KMS natively | One Vault must hold credentials for three clouds |
| Operational cost | N Vaults to upgrade, snapshot, monitor, and rotate | One Vault to operate well |
| Enterprise license cost | Scales with cluster count | Cheaper |
| Bootstrap complexity | Each cell needs KMS key + identity provisioned first | Cells need only a network path and an auth method |
| Audit / compliance | Per-cell audit stream, naturally scoped | One stream, needs namespace/path tagging |
| Failure correlation | Independent; a bad Vault upgrade hits one cell | Correlated; a bad upgrade hits the fleet |

**Recommendation framing.** For a Temporal Cloud–shaped system — many isolated cells, each a tenancy boundary, spread across three clouds — the *default should be cell-local Vault for cell-scoped secrets*, with a *small central Vault (or cloud KMS directly) only for the things that genuinely must be global*: the root PKI, the cell-provisioning identity, and the fleet-wide operator credentials.

The reasoning is blast radius symmetry. A cell already is your isolation unit; if a cell's Vault can read another cell's database credentials, you have quietly made your isolation claim false. The counterargument — "N Vaults is N times the ops burden" — is real, but it is a burden that automation absorbs well (the same Terraform module runs N times) whereas a blast-radius failure is a burden automation cannot absorb at all.

The honest cost: cell-local Vault means the **cell provisioning pipeline** now owns Vault initialization, recovery-key escrow, snapshot scheduling, upgrade rollout, and audit shipping — N times. If your team does not have the automation maturity to do that unattended, a central Vault with rigorously enforced per-cell namespaces is the pragmatic interim, and you should write down that you are trading isolation for operational simplicity, with a date to revisit.

### Auth methods and the secret-zero problem

**Secret zero** is the credential you need in order to get credentials. Every auth method is an answer to "what does this caller already possess that Vault can independently verify?" The good answers are ones where the caller possesses nothing durable — the platform vouches for it.

| Method | Secret zero | How Vault verifies | Revocable early? | Fit for cells |
|---|---|---|---|---|
| **Kubernetes** | Projected SA token | Calls `TokenReview` on the cluster API | Yes (delete the SA/pod) | Primary for in-cell workloads |
| **JWT/OIDC** | Same SA token, or any OIDC JWT | Verifies signature against JWKS/OIDC discovery — no callback | No, TTL only | Good when Vault is outside the cluster |
| **AWS IAM** | The instance/pod's IAM role | Verifies a signed `sts:GetCallerIdentity` request | N/A | AWS cells, non-K8s nodes |
| **GCP** | Instance identity JWT / SA JWT | Verifies Google-signed JWT | N/A | GCP cells |
| **Azure** | Managed identity token (IMDS) | Verifies against Azure AD | N/A | Azure cells |
| **AppRole** | RoleID + SecretID | Compares stored values | Yes | Last resort; you now own secret zero |
| **OIDC (human)** | Browser SSO | Standard OIDC flow to your IdP | Yes | All human access |
| **TLS cert** | Client certificate | Verifies against a configured CA | Via CRL only | Useful for cell-to-Vault bootstrapping |

**Kubernetes auth** is the one to understand deeply, because Kubernetes 1.21 changed it and the change still bites people. The [Vault Kubernetes auth docs](https://developer.hashicorp.com/vault/docs/auth/kubernetes) are the primary source.

The flow: the pod reads its projected service account token from `/var/run/secrets/kubernetes.io/serviceaccount/token` and POSTs it to `auth/kubernetes/login` with a role name. Vault then calls the cluster's **`TokenReview` API** with a *reviewer* credential to ask "is this token valid, and whose is it?" Kubernetes answers with the service account name, namespace, and UID. Vault matches those against `bound_service_account_names` / `bound_service_account_namespaces` on the role and issues a token.

Since Kubernetes 1.21, `BoundServiceAccountTokenVolume` is on by default: mounted SA tokens now expire and are bound to the pod's lifetime, and the `iss` claim is cluster-specific. Two consequences:

1. **`disable_iss_validation` must be `true`.** Vault made this the default for new mounts in 1.9.0, but mounts created before that keep the old default. The docs say `disable_iss_validation=true` is "the new recommended value for all versions of Vault" — the Kubernetes API already validates the issuer during `TokenReview`, so Vault doing it again is duplicated work that breaks on cluster-specific issuers. Note that in Vault 2.x both `disable_iss_validation` and `issuer` are formally **deprecated and slated for removal**; the check only still matters for auth mounts created before Vault 1.9, which retain the old default.
2. **The reviewer JWT cannot be a long-lived static token anymore** without extra work. There are four options, and the docs lay them out with tradeoffs:

```bash
# BEST for Vault running inside the cell's cluster (requires Vault 1.9.3+).
# Omit token_reviewer_jwt and kubernetes_ca_cert; Vault reads its own projected
# token from disk and re-reads it as it rotates.
vault write auth/kubernetes/config \
    kubernetes_host="https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT"
```

```bash
# Vault's own ServiceAccount needs TokenReview permission.
kubectl create clusterrolebinding vault-auth-delegator \
  --clusterrole=system:auth-delegator \
  --serviceaccount=vault:vault
```

The other three: use the *client's* JWT as the reviewer (every client SA needs `system:auth-delegator` — high operational overhead), mint a long-lived `kubernetes.io/service-account-token` Secret (works, loses the short-lived-token benefit), or **skip Kubernetes auth and use JWT auth** with the cluster as an OIDC provider. JWT auth removes the reviewer credential entirely and works when Vault cannot reach the cluster API — the tradeoff is that client tokens **cannot be revoked before their TTL expires**, so keep TTLs short.

For a central Vault serving cells across three clouds and dozens of clusters, JWT/OIDC auth is usually the right call: no network path from Vault back to each cluster's API server, no reviewer credential per cluster, just a JWKS URL per cluster.

Also ensure `--service-account-lookup` is on (default since Kubernetes 1.7); without it, deleted tokens still validate. And note Kubernetes extends admission-injected token lifetimes to a year by default to smooth the migration — turn that off with `--service-account-extend-token-expiration=false` if you want real short-lived tokens.

**AWS IAM auth** is worth a sentence because its verification model is unusual: the client constructs a signed `sts:GetCallerIdentity` request but does not send it; it sends the *signed request* to Vault, and Vault replays it against STS. Vault learns the caller's ARN without the caller ever revealing a credential. See the [AWS auth docs](https://developer.hashicorp.com/vault/docs/auth/aws).

**AppRole** is the escape hatch, and it should feel like one. RoleID is not secret; SecretID is. The only safe pattern is **response wrapping**: a trusted orchestrator requests a wrapped SecretID and hands the single-use wrapping token to the workload, which unwraps it. If the token was already used, the workload knows it was intercepted.

```bash
vault write -wrap-ttl=120s -f auth/approle/role/cell-bootstrap/secret-id
# hand wrapping_token to the workload; it calls:
vault unwrap <wrapping_token>
```

### Policies

Policies are HCL, and the syntax is small enough to memorize.

```hcl
# Read-only on one cell's KV data, with list on the metadata.
path "cells/data/usw2-042/*" {
  capabilities = ["read"]
}
path "cells/metadata/usw2-042/*" {
  capabilities = ["list", "read"]
}

# Generate dynamic Postgres creds for this cell only.
path "cell-usw2-042/creds/temporal-frontend" {
  capabilities = ["read"]
}

# Explicit deny wins over everything, including a broader allow.
path "cells/data/usw2-042/root-credentials" {
  capabilities = ["deny"]
}

# Renew and revoke your own leases.
path "sys/leases/renew" { capabilities = ["update"] }
path "sys/leases/revoke" { capabilities = ["update"] }
```

Capabilities map to HTTP verbs: `create` (POST on a new path), `read` (GET), `update` (POST/PUT), `delete` (DELETE), `list` (LIST), `patch` (PATCH — needed for KV v2 partial updates), plus `sudo` (root-protected endpoints), `subscribe` (event streams), and `deny`. `deny` short-circuits everything.

Globbing has exactly two forms and people confuse them constantly:

- `*` is a **trailing wildcard only**. `secret/foo*` matches `secret/foobar` and `secret/foo/bar`. `secret/*/bar` does *not* work.
- `+` is a **single path-segment wildcard** and can appear anywhere. `secret/+/config` matches `secret/app1/config` but not `secret/app1/db/config`.

**Templated policies** are how you get per-cell isolation without generating N policies. The template renders identity metadata at evaluation time:

```hcl
path "cells/data/{{identity.entity.aliases.auth_kubernetes_bcecb1e1.metadata.cell_id}}/*" {
  capabilities = ["read", "list"]
}
```

To populate that metadata from Kubernetes, set `use_annotations_as_alias_metadata=true` on the auth config and annotate the service account (this requires giving Vault permission to read ServiceAccounts):

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: temporal-frontend
  namespace: cell-usw2-042
  annotations:
    vault.hashicorp.com/alias-metadata-cell_id: usw2-042
```

**A breaking change you must know about:** Vault 2.0.1 made it so that **wildcards and globs in the rendered output of an identity template are rejected with "permission denied"**. If your metadata value could ever contain `*` or `+`, a policy that previously granted broad access now fails closed. OpenBao shipped the same hardening as [GHSA-59w7-v8rr-pr4p](https://openbao.org/community/release-notes/2-6-0/) with opt-out flags `allow_wildcards_in_identity_templates` and `allow_slashes_in_identity_templates`. This was a genuine privilege-escalation class of bug: attacker-controlled metadata could widen a policy.

Always check policy behavior with `vault token capabilities` rather than reading HCL:

```bash
vault token capabilities <token> cells/data/usw2-042/db
# read
```

### Namespaces (Enterprise) vs path-prefix multi-tenancy

**Namespaces** (Enterprise) give each tenant a fully isolated Vault-within-Vault: its own mounts, policies, identities, and tokens, addressed by the `X-Vault-Namespace` header or a path prefix. `admin/cell-usw2-042/`. A token from one namespace is meaningless in another.

**Path-prefix multi-tenancy** is what Community edition gives you: one namespace, and you enforce isolation entirely through mount paths and policy discipline. `vault secrets enable -path=cells/usw2-042 kv-v2` plus a policy that only grants `cells/usw2-042/*`.

| | Namespaces (Enterprise) | Path prefixes (Community) |
|---|---|---|
| Isolation strength | Structural — enforced by Vault | Policy-only — one bad glob leaks |
| Delegated admin | Tenant admins can manage their own mounts/policies | Central team owns everything |
| Audit | Namespace appears in every audit entry | You infer tenancy from the path |
| Blast radius of a policy bug | Bounded to the namespace | Potentially fleet-wide |
| Cost | Enterprise license | Free |
| Mount count pressure | Distributed across namespaces | All mounts in one flat list; thousands of mounts is a real scaling concern |

OpenBao has namespaces in Community, and 2.6.0 added **namespace sealing** — per-namespace Shamir seal configuration with distinct key material, so a tenant can seal their own namespace without affecting others. That is a genuinely interesting property for a cell-per-tenant architecture and worth evaluating.

### Secrets engines

#### KV v1 vs KV v2

KV v2 is versioned. The versioning changes the API path shape, and this is the single most common Vault papercut.

| | KV v1 | KV v2 |
|---|---|---|
| API path | `secret/foo` | `secret/data/foo` for the value, `secret/metadata/foo` for versions |
| CLI path | `secret/foo` | `secret/foo` — **the CLI hides the `data/` segment; the API and policies do not** |
| Versions | None | Configurable `max_versions` (default 10) |
| Delete semantics | Gone | Soft delete, `undelete`, `destroy`, `delete_version_after` |
| Check-and-set | No | `cas` param; `cas_required=true` on the mount forces it |
| Policy gotcha | — | You must write `path "secret/data/foo"`, not `path "secret/foo"` |

```bash
vault kv enable-versioning secret/    # or: vault secrets enable -version=2 kv
vault kv put -cas=0 secret/cell-usw2/api-key value=s3cr3t   # cas=0 = create only
vault kv put -cas=1 secret/cell-usw2/api-key value=rotated  # only if current version is 1
vault kv metadata put -max-versions=5 -delete-version-after=720h secret/cell-usw2/api-key
```

Enable `cas_required` on any mount that GitOps writes to. Without it, two concurrent reconcilers silently clobber each other.

#### Transit — encryption as a service

Transit never stores your data. You send plaintext, Vault returns ciphertext with a key-version prefix (`vault:v3:...`). The application stores the ciphertext; Vault holds the key. This is how you get envelope encryption without every service holding a KEK.

```bash
vault secrets enable transit
vault write -f transit/keys/cell-metadata
vault write transit/encrypt/cell-metadata plaintext=$(base64 <<< "cell config blob")
# ciphertext: vault:v1:8SDd3W...

vault write -f transit/keys/cell-metadata/rotate     # now v2; v1 still decrypts
vault write transit/rewrap/cell-metadata ciphertext="vault:v1:8SDd3W..."
```

Key properties to know:

- **Rotation is non-destructive.** Old versions stay available for decrypt. `min_decryption_version` is how you actually retire an old key, and you must rewrap first.
- **Convergent encryption** makes the same plaintext produce the same ciphertext (deterministic nonce derived from the plaintext), which enables equality search on encrypted columns. It leaks equality — that is the whole point and the whole risk. Only enable it deliberately.
- **`derived=true`** derives a per-context key from the master key, so you can encrypt per-tenant with one key object.
- Transit is CPU-bound and every operation is a network round trip. It is not a substitute for local AES in a hot loop; Vault 2.0 added [envelope encryption](https://developer.hashicorp.com/vault/docs/secrets/transit/envelope-encryption) precisely for that — Vault protects the DEK, the app encrypts locally.

#### Database — dynamic credentials

This is the engine that matters most for Temporal's persistence layer. Instead of a static Postgres password in a Secret, each pod gets a **unique, short-lived, revocable database user**.

```bash
vault secrets enable -path=cell-usw2-042-db database

vault write cell-usw2-042-db/config/temporal \
  plugin_name=postgresql-database-plugin \
  allowed_roles="frontend,history,matching,worker" \
  connection_url="postgresql://{{username}}:{{password}}@pg.cell-usw2-042:5432/temporal?sslmode=verify-full" \
  username="vault_root" \
  password="$INITIAL_ROOT_PW" \
  password_authentication="scram-sha-256"

# Immediately rotate the root password so no human knows it.
vault write -f cell-usw2-042-db/rotate-root/temporal

vault write cell-usw2-042-db/roles/frontend \
  db_name=temporal \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; \
                       GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
  revocation_statements="REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM \"{{name}}\"; \
                         DROP ROLE IF EXISTS \"{{name}}\";" \
  default_ttl="1h" max_ttl="24h"

vault read cell-usw2-042-db/creds/frontend
```

The `rotate-root` step is the important one and it is easy to skip. After it runs, the credential in your Terraform state is dead.

Operational realities: **dynamic creds mean your database has N live users at any moment**, where N scales with pod count times (max_ttl / ttl). Postgres has a `max_connections` and a per-role overhead; a 5-minute TTL across 500 pods creates real churn. Also, `revocation_statements` must handle owned objects — a `DROP ROLE` fails if the role owns anything, and the lease then goes into a retry loop that shows up as `vault.expire.num_irrevocable_leases`.

**Static roles** are the middle ground: Vault owns and rotates a *fixed* username on a schedule, and everyone reads the same current password. Fewer database users, less revocation risk, weaker isolation.

#### PKI

The PKI engine is a full CA: generate or import a root, create intermediates, define roles that constrain what can be issued, and issue leaf certs over the API. This is the natural issuing CA behind cert-manager and gets full treatment in [11-cert-manager-and-pki.md](11-cert-manager-and-pki.md#the-vault-issuer). The key Vault-side facts:

- Roles constrain `allowed_domains`, `allow_subdomains`, `allow_bare_domains`, `allowed_uri_sans`, `key_type`, `key_bits`, `ttl`, `max_ttl`, `ext_key_usage`.
- `tidy` must be scheduled or the stored certificate list grows without bound.
- Vault 2.0 added a [Public CA integration](https://developer.hashicorp.com/vault/docs/secrets/pki-external-ca) so Vault can front a public CA, and PKI role templating got the same wildcard hardening as policies.

#### Cloud dynamic credentials

`aws`, `gcp`, and `azure` secrets engines mint short-lived cloud credentials on demand. For a multi-cloud cell fleet, these are how you stop shipping long-lived cloud keys into cells.

```bash
vault secrets enable -path=aws-usw2 aws
vault write aws-usw2/config/root region=us-west-2   # or use IRSA and omit keys entirely
vault write aws-usw2/roles/cell-provisioner \
  credential_type=assumed_role \
  role_arns=arn:aws:iam::111122223333:role/CellProvisioner \
  default_sts_ttl=15m max_sts_ttl=1h
vault read aws-usw2/creds/cell-provisioner
```

`default_sts_ttl` and `max_sts_ttl` are valid **only** for `credential_type=assumed_role` or `federation_token`. Set them alongside `iam_user` and Vault silently ignores them, leaving you with a credential bounded by nothing but the lease — which is the opposite of what you were trying to buy.

`credential_type` matters: `iam_user` creates a real IAM user (subject to a per-account quota and to IAM's eventual consistency — the classic "credentials not yet valid" retry), `assumed_role` calls STS and is faster and quota-free, `federation_token` is a middle ground. Prefer `assumed_role`. Vault 2.0 also lets Secret Sync use [workload identity federation](https://developer.hashicorp.com/vault/docs/sync) so Vault itself holds no static cloud credentials.

#### SSH and Transform

**SSH** signs user public keys with a CA that your hosts trust via `TrustedUserCAKeys`. It replaces `authorized_keys` distribution entirely and makes break-glass access auditable and time-bounded. For a cell fleet with SSH access to nodes, this is the right answer.

**Transform** (Enterprise) does format-preserving encryption, tokenization, and masking — encrypt a credit card number and get back something shaped like a credit card number. Relevant only if you have data-shape constraints in a legacy schema.

### Leases, renewal, revocation, and the lease-count explosion

Every dynamically generated secret gets a lease: `{lease_id, lease_duration, renewable}`. Vault stores the lease, tracks its expiry, and calls the engine's revoke hook when it lapses. The default `default_lease_ttl` and `max_lease_ttl` are both **768h (32 days)** ([configuration reference](https://developer.hashicorp.com/vault/docs/configuration)), which is far too long for anything a cell uses.

```hcl
# Server-wide defaults — set these low and override upward per mount.
default_lease_ttl = "1h"
max_lease_ttl     = "24h"
```

```bash
vault secrets tune -default-lease-ttl=15m -max-lease-ttl=2h cell-usw2-042-db/
vault lease renew <lease_id>
vault lease revoke <lease_id>
vault lease revoke -prefix cell-usw2-042-db/creds/    # revoke everything from a mount
```

**TTL vs max-TTL** is the part people get wrong. A lease renews up to `max_ttl` measured *from issuance*, not from the last renewal. When `max_ttl` is hit, renewal fails and the client must request a *new* credential. Applications that only implement renew, not re-fetch, break exactly once per `max_ttl` — often weeks after deploy.

**The lease-count explosion.** This is the number one Vault performance incident and it is entirely self-inflicted. Every lease is a write to the storage backend, and Vault's expiration manager holds them in memory. The failure shape:

- A misconfigured sidecar authenticates in a crash loop; each login creates a token with a lease.
- Or: a workload requests a fresh dynamic credential per *request* instead of per *process*.
- Or: `default_lease_ttl` is left at 768h, so nothing ever expires and the table grows monotonically.

Symptoms: rising `vault.expire.num_leases`, growing Raft snapshot size, unseal times climbing into minutes (the expiration manager must load every lease on unseal), and eventually memory pressure on the active node. Recovery is painful because `vault lease revoke -prefix` on a million leases is itself a massive write burst.

Defenses, in order of value:

1. **Lease count quotas (Enterprise)** — hard caps that fail closed instead of degrading the cluster. Rate-limit quotas are the Community-edition lever and are not a substitute; they bound request rate, not lease population:

```bash
# Enterprise only:
vault write sys/quotas/lease-count/cell-usw2-042 \
  path="cell-usw2-042-db/" max_leases=2000

# Community and Enterprise:
vault write sys/quotas/rate-limit/cell-usw2-042 \
  path="cell-usw2-042-db/" rate=200 interval=1s block_interval=60s
```

2. **Short TTLs everywhere**, set at the mount, not relying on server defaults.
3. **`token_num_uses`** on auth roles for one-shot workloads, so the token self-destructs.
4. **Batch tokens** for high-volume, short-lived auth. Batch tokens are not persisted and create no lease — they are signed blobs Vault validates cryptographically. The tradeoff: they cannot be renewed, cannot be revoked before expiry, and do not appear in the token store. Perfect for a request-scoped workload, wrong for a long-lived sidecar.
5. **Alert on `vault.expire.num_leases`** with an absolute threshold, not just a derivative.

### Kubernetes integration — four options

This is a real architectural decision and the options are genuinely different, not just stylistic.

| | Vault Agent Injector | Vault Secrets Operator (VSO) | Secrets Store CSI Driver | External Secrets Operator (ESO) |
|---|---|---|---|---|
| Mechanism | Mutating webhook adds init + sidecar container | Operator syncs Vault → native K8s `Secret` | CSI volume mounts secrets as files | Operator syncs provider → native K8s `Secret` |
| Secret lands in etcd? | **No** — files in a shared `emptyDir` | Yes | No (unless `secretObjects` sync enabled) | Yes |
| Rendering/templating | Yes, consul-template syntax | Basic transformation | No — raw files | Yes, templates |
| Live rotation | Sidecar re-renders; app must reload | Operator updates Secret; app must reload | Requires driver rotation feature | Operator updates Secret; app must reload |
| Env var injection | Only via sourcing a rendered file | Yes (`envFrom` a Secret) | Only via `secretObjects` → Secret | Yes |
| Per-pod overhead | +2 containers per pod | Zero pod overhead | +1 volume, node-level DaemonSet | Zero pod overhead |
| Multi-backend | Vault only | Vault only | Vault, AWS, GCP, Azure providers | Vault, AWS, GCP, Azure, ~20 more |
| GitOps / rendered manifests | Awkward — webhook mutates at admission, so rendered YAML ≠ running pod | **Good** — a CRD is a declarative object you commit | OK — volume spec is declarative | **Good** — a CRD you commit |
| Maintainer | HashiCorp | HashiCorp | Kubernetes SIG-Auth | CNCF |

**For a rendered-manifest/GitOps workflow, VSO or ESO.** The reason is drift: the Agent Injector mutates pods at admission time, which means the manifest you rendered, diffed, and approved is not the manifest that runs. Anything doing server-side diff (Argo CD, Flux with drift detection) will fight the webhook or need explicit ignore rules. A `VaultStaticSecret` or `ExternalSecret` CRD is just an object — it renders, it diffs, it reconciles.

**And the choice is narrower than it looks, because the rendered-manifest pattern rules out the encrypt-in-git alternatives entirely.** Argo CD's Source Hydrator docs are explicit: *"Do not use the source hydrator with any tool that injects secrets into your manifests as part of the hydration process (for example, Helm with SOPS or the Argo CD Vault Plugin). These secrets would be committed to git"* ([source hydrator](https://argo-cd.readthedocs.io/en/latest/user-guide/source-hydrator/)). If your delivery path renders manifests into git — which is your team's path — any scheme that resolves secrets *during* the render is off the table, and a *runtime* secrets operator is structurally forced rather than merely preferred. See [13-gitops-argocd-flux.md](13-gitops-argocd-flux.md#secrets-in-gitops) for the full comparison of what remains.

```yaml
# Vault Secrets Operator — declarative, GitOps-friendly
apiVersion: secrets.hashicorp.com/v1beta1
kind: VaultAuth
metadata:
  name: cell-auth
  namespace: cell-usw2-042
spec:
  method: kubernetes
  mount: kubernetes
  kubernetes:
    role: cell-usw2-042-frontend
    serviceAccount: temporal-frontend
    audiences: ["vault"]
---
apiVersion: secrets.hashicorp.com/v1beta1
kind: VaultDynamicSecret
metadata:
  name: temporal-db
  namespace: cell-usw2-042
spec:
  vaultAuthRef: cell-auth
  mount: cell-usw2-042-db
  path: creds/frontend
  destination:
    create: true
    name: temporal-db-creds
  rolloutRestartTargets:
    - kind: Deployment
      name: temporal-frontend
```

`rolloutRestartTargets` is the killer feature: VSO restarts the Deployment when the credential rotates, which solves the "secret updated but the app never re-read it" problem without an extra Reloader.

**Choose the Agent Injector** only when the secret genuinely must not enter etcd and you cannot use CSI — for example, a compliance requirement that says "no secret material in the API server's datastore."

**Choose CSI Driver** when you want file-mounted secrets with no etcd storage and you are already using it for other providers.

**Choose ESO** when you need one operator to speak to Vault *and* AWS Secrets Manager *and* GCP Secret Manager — which, for a three-cloud footprint, is a genuinely strong argument.

### Operations

**Initialization and recovery keys.** With auto-unseal, capture the recovery keys at init and escrow them. In an automated cell pipeline this means the pipeline briefly holds them — encrypt them to a KMS key or an escrow service immediately and never write them to CI logs or Terraform state.

```bash
vault operator init \
  -recovery-shares=5 -recovery-threshold=3 \
  -format=json > /dev/shm/init.json
# Then: encrypt to escrow, wipe, and revoke the initial root token.
vault token revoke -self
```

**Never keep a root token.** Generate one on demand with recovery keys and revoke it when done:

```bash
vault operator generate-root -init
vault operator generate-root -nonce=<nonce>   # T times, one per recovery key holder
vault operator generate-root -decode=<encoded-token> -otp=<otp>
```

**Upgrades.** Vault 2.x follows the same pattern: upgrade standbys first, then step down the active node. With Raft, upgrade one node at a time, waiting for it to rejoin and catch up before touching the next.

```bash
# On each standby: replace binary, restart, confirm it rejoins
vault status
vault operator raft list-peers

# Finally, on the active node:
vault operator step-down     # a standby takes over
# then upgrade the (now standby) former leader
```

Always read [Important changes](https://developer.hashicorp.com/vault/docs/updates/important-changes) before an upgrade. Recent examples that would bite a cell operator: Vault 2.0.2 [removed `IPC_LOCK`](https://developer.hashicorp.com/vault/docs/updates/important-changes#ipc_lock-removed) from the official container images (so `mlock` behavior changed), and Vault 2.0.0 added `max_token_header_size` on the TCP listener defaulting to **8 KB** — fine for opaque Vault tokens, potentially breaking for large OIDC tokens with big `authorization_details` claims.

Vault Community support covers only the latest minor release — a version goes EOL the day its successor ships ([1.21 went EOL on 2026-04-13](https://endoflife.date/hashicorp-vault) when 2.0 shipped). The overlapping two-release window is an Enterprise LTS feature, not a Community one. Plan cell fleet upgrades on a cadence that keeps you inside it.

**Snapshots and restore.**

```bash
vault operator raft snapshot save cell-usw2-042-$(date -u +%FT%TZ).snap
vault operator raft snapshot restore -force cell-usw2-042.snap
```

Two things people learn the hard way: (1) a snapshot is **encrypted with the barrier key**, so restoring it into a cluster with a different seal requires the original seal to be reachable — a snapshot is not a portable backup unless you plan for it. (2) `-force` is required when the restore target has a different cluster ID, which is exactly the disaster-recovery case, so `-force` is normal, not scary. Test restores. An untested restore is not a backup.

**Audit devices.** This is the classic outage, and it is documented behavior, not a bug. From the [audit docs](https://developer.hashicorp.com/vault/docs/audit): "if you have audit devices enabled and Vault cannot log information to at least one of the enabled devices, Vault refuses to service the corresponding API request. When all enabled audit devices become unavailable, Vault in effect becomes unavailable as well."

The failure: one `file` audit device pointed at a disk that fills, or one `socket` device pointed at a log collector that dies. Vault stops serving. Mitigation is explicit in the docs — **enable at least two audit devices** with independent failure modes.

```bash
vault audit enable -path=file-local file file_path=/vault/logs/audit.log
vault audit enable -path=syslog-remote syslog tag="vault" facility="AUTH"
```

Note that `sys/seal-status`, `sys/unseal`, and `sys/health` are on the [audit-exempt list](https://developer.hashicorp.com/vault/docs/audit#exempted-api-endpoints), which is the only reason you can still unseal a Vault whose audit devices are wedged. Recovery: unseal, then disable the broken device — HashiCorp has a [recover blocked audit devices](https://developer.hashicorp.com/vault/tutorials/monitoring/blocked-audit-devices) tutorial precisely because this happens often.

Also: log rotation must be `copytruncate`-free. If logrotate moves the file, Vault keeps writing to the moved inode. Send `SIGHUP` or use the `file` device's reload behavior.

**Telemetry.** Configure Prometheus and alert on the handful of metrics that actually predict incidents:

```hcl
telemetry {
  prometheus_retention_time = "24h"
  disable_hostname          = true
}
```

| Metric | Why |
|---|---|
| `vault.core.unsealed` | 0 means down. Page. |
| `vault.expire.num_leases` | Lease explosion early warning |
| `vault.expire.num_irrevocable_leases` | Revocation is failing; engine or DB problem |
| `vault.raft_storage.follower.applied_index_delta` | Follower falling behind |
| `vault.core.handle_request` (p99) | Latency, usually storage-bound |
| `vault.token.count.by_auth` | Auth loop detection |
| `vault.audit.log_request_failure` | About to become an outage |
| `vault.runtime.alloc_bytes` | Memory, dominated by the lease table |

**Rate limits and performance tuning.** Beyond lease-count quotas, `sys/quotas/rate-limit` gives per-path or per-namespace request-rate caps. Enable `enable_rate_limit_audit_logging` selectively — it is loud. For performance: put `raft` storage on fast local NVMe (Raft is fsync-bound), keep `default_lease_ttl` low, and use `elide_list_responses=true` on audit devices so a `LIST` of a million leases does not produce a megabyte audit record.

### Disaster scenarios

| Scenario | Reality | Recovery path |
|---|---|---|
| **Lost Shamir unseal keys** (below threshold) | **Unrecoverable.** The barrier key is gone. | None. Restore from a snapshot into a *new* Vault with a *new* seal is also impossible, because the snapshot is barrier-encrypted. Rebuild from source of truth. |
| **Lost recovery keys** (auto-unseal) | Vault keeps running and keeps unsealing. | Not fatal. You lose the ability to `generate-root` and to rekey. Fix by using an existing root/privileged token to... you cannot. Plan a migration to a new cluster. Escrow recovery keys. |
| **KMS key deleted** | Fatal on next restart. Running Vaults survive until restarted. | AWS KMS has a 7–30 day pending-deletion window — **cancel the deletion**. If actually deleted, the root key is unrecoverable. Prevention: KMS key policies denying `ScheduleKeyDeletion`, plus deletion protection. |
| **KMS regionally unavailable** | Existing nodes fine; restarts seal. | Wait. Or, if you planned for it, `seal` + `disabled_seal` migration to a secondary KMS key in another region. Design multi-region seal *before* the incident. |
| **Raft quorum loss** (2 of 3 nodes gone) | Vault is read-only-ish and then unavailable; no leader elected. | If nodes are recoverable, bring them back. If not, use `raft/peers.json` recovery on a surviving node to force a single-node cluster, then re-add peers. Documented under [Integrated Storage](https://developer.hashicorp.com/vault/docs/concepts/integrated-storage). Losing quorum with unrecoverable nodes means restoring the latest snapshot. |
| **Expired / lost root token** | Normal state — you should not have one. | `vault operator generate-root` with recovery/unseal key holders. |
| **Lease table too large to unseal** | Unseal hangs for minutes to hours. | Increase memory, wait it out, then aggressively `lease revoke -prefix` and set quotas. In extreme cases, restore an older snapshot. |
| **All audit devices failed** | Every write is refused; reads too. | Unseal endpoints are audit-exempt. Fix the sink or `vault audit disable` the broken path using a token you already hold. |

The pattern across all of these: **Vault's failure modes are mostly unrecoverable-by-design**. There is no "just reset the password." Your runbooks should therefore be heavily weighted toward *prevention and drills*, not toward heroic recovery.

### The BUSL license change and OpenBao

In August 2023, HashiCorp moved Vault (and Terraform, Consul, Nomad, and others) from **MPL 2.0 to the Business Source License 1.1**. BUSL is not an OSI-approved open source license: it permits use except to provide a competing hosted service, converting to MPL 2.0 four years after each release. For a company whose product *is* a hosted service, that clause deserves an actual legal read rather than an assumption.

**IBM completed its acquisition of HashiCorp in February 2025** ([IBM newsroom](https://newsroom.ibm.com/2025-02-27-ibm-completes-acquisition-of-hashicorp,-creates-comprehensive,-end-to-end-hybrid-cloud-platform)), a $6.4B deal. The visible effects in the docs today: support routes to `ibm.com/mysupport`, and Vault 2.0 added [IBM Passport Advantage license support](https://developer.hashicorp.com/vault/docs/configuration/license-entitlement). Licensing has moved toward workload/machine-identity-based pricing, which for a fleet of per-cell Vaults is a number worth modeling before you commit to the topology.

**OpenBao** is the fork. It is under **Linux Foundation** governance as an **OpenSSF Sandbox** project, licensed **MPL 2.0**, and it is real: [2.6.0 shipped 2026-07-14](https://openbao.org/community/release-notes/2-6-0/), with 2.6.2 on 2026-08-18. Its trajectory has diverged from Vault rather than just tracking it:

- **Namespaces in Community** (Vault requires Enterprise), plus **namespace sealing** in 2.6.0 — per-namespace Shamir keys, so a tenant can seal their own partition.
- **Horizontal read scalability** — standbys serve reads, shipped in 2.5.0. Vault charges for this as performance standbys.
- **Pluggable auto-unseal** via a new `kms` plugin type; built-in cloud seals are deprecated for removal in 2.7.0.
- **Declarative self-initialization** and **Workflows** (`sys/workflows`), which are genuinely interesting for unattended cell provisioning: a Vault that configures itself from its own config file removes a whole class of bootstrap orchestration.
- **Transactional storage** and a CEL policy engine, neither of which Vault has.
- Deprecations to watch: the **file storage backend is deprecated for removal in 2.7.0**, and LDAP/Kerberos/RADIUS plugins are moving out of the main binary.

The practical assessment for a platform team: OpenBao's API is Vault-compatible enough that migration is realistic, the namespace-in-community story materially changes the cell-local-vs-central calculus, and MPL 2.0 removes the "are we a competing service" question entirely. The counterweight is ecosystem maturity — VSO, the Agent Injector, and much third-party tooling target Vault. This is a decision worth an explicit evaluation rather than a default.

### Terraform's Vault provider and the state problem

The [Vault provider](https://registry.terraform.io/providers/hashicorp/vault/latest/docs) is the right way to manage Vault *configuration*: mounts, auth backends, roles, policies. It is the wrong way to manage Vault *secrets*.

The reason is stated in the provider's own docs: **Terraform stores every value it reads or writes in state, in plaintext.** A `vault_generic_secret` data source, or a `vault_database_secret_backend_static_role` whose password Terraform reads, puts that secret in `terraform.tfstate` forever, including in every historical state version your backend retains.

```hcl
# GOOD — configuration only. No secret values cross the boundary.
resource "vault_mount" "cell_db" {
  path = "cell-${var.cell_id}-db"
  type = "database"
}

resource "vault_database_secret_backend_role" "frontend" {
  backend             = vault_mount.cell_db.path
  name                = "frontend"
  db_name             = vault_database_secret_backend_connection.temporal.name
  creation_statements = [file("${path.module}/sql/create_frontend.sql")]
  default_ttl         = 900
  max_ttl             = 7200
}

resource "vault_policy" "cell_frontend" {
  name   = "cell-${var.cell_id}-frontend"
  policy = templatefile("${path.module}/policy.hcl.tftpl", { cell_id = var.cell_id })
}
```

```hcl
# BAD — this password is now permanently in state.
data "vault_generic_secret" "db" {
  path = "secret/cell-${var.cell_id}/db"
}
resource "kubernetes_secret" "db" {
  data = { password = data.vault_generic_secret.db.data["password"] }
}
```

Mitigations, roughly in order:

1. **Do not read secrets in Terraform.** Let the workload fetch them at runtime via VSO/ESO/Agent. This is the actual fix.
2. If you must, the provider's `set_namespace`/short-lived token config with `max_lease_ttl_seconds` at least bounds the Terraform *client's* credential lifetime.
3. Encrypt state at rest with a customer-managed KMS key, restrict who can read the backend, and **enable versioning with a lifecycle policy** — old state versions are the forgotten leak.
4. Use ephemeral resources / write-only arguments where the provider supports them (Terraform 1.10+ / 1.11+), which keep values out of state by design.

There is also a subtler Terraform-and-Vault problem specific to cell teardown: `terraform destroy` deletes the mount, which revokes leases *asynchronously*. If the database is torn down in the same apply, revocation fails, leases become irrevocable, and they accumulate in the surviving Vault. Order teardown explicitly: revoke leases, then delete mounts, then delete the database.

---

## Hands-on

Everything below runs on a laptop with Docker and `kind`. Times are rough.

### Lab 0 — Dev server, 10 minutes

```bash
brew install vault    # or: https://developer.hashicorp.com/vault/install
vault server -dev -dev-root-token-id=root
```

In a second terminal:

```bash
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=root

vault status
vault secrets list -detailed

# KV v2 and the data/ path gotcha, made concrete
vault kv put secret/hello foo=world
vault kv get secret/hello
curl -s -H "X-Vault-Token: root" $VAULT_ADDR/v1/secret/hello | jq       # 404!
curl -s -H "X-Vault-Token: root" $VAULT_ADDR/v1/secret/data/hello | jq  # works

# Policies
cat > readonly.hcl <<'EOF'
path "secret/data/hello" { capabilities = ["read"] }
EOF
vault policy write readonly readonly.hcl
RO=$(vault token create -policy=readonly -ttl=5m -field=token)
VAULT_TOKEN=$RO vault kv get secret/hello        # ok
VAULT_TOKEN=$RO vault kv put secret/hello a=b    # permission denied

# Prove your policy reasoning with the API, not by reading HCL
vault token capabilities $RO secret/data/hello
```

**Exercise:** write a policy using `+` and one using `*` and predict which paths each matches before running `vault token capabilities`. Get it wrong at least once — that is the point.

### Lab 1 — A real 3-node Raft cluster in Docker, 45 minutes

```bash
mkdir -p ~/vault-lab && cd ~/vault-lab
docker network create vaultnet

for i in 0 1 2; do
  mkdir -p node$i/data node$i/config
  cat > node$i/config/vault.hcl <<EOF
ui = true
disable_mlock = true

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

storage "raft" {
  path    = "/vault/data"
  node_id = "node$i"
  retry_join { leader_api_addr = "http://vault0:8200" }
  retry_join { leader_api_addr = "http://vault1:8200" }
  retry_join { leader_api_addr = "http://vault2:8200" }
}

cluster_addr = "http://vault$i:8201"
api_addr     = "http://vault$i:8200"
EOF
done

for i in 0 1 2; do
  docker run -d --name vault$i --network vaultnet \
    -p $((8200 + i * 10)):8200 \
    -v "$PWD/node$i/config:/vault/config" \
    -v "$PWD/node$i/data:/vault/data" \
    hashicorp/vault:latest server
done
```

Note the absence of `--cap-add=IPC_LOCK`: the official images dropped that capability in Vault 2.0.2 (gotcha 19 below), and the config above already sets `disable_mlock = true`, so it would be a no-op even if the images still carried it.

Initialize on node 0 only:

```bash
export VAULT_ADDR=http://127.0.0.1:8200
vault operator init -key-shares=3 -key-threshold=2 -format=json > init.json
jq -r '.unseal_keys_b64[]' init.json
export VAULT_TOKEN=$(jq -r .root_token init.json)

for k in $(jq -r '.unseal_keys_b64[0,1]' init.json); do vault operator unseal "$k"; done
vault status
```

Unseal the other two the same way (`VAULT_ADDR=http://127.0.0.1:8210` and `:8220`), then:

```bash
export VAULT_ADDR=http://127.0.0.1:8200
vault operator raft list-peers
vault operator raft autopilot state
```

**Now break it, deliberately.**

```bash
# 1. Kill the leader. Watch failover.
docker stop vault0
VAULT_ADDR=http://127.0.0.1:8210 vault status      # a new leader exists
VAULT_ADDR=http://127.0.0.1:8210 vault operator raft list-peers

# 2. Kill a second node. Quorum is gone.
docker stop vault1
VAULT_ADDR=http://127.0.0.1:8220 vault status      # no leader; writes fail
VAULT_ADDR=http://127.0.0.1:8220 vault kv put secret/x y=z    # observe the error

# 3. Recover.
docker start vault0 vault1
# Each restarted node is SEALED — Shamir means manual unseal. This is exactly
# why cells use auto-unseal.
```

**Exercise:** repeat step 3, but time how long the whole recovery takes with manual unseal. That number is your MTTR for a Shamir cell, and it is the argument for auto-unseal in one line.

Snapshots:

```bash
export VAULT_ADDR=http://127.0.0.1:8200
vault kv put secret/before-snapshot value=original
vault operator raft snapshot save lab.snap
vault kv put secret/before-snapshot value=changed
vault operator raft snapshot restore -force lab.snap
vault kv get secret/before-snapshot   # value=original
```

### Lab 2 — Kubernetes auth on kind, 45 minutes

```bash
kind create cluster --name vault-lab

helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update
helm install vault hashicorp/vault \
  --namespace vault --create-namespace \
  --set "server.dev.enabled=true" \
  --set "server.dev.devRootToken=root" \
  --set "injector.enabled=false"

kubectl -n vault wait --for=condition=Ready pod/vault-0 --timeout=180s
```

Configure Kubernetes auth using the *local token* pattern (the recommended one):

```bash
kubectl -n vault exec -it vault-0 -- sh -c '
  export VAULT_TOKEN=root
  vault auth enable kubernetes
  vault write auth/kubernetes/config \
      kubernetes_host="https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT"
  vault read auth/kubernetes/config
'
```

Note there is no `token_reviewer_jwt` and no `kubernetes_ca_cert` — Vault reads its own projected token and CA from disk and re-reads them as they rotate. Confirm `disable_iss_validation` is `true`:

```bash
kubectl -n vault exec vault-0 -- sh -c \
  'VAULT_TOKEN=root vault read -field=disable_iss_validation auth/kubernetes/config'
```

Vault's ServiceAccount needs `TokenReview`. The chart already grants it — `server.authDelegator.enabled` defaults to `true` — so verify rather than create a second, differently-named binding (which is what makes a re-run of this lab fail with `AlreadyExists`):

```bash
kubectl get clusterrolebinding -o name | grep -i vault
# If, and only if, nothing is bound:
# kubectl create clusterrolebinding vault-auth-delegator \
#   --clusterrole=system:auth-delegator --serviceaccount=vault:vault
```

Create a workload identity and a policy:

```bash
kubectl create namespace cell-usw2-042
kubectl -n cell-usw2-042 create serviceaccount temporal-frontend

kubectl -n vault exec -it vault-0 -- sh -c '
  export VAULT_TOKEN=root
  vault kv put secret/cells/usw2-042/config datacenter=usw2 shard_count=512

  cat <<EOF | vault policy write cell-usw2-042 -
path "secret/data/cells/usw2-042/*" { capabilities = ["read"] }
EOF

  vault write auth/kubernetes/role/cell-usw2-042 \
    bound_service_account_names=temporal-frontend \
    bound_service_account_namespaces=cell-usw2-042 \
    token_policies=cell-usw2-042 \
    audience=vault \
    token_ttl=20m
'
```

Log in from a pod:

```bash
kubectl -n cell-usw2-042 run probe --rm -it --restart=Never \
  --image=hashicorp/vault:latest \
  --overrides='{"spec":{"serviceAccountName":"temporal-frontend"}}' \
  -- sh -c '
    export VAULT_ADDR=http://vault.vault.svc:8200
    JWT=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
    vault write -field=token auth/kubernetes/login role=cell-usw2-042 jwt=$JWT > /tmp/t
    export VAULT_TOKEN=$(cat /tmp/t)
    vault kv get secret/cells/usw2-042/config
    vault kv get secret/cells/usw2-999/config   # expect: permission denied
  '
```

**Exercises:**

1. Change `bound_service_account_namespaces` to a different namespace and watch the login fail. Read the error carefully — it tells you exactly which bound field rejected you.
2. Add `use_annotations_as_alias_metadata=true` to the auth config, annotate the SA with `vault.hashicorp.com/alias-metadata-cell_id: usw2-042`, and rewrite the policy as a template using `{{identity.entity.aliases.<accessor>.metadata.cell_id}}`. Get the mount accessor from `vault auth list -format=json | jq -r '.["kubernetes/"].accessor'`. This is the pattern that scales to N cells with one policy.
3. Set the annotation value to something containing `*` and observe the Vault 2.0.1+ behavior: permission denied, because wildcards in rendered templates are now rejected.

### Lab 3 — Dynamic Postgres credentials, 45 minutes

```bash
docker run -d --name pg --network vaultnet \
  -e POSTGRES_PASSWORD=rootpw -e POSTGRES_DB=temporal \
  -p 5432:5432 postgres:16

docker exec -i pg psql -U postgres -d temporal <<'SQL'
CREATE TABLE executions (id bigserial primary key, payload text);
INSERT INTO executions (payload) VALUES ('hello');
CREATE ROLE vault_root WITH LOGIN PASSWORD 'bootstrap' CREATEROLE;
GRANT ALL PRIVILEGES ON DATABASE temporal TO vault_root;
GRANT ALL ON ALL TABLES IN SCHEMA public TO vault_root WITH GRANT OPTION;
SQL
```

Against the dev server (Lab 0) or your Raft cluster. Mind which one: `host.docker.internal` only resolves *inside* a container, so it is right when you configure from a Vault container on `vaultnet` — but the Lab 0 dev server runs natively on the host, where the address to use is `127.0.0.1`. From a Vault container on `vaultnet`, `pg:5432` is simpler still, since Postgres is on the same docker network.

```bash
export VAULT_ADDR=http://127.0.0.1:8200
vault secrets enable -path=cell-db database

vault write cell-db/config/temporal \
  plugin_name=postgresql-database-plugin \
  allowed_roles="frontend" \
  connection_url="postgresql://{{username}}:{{password}}@127.0.0.1:5432/temporal?sslmode=disable" \
  username="vault_root" \
  password="bootstrap"

# The step everyone skips. After this, 'bootstrap' is dead.
vault write -f cell-db/rotate-root/temporal

vault write cell-db/roles/frontend \
  db_name=temporal \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; \
                       GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
  revocation_statements="REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM \"{{name}}\"; \
                         DROP ROLE IF EXISTS \"{{name}}\";" \
  default_ttl="2m" max_ttl="6m"

vault read cell-db/creds/frontend
```

Now watch the whole lifecycle:

```bash
CREDS=$(vault read -format=json cell-db/creds/frontend)
USER=$(jq -r .data.username <<< "$CREDS")
PASS=$(jq -r .data.password <<< "$CREDS")
LEASE=$(jq -r .lease_id <<< "$CREDS")

docker exec -i pg psql -U postgres -c "\du" | grep "$USER"
PGPASSWORD=$PASS psql -h localhost -U "$USER" -d temporal -c "SELECT count(*) FROM executions;"

vault lease lookup "$LEASE"
vault lease renew "$LEASE"          # extends, up to max_ttl from ISSUANCE
sleep 130
docker exec -i pg psql -U postgres -c "\du" | grep "$USER"   # gone
```

**Exercises:**

1. Renew the lease in a loop until it refuses. Note the total elapsed time equals `max_ttl` from issuance, *not* from the last renew. This is the bug that breaks apps weeks after deploy.
2. Set a **rate-limit** quota and blow through it. (Lease-count quotas are Enterprise-only, so they will not work against a Community dev server — this is the Community equivalent.)

```bash
vault write sys/quotas/rate-limit/cell-db path="cell-db/" rate=2 interval=1s block_interval=60s
for i in $(seq 1 8); do vault read cell-db/creds/frontend > /dev/null || echo "blocked at $i"; done
```

3. Make revocation fail: create a table *owned by* a dynamic role, then let the lease expire. `DROP ROLE` fails, the lease goes irrevocable, and `vault list sys/leases/lookup/cell-db/creds/frontend` shows the corpse. Then fix it with `vault lease revoke -force -prefix cell-db/creds/frontend` and understand why `-force` is dangerous (it drops Vault's record without the database actually cleaning up).

### Lab 4 — Transit, 20 minutes

```bash
vault secrets enable transit
vault write -f transit/keys/cell-meta

CT=$(vault write -field=ciphertext transit/encrypt/cell-meta \
       plaintext=$(printf 'shard_count=512' | base64))
echo "$CT"    # vault:v1:...

vault write -f transit/keys/cell-meta/rotate
vault read transit/keys/cell-meta      # latest_version: 2

# v1 ciphertext still decrypts
vault write -field=plaintext transit/decrypt/cell-meta ciphertext="$CT" | base64 -d

# Rewrap to v2 without ever seeing plaintext
CT2=$(vault write -field=ciphertext transit/rewrap/cell-meta ciphertext="$CT")
echo "$CT2"   # vault:v2:...

# Now actually retire v1
vault write transit/keys/cell-meta/config min_decryption_version=2
vault write transit/decrypt/cell-meta ciphertext="$CT"   # fails — as intended
```

**Exercise:** enable a second key with `convergent_encryption=true derived=true`, encrypt the same plaintext twice with the same context, and confirm the ciphertexts are identical. Then encrypt with a different context and confirm they differ. Write down in one sentence what an attacker with read access to your database learns.

### Lab 5 — Wedge the audit device, 10 minutes

The most valuable 10 minutes in this guide.

```bash
# On the Raft cluster
docker exec vault0 sh -c 'mkdir -p /vault/logs'
vault audit enable file file_path=/vault/logs/audit.log
vault kv put secret/works fine=yes    # ok

# Break the only audit device
docker exec vault0 sh -c 'rm -rf /vault/logs'
vault kv put secret/breaks now=true
# => 500. Vault refuses the write because it cannot audit it.

# Reads too:
vault kv get secret/works

# But these still work — they are audit-exempt:
vault status
curl -s $VAULT_ADDR/v1/sys/health | jq

# Recover
docker exec vault0 sh -c 'mkdir -p /vault/logs'
vault kv put secret/works fine=yes
```

Then do it right:

```bash
# The parent directory must exist first -- Vault creates the log file, not the
# directory, and `audit enable` fails outright if the path is not writable.
docker exec vault0 sh -c 'mkdir -p /vault/logs2'
vault audit enable -path=file2 file file_path=/vault/logs2/audit.log
vault audit list -detailed
# Break one; Vault keeps serving because one device still succeeds.
docker exec vault0 sh -c 'rm -rf /vault/logs'
vault kv put secret/still-works yes=please
```

Two directories in one container are not *independent* failure modes — they share a disk. The point here is only the mechanism. In a real cell, put the second device on a different backend entirely (socket or syslog), so a full disk cannot take both out at once.

---

## Production gotchas

1. **All audit devices failing makes Vault unavailable — this is documented behavior, not a bug.** Vault "refuses to service the corresponding API request" if it cannot write to at least one enabled device, and "when all enabled audit devices become unavailable, Vault in effect becomes unavailable as well." Always run at least two with independent failure modes. Source: [Audit devices — Availability](https://developer.hashicorp.com/vault/docs/audit#availability-of-audit-devices) and the [recover blocked audit devices](https://developer.hashicorp.com/vault/tutorials/monitoring/blocked-audit-devices) tutorial.

2. **`default_lease_ttl` and `max_lease_ttl` default to 768h (32 days).** Leave them alone and your lease table grows for a month before anything expires. Set them to hours at the server level and tune upward per mount. Source: [Vault configuration parameters](https://developer.hashicorp.com/vault/docs/configuration).

3. **`max_ttl` is measured from issuance, not from the last renewal.** An application that only implements renew will fail exactly once per `max_ttl` window, usually long after deploy, usually at 3am. Implement re-fetch. Source: [Tune the lease TTL](https://developer.hashicorp.com/vault/docs/troubleshoot/tune-lease-ttl).

4. **KV v2 policy paths need `data/` but the CLI hides it.** `vault kv get secret/foo` works while `path "secret/foo"` in a policy does not — you need `path "secret/data/foo"`. Metadata operations need `secret/metadata/foo` separately. Source: [KV v2 docs](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2).

5. **Since Vault 2.0.1, wildcards and globs in *rendered* identity-template output are rejected with permission denied.** If a service account annotation or entity metadata value can contain `*` or `+`, policies that used to work now fail closed. OpenBao shipped the same fix as [GHSA-59w7-v8rr-pr4p](https://openbao.org/community/release-notes/2-6-0/) with explicit opt-out flags. Source: [Vault 2.0.1 release notes](https://developer.hashicorp.com/vault/docs/updates/release-notes#vault-2-0-1).

6. **Kubernetes 1.21+ requires `disable_iss_validation=true`, and mounts created before Vault 1.9 keep the old default.** The Kubernetes API already validates the issuer during `TokenReview`; Vault doing it again breaks on cluster-specific issuers. Check with `vault read -field=disable_iss_validation auth/kubernetes/config` on every existing mount, not just new ones. Source: [Kubernetes auth — Kubernetes 1.21](https://developer.hashicorp.com/vault/docs/auth/kubernetes#kubernetes-1-21).

7. **A short-lived `token_reviewer_jwt` silently expires and takes down all Kubernetes auth.** If Vault runs in the cluster, omit `token_reviewer_jwt` and `kubernetes_ca_cert` entirely so Vault re-reads its own projected token from disk — but this requires **Vault 1.9.3+**; earlier versions read it once and cache it. Source: [How to work with short-lived Kubernetes tokens](https://developer.hashicorp.com/vault/docs/auth/kubernetes#how-to-work-with-short-lived-kubernetes-tokens).

8. **Kubernetes extends admission-injected service account token lifetimes to one year by default.** You think you have short-lived tokens; you do not. Set `--service-account-extend-token-expiration=false` on kube-apiserver or mount your own `serviceAccountToken` projected volume with an explicit expiry. Source: same as above.

9. **Autopilot dead-server cleanup is off by default.** An autoscaler that replaces a node leaves a phantom voter counted in quorum math, so a "3-node" cluster silently becomes a 4-peer cluster that still only tolerates one failure. Enable it explicitly with `vault operator raft autopilot set-config -cleanup-dead-servers=true -min-quorum=3` — `min-quorum` has no default and cleanup stays inert without it. Source: [Integrated Storage autopilot](https://developer.hashicorp.com/vault/docs/concepts/integrated-storage/autopilot).

10. **Raft snapshots are barrier-encrypted, so they are not portable across seals.** Restoring a snapshot into a new cluster requires the original seal to be reachable. Plan seal migration *before* you need the restore, and test it. Source: [Integrated Storage internals](https://developer.hashicorp.com/vault/docs/internals/integrated-storage).

11. **Auto-unseal turns your cloud KMS into a hard startup dependency, including its network path.** No VPC endpoint / Private Service Connect / Private Link means no unseal. A running Vault survives a KMS outage (the root key is in memory); a restarted one does not. This is why the failure always presents as "everything was fine until the rolling restart."

12. **Deleting the KMS key is fatal, but there is a grace window.** AWS KMS enforces a 7–30 day pending-deletion period — cancel it. Add a key policy that denies `kms:ScheduleKeyDeletion` to everyone except a break-glass principal. Source: [AWS KMS seal](https://developer.hashicorp.com/vault/docs/configuration/seal/awskms).

13. **Recovery keys are not unseal keys.** With auto-unseal, `vault operator init` emits recovery keys. They cannot unseal Vault. They authorize `generate-root`, rekey, and seal migration. Losing them is not fatal but is permanent: you can never mint another root token on that cluster. Source: [Seal/unseal concepts](https://developer.hashicorp.com/vault/docs/concepts/seal).

14. **`vault operator rotate` and `vault operator rekey` are different operations.** Rotate changes the encryption key (online, cheap, do it regularly). Rekey changes the unseal/recovery shares (a ceremony requiring the current threshold). People say "rotate the unseal keys" and then run the wrong command.

15. **Dynamic database credentials create real database users, subject to real database limits.** A 5-minute TTL across hundreds of pods means constant `CREATE ROLE`/`DROP ROLE` churn and a live user count of roughly `pods × (max_ttl / ttl)`. Watch `max_connections` and role-table bloat.

16. **`DROP ROLE` fails if the role owns objects, and the lease becomes irrevocable.** Watch `vault.expire.num_irrevocable_leases`. Write `revocation_statements` that `REASSIGN OWNED BY` and `DROP OWNED BY` before dropping, or ensure dynamic roles never own anything.

17. **AWS `iam_user` credential type hits IAM eventual consistency and a per-account user quota.** Newly created access keys are frequently rejected for a few seconds. Prefer `credential_type=assumed_role`, which uses STS and avoids both problems. Source: [AWS secrets engine](https://developer.hashicorp.com/vault/docs/secrets/aws).

18. **Vault 2.0.0 added `max_token_header_size` on the TCP listener, defaulting to 8 KB.** Opaque Vault tokens are fine; Enterprise JWT/OIDC tokens with large `authorization_details` claims can exceed it and get rejected at the listener with a confusing error. Raise it proactively if you use them. Source: [Vault 2.0.0 release notes](https://developer.hashicorp.com/vault/docs/updates/release-notes#vault-2-0-0).

19. **Vault 2.0.2 removed `IPC_LOCK` from the official container images.** If your deployment relied on `mlock` being available in-container, behavior changed. Most Kubernetes deployments set `disable_mlock = true` anyway, but check. Source: [Important changes](https://developer.hashicorp.com/vault/docs/updates/important-changes#ipc_lock-removed).

20. **Terraform's Vault provider writes every secret it touches into state in plaintext, forever, including in old state versions.** Use the provider for configuration only. If you must read a secret, encrypt state with a CMK, lock down the backend, and apply a lifecycle policy to historical versions. Source: [Vault provider docs](https://registry.terraform.io/providers/hashicorp/vault/latest/docs).

21. **`terraform destroy` on a Vault mount revokes leases asynchronously.** If the backing database is destroyed in the same apply, revocation fails and irrevocable leases accumulate. Order cell teardown explicitly: revoke → delete mount → delete database.

22. **The Vault Agent Injector fights GitOps drift detection.** It mutates pods at admission, so the rendered manifest never matches the running pod. Argo CD and Flux will show permanent drift or need explicit ignore rules. Prefer Vault Secrets Operator or External Secrets Operator for rendered-manifest workflows.

23. **Vault Community's support window is exactly one minor release — the latest.** A version goes EOL the day its successor ships; the two-release overlap applies only to Enterprise LTS. [Vault 1.21 went EOL on 2026-04-13](https://endoflife.date/hashicorp-vault) when 2.0 shipped. A fleet of per-cell Vaults on different versions will silently accumulate unsupported clusters unless upgrade rollout is part of the cell lifecycle, not a side project.

24. **Logrotate with `copytruncate` breaks the file audit device.** Vault holds the file descriptor; moving or truncating the file underneath it means audit records go into a hole. Use the device's reload path or `SIGHUP`, and verify by tailing after a rotation.

25. **BUSL 1.1 is not open source and the "competing hosted service" clause is not decorative** for a company that sells hosted Temporal. Vault has shipped under BUSL since August 2023, and HashiCorp is now part of IBM. Get an actual legal read rather than inheriting an assumption. Source: [IBM completes acquisition of HashiCorp](https://newsroom.ibm.com/2025-02-27-ibm-completes-acquisition-of-hashicorp,-creates-comprehensive,-end-to-end-hybrid-cloud-platform).

---

## How this shows up in cell lifecycle

**Provisioning.** The dependency order is rigid and the whole cell blocks on it:

1. Create the cloud KMS key (and its deletion protections) and the network path to the KMS endpoint. If the networking team owns the endpoint, this is a cross-team handoff on the critical path.
2. Create the Vault node identity — IRSA role / GCP Workload Identity binding / Azure managed identity — with `Decrypt` and `Encrypt` on that key and nothing else.
3. Start Vault with the `seal` stanza. Confirm it auto-unseals. If it does not, you have a networking or IAM problem, not a Vault problem — check in that order.
4. `vault operator init` with recovery shares. **Immediately** encrypt the recovery keys to escrow and revoke the initial root token. Never let them touch CI logs or Terraform state.
5. Apply the cell's Vault configuration with Terraform: mounts, auth methods, roles, policies. Configuration only — no secret values cross the Terraform boundary.
6. Configure the database engine and `rotate-root` so the bootstrap password in your pipeline is dead before the pipeline exits.
7. Enable **two** audit devices before any workload authenticates.
8. Register the cluster with monitoring; verify `vault.core.unsealed` is reporting before declaring the cell ready.

**Steady state.** Dynamic credentials mean the cell has no long-lived secrets to rotate — which is the point, and the thing to protect. The recurring work is lease hygiene (quotas, TTL audits, watching `num_irrevocable_leases`), snapshot verification (a restore drill per quarter, not just a backup job), and upgrade rollout across N cells on a cadence that stays inside the support window.

**Upgrading a cell.** Vault upgrade is part of the cell upgrade, and it is the piece that can strand the cell. Standbys first, `step-down` last, one node at a time, waiting for Raft catch-up. Read [Important changes](https://developer.hashicorp.com/vault/docs/updates/important-changes) first — the last three releases alone changed container capabilities, added a token header size limit, and hardened identity templates. Any of those can break a cell that was working.

**Scaling.** Vault does not scale writes by adding nodes. If a cell's Vault is write-bound, the cause is almost always lease churn or an auth loop, not capacity. Fix the workload before adding hardware. Reads scale via performance standbys (Enterprise) or OpenBao's read-serving standbys.

**Teardown.** The step people skip, and it leaves debris in three places. Revoke leases first (`vault lease revoke -prefix` per mount) so external systems — databases, cloud IAM — actually clean up. Then delete mounts, auth roles, and policies. Then destroy the Vault. Then, and only then, schedule deletion of the KMS key, and remember it has a pending-deletion window during which the cell could still be resurrected. A cell whose Vault was destroyed before its leases were revoked leaves orphaned IAM users and database roles that nobody will ever find.

**Cross-cloud.** The seal stanza is the only genuinely cloud-specific part of a cell's Vault config; everything above it is portable. That is an argument for keeping the rest of your Vault configuration cloud-agnostic and templating only the seal — one Terraform module, three seal variants.

---

## Learning path

### Day 1 (3–4 hours)

- Read [What is Vault](https://developer.hashicorp.com/vault/docs/what-is-vault), [Seal/Unseal](https://developer.hashicorp.com/vault/docs/concepts/seal), [Policies](https://developer.hashicorp.com/vault/docs/concepts/policies), and [Leases](https://developer.hashicorp.com/vault/docs/concepts/lease). That is the whole conceptual core.
- Run **Lab 0**. Specifically, hit the KV v2 `data/` path gotcha with `curl` so it lands physically rather than as trivia.
- Run **Lab 5** (audit wedge). Ten minutes, and it inoculates you against the most embarrassing Vault outage.
- Be able to explain, out loud: barrier vs root key vs encryption key vs unseal share; why Vault fails closed; why adding nodes does not add write throughput.

### Week 1 (10–15 hours)

- Run **Labs 1–4** end to end. Do the "break it" exercises; they are where the learning is.
- Read the [Kubernetes auth docs](https://developer.hashicorp.com/vault/docs/auth/kubernetes) in full, including the short-lived-token comparison table. Then read the [JWT/OIDC with Kubernetes](https://developer.hashicorp.com/vault/docs/auth/jwt/oidc-providers/kubernetes) page and form an opinion about which one a central Vault should use.
- Read the [Raft reference architecture](https://developer.hashicorp.com/vault/tutorials/day-one-raft/raft-reference-architecture) and the [autopilot docs](https://developer.hashicorp.com/vault/docs/concepts/integrated-storage/autopilot).
- Write a Terraform module that configures one cell's Vault: mount, database engine, role, policy, Kubernetes auth role. Apply it twice with different `cell_id` values. Confirm nothing secret is in state (`terraform show -json | jq` and grep).
- Read the [Vault 2.x release notes](https://developer.hashicorp.com/vault/docs/updates/release-notes) and [Important changes](https://developer.hashicorp.com/vault/docs/updates/important-changes) cover to cover. This is the single highest-value hour for someone joining a team that already runs Vault.

### Month 1 (ongoing)

- **Write the runbooks and then drill them.** Quorum loss. Lost recovery keys. KMS unavailable. Audit wedge. Lease explosion. Each one should be a page with commands, and each should be executed against a scratch cell at least once.
- Build the **cell teardown** path and verify it leaves nothing behind: query the database for orphaned roles and IAM for orphaned users after a teardown.
- Instrument the metrics table above and set thresholds. Alert on `vault.expire.num_leases` with an absolute number derived from your own lab measurements, not a guess.
- Do a **seal migration** in the lab: Shamir → auto-unseal, and auto-unseal → a different KMS key. You will need this eventually and you do not want the first attempt to be an incident.
- Evaluate **OpenBao** seriously against your topology. The specific questions: does Community namespaces change your cell-local-vs-central decision? Does namespace sealing map onto per-tenant isolation? Does declarative self-initialization simplify unattended cell bootstrap? Does MPL 2.0 remove a legal question you currently have?
- Read the [security model](https://developer.hashicorp.com/vault/docs/internals/security) and [threat model](https://developer.hashicorp.com/vault/docs/internals/security#threat-model) and write down, for your architecture, what Vault does *not* protect against. Most teams cannot answer this and should be able to.

---

## References

1. [What is Vault? — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/what-is-vault) — the orientation page; read it once and never again.
2. [Vault security model — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/internals/security) — states explicitly that the storage backend is untrusted; the basis for every architectural argument in this guide.
3. [Seal/Unseal concepts — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/concepts/seal) — barrier, root key, encryption key, unseal shares, recovery keys.
4. [AWS KMS seal configuration — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/configuration/seal/awskms) — the auto-unseal stanza and its IAM requirements.
5. [GCP Cloud KMS seal configuration — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/configuration/seal/gcpckms) — GCP equivalent.
6. [Azure Key Vault seal configuration — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/configuration/seal/azurekeyvault) — Azure equivalent; prefer managed identity.
7. [Seal wrap — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/enterprise/sealwrap) — extra wrapping for sensitive mounts (Enterprise/HSM).
8. [Integrated Storage internals — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/internals/integrated-storage) — Raft inside Vault, snapshots, peer recovery.
9. [Vault with Integrated Storage reference architecture — HashiCorp Developer](https://developer.hashicorp.com/vault/tutorials/day-one-raft/raft-reference-architecture) — the 3-or-5-node recommendation and network layout.
10. [Integrated Storage autopilot — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/concepts/integrated-storage/autopilot) — voter promotion, stability thresholds, and the dead-server-cleanup default.
11. [Vault configuration parameters — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/configuration) — where `default_lease_ttl = 768h` and the listener options live.
12. [Tune the lease TTL — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/troubleshoot/tune-lease-ttl) — TTL vs max-TTL semantics and mount tuning.
13. [Lease, renew, and revoke — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/concepts/lease) — the lease lifecycle that drives Vault's write load.
14. [Policies — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/concepts/policies) — HCL syntax, capabilities, `*` vs `+`, templated policies.
15. [Kubernetes auth method — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/auth/kubernetes) — `TokenReview` flow, the 1.21 changes, and the short-lived reviewer JWT comparison table. The most important auth page for cell infrastructure.
16. [JWT/OIDC with Kubernetes as OIDC provider — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/auth/jwt/oidc-providers/kubernetes) — the reviewer-free alternative for a Vault outside the cluster.
17. [AWS auth method — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/auth/aws) — the signed-`GetCallerIdentity` verification trick.
18. [AppRole auth method — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/auth/approle) — RoleID/SecretID and why response wrapping is mandatory.
19. [Response wrapping — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/concepts/response-wrapping) — single-use tokens for handing off secret zero.
20. [KV v2 secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2) — the `data/` path, versioning, CAS.
21. [Transit secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/transit) — encryption as a service, rotation, convergent encryption.
22. [Transit envelope encryption — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/transit/envelope-encryption) — new in Vault 2.0; local encryption with Vault-protected DEKs.
23. [Database secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/databases) — dynamic credentials, `rotate-root`, static roles.
24. [PostgreSQL database plugin — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/databases/postgresql) — creation/revocation statement templating.
25. [AWS secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/aws) — `iam_user` vs `assumed_role` and the eventual-consistency trap.
26. [PKI secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/pki) — the issuing CA behind cert-manager; see [11-cert-manager-and-pki.md](11-cert-manager-and-pki.md).
27. [Public CA integration — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/pki-external-ca) — Vault 2.0 feature for fronting a public CA.
28. [SSH secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/ssh) — CA-signed SSH certs; replaces `authorized_keys` distribution.
29. [Audit devices — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/audit) — the "Vault refuses to service the request" behavior and the audit-exempt endpoint list. Read the availability section twice.
30. [Recover blocked audit devices — HashiCorp Developer](https://developer.hashicorp.com/vault/tutorials/monitoring/blocked-audit-devices) — the runbook for the outage in gotcha #1.
31. [Audit best practices — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/audit/best-practices) — two-device recommendation and sizing guidance.
32. [Resource quotas — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/enterprise/lease-count-quotas) — lease-count and rate-limit quotas, the defense against lease explosion.
33. [Telemetry — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/internals/telemetry) — the full metric catalog behind the alerting table.
34. [Vault 2.x release notes — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/updates/release-notes) — 2.0.0 GA 2026-04-14 through 2.0.4 on 2026-08-04; source for the templated-policy wildcard change and `max_token_header_size`.
35. [Vault important changes — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/updates/important-changes) — read before every upgrade; source for the `IPC_LOCK` removal.
36. [HashiCorp Vault end-of-life dates — endoflife.date](https://endoflife.date/hashicorp-vault) — machine-readable support windows; 1.21 EOL 2026-04-13.
37. [Vault upgrade procedure — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/upgrading) — standbys first, `step-down` last.
38. [IBM completes acquisition of HashiCorp — IBM Newsroom](https://newsroom.ibm.com/2025-02-27-ibm-completes-acquisition-of-hashicorp,-creates-comprehensive,-end-to-end-hybrid-cloud-platform) — primary source for the February 2025 close.
39. [OpenBao 2.6.x release notes — OpenBao](https://openbao.org/community/release-notes/2-6-0/) — namespace sealing, pluggable auto-unseal, workflows, and the 2.7.0 deprecations. Primary source for the fork's current state.
40. [OpenBao 2.5.x release notes — OpenBao](https://openbao.org/community/release-notes/2-5-0/) — horizontal read scalability on standby nodes.
41. [OpenBao project home — OpenBao](https://openbao.org/) — MPL 2.0, Linux Foundation, OpenSSF Sandbox governance.
42. [Vault Terraform provider — Terraform Registry](https://registry.terraform.io/providers/hashicorp/vault/latest/docs) — the provider's own warning about secrets in state.
43. [Vault Secrets Operator — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso) — CRD-driven sync with `rolloutRestartTargets`; the GitOps-friendly option.
44. [Vault Agent Injector — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/injector) — sidecar model; keeps secrets out of etcd at the cost of admission-time mutation.
45. [Secrets Store CSI Driver — Kubernetes SIG-Auth](https://secrets-store-csi-driver.sigs.k8s.io/) — file-mounted secrets, multi-provider.
46. [External Secrets Operator](https://external-secrets.io/latest/) — CNCF operator speaking to Vault plus every major cloud secret store; the strongest fit for a three-cloud footprint.
47. [Vault Helm chart — GitHub](https://github.com/hashicorp/vault-helm) — used in Lab 2; read `values.yaml` before trusting any blog post about it.
