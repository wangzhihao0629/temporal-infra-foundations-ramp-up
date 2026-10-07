# Observability for Cells — Answering "Is This Cell Healthy?" Three Hundred Times

**Why this matters.** Every cell bring-up ends with a gate, and that gate is a question about telemetry: *is this cell healthy enough to take traffic?* Every incident begins with a different question about the same telemetry: *which cell is sick?* The eleven tool guides in this library teach you how to build a cell. This one teaches you how to know whether the thing you built works — and, much harder, how to know that for three hundred of them at once without drowning in dashboards, alerts, or bills. Observability is not a support function for cell infrastructure. It is the acceptance criterion for the product, it is the interface between your team and every other team that operates on top of your cells, and it is one of the largest line items in the infrastructure budget. Getting it wrong shows up in two ways: a cell that reports itself green and serves errors, and a pager that fires three hundred times for one problem.

**On sourcing.** Everything here is cited to primary documentation where primary documentation exists. Version and stability claims were verified against upstream release notes and specification status pages on **2026-08-29**. Where I generalize beyond what a vendor has published, I mark it as *industry pattern* or *rule of thumb*. Secondary sources (vendor blogs, comparison posts) are labeled as such.

---

## The mental model

Hold six ideas.

**1. The unit of observation is the cell, not the pod.** This is the single largest mental shift from ordinary application monitoring. In a normal service, you want to know which pod is slow. In a cell fleet you almost never do — the pod is disposable, the cell is the thing with tenants attached and the thing you can fail away from. Every metric your team emits, every recording rule, every dashboard, and every alert should have a `cell` label, and the *default* aggregation level should be per-cell. If a query has to name a pod to be useful, it belongs in a deep-dive dashboard, not the fleet view.

**2. Comparison is the primary analytical operation.** With one cluster, the question is "is this number bad?" and you answer it with a threshold you guessed. With three hundred, the question becomes "is this cell different from its two hundred and ninety-nine siblings?" and you answer it with the fleet's own distribution. Outlier detection across identical units is dramatically more sensitive than absolute thresholds, and it is only available to you because you built a fleet of identical things. Exploit it.

