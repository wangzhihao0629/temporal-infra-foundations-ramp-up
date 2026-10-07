# The Temporal Programming Model — What Your Customers Actually Build

**Why this matters.** If you own cell lifecycle across AWS, GCP, and Azure, a cell is not an abstract unit of capacity — it is a place where thousands of other people's Workflows are mid-flight, holding state that must survive your node rotations, your Karpenter consolidations, and your control-plane upgrades. Everything you do to a cell is felt by somebody's Workflow Execution, and every load characteristic you have to plan for (hot Task Queues, 50 MB Event Histories, high-cardinality Search Attributes, worker fleets that scale from 3 pods to 300 in ninety seconds) is a *customer programming decision* that arrived at your infrastructure. This guide teaches the product from the seat of the person paying for it: what a Workflow is, what it costs, what breaks, and what people complain about. It is deliberately *not* about how the server implements any of this — that is [16-temporal-server-internals.md](16-temporal-server-internals.md). Read this one first anyway: you cannot reason about History Shard hot spots until you know why a customer would put 100,000 Activities in one Workflow.

**On sourcing.** Every limit, default, and version number below was verified against live primary sources on **2026-08-29** and carries an inline link. Temporal's documentation is unusually good and unusually specific about limits; where a number exists in the docs I cite it rather than paraphrase. Where I am extrapolating from public material to what it means for a platform owner, I say so in the sentence. Nothing here is an internal Temporal detail; I do not have any.

---

## The mental model

Hold six ideas. Everything else in this guide is a consequence of one of them.

