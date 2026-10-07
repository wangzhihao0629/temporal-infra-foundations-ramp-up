# Temporal Server Internals — Reading the Machine You Operate

**Why this matters.** If you own cell lifecycle across AWS, GCP, and Azure, the thing you are operating is a Temporal Service: four Go processes, a database, and a membership ring, holding the mid-flight state of thousands of customer Workflow Executions. Every operation your team performs — a node rotation, a Karpenter consolidation, a schema migration, a region build-out, a capacity plan — lands on one of a small number of internal mechanisms: History Shard ownership, the range-ID fence, the per-shard queue processors, the Matching partition tree, and a persistence layer that does a Paxos round per Workflow Task on Cassandra. If you know those five things at the code level, you can reason about blast radius, sizing, and rollout order from first principles instead of from runbooks. If you do not, `numHistoryShards` is a magic number and a shard-unavailability alert is a mystery. This guide is the code-level companion to [15-temporal-programming-model.md](15-temporal-programming-model.md), which covers the same system from the customer's seat.

**On sourcing and version.** Everything below was read against the `temporalio/temporal` **`main` branch on 2026-08-29**; the latest tagged release at that time was **[v1.31.2](https://github.com/temporalio/temporal/releases/tag/v1.31.2)** (2026-07-08). This codebase has been refactored repeatedly — interfaces moved out of `service/history/shard` into `service/history/interfaces`, `history_builder.go` moved into its own package, `common/tqname` became `common/tqid`, `service/worker/archiver/` was deleted — so paths that appear in older blog posts and conference talks are frequently wrong. Where a path in this guide differs from what you remember, the path here is what is on `main`. Where I could not verify something, I say so. Three kinds of claim are kept visually separate throughout:

- **(a) What the open-source code does** — cited with a direct GitHub file link.
- **(b) What Temporal publicly documents about Cloud** — cited to `docs.temporal.io` or the Temporal blog, usually quoted.
- **(c) My inference** — always labelled *Inference*. I have no access to Temporal Cloud internals and have invented none.

---

## The mental model

One Workflow Execution, end to end. Follow the nouns: every service, every internal queue, and every table it touches.

A client calls `StartWorkflowExecution` on the **Frontend**. Frontend is a stateless gRPC edge: it authenticates, rate-limits, validates, and routes. It computes nothing about the workflow. It hashes `namespaceID + "_" + workflowID` to a **History Shard** and forwards to whichever **History** host currently owns that shard.

The History host loads (or creates) the **Mutable State** for that Workflow Execution — a summary object cached in memory and persisted as a row. It appends two Events to the **Event History** (`WorkflowExecutionStarted`, `WorkflowTaskScheduled`), updates Mutable State, and writes a **Transfer Task** into the shard's internal transfer queue. Crucially, the Mutable State update and the Transfer Task are written **in one transaction**, and the shard's own `range_id` is asserted as a fencing condition in that same write. This is the transactional-outbox pattern, and Temporal's own architecture doc names it as such ([history-service.md](https://github.com/temporalio/temporal/blob/main/docs/architecture/history-service.md)).

Asynchronously, the shard's transfer **queue processor** reads that task and makes an RPC to **Matching**: `AddWorkflowTask`. Matching owns Task Queues, which are split into partitions; the task lands on one partition, which either hands it straight to a waiting poller (**sync match**) or spools it to the `tasks` table (**async match**).

A customer **Worker** — running in the customer's infrastructure, never on your cell — long-polls Frontend, which forwards to Matching. Matching hands over the Workflow Task, calling back into History (`RecordWorkflowTaskStarted`) to append `WorkflowTaskStarted` and set a workflow-task-timeout **Timer Task**. The Worker replays the History, runs customer code until it blocks, and returns **Commands** via `RespondWorkflowTaskCompleted`. History turns Commands into Events, and the cycle repeats until a `CompleteWorkflowExecution` command produces `WorkflowExecutionCompleted`, a **Visibility Task**, and eventually a retention **Timer Task** that deletes the data.

```
                    +---------------------------------------------------+
   Customer app     |                  TEMPORAL CELL                    |
   +---------+      |                                                   |
   | Client  |--1-->| +-----------+   stateless: auth, ratelimit, route  |
   +---------+      | | FRONTEND  |   WorkflowService (temporalio/api)   |
                    | +-----+-----+                                      |
   Customer worker  |       | 2  shardID = farm.Fingerprint32(           |
   +---------+      |       |         nsID+"_"+wfID) % N + 1             |
   | Worker  |--6-->|       v    ringpop Lookup(str(shardID))            |
   +----+----+      | +-----------------------------------------------+ |
        ^           | |  HISTORY host  (owns a subset of N shards)    | |
        |           | |  +-----------------------------------------+  | |
        |           | |  | ShardContext #k   rangeID=R  (fence)     |  | |
        |           | |  |  MutableState cache + per-wf lock        |  | |
        |           | |  |  taskID range = [R<<20, (R+1)<<20)       |  | |
        |           | |  +--+--------------------------------------+  | |
        |           | |     | 3  ONE ATOMIC WRITE, IF range_id = R     | |
        |           | |     v                                          | |
        |           | |  +---------------------------------------+     | |
        |           | |  | executions row  (mutable state)       |     | |
        |           | |  | history_node / history_tree (events)  |     | |
        |           | |  | transfer / timer / visibility /       |     | |
        |           | |  |   replication task rows               |     | |
        |           | |  +---------------------------------------+     | |
        |           | |     | 4  per-shard QUEUE PROCESSORS            | |
        |           | |     |    readers -> slices -> executables      | |
        |           | |     v    scheduler -> Execute -> Ack/Nack      | |
        |           | +-----|-----------------------------------------+ |
        |           |       | 5  AddWorkflowTask / AddActivityTask      |
        |           |       v                                           |
        |           | +-----------------------------------------------+ |
        |           | |  MATCHING host (owns TaskQueue partitions)    | |
        |           | |   /_sys/<tq>/1 .. /_sys/<tq>/3   -> root <tq> | |
        |           | |   sync match: hand to waiting poller          | |
        |           | |   async match: spool to `tasks` table         | |
        +-----7-----+ +-----------------------------------------------+ |
          long poll  |                                                   |
                     | +-------------+  internal Temporal workflows on   |
                     | | WORKER svc  |  the `temporal-system` namespace  |
                     | +-------------+  scanners, batcher, DLQ, sched.   |
                     +---------------------------------------------------+
                                  |
                                  v   persistence (Cassandra / PG / MySQL)
                     +---------------------------------------------------+
                     | executions | history_node | history_tree | shards  |
                     | tasks | task_queues | cluster_membership | queues  |
                     +---------------------------------------------------+
                                  |  visibility task -> ES bulk processor
                                  v
                     +---------------------------------------------------+
                     | Elasticsearch / OpenSearch  OR  SQL visibility DB |
                     +---------------------------------------------------+
```

Three properties fall out of this picture and are worth internalising before anything else:

1. **A shard is a serialization domain.** Every Workflow in shard *k* shares one owner, one task-ID sequence, and — on Cassandra — one physical partition. Shard count is your concurrency ceiling.
2. **The only strongly-consistent write is the one History makes.** Matching, Visibility, Replication, and Archival are all downstream of a durable task row. Everything is eventually consistent by construction.
3. **Nothing in the cell runs customer code.** Your capacity problem is state size, task-matching rate, and poll concurrency — never business-logic CPU.

---

## Core concepts

### The four services and why they are split

The repo's own [architecture README](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md) names four internal services. The split is not aesthetic; each one has a different scaling axis and a different failure mode.

| Service | Owns | Scaling axis | State on the host |
|---|---|---|---|
| **Frontend** | The public API surface, auth, rate limits, routing, long-poll parking | Connection count and RPS | None (stateless) |
| **History** | Workflow Execution state, Event History, internal task queues, replication | History Shards | The shards it owns, plus a Mutable State cache |
| **Matching** | Task Queue partitions, task dispatch, worker versioning user data | Task Queue partitions, poller count | The partitions it owns, plus in-memory backlog |
| **Worker** | Internal system Workflows: scanners, batcher, DLQ, schedules, replication | Namespace count | None durable (it is a Temporal client) |

The public API is `WorkflowService` in [`temporal/api/workflowservice/v1/service.proto`](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto), and it is enormous — roughly 120 RPCs on current `master`, covering namespaces, start/signal/query/update, the `Poll*`/`Respond*` worker protocol, schedules, batch operations, worker versioning and deployments, and (new in 2026) first-class Activity and Nexus Operation executions. Operator-facing management lives in a separate `OperatorService` ([`operatorservice/v1/service.proto`](https://github.com/temporalio/api/blob/master/temporal/api/operatorservice/v1/service.proto)): `AddSearchAttributes`, `RemoveSearchAttributes`, `ListSearchAttributes`, `DeleteNamespace`, `AddOrUpdateRemoteCluster`, `RemoveRemoteCluster`, `ListClusters`, and the Nexus endpoint CRUD. Note what is *not* there: there is no DLQ command on `OperatorService`. DLQ management is server-internal and reachable only through `tdbg` (see Hands-on).

The internal service surfaces are separate protos inside the server repo, not in `temporalio/api`:

- [`proto/internal/temporal/server/api/historyservice/v1/service.proto`](https://github.com/temporalio/temporal/blob/main/proto/internal/temporal/server/api/historyservice/v1/service.proto) — around 75 RPCs. The workflow-lifecycle ones (`StartWorkflowExecution`, `SignalWorkflowExecution`, `RespondWorkflowTaskCompleted`, `RecordActivityTaskStarted`, …), the replication ones (`ReplicateEventsV2`, `SyncActivity`, `GetReplicationMessages`, `StreamWorkflowReplicationMessages`), and the operational ones (`GetShard`, `CloseShard`, `DescribeMutableState`, `DescribeHistoryHost`, `RemoveTask`, `GetDLQTasks`, `DeleteDLQTasks`, `ListQueues`, `AddTasks`, `ListTasks`, `RebuildMutableState`, `DeepHealthCheck`).
- [`proto/internal/temporal/server/api/matchingservice/v1/service.proto`](https://github.com/temporalio/temporal/blob/main/proto/internal/temporal/server/api/matchingservice/v1/service.proto) — `AddWorkflowTask`, `AddActivityTask`, `PollWorkflowTaskQueue`, `PollActivityTaskQueue`, `QueryWorkflow`, `DescribeTaskQueue`, `DescribeTaskQueuePartition`, `ForceLoadTaskQueuePartition`, `ForceUnloadTaskQueuePartition`, plus the versioning/user-data set (`GetTaskQueueUserData`, `UpdateTaskQueueUserData`, `ReplicateTaskQueueUserData`, `SyncDeploymentUserData`).

Every History RPC carries an API-category option that drives rate limiting and priority:

```proto
rpc StartWorkflowExecution(StartWorkflowExecutionRequest) returns (StartWorkflowExecutionResponse) {
  option (temporal.server.api.common.v1.api_category).category = API_CATEGORY_STANDARD;
}

rpc StreamWorkflowReplicationMessages(stream StreamWorkflowReplicationMessagesRequest)
    returns (stream StreamWorkflowReplicationMessagesResponse) {
  option (temporal.server.api.common.v1.api_category).category = API_CATEGORY_SYSTEM;
}

rpc PollMutableState(PollMutableStateRequest) returns (PollMutableStateResponse) {
  option (temporal.server.api.common.v1.api_category).category = API_CATEGORY_LONG_POLL;
}
```

The categories are `API_CATEGORY_STANDARD`, `API_CATEGORY_LONG_POLL`, and `API_CATEGORY_SYSTEM`. This matters operationally: long-poll RPCs must not be counted against the same concurrency budget as request/response RPCs, or your first burst of pollers exhausts the frontend.

**Why split at all?** Because the three stateful concerns scale differently and fail differently. History is bound by shard ownership and persistence latency. Matching is bound by partition count and poller concurrency, and its state is *reconstructible* — a Matching host can die and its partitions get reloaded from the `tasks` table with nothing lost but latency. Frontend is bound by connections and is trivially replaceable. Fusing them would mean a single deploy unit whose slowest component sets the rollout risk for all three. The public docs give a sense of the real ratio: "a real-life production deployment can have 5 Frontend, 15 History, 17 Matching, and 3 Worker Services" ([Temporal Server](https://docs.temporal.io/temporal-service/temporal-server)).

The call graph for one Workflow Task, expanded from the [workflow-lifecycle](https://github.com/temporalio/temporal/blob/main/docs/architecture/workflow-lifecycle.md) doc:

```
Client  -> Frontend.StartWorkflowExecution
        -> History.StartWorkflowExecution                (shard-routed)
           -> Persistence.CreateWorkflowExecution        (events + MS + transfer task, atomic)
        <- ok

[History shard transfer queue processor]
        -> Matching.AddWorkflowTask

Worker  -> Frontend.PollWorkflowTaskQueue
        -> Matching.PollWorkflowTaskQueue                (partition-routed)
           -> History.RecordWorkflowTaskStarted          (shard-routed)
              -> Persistence.UpdateWorkflowExecution     (WorkflowTaskStarted + timer task)
              -> Persistence.ReadHistoryBranch           (events for the worker)
        <- WorkflowTask (with history)

Worker  -> Frontend.RespondWorkflowTaskCompleted [commands]
        -> History.RespondWorkflowTaskCompleted
           -> Persistence.UpdateWorkflowExecution        (WorkflowTaskCompleted + command events
                                                          + MS + new transfer/timer tasks, atomic)
[History shard transfer queue processor]
        -> Matching.AddActivityTask
```

Notice that Matching calls *back into* History (`RecordWorkflowTaskStarted`) before it hands a task to a poller. That call is what makes a delivered task durable — the started event and its timeout timer are written before the worker sees anything. It is also why Matching latency and History latency are coupled: a slow History shard shows up as poll latency.

---

### Sharding: the range-ID fence

This is the single most important internals concept for an infrastructure owner, so it gets the most space.

#### The mapping

A Workflow Execution belongs to exactly one History Shard, chosen by a hash of the namespace and workflow ID. The function is in [`common/util.go`](https://github.com/temporalio/temporal/blob/main/common/util.go):

```go
// WorkflowIDToHistoryShard is used to map namespaceID-workflowID pair to a shardID.
// TODO: rename to BusinessIDToHistoryShard.
func WorkflowIDToHistoryShard(
	namespaceID string,
	workflowID string,
	numberOfShards int32,
) int32 {
	idBytes := []byte(namespaceID + "_" + workflowID)
	hash := farm.Fingerprint32(idBytes)
	return int32(hash%uint32(numberOfShards)) + 1 // ShardID starts with 1
}
```

Four consequences you should be able to recite:

1. **FarmHash `Fingerprint32`**, not murmur, not CRC. If you ever need to compute a shard ID out-of-band (to correlate a customer complaint with a host), you need `github.com/dgryski/go-farm`.
2. **Run ID is not in the key.** Every run of a given Workflow ID — every continue-as-new, every reset, every retry — lands on the same shard. A customer who continues-as-new in a tight loop keeps hammering one shard.
3. **Shard IDs are 1-based.** Off-by-one is a real bug source in operational tooling.
4. **The modulus is the shard count.** Change the count and every workflow re-hashes to a different shard, orphaning all existing state. This is why the count is immutable.

The history service wraps it in [`service/history/configs/config.go`](https://github.com/temporalio/temporal/blob/main/service/history/configs/config.go):

```go
// GetShardID return the corresponding shard ID for a given namespaceID and workflowID pair
func (config *Config) GetShardID(namespaceID namespace.ID, workflowID string) int32 {
	return common.WorkflowIDToHistoryShard(namespaceID.String(), workflowID, config.NumberOfShards)
}
```

`NumberOfShards` is a plain `int32` field on the static config, set once at construction. **It is not dynamic config.** There is no `HistoryShardCount` dynamic setting; do not go looking for one.

#### Why `numHistoryShards` is immutable, and what actually happens if you change it

The docs are unambiguous. From [Temporal Server](https://docs.temporal.io/temporal-service/temporal-server):

> "Before integrating a database, the total number of History Shards for the Temporal Service must be chosen and set in the Temporal Service's configuration (see persistence). After the Shard count is configured and the database integrated, the total number of History Shards for the Temporal Service cannot be changed."

And from the [configuration reference](https://docs.temporal.io/references/configuration):

> "*Required* - The number of history shards to create when initializing the Cluster. **Warning: This value is immutable and will be ignored after the first run.** Please ensure you set this value appropriately high enough to scale with the worst case peak load for this Cluster."

What the code actually does is more interesting than "it is immutable", and this is exactly the kind of thing that separates someone who has read the source from someone who has read the docs. The shard count is persisted inside the serialized `ClusterMetadata` blob in the `cluster_metadata_info` table. Two independent mechanisms defend it, and **neither one fails startup**:

First, the persistence layer silently refuses the write. In [`common/persistence/cluster_metadata_store.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/cluster_metadata_store.go):

```go
// immutableFieldsChanged returns true if any of immutable fields changed.
func immutableFieldsChanged(old *persistencespb.ClusterMetadata, cur *persistencespb.ClusterMetadata) bool {
	if (old.ClusterName != "" && old.ClusterName != cur.ClusterName) ||
		(old.ClusterId != "" && old.ClusterId != cur.ClusterId) ||
		(old.HistoryShardCount != 0 && old.HistoryShardCount != cur.HistoryShardCount) ||
		(old.IsGlobalNamespaceEnabled && !cur.IsGlobalNamespaceEnabled) {
		return true
	}
	...
}
```

`SaveClusterMetadata` then returns `(applied=false, err=nil)` — success, no write.

Second, startup reconciliation overwrites your config with the persisted value. `ApplyClusterMetadataConfigProvider` in [`temporal/fx.go`](https://github.com/temporalio/temporal/blob/main/temporal/fx.go) reads the persisted record and calls `overwriteCurrentClusterMetadataWithDBRecord`:

```go
persistedShardCount := currentClusterDBRecord.HistoryShardCount
if svc.Persistence.NumHistoryShards != persistedShardCount {
	logger.Warn(
		mismatchLogMessage,
		tag.Key("persistence.numHistoryShards"),
		tag.IgnoredValue(svc.Persistence.NumHistoryShards),
		tag.Value(persistedShardCount))
	svc.Persistence.NumHistoryShards = persistedShardCount
}
```

with the message defined in [`temporal/server.go`](https://github.com/temporalio/temporal/blob/main/temporal/server.go):

```go
const (
	mismatchLogMessage  = "Supplied configuration key/value mismatches persisted cluster metadata. Continuing with the persisted value as this value cannot be changed once initialized."
	serviceStartTimeout = time.Duration(15) * time.Second
	serviceStopTimeout  = time.Duration(5) * time.Minute
)
```

**Operationally, this is the most important sentence in this section: misconfiguring `numHistoryShards` on an existing cell does not fail, it logs a warning at `WARN` and proceeds with the persisted value.** That is a safety property — it is what prevents a bad Helm value from re-hashing every workflow in the cell — but it also means a config drift can sit undetected for months. Alert on that log line. It is a single, unique, greppable string.

#### `ShardContext` and where ownership lives

The interface moved. It is now in [`service/history/interfaces/shard_context.go`](https://github.com/temporalio/temporal/blob/main/service/history/interfaces/shard_context.go) (package `interfaces`, imported everywhere as `historyi`); the single implementation is `ContextImpl` in [`service/history/shard/context_impl.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/context_impl.go), about 2,400 lines. **`service/history/shard/context.go` does not exist on `main`.**

The methods worth knowing:

| Method | What it is for |
|---|---|
| `GetShardID() int32` | Immutable; no lock taken |
| `GetRangeID() int64` | The current fencing token |
| `GetOwner() string` | `"<hostIdentity>-<seq>-<uuid>"` |
| `GetEngine(ctx)` | Blocks on `engineFuture` until the range lease is acquired |
| `AssertOwnership(ctx)` | Cheap ownership probe (see caveat below) |
| `GenerateTaskID()` / `GenerateTaskIDs(n)` | Allocate task IDs from the in-memory range |
| `GetQueueExclusiveHighReadWatermark(category)` | Exclusive upper bound a queue reader may read to |
| `GetQueueState(category)` / `SetQueueState(...)` | Read/checkpoint per-category queue cursors |
| `UpdateReplicationQueueReaderState(readerID, state)` | Per-remote-cluster replication ack state |
| `AddTasks(ctx, req)` | Write history tasks with `RangeID` stamped |
| `CreateWorkflowExecution` / `UpdateWorkflowExecution` / `ConflictResolveWorkflowExecution` / `SetWorkflowExecution` | All mutable-state writes route through the shard |
| `AppendHistoryEvents(...)` | Append history nodes |
| `UnloadForOwnershipLost()` | External trigger to drop the shard |
| `GetLifecycleContext()` | Cancelled the moment the shard begins stopping |

There is no dedicated `rangeID` field. It is a field of the persisted `shardInfo` proto, guarded by an `RWMutex`:

```go
// All following fields are protected by rwLock, and only valid if state >= Acquiring:
rwLock                        sync.RWMutex
lastUpdated                   time.Time
tasksCompletedSinceLastUpdate int
shardInfo                     *persistencespb.ShardInfo
```

Every persistence write stamps it and classifies the outcome:

```go
request.RangeID = s.getRangeIDLocked()
s.wUnlock()

err = s.executionManager.AddHistoryTasks(ctx, request)
requestCompletionFn(err)
return s.handleWriteError(request.RangeID, err)
```

#### The fence itself

Acquisition happens in `renewRangeLocked`. The comment block at the top is the whole design in six lines:

```go
func (s *ContextImpl) renewRangeLocked(isStealing bool) error {
	// We must drain all in-flight requests before updating the rangeID.
	// This is because requests are conditioned on rangeID, if rangeID
	// is updated before draining them, those requests could fail.
	// This also means renew rangeID will be the only in-flight request
	// when it's issued, so it doesn't matter if semaphore is acquired or not
	// before calling this method.
	s.taskKeyManager.drainTaskRequests()

	updatedShardInfo := trimShardInfo(s.config, s.clusterMetadata.GetAllClusterInfo(), s.copyShardInfo(s.shardInfo))
	updatedShardInfo.RangeId++
	if isStealing {
		updatedShardInfo.StolenSinceRenew++
	}
	...
	err := s.persistenceShardManager.UpdateShard(ctx, &persistence.UpdateShardRequest{
		ShardInfo:       updatedShardInfo,
		PreviousRangeID: previousRangeID,
	})
```

`isStealing = true` on takeover (called from `acquireShard()`), `false` when the range is merely exhausted.

The task-ID arithmetic is in [`service/history/shard/task_key_generator.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/task_key_generator.go), and it is the reason Temporal can mint a million task IDs per database round trip:

```go
func (a *taskKeyGenerator) setRangeID(rangeID int64) {
	a.nextTaskID = rangeID << a.rangeSizeBits
	a.exclusiveMaxTaskID = (rangeID + 1) << a.rangeSizeBits
	...
}

func (a *taskKeyGenerator) generateTaskID() (int64, error) {
	if a.nextTaskID == taskIDUninitialized {
		a.logger.Panic("Range id is not initialized before generating task id")
	}
	if a.nextTaskID == a.exclusiveMaxTaskID {
		if err := a.renewRangeIDFn(); err != nil {
			return taskIDUninitialized, err
		}
		...
	}
	taskID := a.nextTaskID
	a.nextTaskID++
	return taskID, nil
}
```

`RangeSizeBits` is a compile-time field in [`service/history/configs/config.go`](https://github.com/temporalio/temporal/blob/main/service/history/configs/config.go), not tunable:

```go
RangeSizeBits: 20, // 20 bits for sequencer, 2^20 sequence number for any range
```

So the sequence is:

1. On acquire, one conditional `UpdateShard` bumps `range_id` from `R` to `R+1`.
2. In memory, `nextTaskID = (R+1) << 20`, `exclusiveMaxTaskID = (R+2) << 20`.
3. Every task ID after that is `nextTaskID++` under the shard lock — **2^20 = 1,048,576 IDs per DB round trip**.
4. On exhaustion, one more `UpdateShard`, a new disjoint block.

Because ranges are disjoint and monotonic in `rangeID`, a stale owner physically cannot mint an ID a new owner will also mint. That is the fence.

#### How the database enforces it

On Cassandra, [`common/persistence/cassandra/shard_store.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/cassandra/shard_store.go) uses lightweight transactions:

```go
templateCreateShardQuery = `INSERT INTO executions (` +
	`shard_id, type, namespace_id, workflow_id, run_id, visibility_ts, task_id, shard, shard_encoding, range_id)` +
	`VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) IF NOT EXISTS`

templateUpdateShardQuery = `UPDATE executions ` +
	`SET shard = ?, shard_encoding = ?, range_id = ? ` +
	`WHERE shard_id = ? and type = ? and namespace_id = ? and workflow_id = ? ` +
	`and run_id = ? and visibility_ts = ? and task_id = ? ` +
	`IF range_id = ?`
```

and on the not-applied path:

```go
if !applied {
	var columns []string
	for k, v := range previous {
		columns = append(columns, fmt.Sprintf("%s=%v", k, v))
	}
	return &p.ShardOwnershipLostError{
		ShardID: request.ShardID,
		Msg: fmt.Sprintf("Failed to update shard.  previous_range_id: %v, columns: (%v)",
			request.PreviousRangeID, strings.Join(columns, ",")),
	}
}
```

Note the shard record is **a row in the `executions` table** (`type = 0`), not a separate table. On SQL there *is* a `shards` table, and the mechanism is a pessimistic row lock rather than a CAS ([`common/persistence/sql/shard.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/sql/shard.go)):

```go
rangeID, err := tx.WriteLockShards(ctx, sqlplugin.ShardsFilter{ShardID: shardID})
switch err {
case nil:
	if rangeID != oldRangeID {
		return &persistence.ShardOwnershipLostError{
			ShardID: shardID,
			Msg:     fmt.Sprintf("Failed to update shard. Previous range ID: %v; new range ID: %v", oldRangeID, rangeID),
		}
	}
```

There are two `ShardOwnershipLost` types and you will see both in logs: `persistence.ShardOwnershipLostError` (from the store) and `serviceerror.ShardOwnershipLost` (the gRPC-level one, mapping to `codes.Aborted` with an `OwnerHost`/`CurrentHost` detail so the caller can redirect). `shard.IsShardOwnershipLostError` unifies them.

**A caveat that matters and is easy to miss:** `AssertShardOwnership` is a **no-op in both the Cassandra and SQL shard stores** — both return `nil` with a comment saying it is not implemented. This is why `history.shardLingerTimeLimit`'s own doc string warns: *"Do NOT use non-zero value with persistence layers that are missing AssertShardOwnership support."* Graceful shard lingering therefore only terminates when some *other* fenced write fails.

#### The controller: acquisition, loss, and lingering

Ownership is decided by membership, checked on a ticker and on every ring change. [`service/history/shard/ownership.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/ownership.go):

```go
func (o *ownership) verifyOwnership(shardID int32) error {
	ownerInfo, err := o.historyServiceResolver.Lookup(convert.Int32ToString(shardID))
	if err != nil {
		return err
	}
	hostInfo := o.hostInfoProvider.HostInfo()
	if ownerInfo.Identity() != hostInfo.Identity() {
		return serviceerrors.NewShardOwnershipLost(ownerInfo.Identity(), hostInfo.GetAddress())
	}
	return nil
}
```

The ring key is literally the shard ID rendered as a decimal string. The acquire loop is driven by `time.NewTicker(o.config.AcquireShardInterval())` plus a membership-update channel, coalesced through a 1-buffered channel.

[`service/history/shard/controller_impl.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/controller_impl.go) walks every shard ID each interval, with a weighted semaphore and a random start offset so hosts do not stampede in the same order:

```go
concurrency := int64(max(c.config.AcquireShardConcurrency(), 1))
sem := semaphore.NewWeighted(concurrency)
numShards := c.config.NumberOfShards
randomStartOffset := rand.Int31n(numShards)
for index := range numShards {
	shardID := (index+randomStartOffset)%numShards + 1
	if err := sem.Acquire(ctx, 1); err != nil {
		break
	}
	go func() {
		defer sem.Release(1)
		tryAcquire(shardID)
	}()
}
_ = sem.Acquire(ctx, concurrency)
```

So a 512-shard cell has each History host walking all 512 IDs every `history.acquireShardInterval` (default 1 minute), 10 at a time (`history.acquireShardConcurrency`, default 10).

The shard's own lifecycle is an explicit state machine in `context_impl.go`:

```go
const (
	// See transition for overview of state transitions.
	contextStateInitialized contextState = iota
	contextStateAcquiring
	contextStateAcquired
	contextStateStopping
	contextStateStopped
)
```

Acquisition retries with `backoff.NewExponentialRetryPolicy(1 * time.Second).WithExpirationInterval(5 * time.Minute)`, each attempt bounded by `history.shardIOTimeout` (5s), and gives up immediately on `ShardOwnershipLostError`. While the state is Initialized or Acquiring, requests get `ErrShardStatusUnknown` — `serviceerror.NewUnavailable("shard status unknown")`. **That is the error string your customers see as `UNAVAILABLE` during a rolling restart.**

Write-error classification is the other half of the fence, in `handleWriteErrorLocked`. The first guard is subtle and important:

```go
if requestRangeID != s.getRangeIDLocked() {
	return err
}
...
case *persistence.ShardOwnershipLostError:
	// Shard is stolen, trigger shutdown of history engine.
	_ = s.transition(contextRequestStop{reason: stopReasonOwnershipLost})
	return err

default:
	// We have no idea if the write failed or will eventually make it to persistence. Try to re-acquire
	// the shard in the background. If successful, we'll get a new RangeID, to guarantee that subsequent
	// reads will either see that write, or know for certain that it failed.
	_ = s.transition(contextRequestLost{})
	return err
```

Read that `default` branch twice. **An ambiguous persistence error — a timeout, a connection reset — causes the shard to re-acquire, i.e. to bump its range ID.** That is how Temporal converts "I do not know if my write landed" into "I know for certain, because any subsequent read is fenced behind a new range." It is also why a persistence latency spike produces a burst of shard reloads, which is a much bigger event than the latency spike itself. Hold that thought for the Production gotchas section.

Graceful handoff is `shardLingerThenClose`, capped at `shardLingerMaxTimeLimit = 1 * time.Minute` regardless of config, polling `AssertOwnership` at `history.shardLingerOwnershipCheckQPS` (default 4). It runs on its own goroutine, for a documented reason:

```go
// This uses a separate goroutine because acquireShards has a concurrency limit,
// and we don't want to block acquiring new shards while waiting for
// shard ownership lost on this one.
```

Since `AssertShardOwnership` is a no-op on both stores, lingering is effectively disabled by default (`history.shardLingerTimeLimit = 0`) and should stay that way unless you are on a persistence layer that implements it.

#### The shard knobs

All from [`common/dynamicconfig/constants.go`](https://github.com/temporalio/temporal/blob/main/common/dynamicconfig/constants.go):

| Key | Default | Note |
|---|---|---|
| `history.acquireShardInterval` | `1m` | Full scan cadence |
| `history.acquireShardConcurrency` | `10` | Goroutines per scan |
| `history.shardIOConcurrency` | `1` | **Forced to 1 on Cassandra** with a warning log |
| `history.shardIOTimeout` | `5s` | Per persistence op in the shard context |
| `history.shardLingerTimeLimit` | `0` | Disabled; hard-capped at 1m in code |
| `history.shardLingerOwnershipCheckQPS` | `4` | |
| `history.shardFinalizerTimeout` | `2s` | Cleanup of workflow contexts on unload |
| `history.shardUpdateMinInterval` | `5m` | Throttles queue-state checkpoints |
| `history.shardUpdateMinTasksCompleted` | `1000` | Or checkpoint after this many tasks |
| `history.alignMembershipChange` | `0s` | *"This can help reduce effects of shard movement."* |
| `history.persistencePerShardNamespaceMaxQPS` | `0` | Per-shard, per-namespace persistence cap |

Two removals worth knowing so you do not cite dead knobs: **`history.shardOwnershipAssertionEnabled` does not exist on `main`** (it was present in v1.24.0 and gone by v1.25.0), and `RangeSizeBits` has never been dynamic config.

#### What a shard rebalance actually looks like

There is no rebalancer. There is a consistent hash ring and a scan loop. When a History host joins or leaves:

1. Ringpop propagates the membership change; every History host's `ownership.eventLoop` gets a `ChangedEvent` and schedules an immediate acquire pass.
2. Each host recomputes `Lookup(shardID)` for all `N` shards. For shards it should no longer own, it calls `CloseShardByID` (or lingers). For shards it should now own, it creates a `ContextImpl` and starts `acquireShard()`.
3. The new owner does `GetOrCreateShard`, then `renewRangeLocked(true)` — one LWT that increments `range_id` and stamps `StolenSinceRenew++`.
4. The old owner's next fenced write fails with `ShardOwnershipLostError` and it transitions to Stopping.
5. In-flight client requests to the old owner get `serviceerror.ShardOwnershipLost` with the new `OwnerHost`; the history client redirects transparently (see Membership and routing).
6. The new owner replays queue state from the persisted `QueueState`, starts a queue processor per category, and begins re-processing from the last checkpointed cursor. Tasks between the checkpoint and the crash are **re-executed** — this is why every task executor must be idempotent.

*Inference:* the practical cost of a rebalance is dominated by step 6 — cold Mutable State caches and re-processing of up to `history.shardUpdateMinInterval` worth of already-completed tasks — not by the LWT in step 3. Nothing in the docs quantifies this; measure it on your own cells with `sharditem_acquisition_latency` and the shard-info lag gauges.

---
### Mutable state, the workflow lock, and the atomic transaction

#### What Mutable State is

Event History is the source of truth, but rebuilding a workflow's state from events on every RPC would be intolerable. So Temporal keeps a **summary** — pending activities, pending timers, pending child workflows, signal state, the workflow-task state machine — and persists it. The repo's own framing ([history-service.md](https://github.com/temporalio/temporal/blob/main/docs/architecture/history-service.md)):

> "Although most of this data could in principle be recomputed from Workflow History Events when handling an incoming request, this would be slow, and hence the summaries themselves are persisted. ... While it would be natural to arrange the persisted data following a relational schema, Cassandra is our most important persistence backend and has very limited support for RDBMS features, so in practice the data is persisted in a single row, similar to its layout in the in-memory cache."

That single sentence explains the write amplification in the persistence section. **The whole Mutable State blob is rewritten on every Workflow Task.**

The interface is `MutableState` in [`service/history/interfaces/mutable_state.go`](https://github.com/temporalio/temporal/blob/main/service/history/interfaces/mutable_state.go) — note the package move; **`service/history/workflow/mutable_state.go` no longer exists**. The implementation is `MutableStateImpl` in [`service/history/workflow/mutable_state_impl.go`](https://github.com/temporalio/temporal/blob/main/service/history/workflow/mutable_state_impl.go), which at ~374 KB is the largest file in the repo and the one you should expect to spend the most time in.

The dirty-tracking model is visible directly in the struct fields:

```go
MutableStateImpl struct {
	pendingActivityInfoIDs  map[int64]*persistencespb.ActivityInfo // Scheduled Event ID -> Activity Info.
	updateActivityInfos     map[int64]*persistencespb.ActivityInfo // Modified activities from last update.
	deleteActivityInfos     map[int64]struct{}                    // Deleted activities from last update.
	syncActivityTasks       map[int64]struct{}                    // Activity to be sync to remote
	pendingTimerInfoIDs     map[string]*persistencespb.TimerInfo   // User Timer ID -> Timer Info.
	updateTimerInfos        map[string]*persistencespb.TimerInfo   // Modified timers from last update.
	deleteTimerInfos        map[string]struct{}                    // Deleted timers from last update.
	// In-memory only attributes
	currentVersion int64; approximateSize int; stateInDB enumsspb.WorkflowExecutionState
	nextEventIDInDB int64  // Indicates the next event ID in DB, for conditional update.
	dbRecordVersion int64  // Indicates the DB record version, for conditional update.
```

The pattern repeats for child executions, request cancels, signals, signal-requested IDs, and CHASM nodes: a `pending*` map holding current state, plus `update*` / `delete*` delta maps holding what changed since the last flush. `IsDirty()` is `hBuilder.IsDirty() || len(InsertTasks) > 0 || stateMachineNode.Dirty() || chasmTree.IsDirty()`; `cleanupTransaction()` re-`make`s every delta map. Releasing a cache entry with a dirty transaction is a hard failure — the cache panics with `"Cache encountered dirty mutable state transaction"`.

#### Optimistic concurrency

Two conditional-update mechanisms coexist. The modern one is `dbRecordVersion`; the legacy one is `nextEventID`. In `closeTransaction()`:

```go
ms.executionInfo.StateTransitionCount += 1
if ms.dbRecordVersion == 0 { /* noop, existing behavior */ } else { ms.dbRecordVersion += 1 }
	Condition:       ms.nextEventIDInDB,
	DBRecordVersion: ms.dbRecordVersion,
	Checksum:        result.checksum,
```

and in [`common/persistence/cassandra/mutable_state_store.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/cassandra/mutable_state_store.go):

```go
// TODO deprecate templateUpdateWorkflowExecutionQueryDeprecated in favor of templateUpdateWorkflowExecutionQuery
templateUpdateWorkflowExecutionQueryDeprecated = `UPDATE executions SET ... IF next_event_id = ? `
templateUpdateWorkflowExecutionQuery           = `UPDATE executions SET ... IF db_record_version = ? `
```

A CAS failure surfaces as `WorkflowConditionFailedError{Msg, NextEventID, DBRecordVersion}` from [`common/persistence/data_interfaces.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/data_interfaces.go), raised by `extractWorkflowConflictError` in `common/persistence/cassandra/errors.go`. There is an explicit error priority when several conditions fail at once in the same batch: `ShardOwnershipLostError` (0) beats `CurrentWorkflowConditionFailedError` (1) beats `WorkflowConditionFailedError` (2) beats `ConditionFailedError` (3). The shard fence always wins.

#### The workflow lock

`WorkflowContext` (interface in `service/history/interfaces/workflow_context.go`, implementation `ContextImpl` in [`service/history/workflow/context.go`](https://github.com/temporalio/temporal/blob/main/service/history/workflow/context.go)) is the per-Workflow-Execution mutex plus the mutable-state loader. Its lock is a **priority semaphore of size 1**:

```go
lock locks.PrioritySemaphore = locks.NewPrioritySemaphore(1)
// Lock   -> c.lock.Acquire(ctx, lockPriority, 1)
// Unlock -> c.lock.Release(1)
```

The priority type is `locks.Priority` in [`common/locks/priority_semaphore_impl.go`](https://github.com/temporalio/temporal/blob/main/common/locks/priority_semaphore_impl.go) — **not** `workflow.LockPriority`, which does not exist:

```go
type Priority uint32
const ( PriorityHigh Priority = iota; PriorityLow; NumPriorities )
```

A waiter is granted only if `s.size-s.cur >= n && s.noWaiters(priority)`, scanning all higher-priority wait lists. API-driven work takes `PriorityHigh`; background and replication work takes `PriorityLow`. **This is the mechanism that stops a replication backlog from starving live customer traffic on the same workflow.**

The acquisition path in [`service/history/workflow/cache/cache.go`](https://github.com/temporalio/temporal/blob/main/service/history/workflow/cache/cache.go) is worth reading closely, because it is where the "workflow is busy" error your customers see is minted:

```go
if deadline, ok := ctx.Deadline(); ok {
	if headers.GetCallerInfo(ctx).CallerType != headers.CallerTypeAPI {
		newDeadline := time.Now().Add(c.nonUserContextLockTimeout)
		if newDeadline.Before(deadline) { ctx, cancel = context.WithDeadline(ctx, newDeadline); defer cancel() }
	} else {
		newDeadline := deadline.Add(-workflowLockTimeoutTailTime)   // 500ms
		if newDeadline.After(time.Now()) { ctx, cancel = context.WithDeadline(ctx, newDeadline); defer cancel() }
	}
}
if err := workflowCtx.Lock(ctx, lockPriority); err != nil {
	c.Release(cacheKey)                       // ctx is done before lock can be acquired
	return consts.ErrResourceExhaustedBusyWorkflow
}
```

Non-API callers get their deadline clamped to `history.cacheNonUserContextLockTimeout` (default 500ms) so background work cannot camp on a lock. API callers get their deadline shortened by 500ms so the server can return a clean error instead of the client timing out.

The cache itself is host-level (not per-shard), a pinned TTL LRU over [`common/cache/lru.go`](https://github.com/temporalio/temporal/blob/main/common/cache/lru.go) where `tryEvictUntilEnoughSpaceWithSkipEntry` skips entries with `refCount > 0`. Its knobs:

| Key | Default |
|---|---|
| `history.hostLevelCacheMaxSize` | `128000` |
| `history.hostLevelCacheMaxSizeBytes` | `256000*4*1024` |
| `history.cacheTTL` | `1h` |
| `history.cacheSizeBasedLimit` | `false` |
| `history.cacheNonUserContextLockTimeout` | `500ms` |

**`history.cacheSize`, `history.cacheMaxSizeBytes`, and `history.cacheHostLevelMaxSize` are all gone.** If your dashboards or config reference them, they are dead keys.

#### The transaction

`Transaction` in [`service/history/workflow/transaction.go`](https://github.com/temporalio/temporal/blob/main/service/history/workflow/transaction.go) has four methods (`CreateWorkflowExecution`, `ConflictResolveWorkflowExecution`, `UpdateWorkflowExecution`, `SetWorkflowExecution`); `TransactionImpl` in `transaction_impl.go` holds just `{shard historyi.ShardContext; logger log.Logger}`. The payload is `WorkflowMutation`, from `data_interfaces.go`:

```go
WorkflowMutation struct {
	ExecutionInfo *persistencespb.WorkflowExecutionInfo
	ExecutionState *persistencespb.WorkflowExecutionState
	NextEventID int64  // TODO deprecate NextEventID in favor of DBRecordVersion
	UpsertActivityInfos map[int64]*persistencespb.ActivityInfo; DeleteActivityInfos map[int64]struct{}
	UpsertTimerInfos map[string]*persistencespb.TimerInfo; DeleteTimerInfos map[string]struct{}
	// ... child execution, request cancel, signal, signal-requested, CHASM node deltas ...
	NewBufferedEvents []*historypb.HistoryEvent; ClearBufferedEvents bool
	Tasks                 map[tasks.Category][]tasks.Task
	BestEffortDeleteTasks map[tasks.Category][]tasks.Key
	Condition int64; DBRecordVersion int64; Checksum *persistencespb.Checksum
}
```

`Tasks map[tasks.Category][]tasks.Task` is the outbox. The generic state-transition helper is `GetAndUpdateWorkflowWithNew` in `service/history/api/update_workflow_util.go`: it takes a closure that mutates Mutable State, then commits via `UpdateWorkflowExecutionAsActive`. Signal, Update, Start, timer firing, and `RespondWorkflowTaskCompleted` all funnel through it.

The commit is **two persistence operations, not one**:

1. `PersistWorkflowEvents` appends history nodes (unconditionally — see the persistence section).
2. `UpdateWorkflowExecution` writes Mutable State + all task rows + the shard fence in one conditional batch.

Consistency between the two is recovered by storing the identity of the latest event in Mutable State: an event is only "valid" if Mutable State references it, and on failure to persist Mutable State the shard reloads from persistence. The architecture doc states this explicitly and names the Matching handoff as transactional-outbox.

After a successful write:

```go
if persistence.OperationPossiblySucceeded(err) {
	// NotifyOnExecutionMutation / NotifyOnExecutionSnapshot ->
	//   engine.NotifyNewTasks(workflowMutation.Tasks)
	//   NotifyNewHistoryMutationEvent(lastFirstEventID, lastFirstEventTxnID, nextEventID)
}
```

`NotifyNewTasks` is what wakes the queue processors immediately rather than waiting for the next poll interval — the difference between millisecond and second-scale task dispatch.

---

### The event history: append-only, branched, and replay-critical

#### Events and the workflow-task cycle

History Events are the public face of Temporal's event sourcing. The type list is [`temporal/api/enums/v1/event_type.proto`](https://github.com/temporalio/api/blob/master/temporal/api/enums/v1/event_type.proto). The core cycle is three events per workflow task:

```
WorkflowExecutionStarted        <- StartWorkflowExecution
WorkflowTaskScheduled           <- server decides a task is needed
WorkflowTaskStarted             <- Matching delivered it to a poller (RecordWorkflowTaskStarted)
WorkflowTaskCompleted           <- worker responded
  ActivityTaskScheduled         <- one event per Command in that response
  TimerStarted
  ...
```

Event IDs are allocated by the history builder, now in its own package: [`service/history/historybuilder/history_builder.go`](https://github.com/temporalio/temporal/blob/main/service/history/historybuilder/history_builder.go) (plus `event_store.go` and `event_factory.go`). **`service/history/workflow/history_builder.go` does not exist.** `HistoryBuilder` is a two-field struct (`EventStore`, `EventFactory`) with a state machine — `HistoryBuilderStateMutable` → `Immutable` → `Sealed`, guarded by `assertMutable()` / `assertNotSealed()`.

The subtlety is **buffered events**. When a workflow task is in flight, external events (a signal arriving, an activity completing) cannot be given final IDs, because the worker is replaying against a history that does not include them and the resulting Commands must slot in deterministically. So they go into a buffer. `bufferEvent(eventType)` returns `false` — meaning "assign an ID immediately" — for workflow-state-change events, all five workflow-task events, every command-generated event (`ActivityTaskScheduled`, `TimerStarted`, `MarkerRecorded`, `StartChildWorkflowExecutionInitiated`, `SignalExternalWorkflowExecutionInitiated`, `UpsertWorkflowSearchAttributes`, `NexusOperationScheduled`, …), and the two Update message events. Everything else buffers.

`FlushBufferToCurrentBatch()` then reorders (completion events last), allocates IDs, wires `scheduledEventID` → `startedEventID` references, and appends. `Finish(flushBufferEvent bool)` produces a `HistoryMutation{DBEventsBatches, DBBufferBatch, DBClearBuffer, MemBufferBatch, ScheduledIDToStartedID, RequestIDToEventID}`, and the caller allocates one task ID per event batch via `ms.shard.GenerateTaskIDs(len(newEventsBatches))`, chaining `LastFirstEventId` / `LastFirstEventTxnId`.

**Why replay determinism is a server-side contract too.** It is easy to think of determinism as purely an SDK concern. It is not. The server must guarantee that a history, once written, is *stable*: the same event IDs, in the same order, with the same buffered-event placement, forever. That is why buffering exists, why `Sealed` is a state, why the builder panics rather than mutating a finished batch, and why `CloseTransactionAsSnapshot` hard-errors on buffered events (`"cannot generate workflow snapshot with buffered events"`). A server bug that reorders history is not a server bug — it is a customer's workflow throwing a non-determinism error in production and refusing to make progress.

#### Branches, forking, and reset

History is stored as a **tree**, not a list. Two tables:

- `history_node` — the actual event batches, keyed `(tree_id, branch_id, node_id, txn_id)` where `node_id` is the first event ID in the batch.
- `history_tree` — one row per branch, holding a serialized `HistoryTreeInfo`.

A **branch token** is an opaque serialized `persistencespb.HistoryBranch{TreeId, BranchId, Ancestors}` blob. It does not live in a standalone field on Mutable State — it lives *inside* `executionInfo.VersionHistories`, set via `versionhistory.SetVersionHistoryBranchToken`. That indirection exists because version histories are the multi-cluster conflict-resolution structure; branch identity and replication lineage are the same thing.

Forking is in [`common/persistence/history_manager.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/history_manager.go). `ForkHistoryBranch` rejects `ForkNodeID <= 1` with `"ForkNodeID must be > 1"`, mints a fresh `BranchId`, and computes the new ancestor list: if the fork point precedes the current branch's begin node, the ancestors *exclude* the forking branch (with the covering ancestor's `EndNodeId` truncated); otherwise the new branch inherits all ancestors plus `{BranchId, BeginNodeId, EndNodeId: ForkNodeID}`. `DeleteHistoryBranch` is reference-counted via `GetHistoryTreeContainingBranch`, tracking `usedBranches[branchId] = maxEndNodeId`, because a branch's nodes may be shared with its descendants.

Reading merges ranges: `readRawHistoryBranchAndFilter` walks `branch.Ancestors` plus a synthetic trailing range, and `filterHistoryNodes` applies the conflict rule — for the same node ID, the higher `txn_id` wins; a larger node ID with a lower `txn_id` is invalid (happens-before violation) and trips `softassert.UnexpectedDataLoss`. **That last one is a metric you want alerting on.**

Reset ([`service/history/api/resetworkflow/api.go`](https://github.com/temporalio/temporal/blob/main/service/history/api/resetworkflow/api.go)) is the customer-visible use of forking. It validates `WorkflowTaskFinishEventId > common.FirstEventID && < baseMutableState.GetNextEventID()`, takes `locks.PriorityHigh` on the base run *and* the current run if different, dedups on `CreateRequestId`, computes `baseRebuildLastEventID = WorkflowTaskFinishEventId - 1` plus the version-history coordinates, and delegates to `ndc.NewWorkflowResetter(...).ResetWorkflow(...)`. The result is a new run ID whose history is a fork of the old tree — which is why reset is cheap in storage and why resetting a 40 MB workflow does not double your storage.

#### Continue-as-new and the size limits

Continue-as-new is `UpdateWorkflowExecutionWithNewAsActive` in `context.go`: the old run closes as a **mutation**, the new run is written as a **snapshot**, in one transaction, with `SetSuccessorRunID` linking them and `mergeUpdateWithNewReplicationTasks` stamping `NewRunBranchToken`/`NewRunID` onto the trailing replication task. The new run gets a fresh history tree; the old one keeps its own.

The escape hatch exists because the server enforces hard limits in `UpdateWorkflowExecutionAsActive` via `enforceHistorySizeCheck`, `enforceHistoryCountCheck`, and `enforceMutableStateSizeCheck`. On breach the workflow is **force-terminated** with `common.FailureReasonHistorySizeExceedsLimit` / `…HistoryCountExceedsLimit` / `…MutableStateSizeExceedsLimit`.

| Key | Default | Meaning |
|---|---|---|
| `limit.historyCount.suggestContinueAsNew` | `4096` | Flips `ContinueAsNewSuggested` |
| `limit.historySize.suggestContinueAsNew` | `4 MiB` | Flips `ContinueAsNewSuggested` |
| `limit.historyCount.warn` | `10240` | Warning threshold |
| `limit.historySize.warn` | `10 MiB` | Warning threshold |
| `limit.historyCount.error` | `51200` | Force-terminate |
| `limit.historySize.error` | `50 MiB` | Force-terminate |
| `limit.mutableStateSize.warn` | `1 MiB` | |
| `limit.mutableStateSize.error` | `8 MiB` | Force-terminate |
| `limit.numPendingActivities.error` | `2000` | Also children, signals, cancels |

The warn/error numbers match the published limits exactly: "the Workflow Execution's Event History is limited to 51,200 Events or 50 MB and will warn you after 10,240 Events or 10 MB" ([Workflow Execution limits](https://docs.temporal.io/workflow-execution/limits)). The *suggestion* thresholds (4,096 events / 4 MiB) are **not published** — the docs name the `ContinueAsNewSuggested` flag but never state what flips it ([Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)). If a customer asks, the answer is in `constants.go`, and it is namespace-overridable.

The suggestion is delivered to the worker on the `WorkflowTaskStarted` event, via `AddWorkflowTaskStartedEvent(..., suggestContinueAsNew bool, historySizeBytes int64, ..., suggestContinueAsNewReasons []enumspb.SuggestContinueAsNewReason, ...)`.

---

### The internal task queues

**Terminology warning, stated in the repo itself:** these are *not* the Task Queues your customers poll. Those live in Matching. These are per-shard internal queues, an implementation detail of History. The doc says so in italics for good reason — this confusion causes real incidents when someone reads a "task queue backlog" alert and looks in the wrong service.

#### Categories

From [`service/history/tasks/category.go`](https://github.com/temporalio/temporal/blob/main/service/history/tasks/category.go):

```go
// WARNING: These IDS are persisted in the database. Do not change them.
const (
	CategoryIDTransfer    = 1
	CategoryIDTimer       = 2
	CategoryIDReplication = 3
	CategoryIDVisibility  = 4
	CategoryIDArchival    = 5
	CategoryIDMemoryTimer = 6
	CategoryIDOutbound    = 7
)
const ( _ CategoryType = iota; CategoryTypeImmediate; CategoryTypeScheduled )
```

| Category | ID | Type | Purpose |
|---|---|---|---|
| Transfer | 1 | Immediate | Push work to Matching (workflow tasks, activity tasks), close-execution, child/cancel/signal to other workflows |
| Timer | 2 | Scheduled | User timers, activity timeouts, workflow-task timeouts, run timeouts, activity retries, retention deletion |
| Replication | 3 | Immediate | Cross-cluster event and state replication |
| Visibility | 4 | Immediate | Upsert/close/delete in the visibility store |
| Archival | 5 | Scheduled | Move closed histories to blob storage |
| MemoryTimer | 6 | Scheduled | In-memory-only timers (speculative workflow-task timeouts) |
| Outbound | 7 | Immediate | Nexus operations and callbacks |

`NewDefaultTaskCategoryRegistry()` registers Transfer, Timer, Visibility, Replication, MemoryTimer, and Outbound. **Archival is registered conditionally at wiring time** in `temporal/fx.go`, only when `archivalMetadata.GetVisibilityConfig().StaticClusterState() == archiver.ArchivalEnabled`. `MutableTaskCategoryRegistry.AddCategory` panics on a duplicate ID.

Concrete task types live one-per-file under [`service/history/tasks/`](https://github.com/temporalio/temporal/tree/main/service/history/tasks) — `activity_task.go`, `workflow_task.go`, `close_task.go`, `requst_cancel_task.go` (yes, that is the real spelling in the repo), `signal_task.go`, `child_workflow_task.go`, `reset_task.go`, `delete_execution_task.go`, `user_timer.go`, `activity_task_timer.go`, `workflow_task_timer.go`, `workflow_run_timer.go`, `activity_retry_timer.go`, `workflow_delay_timer.go`, `workflow_cleanup_timer.go` (that is `DeleteHistoryEventTask`, the retention deleter), the four `*_visibility_task.go` files, `archive_execution_task.go`, `state_machine_task.go`, `chasm_task.go`, and six replication task files.

Task keys are two-dimensional. `tasks.Key{FireTime time.Time; TaskID int64}`; scheduled tasks use `NewKey(visibilityTimestamp, taskID)` and immediate tasks use `NewImmediateKey(taskID)`, which pins `FireTime` to `DefaultFireTime = time.Unix(0, 0).UTC()`. That is why immediate and scheduled queues share one framework.

#### The queue framework

[`service/history/queues/`](https://github.com/temporalio/temporal/tree/main/service/history/queues) is a generic, multi-cursor task processing framework. The old mental model — "each queue has an ack level, a single integer high-water mark" — is **wrong on current `main`** and has been for several releases. The real model:

- A **queue** (`queueBase` in [`queue_base.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/queue_base.go)) owns a `readerGroup`.
- A **reader** (`ReaderImpl` in `reader.go`) owns an ordered, disjoint list of **slices**.
- A **slice** (`SliceImpl` in `slice.go`) owns a `Scope{Range, Predicate}` — a task-key range plus a predicate over namespace ID, task type, destination, and outbound group.
- Reader 0 (`DefaultReaderId`) is the primary. Higher-numbered readers hold *demoted* work — tasks that are stuck or over-large, split off so they cannot block the primary cursor.

Progress is persisted as a structured `QueueState`, defined in [`proto/internal/temporal/server/api/persistence/v1/queues.proto`](https://github.com/temporalio/temporal/blob/main/proto/internal/temporal/server/api/persistence/v1/queues.proto):

```proto
message TaskKey        { google.protobuf.Timestamp fire_time = 1; int64 task_id = 2; }
message QueueSliceRange { TaskKey inclusive_min = 1; TaskKey exclusive_max = 2; }
message QueueSliceScope { QueueSliceRange range = 1; Predicate predicate = 2; }
message QueueReaderState { repeated QueueSliceScope scopes = 1; }
message QueueState {
  map<int64, QueueReaderState> reader_states = 1;
  TaskKey exclusive_reader_high_watermark = 2;
}
```

So an "ack level" today is `reader_states[0].scopes[0].range.inclusive_min` — the low edge of the primary reader's first slice. Everything above it may or may not be done; that is what the slices and predicates encode.

Checkpointing:

```go
func (p *queueBase) checkpoint() {
	var tasksCompleted int
	p.readerGroup.ForEach(func(_ int64, r Reader) { tasksCompleted += r.ShrinkSlices() })
	runAction(checkpointAction, p.readerGroup, p.metricsHandler)
	newExclusiveDeletionHighWatermark := p.nonReadableScope.Range.InclusiveMin
	for readerID, reader := range p.readerGroup.Readers() {
		scopes := reader.Scopes()
		if len(scopes) == 0 && readerID != DefaultReaderId { p.readerGroup.RemoveReader(readerID); continue }
		readerScopes[readerID] = scopes
		if len(scopes) != 0 { newExclusiveDeletionHighWatermark = tasks.MinKey(newExclusiveDeletionHighWatermark, scopes[0].Range.InclusiveMin) }
	}
	// NOTE: Must range-complete task first. Otherwise, if state is updated first, later deletion fails and the shard gets reloaded.
	p.resetCheckpointTimer(p.updateQueueState(tasksCompleted, readerScopes))
}
```

That trailing comment is the ordering invariant: delete task rows first, then persist the cursor. Reverse it and a failed delete leaves a cursor past undeleted rows, and the shard reloads.

`updateQueueState` calls `p.shard.SetQueueState(p.category, tasksCompleted, ...)`, which is itself a fenced `UpdateShard` — throttled by `history.shardUpdateMinInterval` (5m) and `history.shardUpdateMinTasksCompleted` (1000), so it is a compromise between reprocessing work after a reload and hammering the shard row.

Useful constants from `queue_base.go`: `maxPendingTaskMultiplier = 0.8`, `minMaxPendingTaskCount = 1000`, `queueIOTimeout = 5s`, `forceNewSliceDuration = 5m`, `timerQueuePersistenceMaxRPSRatio = 0.3`.

#### Executables, ack, and nack

An `Executable` ([`executable.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/executable.go)) wraps a task with attempt tracking, priority, and scheduling time. Key behaviours:

- `Ack()` is a no-op unless the task is `TaskStatePending`; then it moves to `TaskStateAcked` and records `metrics.TaskAttempt`.
- `Nack(err)` routes `consts.ErrResourceExhaustedBusyWorkflow` to a dedicated busy-workflow handler; otherwise if `shouldResubmitOnNack` it calls `scheduler.TrySubmit`, else it hands the task to the `Rescheduler` with a backoff derived from the error class.
- `IsRetryableError` is always `false` and `RetryPolicy()` is `backoff.DisabledRetryPolicy` — retry is the queue's job, not the task's.
- Constants: `resubmitMaxAttempts = 10`, `resourceExhaustedResubmitMaxAttempts = 1`, `taskCriticalLogMetricAttempts = 30`.
- A `CircuitBreakerExecutable` wraps a two-step circuit breaker and surfaces `RESOURCE_EXHAUSTED_CAUSE_CIRCUIT_BREAKER_OPEN` when open.

Scheduling is `tasks.NewFIFOScheduler` → `NewExecutionAwareScheduler` → `NewInterleavedWeightedRoundRobinScheduler`, keyed by `TaskChannelKey{NamespaceID, Priority}`. That IWRR keying is the per-namespace fairness mechanism inside a shard: one abusive namespace cannot monopolise the shard's task processing.

#### Active vs standby executors

For a global (replicated) namespace, the same task exists in both clusters. The dispatcher is [`queues/active_standby_executor.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/active_standby_executor.go): if the namespace is active in this cluster, run the active executor; otherwise run the standby one with `headers.CallerTypePreemptable`.

| Category | Active | Standby |
|---|---|---|
| Transfer | `transfer_queue_active_task_executor.go` | `transfer_queue_standby_task_executor.go` |
| Timer | `timer_queue_active_task_executor.go` | `timer_queue_standby_task_executor.go` |
| Visibility | `visibility_queue_task_executor.go` | none — always active |
| Archival | `archival_queue_task_executor.go` | none |
| Outbound | `outbound_queue_active_task_executor.go` | `outbound_queue_standby_task_executor.go` |

A standby executor performs **no side effects**. Its job is to verify that the replicated state has caught up; if it has not, it returns `consts.ErrTaskRetry` (recorded as `metrics.TaskStandbyRetryCounter`, rescheduled on `taskNotReadyReschedulePolicy`, and excluded from `shouldResubmitOnNack`). After `history.standbyTaskMissingEventsDiscardDelay` (15m) it may discard, but only after calling `checkExecutionStillExistsOnSourceBeforeDiscard`, which issues an `adminservice.DescribeMutableStateRequest{SkipForceReload: true}` against the source cluster. The identifier `historyResendHandler` that appears in older write-ups **does not exist on `main`**; a stale comment "resend history fron active side" [sic] survives in `executeWorkflowExecutionTimeoutTask`.

There are exactly two standby write paths, both in the timer executor: `executeActivityTimeoutTask` (creating the next activity timer via `UpdateWorkflowExecutionAsPassive`) and `executeActivityRetryTimerTask` (pushing to Matching directly, because activity retries do not produce history events).

#### The DLQ

A poison task cannot be allowed to block a shard cursor forever. `service/history/queues/dlq_writer.go` handles that. Triggers:

- `isUnexpectedNonRetryableError` — includes `serviceerror.DataLoss` and, when `history.TaskDLQInternalErrors` is on, internal errors.
- `unexpectedErrorAttempts >= history.TaskDLQUnexpectedErrorAttempts` (default **70**, doc-commented *"70 attempts takes about an hour"*).
- A regex match against `history.TaskDLQErrorPattern`.

The write happens on the *next* `Execute()` call, into `persistence.QueueKey{QueueType: QueueTypeHistoryDLQ, Category, SourceCluster, TargetCluster}` via `HistoryTaskQueueManager` over the generic `QueueV2` store. The log line is `"Task enqueued to DLQ"` and the metrics are `TaskTerminalFailures`, `TaskDLQFailures`, `TaskDLQSendLatency`, `DLQWrites`. Reading and replaying is `tdbg dlq read | list | purge | merge` — DLQ type integers are the persisted category IDs (transfer 1, timer 2, replication 3, visibility 4).

#### Queue knobs

| Key | Default |
|---|---|
| `history.transferProcessorMaxPollRPS` | `20` |
| `history.transferProcessorMaxPollInterval` | `1m` |
| `history.transferProcessorUpdateAckInterval` | `30s` |
| `history.transferQueueMaxReaderCount` | `2` |
| `history.timerProcessorMaxPollRPS` | `20` |
| `history.timerProcessorMaxPollInterval` | `5m` |
| `history.timerProcessorUpdateAckInterval` | `30s` |
| `history.timerProcessorMaxTimeShift` | `1s` |
| `history.timerProcessorSchedulerWorkerCount` | `512` |
| `history.timerQueueMaxReaderCount` | `2` |
| `history.visibilityProcessorMaxPollRPS` | `20` |
| `history.queuePendingTaskCriticalCount` | `9000` |
| `history.queuePendingTasksMaxCount` | `10000` |
| `history.queueCriticalSlicesCount` | `50` |
| `history.queueReaderStuckCriticalAttempts` | `3` |
| `history.taskSchedulerMaxQPS` | `0` (unlimited) |
| `history.taskSchedulerGlobalMaxQPS` | `0` |
| `history.taskSchedulerNamespaceMaxQPS` | `0` |
| `history.taskSchedulerEnableRateLimiter` | `false` |
| `history.TaskDLQEnabled` | `true` |
| `history.TaskDLQUnexpectedErrorAttempts` | `70` |

Note `history.queuePendingTasksMaxCount` is **plural** while `history.queuePendingTaskCriticalCount` is **singular** — two distinct knobs, and a classic typo target. `history.taskSchedulerWorkerCount` does not exist here (it is a Cadence key); the worker counts are per-category. `history.queueMaxReaderCount` does not exist either; it is per-category (`history.transferQueueMaxReaderCount`, `history.timerQueueMaxReaderCount`, …).

#### Mitigation: what happens when a queue falls behind

`monitor.go` raises typed alerts — `AlertTypeQueuePendingTaskCount`, `AlertTypeReaderStuck`, `AlertTypeSliceCount` — and `mitigator.go` responds with `Action`s:

- **`queue-pending-task`** — target load factor 0.8. Split slices by predicate and demote them into reader `readerID+1`, pausing that reader for `clearSliceThrottleDuration = 10s`. At `maxReaderCount-1` it clears whole slices outright (dropping them from memory; they get re-read later).
- **`reader-stuck`** — demotes a reader whose watermark has not moved for `history.queueReaderStuckCriticalAttempts` windows. Tracked **only for reader 0 of scheduled queues**.
- **slice-count / move-group** actions run from `checkpoint()` rather than from alerts.

There is also a lossy escape hatch: if a slice's predicate grows past `history.queueMaxPredicateSize` (10 KB) or `history.queueShrinkPredicateMaxPendingKeys` (10), the predicate is replaced with `predicates.Universal[tasks.Task]()` and `metrics.QueuePredicateResolutionLoss` is emitted with a reason tag of `max_pending_keys` or `predicate_size`. The queue then re-reads tasks it had already filtered out. **That metric firing is a real signal that a shard is in trouble.**

---
### Matching: partitions, forwarding, and the two kinds of match

#### Partitions and the mangled name

A customer Task Queue is one logical name. Internally it is a set of **partitions**, each owned by a Matching host, each with its own row in `task_queues` and its own slice of the `tasks` table. Encoding lives in [`common/tqid/task_queue_id.go`](https://github.com/temporalio/temporal/blob/main/common/tqid/task_queue_id.go) — **not `common/tqname`, which was renamed**:

```go
const (
	// nonRootPartitionPrefix is the prefix for all mangled task queue names.
	nonRootPartitionPrefix = "/_sys/"
	partitionDelimiter     = "/"
)

// RPC names look like this:
//  sticky partition:            <sticky name>
//	root normal partition: 	     <task queue name>
//	non-root normal partition:   /_sys/<task queue name>/<partition id>
func (p *NormalPartition) RpcName() string {
	if p.IsRoot() { return p.TaskQueue().family.Name() }
	return nonRootPartitionPrefix + p.TaskQueue().Name() + partitionDelimiter + strconv.Itoa(p.partitionId)
}
```

Partition 0 is the **root** and uses the bare name — which is why a task queue with 4 partitions has rows named `orders`, `/_sys/orders/1`, `/_sys/orders/2`, `/_sys/orders/3`. `parseRpcName` rejects `partition <= 0` after the prefix and rejects a user-supplied base name that itself begins with `/_sys/`, so customers cannot forge partition names.

Three `Partition` implementations: `NormalPartition`, `StickyPartition`, and `WorkerCommandsPartition`. Versioned physical queues add further suffixes ([`service/matching/physical_task_queue_key.go`](https://github.com/temporalio/temporal/blob/main/service/matching/physical_task_queue_key.go), with `versionSetDelimiter = ":"`, `buildIdDelimiter = "#"`, `deploymentNameDelimiter = "|"`):

```
/_sys/<base>/<b64 deploymentName>|<b64 buildID>#<partition id>
/_sys/<base>/<b64 buildID>#<partition id>
/_sys/<base>/<version set id>:<partition id>
```

#### How a task picks a partition

This surprises people: **it is not a hash of the workflow ID.** Partition selection happens in the *client*, in [`client/matching/loadbalancer.go`](https://github.com/temporalio/temporal/blob/main/client/matching/loadbalancer.go), and is random or backlog-weighted:

```go
// pickWritePartitionByGap picks a partition with probability proportional to how far its backlog
// is below backlogCap. Falls back to uniform random if any of these are true:
//   - every partition is at or above the backlogCap
//   - backlogCap is 0
//   - when backlog data is not available for all write partitions
func pickWritePartitionByGap(counts []number.Compact8, partitionCount int, backlogCap int64) int {
	if backlogCap == 0 || len(counts) < partitionCount { return rand.Intn(partitionCount) }
	var total int64
	for i := range partitionCount {
		if gap := backlogCap - number.DecodeCompact8(counts[i]); gap > 0 { total += gap }
	}
	if total <= 0 { return rand.Intn(partitionCount) }  // all partitions at or above cap
	r := rand.Int63n(total)
```

Reads are symmetrical: `pickReadPartition` uses backlog weighting when data is complete, otherwise `pickReadPartitionWithFewestPolls`, returning a `pollToken`. Load balancing is disabled for forwarded requests and sticky queues:

```go
loadBalance := p.SupportsPartitions() && p.IsRoot() && forwardedFrom == ""
```

Host ownership is `c.clients.Lookup(p.RoutingKey(spread))`, where a `NormalPartition`'s routing key is `"<nsID>:<rpcName>:<taskType>"` (or batched by `partitionId / matching.spreadRoutingBatchSize`). The **only** hash-of-name in the whole path is the sticky-to-normal user-data parenting in `user_data_manager.go`: `int(farm.Fingerprint32([]byte(p.RpcName()))) % m.config.NumReadPartitions()`.

#### The forwarding tree

Partitions form a tree so that a poller on an empty partition can still receive a task sitting on a different partition. The parent computation is a two-liner:

```go
// ParentPartition returns a NormalPartition for the parent partition, using the given branching degree.
func (p *NormalPartition) ParentPartition(degree int) (*NormalPartition, error) {
	if p.IsRoot() {
		return nil, ErrNoParent
	} else if degree < 1 {
		return nil, ErrInvalidDegree
	}
	parent := (p.partitionId+degree-1)/degree - 1
	return p.taskQueue.NormalPartition(parent), nil
}
```

Degree is `matching.forwarderMaxChildrenPerNode`, default **20**, re-read on every forward. With the default 4 partitions, that is a depth-2 star: partitions 1–3 all forward directly to root 0. With hundreds of partitions it becomes a real tree.

The repo's own [matching-service.md](https://github.com/temporalio/temporal/blob/main/docs/architecture/matching-service.md) states the load consequence:

> "If a root partition of a Task Queue is loaded, this will force all other partitions of that Task Queue to also load. This ensures that forwarding can occur between a child partition with a task in its backlog and a long-awaited poller."

That is implemented as `taskQueuePartitionManagerImpl.ForceLoadAllChildPartitions()` (root-only), which fires `ForceLoadTaskQueuePartition` at every child and records `metrics.ForceLoadedTaskQueuePartitions`.

Forwarding is rate-limited (`matching.forwarderMaxRatePerSecond`, default 10.0) and concurrency-limited by token channels sized `matching.forwarderMaxOutstandingPolls` and `matching.forwarderMaxOutstandingTasks`, both default **1**. When the limiter denies, you get `errForwarderSlowDown`. The new priority matcher spawns one `forwardTasks` goroutine per outstanding task slot and one `forwardPolls` per poll slot, and carries the comment `// TODO(pri): ForwarderMaxOutstandingTasks > 1 is not supported`.

Critically, forwarding is **gated on backlog**. In `MustOffer`, if `!tm.isBacklogNegligible()` and `timeSinceLastPoll() < matching.maxWaitForPollerBeforeFwd` (200ms), the forward token channel is set to `nil` and a reconsider timer is set. This keeps leaf and root partitions draining at the same rate rather than funnelling everything through root.

#### Sync match vs spooled match

The classic matcher ([`service/matching/matcher.go`](https://github.com/temporalio/temporal/blob/main/service/matching/matcher.go)) is a rendezvous on **unbuffered channels**:

```go
// MustOffer blocks until a consumer is found to handle this task
// Returns error only when context is canceled or the ratelimit is set to zero (allow nothing)
// The passed in context MUST NOT have a deadline associated with it
// Note that calling MustOffer is the only way that matcher knows there are spooled tasks in the
// backlog, in absence of a pending MustOffer call, the forwarding logic assumes that backlog is empty.
func (tm *TaskMatcher) MustOffer(ctx context.Context, task *internalTask, interruptCh <-chan struct{}) error {
	tm.registerBacklogTask(task)
	defer tm.unregisterBacklogTask(task)
	if err := tm.rateLimiter.Wait(ctx); err != nil { return err }
	task.recycleToken = tm.recycleToken
	select {
	case tm.taskC <- task:
		tm.emitDispatchLatency(task, false)
```

`taskC` and `queryTaskC` are separate channels — deliberately, so a namespace that is not active in this cluster can still serve queries. `Offer` is the non-blocking variant: try `taskC`, else try to forward, else fall through. `Poll` is a hand-written prioritised select: context/close first, then local tasks, then forwarding (skipped when the backlog is not negligible), then block.

The **sync match** is the fast path: a task handed directly from `Offer`/`MustOffer` into a waiting poller's channel. It never touches the database. The **async / spooled match** is what happens when nobody is waiting:

```
taskQueuePartitionManagerImpl.AddTask
  -> syncMatchQueue.TrySyncMatch(...)      // sync match attempt
     on failure:
  -> spoolQueue.SpoolTask(params.taskInfo)
     -> backlogManagerImpl.SpoolTask
        -> taskWriter.appendTask           // INSERT into `tasks`
        -> taskReader.Signal()
```

`taskWriter.appendCh` is sized `matching.outstandingTaskAppendsThreshold` (default 250); when full you get `TaskWriteThrottlePerTaskQueueCounter` and a `ResourceExhausted{RESOURCE_EXHAUSTED_CAUSE_SYSTEM_OVERLOADED, SCOPE_NAMESPACE, "Too many outstanding appends to the task queue"}`. On the read side, `taskReader.getTasksPump` calls `db.GetTasks(ctx, subqueueZero, readLevel+1, maxReadLevel+1, GetTasksBatchSize())` (default batch 1000) into a buffer of `GetTasksBatchSize()-1`, then dispatches through `MustOffer`.

**This is the single most important operational distinction in Matching.** A sync match is one in-memory channel send. An async match is an INSERT, a SELECT, a dispatch, and later a DELETE — four database operations for one task. Temporal's own scaling guidance treats the ratio as a top-line SLI: Poll Sync Rate should be at or above 99%, computed as `sum by (task_type)(rate(poll_success_sync[1m])) / sum by (task_type)(rate(poll_success[1m]))`, because async match "increases the load on the persistence database and is a lot less efficient" ([Scaling Temporal: The Basics](https://temporal.io/blog/scaling-temporal-the-basics)).

The new matcher (`pri_matcher.go` with `matcher_data.go`) is the default on `main` — `matching.useNewMatcher = StaticGradualChange(true)` — replacing channel rendezvous with a B-tree ordered by effective priority and fair level, an intrusive poller list, and GCRA rate limiting. Forwarders appear in it as pseudo-pollers (`waitingPoller{taskForwarderType: parentTaskForwarder}`). The classic `TaskMatcher` remains behind `// TODO(pri): old matcher cleanup`. Fairness scheduling (`matching.enableFairness`, default `false`, "Implies matching.useNewMatcher") adds a `tasks_v2` table with a stride-scheduling `pass` column.

#### Matching has its own range-ID fence

This is the detail most people miss: Matching does the same lease trick as History, on the `task_queues` row. From [`service/matching/db.go`](https://github.com/temporalio/temporal/blob/main/service/matching/db.go) and `backlog_manager.go`:

```go
const ( initialRangeID = 1 /* Id of the first range of a new task queue */ )

func (db *taskQueueDB) updateTaskQueueLocked(ctx context.Context, incrementRangeId bool) error {
	newRangeID := db.rangeID
	if incrementRangeId { newRangeID++ }
	if _, err := db.store.UpdateTaskQueue(ctx, &persistence.UpdateTaskQueueRequest{
		RangeID:       newRangeID,
		TaskQueueInfo: db.cachedQueueInfo(),
		PrevRangeID:   db.rangeID,     // <- LWT / conditional-update fence
	}); err != nil { return err }
	db.lastWrite = time.Now(); db.rangeID = newRangeID
	return nil
}

func rangeIDToTaskIDBlock(rangeID int64, rangeSize int64) taskIDBlock {
	return taskIDBlock{ start: (rangeID-1)*rangeSize + 1, end: rangeID * rangeSize }
}
```

`RangeSize` is a compile-time `100000` field in `service/matching/config.go` — **`matching.rangeSize` is not a config key.** On loss:

```go
if response.RangeID != db.rangeID {
	return &persistence.ConditionFailedError{Msg: "task queue ownership lost: stored rangeID %d, in-memory rangeID %d"}
}
```

which increments `ConditionFailedErrorPerTaskQueueCounter`, sets `skipFinalUpdate`, and calls `UnloadFromPartitionManager(unloadCauseConflict)`. `taskWriter.assignTaskIDs` additionally guards contiguity with `errNonContiguousBlocks` if `currBlock.end != prevBlockEnd`.

#### Managers and backlog accounting

The types were renamed in v1.21 and the old names appear all over the internet: `taskQueueManager` / `taskQueueManagerImpl` are gone, replaced by **`taskQueuePartitionManagerImpl`** (one per partition) and **`physicalTaskQueueManagerImpl`** (one per DB-level queue within a partition). The `service/matching/README.md` explains the nesting: "each Task Queue partition is made of one or more DB-level queues. There is always a default DB queue. For versioned TQs, there is an additional DB queue for each Build ID."

Three backlog manager implementations are selected in `newPhysicalTaskQueueManager`: `backlogManagerImpl` (classic: taskWriter + taskReader + ackManager + taskGC), `priBacklogManagerImpl` (one `priTaskReader` per priority subqueue), and `fairBacklogManagerImpl` (over the `FairTaskManager`).

Task completion is not free. On start failure the task is **re-appended with a higher task ID** (`metrics.TaskRewrites`); then the ack manager advances, `db.updateBacklogStats(-numAcked, backlogHead)` runs, and `taskGC.Run(ackLevel)` batches `CompleteTasksLessThan` calls governed by `matching.maxTaskDeleteBatchSize` and `matching.taskDeleteInterval`.

`approximateBacklogCount` is per-subqueue in `taskQueueDB`, incremented on `CreateTasks`, decremented on ack, and **reset to 0 whenever `ackLevel == maxReadLevel`**. Under-counting is defended explicitly:

```go
if *count+countDelta < 0 {
	// log "ApproximateBacklogCount could have under-counted."
	*count = 0
}
```

Two different "backlog age" numbers exist and they mean different things: `taskReader.getBacklogHeadAge()` (from the DB head's create time) and `TaskMatcher.getBacklogAge()` (over tasks currently blocked in `MustOffer`, used by `isBacklogNegligible()`). The CLI surfaces the former.

#### Sticky task queues

A sticky queue is a per-worker cache queue: after a worker executes a workflow task, the server sends subsequent tasks for that run back to the *same* worker so it can reuse its in-memory workflow state instead of replaying history. Server-side, `StickyPartition` is deliberately degenerate — `RpcName()` is the raw sticky name, `IsRoot()` false, `SupportsPartitions()` false, `SupportsFairness()` false, `SupportsVersioning()` false, `MetricTag()` is the literal `"__sticky__"`, and `PersistenceTTL()` is `24 * time.Hour`, which becomes `expiry_time` on the `task_queues` row.

The window after which the server gives up on a sticky worker is **hard-coded, not configurable**:

```go
stickyPollerUnavailableWindow = 10 * time.Second
// "If sticky poller is not seen in last 10s, we treat it as sticky worker unavailable...
//  default sticky schedule_to_start timeout is 5s"
```

**`matching.stickyPollerUnavailableWindow` is not a config key** (its live neighbour `matching.queryPollerUnavailableWindow`, 20s, is). When the check fails, `serviceerrors.NewStickyWorkerUnavailable()` is returned, and on the History side `transfer_queue_active_task_executor.go processWorkflowTask` re-pushes to the normal queue. `workflow_task_state_machine.go` also does the right thing on failure so a dead sticky worker does not burn retry attempts:

```go
if m.ms.IsStickyTaskQueueSet() { incrementAttempt = false; m.ms.ClearStickyTaskQueue() }
```

Versioning bounces sticky too: `task_queue_partition_manager.go` returns `StickyWorkerUnavailable` when the partition kind is sticky and the target deployment no longer matches, and `checkVersionForStickyAdd` does the same when a build ID stops being its set's default.

#### Versioning: all three generations coexist

On `main`, all three worker-versioning schemes exist simultaneously. The persisted shape is in `proto/internal/temporal/server/api/persistence/v1/task_queues.proto`:

```proto
message VersioningData {
  repeated CompatibleVersionSet version_sets = 1;   // v1
  repeated AssignmentRule       assignment_rules = 2;  // v2
  repeated RedirectRule         redirect_rules = 3;    // v2
}
message TaskQueueUserData {
  HybridLogicalClock clock;
  VersioningData versioning_data;
  map<int32, TaskQueueTypeUserData> per_type;   // v3 deployment data lives here
}
```

- **v1, version sets** — [`service/matching/version_sets.go`](https://github.com/temporalio/temporal/blob/main/service/matching/version_sets.go) plus `version_sets_merge.go` (HLC-based merge for replication). Marked deprecated, `// TODO: [cleanup-old-wv]`.
- **v2, build-ID assignment and redirect rules** — `version_rule_helpers.go` (`AddCompatibleRedirectRule`, `FindAssignmentBuildId`, `CommitBuildID`, `CleanupRuleTombstones`) and `reachability.go` for build-ID reachability.
- **v3, worker deployments** — `physical_task_queue_key.go` (`DeploymentQueueKey`, `WorkerDeploymentVersionS()`), registration through `ensureRegisteredInDeploymentVersion`, pinned routing in `task_queue_partition_manager.go`, helper package `common/worker_versioning/`. Gated by `EnableDeployments` and `EnableDeploymentVersions`.

User data ownership is a small distributed system of its own: **only the root workflow partition owns the row.** Everyone else long-polls their forwarding parent via `GetTaskQueueUserData` with `WaitNewData`; root non-workflow partitions poll the root workflow partition; sticky partitions poll `farm.Fingerprint32(stickyName) % NumReadPartitions`. This is why the matching proto comment says `GetTaskQueueUserData` "should always be routed to the node holding the root partition of the workflow task queue."

#### Why a hot task queue is a partition problem

Put the pieces together. A single Task Queue's throughput ceiling is:

```
throughput ≈ (partitions) × (per-partition sync-match rate)
             + (partitions) × (per-partition spool rate, bounded by DB)
```

The default is **4 read and 4 write partitions** (`matching.numTaskqueueReadPartitions`, `matching.numTaskqueueWritePartitions`), and each partition is owned by exactly one Matching host. So one Task Queue can use at most 4 Matching hosts by default, no matter how many you run. If a customer drives a million tasks per second at one Task Queue name, that is not a "Matching is undersized" problem — it is 4 partitions doing all the work while your other 13 Matching hosts idle. The fix is more partitions for that namespace/task queue, which is a per-namespace dynamic config override, and the cost is more `task_queues` rows, more forwarding hops, and more poller spread.

The second failure mode is **poller starvation with a low partition count relative to poller count**: pollers distribute across partitions, so if a customer runs 2 workers against a 4-partition queue, two partitions have no pollers and rely entirely on forwarding, which is capped at `matching.forwarderMaxRatePerSecond = 10`. Symptom: tasks sitting in backlog while pollers idle.

The matching knobs worth memorising:

| Key | Default |
|---|---|
| `matching.numTaskqueueReadPartitions` | `4` (1 for per-namespace-worker and system-local activity queues) |
| `matching.numTaskqueueWritePartitions` | `4` (same constraint) |
| `matching.forwarderMaxChildrenPerNode` | `20` |
| `matching.forwarderMaxOutstandingPolls` | `1` |
| `matching.forwarderMaxOutstandingTasks` | `1` |
| `matching.forwarderMaxRatePerSecond` | `10` |
| `matching.getTasksBatchSize` | `1000` |
| `matching.outstandingTaskAppendsThreshold` | `250` |
| `matching.longPollExpirationInterval` | `1m` |
| `matching.maxTaskQueueIdleTime` | `5m` |
| `matching.syncMatchWaitDuration` | `200ms` |
| `matching.backlogNegligibleAge` | `5s` |
| `matching.maxWaitForPollerBeforeFwd` | `200ms` |
| `matching.backlogTaskForwardTimeout` | `60s` |
| `matching.useNewMatcher` | `true` (gradual) |
| `matching.enableFairness` | `false` (gradual) |

---

### Persistence: stores, schemas, and where the amplification is

#### The layering

Two tiers. The **manager** tier (`common/persistence/data_interfaces.go`) speaks protos; the **store** tier (`common/persistence/persistence_interface.go`) speaks `Internal*` structs whose payloads are `*commonpb.DataBlob`. The manager owns the `serialization.Serializer`, so Cassandra and SQL plugins share zero serialization code.

Managers: `ShardManager`, `ExecutionManager`, `TaskManager` (+ alias `FairTaskManager`), `MetadataManager`, `ClusterMetadataManager`, `NexusEndpointManager`, `HistoryTaskQueueManager`, and `NamespaceReplicationQueue` (in its own file). Stores: `ExecutionStore`, `ShardStore`, `TaskStore`, `MetadataStore`, `ClusterMetadataStore`, `Queue` (legacy), `QueueV2`, `NexusEndpointStore`.

`ExecutionStore` is the one to read; it is the whole History service's contract with the database:

```go
// ExecutionStore is used to manage workflow execution including mutable states / history / tasks.
ExecutionStore interface {
	Closeable
	GetName() string
	GetHistoryBranchUtil() HistoryBranchUtil

	// The below three APIs are related to serialization/deserialization
	CreateWorkflowExecution(ctx, *InternalCreateWorkflowExecutionRequest) (*InternalCreateWorkflowExecutionResponse, error)
	UpdateWorkflowExecution(ctx, *InternalUpdateWorkflowExecutionRequest) error
	ConflictResolveWorkflowExecution(ctx, *InternalConflictResolveWorkflowExecutionRequest) error

	DeleteWorkflowExecution / DeleteCurrentWorkflowExecution / GetCurrentExecution
	GetWorkflowExecution / SetWorkflowExecution / ListConcreteExecutions

	AddHistoryTasks / GetHistoryTasks / CompleteHistoryTask / RangeCompleteHistoryTasks

	PutReplicationTaskToDLQ / GetReplicationTasksFromDLQ / DeleteReplicationTaskFromDLQ
	RangeDeleteReplicationTaskFromDLQ / IsReplicationDLQEmpty

	// The below are history V2 APIs
	// V2 regards history events growing as a tree, decoupled from workflow concepts
	AppendHistoryNodes / DeleteHistoryNodes / ReadHistoryBranch
	ForkHistoryBranch / DeleteHistoryBranch
	GetHistoryTreeContainingBranch / GetAllHistoryTreeBranches
}
```

`TaskStore` is Matching's equivalent: `CreateTaskQueue`, `GetTaskQueue`, `UpdateTaskQueue`, `ListTaskQueue`, `DeleteTaskQueue`, `CreateTasks`, `GetTasks`, `CompleteTasksLessThan`, plus the user-data set (`GetTaskQueueUserData`, `UpdateTaskQueueUserData`, `ListTaskQueueUserDataEntries`, `GetTaskQueuesByBuildId`, `CountTaskQueuesByBuildId`).

#### The decorator chain

Wired in [`common/persistence/client/factory.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/client/factory.go):

```go
func (f *factoryImpl) NewExecutionManager() (persistence.ExecutionManager, error) {
	store, err := f.dataStoreFactory.NewExecutionStore()
	...
	result := persistence.NewExecutionManager(store, f.serializer, f.eventBlobCache, f.logger,
		f.config.TransactionSizeLimit, f.enableBestEffortDeleteTasksOnWorkflowUpdate)
	if f.systemRateLimiter != nil && f.namespaceRateLimiter != nil {
		result = persistence.NewExecutionPersistenceRateLimitedClient(result,
			f.systemRateLimiter, f.namespaceRateLimiter, f.shardRateLimiter, f.logger)
	}
	if f.metricsHandler != nil && f.healthSignals != nil {
		result = persistence.NewExecutionPersistenceMetricsClient(result, f.metricsHandler,
			f.healthSignals, f.logger, f.enableDataLossMetrics)
	}
	result = persistence.NewExecutionPersistenceRetryableClient(result, retryPolicy, IsPersistenceTransientError)
	return result, nil
}
```

Wrap order innermost to outermost: **driver store → fault injection → OTel telemetry → Manager (serialization) → rate limited → metrics → retryable.** Call order reverses, which has three consequences you must internalise for debugging:

1. **Metrics and health signals are recorded per retry attempt**, not per logical operation. A `persistence_latency` p99 spike may be three retries of the same request.
2. **Retries consume rate-limiter quota.** A degraded database therefore consumes more quota, which throttles healthy traffic. This is a positive feedback loop.
3. **Rate-limit rejections are not retried** — `IsPersistenceTransientError` matches only `serviceerror.Unavailable` and `serviceerror.DataLoss`.

Admission order inside the limiter is shard → namespace → system, producing `ErrPersistenceNamespaceShardLimitExceeded`, `ErrPersistenceNamespaceLimitExceeded`, `ErrPersistenceSystemLimitExceeded` respectively. There is an adaptive limiter (`common/persistence/client/health_request_rate_limiter.go`) with `RateBackoffStepSize: 0.3`, `RateIncreaseStepSize: 0.1`, `RateMultiMin: 0.8`, `RateMultiMax: 1.0` — but note it **ships disabled** (`Enabled: false`) and both trigger thresholds default to `0.0`, which disables both triggers anyway. If you want adaptive persistence throttling on your cells, you must turn it on deliberately and set thresholds.

#### The Cassandra schema

Version `1.13` (`schema/cassandra/version.go`); versioned migrations under `schema/cassandra/temporal/versioned/v1.0` … `v1.13`. The central table, verbatim from [`schema/cassandra/temporal/schema.cql`](https://github.com/temporalio/temporal/blob/main/schema/cassandra/temporal/schema.cql):

```sql
CREATE TABLE executions (
  shard_id                       int,
  type                           int, -- enum RowType { Shard, Execution, TransferTask, TimerTask, ReplicationTask, VisibilityTask}
  namespace_id                   uuid,
  workflow_id                    text,
  run_id                         uuid,
  current_run_id                 uuid,
  visibility_ts                  timestamp, -- unique identifier for timer tasks for an execution
  task_id                        bigint, -- unique identifier for transfer and timer tasks for an execution
  shard blob, execution blob, execution_state blob,   -- + a *_encoding text column each
  transfer blob, timer blob, replication blob,        -- one payload column per task category
  visibility_task_data blob, task_data blob,          -- task_data = the generic history-task column
  next_event_id                  bigint,  -- This is needed to make conditional updates on session history
  range_id                       bigint,  -- Increasing sequence identifier for transfer queue, checkpointed into shard info
  activity_map map<bigint, blob>, timer_map map<text, blob>,     -- + *_encoding text each
  child_executions_map map<bigint, blob>, request_cancel_map map<bigint, blob>,
  signal_map map<bigint, blob>, signal_requested set<uuid>,
  chasm_node_map map<text, blob>, -- Map from path to CHASM node blob
  buffered_events_list           list<frozen<serialized_event_batch>>,
  workflow_last_write_version    bigint,
  workflow_state                 int,
  checksum                       blob,
  checksum_encoding              text,
  db_record_version              bigint,
  PRIMARY KEY  (shard_id, type, namespace_id, workflow_id, run_id, visibility_ts, task_id)
) WITH COMPACTION = {
    'class': 'org.apache.cassandra.db.compaction.LeveledCompactionStrategy'
  };
```

**Stare at that primary key.** The partition key is `shard_id` *alone*. Everything about a shard — its lease row, every workflow's mutable state, every transfer task, every timer task, every replication task, every visibility task — lives in a single Cassandra partition. That is what makes an atomic conditional batch legal, and it is also the single biggest reason shard count is your throughput ceiling.

The row-type discriminator in the CQL comment is **stale and incomplete** (it omits DLQ). The authoritative values are the `iota` block in `common/persistence/cassandra/execution_store.go`:

```go
const (
	// Row types for table executions
	rowTypeShard = iota           // 0
	rowTypeExecution              // 1
	rowTypeTransferTask           // 2
	rowTypeTimerTask              // 3
	rowTypeReplicationTask        // 4
	rowTypeDLQ                    // 5
	rowTypeVisibilityTask         // 6
	// NOTE: the row type for history task is the task category ID
	// rowTypeHistoryTask
)
```

Values `>= 7` are **task category IDs** used by the generic history-task path. Non-task rows use sentinel UUIDs and IDs so they sort predictably: `permanentRunID = "30000000-0000-f000-f000-000000000001"` for the current-execution row, `rowTypeExecutionTaskID = -10`, `rowTypeShardTaskID = -11`, and `defaultVisibilityTimestamp` fixed at 2000-01-01.

History is separate and is a tree:

```sql
CREATE TABLE history_node (
  tree_id           uuid, -- run_id if no reset, otherwise run_id of first run
  branch_id         uuid, -- changes in case of reset workflow. Conflict resolution can also change branch id.
  node_id           bigint, -- == first eventID in a batch of events
  txn_id            bigint, -- in case of multiple transactions on same node, we utilize highest transaction ID. Unique.
  prev_txn_id       bigint, -- point to the previous node: event chaining
  data                blob, -- batch of workflow execution history events as a blob
  data_encoding       text, -- protocol used for history serialization
  PRIMARY KEY ((tree_id), branch_id, node_id, txn_id )
) WITH CLUSTERING ORDER BY (branch_id ASC, node_id ASC, txn_id DESC)
  AND COMPACTION = { 'class': 'org.apache.cassandra.db.compaction.LeveledCompactionStrategy' };

CREATE TABLE history_tree (
  tree_id               uuid,
  branch_id             uuid,
  branch                blob,
  branch_encoding       text,
  PRIMARY KEY ((tree_id), branch_id )
) WITH COMPACTION = { 'class': 'org.apache.cassandra.db.compaction.LeveledCompactionStrategy' };
```

History nodes are partitioned by `tree_id`, **not** by shard. That is deliberate: history is append-only and read-heavy, and keeping it out of the shard partition prevents a 50 MB workflow from bloating the hot partition.

Matching's tables:

```sql
-- Stores activity or workflow tasks
CREATE TABLE tasks (
  namespace_id        uuid,
  task_queue_name     text,
  task_queue_type     int, -- enum TaskQueueType {ActivityTask, WorkflowTask}
  type                int, -- enum rowType {Task, TaskQueue} and subqueue id
  task_id             bigint,  -- unique identifier for tasks, monotonically increasing
  range_id            bigint, -- Used to ensure that only one process can write to the table
  task                blob,
  task_encoding       text,
  task_queue          blob,
  task_queue_encoding text,
  PRIMARY KEY ((namespace_id, task_queue_name, task_queue_type), type, task_id)
) WITH COMPACTION = { 'class': 'org.apache.cassandra.db.compaction.LeveledCompactionStrategy' };
```

**There is no `task_queues` table on Cassandra.** The task-queue metadata row lives in `tasks` itself, discriminated by `type` with the sentinel `taskQueueTaskID = -12345`. There is also a `tasks_v2` with a `pass bigint` clustering column for fairness (stride scheduling).

Plus `cluster_metadata_info`, `namespaces`, `namespaces_by_id`, `task_queue_user_data`, `queue_metadata`, `queue`, `queues`, `queue_messages`, `nexus_endpoints`, `cluster_membership`, and the `serialized_event_batch` UDT.

#### The SQL schema

Only `schema/postgresql/v12/` exists (no bare `schema/postgresql/temporal/`); versions are in `schema/postgresql/v12/version.go`: `Version = "1.19"`, `VisibilityVersion = "1.14"`. MySQL 8 is identical in structure (`schema/mysql/v8/version.go`, same version strings). Thirty-seven tables each.

The SQL model is **structurally different** from Cassandra, and that difference is the thing to remember:

```sql
CREATE TABLE shards (            -- on Cassandra this is `executions` rows with type=0
  shard_id INTEGER NOT NULL, range_id BIGINT NOT NULL,
  data BYTEA NOT NULL, data_encoding VARCHAR(16) NOT NULL,
  PRIMARY KEY (shard_id));

CREATE TABLE executions(
  shard_id INTEGER NOT NULL, namespace_id BYTEA NOT NULL,
  workflow_id VARCHAR(255) NOT NULL, run_id BYTEA NOT NULL,
  next_event_id BIGINT NOT NULL, last_write_version BIGINT NOT NULL,
  data BYTEA NOT NULL, data_encoding VARCHAR(16) NOT NULL,       -- WorkflowExecutionInfo
  state BYTEA NOT NULL, state_encoding VARCHAR(16) NOT NULL,     -- WorkflowExecutionState
  db_record_version BIGINT NOT NULL DEFAULT 0,                   -- the CAS condition
  PRIMARY KEY (shard_id, namespace_id, workflow_id, run_id));

-- a real table on SQL; on Cassandra this is a sentinel row inside `executions`
CREATE TABLE current_executions(
  shard_id INTEGER NOT NULL, namespace_id BYTEA NOT NULL, workflow_id VARCHAR(255) NOT NULL,
  run_id BYTEA NOT NULL, create_request_id VARCHAR(255) NOT NULL,
  state INTEGER NOT NULL, status INTEGER NOT NULL, last_write_version BIGINT NOT NULL,
  start_version BIGINT NOT NULL DEFAULT 0, start_time TIMESTAMP NULL,
  data BYTEA NULL, data_encoding VARCHAR(16) NOT NULL DEFAULT '',
  PRIMARY KEY (shard_id, namespace_id, workflow_id));

-- generic history tasks, keyed by category
CREATE TABLE history_immediate_tasks(
  shard_id INTEGER NOT NULL, category_id INTEGER NOT NULL, task_id BIGINT NOT NULL,
  data BYTEA NOT NULL, data_encoding VARCHAR(16) NOT NULL,
  PRIMARY KEY (shard_id, category_id, task_id));
CREATE TABLE history_scheduled_tasks (
  shard_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
  visibility_timestamp TIMESTAMP NOT NULL, task_id BIGINT NOT NULL,
  data BYTEA NOT NULL, data_encoding VARCHAR(16) NOT NULL,
  PRIMARY KEY (shard_id, category_id, visibility_timestamp, task_id));

-- Matching: `task_queues` holds ack levels/expiry and the rangeID fence; `tasks` holds spooled tasks
CREATE TABLE task_queues (range_hash BIGINT NOT NULL, task_queue_id BYTEA NOT NULL,
  range_id BIGINT NOT NULL, data BYTEA NOT NULL, data_encoding VARCHAR(16) NOT NULL,
  PRIMARY KEY (range_hash, task_queue_id));
CREATE TABLE tasks (range_hash BIGINT NOT NULL, task_queue_id BYTEA NOT NULL,
  task_id BIGINT NOT NULL, data BYTEA NOT NULL, data_encoding VARCHAR(16) NOT NULL,
  PRIMARY KEY (range_hash, task_queue_id, task_id));
```

The legacy per-category task tables (`transfer_tasks`, `timer_tasks`, `visibility_tasks`, `replication_tasks`) still exist alongside the generic `history_immediate_tasks` / `history_scheduled_tasks`. Mutable-state children are **normalized into separate tables** on SQL — `activity_info_maps`, `timer_info_maps`, `child_execution_info_maps`, `request_cancel_info_maps`, `signal_info_maps`, `signals_requested_sets`, `chasm_node_maps`, `buffered_events` — rather than Cassandra's collection columns. And `history_node` / `history_tree` carry a `shard_id` column on SQL that Cassandra does not have.

MySQL differs only in types: `BYTEA` → `MEDIUMBLOB` (note the **16 MB cap**), UUIDs → `BINARY(16)`, `task_queue_id` → `VARBINARY(272)`, `range_hash` → `INT UNSIGNED`, `TIMESTAMP` → `DATETIME(6)`.

#### The write path and where the amplification is

For one Workflow Task completion on Cassandra that schedules one activity — three new events, one transfer task, one activity-timeout timer task, one visibility task, one replication task if the namespace is global:

**Round trip 1: history append, outside the conditional batch.** `ExecutionStore.UpdateWorkflowExecution` loops the new-event batches and calls `AppendHistoryNodes` once per batch — a plain, *unconditional* `INSERT INTO history_node` at quorum. Only when `IsNewBranch` does it become a 2-statement logged batch (`history_tree` + `history_node`).

**Round trip 2: one logged batch with three conditional predicates, all on partition `shard_id`:**

| # | Statement | Row |
|---|---|---|
| 1 | `templateUpdateCurrentWorkflowExecutionQuery ... IF current_run_id = ?` | `executions`, `type=1`, `run_id=permanentRunID` |
| 2 | `templateUpdateWorkflowExecutionQuery ... IF db_record_version = ?` | `executions`, `type=1`, real run ID |
| 3..k | `activity_map[?] = ?`, `DELETE activity_map[?]`, timer/child/cancel/signal/CHASM upserts | same row, collection cells |
| — | `signal_requested + ?`, buffered-events append or clear | same row |
| k+1 | `INSERT ... transfer` (`type=2`) | new task row |
| k+2 | `INSERT ... timer` (`type=3`, `visibility_ts` = fire time) | new task row |
| k+3 | `INSERT ... visibility_task_data` (`type=6`) | new task row |
| k+4 | `INSERT ... replication` (`type=4`) | new task row |
| last | `templateUpdateLeaseQuery ... IF range_id = ?` | `executions`, `type=0` shard row |

So: roughly 9–10 CQL statements, 2 network round trips, but **one Paxos consensus round over a single partition covering three conditional predicates**, plus a batchlog write. At `SERIAL` consistency a Cassandra LWT is about four round trips internally (prepare/promise, read, propose/accept, commit).

Amplification sources, worst first:

1. **The whole Mutable State is rewritten every task.** `execution` and `execution_state` are full serialized blobs with no delta encoding. A workflow with 500 pending activities rewrites the entire `WorkflowExecutionInfo` blob on every Workflow Task. This is why `limit.mutableStateSize.error` exists and why pending-operation counts matter far more than they look.
2. **Every task category costs a row in the hot partition.** Enabling replication or archival is not free; it is +1 row per state transition.
3. **Task completion is a `DELETE`** — a tombstone in the same hot partition, reclaimed later by LeveledCompaction. Task *reads* scan the clustering range and pay for those tombstones.
4. **Everything is fenced through one shard row**, so the ~4-RTT LWT cost is paid per state transition and serialized per shard.
5. **Downstream**: the visibility task becomes an ES upsert (or a SQL upsert against a table with ~50 STORED generated columns and their indexes); the replication task becomes a cross-cluster read. Neither is counted above.

On SQL the same logical operation is one multi-statement transaction with ordinary MVCC row locks instead of Paxos — more statements, cheaper coordination, but a single-writer database instead of a distributed one.

---
### Visibility and the dual-write problem

#### Standard is gone; "advanced" means SQL or Elasticsearch

The historical distinction was "standard visibility" (a simple SQL/Cassandra table supporting a fixed set of filters) versus "advanced visibility" (Elasticsearch with arbitrary Search Attributes). On `main`, **`common/persistence/visibility/store/standard/` no longer exists.** `factory.go`'s `newVisibilityStoreFromDataStoreConfig` has exactly three branches — `dsConfig.SQL`, `dsConfig.Elasticsearch`, `dsConfig.CustomDataStoreConfig` — and otherwise `logger.Fatal("invalid config: visibility store must be configured")`.

So advanced visibility today is one interface with two backends. The docs' compatibility matrix ([Visibility setup](https://docs.temporal.io/self-hosted-guide/visibility)) records the history: ES7 from v1.7, ES8 from v1.18, OpenSearch 2+ from v1.30.1, SQL-based advanced visibility (MySQL 8.0.17+ / PostgreSQL 12+ / SQLite 3.31+) from v1.20, Cassandra visibility deprecated in v1.21 and **removed in v1.24**, and Dual Visibility added in v1.21.

The SQL backend pays for query flexibility with **pre-allocated typed columns**. From `common/searchattribute/sadefs/constants.go`: BOOL 3, INT 3, DOUBLE 3, DATETIME 3, KEYWORD 10, KEYWORD_LIST 3, TEXT 3 — fields named `Bool01`..`Bool03`, `Keyword01`..`Keyword10`, matched by `^%s(0[1-9]|[1-9][0-9])$`. In the Postgres DDL these are generated columns over a JSONB blob:

```sql
CREATE TABLE executions_visibility (
  namespace_id CHAR(64) NOT NULL, run_id CHAR(64) NOT NULL,
  _version BIGINT NOT NULL DEFAULT 0, -- increasing version, used to reject upserts which are out of order
  start_time TIMESTAMP NOT NULL, execution_time TIMESTAMP NOT NULL,
  workflow_id VARCHAR(255) NOT NULL, workflow_type_name VARCHAR(255) NOT NULL,
  status INTEGER NOT NULL,
  close_time TIMESTAMP NULL, history_length BIGINT NULL, history_size_bytes BIGINT NULL,
  execution_duration BIGINT NULL, state_transition_count BIGINT NULL,
  memo BYTEA NULL, encoding VARCHAR(64) NOT NULL, task_queue VARCHAR(255) NOT NULL DEFAULT '',
  search_attributes JSONB NULL,
  Keyword01 VARCHAR(255) GENERATED ALWAYS AS (search_attributes->>'Keyword01') STORED,
  Text01    TSVECTOR     GENERATED ALWAYS AS ((search_attributes->>'Text01')::tsvector) STORED,
  Datetime01 TIMESTAMP   GENERATED ALWAYS AS (convert_ts(search_attributes->>'Datetime01')) STORED,
  KeywordList01 JSONB    GENERATED ALWAYS AS (search_attributes->'KeywordList01') STORED,
  -- ... ~40 more generated columns ...
  PRIMARY KEY  (namespace_id, run_id)
);
```

It needs `CREATE EXTENSION btree_gin` and a custom immutable `convert_ts()` function, and carries roughly fifty indexes of the shape `(namespace_id, <col>, COALESCE(close_time,'9999-12-31 23:59:59') DESC, start_time DESC, run_id)`. **Every one of those STORED columns and indexes is recomputed on every visibility write.** That is real write amplification you are paying for on the visibility database, and it is the reason ES scales better for high-cardinality search.

The ES side is a symlinked index template (`schema/elasticsearch/visibility/index_template_v7.json` → `versioned/v14/`) with `"dynamic": "false"` — so unmapped attributes are stored but not indexed — and an index-level sort on `[CloseTime, StartTime, RunId]` descending, which is what makes the default "list recent workflows" query cheap.

The system search attributes (from `sadefs/constants.go`, note the lowercase `d` in the literals) are `WorkflowId`, `RunId`, `WorkflowType`, `StartTime`, `ExecutionTime`, `CloseTime`, `ExecutionStatus`, `TaskQueue`, `HistoryLength`, `ExecutionDuration`, `StateTransitionCount`, `HistorySizeBytes`, `ParentWorkflowId`, `ParentRunId`, `RootWorkflowId`, `RootRunId`. Predefined ones include `TemporalChangeVersion`, `BinaryChecksums`, `BuildIds`, `BatcherNamespace`, `BatcherUser`, `TemporalScheduledStartTime`, `TemporalScheduledById`, `TemporalSchedulePaused`, `TemporalNamespaceDivision`, and the worker-deployment set. Reserved names are `NamespaceId`, `MemoEncoding`, `Memo`, `VisibilityTaskKey`, and anything with the `Temporal` prefix.

#### The pipeline, and how the dual write is made consistent

The dual-write problem is real: the workflow's authoritative state is in one database and its searchable projection is in another, and there is no distributed transaction. Temporal solves it exactly the way it solves the Matching handoff — with a task in the same transaction.

1. The visibility task is an ordinary history task in `tasks.CategoryVisibility`, written in the same conditional batch as Mutable State. If the workflow write fails, no visibility task exists.
2. [`service/history/visibility_queue_task_executor.go`](https://github.com/temporalio/temporal/blob/main/service/history/visibility_queue_task_executor.go) dispatches by task type: `StartExecutionVisibilityTask` → `processStartExecution`, `UpsertExecutionVisibilityTask` → `processUpsertExecution`, `CloseExecutionVisibilityTask` → `processCloseExecution`, `DeleteExecutionVisibilityTask` → `processDeleteExecution`.
3. It loads Mutable State under the workflow lock, runs `CheckTaskVersion` (the close path uses `GetCloseVersion()`; upsert deliberately skips it, because "upsert doesn't require verifyTask, because it is just a sync of mutableState"), and sets `VisibilityRequestBase.TaskID = task.GetTaskID()`.
4. That `TaskID` becomes the **ES `_version`** (and the SQL `_version` column). Because task IDs within a shard are monotonic — courtesy of the range-ID fence — out-of-order deliveries are rejected by the store itself. This is the crux of the whole design: *the ordering guarantee for the visibility projection is inherited from the shard's task-ID allocator.*
5. The workflow lock is released *before* the RPC (`// NOTE: do not access anything related mutable state after this lock release`).
6. Ordering hazards are handled explicitly: delete-before-close is guarded by `ensureCloseBeforeDelete()` and `isCloseExecutionVisibilityTaskPending` (checked via `queues.IsTaskAcked` against `GetQueueState(tasks.CategoryVisibility)`), returning the retryable `consts.ErrDependencyTaskNotCompleted`.

Visibility tasks are **always executed as active** (`ExecutedAsActive: true` unconditionally); there is no standby visibility executor.

The ES write goes through a bulk processor ([`store/elasticsearch/processor.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/visibility/store/elasticsearch/processor.go)). Its interface is `Add(request, visibilityTaskKey) *future.FutureImpl[bool]` — **`AddMessage` does not exist on `main`**. The caller blocks on the future:

```go
// AddBulkRequestAndWait ... "Add method is blocking. If bulk processor is busy
// flushing previous bulk, request will wait here."
ackF := s.processor.Add(bulkRequest, visibilityTaskKey)
```

bounded by `ESProcessorAckTimeout`. Deduplication uses a sharded concurrent map (`collection.NewShardedConcurrentTxMap(1024, ...)`, sharded by `farm.Hash32(visibilityTaskKey) % indexerConcurrency`) emitting `ElasticsearchBulkProcessorDuplicateRequest`. The success predicate is instructive: 2xx is success, **409 is success** (version conflict means a newer write already landed — exactly the `_version` guard doing its job), and 404 is success unless the error type is `index_not_found_exception`.

Config: `IndexerConcurrency`, `ESProcessorNumOfWorkers`, `ESProcessorBulkActions`, `ESProcessorBulkSize`, `ESProcessorFlushInterval`, `ESProcessorAckTimeout`. The four latency metrics decompose the pipeline usefully — `elasticsearch_bulk_processor_wait_add_latency` (created → added), `_wait_start_latency` (added → sent), `_commit_latency` (sent → acked), `_request_latency` (created → acked). Note the metric prefix is `elasticsearch_bulk_processor_*`, **not** `es_bulk_processor_*`.

Dual Visibility (`visibility_manager_dual.go`) fans writes out to `managerSelector.writeManagers()` in parallel and `errors.Join`s the results; shadow reads run the secondary on a separate `shadowReadCtx` and discard the result. Relevant keys: `system.secondaryVisibilityWritingMode` (`"off"` / `"on"` / `"dual"` — **`system.advancedVisibilityWritingMode` no longer exists**), `system.enableReadFromSecondaryVisibility` (`false`), `system.visibilityEnableShadowReadMode` (`false`), `system.visibilityDisableOrderByClause` (`true`), `system.visibilityEnableManualPagination` (`true`).

---

### Multi-cluster replication and what failover means

#### What is in the open-source code

Three layers, all present on `main`:

**Namespace replication.** Namespace metadata changes propagate through a persistence-backed queue: `common/persistence/namespace_replication_queue.go` with `EnqueueMessage`, `EnqueueMessageToDLQ`, `ReadQueueMessages`, `DeleteMessagesBefore`, `UpdateAckLevel`. The handlers live in **`common/namespace/nsreplication/`** (`transmission_task_handler.go`, `replication_task_executor.go`, `replication_admitter.go`, `data_merger.go`, `dlq_message_handler.go`) and the worker-side processor in **`service/worker/replicator/`**. Note the renames: `common/namespace/replication/` and `service/worker/namespace/` **do not exist**. It is started conditionally:

```go
if s.clusterMetadata.IsGlobalNamespaceEnabled() { s.startReplicator() }
```

**History replication, streaming.** The current mechanism is a bidirectional gRPC stream, `HistoryService.StreamWorkflowReplicationMessages`, implemented across `service/history/replication/stream_sender.go` / `stream_receiver.go` / `bi_direction_stream.go` / `stream_receiver_monitor.go` plus flow controllers on both ends. The sender's field comments name the roles precisely:

```go
server         historyservice.HistoryService_StreamWorkflowReplicationMessagesServer
clientShardKey ClusterShardKey // client is the target cluster (passive cluster)
serverShardKey ClusterShardKey // server is the source cluster (active cluster)
```

`sendEventLoop` subscribes to replication notifications, does a `sendCatchUp(priority)` from the persisted watermark (`GetQueueExclusiveHighReadWatermark(tasks.CategoryReplication).TaskID`), then switches to `sendLive`. The reverse direction carries **only acknowledgements** (`SyncReplicationState`), which the sender persists as `QueueReaderState` via `UpdateReplicationQueueReaderState`. Under `EnableReplicationTaskTieredProcessing` there are two priority streams — high for live traffic, low for force-replicating closed workflows. Anti-wedge protection: `TaskMaxSkipCount = 1000` forces a bare watermark message so a long run of filtered tasks cannot stall the cursor.

Legacy pull-based replication (`task_fetcher.go`, `task_processor.go`, `poller_manager.go`, `ack_manager.go`) is still in the tree.

**Conflict resolution.** `service/history/ndc/` — `history_replicator.go`, `conflict_resolver.go`, `branch_manager.go`, `transaction_manager.go`, `state_rebuilder.go`, `workflow_resetter.go`, `activity_state_replicator.go`, `workflow_state_replicator.go`, `hsm_state_replicator.go`. This is what runs when two clusters have both written to the same workflow: version histories are compared, a common ancestor is found, a branch is forked, and the losing side's events are re-applied.

#### Failover versions

Every write carries a failover version, and the version encodes which cluster wrote it. From [`common/cluster/metadata.go`](https://github.com/temporalio/temporal/blob/main/common/cluster/metadata.go):

```go
func (m *metadataImpl) GetNextFailoverVersion(clusterName string, currentFailoverVersion int64) int64 {
	info, ok := m.clusterInfo[clusterName]
	...
	failoverVersion := currentFailoverVersion/m.failoverVersionIncrement*m.failoverVersionIncrement + info.InitialFailoverVersion
	if failoverVersion < currentFailoverVersion {
		return failoverVersion + m.failoverVersionIncrement
	}
	return failoverVersion
}

func (m *metadataImpl) IsVersionFromSameCluster(version1, version2 int64) bool {
	return (version1-version2)%m.failoverVersionIncrement == 0
}
```

`ClusterNameForFailoverVersion` is just `failoverVersion % failoverVersionIncrement` mapped back to a cluster name (with `common.EmptyVersion` mapping to `failoverVersionIncrement`, since "Failover version starts with 1. Zero is an invalid value for failover version"). The docs state the same rule from the operator side: `version % (shared version increment) == (active cluster's initial version)`, and the highest version wins ([Multi-Cluster Replication](https://docs.temporal.io/self-hosted-guide/multi-cluster-replication)).

Validation is strict and fatal. `ValidateClusterInformation` requires a non-empty name, `0 < InitialFailoverVersion < FailoverVersionIncrement`, and a non-empty RPC address when enabled; duplicates are rejected. On startup failure: `m.logger.Fatal("Unable to initialize cluster metadata cache", ...)` — deliberately, "Crash rather than start with partial cluster metadata." `NewMetadata` panics if `failoverVersionIncrement == 0 || > math.MaxInt32`. Cluster metadata is refreshed from the database every minute into a candidate map, validated, then swapped.

**A failover is an `UpdateNamespace` that changes `ActiveClusterName`** and bumps the failover version. Everything else follows mechanically: task executors flip from standby to active (via `queues/active_standby_executor.go`), the new active cluster starts generating events at a version that beats the old cluster's, and any concurrent writes from the old active are resolved by version-history conflict resolution. The docs are blunt about the guarantees: replication is asynchronous, "data across clusters is not strongly consistent", "Activity Execution completions are not forwarded across Clusters", progress can roll back on failover, but "Temporal provides the guarantee that Workflow Executions won't get stuck."

The OSS feature carries an explicit health warning: multi-cluster replication is "considered experimental and not subject to normal versioning and support policy."

#### Active-active / multi-region on `main`

There is a seam but not a shipped feature. `common/namespace/replication_resolver.go` defines a `ReplicationResolver` interface with `ActiveClusterName(routingKey RoutingKey)`, `ClusterNames(businessID)`, `FailoverVersion(businessID)` — all parameterised by a routing key, which is exactly the shape you would need for per-workflow active-active routing. The shipped implementation, `defaultReplicationResolver`, **ignores the business ID and returns `replicationConfig.ActiveClusterName`**. Read that as: the abstraction has been introduced ahead of the capability.

#### Is this what Temporal Cloud uses?

Be careful here, because this is exactly the question where it is tempting to over-claim.

**(b) What is publicly documented.** Temporal Cloud's [High Availability](https://docs.temporal.io/cloud/high-availability) page describes Multi-region Replication and Multi-cloud Replication, achieved "Using asynchronous replication between multiple regions or cloud providers, combined with automatic outage detection and failover", with **automatic failover at a 20-minute RTO**, sub-1-minute RPO, and a "conflict resolution process [that] reconciles discrepancies" if the regions are not in sync at failover time. Baseline (non-HA) namespaces replicate across "three Availability Zones", with changes "saved in all three AZs before the Temporal Service acknowledges a change back to the Client." One replica is active and one passive; the passive forwards transparently, and "Client requests such as Start Workflow, Signal, Query, Cancel, and Terminate are always forwarded."

The page also names cells directly, under Same-region Replication (Public Preview):

> "In selected regions, you can add a replica to a Namespace in the same region. Temporal operates a 'cell architecture' and will replicate the Namespace across multiple cells in that region." ... "Failovers between cells are always managed automatically by Temporal. Unlike Multi-region and Multi-cloud Replication, you cannot disable automatic failovers and you cannot trigger a manual failover for a Same-region Replication Namespace."

And the [SLA](https://docs.temporal.io/cloud/sla) page states it as architecture:

> "Internally, our components are distributed across a minimum of three availability zones per region. We implement a cell architecture. Each cell contains the software and services necessary to host a Namespace. Within each cell, the components are distributed across a minimum of three availability zones per region."

Sergey Bykov's engineering blog adds the data-plane shape: "For the data plane, we applied a cell-based architecture to achieve strong isolation and scalability. Each cell operates as a self-contained unit with its own AWS account, VPC, EKS cluster, and supporting infrastructure. ... This approach ensures that failures or updates in one cell do not impact others, reducing the risk of cascading outages" ([Building Durable Cloud Control Systems](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal)). The same post describes the control plane as itself built on Temporal, with deployment rings ("Ring 0: Synthetic traffic only... monitored here for at least a week").

**(c) Inference, clearly labelled.** No public page asserts that Cloud HA runs the OSS multi-cluster replication code path. The vocabulary matches exactly — asynchronous replication, Global Namespaces, conflict resolution, active/passive with transparent forwarding — which makes common lineage highly likely. But the OSS feature is labelled experimental and Cloud HA plainly is not, so at minimum there is substantial divergence. **The correct thing to say in a design review is: "the public docs describe both as asynchronous cross-cluster replication with conflict resolution; Temporal does not publicly state that Cloud runs the OSS multi-cluster code path."** Do not assert more than that, and do not guess at Cloud's shard counts, failover implementation, or cell sizing — none of it is published.

Replication knobs verified on `main` (note the anomalous capital R in several key strings): `history.enableReplicationStream = true`, `history.ReplicationTaskProcessorShardQPS = 30`, `history.ReplicationTaskFetcherParallelism = 4`, `history.EnableReplicationTaskBatching = false`. **`history.replicatorProcessorMaxPollRPS` no longer exists.**

---

### Membership and routing

#### Ringpop is still the mechanism

[`common/membership/`](https://github.com/temporalio/temporal/tree/main/common/membership) has two implementations: `ringpop/` (production) and `static/` (fixed host list). Selection is in `temporal/fx.go`:

```go
membershipModule := ringpop.MembershipModule
if len(params.StaticServiceHosts) > 0 {
	membershipModule = static.MembershipModule(params.StaticServiceHosts)
}
```

The interfaces:

```go
Monitor interface {
	Start()
	EvictSelf() error                                  // called on graceful shutdown
	EvictSelfAt(asOf time.Time) (time.Duration, error)
	GetResolver(service primitives.ServiceName) (ServiceResolver, error)
	GetReachableMembers() ([]string, error)
	WaitUntilInitialized(context.Context) error
	SetDraining(draining bool) error
	ApproximateMaxPropagationTime() time.Duration
}

ServiceResolver interface {
	Lookup(key string) (HostInfo, error)               // <- shard ID as a decimal string
	LookupN(key string, n int) []HostInfo
	AddListener(name string, notifyChannel chan<- *ChangedEvent) error
	RemoveListener(name string) error
	MemberCount() int; AvailableMemberCount() int
	Members() []HostInfo; AvailableMembers() []HostInfo
	RequestRefresh()
}
```

The ring itself is `hashring.New(farm.Fingerprint32, replicaPoints)` with `defaultRefreshInterval = 10s` and `minRefreshInternal = 4s`. On a lookup miss it calls `RequestRefresh()` and returns `ErrInsufficientHosts` — which surfaces to callers as `serviceerror.Unavailable`.

#### Bootstrap is database-backed, not DNS

This is the part people get wrong. Ringpop is SWIM gossip, but its *bootstrap* list comes from a database table. [`common/membership/ringpop/monitor.go`](https://github.com/temporalio/temporal/blob/main/common/membership/ringpop/monitor.go):

```go
const (
	upsertMembershipRecordExpiryDefault = time.Hour * 48
	// 10 second base reporting frequency + 5 second jitter + 5 second acceptable time skew
	healthyHostLastHeartbeatCutoff = time.Second * 20
	maxBootstrapRetries = 5
	maxScheduledEventTimeSeconds = 15
)
```

The sequence on start: prune expired records; upsert this host's own record (`{Role, RPCAddress, RPCPort, SessionStart, RecordExpiry: 48h, HostID}`) **before** joining ringpop, with an acknowledged race in the comment; start a heartbeat loop that re-upserts every 10–15 seconds (`jitter := math.Round(rand.Float64() * 5)`); and bootstrap by reading `GetClusterMembers{LastHeartbeatWithin: 20s, PageSize: 1000}`, deduping, and stopping at 500 unique `ip:port`.

The table:

```sql
CREATE TABLE cluster_membership
(
    membership_partition tinyint,
    host_id              uuid,
    rpc_address          inet,
    rpc_port             smallint,
    role                 tinyint,
    session_start        timestamp,
    last_heartbeat       timestamp,
    PRIMARY KEY (membership_partition, role, host_id)
) WITH COMPACTION = {
    'class': 'org.apache.cassandra.db.compaction.LeveledCompactionStrategy'
  };

CREATE INDEX cm_lastheartbeat_idx on cluster_membership (last_heartbeat);
CREATE INDEX cm_sessionstart_idx on cluster_membership (session_start);
```

The SQL variant adds an explicit `record_expiry` column (Cassandra uses TTL). **Operational consequence: if your persistence layer is down, new pods cannot bootstrap into the ring at all.** A database outage is not just a data-plane outage; it is a membership outage, and that changes how you sequence recovery.

Static config for membership is `global.membership.broadcastAddress` and `maxJoinDuration`, plus a per-service `services.<svc>.rpc.membershipPort`. `buildBroadcastHostPort()` errors with *"broadcastAddress required when listening on all interfaces (0.0.0.0/[::])"* — a common Kubernetes misconfiguration.

#### Finding the right History host

[`client/history/client.go`](https://github.com/temporalio/temporal/blob/main/client/history/client.go) computes the shard, then hands off to a redirector:

```go
func (c *clientImpl) shardIDFromWorkflowID(namespaceID, workflowID string) int32 {
	return common.WorkflowIDToHistoryShard(namespaceID, workflowID, c.numberOfShards)
}

func (c *clientImpl) executeWithRedirect(ctx context.Context, shardID int32, op ClientOperation[...]) error {
	return c.redirector.Execute(ctx, shardID, op)
}
```

and the lookup is the same string-keyed ring lookup the shard controller uses:

```go
func shardLookup(resolver membership.ServiceResolver, shardID int32) (rpcAddress, error) {
	hostInfo, err := resolver.Lookup(convert.Int32ToString(shardID))
	if err != nil {
		return "", err
	}
	return rpcAddress(hostInfo.GetAddress()), nil
}
```

The redirect loop is what makes a rolling restart survivable:

```go
func (r *BasicRedirector[C]) redirectLoop(ctx context.Context, address rpcAddress, op ClientOperation[C]) error {
	for {
		if err := common.IsValidContext(ctx); err != nil { return err }
		clientConn := r.connections.getOrCreateClientConn(address)
		err := op(ctx, clientConn.grpcClient)
		var solErr *serviceerrors.ShardOwnershipLost
		if !errors.As(err, &solErr) || len(solErr.OwnerHost) == 0 {
			return err
		}
		// TODO: consider emitting a metric for number of redirects
		address = rpcAddress(solErr.OwnerHost)
	}
}
```

The old owner tells the caller who the new owner is, and the caller retries there — no back-off to the ring, no client-visible error. `CachingRedirector` (enabled by `history.clientOwnershipCachingEnabled`, default `false`) keeps a `map[int32]cacheEntry`, evicts on `ShardOwnershipLost` / host-down / connection shutdown, registers a membership listener to mark entries stale after `history.clientOwnershipCachingUnusedTTL` (30s), and calls `resetConnectBackoff` on add because "new history instances might reuse the address of a previously live history instance."

#### What a rolling restart actually looks like

Sequenced from the code paths above:

1. The pod being replaced calls `Monitor.EvictSelf()` on shutdown (Matching does this explicitly in `service.go`). Ringpop propagates the leave.
2. Every other History host's `ownership.eventLoop` receives a `ChangedEvent`, schedules an acquire pass, and picks up the orphaned shards. `history.alignMembershipChange` exists to batch these events and reduce churn.
3. For each newly-owned shard: `GetOrCreateShard`, `renewRangeLocked(true)` (one LWT), start the engine, start the queue processors, replay from the checkpointed `QueueState`.
4. Requests arriving at the departing host during the gap get `serviceerror.ShardOwnershipLost` and are redirected; requests arriving at the new owner before acquisition completes get `ErrShardStatusUnknown` → gRPC `UNAVAILABLE`. SDKs retry `UNAVAILABLE`.
5. Latency, not errors, is the expected symptom — but the tail is set by `history.shardIOTimeout` (5s) and the acquisition retry policy (1s exponential up to 5m). A single slow shard acquisition can hold a slice of workflow IDs unavailable for seconds.

*Inference:* the practical guidance that follows is to restart History hosts **one at a time with a settle window at least as long as `history.acquireShardInterval`**, and to never restart History concurrently with a persistence maintenance operation, since ambiguous persistence errors trigger the range-bump path described earlier. That sequencing rule is mine, not Temporal's; the docs only say to allow "approximately 10 minutes on each version for these processes to complete" during upgrades.

---

### Configuration surfaces

#### Static config

`config/development.yaml` is a symlink to `config/development-sqlite.yaml`. The shape (abridged; the full file is ~130 lines):

```yaml
persistence:
  defaultStore: sqlite-default
  visibilityStore: sqlite-visibility
  numHistoryShards: 1
  datastores:
    sqlite-default:
      sql:
        pluginName: "sqlite"          # or "cassandra:" / "postgres12" / "mysql8"
        databaseName: "default"
        connectAddr: "localhost"
        connectAttributes: { mode: "memory", cache: "private" }
        maxConns: 1                   # connection pool sizing is STATIC config
        maxIdleConns: 1
        maxConnLifetime: "1h"
        tls: { enabled: false }

global:
  membership: { maxJoinDuration: 30s, broadcastAddress: "127.0.0.1" }
  pprof: { port: 7936 }
  metrics:
    prometheus: { framework: "tally", timerType: "histogram", listenAddress: "127.0.0.1:8000" }

services:      # gRPC port / membership port per service
  frontend: { rpc: { grpcPort: 7233, membershipPort: 6933, httpPort: 7243, bindOnLocalHost: true } }
  history:  { rpc: { grpcPort: 7234, membershipPort: 6934, bindOnLocalHost: true } }
  matching: { rpc: { grpcPort: 7235, membershipPort: 6935, bindOnLocalHost: true } }
  worker:   { rpc: { grpcPort: 7239, membershipPort: 6939, bindOnLocalHost: true } }

clusterMetadata:
  enableGlobalNamespace: false
  failoverVersionIncrement: 10        # must match across all clusters
  masterClusterName: "active"
  currentClusterName: "active"
  clusterInformation:
    active: { enabled: true, initialFailoverVersion: 1, rpcName: "frontend",
              rpcAddress: "localhost:7233", httpAddress: "localhost:7243" }

dcRedirectionPolicy: { policy: "noop" }
dynamicConfigClient: { filepath: "config/dynamicconfig/development-sql.yaml", pollInterval: "10s" }
```

The things that are **only** settable here, never dynamically: `numHistoryShards`, the datastore definitions and connection pool sizes, ports, `clusterMetadata` (cluster names, `failoverVersionIncrement`, `initialFailoverVersion`), TLS, and the archival provider configuration. Everything on that list is a cell-build-time decision.

Note that there is **no `publicClient:` block** in the current file; the worker derives the frontend address from `clusterMetadata`.

#### Dynamic config

The file-based client ([`common/dynamicconfig/file_based_client.go`](https://github.com/temporalio/temporal/blob/main/common/dynamicconfig/file_based_client.go)):

```go
const minPollInterval = time.Second * 5

FileBasedClientConfig struct {
	Filepath     string        `yaml:"filepath"`
	PollInterval time.Duration `yaml:"pollInterval"`
}
```

`validateStaticConfig` rejects a poll interval under 5s. `Update()` short-circuits on mtime, parses the whole file, and swaps atomically; parse errors are **not retried** because they "fail deterministically until the file is fixed", and the failure surfaces as the `DynamicConfigUpdateFailure` gauge. On success it logs `"Updated dynamic config"` with a diff. **Alert on that gauge** — a malformed dynamic config file means your cell silently keeps running the previous values.

The YAML shape is a list of constrained values per key:

```yaml
limit.maxIDLength:
  - value: 255
    constraints: {}
history.persistenceMaxQPS:
  - value: 3000
    constraints: {}
  - value: 500
    constraints:
      namespace: "noisy-tenant"
matching.numTaskqueueReadPartitions:
  - value: 16
    constraints:
      namespace: "big-tenant"
      taskQueueName: "orders"
```

Constraint matching (namespace, namespace ID, task queue name, task type, shard ID, destination) happens in the `Collection`/`Setting` layer, not in the file client — the file client is a dumb key → `[]ConstrainedValue` store.

The knobs that actually get tuned in practice, grouped by what they protect:

| Concern | Keys |
|---|---|
| Protect the database | `history.persistenceMaxQPS`, `history.persistenceGlobalMaxQPS`, `frontend.persistenceMaxQPS`, `matching.persistenceMaxQPS`, `history.persistencePerShardNamespaceMaxQPS`, `history.persistenceDynamicRateLimitingParams` |
| Protect the frontend | `frontend.rps` (2400), `frontend.namespaceRPS` (2400), `frontend.globalNamespaceRPS` (0 = off), `frontend.namespaceCount` (1200 concurrent long-running per namespace per API), `frontend.keepAliveMaxConnectionAge` (5m) |
| Shard behaviour | `history.acquireShardInterval`, `history.acquireShardConcurrency`, `history.shardIOTimeout`, `history.shardUpdateMinInterval`, `history.alignMembershipChange` |
| Queue throughput | `history.timerProcessorSchedulerWorkerCount` (512), `history.transferProcessorMaxPollRPS`, `history.timerProcessorMaxPollRPS`, `history.taskSchedulerNamespaceMaxQPS` |
| Cache | `history.hostLevelCacheMaxSize` (128000), `history.hostLevelCacheMaxSizeBytes`, `history.cacheTTL` |
| Task queue capacity | `matching.numTaskqueueReadPartitions`, `matching.numTaskqueueWritePartitions`, `matching.forwarderMaxChildrenPerNode` |
| Customer limits | `limit.historySize.error`, `limit.historyCount.error`, `limit.numPendingActivities.error`, `limit.maxIDLength` |

`frontend.namespaceCount` deserves a footnote: its own doc string admits the name "is a bit of a misnomer" — it is a per-namespace, **per-API** count of concurrent long-running requests, which in practice means concurrent pollers. Tuning it down throttles workers; tuning it up lets one namespace exhaust frontend goroutines.

#### The frontend interceptor chain

Worth knowing in order, because the order determines what gets counted and what gets rejected first ([`service/frontend/fx.go`](https://github.com/temporalio/temporal/blob/main/service/frontend/fx.go)):

```go
unaryInterceptors := []grpc.UnaryServerInterceptor{
	// Order of interceptors is important
	// Mask error interceptor should be the most outer interceptor since it handle the errors format
	maskInternalErrorDetailsInterceptor.Intercept,
	serviceErrorInterceptor.Intercept,
	interceptor.NewFrontendServiceErrorInterceptor(logger),
	businessIDInterceptor.Intercept,
	namespaceValidatorInterceptor.NamespaceValidateIntercept,
	namespaceLogInterceptor.Intercept,
	metrics.NewServerMetricsContextInjectorInterceptor(),
	authInterceptor.Intercept,
	namespaceHandoverInterceptor.Intercept,
	redirectionInterceptor.Intercept,
	// Telemetry interceptor must be after redirection to ensure metrics are recorded in the correct cluster
	telemetryInterceptor.UnaryIntercept,
	healthInterceptor.Intercept,
	namespaceValidatorInterceptor.StateValidationIntercept,
	namespaceCountLimiterInterceptor.Intercept,   // concurrent long-running requests
	namespaceRateLimiterInterceptor.Intercept,    // per-namespace RPS
	rateLimitInterceptor.Intercept,               // host RPS
	sdkVersionInterceptor.Intercept, callerInfoInterceptor.Intercept,
	slowRequestLoggerInterceptor.Intercept, contextMetadataInterceptor.Intercept,
}
// retry interceptor should be the most inner interceptor
unaryInterceptors = append(unaryInterceptors, retryableInterceptor.Intercept)
```

Rate limiting is priority-aware. `service/frontend/configs/quotas.go` maps every API to a priority: **P0** system reads (`GetClusterInfo`, `GetSystemInfo`, `DescribeNamespace`), **P1** external events and progress (`StartWorkflowExecution`, `SignalWorkflowExecution`, `UpdateWorkflowExecution`, `RecordActivityTaskHeartbeat`, `Respond*Completed`), **P2** state change plus `GetWorkflowExecutionHistory` ("relatively high priority because it is required for replay"), **P3** describe and query, **P4** `Poll*`, **P5** long-poll history. Callers tagged `headers.CallerTypeOperator` get `quotas.OperatorPriority`, which is how operator tooling stays usable during a customer-driven overload.

Authorization is a plugin seam, not built-in policy ([`common/authorization/authorizer.go`](https://github.com/temporalio/temporal/blob/main/common/authorization/authorizer.go)):

```go
type CallTarget struct {
	// APIName must be the full API function name.
	// Example: "/temporal.api.workflowservice.v1.WorkflowService/StartWorkflowExecution".
	APIName string; Namespace string; NexusEndpointName string; Request any
}

type Authorizer interface {
	Authorize(ctx context.Context, caller *Claims, target *CallTarget) (Result, error)
}

func GetAuthorizerFromConfig(config *config.Authorization) (Authorizer, error) {
	switch strings.ToLower(config.Authorizer) {
	case "":        return NewNoopAuthorizer(), nil   // <- the default
	case "default": return NewDefaultAuthorizer(), nil
	}
	return nil, fmt.Errorf("unknown authorizer: %s", config.Authorizer)
}
```

The default is the *noop* authorizer, and the docs say so bluntly: "If you do not explicitly configure an `Authorizer`, Temporal uses the default `noopAuthorizer`. This default allows every API request, with no authentication or access control" ([Security](https://docs.temporal.io/self-hosted-guide/security)). The `Authorizer` and `ClaimMapper` are supplied at process level via `temporal.WithAuthorizer(...)` / `temporal.WithClaimMapper(...)` in `cmd/server/main.go`, and the `internal-frontend` service is decorated with `authorization.NewInternalClaimMapper()`.

---

### Startup and dependency injection

The process is an `fx` graph, or rather **five** of them. [`temporal/fx.go`](https://github.com/temporalio/temporal/blob/main/temporal/fx.go):

```go
var TopLevelModule = fx.Options(
	fx.Provide(
		NewServerFxImpl, ServerOptionsProvider,
		resource.ArchivalMetadataProvider, TaskCategoryRegistryProvider,
		HistoryServiceProvider, MatchingServiceProvider,
		FrontendServiceProvider, InternalFrontendServiceProvider, WorkerServiceProvider,
		ApplyClusterMetadataConfigProvider,
	),
	dynamicconfig.Module, pprof.Module, TraceExportModule, chasm.Module,
	serialization.Module, FxLogAdapter,
	fx.Invoke(ServerLifetimeHooks),
)
```

Each service gets its **own child `fx.App`**, not a shared graph:

```go
func HistoryServiceProvider(params ServiceProviderParamsCommon) (ServicesGroupOut, error) {
	serviceName := primitives.HistoryService
	if _, ok := params.ServiceNames[serviceName]; !ok {
		params.Logger.Info("Service is not requested, skipping initialization.", tag.Service(serviceName))
		return ServicesGroupOut{}, nil
	}
	app := fx.New(params.GetCommonServiceOptions(serviceName), history.QueueModule, history.Module, replication.Module)
	return NewService(app, serviceName, params.Logger), app.Err()
}
```

That is why one binary can run any subset of services: the `--service` flag (`TEMPORAL_SERVICES` env) selects which providers instantiate their child app; the rest log "Service is not requested" and return empty. `temporal.DefaultServices` is frontend, history, matching, worker; `internal-frontend` exists but is opt-in.

Startup order is fixed and matters ([`temporal/server_impl.go`](https://github.com/temporalio/temporal/blob/main/temporal/server_impl.go)):

```go
var initOrder = map[primitives.ServiceName]int{
	primitives.MatchingService: 1, primitives.HistoryService: 2,
	primitives.InternalFrontendService: 3, primitives.FrontendService: 3,
	primitives.WorkerService: 4,
}

func (s *ServerImpl) startServices() error {
	// The membership join time may exceed the configured max join duration.
	// Double the service start timeout to make sure there is enough time for start logic.
	timeout := max(serviceStartTimeout, 2*s.so.config.Global.Membership.MaxJoinDuration)
	...
}
```

Matching first (so History has somewhere to push tasks), then History, then Frontend, then Worker. Shutdown reverses it, with `serviceStopTimeout = 5m`. `ServerImpl.Start` also runs `initSystemNamespaces(...)` — registering `temporal-system` — before starting anything.

**`temporal server start-dev` versus a real deployment.** The dev server ships in the separate [`temporalio/cli`](https://github.com/temporalio/cli) repo, and I was not able to fetch its source, so treat the internals as *inference*: it runs all four services plus the Web UI in one process, defaults persistence to SQLite (in-memory unless `--db-filename` is given), auto-registers the `default` namespace, and applies dev-friendly dynamic config. What is *documented* is the behaviour and the warning: "**WARNING: The development server is not intended for production use. It skips certain HTTP security checks to make local use simpler**", and "By default, Workflow Executions are lost when the server process dies" ([CLI server reference](https://docs.temporal.io/cli/command-reference/server)). The SQLite plugin is linked into `cmd/server/main.go` in the server repo, and `config/development-sqlite.yaml` sets `numHistoryShards: 1` — so a dev server is a *one-shard* cell, which means it exercises none of the sharding behaviour you actually operate.

---
### The worker service: Temporal running on Temporal

The fourth service is the least discussed and the most conceptually elegant: it is an ordinary Temporal client that runs system Workflows on the `temporal-system` namespace. From [`common/primitives/namespaces.go`](https://github.com/temporalio/temporal/blob/main/common/primitives/namespaces.go):

```go
SystemLocalNamespace     = "temporal-system"
SystemNamespaceID        = "32049b68-7872-4094-8e63-d0dd59896a83"
SystemNamespaceRetention = time.Hour * 24 * 7
```

`service/worker/service.go` starts in this order: `membershipMonitor.Start()` → `ensureSystemNamespaceExists` (which does `logger.Fatal("temporal-system namespace does not exist")` on `NamespaceNotFound`) → `startScanner()` → `startReplicator()` if global namespaces are enabled → `startParentClosePolicyProcessor()` if enabled → `workerManager.Start()` → `perNamespaceWorkerManager.Start(...)`.

The sub-packages:

| Package | What it runs |
|---|---|
| `scanner/` | `executions`, `taskqueue`, `history`, `build_ids`, `scheduleinvariants` scavengers |
| `batcher/` | Batch terminate/signal/cancel/reset jobs (`BatcherRPS`, `BatcherConcurrency`) |
| `deletenamespace/` | The multi-step namespace deletion workflow |
| `migration/` | Force-replication and verification for cluster migration |
| `parentclosepolicy/` | Terminating children when a parent closes |
| `replicator/` | Namespace replication message processing |
| `dlq/` | The history-task DLQ management workflow |
| `scheduler/` | Schedules (plus a newer CHASM-native scheduler in `chasm/lib/scheduler`) |
| `workerdeployment/` | Worker deployment (versioning v3) bookkeeping |
| `addsearchattributes/` | Adding custom search attributes to the visibility store |

**`service/worker/archiver/` and `service/worker/namespace/` no longer exist** — archival client code moved to `service/history/archival/` and `common/archiver/`, namespace replication to `service/worker/replicator/` and `common/namespace/nsreplication/`.

The scanner toggles, with an important store-dependency wrinkle:

| Key | Default | Runs when |
|---|---|---|
| `worker.taskQueueScannerEnabled` | `true` | **SQL store only** |
| `worker.historyScannerEnabled` | `true` | Any store |
| `worker.executionsScannerEnabled` | `false` | **NoSQL store only** (logs "ExecutionsScanner is not supported for SQL store") |
| `worker.buildIdScavengerEnabled` | `false` | Any store |

Startup retries forever with `backoff.NewExponentialRetryPolicy(time.Second).WithMaximumInterval(time.Minute).WithExpirationInterval(backoff.NoInterval)`, treating `WorkflowExecutionAlreadyStarted` as success — so scanners are singletons across the cell by workflow-ID uniqueness, not by leader election.

Archival lives in `common/archiver/` with three providers (`filestore/`, `gcloud/`, `s3store/`) and the `tasks.CategoryArchival` queue in History. The docs describe archival as **experimental**, off by default, and note "The Archival URI cannot be changed after the Namespace is created" ([Archival](https://docs.temporal.io/self-hosted-guide/archival)).

---

## Hands-on

### The five-minute version (no Docker)

```bash
brew install temporal
temporal server start-dev
```

That gives you a single process on `localhost:7233` with the Web UI on `http://localhost:8233`, SQLite **in memory**, `numHistoryShards: 1`, and the `default` namespace auto-created. Useful flags ([CLI server reference](https://docs.temporal.io/cli/command-reference/server)):

```bash
# Do not put trailing `# comments` after a line-continuation backslash: the
# backslash escapes the space, the comment eats the rest of the line, and the
# command silently ends there with every later flag dropped.
temporal server start-dev \
  --db-filename ./temporal.db \
  --port 7233 --ui-port 8233 \
  --http-port 7243 \
  --metrics-port 8000 \
  --namespace demo --namespace other \
  --search-attribute CustomKey=Keyword \
  --dynamic-config-value history.hostLevelCacheMaxSize=1024 \
  --headless
#  --db-filename   survive restarts; without it, everything is lost on exit
#  --ui-port       defaults to --port + 1000
#  --metrics-port  Prometheus scrape target
#  --headless      no Web UI
```

`--dynamic-config-value` takes `KEY=VALUE` with JSON values and can be repeated — this is how you exercise a dynamic config knob without a config file.

**Caveat that matters:** the dev server is a one-shard cell. It is fine for reading event histories and learning the CLI. It exercises **none** of the sharding, membership, forwarding, or replication behaviour that this guide is about. Do not draw operational conclusions from it.

### Building and running the real server from source (Docker required)

From [`CONTRIBUTING.md`](https://github.com/temporalio/temporal/blob/main/CONTRIBUTING.md) and the [`Makefile`](https://github.com/temporalio/temporal/blob/main/Makefile):

```bash
git clone https://github.com/temporalio/temporal.git && cd temporal
make                       # first time: builds temporal-server, tdbg, and the schema tools
make bins                  # subsequent builds

# SQLite in-memory, no external dependencies at all:
make start                 # == make start-sqlite
#   -> ./temporal-server --config-file config/development-sqlite.yaml --allow-no-auth start
temporal operator namespace create -n default
```

For anything realistic you need the dependency stack, which **does** need Docker:

```bash
make start-dependencies    # docker compose up over ./develop/docker-compose/
#   brings up: mysql:3306, cassandra:9042, postgresql:5432, elasticsearch:9200,
#              prometheus, grafana, tempo, temporal-ui

make install-schema-cass-es && make start-cass-es      # Cassandra + Elasticsearch
make install-schema-postgresql12 && make start-postgres
make install-schema-mysql8 && make start-mysql
make start-sqlite-file                                 # SQLite on disk
make start-xdc-cluster-a                               # multi-cluster replication sandbox

make stop-dependencies
```

Note: there is **no bare `make install-schema` target and no `make build`**. The real targets are `install-schema-cass-es`, `install-schema-mysql8`, `install-schema-postgresql12`, `install-schema-es`, `install-schema-xdc`. Web UI is on `localhost:8080` in this setup (not 8233 — that is the dev server's port).

Tests: `make unit-test`, `make integration-test`, `make functional-test`, `make test`. The repo's own guidance is to always pass `-tags test_dep`.

For a container-only stack, note that **`temporalio/docker-compose` is archived** — "All docker-compose examples have been moved to the samples-server repository":

```bash
git clone https://github.com/temporalio/samples-server.git
cd samples-server/compose && docker compose up
```

The compose files worth knowing: `docker-compose.yml` (Postgres + Elasticsearch + UI, frontend 7233, UI 8080), `docker-compose-dev.yml` (a single `server start-dev` container, UI 8233), `docker-compose-cass-es.yml`, `docker-compose-postgres-opensearch.yml`, `docker-compose-tls.yml`, and — the interesting one for an infra owner — **`docker-compose-multirole.yaml`**, which splits history / matching / frontend / frontend2 / worker into separate containers behind nginx, with Prometheus, Grafana, Jaeger, and an OTel collector. That is the closest thing to a cell you can run on a laptop.

### Inspecting a running cell

Customer-facing CLI:

```bash
temporal operator cluster health
temporal operator cluster describe --detail        # "Show history shard count and Cluster/Service version information"
temporal operator cluster list                     # name, ID, address, History Shard count, Failover version
temporal operator cluster system --frontend-address host:7233
temporal operator cluster upsert --frontend-address <ep> --enable-connection --enable-replication

temporal operator namespace list
temporal operator namespace describe --namespace demo
temporal operator namespace update --namespace demo --active-cluster clusterB   # a failover

temporal operator search-attribute create --name OrderRegion --type Keyword
temporal operator search-attribute list
```

`temporal operator cluster describe --detail` is how you read the local Service's history shard count from the public CLI; `temporal operator cluster list` reports the same figure for each registered remote cluster.

Task queue health — this is your first stop for a "workers are slow" report:

```bash
temporal task-queue describe --task-queue orders
temporal task-queue describe --task-queue orders --task-queue-type activity
temporal task-queue list-partition --task-queue orders
```

It returns `ApproximateBacklogCount`, `ApproximateBacklogAge` (seconds), `TasksAddRate`, `TasksDispatchRate`, and `BacklogIncreaseRate` (add minus dispatch, "accurate for backlogs older than a few seconds"). And a fact worth memorising: "A `LastAccessTime` over one minute may indicate the Worker is at capacity or has shut down. Temporal Workers are removed if 5 minutes have passed since the last poll request" ([task-queue reference](https://docs.temporal.io/cli/command-reference/task-queue)).

Reading a real event history — do this at least once, slowly:

```bash
temporal workflow list --query 'ExecutionStatus="Running"'
temporal workflow describe -w my-workflow-id
temporal workflow show -w my-workflow-id --detailed
temporal workflow show -w my-workflow-id --follow
temporal workflow count --query 'WorkflowType="OrderWorkflow"'
```

Look for the `WorkflowExecutionStarted` / `WorkflowTaskScheduled` / `WorkflowTaskStarted` / `WorkflowTaskCompleted` rhythm, then find an `ActivityTaskScheduled` and trace its `scheduledEventId` through `ActivityTaskStarted` and `ActivityTaskCompleted`. Then look at `describe` output for `pendingActivities` and compare it against what you would have to recompute from the events — that gap is exactly what Mutable State is.

### `tdbg`: the operator's real tool

`tdbg` is the server-side debug CLI. It is in the server repo — entrypoint `cmd/tools/tdbg/main.go`, implementation in [`tools/tdbg/`](https://github.com/temporalio/temporal/tree/main/tools/tdbg), command tree in `tools/tdbg/tdbg_commands.go`. It is built by default (`make` / `make bins` include it) and ships in the `temporalio/admin-tools` image, so `docker run --rm -it temporalio/admin-tools:<tag> tdbg ...` works. There is **no `tdbg admin` subcommand** — the tree is flat.

```bash
# global flags: --address (default 127.0.0.1:7233), --namespace/-n, --context-timeout/--ct (5s), TLS flags

# shards
tdbg shard describe --shard-id 42
tdbg shard list-tasks --shard-id 42 --task-category transfer --max-task-id 9999999
#   --shard-id and --task-category are always required; --max-task-id is
#   additionally required for transfer/replication/visibility, and
#   --max-visibility-ts for timer.
tdbg shard list-tasks --shard-id 42 --task-category timer \
    --min-visibility-ts 2026-08-29T00:00:00Z --max-visibility-ts 2026-08-30T00:00:00Z
tdbg shard close-shard --shard-id 42        # force the owner to drop it; it will be re-acquired
tdbg shard remove-task ...                  # surgical task deletion; last resort

# workflows: describe | show | refresh-tasks (regenerate tasks) | rebuild (from history)
tdbg workflow show --workflow-id wid --output-filename history.json

# DLQ (this is the ONLY interface -- there is no `temporal ... dlq` command)
tdbg dlq list
tdbg dlq read  --dlq-type transfer --max-message-count 100
tdbg dlq merge --dlq-type transfer --last-message-id N
tdbg dlq purge --dlq-type transfer --last-message-id N
tdbg dlq job describe --job-token ...

# task queues, membership, decoding
tdbg taskqueue describe-task-queue-partition | force-unload-task-queue-partition | list-tasks
tdbg history-host describe
tdbg history-host get-shardid --namespace-id <uuid> --workflow-id wid --number-of-shards 512
tdbg membership list-gossip        # what the ring believes
tdbg membership list-db            # what cluster_membership says
tdbg decode proto | base64 | task
```

`tdbg history-host get-shardid` computes which *shard* a workflow hashes to — it is pure local arithmetic over `--namespace-id`, `--workflow-id`, and `--number-of-shards`, and makes no RPC, which is why the shard count has to be supplied. Pair it with `tdbg history-host describe --workflow-id wid` to learn which History host currently owns that shard. `tdbg membership list-gossip` versus `list-db` is the operational answer to "is the ring consistent with the bootstrap table", which is the first thing to check when hosts disagree about ownership.

**`tdbg shard close-shard` is the sharpest tool here.** It forces the owner to drop a shard; the controller re-acquires it (possibly on the same host) with a fresh range ID. That is the supported way to clear a wedged shard without restarting a pod. Use it knowing it costs a cold cache and a queue-state replay.

### Watching it work

Start the multirole compose stack, open Grafana, and watch these while you drive load:

```promql
# shard health
numshards_gauge
rate(sharditem_created_count[1m])
rate(shard_closed_count[1m])
histogram_quantile(0.95, sum by (le) (rate(sharditem_acquisition_latency_bucket[1m])))
histogram_quantile(0.95, sum by (le) (rate(lock_latency_bucket[1m])))   # target <5ms, ideally ~1ms

# task processing
histogram_quantile(0.99, sum by (le, task_type) (rate(task_latency_bucket[1m])))
sum by (task_type) (rate(task_requests[1m]))
pending_tasks

# matching
sum by (task_type)(rate(poll_success_sync[1m])) / sum by (task_type)(rate(poll_success[1m]))   # want >=0.99
approximate_backlog_count
approximate_backlog_age_seconds

# persistence
histogram_quantile(0.99, sum by (le, operation) (rate(persistence_latency_bucket[1m])))
sum by (operation) (rate(persistence_error_with_type[1m]))
```

The shard-lock-latency SLI is Temporal's own: "For good performance we'd expect shard lock latency to be less than 5ms, ideally around 1ms. This tells us that we probably have too few shards" — in their example, moving from 4 to 512 shards dropped p95 from ~50ms to ~1ms ([Scaling Temporal: The Basics](https://temporal.io/blog/scaling-temporal-the-basics)).

---

## Production gotchas

**1. `numHistoryShards` misconfiguration does not fail — it warns and proceeds.** The exact log line is `"Supplied configuration key/value mismatches persisted cluster metadata. Continuing with the persisted value as this value cannot be changed once initialized."` from [`temporal/server.go`](https://github.com/temporalio/temporal/blob/main/temporal/server.go), emitted by `overwriteCurrentClusterMetadataWithDBRecord` in `temporal/fx.go`. Alert on that string. Otherwise a Helm value drift from 512 to 4096 sits silently for a year and then someone "fixes" the persisted record.

**2. Shard count is permanent and it is your throughput ceiling.** The docs: "You set Shard capacity, and often overall Temporal Service throughput, at build time and can't adjust it later. Adding more Shards if needed requires a rebuild and a migration to the new Temporal Service" ([production checklist](https://docs.temporal.io/self-hosted-guide/production-checklist)). Documented range is 1 to 128K, with "Temporal recommends starting at a ratio of 1 History Service process for every 500 History Shards" ([Temporal Server](https://docs.temporal.io/temporal-service/temporal-server)). The commonly quoted 512 / 4,096 figures are **blog, not docs**: "Temporal recommends that small production clusters use 512 shards. To give an idea of scale, it is rare for even large Temporal clusters to go beyond 4,096 shards" ([Scaling Temporal](https://temporal.io/blog/scaling-temporal-the-basics)). The Docker template default is 4; `config/development-sqlite.yaml` uses 1. Neither is a production value.

**3. An ambiguous persistence error re-acquires the shard.** From `handleWriteErrorLocked`: on any unrecognised error, "We have no idea if the write failed or will eventually make it to persistence. Try to re-acquire the shard in the background." A ten-second database blip therefore produces a wave of shard reloads across every History host, each costing a cold Mutable State cache and a queue-state replay. **Persistence latency spikes are amplified, not absorbed.** This is the single most important failure-mode fact in this guide.

**4. Cassandra puts an entire shard in one partition, and every state transition is a Paxos round on it.** The `executions` primary key is `(shard_id, type, namespace_id, workflow_id, run_id, visibility_ts, task_id)` — partition key `shard_id` alone ([schema.cql](https://github.com/temporalio/temporal/blob/main/schema/cassandra/temporal/schema.cql)). Every workflow-task completion issues a logged batch with three `IF` predicates against that partition. Too few shards means Paxos contention on a hot partition, which appears as shard lock latency, not as database CPU.

**5. Task completion is a tombstone.** `CompleteHistoryTask` and `CompleteTasksLessThan` are `DELETE`s in the same hot partitions that reads scan. On Cassandra with LeveledCompactionStrategy this is survivable by design, but a queue that falls behind and then catches up in a burst produces a tombstone storm and read amplification on the *next* scan. Watch `task_batch_complete_counter` alongside compaction metrics.

**6. `AssertShardOwnership` is a no-op on both Cassandra and SQL, so shard lingering does not work.** `history.shardLingerTimeLimit` defaults to `0` and its own doc string says "Do NOT use non-zero value with persistence layers that are missing AssertShardOwnership support." Leave it at zero unless you have verified your store implements it. Enabling it delays shard handoff to no benefit.

**7. Default 4 partitions means one Task Queue can use at most 4 Matching hosts.** `matching.numTaskqueueReadPartitions` and `matching.numTaskqueueWritePartitions` both default to 4. A customer with one very hot Task Queue does not benefit from you adding Matching pods. Raise partitions per namespace/task queue; the cost is more `task_queues` rows, deeper forwarding, and thinner poller spread per partition.

**8. Async match costs four database operations per task; sync match costs zero.** Track the Poll Sync Rate SLI — `sum by (task_type)(rate(poll_success_sync[1m])) / sum by (task_type)(rate(poll_success[1m]))` — and treat a sustained drop as a database-load event, not a latency event. Async match "increases the load on the persistence database and is a lot less efficient" ([Scaling Temporal](https://temporal.io/blog/scaling-temporal-the-basics)).

**9. Forwarding is rate-limited at 10/s with 1 outstanding poll and 1 outstanding task by default.** `matching.forwarderMaxRatePerSecond` = 10, `matching.forwarderMaxOutstandingPolls` = `matching.forwarderMaxOutstandingTasks` = 1. If pollers are unevenly spread across partitions, forwarding is the only thing rescuing the empty ones, and it will not rescue them fast. Symptom: non-zero `approximate_backlog_count` on some partitions while `pending_polls` is non-zero on others.

**10. The sticky-worker window is hard-coded at 10 seconds.** `stickyPollerUnavailableWindow = 10 * time.Second` in `matching_engine.go`, with the comment "default sticky schedule_to_start timeout is 5s". There is no `matching.stickyPollerUnavailableWindow` key. A worker fleet that restarts slower than 10 seconds will see a burst of `StickyWorkerUnavailable` and full history replays.

**11. Persistence retries consume rate-limiter quota, and metrics are emitted per attempt.** The decorator order is retryable (outermost call) → metrics → rate limited → manager. A degraded database therefore self-amplifies: more retries, more quota consumed, more throttling of healthy traffic. Also, `persistence_latency` p99 during an incident is retry latency, not single-call latency. The adaptive limiter that could damp this (`health_request_rate_limiter.go`) **ships disabled with both thresholds at 0.0**.

**12. History size limits force-terminate workflows.** Breaching `limit.historySize.error` (50 MB) or `limit.historyCount.error` (51,200) or `limit.mutableStateSize.error` (8 MB) terminates the workflow with `FailureReasonHistorySizeExceedsLimit` and friends, from `enforceHistorySizeCheck` in `service/history/workflow/context.go`. The documented framing: "the Workflow Execution's Event History is limited to 51,200 Events or 50 MB and will warn you after 10,240 Events or 10 MB" ([limits](https://docs.temporal.io/workflow-execution/limits)). Pending-operation limits are **2,000** each for activities, children, signals, and cancels — not 128 — because "Too many entries in a single Workflow Execution's mutable state causes unstable persistence."

**13. `ContinueAsNewSuggested` flips at 4,096 events / 4 MiB, and that is undocumented.** The docs name the flag but never the threshold ([Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)). The values are `limit.historyCount.suggestContinueAsNew` and `limit.historySize.suggestContinueAsNew` in `constants.go`, and they are namespace-overridable — so you can advise a customer to lower them without touching their code.

**14. Upgrade order is schema first, then binary, one minor version at a time.** "Temporal Server should be upgraded sequentially, one minor version at a time. Before bumping to the next minor version, first upgrade to the highest available patch version of your current minor version." And: "Temporal Server ensures backward compatibility only between two successive minor versions. Consequently, skipping versions during an upgrade may lead to older data formats becoming unreadable" ([upgrade guide](https://docs.temporal.io/self-hosted-guide/upgrade-server)). You may skip patch versions; you may not skip minors.

**15. Every upgrade reloads every shard, and the docs put a number on it.** "each upgrade requires the History Service to load all Shards and update the Shard metadata, so allow approximately 10 minutes on each version for these processes to complete before upgrading to the next version" (same source). Ten minutes per hop, multiplied by the number of minor versions you are behind, is your real upgrade window.

**16. Whether a release needs a schema migration is only stated in the release notes.** "If a database schema upgrade is required, it will be called out directly in the release notes... however there is no guarantee that there is compatibility between any two non-consecutive versions." Your automation must parse or gate on release notes; there is no API for it.

**17. A persistence outage is also a membership outage.** Ringpop bootstraps from the `cluster_membership` table (`GetClusterMembers{LastHeartbeatWithin: 20s, PageSize: 1000}`). If the database is unavailable, new pods cannot join the ring at all, even though gossip between surviving pods still works. Recovery sequencing must bring persistence back before scaling anything.

**18. `broadcastAddress` is required when binding to all interfaces.** `buildBroadcastHostPort()` fails with "broadcastAddress required when listening on all interfaces (0.0.0.0/[::])". This is the most common Kubernetes membership misconfiguration, and it fails at startup rather than at first lookup.

**19. The default authorizer allows everything.** "If you do not explicitly configure an `Authorizer`, Temporal uses the default `noopAuthorizer`. This default allows every API request, with no authentication or access control" ([security](https://docs.temporal.io/self-hosted-guide/security)). Also: "Self-hosted Temporal doesn't support role-based access control (RBAC) or audit logging out of the box" ([production checklist](https://docs.temporal.io/self-hosted-guide/production-checklist)).

**20. A malformed dynamic config file is silently ignored.** `file_based_client.go` does not retry parse errors ("fail deterministically until the file is fixed") and keeps serving the last good values. The only signal is the `DynamicConfigUpdateFailure` gauge and the absence of the `"Updated dynamic config"` log line. Alert on both.

**21. Watch for the predicate-resolution escape hatch.** When a queue slice's predicate exceeds `history.queueMaxPredicateSize` (10 KB) or `history.queueShrinkPredicateMaxPendingKeys` (10), the framework replaces it with a universal predicate and emits `metrics.QueuePredicateResolutionLoss` with reason `max_pending_keys` or `predicate_size`. The queue then re-reads tasks it had already excluded. This is a correctness-preserving but throughput-destroying event, and it is a leading indicator of a shard in distress.

**22. DLQ requires `tdbg`.** There is no DLQ command on `temporal` or `OperatorService`; the groups are `cluster`, `namespace`, `nexus`, `search-attribute`. A task goes to the DLQ after `history.TaskDLQUnexpectedErrorAttempts` (default 70, "about an hour") or on a non-retryable error class, logging `"Task enqueued to DLQ"`. If nobody has `tdbg` in the incident-response path, those tasks are invisible and permanent.

**23. Metric names are not what you expect.** Several are counterintuitive and cost real debugging time: it is `syncmatch_latency` not `sync_match_latency`; `numshards_gauge` not `num_shards_gauge`; `sharditem_acquisition_latency` not `shard_item_acquisition_latency`; `pending_tasks` not `queue_pending_task_count`; `task_latency_schedule` not `task_schedule_latency`; `replication_tasks_lag` not `replication_task_lag`; and the ES prefix is `elasticsearch_bulk_processor_*` not `es_bulk_processor_*`. There are no separate `workflow_task_schedule_to_start_latency` / `activity_schedule_to_start_latency` metrics — there is one `task_schedule_to_start_latency` dimensioned by task type.

**24. Cassandra visibility was removed in v1.24.** If you are migrating a very old cell, that is a hard stop, not a warning ([visibility setup](https://docs.temporal.io/self-hosted-guide/visibility)). Also check the Helm compatibility break: "Helm chart versions below 0.73.1 are not compatible with `server` and `admin-tools` images version 1.30 and later" ([deployment](https://docs.temporal.io/self-hosted-guide/deployment)).

**25. OSS multi-cluster replication is labelled experimental.** "considered experimental and not subject to normal versioning and support policy", asynchronous, and "data across clusters is not strongly consistent" ([multi-cluster replication](https://docs.temporal.io/self-hosted-guide/multi-cluster-replication)). Do not present OSS XDC behaviour as a description of Temporal Cloud's HA behaviour; see the labelled inference above.

---
## How this shows up in cell lifecycle

Here is the internals content mapped onto each phase of cell lifecycle.

### Sizing shards at cell creation

This is the one irreversible decision in a cell build, so it deserves the most rigour. What the code and docs give you:

- The count is fixed at first run and enforced by silently ignoring your config thereafter.
- The documented operating range is 1 to 128K, with roughly **one History process per 500 shards**.
- The blog's practical guidance is 512 for a small production cluster, rarely above 4,096 even for large ones.
- The cost of over-provisioning is per-shard overhead: "each shard also has its own task processing queues, which puts extra pressure on the persistence database" — you pay CPU and memory on History pods and QPS on the database for shards that hold nothing.
- The cost of under-provisioning is Paxos contention on hot Cassandra partitions and shard-lock latency, and it is unfixable without a cell rebuild and customer migration.

*Inference, and the way I would actually run this:* pick shard count from the **cell's design capacity in state transitions per second**, not from expected namespace count, because state transitions are what drive the per-shard write path. Then validate with a load test against a candidate count and use Temporal's own SLI — p95 shard lock latency under 5 ms, ideally near 1 ms — as the accept/reject criterion. Choose the same value for every cell of a given class so that cells are fungible and so that a namespace can be migrated between them without a shard-count conversation. Round up: the asymmetry of consequences is extreme, since over-provisioning costs money and under-provisioning costs a migration.

Two second-order effects that matter for cell design:

- **A shard's ownership key is the shard ID string, and the ring is per-service.** So History pod count and shard count interact directly: with 512 shards and 4 History pods, each pod owns 128 shards and each pod loss moves 128 shards. Shard-per-pod is your blast-radius unit.
- **Because run ID is not in the shard hash, one Workflow ID is permanently pinned to one shard.** A customer with a single high-frequency continue-as-new workflow creates a hot shard that no amount of shard count fixes. That is a customer-shaping problem, not an infrastructure problem, and worth detecting early.

### Provisioning persistence

The write path per state transition is: one unconditional `history_node` insert, plus a logged batch of ~9–10 statements with three conditional predicates on a single partition, plus later deletes. Provision for that shape, not for "QPS".

- **Cassandra:** the dominant cost is LWT/Paxos on the shard partition. Size for `SERIAL` writes at your target state-transition rate, keep LeveledCompactionStrategy (the schema sets it), and expect tombstone pressure proportional to task throughput. `history.shardIOConcurrency` is **forced to 1** on Cassandra with a warning log — do not try to raise it.
- **SQL:** the dominant cost is transaction throughput and index maintenance, especially on `executions_visibility` with its ~50 STORED generated columns. Connection pool sizing (`maxConns`, `maxIdleConns`, `maxConnLifetime`) is static config, so it is a cell-build decision.
- **Visibility is a separate provisioning exercise.** ES bulk processor parameters (`ESProcessorBulkActions`, `ESProcessorBulkSize`, `ESProcessorFlushInterval`, `ESProcessorAckTimeout`, `IndexerConcurrency`) determine how much visibility lag you accumulate under load, and the `_wait_add` / `_wait_start` / `_commit` / `_request` latency decomposition tells you which stage is the bottleneck.
- **Rate limits are your isolation mechanism and they are three-tiered** (shard → namespace → system). `history.persistencePerShardNamespaceMaxQPS` is the finest-grained blast-radius control you have for a noisy namespace, and it defaults to `0` (off). Turning it on is one of the highest-leverage things a multi-tenant cell operator can do.

### Rolling restarts and node rotation

The sequence from the Membership section, turned into rules:

1. **One History pod at a time**, with a settle window at least `history.acquireShardInterval` (60s default) plus observed p99 `sharditem_acquisition_latency`.
2. **Drain properly.** `Monitor.EvictSelf()` on shutdown is what makes the ring converge quickly; a pod killed without draining leaves the ring waiting for gossip failure detection. Matching does this explicitly on `Stop`; verify your pod lifecycle hooks give it time.
3. **Never rotate History concurrently with persistence maintenance.** Ambiguous persistence errors trigger the range-bump path; combining that with membership churn produces overlapping re-acquisitions.
4. **Restart order across services** should mirror `initOrder`: Matching, then History, then Frontend, then Worker. Frontend last means clients keep a stable endpoint while the stateful tier settles.
5. **Expect `UNAVAILABLE`, not errors.** `ErrShardStatusUnknown` is `serviceerror.NewUnavailable("shard status unknown")` and SDKs retry it. Your SLO math should treat a bounded burst of `UNAVAILABLE` during rotation as expected; sustained `UNAVAILABLE` means a shard is failing to acquire.
6. **Karpenter consolidation is a rolling restart you did not schedule.** Consolidating two History nodes simultaneously is exactly the failure mode above. Node-level disruption budgets and `do-not-disrupt` semantics for History are worth more than for any other workload in the cell.

### Upgrades and schema migration ordering

The documented order is unambiguous and worth encoding directly into your pipeline:

1. Read the release notes for the target version and determine whether a schema change is required — this is the **only** place it is stated.
2. Run the schema tool against **both** the main and visibility databases:

```bash
temporal-cassandra-tool --tls --tls-ca-file <ca> --user <u> --password <p> \
  --endpoint <cassandra-host> --keyspace temporal --timeout 120 \
  update --schema-dir ./schema/cassandra/temporal/versioned

./temporal-sql-tool --ep <host> -p 5432 -u temporal -pw temporal \
  --pl postgres12 --db temporal \
  update-schema -d ./schema/postgresql/v12/temporal/versioned

./temporal-sql-tool --ep <host> -p 5432 -u temporal -pw temporal \
  --pl postgres12 --db temporal_visibility \
  update-schema -d ./schema/postgresql/v12/visibility/versioned

temporal-elasticsearch-tool update-schema --index "$ES_VISIBILITY_INDEX"
```

3. Upgrade the server binaries **after** the schema.
4. Wait ~10 minutes per version hop for shard reload and shard-metadata update.
5. Repeat, one minor version at a time. Patch versions may be skipped; minors may not.

Practical consequences for cell lifecycle:

- **Your version skew budget is the whole upgrade cost.** A cell three minors behind is three sequential hops, three schema checks, and ~30 minutes of shard reload before you even count validation. Cells drifting apart in version is therefore a compounding liability; keeping every cell within one minor of the fleet is worth real investment.
- **Schema tooling must be versioned with the binary.** The `admin-tools` image tag and the `server` image tag must match the release you are migrating to, since the versioned schema directories ship inside them. If you use `temporalio/server` rather than `auto-setup`, "you have to manually manage schema updates."
- **Stage the rollout.** Temporal's own control-plane blog describes deployment rings — "Ring 0: Synthetic traffic only... monitored here for at least a week. Ring 1: Low-priority traffic namespaces... Higher Rings: Gradually expanding to critical, high-priority traffic customers." A cell fleet is the natural unit for rings.
- **Schema migrations are cell-scoped, not fleet-scoped**, because each cell has its own database. That is the good news: schema risk is contained by the cell boundary, which is the whole point of the architecture.

### Capacity per cell

Assemble the ceiling from the internals rather than from a single number:

| Dimension | Ceiling set by | Where to watch it |
|---|---|---|
| State transitions/sec | Shard count × per-shard Paxos/transaction rate | `state_transition_count`, shard lock latency |
| Concurrent open workflows | Shard count × per-shard mutable-state footprint; database size | `executions` table size, `history_size` |
| Per-Task-Queue dispatch rate | Partition count × per-partition match rate | `syncmatch_latency`, `approximate_backlog_count` |
| Poller concurrency | `frontend.namespaceCount` per namespace per API; connection limits | `pending_polls`, `poll_timeouts` |
| Visibility write rate | ES bulk processor throughput, or SQL index maintenance | `elasticsearch_bulk_processor_commit_latency` |
| Replication throughput | Stream sender/receiver flow control, `history.ReplicationTaskProcessorShardQPS` | `replication_tasks_lag` |

**(b) What Cloud publishes about its own capacity model, and what it does not.** Temporal Cloud exposes capacity as **Actions Per Second** (default 500 APS, auto-scaling under On-Demand Capacity, or set explicitly via Temporal Resource Units), plus dynamic RPS/OPS limits, **20,000 Activity pollers and 20,000 Workflow Task pollers per namespace**, a 30-day default retention (settable 1–90), a Visibility API cap of **30 calls/sec**, and 10 namespaces per account by default ([Cloud limits](https://docs.temporal.io/cloud/limits)). Latency SLO is "p99 latency SLO of 200ms per region" ([service availability](https://docs.temporal.io/cloud/service-availability)); availability is 99.99% with a 99.9% contractual SLA standard, 99.99% contractual with the HA feature ([SLA](https://docs.temporal.io/cloud/sla)).

**History shard count is not documented anywhere in the Cloud documentation set.** I checked `/cloud/limits`, `/cloud/namespaces`, `/cloud/high-availability`, `/cloud/sla`, and `/cloud/service-availability`; the term does not appear. *Inference:* Cloud deliberately abstracts shards behind APS/RPS/OPS and Temporal Resource Units, which is consistent with everything published but is not stated. The right answer to "how many shards does a Cloud cell have" is: **not public — Cloud sells Actions and Requests per second, not shards.**

### The one-paragraph version for a design review

A cell's durability and throughput both come from a single mechanism: a fixed number of History Shards, each owned by exactly one process at a time, each fenced by a monotonically increasing range ID that is asserted as a condition on every write and that also allocates the shard's task IDs a million at a time. Everything else — Matching, Visibility, Replication, Archival — hangs off durable task rows written in the same transaction as the state they describe. Shard count is chosen once, cannot change, and sets the ceiling. Persistence latency does not degrade gracefully; it converts into shard reloads. And the cell boundary is what contains all of it, which is why cell lifecycle is the leverage point.

---

## Reading the codebase

A suggested order. This is roughly two focused days of reading if you skim the big files and read the small ones properly.

**Day one, morning — the map.**

1. [`docs/architecture/README.md`](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md) — 10 minutes, and it is the only overview you need.
2. [`docs/architecture/workflow-lifecycle.md`](https://github.com/temporalio/temporal/blob/main/docs/architecture/workflow-lifecycle.md) — the seven-step sequence diagram walkthrough. Read it beside a real `temporal workflow show` output.
3. [`docs/architecture/history-service.md`](https://github.com/temporalio/temporal/blob/main/docs/architecture/history-service.md) — shards, mutable state, queue processing, state transitions, consistency guarantees. Its collapsed "Code entrypoints" sections are the fastest index into the codebase that exists.
4. [`AGENTS.md`](https://github.com/temporalio/temporal/blob/main/AGENTS.md) — the maintainers' own directory map and conventions, in about 80 lines.

**Day one, afternoon — sharding, end to end.**

5. [`common/util.go`](https://github.com/temporalio/temporal/blob/main/common/util.go) — find `WorkflowIDToHistoryShard`. Twelve lines.
6. [`service/history/interfaces/shard_context.go`](https://github.com/temporalio/temporal/blob/main/service/history/interfaces/shard_context.go) — the interface only. Read every method name.
7. [`service/history/shard/task_key_generator.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/task_key_generator.go) — short, and it is the whole task-ID story.
8. [`service/history/shard/context_impl.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/context_impl.go) — do **not** read linearly. Read `transition` (and its invariant comment block), `acquireShard`, `renewRangeLocked`, `handleWriteErrorLocked`, `updateShardInfo`. That is maybe 300 of its 2,400 lines and it is 90% of the value.
9. [`service/history/shard/controller_impl.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/controller_impl.go) and [`ownership.go`](https://github.com/temporalio/temporal/blob/main/service/history/shard/ownership.go) — `acquireShards`, `verifyOwnership`, `shardRemoveAndStop`, `doLinger`.
10. [`common/persistence/cassandra/shard_store.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/cassandra/shard_store.go) — the CQL templates at the top, then `UpdateShard`.

**Day two, morning — the write path.**

11. [`schema/cassandra/temporal/schema.cql`](https://github.com/temporalio/temporal/blob/main/schema/cassandra/temporal/schema.cql) — read `executions`, `history_node`, `history_tree`, `tasks`, `cluster_membership`. Fifteen minutes, enormous payoff.
12. [`schema/postgresql/v12/temporal/schema.sql`](https://github.com/temporalio/temporal/blob/main/schema/postgresql/v12/temporal/schema.sql) — read it beside the CQL and note every structural difference.
13. [`common/persistence/persistence_interface.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/persistence_interface.go) — `ExecutionStore` and `TaskStore`.
14. [`common/persistence/data_interfaces.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/data_interfaces.go) — `WorkflowMutation`, `WorkflowSnapshot`, and the error types. Skim the rest.
15. [`service/history/workflow/context.go`](https://github.com/temporalio/temporal/blob/main/service/history/workflow/context.go) — `UpdateWorkflowExecutionWithNew`, `enforceHistorySizeCheck`, `PersistWorkflowEvents`.
16. [`service/history/workflow/mutable_state_impl.go`](https://github.com/temporalio/temporal/blob/main/service/history/workflow/mutable_state_impl.go) — the struct declaration and `closeTransaction()` / `IsDirty()`. Do not attempt the rest.
17. [`common/persistence/cassandra/mutable_state_store.go`](https://github.com/temporalio/temporal/blob/main/common/persistence/cassandra/mutable_state_store.go) — the query templates and `UpdateWorkflowExecution`'s batch construction.

**Day two, afternoon — dispatch and queues.**

18. [`service/history/tasks/category.go`](https://github.com/temporalio/temporal/blob/main/service/history/tasks/category.go) — five minutes; memorise the IDs.
19. [`service/history/queues/queue_base.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/queue_base.go), then [`reader.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/reader.go) and [`slice.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/slice.go), then [`executable.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/executable.go), then [`mitigator.go`](https://github.com/temporalio/temporal/blob/main/service/history/queues/mitigator.go) + `action_pending_task_count.go`.
20. [`service/history/transfer_queue_active_task_executor.go`](https://github.com/temporalio/temporal/blob/main/service/history/transfer_queue_active_task_executor.go) — `Execute` and `processActivityTask` / `processWorkflowTask`.
21. [`common/tqid/task_queue_id.go`](https://github.com/temporalio/temporal/blob/main/common/tqid/task_queue_id.go) — `RpcName`, `ParentPartition`, the sticky partition methods.
22. [`service/matching/matcher.go`](https://github.com/temporalio/temporal/blob/main/service/matching/matcher.go) — `Offer`, `MustOffer`, `Poll`. Then [`db.go`](https://github.com/temporalio/temporal/blob/main/service/matching/db.go) for the second range-ID fence.
23. [`client/history/redirector.go`](https://github.com/temporalio/temporal/blob/main/client/history/redirector.go) — 40 lines that explain why rolling restarts work.

**Anytime — configuration and wiring.**

24. [`config/development-sqlite.yaml`](https://github.com/temporalio/temporal/blob/main/config/development-sqlite.yaml) — the static config shape.
25. [`common/dynamicconfig/constants.go`](https://github.com/temporalio/temporal/blob/main/common/dynamicconfig/constants.go) — do not read it, **grep it**. It is ~175 KB and every setting carries its default and a doc comment. This is the best-documented file in the repo.
26. [`temporal/fx.go`](https://github.com/temporalio/temporal/blob/main/temporal/fx.go) and [`temporal/server_impl.go`](https://github.com/temporalio/temporal/blob/main/temporal/server_impl.go) — `TopLevelModule`, the service providers, `initOrder`.
27. [`common/metrics/metric_defs.go`](https://github.com/temporalio/temporal/blob/main/common/metrics/metric_defs.go) — grep for the exact string literal before you write any dashboard query.

### Tests worth reading

The repo distinguishes three tiers, described in [`docs/development/testing.md`](https://github.com/temporalio/temporal/blob/main/docs/development/testing.md) and driven by `make unit-test`, `make integration-test`, `make functional-test`.

- **Unit tests** live beside the code (`*_test.go`). The maintainers' conventions: prefer `require` over `assert`, avoid testify suites in unit tests, use `require.Eventually` instead of `time.Sleep` (the linter forbids `time.Sleep`), and always build with `-tags test_dep`.
- **Functional tests** live in [`tests/`](https://github.com/temporalio/temporal/tree/main/tests) and spin up a whole in-process test cluster; they *do* use testify suites because the cluster setup needs them. These are the ones to read when you want to know what a feature actually does end to end, because they exercise the real service boundaries.
- Specifically worth reading for this guide's material: the shard controller tests (`service/history/shard/controller_impl_test.go`) for the acquisition and ownership-loss state machine, the queue framework tests in `service/history/queues/` for the reader/slice/mitigation behaviour, and the matching engine tests for sync-vs-async match paths.
- `temporaltest/` provides an embeddable server for Go tests, and `chasm/` is the newer state-machine framework — worth knowing exists, since new components (schedulers, callbacks) are being built on it rather than on bespoke mutable-state code.

---

## Learning path

### Day 1 — get it running and trace one workflow

- Install the CLI, run `temporal server start-dev --db-filename ./t.db --ui-port 8233`.
- Run a hello-world sample from `samples-go`. Then `temporal workflow show -w <id> --detailed` and read every event. Identify the `WorkflowTaskScheduled` → `Started` → `Completed` cycle, and one activity's `scheduledEventId` chain.
- Run `temporal workflow describe -w <id>` and compare `pendingActivities` against what you would recompute from the events. That is Mutable State.
- Read `docs/architecture/README.md` and `workflow-lifecycle.md` side by side with your event output.
- Read `common/util.go`'s `WorkflowIDToHistoryShard` and compute by hand which shard your workflow would be on in a 512-shard cell.
- **Success criterion:** you can narrate the full path of one `StartWorkflowExecution` naming Frontend, History, the transfer queue, Matching, and the worker, without notes.

### Week 1 — the stateful tier

- Bring up the real stack: `make start-dependencies`, then `make install-schema-cass-es && make start-cass-es`. Confirm Docker is required for this and the dev server is not.
- Read `schema.cql` and the Postgres `schema.sql` back to back. Write down every structural difference you find; there are at least six.
- Follow the Day-one-afternoon reading list above through the shard code. Then: `tdbg shard describe --shard-id 1`, `tdbg shard list-tasks --shard-id 1 --task-category transfer`, and `tdbg shard close-shard --shard-id 1` while watching logs. Find the range-ID increment in the log output.
- Read `queue_base.go` `checkpoint()` and correlate it with `SetQueueState` calls and `history.shardUpdateMinInterval`.
- Load-test one task queue hard enough to force async match; watch the Poll Sync Rate drop and `approximate_backlog_count` rise. Then raise `matching.numTaskqueueReadPartitions` and watch it recover.
- Read the frontend interceptor chain and `service/frontend/configs/quotas.go`. Map three real customer-facing errors (`RESOURCE_EXHAUSTED`, `UNAVAILABLE`, `StickyWorkerUnavailable`) back to the exact code that produces them.
- **Success criterion:** given a symptom (rising `task_latency`, falling Poll Sync Rate, a burst of `shard_closed_count`), you can name the probable mechanism and the metric that would confirm it.

### Month 1 — operate it like an owner

- Run the `docker-compose-multirole.yaml` stack from `samples-server` and practise a rolling restart of the history container while driving load. Measure the `UNAVAILABLE` burst and the acquisition-latency tail. Repeat with two history containers restarting at once and observe the difference; that experiment is the argument for your disruption budget.
- Do a full upgrade rehearsal: pick a cell two minor versions behind, run the schema tool for each hop, upgrade, wait the documented ten minutes, validate. Time the whole thing. That number is your fleet's version-skew cost.
- Build the dashboard from the metric names in this guide, using the exact string literals. Verify each one exists by grepping `metric_defs.go` before you commit the query.
- Set up alerts on the four silent failures: the `numHistoryShards` mismatch log line, `DynamicConfigUpdateFailure`, `QueuePredicateResolutionLoss`, and `"Task enqueued to DLQ"`.
- Write your own shard-sizing document for the cell classes your team runs: target state transitions/sec, chosen shard count, the load test that validated it, and the shard-lock-latency evidence. Cite the docs' 1-process-per-500-shards ratio and the blog's 512/4,096 guidance, and be explicit that neither is a Cloud statement.
- Read the multi-cluster replication code (`service/history/replication/stream_sender.go`, `common/cluster/metadata.go`) and be able to explain failover versions on a whiteboard — then be equally able to say precisely where the public documentation stops and inference begins for Temporal Cloud.
- **Success criterion:** you can defend a shard-count decision, a restart procedure, and an upgrade window in a design review, with a source citation for every number.

---

## References

1. [temporalio/temporal — GitHub](https://github.com/temporalio/temporal) — the server source; everything in this guide was read against `main` on 2026-08-29 (latest tag v1.31.2).
2. [Architecture overview — temporalio/temporal docs](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md) — the four services, tasks, and design premises in ten minutes.
3. [History Service architecture — temporalio/temporal docs](https://github.com/temporalio/temporal/blob/main/docs/architecture/history-service.md) — shards, mutable state, queue processing, and the transactional-outbox consistency argument.
4. [Matching Service architecture — temporalio/temporal docs](https://github.com/temporalio/temporal/blob/main/docs/architecture/matching-service.md) — partitions, forwarding, and the root-partition load rule.
5. [Workflow lifecycle sequence diagrams — temporalio/temporal docs](https://github.com/temporalio/temporal/blob/main/docs/architecture/workflow-lifecycle.md) — the seven-step end-to-end trace with code entrypoints.
6. [AGENTS.md — temporalio/temporal](https://github.com/temporalio/temporal/blob/main/AGENTS.md) — the maintainers' own directory map and code conventions.
7. [Makefile — temporalio/temporal](https://github.com/temporalio/temporal/blob/main/Makefile) — the real target names: `start`, `start-dependencies`, `install-schema-*`, `tdbg`.
8. [common/util.go — temporalio/temporal](https://github.com/temporalio/temporal/blob/main/common/util.go) — `WorkflowIDToHistoryShard`, the FarmHash shard mapping.
9. [service/history/interfaces/shard_context.go](https://github.com/temporalio/temporal/blob/main/service/history/interfaces/shard_context.go) — the `ShardContext` interface after the package move.
10. [service/history/shard/context_impl.go](https://github.com/temporalio/temporal/blob/main/service/history/shard/context_impl.go) — `renewRangeLocked`, `acquireShard`, `handleWriteErrorLocked`, the context state machine.
11. [service/history/shard/task_key_generator.go](https://github.com/temporalio/temporal/blob/main/service/history/shard/task_key_generator.go) — the `rangeID << 20` task-ID block arithmetic.
12. [service/history/shard/controller_impl.go](https://github.com/temporalio/temporal/blob/main/service/history/shard/controller_impl.go) — `acquireShards`, the semaphore, shard lingering.
13. [service/history/shard/ownership.go](https://github.com/temporalio/temporal/blob/main/service/history/shard/ownership.go) — `verifyOwnership` and the membership-driven acquire loop.
14. [common/persistence/cassandra/shard_store.go](https://github.com/temporalio/temporal/blob/main/common/persistence/cassandra/shard_store.go) — the `IF range_id = ?` lightweight transaction.
15. [common/persistence/persistence_interface.go](https://github.com/temporalio/temporal/blob/main/common/persistence/persistence_interface.go) — `ExecutionStore`, `TaskStore`, `ShardStore`, `QueueV2`.
16. [common/persistence/data_interfaces.go](https://github.com/temporalio/temporal/blob/main/common/persistence/data_interfaces.go) — `WorkflowMutation`, `WorkflowSnapshot`, and every persistence error type.
17. [schema/cassandra/temporal/schema.cql](https://github.com/temporalio/temporal/blob/main/schema/cassandra/temporal/schema.cql) — the real DDL, including the single-partition `executions` primary key.
18. [schema/postgresql/v12/temporal/schema.sql](https://github.com/temporalio/temporal/blob/main/schema/postgresql/v12/temporal/schema.sql) — the normalized SQL model and the `shards` table.
19. [common/persistence/cassandra/mutable_state_store.go](https://github.com/temporalio/temporal/blob/main/common/persistence/cassandra/mutable_state_store.go) — the conditional batch and `db_record_version` CAS.
20. [service/history/workflow/mutable_state_impl.go](https://github.com/temporalio/temporal/blob/main/service/history/workflow/mutable_state_impl.go) — the dirty-tracking delta maps and `closeTransaction`.
21. [service/history/workflow/context.go](https://github.com/temporalio/temporal/blob/main/service/history/workflow/context.go) — the workflow lock, continue-as-new, and the history-size enforcement.
22. [service/history/tasks/category.go](https://github.com/temporalio/temporal/blob/main/service/history/tasks/category.go) — the persisted category IDs and Immediate/Scheduled types.
23. [service/history/queues/queue_base.go](https://github.com/temporalio/temporal/blob/main/service/history/queues/queue_base.go) — `checkpoint()` and the delete-then-persist ordering invariant.
24. [service/history/visibility_queue_task_executor.go](https://github.com/temporalio/temporal/blob/main/service/history/visibility_queue_task_executor.go) — the visibility task pipeline and the `_version` ordering guarantee.
25. [common/tqid/task_queue_id.go](https://github.com/temporalio/temporal/blob/main/common/tqid/task_queue_id.go) — `/_sys/` partition naming, `ParentPartition`, sticky partition semantics.
26. [service/matching/matcher.go](https://github.com/temporalio/temporal/blob/main/service/matching/matcher.go) — sync match, `MustOffer`, and the backlog gate on forwarding.
27. [client/history/redirector.go](https://github.com/temporalio/temporal/blob/main/client/history/redirector.go) — the `ShardOwnershipLost` redirect loop that makes rolling restarts survivable.
28. [common/membership/interfaces.go](https://github.com/temporalio/temporal/blob/main/common/membership/interfaces.go) — `Monitor` and `ServiceResolver`.
29. [common/membership/ringpop/monitor.go](https://github.com/temporalio/temporal/blob/main/common/membership/ringpop/monitor.go) — heartbeat cadence, the 20s health cutoff, and DB-backed bootstrap.
30. [common/cluster/metadata.go](https://github.com/temporalio/temporal/blob/main/common/cluster/metadata.go) — failover-version arithmetic and cluster-metadata validation.
31. [service/history/replication/stream_sender.go](https://github.com/temporalio/temporal/blob/main/service/history/replication/stream_sender.go) — streaming replication, catch-up versus live, tiered priority.
32. [temporal/fx.go](https://github.com/temporalio/temporal/blob/main/temporal/fx.go) — the fx graph, per-service child apps, and the shard-count reconciliation.
33. [config/development-sqlite.yaml](https://github.com/temporalio/temporal/blob/main/config/development-sqlite.yaml) — the canonical static-config shape.
34. [common/dynamicconfig/constants.go](https://github.com/temporalio/temporal/blob/main/common/dynamicconfig/constants.go) — every dynamic-config key with its default and doc comment; grep, do not read.
35. [tools/tdbg/tdbg_commands.go](https://github.com/temporalio/temporal/blob/main/tools/tdbg/tdbg_commands.go) — the authoritative `tdbg` command and flag tree.
36. [temporalio/api — WorkflowService proto](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto) — the public API surface every SDK speaks.
37. [Temporal Server — Temporal Docs](https://docs.temporal.io/temporal-service/temporal-server) — the shard-immutability statement, 1–128K range, and the 1-process-per-500-shards ratio.
38. [Cluster configuration reference — Temporal Docs](https://docs.temporal.io/references/configuration) — `numHistoryShards` warning, persistence and clusterMetadata schema.
39. [Production readiness checklist — Temporal Docs](https://docs.temporal.io/self-hosted-guide/production-checklist) — shard capacity as a build-time decision, the three metric families, the no-RBAC caveat.
40. [Upgrade the Temporal Server — Temporal Docs](https://docs.temporal.io/self-hosted-guide/upgrade-server) — sequential minors, schema-first order, the ten-minutes-per-hop shard reload, and the exact tool commands.
41. [Workflow Execution limits — Temporal Docs](https://docs.temporal.io/workflow-execution/limits) — the warn/error thresholds and why mutable-state size destabilises persistence.
42. [CLI server command reference — Temporal Docs](https://docs.temporal.io/cli/command-reference/server) — every `temporal server start-dev` flag and the not-for-production warning.
43. [CLI operator command reference — Temporal Docs](https://docs.temporal.io/cli/command-reference/operator) — `cluster describe --detail`, namespace and search-attribute management; also proves there is no DLQ command.
44. [CLI task-queue command reference — Temporal Docs](https://docs.temporal.io/cli/command-reference/task-queue) — backlog stats and the five-minute poller-removal rule.
45. [Self-hosted Visibility setup — Temporal Docs](https://docs.temporal.io/self-hosted-guide/visibility) — the store/version compatibility matrix and Cassandra visibility removal in v1.24.
46. [Self-hosted Multi-Cluster Replication — Temporal Docs](https://docs.temporal.io/self-hosted-guide/multi-cluster-replication) — failover versions, the experimental label, and the consistency caveats.
47. [Temporal Platform security — Temporal Docs](https://docs.temporal.io/self-hosted-guide/security) — the noopAuthorizer default and the ClaimMapper/Authorizer seam.
48. [High Availability — Temporal Cloud Docs](https://docs.temporal.io/cloud/high-availability) — multi-region/multi-cloud replication, 20-minute RTO, and the "cell architecture" statement under Same-region Replication.
49. [Service Level Agreement — Temporal Cloud Docs](https://docs.temporal.io/cloud/sla) — the explicit cell-architecture paragraph, 99.9% vs 99.99%, and the excluded error codes.
50. [System limits — Temporal Cloud Docs](https://docs.temporal.io/cloud/limits) — 500 APS default, 20,000 pollers per namespace, retention and visibility-API caps; note the absence of any shard-count statement.
51. [Scaling Temporal: The Basics — Temporal Blog](https://temporal.io/blog/scaling-temporal-the-basics) — the 512/4,096 shard guidance, the shard-lock-latency SLI, and Poll Sync Rate.
52. [Building Durable Cloud Control Systems with Temporal — Temporal Blog](https://temporal.io/blog/building-durable-cloud-control-systems-with-temporal) — cell = its own AWS account, VPC, and EKS cluster; deployment rings.
