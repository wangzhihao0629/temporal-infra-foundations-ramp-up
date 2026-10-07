# cert-manager and PKI for Cells

**Why this matters.** A Temporal Cloud cell is a mesh of gRPC services — frontend, history, matching, worker — that must authenticate each other, plus an ingress path that must present a certificate a customer's SDK will trust. Every one of those certificates has an expiry, and every expiry is a scheduled outage unless something renews it. cert-manager is the machine that renews them. But cert-manager is the easy half. The hard half is the PKI *design* underneath it: who is the root, where does the private key live, which chain does each service present, and — the question that has broken more internal PKIs than any other — how do you rotate a root without simultaneously distrusting everything it signed. You are being asked to own "cert-manager and in general PKI for cells," and those are genuinely different disciplines. This guide covers both, with the PKI half weighted heavier because it is the part with no undo button.

---

## The mental model

**A certificate is a signed assertion with an expiry date, and a PKI is the machinery for making, distributing, and retiring those assertions.** Three separate problems, and teams that conflate them build PKIs that cannot rotate.

1. **Issuance** — a workload proves it deserves an identity, and a CA signs a certificate binding a key to that identity. cert-manager owns this in Kubernetes.
2. **Trust distribution** — a workload learns which CAs to believe. This is a completely separate pipeline, and it is the one that has no automation by default. `trust-manager`, `ClusterTrustBundle`, and container CA bundles live here.
3. **Retirement** — a certificate or a CA stops being valid. In practice this means *short lifetimes*, because revocation does not work at scale.

The asymmetry that governs everything: **issuance is fast and reversible; trust distribution is slow and one-way.** You can issue a new leaf in seconds. Getting a new root into every trust store in the fleet takes as long as your slowest deploy. Therefore the invariant:

> **Distribute trust before you use it. Remove trust after you stop using it. Never in the other order.**

Violate that and you get the classic internal-PKI outage: you rotate the root, cert-manager issues leaves from the new root, and every client that has not yet received the new trust bundle rejects them. This is what the "overlapping trust rollover" procedure below exists to prevent.

The second governing idea: **hierarchy is about blast radius and key storage, not about cryptography.** A root exists to be offline. An intermediate exists to be online and disposable. If your root's private key is sitting in a Kubernetes Secret, you have a one-level PKI with extra steps.

The third: **a certificate says nothing about authorization.** It says "the holder of this key is named X, according to this CA." Whether X may call your API is your problem. SPIFFE is what happens when you take that seriously and give every workload a structured, verifiable name.

---

## Core concepts

### What a CA actually is

Operationally: a private key, a self-signed (or parent-signed) certificate with `CA:TRUE` in Basic Constraints, a serial number counter, and a policy about what it will sign. That is it. Everything else — HSMs, ceremonies, CP/CPS documents — is process wrapped around protecting the key and constraining the policy.

The certificate that makes something a CA has two mandatory-ish extensions:

