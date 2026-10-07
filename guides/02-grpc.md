# gRPC and Protocol Buffers (Go-first)

**Why this matters.** Temporal's entire client/server contract is gRPC: the schema lives in [`temporalio/api`](https://github.com/temporalio/api), the SDKs dial the frontend over it, and frontend, history, and matching talk to each other over it as well — [`common/rpc/grpc.go`](https://github.com/temporalio/temporal/blob/main/common/rpc/grpc.go) is the single dial helper every internode client goes through. If you work on the infrastructure rather than on the Temporal server itself, you are not primarily writing gRPC services; you are standing up the cells those services run in, which means you own the parts that break: the load balancer in front of the frontend, the certificates that terminate mTLS, the keepalive and idle-timeout settings that decide whether a worker's 60-second long poll survives, and the drain semantics that decide whether a cell upgrade is invisible or drops ten thousand in-flight streams. Almost every gRPC production incident is really an infrastructure incident wearing an application-layer costume. This guide is about recognizing the costume.

---

## The mental model

Hold four layers in your head at once. Most confusion about gRPC comes from reasoning at the wrong one.

**Layer 1 — Protobuf is a schema language and a wire format, and they are separable.** The `.proto` file is a human-facing IDL. The wire format is a sequence of `(field_number, wire_type, value)` tuples with no field names in it at all ([encoding reference](https://protobuf.dev/programming-guides/encoding/)). Everything surprising about schema evolution follows from that one fact: renaming a field is free on the wire, renumbering it is catastrophic.

**Layer 2 — gRPC is a calling convention layered on HTTP/2.** An RPC is exactly one HTTP/2 stream. The method is the `:path` (`/package.Service/Method`), request metadata is HTTP/2 headers, the deadline is the `grpc-timeout` header, messages are length-prefixed frames in the DATA stream, and the final status is an HTTP/2 *trailer* (`grpc-status`, `grpc-message`) — which is why gRPC needs real trailer support and why naive HTTP/1 proxies cannot carry it ([PROTOCOL-HTTP2](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md)).

**Layer 3 — The channel is a *pool*, not a connection.** In grpc-go, a `ClientConn` owns a name resolver, a load-balancing policy, and a set of subchannels. It reconnects on its own, it can go idle, and it does not correspond one-to-one with a TCP socket. `grpc.NewClient` is a constructor that performs no I/O; it does not "connect" ([anti-patterns](https://github.com/grpc/grpc-go/blob/v1.83.2/Documentation/anti-patterns.md)).

**Layer 4 — Because HTTP/2 multiplexes, connection-level load balancing is a lie.** One TCP connection carries every concurrent RPC. An L4 load balancer picks a backend once, at connection setup, and every subsequent request rides the same pipe. This is *the* gRPC infrastructure gotcha and it has its own section below.

A corollary worth internalizing early: **gRPC pushes policy into configuration that ships with the schema.** Retries, hedging, timeouts, and the load-balancing policy are all expressible in a JSON "service config" that a resolver can hand to the client ([service config doc](https://github.com/grpc/grpc/blob/master/doc/service_config.md)). Temporal uses exactly this mechanism to force round-robin: both the Go SDK and the server's internode dialer pass `{"loadBalancingConfig": [{"round_robin":{}}]}` as the default service config.

Current versions as of this writing (2026-08-29): [grpc-go v1.83.2](https://github.com/grpc/grpc-go/releases), [`google.golang.org/protobuf` v1.36.12](https://github.com/protocolbuffers/protobuf-go/releases), [protoc 34.1](https://protobuf.dev/installation/), [buf v1.72.0](https://buf.build/docs/cli/installation/).

---

## Core concepts

### Protobuf: proto3 semantics you actually need

A minimal, idiomatic file. Note the directory-matching package, the versioned package name, and the enum zero value:

```proto
syntax = "proto3";

package cellctl.v1;

option go_package = "github.com/example/cellctl/gen/go/cellctl/v1;cellctlv1";

import "google/protobuf/timestamp.proto";
import "google/protobuf/duration.proto";

enum CellPhase {
  CELL_PHASE_UNSPECIFIED = 0;   // required by convention: zero value means "not set"
  CELL_PHASE_PROVISIONING = 1;
  CELL_PHASE_READY = 2;
  CELL_PHASE_DRAINING = 3;
  CELL_PHASE_TERMINATED = 4;
}

message Cell {
  string cell_id = 1;
  string cloud = 2;                            // "aws" | "gcp" | "azure"
  string region = 3;
  CellPhase phase = 4;
  google.protobuf.Timestamp created_at = 5;
  map<string, string> labels = 6;

  // Presence matters here: "0 replicas" and "caller did not specify" differ.
  optional int32 desired_replicas = 7;

  oneof network {
    AwsNetwork aws = 10;
    GcpNetwork gcp = 11;
    AzureNetwork azure = 12;
  }

  reserved 8, 9;
  reserved "legacy_vpc_id", "legacy_subnet";
}
```

The rules that bite:

- **Field numbers are the contract.** Numbers 1–15 encode their tag in a single byte; 16–2047 take two. Put your hot, always-present fields in 1–15 ([encoding](https://protobuf.dev/programming-guides/encoding/)). Numbers 19000–19999 are reserved for the protobuf implementation itself.
- **Every field is optional in proto3's wire sense.** There is no "required". A missing scalar deserializes to its zero value.
- **Unknown fields are preserved by default in proto3** (since protobuf 3.5). A middle-tier service that parses and re-serializes a message will not silently drop fields it does not know about. Do not rely on this for security decisions — rely on it for rolling upgrades.
- **`reserved`** removes a field number and/or name from reuse. Use it *every time* you delete a field. It is the only mechanism that stops a future engineer from reusing number 8 for a `bool` when an old client still sends a `string` there.

### Presence: proto3's original sin, `optional`, and `oneof`

In original proto3, singular scalars had **no explicit presence**: you could not distinguish `desired_replicas = 0` from "field absent". This broke every PATCH-style API ever written. Since protobuf 3.15, `optional` on a proto3 singular field restores explicit presence and generates a pointer in Go plus a `Has*`-style check ([field presence](https://protobuf.dev/programming-guides/field_presence/)).

| Construct | Presence? | Go shape (open API) | Use when |
|---|---|---|---|
| `int32 x = 1;` | No (implicit) | `X int32` | Zero is a meaningful, safe default |
| `optional int32 x = 1;` | Yes | `X *int32` | Zero and unset must differ (updates, patches) |
| `google.protobuf.Int32Value x = 1;` | Yes (wrapper) | `X *wrapperspb.Int32Value` | Legacy; prefer `optional` in new code |
| `message Foo x = 1;` | Yes (always) | `X *Foo` | Sub-messages always have presence |
| `oneof n { A a = 1; B b = 2; }` | Yes (which-one) | interface + wrapper structs | Mutually exclusive variants |
| `repeated`, `map` | No | slice / map | Empty and absent are indistinguishable |

`oneof` is a *wire-level* union: setting one member clears the others, and adding a new member to an existing `oneof` is wire-safe but changes the generated Go type switch (a source-level break for exhaustive switches). Moving an existing standalone field *into* a `oneof` is a source break and, if the field could previously coexist with the others, a semantic break.

`map<K,V>` is syntactic sugar for `repeated MapEntry { key = 1; value = 2; }`. Consequences: **map ordering is not defined**, maps cannot be `repeated`, and — critically for anyone comparing bytes — **protobuf serialization is not canonical**. Two serializations of the same message may differ. Never hash serialized protobuf bytes for equality or signing without a canonicalization step ([serialization is not canonical](https://protobuf.dev/programming-guides/serialization-not-canonical/)).

### Enums and the zero-value trap

Proto3 enums are **open**: an unknown numeric value received on the wire is preserved and returned as-is, not rejected ([enum behavior](https://protobuf.dev/programming-guides/enum/)). Combined with implicit presence, this produces the classic bug:

```go
// BAD: a new client sends CELL_PHASE_QUARANTINED = 5, which this old server
// does not know. It is NOT zero, so this switch silently falls through.
switch cell.Phase {
case cellctlv1.CellPhase_CELL_PHASE_READY:
    admit(cell)
default:
    // ... and "unknown future phase" got the same handling as "unspecified".
}
```

Rules, in order of importance:

1. The zero value must be named `<ENUM>_UNSPECIFIED` and must mean "caller did not set this". Never give `0` real semantics — an unset field and a deliberately-set-to-first-value field are indistinguishable.
2. Enum value names live in a **C++-style scope shared with the enclosing package**, not the enum. Two enums in the same proto package cannot both declare `READY`. Hence the `CELL_PHASE_` prefix convention.
3. Handle unknown values explicitly. Treat "not a value I compiled against" as a distinct branch, usually fail-closed.

### Schema evolution: what is safe, what breaks

This is the table to memorize. "Wire-safe" means old and new binaries interoperate on the network. "Source-safe" means existing Go code still compiles.

| Change | Wire-safe | Source-safe (Go) | Notes |
|---|---|---|---|
| Add a new field with a fresh number | Yes | Yes | The single safest change |
| Delete a field, and `reserved` its number + name | Yes | No | Callers referencing it stop compiling — intended |
| Delete a field without `reserved` | **No** | No | Number can be reused later with a different type |
| Rename a field (same number, same type) | Yes | No | Names are not on the wire; JSON/ProtoJSON *does* break |
| Change a field's number | **No** | No | Catastrophic; old peers misparse |
| `int32` ↔ `int64` ↔ `uint32` ↔ `uint64` ↔ `bool` | Yes (varint family) | Maybe | Truncation/sign surprises; audit ranges |
| `sint32` ↔ `int32` | **No** | — | Different varint (zigzag) encoding |
| `string` ↔ `bytes` | Yes if UTF-8 valid | Yes-ish | Both are length-delimited |
| `fixed32` ↔ `sfixed32` (and 64-bit pair) | Yes | Maybe | Same wire type |
| singular ↔ `repeated` (same type) | Mostly | No | Packed scalars: singular reader sees last element |
| Add a value to an enum | Yes | Yes | But readers must handle unknowns (see above) |
| Add a member to an existing `oneof` | Yes | No | Type switch is no longer exhaustive |
| Move a field into or out of a `oneof` | **No (semantics)** | No | Clearing behaviour changes |
| Add a new `rpc` to a service | Yes | Yes | Old servers return `UNIMPLEMENTED` |
| Delete or rename an `rpc` | **No** | No | Callers get `UNIMPLEMENTED` at runtime |
| Rename a `package` or `service` | **No** | No | The `:path` changes; every client 404s |
| Change a method to/from streaming | **No** | No | Different framing contract entirely |

Two more that catch people: changing the **default value semantics** of a field (e.g. deciding `timeout_seconds = 0` now means "infinite" rather than "use default") is wire-safe and source-safe and still a production outage. And moving a message to a different `.proto` file within the same package is safe; moving it to a different *package* is not, because the fully-qualified name is what `google.protobuf.Any` records.

Full authoritative list: [Buf's breaking-change rules](https://buf.build/docs/breaking/rules/) and [protobuf's dos and don'ts](https://protobuf.dev/best-practices/dos-donts/).

### Well-known types and `Any`

Import them; do not reinvent them.

| Type | Use |
|---|---|
| `google.protobuf.Timestamp` | Absolute time (UTC, seconds + nanos). Not a duration. |
| `google.protobuf.Duration` | Signed span. Temporal's API uses this pervasively for timeouts. |
| `google.protobuf.Empty` | Request/response with no fields — but prefer a named empty message so you can add fields later without a breaking signature change. |
| `google.protobuf.FieldMask` | Partial update paths for PATCH-style RPCs. |
| `google.protobuf.Struct` / `Value` | Genuinely schemaless JSON-ish blobs. |
| `google.protobuf.Any` | Type-erased embedding: stores a type URL plus bytes. |

`Any` is how gRPC's rich error model works, and it is also how you smuggle plugin-specific payloads through a generic control plane. The cost is that the receiver needs the descriptor to unmarshal, so `Any` across an org boundary without a shared registry (a BSR, or a vendored descriptor set) is a support ticket generator. Full list: [well-known types](https://protobuf.dev/reference/protobuf/google.protobuf/).

**Editions.** Protobuf is migrating away from `syntax = "proto2"/"proto3"` toward *editions*, where per-file and per-field behavior is set by explicit `features` ([editions overview](https://protobuf.dev/editions/overview/)). Edition 2023 and Edition 2024 both have published language specs. For a Go shop in 2026 this mostly means: new files can stay proto3, but expect `edition = "2024"` to show up in dependencies, and make sure your `protoc`/`buf` are new enough to compile them.

### Codegen in Go: `protoc` vs `buf`

The classic path uses `protoc` plus two plugins, and it works ([Go quickstart](https://grpc.io/docs/languages/go/quickstart/)):

```sh
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
export PATH="$PATH:$(go env GOPATH)/bin"

protoc --go_out=. --go_opt=paths=source_relative \
       --go-grpc_out=. --go-grpc_opt=paths=source_relative \
       proto/cellctl/v1/cellctl.proto
```

Two plugins, two outputs: `protoc-gen-go` emits `*.pb.go` (messages, marshalling), `protoc-gen-go-grpc` emits `*_grpc.pb.go` (client stub + server interface). They version independently — a fact that has bitten every Go monorepo at least once.

The reason **buf is the default in most shops now** is not aesthetics. `protoc` requires you to manage an `--proto_path` include graph by hand, vendor third-party protos into your tree, install matching plugin binaries on every developer machine and CI runner, and re-derive the file list on every invocation. Buf replaces all of that with a declarative workspace, a dependency lockfile, remote plugins, and — the part infra teams care about — `buf breaking`, which mechanically diffs your schema against a git ref and fails CI ([migrate from protoc](https://buf.build/docs/migration-guides/migrate-from-protoc/)).

`buf.yaml` at the workspace root:

```yaml
# https://buf.build/docs/configuration/v2/buf-yaml
version: v2
modules:
  - path: proto
lint:
  use:
    - STANDARD
breaking:
  use:
    - FILE
```

`buf.gen.yaml` next to it. This uses **remote plugins**, so nobody needs a local `protoc` or plugin install:

```yaml
version: v2
clean: true
managed:
  enabled: true
  override:
    - file_option: go_package_prefix
      value: github.com/example/cellctl/gen/go
plugins:
  - remote: buf.build/protocolbuffers/go:v1.36.11
    out: gen/go
    opt: paths=source_relative
  - remote: buf.build/grpc/go:v1.6.2
    out: gen/go
    opt: paths=source_relative
inputs:
  - directory: proto
```

`clean: true` deletes each `out` directory before generating, so deleting a `.proto` does not leave an orphaned `.pb.go` behind. `managed.enabled` lets `buf.gen.yaml` set `go_package` instead of hard-coding it in every file — which matters when the same protos are consumed by more than one repo ([buf generate quickstart](https://buf.build/docs/generate/tutorial/), [`buf.gen.yaml` reference](https://buf.build/docs/configuration/v2/buf-gen-yaml/)).

Everyday commands:

```sh
buf lint                                  # style + naming
buf format -w                             # canonical formatting
buf breaking --against '.git#branch=main' # CI gate: did we break the wire?
buf build -o descriptor.binpb             # FileDescriptorSet for tooling
buf push                                  # publish module to the BSR
```

`buf breaking` categories, weakest to strictest: `FILE` (default; also catches moving a type between files, which breaks some languages' generated code), `PACKAGE`, `WIRE_JSON`, `WIRE`. Pick `FILE` for a Go monorepo where you control all consumers; pick `WIRE` for a schema published to third parties where only wire compatibility is promised.

The **Buf Schema Registry (BSR)** is the piece that changes team dynamics: schemas become versioned modules with dependencies, consumers pull generated SDKs from their native package manager instead of vendoring `.proto` files, and breaking checks run server-side on push ([BSR docs](https://buf.build/docs/bsr/)).

### What the generated Go code looks like

Current `protoc-gen-go-grpc` emits **generics-based stream types by default** ([generated-code reference](https://grpc.io/docs/languages/go/generated-code/)). For a service:

```proto
service CellService {
  rpc GetCell(GetCellRequest) returns (Cell);                          // unary
  rpc WatchCells(WatchCellsRequest) returns (stream CellEvent);        // server stream
  rpc ReportMetrics(stream MetricSample) returns (ReportAck);          // client stream
  rpc Sync(stream SyncRequest) returns (stream SyncResponse);          // bidi
}
```

you get, on the server side:

```go
type CellServiceServer interface {
    GetCell(context.Context, *GetCellRequest) (*Cell, error)
    WatchCells(*WatchCellsRequest, grpc.ServerStreamingServer[*CellEvent]) error
    ReportMetrics(grpc.ClientStreamingServer[*MetricSample, *ReportAck]) error
    Sync(grpc.BidiStreamingServer[*SyncRequest, *SyncResponse]) error
    mustEmbedUnimplementedCellServiceServer()
}
```

and on the client side:

```go
type CellServiceClient interface {
    GetCell(ctx context.Context, in *GetCellRequest, opts ...grpc.CallOption) (*Cell, error)
    WatchCells(ctx context.Context, in *WatchCellsRequest, opts ...grpc.CallOption) (grpc.ServerStreamingClient[*CellEvent], error)
    ReportMetrics(ctx context.Context, opts ...grpc.CallOption) (grpc.ClientStreamingClient[*MetricSample, *ReportAck], error)
    Sync(ctx context.Context, opts ...grpc.CallOption) (grpc.BidiStreamingClient[*SyncRequest, *SyncResponse], error)
}
```

Three things to note. **Embed `UnimplementedCellServiceServer`** in your implementation — the `mustEmbed` method makes it mandatory precisely so that adding an RPC to the proto does not break your build; you get an automatic `UNIMPLEMENTED` until you write the method. **Always use the `Get*()` accessors** on messages rather than field access: they are nil-safe, so `resp.GetCell().GetPhase()` will not panic when `resp.Cell` is nil. And **an individual stream is not safe for concurrent reads or concurrent writes**, though one reader concurrent with one writer is fine — a fact the generated-code reference states explicitly and which people rediscover under load.

### The four RPC kinds and what they cost

| Kind | Client sends | Server sends | Right for | Operational cost |
|---|---|---|---|---|
| Unary | 1 | 1 | 95% of control-plane APIs | Cheapest; retryable; LB-friendly |
| Server-streaming | 1 | N | Watch/tail/subscribe, large paginated reads | Long-lived stream pins a backend |
| Client-streaming | N | 1 | Bulk upload, metric batches | Retry buffer grows with the stream |
| Bidi-streaming | N | N | Interactive protocols, replication, shard ownership handoff | Most expensive; stateful on both ends |

Default to unary. The bias is not stylistic:

- **A stream pins the RPC to one backend for its entire life.** It cannot be re-balanced, and it dies when that pod is drained.
- **A stream is not retryable once headers arrive.** gRPC's built-in retry gives up as soon as the RPC is *committed*, which happens when the client receives response headers or when the outbound message buffer overflows ([gRFC A6](https://github.com/grpc/proposal/blob/master/A6-client-retries.md)).
- **Streams break your deadline story.** A unary call with a 5s deadline is self-limiting. A stream needs application-level liveness (heartbeat messages, or transport keepalive) or it will hang until a TCP timeout minutes later.
- **N streams is N times the server memory.** Each stream in grpc-go carries a flow-control window and a write quota (64 KiB by default, per [`defaults.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/defaults.go)).

Use a stream when you genuinely need server-push or ordered incremental transfer. Long-poll unary — which is what Temporal does — is often the better trade: it keeps the backend pinned only for the poll duration and re-balances on every cycle.

### HTTP/2 underneath

Everything gRPC does at the transport level is HTTP/2 ([RFC 9113](https://www.rfc-editor.org/rfc/rfc9113.html)).

**Streams and multiplexing.** Each RPC is one stream with a unique odd-numbered ID (client-initiated). Many streams share one TCP connection, interleaved at frame granularity. grpc-go's max frame size is 16 KiB (`http2MaxFrameLen = 16384`).

**Stream ID exhaustion.** Stream IDs are 31-bit and monotonically increasing per connection. grpc-go proactively drains a connection and creates a new one when the ID reaches 75% of 2³¹−1 (`MaxStreamID = math.MaxInt32 * 3 / 4`). A very hot, very long-lived connection *will* silently rotate — which is fine unless your LB logs make that look like an incident.

**HPACK.** Headers are compressed with a shared dynamic table ([RFC 7541](https://www.rfc-editor.org/rfc/rfc7541.html)). This is why metadata is cheap for repeated keys and expensive for high-cardinality values: a unique auth token on every request defeats the table. grpc-go initializes the HPACK decoder with a 4096-byte table and defaults the max header *list* size to 16 MiB — with an announced move to 8 KiB, currently opt-in via `GRPC_GO_EXPERIMENTAL_ENABLE_8KB_DEFAULT_HEADER_LIST_SIZE=true` ([`defaults.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/defaults.go), [`http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go)). If you stuff large tokens or trace baggage into metadata, budget for that change.

**Flow control, at two levels.** HTTP/2 has a per-stream window *and* a connection-level window, both defaulting to 65,535 bytes. grpc-go starts both at 65535 and then, by default, runs a **BDP estimator** that grows the windows dynamically based on measured bandwidth-delay product; you disable that by setting a static window with `grpc.WithStaticStreamWindowSize` / `grpc.WithStaticConnWindowSize`, and the lower bound for a configured window is 64 KiB ([`dialoptions.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/dialoptions.go)). Practical consequence: on a high-latency inter-region link, a *static* 64 KiB window caps a single stream at roughly `65536 / RTT` bytes per second — about 650 KB/s at 100 ms RTT — regardless of available bandwidth. Leave the dynamic estimator on unless you are memory-constrained.

Note the subtlety in grpc-go's server: connection-level flow control is credited **when data arrives**, not when the application reads it, deliberately, so one slow stream cannot starve fast ones. Stream-level flow control is still gated on application reads ([`http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go)).

**`SETTINGS_MAX_CONCURRENT_STREAMS`.** This is where grpc-go differs from most HTTP/2 servers and it matters:

- The grpc-go server's default is `math.MaxUint32` — effectively unlimited — and when the value is `MaxUint32` the server **does not send the setting at all** ([`http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go)).
- The grpc-go *client*, when the server advertises nothing, assumes **100** (`defaultMaxStreamsClient = 100`). Beyond that it queues.
- If you *do* set `grpc.MaxConcurrentStreams(n)`, grpc-go both advertises it and enforces a semaphore so that no more than `n` handler goroutines run at once ([PR #6703](https://github.com/grpc/grpc-go/commit/f2180b4d5403d2210b30b93098eb7da31c05c721)). Exceeding it results in `RST_STREAM` with `REFUSED_STREAM`, which clients transparently retry.

So an unconfigured grpc-go server accepts unbounded concurrent streams per connection, while each client self-limits to 100. With a small number of client pods and an L4 LB, you can be simultaneously *overloaded on one backend* and *artificially throttled at 100 in-flight RPCs per client*. Set the value deliberately.

**GOAWAY and graceful drain.** grpc-go's `GracefulStop` path writes a first `GOAWAY` with stream ID 2³¹−1 (meaning "no more new streams, but I have not picked a cutoff yet"), sends a PING, waits for the ack or 5 seconds, then sends a second `GOAWAY` carrying the real last-accepted stream ID. Clients that started a stream in the race window get `REFUSED_STREAM` and retry transparently. This two-phase dance is why a correctly-drained gRPC pod causes zero client-visible errors and an abruptly-killed one causes a spike.

### The biggest infra gotcha: L4 load balancing pins gRPC

Say it plainly: **an L4 (TCP) load balancer in front of gRPC does not balance gRPC.** It balances TCP connections. Because HTTP/2 multiplexes every RPC from a client onto a single long-lived connection, one client sends all of its traffic to exactly one backend, forever — until the connection breaks ([Kubernetes blog: gRPC Load Balancing on Kubernetes without Tears](https://kubernetes.io/blog/2018/11/07/grpc-load-balancing-on-kubernetes-without-tears/)).

The symptoms are distinctive, and recognizing them saves hours:

- CPU is wildly uneven across replicas; a couple of pods sit at 90% while others idle at 5%.
- Scaling up does nothing. New pods receive no traffic because no client reconnects.
- A rolling restart "fixes" it — for a while — because it forces every connection to be re-established.
- Load shifts abruptly when a pod dies, and the pod that inherits it falls over next.

A plain Kubernetes `Service` (`type: ClusterIP`) is an L4 load balancer implemented in iptables/IPVS. It has exactly this problem. So do AWS NLB, GCP passthrough NLB, and Azure Load Balancer.

You have three families of fixes.

*See also: [load balancing (and the gRPC problem)](03-multicloud-aws-gcp-azure.md#load-balancing-and-the-grpc-problem) for how each cloud's L7 answers this — and why Azure has none, so a Temporal-shaped cell there runs its own in-cluster proxy. [Service types and datapath](05-cni-and-host-networking.md#service-types-and-datapath) shows the iptables rules doing the pinning.*

### Load balancing options, compared

| Approach | Where the decision happens | Per-RPC balancing | Needs | Cost / caveats |
|---|---|---|---|---|
| **Client-side + headless Service** | In the client's `ClientConn` | Yes | `clusterIP: None`, `dns:///` target, `round_robin` policy | Every client holds a connection to every backend: N×M connections. DNS TTL/caching delays convergence. |
| **Client-side + xDS (`xds:///`)** | In the client, driven by a control plane | Yes | An xDS server (Istio, Traffic Director, custom) | Proxyless; full policy control; substantial control-plane operational burden |
| **L7 proxy (Envoy)** | In the proxy | Yes | Envoy with `http2_protocol_options` on the cluster | Extra hop, extra latency, extra thing to operate; but centralized policy, retries, mTLS, observability |
| **Service mesh sidecar (Linkerd, Istio)** | In the sidecar | Yes | Mesh installed cluster-wide | Same as above plus per-pod resource overhead; usually the cheapest *organizational* answer |
| **NGINX `grpc_pass`** | In NGINX | Yes | `http2` listener + [`ngx_http_grpc_module`](https://nginx.org/en/docs/http/ngx_http_grpc_module.html) | Works, but weaker gRPC-native features than Envoy |
| **L4 LB (NLB / ClusterIP / Azure LB)** | Connection setup only | **No** | Nothing | Pins traffic; only acceptable when clients are numerous and short-lived, or with forced connection cycling |
| **L4 LB + `MaxConnectionAge`** | Connection setup, repeatedly | Approximately | `keepalive.ServerParameters.MaxConnectionAge` | The pragmatic hack: force periodic reconnection so the LB re-picks. grpc-go adds ±10% jitter automatically. |

Client-side round robin in grpc-go, which is what Temporal does:

```go
import (
    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials"
    _ "google.golang.org/grpc/health" // registers the health-check LB helper
)

conn, err := grpc.NewClient(
    "dns:///temporal-frontend.temporal.svc.cluster.local:7233",
    grpc.WithTransportCredentials(credentials.NewTLS(tlsCfg)),
    grpc.WithDefaultServiceConfig(`{
      "loadBalancingConfig": [{"round_robin":{}}],
      "healthCheckConfig": {"serviceName": ""}
    }`),
)
```

Three details that are easy to get wrong:

1. **The `dns:///` prefix is load-bearing.** `grpc.NewClient` defaults to the `dns` resolver, but `grpc.Dial` (deprecated) defaults to `passthrough`, which hands the target string straight to the dialer and resolves exactly one address — silently defeating `round_robin`. Being explicit costs nothing ([anti-patterns](https://github.com/grpc/grpc-go/blob/v1.83.2/Documentation/anti-patterns.md), [naming](https://github.com/grpc/grpc/blob/master/doc/naming.md)).
2. **The Service must be headless** (`clusterIP: None`) so DNS returns pod IPs rather than the single virtual IP.
3. **The default policy is `pick_first`**, not round robin. `pick_first` connects to one address and stays there — correct for a proxy target, wrong for a pod list.

`grpc.NewClient` versus `grpc.Dial`: use `NewClient`. It was introduced in grpc-go v1.63 and performs no I/O; `Dial` is deprecated, connects eagerly, defaults to the `passthrough` resolver, and enables the `WithBlock`/`WithTimeout`/`WithReturnConnectionError` options that encourage the anti-pattern of "verify connectivity at startup". Connectivity at startup tells you nothing about connectivity one second later; handle errors from RPCs instead.

For Envoy, the one-line version: gRPC is just HTTP/2, so put `http2_protocol_options` on the cluster (and the listener), and Envoy will load-balance per *request* rather than per connection ([Envoy gRPC overview](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/other_protocols/grpc)). Envoy also understands `grpc-status` for outlier detection and retry-on-`UNAVAILABLE`, which a pure L4 hop cannot.

### Deadlines, context propagation, and cancellation

**Every gRPC call must have a deadline.** Not "should". The default is no deadline, and a call with no deadline against a hung backend leaks a goroutine, a stream, and a flow-control window until the transport dies — which, with keepalives off, can be TCP-timeout long ([deadlines guide](https://grpc.io/docs/guides/deadlines/)).

The mechanics: `context.WithTimeout` on the client becomes the `grpc-timeout` header on the wire; the server decodes it and derives the handler's `ctx` from it, so the handler's `ctx.Done()` fires when the client's deadline expires. In grpc-go's server transport, a timer is armed on stream creation that closes the stream at the deadline.

```go
func (s *cellService) Provision(ctx context.Context, req *pb.ProvisionRequest) (*pb.ProvisionResponse, error) {
    // Deadline propagates automatically. Do NOT use context.Background() here:
    // it detaches the downstream call from the caller's deadline and cancellation.
    sub, cancel := context.WithTimeout(ctx, 3*time.Second)
    defer cancel()

    net, err := s.networkClient.AllocateSubnet(sub, &netpb.AllocateSubnetRequest{Cell: req.CellId})
    if err != nil {
        // Translate: an INVALID_ARGUMENT from a dependency is OUR bug, not the caller's.
        if status.Code(err) == codes.InvalidArgument {
            return nil, status.Errorf(codes.Internal, "network allocation rejected: %v", err)
        }
        return nil, err
    }
    ...
}
```

Cancellation propagates in both directions and shows up as distinct codes: a client that cancels gets `codes.Canceled`; a deadline that expires gets `codes.DeadlineExceeded`. On the server, `ctx.Err()` distinguishes them. Cancellation is *advisory* — it closes the stream, but a handler that ignores `ctx` keeps burning CPU. Check `ctx.Err()` in any loop that runs longer than a few milliseconds.

Two propagation rules worth writing on a wall:

- **A deadline is a budget, not a per-hop timeout.** If the edge grants 5s and each of four hops sets its own fresh 5s, the edge times out while three services keep working. Always derive from the incoming `ctx`.
- **The deadline spans all retry attempts.** `maxAttempts: 4` with a 1s deadline gets you however many attempts fit in 1s, not four.

### Status codes and the error model

gRPC statuses are a fixed enum, not HTTP codes ([status codes guide](https://grpc.io/docs/guides/status-codes/)). The ones that matter operationally:

| Code | Meaning | Safe to retry? |
|---|---|---|
| `UNAVAILABLE` (14) | Transport failure, backend down, connection lost | **Yes** — the canonical retryable code |
| `DEADLINE_EXCEEDED` (4) | Budget consumed | Only with a fresh budget; usually no |
| `RESOURCE_EXHAUSTED` (8) | Quota, rate limit, or message-too-large | Yes, with backoff — but *not* for message-size errors |
| `ABORTED` (10) | Concurrency conflict (optimistic-lock failure) | Yes, at a higher level |
| `INTERNAL` (13) | Server bug or protocol violation | No |
| `UNIMPLEMENTED` (12) | Method does not exist on this server | No — usually a version skew or routing bug |
| `UNAUTHENTICATED` (16) / `PERMISSION_DENIED` (7) | Missing/invalid creds vs. valid creds without authority | No |
| `FAILED_PRECONDITION` (9) | State makes this impossible now | No (client must fix state) |
| `INVALID_ARGUMENT` (3) | Malformed request | No |
| `NOT_FOUND` (5) / `ALREADY_EXISTS` (6) | Self-explanatory | No |
| `CANCELED` (1) | Caller went away | No |

Note that grpc-go maps transport-level failures into these: an HTTP/2 `REFUSED_STREAM` becomes `UNAVAILABLE`, `ENHANCE_YOUR_CALM` becomes `RESOURCE_EXHAUSTED`, `FLOW_CONTROL_ERROR` becomes `RESOURCE_EXHAUSTED`, and HTTP `502/503/504/429` map to `UNAVAILABLE`.

Producing and consuming errors:

```go
import (
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    epb "google.golang.org/genproto/googleapis/rpc/errdetails"
)

// Simple.
return nil, status.Errorf(codes.NotFound, "cell %q not found", req.GetCellId())

// Rich: attach typed details via google.rpc.Status.details (an Any list).
st := status.New(codes.ResourceExhausted, "regional cell quota exhausted")
st, err := st.WithDetails(
    &epb.QuotaFailure{Violations: []*epb.QuotaFailure_Violation{{
        Subject:     "region/us-west-2",
        Description: "cell quota 40/40 in use",
    }}},
    &epb.RetryInfo{RetryDelay: durationpb.New(30 * time.Second)},
)
if err != nil { return nil, status.Error(codes.Internal, "failed to build status") }
return nil, st.Err()
```

Rich details ride in the `grpc-status-details-bin` trailer as a serialized `google.rpc.Status`. Because they land in a *header list*, they are subject to the max-header-list-size limit — do not put a stack trace in there, especially with the 8 KiB default arriving.

On the client:

```go
if st, ok := status.FromError(err); ok {
    for _, d := range st.Details() {
        switch info := d.(type) {
        case *epb.QuotaFailure:  metrics.QuotaHit.Inc()
        case *epb.RetryInfo:     time.Sleep(info.GetRetryDelay().AsDuration())
        }
    }
}
```

**Translate codes at service boundaries.** If a dependency returns `INVALID_ARGUMENT`, your caller did not send an invalid argument — *you* did. Returning it verbatim makes your service look like it is rejecting valid requests and, worse, makes it non-retryable when it should be `INTERNAL` or `UNAVAILABLE` ([error handling guide](https://grpc.io/docs/guides/error/)).

### Retries, hedging, and service config

gRPC has **built-in, declarative retries** configured through service config, so you rarely need a retry interceptor ([gRFC A6](https://github.com/grpc/proposal/blob/master/A6-client-retries.md), [retry guide](https://grpc.io/docs/guides/retry/)).

```go
const svcCfg = `{
  "loadBalancingConfig": [{"round_robin":{}}],
  "methodConfig": [{
    "name": [{"service": "cellctl.v1.CellService", "method": "GetCell"}],
    "timeout": "5s",
    "waitForReady": true,
    "retryPolicy": {
      "maxAttempts": 4,
      "initialBackoff": "0.1s",
      "maxBackoff": "1s",
      "backoffMultiplier": 2,
      "retryableStatusCodes": ["UNAVAILABLE", "RESOURCE_EXHAUSTED"]
    }
  }],
  "retryThrottling": { "maxTokens": 10, "tokenRatio": 0.1 }
}`

conn, err := grpc.NewClient(target, grpc.WithDefaultServiceConfig(svcCfg), creds)
```

Semantics you must know before enabling it:

- **`maxAttempts` is clamped to 5 by the client** (`defaultMaxCallAttempts = 5` in [`dialoptions.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/dialoptions.go)); raise it with `grpc.WithMaxCallAttempts(n)` if you truly must.
- **Backoff has ±20% jitter**: attempt *n* fires at `min(initialBackoff × multiplier^(n-1), maxBackoff) × random(0.8, 1.2)`.
- **`retryThrottling` is a token bucket per server name**, shared across all methods. Every failure costs 1 token, every success returns `tokenRatio`; when the count falls to `maxTokens/2`, retries stop entirely. This is the circuit breaker that stops a retry storm from finishing off a degraded backend. Configure it. The default is none.
- **Servers can push back** with the `grpc-retry-pushback-ms` metadata key: a non-negative value means "retry after exactly this many ms", a negative or unparseable value means "do not retry at all".
- **Servers can see the attempt number** via the `grpc-previous-rpc-attempts` header — absent on the first try, `1` on the second, and so on.
- **Transparent retries happen regardless of your policy**, when gRPC knows the server never saw the request: a `RST_STREAM` with `REFUSED_STREAM`, or a `GOAWAY` whose last-stream-id precedes this stream. These do not count against `maxAttempts` or the throttle. This is exactly what makes a well-drained rolling upgrade invisible.
- **An RPC becomes *committed* and stops being retryable** once response headers arrive or the client's outbound buffer overflows (`maxRetryRPCBufferSize`, default 256 KiB per RPC in [`rpc_util.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/rpc_util.go)).

**Hedging** is the aggressive cousin: send attempt 2 after `hedgingDelay` without waiting for attempt 1 to fail, take the first success, cancel the rest. Note that `hedgingDelay: "0s"` fires all attempts at once.

```json
"hedgingPolicy": {
  "maxAttempts": 3,
  "hedgingDelay": "0.5s",
  "nonFatalStatusCodes": ["UNAVAILABLE"]
}
```

Only hedge **idempotent, cheap, latency-sensitive reads**. Hedging a workflow-start RPC creates duplicate workflows. A method may have a retry policy or a hedging policy, never both. Note that grpc-go implements retries but **not hedging** as of A6's status line.

Finally, `waitForReady`: by default an RPC fails fast with `UNAVAILABLE` if the channel is in `TRANSIENT_FAILURE`. Setting `waitForReady: true` makes it queue until the channel recovers or the deadline expires. That is the right default for internal control-plane calls with sane deadlines, and the wrong default for anything user-facing.

### Keepalives, `ENHANCE_YOUR_CALM`, and idle timeouts

gRPC keepalive is an HTTP/2 PING sent when the connection is quiet ([keepalive guide](https://grpc.io/docs/guides/keepalive/), [gRFC A8](https://github.com/grpc/proposal/blob/master/A8-client-side-keepalive.md), [gRFC A9](https://github.com/grpc/proposal/blob/master/A9-server-side-conn-mgt.md)). The grpc-go defaults, straight from [`keepalive.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/keepalive/keepalive.go) and [`defaults.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/defaults.go):

| Parameter | Side | grpc-go default |
|---|---|---|
| `ClientParameters.Time` | Client | **Infinity — keepalive is OFF by default** |
| `ClientParameters.Timeout` | Client | 20s |
| `ClientParameters.PermitWithoutStream` | Client | `false` |
| Minimum client ping interval | Client | 10s (values below are raised to 10s) |
| `ServerParameters.Time` | Server | 2 hours |
| `ServerParameters.Timeout` | Server | 20s |
| `ServerParameters.MaxConnectionIdle` | Server | Infinity |
| `ServerParameters.MaxConnectionAge` | Server | Infinity (±10% jitter when set) |
| `ServerParameters.MaxConnectionAgeGrace` | Server | Infinity |
| `EnforcementPolicy.MinTime` | Server | **5 minutes** |
| `EnforcementPolicy.PermitWithoutStream` | Server | `false` |

The two numbers to stare at are the client's *off by default* and the server's *5-minute minimum*. They are a trap in both directions.

**The `too_many_pings` failure.** grpc-go's server counts "ping strikes". A ping arriving sooner than `EnforcementPolicy.MinTime` is a strike; if there are zero active streams and `PermitWithoutStream` is false, a ping arriving sooner than a hard-coded 2 hours is a strike. Any DATA or HEADERS frame the server writes resets the strike counter to zero. After **more than 2 strikes** (`maxPingStrikes = 2`), the server sends `GOAWAY` with error code `ENHANCE_YOUR_CALM` and debug data `too_many_pings`, and closes the connection ([`http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go)).

The pathological version of this is a **GOAWAY storm**: a fleet of clients configured with `Time: 10s, PermitWithoutStream: true` against a server left at `MinTime: 5m`. Every client gets killed after a few pings, reconnects, gets killed again. Connection churn spikes, TLS handshake CPU spikes, and the LB sees a thundering herd. grpc-go mitigates by doubling the client's `Time` when it is disconnected for policy reasons, but you should just configure both sides together. **Client keepalive settings are a contract with the service owner, not a local decision.**

Concrete, matched settings for a cell-internal service:

```go
// Server
grpc.NewServer(
    grpc.KeepaliveParams(keepalive.ServerParameters{
        Time:                  30 * time.Second, // ping an idle conn every 30s
        Timeout:               10 * time.Second, // ...and close if unacked in 10s
        MaxConnectionAge:      30 * time.Minute, // force rebalance (jittered ±10%)
        MaxConnectionAgeGrace: 30 * time.Second, // let in-flight RPCs finish
    }),
    grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
        MinTime:             15 * time.Second, // MUST be <= client Time
        PermitWithoutStream: true,             // MUST be true if clients set it
    }),
)

// Client
grpc.NewClient(target,
    grpc.WithKeepaliveParams(keepalive.ClientParameters{
        Time:                30 * time.Second,
        Timeout:             10 * time.Second,
        PermitWithoutStream: true,
    }),
    creds,
)
```

**Cloud LB idle timeouts are the reason keepalive exists.** A middlebox that silently drops an idle flow leaves both endpoints believing the connection is fine; the failure surfaces minutes later as a hung RPC. The numbers you are up against on Temporal Cloud's three clouds:

| Layer | Default idle timeout | Configurable? |
|---|---|---|
| AWS NLB (TCP) | **350 seconds** | Yes since Sept 2024: `tcp.idle_timeout.seconds`, 60–6000s ([docs](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html)) |
| Azure Load Balancer | **4 minutes** | Yes, 4–100 min for LB rules; also enable TCP Reset so the drop is visible ([docs](https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-tcp-reset)) |
| GCP internal passthrough NLB | **600 seconds** | Only in limited tracking modes; max 57,600s ([docs](https://cloud.google.com/load-balancing/docs/internal/int-netlb-traffic-distribution)) |
| GCP external passthrough NLB | 60 seconds after last packet | No |
| NAT gateways generally | Varies, often 300s | Varies |

Rule of thumb: **client keepalive `Time` must be comfortably below the smallest idle timeout on the path**, and if you cannot guarantee an active stream, `PermitWithoutStream: true` is mandatory. Note also that gRPC sets `TCP_USER_TIMEOUT` on Linux to the keepalive `Timeout` when keepalive is enabled — but `TCP_USER_TIMEOUT` only observes the hop to the L4 LB, whereas PINGs traverse it end to end. That is exactly why PING-based keepalive exists on top of TCP keepalive ([gRFC A18](https://github.com/grpc/proposal/blob/master/A18-tcp-user-timeout.md)).

Separately, grpc-go channels have a **client-side idle timeout of 30 minutes** by default: with no RPCs for that long, the channel enters idle mode and shuts down its resolver, balancer, and connections, reconnecting on the next RPC ([`dialoptions.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/dialoptions.go)). Harmless, unless you are debugging "why did the first request after lunch take 300 ms".

### Interceptors

Interceptors are gRPC's middleware. Four kinds: {unary, stream} × {client, server}. Chain them with `grpc.ChainUnaryInterceptor` / `grpc.ChainStreamInterceptor` — the first in the list is outermost ([interceptors guide](https://grpc.io/docs/guides/interceptors/)).

```go
func authInterceptor(ctx context.Context, req any,
    info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {

    md, ok := metadata.FromIncomingContext(ctx)
    if !ok {
        return nil, status.Error(codes.Unauthenticated, "missing metadata")
    }
    tok := md.Get("authorization")
    if len(tok) == 0 {
        return nil, status.Error(codes.Unauthenticated, "missing authorization")
    }
    claims, err := verify(tok[0])
    if err != nil {
        return nil, status.Error(codes.Unauthenticated, "invalid token")
    }
    return handler(context.WithValue(ctx, claimsKey{}, claims), req)
}

srv := grpc.NewServer(
    grpc.ChainUnaryInterceptor(
        recovery.UnaryServerInterceptor(),  // OUTERMOST: catches panics from everything inside
        otelgrpc-or-equivalent,
        authInterceptor,
        rateLimitInterceptor,
    ),
    grpc.ChainStreamInterceptor( /* the stream equivalents */ ),
)
```

Two rules people get wrong. **Panic recovery must be outermost**, or a panic in another interceptor skips it and kills the process. And **stream interceptors need their own implementation** — a unary interceptor does not cover streaming methods, so an auth interceptor registered only as unary leaves every streaming RPC unauthenticated. That is a real class of vulnerability.

To modify a stream's context or wrap `RecvMsg`/`SendMsg`, wrap `grpc.ServerStream`:

```go
type wrappedStream struct {
    grpc.ServerStream
    ctx context.Context
}
func (w *wrappedStream) Context() context.Context { return w.ctx }
```

Do not write these from scratch. [`grpc-ecosystem/go-grpc-middleware/v2`](https://github.com/grpc-ecosystem/go-grpc-middleware) provides tested `auth`, `logging`, `recovery`, `retry`, `ratelimit`, `validator`, and `protovalidate` interceptors. Temporal's own Go SDK builds its client interceptor chain from it — it imports `go-grpc-middleware/v2/interceptors/retry` directly ([`grpc_dialer.go`](https://github.com/temporalio/sdk-go/blob/master/internal/grpc_dialer.go)).

For metrics and tracing specifically, prefer `stats.Handler` over interceptors: it sees transport-level events (bytes on the wire, per-message timing) that interceptors cannot, and it is what grpc-go's own OpenTelemetry integration uses.

### Security: TLS, mTLS, ALTS, and per-RPC credentials

gRPC separates **channel credentials** (who the peer is, at the connection level) from **call credentials** (who the caller is, per RPC) ([auth guide](https://grpc.io/docs/guides/auth/)).

```go
// Server: mutual TLS with a cert-manager-issued keypair and a cluster CA.
cert, _ := tls.LoadX509KeyPair("/etc/tls/tls.crt", "/etc/tls/tls.key")
caPEM, _ := os.ReadFile("/etc/tls/ca.crt")
pool := x509.NewCertPool()
pool.AppendCertsFromPEM(caPEM)

creds := credentials.NewTLS(&tls.Config{
    Certificates: []tls.Certificate{cert},
    ClientAuth:   tls.RequireAndVerifyClientCert, // this is what makes it *mutual*
    ClientCAs:    pool,
    MinVersion:   tls.VersionTLS13,
})
srv := grpc.NewServer(grpc.Creds(creds))
```

```go
// Client: mTLS channel creds + a per-RPC token.
conn, err := grpc.NewClient(target,
    grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)),
    grpc.WithPerRPCCredentials(tokenSource{}), // adds "authorization" metadata per call
)
```

Points that matter operationally:

- **`credentials.PerRPCCredentials` refuses to send over an insecure channel** if it declares `RequireTransportSecurity() == true`. That is the guardrail preventing a bearer token from leaking in plaintext. It also means "it works locally with `insecure.NewCredentials()`" tells you nothing.
- **`insecure.NewCredentials()` is explicit for a reason.** There is no implicit plaintext mode; `grpc.WithInsecure()` is deprecated in favor of the explicit form.
- **Certificate rotation.** `credentials.NewTLS` captures a `*tls.Config` once. To pick up rotated certs without a restart, use `tls.Config.GetCertificate` (server) / `GetClientCertificate` (client) with a callback that reads the current keypair from disk. With [cert-manager](https://cert-manager.io/docs/) writing into a mounted Secret, the file changes underneath you and a static `Certificates` slice will keep serving the expired one until the pod restarts. This is a classic "cell was fine for 89 days" incident.
- **SPIFFE / SAN validation.** `RequireAndVerifyClientCert` proves the peer holds a cert from your CA — it does not prove *which* service it is. Add a `VerifyPeerCertificate` callback (or an interceptor reading `peer.FromContext` → `credentials.TLSInfo`) that checks the SAN against an allowlist, or every workload in the cell can impersonate every other.
- **ALTS** is Google's mutual-authentication transport for workloads running on Google infrastructure; it uses GCE/GKE service-account identity instead of certificates you manage, and grpc-go supports it directly via `alts.NewServerCreds()` / `alts.NewClientCreds()` ([Go ALTS](https://grpc.io/docs/languages/go/alts/)). It only works inside Google's environment, so for a multi-cloud footprint it is at best a GCP-only optimization — one more reason a cross-cloud control plane standardizes on mTLS with a shared issuer.

*See also: [mTLS between services: cert-manager vs SPIFFE/SPIRE](11-cert-manager-and-pki.md#mtls-between-services-cert-manager-vs-spiffespire) for where that shared issuer comes from, and [the reload problem](11-cert-manager-and-pki.md#the-reload-problem) for the rotation failure the `GetCertificate` callback above exists to avoid.*

### Observability: OpenTelemetry, channelz, health checking

**OpenTelemetry.** grpc-go ships first-party OTel instrumentation in [`google.golang.org/grpc/stats/opentelemetry`](https://pkg.go.dev/google.golang.org/grpc/stats/opentelemetry), producing the standardized per-attempt and per-call metrics from the gRPC metrics spec ([OTel metrics guide](https://grpc.io/docs/guides/opentelemetry-metrics/)):

```go
import "google.golang.org/grpc/stats/opentelemetry"

mo := opentelemetry.MetricsOptions{MeterProvider: meterProvider}
srv := grpc.NewServer(opentelemetry.ServerOption(opentelemetry.Options{MetricsOptions: mo}))
cc, _ := grpc.NewClient(target, opentelemetry.DialOption(opentelemetry.Options{MetricsOptions: mo}), creds)
```

The metric split that matters when debugging LB problems: **per-attempt** metrics count every retry and every hedge separately, while **per-call** metrics count the logical RPC once. A retry storm shows up as attempt count diverging from call count — often the first hard signal that a backend is degraded.

**channelz** is gRPC's built-in introspection service: top-level channels, subchannels, servers, and sockets, with per-socket stream counts, message counts, keepalives sent, and **the current flow-control windows in both directions** ([gRFC A14](https://github.com/grpc/proposal/blob/master/A14-channelz.md)). Register it on a debug port and you can answer "which backend is this client actually pinned to" and "is this stream stalled on flow control" without a packet capture:

```go
import channelzsvc "google.golang.org/grpc/channelz/service"
channelzsvc.RegisterChannelzServiceToServer(debugServer)
```

Then `grpcurl -plaintext localhost:9999 grpc.channelz.v1.Channelz/GetTopChannels`.

**Health checking** is a standard service, not a convention ([health checking protocol](https://github.com/grpc/grpc/blob/master/doc/health-checking.md)):

```proto
service Health {
  rpc Check(HealthCheckRequest) returns (HealthCheckResponse);
  rpc Watch(HealthCheckRequest) returns (stream HealthCheckResponse);
}
```

The empty string `""` is the key for overall server health; per-service keys let you mark one service `NOT_SERVING` while the process stays up. In Go, `google.golang.org/grpc/health` gives you a server implementation and `healthpb.RegisterHealthServer` wires it up; flip states with `healthServer.SetServingStatus(name, status)`.

Kubernetes speaks this natively — the `grpc` probe field went beta in 1.24 and **GA in 1.27**, so no sidecar binary is needed ([probes docs](https://kubernetes.io/docs/concepts/workloads/pods/probes/)):

```yaml
readinessProbe:
  grpc:
    port: 7233
    service: ""          # overall health
  initialDelaySeconds: 5
  periodSeconds: 10
```

On older clusters, or for shell-based checks, [`grpc_health_probe`](https://github.com/grpc-ecosystem/grpc-health-probe) is the standard binary.

The subtlety: **the same health signal is being consumed by two very different controllers.** Kubernetes uses it to decide pod readiness (remove from Service endpoints). gRPC clients can *also* consume it, per-subchannel, via `"healthCheckConfig": {"serviceName": ""}` in the service config, so a `round_robin` balancer stops sending to an unhealthy backend without waiting for endpoint propagation. Turn the second one on for latency-sensitive internal traffic — endpoint propagation through kube-proxy is seconds, and a `Watch`-driven subchannel update is milliseconds.

### Message size limits, compression, and backpressure

**Defaults, verified in [`rpc_util.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/rpc_util.go):** max *receive* message size is **4 MB** (both client and server); max *send* message size is `math.MaxInt32`, i.e. effectively unlimited. That asymmetry is the whole story behind the most common gRPC error message in existence:

```text
rpc error: code = ResourceExhausted desc = grpc: received message larger than max (5242880 vs. 4194304)
```

The sender happily sent 5 MB; the receiver refused it. Raise the limit on **both** sides, and raise it deliberately — a 4 MB message costs at least 4 MB of heap per concurrent RPC, times your concurrency, and there is no partial-parse escape hatch.

```go
srv := grpc.NewServer(
    grpc.MaxRecvMsgSize(16*1024*1024),
    grpc.MaxSendMsgSize(16*1024*1024),
)
cc, _ := grpc.NewClient(target,
    grpc.WithDefaultCallOptions(
        grpc.MaxCallRecvMsgSize(16*1024*1024),
        grpc.MaxCallSendMsgSize(16*1024*1024),
    ), creds)
```

Temporal is a live example of both halves. The server treats **4 MB as its maximum inbound frontend gRPC request** (`MaxHTTPAPIRequestBytes = 4 * 1024 * 1024`, commented "currently set to the max gRPC request size"), while raising the *internode* receive limit to **128 MB** (`maxInternodeRecvPayloadSize`) and the Go SDK's client limit to **128 MB** (`defaultMaxPayloadSize`) ([`common/rpc/grpc.go`](https://github.com/temporalio/temporal/blob/main/common/rpc/grpc.go), [`grpc_dialer.go`](https://github.com/temporalio/sdk-go/blob/master/internal/grpc_dialer.go)). External traffic is tightly bounded; internal traffic — replication, history transfer — is not.

**Compression** is per-message and negotiated via the `grpc-encoding` and `grpc-accept-encoding` headers. In Go, `import _ "google.golang.org/grpc/encoding/gzip"` registers the codec; `grpc.UseCompressor(gzip.Name)` as a call or dial option turns it on for requests. Temporal's Go SDK enables gzip by default and includes a downgrade interceptor that retries with identity encoding if the server answers `UNIMPLEMENTED` mentioning compression — a nice pattern for talking to heterogeneous server versions. Note grpc-go bounds decompression output at `maxReceiveMessageSize + 1` for the built-in gzip decompressor specifically so a zip bomb cannot expand to gigabytes before the size check fires.

**Backpressure on streams** comes from HTTP/2 flow control, and it is real but invisible: `stream.Send()` blocks when the peer's window is exhausted. Each grpc-go stream also has a 64 KiB write quota (`defaultWriteQuota`) governing how much it can schedule before flushing. If your producer never blocks and your memory grows anyway, you have a buffer in *your* code, not in gRPC. Never wrap `Send` in an unbounded goroutine-per-message pattern; that converts transport backpressure into an OOM ([flow control guide](https://grpc.io/docs/guides/flow-control/)).

### Reflection and the CLI toolbox

Server reflection lets a client discover services and message schemas at runtime, which is what makes ad-hoc CLI calls possible without a `.proto` file in hand ([reflection guide](https://grpc.io/docs/guides/reflection/)):

```go
import "google.golang.org/grpc/reflection"
reflection.Register(srv)
```

Enable it in dev and staging. In production, treat it as a debug-surface decision — it exposes your entire API shape, so most shops expose it only on an internal port or behind authz.

```sh
# Discover
grpcurl -plaintext localhost:8080 list
grpcurl -plaintext localhost:8080 describe cellctl.v1.CellService

# Call, with reflection
grpcurl -plaintext -d '{"cell_id":"cell-usw2-014"}' \
  localhost:8080 cellctl.v1.CellService/GetCell

# Call, without reflection, from a descriptor set
buf build -o image.binpb
grpcurl -protoset image.binpb -plaintext -d '{"cell_id":"x"}' \
  localhost:8080 cellctl.v1.CellService/GetCell

# buf's native equivalent, resolves the schema from your workspace or the BSR
buf curl --schema . --protocol grpc --http2-prior-knowledge \
  -d '{"cell_id":"x"}' http://localhost:8080/cellctl.v1.CellService/GetCell

# mTLS
grpcurl -cacert ca.crt -cert client.crt -key client.key \
  temporal-frontend.internal:7233 list

# Health
grpc_health_probe -addr=localhost:7233 -tls -tls-ca-cert=ca.crt
grpcurl -plaintext localhost:7233 grpc.health.v1.Health/Check
```

`--http2-prior-knowledge` on `buf curl` matters for plaintext gRPC: without TLS-ALPN there is no way to negotiate HTTP/2, so the client must assume it ([`buf curl` usage](https://buf.build/docs/curl/usage/)).

### gRPC-Web, gRPC-Gateway, and Connect

Browsers cannot speak gRPC. The XHR/fetch APIs give no control over HTTP/2 framing and no access to trailers, which is precisely where `grpc-status` lives. Three answers:

| Option | What it is | Use when |
|---|---|---|
| [**gRPC-Web**](https://github.com/grpc/grpc-web) | A wire variant that moves trailers into the body; requires a translating proxy (Envoy's `grpc_web` filter, or a Go handler) | You have a browser client and want to keep protobuf end to end |
| [**gRPC-Gateway**](https://github.com/grpc-ecosystem/grpc-gateway) | A codegen'd reverse proxy that exposes RESTful JSON endpoints mapped from `google.api.http` annotations | You must offer a conventional REST/JSON API alongside gRPC, e.g. for `curl` users, webhooks, or partners |
| [**Connect**](https://connectrpc.com/docs/introduction) | A protocol + Go/TS libraries that serve gRPC, gRPC-Web, and its own simpler HTTP/1.1-friendly protocol from one handler | Greenfield services that need browser and CLI access without a proxy; `curl`-able by design |

For a Temporal-shaped system all three are peripheral, with one exception worth knowing: Temporal's server exposes an **HTTP API on the frontend** alongside gRPC (`RPC.HTTPPort` in [`common/config/config.go`](https://github.com/temporalio/temporal/blob/main/common/config/config.go)), with its own request-size cap and a configurable set of headers forwarded from HTTP into gRPC metadata. If you are terminating traffic in the cell's traffic layer, that is a second protocol on the same service that also needs routing rules.

### Temporal-specific: what to know before your first on-call

**Where the schema lives.** [`temporalio/api`](https://github.com/temporalio/api) holds the entire public contract, organized as `temporal/api/<domain>/v1/*.proto`. The service surface is [`temporal/api/workflowservice/v1/service.proto`](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto) (`WorkflowService`), with `OperatorService` and an internal `AdminService` alongside. Generated Go lands in `go.temporal.io/api`. The repo is Buf-managed; schema changes go through `buf breaking`, which is why the wire contract has stayed stable across years of releases.

**How an SDK talks to the frontend.** Read [`internal/grpc_dialer.go`](https://github.com/temporalio/sdk-go/blob/master/internal/grpc_dialer.go) once; it is 200 lines and it encodes most of this guide's advice:

- `grpc.NewClient` (not `Dial`), with `{"loadBalancingConfig": [{"round_robin":{}}]}` as the default service config — client-side load balancing over DNS-resolved frontend addresses.
- Keepalive `Time: 30s`, `Timeout: 15s`, and `PermitWithoutStream` **true** by default.
- `MaxCallSendMsgSize` and `MaxCallRecvMsgSize` both set to **128 MB**.
- gzip compression on by default, with a downgrade interceptor for older servers.
- `ConnectParams` overriding the default 120s max backoff down to the poll retry interval, with `MinConnectTimeout: 20s`.
- An interceptor chain doing error translation (`serviceerror.FromStatus`), metrics at both the call and the attempt layer, optional retries, credentials, and a `temporal-namespace` header injected from the request.

That last one is worth flagging for a traffic layer: **the namespace is available both as a request field and as the `temporal-namespace` metadata header**, which is exactly what an L7 router needs to shard traffic per namespace without parsing the protobuf body.

**Long polls are why LB tuning is special.** `PollWorkflowTaskQueue` and `PollActivityTaskQueue` are unary RPCs that intentionally block server-side waiting for work, returning an empty response if nothing arrives — the matching service's `longPollExpirationInterval` defaults to **1 minute** ([dynamic configuration reference](https://docs.temporal.io/references/dynamic-configuration)). A worker fleet therefore holds a large, steady population of RPCs that each live ~60 seconds and produce zero bytes in the meantime.

Everything downstream of that fact:

- **Any proxy or LB idle/request timeout below ~60 seconds will kill polls.** Envoy's `route.timeout`, an ALB idle timeout, an ingress controller's `proxy_read_timeout` — all of them must exceed the poll expiration with margin.
- **A poll produces no traffic while it waits**, so connection-level idle timeouts can still fire even though an RPC is technically in flight — the connection carries no frames. This is exactly the case `PermitWithoutStream` plus a sub-idle-timeout keepalive interval covers.
- **Restarting matching kills every long poll at once**, and every worker immediately re-polls. Expect a synchronized thundering herd on any matching-service restart, and design cell upgrades to roll matching pods gradually.
- **Poll latency is not a useful SLI.** A 60-second p99 on `PollWorkflowTaskQueue` is healthy. If your dashboards or LB health checks treat long polls like ordinary RPCs, you will page yourself for normal behavior. Exclude poll methods explicitly.
- **The frontend is a fan-out point.** Client-side round-robin from the SDK spreads polls across frontends; each frontend then fans out to matching. A single misconfigured client with `pick_first` can hot-spot one frontend with thousands of long polls.

**Server-side keepalive knobs are exposed in config**, and their defaults deliberately mirror grpc-go's: `keepAliveServerParameters` defaults to `Time: 2h, Timeout: 20s`, infinite max-age/idle, and `keepAliveEnforcementPolicy` defaults to `MinTime: 5m, PermitWithoutStream: false` ([`common/config/config.go`](https://github.com/temporalio/temporal/blob/main/common/config/config.go)). Compare that to the SDK's `Time: 30s, PermitWithoutStream: true` and the mismatch is visible on paper: an untuned server's enforcement policy is stricter than the SDK's ping rate. In practice, servers under load constantly write frames — which resets the ping-strike counter — so it usually does not fire; but a quiet namespace on a quiet cell is exactly where it can. **Verify `keepAliveEnforcementPolicy.minTime` in your cell's rendered config, not in your memory of the defaults.**

*See also: [Matching: partitions, forwarding, and the two kinds of match](16-temporal-server-internals.md#matching-partitions-forwarding-and-the-two-kinds-of-match) for what is on the other end of those long polls, and [workers, task queues, and long polling](15-temporal-programming-model.md#workers-task-queues-and-long-polling) for the customer-side tuning that decides how many of them a cell has to hold.*

---

## Hands-on

A single lab, ~90 minutes, that ends with you watching gRPC pin itself to one backend and then fixing it. Requires Go 1.24+, [buf](https://buf.build/docs/cli/installation/), [grpcurl](https://github.com/fullstorydev/grpcurl), Docker, and `kind`.

### Part 1 — Schema and codegen

```sh
mkdir grpc-lab && cd grpc-lab
go mod init example.com/grpclab
buf config init
mkdir -p proto/cellctl/v1
```

`buf.yaml` — add the module path:

```yaml
version: v2
modules:
  - path: proto
lint:
  use:
    - STANDARD
  except:
    # STANDARD requires every RPC to return <Method>Response. GetCell returns
    # Cell and WatchCells streams CellEvent, so without this exception the
    # `buf lint` below fails on its first run. Try it with the except removed
    # first — reading the rule name it prints is the useful part.
    - RPC_RESPONSE_STANDARD_NAME
breaking:
  use:
    - FILE
```

`proto/cellctl/v1/cellctl.proto`:

```proto
syntax = "proto3";

package cellctl.v1;

enum CellPhase {
  CELL_PHASE_UNSPECIFIED = 0;
  CELL_PHASE_PROVISIONING = 1;
  CELL_PHASE_READY = 2;
}

message Cell {
  string cell_id = 1;
  string region = 2;
  CellPhase phase = 3;
}

message GetCellRequest { string cell_id = 1; }
message WhoAmIRequest {}
message WhoAmIResponse { string pod = 1; int64 request_count = 2; }
message WatchCellsRequest {}
message CellEvent { Cell cell = 1; }

service CellService {
  rpc GetCell(GetCellRequest) returns (Cell);
  rpc WhoAmI(WhoAmIRequest) returns (WhoAmIResponse);
  rpc WatchCells(WatchCellsRequest) returns (stream CellEvent);
}
```

`buf.gen.yaml`:

```yaml
version: v2
clean: true
managed:
  enabled: true
  override:
    - file_option: go_package_prefix
      value: example.com/grpclab/gen/go
plugins:
  - remote: buf.build/protocolbuffers/go:v1.36.11
    out: gen/go
    opt: paths=source_relative
  - remote: buf.build/grpc/go:v1.6.2
    out: gen/go
    opt: paths=source_relative
inputs:
  - directory: proto
```

```sh
buf lint
buf format -w
buf generate
find gen -name '*.go'    # expect cellctl.pb.go and cellctl_grpc.pb.go
```

Read both generated files. Find `mustEmbedUnimplementedCellServiceServer`, find the `grpc.ServerStreamingServer[*CellEvent]` type, and find the `file_..._rawDesc` byte blob — that is the embedded FileDescriptor that powers reflection.

### Part 2 — Server and client

`cmd/server/main.go`:

```go
package main

import (
	"context"
	"log"
	"net"
	"os"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/reflection"
	"google.golang.org/grpc/status"

	pb "example.com/grpclab/gen/go/cellctl/v1"
)

type server struct {
	pb.UnimplementedCellServiceServer
	pod   string
	count atomic.Int64
}

func (s *server) GetCell(ctx context.Context, req *pb.GetCellRequest) (*pb.Cell, error) {
	s.count.Add(1)
	if req.GetCellId() == "" {
		return nil, status.Error(codes.InvalidArgument, "cell_id is required")
	}
	if req.GetCellId() == "missing" {
		return nil, status.Errorf(codes.NotFound, "cell %q not found", req.GetCellId())
	}
	return &pb.Cell{CellId: req.GetCellId(), Region: "us-west-2", Phase: pb.CellPhase_CELL_PHASE_READY}, nil
}

func (s *server) WhoAmI(ctx context.Context, _ *pb.WhoAmIRequest) (*pb.WhoAmIResponse, error) {
	return &pb.WhoAmIResponse{Pod: s.pod, RequestCount: s.count.Add(1)}, nil
}

func (s *server) WatchCells(_ *pb.WatchCellsRequest, stream grpc.ServerStreamingServer[*pb.CellEvent]) error {
	for i := 0; i < 100; i++ {
		select {
		case <-stream.Context().Done(): // respect client cancellation / deadline
			return stream.Context().Err()
		case <-time.After(time.Second):
		}
		if err := stream.Send(&pb.CellEvent{Cell: &pb.Cell{CellId: "cell-1", Phase: pb.CellPhase_CELL_PHASE_READY}}); err != nil {
			return err
		}
	}
	return nil
}

func main() {
	pod := os.Getenv("POD_NAME")
	if pod == "" { pod, _ = os.Hostname() }

	lis, err := net.Listen("tcp", ":8080")
	if err != nil { log.Fatal(err) }

	srv := grpc.NewServer(
		grpc.KeepaliveParams(keepalive.ServerParameters{
			Time: 30 * time.Second, Timeout: 10 * time.Second,
			MaxConnectionAge: 5 * time.Minute, MaxConnectionAgeGrace: 30 * time.Second,
		}),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime: 15 * time.Second, PermitWithoutStream: true,
		}),
		grpc.MaxConcurrentStreams(256),
	)
	pb.RegisterCellServiceServer(srv, &server{pod: pod})

	hs := health.NewServer()
	hs.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(srv, hs)
	reflection.Register(srv)

	log.Printf("serving on :8080 as %s", pod)
	log.Fatal(srv.Serve(lis))
}
```

`cmd/client/main.go`:

```go
package main

import (
	"context"
	"flag"
	"log"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"

	pb "example.com/grpclab/gen/go/cellctl/v1"
)

const svcCfg = `{
  "loadBalancingConfig": [{"round_robin":{}}],
  "methodConfig": [{
    "name": [{"service": "cellctl.v1.CellService"}],
    "timeout": "3s",
    "retryPolicy": {
      "maxAttempts": 4, "initialBackoff": "0.1s", "maxBackoff": "1s",
      "backoffMultiplier": 2, "retryableStatusCodes": ["UNAVAILABLE"]
    }
  }],
  "retryThrottling": {"maxTokens": 10, "tokenRatio": 0.1}
}`

func main() {
	target := flag.String("target", "dns:///localhost:8080", "gRPC target")
	n := flag.Int("n", 20, "requests")
	flag.Parse()

	cc, err := grpc.NewClient(*target,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultServiceConfig(svcCfg),
		grpc.WithKeepaliveParams(keepalive.ClientParameters{
			Time: 30 * time.Second, Timeout: 10 * time.Second, PermitWithoutStream: true,
		}),
	)
	if err != nil { log.Fatal(err) }
	defer cc.Close()

	c := pb.NewCellServiceClient(cc)
	hits := map[string]int{}
	for i := 0; i < *n; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		resp, err := c.WhoAmI(ctx, &pb.WhoAmIRequest{})
		cancel()
		if err != nil { log.Printf("err: %v", err); continue }
		hits[resp.GetPod()]++
	}
	log.Printf("distribution: %v", hits)
}
```

Run it:

```sh
go mod tidy
go run ./cmd/server &
go run ./cmd/client -n 20
```

### Part 3 — Break schema compatibility on purpose

Commit first (`buf breaking` diffs against git):

```sh
# -b main matters: git still defaults to `master`, and the --against ref below
# names `main` explicitly.
git init -b main && git add -A && git commit -m "baseline"
```

Now make a change that is *source*-breaking but wire-safe — rename `region` to `cloud_region`, keeping number 2:

```sh
sed -i '' 's/string region = 2;/string cloud_region = 2;/' proto/cellctl/v1/cellctl.proto
buf breaking --against '.git#branch=main'
```

You get a `FIELD_SAME_NAME` violation. Now do the genuinely dangerous one — revert, then *renumber*:

```sh
git checkout proto/cellctl/v1/cellctl.proto
sed -i '' 's/string region = 2;/string region = 7;/' proto/cellctl/v1/cellctl.proto
buf breaking --against '.git#branch=main'   # FIELD_SAME_NUMBER
```

Then prove why it matters. Revert, regenerate a *v1* client binary, then change the proto so `region` becomes number 3 and `phase` becomes number 2 (swapping them), regenerate the server, and run the old client against the new server. The client will decode the enum's varint into the string field or fail to parse — a silent data corruption that no test with matching generated code would ever catch. This is the single most valuable ten minutes in the lab.

Finally, delete a field the right way:

```proto
message Cell {
  string cell_id = 1;
  CellPhase phase = 3;
  reserved 2;
  reserved "region";
}
```

`buf breaking` still flags the removal (correctly — it is a source break), but the `reserved` guarantees nobody reuses number 2 later.

### Part 4 — grpcurl against reflection

Before starting: restore the proto to its baseline and regenerate, or the server
will not compile against the renumbered/reserved schema from Part 3.

```sh
git checkout proto/cellctl/v1/cellctl.proto && buf generate

go run ./cmd/server &

grpcurl -plaintext localhost:8080 list
grpcurl -plaintext localhost:8080 describe cellctl.v1.Cell
grpcurl -plaintext -d '{"cell_id":"cell-1"}' localhost:8080 cellctl.v1.CellService/GetCell

# Errors, including the status code
grpcurl -plaintext -d '{}'                localhost:8080 cellctl.v1.CellService/GetCell
grpcurl -plaintext -d '{"cell_id":"missing"}' localhost:8080 cellctl.v1.CellService/GetCell

# A server stream, and a deadline that expires mid-stream
grpcurl -plaintext -d '{}' -max-time 5 localhost:8080 cellctl.v1.CellService/WatchCells

# Health
grpcurl -plaintext localhost:8080 grpc.health.v1.Health/Check
```

Watch the server log when `-max-time 5` fires: `stream.Context().Done()` closes and the handler returns `context.Canceled`. That is client cancellation propagating to the server, which is the mechanism that stops a cancelled request from burning backend CPU.

### Part 5 — Observe LB pinning in kind, then fix it

```sh
kind create cluster --name grpclab

cat > Dockerfile <<'EOF'
FROM golang:1.24 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -o /server ./cmd/server
RUN CGO_ENABLED=0 go build -o /client ./cmd/client
FROM gcr.io/distroless/static
COPY --from=build /server /server
COPY --from=build /client /client
ENTRYPOINT ["/server"]
EOF

docker build -t grpclab:dev .
kind load docker-image grpclab:dev --name grpclab
```

`k8s.yaml` — note **two** Services for the same pods:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: cellsvc }
spec:
  replicas: 4
  selector: { matchLabels: { app: cellsvc } }
  template:
    metadata: { labels: { app: cellsvc } }
    spec:
      containers:
        - name: server
          image: grpclab:dev
          imagePullPolicy: IfNotPresent
          ports: [{ containerPort: 8080 }]
          env:
            - name: POD_NAME
              valueFrom: { fieldRef: { fieldPath: metadata.name } }
          readinessProbe:
            grpc: { port: 8080, service: "" }
            initialDelaySeconds: 2
---
apiVersion: v1
kind: Service                 # L4 ClusterIP: this is the trap
metadata: { name: cellsvc-clusterip }
spec:
  selector: { app: cellsvc }
  ports: [{ port: 8080, targetPort: 8080 }]
---
apiVersion: v1
kind: Service                 # Headless: DNS returns all pod IPs
metadata: { name: cellsvc-headless }
spec:
  clusterIP: None
  selector: { app: cellsvc }
  ports: [{ port: 8080, targetPort: 8080 }]
```

```sh
kubectl apply -f k8s.yaml
kubectl rollout status deploy/cellsvc
kubectl get endpoints cellsvc-headless     # should list 4 pod IPs
```

Now run the client from inside the cluster, twice.

```sh
# A) Through the ClusterIP Service. Expect ALL requests on ONE pod.
kubectl run c1 --rm -it --restart=Never --image=grpclab:dev \
  --command -- /client -target dns:///cellsvc-clusterip:8080 -n 40

# B) Through the headless Service with round_robin. Expect ~10 per pod.
kubectl run c2 --rm -it --restart=Never --image=grpclab:dev \
  --command -- /client -target dns:///cellsvc-headless:8080 -n 40
```

(The Dockerfile above builds `/client` into the same image for exactly this. If you would rather not rebuild, run the client on your laptop against `kubectl port-forward` with a `dns:///` target pointed at the forwarded address — but note that port-forward is itself a single connection, so it hides the very effect you are trying to see.)

Run A prints something like `distribution: map[cellsvc-7d9f-4kx2q:40]`. Run B prints roughly `map[...:10 ...:10 ...:10 ...:10]`. That contrast — same pods, same client code, one line of target and service config different — is the entire L4/L7 story in one experiment.

Two extensions worth doing:

1. `kubectl delete pod <the pinned pod>` during run A and watch every request fail with `UNAVAILABLE` before the retry policy recovers on a new connection.
2. Set `MaxConnectionAge: 30s` on the server, rerun A with `-n 400`, and watch the distribution slowly even out as `GOAWAY`s force reconnection. That is the L4 mitigation, live.

---

## Production gotchas

1. **An L4 load balancer in front of gRPC pins every client to one backend.** HTTP/2 multiplexes all RPCs onto one connection, so the LB's choice is made once and never revisited. Symptoms: uneven CPU, scaling-up does nothing, restarts "fix" it. Fix with client-side LB over a headless Service, an L7 proxy, or a mesh. Mitigate with `MaxConnectionAge`. ([Kubernetes blog](https://kubernetes.io/blog/2018/11/07/grpc-load-balancing-on-kubernetes-without-tears/))

2. **`grpc.Dial` defaults to the `passthrough` resolver; `grpc.NewClient` defaults to `dns`.** If you migrate a `Dial` call to `NewClient` without an explicit `dns:///` prefix, behavior changes; if you keep `Dial` and expect `round_robin` to work, it silently balances over exactly one address. Always write the scheme. ([anti-patterns](https://github.com/grpc/grpc-go/blob/v1.83.2/Documentation/anti-patterns.md))

3. **The default LB policy is `pick_first`, not `round_robin`.** Adding pod IPs to DNS accomplishes nothing without `"loadBalancingConfig": [{"round_robin":{}}]`. ([load-balancing doc](https://github.com/grpc/grpc/blob/master/doc/load-balancing.md))

4. **Max receive message size is 4 MB, max send is unbounded.** A sender will happily transmit a message the receiver refuses with `ResourceExhausted`. Raise both sides, and only after doing the memory math: 4 MB × concurrency lives on the heap. ([`rpc_util.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/rpc_util.go))

5. **Client keepalive is off by default, and the server's enforcement minimum is 5 minutes.** Turning on client keepalive without coordinating the server's `EnforcementPolicy.MinTime` earns `GOAWAY` + `ENHANCE_YOUR_CALM` + `too_many_pings` after more than 2 strikes, then a reconnect loop across your whole fleet. Configure both sides in the same change. ([`keepalive.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/keepalive/keepalive.go), [`http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go))

6. **Cloud LB idle timeouts silently eat idle connections.** AWS NLB defaults to 350s, Azure LB to 4 minutes, GCP internal passthrough NLB to 600s. Set client keepalive `Time` well under the smallest value on the path, and set `PermitWithoutStream: true` — an idle *connection* with an in-flight long-poll RPC still sends zero frames. ([AWS](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html), [Azure](https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-tcp-reset), [GCP](https://cloud.google.com/load-balancing/docs/internal/int-netlb-traffic-distribution))

7. **An RPC without a deadline is a leak.** No deadline is the default. Set one on every call, derive downstream contexts from the incoming `ctx`, and never pass `context.Background()` to a call made on behalf of a request. ([deadlines guide](https://grpc.io/docs/guides/deadlines/))

8. **The deadline is a total budget across retries and hedges**, not a per-attempt timeout. A `maxAttempts: 5` policy under a 1s deadline gets as many attempts as fit in 1s. ([gRFC A6](https://github.com/grpc/proposal/blob/master/A6-client-retries.md))

9. **Retries without `retryThrottling` turn a brownout into an outage.** The throttle is a per-server token bucket that disables retries when the failure ratio crosses a threshold; there is no default. Configure `maxTokens`/`tokenRatio` whenever you configure `retryPolicy`.

10. **Enum zero values and open enums combine into silent misbehavior.** A value your binary was not compiled against is preserved as an unknown number, not rejected — so a naive `switch` with a `default` treats "future phase" identically to "unspecified". Always reserve `0` for `_UNSPECIFIED` and handle unknowns as a distinct branch. ([enum behavior](https://protobuf.dev/programming-guides/enum/))

11. **Deleting a field without `reserved` is a time bomb.** Nothing stops a future change from reusing that number with a different type, and old peers will misparse the new bytes into the old field. `reserved` both the number and the name, always. ([dos and don'ts](https://protobuf.dev/best-practices/dos-donts/))

12. **Unary interceptors do not cover streaming RPCs.** An auth or rate-limit interceptor registered only via `ChainUnaryInterceptor` leaves every streaming method wide open. Register the stream variant too, and put panic recovery outermost. ([interceptors guide](https://grpc.io/docs/guides/interceptors/))

13. **`credentials.NewTLS` snapshots the `*tls.Config` at construction.** Certificates rotated on disk by cert-manager are not picked up until restart unless you use `GetCertificate`/`GetClientCertificate` callbacks. Everything works for 89 days and then does not. ([cert-manager](https://cert-manager.io/docs/))

14. **`RequireAndVerifyClientCert` authenticates the CA, not the identity.** Without SAN/SPIFFE checking, any workload holding a cert from your issuer can call any service. Add explicit peer-identity verification.

15. **grpc-go's server does not advertise `MAX_CONCURRENT_STREAMS` by default** (its default is `math.MaxUint32`), so clients fall back to assuming 100 and self-throttle there while the server accepts unbounded concurrency. Set `grpc.MaxConcurrentStreams(n)` explicitly so both the advertised limit and the handler-count semaphore reflect your real capacity. ([`http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go), [PR #6703](https://github.com/grpc/grpc-go/commit/f2180b4d5403d2210b30b93098eb7da31c05c721))

16. **Long polls look like hung requests to every generic timeout in the path.** Temporal's matching long poll defaults to 1 minute; any proxy route timeout, ingress read timeout, or LB request timeout below that will sever them. Also exclude poll methods from latency SLOs and from any "slow request" alerting. ([dynamic configuration](https://docs.temporal.io/references/dynamic-configuration))

17. **Static flow-control windows throttle high-latency links.** A fixed 64 KiB stream window caps throughput at roughly `window / RTT` — about 650 KB/s at 100 ms RTT — no matter how much bandwidth exists. grpc-go's dynamic BDP estimator is on by default; do not disable it for cross-region traffic without measuring. ([`dialoptions.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/dialoptions.go), [flow control guide](https://grpc.io/docs/guides/flow-control/))

18. **`GracefulStop` matters more than you think.** The two-phase `GOAWAY` handshake lets in-flight RPCs finish and lets racing new streams be transparently retried. `Stop()` (or a `SIGKILL` after too short a `terminationGracePeriodSeconds`) turns a clean rolling upgrade into a client-visible error spike. Make the grace period longer than your longest expected RPC — including long polls.

19. **Rich error details ride in a header list** (`grpc-status-details-bin`) and are therefore bounded by max-header-list-size — currently 16 MB in grpc-go but moving to 8 KiB. Keep details small and structured; never put a stack trace there. ([`defaults.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/defaults.go))

20. **Protobuf serialization is not canonical.** Do not compare or hash serialized bytes to test message equality — use `proto.Equal`. Map ordering alone makes byte comparison unreliable. ([serialization is not canonical](https://protobuf.dev/programming-guides/serialization-not-canonical/))

---

## How this shows up in cell lifecycle

**Provisioning.** A cell is not ready when its pods are `Running`; it is ready when a client outside the cell can complete an RPC. That chain is: DNS resolves → LB has healthy targets → TLS handshakes with a cert whose SAN matches the endpoint → the gRPC health service reports `SERVING`. Your provisioning readiness gate should be an actual `grpc.health.v1.Health/Check` through the real ingress path, not a TCP dial. `grpc_health_probe` or `grpcurl` in a job is the right primitive. And since networking is often co-owned with a separate networking team, the health check is the contract boundary: it is the assertion that both halves of the cell are done.

**The L4/L7 decision is a per-cloud decision, and it is yours.** AWS NLB, GCP passthrough NLB, and Azure Load Balancer are all L4 and all pin gRPC. AWS ALB, GCP Application Load Balancer, and Azure Application Gateway are L7 and do per-request balancing, but each has its own gRPC caveats and its own idle-timeout semantics. A cell blueprint that says "put a load balancer in front of the frontend" without specifying the layer will behave differently in each cloud — and the difference will surface as a load-distribution incident in whichever cloud you tested least. Encode the layer, the idle timeout, and the request timeout as explicit, per-cloud values in the Helm templates.

**Helm templates the numbers; nothing enforces they agree.** Because Helm here is templating only, with no release state, the coupled values in this guide — server `keepAliveEnforcementPolicy.minTime` versus client `keepalive.Time`, LB idle timeout versus keepalive interval, proxy route timeout versus long-poll expiration, `MaxConnectionAge` versus `terminationGracePeriodSeconds` — are just numbers in different YAML files that happen to need to be consistent. That is a good place for a CI check or a rendered-manifest test: assert `minTime <= client keepalive Time`, assert `keepalive Time < lb_idle_timeout - margin`, assert `route_timeout > long_poll_expiration + margin`. These are cheap tests that prevent expensive pages.

**Upgrades are a drain problem, and drain is a gRPC problem.** Rolling a cell's frontend means every worker's long poll and every SDK connection has to move. Done right: `GracefulStop`, a `terminationGracePeriodSeconds` longer than your longest RPC, `preStop` sleep long enough for endpoint removal to propagate, and `MaxConnectionAge` already spreading reconnections so the herd is not synchronized. Done wrong: a wall of `UNAVAILABLE` and a synchronized re-poll stampede against the new pods. The mechanism that makes "done right" invisible is gRPC's transparent retry on `REFUSED_STREAM`/`GOAWAY` — you get it for free, but only if the server drains rather than dies.

**Teardown has the same shape with a harder deadline.** Before deleting a cell you need to know that no client is still holding a stream to it. channelz on the server side (`GetServers` → sockets → `StreamsStarted` minus `StreamsSucceeded`/`StreamsFailed`) gives you in-flight stream counts directly, which is a better teardown gate than "no traffic in the last N seconds".

**Certificates are cell lifecycle, not security theater.** Each cell needs internode certs (history ↔ matching ↔ frontend) and frontend-facing certs, typically cert-manager-issued into mounted Secrets. Two failure modes to design against: a provisioning race where pods start before the Certificate is `Ready` (add an init container or a readiness gate), and a rotation failure where the cert on disk is updated but the process holds the old one (use `GetCertificate` callbacks). Temporal's config has a `refreshInterval` for exactly this and cert-expiration warning windows — set them.

**Multi-cloud means the transport story cannot depend on one cloud's features.** ALTS is only available on Google infrastructure. Managed L7 features differ across ALB/GCLB/App Gateway. The portable substrate is: mTLS with a shared issuer, client-side round-robin over headless Services for in-cell traffic, and an explicit, per-cloud L7 configuration for ingress. Any design that assumes one cloud's LB semantics will need a special case for the other two.

---

## Learning path

**Day 1 (≈3 hours).** Read the [gRPC core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/) page and skim [PROTOCOL-HTTP2](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md) — specifically the headers/trailers section, so "status lives in a trailer" is permanent. Do Parts 1, 2, and 4 of the lab: schema, codegen with buf, server, client, grpcurl. Read the two generated `.go` files end to end. Then read [`temporalio/api`'s `service.proto`](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto) and find the poll RPCs.

**Week 1 (≈8 hours).** Do lab Parts 3 and 5 — schema breakage and LB pinning. These are the two experiments that change how you read incidents. Then read, in this order: [`Documentation/anti-patterns.md`](https://github.com/grpc/grpc-go/blob/v1.83.2/Documentation/anti-patterns.md), the [keepalive guide](https://grpc.io/docs/guides/keepalive/) plus [`keepalive.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/keepalive/keepalive.go), and [gRFC A6](https://github.com/grpc/proposal/blob/master/A6-client-retries.md) in full. Then read Temporal's [`internal/grpc_dialer.go`](https://github.com/temporalio/sdk-go/blob/master/internal/grpc_dialer.go) and [`common/rpc/grpc.go`](https://github.com/temporalio/temporal/blob/main/common/rpc/grpc.go) and annotate every option with which section of this guide it corresponds to. Finally, go find your cells' actual rendered values for: server `keepAliveEnforcementPolicy.minTime`, LB idle timeout per cloud, proxy route timeout, and `terminationGracePeriodSeconds`. Write them down in one table.

**Month 1.** Read [`internal/transport/http2_server.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go) — the ping-strike logic, the `GOAWAY` drain handler, and `handleData`'s flow-control comments are the highest-value 200 lines in the repo. Skim [RFC 9113](https://www.rfc-editor.org/rfc/rfc9113.html) sections 5 (streams), 6.9 (flow control), and 6.8 (GOAWAY). Stand up channelz on a staging cell and use it to answer a real question. Wire [`stats/opentelemetry`](https://pkg.go.dev/google.golang.org/grpc/stats/opentelemetry) into something and build a dashboard that separates per-attempt from per-call metrics. Add `buf breaking` to a CI pipeline that does not have it. Then write the CI assertions described above that check the coupled timeout values agree across the Helm templates — that artifact is a concrete, reviewable contribution in your first month.

---

## References

1. [gRPC over HTTP/2 protocol specification — grpc/grpc](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md) — exact header, trailer, and framing contract; the primary wire reference.
2. [RFC 9113: HTTP/2 — IETF](https://www.rfc-editor.org/rfc/rfc9113.html) — streams, SETTINGS, flow control, GOAWAY; obsoletes RFC 7540.
3. [RFC 7541: HPACK — IETF](https://www.rfc-editor.org/rfc/rfc7541.html) — header compression, and why high-cardinality metadata is expensive.
4. [Language Guide (proto3) — Protocol Buffers](https://protobuf.dev/programming-guides/proto3/) — the canonical proto3 semantics reference.
5. [Encoding — Protocol Buffers](https://protobuf.dev/programming-guides/encoding/) — wire types, varints, and why field numbers 1–15 are cheap.
6. [Application Note: Field Presence — Protocol Buffers](https://protobuf.dev/programming-guides/field_presence/) — implicit vs explicit presence and what `optional` restores in proto3.
7. [Enum Behavior — Protocol Buffers](https://protobuf.dev/programming-guides/enum/) — open vs closed enums and the unknown-value rules.
8. [Proto Best Practices (dos and don'ts) — Protocol Buffers](https://protobuf.dev/best-practices/dos-donts/) — the official list of schema changes that break things.
9. [Proto Serialization Is Not Canonical — Protocol Buffers](https://protobuf.dev/programming-guides/serialization-not-canonical/) — why you must never hash or byte-compare serialized messages.
10. [Well-Known Types — Protocol Buffers](https://protobuf.dev/reference/protobuf/google.protobuf/) — `Timestamp`, `Duration`, `Any`, `FieldMask`, `Struct`.
11. [Protobuf Editions Overview — Protocol Buffers](https://protobuf.dev/editions/overview/) — what replaces `syntax = "proto3"` and how features work.
12. [Protocol Buffer Compiler Installation — Protocol Buffers](https://protobuf.dev/installation/) — current `protoc` version (34.1) and install paths.
13. [Generated-code reference (Go) — gRPC](https://grpc.io/docs/languages/go/generated-code/) — the generics-based stream types and thread-safety rules for generated stubs.
14. [Releases — grpc/grpc-go](https://github.com/grpc/grpc-go/releases) — current version (v1.83.2) and behavior-change notes: header list size, path validation, frame throttling.
15. [Releases — protocolbuffers/protobuf-go](https://github.com/protocolbuffers/protobuf-go/releases) — current `google.golang.org/protobuf` version (v1.36.12).
16. [Install the Buf CLI — Buf](https://buf.build/docs/cli/installation/) — current buf version (1.72.0) and install/verify steps.
17. [Generate code with the Buf CLI — Buf](https://buf.build/docs/generate/tutorial/) — working `buf.yaml` / `buf.gen.yaml` v2, local vs remote plugins, managed mode.
18. [`buf.gen.yaml` reference (v2) — Buf](https://buf.build/docs/configuration/v2/buf-gen-yaml/) — every generation key, including `clean` and `managed`.
19. [Breaking rules and categories — Buf](https://buf.build/docs/breaking/rules/) — the authoritative FILE/PACKAGE/WIRE_JSON/WIRE rule sets.
20. [Buf Schema Registry — Buf](https://buf.build/docs/bsr/) — modules, generated SDKs, lint/breaking policies, server-side checks.
21. [Calling APIs with `buf curl` — Buf](https://buf.build/docs/curl/usage/) — schema-aware CLI calls, including `--http2-prior-knowledge` for plaintext.
22. [Deadlines — gRPC](https://grpc.io/docs/guides/deadlines/) — why every call needs one and how propagation works.
23. [Status codes and their use in gRPC — gRPC](https://grpc.io/docs/guides/status-codes/) — the canonical code list and when each is generated.
24. [Error handling — gRPC](https://grpc.io/docs/guides/error/) — the `google.rpc.Status` rich-error model and details.
25. [gRFC A6: gRPC Retry Design — grpc/proposal](https://github.com/grpc/proposal/blob/master/A6-client-retries.md) — retries, hedging, throttling, pushback, transparent retries, commit semantics.
26. [Service Config — grpc/grpc](https://github.com/grpc/grpc/blob/master/doc/service_config.md) — the JSON schema for LB policy, per-method timeouts, and retry config.
27. [Keepalive — gRPC](https://grpc.io/docs/guides/keepalive/) — the cross-language defaults table and TCP_USER_TIMEOUT interaction.
28. [gRFC A8: Client-side Keepalive — grpc/proposal](https://github.com/grpc/proposal/blob/master/A8-client-side-keepalive.md) — the client ping state machine.
29. [gRFC A9: Server-side Connection Management — grpc/proposal](https://github.com/grpc/proposal/blob/master/A9-server-side-conn-mgt.md) — max connection age/idle and the enforcement policy.
30. [`keepalive/keepalive.go` (v1.83.2) — grpc-go](https://github.com/grpc/grpc-go/blob/v1.83.2/keepalive/keepalive.go) — every keepalive default, documented inline.
31. [`internal/transport/defaults.go` (v1.83.2) — grpc-go](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/defaults.go) — window sizes, write quota, header list sizes, `MaxStreamID`, client stream default of 100.
32. [`internal/transport/http2_server.go` (v1.83.2) — grpc-go](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_server.go) — ping strikes, `ENHANCE_YOUR_CALM`, `REFUSED_STREAM`, the two-phase GOAWAY drain, BDP flow control.
33. [`dialoptions.go` (v1.83.2) — grpc-go](https://github.com/grpc/grpc-go/blob/v1.83.2/dialoptions.go) — default scheme, 30-minute channel idle timeout, buffer sizes, `maxCallAttempts = 5`.
34. [`rpc_util.go` (v1.83.2) — grpc-go](https://github.com/grpc/grpc-go/blob/v1.83.2/rpc_util.go) — the 4 MB receive default, unbounded send default, retry buffer size, HTTP-to-gRPC code mapping.
35. [Anti-patterns of client creation — grpc-go](https://github.com/grpc/grpc-go/blob/v1.83.2/Documentation/anti-patterns.md) — `NewClient` vs `Dial`, why `WithBlock` is wrong, error-handling guidance.
36. [gRPC Name Resolution — grpc/grpc](https://github.com/grpc/grpc/blob/master/doc/naming.md) — target URI schemes, resolver plugin contract, service-config delivery.
37. [Load Balancing in gRPC — grpc/grpc](https://github.com/grpc/grpc/blob/master/doc/load-balancing.md) — the client-side LB design and the pick_first/round_robin model.
38. [gRPC Load Balancing on Kubernetes without Tears — Kubernetes blog](https://kubernetes.io/blog/2018/11/07/grpc-load-balancing-on-kubernetes-without-tears/) — secondary, but the clearest write-up of the L4 pinning failure mode.
39. [gRPC — Envoy documentation](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/other_protocols/grpc) — what an L7 proxy adds: per-request balancing, `grpc-status` awareness, bridges.
40. [GRPC Health Checking Protocol — grpc/grpc](https://github.com/grpc/grpc/blob/master/doc/health-checking.md) — the `Health` service definition, empty-string convention, and `Watch` semantics.
41. [Liveness, Readiness, and Startup Probes — Kubernetes](https://kubernetes.io/docs/concepts/workloads/pods/probes/) — native `grpc` probe configuration (GA since 1.27).
42. [grpcurl — fullstorydev](https://github.com/fullstorydev/grpcurl) — reflection-driven and protoset-driven CLI calls, including mTLS flags.
43. [go-grpc-middleware v2 — grpc-ecosystem](https://github.com/grpc-ecosystem/go-grpc-middleware) — production interceptors for auth, logging, recovery, retry, rate limiting; used by Temporal's Go SDK.
44. [`stats/opentelemetry` — grpc-go on pkg.go.dev](https://pkg.go.dev/google.golang.org/grpc/stats/opentelemetry) — first-party OTel metrics via `DialOption`/`ServerOption`.
45. [gRFC A14: gRPC Channelz — grpc/proposal](https://github.com/grpc/proposal/blob/master/A14-channelz.md) — what channelz exposes, including per-socket flow-control windows and stream counts.
46. [Authentication — gRPC](https://grpc.io/docs/guides/auth/) — channel credentials vs call credentials, TLS/mTLS/ALTS/token composition.
47. [ALTS authentication (Go) — gRPC](https://grpc.io/docs/languages/go/alts/) — Google-infrastructure-only mutual auth via `alts.NewServerCreds`.
48. [cert-manager documentation](https://cert-manager.io/docs/) — Certificate resources, issuers, and Secret-mounted rotation.
49. [`temporalio/api`](https://github.com/temporalio/api) — the Buf-managed repository holding Temporal's entire gRPC contract.
50. [`temporal/api/workflowservice/v1/service.proto` — temporalio/api](https://github.com/temporalio/api/blob/master/temporal/api/workflowservice/v1/service.proto) — `WorkflowService`, including the long-poll RPCs.
51. [`internal/grpc_dialer.go` — temporalio/sdk-go](https://github.com/temporalio/sdk-go/blob/master/internal/grpc_dialer.go) — the SDK's real dial options: round-robin, 30s/15s keepalive, 128 MB payloads, gzip, interceptor chain.
52. [`common/rpc/grpc.go` — temporalio/temporal](https://github.com/temporalio/temporal/blob/main/common/rpc/grpc.go) — internode dialer: round-robin service config, 128 MB internode receive, 4 MB frontend request cap.
53. [`common/config/config.go` — temporalio/temporal](https://github.com/temporalio/temporal/blob/main/common/config/config.go) — server keepalive/enforcement defaults, TLS groups, cert refresh interval, HTTP port.
54. [Temporal Cluster dynamic configuration reference — Temporal](https://docs.temporal.io/references/dynamic-configuration) — `matching.longPollExpirationInterval` and the rest of the runtime knobs.
55. [Update the TCP idle timeout for your NLB listener — AWS](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html) — the 350s default and the 60–6000s configurable range.
56. [Load Balancer TCP Reset and idle timeout — Microsoft Learn](https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-tcp-reset) — Azure's 4-minute default and why to enable TCP Reset.
57. [Traffic distribution for internal passthrough Network Load Balancers — Google Cloud](https://cloud.google.com/load-balancing/docs/internal/int-netlb-traffic-distribution) — the 600-second connection-tracking idle timeout.