**1. A Workflow is a function whose call stack is a database row.** You write ordinary-looking code. The SDK intercepts every call that touches the outside world or the passage of time and turns it into a **Command** sent to the server; the server turns Commands into **Events** and appends them to an [Event History](https://docs.temporal.io/workflow-execution/event). If the process holding your function dies, another process re-runs the function from the top, feeding it the recorded Events instead of re-doing the work, until it reaches the point where the old process stopped. Then it continues. That is the whole trick, and every constraint in the product falls out of it.

**2. Determinism is not a style guide; it is the load-bearing wall.** Because recovery works by re-running your function, the function must produce *the same sequence of Commands* every time, given the same History. The docs state the requirement plainly: "any time your Workflow code is executed it makes the same Workflow API calls in the same sequence, given the same input" ([Workflow Definition](https://docs.temporal.io/workflow-definition#deterministic-constraints)). Clocks, random numbers, map iteration order, goroutines, and network calls all violate this. The SDK gives you replay-safe replacements for the first four and tells you to put the fifth in an Activity.

**3. Activities are the airlock.** Anything non-deterministic goes in an Activity, which runs outside the replay path and is retried by the platform. The price of that convenience is **at-least-once execution**: the platform will happily run your Activity twice if the first attempt's result never made it back. Temporal's own recommendation is that Activities "be [idempotent](https://docs.temporal.io/activity-definition#idempotency)" ([Activities](https://docs.temporal.io/activities)). Idempotency is not a nicety here. It is the contract.

**4. The Temporal Service never runs customer code.** This is the single most important architectural fact for you. From the docs: "the Temporal Service (including the Temporal Cloud) doesn't execute any of your code (Workflow and Activity Definitions) on Temporal Service machines" ([Workers](https://docs.temporal.io/workers#worker-process)). Workers live in the customer's infrastructure and *poll* your cells. Your cells hold state and match tasks; they never execute business logic. Every capacity conversation is therefore about **state size, task-matching rate, and poll concurrency** — never about CPU spent on customer logic.

**5. Everything has a size limit, and customers will find all of them.** Event History caps at [51,200 Events or 50 MB](https://docs.temporal.io/workflow-execution/limits) with warnings starting at 10,240 Events / 10 MB. A single payload caps at 2 MB, a gRPC message at 4 MB ([Cloud limits](https://docs.temporal.io/cloud/limits)). These are precautionary limits that exist because the alternative is your database falling over. When a customer hits one, it becomes your incident.

**6. The Namespace is the tenancy unit, and it maps onto your cells.** A [Namespace](https://docs.temporal.io/namespaces) is "a unit of isolation" providing Workflow ID uniqueness, resource isolation, and configuration boundaries. Temporal Cloud's namespace-provisioning path selects a cell within a region, and Same-region Replication explicitly "replicate[s] the Namespace across multiple cells in that region" ([High Availability](https://docs.temporal.io/cloud/high-availability#same-region-replication)). Namespace is the partition key Temporal Cloud's cells are partitioned by. See [12-cell-lifecycle-synthesis.md](12-cell-lifecycle-synthesis.md).

---

## Core concepts

### Durable execution: what it replaces, and what it costs

The pitch is narrow and worth stating precisely. Without Temporal, a long multi-step business process gets built out of five separate mechanisms, each with its own failure modes:

| Home-grown mechanism | What it does | What Temporal replaces it with |
|---|---|---|
| Cron / scheduled job | Kick off work at a time | [Schedules](https://docs.temporal.io/schedule) and durable [Timers](https://docs.temporal.io/workflow-execution/timers-delays) |
| Message queue + consumer | Hand work between steps | [Task Queues](https://docs.temporal.io/task-queue) and Activity dispatch |
| Retry table / dead-letter queue | Re-attempt failed steps with backoff | [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies), on by default for Activities |
| State machine table in your DB | Remember where the process got to | [Event History](https://docs.temporal.io/workflow-execution/event), maintained by the platform |
| Saga bookkeeping / compensation ledger | Undo partial work | Ordinary control flow plus the [Saga pattern](https://docs.temporal.io/design-patterns/saga-pattern) |

The value proposition is that all five collapse into a function you can read top to bottom. The docs' framing of the payoff: "The only reason a Workflow Execution might fail is due to the code throwing an error or exception, not because of underlying infrastructure outages" ([Workflow Definition](https://docs.temporal.io/workflow-definition#unreliable-worker-processes)).

The honest tradeoffs, none of which Temporal hides:

- **You give up writing normal code inside Workflows.** No clocks, no `rand`, no `range` over a map, no `go` statement, no network calls. See the determinism section below.
- **Deploys become hard.** Changing a Workflow that has in-flight executions is the single hardest operational problem in the product. It has its own section.
- **Every step is a database write.** Event History is durable state; a Workflow with 100,000 Activities is 300,000+ Events and will not fit. Costs and limits are real.
- **You now operate a worker fleet.** Temporal does not run your code, so poller counts, slot counts, and pod counts are your problem — and, when they go wrong in a way that looks like a Temporal problem, your customer's support ticket lands on your team.
- **Debugging moves.** A stuck process is now debugged by reading an Event History in a Web UI instead of grepping logs, which is better once you know how and disorienting until then.

### Workflows and the determinism constraint

A Go Workflow is an exported function whose first parameter is `workflow.Context` ([Go SDK basics](https://docs.temporal.io/develop/go/workflows/basics)):

```go
package billing

import (
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

type ChargeRequest struct {
	CustomerID string
	AmountCents int64
	IdempotencyKey string
}

type ChargeResult struct {
	TransactionID string
	SettledAt     time.Time
}

func ChargeWorkflow(ctx workflow.Context, req ChargeRequest) (*ChargeResult, error) {
	ao := workflow.ActivityOptions{
		StartToCloseTimeout:    30 * time.Second,
		ScheduleToCloseTimeout: 10 * time.Minute,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:        time.Second,
			BackoffCoefficient:     2.0,
			MaximumInterval:        30 * time.Second,
			NonRetryableErrorTypes: []string{"InvalidCard"},
		},
	}
	ctx = workflow.WithActivityOptions(ctx, ao)

	var auth AuthResult
	if err := workflow.ExecuteActivity(ctx, AuthorizeCard, req).Get(ctx, &auth); err != nil {
		return nil, err
	}

	// A durable timer. The worker can die here; the timer lives on the server.
	if err := workflow.Sleep(ctx, 24*time.Hour); err != nil {
		return nil, err
	}

	var res ChargeResult
	if err := workflow.ExecuteActivity(ctx, CaptureFunds, auth).Get(ctx, &res); err != nil {
		return nil, err
	}
	return &res, nil
}
```

Three things in that snippet are doing invisible work. `workflow.ExecuteActivity` emits a [`ScheduleActivityTask`](https://docs.temporal.io/references/commands) Command. `workflow.Sleep` emits a `StartTimer` Command and the function *suspends* — the worker process is free to evict this Workflow from memory entirely and the sleep still fires 24 hours later. `.Get(ctx, &res)` blocks the Workflow's coroutine, not an OS thread.

**What breaks determinism.** The docs enumerate the Command-producing calls that must not be reordered, added, or removed without versioning ([Workflow Definition](https://docs.temporal.io/workflow-definition#deterministic-constraints)): starting or cancelling a Timer; scheduling or cancelling Activity Executions (including local Activities); starting or cancelling Child Workflows; signalling or cancelling signals to external Workflows; scheduling or cancelling Nexus operations; ending the Workflow in any way; `Patched`/`GetVersion` calls; upserting Search Attributes or Memos; and running a `SideEffect` or `MutableSideEffect`.

Separately, **intrinsic non-determinism** is logic that could branch differently on re-execution even with identical input. In Go the specific landmines are named in the docs ([Go SDK basics](https://docs.temporal.io/develop/go/workflows/basics#workflow-logic-requirements)):

| Non-deterministic construct | Replay-safe Go replacement | Why it breaks |
|---|---|---|
| `time.Now()` | `workflow.Now(ctx)` | Returns the time of the last Workflow Task, consistent across replays |
| `time.Sleep()` | `workflow.Sleep(ctx, d)` | Real sleep blocks a worker thread and is not durable |
| `rand`, `uuid.New()` | `workflow.SideEffect` (records the value in History) or an Activity | Fresh randomness on each replay diverges the Command sequence |
| `range` over a map | Collect keys, sort, then iterate — or do it in an Activity | Go randomizes map iteration order by design |
| `go func()` | `workflow.Go(ctx, ...)` | Real goroutines are outside the SDK's deterministic scheduler |
| `chan` / `select` | `workflow.Channel` / `workflow.Selector` | Same reason |
| `log`, `fmt.Println` | `workflow.GetLogger(ctx)` | The SDK logger suppresses duplicate lines during replay |
| HTTP calls, DB queries, file I/O | An Activity | Non-deterministic by nature, and not retried by the platform if inline |

`workflow.IsReplaying(ctx)` exists for guarding metrics emission and similar side channels, with an explicit warning never to branch business logic on it.

**How the SDK enforces it.** Two mechanisms, neither complete. First, at replay time the SDK compares each emitted Command against the corresponding Event in History; a mismatch produces a [non-determinism error](https://docs.temporal.io/references/errors#non-deterministic-error). Second, the Go SDK performs a **runtime check** that catches obvious reordering of `ExecuteActivity`, `ExecuteChildWorkflow`, `NewTimer`, `RequestCancelWorkflow`, `SideEffect`, `SignalExternalWorkflow`, and `Sleep` — but the docs are blunt that "the runtime check does not perform a thorough check" and does not inspect Activity arguments or Timer durations ([Go SDK versioning](https://docs.temporal.io/develop/go/workflows/versioning#runtime-checking)). Third, and best, there is a **static** determinism checker shipped in the Go SDK repo at [`contrib/tools/workflowcheck`](https://github.com/temporalio/sdk-go/tree/main/contrib/tools/workflowcheck), which flags non-deterministic calls at build time. Customers who do not run it discover their bugs in production.

The safe-change list is short and worth memorizing, because it is what a customer will ask you at 2 a.m.: you *can* change Activity/Child Workflow input parameters, return values, and timeouts; you *can* change a Timer's duration (except to or from zero in Go, Java, and Python); you *can* add a Signal handler for a Signal type not yet sent; you *cannot* change Activity or Child Workflow **types or IDs**.

### Replay, and where a non-determinism error surfaces

When a worker crashes or a Workflow is evicted from cache, recovery is: the Workflow Task times out, another worker picks it up, "the new Worker replays the Event History to reconstruct state," and execution continues ([Worker tuning reference](https://docs.temporal.io/develop/worker-tuning-reference#how-worker-failure-recovery-works)).

A non-determinism error is *not* a Workflow failure. It is a **Workflow Task failure**, which the docs distinguish carefully ([Application failures](https://docs.temporal.io/encyclopedia/application-failures#workflow-task-failures-vs-workflow-execution-failures)):

| | Workflow Task failure | Workflow Execution failure |
|---|---|---|
| Caused by | Non-Temporal errors: nil deref, type errors, **non-determinism errors** | Temporal failures thrown by your code, e.g. Application Failure |
| Retried | Yes, automatically, until the Workflow Execution Timeout, with exponential backoff capped at a 10-minute interval ([Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)) | No |
| Workflow state | Preserved. Fix the bug, redeploy, it continues | "Failed" permanently |
| Typical cause | A bug in the Workflow code | A permanent business-logic failure |

This is a genuinely good design and a genuinely confusing one. A bad deploy does not destroy work — it wedges every affected Workflow in a retry loop that keeps ticking until someone ships a fix. From your side of the fence, that retry loop is **load**: every retry is a Workflow Task dispatch and a History read against your cell. A customer who deploys a non-deterministic change to ten thousand running Workflows generates a sustained, self-inflicted, low-grade DDoS against one Namespace, and it will not stop on its own. The Web UI surfaces these in a [Task Failures view](https://docs.temporal.io/web-ui#task-failures-view), flagged after five consecutive Workflow Task failures via the `TemporalReportedProblems` Search Attribute.

### Versioning: the hardest part of operating Temporal

If you learn one customer-side pain point deeply, make it this one. Temporal offers two strategies, and as of the current docs it recommends the newer one ([Workflow Definition](https://docs.temporal.io/workflow-definition#workflow-versioning)).

**Patching (`GetVersion` in Go).** You put a branch in the Workflow keyed to a change ID, and the SDK records a marker in History so that replays take the branch they originally took ([Go SDK versioning](https://docs.temporal.io/develop/go/workflows/versioning#patching)):

```go
// v == workflow.DefaultVersion for executions that predate this change.
v := workflow.GetVersion(ctx, "Step1", workflow.DefaultVersion, 1)
if v == workflow.DefaultVersion {
	err = workflow.ExecuteActivity(ctx, ActivityA, data).Get(ctx, &result1)
} else {
	err = workflow.ExecuteActivity(ctx, ActivityC, data).Get(ctx, &result1)
}
```

The lifecycle is four stages and most teams get stuck on stage three. Introduce the patch with `minSupported = DefaultVersion`. Ship. Wait for old executions to leave retention. Raise `minSupported` and delete the dead branch. Eventually, once no execution predates the change, you can collapse to `workflow.GetVersion(ctx, "Step1", 2, 2)` — but the docs advise **keeping the call**, both so a stale execution fails loudly rather than silently taking the wrong path, and so the next change to that step is a one-line bump of `maxSupported`.

Finding out whether it is safe to clean up is a Visibility query, which is the detail most people miss:

```text
WorkflowType = "PizzaWorkflow"
    AND ExecutionStatus = "Running"
    AND TemporalChangeVersion = "ChangedNotificationActivityType-1"
```

and, for executions that started before `GetVersion` existed in that code path:

```text
WorkflowType = "PizzaWorkflow"
    AND ExecutionStatus = "Running"
    AND TemporalChangeVersion IS NULL
```

`TemporalChangeVersion` is a default [Search Attribute](https://docs.temporal.io/search-attribute) of type Keyword List that "stores change/version pairs if the GetVersion API is enabled." Which means: **every patch a customer adds writes a Search Attribute value, and patched long-running Workflows accumulate them.** That is Visibility-store index pressure created by an application-code decision, and it lands on your Elasticsearch.

The `patched()` semantics in the newer SDKs have two behaviors the docs call "potentially unexpected" ([Patching](https://docs.temporal.io/patching)): a patch encountered *before* its marker's position in History throws a non-determinism error, while a patch with no marker anywhere returns `false` — permanently, for the rest of that execution, even after replay ends and the code is running live. The prescription is to always put the newest branch at the top of an if-patched chain.

**Worker Versioning.** The recommended alternative pins a Workflow Execution to a **Worker Deployment Version** (a deployment name plus a Build ID) so old executions keep running old code and new executions start on new code ([Worker Versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)). Workflow Types are annotated `PINNED` (runs entirely on one version) or `AUTO_UPGRADE` (moves forward, still needs patching). The decision guide the docs publish:

| Workflow duration | Uses Continue-As-New? | Recommended behavior | Patching required? |
|---|---|---|---|
| Short (completes before next deploy) | N/A | `PINNED` | Never |
| Medium (spans multiple deploys) | No | `AUTO_UPGRADE` | Yes |
| Long (weeks to years) | Yes | `PINNED` + upgrade on Continue-As-New | Never |
| Long (weeks to years) | No | `AUTO_UPGRADE` + patching | Yes |

Minimum versions matter to you because customers will ask: Go SDK [v1.35.0](https://github.com/temporalio/sdk-go/releases/tag/v1.35.0), Server [v1.29.1](https://github.com/temporalio/temporal/releases/tag/v1.29.1), CLI [v1.4.1](https://github.com/temporalio/cli/releases/tag/v1.4.1), UI [v2.38.0](https://github.com/temporalio/ui/releases/tag/v2.38.0). Worker Versioning is incompatible with **rolling** deployments; it needs blue-green or "rainbow" (many concurrent versions). There is a [Temporal Worker Controller](https://docs.temporal.io/production-deployment/worker-deployments/kubernetes-controller) for Kubernetes that automates rainbow deploys.

Note the deprecation you will hear about: support for the pre-2025 experimental Worker Versioning method "will be removed from Temporal Server in March 2026" ([Workflow Definition](https://docs.temporal.io/workflow-definition#worker-versioning)). Given today's date, that removal has landed; customers still on the legacy API are already broken or already migrated.

A platform-owner consequence worth internalizing: Worker Versioning changes the *shape* of Task Queue traffic. Each Deployment Version's Workers register against the Task Queue, and the docs warn about a real failure mode — "if all of your Current Version Workers go down, you would expect at least 50% of new Workflows to go to the Ramping Version. This won't happen because the Tasks for the Current Version are blocking the queue." The Cloud limits cap Worker Deployments at 100 per Namespace, versions at 100 per deployment, and Task Queues at 100 per Deployment Version ([Cloud limits](https://docs.temporal.io/cloud/limits#worker-versioning-level)).

### Activities: at-least-once, idempotency, heartbeating, cancellation

An Activity is "a normal function or method that executes a single, well-defined action" and its code "can be non-deterministic" ([Activities](https://docs.temporal.io/activities)). It is invoked by the Workflow, executed by a Worker, and its result is written to Event History.

The critical semantics: **an Activity Execution can consist of many Activity Task Executions**, one per attempt. If an attempt fails, times out, or the worker dies mid-flight, the platform schedules another. If the work completed but the completion never reached the server, the platform re-runs it anyway. This is at-least-once, and the mitigation is idempotency keys carried in the Activity input.

```go
func CaptureFunds(ctx context.Context, auth AuthResult) (ChargeResult, error) {
	info := activity.GetInfo(ctx)
	// The idempotency key must be stable across retries. Derive it from the
	// Workflow, not from the attempt.
	key := fmt.Sprintf("%s/%s", info.WorkflowExecution.ID, info.ActivityID)

	return psp.Capture(ctx, psp.CaptureRequest{
		AuthToken:      auth.Token,
		IdempotencyKey: key,
	})
}
```

`activity.GetInfo(ctx).Attempt` is available if you need to reason about which try you are on, and there is an explicit escape hatch for permanent failures ([Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies#non-retryable-errors)):

```go
if !card.Valid() {
	return ChargeResult{}, temporal.NewNonRetryableApplicationError(
		"card rejected", "InvalidCard", nil)
}
```

**Heartbeating.** A [Heartbeat](https://docs.temporal.io/encyclopedia/detecting-activity-failures#activity-heartbeat) is a ping from the worker executing the Activity to the Service, telling it the work is progressing and the worker has not crashed. Three properties matter:

1. Heartbeats carry an optional `details` payload used as a **checkpoint**. On retry, `activity.HasHeartbeatDetails(ctx)` / `GetHeartbeatDetails(ctx, &v)` recover it, so a partially-completed batch resumes instead of restarting ([Go SDK timeouts](https://docs.temporal.io/develop/go/activities/timeouts#activity-heartbeats)).
2. **Cancellations are delivered only on Heartbeat.** The docs are explicit: "Activities that don't Heartbeat can't receive a Cancellation." A long-running Activity with no heartbeat is uncancellable, and the customer's "cancel this Workflow" button appears broken.
3. Heartbeats are **throttled by the worker**, not sent every call. The throttle interval is the smaller of `heartbeatTimeout * 0.8` (or `defaultHeartbeatThrottleInterval`, default 30s, when no heartbeat timeout is set) and `maxHeartbeatThrottleInterval`, default 60s. Throttling "does not apply to the final Heartbeat message in the case of Activity Failure," so checkpoints survive a clean failure — but not a worker crash before delivery.

```go
func ProcessShards(ctx context.Context, in Input) error {
	start := in.StartIndex
	if activity.HasHeartbeatDetails(ctx) {
		var done int
		if err := activity.GetHeartbeatDetails(ctx, &done); err == nil {
			start = done + 1
		}
	}
	for i := start; i < in.EndIndex; i++ {
		if err := processOne(ctx, i); err != nil {
			return err
		}
		activity.RecordHeartbeat(ctx, i) // checkpoint + liveness + cancellation delivery
	}
	return nil
}
```

Heartbeating is not required for [Local Activities](https://docs.temporal.io/local-activity) and does nothing there. The docs' rule of thumb: heartbeat when you can report definite progress and the work is long — reading a large S3 object, running a GPU training job — not for a quick API call.

**Cancellation.** Cancelling a Workflow propagates to its Activities; the Activity's `context.Context` is cancelled and the Activity should clean up and return. Because compensation logic must run *after* cancellation, the Go SDK provides `workflow.NewDisconnectedContext` — the Saga docs call this out directly as a best practice.

### The activity timeout quartet

Four timeouts, all with different scopes. This table is the single densest piece of Temporal knowledge in the guide, and the last column is what people get wrong.

| Timeout | Scope | When it fires | Default | Retryable? | What people get wrong |
|---|---|---|---|---|---|
| **Schedule-To-Start** | One Activity **Task** (every attempt) | Task sits in the Task Queue too long before any worker picks it up | ∞ (infinity) | **No, by design** — a retry would just re-enqueue on the same queue | Setting it at all. The docs say: "In most cases, we recommend monitoring the `temporal_activity_schedule_to_start_latency` metric ... instead of setting this timeout." Customers set it to 60s, their worker fleet scales slowly, and their Workflows fail permanently instead of waiting |
| **Start-To-Close** | One Activity **Task Execution** (one attempt) | A single attempt runs longer than allowed | Same as Schedule-To-Close default | Yes — resets the timer, increments attempt | Not setting it. "The Temporal Server doesn't detect failures when a Worker loses communication with the Server or crashes. Therefore, the Temporal Server relies on the Start-To-Close Timeout to force Activity retries." Without it, a crashed worker leaves an Activity hanging forever |
| **Schedule-To-Close** | The whole Activity **Execution** (all attempts) | Total elapsed time across the retry chain exceeds the budget | ∞ (infinity) | It *is* the retry budget | Confusing "Schedule" with the one in Schedule-To-Start. Schedule-To-Start is per-Task; Schedule-To-Close is measured from the **first** Task in the chain |
| **Heartbeat** | Between consecutive Heartbeats | No Heartbeat received within the window | Unset | Yes | Setting a Heartbeat Timeout without actually calling `RecordHeartbeat`, which fails the Activity on a timer. Or setting it so short that throttling (0.8×) fights it |

An Activity **must** have either Start-To-Close or Schedule-To-Close set. Temporal "strongly recommend[s] setting a Start-To-Close Timeout" ([Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures)). The pragmatic default a competent customer uses: Start-To-Close sized to the p99.9 of one attempt, Schedule-To-Close sized to the business deadline, Heartbeat set only on genuinely long Activities that actually heartbeat, and Schedule-To-Start left unset with an alert on the latency metric instead.

For your side of the fence: `temporal_activity_schedule_to_start_latency` rising is the canonical signal that a *customer's* worker fleet is undersized. It will show up in your support queue as "Temporal is slow."

### Retry policies

Activities retry by default; Workflows do not ([Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)). The default policy:

```text
Initial Interval     = 1 second
Backoff Coefficient  = 2.0
Maximum Interval     = 100 × Initial Interval
Maximum Attempts     = ∞
Non-Retryable Errors = []
```

Unlimited attempts by default is the setting that surprises people most. An Activity calling a permanently-broken downstream will retry at a 100-second interval forever unless a Schedule-To-Close Timeout or Maximum Attempts bounds it. The docs prefer the timeout: "we recommend using the Workflow Execution Timeout for Workflows or the Schedule-To-Close Timeout for Activities to limit the total duration of retries, rather than using [Maximum Attempts]."

Workflow-level retry exists but is discouraged: "retrying the whole Workflow would repeat the same logic without resolving the underlying issue." Two legitimate uses the docs name are Cron Jobs and file-processing Workflows tied to a specific host.

One Event History subtlety that trips up debugging: while an Activity is retrying, `ActivityTaskScheduled` is the *only* Activity Event in History. `ActivityTaskStarted` is not written until the Activity closes, "to avoid filling the Event History with noise" — the final attempt count is an attribute of `ActivityTaskStarted` and is not known until then. To see a retrying Activity's current attempt, use the Describe API or the Web UI's Pending Activities panel.

### Workers, task queues, and long polling

A [Worker Process](https://docs.temporal.io/workers#worker-process) polls a Task Queue, dequeues Tasks, executes code, and reports results. A **Worker Entity** polls exactly one Task Queue; a process may host several entities and thus poll several queues.

```go
func main() {
	c, err := client.Dial(client.Options{
		HostPort:  os.Getenv("TEMPORAL_ADDRESS"),
		Namespace: os.Getenv("TEMPORAL_NAMESPACE"),
	})
	if err != nil {
		log.Fatalln("dial:", err)
	}
	defer c.Close()

	w := worker.New(c, "billing", worker.Options{
		MaxConcurrentActivityExecutionSize:     200,
		MaxConcurrentWorkflowTaskExecutionSize: 100,
		MaxConcurrentActivityTaskPollers:       8,
		MaxConcurrentWorkflowTaskPollers:       8,
		WorkerStopTimeout:                      60 * time.Second, // graceful drain
	})

	w.RegisterWorkflow(billing.ChargeWorkflow)
	w.RegisterActivity(billing.AuthorizeCard)
	w.RegisterActivity(billing.CaptureFunds)

	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalln("worker:", err)
	}
}
```

**Task Queues** are created on demand, need no registration, and are unlimited in number ([Task Queues](https://docs.temporal.io/task-queue)). Workers poll via long-polling gRPC, which the docs list benefits for: a worker polls "only when it has spare capacity," queues load-balance across workers, Activity queues support server-side throttling, and — importantly for network design — "Worker Processes connect directly to the Temporal Service ... without needing to open exposed ports."

Two operational facts hide in that page:

- **All workers on a Task Queue must register the same Workflow Types, Activity Types, and Nexus Operations.** A Task for an unregistered type fails with a retryable "Not Found." A customer that splits a queue's registrations across two deployments produces a mysterious stall. The two exceptions are Worker Versioning and dynamic handlers.
- **Task Queues are partitioned.** "By [default](https://docs.temporal.io/references/dynamic-configuration#service-level-rps-limits) each Task Queue has 4 partitions." Single-partition queues are nearly FIFO but throughput-limited; multi-partition queues assign tasks to random partitions and, once a backlog forms, "the sync match rate will drop to nearly zero because the task queue will instead dispatch tasks from the backlog." That transition from sync-match to async-match is the observable signature of a saturated Task Queue and is exactly what your `poll_success_sync_count` graph shows.

### Sticky execution and the workflow cache

[Sticky Execution](https://docs.temporal.io/sticky-execution) is the optimization that makes Temporal fast enough to be usable. After a worker picks up a Workflow Task, it keeps polling the shared queue *and* starts polling a private, auto-named **Sticky Queue**. Subsequent Workflow Tasks for that execution go to the sticky queue, and the worker serves them from its in-memory cache instead of replaying History.

The failure path is the interesting one. "If the Worker fails to start a Workflow Task in the Sticky Queue shortly after it's scheduled (within five seconds by default), the Temporal Service disables stickiness for that Workflow Execution" and reschedules on the original queue. Also, a failed Workflow Task evicts the execution from cache, invalidating stickiness.

Consequences you will meet as a platform owner:

- **A cache miss costs a full History replay.** For a 40,000-Event Workflow that is expensive in worker CPU *and* in reads against your persistence layer.
- **Rolling a customer's worker deployment invalidates every sticky cache entry it held**, producing a replay storm. So does anything that kills their pods — including, indirectly, node churn if their workers run on infrastructure that behaves like yours.
- The `sticky_cache_hit` / `sticky_cache_miss` / `sticky_cache_total_forced_eviction` metrics ([SDK metrics](https://docs.temporal.io/references/sdk-metrics)) are the customer-side diagnostic.

### Worker capacity tuning

Defaults are, in Temporal's own words, "designed for ease in development and testing, but not optimal for production" ([Worker best practices](https://docs.temporal.io/best-practices/worker)). The published defaults ([Worker tuning quick reference](https://docs.temporal.io/develop/worker-tuning-reference)):

| SDK | MaxConcurrentWorkflowTaskExecutionSize | MaxConcurrentActivityTaskExecutionSize | MaxConcurrentLocalActivityTaskExecutionSize | MaxCachedWorkflows | Workflow pollers | Activity pollers |
|---|---|---|---|---|---|---|
| Go | 1,000 | 1,000 | 1,000 | 10,000 | 2 | 2 |
| Java | 200 | 200 | 200 | 600 | 5 | 5 |
| TypeScript | 40 | 100 | 100 | dynamic (~2,000 at 4 GiB) | 10 | 10 |
| Python | 100 | 100 | 100 | 1,000 | 5 | 5 |
| .NET | 100 | 100 | 100 | 10,000 | 5 | 5 |

Those column headers are the cross-SDK names the docs table uses; they are not all Go field names. In Go the struct fields are `MaxConcurrentWorkflowTaskExecutionSize`, `MaxConcurrentActivityExecutionSize`, and `MaxConcurrentLocalActivityExecutionSize` (no "Task" in the latter two), and **the workflow cache is not a `worker.Options` field at all** — it is the process-wide `worker.SetStickyWorkflowCacheSize(10000)`, shared by every worker in the process.

The Go defaults are aggressive — 1,000 concurrent Activity slots and a 10,000-Workflow cache on a container that may have 1 vCPU and 512 MiB. Go is also the SDK most likely to be running in a small sidecar. The mismatch produces OOMKills and slot starvation, and the customer's first hypothesis is always that Temporal is broken.

Newer SDK versions supersede the `maxConcurrentXXXTask` knobs with **Worker Tuners** and **Slot Suppliers** ([Worker performance](https://docs.temporal.io/develop/worker-performance)). Three supplier kinds: fixed-size, resource-based (targets CPU and memory utilization, cgroup-aware in containers), and custom. The docs warn that "Worker tuners supersede the existing `maxConcurrentXXXTask` style Worker options. Using both styles will cause an error at Worker initialization time." **Poller Autoscaling** is now recommended for most workloads over hand-set poller counts.

The diagnostic loop Temporal publishes is worth memorizing because it is the exact conversation you will have with a customer who blames your cell:

| Schedule-to-start latency | Worker CPU/memory | Diagnosis |
|---|---|---|
| High | High | Workers saturated. Scale out, or they are blocked on Activities |
| High | Low | Workers underutilized. Increase pollers and/or slots. If `temporal_long_request_latency` or `temporal_long_request_failure` is also high, the workers cannot reach the Service — **this is the case that is actually your problem** |
| Low | Low | Possibly over-provisioned; consider scaling down |

Two more best practices that shape your load: **separate Task Queues logically** (per service, so one workload cannot starve another, with "at least two Workers to poll" each queue), and **manage scale-down safely** using `WorkerStopTimeout` so long Activities drain rather than time out.

**Why worker fleet sizing becomes a platform problem.** A customer's undersized fleet manifests as a Task Queue backlog inside *your* cell. Backlogged tasks are rows in your persistence layer, matched against by a Matching service that is now doing async-match work instead of cheap sync matches. A customer's *oversized* fleet manifests as poll pressure: Temporal Cloud caps each Namespace at "20,000 Activity pollers and 20,000 Workflow Task pollers concurrently" ([Cloud limits](https://docs.temporal.io/cloud/limits#concurrent-task-pollers)), and the docs note per-worker poller settings "do not affect the global Namespace limit." A fleet of 500 pods each configured with 50 pollers hits that ceiling and starts getting `ResourceExhausted`.

### Signals, Queries, and Updates

Three ways to talk to a running Workflow ([Workflow message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing)):

| | Signal | Query | Update |
|---|---|---|---|
| Semantics | Asynchronous write | Synchronous read | Synchronous, tracked write |
| Writes to Event History | Yes (`WorkflowExecutionSignaled`) | **No** | Yes (`...UpdateAccepted`, `...UpdateCompleted`) |
| Caller gets a result | No — fire and forget | Yes | Yes, or an error |
| Needs a live worker | No — buffered by the server | **Yes** | **Yes** |
| Can block in the handler | Yes | **No** | Yes |
| Works on closed Workflows | No | Yes, within retention (not on terminated) | No |
| Per-execution limit | 10,000 Signals | n/a | 10 in-flight, 2,000 total in History |

Go handlers:

```go
func ClusterWorkflow(ctx workflow.Context, in Input) error {
	state := newState(in)
	mu := workflow.NewMutex(ctx)

	// Query: read-only, must not block, must not mutate.
	if err := workflow.SetQueryHandler(ctx, "getNodeCount", func() (int, error) {
		return len(state.Nodes), nil
	}); err != nil {
		return err
	}

	// Update: synchronous write, with an optional validator that rejects
	// before anything is written to History.
	err := workflow.SetUpdateHandlerWithOptions(ctx, "addNode",
		func(ctx workflow.Context, req AddNode) (NodeID, error) {
			if err := mu.Lock(ctx); err != nil { // workflow.Mutex, not sync.Mutex
				return "", err
			}
			defer mu.Unlock()
			return state.add(ctx, req)
		},
		workflow.UpdateHandlerOptions{
			Validator: func(ctx workflow.Context, req AddNode) error {
				if req.Size == "" {
					return errors.New("size required")
				}
				return nil
			},
		})
	if err != nil {
		return err
	}

	// Signal: asynchronous, delivered through a channel.
	drain := workflow.GetSignalChannel(ctx, "drain")
	var req DrainRequest
	drain.Receive(ctx, &req)

	// Do not Continue-As-New or return while handlers are still running.
	return workflow.Await(ctx, func() bool { return workflow.AllHandlersFinished(ctx) })
}
```

**Update's synchronous-completion model** is the newest and most subtle of the three ([Sending messages](https://docs.temporal.io/sending-messages#sending-updates)). The caller chooses what to wait for via `WaitForStage`:

- `WorkflowUpdateStageAccepted` — returns once the worker has run the validator and the server has persisted `WorkflowExecutionUpdateAccepted`. The Update is now durable; you can fetch its result later with the handle.
- `WorkflowUpdateStageCompleted` — returns once the handler has finished and produced a value.

A **rejected** Update (validator returns an error) writes nothing to History at all; the caller just gets "Update failed." Deduplication is by Update ID, handled server-side — except across a Continue-As-New boundary, where "Update ID deduplication by the server is per Workflow run" and the customer must dedupe in their own code.

**Update-with-Start** combines starting a Workflow and sending an Update in one round trip, using `WorkflowIDConflictPolicy`. The caveat the docs flag in a warning box matters: "Unlike Signal-with-Start — Update-With-Start is *not* atomic. If the Update can't be delivered, for example, because there's no running Worker available, a new Workflow Execution will still start."

Two Go-specific hazards in the handler patterns: handlers run **interleaved** with the main Workflow function on a single deterministic scheduler, so blocking handlers need `workflow.Mutex` to avoid interleaved partial state; and Signals must be **drained asynchronously** before completing or continuing-as-new, "otherwise, the Signals will be lost" ([Go message passing](https://docs.temporal.io/develop/go/workflows/message-passing)).

### Child Workflows, Continue-As-New, and history limits

The limits, from [Workflow Execution limits](https://docs.temporal.io/workflow-execution/limits) and [Cloud limits](https://docs.temporal.io/cloud/limits), verified 2026-08-29:

| Limit | Value | Configurable on Cloud? |
|---|---|---|
| Event History length | **51,200 Events** (warn at 10,240) | No |
| Event History size | **50 MB** (warn at 10 MB) | No |
| Single payload | **2 MB** | No |
| Event History transaction | **4 MB** | No |
| gRPC message | **4 MB** | No (platform-wide) |
| Pending Activities / Child Workflows / Signals / Cancel requests, each | **2,000** per execution | Dynamic config on self-hosted (`NumPendingActivitiesLimit`, etc.) |
| Signals received per execution | **10,000** | No |
| Updates: in-flight / total in History | **10** / **2,000** | No |
| Callbacks per execution | 2,000 | `MaxCallbacksPerWorkflow` on self-hosted |
| In-flight Nexus Operations | **30** | `MaxConcurrentOperations` |
| Nexus Operation Schedule-To-Close | max **60 days** | No |
| Identifier length (Workflow ID, Type, Task Queue name) | **1,000 bytes** | No |
| Timer duration | max **100 years** on Cloud | No |

The Event History limits are enforced by **terminating** the execution ([Events](https://docs.temporal.io/workflow-execution/event#event-history-limits)). Temporal's practical guidance is much stricter than the hard limit: "We recommend not exceeding a few thousand Events in a single Workflow Execution," because replay of a huge History "affects the performance of the new Worker and may even cause timeout errors well before the hard limit of 51,200 Events is reached" ([Worker best practices](https://docs.temporal.io/best-practices/worker#manage-event-history-growth)). Similarly, "for optimal performance, limit concurrent operations to 500 or fewer."

**Continue-As-New** is the escape valve. It "completes the current Workflow instance and atomically starts a new one with the same Workflow ID" with a new Run ID and a fresh History ([Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)). In Go it is a returned sentinel error:

```go
func (cm *ClusterManager) shouldContinueAsNew(ctx workflow.Context) bool {
	if workflow.GetInfo(ctx).GetContinueAsNewSuggested() {
		return true
	}
	// A test hook so CI can exercise the CaN path without 50k events.
	return cm.maxHistoryLength > 0 &&
		workflow.GetInfo(ctx).GetCurrentHistoryLength() > cm.maxHistoryLength
}

// ... later, from the MAIN workflow function, never from a handler:
return Result{}, workflow.NewContinueAsNewError(ctx, ClusterManagerWorkflow, Input{
	State: &cm.state,
})
```

`GetContinueAsNewSuggested()` is the server telling the Workflow it is approaching trouble. The second reason to Continue-As-New has nothing to do with size: it bounds how many code versions a running execution can span, which is a versioning mitigation.

Three sharp edges:

- **Child Workflows do not carry over.** "If a Parent Workflow Execution uses Continue-As-New, any ongoing Child Workflow Executions will not be retained in the new continued instance" ([Child Workflows](https://docs.temporal.io/child-workflows)).
- **Never Continue-As-New from an Update or Signal handler.** Wait for `AllHandlersFinished` in the main function first.
- **`LastCompletionResult` is lost.** For Schedules, "if, during the subsequent run, the Workflow employs the Continue-As-New feature, `LastCompletionResult` won't be accessible" ([Schedule](https://docs.temporal.io/schedule)).

**Child Workflows** exist to partition work and to model resources, not for code organization. The docs' arithmetic: a single Workflow cannot spawn 100,000 Activities, but "a Parent Workflow Execution can spawn 1,000 Child Workflow Executions that each spawn 1,000 Activity Executions to achieve a total of 1,000,000." The counter-constraint: "a single Parent should not spawn more than 1,000 Child Workflow Executions," because the parent's History records each child's status. And the general advice: "Child Workflow Executions result in more overall Events recorded in Event Histories than Activities. ... **When in doubt, use an Activity.**"

The `ABANDON` [Parent Close Policy](https://docs.temporal.io/parent-close-policy) is a common source of orphans: children configured to abandon survive the parent's close, and continue consuming your cell's resources with nobody watching them.

### Timers, Schedules, and Cron

Timers are durable server-side state, not sleeping threads; `workflow.Sleep` survives worker death and, on Cloud, can run up to 100 years.

**Schedules** are the modern recurring-execution primitive and are recommended over Cron Jobs ([Schedule](https://docs.temporal.io/schedule)). A Schedule is an independent entity with an identity, unlike a Cron Schedule which is a property of a Workflow Execution. Two spec kinds: an interval (`45m`, `6h/5h`) or a calendar expression (cron string or a JSON object with `year`/`month`/`dayOfMonth`/`dayOfWeek`/`hour`/`minute`/`second`).

The pieces that matter operationally:

| Feature | Behavior | Why you care |
|---|---|---|
| Overlap Policy | `Skip` (default), `BufferOne`, `BufferAll`, `CancelOther`, `TerminateOther`, `AllowAll` | `AllowAll` is the only one permitting concurrent runs. `BufferAll` after an outage means a thundering herd |
| Catchup Window | Default **1 year**, minimum 10 seconds | After *your* outage, a customer's Schedule with the default window fires every missed action. This is a stampede aimed at your cell |
| Jitter | Random offset up to a max, added per action | The docs explicitly ask customers to use it: "don't schedule all your Workflow Executions to start at the same time" |
| Pause / Backfill | Stop future actions; replay a time range | Backfill with `AllowAll` runs in parallel |
| Rate limit | **10 schedule requests per second** per Namespace on Cloud | ([Cloud limits](https://docs.temporal.io/cloud/limits#schedules-rate-limit)) |
| Implementation | "Internally, a Schedule is implemented as a Workflow" | Schedules consume the same machinery they orchestrate |
| Listing | `ListSchedules`/`CountSchedules` are Visibility calls, eventually consistent, sharing the 30 calls/sec Visibility rate limit | A customer polling their schedule list can exhaust their own Visibility budget |

**Cron Jobs** are legacy but everywhere. Cron Schedules are UTC by default, use `robfig/cron` syntax including `@daily` and `@every <duration>`, and support `CRON_TZ=America/New_York` prefixes. The time-zone warnings in the docs are severe and worth repeating to customers: around a DST transition a job "might run zero, one, or two times in a day," the library "does not do any special handling of DST transitions," and the next run's absolute time "is computed and stored in the database when the previous Run completes, and is not recomputed." For a self-hosted service, *you* are responsible for `tzdata` currency — a genuinely load-bearing fact for anyone building base images.

### Namespaces: the tenancy unit

A [Namespace](https://docs.temporal.io/namespaces) provides Workflow ID uniqueness, resource isolation ("heavy traffic from one Namespace will not impact other Namespaces running on the same Temporal Service"), and configuration boundaries — Retention Period and Archival destination are per-Namespace. Note that a Namespace is itself still multi-tenant: teams sharing one must coordinate on Workflow ID and Task Queue names.

Temporal Cloud specifics ([Cloud Namespaces](https://docs.temporal.io/cloud/namespaces)):

- **Naming**: 2–39 characters, lowercase, starts with a letter, ends with a letter or number, hyphens allowed. Immutable after provisioning.
- **Namespace ID** is `<namespace-name>.<account-id>`, e.g. `accounting-production.f45a2`. The Account ID is a short alphanumeric string of at least five characters.
- **Endpoints**: the recommended **Namespace endpoint** `<namespace>.<account>.tmprl.cloud:7233` always resolves to the Namespace regardless of region; the **regional endpoint** `<region>.<cloud_provider>.api.temporal.io:7233` pins to a region and requires clients using mTLS to set `server_name` to the Namespace endpoint value for SNI.
- **Auth**: [API keys](https://docs.temporal.io/cloud/api-keys) *or* [mTLS](https://docs.temporal.io/cloud/certificates), with both on one Namespace in pre-release. API keys max out at a 2-year expiry, 10 non-expired per user and 20 per Service Account, with expiry emails at 30/20/10 days.
- **Retention**: default **30 days** on Cloud, settable 1–90 days. Closed Workflow Histories "remain in Temporal storage until the user-defined retention period expires."
- **Deletion**: immediate removal of Workflow Executions and Task Queues, permanent, with an optional **Deletion Protection** toggle.
- **Certificates**: 32 KB or 16 certificates per Namespace, whichever comes first.
- **Tags**: up to 10 key-value pairs per Namespace, keys and values 1–63 characters.
- **Account defaults**: 300 users, 5 Projects, 10 Namespaces (auto-increasing as you create them), 25 Custom Roles.

The DNS warning in the docs is directly relevant to how you build ingress: "In general, the IP addresses that Temporal Cloud endpoints resolve to are subject to change without notice." Guaranteed resolution behavior exists only with [Stable IPs](https://docs.temporal.io/cloud/connectivity/ip-addresses) or with High Availability plus Private Connectivity, where the Namespace endpoint resolves to a regional intermediary `<provider>-<region>.region.tmprl.cloud` that customers override in their own private DNS zone.

**Archival** copies closed Event Histories and Visibility records to blob storage ([Archival](https://docs.temporal.io/temporal-service/archival)). It runs asynchronously after a randomized delay, up to 5 minutes by default, capped by retention. Two caveats: it is "considered **experimental** and not subject to normal versioning and support policy," and it is not supported when running Temporal through Docker and disabled by default in the Helm charts. For Cloud, the docs say to contact your Temporal representative.

### Error handling, sagas, and compensation

Temporal splits failures into **platform failures** (worker crashes, network partitions — handled transparently by forward recovery) and **application failures** (your code's errors, which "do not resolve on their own through retries alone" and often need **backward recovery**) ([Application failures](https://docs.temporal.io/encyclopedia/application-failures)).

The typed failure taxonomy: **Application Failure** (the only one you create), **Activity Failure** (wraps the Activity's error in `cause`), **Child Workflow Failure**, **Timeout Failure**, **Cancelled Failure**, **Terminated Failure**, **Server Failure**.

The single most dangerous behavior in this area, and the one a senior engineer should be able to quote:

> "The SDK inspects the **outermost** error to decide how to represent the failure. ... wrapping an Application Failure in a generic language error silently loses the `non_retryable` flag."

In Go this means `fmt.Errorf("charging card: %w", nonRetryableErr)` **converts your non-retryable error into a retryable one**, and the Activity retries forever. The Temporal Service "only inspects the top-level `failure_info` ... [it] does not look at `cause` to determine retryability." Add context by wrapping in another Application Failure, never in `fmt.Errorf`.

Go-specific: "returning an error behaves like an Application Failure in the other SDKs. Panics behave like non-Application Failure exceptions ... in that they cause a Workflow Task Failure" ([Handling messages](https://docs.temporal.io/handling-messages#errors-and-panics-in-message-handlers-in-the-go-sdk)).

**The saga pattern** is just control flow, because durable execution makes it so ([Saga Pattern](https://docs.temporal.io/design-patterns/saga-pattern)):

```go
func OpenAccountWorkflow(ctx workflow.Context, req OpenAccountRequest) error {
	var compensations []func()
	runCompensations := func() {
		for i := len(compensations) - 1; i >= 0; i-- {
			compensations[i]()
		}
	}

	if err := workflow.ExecuteActivity(ctx, CreateAccount, req).Get(ctx, nil); err != nil {
		return err
	}

	// Register BEFORE execution: the forward Activity may have had partial
	// effects before it failed. The compensation must be idempotent and must
	// no-op when the forward action never happened.
	compensations = append(compensations, func() {
		_ = workflow.ExecuteActivity(ctx, ClearPostalAddresses, req).Get(ctx, nil)
	})
	if err := workflow.ExecuteActivity(ctx, AddAddress, req).Get(ctx, nil); err != nil {
		runCompensations()
		return err
	}
	return nil
}
```

Temporal's best practices for compensation, condensed: make every compensation idempotent; register before execution; use idempotency keys on forward Activities; set `StartToCloseTimeout` on compensations but **avoid** `ScheduleToCloseTimeout` so they retry until they succeed; use `workflow.NewDisconnectedContext` so compensation survives Workflow cancellation; keep compensation payloads small (references, not objects, to stay under 2 MB); log compensation failures and continue rather than aborting the rollback; and re-throw the original error afterward so the Workflow reports the right cause.

### Testing: the test framework, time skipping, replay

The Go SDK ships a test framework built on `testify` ([Go testing suite](https://docs.temporal.io/develop/go/best-practices/testing-suite)). The high-leverage capabilities:

**Automatic time skipping.** `testsuite.TestWorkflowEnvironment` "automatically skips time when possible. ... time advances automatically whenever there are no Activities running." A Workflow that sleeps 90 days is tested in under a second. Time is a *global* property of the environment, so tests with different time behavior must not run concurrently against the same instance.

```go
func TestSleepForDaysWorkflow(t *testing.T) {
	ts := &testsuite.WorkflowTestSuite{}
	env := ts.NewTestWorkflowEnvironment()

	calls := 0
	env.RegisterActivity(SendEmailActivity)
	env.OnActivity(SendEmailActivity, mock.Anything, mock.Anything).
		Run(func(mock.Arguments) { calls++ }).Return(nil)

	start := env.Now()
	env.RegisterDelayedCallback(func() {
		require.Equal(t, 3, calls)
		env.SignalWorkflow("complete", nil)
		require.Equal(t, time.Hour*24*90, env.Now().Sub(start))
	}, time.Hour*24*90)

	env.ExecuteWorkflow(SleepForDaysWorkflow)
}
```

**Replay tests against production histories.** This is the practice that separates teams that survive deploys from teams that do not. The docs' recommended CI check:

1. Determine which Workflow Types or Task Queues the worker under test targets.
2. Download the Event Histories of a representative set of recent open and closed Workflows.
3. Run them through replay.
4. Fail CI on any replay error.

```go
func TestReplayProductionHistories(t *testing.T) {
	replayer := worker.NewWorkflowReplayer()
	replayer.RegisterWorkflow(ChargeWorkflow)

	paths, _ := filepath.Glob("testdata/histories/*.json")
	for _, p := range paths {
		t.Run(filepath.Base(p), func(t *testing.T) {
			require.NoError(t, replayer.ReplayWorkflowHistoryFromJSONFile(nil, p))
		})
	}
}
```

Histories come from `client.GetWorkflowHistory` or from `temporal workflow show --output json`. Replay "is a good way to see exactly what code path was taken for given input and events."

### The SDK family, and where Go sits

Eight official SDKs, plus community ones ([About Temporal SDKs](https://docs.temporal.io/encyclopedia/architecture/temporal-sdks)):

| SDK | Implementation lineage | Notable characteristics |
|---|---|---|
| **Go** | Native. Its own state machine; PHP's metrics are also "defined in the Go SDK" ([SDK metrics](https://docs.temporal.io/references/sdk-metrics)) | Coroutine-based deterministic scheduler; `workflow.Context`, `workflow.Channel`, `workflow.Selector` shadow the stdlib. `SideEffect` available. Highest default concurrency of any SDK. Ships `workflowcheck` static analyzer. Latest release [v1.48.0](https://pkg.go.dev/go.temporal.io/sdk), 18 Aug 2026 |
| **Java** | Native, independent implementation | Interface + `@WorkflowInterface` annotation style; built-in `Saga` helper class; thread-based, so `MaxWorkflowThreadCount` (default 600) is a real constraint. `SideEffect` available |
| **TypeScript** | Core (Rust) | Runs Workflows in a V8 isolate — the sandbox is enforced by the runtime, not by convention. `reuseV8Context` changes the memory profile. Lowest default Workflow-task concurrency (40) |
| **Python** | Core (Rust) | `@workflow.defn` / `@workflow.run`; sandbox via module reloading; `patched()` semantics as documented on the [Patching](https://docs.temporal.io/patching) page |
| **.NET** | Core (Rust) | `[Workflow]` / `[WorkflowRun]`; note the documented quirk that changing a Timer to or from `-1` ("infinite") is non-deterministic |
| **Ruby** | Core (Rust) | Newer; Worker Versioning from [v0.5.0](https://github.com/temporalio/sdk-ruby/releases/tag/v0.5.0) |
| **PHP** | Metrics defined in the Go SDK, indicating a Go-hosted worker runtime | Attribute-based interface style |
| **Rust** | Core itself | Newest; `#[workflow]` macro style |

Third-party, explicitly unsupported: [Swift](https://github.com/apple/swift-temporal-sdk) (Apple), [Haskell](https://github.com/MercuryTechnologies/hs-temporal-sdk), [Clojure](https://github.com/manetu/temporal-clojure-sdk), [Scala](https://github.com/vitaliihonta/zio-temporal).

Where Go sits, and why you should care: Go is the SDK Temporal itself is written in, the one the server's internal workers use, and the one you will read most often when debugging a customer's problem — and, per the sibling guide, the one your own cell-lifecycle Workflows will be written in. It also has the loosest sandbox: unlike TypeScript's V8 isolate or Python's module reloading, nothing in Go *prevents* a customer from calling `time.Now()` inside a Workflow. The Go SDK relies on discipline plus `workflowcheck` plus replay tests. That is why Go customers hit non-determinism errors that TypeScript customers do not.

One unit gotcha across SDKs: histogram metrics are in **seconds** for Go and Java, **milliseconds** for the Core-based SDKs. Dashboards get built wrong because of this.

### Observability from the customer's side

**Web UI.** Ships with every [Temporal CLI](https://docs.temporal.io/cli) release and is available in Cloud ([Web UI](https://docs.temporal.io/web-ui)). The parts customers actually use: the Workflows list with [List Filters](https://docs.temporal.io/list-filter) and Saved Views (up to 20, stored per-browser); the History tab with Timeline / All / Compact / JSON views and a full JSON download; Pending Activities showing current attempt and heartbeat state; the **Call Stack** tab, which issues a live `__stack_trace` Query and "shows each location where Workflow code is waiting"; the Workers tab showing who is polling the Task Queue; and the Task Failures Saved View.

**CLI.** `temporal` is the single binary; it embeds a dev server and the Web UI. The commands that matter in an incident:

```bash
temporal workflow list --query "ExecutionStatus = 'Running' AND WorkflowType = 'ChargeWorkflow'"
temporal workflow describe --workflow-id order-1234
temporal workflow show --workflow-id order-1234 --output json > history.json
temporal workflow query --workflow-id order-1234 --type __stack_trace
temporal task-queue describe --task-queue billing
temporal workflow reset --workflow-id order-1234 --event-id 42 --reason "bad deploy"
```

**Event History as a debugging artifact.** This is the genuinely differentiated thing. Roughly 40 Event types exist ([Events reference](https://docs.temporal.io/references/events)); the History is an append-only, ordered, complete record of everything that happened, downloadable as JSON, and replayable locally against any version of the code. "Replaying a Workflow Execution locally is a good way to see exactly what code path was taken for given input and events." No log aggregation system gives you that.

Related: a **[Reset](https://docs.temporal.io/workflow-execution/event#reset)** terminates an execution and creates a new one with History copied up to a chosen reset point (valid points are `WorkflowTaskStarted`, `WorkflowTaskCompleted`, `WorkflowTaskTimedOut`, `WorkflowTaskFailed`), optionally re-applying Signals. This is the "un-break my Workflows after a bad deploy" tool, and a customer resetting ten thousand Workflows is a large, sudden write burst against your cell.

**Worker metrics.** The [SDK metrics reference](https://docs.temporal.io/references/sdk-metrics) is the authoritative list. The ones that carry the most signal:

| Metric | Type | What it tells you |
|---|---|---|
| `temporal_workflow_task_schedule_to_start_latency` | Histogram | Workflow Tasks queueing — worker fleet too small or too few pollers |
| `temporal_activity_schedule_to_start_latency` | Histogram | Same, for Activities. The canonical undersized-fleet signal |
| `temporal_worker_task_slots_available` / `_used` | Gauge | Slot exhaustion, tagged by `worker_type` |
| `temporal_sticky_cache_hit` / `_miss` / `_total_forced_eviction` | Counter | Cache thrash and replay cost |
| `temporal_num_pollers` | Gauge | Tagged by `poller_type` |
| `temporal_workflow_task_execution_failed` | Counter | Tagged `failure_reason`: `NonDeterminismError`, `GrpcMessageTooLarge`, `PayloadsTooLarge`, `WorkflowError`. **This is the deploy-broke-everything alarm** |
| `temporal_request_failure` / `temporal_long_request_failure` | Counter | Tagged `status_code`; `RESOURCE_EXHAUSTED` here means throttling |
| `temporal_request_resource_exhausted` (Go only) | Counter | Tagged `cause`, e.g. `RESOURCE_EXHAUSTED_CAUSE_RPS_LIMIT`, `..._CONCURRENT_LIMIT`, `..._SYSTEM_OVERLOADED`, `..._CIRCUIT_BREAKER_OPEN`. The docs say to "prefer this metric over `request_failure` when investigating throttling" |
| `temporal_workflow_continue_as_new` | Counter | Continue-As-New rate — proxy for entity-Workflow churn |

Cloud additionally exposes `temporal_cloud_v1_poll_success_sync_count` (sync-match rate), `temporal_cloud_v1_approximate_backlog_count`, and the limit gauges `temporal_cloud_v1_action_limit`, `temporal_cloud_v1_service_request_limit`, `temporal_cloud_v1_operations_limit`. `DescribeTaskQueue` returns `ApproximateBacklogCount`, `ApproximateBacklogAge`, `TasksAddRate`, `TasksDispatchRate`, and `BacklogIncreaseRate`.

### Data converters and codec servers

Payloads are serialized by a [Data Converter](https://docs.temporal.io/dataconversion) into a `Payload` (binary data plus metadata). A [Payload Codec](https://docs.temporal.io/payload-codec) then does bytes-to-bytes transformation — compression, or encryption. Crucially, "Payload Codecs do not operate within the Workflow sandbox," which is what allows them to call remote KMS.

With encryption enabled, "data exists unencrypted only on the Client and the Worker process ... on hosts that you control." Which creates the obvious problem: the Web UI shows ciphertext. The fix is a **[Codec Server](https://docs.temporal.io/codec-server)** — a customer-operated HTTPS service exposing `/decode`, `/encode`, and optionally `/download`, which the Web UI and CLI call to render payloads ([Codecs and encryption](https://docs.temporal.io/production-deployment/data-encryption)).

The mechanics you will be asked about:

- CORS must allow `Access-Control-Allow-Origin: https://cloud.temporal.io`, methods `POST, GET, OPTIONS`, and headers `X-Namespace, Content-Type` (plus `Authorization` if used).
- Requests carry `X-Namespace: {namespace}` and, optionally, a JWT that the customer verifies against Temporal's JWKS at `https://login.tmprl.cloud/.well-known/jwks.json`. The token carries the requesting user's email, so the customer can implement their own per-user authorization.
- The endpoint is configured **per-Namespace** on Cloud (requires Namespace Admin) and can be overridden per-browser. The CLI takes the global flags `--codec-endpoint` and `--codec-auth`, or you persist them per environment with `temporal env set --env <name> --key codec-endpoint --value <url>` (`env set` accepts only `--key`/`--value`, not the codec flags directly).
- "Expect the Codec Server to receive multiple requests per Workflow Execution."
- A codec server reachable only on `localhost` is explicitly called "a legitimate security pattern."
- Failure messages and stack traces are **not** codec-encoded by default; enabling that requires a custom [Failure Converter](https://docs.temporal.io/failure-converter).
- **Search Attributes are never encrypted.** "The Temporal Server must be able to read these values in plain text to support filtering and ordering, so encryption is not possible without breaking search functionality." Customers put PII there anyway.

### Temporal Cloud specifics

Everything here is publicly documented; I do not have and do not speculate about internals.

**Availability tiers** ([High Availability](https://docs.temporal.io/cloud/high-availability)):

| Tier | Replication | SLA | RPO | RTO |
|---|---|---|---|---|
| Standard Namespace | Three Availability Zones; writes acknowledged only after all three persist | 99.9% | n/a | n/a |
| Multi-region Replication | Two regions on the same continent | 99.99% including cloud provider outages | Sub-1-minute | 20 minutes, automatic |
| Multi-cloud Replication | Two different cloud providers; replicated data encrypted over the public internet | 99.99% | Sub-1-minute | 20 minutes, automatic |
| **Same-region Replication** | "Temporal operates a 'cell architecture' and will replicate the Namespace across multiple cells in that region." Public Preview in selected regions | — | — | Automatic only; manual failover not available |

**What a customer sees during a failover.** The docs describe this in enough detail to answer the question honestly. A replicated Namespace has one active and one passive replica; the active accepts reads and writes, the passive receives state asynchronously. On failover, "DNS reroutes your Namespace Endpoint to the active region," so clients and workers using the Namespace endpoint need no change. Requests that land on the passive replica are **forwarded** to the active one — with a cross-region hop and therefore higher latency. Worker poll forwarding can be disabled (Active/Hot-Passive); Client requests (Start, Signal, Query, Cancel, Terminate) "are always forwarded" regardless. If the regions were not fully in sync, "Temporal's conflict resolution process reconciles discrepancies."

For Same-region Replication specifically — the one most directly about cells — "Failovers between cells are always managed automatically by Temporal. Unlike Multi-region and Multi-cloud Replication, you cannot disable automatic failovers and you cannot trigger a manual failover." From the customer's seat, a cell failover is meant to be invisible: same endpoint, brief elevated latency, workflows keep running.

**Regions.** Cloud operates in AWS (`us-east-1`, `us-east-2`, `us-west-2`, `ca-central-1`, `eu-central-1`, `eu-west-1`, `eu-west-2`, `ap-northeast-1`, `ap-northeast-2`, `ap-south-1`, `ap-south-2`, `ap-southeast-1`, `ap-southeast-2`, `sa-east-1`) and GCP (`us-central1`, `us-west1`, `us-east4`, `europe-west3`, `asia-south1`, `asia-southeast2`) ([Service regions](https://docs.temporal.io/cloud/regions)). Same-region Replication is currently listed as available in `aws-us-east-1`, `aws-us-west-2`, and `aws-ap-southeast-2`. Multi-cloud pairs are enumerated per region. Notably, **Azure does not appear in the public regions list** — worth holding as a fact while you build Azure cell tooling.

**Throughput and throttling** ([Cloud limits](https://docs.temporal.io/cloud/limits)):

| Limit | Value |
|---|---|
| Actions per second (APS) | Default floor 500; scales with On-Demand Capacity based on 7-day usage, or fixed by Temporal Resource Units under Provisioned Capacity |
| Requests per second / Operations per second | Dynamic, capacity-mode dependent |
| Visibility API calls | 30/sec, **not configurable** |
| Schedule requests | 10/sec |
| Concurrent pollers | 20,000 Activity + 20,000 Workflow Task, per Namespace |
| Per-primitive ID reuse | 1 new Execution per second per ID, with burst — counts Continue-As-New, resets, and `SignalWithStart` |
| Batch jobs | 1 concurrent per Namespace, 50 executions/sec |
| Custom Search Attributes | Bool/Datetime/Double/Int 20 each; Keyword 40; KeywordList 5; Text 5. Names ≤64 chars |

Throttling behavior is priority-based: "Low-priority operations are throttled first. Higher-priority operations like `StartWorkflowExecution`, `SignalWorkflowExecution`, and `UpdateWorkflowExecution` continue to go through when possible," using "similar throttling priorities as the [open source server](https://github.com/temporalio/temporal/blob/main/service/frontend/configs/quotas.go)." Throttled requests get `ResourceExhausted` and SDKs retry, but "if throttling persists beyond the SDK's retry limit, client calls fail. This means work *can* be lost."

---

## Hands-on

Roughly 45 minutes. You will run a local server, write and run a Workflow, deliberately break determinism, watch a real replay failure, and fix it with a patch.

### Prerequisites

- **Go 1.24 or later.** The Go SDK's `go.mod` requires it as of [v1.48.0](https://pkg.go.dev/go.temporal.io/sdk). Check with `go version`.
- **Temporal CLI.** `brew install temporal` on macOS or Linux, or download from `https://temporal.download/cli/archive/latest?platform=<os>&arch=<arch>` and put `temporal` on your PATH ([Run a development server](https://docs.temporal.io/develop/run-a-development-server)).
- Ports **7233** (gRPC) and **8233** (Web UI) free.
- No Docker, no database. The dev server is a single process using in-memory SQLite.

### 1. Start the dev server

```bash
temporal server start-dev
```

This "automatically starts the Web UI, creates the default Namespace, and uses an in-memory database." Server on `localhost:7233`, UI on `http://localhost:8233`. Leave it running. Use `--ui-port 8080` if 8233 is taken, and `--db-filename ./temporal.db` if you want state to survive a restart.

### 2. Scaffold the project

```bash
mkdir -p ~/temporal-lab && cd ~/temporal-lab
go mod init lab
go get go.temporal.io/sdk@latest
go get github.com/stretchr/testify@latest   # for the replay test in step 5
```

The module is named `lab` and `lab.go` lives at the module root, so `worker/main.go` imports it as plain `"lab"`. Run `go mod tidy` after you have written both files.

### 3. Write the Workflow and Activity

`lab.go`:

```go
package lab

import (
	"context"
	"fmt"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/workflow"
)

const TaskQueue = "lab"

func Greet(ctx context.Context, name string) (string, error) {
	activity.GetLogger(ctx).Info("greeting", "name", name)
	return fmt.Sprintf("Hello %s", name), nil
}

func Farewell(ctx context.Context, name string) (string, error) {
	return fmt.Sprintf("Goodbye %s", name), nil
}

func LabWorkflow(ctx workflow.Context, name string) (string, error) {
	ao := workflow.ActivityOptions{StartToCloseTimeout: 10 * time.Second}
	ctx = workflow.WithActivityOptions(ctx, ao)

	var hello string
	if err := workflow.ExecuteActivity(ctx, Greet, name).Get(ctx, &hello); err != nil {
		return "", err
	}

	// A long durable timer. This is the window in which we will deploy a
	// breaking change while the execution is suspended.
	if err := workflow.Sleep(ctx, 2*time.Minute); err != nil {
		return "", err
	}

	return hello, nil
}
```

`worker/main.go`:

```go
package main

import (
	"log"

	"lab"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
)

func main() {
	c, err := client.Dial(client.Options{})
	if err != nil {
		log.Fatalln("dial:", err)
	}
	defer c.Close()

	w := worker.New(c, lab.TaskQueue, worker.Options{})
	w.RegisterWorkflow(lab.LabWorkflow)
	w.RegisterActivity(lab.Greet)
	w.RegisterActivity(lab.Farewell)

	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalln("worker:", err)
	}
}
```

### 4. Run it

In a second terminal:

```bash
go run ./worker
```

In a third:

```bash
temporal workflow start \
  --task-queue lab \
  --type LabWorkflow \
  --workflow-id lab-1 \
  --input '"Zhihao"'
```

Open `http://localhost:8233`, click into `lab-1`, and read the History. You should see `WorkflowExecutionStarted`, `WorkflowTaskScheduled/Started/Completed`, `ActivityTaskScheduled`, then — once the Activity finishes — `ActivityTaskStarted` and `ActivityTaskCompleted`, then `TimerStarted`. The execution is now suspended on a durable Timer with the worker idle. Note that `ActivityTaskStarted` appeared only at the end; that is the noise-reduction behavior described earlier.

### 5. Break determinism on purpose

While `lab-1` is still sleeping, edit `LabWorkflow` to insert a second Activity **before** the timer:

```go
	var hello string
	if err := workflow.ExecuteActivity(ctx, Greet, name).Get(ctx, &hello); err != nil {
		return "", err
	}

	// NEW: inserted before the timer. This is the breaking change.
	var bye string
	if err := workflow.ExecuteActivity(ctx, Farewell, name).Get(ctx, &bye); err != nil {
		return "", err
	}

	if err := workflow.Sleep(ctx, 2*time.Minute); err != nil {
		return "", err
	}
	return hello + " / " + bye, nil
```

Stop the worker (Ctrl-C) and restart it with the new code. When the Timer fires, the worker replays: it re-executes `Greet` from History, then emits a `ScheduleActivityTask` Command for `Farewell` where History says `TimerStarted`. That is the exact scenario the docs walk through — "The first Command the Worker sees would be ScheduleActivityTask Command, which wouldn't match up to the expected TimerStarted Event."

Watch it happen:

```bash
temporal workflow describe --workflow-id lab-1
```

You will see the Workflow still **Running**, with a failed pending Workflow Task. In the Web UI it appears in the Task Failures view. In the worker log you get a non-determinism error naming the mismatched Command. Crucially: **nothing was lost.** The execution is wedged, retrying with backoff (capped at a 10-minute interval), waiting for you to fix the code.

If you want the failure without waiting two minutes, capture the history and replay it locally — this is the CI check from the testing section:

```bash
temporal workflow show --workflow-id lab-1 --output json > history.json
```

```go
func TestReplay(t *testing.T) {
	r := worker.NewWorkflowReplayer()
	r.RegisterWorkflow(lab.LabWorkflow)
	err := r.ReplayWorkflowHistoryFromJSONFile(nil, "history.json")
	require.NoError(t, err) // fails loudly with the new code, passes with the old
}
```

### 6. Fix it with a patch

Wrap the new Activity in a version gate, exactly as [the Go versioning docs](https://docs.temporal.io/develop/go/workflows/versioning#patching) prescribe:

```go
	var hello string
	if err := workflow.ExecuteActivity(ctx, Greet, name).Get(ctx, &hello); err != nil {
		return "", err
	}

	out := hello
	v := workflow.GetVersion(ctx, "add-farewell", workflow.DefaultVersion, 1)
	if v != workflow.DefaultVersion {
		var bye string
		if err := workflow.ExecuteActivity(ctx, Farewell, name).Get(ctx, &bye); err != nil {
			return "", err
		}
		out = hello + " / " + bye
	}

	if err := workflow.Sleep(ctx, 2*time.Minute); err != nil {
		return "", err
	}
	return out, nil
```

Restart the worker. Two things now happen, and you should verify both:

- **`lab-1` recovers and completes.** Because its History has no marker for `add-farewell`, `GetVersion` returns `DefaultVersion`, the new branch is skipped, and the Command sequence matches. `temporal workflow describe --workflow-id lab-1` eventually shows `Completed` with result `"Hello Zhihao"`. It never restarted; it resumed.
- **A new execution takes the new path.** Start `lab-2` the same way. Its History gains a `MarkerRecorded` Event for `add-farewell`, both Activities run, and the result is `"Hello Zhihao / Goodbye Zhihao"`.

Then confirm the cleanup query works:

```bash
temporal workflow list --query "ExecutionStatus = 'Running' AND TemporalChangeVersion IS NULL"
```

When that returns nothing, no running execution predates the patch, and you could raise `minSupported` to `1`. That query is the whole reason `TemporalChangeVersion` exists, and running it once by hand makes the versioning lifecycle concrete in a way that reading about it does not.

### 7. Optional: watch a backlog form

Stop the worker entirely and start twenty Workflows. Then:

```bash
temporal task-queue describe --task-queue lab
```

You are looking at `ApproximateBacklogCount` and `ApproximateBacklogAge` climbing with zero pollers. Restart the worker and watch them drain. This is the single most common customer-side production symptom, reproduced in thirty seconds.

---

## Production gotchas

Numbered, specific, each traceable to a primary source.

**1. Schedule-To-Start is a trap, and it is non-retryable.** Customers set it defensively, then a deploy or an autoscaling lag delays worker availability past the window and Activities fail permanently — because "This timeout is non-retryable by design. It **does not** trigger any retries regardless of the Retry Policy, as a retry would place the Activity Task back into the same Task Queue." Temporal's own advice is to leave it unset and alert on `temporal_activity_schedule_to_start_latency` instead. ([Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures#schedule-to-start-timeout))

**2. Omitting Start-To-Close makes worker crashes invisible.** "The Temporal Server doesn't detect failures when a Worker loses communication with the Server or crashes. Therefore, the Temporal Server relies on the Start-To-Close Timeout to force Activity retries." Without it and without a Heartbeat Timeout, a crashed worker leaves an Activity hanging until the Schedule-To-Close Timeout, which defaults to infinity. ([Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures#start-to-close-timeout))

**3. Non-determinism errors after a deploy wedge, not kill — and generate sustained load.** A Workflow Task failure "is retried, automatically" while "Workflow state [is] preserved." Ten thousand affected executions retry indefinitely against your cell until someone ships a fix or resets them. Watch `temporal_workflow_task_execution_failed{failure_reason="NonDeterminismError"}`. ([Application failures](https://docs.temporal.io/encyclopedia/application-failures#workflow-task-failures-vs-workflow-execution-failures))

**4. Wrapping an error in `fmt.Errorf` silently makes it retryable.** "The Temporal Service only inspects the top-level `failure_info` ... [it] does not look at `cause` to determine retryability." A non-retryable Application Failure re-wrapped by a well-meaning `%w` becomes an infinite retry loop against a permanently broken downstream. ([Application failures](https://docs.temporal.io/encyclopedia/application-failures#the-outermost-error-type-determines-retryability))

**5. Oversized payloads behave differently depending on SDK version.** On Go SDK ≥1.43.0 and Python ≥1.23.0, the SDK "fails the Workflow Task with cause `WORKFLOW_TASK_FAILED_CAUSE_PAYLOADS_TOO_LARGE`" and the Workflow stays open so you can fix it. On older SDKs, an oversized *input* causes the Service to reject the command and **terminate** the Workflow. Same customer mistake, catastrophically different outcome. ([Blob size limit errors](https://docs.temporal.io/troubleshooting/blob-size-limit-error))

**6. The 4 MB gRPC limit fires even when every payload is under 2 MB.** "Scheduling several Activities with moderate-sized inputs, or hundreds of Activities with tiny inputs in the same Workflow Task can push the combined request past 4 MB." When that happens on a Workflow Task, the Service **terminates** the execution — "because replay would produce the same oversized request on every attempt." On an Activity Task, the Activity "gets stuck in a retry loop" succeeding each time and failing to deliver, until Schedule-To-Close expires — or forever if it is unset. ([Blob size limit errors](https://docs.temporal.io/troubleshooting/blob-size-limit-error#grpc-message-size-limit))

**7. Task Queue backlog flips the queue from sync-match to async-match.** "Once a task queue builds up a backlog, the sync match rate will drop to nearly zero because the task queue will instead dispatch tasks from the backlog first." That transition is a step change in persistence load in your cell, not a gradual one. Track `temporal_cloud_v1_poll_success_sync_count` against `approximate_backlog_count`. ([Task Queues](https://docs.temporal.io/task-queue#task-ordering))

**8. Poller starvation has two opposite causes, distinguishable only by worker resource metrics.** High schedule-to-start with high CPU means saturation (scale out); high schedule-to-start with low CPU means too few pollers or slots — and if it comes with high `temporal_long_request_latency` or `temporal_long_request_failure`, "your Workers are struggling to reach the Temporal Service," which is genuinely your problem, not theirs. Never diagnose from schedule-to-start alone. ([Worker best practices](https://docs.temporal.io/best-practices/worker#interpret-metrics-as-a-whole))

**9. The 2,000-pending-operations limit fails Workflow Tasks, it does not raise a clean error.** With 2,000 incomplete Activities, Child Workflows, Signals, or cancel requests, "the Workflow Task Execution will fail and get retried" when the next one is scheduled. The customer sees an inexplicable stall, not a quota message. The advisory limit is far lower: "for optimal performance, limit concurrent operations to 500 or fewer." ([Workflow Execution limits](https://docs.temporal.io/workflow-execution/limits))

**10. Continue-As-New silently drops Child Workflows and `LastCompletionResult`.** "If a Parent Workflow Execution uses Continue-As-New, any ongoing Child Workflow Executions will not be retained." And for scheduled Workflows, a run that continues-as-new is marked `Continued-As-New`, not `Completed`, so the next scheduled run cannot read its result. Both are quiet data-loss bugs in customer code. ([Child Workflows](https://docs.temporal.io/child-workflows), [Schedule](https://docs.temporal.io/schedule))

**11. Signals not drained before completion are lost.** "Before completing the Workflow or using Continue-As-New, make sure to do an asynchronous drain on the Signal channel. Otherwise, the Signals will be lost." There is no error and no warning; the Signal simply never happened. ([Go message passing](https://docs.temporal.io/develop/go/workflows/message-passing#signals))

**12. Update-with-Start is not atomic.** "If the Update can't be delivered, for example, because there's no running Worker available, a new Workflow Execution will still start." Customers use it as a lazy-init primitive and get orphaned empty Workflows during worker outages. ([Sending messages](https://docs.temporal.io/sending-messages#update-with-start))

**13. Search Attributes are stored unencrypted and are not codec-processed.** "The Temporal Server must be able to read these values in plain text to support filtering and ordering, so encryption is not possible without breaking search functionality." A customer with a codec server may reasonably but wrongly assume Search Attributes are covered. They are not, and the docs flag GDPR/HIPAA/SOC 2 exposure explicitly. ([Search Attributes](https://docs.temporal.io/search-attribute))

**14. A codec server is on the critical path for the Web UI and gets more traffic than people expect.** "Expect the Codec Server to receive multiple requests per Workflow Execution." A single-replica codec server behind a browser rendering a 20,000-event History is a self-inflicted outage of the customer's own debugging tooling — precisely when they need it. ([Codecs and encryption](https://docs.temporal.io/production-deployment/data-encryption))

**15. A Schedule's default Catchup Window is one year.** After any outage — including yours — Schedules with the default window fire every missed action on recovery. Combined with `BufferAll` this is a stampede. Temporal asks customers to use jitter for exactly this reason. ([Schedule](https://docs.temporal.io/schedule#catchup-window))

**16. Cron with a time zone can run zero, one, or two times on a DST day.** "The Cron library that we use does not do any special handling of DST transitions." And the next run's absolute time is computed and stored when the previous run completes and "is not recomputed," so a `tzdata` change between infrequent runs fires the job at the wrong time. Self-hosted operators own `tzdata` currency. ([Cron Job](https://docs.temporal.io/cron-job#cron-job-time-zones))

**17. Deleting or expiring an API key kills a running worker fleet.** "If you delete or disable an API key being used by Workers to run a Workflow, those Workers will be unable to connect to Temporal until a new API key secret is created and configured." Max key lifetime is 2 years; long-running Workflows outlive keys. Expiry emails go out at 30/20/10 days and get ignored. ([API keys](https://docs.temporal.io/cloud/api-keys))

**18. Do not pin anything to Temporal Cloud endpoint IPs.** "In general, the IP addresses that Temporal Cloud endpoints resolve to are subject to change without notice." Customers allowlist observed IPs in a firewall and break themselves on the next change. The supported answers are Stable IPs or full cloud-provider range allowlisting. ([Cloud Namespaces](https://docs.temporal.io/cloud/namespaces#access-namespaces))

---

## How this shows up in cell lifecycle

The connection between this guide and your day job runs in both directions.

**Direction one: customer behavior becomes your infrastructure load.** Five programming decisions dominate:

| Customer behavior | Mechanism | What it does to a cell |
|---|---|---|
| **Hot Task Queue** | One Task Queue for everything, high throughput, insufficient partitions (default 4) | Concentrated Matching-service load; sync-match rate collapses under backlog and every task becomes a persisted row plus a read ([Task Queues](https://docs.temporal.io/task-queue#task-ordering)) |
| **Huge Event Histories** | No Continue-As-New; entity Workflows running for months | Every cache miss is a large History read. Replay latency rises, Workflow Tasks time out, which causes more replays. Reads amplify against whichever History Shard owns that Workflow |
| **High-cardinality Search Attributes** | Per-request IDs in Keyword attributes; accumulating `TemporalChangeVersion` values from years of patches | Elasticsearch mapping and index pressure. Cloud caps custom attributes per type (Keyword 40, Text 5, KeywordList 5), values at 2 KB, total at 40 KB ([Search Attributes](https://docs.temporal.io/search-attribute)) |
| **Spiky worker fleets** | KEDA/HPA scaling on queue depth; blue-green deploys; rainbow deploys with many concurrent versions | Poll-connection churn against your Frontend, up to 20,000 + 20,000 pollers per Namespace. Every worker restart invalidates its sticky cache, producing a replay burst |
| **Bad deploy → non-determinism storm** | Unversioned change to a Workflow with many in-flight executions | Every affected execution retries its Workflow Task indefinitely. Self-sustaining until the customer fixes or resets. Then the *reset* is a second write burst |

The unifying insight: **your cell's load is not a function of how much work customers do. It is a function of how much state they keep and how often they poll.** A customer running a million short Workflows with small histories is cheaper for you than one customer running ten thousand entity Workflows that never continue-as-new.

**Direction two: your own cell-lifecycle automation is a Temporal application.** Temporal has said publicly that control-plane tasks "involve complex long-running processes with many interdependent steps," and that each cell has an entity Workflow that "manages its lifecycle, from provisioning to upgrades" ([Building durable cloud control systems with Temporal](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). Everything above therefore applies to *you*, as a customer:

- Your cell-provisioning steps are **Activities**, and they must be idempotent, because they will run twice. `CreateVPC` must be a get-or-create. So must the IAM role, the KMS key, and the DNS record. This is the same discipline the [Terraform](08-terraform.md) and [cert-manager](11-cert-manager-and-pki.md) guides describe from a different angle.
- Long provisioning steps must **heartbeat**, both so a dead worker is detected promptly and so the operation is **cancellable** — an operator who wants to abort a half-provisioned cell needs the Activity to be listening.
- A cell entity Workflow that lives for the cell's whole life will accumulate a huge Event History. It must **Continue-As-New**, and per the Worker Versioning decision guide, "long (weeks to years) + uses Continue-As-New" maps to `PINNED` plus upgrade-on-Continue-As-New — no patching needed.
- Cell operations arrive as **Signals or Updates**: "upgrade to version X," "drain this cell," "decommission." Updates give the operator synchronous confirmation that the request was accepted and validated; Signals do not. For an operation as consequential as decommissioning, the validator is your last line of defense.
- **Compensation is the whole game.** A half-provisioned cell that leaves behind a load balancer, an ENI, a KMS key, or a DNS record is exactly the teardown-ordering problem guide 12 describes. The [Saga pattern](https://docs.temporal.io/design-patterns/saga-pattern) with compensations registered *before* each step, and `workflow.NewDisconnectedContext` so they survive cancellation, is the mechanism.
- **Replay tests against real cell histories** should gate your CI. You are deploying Workflow code changes into a fleet of long-running executions. That is precisely the situation replay testing exists for.

**Direction three: the connective tissue with the rest of the library.** Worker polling is long-lived gRPC streams, so drain semantics, keepalives, and load-balancer idle timeouts decide whether a cell upgrade is invisible or is a customer-visible poll storm — see [02-grpc.md](02-grpc.md). Worker fleets are Kubernetes Deployments with PodDisruptionBudgets and graceful-shutdown windows, and `WorkerStopTimeout` is the Temporal-side counterpart to `terminationGracePeriodSeconds` — see [04-managed-kubernetes-eks-gke-aks.md](04-managed-kubernetes-eks-gke-aks.md). mTLS to a Namespace is a customer-supplied CA bundle with a 32 KB / 16-certificate ceiling, which is a PKI design constraint — see [11-cert-manager-and-pki.md](11-cert-manager-and-pki.md). And the actual mechanics of History Shards, Matching partitions, and the Frontend rate limiter live in [16-temporal-server-internals.md](16-temporal-server-internals.md), which is where to go once the customer-facing model here is solid.

---

## Learning path

### Day 1 (about 3 hours)

Goal: be able to hold a week-one conversation without bluffing.

1. Do the **entire Hands-on section** above, including step 5 and 6. Watching a real non-determinism error and fixing it with `GetVersion` is worth more than any amount of reading. (60 min)
2. Read [Workflow Definition](https://docs.temporal.io/workflow-definition) end to end, especially "Deterministic constraints" and "Intrinsic non-deterministic logic." (20 min)
3. Read [Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures). Be able to draw the four timeouts on a whiteboard without notes. (20 min)
4. Read [Workflow Execution limits](https://docs.temporal.io/workflow-execution/limits) and [Cloud limits](https://docs.temporal.io/cloud/limits). Memorize four numbers: **51,200 Events**, **50 MB history**, **2 MB payload**, **4 MB gRPC**. (20 min)
5. Skim [Workers](https://docs.temporal.io/workers) and internalize the one sentence that governs your job: the Temporal Service "doesn't execute any of your code." (15 min)
6. Click through the Web UI of your local `lab-1`: History timeline, JSON view, Pending Activities, Call Stack. (15 min)

### Week 1

Goal: be the person on your team who can read a customer's Workflow and predict what it will do to a cell.

- Read the [Worker tuning quick reference](https://docs.temporal.io/develop/worker-tuning-reference) and [Worker best practices](https://docs.temporal.io/best-practices/worker). Build the mental table of "which metric means which fix."
- Read [Worker Versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning) and the [Patching](https://docs.temporal.io/patching) encyclopedia page. Understand why a customer would choose one over the other, and what the March 2026 legacy removal did to anyone who did not migrate.
- Read the [SDK metrics reference](https://docs.temporal.io/references/sdk-metrics) and pick the ten metrics you would put on a customer-facing dashboard. Note the seconds-vs-milliseconds split between Go/Java and Core SDKs.
- Read [Workflow message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing) and [Handling messages](https://docs.temporal.io/handling-messages). Understand handler concurrency: interleaved on one deterministic scheduler, `workflow.Mutex` required.
- Read [High Availability](https://docs.temporal.io/cloud/high-availability) closely, especially "Same-region Replication" and "Request forwarding." That section is cell infrastructure described from the customer's seat.
- Build the **backlog lab**: script twenty Workflows against a stopped worker, chart `DescribeTaskQueue` output as it drains, and correlate with `temporal_activity_schedule_to_start_latency` from the worker's Prometheus endpoint.
- Find out from your team: which cells are Same-region Replication enabled, what the largest Event History in the fleet is, and what the p99 `schedule_to_start` is per cell. If nobody knows the second one, that is a gap worth owning.

### Month 1

Goal: make the customer-side model load-bearing in your own work.

- Read the [Saga pattern](https://docs.temporal.io/design-patterns/saga-pattern) page and then read your team's actual cell-provisioning Workflow with it in hand. Check three things: are compensations registered *before* their forward Activity, are they idempotent, and do they use `NewDisconnectedContext`?
- Write and land a **replay test in CI** for one of your team's Workflows using real histories pulled from a non-production cell. This is the single highest-leverage thing you can ship in your first month.
- Audit the timeouts on your team's provisioning Activities against the quartet table. Look specifically for Schedule-To-Start set defensively, and for missing Start-To-Close.
- Instrument, or find, the Continue-As-New rate and max Event History length per Namespace on a cell you own. Build the capacity argument that follows from it.
- Read [16-temporal-server-internals.md](16-temporal-server-internals.md) with everything above in mind. The customer behaviors in this guide are the inputs to every mechanism that guide describes.
- Go one level deeper on the SDK you will actually read: clone [temporalio/sdk-go](https://github.com/temporalio/sdk-go), run `go run . unit-test` from `internal/cmd/build`, and read `contrib/tools/workflowcheck` to see how static determinism detection is implemented. Understanding how the SDK decides something is non-deterministic makes every customer conversation about it faster.
- Pick one production gotcha from the list above that your team has *not* got a guardrail for, and build the guardrail.

---

## References

All URLs verified 2026-08-29.

1. [Workflow Definition](https://docs.temporal.io/workflow-definition) — determinism constraints, Command-producing APIs, safe-change list, versioning overview
2. [Workflow Execution limits](https://docs.temporal.io/workflow-execution/limits) — 51,200 Events / 50 MB, 2,000 pending operations, Nexus and callback limits
3. [Events and Event History](https://docs.temporal.io/workflow-execution/event) — Activity Event ordering, history limits, Reset, Side Effect, Principal Attribution
4. [Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new) — the mechanism and when to use it
5. [Continue-As-New — Go SDK](https://docs.temporal.io/develop/go/workflows/continue-as-new) — `NewContinueAsNewError`, `GetContinueAsNewSuggested`, test hooks
6. [Patching](https://docs.temporal.io/patching) — `patched()` semantics, the two unexpected behaviors, newest-branch-first rule
7. [Versioning — Go SDK](https://docs.temporal.io/develop/go/workflows/versioning) — `GetVersion` lifecycle, deprecation queries, runtime checking
8. [Worker Versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning) — Deployment Versions, Pinned vs Auto-Upgrade, minimum versions, queue-blocking caveat
9. [Activities](https://docs.temporal.io/activities) — definition, idempotency recommendation, heartbeat checkpointing
10. [Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures) — the timeout quartet, heartbeat throttling, cancellation delivery
11. [Activity Timeouts — Go SDK](https://docs.temporal.io/develop/go/activities/timeouts) — `ActivityOptions`, `RecordHeartbeat`, `NextRetryDelay`
12. [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies) — defaults, non-retryable errors, Workflow Task retry behavior, Event History nuances
13. [Workers](https://docs.temporal.io/workers) — Worker Process/Entity/Identity, the "Temporal does not run your code" statement
14. [Task Queues](https://docs.temporal.io/task-queue) — long polling, registration requirements, 4 default partitions, sync vs async match
15. [Sticky Execution](https://docs.temporal.io/sticky-execution) — sticky queues, the 5-second default, cache invalidation
16. [Worker performance](https://docs.temporal.io/develop/worker-performance) — slot suppliers, Worker Tuners, poller autoscaling, eager execution
17. [Worker tuning quick reference](https://docs.temporal.io/develop/worker-tuning-reference) — per-SDK defaults table, metrics by resource type
18. [Worker deployment and performance best practices](https://docs.temporal.io/best-practices/worker) — metric interpretation matrix, Task Queue separation, graceful shutdown, history growth
19. [Workflow message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing) — Signal vs Update vs Query decision tables
20. [Sending Signals, Queries, and Updates](https://docs.temporal.io/sending-messages) — Update wait stages, Update-with-Start non-atomicity, stack-trace Query
21. [Handling Signals, Queries, and Updates](https://docs.temporal.io/handling-messages) — handler concurrency loop, validators, exception semantics, Go errors vs panics
22. [Workflow message passing — Go SDK](https://docs.temporal.io/develop/go/workflows/message-passing) — `SetQueryHandler`, `SetUpdateHandlerWithOptions`, signal draining, `AllHandlersFinished`, `workflow.Mutex`
23. [Child Workflows](https://docs.temporal.io/child-workflows) — partitioning math, the 1,000-child guidance, Continue-As-New interaction, "when in doubt, use an Activity"
24. [Schedule](https://docs.temporal.io/schedule) — specs, overlap policies, catchup window, jitter, backfill, `LastCompletionResult` caveat
25. [Temporal Cron Job](https://docs.temporal.io/cron-job) — robfig syntax, `CRON_TZ`, DST hazards, tzdata ownership
26. [Namespaces](https://docs.temporal.io/namespaces) — isolation properties, configuration boundaries
27. [Cloud Namespaces](https://docs.temporal.io/cloud/namespaces) — naming rules, Namespace ID, endpoint types, retention, deletion protection, tags, DNS warning
28. [Cloud system limits](https://docs.temporal.io/cloud/limits) — APS/RPS/OPS, throttling behavior, poller caps, retention range, Search Attribute counts, payload and gRPC limits
29. [Cloud High Availability](https://docs.temporal.io/cloud/high-availability) — 3-AZ baseline, multi-region/multi-cloud, RTO/RPO, request forwarding, Same-region Replication and cells
30. [Cloud service regions](https://docs.temporal.io/cloud/regions) — AWS and GCP region list, replication pairings, Same-region availability
31. [Cloud API keys](https://docs.temporal.io/cloud/api-keys) — lifecycle, rotation, expiry notifications, worker impact of revocation
32. [Failures and error handling](https://docs.temporal.io/encyclopedia/failures-and-error-handling) — the platform-vs-application split
33. [Application failures](https://docs.temporal.io/encyclopedia/application-failures) — failure taxonomy, Task vs Execution failure, outermost-error retryability rule
34. [Saga Pattern](https://docs.temporal.io/design-patterns/saga-pattern) — compensation ordering, registration timing, best practices, pitfalls
35. [Testing — Go SDK](https://docs.temporal.io/develop/go/best-practices/testing-suite) — `TestWorkflowEnvironment`, time skipping, `RegisterDelayedCallback`, `WorkflowReplayer`
36. [About Temporal SDKs](https://docs.temporal.io/encyclopedia/architecture/temporal-sdks) — official SDK list, third-party SDKs, SDK/Service interaction walkthrough
37. [SDK metrics reference](https://docs.temporal.io/references/sdk-metrics) — full metric list, tags, `failure_reason` values, seconds-vs-milliseconds note
38. [Temporal Web UI](https://docs.temporal.io/web-ui) — Workflows list, Saved Views, History tabs, Task Failures view, Call Stack, Codec Server integration
39. [Search Attributes](https://docs.temporal.io/search-attribute) — default attributes including `TemporalChangeVersion`, custom limits, unencrypted-storage warning
40. [Data conversion](https://docs.temporal.io/dataconversion) and [Payload Codec](https://docs.temporal.io/payload-codec) — Payloads, converter/codec chain, encryption
41. [Codecs and Encryption](https://docs.temporal.io/production-deployment/data-encryption) — Codec Server contract, CORS, JWKS auth, CLI flags
42. [Troubleshoot payload and gRPC message size limit errors](https://docs.temporal.io/troubleshooting/blob-size-limit-error) — SDK-version-dependent behavior, claim check pattern
43. [Archival](https://docs.temporal.io/temporal-service/archival) — experimental status, delay, Docker/Helm caveats
44. [Run a development server](https://docs.temporal.io/develop/run-a-development-server) — CLI install, `temporal server start-dev`, ports
45. [Set up your local with the Go SDK](https://docs.temporal.io/develop/go/set-up-your-local-go) — module setup, worker and starter skeletons
46. [Workflow basics — Go SDK](https://docs.temporal.io/develop/go/workflows/basics) — parameters, return values, replay-safe API list, `SideEffect`, `IsReplaying`
47. [temporalio/sdk-go](https://github.com/temporalio/sdk-go) and [pkg.go.dev/go.temporal.io/sdk](https://pkg.go.dev/go.temporal.io/sdk) — Go SDK source, `workflowcheck`, v1.48.0 (18 Aug 2026)
48. [Building durable cloud control systems with Temporal](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) — Temporal Cloud's own use of entity Workflows for cell lifecycle