- **Basic Constraints**: `CA:TRUE`, optionally with `pathlen:N`.
- **Key Usage**: must include `keyCertSign`. Usually also `cRLSign`. [RFC 5280 §4.2.1.3](https://www.rfc-editor.org/rfc/rfc5280#section-4.2.1.3) requires that if `keyCertSign` is asserted, Basic Constraints `cA` must be TRUE.

A CA whose certificate lacks `keyCertSign` will produce certificates that fail validation in every correct implementation, and the error message will be about the *leaf*, not the CA. This is a classic half-day debugging session.

### Root vs intermediate, and why you want at least two levels

| | Root CA | Intermediate (issuing) CA |
|---|---|---|
| Private key location | Offline: HSM, KMS with no online signer, or air-gapped | Online: Vault PKI, cloud CA, KMS-backed signer |
| Lifetime | 10–20 years | 1–5 years |
| What it signs | Only intermediates | Only leaves |
| Distributed to trust stores | Yes — this is the trust anchor | No — sent in the chain at handshake time |
| Compromise impact | Rebuild the entire PKI, redistribute trust everywhere | Revoke it, issue a new one from the root, reissue leaves. Painful but survivable. |
| Rotation cadence | As rarely as possible | Regularly, ideally boringly |

The reason for the split is not cryptographic strength — it is that **the intermediate is designed to be disposable and the root is designed never to move.** If you have one CA and it is online, an incident means redistributing trust to every workload under time pressure, which is the worst possible moment to be doing the slow one-way operation.

`pathlen` is how you enforce the shape. `pathlen:0` on an intermediate means "this CA may sign leaves but not further CAs." Note up front that **cert-manager cannot set it** — there is no `pathLen` field on `Certificate` ([cert-manager#2820](https://github.com/cert-manager/cert-manager/issues/2820)), so a cert-manager-signed intermediate is unconstrained. Vault PKI (`max_path_length` on `pki/root/generate` and `pki/intermediate/set-signed`) and the cloud private CAs can set it; that asymmetry is one more argument for Vault as the issuing CA.

```bash
# Read a CA cert and check its constraints
openssl x509 -in intermediate.crt -noout -text | \
  grep -A1 'Basic Constraints\|Key Usage'
#   X509v3 Basic Constraints: critical
#       CA:TRUE, pathlen:0
#   X509v3 Key Usage: critical
#       Certificate Sign, CRL Sign
```

### Cross-signing and the "which chain do I serve" problem

**Cross-signing** means the same public key + subject is certified by two different parents, producing two certificates. The classic example is Let's Encrypt's ISRG Root X1, which for years was also cross-signed by IdenTrust's DST Root CA X3 so that old Android devices — which had DST but not ISRG — could still validate.

This creates the operational problem that trips up almost everyone: **a server does not send "the chain." It sends *a* chain, and the chain it sends determines which clients succeed.**

- Send the chain up to the *new* root: modern clients validate; old clients that only trust the old root fail.
- Send the chain up to the *old* root via the cross-signed intermediate: old clients validate; modern clients validate too (they can build a path either way, and most will).

The rule: **serve the chain that maximizes the set of clients who can build a path to a root they trust.** During a cross-signed transition that usually means serving the longer, cross-signed chain until the old clients are gone, then shortening it.

Modern OpenSSL and Go build paths reasonably well and will often find an alternative if you send a suboptimal chain. Older OpenSSL 1.0.x and many embedded stacks do strict chain-following and will fail. Test with what your clients actually run.

cert-manager exposes this directly on the ACME issuer via `preferredChain`:

```yaml
spec:
  acme:
    preferredChain: "ISRG Root X1"   # ask the CA for a specific chain
```

Never put an intermediate in a trust store to "fix" a chain problem. The [trust-manager docs](https://cert-manager.io/docs/trust/trust-manager/#bundling-intermediates) are explicit about why: it makes the intermediate a de facto root, which "means that the intermediate cannot be safely rotated without all trust stores which contain it being updated first" — you have destroyed the entire reason intermediates exist.

### Name constraints

**Name Constraints** ([RFC 5280 §4.2.1.10](https://www.rfc-editor.org/rfc/rfc5280#section-4.2.1.10)) let a CA certificate declare "any certificate below me is only valid for names in this set." It is the single most underused control in internal PKI.

If you cross-sign a partner's CA, or you hand a per-region intermediate to a regional team, name constraints turn "this CA can impersonate anything" into "this CA can only issue for `*.usw2.cells.example.internal`." Enforcement is client-side, and support is good in Go, OpenSSL 1.1+, and NSS; historically weak in Java and some embedded stacks, so it is defense in depth, not a hard boundary.

```
X509v3 Name Constraints: critical
    Permitted:
      DNS:.usw2.cells.example.internal
      DNS:.usw2.svc.cluster.local
    Excluded:
      DNS:.example.com
```

Note the leading dot: `DNS:.example.com` constrains *subdomains*, and per RFC 5280 the constraint matches by suffix on domain components. A leaf that violates a permitted subtree fails with `error 47 at 0 depth lookup: permitted subtree violation` under `openssl verify`.

In cert-manager this is `spec.nameConstraints`, but it is an **alpha** field: it does nothing unless `--feature-gates=NameConstraints=true` is set on *both* the controller and the webhook. The shape is `nameConstraints: {critical, permitted: {dnsDomains, ipRanges, emailAddresses, uriDomains}, excluded: {...}}` — not the `DNS:`-prefixed strings openssl prints. Vault PKI's `permitted_dns_domains` on the intermediate needs no feature gate and is the easier path.

### EKU, KU, and SANs

**Key Usage (KU)** describes what the *key* may do: `digitalSignature`, `keyEncipherment`, `keyCertSign`, `cRLSign`. **Extended Key Usage (EKU)** describes what the *certificate* is for: `serverAuth` (1.3.6.1.5.5.7.3.1), `clientAuth` (1.3.6.1.5.5.7.3.2), `codeSigning`, etc.

For mTLS between cell services, every certificate needs **both** `serverAuth` and `clientAuth`, because every service is both. Forgetting `clientAuth` produces a beautifully confusing failure: the server side of the handshake works, the client side is rejected, and half your service graph is fine.

In cert-manager:

```yaml
spec:
  usages:
    - digital signature
    - key encipherment
    - server auth
    - client auth
```

**SANs and the death of CN.** The Common Name has not been a valid source of hostname identity for years. [RFC 6125](https://www.rfc-editor.org/rfc/rfc6125) deprecated CN fallback, the CA/Browser Forum Baseline Requirements forbid public CAs from relying on it, and **Go disabled CN fallback by default in Go 1.15 (behind a temporary `GODEBUG=x509ignoreCN=0` escape hatch) and removed it entirely in Go 1.17** — which matters enormously here, because Temporal, Kubernetes, and essentially the entire cloud-native stack are written in Go. A certificate with `CN=frontend.cell.internal` and no SAN simply does not work.

Always set `dnsNames`. Treat `commonName` as a human-readable label, or omit it. Note also that the CN field is limited to 64 characters, which silently truncates long service names — another reason to stop using it.

```yaml
spec:
  commonName: temporal-frontend        # cosmetic
  dnsNames:                            # this is what actually matters
    - temporal-frontend.cell-usw2-042.svc.cluster.local
    - temporal-frontend.cell-usw2-042.svc
    - temporal-frontend
  uris:
    - spiffe://example.internal/ns/cell-usw2-042/sa/temporal-frontend
```

### Key algorithms: what actually works

| Algorithm | Size | Handshake cost | Support reality | Verdict |
|---|---|---|---|---|
| RSA | 2048 | Slow sign, fast verify | Universal | Safe default when you have unknown clients |
| RSA | 4096 | Much slower sign | Universal | Roots only; pointless for leaves |
| ECDSA | P-256 | Fast | Excellent — every modern TLS stack | **Best choice for internal PKI** |
| ECDSA | P-384 | Fast | Excellent | Roots and intermediates |
| Ed25519 | — | Fastest | **Effectively TLS 1.3 in practice (TLS 1.2 use is specified by RFC 8422 but patchy), and stack support is uneven** | Not yet for a heterogeneous fleet |

The Ed25519 caveat is the one that surprises people. It is the best algorithm on paper, it is supported in Go and OpenSSL 3.x, but it is **not usable in the Web PKI** (the CA/Browser Forum Baseline Requirements do not permit it for publicly trusted TLS), and support in Java, .NET, and older load balancers is patchy. cert-manager supports `Ed25519` in `spec.privateKey.algorithm`, but your issuing CA has to support it too — Vault PKI does, AWS Private CA does not.

For internal cell PKI where you control both ends: **ECDSA P-256 leaves, ECDSA P-384 intermediates and root.** Faster handshakes, smaller certificates, less CPU on services doing thousands of mTLS connections. For anything a customer's SDK terminates against: RSA 2048 or ECDSA P-256 depending on what you can verify your customers support.

```yaml
spec:
  privateKey:
    algorithm: ECDSA
    size: 256
    encoding: PKCS8
    rotationPolicy: Always
```

### Serial numbers

Serial numbers must be unique per issuer and, for public CAs, must contain at least 64 bits of CA-generated entropy (a Baseline Requirements rule that exists because of the MD5 collision attacks on sequential serials). For internal CAs the same practice is correct: random, not sequential. Vault PKI and cert-manager's CA issuer both do this. Sequential serials leak issuance volume and enable a class of prediction attacks.

Serials also matter operationally because they are how you correlate a certificate in a log with a certificate on a wire:

```bash
openssl x509 -in leaf.crt -noout -serial
# serial=5A3F1C09B2E7D48A...
```

### AIA, CRL, and OCSP

Three extensions that tell a client where to get more information:

- **AIA (Authority Information Access)** — `caIssuers` gives a URL where the issuing certificate can be fetched, letting a client complete a chain the server failed to send. `OCSP` gives a responder URL.
- **CRL Distribution Points** — where to fetch the certificate revocation list.
- **OCSP** — an online query for a single certificate's status.

**For internal PKI, the honest position is that none of these work well, and you should design so you do not need them.** CRLs grow unboundedly and are cached for hours. OCSP adds a network dependency to every handshake, and OCSP stapling — the fix — requires server support you may not have. Chrome removed online OCSP checking years ago; browsers use pushed revocation lists instead. And the CA/Browser Forum's own [SC-081v3 ballot](https://cabforum.org/2025/04/11/ballot-sc081v3-introduce-schedule-of-reducing-validity-and-data-reuse-periods/) says it plainly: certificate status services "do not adequately protect relying parties at the current scale of the internet."

If you do set AIA `caIssuers` for internal certs, the URL must be reachable from every client, which for a cell means an internal endpoint with its own availability requirement. Usually not worth it. **Serve the full chain and use short lifetimes instead.**

### Certificate lifetimes and the industry shortening

This is the change that reshapes certificate operations, and it is already partly in effect.

**CA/Browser Forum Ballot SC-081v3** passed on 2025-04-11 with **25 Certificate Issuers voting yes, zero no**, and all four Certificate Consumers (Apple, Google, Microsoft, Mozilla) voting yes. It sets a phased reduction of the maximum validity period for publicly trusted TLS certificates:

| Effective date | Max certificate validity | Max DCV (domain validation) data reuse |
|---|---|---|
| Before 2026-03-15 | 398 days | 398 days |
| **2026-03-15** | **200 days** | 200 days |
| **2027-03-15** | **100 days** | 100 days |
| **2029-03-15** | **47 days** | **10 days** |

As of today (2026-08-29) **the 200-day limit is already in force.** The 47-day endpoint in March 2029 is the number people quote, but the operational lesson lands earlier: by March 2027 you are renewing public certificates roughly every seven weeks, and manual renewal is simply not viable.

Two things to be precise about:

1. **This applies only to publicly trusted TLS server certificates.** The ballot's own scope note says the Baseline Requirements address certificates "intended to be used for authenticating servers accessible through the Internet." Your internal cell CA is not bound by it.
2. **You should adopt shorter lifetimes internally anyway**, and for a better reason: short lifetimes are your revocation strategy. A 24-hour internal certificate means a compromised key is worthless tomorrow without any CRL infrastructure. It also means your renewal path is exercised constantly, so it cannot silently rot — the failure mode where renewal has been broken for six months and nobody noticed until expiry becomes structurally impossible.

Practical internal targets: **leaves 24h–90d, intermediates 1–5y, root 10–20y.** With cert-manager and a Vault or cloud issuing CA, 24-hour leaves are entirely routine. Note cert-manager fixed an integer overflow in `renewBeforePercentage` in 1.21 that affected certificates with durations longer than roughly three years — another reason not to make leaves long-lived.

---

## Trust distribution

This is the part with no default automation, and it is where internal PKIs actually fail.

### How a workload decides to trust your CA

A TLS client validates a chain against a set of **trust anchors**. Where it finds them depends entirely on the stack:

| Stack | Where it looks | How you add a CA |
|---|---|---|
| OpenSSL / most Linux C programs | `/etc/ssl/certs/ca-certificates.crt`, `$SSL_CERT_FILE`, `$SSL_CERT_DIR` | Drop PEM in `/usr/local/share/ca-certificates/`, run `update-ca-certificates` |
| Go | System store via `crypto/x509`, or an explicit `RootCAs` pool | Either system store or `tls.Config.RootCAs` |
| Java | `$JAVA_HOME/lib/security/cacerts` (JKS/PKCS#12) | `keytool -importcert`, or a PKCS#12 truststore |
| Node.js | Bundled CA list, plus `NODE_EXTRA_CA_CERTS` | `NODE_EXTRA_CA_CERTS=/etc/ssl/internal-ca.pem` |
| Python `requests` | `certifi` bundle | `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` |
| Envoy / Istio | Explicit config, never the system store | `validation_context.trusted_ca` |

The Go behavior is the one that matters most here, and it is a trap: Go on Linux reads the system store, which in a **distroless or scratch container may not exist at all**. A Go service in a distroless image with no `/etc/ssl/certs` will fail every outbound TLS connection with a nearly useless error. You must either mount a bundle or use the `-nonroot` / `base` distroless variants that include `ca-certificates`.

### The four mechanisms, compared

| Mechanism | Scope | Rotation | Requires pod restart? | Verdict |
|---|---|---|---|---|
| Baked into container image | Per image | Rebuild + redeploy every image | Yes | Never. This is the anti-pattern. |
| ConfigMap mounted as a volume | Per namespace | Update the ConfigMap; kubelet syncs the file (up to ~1 min) | No, *if* the app re-reads the file | Workable, but you manage the ConfigMap in N namespaces yourself |
| **trust-manager `Bundle`** | Cluster-wide, namespace-selected | Update a source; the operator fans out to every namespace | No, if the app re-reads | **The current answer for Kubernetes** |
| **`ClusterTrustBundle`** | Cluster-wide, native API | Update the object; kubelet projects it into the volume | No | **The future answer — now GA** |

**`ClusterTrustBundle` reached GA (stable, enabled by default) in Kubernetes 1.37**, released 2026-08-26, in the `certificates.k8s.io/v1` API group. It went alpha in 1.27 and beta in 1.33. It is a cluster-scoped object holding PEM trust anchors that pods request via a projected volume, keyed by signer name. In the same release, **`PodCertificateRequest` (KEP-4317) also went stable**: a pod declares a `podCertificate` projected volume naming a signer, the kubelet creates a `PodCertificateRequest`, a signer controller issues a short-lived certificate and maintains the corresponding `ClusterTrustBundle`, and the key and certificate are delivered straight into the pod with automatic rotation.

That combination is significant: Kubernetes now has a native workload-PKI story that did not exist a year ago. It does not make cert-manager obsolete — you still need a real CA behind the signer, policy, ACME for public certs, and multi-format output — but it changes the trust-distribution half of the problem substantially, and it is worth designing toward. Note that cert-manager 1.21 supports Kubernetes **1.33 → 1.36**, so 1.37 support arrives with cert-manager 1.22 (~November 2026); check the [supported releases table](https://cert-manager.io/docs/releases/) before assuming compatibility.

### trust-manager

trust-manager adds one cluster-scoped CRD, `Bundle`, which assembles trust anchors from several sources and writes the result to a ConfigMap (or Secret) in every selected namespace.

```yaml
apiVersion: trust.cert-manager.io/v1alpha1
kind: Bundle
metadata:
  name: temporal-internal-trust
spec:
  sources:
    # Public CAs, for outbound calls to cloud APIs. Pin the package image.
    - useDefaultCAs: true

    # Our root(s) — deliberately copied into a dedicated ConfigMap, NOT read
    # directly from the cert-manager Secret. See the rotation section below.
    - configMap:
        name: temporal-trust-anchors
        key: roots.pem

  target:
    configMap:
      key: ca-certificates.crt
    additionalFormats:
      pkcs12:
        key: truststore.p12
    namespaceSelector:
      matchLabels:
        example.com/cell: "true"
```

Key facts from the [trust-manager docs](https://cert-manager.io/docs/trust/trust-manager/):

- Sources: `configMap`, `secret`, `inLine`, `useDefaultCAs`. ConfigMap and Secret sources support either a single `key` or `includeAllKeys`, and either a `name` or a label `selector` — mutually exclusive in both cases.
- Targets: ConfigMap by default; **Secret targets require explicitly enabling them in the controller** and have existed since v0.7.0.
- Additional formats: **JKS since v0.5.0, PKCS#12 since v0.7.0.** Default passwords are `changeit` for JKS and empty for PKCS#12, and the docs correctly call these "security theater." JKS output is now marked deprecated in the trust-manager API and slated for removal — use PKCS#12, which Java reads fine.
- The default CA package is a container image, currently `quay.io/jetstack/trust-pkg-debian-bookworm` for trust-manager >= v0.16. **You must keep it updated** — it is the equivalent of never running `apt-get upgrade ca-certificates`.
- **An empty `namespaceSelector` currently syncs to all namespaces, and the docs warn this will change** to sync only to the trust-manager namespace. Always set the selector explicitly.
- trust-manager is [migrating to a `ClusterBundle` resource](https://cert-manager.io/announcements/2025/09/05/trust-manager-clusterbundle-future/) in a new API group `trust-manager.io/v1alpha2`, aligning more closely with the native `ClusterTrustBundle`. Plan for the API change.

### The root rotation problem — the hardest part of internal PKI

This is the section to read twice.

**The failure mode.** You point a `Bundle` directly at the cert-manager Secret holding your root. You rotate the root. trust-manager sees the Secret change and immediately updates every trust bundle to contain only the new root. Every service still holding a leaf signed by the old root is now untrusted, cluster-wide, in about thirty seconds. The trust-manager docs describe exactly this: "if you rotate your issuer such that it's issued from a new root certificate, trust-manager will see the Secret be updated and automatically update your trust bundle to include the new root — immediately distrusting the old root."

**The fix has two parts.**

*Part one: decouple.* Never point a `Bundle` at a live cert-manager Secret. Copy the root into a dedicated ConfigMap that only *you* change, and point the Bundle at that. This seems like pointless indirection until the first rotation, at which point it is the only thing standing between you and a fleet-wide outage. The trust-manager docs call this out as a best practice.

*Part two: overlap.* Trust both roots for a window that exceeds the maximum lifetime of any leaf signed by the old root.

**The overlapping-trust rollover procedure, in order:**

**Phase 0 — Preconditions.**

- Know the maximum lifetime of every leaf signed by the old root. Call it `L`.
- Know your slowest trust-bundle propagation time: how long from "I update the ConfigMap" to "the last pod in the fleet has re-read it." Call it `P`. For a fleet where some services only re-read on restart, `P` is your slowest deploy cadence, which may be days.
- Have monitoring that can answer "which certificates in the fleet chain to root A?" If you cannot answer that, stop and build it first. Certificate Transparency does not exist for internal PKI; you need your own inventory.

**Phase 1 — Create the new root. Do not use it.**

Generate root B, offline, with a lifetime that overlaps root A generously. Do not create any intermediate under it yet.

**Phase 2 — Distribute trust for both roots. Wait `P`.**

```yaml
# temporal-trust-anchors ConfigMap now contains BOTH
apiVersion: v1
kind: ConfigMap
metadata:
  name: temporal-trust-anchors
  namespace: cert-manager
data:
  roots.pem: |
    -----BEGIN CERTIFICATE-----   # root A (current)
    ...
    -----END CERTIFICATE-----
    -----BEGIN CERTIFICATE-----   # root B (new, not yet issuing)
    ...
    -----END CERTIFICATE-----
```

**Verify propagation before proceeding.** Not "wait a while" — actually check:

```bash
for ns in $(kubectl get ns -l example.com/cell=true -o name | cut -d/ -f2); do
  n=$(kubectl -n "$ns" get cm temporal-internal-trust \
        -o jsonpath='{.data.ca-certificates\.crt}' 2>/dev/null \
      | grep -c 'BEGIN CERTIFICATE')
  echo "$ns: $n anchors"
done
```

And verify inside a running pod, not just in the ConfigMap — the ConfigMap being correct does not mean the process re-read it:

```bash
kubectl -n cell-usw2-042 exec deploy/temporal-frontend -- \
  sh -c 'openssl crl2pkcs7 -nocrl -certfile /etc/ssl/certs/ca-certificates.crt \
         | openssl pkcs7 -print_certs -noout | grep -c subject'
```

**Phase 3 — Create the intermediate under root B and switch issuance.**

Now, and only now, create intermediate B under root B, and repoint your `ClusterIssuer` at it. New leaves chain to B. Old leaves chaining to A are still valid, and still trusted, because both roots are in every bundle.

**Phase 4 — Wait for natural expiry, or force reissuance.**

Either wait `L` for every old leaf to expire and renew, or actively force reissuance:

```bash
# cert-manager: force reissuance of every Certificate in a namespace
kubectl -n cell-usw2-042 get certificate -o name | \
  xargs -I{} kubectl -n cell-usw2-042 patch {} --type=merge \
    -p '{"spec":{"privateKey":{"rotationPolicy":"Always"}}}'
# or, with cmctl:
cmctl renew --all --namespace cell-usw2-042
```

**Phase 5 — Verify zero remaining dependence on root A.**

```bash
# Check what a live service is actually serving
kubectl -n cell-usw2-042 exec deploy/temporal-frontend -- \
  openssl s_client -connect temporal-history:7234 -showcerts </dev/null 2>/dev/null \
  | openssl crl2pkcs7 -nocrl -certfile /dev/stdin \
  | openssl pkcs7 -print_certs -noout
```

Do this across every service, every cell, and every non-Kubernetes consumer (there is always one — a CI runner, a monitoring probe, a partner integration).

**Phase 6 — Remove root A from the anchors ConfigMap. Wait `P` again.**

**Phase 7 — Destroy root A's private key** only after step 6 has propagated and been verified.

Total elapsed time: `P + L + P`, plus verification. For a fleet with 90-day leaves and a weekly deploy cadence, that is on the order of a quarter. **Plan root rotations as quarter-long projects, not as change tickets.** This is why roots have 10–20 year lifetimes and why the intermediate exists to absorb routine rotation instead.

**Intermediate rotation is much cheaper** because intermediates are not trust anchors — they travel in the chain. Rotating an intermediate under the same root requires no trust distribution at all, just reissuance. This is the entire argument for the two-level hierarchy, stated operationally.

---

## cert-manager architecture

### Components

Four deployments, and knowing which one is broken saves hours:

| Component | Job | Failure symptom |
|---|---|---|
| **controller** | Runs all the reconcile loops: Certificate, CertificateRequest, Issuer, Order, Challenge | Certificates stuck `Ready=False`, no events progressing |
| **webhook** | Validating + mutating admission for cert-manager CRDs | `kubectl apply` of *any* cert-manager resource hangs or fails with a webhook error. Also blocks unrelated deploys if failurePolicy is Fail. |
| **cainjector** | Injects CA bundles into `ValidatingWebhookConfiguration`, `MutatingWebhookConfiguration`, `APIService`, and `CustomResourceDefinition` `caBundle` fields | Other operators' webhooks break with x509 errors |
| **startupapicheck** | A one-shot Job verifying the API is serving | Fails at install; usually an RBAC or webhook-reachability problem |

The **webhook is the bootstrap paradox**: cert-manager's webhook needs a TLS certificate, and cert-manager is the thing that issues certificates. It resolves this by self-managing — the webhook generates its own self-signed CA at startup and cainjector wires the bundle into its own `ValidatingWebhookConfiguration`. This is why a broken cainjector breaks cert-manager itself. (cert-manager 1.21 fixed a related bug where the webhook's serving certificate was not renewed after a system suspend or VM live migration; it now polls wall-clock time and recovers within a minute.)

### The CRDs and the reconcile flow

```
Certificate                          (what you want: names, duration, issuer)
    │  controller creates
    ▼
CertificateRequest                   (one PEM CSR + issuerRef; immutable)
    │  issuer-specific controller picks it up
    ├── SelfSigned / CA / Vault / external → signs directly
    └── ACME issuer
            │  creates
            ▼
        Order                        (ACME order for this identifier set)
            │  creates one per identifier
            ▼
        Challenge                    (HTTP-01 or DNS-01; solver pod / DNS record)
            │  on success, Order gets the cert
            ▼
    CertificateRequest.status.certificate  ← signed PEM
    │  controller writes
    ▼
Secret (type kubernetes.io/tls)      { tls.crt, tls.key, ca.crt }
```

The important structural facts:

- **`CertificateRequest` is immutable and one-shot.** Every renewal creates a *new* one. Old ones are kept per `spec.revisionHistoryLimit`. If you want to know why a renewal failed three days ago, the evidence is in an old CertificateRequest — until it is garbage collected.
- **The private key is generated by cert-manager, in the cluster, and stored in the Secret.** cert-manager never sees a key it did not make (except with `csi-driver-spiffe`, where the key never leaves node memory). It also stores the *next* private key in a temporary Secret (`nextPrivateKeySecretName`) during reissuance.
- **`ca.crt` is best-effort.** Not every issuer can populate it. The trust-manager docs are blunt about this: `ca.crt` "can only ever be populated on a best-effort basis" and depends on the Issuer being configured correctly. Do not build a trust pipeline that assumes it exists.
- **`tls.crt` contains the leaf plus the chain** (excluding the root, normally). This is what you serve. It is *not* what you put in a trust store.

```bash
kubectl describe certificate temporal-frontend -n cell-usw2-042
kubectl get certificaterequest -n cell-usw2-042
kubectl describe order -n cell-usw2-042
kubectl describe challenge -n cell-usw2-042
# cmctl gives the whole tree at once:
cmctl status certificate temporal-frontend -n cell-usw2-042
```

---

## Issuers

`Issuer` is namespaced; `ClusterIssuer` is cluster-scoped and reads its credentials from the cert-manager namespace. For a cell, `ClusterIssuer` is almost always right — you do not want to replicate issuer config into every namespace.

| Issuer | Where the CA key lives | Audit trail | Rate limits | Good for |
|---|---|---|---|---|
| **SelfSigned** | Nowhere — each cert signs itself | None | None | Bootstrapping a root; webhook certs; tests |
| **CA** | A Kubernetes Secret in the cluster | Kubernetes audit log only | None | Cheap internal CA. **The key is in etcd** — accept that or do not use it. |
| **ACME** | The public CA's HSM | Certificate Transparency logs | Yes, strict | Public-facing endpoints |
| **Vault** | Vault PKI, backed by Vault's barrier | Vault audit devices | Vault quotas | **The right answer for cell PKI** |
| **Venafi / CyberArk** | Enterprise CLM platform | Full enterprise audit | Platform-dependent | Orgs with an existing Venafi estate |
| **AWS PCA issuer** (external) | AWS Private CA, FIPS 140-3 L3 HSM | CloudTrail | AWS API limits | AWS-native, compliance-driven |
| **Google CAS issuer** (external) | GCP Certificate Authority Service HSM | Cloud Audit Logs | GCP API limits | GCP-native |
| **step-issuer** (external) | `step-ca` | step-ca logs | None | Smallstep shops |

The Venafi issuer is now documented under the [CyberArk](https://cert-manager.io/docs/configuration/venafi/) name following the acquisition, and cert-manager 1.21 extended it to support PANW NGTS as a backend.

### SelfSigned → CA: building a hierarchy

This is the standard bootstrap and it is worth internalizing, because it is exactly how you would build a cell's PKI if you were not using Vault.

```yaml
# 1. A SelfSigned issuer exists only to sign the root.
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-bootstrap
spec:
  selfSigned: {}
---
# 2. The root. isCA: true, long duration, strong key.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: temporal-root-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: Temporal Internal Root CA
  secretName: temporal-root-ca
  duration: 87600h      # 10 years
  renewBefore: 8760h    # 1 year
  privateKey:
    algorithm: ECDSA
    size: 384
  issuerRef:
    name: selfsigned-bootstrap
    kind: ClusterIssuer
---
# 3. A CA issuer backed by the root's Secret.
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: temporal-root-issuer
spec:
  ca:
    secretName: temporal-root-ca
---
# 4. The intermediate. NOTE: cert-manager has no field for the Basic Constraints
#    pathLen — see cert-manager#2820; the proposed `maxPathLen` was never merged.
#    Enforce the shape with Vault PKI (`max_path_length`), a cloud private CA, or
#    an out-of-band signing ceremony instead.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: temporal-intermediate-usw2
  namespace: cert-manager
spec:
  isCA: true
  commonName: Temporal Intermediate CA usw2
  secretName: temporal-intermediate-usw2
  duration: 26280h      # 3 years
  renewBefore: 4380h    # 6 months
  privateKey:
    algorithm: ECDSA
    size: 384
  usages: ["cert sign", "crl sign", "digital signature"]
  issuerRef:
    name: temporal-root-issuer
    kind: ClusterIssuer
---
# 5. THIS is the issuer workloads use.
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cell-issuer
spec:
  ca:
    secretName: temporal-intermediate-usw2
```

**The honest caveat:** in this design both CA private keys are Kubernetes Secrets, base64-encoded in etcd, readable by anyone with `get secrets` in `cert-manager`. That is fine for a lab and unacceptable for production. Production means Vault PKI or a cloud CA, with cert-manager as the requester rather than the signer.

### ACME: HTTP-01 vs DNS-01

| | HTTP-01 | DNS-01 |
|---|---|---|
| Proves | Control of the HTTP endpoint on port 80 | Control of the DNS zone |
| Wildcards | No | **Yes — required for wildcards** |
| Needs public inbound? | Yes, port 80 reachable from the ACME CA | No |
| Works for internal-only services | No | Yes |
| Failure modes | Ingress misroute, port 80 blocked, split-horizon DNS | Propagation delay, cross-account IAM, NS delegation |
| Cell fit | Public frontends only | Everything else |

For cells — mostly private endpoints, likely wildcard certs per cell — **DNS-01 is the answer**, and the interesting part is the cloud identity plumbing.

**Route53 with IRSA and cross-account:**

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-dns
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: platform@example.com
    privateKeySecretRef:
      name: letsencrypt-dns-account-key
    solvers:
      - dns01:
          route53:
            region: us-west-2
            hostedZoneID: Z0123456789ABCDEFGHIJ
            # Cross-account: cert-manager's IRSA role assumes a role in the
            # DNS account. No static credentials anywhere.
            role: arn:aws:iam::999988887777:role/cert-manager-dns01
```

cert-manager's ServiceAccount is annotated with its own IRSA role; that role's policy allows `sts:AssumeRole` on the DNS account role; the DNS account role trusts it and holds `route53:ChangeResourceRecordSets` scoped to the hosted zone plus `route53:GetChange`. **cert-manager 1.21 removed the default `serviceaccounts/token: create` RBAC from the Helm chart** — if you were relying on `serviceAccountRef.name` pointing at the controller's own ServiceAccount (an undocumented pattern), you must now create that RBAC yourself or move to a dedicated ServiceAccount. Also new in 1.21: [AWS IAM authentication for the Vault issuer](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/) supporting IRSA and EKS Pod Identity, which removes long-lived AWS Secrets from that path too.

**Google CloudDNS with Workload Identity:**

```yaml
      - dns01:
          cloudDNS:
            project: temporal-dns-prod
            # Omit serviceAccountSecretRef entirely; ambient Workload Identity
            # credentials are used.
```

Bind the Kubernetes SA to a Google SA with `roles/dns.admin` on the project (or finer-grained on the zone).

**AzureDNS with workload identity:**

```yaml
      - dns01:
          azureDNS:
            resourceGroupName: dns-rg
            subscriptionID: 00000000-0000-0000-0000-000000000000
            hostedZoneName: cells.example.com
            environment: AzurePublicCloud
            managedIdentity:
              clientID: 11111111-1111-1111-1111-111111111111
```

**The CNAME delegation trick.** For all three clouds, the cleanest pattern is to *not* grant cert-manager write access to your production zone at all. Instead, `CNAME` the `_acme-challenge` record to a dedicated throwaway zone:

```
_acme-challenge.cell-usw2-042.example.com.  CNAME  cell-usw2-042.acme-delegation.internal.
```

Then cert-manager only needs write access to `acme-delegation.internal`, which contains nothing else. Enable it with `cnameStrategy: Follow` on the solver. This is the single best blast-radius reduction available for DNS-01.

### The Vault issuer

For cell PKI, this is the design: Vault PKI holds the intermediate's private key inside its barrier, cert-manager holds no CA key at all and simply requests signatures.

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cell-vault-issuer
spec:
  vault:
    server: https://vault.cell-usw2-042.internal:8200
    path: pki_int_usw2/sign/cell-workload
    caBundle: <base64 of Vault's own CA>
    auth:
      kubernetes:
        role: cert-manager
        mountPath: /v1/auth/kubernetes
        serviceAccountRef:
          name: cert-manager
          audiences: ["vault"]
```

cert-manager authenticates to Vault with its own Kubernetes service account token, gets a Vault token bound to a policy that permits exactly `pki_int_usw2/sign/cell-workload`, and Vault's PKI role constrains what can actually be issued:

```bash
vault write pki_int_usw2/roles/cell-workload \
  allowed_domains="cell-usw2-042.svc.cluster.local,usw2.cells.example.internal" \
  allow_subdomains=true \
  allow_bare_domains=false \
  allowed_uri_sans="spiffe://example.internal/ns/cell-usw2-042/*" \
  key_type=ec key_bits=256 \
  ttl=24h max_ttl=72h \
  ext_key_usage="ServerAuth,ClientAuth"
```

This gives you defense in depth that a CA-issuer-in-a-Secret design cannot: even a fully compromised cert-manager can only mint certificates matching the Vault role's constraints, and every issuance appears in Vault's audit log.

One 1.21 hardening note: the Vault issuer webhook now [rejects `..` path segments](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/) in `spec.vault.path` and auth mount paths, closing a path-traversal issue where `path.Join` silently resolved relative segments.

*See also: [secrets engines](10-vault.md#secrets-engines) for the Vault side of this — mount layout, role constraints, and the lease behavior of PKI issuance — and [policies](10-vault.md#policies) for writing the policy that scopes cert-manager to exactly one `sign` path.*

### External issuers

External issuers are separate controllers implementing the `CertificateRequest` contract. They plug in identically from the user's perspective.

- **[AWS Private CA Issuer](https://github.com/cert-manager/aws-privateca-issuer)** — `AWSPCAIssuer` / `AWSPCAClusterIssuer`, authenticated with IRSA.
- **[Google CAS Issuer](https://github.com/jetstack/google-cas-issuer)** — `GoogleCASIssuer`, authenticated with Workload Identity.
- **[step-issuer](https://github.com/smallstep/step-issuer)** — for `step-ca`.

---

## Renewal and the reload problem

### How cert-manager schedules renewal

```yaml
spec:
  duration: 24h
  renewBefore: 8h          # renew when 8h remain → every 16h
  # or, preferred for short certs:
  # renewBeforePercentage: 33
```

Constraints and defaults from the [Certificate docs](https://cert-manager.io/docs/usage/certificate/): `duration` defaults to 90 days (2160h), `renewBefore` defaults to one-third of duration, `renewBefore` must be less than `duration`, and the effective minimum duration cert-manager will accept is one hour. `renewBefore` and `renewBeforePercentage` are mutually exclusive.

cert-manager 1.21 added a **`renewal` field** with genuinely useful semantics for a platform team: schedule renewal into approved maintenance windows with cron specs, or disable automatic renewal entirely with `spec.renewal.policy: Disabled`. Note 1.21.1 fixed a controller panic on `policy: Disabled`, so use 1.21.1 or later if you rely on it.

**Reissuance triggers** — the full list, because "why did it reissue?" is a common question:

1. The renewal time arrived.
2. `spec.dnsNames`, `commonName`, `uris`, `ipAddresses`, `usages`, `subject`, or `duration` changed.
3. The `issuerRef` changed.
4. The Secret was deleted or its `tls.crt` became unparseable.
5. Manual: `cmctl renew`, or adding the `cert-manager.io/issue-temporary-certificate` annotation.
6. `spec.privateKey.rotationPolicy: Always` combined with any of the above.

**`rotationPolicy`** is the one to get right. Since cert-manager v1.18 the default is `Always` — a fresh private key on every issuance. Before v1.18 it defaulted to `Never`, meaning **cert-manager reused the existing private key on renewal**, so any Certificate carried over from an older install, or any manifest that still pins `rotationPolicy: Never`, keeps a leaked key leaked across every renewal until someone notices.

```yaml
spec:
  privateKey:
    rotationPolicy: Always    # generate a fresh key on every issuance
```

Set `Always` unless you have a specific reason not to — typically a system that pins the public key (HPKP-style, or a hardware token). The cost is that consumers must handle the key changing, which they must handle anyway.

### The reload problem

**cert-manager's job ends when it writes the Secret.** Nothing tells your application. If your Go service read `tls.crt` at startup into a `tls.Config`, it will happily serve the old certificate until it expires and then start failing — even though a perfectly good new certificate has been sitting in the mounted volume for weeks.

This is the single most common "cert-manager is broken" report, and cert-manager is not broken.

The kubelet syncs updated Secret volumes into the pod, but the sync is **not instantaneous** — it is bounded by the kubelet sync period (1 minute by default) *plus* the Secret cache TTL (also 1 minute by default), so up to roughly two minutes, and **Secrets mounted with `subPath` are never updated at all**. That last one is a silent killer: `subPath` mounts are frozen at pod creation.

| Solution | How | Best for |
|---|---|---|
| **`tls.Config.GetCertificate`** | Callback re-reads from disk (with caching) on each handshake | Go services you own. **The correct fix.** |
| **fsnotify / file watch** | Watch the mount, rebuild the `tls.Config` on change | Go services; slightly more code, works for non-TLS material too |
| **SIGHUP** | nginx, HAProxy, Envoy reload on signal | Proxies; needs something to send the signal |
| **[Reloader](https://github.com/stakater/Reloader)** | Annotation-driven controller that restarts Deployments when a Secret changes | Apps you cannot modify. Blunt but effective. |
| **VSO-style `rolloutRestartTargets`** | Same idea, from the secret-sync side | Vault-sourced material |
| **Envoy SDS** | Certificates delivered over xDS, hot-swapped | Service meshes |
| **`csi-driver`** | Cert written to an ephemeral volume, driver renews in place | Per-pod certs, no Secret at all |

The Go pattern, which is what you actually want in a cell:

```go
type reloadingCert struct {
    mu                sync.RWMutex
    cert              *tls.Certificate
    certPath, keyPath string
}

func (r *reloadingCert) load() error {
    c, err := tls.LoadX509KeyPair(r.certPath, r.keyPath)
    if err != nil {
        return err
    }
    r.mu.Lock(); r.cert = &c; r.mu.Unlock()
    return nil
}

func (r *reloadingCert) Get(*tls.ClientHelloInfo) (*tls.Certificate, error) {
    r.mu.RLock(); defer r.mu.RUnlock()
    return r.cert, nil
}

// Call load() on fsnotify events on the mount directory AND on a ticker as a
// backstop — the atomic symlink swap that Secret volumes use generates events
// that are easy to miss.
tlsCfg := &tls.Config{GetCertificate: rc.Get}
```

**Annotate the reload path into your monitoring.** The observable symptom of a failed reload is "the Secret's `tls.crt` notAfter is in the future, but the certificate served on the wire has a notAfter in the past." Probe the wire, not the Secret.

---

## Integration points

### Ingress and Gateway API

The **ingress-shim** watches Ingress objects and creates Certificates from annotations:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: cell-frontend
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-dns
    cert-manager.io/renew-before: 720h
    cert-manager.io/private-key-rotation-policy: Always
spec:
  tls:
    - hosts: ["cell-usw2-042.example.com"]
      secretName: cell-frontend-tls   # cert-manager creates this
  rules:
    - host: cell-usw2-042.example.com
      # ...
```

Use `cert-manager.io/cluster-issuer`, not `cert-manager.io/issuer`, unless you genuinely want a namespaced Issuer. The full annotation list is in the [annotations reference](https://cert-manager.io/docs/reference/annotations/); 1.21 added handling for `cert-manager.io/alt-names` and `cert-manager.io/ip-sans` on ingress-like objects.

**Gateway API** works the same way, on the Gateway's listeners. cert-manager 1.21 restructured the configuration: `enableGatewayAPI` and `enableGatewayAPIListenerSet` are deprecated in favor of `gatewayAPI.enabled` and `gatewayAPI.enableListenerSet`. It also added `cert-manager.io/ignore-tls-listeners` to exclude specific listeners, and an `acme.cert-manager.io/http01-parentreffallback: "true"` annotation so TLS-only ListenerSets can borrow a shared Gateway HTTP listener for challenges.

**The honest recommendation for cells:** do not use the shim for anything that matters. Write explicit `Certificate` resources. The shim's implicit lifecycle — delete the Ingress and the Certificate goes with it — is exactly the kind of coupling you do not want when a routing change can revoke a cell's TLS.

*See also: [ingress and egress at the cell edge](05-cni-and-host-networking.md#ingress-and-egress-at-the-cell-edge) for what terminates this certificate and where, and [the biggest infra gotcha: L4 load balancing pins gRPC](02-grpc.md#the-biggest-infra-gotcha-l4-load-balancing-pins-grpc) for why the TLS termination point and the balancing point are the same decision for a Temporal cell.*

### CSI drivers — per-pod ephemeral certificates

**`csi-driver`** issues a certificate at pod start into an ephemeral volume. No Kubernetes Secret exists at all: the private key is generated on the node and never persisted. The certificate's lifecycle is the pod's lifecycle.

```yaml
volumes:
  - name: tls
    csi:
      driver: csi.cert-manager.io
      readOnly: true
      volumeAttributes:
        csi.cert-manager.io/issuer-name: cell-vault-issuer
        csi.cert-manager.io/issuer-kind: ClusterIssuer
        csi.cert-manager.io/dns-names: "${POD_NAME}.${POD_NAMESPACE}.svc.cluster.local"
        csi.cert-manager.io/duration: 24h
        csi.cert-manager.io/renew-before: 8h
        csi.cert-manager.io/key-usages: "server auth,client auth"
```

**`csi-driver-spiffe`** is the same mechanism specialized for SPIFFE: it uses CSI Token Requests to derive the pod's identity, mints a SPIFFE ID from the service account, and mounts an X.509 SVID. Per the [project docs](https://github.com/cert-manager/csi-driver-spiffe), the resulting documents are automatically renewed, the private key never leaves the node's virtual memory, each pod's document is unique, and it is destroyed on pod termination.

For a cell full of gRPC services doing mTLS, `csi-driver-spiffe` is a strong fit: no Secrets, no reload problem (the driver renews in place), per-pod identity, and SPIFFE IDs that a service mesh or your own authz layer can consume.

### Webhook certificate self-management

`cainjector` injects CA bundles into other components' webhook configurations, which makes cert-manager the natural certificate source for every operator in a cell.

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: kyverno-resource-validating-webhook-cfg
  annotations:
    cert-manager.io/inject-ca-from: kyverno/kyverno-svc-tls
webhooks:
  - name: validate.kyverno.svc
    clientConfig:
      service:
        name: kyverno-svc
        namespace: kyverno
        path: /validate
      caBundle: Cg==   # cainjector overwrites this
```

Three injection annotations: `cert-manager.io/inject-ca-from` (namespace/certificate-name), `cert-manager.io/inject-ca-from-secret` (namespace/secret-name), and `cert-manager.io/inject-apiserver-ca: "true"`. They work on `ValidatingWebhookConfiguration`, `MutatingWebhookConfiguration`, `APIService`, and `CustomResourceDefinition`.

Kyverno ships its own certificate management by default but supports being driven by cert-manager instead, which is worth doing for consistency — one certificate lifecycle for the whole cell rather than N operator-specific ones. cert-manager 1.21 promoted `CAInjectorMerging` to GA and made cainjector use server-side apply unconditionally, and added `--ignore-namespaces` to skip watching Secrets in specified namespaces (useful in large clusters where cainjector's Secret watch is expensive).

**The ordering trap:** if cert-manager's own webhook is down, you cannot create the Certificate that another operator's webhook needs, and if that operator's webhook has `failurePolicy: Fail` on a broad resource set, you can deadlock the cluster. Keep `failurePolicy: Ignore` on non-security-critical webhooks and always scope `namespaceSelector` to exclude `kube-system` and `cert-manager`.

---

## mTLS between services: cert-manager vs SPIFFE/SPIRE

A cell full of gRPC services doing mutual TLS is precisely the SPIFFE use case, so this comparison deserves a real answer rather than a hand-wave.

| | cert-manager as mesh CA | SPIFFE / SPIRE |
|---|---|---|
| Identity model | DNS names in SANs | Structured URI SANs: `spiffe://trust-domain/path` |
| Who gets an identity | Whatever you write a Certificate for | Every workload, via attestation policy |
| **Workload attestation** | Kubernetes RBAC decides who may create a Certificate | **SPIRE agent verifies the workload itself** — kubelet API, process UID/GID, container image ID, node identity docs |
| Key custody | Secret (or ephemeral, with csi-driver) | Never on disk; delivered over a Unix socket via the Workload API |
| Rotation | Minutes to hours | Minutes, continuous, transparent to the app |
| Cross-cluster / cross-cloud | Manual trust bundle exchange | **Federation** — first-class trust bundle exchange between trust domains |
| Non-Kubernetes workloads | Awkward | First-class (VMs, bare metal, Lambda) |
| Operational weight | One operator | SPIRE server (with its own datastore + HA) + agent DaemonSet |
| Ecosystem | Kubernetes-native | Istio, Linkerd, Envoy, Consul, Vault (Vault 2.0 added a [SPIFFE JWT-SVID engine](https://developer.hashicorp.com/vault/docs/secrets/spiffe)) |

**The real distinction is attestation.** cert-manager's security model is "if you can create a `Certificate` in this namespace, you get this identity" — the identity is granted by Kubernetes RBAC. SPIRE's model is "prove to me *what you are* and I will tell you *who you are*": the [SPIRE server](https://spiffe.io/docs/latest/spire-about/spire-concepts/) holds registration entries mapping attestation selectors — node identity, Kubernetes namespace and service account, container image digest, process UID — to SPIFFE IDs, and the agent verifies those selectors locally before issuing an SVID.

**When cert-manager is enough:** a single trust domain, everything in Kubernetes, DNS-name-based authorization, and you are willing to trust namespace RBAC as your identity boundary. This describes most single-cluster cells, and it is a legitimate answer.

**When you want SPIFFE:**

1. **Multi-cloud, multi-cluster, one logical identity space.** A cell in AWS calling a control-plane service in GCP needs a name that means the same thing in both. SPIFFE federation solves trust bundle exchange between trust domains as a designed feature; with cert-manager you are hand-rolling it.
2. **Non-Kubernetes workloads.** Any VM, bare-metal node, or managed service in the identity mesh.
3. **Image-level attestation.** "Only this exact container digest may hold this identity" is not expressible in cert-manager.
4. **Zero key material at rest.** The SPIFFE Workload API delivers SVIDs over a Unix domain socket; nothing touches disk or etcd.

**The pragmatic middle ground — and probably the right answer for a cell fleet — is `csi-driver-spiffe`.** You get SPIFFE-shaped URI SAN identities, ephemeral per-pod keys that never leave node memory, and automatic renewal, while keeping cert-manager as the issuance path and Vault as the CA. You do not get SPIRE's attestation depth or federation, but you do get identities that are *compatible* with a future SPIRE deployment. It is the option that does not paint you into a corner.

And note the new third option: **`PodCertificateRequest` plus `ClusterTrustBundle`, both stable in Kubernetes 1.37**, give you kubelet-mediated per-pod certificates with automatic rotation using nothing but upstream Kubernetes. You still supply the signer, but the delivery and trust-anchor mechanism is now in the platform. For a team designing cell PKI in late 2026, this deserves evaluation before committing to any of the above.

---

## Designing PKI for cells

Here is a concrete design and the reasoning behind each choice.

```
                    Temporal Internal Root CA          (offline; key in HSM or
                    ECDSA P-384, 20 years               KMS with no online signer)
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   AWS Region CA         GCP Region CA        Azure Region CA   (1-3 years, online,
   ECDSA P-384           ECDSA P-384          ECDSA P-384        name-constrained)
        │                     │                     │
   ┌────┴────┐           ┌────┴────┐           ┌────┴────┐
 Cell-001  Cell-002    Cell-101  Cell-102    Cell-201  Cell-202  (Vault PKI per cell,
 (Vault)   (Vault)     (Vault)   (Vault)     (Vault)   (Vault)    90d-1y, pathlen:0)
    │         │            │         │           │         │
  leaves    leaves       leaves    leaves      leaves    leaves   (24h, ECDSA P-256,
                                                                   serverAuth+clientAuth)
```

**Offline root.** The root's private key lives in an HSM or a cloud KMS key with no online signing path, and it is used perhaps twice a year to sign region intermediates. If the root key can be used by an automated system, it is not offline, regardless of where it is stored. Signing ceremonies are documented, dual-controlled, and recorded.

**Per-region intermediates, name-constrained.** Each region CA carries name constraints permitting only that region's DNS suffix. A compromise of the `usw2` intermediate cannot mint a valid certificate for `euw1`. This is where name constraints earn their keep. It is close to free with Vault PKI (`permitted_dns_domains`); with cert-manager it costs you an alpha feature gate on the controller and the webhook.

**Per-cell issuing CAs in Vault PKI.** Each cell's Vault holds its own issuing intermediate, signed by the region CA. Consequences that matter:

- Cell teardown is clean: destroy the Vault, and the issuing CA's key is gone with it.
- Compromise is bounded to one cell, and the Vault PKI *role* constrains issuance further than the certificate constraints do.
- Every issuance appears in that cell's Vault audit log, giving per-cell attribution for free.
- Cross-cloud is uniform: the Vault PKI API is identical in all three clouds; only the seal differs.

**Short leaves as the revocation strategy.** 24-hour leaves, renewed at 8 hours remaining. There is no CRL and no OCSP responder. Revocation is "stop issuing and wait one day," or in an emergency, "revoke the cell's Vault PKI role and force reissuance." This is a deliberate trade: you accept a 24-hour worst-case window in exchange for eliminating an entire class of infrastructure that does not work reliably anyway. Get explicit agreement on that window from whoever owns your security posture; it is a policy decision, not a technical one.

**Naming and SAN conventions.** Pick them once and enforce them in the Vault PKI role, not in a wiki:

```
DNS SANs:
  <service>.<cell-ns>.svc.cluster.local        # in-cluster
  <service>.<cell-id>.<region>.cells.internal  # cross-cell / cross-cloud

URI SAN:
  spiffe://example.internal/cell/<cell-id>/ns/<ns>/sa/<service-account>

CN: omitted (or the service name, purely cosmetic)
```

The URI SAN is the important one. Costing nothing today, it means that if you adopt SPIRE or a mesh in two years, your existing certificates already carry the right identity shape.

**Key storage.** Root in an HSM or a KMS key with deletion protection and a key policy denying `ScheduleKeyDeletion` to everyone except a break-glass principal. Region intermediates in Vault, protected by the barrier plus seal-wrap. Cell intermediates in each cell's Vault. Leaf keys ephemeral, generated per pod by `csi-driver`, never persisted.

**Audit.** Vault audit devices for issuance (who asked, for what names, when — with at least two devices, per [10-vault.md](10-vault.md#operations)). Kubernetes audit for `Certificate` and `Secret` operations. And a **certificate inventory** — a periodic job that enumerates every Certificate across every cell, records issuer, notAfter, SANs, and key algorithm, and stores it somewhere queryable. You need this to answer "what still chains to root A" during a rotation, and internal PKI has no Certificate Transparency to fall back on.

---

## Multi-cloud: cloud CA vs Vault PKI

| | AWS Private CA | GCP CA Service | Azure Key Vault certificates | Vault PKI |
|---|---|---|---|---|
| Key protection | FIPS 140-3 L3 HSM | Cloud HSM (Enterprise tier) | Managed HSM optional | Vault barrier + seal wrap; PKCS#11 for real HSM (Enterprise) |
| Cost model | **$400/CA/month general-purpose**, or **$50/CA/month short-lived mode** (max 7-day certs); certs $0.75 for the first 1,000, $0.35 for the next 9,000, $0.001 beyond — or $0.058/cert in short-lived mode | Per-CA monthly fee by tier (DevOps vs Enterprise) plus per-certificate; see [pricing](https://cloud.google.com/certificate-authority-service/pricing) | Per-operation, plus Managed HSM if used | Vault license + compute |
| cert-manager integration | External issuer (aws-privateca-issuer) | External issuer (google-cas-issuer) | Via CSI/ESO; no first-class issuer | **Built-in issuer** |
| Cross-cloud portability | None | None | None | **Identical everywhere** |
| Audit | CloudTrail | Cloud Audit Logs | Azure Monitor | Vault audit devices |
| Offline root support | Import an external root | Import an external root | Import | Import or generate |
| Per-cell CA at scale | **$400/mo × N cells is prohibitive**; short-lived mode at $50 helps a lot | Per-CA fee × N | N/A | Marginal |
| Operational burden | Managed | Managed | Managed | You run Vault |

**The recommendation.** For a fleet of cells across three clouds, **Vault PKI as the issuing CA is the right default**, for one dominant reason: it is the only option that is identical in all three clouds. A per-cell CA design where the CA is AWS Private CA in AWS, CAS in GCP, and something else in Azure means three code paths, three IAM models, three audit formats, and three sets of failure modes for what is conceptually one operation. And $400/CA/month multiplied across a growing cell fleet is a real number that gets noticed. AWS's short-lived certificate mode at $50/CA/month with 7-day maximum validity is genuinely attractive if your design already uses 24-hour leaves — worth pricing out.

**Where the cloud CA wins** is compliance. If you need FIPS 140-3 Level 3 HSM attestation for the CA key and you are not buying Vault Enterprise with PKCS#11 support, AWS Private CA gives it to you as a line item. That is a compliance-driven decision, not an engineering one, and it should be made as such.

**A defensible hybrid:** the offline root in a cloud HSM (AWS Private CA in short-lived mode, or a KMS asymmetric key used only for ceremonies), region and cell intermediates in Vault PKI. You get HSM attestation where it matters — the root — and uniform, cheap, fast issuance everywhere else.

---

## Hands-on

**Prerequisites for every lab below:** a running Docker (or Podman) daemon, `kind` v0.30+, `kubectl`, `helm` v3 or v4, `jq`, `openssl` 3.x, and roughly 4 GB of free RAM for the kind node. Labs 2–6 each assume the cluster and the objects created by the preceding labs, so run them in order. Everything is local — no cloud account and no cost.

### Lab 1 — Install cert-manager on kind, 20 minutes

```bash
kind create cluster --name pki-lab

helm repo add jetstack https://charts.jetstack.io && helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.21.1 \
  --set crds.enabled=true

kubectl -n cert-manager get pods
# cert-manager, cert-manager-webhook, cert-manager-cainjector

# cmctl is worth installing now; it collapses four kubectl commands into one.
# https://cert-manager.io/docs/reference/cmctl/
cmctl check api
```

Also install `step` for reading certificates — `openssl x509 -text` works but `step certificate inspect` is dramatically more readable.

```bash
brew install step   # or https://smallstep.com/docs/step-cli/installation/
```

### Lab 2 — Self-signed root → intermediate → leaf, 45 minutes

Save the five-document manifest from the "SelfSigned → CA: building a hierarchy" section above into `hierarchy.yaml` first — it is not created for you — then apply it and read what you built.

```bash
kubectl apply -f hierarchy.yaml   # the SelfSigned → root → CA → intermediate → cell-issuer chain

kubectl -n cert-manager get certificate
kubectl -n cert-manager wait --for=condition=Ready certificate/temporal-root-ca --timeout=60s
kubectl -n cert-manager wait --for=condition=Ready certificate/temporal-intermediate-usw2 --timeout=60s
```

Now a leaf:

```bash
kubectl create namespace cell-usw2-042

kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: temporal-frontend
  namespace: cell-usw2-042
spec:
  secretName: temporal-frontend-tls
  duration: 24h
  renewBefore: 8h
  commonName: temporal-frontend
  dnsNames:
    - temporal-frontend.cell-usw2-042.svc.cluster.local
    - temporal-frontend.cell-usw2-042.svc
  uris:
    - spiffe://example.internal/cell/usw2-042/ns/cell-usw2-042/sa/temporal-frontend
  usages:
    - digital signature
    - key encipherment
    - server auth
    - client auth
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Always
  issuerRef:
    name: cell-issuer
    kind: ClusterIssuer
EOF
```

**Read the certificates. This is the actual exercise.**

```bash
kubectl -n cell-usw2-042 get secret temporal-frontend-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > leaf.pem

step certificate inspect leaf.pem
openssl x509 -in leaf.pem -noout -text

# What is in tls.crt? Count the certificates.
grep -c 'BEGIN CERTIFICATE' leaf.pem     # leaf + intermediate

# And ca.crt?
kubectl -n cell-usw2-042 get secret temporal-frontend-tls \
  -o jsonpath='{.data.ca\.crt}' | base64 -d | step certificate inspect --short -
```

```bash
# Pull out just the fields that matter
step certificate inspect leaf.pem --format json | jq '{
  subject: .subject, sans: .extensions.subject_alt_name,
  eku: .extensions.extended_key_usage, ku: .extensions.key_usage,
  serial: .serial_number, notAfter: .validity.end }'
```

Verify the chain by hand:

```bash
kubectl -n cert-manager get secret temporal-root-ca \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > root.pem
kubectl -n cert-manager get secret temporal-intermediate-usw2 \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > int.pem

openssl verify -CAfile root.pem -untrusted int.pem leaf.pem
# leaf.pem: OK

# Now prove that the chain matters: verify without the intermediate.
openssl verify -CAfile root.pem leaf.pem
# CN = temporal-frontend
# error 20 at 0 depth lookup: unable to get local issuer certificate
# error leaf.pem: verification failed
# (exit status 2)
```

**Exercises:**

1. Remove `client auth` from `usages`, wait for reissuance, and inspect. Then set up a two-pod mTLS test and watch the client side fail while the server side works.
2. Try to issue another `isCA: true` certificate from `cell-issuer` and observe that cert-manager happily does it — there is no `pathLen` control on the `Certificate` API ([cert-manager#2820](https://github.com/cert-manager/cert-manager/issues/2820)), so the CA issuer cannot enforce the shape. Then build the same constraint by hand with openssl (`basicConstraints = critical,CA:TRUE,pathlen:0`) and confirm `openssl verify` rejects a three-deep chain. That gap is what Vault PKI's `max_path_length` closes.
3. Create a certificate with only `commonName` and no `dnsNames`. Serve it from a Go HTTP server and connect with a Go client. Read the error. This is the death of CN, demonstrated.

### Lab 3 — trust-manager distribution, 30 minutes

```bash
helm upgrade trust-manager oci://quay.io/jetstack/charts/trust-manager \
  --install --namespace cert-manager --wait

kubectl label namespace cell-usw2-042 example.com/cell=true
```

**Do it the wrong way first**, so the failure is concrete:

```bash
kubectl apply -f - <<'EOF'
apiVersion: trust.cert-manager.io/v1alpha1
kind: Bundle
metadata:
  name: temporal-internal-trust
spec:
  sources:
    - useDefaultCAs: true
    - secret:
        name: temporal-root-ca      # DIRECTLY at the cert-manager Secret. Bad.
        key: tls.crt
  target:
    configMap:
      key: ca-certificates.crt
    namespaceSelector:
      matchLabels:
        example.com/cell: "true"
EOF

kubectl get bundle temporal-internal-trust
kubectl -n cell-usw2-042 get cm temporal-internal-trust \
  -o jsonpath='{.data.ca-certificates\.crt}' | grep -c 'BEGIN CERTIFICATE'
```

Now trigger the disaster:

```bash
# Force the root to be regenerated with a new key. Keep the leaf.pem and int.pem
# you saved in Lab 2 — they are what you use to prove the break.
kubectl -n cert-manager delete secret temporal-root-ca

# Do NOT `kubectl wait --for=condition=Ready` here: the Certificate's Ready
# condition is still True from the previous issuance, so wait returns instantly
# and you race the controller. Wait for the Secret to come back instead.
until kubectl -n cert-manager get secret temporal-root-ca >/dev/null 2>&1; do sleep 2; done

sleep 15
# The bundle now contains the NEW root only. Verify the old leaf no longer validates:
kubectl -n cell-usw2-042 get cm temporal-internal-trust \
  -o jsonpath='{.data.ca-certificates\.crt}' > bundle.pem
openssl verify -CAfile bundle.pem -untrusted int.pem leaf.pem
# FAILS. Every service holding a leaf from the old root is now untrusted.
```

**Now do it right.** Rebuild the hierarchy, then decouple the Bundle from the live Secret by pointing it at a ConfigMap only you change:

```bash
kubectl -n cert-manager get secret temporal-root-ca \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > root.pem
kubectl -n cert-manager create configmap temporal-trust-anchors \
  --from-file=roots.pem=root.pem

kubectl patch bundle temporal-internal-trust --type=json -p='[
  {"op":"replace","path":"/spec/sources/1",
   "value":{"configMap":{"name":"temporal-trust-anchors","key":"roots.pem"}}}]'
```

### Lab 4 — A full root rotation, 60 minutes

The most valuable exercise in this guide.

```bash
# --- Phase 1: create root B, do not use it ---
kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: temporal-root-ca-b
  namespace: cert-manager
spec:
  isCA: true
  commonName: Temporal Internal Root CA B
  secretName: temporal-root-ca-b
  duration: 87600h
  privateKey: { algorithm: ECDSA, size: 384 }
  issuerRef: { name: selfsigned-bootstrap, kind: ClusterIssuer }
EOF

kubectl -n cert-manager wait --for=condition=Ready certificate/temporal-root-ca-b --timeout=60s
kubectl -n cert-manager get secret temporal-root-ca-b \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > rootB.pem
```

```bash
# --- Phase 2: distribute trust for BOTH. Verify propagation. ---
cat root.pem rootB.pem > both-roots.pem
kubectl -n cert-manager create configmap temporal-trust-anchors \
  --from-file=roots.pem=both-roots.pem --dry-run=client -o yaml | kubectl apply -f -

sleep 15
kubectl -n cell-usw2-042 get cm temporal-internal-trust \
  -o jsonpath='{.data.ca-certificates\.crt}' > bundle.pem
# Old leaves still validate:
openssl verify -CAfile bundle.pem -untrusted int.pem leaf.pem   # OK
```

```bash
# --- Phase 3: intermediate B, switch issuance ---
kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: temporal-root-issuer-b }
spec: { ca: { secretName: temporal-root-ca-b } }
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: { name: temporal-intermediate-usw2-b, namespace: cert-manager }
spec:
  isCA: true
  commonName: Temporal Intermediate CA usw2 B
  secretName: temporal-intermediate-usw2-b
  duration: 26280h
  privateKey: { algorithm: ECDSA, size: 384 }
  usages: ["cert sign", "crl sign", "digital signature"]
  issuerRef: { name: temporal-root-issuer-b, kind: ClusterIssuer }
EOF

kubectl -n cert-manager wait --for=condition=Ready \
  certificate/temporal-intermediate-usw2-b --timeout=60s

# Repoint the issuer workloads use.
kubectl patch clusterissuer cell-issuer --type=merge \
  -p '{"spec":{"ca":{"secretName":"temporal-intermediate-usw2-b"}}}'
```

```bash
# --- Phase 4: force reissuance ---
cmctl renew --all -n cell-usw2-042
kubectl -n cell-usw2-042 get certificaterequest -w    # ctrl-c when Ready

kubectl -n cell-usw2-042 get secret temporal-frontend-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > leaf-b.pem
step certificate inspect leaf-b.pem --short    # issuer is now "... CA usw2 B"
```

```bash
# --- Phase 5: verify nothing depends on root A ---
kubectl -n cell-usw2-042 get secret -o json | \
  jq -r '.items[] | select(.type=="kubernetes.io/tls") | .metadata.name'
# For each, confirm it chains to root B and NOT to root A.

# --- Phase 6: remove root A from the anchors ---
kubectl -n cert-manager create configmap temporal-trust-anchors \
  --from-file=roots.pem=rootB.pem --dry-run=client -o yaml | kubectl apply -f -
sleep 15
kubectl -n cell-usw2-042 get cm temporal-internal-trust \
  -o jsonpath='{.data.ca-certificates\.crt}' | grep -c 'BEGIN CERTIFICATE'
```

**Now do it wrong on purpose** and time how long the outage lasts: skip Phase 2 entirely — switch the issuer first, *then* update the anchors. Every service is broken for the duration of the gap. Write down the number. That number is the argument for the procedure.

### Lab 5 — ACME with a local ACME server, 30 minutes

Do not point at Let's Encrypt production while learning; you will burn rate limits. Run [Pebble](https://github.com/letsencrypt/pebble), Let's Encrypt's test ACME server, in-cluster:

```bash
kubectl create namespace pebble
kubectl -n pebble create deployment pebble --image=ghcr.io/letsencrypt/pebble:latest \
  -- pebble -config /test/config/pebble-config.json -dnsserver 10.96.0.10:53
kubectl -n pebble expose deployment pebble --port=14000
```

Two Pebble facts that will otherwise cost you the afternoon: it validates HTTP-01 against **port 5002**, not port 80, so a stock Ingress will never satisfy a challenge; and it deliberately rejects ~5% of nonces to exercise client retry logic. For a lab that is about watching the *state machine* rather than about real validation, set `PEBBLE_VA_ALWAYS_VALID=1` on the Deployment so every challenge passes, and turn it off later if you want to debug real solver routing.

```bash
kubectl -n pebble set env deployment/pebble PEBBLE_VA_ALWAYS_VALID=1
```

Point an ACME `ClusterIssuer` at `https://pebble.pebble.svc:14000/dir` with `skipTLSVerify: true`, create an Ingress with the `cert-manager.io/cluster-issuer` annotation, then watch the state machine:

```bash
kubectl get order,challenge -A
kubectl describe challenge -n cell-usw2-042
kubectl -n cell-usw2-042 get pods    # note the ephemeral cm-acme-http-solver pod
```

**Exercises:**

1. Delete the solver pod mid-challenge and watch the retry behavior.
2. Break the Ingress routing so the solver is unreachable. Read the Challenge status message — it tells you exactly what the ACME server saw.
3. Read the [Let's Encrypt rate limits](https://letsencrypt.org/docs/rate-limits/) page and calculate: with 50 certificates per registered domain per 7 days, how many cells can you provision per week under `*.temporal.io` before you need an override? (The answer motivates per-cell subdomains or a private CA.)

### Lab 6 — The reload problem, demonstrated

```bash
# Serve a 1-hour cert from a pod that reads it once at startup.
# Force reissuance and watch the wire NOT change.
cmctl renew temporal-frontend -n cell-usw2-042

# Secret says the new notAfter:
kubectl -n cell-usw2-042 get secret temporal-frontend-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | \
  openssl x509 -noout -enddate

# The wire says the old one:
kubectl -n cell-usw2-042 exec deploy/probe -- \
  openssl s_client -connect temporal-frontend:8443 </dev/null 2>/dev/null | \
  openssl x509 -noout -enddate
```

**This divergence is the thing to monitor.** Write a probe that compares the two. Then fix the app with `GetCertificate` and confirm the divergence disappears.

---

## Production gotchas

1. **Go disabled Common Name fallback by default in Go 1.15 and removed the `GODEBUG=x509ignoreCN=0` escape hatch entirely in Go 1.17.** A certificate with a CN but no SAN is invalid to Kubernetes, Temporal, Envoy, and essentially every cloud-native component. Always set `dnsNames`. The error message will complain about the hostname, not about the missing SAN. Source: [Go 1.15 release notes](https://go.dev/doc/go1.15#commonname) and [RFC 6125](https://www.rfc-editor.org/rfc/rfc6125).

2. **The CA/Browser Forum's 200-day maximum for public TLS certificates has been in force since 2026-03-15**, dropping to 100 days on 2027-03-15 and 47 days on 2029-03-15, with domain validation reuse falling to 10 days. Anything with a manual renewal step for public certificates is already on borrowed time. Source: [Ballot SC-081v3](https://cabforum.org/2025/04/11/ballot-sc081v3-introduce-schedule-of-reducing-validity-and-data-reuse-periods/).

3. **Pointing a trust-manager `Bundle` directly at a cert-manager Secret means the next root rotation instantly distrusts every existing leaf.** The trust-manager docs warn about this explicitly. Copy roots into a dedicated ConfigMap you control and point the Bundle at that. Source: [trust-manager — Intentionally Copying CA Certificates](https://cert-manager.io/docs/trust/trust-manager/#cert-manager-integration-intentionally-copying-ca-certificates).

4. **Never put an intermediate in a trust store to "fix" a chain error.** It makes the intermediate a de facto root that cannot be rotated without updating every trust store first — destroying the reason intermediates exist. Fix the server's chain instead. Source: [trust-manager — Bundling Intermediates](https://cert-manager.io/docs/trust/trust-manager/#bundling-intermediates).

5. **`spec.privateKey.rotationPolicy` defaulted to `Never` before cert-manager v1.18 and defaults to `Always` from v1.18 onward.** Wherever `Never` is still in effect — an older cluster, or a manifest that pins it — renewal reuses the existing private key and a leaked key stays leaked. Audit for explicit `Never`. Source: [Certificate resource docs](https://cert-manager.io/docs/usage/certificate/).

6. **`ca.crt` in a cert-manager Secret is populated on a best-effort basis and some issuers cannot fill it at all.** Do not build a trust pipeline that assumes it exists. Source: [trust-manager — `ca.crt` vs `tls.crt`](https://cert-manager.io/docs/trust/trust-manager/#cert-manager-integration-cacrt-vs-tlscrt).

7. **Secrets mounted with `subPath` are never updated by the kubelet.** The certificate renews, the file on disk does not, and the pod serves an expired cert until it restarts. Mount the whole volume. Source: [Kubernetes — projected volumes / mounted ConfigMaps are updated automatically](https://kubernetes.io/docs/concepts/storage/volumes/#configmap).

8. **cert-manager's job ends when it writes the Secret; nothing reloads your application.** The observable symptom is a Secret with a future `notAfter` and a wire certificate with a past one. Monitor the wire, not the Secret, and implement `GetCertificate` or use Reloader.

9. **Missing `clientAuth` in EKU breaks mTLS in a maximally confusing way**: the server half of every handshake works and the client half is rejected, so exactly half your service graph appears healthy. Always set both `server auth` and `client auth` for mesh certificates.

10. **cert-manager 1.21 removed the default `serviceaccounts/token: create` Role and RoleBinding from the Helm chart.** If you used `serviceAccountRef.name` pointing at the controller's own ServiceAccount (an undocumented pattern), DNS-01 and Vault auth break on upgrade. Create the RBAC yourself or move to a dedicated ServiceAccount. Source: [cert-manager 1.21 release notes](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/).

11. **cert-manager 1.21 removed three Prometheus Helm values** (`prometheus.servicemonitor.targetPort`, `prometheus.servicemonitor.path`, `prometheus.podmonitor.path`) and renamed the metrics port from `tcp-prometheus-servicemonitor` to `http-metrics`. Because the values schema uses `additionalProperties: false`, leaving any of them in your overrides is a hard schema validation failure on upgrade. Source: same release notes.

12. **cert-manager 1.21 tightened the `cert-manager-edit` aggregate ClusterRole** (GHSA-8rvj-mm4h-c258), removing `create` on Challenges and `create`/`patch`/`update` on Orders. Tooling that manipulated those directly needs explicit grants. Already shipped in 1.20.3 and 1.19.6. Source: same release notes.

13. **Let's Encrypt allows 50 certificates per registered domain per 7 days, and 5 per exact set of identifiers per 7 days.** Provisioning cells under one apex domain hits the first limit fast; a CrashLooping test setup hits the second immediately. ARI-coordinated renewals are exempt from all rate limits — cert-manager 1.21 added ARI behind the `ACMEUseARI` feature gate. Use the staging environment for anything experimental. Source: [Let's Encrypt rate limits](https://letsencrypt.org/docs/rate-limits/), updated 2026-08-05.

14. **Let's Encrypt also caps consecutive authorization failures per identifier at 1,152**, after which the account is *paused* for that identifier and requires a self-service unpause. A permanently broken DNS-01 solver will eventually get you paused, not just throttled. Source: same page.

15. **Stuck ACME Orders and Challenges usually mean the ACME server saw something you did not.** `kubectl describe challenge` contains the server's own error text. Common causes: split-horizon DNS (the solver resolves internally but the CA resolves externally), NAT hairpin, and port 80 blocked. cert-manager 1.21 added `waitInsteadOfSelfCheck` as an escape hatch for exactly these environments. Source: [Troubleshooting ACME](https://cert-manager.io/docs/troubleshooting/acme/).

16. **cert-manager's default CertificateRequest retry backoff caps at 32 hours.** A transient CA outage can leave a failed request sitting for well over a day. cert-manager 1.21 made this configurable via `--certificate-request-maximum-backoff-duration`. Lower it if your CA has scheduled maintenance windows. Source: [cert-manager 1.21 release notes](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/).

17. **Clock skew breaks certificate validation in both directions.** A node whose clock is ahead sees freshly issued certificates as not-yet-valid (`notBefore` in the future); a node behind sees expired ones as valid. cert-manager sets a small `notBefore` backdate, but a node minutes off will still fail. Monitor NTP as a PKI dependency.

18. **Deleting a `Certificate`'s Secret triggers immediate reissuance, and deleting the `Certificate` does not delete the Secret** (unless `enableCertificateOwnerRef` is set). The asymmetry produces two distinct messes: a "cleanup" script that deletes Secrets causes a reissuance storm and can hit rate limits, while deleting Certificates leaves orphaned Secrets that nothing renews. Source: [cert-manager FAQ](https://cert-manager.io/docs/faq/).

19. **A manually created Secret at a `Certificate`'s `secretName` will be overwritten.** cert-manager takes ownership. The reverse — an app that writes to the Secret — produces a fight loop where cert-manager and the app overwrite each other every reconcile.

20. **cert-manager CRDs are not upgraded by `helm upgrade` unless `crds.enabled=true` was used at install.** If you installed CRDs separately via `kubectl apply`, you must upgrade them separately, and skipping a minor version is unsupported. Upgrading from 1.19 to 1.21 requires going through 1.20. Source: [Upgrading cert-manager](https://cert-manager.io/docs/installation/upgrade/).

21. **cert-manager 1.21 supports Kubernetes 1.33–1.36 only.** Kubernetes 1.37 shipped 2026-08-26 with ClusterTrustBundle and PodCertificateRequest going stable; cert-manager 1.22 (~November 2026) is the first release to support it. Check the [supported releases table](https://cert-manager.io/docs/releases/) before upgrading a cluster ahead of cert-manager.

22. **cert-manager's webhook is a cluster-wide availability dependency.** If it is unreachable, every `kubectl apply` of a cert-manager resource fails, and a badly scoped `namespaceSelector` can affect unrelated resources. Never let cainjector and the webhook be co-scheduled on a single node in a cell that matters; set PDBs and topology spread.

23. **Two cert-manager bugs fixed in 1.21 are worth recognizing by symptom.** An issuer returning an already-expired certificate caused an *infinite re-issuance loop* (symptom: a Certificate reissuing continuously with no errors — check the CA's clock and its own expiry), and `renewBeforePercentage` had an *integer overflow* for durations longer than roughly three years, producing incorrect validation and renewal times. Both are further arguments against long-lived leaves. Source: [cert-manager 1.21 release notes](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/).

24. **trust-manager's default CA package is a container image you must keep updated.** Leaving it pinned is equivalent to never running `apt-get upgrade ca-certificates` — you will keep trusting distrusted roots and will not trust newly added ones. Source: [trust-manager — Securely Maintaining an Installation](https://cert-manager.io/docs/trust/trust-manager/#securely-maintaining-a-trust-manager-installation).

25. **trust-manager's empty `namespaceSelector` currently syncs to all namespaces, and the docs warn this behavior will change.** Always set the selector explicitly so the upgrade is a no-op. Source: same docs.

26. **Expired-certificate outages happen to organizations with mature ops functions, because the certificate that expires is always the one outside the inventory.** Microsoft Teams was down roughly three hours in February 2020 on an expired auth certificate; Spotify had roughly an hour of downtime in August 2020 on an expired cert on an *internal, non-customer-facing* service that monitoring did not cover; Ericsson's expired certificate took O2's UK 4G network down for nearly a day in December 2018, affecting around 32 million subscribers. The common thread, per [this incident analysis](https://www.configclarity.dev/incidents/ssl-expiry-outages/) (secondary source), is that the certificate existed outside whatever inventory the team assumed was complete. **Build the inventory, and probe the wire.**

27. **Distroless and scratch containers may have no system trust store at all.** A Go binary in `gcr.io/distroless/static` fails every outbound TLS call with an opaque error because `/etc/ssl/certs` does not exist. Use the `base` variant, or mount a trust bundle explicitly.

---

## How this shows up in cell lifecycle

**Provisioning.** PKI is on the critical path for a cell reaching "ready," and the order is fixed:

1. Region intermediate must already exist. If it does not, a human signing ceremony blocks the cell — which is why intermediates are created ahead of demand, not on demand.
2. Create the cell's Vault PKI mount, generate a CSR, sign it with the region CA, import the chain, and configure the PKI role with the cell's name constraints. Schedule `tidy`.
3. Install cert-manager and configure the Vault `ClusterIssuer` with Kubernetes auth. Verify by issuing a throwaway certificate before anything real depends on it.
4. Install trust-manager and create the anchors ConfigMap and the `Bundle`. **Trust distribution must complete before any workload starts**, or the first pods come up unable to validate each other.
5. Issue the cell's workload certificates. Verify on the wire, not just in the Secret.
6. Register every certificate in the fleet inventory.

The dependency worth calling out: cert-manager needs Vault, Vault needs KMS, KMS needs a network path the networking team owns. A cell that will not come up because certificates will not issue is often actually a networking incident three layers down. Build the diagnostic that distinguishes these, or you will debug it from the top every time.

**Steady state.** With 24-hour leaves, every certificate in the cell renews about once a day, which means the renewal path is continuously exercised and cannot silently rot. What you monitor:

- `certmanager_certificate_expiration_timestamp_seconds` — alert at 2× `renewBefore` remaining, not at expiry.
- `certmanager_certificate_ready_status` — any Certificate not Ready for more than an hour.
- **A wire probe** comparing the certificate served on each service port against the Secret. This catches the reload failure, which no cert-manager metric can see.
- Vault PKI issuance rate per cell — a spike means a reissuance loop; a drop to zero means issuance is broken and you have hours, not days.

**Upgrading a cell.** cert-manager upgrades are single-minor-version steps with CRD updates, and the last release alone had three breaking Helm changes. Test in one cell, then roll. Never skip a minor version. And check the Kubernetes compatibility matrix in both directions — upgrading Kubernetes past what your cert-manager supports is as bad as the reverse.

**Root rotation.** A quarter-long, fleet-wide project, not a change ticket. It is the reason the two-level hierarchy exists — so that routine rotation happens at the intermediate layer, which needs no trust distribution at all. The runbook is the seven-phase procedure above, and it should have been rehearsed in a scratch cell before it is needed.

**Teardown.** Destroy the cell's Vault and the issuing CA's private key goes with it — clean, and one of the strongest arguments for per-cell Vault PKI. Remaining work: remove the cell's intermediate from any inventory or CRL, remove the cell's namespace label so trust-manager stops syncing, and delete the fleet-inventory entries. A cell whose certificates outlive it is a set of valid credentials for infrastructure that no longer exists — short lifetimes mean this self-corrects within a day, which is exactly the point.

**Cross-cloud.** Vault PKI makes the issuance path identical in AWS, GCP, and Azure; only the DNS-01 solver configuration and the Vault seal differ. Keep those two as the only cloud-conditional parts of your cell PKI module.

---

## Learning path

### Day 1 (3–4 hours)

- Read [cert-manager Concepts](https://cert-manager.io/docs/concepts/) and the [Certificate resource docs](https://cert-manager.io/docs/usage/certificate/) end to end. Then skim [RFC 5280 §4.2](https://www.rfc-editor.org/rfc/rfc5280#section-4.2) — just the extensions section, not the whole thing.
- Run **Lab 1** and **Lab 2**. Spend most of the time in `step certificate inspect` and `openssl verify`. Reading certificates fluently is the underrated skill here.
- Be able to explain out loud: root vs intermediate and why; what `tls.crt` contains vs what belongs in a trust store; why CN does not work anymore; why `clientAuth` matters for mTLS.

### Week 1 (10–15 hours)

- Run **Labs 3, 4, 5, and 6**. Lab 4 (root rotation) is the one that matters; do the "wrong way" version too and time the outage.
- Read the [trust-manager docs](https://cert-manager.io/docs/trust/trust-manager/) in full, especially "Preparing for Production." It is the best-written explanation of the trust rotation problem anywhere.
- Read the [cert-manager 1.21 release notes](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/) and the [supported releases table](https://cert-manager.io/docs/releases/). Know the three breaking changes.
- Read [Ballot SC-081v3](https://cabforum.org/2025/04/11/ballot-sc081v3-introduce-schedule-of-reducing-validity-and-data-reuse-periods/) — at minimum the Benefits and Revocation sections. It is the clearest statement of why short lifetimes beat revocation.
- Set up Vault PKI as the issuing CA behind cert-manager (combining this with [10-vault.md](10-vault.md#lab-2--kubernetes-auth-on-kind-45-minutes)'s Lab 2, which builds the Kubernetes auth mount the Vault issuer needs). This is the production shape.

### Month 1 (ongoing)

- **Write the root rotation runbook for your actual fleet** and execute it in a scratch cell. Measure `P` (trust propagation time) empirically — do not guess it.
- **Build the certificate inventory.** A job that walks every cell, records issuer/notAfter/SANs/algorithm for every certificate, and exposes it queryably. You cannot rotate a root without it, and there is no CT log for internal PKI.
- **Build the wire probe.** For every service port in a cell, compare the certificate on the wire against the Secret. This is the only monitor that catches the reload failure.
- Read the [SPIFFE concepts](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/) and [SPIRE concepts](https://spiffe.io/docs/latest/spire-about/spire-concepts/) docs and deploy `csi-driver-spiffe` in a lab. Form an opinion on whether the cell fleet should adopt SPIFFE identities now (cheap: just URI SANs) or SPIRE later (expensive: another control plane).
- Evaluate **`PodCertificateRequest` + `ClusterTrustBundle`** on a Kubernetes 1.37 cluster. Both went stable on 2026-08-26 and they materially change the trust-distribution and per-pod-certificate story. Whatever you design now should be able to migrate onto them.
- Price out **AWS Private CA short-lived mode ($50/CA/month, 7-day max validity)** against Vault PKI for your projected cell count. If your design already uses 24-hour leaves, the compliance story may be worth the money for the root.

---

## References

1. [cert-manager Documentation](https://cert-manager.io/docs/) — the project's own docs; unusually good, and the primary source for everything cert-manager-specific here.
2. [cert-manager Supported Releases](https://cert-manager.io/docs/releases/) — the release/EOL table and Kubernetes compatibility matrix. 1.21 (Jul 08 2026) supports K8s 1.33–1.36; 1.22 lands ~Nov 2026.
3. [cert-manager 1.21 Release Notes](https://cert-manager.io/docs/releases/release-notes/release-notes-1.21/) — the three breaking changes, ARI support, AWS IAM auth for the Vault issuer, renewal policies, and the reissuance-loop and `renewBeforePercentage` fixes.
4. [Upgrading cert-manager](https://cert-manager.io/docs/installation/upgrade/) — CRD handling and the no-skipping-minors rule.
5. [Certificate resource — cert-manager](https://cert-manager.io/docs/usage/certificate/) — `duration`, `renewBefore`, `renewBeforePercentage`, `rotationPolicy`, and the new `renewal` field.
6. [Issuer concepts — cert-manager](https://cert-manager.io/docs/concepts/issuer/) — Issuer vs ClusterIssuer and the issuer type catalog.
7. [ACME Orders and Challenges — cert-manager](https://cert-manager.io/docs/concepts/acme-orders-challenges/) — the Order/Challenge state machine you will be reading in incidents.
8. [CA Injector concepts — cert-manager](https://cert-manager.io/docs/concepts/ca-injector/) — the three injection annotations and which resource kinds they work on.
9. [Vault Issuer configuration — cert-manager](https://cert-manager.io/docs/configuration/vault/) — Kubernetes auth via `serviceAccountRef`, plus the new AWS IAM auth path.
10. [CA Issuer configuration — cert-manager](https://cert-manager.io/docs/configuration/ca/) — the simplest real CA, with the key in a Secret.
11. [SelfSigned Issuer — cert-manager](https://cert-manager.io/docs/configuration/selfsigned/) — bootstrapping a root.
12. [ACME DNS-01 Route53 — cert-manager](https://cert-manager.io/docs/configuration/acme/dns01/route53/) — IRSA and the cross-account `role` field.
13. [ACME DNS-01 Google CloudDNS — cert-manager](https://cert-manager.io/docs/configuration/acme/dns01/google/) — Workload Identity setup.
14. [ACME DNS-01 AzureDNS — cert-manager](https://cert-manager.io/docs/configuration/acme/dns01/azuredns/) — managed identity and workload identity.
15. [ACME configuration — cert-manager](https://cert-manager.io/docs/configuration/acme/) — solver selection, `preferredChain`, and `waitInsteadOfSelfCheck`.
16. [Troubleshooting ACME — cert-manager](https://cert-manager.io/docs/troubleshooting/acme/) — the runbook for stuck Orders and Challenges.
17. [Annotations reference — cert-manager](https://cert-manager.io/docs/reference/annotations/) — every ingress-shim and cainjector annotation in one place.
18. [Gateway API usage — cert-manager](https://cert-manager.io/docs/usage/gateway/) — listener-based certificate management.
19. [csi-driver — cert-manager](https://cert-manager.io/docs/usage/csi-driver/) — ephemeral per-pod certificates with no Secret.
20. [csi-driver-spiffe — cert-manager](https://cert-manager.io/docs/usage/csi-driver-spiffe/) — SPIFFE SVIDs via CSI Token Requests; key never leaves node memory.
21. [csi-driver-spiffe — GitHub](https://github.com/cert-manager/csi-driver-spiffe) — the property list (auto-renewal, per-pod uniqueness, destroyed on termination).
22. [trust-manager — cert-manager](https://cert-manager.io/docs/trust/trust-manager/) — Bundle sources and targets, JKS/PKCS#12 support, and the best written explanation of the trust rotation problem. Read "Preparing for Production" twice.
23. [trust-manager is moving to ClusterBundle — cert-manager Announcements](https://cert-manager.io/announcements/2025/09/05/trust-manager-clusterbundle-future/) — the `trust-manager.io/v1alpha2` API migration to plan for.
24. [Best Practice Installation Options — cert-manager](https://cert-manager.io/docs/installation/best-practice/) — hardened install values worth adopting wholesale.
25. [Prometheus Metrics — cert-manager](https://cert-manager.io/docs/devops-tips/prometheus-metrics/) — `certmanager_certificate_expiration_timestamp_seconds` and friends.
26. [cmctl — cert-manager](https://cert-manager.io/docs/reference/cmctl/) — `cmctl status certificate` collapses four kubectl commands into one; `cmctl renew` forces reissuance.
27. [RFC 5280 — Internet X.509 PKI Certificate and CRL Profile](https://www.rfc-editor.org/rfc/rfc5280) — the normative source for Basic Constraints, Key Usage, Name Constraints, and path validation. Read §4.2 and §6.
28. [RFC 6125 — Service Identity in TLS](https://www.rfc-editor.org/rfc/rfc6125) — why SANs replaced CN.
29. [RFC 8555 — ACME](https://www.rfc-editor.org/rfc/rfc8555) — the protocol behind Order and Challenge.
30. [RFC 9773 — ACME Renewal Information (ARI)](https://www.rfc-editor.org/rfc/rfc9773) — the renewal-window extension cert-manager 1.21 added behind `ACMEUseARI`; ARI renewals are exempt from Let's Encrypt rate limits.
31. [Ballot SC-081v3 — CA/Browser Forum](https://cabforum.org/2025/04/11/ballot-sc081v3-introduce-schedule-of-reducing-validity-and-data-reuse-periods/) — the 398 → 200 → 100 → 47 day schedule, the vote tally, and the clearest published argument for why revocation does not scale.
32. [CA/Browser Forum TLS Baseline Requirements — GitHub](https://github.com/cabforum/servercert) — the live normative text and its release history.
33. [Let's Encrypt Rate Limits](https://letsencrypt.org/docs/rate-limits/) — updated 2026-08-05; 50 certs per registered domain per week, 5 per exact identifier set, 1,152 consecutive-failure pause, and the ARI exemption.
34. [Let's Encrypt Staging Environment](https://letsencrypt.org/docs/staging-environment/) — use it for everything experimental.
35. [Pebble — GitHub](https://github.com/letsencrypt/pebble) — Let's Encrypt's small test ACME server; run it locally instead of burning rate limits.
36. [Kubernetes v1.37 release announcement](https://kubernetes.io/blog/2026/08/26/kubernetes-v1-37-release/) — ClusterTrustBundle (KEP-3257) and PodCertificateRequest (KEP-4317) both reaching stable.
37. [KEP-3257: ClusterTrustBundles — kubernetes/enhancements](https://github.com/kubernetes/enhancements/issues/3257) — the design history from alpha in 1.31 through GA.
38. [KEP-4317: Pod Certificates — kubernetes/enhancements](https://github.com/kubernetes/enhancements/tree/master/keps/sig-auth/4317-pod-certificates) — kubelet-mediated per-pod X.509 with automatic rotation.
39. [ClusterTrustBundle API reference — Kubernetes](https://kubernetes.io/docs/reference/kubernetes-api/certificates/cluster-trust-bundle-v1beta1/) — the object shape and signer-name semantics.
40. [SPIFFE Concepts](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/) — SPIFFE IDs, SVIDs, trust domains, and the Workload API.
41. [SPIRE Concepts](https://spiffe.io/docs/latest/spire-about/spire-concepts/) — registration entries, attestation selectors, and federation. The attestation model is the thing cert-manager does not have.
42. [X509-SVID specification — SPIFFE](https://spiffe.io/docs/latest/spiffe-specs/x509-svid/) — the URI SAN format; adopt it in your naming convention even if you never deploy SPIRE.
43. [Vault PKI secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/pki) — roles, constraints, and `tidy`; the issuing CA behind cert-manager.
44. [Vault SPIFFE secrets engine — HashiCorp Developer](https://developer.hashicorp.com/vault/docs/secrets/spiffe) — JWT-SVID issuance added in Vault 2.0 (Enterprise).
45. [AWS Private CA Pricing](https://aws.amazon.com/private-ca/pricing/) — $400/CA/month general-purpose vs $50/CA/month short-lived mode (7-day max), plus per-certificate tiers.
46. [AWS Private CA Issuer — GitHub](https://github.com/cert-manager/aws-privateca-issuer) — the external issuer, authenticated with IRSA.
47. [Google Cloud Certificate Authority Service — Overview](https://cloud.google.com/certificate-authority-service/docs/ca-service-overview) and [Pricing](https://cloud.google.com/certificate-authority-service/pricing) — DevOps vs Enterprise tiers.
48. [Google CAS Issuer — GitHub](https://github.com/jetstack/google-cas-issuer) — the external issuer for CAS.
49. [Reloader — GitHub](https://github.com/stakater/Reloader) — annotation-driven restart on Secret change; the blunt fix for the reload problem.
50. [SSL Certificate Expiry Outages: Microsoft, Ericsson, Spotify — ConfigClarity](https://www.configclarity.dev/incidents/ssl-expiry-outages/) — *secondary source*; a useful compilation of expired-certificate incidents and the common inventory-gap root cause.
51. [Impacts of an Expired TLS Certificate — ThousandEyes](https://www.thousandeyes.com/blog/impacts-expired-tls-certificate) — *secondary source*; network-level analysis of what an expiry actually looks like from the outside.
52. [step CLI documentation — Smallstep](https://smallstep.com/docs/step-cli/) — `step certificate inspect` is the fastest way to read a certificate; install it before you need it.