**3. Cardinality is the budget, and you will spend it accidentally.** A Prometheus time series is uniquely identified by its metric name and the full set of its label values ([data model](https://prometheus.io/docs/concepts/data_model/)). Series count multiplies: `metrics × label₁ × label₂ × …`. Every unbounded label — a workflow ID, a namespace name in a multi-tenant system, a full URL path, a customer identifier — turns a cheap metric into an unbounded one. Cardinality is simultaneously the dominant cost driver, the dominant cause of monitoring outages, and the thing that is easiest to add by accident in a one-line code change. Treat it like a resource with a quota.

**4. Metrics answer "what and where," traces answer "why," logs answer "exactly what happened to this one request" — and they cost roughly in that order.** Reaching for logs first at fleet scale is the classic failure. Three hundred cells producing structured logs at even a modest rate is a firehose that is expensive to store, slow to query, and impossible to aggregate meaningfully. Start with a per-cell metric, narrow to a cell, then to a service, then use a trace to find the slow hop, and only then read logs for the specific request you already identified.

**5. Alert on symptoms the customer feels, and let per-cell SLOs make fleet incidents legible.** The Google SRE Book's [monitoring chapter](https://sre.google/sre-book/monitoring-distributed-systems/) is blunt about this: pages should be for things that are "urgent, important, actionable, and *symptoms* rather than causes." In a cell architecture the symptom is per-cell — "cell `usw2-07` is burning error budget" — and the fleet aggregate over those per-cell symptoms is the only alert that scales.

**6. Observability of the cell must not depend on the cell.** If the only way to see that a cell is broken is to query something running inside it, then the failure mode that matters most — the cell is wedged — is exactly the one you cannot see. This is the "observability paradox" named in [guide 12](12-cell-lifecycle-synthesis.md): the tooling you would use to debug a failing bootstrap is installed *during* the bootstrap. The resolution is an external vantage point: remote-written metrics that keep flowing when the cell's query path is down, synthetic probes from outside the cell, and provisioning-workflow events emitted from the control plane rather than from inside the thing being provisioned.

---

## Core concepts

### The three signals, and why the framing is insufficient

"Metrics, logs, traces" is a taxonomy of *data shapes*, and it is genuinely useful for reasoning about instrumentation libraries and storage backends. It is not a design for a fleet observability system, because it says nothing about the two operations you actually perform:

- **Aggregate within a cell**, to produce a small number of cell-level facts from thousands of pod-level series.
- **Compare across cells**, to find the outlier.

Both of those are metrics operations, and both depend on a discipline the three-signals framing does not mention: **label hygiene**. A fleet where cell A labels its cell identity `cell="usw2-07"` and cell B labels it `cluster="usw2-07"` cannot be compared. A fleet where the Temporal service label is `service_name` in one place and `app` in another cannot be aggregated. The most valuable artifact your team can own is not a dashboard — it is a **label contract**: a small, versioned, enforced set of labels that every series in every cell carries.

A workable contract, and *industry pattern* rather than a Temporal-published one:

| Label | Meaning | Cardinality | Source |
|---|---|---|---|
| `cell` | Cell identity, globally unique | ~300 | Prometheus `externalLabels` |
| `cloud` | `aws` / `gcp` / `azure` | 3 | `externalLabels` |
| `region` | Provider region | ~30 | `externalLabels` |
| `cell_generation` | Which template version built this cell | ~5 in flight | `externalLabels` |
| `service` | Temporal service or infra component | ~20 | Instrumentation / relabeling |
| `namespace` | Kubernetes namespace | ~30 | Kubernetes SD |
| `pod` | Pod name | ~1000/cell | Kubernetes SD, dropped in most recording rules |

The three `externalLabels` entries are the important ones: they are applied by the Prometheus server at write/federation time, so they are correct by construction and cannot be forgotten by an application team. Everything below them is negotiable; those three are not.

The other thing the three-signals framing hides: **the signals must join**. A trace is only useful if you can get from a slow trace to the log lines emitted during it, and from a cell-level latency spike to an example trace. Those joins are `trace_id` in structured logs and exemplars on histograms. Design them in at the start; retrofitting them is miserable.

### Prometheus: the data model

A Prometheus sample is a `(metric name, label set) → (timestamp, value)` tuple. The metric-name-plus-label-set is the **series identity**, and the number of distinct identities is the number your infrastructure bills you for ([data model](https://prometheus.io/docs/concepts/data_model/)).

Four metric types ([metric types](https://prometheus.io/docs/concepts/metric_types/)):

| Type | Semantics | What you do with it | Aggregates across cells? |
|---|---|---|---|
| Counter | Monotonically increasing, resets to 0 on process restart | `rate()`, `increase()` | Yes, after `rate()` |
| Gauge | Arbitrary up/down value | Read directly, `avg`/`max`/`min` | Yes, but choose the operator deliberately |
| Histogram | Cumulative bucket counts (`_bucket{le=...}`) plus `_sum` and `_count` | `histogram_quantile()` over summed rates | **Yes** — this is the reason to use them |
| Summary | Client-side quantiles plus `_sum`/`_count` | Read the quantile directly | **No** — quantiles are not aggregatable |

That last row is the load-bearing one for a fleet and gets its own section below.

Prometheus 3 relaxed a long-standing constraint: metric and label names may now contain UTF-8, which matters mostly because it lets OpenTelemetry metric names survive the trip without dots being mangled into underscores ([UTF-8 in Prometheus](https://prometheus.io/docs/guides/utf8/), [Prometheus 3.0 announcement](https://prometheus.io/blog/2024/11/14/prometheus-3-0/)). It is a compatibility win and an operational trap: names can change on upgrade according to what endpoints expose, and previously-invalid names now silently succeed ([3.0 migration guide](https://prometheus.io/docs/prometheus/latest/migration/)).

As of 2026-08-29 the current release line is **v3.14.0** (2026-08-17), with **v3.13.0** (2026-07-01) designated LTS ([releases](https://github.com/prometheus/prometheus/releases)). For a fleet, run the LTS in cells and let the central query layer move faster; a cell's Prometheus is infrastructure, not a place to chase features.

### Cardinality is the dominant cost and failure mode

Cardinality failures do not look like cardinality failures. They look like: Prometheus OOMKilled, queries timing out, remote write falling behind, the ingestion endpoint returning 429, and — the worst one — *monitoring going down at the same moment as the thing it was supposed to be monitoring*, because the incident that spiked your error rate also spiked the label that carries the error message.

The arithmetic is unforgiving. A single `http_request_duration_seconds` histogram with 12 buckets, 8 handlers, 5 methods, and 6 status codes is `12 × 8 × 5 × 6 = 2,880` series *per instance*. Twenty pods per cell is 57,600. Three hundred cells is **17.3 million series** for one metric. Now someone adds a `namespace` label because it seemed useful, and a busy cell has 500 namespaces.

Prometheus's own storage is efficient per *sample* — "on average, Prometheus uses only around 1-2 bytes per sample" after compression ([storage docs](https://prometheus.io/docs/prometheus/latest/storage/)) — but the per-*series* overhead in the head block (index, labels, chunk metadata) is what consumes memory, and it is orders of magnitude larger. Do not memorize a bytes-per-series figure from a blog post; measure yours:

```promql
prometheus_tsdb_head_series                          # total active series: the number that matters
topk(20, count by (__name__)({__name__=~".+"}))      # biggest contributors by metric name
count(count by (namespace) (service_requests))       # which label is exploding, per suspect metric
sum(rate(prometheus_tsdb_head_series_created_total[10m]))  # churn: the signature of a UUID in a label
topk(10, scrape_samples_scraped)                     # targets producing more than you expect
```

Defences, in the order you should deploy them:

1. **A scrape-time sample limit.** `sample_limit` on the scrape config fails a scrape that exceeds N samples rather than accepting the bomb. This converts a slow-motion cluster-wide outage into a single failed target and an alert. Set it. ([scrape config](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config))
2. **`metric_relabel_configs` to drop known-bad series at ingest.** Cheaper than dropping them later, and it works even when you cannot change the application.
3. **A CI check on cardinality.** `promtool` plus a scrape of the candidate build in a test cell, diffed against the current build. A pull request that adds 40% more series should say so in its diff.
4. **A label allowlist policy.** In Kubernetes this can be a Kyverno rule ([guide 07](07-kyverno.md)) that rejects a `ServiceMonitor` without `metricRelabelings`, or a lint step on the monitoring repo.
5. **Per-tenant metrics are a design decision, not an instrumentation detail.** If you genuinely need per-namespace Temporal metrics for 10,000 namespaces, that is a data warehouse problem, not a Prometheus problem. Decide explicitly; do not let it happen by accident.

The [metric and label naming](https://prometheus.io/docs/practices/naming/) best-practices page is short and worth re-reading annually; the rule it states most clearly is that a label should never have unbounded values.

### Scrape architecture and Kubernetes service discovery

Prometheus pulls. That is a design choice with real consequences: the scraper is the source of truth for *what exists*, targets do not need to know where the monitoring system is, and a dead target produces `up == 0` — an affirmative signal of absence, which push-based systems do not give you for free.

In Kubernetes, `kubernetes_sd_config` queries the API server for `node`, `service`, `pod`, `endpoints`, `endpointslice`, and `ingress` objects, and exposes their metadata as `__meta_kubernetes_*` labels which you then reshape with `relabel_configs` ([Kubernetes SD](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config)). Two facts about this that bite people:

- **Relabeling happens in two places with different meanings.** `relabel_configs` runs *before* the scrape and decides which targets to scrape and what their target labels are. `metric_relabel_configs` runs *after* the scrape and decides which series to keep. Dropping an expensive metric belongs in the second; dropping an entire pod belongs in the first.
- **`endpointslice` discovery scales better than `endpoints`** on large clusters, because the API server does not have to ship a single enormous Endpoints object on every pod change. The Prometheus Operator supports it but does **not** default to it: set `serviceDiscoveryRole: EndpointSlice` on the `ServiceMonitor` (the default is `Endpoints`) *and* grant the Prometheus ServiceAccount get/list/watch on `endpointslices.discovery.k8s.io`. Omit that RBAC and discovery silently returns no targets ([Operator design](https://prometheus-operator.dev/docs/getting-started/design/)).

The other key Prometheus mode for a fleet is **agent mode**: a Prometheus that scrapes and remote-writes but does not keep a queryable local TSDB ([agent mode](https://prometheus.io/docs/prometheus/latest/prometheus_agent/)). It is dramatically cheaper per cell. It is also a trap if you have not thought it through, because a cell in agent mode has *no local query capability* — when the network path to the central store is broken, you are blind exactly when you need sight. See the fleet-scaling section.

### Recording rules, alerting rules, and staleness

**Recording rules** precompute expressions on the evaluation interval and store the result as a new series. They exist for two reasons: dashboard queries that would otherwise touch millions of series become one series lookup, and — critically for a fleet — they are the mechanism by which you turn per-pod detail into per-cell facts *before* the data leaves the cell.

The naming convention is `level:metric:operations`, and following it is not cosmetic — it is how a reader knows the aggregation level of a series at a glance ([recording rules best practices](https://prometheus.io/docs/practices/rules/)).

```yaml
groups:
  - name: cell-sli.rules
    interval: 30s
    rules:
      # Per-cell request rate, all Temporal frontend operations.
      - record: cell:temporal_frontend_requests:rate5m
        expr: sum(rate(service_requests{service_name="frontend"}[5m]))

      # Per-cell availability SLI: fraction of frontend requests that FAILED,
      # counting only server-fault error types (see gotcha 10 on why the
      # error_type filter is mandatory). Repeat this rule at 30m/1h/6h/3d —
      # the windows the burn-rate alerts below need.
      - record: cell:sli_frontend_errors:ratio_rate5m
        expr: |
          sum(rate(service_error_with_type{service_name="frontend",
                    error_type=~"Internal|Unavailable|DeadlineExceeded"}[5m]))
          / sum(rate(service_requests{service_name="frontend"}[5m]))
```

Note what is *not* in those rules: no `by (pod)`, no `by (namespace)`. The `cell` label arrives from `externalLabels` when the series is remote-written or federated. Each rule produces exactly one series per cell. Three hundred cells produce three hundred series. That is the whole trick.

**Alerting rules** are the same expression language plus `for`, `labels`, and `annotations` ([alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)). The `for` clause is a debounce: the condition must hold continuously for that duration before the alert fires, which is your main defence against a single bad scrape paging someone.

**Staleness** is the subtle one, and it is worth reading the [primary text](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness) rather than trusting your memory. Prometheus evaluates a query at a timestamp and, for each series, takes "the newest sample that is less than the lookback period ago. The lookback period is 5 minutes by default." Consequences:

- A metric that stops being scraped keeps returning its last value for up to five minutes. An alert on `some_gauge > 0` will not clear promptly if the exporter dies.
- When a target disappears, Prometheus writes an explicit **staleness marker**, and the series then returns nothing rather than a stale value — so `some_gauge > 0` also does not fire for a deleted pod. Good.
- But "returns nothing" is itself dangerous in alerting: an expression that evaluates to an empty vector produces *no alert*, which looks identical to "everything is fine." Every alert that depends on a series existing needs a companion `absent()` or `up == 0` alert. This is the single most common way a fleet goes silently blind.
- Exporters that set their own timestamps behave differently: their series keep the last value for five minutes before disappearing, controllable with `track_timestamps_staleness`.

### `rate`, `increase`, `histogram_quantile` — and how each is misused

**`rate(v[d])`** computes the per-second average rate of increase of a counter over the window, correcting for counter resets. Two properties cause almost all the confusion ([functions reference](https://prometheus.io/docs/prometheus/latest/querying/functions/#rate)):

- It needs at least two samples in the range. A window narrower than two scrape intervals returns nothing — which, per the staleness discussion, means your alert silently does not fire. *Rule of thumb, widely used:* make the range at least 4× the scrape interval so one missed scrape does not empty the window.
- It **extrapolates** to the boundaries of the range. This is why `rate()` on a slow counter can return a value implying a fractional event, and why `increase()` — which is `rate()` scaled by the range — can return `3.7` when only 3 things happened.

The classic misuses:

```promql
# WRONG: sum first, then rate. sum() erases the per-series identity that rate()
# needs to detect counter resets, so a single pod restart produces a huge
# negative step that rate() cannot correct for.
rate(sum(service_requests)[5m:])
sum(rate(service_requests[5m]))            # RIGHT: rate per series, then aggregate

# WRONG: increase() on a gauge. Gauges go down for legitimate reasons, and
# increase() interprets every decrease as a counter reset.
increase(kube_node_status_allocatable[1h])

# WRONG for exact counting: increase() extrapolates, so this does not give you
# "failed workflows today" as an integer. Prometheus is a sampling system; it
# does not do accounting. For exact counts, subtract the counter at two points.
increase(workflow_failed[24h])
```

**`histogram_quantile(φ, b)`** estimates a quantile from bucket counts. For a **classic** histogram, the input must be an instant vector of `_bucket` series carrying the `le` label, and you must `rate()` them first (they are counters) and preserve `le` in the aggregation:

```promql
# RIGHT (classic histogram): rate the buckets, aggregate keeping `le`. This is
# the exact shape Temporal's own docs use for frontend p95 latency.
histogram_quantile(0.95,
  sum by (le, operation) (rate(service_latency_bucket{service_name="frontend"}[5m])))

# WRONG: dropped `le`. histogram_quantile has nothing to interpolate over.
histogram_quantile(0.95, sum by (operation) (rate(service_latency_bucket[5m])))
# WRONG: forgot to rate. Raw cumulative buckets give the quantile over all time
# since process start, which is never what you want.
histogram_quantile(0.95, sum by (le) (service_latency_bucket))
# WRONG: quantile of an average. There is no such thing.
histogram_quantile(0.95, avg by (le) (rate(service_latency_bucket[5m])))
```

The first form above is what Temporal's [metrics reference](https://docs.temporal.io/references/cluster-metrics) publishes; the other three are the failure modes.

Two accuracy caveats from the [functions reference](https://prometheus.io/docs/prometheus/latest/querying/functions/#histogram_quantile) worth internalizing: the function **linearly interpolates within a bucket**, so your answer is only as precise as your bucket boundaries near the quantile of interest; and if the quantile falls in the `+Inf` bucket, it returns the upper bound of the second-highest bucket — meaning a histogram whose largest finite bucket is `2.5s` can never report a p99 above 2.5s, no matter how slow things get. A latency SLO of "p99 under 1s" measured on a histogram whose buckets top out at 1s is a metric that structurally cannot tell you that you are failing.

### Histograms: classic vs native, and aggregating latency across a fleet

This is the section that most directly earns its place in a *fleet* guide.

**Why quantile-of-average and average-of-quantile are both wrong.** Suppose cell A serves 1,000,000 requests/sec at p99 = 50 ms, and cell B serves 100 requests/sec at p99 = 5,000 ms. The fleet p99 is essentially 50 ms — cell B's traffic is a rounding error. But `avg(cell:p99)` returns 2,525 ms, and it will do so no matter how tiny cell B is. Averaging quantiles weights cells equally rather than weighting requests equally, and in a fleet with heterogeneous cell sizes that is not a small error; it is a wrong answer with the wrong sign. It also means a single idle canary cell can make your fleet dashboard look like an outage.

The same reasoning kills client-side **summaries** for fleet use. A summary's `{quantile="0.99"}` series is computed inside each process; there is no mathematically valid way to combine those numbers across processes, let alone across cells. Prometheus's [histograms and summaries](https://prometheus.io/docs/practices/histograms/) page says this directly, and it is the reason nearly all server-side instrumentation should use histograms.

**Why classic histograms aggregate correctly.** A classic histogram exposes *cumulative counts per bucket boundary*. Counts are additive. If every cell uses the *same bucket boundaries*, then summing `rate(x_bucket[5m])` across cells, grouped by `le`, produces the true fleet-wide bucket distribution, and `histogram_quantile` over that is the true request-weighted fleet quantile:

```promql
# Fleet-wide, request-weighted p99. Correct.
histogram_quantile(0.99, sum by (le) (rate(service_latency_bucket{service_name="frontend"}[5m])))

# Per-cell p99, for comparison and outlier detection. Also correct.
histogram_quantile(0.99, sum by (cell, le) (rate(service_latency_bucket{service_name="frontend"}[5m])))

# The fleet outlier query: which cells are more than 3x the fleet median p99?
(histogram_quantile(0.99, sum by (cell, le) (rate(service_latency_bucket[5m]))))
> on() group_left
(3 * quantile(0.5, histogram_quantile(0.99, sum by (cell, le) (rate(service_latency_bucket[5m])))))
```

The hard requirement hiding in "if every cell uses the same bucket boundaries" is a **fleet-wide bucket contract**. The moment one cell runs a Temporal build with different histogram boundaries — a version skew during a rolling upgrade, say — cross-cell aggregation silently produces garbage, because summing by `le` merges series that were never comparable. This is a genuine, easy-to-miss failure mode during exactly the operation your team performs constantly: a staged fleet upgrade.

**Native (sparse) histograms** fix both problems at once. Instead of N pre-chosen bucket series, a native histogram is a *single* series carrying a full histogram with exponentially-spaced buckets generated at a chosen resolution ([native histograms spec](https://prometheus.io/docs/specs/native_histograms/)). The consequences are large:

- **Cardinality collapses.** One series per label set instead of one per `le` value. For a 12-bucket histogram that is a 12× reduction, and native histograms typically offer far better resolution than 12 buckets would.
- **Bucket layouts reconcile automatically.** PromQL documents that native histograms with different layouts "are generally convertible to compatible versions to apply binary and aggregation operations," with reconciliation performed across all histogram samples in an aggregation, and a warn-level annotation when layouts genuinely cannot be reconciled ([querying basics](https://prometheus.io/docs/prometheus/latest/querying/basics/)). That directly removes the version-skew hazard above.
- **The query gets simpler**, because there is no `le` to preserve:

```promql
# Native histogram: no `le`, no `_bucket` suffix.
histogram_quantile(0.99, sum by (cell) (rate(service_latency[5m])))
```

Status as of 2026-08-29, verified: **native histograms became a stable feature in Prometheus v3.8.0** (2025-11-28), and scraping them must be turned on explicitly via the `scrape_native_histograms` setting; the old `--enable-feature=native-histograms` flag became a complete no-op in v3.9.0, with `scrape_native_histograms` defaulting to `false` ([v3.8.0 release notes](https://github.com/prometheus/prometheus/releases/tag/v3.8.0), [v3.9.0 release notes](https://github.com/prometheus/prometheus/releases/tag/v3.9.0)). Native histogram work continued through the v3.13.0 LTS ([v3.13.0](https://github.com/prometheus/prometheus/releases/tag/v3.13.0)), and v3.14.0 shipped a fix for native histogram data becoming incorrect after a restart ([v3.14.0](https://github.com/prometheus/prometheus/releases/tag/v3.14.0)) — a reminder that "stable" means "API-stable," not "bug-free."

Practical guidance for a Temporal Cloud cell fleet: native histograms are the correct destination and materially reduce both cost and a real correctness hazard, but the migration is not free. Your downstream stack must support them end to end — remote write (Remote-Write 2.0 carries native histograms natively; [RW 2.0 spec](https://prometheus.io/docs/specs/prw/remote_write_spec_2_0/)), your long-term store, your Grafana version, and every dashboard and recording rule that currently references `_bucket` and `le`. Plan it as a fleet-wide migration with a dual-emit period, not a flag flip.

### Scaling metrics for a fleet

Four architectures, in increasing order of centralization.

| Architecture | How it works | Survives cell isolation? | Global queries? | Operational load |
|---|---|---|---|---|
| **Per-cell Prometheus only** | Each cell runs a full Prometheus; you query each one | Yes — fully self-contained | No. You cannot ask "how many cells are unhealthy" | Low per cell, unbearable at 300 |
| **Per-cell Prometheus + remote write to central** | Full local TSDB *and* a copy shipped out | Yes, and it backfills on reconnect | Yes, centrally | Medium. Two storage paths to run |
| **Per-cell agent-mode Prometheus + central store** | Scrape locally, ship everything, no local TSDB | **No.** A partitioned cell is invisible and unqueryable | Yes | Lowest per cell |
| **Central scraping across cells** | One Prometheus reaches into many clusters | No, and it needs cross-cell network reachability | Yes | Violates cell independence |

The last row deserves an explicit rejection. Central scraping requires a network path from a shared component into every cell, which is precisely the cross-cell dependency the [AWS cell-based architecture guidance](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-design.html) tells you to avoid — and it makes your monitoring system a shared failure domain spanning the entire fleet. Do not do it.

The defensible answer for a cell fleet is row two — **local Prometheus for cell-local truth, remote write for the fleet view** — with local retention deliberately short (hours to a couple of days) because the central store owns history. This gives you the property that matters: an operator connected to an isolated cell can still run PromQL against it, *and* the fleet dashboard has every cell.

**Remote write** ships samples over HTTP as batched, compressed protobuf. Its behavior under stress is the thing to understand: it uses in-memory shards with a WAL-backed queue, and it *auto-scales the shard count* based on how far behind it is. When the receiver is slow, your cell's Prometheus grows memory. The [remote write tuning](https://prometheus.io/docs/practices/remote_write/) guide is the primary text; the metrics that matter are:

```promql
# Seconds behind: the remote write SLI.
prometheus_remote_storage_highest_timestamp_in_seconds
  - ignoring(remote_name, url) prometheus_remote_storage_queue_highest_sent_timestamp_seconds
# Sharding up because we cannot keep up? And are samples being dropped outright?
prometheus_remote_storage_shards / prometheus_remote_storage_shards_max
rate(prometheus_remote_storage_samples_dropped_total[5m])
```

**Remote-Write 2.0** is the version you want going forward: it adds native support for metadata, exemplars, created timestamps, and native histograms, and uses string interning to shrink payloads and CPU cost ([RW 2.0 spec](https://prometheus.io/docs/specs/prw/remote_write_spec_2_0/)). One behavioral note from the same spec family: RW 2.0 accepts all UTF-8 names, with no way to enforce legacy character-set validation — so name hygiene has to be enforced upstream.

**The central store: Thanos vs Mimir vs Cortex vs managed.**

| Option | Model | Multi-cloud fit | Notes |
|---|---|---|---|
| **[Thanos](https://thanos.io/)** | Sidecar/receive + object storage + a global Querier that fans out over Prometheus instances, store gateways, and object storage | Good. Object storage exists on all three clouds; Querier can span them | Extends an existing Prometheus fleet with the least disruption. CNCF Incubating |
| **[Grafana Mimir](https://grafana.com/docs/mimir/latest/)** | Metrics warehouse; Prometheus/Alloy/OTel Collector remote-write into it | Good, but it is a large distributed system you now operate | Mimir 3.0 (Nov 2025) decoupled read/write paths with Kafka and made the streaming Mimir Query Engine the default ([Grafana release blog](https://grafana.com/blog/grafana-mimir-3-0-release-all-the-latest-updates/) — vendor source) |
| **[Cortex](https://cortexmetrics.io/)** | The ancestor of Mimir | — | *Secondary sources report the project is effectively in maintenance mode with maintainers having moved to Mimir* ([Oodle blog](https://blog.oodle.ai/scaling-prometheus-from-single-node-to-enterprise-grade-observability/) — secondary, unverified against a Cortex project statement). Do not start here in 2026 |
| **[Amazon Managed Service for Prometheus](https://docs.aws.amazon.com/prometheus/latest/userguide/what-is-Amazon-Managed-Service-Prometheus.html)** | Managed workspace, remote-write in, PromQL out | AWS cells only | Auto-scales active series; max **1.5 billion active series per workspace**, minimum capacity 2 million, with ingestion throttling surfacing as `DiscardedSamples` in CloudWatch ([AMP quotas](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP_quotas.html)) |
| **[Google Cloud Managed Service for Prometheus](https://cloud.google.com/stackdriver/docs/managed-prometheus)** | Managed collection via `PodMonitoring` CRDs, or self-deployed collection | GCP cells only | Shares Cloud Monitoring quotas; default ingest quota is 500 QPS × up to 200 samples per call, i.e. **~100,000 samples/second per project** ([Cloud Monitoring quotas](https://cloud.google.com/monitoring/quotas)) |
| **[Azure Monitor managed service for Prometheus](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/prometheus-metrics-overview)** | AKS add-on writes to an Azure Monitor workspace | Azure cells only | Default **1 million active time series** per workspace, raisable to 20 million by API (auto-approved to 2 million), above that by support ticket ([scaling best practices](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/azure-monitor-workspace-scaling-best-practice)) |

**The multi-cloud angle is the decision.** Three managed backends means three query languages' worth of quirks, three quota models, three cost models, three sets of dashboards, and — fatally — *no single place to ask "how many of my 300 cells are unhealthy right now."* The three managed offerings are all excellent at monitoring workloads that live on one cloud. They are structurally bad at being the fleet view for a fleet that spans three.

The pattern that resolves this, and it is *industry pattern* rather than a Temporal-published design: use the managed service per cloud as the **cheap, high-retention, per-cell archive** — because it is the cheapest place to put per-cloud data and it needs no operational effort — and run **one self-hosted global layer** (Thanos Querier or Mimir) that holds only the *aggregated, per-cell recording-rule output* from every cell on every cloud. The volume difference is what makes this work: the raw data is millions of series per cell; the recording-rule output is a few hundred series per cell, or a few tens of thousands for the whole fleet. A fleet view built on aggregates is small enough to be centralized without becoming a correlated failure domain worth worrying about.

### The Prometheus Operator, and how a cell gets monitoring at bring-up

The [Prometheus Operator](https://prometheus-operator.dev/) turns monitoring configuration into Kubernetes objects, which is exactly what you want when monitoring must be bootstrapped as a *layer* in a cell's dependency DAG rather than configured by hand.

The custom resources you will use ([API reference](https://prometheus-operator.dev/docs/api-reference/api/), [design docs](https://prometheus-operator.dev/docs/getting-started/design/)):

| CRD | What it declares | When you reach for it |
|---|---|---|
| `Prometheus` / `PrometheusAgent` | A Prometheus server (or agent), its storage, retention, `externalLabels`, remote write, and its selectors | Once per cell, from the cell template |
| `ServiceMonitor` | Scrape a group of Services, via their Endpoints (or EndpointSlices with `serviceDiscoveryRole: EndpointSlice`) | The default for anything with a Service |
| `PodMonitor` | Scrape a set of Pods directly | Headless workloads, DaemonSets, sidecars with no Service |
| `Probe` | Blackbox-probe ingresses or static targets via a prober | External synthetic checks against the cell's real endpoint |
| `ScrapeConfig` | A raw scrape config for targets outside the cluster or shapes the higher-level CRDs cannot express | Cloud-managed dependencies: the RDS/Cloud SQL exporter, the load balancer |
| `PrometheusRule` | Recording and alerting rules, reconciled and hot-loaded without a Prometheus restart | Every rule you own |
| `Alertmanager` / `AlertmanagerConfig` | Alertmanager instances and routing | Once per cell, or point cells at a shared Alertmanager cluster |

The mechanism that ties them together is **selectors**: the `Prometheus` object's `serviceMonitorSelector`, `podMonitorSelector`, `probeSelector`, `scrapeConfigSelector`, and `ruleSelector` decide which of those objects get compiled into the running configuration. This is the seam that makes cell bootstrapping work — the cell template creates a `Prometheus` with a selector like `monitoring: cell-baseline`, and every component installed later that carries that label is picked up automatically, in whatever order it happens to arrive.

That last property is worth dwelling on, because it resolves an ordering problem. Guide 12 puts observability at layer L8, "before L9," so that "you need to be able to *see* the database bootstrap fail." But observability cannot literally be first — it needs a CNI, DNS, storage, and certificates. The Operator's selector model breaks the tension: install the Operator and a `Prometheus` object as early as the cluster can host a pod, and let every later layer ship its own `ServiceMonitor` and `PrometheusRule` alongside its Deployment. Monitoring converges as the cell converges, rather than being a discrete step that either happened or did not.

A minimal, realistic cell-template shape:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata: { name: cell, namespace: monitoring }
spec:
  replicas: 2
  retention: 24h                    # short: the central store owns history
  # The label contract. Applied by the server, so it cannot be forgotten.
  externalLabels:
    cell: usw2-07
    cloud: aws
    region: us-west-2
    cell_generation: "2026.08.3"
  serviceMonitorSelector: { matchLabels: { monitoring: cell-baseline } }
  podMonitorSelector:     { matchLabels: { monitoring: cell-baseline } }
  ruleSelector:           { matchLabels: { monitoring: cell-baseline } }
  enforcedSampleLimit: 50000        # cardinality guardrail, fleet-wide
  enforcedLabelLimit: 40
  remoteWrite:
    - url: https://metrics.internal.example/api/v1/write
      writeRelabelConfigs:
        # Ship ONLY aggregated recording-rule output to the global layer. Raw
        # series stay local (and go to the per-cloud managed archive).
        - { sourceLabels: [__name__], regex: "(cell|fleet):.*", action: keep }
      queueConfig: { capacity: 10000, maxShards: 50 }
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: temporal-frontend
  namespace: temporal
  labels: { monitoring: cell-baseline }   # matches the Prometheus selector
spec:
  selector:
    matchLabels: { app.kubernetes.io/name: temporal, app.kubernetes.io/component: frontend }
  endpoints:
    - port: metrics
      interval: 30s
      scrapeTimeout: 10s
      metricRelabelings:
        # Drop the per-namespace breakdown we cannot afford at fleet scale.
        # `labeldrop` matches its regex against LABEL NAMES, not values.
        - { regex: "namespace", action: labeldrop }
```

**[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)** is the Helm chart that packages the Operator, Prometheus, Alertmanager, Grafana, `node-exporter`, `kube-state-metrics`, and a large default rule set from the [kube-prometheus](https://github.com/prometheus-operator/kube-prometheus) project. For a lab it is one command. For a cell fleet, treat its defaults as a *starting point you fork*: the bundled rules are written for a generic cluster with a self-managed control plane, and several of them (etcd, scheduler, controller-manager alerts) will fire perpetually on EKS/GKE/AKS where those components are not yours to scrape ([guide 04](04-managed-kubernetes-eks-gke-aks.md)). Rendering the chart's manifests and committing them, per the approach in [guide 09](09-helm.md), makes that fork reviewable.

### OpenTelemetry: data model, Collector, semantic conventions, OTLP

OpenTelemetry is a specification plus SDKs plus a Collector, and as of **2026-05-21 it is a CNCF Graduated project** ([CNCF announcement](https://www.cncf.io/announcements/2026/05/21/cloud-native-computing-foundation-announces-opentelemetrys-graduation-solidifying-status-as-the-de-facto-observability-standard/), [OTel blog](https://opentelemetry.io/blog/2026/otel-graduates/)). Graduation is a governance and maturity signal, not a statement that every component is stable — the per-signal picture still matters a great deal.

**Signal stability, verified 2026-08-29 against the [Specification Status Summary](https://opentelemetry.io/docs/specs/status/).** The specification page is the authority and it explicitly recommends checking each client's own repository README, because clients are developed independently. Its component lifecycle is Draft → Experimental → Stable → Deprecated → Removed, and it notes that the Collector's status for a signal matches the Protocol's status for that signal.

| Signal | Where it stands | What that means for you |
|---|---|---|
| **Tracing** | The spec page states the tracing specification "is now completely stable, and covered by long term support," still extensible only in a backward-compatible way; clients go to v1.0 once tracing is complete | Safe to build on |
| **Metrics** | Data model stable and released as part of OTLP; the spec page still describes OTel Metrics as "under active development," with metric pipelines in the Collector marked experimental and Collector support for Prometheus "under development, in collaboration with the Prometheus community" | Usable, but this is exactly why Prometheus scraping remains the safer choice for infrastructure metrics |
| **Baggage** | Described as "completely stable." Not an observability signal — it attaches key/values to a transaction for downstream services | Useful for propagating a `cell` or tenant identifier; no OTLP or Collector component |
| **Logs** | Logs data model released as part of OTLP; the **Log Bridge API** is explicitly "not meant to be called directly by end users," and log appenders bridging existing frameworks are "under development in many languages" | Bridge from `log/slog`, do not rewrite application logging on top of the OTel API |
| **Profiles** | Entered **public alpha in March 2026** with an OTLP profiles data model ([OTel blog](https://opentelemetry.io/blog/2026/profiles-alpha/)) | Interesting, not something to build alerting on |

Current **semantic conventions** version is **1.44.0** ([semconv docs](https://opentelemetry.io/docs/specs/semconv/)); Kubernetes attributes were promoted to release candidate during 2026 ([OTel blog](https://opentelemetry.io/blog/2026/k8s-semconv-rc/)). Semantic conventions are the part most people skip and should not: they are what makes telemetry from a Go control-plane service, a Rust sidecar, and a third-party operator queryable with the same attribute names. Adopt them, and *pin the version you adopted*, because attribute renames between semconv versions will break dashboards.

**The Collector** is a single binary with a pipeline of receivers → processors → exporters, plus connectors and extensions ([Collector docs](https://opentelemetry.io/docs/collector/), [components](https://opentelemetry.io/docs/collector/components/)). It uses dual versioning: stable modules on a 1.x line alongside beta modules on a 0.x line (v1.49.0/v0.143.0 in January 2026, with the 0.x line at roughly v0.159.0 by August 2026 — [releases](https://github.com/open-telemetry/opentelemetry-collector-releases/releases)). Individual component stability is documented in each component's own README, and the Collector's overall status is "mixed" for exactly that reason.

Two deployment patterns, both documented upstream:

| Pattern | Shape | Good at | Weak at |
|---|---|---|---|
| **[Agent](https://opentelemetry.io/docs/collector/deploy/agent/)** | DaemonSet or sidecar, one per node/pod | Host and pod metadata enrichment (`k8sattributes`), no network hop for the app, resilient to gateway outage | No cross-pod view — cannot do tail sampling, cannot batch efficiently across the cell |
| **[Gateway](https://opentelemetry.io/docs/collector/deploy/gateway/)** | Standalone Deployment, typically per cell | Central egress control, one set of credentials, tail sampling, aggregation, quota enforcement | A per-cell single point of failure if not replicated; needs its own scaling story |

For a cell, the [agent-to-gateway](https://opentelemetry.io/docs/collector/deploy/other/agent-to-gateway/) combination is the right default: a DaemonSet agent that enriches with Kubernetes metadata and forwards over the node-local network, feeding a small replicated per-cell gateway that owns sampling, redaction, and the single outbound connection. That shape also means exactly one component in the cell holds the credentials for the outside world, which matters for the reasons in [guide 10](10-vault.md).

**OTLP** is the wire protocol — gRPC or HTTP, protobuf or JSON — and it is the thing that actually delivers on vendor neutrality ([OTLP spec](https://opentelemetry.io/docs/specs/otlp/)). Since it is gRPC by default, everything in [guide 02](02-grpc.md) about deadlines, keepalives, and load balancing applies to your telemetry path too; a Collector gateway behind an L4 load balancer will pin every agent's long-lived stream to one replica, and you will discover that during a traffic spike.

### Where OTel replaces Prometheus, and where it does not

The honest answer in 2026 is that they occupy overlapping but distinct roles, and pretending otherwise causes migrations that stall halfway.

| Job | Use | Why |
|---|---|---|
| Infrastructure metrics (kubelet, node, kube-state-metrics, etcd, CoreDNS, Karpenter, cert-manager) | **Prometheus scraping** | These all expose Prometheus endpoints. Pull gives you `up` as an explicit liveness signal. Nothing about OTel improves this |
| Temporal server metrics | **Prometheus scraping** | Temporal emits an OpenMetrics-compatible endpoint and Temporal's own docs, dashboards, and query examples are Prometheus-shaped ([metrics reference](https://docs.temporal.io/references/cluster-metrics)) |
| Traces | **OpenTelemetry** | Prometheus has no trace story. This is not a competition |
| Logs | **OTel Collector as a transport**, optionally | The Collector has strong log-processing components (from the Stanza donation, per the spec status page). Whether you route logs through it or through Fluent Bit/Vector is a preference, not a correctness question |
| Application metrics in your own Go control-plane services | **Either** | OTel gives you one SDK for all three signals and exemplars for free. Prometheus client_golang gives you the simplest possible thing. Pick one per codebase and be consistent |
| Cross-cloud normalization | **OTel Collector** | Its processors are the natural place to rename cloud-specific attributes into your label contract before anything downstream sees them |

Two bridges are worth knowing about because they let the two ecosystems coexist without a big-bang migration:

- The Collector's `prometheusreceiver` scrapes Prometheus endpoints into OTLP, and its `prometheusexporter`/`prometheusremotewriteexporter` go the other way.
- **Prometheus can receive OTLP directly**, on `/api/v1/otlp/v1/metrics` ([Prometheus OTLP guide](https://prometheus.io/docs/guides/opentelemetry/)). The critical default: the OTLP receiver's `translation_strategy` defaults to `UnderscoreEscapingWithSuffixes`, i.e. classic Prometheus normalization, even though Prometheus 3 supports UTF-8 in storage and the UI. If you expect `http.server.request.duration` to arrive with dots intact, you must change that setting — and changing it changes your metric names, which changes every query.

My recommendation for a cell fleet, stated as opinion: **keep Prometheus as the metrics substrate for cells, adopt OTel for traces immediately, and treat the OTel Collector as the fleet's normalization and egress layer for everything.** That gets you vendor neutrality at the boundary without betting the health gate on a metrics pipeline whose specification still describes itself as under active development.

### Tracing: sampling, propagation, correlation, cost

**Head vs tail sampling** is the central trade.

| | Head sampling | Tail sampling |
|---|---|---|
| Decision made | At the root span, before anything is known | After the whole trace is assembled |
| Cost | Cheapest. Unsampled traces are never created | Expensive. Every span must be transmitted and buffered until the decision |
| Can it keep all the slow/failed traces? | No — it is blind to the outcome | Yes. That is the entire point |
| Where it runs | In the SDK, via `TraceIdRatioBased` or `ParentBased` samplers | In the Collector, via the `tailsamplingprocessor` |
| Constraint | Must propagate the decision, or you get partial traces | **All spans of a trace must reach the same Collector instance** |

That last constraint is the operationally significant one. The [`tailsamplingprocessor` README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md) — stability **beta** for traces — states plainly that all spans for a given trace must be received by the same collector instance, and that the processor must be placed after any context-dependent processor such as `k8sattributes`, because it reassembles spans into new batches and they lose their original context. In practice this means a two-tier gateway: a first tier that load-balances by trace ID (the `loadbalancingexporter` with `routing_key: traceID`), and a second tier that does the tail sampling. That is real infrastructure with real cost, and it is why many teams run head sampling at a low rate plus an always-sample rule for errors.

A pragmatic policy for a cell fleet, *industry pattern*:

- Head-sample at a low base rate (1%) for ordinary traffic.
- Always sample traces that touch a cell during a bring-up or upgrade, keyed off a `cell_generation` attribute — the window where you most need detail is the window where volume is lowest.
- Tail-sample to keep 100% of traces with an error status or latency above the SLO threshold.
- Sample control-plane workflow traces at 100%. There are few of them and each one is a cell's life story.

**Context propagation through gRPC.** The [W3C Trace Context](https://www.w3.org/TR/trace-context/) recommendation defines `traceparent` and `tracestate` headers; over gRPC these travel as request metadata, which is the same mechanism [guide 02](02-grpc.md) describes for auth and deadlines. In Go the standard implementation is `otelgrpc` from [opentelemetry-go-contrib](https://github.com/open-telemetry/opentelemetry-go-contrib), installed as a stats handler on both dial and serve. The rule that catches people, and it is the same rule guide 02 states for deadlines: **derive the outgoing context from the incoming one.** A handler that calls `context.Background()` for a downstream call breaks the trace exactly as it breaks deadline propagation, and the symptom — a trace that mysteriously ends at a service boundary — looks like an instrumentation bug rather than the context bug it is.

**Trace-to-log correlation** is one line of discipline: every log record emitted inside a span carries `trace_id` and `span_id`. With Go's `log/slog` this is a handler that pulls the span context out of `ctx`. Without it, traces and logs are two separate products you happen to pay for twice.

**Exemplars** are the metrics-to-traces join: a histogram bucket can carry a sample trace ID, so a spike on a latency graph becomes a click-through to a trace of a slow request. Remote-Write 2.0 carries exemplars natively ([RW 2.0 spec](https://prometheus.io/docs/specs/prw/remote_write_spec_2_0/)), which removes the main reason teams used to skip them.

**The honest cost story.** Tracing at full fidelity produces more data than metrics by one to two orders of magnitude, and unlike metrics that data does not compress into a fixed number of series — it grows linearly with traffic forever. A 1% head sample of a busy cell is still a large data volume, and tail sampling *does not reduce the volume you transmit*, only the volume you store; the network and Collector CPU cost is paid on 100% of spans. Budget accordingly, and be suspicious of any tracing plan that does not state a sampling rate.

### Logs: structured, collected, stored, and used last

**Structured logging** means a log line is a set of typed key/value pairs, not a sentence. Go's standard library has provided this since Go 1.21 in [`log/slog`](https://pkg.go.dev/log/slog), which matters here because every component in [guide 01](01-golang.md)'s world is Go and `slog` removes the last excuse for `fmt.Sprintf` logging.

```go
// A cell-aware slog handler: every record carries the label contract plus
// trace correlation, so logs join to both metrics and traces.
func NewCellHandler(w io.Writer, cell, cloud string) slog.Handler {
    base := slog.NewJSONHandler(w, &slog.HandlerOptions{
        Level: slog.LevelInfo, AddSource: true,
    }).WithAttrs([]slog.Attr{
        slog.String("cell", cell), slog.String("cloud", cloud),
    })
    return &cellHandler{Handler: base}
}

type cellHandler struct{ slog.Handler }

// Handle pulls trace context off ctx so every line inside a span is joinable.
func (h *cellHandler) Handle(ctx context.Context, r slog.Record) error {
    if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
        r.AddAttrs(slog.String("trace_id", sc.TraceID().String()),
            slog.String("span_id", sc.SpanID().String()))
    }
    return h.Handler.Handle(ctx, r)
}
```

Two `slog` details worth knowing: the context-aware `Handle` above only receives a context if callers use the `slog.InfoContext`-style methods (the non-context variants pass `context.Background()`), and `AddSource: true` costs a stack walk per record — measure before enabling it on a hot path.

**Collection** in Kubernetes is a DaemonSet tailing container log files.

| Agent | Language | Strengths | Notes |
|---|---|---|---|
| **[Fluent Bit](https://docs.fluentbit.io/)** | C | Lowest footprint, ubiquitous, first-class Kubernetes metadata filter | The default choice for a resource-constrained DaemonSet. CNCF project |
| **[Vector](https://vector.dev/docs/)** | Rust | Strongest transformation language (VRL), excellent throughput, good back-pressure semantics | Heavier than Fluent Bit; the right pick when you need real routing logic |
| **[Grafana Alloy](https://grafana.com/docs/alloy/latest/)** | Go | One agent for logs, metrics, and OTLP; a Collector distribution underneath | **Promtail reached end of life in March 2026** and Alloy is its replacement ([migration guide](https://grafana.com/docs/alloy/latest/set-up/migrate/from-promtail/)). Do not start a new deployment on Promtail |
| **[OTel Collector](https://opentelemetry.io/docs/collector/)** | Go | One pipeline for all signals; `filelog` receiver plus the Stanza-derived operators | Sensible if you already run the Collector as your egress layer |

**Storage.**

| Option | Model | Cost shape | Query |
|---|---|---|---|
| **[Grafana Loki](https://grafana.com/docs/loki/latest/)** | Indexes *labels only*, stores compressed log content in object storage | Cheapest per GB by a wide margin; you pay at query time instead | LogQL. Grep-like: fast on label selectors, brute-force on content |
| **Elasticsearch / OpenSearch** | Full inverted index | Expensive to store, fast to search arbitrarily | Rich, but the index is often larger than the logs |
| **CloudWatch Logs / Cloud Logging / Azure Monitor Logs** | Managed, per-cloud | Zero operational effort; ingest-priced and it adds up fast | Per-cloud query language. Three of them, for a three-cloud fleet |

Loki's design has a sharp edge that is the log-world analogue of Prometheus cardinality: **every distinct label combination creates a stream**, and high-cardinality labels (pod name, request ID, trace ID) will destroy a Loki cluster just as surely as they destroy a Prometheus. Put `cell`, `cloud`, `namespace`, and `service` in labels; put everything else in the log line and search it.

**Why logs are the worst signal to reach for first at fleet scale.** Three hundred cells is three hundred independent log streams with no shared timeline and no aggregate. "Search all cells for this error" is a full scan across the fleet's entire retention window — minutes to answer, if it answers at all, and expensive every time someone tries. Meanwhile the equivalent metric query returns three hundred numbers in fifty milliseconds. Logs are for the last hop: you already know the cell, the service, and ideally the trace ID, and you want the exact detail. Reaching for logs first is a habit imported from single-service operations, and it is the most common way an incident's first thirty minutes get wasted.

**Retention** is where logs quietly become the biggest bill. A workable *industry pattern* tiering: 7 days hot and queryable, 30 days in object storage queryable with a delay, one year in cold storage for compliance only, and aggressive sampling of high-volume low-value lines (access logs for health checks, in particular — a cell with a 1-second readiness probe on 500 pods emits 43 million health-check log lines a day that nobody will ever read).

### What to actually monitor in a cell

This is the concrete list. Metric names below are drawn from the referenced upstream documentation; where a project renames metrics between versions — Karpenter is the notorious one — check the version you run.

#### Kubernetes health

From [kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) and the kubelet:

```promql
# Node readiness. One not-Ready node is worth knowing; several is a cell event.
count by (cell) (kube_node_status_condition{condition="Ready",status="true"} == 0)

# Pod restarts. A crashlooping pod is not the story; a crashlooping
# *component across many cells* is a bad release.
sum by (cell, namespace) (increase(kube_pod_container_status_restarts_total[15m])) > 3

# Scheduling failures. Distinguish "no capacity" (a Karpenter problem, guide 06)
# from "no valid node" (a taint/affinity/policy problem).
sum by (cell) (kube_pod_status_phase{phase="Pending"}) > 0
sum by (cell) (rate(scheduler_schedule_attempts_total{result="unschedulable"}[5m]))

# PVC pressure. Temporal's persistence layer and Elasticsearch both care.
min by (cell, persistentvolumeclaim) (
  kubelet_volume_stats_available_bytes / kubelet_volume_stats_capacity_bytes) < 0.15
```

For the API server, Kubernetes distinguishes the raw request duration from the **SLI** duration, which excludes time the request spent in webhook calls and priority-and-fairness queues — the difference between "the API server is slow" and "something you installed is making the API server slow" ([Kubernetes API server SLIs](https://kubernetes.io/docs/reference/instrumentation/slis/)):

```promql
# apiserver p99 by verb, excluding WATCH/CONNECT (long-lived by design; they
# will otherwise dominate the graph).
histogram_quantile(0.99, sum by (cell, le, verb) (
  rate(apiserver_request_sli_duration_seconds_bucket{verb!~"WATCH|CONNECT"}[5m])))
# apiserver 5xx ratio, and priority-and-fairness rejections (load shedding).
sum by (cell) (rate(apiserver_request_total{code=~"5.."}[5m]))
  / sum by (cell) (rate(apiserver_request_total[5m]))
sum by (cell) (rate(apiserver_flowcontrol_rejected_requests_total[5m]))
```

For **etcd** ([etcd metrics docs](https://etcd.io/docs/latest/metrics/)), the four that matter are leader presence, leader churn, WAL fsync latency, and database size:

```promql
etcd_server_has_leader == 0
increase(etcd_server_leader_changes_seen_total[1h]) > 3
histogram_quantile(0.99, rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])) > 0.05
etcd_mvcc_db_total_size_in_bytes / etcd_server_quota_backend_bytes > 0.8
```

**The multi-cloud caveat, and it is a big one:** on EKS, GKE, and AKS the control plane is managed, and what you can scrape varies by provider ([guide 04](04-managed-kubernetes-eks-gke-aks.md)). Some `apiserver_*` and a subset of `etcd_*` series are available through the API server's own `/metrics` endpoint with the right RBAC; scheduler and controller-manager metrics generally are not. Any alert rule inherited from kube-prometheus that assumes a self-managed control plane will either fire forever or evaluate to an empty vector forever, and per the staleness discussion the second failure is the dangerous one.

#### The cell's own dependencies

| Dependency | What to watch | Why it matters | Guide |
|---|---|---|---|
| **CNI / datapath** | Agent readiness, packet drops, conntrack table utilization (`node_nf_conntrack_entries / node_nf_conntrack_entries_limit`), IPAM address exhaustion | A full conntrack table produces intermittent, inexplicable connection failures that look like an application bug | [05](05-cni-and-host-networking.md) |
| **DNS** | CoreDNS `SERVFAIL`/`NXDOMAIN` rate (`coredns_dns_responses_total{rcode="SERVFAIL"}`), request latency, `coredns_forward_healthcheck_failures_total` | DNS failure presents as *everything* being intermittently broken. It is worth its own top-level panel | [05](05-cni-and-host-networking.md) |
| **Certificate expiry** | `certmanager_certificate_expiration_timestamp_seconds - time()`, `certmanager_certificate_ready_status` | The one class of outage with a known start time and no undo. Alert at 30 days, page at 7 | [11](11-cert-manager-and-pki.md) |
| **Vault** | `vault_core_unsealed`, request latency, token/lease counts | A sealed Vault means the cell cannot bootstrap secrets. This must be visible from *outside* the cell | [10](10-vault.md) |
| **Karpenter** | Node provisioning latency, nodeclaim launch failures, drift/disruption counters | Slow provisioning is invisible in pod-level metrics; you only see pods Pending. Metric names change between Karpenter minors — check the [metrics reference](https://karpenter.sh/docs/reference/metrics/) for your version | [06](06-karpenter.md) |
| **Admission webhooks** | `apiserver_admission_webhook_admission_duration_seconds`, `apiserver_admission_webhook_rejection_count` | A slow or failing webhook with `failurePolicy: Fail` takes the whole API server's write path with it. This is a top-three cause of "the cell will not converge" | [07](07-kyverno.md) |

The cert-expiry query, written as the alert you actually want, is `(min by (cell) (certmanager_certificate_expiration_timestamp_seconds) - time()) / 86400` — days until the soonest-expiring certificate in each cell, which is a single number per cell and therefore a fleet-wide panel.

#### Temporal server signals

Temporal publishes a metrics reference and a community dashboards repository; the metric names below are from that reference ([OSS Temporal Service metrics reference](https://docs.temporal.io/references/cluster-metrics), [temporalio/dashboards](https://github.com/temporalio/dashboards)). The complete definitive list lives in [`metric_defs.go`](https://github.com/temporalio/temporal/blob/main/common/metrics/metric_defs.go), and the docs point you there for anything not covered.

**Common, across services.** All gRPC service requests are emitted with `type`, `operation`, and `namespace` tags, plus `service_name` for the service role:

```promql
sum by (operation) (rate(service_requests{service_name="frontend"}[2m]))   # request rate
histogram_quantile(0.95, sum by (operation, le) (                          # p95 latency
  rate(service_latency_bucket{service_name="frontend"}[5m])))
sum by (error_type) (                                                      # errors by type (v1.17.0+)
  rate(service_error_with_type{service_name="frontend"}[5m]))
sum(rate(client_errors{service_name="frontend",service_role="history"}[5m]))  # inter-service health
```

**Persistence.** Temporal emits metrics for every database read and write, tagged by `operation`; the docs describe these as the way to identify issues caused by the database:

```promql
sum by (operation) (rate(persistence_requests{service_name="history"}[1m]))
sum by (error_type) (rate(persistence_error_with_type{service_name="history"}[1m]))
histogram_quantile(0.95, sum by (operation, le) (
  rate(persistence_latency_bucket{service_name="history"}[1m])))
```

Persistence latency is the metric most likely to be the real cause when everything else looks slow, because every service path goes through it. It is also the one that changes when you change cell storage — a different instance class, a different provider's managed database — which makes it a per-cloud comparison you should have on a dashboard.

**Task queues and backlog.** From the Matching service:

```promql
sum(rate(poll_success[5m]))     # tasks matched to a poller
sum(rate(poll_timeouts[5m]))    # polls that found nothing

# Backlog: time from task creation to delivery. Temporal's docs put it directly:
# "the larger this latency, the longer Tasks are sitting in the queue waiting
# for your Workers to pick them up."
histogram_quantile(0.95, sum by (operation, le) (
  rate(asyncmatch_latency_bucket{service_name="matching"}[5m])))

# A task added to a queue with no poller — usually a worker or starter on the
# wrong Task Queue.
sum(rate(no_poller_tasks[5m]))
```

**History task processing.** The docs are explicit that keeping the History Task processing system healthy is critical:

```promql
sum(rate(task_requests{operation=~"TransferActive.*"}[1m]))
sum(rate(task_errors{operation=~"TransferActive.*"}[1m]))

# Attempts per task execution. Tasks retry forever, so a rising p95 attempt
# count is the early warning that something downstream is failing.
histogram_quantile(0.95, sum by (operation, le) (
  rate(task_attempt_bucket{operation=~"TransferActive.*"}[1m])))

# Schedule-to-start latencies (v1.18.0+): persistence queue, then in-memory queue.
histogram_quantile(0.95, sum by (operation, le) (rate(task_latency_load_bucket[1m])))
histogram_quantile(0.95, sum by (operation, le) (rate(task_latency_schedule_bucket[1m])))

# Latency introduced by workflow logic itself (per-workflow lock contention).
# Important because it is NOT your problem to fix, and you need data to say so.
histogram_quantile(0.95, sum by (operation, le) (rate(service_latency_userlatency_bucket[5m])))
```

**Shard health and ownership.** [Guide 12](12-cell-lifecycle-synthesis.md) makes the operational point: shard ownership moves when a History pod goes away, and "a rollout that replaces History pods faster than shards can be reclaimed produces a latency spike that looks like a database problem," so you should "roll History slowly, and watch shard-ownership metrics rather than pod readiness." Temporal's [performance bottlenecks troubleshooting guide](https://docs.temporal.io/troubleshooting/performance-bottlenecks) discusses shard lock latency as a key indicator, with good performance expected under 5 ms and ideally around 1 ms. The exact shard-related metric names are version-dependent; resolve them against `metric_defs.go` for the server version your cells run rather than copying names from a blog post. The rule to encode is: **an upgrade gate for the History service must assert that shard ownership has re-converged, not merely that pods are Ready.** See [guide 16](16-temporal-server-internals.md) for the internals that make this true.

### SLOs, error budgets, and burn-rate alerting

An **SLI** is a measurement, an **SLO** is a target on that measurement over a window, and an **error budget** is `1 - SLO` — the amount of failure you have explicitly decided to tolerate. The [SRE Workbook's implementation chapter](https://sre.google/workbook/implementing-slos/) is the reference text.

For a cell fleet there are two levels and they answer different questions.

| Level | Example | Answers | Who acts |
|---|---|---|---|
| **Per-cell SLO** | 99.9% of frontend requests in cell `usw2-07` succeed, over 30 days | "Is this cell's tenants' experience acceptable?" | The on-call for the cell |
| **Fleet SLO** | 99.5% of cells are within their own SLO at any moment | "Is the *fleet* healthy?" | The team, at a planning level |

The fleet SLO is what makes a fleet-wide incident legible. If three cells are burning budget, that is normal operations. If ninety are, that is one incident with one cause — a bad release, a shared dependency, a provider event — and it should page once, not ninety times. Expressing it as a count over per-cell SLO state is what turns a wall of alerts into a single number:

```promql
count(cell:sli_frontend_errors:ratio_rate1h > 0.001)          # cells burning faster than 1x
count(cell:sli_frontend_errors:ratio_rate1h > 0.001)          # as a fraction: the fleet SLI
  / count(cell:sli_frontend_errors:ratio_rate1h)
```

**Burn-rate alerting** is the technique from [Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/) in the SRE Workbook. The insight is that a fixed error-rate threshold is either too sensitive (pages on a blip) or too slow (a small, sustained elevation exhausts a 30-day budget without ever crossing the threshold). Burn rate normalizes: a burn rate of 1 exhausts the budget exactly at the end of the window; a burn rate of 14.4 exhausts 2% of a 30-day budget in one hour.

**Multi-window, multi-burn-rate** adds a short window as a confirmation to each long window, so an alert fires only if the problem is both severe over the long window *and* still happening right now — which is what makes them reset quickly after the incident ends. The Workbook's canonical table:

| Long window | Short window | Burn rate | Budget consumed | Action |
|---|---|---|---|---|
| 1 hour | 5 minutes | 14.4 | 2% | Page |
| 6 hours | 30 minutes | 6 | 5% | Page |
| 3 days | 6 hours | 1 | 10% | Ticket |

Written against the recording rules from earlier, for a 99.9% SLO (budget = 0.001):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: cell-slo-burn, labels: { monitoring: cell-baseline } }
spec:
  groups:
    - name: cell-slo-burn.rules
      rules:
        - alert: CellErrorBudgetBurnFast
          expr: |
            (   cell:sli_frontend_errors:ratio_rate1h  > (14.4 * 0.001)
            and cell:sli_frontend_errors:ratio_rate5m  > (14.4 * 0.001) )
            or
            (   cell:sli_frontend_errors:ratio_rate6h  > (6 * 0.001)
            and cell:sli_frontend_errors:ratio_rate30m > (6 * 0.001) )
          labels: { severity: page, slo: frontend_availability }
          annotations:
            summary: "Cell {{ $labels.cell }} is burning error budget fast"
            runbook_url: "https://runbooks.internal/cell-error-budget-burn"

        - alert: CellErrorBudgetBurnSlow
          expr: |
            cell:sli_frontend_errors:ratio_rate3d > (1 * 0.001)
            and cell:sli_frontend_errors:ratio_rate6h > (1 * 0.001)
          labels: { severity: ticket, slo: frontend_availability }

        # The fleet-level alert. Fires ONCE for a fleet-wide event.
        - alert: FleetWideErrorBudgetBurn
          expr: count(cell:sli_frontend_errors:ratio_rate1h > (14.4 * 0.001)) > 10
          for: 5m
          labels: { severity: page, scope: fleet }
          annotations:
            summary: "{{ $value }} cells burning error budget — suspect a common cause"
```

Note that the fast alert has no `for:` clause. That is deliberate and it is the Workbook's guidance: the short window *is* the debounce, and adding `for:` on top of a burn-rate alert delays detection without improving precision.

### Alerting that does not page you at 3am for nothing

**Symptom, not cause.** The SRE Book's monitoring chapter and Rob Ewaschuk's [philosophy on alerting](https://docs.google.com/document/d/199PqyG3UsyXlwieHaqbGiWVa8eMWi8zzAn0YfcApr8Q/preview) — the document that chapter grew from — both argue that pages should correspond to a symptom a user experiences. "Cell error rate above SLO" is a symptom. "Node memory above 90%" is a cause, and it is only worth paging for if it reliably predicts a symptom, which on a well-configured cluster it does not. Prometheus's own [alerting best practices](https://prometheus.io/docs/practices/alerting/) reaches the same conclusion and adds a useful sharpening: page on symptoms at the *highest* level you can, and put causes in the alert's annotations, dashboard links, and runbook — not in additional pages.

**The 300-cells problem.** A rule that is correct for one cell fires three hundred times for a fleet-wide event. Four mechanisms, deployed together:

1. **Evaluate fleet rules centrally, on recording-rule output.** The `FleetWideErrorBudgetBurn` alert above cannot be written in a per-cell Prometheus, because a per-cell Prometheus does not know about other cells. It belongs in the global query layer. This is the strongest reason to have a global layer at all.
2. **Group in Alertmanager by cause, not by cell.** `group_by: [alertname, severity, slo]` — deliberately *omitting* `cell` — collapses three hundred instances of one alertname into a single notification whose body lists the affected cells. Getting this wrong (`group_by: [...]` including `cell`, or the `group_by: [...]` catch-all) is how teams end up with a pager storm despite having done everything else right.
3. **Inhibition.** An Alertmanager `inhibit_rule` where a firing `scope: fleet` alert suppresses the per-cell alerts of the same `slo`. The fleet alert already contains the information; the three hundred children add nothing.
4. **Route by severity, and be honest about the tiers.** Per-cell fast burn pages. Per-cell slow burn creates a ticket. Cause-level alerts (node pressure, cert expiry at 30 days, Karpenter drift) go to a dashboard or a daily digest and never page. If more than a handful of alert *names* can page, the taxonomy is wrong.

```yaml
# Alertmanager: the grouping that makes 300 cells survivable.
route:
  group_by: [alertname, severity, slo]     # NOT cell
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: default
  routes:
    - matchers: [ 'scope = fleet' ]
      receiver: pager
      group_wait: 0s
    - matchers: [ 'severity = page' ]
      receiver: pager
    - matchers: [ 'severity = ticket' ]
      receiver: ticketing

inhibit_rules:
  # A fleet-wide alert silences its per-cell children for the same SLO.
  - source_matchers: [ 'scope = fleet' ]
    target_matchers: [ 'severity = page' ]
    equal: [ slo ]
```

**The alerts nobody writes and everybody needs.** Meta-monitoring, because a monitoring system that fails silently is worse than none:

```promql
# "Cell went dark." Must be evaluated CENTRALLY — a cell cannot report its own silence.
absent_over_time(cell:temporal_frontend_requests:rate5m[10m])
# A target that was there and is now gone. Needed because, per the staleness
# section, a plain threshold alert on a missing series never fires.
up == 0
# Prometheus failing to evaluate rules — i.e. the alerts are not running.
rate(prometheus_rule_evaluation_failures_total[5m]) > 0
# The sample_limit guardrail tripping: a cardinality bomb was just contained.
increase(prometheus_target_scrapes_exceeded_sample_limit_total[10m]) > 0
```

### The cell health gate

[Guide 12](12-cell-lifecycle-synthesis.md) states the gate's principle: "'Cell is ready' means an external client completed a real RPC, not that pods are `Running`," and its first production gotcha is that "a cell whose readiness gate checks pod status will be marked ready before it can serve." Its L12 gate runs `temporal operator cluster health` and asserts an external client can complete a workflow.

That end-to-end assertion is necessary and not sufficient. A single successful RPC proves the happy path works *once*; it does not prove the cell can sustain traffic, that its dependencies are healthy, or that it will still be healthy in ten minutes. The gate below adds the telemetry half. Every line is a PromQL expression evaluated against the cell's own Prometheus, and every line must be **true for a sustained soak window** — 15 minutes is a reasonable default — under synthetic load, not evaluated once.

```promql
# ---- Gate A: the telemetry pipeline itself works ----
# A1. Every expected target up. Empty (not zero) here means SD is broken
#     and every gate below is meaningless.
count(up{job=~"temporal-.*|kubelet|kube-state-metrics|coredns"} == 0) == 0
# A2. Remote write is current, so the fleet can see this cell.
(prometheus_remote_storage_highest_timestamp_in_seconds
   - ignoring(remote_name, url) prometheus_remote_storage_queue_highest_sent_timestamp_seconds) < 60
# A3. Nothing is tripping the cardinality guardrail.
increase(prometheus_target_scrapes_exceeded_sample_limit_total[15m]) == 0

# ---- Gate B: Kubernetes substrate ----
# B1. All nodes Ready; nothing stuck Pending.
count(kube_node_status_condition{condition="Ready",status="true"} == 0) == 0
sum(kube_pod_status_phase{phase="Pending"}) == 0
# B2. apiserver p99 (SLI variant) and 5xx ratio within budget.
histogram_quantile(0.99, sum by (le) (
  rate(apiserver_request_sli_duration_seconds_bucket{verb!~"WATCH|CONNECT"}[5m]))) < 1
sum(rate(apiserver_request_total{code=~"5.."}[5m]))
  / sum(rate(apiserver_request_total[5m])) < 0.001
# B3. No admission webhook slow enough to threaten the write path.
histogram_quantile(0.99, sum by (le) (
  rate(apiserver_admission_webhook_admission_duration_seconds_bucket[5m]))) < 1

# ---- Gate C: cell dependencies ----
# C1. DNS clean.  C2. Conntrack has headroom.  C3. Certs ready and not
# expiring soon.  C4. Vault unsealed.
sum(rate(coredns_dns_responses_total{rcode="SERVFAIL"}[5m]))
  / sum(rate(coredns_dns_responses_total[5m])) < 0.001
max(node_nf_conntrack_entries / node_nf_conntrack_entries_limit) < 0.7
count(certmanager_certificate_ready_status{condition="True"} == 0) == 0
(min(certmanager_certificate_expiration_timestamp_seconds) - time()) > 30 * 86400
min(vault_core_unsealed) == 1

# ---- Gate D: Temporal is actually serving ----
# D1. Traffic is flowing (synthetic load is on).
sum(rate(service_requests{service_name="frontend"}[5m])) > 0
# D2. Error ratio under the SLO, counting only server faults.
sum(rate(service_error_with_type{service_name="frontend",
        error_type=~"Internal|Unavailable|DeadlineExceeded"}[5m]))
  / sum(rate(service_requests{service_name="frontend"}[5m])) < 0.001
# D3. Frontend p95, and D4. persistence p95 + zero persistence errors.
histogram_quantile(0.95, sum by (le) (
  rate(service_latency_bucket{service_name="frontend"}[5m]))) < 0.5
histogram_quantile(0.95, sum by (le) (
  rate(persistence_latency_bucket{service_name="history"}[5m]))) < 0.1
sum(rate(persistence_errors{service_name="history"}[5m])) == 0
# D5. Task attempts not climbing.  D6. Task queues draining, pollers present.
histogram_quantile(0.95, sum by (le) (rate(task_attempt_bucket[5m]))) < 2
histogram_quantile(0.95, sum by (le) (
  rate(asyncmatch_latency_bucket{service_name="matching"}[5m]))) < 1
sum(rate(no_poller_tasks[5m])) == 0

# ---- Gate E: comparison to the fleet (evaluated CENTRALLY) ----
# E1. p95 within 2x the fleet median. Catches the cell that passes every
#     absolute threshold and is still visibly the sick one.
(histogram_quantile(0.95, sum by (cell, le) (
   rate(service_latency_bucket{cell="usw2-07"}[5m]))))
< on() group_left
(2 * quantile(0.5, histogram_quantile(0.95, sum by (cell, le) (
   rate(service_latency_bucket[5m])))))
```

Three properties of this gate are what make it worth the effort:

- **It is the same set of expressions as the steady-state alerts**, with tighter thresholds. Anything you gate on, you can alert on; anything you alert on, you should have gated on. A single list, two consumers.
- **It is checkable by a machine** and therefore usable as an activity in the cell provisioning workflow, which is the form [guide 12](12-cell-lifecycle-synthesis.md) requires: idempotent, retryable, individually-timed.
- **Gate E cannot be evaluated inside the cell**, which is a useful forcing function: it makes the fleet-wide query layer a prerequisite for the health gate, rather than a nice-to-have someone builds later.

### Multi-cloud reality

| | EKS | GKE | AKS |
|---|---|---|---|
| **Managed Prometheus** | [Amazon Managed Service for Prometheus](https://docs.aws.amazon.com/prometheus/latest/userguide/what-is-Amazon-Managed-Service-Prometheus.html), remote-write in | [Google Cloud Managed Service for Prometheus](https://cloud.google.com/stackdriver/docs/managed-prometheus) with in-cluster managed collection | [Azure Monitor managed service for Prometheus](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/prometheus-metrics-overview) via the AKS add-on |
| **Scrape config CRD** | Standard `ServiceMonitor` (you install the Operator) | `PodMonitoring` / `ClusterPodMonitoring` (GMP's own CRDs) | `ServiceMonitor`/`PodMonitor` under the `azmonitoring.coreos.com` API group |
| **Control-plane metrics** | Subset of `apiserver_*`/`etcd_*` from the API server `/metrics` with RBAC; no scheduler/controller-manager | System and (opt-in) control-plane metrics into Cloud Monitoring | Check current availability; historically the most limited of the three |
| **Node/system metrics free?** | No — you run `node-exporter` | Partly, via GKE system metrics | Partly, via the add-on's default targets |
| **Headline quota** | 1.5B active series/workspace, min 2M ([quotas](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP_quotas.html)) | ~100k samples/sec per project by default ([quotas](https://cloud.google.com/monitoring/quotas)) | 1M active series/workspace default, 20M by API ([scaling](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/azure-monitor-workspace-scaling-best-practice)) |
| **You must run yourself** | Operator, `kube-state-metrics`, `node-exporter`, all rules, all dashboards | Rules and dashboards; collection can be managed | Rules and dashboards; collection via the add-on |

**Keeping dashboards portable across three clouds.** Three principles, in order of importance:

1. **Normalize at collection time, never at query time.** The place to turn `cloud.region` (GCP) and `topology.kubernetes.io/region` (everywhere) into your single `region` label is a relabeling rule or an OTel Collector `transform` processor inside the cell. A dashboard containing a three-branch `or` for three clouds is a dashboard nobody will maintain.
2. **Never let a cloud-specific metric name into a fleet dashboard.** If a metric only exists on one cloud, either derive an equivalent for the others or accept that it belongs on a cloud-specific drill-down panel, not the fleet view.
3. **One Grafana, one dashboard JSON, one `$cell` variable.** The datasource is a variable too. If a dashboard has to be cloned per cloud, the normalization in step 1 is incomplete, and you should fix that rather than clone.

There is a structural point underneath all three: **the layer that makes multi-cloud tractable is the one you own.** The managed services are per-cloud by construction, so anything you want to be uniform across clouds — labels, aggregation, rules, dashboards, the health gate — has to live in components running inside your cells and in your own global layer. Budget for that; it is not overhead, it is the product.

### Cost

Observability spend rivaling compute spend is a real and common outcome, not a cautionary exaggeration. The four levers, in order of effect:

**1. Cardinality budget.** This dominates everything. Give each team a series budget per cell, make it visible on a dashboard, and enforce it with `sample_limit` so that exceeding it is a scrape failure and not a surprise invoice. The single most effective intervention available here is **migrating to native histograms**, which collapses N `le` series into one per label set — for a fleet where latency histograms are typically the largest single metric family, that is a step change rather than a trim.

**2. Ship aggregates, not raw series, across the WAN.** The `writeRelabelConfigs` `keep` filter in the `Prometheus` spec earlier is a two-line change with an enormous effect: the global layer receives hundreds of series per cell rather than millions. Raw data stays local and in the cheap per-cloud archive.

**3. Retention tiers.** A workable *industry pattern* shape: hot and fast is 15 days of full-resolution metrics, 7 days of sampled traces, and 7 days of logs; warm in object storage is 90 days of metrics downsampled to 5-minute resolution and 30 days of logs; cold, for compliance only, is 13 months of metrics at 1-hour resolution and 12 months of logs. Traces do not tier — a trace older than a week is almost never read, so the honest choice is to keep fewer of them rather than keep them longer.

**4. Sampling, for traces and for logs.** Traces: a base head-sample rate with tail sampling for errors and slow requests. Logs: drop health-check access logs at the collection agent, and be aggressive about it — they are the highest-volume, lowest-value data in the system, and a cell with hundreds of pods on a 1-second probe interval generates tens of millions of them per day.

Two habits that keep this honest. First, **put observability cost per cell on the same dashboard as compute cost per cell**; a ratio that drifts is visible long before an invoice arrives. Second, **treat a cardinality increase as a change that needs review**, the same way a change to a cell's instance type would be. Both are decisions about how much the fleet costs, and only one of them currently feels like one.

---

## Hands-on

A local lab on `kind` that exercises the whole chain: install the stack, scrape something, write a recording rule and a burn-rate alert, blow up cardinality on purpose and watch it hurt, then add an OTel Collector and produce a trace.

### Prerequisites

- Docker (or Colima/Podman with a Docker-compatible socket), with **at least 8 GB of memory and 4 CPUs** available to the VM. The cardinality step deliberately consumes memory; with less, the node will evict pods before you see the effect you are looking for.
- `kind` v0.30 or later, `kubectl` matching your cluster minor, and `helm` v4 (v3 also works for this lab).
- About 45 minutes, and roughly 4 GB of container image pulls.
- Internet access for Helm repos and images.

Version note: image tags below were current at the time of writing. If a tag has moved, check the referenced repository rather than guessing — a `ImagePullBackOff` here wastes more time than the lookup.

### Step 1 — A three-node cluster

```bash
cat > /tmp/kind-obs.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF

kind create cluster --name obs --config /tmp/kind-obs.yaml
kubectl cluster-info --context kind-obs
kubectl get nodes -o wide
```

### Step 2 — Install kube-prometheus-stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

cat > /tmp/kps-values.yaml <<'EOF'
# Pretend this kind cluster is a cell. These labels are the label contract.
prometheus:
  prometheusSpec:
    externalLabels: { cell: lab-01, cloud: kind, region: local }
    retention: 6h
    enforcedSampleLimit: 20000       # guardrail, deliberately low so Step 5 trips it
    serviceMonitorSelectorNilUsesHelmValues: false   # pick up any labelled SM/rule
    ruleSelectorNilUsesHelmValues: false
    resources:
      requests: { memory: 512Mi, cpu: 200m }
      limits:   { memory: 2Gi }
# kind has no real control plane to scrape; silence the perpetual alerts.
kubeEtcd:              { enabled: false }
kubeScheduler:         { enabled: false }
kubeControllerManager: { enabled: false }
kubeProxy:             { enabled: false }
grafana: { adminPassword: admin }
EOF

helm install kps prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values /tmp/kps-values.yaml --wait --timeout 10m

kubectl -n monitoring get pods
kubectl -n monitoring get prometheus,alertmanager,servicemonitor
```

Disabling `kubeEtcd`/`kubeScheduler`/`kubeControllerManager` is not a lab shortcut — it is the same fork you will make for EKS/GKE/AKS, where those components are managed and unscrapable. Doing it here makes the reason concrete.

Port-forward Prometheus in a second terminal and leave it running:

```bash
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
# http://localhost:9090
```

Confirm the label contract took effect — run this in the Prometheus UI:

```promql
# Every series should carry cell/cloud/region once it leaves this server.
# Locally, check the configured external labels instead:
prometheus_config_last_reload_successful
```

Then check `Status → Configuration` and confirm `external_labels` contains `cell: lab-01`.

### Step 3 — Scrape a sample app

`avalanche` is a synthetic metrics generator maintained by the Prometheus community, which makes it both the sample app and — in Step 5 — the cardinality bomb ([prometheus-community/avalanche](https://github.com/prometheus-community/avalanche)).

```bash
kubectl create namespace lab

cat > /tmp/avalanche.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: { name: avalanche, namespace: lab }
spec:
  replicas: 1
  selector: { matchLabels: { app: avalanche } }
  template:
    metadata: { labels: { app: avalanche } }
    spec:
      containers:
        - name: avalanche
          image: quay.io/prometheuscommunity/avalanche:main
          args: [--metric-count=20, --label-count=3, --series-count=10,
                 --value-interval=15, --port=9001]
          ports: [{ name: metrics, containerPort: 9001 }]
          resources:
            requests: { memory: 64Mi, cpu: 50m }
            limits:   { memory: 512Mi }
---
apiVersion: v1
kind: Service
metadata: { name: avalanche, namespace: lab, labels: { app: avalanche } }
spec:
  selector: { app: avalanche }
  ports: [{ name: metrics, port: 9001, targetPort: metrics }]
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata: { name: avalanche, namespace: lab, labels: { release: kps } }
spec:
  selector: { matchLabels: { app: avalanche } }
  endpoints: [{ port: metrics, interval: 15s }]
EOF

kubectl apply -f /tmp/avalanche.yaml
kubectl -n lab rollout status deploy/avalanche
```

In the Prometheus UI, confirm discovery under `Status → Targets`, then:

```promql
up{job="avalanche"}
count({__name__=~"avalanche.*"})
prometheus_tsdb_head_series
```

Write down that `prometheus_tsdb_head_series` value. You will want the before/after.

### Step 4 — A recording rule and a burn-rate alert

`avalanche` emits gauges, so synthesize an SLI from the scrape itself — the point is to exercise the rule machinery, not to produce a meaningful number.

```bash
cat > /tmp/lab-rules.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: lab-cell-slo, namespace: lab, labels: { release: kps } }
spec:
  groups:
    - name: lab-cell-sli.rules
      interval: 15s
      rules:
        # SLI: fraction of scrapes of the lab job that FAILED.
        - record: cell:sli_scrape_errors:ratio_rate5m
          expr: (count(up{job="avalanche"} == 0) or vector(0)) / count(up{job="avalanche"})
        - record: cell:sli_scrape_errors:ratio_rate30m
          expr: (count(up{job="avalanche"} == 0) or vector(0)) / count(up{job="avalanche"})
    - name: lab-cell-burn.rules
      rules:
        - alert: LabCellErrorBudgetBurnFast
          expr: |
            cell:sli_scrape_errors:ratio_rate30m > (14.4 * 0.001)
            and cell:sli_scrape_errors:ratio_rate5m > (14.4 * 0.001)
          labels: { severity: page, slo: lab_scrape_availability }
          annotations:
            summary: "Cell {{ $labels.cell }} burning error budget fast"
EOF

kubectl apply -f /tmp/lab-rules.yaml
```

The `or vector(0)` is the fix for the staleness trap from earlier: without it, zero failing targets produces an *empty vector*, the ratio is empty, and the alert can never fire — indistinguishable from healthy.

Watch the recording rule appear (`Status → Rules`), then make the alert fire. Note *how* you break it: scaling the Deployment to zero would delete the Endpoints and the target would vanish from discovery entirely, so `up{job="avalanche"}` returns an **empty vector**, both the numerator and denominator go empty, and the SLI just goes stale — the alert still cannot fire. You have to keep the target discovered and make the scrape *fail*:

```bash
# Point the scrape at a path that 404s. The target stays discovered; up goes to 0.
kubectl -n lab patch servicemonitor avalanche --type merge \
  -p '{"spec":{"endpoints":[{"port":"metrics","interval":"15s","path":"/nope"}]}}'

# Wait ~2 minutes, then check Alerts in the Prometheus UI.

kubectl -n lab patch servicemonitor avalanche --type merge \
  -p '{"spec":{"endpoints":[{"port":"metrics","interval":"15s","path":"/metrics"}]}}'
```

Note how quickly it clears once the short window recovers. That is the multi-window design working. And note that the failure you *could not* use — the vanished target — is exactly the staleness trap: absence of data is not the same signal as a bad value, and only one of the two makes your SLI expression evaluate.

### Step 5 — Blow up cardinality on purpose

Baseline first:

```promql
prometheus_tsdb_head_series
topk(10, count by (__name__)({__name__=~".+"}))
process_resident_memory_bytes{job="kps-kube-prometheus-stack-prometheus"}
```

Now detonate:

`kubectl set` has subcommands for `env`, `image`, `resources`, `selector`, `serviceaccount`, and `subject` — there is no `set args`. Replace the container's args with a JSON patch:

```bash
kubectl -n lab patch deploy/avalanche --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/args",
   "value":["--metric-count=200","--label-count=10","--series-count=500",
            "--value-interval=15","--port=9001"]}]'

kubectl -n lab rollout status deploy/avalanche
```

That is 200 metrics × 500 series each = 100,000 series from one pod, against a 20,000 `enforcedSampleLimit`. Watch, over the next few minutes:

```promql
# The guardrail firing. This is the good outcome.
increase(prometheus_target_scrapes_exceeded_sample_limit_total[10m])

# The target is now DOWN — one failed target instead of a dead Prometheus.
up{job="avalanche"}

# Head series did NOT explode, because the sample limit rejected the scrape.
prometheus_tsdb_head_series

# Memory held.
process_resident_memory_bytes{job="kps-kube-prometheus-stack-prometheus"}
```

Now see what happens **without** the guardrail — this is the part that teaches the lesson:

```bash
kubectl -n monitoring patch prometheus kps-kube-prometheus-stack-prometheus \
  --type merge -p '{"spec":{"enforcedSampleLimit":null}}'
# The Operator rewrites the config and Prometheus hot-reloads.
```

Watch `prometheus_tsdb_head_series` climb into six figures, `process_resident_memory_bytes` follow it, and query latency degrade. Try a wide query and feel it:

```promql
count by (__name__)({__name__=~".+"})
```

Then diagnose it the way you would in production, and fix it without touching the application:

```bash
# Identify the culprits in the UI with topk(10, count by (__name__)({__name__=~".+"})),
# then drop them at ingest with metricRelabelings — no application change needed.
kubectl -n lab patch servicemonitor avalanche --type merge -p '{
  "spec": {"endpoints": [{"port": "metrics", "interval": "15s",
    "metricRelabelings": [{"sourceLabels": ["__name__"],
      "regex": "avalanche_metric_mmmmm_[0-9]+.*", "action": "drop"}]}]}}'
```

Watch head series fall back as the old series go stale and get compacted out. Then restore the guardrail and the small workload:

```bash
kubectl -n monitoring patch prometheus kps-kube-prometheus-stack-prometheus \
  --type merge -p '{"spec":{"enforcedSampleLimit":20000}}'
kubectl -n lab patch deploy/avalanche --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/args",
   "value":["--metric-count=20","--label-count=3","--series-count=10",
            "--value-interval=15","--port=9001"]}]'
```

The takeaway to internalize: with the limit, one target went down and everything else kept working. Without it, the monitoring system degraded globally — during the exact window when someone would be relying on it.

### Step 6 — An OTel Collector and a trace

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

cat > /tmp/otel-values.yaml <<'EOF'
mode: deployment          # gateway pattern; one replica is enough for a lab
replicaCount: 1
image: { repository: otel/opentelemetry-collector-contrib }
config:
  receivers:
    otlp:
      protocols:
        grpc: { endpoint: 0.0.0.0:4317 }
        http: { endpoint: 0.0.0.0:4318 }
  processors:
    batch: {}
    resource:               # stamp the label contract onto every span
      attributes:
        - { key: cell,  value: lab-01, action: upsert }
        - { key: cloud, value: kind,   action: upsert }
  connectors:
    spanmetrics:            # derive RED metrics from spans: the traces/metrics bridge
      histogram: { explicit: { buckets: [5ms, 10ms, 50ms, 100ms, 500ms, 1s, 5s] } }
  exporters:
    debug: { verbosity: detailed }
    prometheus: { endpoint: 0.0.0.0:8889 }
  service:
    pipelines:
      traces:
        receivers: [otlp]
        processors: [resource, batch]
        exporters: [debug, spanmetrics]
      metrics/spanmetrics: { receivers: [spanmetrics], exporters: [prometheus] }
ports:
  prom-exporter: { enabled: true, containerPort: 8889, servicePort: 8889, protocol: TCP }
EOF

helm install otel open-telemetry/opentelemetry-collector \
  --namespace monitoring --values /tmp/otel-values.yaml --wait
```

Scrape the Collector's derived span metrics with Prometheus:

```bash
cat > /tmp/otel-sm.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata: { name: otel-spanmetrics, namespace: monitoring, labels: { release: kps } }
spec:
  selector: { matchLabels: { app.kubernetes.io/name: opentelemetry-collector } }
  endpoints: [{ port: prom-exporter, interval: 15s }]
EOF
kubectl apply -f /tmp/otel-sm.yaml
```

Generate traces with `telemetrygen`, the load generator from collector-contrib:

```bash
kubectl -n monitoring run telemetrygen --rm -it --restart=Never \
  --image=ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:latest -- \
  traces --otlp-insecure \
  --otlp-endpoint otel-opentelemetry-collector.monitoring.svc.cluster.local:4317 \
  --traces 200 --service lab-frontend
```

Confirm the spans arrived:

```bash
kubectl -n monitoring logs deploy/otel-opentelemetry-collector | grep -A5 "lab-frontend"
```

Then query the span-derived metrics in Prometheus, which is the payoff — trace data has become a fleet-aggregatable metric carrying your `cell` label:

```promql
# RED metrics derived from spans.
sum by (service_name, span_name) (rate(traces_span_metrics_calls_total[5m]))

# p95 span duration, aggregated correctly by preserving `le`.
histogram_quantile(0.95, sum by (le, service_name) (
  rate(traces_span_metrics_duration_milliseconds_bucket[5m])))
```

Metric names emitted by the `spanmetrics` connector have changed across Collector versions; if the queries return nothing, list what actually arrived with `{__name__=~"traces.*"}` or curl the exporter endpoint directly.

### Step 7 — Clean up, and what to take away

Tear the cluster down with `kind delete cluster --name obs`. Four things should have stuck:

- The `ServiceMonitor` → `Prometheus` selector relationship is how a cell bootstraps monitoring incrementally, in whatever order components arrive.
- `or vector(0)` versus an empty vector is the difference between an alert that works and one that is silently dead.
- `sample_limit` converts a fleet-wide monitoring outage into one down target and one alert. It is the single highest-leverage line in a Prometheus config.
- The `spanmetrics` connector is the practical bridge between traces and metrics: RED metrics per service without separate instrumentation, and the output aggregates across cells because it is a histogram.

---

## Production gotchas

**1. An alert on a missing series never fires.** Prometheus returns the newest sample within the lookback period — 5 minutes by default, settable with `--query.lookback-delta` — and after a staleness marker the series returns *nothing at all* ([staleness](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness)). An expression evaluating to an empty vector produces no alert, which is indistinguishable from healthy. Every alert that depends on a series existing needs a companion `absent()`, `absent_over_time()`, or `up == 0` rule, and every ratio needs `or vector(0)` on the numerator.

**2. `sum()` before `rate()` silently corrupts counters.** `rate()` detects counter resets per series; `sum()` destroys series identity, so a single pod restart inside the sum looks like an enormous negative step that `rate()` cannot correct ([rate](https://prometheus.io/docs/prometheus/latest/querying/functions/#rate)). Always `sum(rate(x[5m]))`, never `rate(sum(x)[5m:])`. This produces plausible-looking wrong numbers, which is worse than an error.

**3. Averaging per-cell quantiles gives the wrong answer with the wrong sign.** `avg(cell:p99)` weights a 100 req/s canary the same as a 1M req/s production cell. Aggregate bucket counts, not quantiles: `histogram_quantile(0.99, sum by (le) (rate(x_bucket[5m])))` ([histograms and summaries](https://prometheus.io/docs/practices/histograms/)). Client-side **summaries** cannot be aggregated at all — if a component only exposes summaries, you cannot compute a fleet quantile from it, full stop.

**4. Cross-cell histogram aggregation breaks silently on version skew.** Summing classic-histogram buckets across cells is only valid when every cell uses identical `le` boundaries. During a staged fleet upgrade — the operation your team performs constantly — two Temporal builds with different bucket definitions will merge into a nonsense distribution with no error. Native histograms remove this hazard by reconciling bucket layouts automatically and annotating when they cannot ([querying basics](https://prometheus.io/docs/prometheus/latest/querying/basics/)); until you migrate, treat histogram bucket boundaries as a fleet-wide API with a compatibility policy.

**5. Native histograms are stable but off by default.** They became stable in Prometheus **v3.8.0**, but scraping them requires explicitly setting `scrape_native_histograms`, and from v3.9.0 the old `--enable-feature=native-histograms` flag is a complete no-op with the setting defaulting to `false` ([v3.8.0](https://github.com/prometheus/prometheus/releases/tag/v3.8.0), [v3.9.0](https://github.com/prometheus/prometheus/releases/tag/v3.9.0)). Upgrading Prometheus does not enable them. Also note v3.14.0 fixed native histogram data becoming incorrect after a restart ([v3.14.0](https://github.com/prometheus/prometheus/releases/tag/v3.14.0)) — pin to a version at or after that fix before migrating a fleet.

**6. Prometheus's OTLP receiver still normalizes names by default.** Prometheus 3 supports UTF-8 in storage and the UI, but the OTLP metrics receiver's `translation_strategy` defaults to `UnderscoreEscapingWithSuffixes` ([Prometheus OTLP guide](https://prometheus.io/docs/guides/opentelemetry/)). Teams enable UTF-8, assume dotted OTel names now survive, and discover their queries do not match. Decide the strategy once, fleet-wide, and change it during a maintenance window — it renames metrics.

**7. Tail sampling requires all spans of a trace to hit the same Collector.** The `tailsamplingprocessor` README states this outright, and adds that it must sit after context-dependent processors like `k8sattributes` because it reassembles spans into new batches and they lose their original context ([README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md)). A naively horizontally-scaled Collector deployment behind a round-robin Service produces silently incomplete sampling decisions. You need a trace-ID-aware load balancing tier in front, or you need to accept head sampling.

**8. A Collector gateway behind an L4 load balancer pins every agent to one replica.** OTLP over gRPC uses long-lived HTTP/2 connections; an L4 balancer distributes *connections*, not requests, and gRPC multiplexes everything onto one connection ([guide 02](02-grpc.md)). The symptom is one Collector pod at 100% CPU while the others idle. The fix is the same as everywhere else in the gRPC world: client-side load balancing over a headless Service, or an L7 proxy.

**9. Inherited kube-prometheus rules fire forever on managed Kubernetes.** The default rule set assumes a self-managed control plane. On EKS/GKE/AKS, etcd, scheduler, and controller-manager targets do not exist, and their alerts either fire permanently or — worse, per gotcha 1 — evaluate empty forever and give you false confidence ([guide 04](04-managed-kubernetes-eks-gke-aks.md)). Fork the rule set per cloud and review it; do not install the chart's defaults into a cell template unread.

**10. `service_error_with_type` is not an availability SLI.** Temporal's error counters include error types that are client-caused and entirely expected — a workflow that already exists, an entity that is not found ([metrics reference](https://docs.temporal.io/references/cluster-metrics)). Building an SLI on the raw counter produces an error budget that burns during normal operation and teaches everyone to ignore the alert. Filter to server-fault types explicitly, write down which ones you chose, and re-review the list on every Temporal server upgrade.

**11. Pod readiness is not shard readiness.** [Guide 12](12-cell-lifecycle-synthesis.md) states it plainly: "Roll History slowly, and watch shard-ownership metrics rather than pod readiness," because shard ownership moves when a History pod goes away, and a rollout that outpaces shard reclamation produces a latency spike that looks like a database problem. A health gate that checks `Ready` will pass a cell whose shards are still redistributing. Assert re-convergence explicitly, using the names in [`metric_defs.go`](https://github.com/temporalio/temporal/blob/main/common/metrics/metric_defs.go) for your server version, not names copied from a blog post — see [guide 16](16-temporal-server-internals.md).

**12. Alertmanager `group_by` including `cell` recreates the pager storm.** Every other mechanism can be correct — central rule evaluation, fleet aggregate alerts, inhibition — and one line of grouping configuration still delivers three hundred notifications, because grouping by `cell` means each cell is its own group. Group by `[alertname, severity, slo]` and let the notification body enumerate the cells ([Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/)).

**13. Remote write turns a slow receiver into a cell-local memory problem.** Remote write auto-scales its shard count when it falls behind, buffering in memory against a WAL ([remote write tuning](https://prometheus.io/docs/practices/remote_write/)). A central store that throttles — and all three managed services throttle on quota ([AMP quotas](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP_quotas.html)) — pushes memory pressure back into every cell simultaneously. Alert on the send-lag expression and on `prometheus_remote_storage_shards / prometheus_remote_storage_shards_max`, and give the cell's Prometheus enough memory headroom to absorb a multi-hour central outage.

**14. Promtail is end-of-life.** Promtail entered LTS in February 2025 and reached end of life in **March 2026**; Grafana Alloy is the successor and ships a config conversion tool ([migration guide](https://grafana.com/docs/alloy/latest/set-up/migrate/from-promtail/)). Any cell template or runbook still referencing Promtail is shipping an unsupported component into three hundred cells.

---

## How this shows up in cell lifecycle

**Provisioning.** Observability is a layer in the cell's dependency DAG — [guide 12](12-cell-lifecycle-synthesis.md) places it at L8, before the database bootstrap, specifically so a failing bootstrap is visible. The Prometheus Operator's selector model is what makes that ordering work in practice: install the Operator and the `Prometheus` object as early as the cluster can host a pod, then let every subsequent layer ship its own `ServiceMonitor` and `PrometheusRule` alongside its workload. Monitoring converges with the cell. The `externalLabels` block is written once, from the cell's identity in the provisioning workflow's input, and everything downstream inherits the label contract for free.

**The readiness gate.** The Gate A–E expressions above are the machine-checkable form of "this cell can take traffic." They are an activity in the provisioning workflow: idempotent, retryable, individually timed, and evaluated over a soak window under synthetic load rather than once. Gate E — the comparison to the fleet median — deliberately cannot be evaluated inside the cell, which forces the global aggregation layer to exist before the first cell can be declared ready. That is the right dependency direction.

**Upgrades.** Every stage of a fleet upgrade is gated on the same expressions, with the addition of a *differential*: does this cell look different from before the upgrade, and different from the cells that have not been upgraded yet? The `cell_generation` external label is what makes that query expressible — compare the distribution of a metric across `cell_generation="2026.08.3"` versus `cell_generation="2026.08.2"` and you have an automatic canary analysis over the whole fleet. This is the single highest-value use of the label contract, and it is only available because every cell is identical and labeled.

**Teardown.** A deleted cell must stop producing series, and its series must go stale rather than lingering. Two failure modes to design against: alerts that fire forever on a cell that no longer exists (fix: alert rules scoped to a live cell inventory, not to whatever series happen to exist), and series that never expire in the central store, quietly paying for a decommissioned cell for the length of your retention. Teardown is also when the observability-of-the-cell-must-not-depend-on-the-cell rule pays off — the record of what the cell was doing in its last minutes has to survive the cell.

**Incidents.** The first question is always "which cell." The fleet dashboard should answer it without a click: a count of cells burning error budget, a heatmap of per-cell p95 against the fleet median, and the `cell_generation` breakdown so "is this the new release" is answered before anyone asks. The second question is "is it just this cell or is it the cloud" — which is why `cloud` and `region` are in the label contract rather than being derivable from the cell name. Only after those two answers does anyone open a trace, and only after a trace does anyone open logs.

**The interfaces you own.** Three things your team publishes that other teams consume: the label contract, the per-cell SLI recording rules, and the health gate definition. All three are APIs with compatibility obligations. Renaming a recording rule breaks someone's dashboard in the same way that renaming a gRPC field breaks a client ([guide 02](02-grpc.md)), and it deserves the same deprecation discipline.

---

## Learning path

### Day 1

- Read [The mental model](#the-mental-model) above, then Prometheus's [data model](https://prometheus.io/docs/concepts/data_model/) and [metric types](https://prometheus.io/docs/concepts/metric_types/). Twenty minutes, and it removes 80% of PromQL confusion.
- Read the [staleness section](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness) of Querying Basics carefully. It is the source of the subtlest bugs in this domain.
- Run Hands-on Steps 1–4. Stop after the burn-rate alert fires and clears.
- Find your team's existing per-cell dashboard and answer one question from it: how many cells are currently unhealthy? If you cannot, you have found your first project.

### Week 1

- Finish the Hands-on lab, including the cardinality blow-up. Do not skip it — the memory curve is the part that sticks.
- Read the SRE Workbook's [Implementing SLOs](https://sre.google/workbook/implementing-slos/) and [Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/). These are the two most valuable chapters in either SRE book for cell infrastructure.
- Read Temporal's [Service metrics reference](https://docs.temporal.io/references/cluster-metrics) end to end, then open [`metric_defs.go`](https://github.com/temporalio/temporal/blob/main/common/metrics/metric_defs.go) and skim it. The docs cover a fraction of what exists.
- Write down your team's actual label contract. If it is not written down anywhere, that is the finding — write the first draft and circulate it.
- Audit one production alert rule against gotchas 1, 2, and 3. Do not be surprised to find a problem.

### Month 1

- Read [Histograms and summaries](https://prometheus.io/docs/practices/histograms/) and the [native histograms spec](https://prometheus.io/docs/specs/native_histograms/), then find out whether your fleet has a bucket-boundary compatibility policy. Propose one if not; this is a genuine correctness gap in most fleets and a well-scoped senior project.
- Read the [OpenTelemetry spec status page](https://opentelemetry.io/docs/specs/status/) and the [Collector deployment patterns](https://opentelemetry.io/docs/collector/deploy/). Form your own opinion about where OTel belongs in your cells and write it down as a one-page position.
- Inventory the fleet's cardinality: top 20 metric names by series count, per cell, and the delta over the last quarter. Present it next to the observability bill.
- Take the Gate A–E health-gate skeleton above and reconcile it line-by-line with your team's actual readiness checklist ([guide 12](12-cell-lifecycle-synthesis.md)). Every checklist line that has no query is a gap; every query with no checklist line is unexplained.
- Ask the three questions from guide 12 that this guide is the answer to: can we bring up a cell if the shared observability backend is down? Can we tell a gray failure — a cell that is up, healthy by its own report, and serving errors to half its clients? How would we know?

---

## References

1. [Data model — Prometheus](https://prometheus.io/docs/concepts/data_model/) — series identity, and therefore cardinality, defined precisely.
2. [Metric types — Prometheus](https://prometheus.io/docs/concepts/metric_types/) — counter/gauge/histogram/summary and their aggregation properties.
3. [Querying basics — Prometheus](https://prometheus.io/docs/prometheus/latest/querying/basics/) — selectors, the 5-minute lookback, staleness markers, and native histogram bucket reconciliation.
4. [Query functions — Prometheus](https://prometheus.io/docs/prometheus/latest/querying/functions/) — exact semantics of `rate`, `increase`, and `histogram_quantile`, including bucket interpolation and `+Inf` behavior.
5. [Histograms and summaries — Prometheus](https://prometheus.io/docs/practices/histograms/) — why summaries cannot be aggregated and histograms can.
6. [Native Histograms specification — Prometheus](https://prometheus.io/docs/specs/native_histograms/) — the exponential bucket schema and the compatibility rules.
7. [Release v3.8.0 — prometheus/prometheus](https://github.com/prometheus/prometheus/releases/tag/v3.8.0) — native histograms declared stable; `scrape_native_histograms` introduced (v3.9.0 made the old feature flag a no-op).
8. [Release v3.13.0 (LTS) — prometheus/prometheus](https://github.com/prometheus/prometheus/releases/tag/v3.13.0) — the current LTS line to run inside cells.
9. [Release v3.14.0 — prometheus/prometheus](https://github.com/prometheus/prometheus/releases/tag/v3.14.0) — latest release as of 2026-08-29; native histogram restart-correctness fix.
10. [Announcing Prometheus 3.0 — Prometheus](https://prometheus.io/blog/2024/11/14/prometheus-3-0/) — UTF-8 names, OTLP ingestion, and the breaking-change summary.
11. [Configuration reference — Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/configuration/) — `scrape_config`, `sample_limit`, `relabel_configs`, `metric_relabel_configs`, `kubernetes_sd_config`.
12. [Alerting best practices — Prometheus](https://prometheus.io/docs/practices/alerting/) — page on symptoms, at the highest level possible.
13. [Metric and label naming — Prometheus](https://prometheus.io/docs/practices/naming/) — the rule against unbounded label values, and the `level:metric:operations` recording-rule convention's companion.
14. [Remote write tuning — Prometheus](https://prometheus.io/docs/practices/remote_write/) — shard auto-scaling, memory behavior under a slow receiver, and the metrics to alert on.
15. [Remote-Write 2.0 specification — Prometheus](https://prometheus.io/docs/specs/prw/remote_write_spec_2_0/) — metadata, exemplars, native histograms, string interning, UTF-8 acceptance.
16. [Using Prometheus as your OpenTelemetry backend — Prometheus](https://prometheus.io/docs/guides/opentelemetry/) — the OTLP receiver and the `translation_strategy` default that renames your metrics.
17. [Alertmanager configuration — Prometheus](https://prometheus.io/docs/alerting/latest/configuration/) — `group_by`, routing trees, and inhibition rules: the 300-cells dedupe machinery.
18. [Prometheus Operator design — prometheus-operator.dev](https://prometheus-operator.dev/docs/getting-started/design/) — the CRDs and the selector model that makes incremental cell bootstrap work.
19. [kube-prometheus-stack chart — prometheus-community/helm-charts](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) — the chart used in the lab, and the values you will fork per cloud.
20. [Kubernetes API server SLIs — kubernetes.io](https://kubernetes.io/docs/reference/instrumentation/slis/) — why `apiserver_request_sli_duration_seconds` differs from the raw duration metric.
21. [etcd metrics — etcd.io](https://etcd.io/docs/latest/metrics/) — leader presence, leader churn, WAL fsync latency, and database size.
22. [Specification Status Summary — OpenTelemetry](https://opentelemetry.io/docs/specs/status/) — the authoritative per-signal stability picture and the component lifecycle definitions.
23. [OpenTelemetry Collector — opentelemetry.io](https://opentelemetry.io/docs/collector/) — receivers, processors, exporters, connectors, agent/gateway deployment, and per-component stability.
24. [Semantic Conventions 1.44.0 — OpenTelemetry](https://opentelemetry.io/docs/specs/semconv/) — the attribute vocabulary to adopt and pin.
25. [OTLP specification — OpenTelemetry](https://opentelemetry.io/docs/specs/otlp/) — the wire protocol underneath all of it.
26. [tailsamplingprocessor README — opentelemetry-collector-contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md) — beta status, the same-instance constraint, and the processor ordering requirement.
27. [OpenTelemetry Profiles Enters Public Alpha — OpenTelemetry](https://opentelemetry.io/blog/2026/profiles-alpha/) — the fourth signal, and how far along it is.
28. [CNCF announces OpenTelemetry's graduation — CNCF](https://www.cncf.io/announcements/2026/05/21/cloud-native-computing-foundation-announces-opentelemetrys-graduation-solidifying-status-as-the-de-facto-observability-standard/) — graduation on 2026-05-21 and what the review covered.
29. [W3C Trace Context — W3C](https://www.w3.org/TR/trace-context/) — `traceparent` and `tracestate`, the headers that make cross-service tracing work.
30. [`log/slog` — pkg.go.dev](https://pkg.go.dev/log/slog) — structured logging in the Go standard library since 1.21.
31. [Migrate from Promtail to Grafana Alloy — Grafana](https://grafana.com/docs/alloy/latest/set-up/migrate/from-promtail/) — Promtail's March 2026 EOL and the conversion tooling (vendor source).
32. [Grafana Loki documentation — Grafana](https://grafana.com/docs/loki/latest/) — label-only indexing, and why stream cardinality is the Loki analogue of series cardinality (vendor source).
33. [Vector documentation — vector.dev](https://vector.dev/docs/) — the programmable log/metric pipeline agent, and its back-pressure model (vendor source).
34. [Thanos — thanos.io](https://thanos.io/) — global PromQL over a fleet of Prometheus servers plus object storage.
35. [Grafana Mimir documentation — Grafana](https://grafana.com/docs/mimir/latest/) — the metrics-warehouse model and the 3.0 decoupled architecture (vendor source).
36. [Amazon Managed Service for Prometheus service quotas — AWS](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP_quotas.html) — the 1.5B active-series ceiling, 2M minimum, and how throttling surfaces.
37. [Cloud Monitoring quotas and limits — Google Cloud](https://cloud.google.com/monitoring/quotas) — the ~100k samples/sec default ingest quota shared with Google Managed Prometheus.
38. [Scaling Azure Monitor Workspaces — Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/azure-monitor-workspace-scaling-best-practice) — the 1M active-series default and how to raise it.
39. [OSS Temporal Service metrics reference — Temporal](https://docs.temporal.io/references/cluster-metrics) — every Temporal metric name and example query used in this guide.
40. [`common/metrics/metric_defs.go` — temporalio/temporal](https://github.com/temporalio/temporal/blob/main/common/metrics/metric_defs.go) — the definitive metric list; the docs are a curated subset.
41. [Performance bottlenecks troubleshooting — Temporal](https://docs.temporal.io/troubleshooting/performance-bottlenecks) — shard lock latency expectations and the persistence-first diagnostic order.
42. [Monitoring Distributed Systems — Google SRE Book](https://sre.google/sre-book/monitoring-distributed-systems/) — the four golden signals, and "symptoms rather than causes."
43. [Implementing SLOs — Google SRE Workbook](https://sre.google/workbook/implementing-slos/) — SLI/SLO/error-budget definitions and how to choose them.
44. [Alerting on SLOs — Google SRE Workbook](https://sre.google/workbook/alerting-on-slos/) — the multiwindow multi-burn-rate technique and the 14.4/6/1 table.
