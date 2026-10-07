# Go for Infrastructure Engineers

**Why this matters.** Almost everything you will touch in cloud infrastructure work is Go. The Temporal server itself is a Go module (`go.temporal.io/server`, currently pinned to `go 1.26.4` with Uber's `fx` DI container, `zap`, `tally`, `gocql`, and `pgx` in its dependency tree — see [its `go.mod`](https://github.com/temporalio/temporal/blob/main/go.mod)). The Temporal SDKs you will use to model cell lifecycle as workflows are Go ([temporalio/sdk-go](https://github.com/temporalio/sdk-go)). And every tool between you and a running cell — Kubernetes, `client-go`, `controller-runtime`, Karpenter, cert-manager, Terraform providers, external-dns, the cloud SDKs for AWS/GCP/Azure — is Go. You will spend more time *reading* Go than writing it: chasing a reconcile loop that will not converge, reading `client-go` cache semantics to explain a stale read, or reading Temporal server code to understand why a task queue is backing up. This guide is aimed at that: the model, the API surface, and the places Go surprises people who already know systems.

Go version facts in this guide were verified against the official release notes on 2026-08-29. The current release is **Go 1.27, released August 2026** ([release notes](https://go.dev/doc/go1.27), [release history](https://go.dev/doc/devel/release)); Go 1.26 shipped February 2026 ([notes](https://go.dev/doc/go1.26)) and Go 1.25 in August 2025 ([notes](https://go.dev/doc/go1.25)).

---

## The mental model

Go is not "Python with types" or "Java without the ceremony." It is a language designed around one thesis: **the bottleneck in large systems is human comprehension of code, not expressiveness.** Every design decision follows from that, and several of them will feel like regressions until you internalize the trade.

**1. Compilation produces one file, and that file is the deployment artifact.** `go build` emits a single executable with the runtime, GC, scheduler, and all your dependencies linked in. There is no interpreter, no JVM, no site-packages, no wheel. Your container image can be `FROM scratch` plus a binary plus CA certificates. Cross-compilation is `GOOS=linux GOARCH=arm64 go build` — no cross-toolchain, no sysroot, as long as you are not using cgo. For a team that ships the same control-plane component to AWS, GCP, and Azure, on amd64 and arm64, this is the single biggest practical win over Python or Java.

The caveat that bites everyone: "static" is a property of the *build*, not the language. If cgo is enabled (the default when a C toolchain is present) and you import `net` or `os/user`, the linker will pull in the system resolver and produce a dynamically linked binary against glibc. `CGO_ENABLED=0` forces the pure-Go path ([`net` name resolution docs](https://pkg.go.dev/net#hdr-Name_Resolution)).

**2. There is no inheritance. There are no classes.** There are structs, methods on named types, and interfaces. Reuse comes from *composition* (embedding a struct or interface inside another) and from *interfaces*, never from an "is-a" hierarchy. If you come from Java, the mental substitution is: replace every abstract base class with an interface plus a concrete struct, and replace every `extends` with a field.

**3. Interfaces are satisfied implicitly (structural typing).** A type implements an interface by having the methods. There is no `implements` keyword, no registration, no import of the interface package. This inverts the dependency direction: the *consumer* declares the interface it needs, the *producer* knows nothing about it. This is why Go codebases have hundreds of one-method and two-method interfaces defined right next to the function that consumes them.

**4. Every type has a useful zero value, and you get it for free.** `var buf bytes.Buffer` is a ready-to-use buffer. `var mu sync.Mutex` is an unlocked mutex. `var wg sync.WaitGroup` is a zero-count WaitGroup. A `nil` map reads fine (returns zero values) but panics on write. A `nil` slice appends fine. Idiomatic Go designs types so the zero value works, which is why you see very few constructors compared to Java. [Effective Go](https://go.dev/doc/effective_go#composite_literals) treats this as a design obligation, not a convenience.

**5. Errors are ordinary values, returned explicitly, checked explicitly.** There are no exceptions in the control-flow sense. `panic` exists and unwinds the stack, but it is for programmer bugs and unrecoverable states, not for "the file was missing." The `if err != nil` boilerplate is the cost; the benefit is that every failure path is visible in the source, which matters enormously when you are reading unfamiliar infrastructure code trying to answer "can this return without cleaning up?"

**6. Concurrency is cheap and first-class, but it is not free of the usual hazards.** Goroutines cost a few kilobytes of stack and are multiplexed onto OS threads by the runtime. Channels give you CSP-style handoff. But Go has real shared memory, real data races, and a formal [memory model](https://go.dev/ref/mem) you are expected to respect. The race detector is a first-class tool, not an afterthought.

**7. The toolchain is part of the language.** `go build`, `go test`, `go vet`, `go fmt`, `go mod`, `pprof`, the race detector, and the execution tracer all ship in the distribution and all agree on the same project layout. There is no Maven-vs-Gradle, no pip-vs-poetry-vs-uv. This is why the CNCF ecosystem converged on Go: contributions from strangers build the same way everywhere.

**What Go deliberately does not give you:** no sum types / tagged unions (you fake them with interfaces or a struct with a discriminator), no exceptions, no operator overloading, no immutability keyword, no ownership/borrow checking, no macro system, no dependency injection framework in the stdlib (Temporal server uses [`go.uber.org/fx`](https://github.com/temporalio/temporal/blob/main/go.mod) for that), and — until very recently — no method-level generics.

---

## Core concepts

### Compilation, binaries, and the build model

```bash
go build ./...                 # compile every package, write binaries for main packages
go build -o bin/cellctl ./cmd/cellctl
go install ./cmd/cellctl       # build and place in $GOBIN (default $HOME/go/bin)
go run ./cmd/cellctl -- --dry-run
```

Useful build knobs for infra work:

| Flag / env | Effect | Why you care |
|---|---|---|
| `CGO_ENABLED=0` | Pure-Go build, no libc linkage | Reproducible `FROM scratch` images; no glibc-vs-musl surprises |
| `GOOS` / `GOARCH` | Cross-compile target | One CI job builds linux/amd64 + linux/arm64 |
| `-ldflags "-s -w"` | Strip symbol table and DWARF | Smaller images; costs you readable stack symbols in some tools |
| `-ldflags "-X main.version=$(git rev-parse HEAD)"` | Inject a string at link time | Stamping build metadata into the binary |
| `-trimpath` | Remove local filesystem paths from the binary | Reproducible builds |
| `-race` | Build with the race detector | Test-only; 2-20x slower ([docs](https://go.dev/doc/articles/race_detector)) |
| `-gcflags='-m'` | Print escape-analysis decisions | Finding accidental heap allocations |
| `-tags foo,bar` | Enable build-tagged files | Cloud-specific or FIPS-specific code paths |

Every Go binary embeds its own build metadata — module versions, VCS commit, build settings. This is genuinely useful when you are staring at a running cell and want to know exactly what is deployed:

```bash
go version -m ./bin/cellctl
# ./bin/cellctl: go1.27.0
#   path    github.com/temporal/cellctl/cmd/cellctl
#   mod     github.com/temporal/cellctl  (devel)
#   dep     sigs.k8s.io/controller-runtime  v0.x.y   h1:...
#   build   -buildmode=exe
#   build   CGO_ENABLED=0
#   build   vcs.revision=9f3a1c...
```

Go 1.25 added `go version -m -json` so you can pipe this into tooling ([Go 1.25 notes](https://go.dev/doc/go1.25#go-command)).

### Types, zero values, and what "no inheritance" costs you

```go
package cell

import "time"

// Phase is a string enum. Go has no enum type; this is the idiom.
type Phase string

const (
	PhasePending     Phase = "Pending"
	PhaseProvisioned Phase = "Provisioned"
	PhaseDraining    Phase = "Draining"
	PhaseTerminated  Phase = "Terminated"
)

// Spec is the desired state of a cell. Note: every field's zero value is
// meaningful, or explicitly optional via a pointer.
type Spec struct {
	Name     string
	Region   string
	Provider string        // "aws" | "gcp" | "azure"
	Replicas int           // 0 means "unset"; validate explicitly
	Timeout  time.Duration // 0 means "use default"

	// Pointer distinguishes "not set" from "set to false".
	EnablePrivateLink *bool
}

// Status is observed state. The zero value is a legal "nothing observed yet".
type Status struct {
	Phase       Phase
	LastUpdated time.Time
	Conditions  []Condition // nil slice is fine: len == 0, range is a no-op
}
```

Things that surprise people:

- **`nil` slice vs empty slice.** `var s []string` is `nil`, but `len(s) == 0`, `append(s, "x")` works, and `for range s` is a no-op. The only visible difference is `s == nil` and JSON marshaling (`null` vs `[]`). Do not write `s := []string{}` reflexively.
- **`nil` map reads work, writes panic.** `var m map[string]int; m["k"]` returns `0`; `m["k"] = 1` panics. Always `make(map...)` before writing.
- **Struct assignment copies.** `a := b` where both are structs is a shallow copy. Copying a struct containing a `sync.Mutex` copies the lock state — this is a bug, and `go vet` catches it.
- **Method sets and pointer receivers.** If `func (c *Cell) Reconcile()` exists, then `*Cell` implements the interface but `Cell` does not. This is the single most common "why doesn't my type satisfy the interface" confusion.
- **`for` loop variables are per-iteration since Go 1.22.** The classic `for _, v := range xs { go func(){ use(v) }() }` bug is fixed in modern Go. If you read a codebase pinned to `go 1.21` or earlier in `go.mod`, the old semantics still apply — the behavior is gated on the language version in `go.mod`.

### Interfaces, embedding, and "accept interfaces, return structs"

```go
// Consumer-side interface: defined where it is USED, not where it is implemented.
// This is the whole point of implicit satisfaction.
type SubnetAllocator interface {
	Allocate(ctx context.Context, cidr string) (Subnet, error)
	Release(ctx context.Context, id string) error
}

// Producer returns a concrete struct. Callers can use every method;
// they narrow to an interface themselves if they want to.
type AWSAllocator struct {
	ec2   ec2API
	log   *slog.Logger
	limit rate.Limiter
}

func NewAWSAllocator(c ec2API, log *slog.Logger) *AWSAllocator { /* ... */ }
```

The rule "**accept interfaces, return structs**" ([Go Code Review Comments](https://go.dev/wiki/CodeReviewComments#interfaces)) exists because:

- Returning an interface hides methods callers may legitimately need, and forces every future addition through a breaking interface change.
- Returning an interface makes `nil` checking treacherous (see gotcha #2 below).
- Defining an interface in the producer package forces every consumer to import the producer just to name the type, which defeats the decoupling.

**Embedding** is Go's composition mechanism, and it looks like inheritance but is not:

```go
type Base struct{ log *slog.Logger }

func (b *Base) Logf(msg string) { b.log.Info(msg) }

type CellReconciler struct {
	Base                      // embedded struct: promotes Base's fields and methods
	client.Client             // embedded interface: CellReconciler now satisfies client.Client
	Scheme *runtime.Scheme
}

// r.Logf("...") works. r.Get(ctx, key, obj) works, delegating to the embedded Client.
```

The critical difference from inheritance: **there is no virtual dispatch through the embedded type.** If `Base.Logf` calls another `Base` method, it calls `Base`'s version, not an override in `CellReconciler`. Template Method does not work here. If you find yourself wanting it, use a function field or an explicit interface parameter.

Embedding an interface in a struct is a very common trick in `client-go`/`controller-runtime` code and in test doubles: embed the interface, override only the two methods you care about, and let everything else panic on nil if it is ever called (which is exactly what you want in a test).

### Errors: sentinel, wrapping, `errors.Is` / `errors.As`

Go 1.13 introduced error wrapping and the `errors.Is` / `errors.As` inspection functions ([Working with Errors in Go 1.13](https://go.dev/blog/go1.13-errors)). The vocabulary:

```go
import (
	"errors"
	"fmt"
)

// 1. Sentinel error: a package-level value used for equality comparison.
//    Name it Err<Thing>. Use sparingly — it becomes part of your API forever.
var ErrCellNotFound = errors.New("cell: not found")

// 2. Typed error: carries structured data. Name it <Thing>Error.
type QuotaError struct {
	Provider string
	Resource string
	Limit    int
}

func (e *QuotaError) Error() string {
	return fmt.Sprintf("cell: %s quota exceeded for %s (limit %d)", e.Provider, e.Resource, e.Limit)
}

// 3. Wrapping: %w records the cause in the chain. %v does NOT.
func provision(ctx context.Context, s Spec) error {
	if err := allocateSubnet(ctx, s); err != nil {
		return fmt.Errorf("provision %s in %s: %w", s.Name, s.Region, err)
	}
	return nil
}

// 4. Inspection at the call site.
func handle(err error) {
	if errors.Is(err, ErrCellNotFound) {          // walks the %w chain
		// ...
	}

	var qe *QuotaError
	if errors.As(err, &qe) {                       // finds a *QuotaError anywhere in the chain
		log.Warn("quota", "provider", qe.Provider, "limit", qe.Limit)
	}
}
```

Go 1.26 added [`errors.AsType`](https://go.dev/pkg/errors#AsType), a generic and type-safe version of `errors.As` that avoids the out-parameter dance:

```go
// Go 1.26+
if qe, ok := errors.AsType[*QuotaError](err); ok {
	log.Warn("quota", "provider", qe.Provider)
}
```

Rules that will come up in code review:

| Situation | Do |
|---|---|
| Adding context to an error you are returning up | `fmt.Errorf("doing X: %w", err)` — lowercase, no trailing punctuation, no "failed to" |
| You do NOT want callers to depend on the cause | `fmt.Errorf("doing X: %v", err)` — deliberately breaks the chain |
| Multiple errors from parallel work | `errors.Join(err1, err2)` — `errors.Is` works across all of them |
| Comparing errors | `errors.Is(err, ErrX)`, never `err == ErrX` (breaks the moment someone wraps) |
| Sentinel from another package | Prefer `errors.Is` against their exported sentinel; never string-match `err.Error()` |
| A `context` cancellation | `errors.Is(err, context.Canceled)` / `context.DeadlineExceeded` |

**When to panic.** Panic on programmer error that cannot be recovered from meaningfully: an impossible switch default, a failed invariant in a data structure, a `MustCompile` at package init. Do **not** panic across a package boundary. Library code that panics on bad input from a caller is acceptable only when the input is a programming mistake (regexp compilation of a literal), not a runtime condition. Long-running servers should `recover()` at exactly one place per goroutine boundary (an HTTP middleware, a worker loop) and turn the panic into a logged error plus a metric — otherwise one bad reconcile takes down the whole controller process, since an unrecovered panic in *any* goroutine kills the program.

### Modules, `go.mod`, workspaces, vendoring, build tags

A module is a versioned collection of packages with a `go.mod` at its root ([Go Modules Reference](https://go.dev/ref/mod)).

```
module github.com/temporalio/cell-controller

go 1.27

require (
    sigs.k8s.io/controller-runtime v0.x.y   // pin whatever matches your k8s.io/* minor
    k8s.io/apimachinery v0.35.4
    go.temporal.io/sdk v1.41.1
)

require (
    github.com/go-logr/logr v1.4.3 // indirect
)

replace github.com/some/fork => ../local-fork

exclude github.com/bad/dep v1.2.3

retract v1.0.1 // published by mistake
```

Key mechanics:

- **The `go` line is a language and behavior selector, not just documentation.** It gates language features (per-iteration loop variables from `go 1.22`), and it gates *runtime behavior changes* via the GODEBUG compatibility mechanism ([GODEBUG history](https://go.dev/doc/godebug)). Container-aware `GOMAXPROCS` is a concrete example: you get the new default by setting `go 1.25.0` or higher in `go.mod` ([Go blog](https://go.dev/blog/container-aware-gomaxprocs)). Bumping this line is a real change with real blast radius.
- **Minimal Version Selection (MVS).** Go picks the *maximum of the minimums* required across the graph, not the newest available. Builds are reproducible without a lockfile-resolver algorithm; `go.sum` records hashes, not a resolution.
- **`go mod tidy`** adds what is imported and removes what is not. In Go 1.27, for modules declaring `go 1.27`+, it also merges duplicate `require` blocks into at most two — one direct, one indirect ([Go 1.27 notes](https://go.dev/doc/go1.27#go-mod-tidy)).
- **`go mod init` picks a conservative `go` line.** Since Go 1.26, running `go mod init` with toolchain `1.N.X` writes `go 1.(N-1).0` ([Go 1.26 notes](https://go.dev/doc/go1.26#go-command)), so new modules stay usable by people one release behind.

**Vendoring.** `go mod vendor` copies dependencies into `./vendor`. If that directory exists and the `go` line is `1.14`+, the go command uses it automatically and builds offline. Kubernetes itself vendors; most operator repos do not. Vendoring buys you hermetic builds and a reviewable diff when a dependency changes; it costs you an enormous repo and noisy PRs. Know which convention each repo uses before you run `go mod tidy` and commit 40,000 lines.

**Workspaces (`go.work`).** For working on several modules at once without `replace` directives polluting `go.mod` ([tutorial](https://go.dev/doc/tutorial/workspaces)):

```bash
mkdir cellwork && cd cellwork
git clone https://github.com/temporalio/sdk-go
git clone https://internal/cell-controller
go work init ./cell-controller ./sdk-go
go build ./...   # cell-controller now compiles against your local sdk-go checkout
```

`go.work` and `go.work.sum` should generally be `.gitignore`d — a workspace is a developer's local view, not a property of the repo. Go 1.25 added the `work` package pattern, which matches all packages in the workspace (or the single main module outside workspace mode) ([Go 1.25 notes](https://go.dev/doc/go1.25#go-command)).

**Build tags / constraints.** Two mechanisms, both used heavily in cloud-portable code ([`go` command docs](https://pkg.go.dev/cmd/go#hdr-Build_constraints)):

```go
//go:build linux && (amd64 || arm64)

package cgroups
```

and *filename suffixes*, which are implicit constraints: `probe_linux.go`, `probe_darwin.go`, `probe_linux_arm64.go`, and `foo_test.go`. Filename suffixes are stronger than they look — `provider_aws.go` is **not** a build constraint (`aws` is not a known GOOS/GOARCH), so beginners sometimes think they have conditional compilation when they have nothing of the sort.

A pattern you will see in multi-cloud repos: a `//go:build integration` tag on tests that require real cloud credentials, run in CI with `go test -tags=integration ./...` and skipped otherwise.

### Concurrency: goroutines, channels, `select`

```go
go doWork(ctx)   // that's it — a few KB of stack, grown on demand
```

Channels are typed, optionally buffered queues with blocking semantics:

```go
ch := make(chan Result)      // unbuffered: send blocks until a receiver is ready (rendezvous)
ch := make(chan Result, 64)  // buffered: send blocks only when full
close(ch)                    // only the SENDER closes; receivers see zero value + ok=false
v, ok := <-ch                // ok == false means closed and drained
for v := range ch { }        // ranges until closed
```

`select` multiplexes. It blocks until exactly one case is ready, choosing pseudo-randomly among ready cases:

```go
func (w *Worker) run(ctx context.Context, in <-chan Task, out chan<- Result) error {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()

		case t, ok := <-in:
			if !ok {
				return nil // upstream closed; clean shutdown
			}
			r, err := w.handle(ctx, t)
			if err != nil {
				return fmt.Errorf("handle task %s: %w", t.ID, err)
			}
			select {
			case out <- r:
			case <-ctx.Done(): // never block forever on a send
				return ctx.Err()
			}

		case <-ticker.C:
			w.emitHeartbeat()
		}
	}
}
```

Two details in that snippet that separate correct from almost-correct code:

1. **`case <-ctx.Done()` is present on both the receive and the send.** A goroutine blocked on `out <- r` with no cancellation case is a leak the instant the consumer goes away.
2. **`defer ticker.Stop()`.** Before Go 1.23, an unstopped `time.Ticker` leaked. Modern Go garbage-collects unreferenced timers, but stopping is still correct and self-documenting. Note that as of **Go 1.27, channels created by the `time` package are always unbuffered (synchronous)**; the `asynctimerchan` GODEBUG that restored the old buffered behavior has been removed permanently ([Go 1.27 notes](https://go.dev/doc/go1.27#runtime)).

`select` with a `default` case is non-blocking — useful for "try to send, drop on the floor if the buffer is full" metric emitters, and dangerous everywhere else because it turns a blocking bug into a silent-data-loss bug.

### The scheduler: the G-M-P model

Go's runtime scheduler is an M:N scheduler described in Dmitry Vyukov's design document ([Scalable Go Scheduler Design Doc](https://golang.org/s/go11sched)); the authoritative version is the comment block at the top of [`runtime/proc.go`](https://github.com/golang/go/blob/master/src/runtime/proc.go).

| Entity | What it is |
|---|---|
| **G** | A goroutine: stack, program counter, scheduling state. Cheap; millions are fine. |
| **M** | An OS thread ("machine"). Created on demand; can be blocked in a syscall. |
| **P** | A "processor": a scheduling context holding a local run queue of Gs. **The number of Ps is `GOMAXPROCS`.** |

To run a goroutine, an M must hold a P. The counts matter:

- `GOMAXPROCS` limits **parallelism** (Gs running simultaneously), not thread count. A program with `GOMAXPROCS=4` can easily have 40 OS threads if 36 of them are blocked in syscalls.
- When a G makes a blocking syscall, the M detaches from its P, and the P is handed to another M so other goroutines keep running. This is why `os/exec` and blocking file I/O do not stall your whole server.
- Idle Ps **steal work** from other Ps' run queues, and there is a global run queue as a fallback.
- `sysmon`, a dedicated thread that runs without a P, retakes Ps from Gs that have been running too long and manages network poller readiness.
- **Preemption is asynchronous since Go 1.14** ([Go 1.14 notes](https://go.dev/doc/go1.14#runtime)). Before that, a tight loop with no function calls could wedge the scheduler; now the runtime delivers a signal and preempts at a safe point. You will still see this listed as a Go gotcha in old blog posts — it has not been true for years.

Go 1.26 added scheduler observability to [`runtime/metrics`](https://go.dev/pkg/runtime/metrics): goroutine counts by state under `/sched/goroutines`, OS thread count at `/sched/threads:threads`, and lifetime goroutine creations at `/sched/goroutines-created:goroutines` ([Go 1.26 notes](https://go.dev/doc/go1.26#runtimemetrics)). Export these. "Runnable goroutines climbing while CPU is flat" is the signature of CFS throttling, and now you can graph it.

### When NOT to use channels

This is the most common source of over-engineered Go in infrastructure codebases. The official position ([Go Wiki: Use a sync.Mutex or a channel?](https://go.dev/wiki/MutexOrChannel)) is "use whichever is most expressive and most simple," and explicitly warns against over-using channels because they are fun.

| Use a channel when | Use a mutex when |
|---|---|
| Passing ownership of data between goroutines | Protecting a shared data structure read/written in place |
| Distributing units of work | Guarding a cache, a counter, a connection pool's free list |
| Signaling completion, cancellation, or events | The critical section is short and non-blocking |
| Building pipelines with backpressure | You need `RLock` for a read-heavy structure |

A concrete anti-pattern: implementing a thread-safe map as a goroutine owning a `map` plus request/response channels. It is 40 lines instead of 8, roughly an order of magnitude slower, adds a goroutine to every stack trace, and introduces a shutdown problem. Just use `sync.RWMutex` around a `map`, or `sync.Map` if the access pattern is append-mostly with disjoint key sets.

Corollaries worth internalizing:

- **A mutex-protected struct field is not a "less advanced" solution.** Both the Kubernetes and Temporal codebases are full of them.
- **Do not use an unbuffered channel as a lock.** It works and it is slower and more confusing.
- **`sync/atomic` typed values (`atomic.Int64`, `atomic.Bool`, `atomic.Pointer[T]`) beat both** for a single counter or flag, and since Go 1.19 the typed API removes the alignment footguns of the old function-based API ([`sync/atomic`](https://pkg.go.dev/sync/atomic)).
- **`golang.org/x/sync/errgroup` replaces most hand-rolled `WaitGroup` + error channel code** ([docs](https://pkg.go.dev/golang.org/x/sync/errgroup)). `errgroup.WithContext` cancels siblings on first error; `g.SetLimit(n)` bounds concurrency.
- Go 1.25 added [`sync.WaitGroup.Go`](https://go.dev/pkg/sync#WaitGroup.Go), which fuses `wg.Add(1)` + `go func(){ defer wg.Done(); ... }()` into one call and eliminates the classic misplaced-`Add` bug (which `go vet`'s `waitgroup` analyzer, added in Go 1.25, also catches).

```go
// Provisioning three cloud resources concurrently, bounded, cancel-on-first-error.
func (p *Provisioner) provisionAll(ctx context.Context, specs []Spec) error {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(8) // bound concurrency: cloud APIs rate-limit

	results := make([]Result, len(specs))
	for i, s := range specs {
		g.Go(func() error {
			r, err := p.provisionOne(ctx, s)
			if err != nil {
				return fmt.Errorf("provision %s: %w", s.Name, err)
			}
			results[i] = r // safe: disjoint indices, no shared slice header mutation
			return nil
		})
	}
	return g.Wait()
}
```

### `context.Context` — the single most important thing

If you learn one Go API deeply for this job, make it this one. `context.Context` is how cancellation, deadlines, and request-scoped values propagate through every layer of a Go system, and every infrastructure library — `client-go`, `controller-runtime`, gRPC, the AWS/GCP/Azure SDKs, the Temporal SDK — is built around it ([package docs](https://pkg.go.dev/context), [Go blog: Context](https://go.dev/blog/context)).

**The contract.**

```go
type Context interface {
	Deadline() (deadline time.Time, ok bool)
	Done() <-chan struct{}   // closed when cancelled or deadline exceeded
	Err() error              // nil, context.Canceled, or context.DeadlineExceeded
	Value(key any) any
}
```

`Done()` returns a channel that is *closed*, not sent to. Closing broadcasts to every receiver at once, which is exactly the fan-out cancellation semantic you want. That is the whole trick.

**Construction.**

```go
ctx := context.Background()                              // root, in main() or a test
ctx := context.TODO()                                    // "I haven't wired this up yet" marker

ctx, cancel := context.WithCancel(parent)                // manual cancellation
ctx, cancel := context.WithTimeout(parent, 30*time.Second)
ctx, cancel := context.WithDeadline(parent, t)
defer cancel()                                           // ALWAYS. Not calling it leaks the parent's child list.

ctx, cancel := context.WithCancelCause(parent)           // Go 1.20: attach a reason
cancel(fmt.Errorf("cell %s drained", name))
context.Cause(ctx)                                       // returns that reason, not just context.Canceled

ctx := context.WithoutCancel(parent)                     // Go 1.21: detach from parent cancellation
ctx, cancel := context.WithDeadlineCause(parent, t, err) // Go 1.21
stop := context.AfterFunc(ctx, func() { /* on cancel */ })// Go 1.21: no goroutine needed
```

`context.WithoutCancel` deserves a callout for cell teardown work: when the parent request is cancelled but you still need to run cleanup or emit a final audit record, `WithoutCancel(ctx)` preserves the values (trace IDs, logger) while dropping the cancellation. Pair it with a fresh `WithTimeout` so cleanup is still bounded.

**The rules, and why each one exists.**

1. **`ctx` is the first parameter, always, and it is named `ctx`.** Never store it in a struct. The `containedctx` linter exists specifically to catch this ([golangci-lint linters](https://golangci-lint.run/docs/linters/)). The reason: a context is scoped to a call tree; a struct outlives call trees, so a stored context is either stale or a lifetime bug.
2. **Always call `cancel()`, even on the timeout path.** `WithTimeout` registers the child in the parent's children set; not cancelling leaks that entry until the *parent* is cancelled. In a long-lived server whose parent is `context.Background()`, that is an unbounded leak. `go vet`'s `lostcancel` analyzer catches the obvious cases.
3. **Cancellation is advisory.** Nothing forcibly stops a goroutine. Your code has to *check* — via `select { case <-ctx.Done(): }`, or by passing `ctx` into a library that checks. A CPU-bound loop with no check runs to completion regardless.
4. **Do not pass a `nil` Context.** Use `context.TODO()`.
5. **`context.Value` is for request-scoped data that crosses API boundaries, not for optional parameters.** Trace IDs, auth principals, request-scoped loggers: yes. Configuration, database handles, feature flags: no. Use an unexported key type so nobody can collide with or forge your key:

```go
type ctxKey struct{}          // unexported, zero-size, unique per package
type cellIDKey ctxKey

func WithCellID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, cellIDKey{}, id)
}

func CellIDFrom(ctx context.Context) (string, bool) {
	id, ok := ctx.Value(cellIDKey{}).(string)
	return id, ok
}
```

**Propagation through gRPC.** This is where context stops being a Go nicety and becomes a distributed-systems primitive. gRPC-Go maps the context deadline onto the wire: a client's remaining time is serialized into the `grpc-timeout` HTTP/2 header, and the server reconstructs a context with that deadline ([gRPC HTTP/2 protocol spec](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md), [gRPC deadlines guide](https://grpc.io/docs/guides/deadlines/)). Cancellation propagates too: if the client cancels, the server's `ctx.Done()` fires. `context.Value` does **not** cross the wire — for that you use gRPC metadata explicitly ([`google.golang.org/grpc/metadata`](https://pkg.go.dev/google.golang.org/grpc/metadata)), which is what OpenTelemetry propagators do under the hood.

The practical consequence for a cell control plane: **a deadline set at the edge is enforced at every hop, automatically, as long as everyone passes `ctx` through.** The failure mode is equally automatic: one service that does `context.Background()` instead of forwarding `ctx` becomes a deadline black hole, and you will only find it when a slow dependency causes unbounded queueing behind it. This is worth a lint rule (`contextcheck` reports functions that use a non-inherited context).

```go
// Correct: deadline flows client -> server -> downstream cloud API.
func (s *Server) ProvisionCell(ctx context.Context, req *pb.ProvisionRequest) (*pb.ProvisionResponse, error) {
	// Shorten, never lengthen, an inherited deadline.
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	out, err := s.ec2.CreateSubnet(ctx, &ec2.CreateSubnetInput{ /* ... */ })
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return nil, status.Error(codes.DeadlineExceeded, "subnet creation timed out")
		}
		return nil, status.Errorf(codes.Internal, "create subnet: %v", err)
	}
	// ...
}
```

Note the shape of that timeout: `WithTimeout(ctx, 20s)` takes the **minimum** of the inherited deadline and 20 seconds. You can always tighten a deadline; you can never loosen one (that is what `WithoutCancel` plus a new timeout is for, and it should be a deliberate, commented decision).

### Generics

Type parameters landed in Go 1.18 ([An Introduction to Generics](https://go.dev/blog/intro-generics), [type parameters proposal](https://go.googlesource.com/proposal/+/refs/heads/master/design/43651-type-parameters.md)). **Go 1.27 added generic methods** — a method declaration may now declare its own type parameters, which was the single largest gap ([Go 1.27 notes](https://go.dev/doc/go1.27#language)).

```go
// Constraint: a named interface used as a type set.
type Ordered interface {
	~int | ~int64 | ~float64 | ~string
}

func Max[T Ordered](a, b T) T {
	if a > b {
		return a
	}
	return b
}

// Generic container. Extremely common shape in infra code.
type Set[T comparable] struct{ m map[T]struct{} }

func NewSet[T comparable](vs ...T) *Set[T] {
	s := &Set[T]{m: make(map[T]struct{}, len(vs))}
	for _, v := range vs {
		s.m[v] = struct{}{}
	}
	return s
}

func (s *Set[T]) Has(v T) bool { _, ok := s.m[v]; return ok }
```

**What generics are actually good for** ([When to Use Generics](https://go.dev/blog/when-generics)):

- Container data structures: sets, ordered maps, LRU caches, typed work queues, priority queues.
- Functions over slices and maps where the element type is irrelevant to the logic: the stdlib `slices` and `maps` packages are the canonical example.
- Eliminating `interface{}`/`any` plus type assertions at API boundaries — `errors.AsType[*QuotaError](err)` in Go 1.26 is precisely this.
- Reducing near-identical code across numeric or string-keyed types.

**What generics still cannot do**, and this matters when you read library code:

- **No specialization or template metaprogramming.** The compiler uses GC shape stenciling with dictionaries; you cannot write different bodies per instantiation.
- **No covariance.** `[]*Cell` is not a `[]Object`, and a `Set[Dog]` is not a `Set[Animal]`. Ever.
- **Type parameters on interface methods are not allowed.** Even in Go 1.27, interface methods may not declare type parameters, and interface methods cannot be implemented by generic methods ([Go 1.27 notes](https://go.dev/doc/go1.27#language)). This is why Kubernetes API machinery still leans on `runtime.Object` and reflection rather than generics.
- **No operator constraints beyond the built-in type sets.** You cannot say "T has a `+` method."
- **Type inference is good but not total.** Go 1.27 generalized function type inference to all contexts where a generic function is assigned to or converted to a matching function type, which removes a common class of explicit-instantiation noise.

The Go community norm — and this is a real code-review norm, not a style preference — is: **write the concrete version first; introduce a type parameter only when you have at least two real instantiations.** Premature generics in Go reads as badly as premature abstraction anywhere else.

### The standard library infra people actually live in

**`net/http`.** Server and client in one package. Two things everybody gets wrong:

```go
// SERVER: the zero-value http.Server has no timeouts. This is a DoS vector.
srv := &http.Server{
	Addr:              ":8443",
	Handler:           mux,
	ReadHeaderTimeout: 5 * time.Second,
	ReadTimeout:       30 * time.Second,
	WriteTimeout:      30 * time.Second,
	IdleTimeout:       120 * time.Second,
	BaseContext:       func(net.Listener) context.Context { return rootCtx },
}

// Graceful shutdown: stop accepting, drain in-flight, bounded.
go func() {
	<-rootCtx.Done()
	sctx, cancel := context.WithTimeout(context.WithoutCancel(rootCtx), 25*time.Second)
	defer cancel()
	_ = srv.Shutdown(sctx)
}()

// CLIENT: never use http.DefaultClient in production. No timeout at all.
client := &http.Client{
	Timeout: 30 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 50,          // default is 2 — the #1 cause of connection churn
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	},
}

// ALWAYS drain and close the body, or you leak the connection back to the pool.
resp, err := client.Do(req)
if err != nil { return err }
defer resp.Body.Close()
```

Go 1.27 improved the drain situation: HTTP/1 `Response.Body` now automatically drains unread content on `Close`, up to a conservative limit, specifically to improve connection reuse ([Go 1.27 notes](https://go.dev/doc/go1.27#nethttp)). You still must `Close`.

**`encoding/json`.** As of **Go 1.27, `encoding/json` is backed by the new v2 implementation** — marshal is at parity, unmarshal is significantly faster, and the escape hatch is `GOEXPERIMENT=nojsonv2` at build time ([Go 1.27 notes](https://go.dev/doc/go1.27#json)). `encoding/json/v2` and `encoding/json/jsontext` are now available directly; v2 is stricter by default (rejects invalid UTF-8 in strings, rejects duplicate object names). Behavior of the v1 API is preserved, but **error message text may differ** — if you have tests asserting on JSON error strings, they will break on a toolchain bump.

Field-tag mechanics you need for CRDs and API types:

```go
type Spec struct {
	Name     string  `json:"name"`
	Region   string  `json:"region,omitempty"`   // omit if zero value
	Replicas *int32  `json:"replicas,omitempty"` // pointer: distinguishes 0 from unset
	Internal string  `json:"-"`                  // never serialized
}
```

**`time`.** `time.Duration` is an `int64` nanosecond count — `5 * time.Second`, never `5000`. `time.Time` carries a monotonic reading in addition to wall time; **use `time.Since(start)` for elapsed measurement**, which uses the monotonic clock and is immune to NTP steps and DST, and never subtract two wall-clock timestamps for durations. Always store and compare in UTC. `time.After` in a `select` inside a loop allocates a timer per iteration that lives until it fires — use a reusable `time.Timer` in hot loops.

**`sync` / `sync/atomic`.** `Mutex`, `RWMutex`, `WaitGroup` (with `.Go` since 1.25), `Once`, `OnceValue`/`OnceFunc` (1.21), `Pool`, `Map`. `sync.Pool` is for reducing GC pressure on short-lived, uniformly-sized buffers, and is cleared at every GC cycle — it is a caching optimization, not an object pool with lifetime guarantees. Typed atomics (`atomic.Int64`, `atomic.Bool`, `atomic.Pointer[T]`) are the correct choice for counters and flags.

**`os/exec`.** Every shell-out from a control plane should be context-aware and captured:

```go
func kubectlApply(ctx context.Context, manifest []byte) error {
	// CommandContext kills the process when ctx is cancelled.
	cmd := exec.CommandContext(ctx, "kubectl", "apply", "-f", "-")
	cmd.Stdin = bytes.NewReader(manifest)

	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr

	// Go 1.20+: run a graceful signal before the hard kill.
	cmd.Cancel = func() error { return cmd.Process.Signal(os.Interrupt) }
	cmd.WaitDelay = 5 * time.Second

	if err := cmd.Run(); err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return fmt.Errorf("kubectl apply exit %d: %s", ee.ExitCode(), stderr.String())
		}
		return fmt.Errorf("kubectl apply: %w", err)
	}
	return nil
}
```

`exec.CommandContext` kills only the direct child, not its process group — a `helm template` that spawns children can leave orphans. If that matters, set `SysProcAttr.Setpgid` and signal the negative PID.

**`log/slog`.** Structured logging in the standard library since Go 1.21 ([blog](https://go.dev/blog/slog), [package](https://pkg.go.dev/log/slog)). This is what you should use in new code rather than adding a logging dependency.

```go
logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
	Level:     slog.LevelInfo,
	AddSource: true,
}))
slog.SetDefault(logger)

// Attach dimensions once; they ride along on every subsequent record.
log := logger.With(
	slog.String("cell", spec.Name),
	slog.String("provider", spec.Provider),
	slog.String("region", spec.Region),
)
log.InfoContext(ctx, "provisioning started", slog.Int("replicas", spec.Replicas))
log.ErrorContext(ctx, "provisioning failed", slog.Any("err", err))
```

Use the `InfoContext`/`ErrorContext` variants so a custom handler can pull trace IDs out of the context. Go 1.26 added [`slog.NewMultiHandler`](https://go.dev/pkg/log/slog#NewMultiHandler) for fanning records to several handlers ([Go 1.26 notes](https://go.dev/doc/go1.26#logslog)). Note that the Kubernetes ecosystem standardized on `logr` (and `zapr`) before `slog` existed — `controller-runtime` gives you a `logr.Logger` from `log.FromContext(ctx)`, and Temporal server uses `zap`. Expect to bridge, not to unify.

### Testing

**Table-driven tests with `t.Run` are the house style** ([`testing` package](https://pkg.go.dev/testing)):

```go
func TestParseCellRef(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		in      string
		want    CellRef
		wantErr error
	}{
		{name: "aws happy path", in: "aws/us-west-2/cell-0042",
			want: CellRef{Provider: "aws", Region: "us-west-2", ID: "cell-0042"}},
		{name: "azure region with dash", in: "azure/west-europe/cell-1",
			want: CellRef{Provider: "azure", Region: "west-europe", ID: "cell-1"}},
		{name: "missing id", in: "gcp/us-central1", wantErr: ErrMalformedRef},
		{name: "empty", in: "", wantErr: ErrMalformedRef},
		{name: "unknown provider", in: "oracle/x/y", wantErr: ErrUnknownProvider},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()

			got, err := ParseCellRef(tt.in)
			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("ParseCellRef(%q) error = %v, want %v", tt.in, err, tt.wantErr)
			}
			if tt.wantErr != nil {
				return
			}
			if diff := cmp.Diff(tt.want, got); diff != "" {
				t.Errorf("ParseCellRef(%q) mismatch (-want +got):\n%s", tt.in, diff)
			}
		})
	}
}
```

`t.Run` gives you subtest names you can target with `-run 'TestParseCellRef/azure'`. `t.Parallel()` at both levels runs subtests concurrently — which is also a free race-detector workout.

**`testify` vs stdlib.** `github.com/stretchr/testify` gives you `assert`/`require` and a mocking package, and it is everywhere in the CNCF ecosystem (Temporal server depends on `testify v1.11.1` per its `go.mod`). Google's official style guidance is skeptical of assertion libraries because they produce failure messages divorced from the intent of the test and encourage assertion-per-line tests ([Google Go Style Decisions: assertion libraries](https://google.github.io/styleguide/go/decisions#assertion-libraries)). Practical position: match the repo. In new code, prefer stdlib comparisons plus [`github.com/google/go-cmp`](https://pkg.go.dev/github.com/google/go-cmp/cmp) for struct diffs; `cmp.Diff` output is dramatically more useful than `assert.Equal`. Use `require` when a failure should abort the subtest (it calls `t.FailNow`), `assert` when it should continue — mixing them up produces nil-pointer panics after a failed assertion.

For mocks, [`go.uber.org/mock`](https://github.com/uber-go/mock) (the maintained fork of gomock) is what Temporal server uses.

**`httptest`.** For anything that speaks HTTP — and for faking cloud APIs:

```go
func TestClientRetriesOn503(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL, WithMaxRetries(5))
	if _, err := c.Status(context.Background()); err != nil {
		t.Fatalf("Status() = %v, want nil", err)
	}
	if got := calls.Load(); got != 3 {
		t.Errorf("server calls = %d, want 3", got)
	}
}
```

Go 1.27 added [`httptest.NewTestServer`](https://go.dev/pkg/net/http/httptest#NewTestServer), which builds a server on an in-memory fake network suitable for use inside a `testing/synctest` bubble ([Go 1.27 notes](https://go.dev/doc/go1.27#nethttphttptest)).

**`testing/synctest`** is the sleeper feature for anyone testing timeout and retry logic. GA since Go 1.25 ([notes](https://go.dev/doc/go1.25#testingsynctest), [Go blog: Testing Time](https://go.dev/blog/synctest)). Inside a `synctest.Test` bubble, the `time` package runs on a fake clock that jumps forward instantly whenever every goroutine in the bubble is blocked. A backoff test that would take 90 seconds of real sleeping runs in microseconds, deterministically. Go 1.27 added `synctest.Sleep`, combining `time.Sleep` and `synctest.Wait`.

```go
func TestBackoffGivesUpAfterDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
		defer cancel()

		err := RetryWithBackoff(ctx, func(context.Context) error {
			return errors.New("still not ready")
		})
		// No real time elapses. The fake clock advances 5 simulated minutes instantly.
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("err = %v, want DeadlineExceeded", err)
		}
	})
}
```

**Fuzzing.** Built into the toolchain since Go 1.18 ([Go Fuzzing](https://go.dev/doc/security/fuzz/)). Worth it for anything that parses untrusted or semi-structured input: CIDR arithmetic, label selectors, cell reference strings, config parsers.

```go
func FuzzParseCellRef(f *testing.F) {
	f.Add("aws/us-west-2/cell-0042")
	f.Add("")
	f.Fuzz(func(t *testing.T, s string) {
		ref, err := ParseCellRef(s)   // must never panic
		if err != nil {
			return
		}
		if got := ref.String(); got != s {  // round-trip property
			t.Errorf("round-trip: %q -> %q", s, got)
		}
	})
}
```

```bash
go test -fuzz=FuzzParseCellRef -fuzztime=60s ./internal/cell
```

Failing inputs are written to `testdata/fuzz/` and become permanent regression tests.

**`-race`.** Run it in CI on every package, always.

```bash
go test -race -count=1 ./...
```

The race detector reports *actual* races observed at runtime, not potential ones — coverage matters, so `-race` plus `t.Parallel()` plus a load-ish integration test is the combination that finds real bugs. Cost: memory usage may increase 5-10x and execution time 2-20x ([Data Race Detector](https://go.dev/doc/articles/race_detector)). `-count=1` defeats the test result cache, which you want when chasing a flake.

### Profiling and debugging

Go's diagnostic story is unusually good and it is all in the box ([Diagnostics](https://go.dev/doc/diagnostics)).

**`pprof`.** Expose it in every long-running binary, on a separate, non-public listener:

```go
import _ "net/http/pprof" // registers handlers on http.DefaultServeMux

func init() {
	go func() {
		// Bind to localhost. Never expose pprof on a public interface.
		_ = http.ListenAndServe("127.0.0.1:6060", nil)
	}()
}
```

```bash
go tool pprof -http=:8080 http://localhost:6060/debug/pprof/heap
go tool pprof -http=:8080 http://localhost:6060/debug/pprof/profile?seconds=30   # CPU
curl -s 'http://localhost:6060/debug/pprof/goroutine?debug=2' > goroutines.txt   # full stacks
```

Available profiles include `heap`, `allocs`, `goroutine`, `block`, `mutex`, and `threadcreate` ([`net/http/pprof`](https://pkg.go.dev/net/http/pprof)). `block` and `mutex` profiling are off by default and must be enabled with `runtime.SetBlockProfileRate` / `runtime.SetMutexProfileFraction` — turn them on with a sampling rate, not fully, in production.

**New and directly relevant: the goroutine leak profile.** Introduced as an experiment in Go 1.26 and **generally available in Go 1.27** as the `goroutineleak` profile type in `runtime/pprof`, plus a `/debug/pprof/goroutineleak` endpoint ([Go 1.27 notes](https://go.dev/doc/go1.27#goroutine-leak-profile)). The runtime uses GC reachability: if a goroutine is blocked on a channel or mutex that is unreachable from any runnable goroutine, it can never wake, and it is reported. It does not incur overhead unless in use. For a control plane that spawns a goroutine per cell operation, this turns "our memory grows 200MB a week" from a multi-day investigation into a single curl.

```bash
curl -s http://localhost:6060/debug/pprof/goroutineleak > leaks.pb.gz
go tool pprof -http=:8080 leaks.pb.gz
```

Note that Go 1.26 changed the pprof web UI to default to the flame graph view; the old call graph is under "View -> Graph" or `/ui/graph` ([Go 1.26 notes](https://go.dev/doc/go1.26#pprof)).

**Execution tracer.** `go tool trace` shows goroutine scheduling, GC pauses, syscall blocking, and network poller activity on a timeline ([`runtime/trace`](https://pkg.go.dev/runtime/trace)).

```bash
curl -s 'http://localhost:6060/debug/pprof/trace?seconds=5' > trace.out
go tool trace trace.out
```

In Go 1.27, `go tool trace -http=:6060` now binds to localhost when given only a port, matching `go tool pprof`; pass `-http=0.0.0.0:6060` to listen on all addresses ([Go 1.27 notes](https://go.dev/doc/go1.27#trace)). Go 1.25 added [`runtime/trace.FlightRecorder`](https://go.dev/pkg/runtime/trace#FlightRecorder), a continuous in-memory ring buffer you snapshot on a triggering event — the right tool for "capture a trace of the 3 seconds *before* the latency spike."

**Escape analysis.** The compiler decides whether a value lives on the stack (free) or the heap (GC pressure). Ask it:

```bash
go build -gcflags='-m -m' ./internal/cell 2>&1 | grep escapes
# ./cell.go:41:13: &Spec{...} escapes to heap:
# ./cell.go:41:13:   flow: ~r0 = &{storage for &Spec{...}}:
```

Common causes of unintended heap allocation:

| Trap | Fix |
|---|---|
| Returning `*T` from a constructor for a small struct | Return `T` by value if it is small and not shared |
| Storing a value in an `interface{}` / `any` | Boxing always heap-allocates non-pointer values |
| `fmt.Sprintf` in a hot loop | `strconv.Itoa`, `strings.Builder`, or precompute |
| `append` without capacity | `make([]T, 0, n)` when `n` is known |
| `[]byte(s)` / `string(b)` conversions in a loop | Reuse buffers; use `unsafe.String`/`unsafe.Slice` only with extreme care |
| Closures capturing large structs | Capture the specific fields you need |
| Passing a large struct by value to a method with a pointer receiver elsewhere | Be consistent about receivers (`recvcheck` linter) |

Go has been getting better at this on its own: Go 1.25 and 1.26 both expanded stack allocation of slice backing stores, and Go 1.27 added size-specialized allocation routines that cut the cost of small (<80 byte) allocations by up to 30%, with an expected ~1% overall win in allocation-heavy programs and about 60KB of extra binary size ([Go 1.27 notes](https://go.dev/doc/go1.27#faster-memory-allocation)).

**`GOMAXPROCS` in containers.** Historically this was the number one Go-on-Kubernetes footgun: from Go 1.5 through Go 1.24, `GOMAXPROCS` defaulted to the machine's logical CPU count, ignoring the container's CPU limit entirely. A pod with a 2-CPU limit on a 128-core node got `GOMAXPROCS=128`, spiked CPU usage during GC, and got hard-throttled by the CFS bandwidth controller for the remainder of the 100ms period — devastating for tail latency ([Container-aware GOMAXPROCS](https://go.dev/blog/container-aware-gomaxprocs)). The industry workaround was [`go.uber.org/automaxprocs`](https://pkg.go.dev/go.uber.org/automaxprocs).

**Go 1.25 fixed this in the runtime** ([Go 1.25 notes](https://go.dev/doc/go1.25#container-aware-gomaxprocs)):

1. On Linux, the runtime reads the CPU bandwidth limit of the process's cgroup; if it is lower than the logical CPU count, `GOMAXPROCS` defaults to that limit (rounded **up**, since limits can be fractional and `GOMAXPROCS` must be a positive integer).
2. On all OSes, the runtime periodically re-reads the limit and adjusts if it changes — so a live `kubectl set resources` is picked up.

Critical details you must know:

- **This is based on CPU *limits*, not CPU *requests*.** The runtime explicitly does not consider requests, because containers with a request and no limit are meant to burst into idle capacity. If your pods set requests only (a very common pattern), you get the old behavior and should set `GOMAXPROCS` explicitly.
- **Setting the `GOMAXPROCS` env var or calling `runtime.GOMAXPROCS` disables both behaviors.** Go 1.25 added [`runtime.SetDefaultGOMAXPROCS`](https://go.dev/pkg/runtime#SetDefaultGOMAXPROCS) to opt back in.
- The GODEBUG kill switches are `containermaxprocs=0` and `updatemaxprocs=0`.
- **You get this by setting `go 1.25.0` or higher in `go.mod`** — it is a GODEBUG-gated compatibility change, not a toolchain-only change.
- The runtime keeps cached file descriptors on the cgroup files for the process lifetime to support the periodic re-read.

Downstream Kubernetes reading on why throttling hurts: [managing resources for containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#how-pods-with-resource-limits-are-run).

### Memory and GC behavior on Kubernetes

Go's collector is a concurrent, tri-color mark-and-sweep collector that is **non-generational and non-moving** ([A Guide to the Go Garbage Collector](https://go.dev/doc/gc-guide)). Two knobs matter:

| Knob | Meaning | Default |
|---|---|---|
| `GOGC` | Target heap growth percentage before the next cycle | `100` (collect when the live heap doubles) |
| `GOMEMLIMIT` | Soft limit on total runtime-managed memory | `math.MaxInt64` (effectively off) |

`GOMEMLIMIT` (Go 1.19+, also settable via [`runtime/debug.SetMemoryLimit`](https://pkg.go.dev/runtime/debug#SetMemoryLimit); design in [proposal 48409](https://github.com/golang/proposal/blob/master/design/48409-soft-memory-limit.md)) is the single most important production setting for Go on Kubernetes, and it is off by default.

The problem it solves: `GOGC` is *relative*. A service with a 100MB live heap and `GOGC=100` collects at 200MB. If a burst pushes the live heap to 600MB, it will happily grow to 1.2GB before collecting — and if your pod's memory limit is 1GB, the kernel OOM-kills it first. Go's GC has no idea the limit exists. The pod restarts, the burst re-arrives, and you have a crash loop that looks like a memory leak but is not.

`GOMEMLIMIT` gives the runtime the ceiling. As the total approaches it, GC runs more aggressively and, if that is not enough, more continuously.

```yaml
# Kubernetes: derive GOMEMLIMIT from the container limit with the Downward API.
env:
  - name: GOMEMLIMIT
    valueFrom:
      resourceFieldRef:
        resource: limits.memory
        divisor: "1"                 # bytes
```

That sets `GOMEMLIMIT` to 100% of the container limit, which is usually too aggressive. Practical guidance:

- **Set `GOMEMLIMIT` to roughly 85-90% of the container memory limit**, and treat the exact number as something to tune with a heap profile rather than a magic constant.
- **It is a *soft* limit.** The runtime makes no guarantee it will be respected under all circumstances — if the live heap genuinely exceeds it, the program still allocates rather than deadlocking.
- **It does not cover everything the kernel counts.** The limit covers the Go heap, goroutine stacks, and runtime-internal structures; it excludes the binary's own mappings, memory allocated by cgo or another language, and OS-held memory on the program's behalf. Leave headroom for those.
- **Death-spiral protection exists.** To avoid pathological GC thrash when the live heap approaches the limit, the runtime caps GC at roughly 50% of CPU. This turns "OOMKill" into "everything gets slow," which is easier to diagnose but is still an outage. Alert on GC CPU fraction, not just on OOMKills.
- **The `GOGC=off` + `GOMEMLIMIT` pattern** is documented in the GC guide for workloads with a well-understood peak: disable proportional GC entirely and let the memory limit be the only trigger. Do this only when you are confident about the peak live heap; if the live heap exceeds the limit, you get continuous GC.

Also relevant if you are reading recent runtime behavior changes: the **Green Tea garbage collector became the default in Go 1.26**, with an expected 10-40% reduction in GC overhead for GC-heavy programs, and a further ~10% on newer amd64 CPUs via vector instructions for small-object scanning ([Go 1.26 notes](https://go.dev/doc/go1.26#new-garbage-collector)). The `GOEXPERIMENT=nogreenteagc` opt-out was expected to be removed in Go 1.27. If you are bisecting a memory-behavior change across a toolchain bump from 1.25 to 1.26, this is the first thing to check.

*See also: [what to actually monitor in a cell](14-observability-for-cells.md#what-to-actually-monitor-in-a-cell) for where GC CPU fraction and OOMKill counts belong in a cell's alert set — "everything gets slow" is only diagnosable if you were already exporting the runtime metrics.*

### A short tour of controller-runtime and client-go

You will read operators — Karpenter, cert-manager, external-secrets, and whatever the cell lifecycle machinery uses — long before you write one. Here is enough to follow the code.

**The architecture** ([client-go under the hood](https://github.com/kubernetes/sample-controller/blob/master/docs/controller-client-go.md), [Kubebuilder Book](https://book.kubebuilder.io/architecture)):

```
API Server
    | LIST + WATCH
    v
Reflector  -->  DeltaFIFO  -->  Informer  -->  Indexer (thread-safe local cache)
                                    |
                                    | ResourceEventHandler (OnAdd/OnUpdate/OnDelete)
                                    v
                            Workqueue (rate-limited, deduplicating)
                                    |
                                    v
                            Reconcile(ctx, req)  --> reads from Indexer, writes to API server
```

The pieces, and why each exists:

- **Reflector** performs a LIST then a WATCH against the API server for one resource type and pushes deltas into a DeltaFIFO. On watch expiry it re-LISTs and resyncs.
- **Informer** pops the DeltaFIFO, updates the local **Indexer** (a thread-safe store keyed by `<namespace>/<name>` via `MetaNamespaceKeyFunc`), and fires event handlers.
- **Indexer / Lister** is your read path. `controller-runtime`'s default `client.Client` reads from this cache, not from the API server. This is the source of the classic surprise: **you create an object, immediately read it back, and get a stale result or a not-found.** The cache is eventually consistent with the API server. Write code that tolerates it, or use an uncached client for the specific read that must be authoritative.
- **Workqueue** ([`k8s.io/client-go/util/workqueue`](https://pkg.go.dev/k8s.io/client-go/util/workqueue)) holds *keys*, not objects, and it **deduplicates**: enqueueing the same key ten times while it is waiting yields one reconcile. It also guarantees a key is not processed by two workers concurrently, and provides rate-limited requeue with exponential backoff. This is what makes level-triggered reconciliation tractable.

**Reconcile is level-triggered, not edge-triggered.** This is the single most important conceptual point. `Reconcile` receives a *name*, not an event and not a diff. Its job is: read current desired state, read current actual state, make actual converge toward desired, return. It must be **idempotent** and must tolerate being called with no change at all, out of order, and many times.

```go
type CellReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

func (r *CellReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx).WithValues("cell", req.NamespacedName)

	var cell infrav1.Cell
	if err := r.Get(ctx, req.NamespacedName, &cell); err != nil {
		// NotFound means deleted and already garbage-collected. Do not requeue.
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Finalizer: gate deletion so we can tear down cloud resources first.
	const finalizer = "cells.example.com/teardown"
	if !cell.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(&cell, finalizer) {
			if err := r.teardownCloudResources(ctx, &cell); err != nil {
				return ctrl.Result{}, fmt.Errorf("teardown: %w", err)
			}
			controllerutil.RemoveFinalizer(&cell, finalizer)
			if err := r.Update(ctx, &cell); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{}, nil
	}

	// Desired child object, owned by the Cell so it is GC'd with it.
	desired := r.desiredNamespace(&cell)
	if err := controllerutil.SetControllerReference(&cell, desired, r.Scheme); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.Patch(ctx, desired, client.Apply,
		client.FieldOwner("cell-controller"), client.ForceOwnership); err != nil {
		return ctrl.Result{}, fmt.Errorf("apply namespace: %w", err)
	}

	if !r.isReady(ctx, &cell) {
		// Not an error: poll again. Returning an error would use exponential backoff
		// and pollute error metrics with an expected condition.
		return ctrl.Result{RequeueAfter: 15 * time.Second}, nil
	}

	log.Info("cell converged")
	return ctrl.Result{}, nil
}

func (r *CellReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&infrav1.Cell{}).            // primary resource
		Owns(&corev1.Namespace{}).       // watch children; map back via owner reference
		Complete(r)
}
```

Return-value semantics ([`reconcile` package](https://pkg.go.dev/sigs.k8s.io/controller-runtime/pkg/reconcile)):

| Return | Effect |
|---|---|
| `ctrl.Result{}, nil` | Done. No requeue (until a watch event fires). |
| `ctrl.Result{RequeueAfter: d}, nil` | Requeue after `d`. Use for polling an external system. |
| `ctrl.Result{}, err` | Requeue with rate-limited exponential backoff, and log the error. |
| `ctrl.Result{Requeue: true}, nil` | **Deprecated** in favor of `RequeueAfter` or returning an error. |

**Owner references** ([Kubernetes docs](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/)) are how child objects get cleaned up. `SetControllerReference` stamps the parent's UID into the child's `metadata.ownerReferences`; when the parent is deleted, the API server's garbage collector deletes the children. Two things to know: owner references **cannot cross namespaces** (a namespaced object cannot be owned by an object in another namespace), and a cluster-scoped object cannot be owned by a namespaced one. Cross-namespace or cross-cluster cleanup — which is what most cell teardown actually is — needs a **finalizer** plus explicit deletion logic, exactly as in the snippet above.

**Server-side apply.** `client.Apply` with a `FieldOwner` is the modern way for a controller to express "I own these fields" without clobbering fields owned by other actors. `ForceOwnership` takes over conflicting fields. If you have ever seen two controllers fight over a field in a hot loop, this is the fix.

**Reading operator code efficiently.** Start at `SetupWithManager` — it tells you what the controller watches and therefore what wakes it up. Then read `Reconcile` top to bottom as a state machine on the object's status. Then find the finalizer name and grep for it. That is 80% of understanding any operator.

*See also: [disruption in depth](06-karpenter.md#disruption-in-depth) and [the CRDs and the reconcile flow](11-cert-manager-and-pki.md#the-crds-and-the-reconcile-flow) for two production controllers worth reading with exactly this method — Karpenter's is the one whose finalizer can hold a node, and cert-manager's is the one whose reconcile chain is four CRDs deep.*

### Style and lint

**`gofmt` is not negotiable.** There is one formatting, the tool applies it, and nobody argues about it in review. `goimports` (or `gofmt` via `golangci-lint`'s formatters) additionally manages the import block. Configure your editor to run it on save; CI should fail on unformatted code.

The two canonical documents, and you should actually read both end to end once:

- [Effective Go](https://go.dev/doc/effective_go) — the "how to think in Go" document. Dated in places (predates modules, generics, and errors-wrapping) but still the best explanation of naming, interfaces, and composition.
- [Go Code Review Comments](https://go.dev/wiki/CodeReviewComments) — a checklist of the comments Go reviewers actually leave. Short. Read it before your first PR.

Also worth having: [Google's Go Style Guide](https://google.github.io/styleguide/go/) (three tiers: Style Guide, Decisions, Best Practices — the Decisions document is the most useful) and the [Uber Go Style Guide](https://github.com/uber-go/guide/blob/master/style.md), which is more opinionated and widely mirrored in the CNCF world.

Review comments you will receive, ranked by frequency:

1. **Error strings are lowercase and unpunctuated.** `fmt.Errorf("read config: %w", err)`, not `"Failed to read config."`. They get concatenated by wrapping, so capitals and periods appear mid-sentence.
2. **Don't say "failed to".** The fact that it is an error already says that. Say what you were doing.
3. **Receiver names are short and consistent** — `c *Cell`, not `this` or `self`, and the same letter on every method of the type.
4. **Package names are lowercase, singular, no underscores, and not `util`/`common`/`base`.** `cell`, not `cellutils`. Avoid stuttering: `cell.New`, not `cell.NewCell`.
5. **Don't name a getter `GetFoo`.** It is `Foo()`. (Except in generated protobuf code.)
6. **Return early; keep the happy path at minimal indentation.**
7. **Every exported identifier has a doc comment starting with its name.**
8. **`interface{}` should be `any`** in modern code.
9. **Don't add an interface until you have a second implementation** (or a test that genuinely needs a fake).

**`golangci-lint`** is the standard aggregator. As of v2 the configuration collapsed the old `enable-all`/`disable-all` into `linters.default`, which takes values like `standard`, `all`, `fast`, or `none` ([migration guide](https://golangci-lint.run/docs/product/migration-guide/), [configuration file reference](https://golangci-lint.run/docs/configuration/file/)). Check what your repo enables with `golangci-lint linters`.

```yaml
# .golangci.yml  (golangci-lint v2 schema)
version: "2"

linters:
  default: standard
  enable:
    - bodyclose        # HTTP response bodies must be closed
    - containedctx     # no context.Context stored in a struct
    - contextcheck     # no non-inherited contexts
    - errorlint        # correct use of %w, errors.Is/As
    - noctx            # HTTP requests must carry a context
    - nilerr           # returning nil after checking err != nil
    - recvcheck        # consistent pointer/value receivers
    - sloglint         # consistent log/slog usage
    - errcheck
    - govet
    - staticcheck
    - unused
    - ineffassign
  settings:
    errcheck:
      check-type-assertions: true

formatters:
  enable:
    - gofmt
    - goimports
```

The linters most worth their false-positive rate on infrastructure code are `bodyclose`, `contextcheck`, `containedctx`, `noctx`, and `errorlint` — every one of them catches a class of bug that shows up as a production leak rather than a test failure ([full linter list](https://golangci-lint.run/docs/linters/)).

Finally, **`go vet` runs automatically as part of `go test`** on a subset of high-confidence analyzers, and Go 1.27 added the `stdversion` check to that default set (it reports use of stdlib symbols newer than the `go` directive allows) ([Go 1.27 notes](https://go.dev/doc/go1.27#go-test)). Separately, **`go fix` was completely rewritten in Go 1.26** into a suite of "modernizers" that mechanically update code to current idioms and APIs, built on the same analysis framework as `go vet` ([Go 1.26 notes](https://go.dev/doc/go1.26#go-command)). Running `go fix ./...` on an old package is now a reasonable first step in a cleanup PR.

### Temporal's Go SDK in one subsection

You will be at Temporal, so a quick orientation. The [Go SDK developer guide](https://docs.temporal.io/develop/go) is the entry point; the API reference is [`go.temporal.io/sdk`](https://pkg.go.dev/go.temporal.io/sdk).

Three concepts:

- **Workflow** — deterministic orchestration code. It is replayed from an event history after a worker restart, so it must produce identical decisions given identical history.
- **Activity** — ordinary Go code with side effects (call an API, create a subnet). Retried by the service according to a retry policy. Not replayed.
- **Worker** — a process that polls a task queue and executes workflow and activity tasks.

```go
package cellflow

import (
	"time"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

func ProvisionCell(ctx workflow.Context, spec Spec) (Result, error) {
	ao := workflow.ActivityOptions{
		StartToCloseTimeout: 10 * time.Minute,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    10,
		},
	}
	ctx = workflow.WithActivityOptions(ctx, ao)

	var net NetworkResult
	if err := workflow.ExecuteActivity(ctx, AllocateNetwork, spec).Get(ctx, &net); err != nil {
		return Result{}, err
	}

	var cluster ClusterResult
	if err := workflow.ExecuteActivity(ctx, CreateCluster, spec, net).Get(ctx, &cluster); err != nil {
		return Result{}, err
	}
	return Result{Network: net, Cluster: cluster}, nil
}
```

The Go-specific rules that will trip you up:

- **`workflow.Context`, not `context.Context`, inside workflow code.** They are different types on purpose.
- **Never use native `go` statements, `time.Sleep`, `time.Now`, `math/rand`, or map iteration order in workflow code.** All of these are non-deterministic across replays. Use `workflow.Go()`, `workflow.Sleep()`, `workflow.Now()`, `workflow.SideEffect()`. The SDK runs workflow goroutines through a deterministic runner that schedules exactly one at a time, which is why you generally do not need mutexes inside workflows ([Temporal Go SDK multithreading](https://docs.temporal.io/develop/go/best-practices/multithreading)).
- **`workflowcheck`** is a static analyzer shipped in the SDK repo that flags non-deterministic constructs in workflow definitions ([README](https://github.com/temporalio/sdk-go/blob/main/contrib/tools/workflowcheck/README.md)). Wire it into CI for any repo containing workflow code.
- **Activities get a real `context.Context`**, including heartbeating via `activity.RecordHeartbeat(ctx, progress)` for long operations ([`activity` package](https://pkg.go.dev/go.temporal.io/sdk/activity)).
- Testing uses `testsuite.WorkflowTestSuite` with a mock environment ([testing guide](https://docs.temporal.io/develop/go/best-practices/testing-suite)).

Cell provisioning is close to the platonic Temporal use case: a long-running, multi-step, partially-failing process across three cloud providers where each step must be retried independently and the whole thing must survive a control-plane restart.

*See also: [workflows and the determinism constraint](15-temporal-programming-model.md#workflows-and-the-determinism-constraint) and [versioning](15-temporal-programming-model.md#versioning-the-hardest-part-of-operating-temporal) for the full customer-side model these rules are the Go surface of — including why a deployed Workflow you edit in place breaks on replay.*

---

## Hands-on

A single self-contained lab. Roughly 60-90 minutes. Requires a Go toolchain (`go1.25`+; some steps note `go1.27`) and Docker.

### 0. Verify your toolchain

```bash
go version
# go version go1.27.0 darwin/arm64   (or whatever you have)
go env GOROOT GOPATH GOMODCACHE GOTOOLCHAIN
```

If `go version` prints something older than 1.25, several steps below will behave differently; note which and move on rather than fighting it.

### 1. Create the module

```bash
mkdir -p ~/lab/cellprobe && cd ~/lab/cellprobe
go mod init example.com/cellprobe
cat go.mod
```

**Look for:** the `go` line. On a Go 1.27 toolchain, `go mod init` writes `go 1.26.0`, not `1.27` — that is the conservative default introduced in Go 1.26 ([notes](https://go.dev/doc/go1.26#go-command)). Bump it deliberately:

```bash
go get go@1.27.0    # or: go mod edit -go=1.27.0
```

### 2. A worker pool with context cancellation

```bash
mkdir -p cmd/probe internal/pool
go get golang.org/x/sync/errgroup
```

`internal/pool/pool.go`:

```go
package pool

import (
	"context"
	"fmt"
	"time"

	"golang.org/x/sync/errgroup"
)

type Job struct {
	ID  int
	Dur time.Duration
}

// Run executes jobs with bounded concurrency, cancelling siblings on first error.
func Run(ctx context.Context, jobs []Job, limit int) ([]string, error) {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(limit)

	out := make([]string, len(jobs))
	for i, j := range jobs {
		g.Go(func() error {
			select {
			case <-time.After(j.Dur):
			case <-ctx.Done():
				return fmt.Errorf("job %d: %w", j.ID, ctx.Err())
			}
			if j.ID == 7 {
				return fmt.Errorf("job %d: simulated provisioning failure", j.ID)
			}
			out[i] = fmt.Sprintf("job %d ok", j.ID)
			return nil
		})
	}
	return out, g.Wait()
}
```

`cmd/probe/main.go`:

```go
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"example.com/cellprobe/internal/pool"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	// NotifyContext cancels on SIGINT/SIGTERM. Since Go 1.26 the cancel cause
	// records which signal fired: https://go.dev/doc/go1.26#ossignal
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	slog.Info("runtime", "GOMAXPROCS", runtime.GOMAXPROCS(0), "NumCPU", runtime.NumCPU())

	jobs := make([]pool.Job, 20)
	for i := range jobs {
		jobs[i] = pool.Job{ID: i, Dur: time.Duration(100*i) * time.Millisecond}
	}

	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	res, err := pool.Run(ctx, jobs, 4)
	if err != nil {
		slog.Error("pool failed", "err", err, "cause", context.Cause(ctx))
	}
	var done int
	for _, r := range res {
		if r != "" {
			done++
		}
	}
	slog.Info("finished", "completed", done, "total", len(jobs))
}
```

```bash
go run ./cmd/probe
```

**Look for:** the run stops shortly after job 7 fails — about a second of wall clock, since job 7's own 700 ms only starts once one of the four slots frees up — rather than after the full ~1.9s the longest job would take — that is `errgroup.WithContext` cancelling siblings. `completed` should be well under 20. Then comment out the `j.ID == 7` branch and re-run: now the 3-second `WithTimeout` cuts it off instead, and the error is `context deadline exceeded`.

Now press Ctrl-C during a run and confirm it exits immediately instead of hanging.

### 3. Find a data race

Add a deliberately racy counter to `pool.Run` — replace the `out[i] = ...` line's surroundings with a shared, unsynchronized `total := 0` incremented inside each `g.Go` closure. Then:

```bash
go run ./cmd/probe          # probably prints a plausible-looking number
go run -race ./cmd/probe
```

**Look for:**

```
WARNING: DATA RACE
Write at 0x00c0000140a0 by goroutine 9:
  example.com/cellprobe/internal/pool.Run.func1()
      .../pool.go:31 +0x...
Previous write at 0x00c0000140a0 by goroutine 8:
  ...
```

The first run is the important lesson: the racy version produces a believable answer most of the time. Now fix it with `atomic.Int64` and confirm `-race` is clean. Note the run took noticeably longer under `-race` — that is the documented 2-20x ([race detector docs](https://go.dev/doc/articles/race_detector)).

### 4. Table-driven tests, subtests, and a fake HTTP server

`internal/pool/pool_test.go`:

```go
package pool

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestRun(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name      string
		jobs      []Job
		limit     int
		timeout   time.Duration
		wantErrIs error
	}{
		{name: "all succeed", jobs: []Job{{ID: 1}, {ID: 2}}, limit: 2, timeout: time.Second},
		{name: "one fails", jobs: []Job{{ID: 1}, {ID: 7}}, limit: 2, timeout: time.Second},
		{name: "deadline", jobs: []Job{{ID: 1, Dur: time.Hour}}, limit: 1,
			timeout: 50 * time.Millisecond, wantErrIs: context.DeadlineExceeded},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			ctx, cancel := context.WithTimeout(context.Background(), tt.timeout)
			defer cancel()

			_, err := Run(ctx, tt.jobs, tt.limit)
			if tt.wantErrIs != nil && !errors.Is(err, tt.wantErrIs) {
				t.Fatalf("err = %v, want errors.Is(_, %v)", err, tt.wantErrIs)
			}
		})
	}
}
```

```bash
go test -race -count=1 -v ./...
go test -run 'TestRun/deadline' -v ./internal/pool
```

**Look for:** the subtest names in the output (`--- PASS: TestRun/deadline`), and that `-run` with a slash targets a single subtest.

### 5. Static binary and container image

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -trimpath -ldflags="-s -w" -o bin/probe ./cmd/probe

file bin/probe
# bin/probe: ELF 64-bit LSB executable, x86-64, statically linked, stripped

go version -m bin/probe | head -20
```

**Look for:** `statically linked`. Now rebuild with `CGO_ENABLED=1` on a Linux host with a C toolchain and a program that imports `net`, and confirm it says `dynamically linked`. That difference is the entire "why does my scratch image say `no such file or directory` when the binary is right there" class of bug.

```dockerfile
# Dockerfile
FROM golang:1.27 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /probe ./cmd/probe

FROM gcr.io/distroless/static-debian12
COPY --from=build /probe /probe
ENTRYPOINT ["/probe"]
```

### 6. Watch `GOMAXPROCS` respond to a CPU limit

```bash
docker build -t cellprobe .

docker run --rm cellprobe
# {"level":"INFO","msg":"runtime","GOMAXPROCS":<all host CPUs>,"NumCPU":<all host CPUs>}

docker run --rm --cpus=2 cellprobe
# {"level":"INFO","msg":"runtime","GOMAXPROCS":2,"NumCPU":<all host CPUs>}
```

**Look for:** `GOMAXPROCS` tracking `--cpus` while `NumCPU` still reports the host. This only happens if `go.mod` says `go 1.25.0` or higher — that is the whole point of step 1. Prove it:

```bash
go mod edit -go=1.24.0 && docker build -t cellprobe-old . && docker run --rm --cpus=2 cellprobe-old
# GOMAXPROCS is now the full host CPU count again.
go mod edit -go=1.27.0   # put it back
```

Then confirm the GODEBUG kill switch and the fractional rounding:

```bash
docker run --rm --cpus=2   -e GODEBUG=containermaxprocs=0 cellprobe   # back to host count
docker run --rm --cpus=2.5 cellprobe                                   # GOMAXPROCS=3 (rounds up)
```

### 7. Escape analysis and allocation

```bash
go build -gcflags='-m' ./internal/pool 2>&1 | head -30
```

**Look for:** lines like `moved to heap:` and `... escapes to heap`. Now add a function that builds a string with `fmt.Sprintf` in a loop versus one that uses `strings.Builder`, write a benchmark for each, and:

```bash
go test -bench=. -benchmem -run=^$ ./internal/pool
# BenchmarkSprintf-10     1000000    1150 ns/op    320 B/op    12 allocs/op
# BenchmarkBuilder-10     5000000     210 ns/op     64 B/op     2 allocs/op
```

The `B/op` and `allocs/op` columns are what you optimize; `ns/op` usually follows.

### 8. pprof and the goroutine leak profile

Add to `main.go`:

```go
import (
	"net/http"      // needed for ListenAndServe below
	_ "net/http/pprof"
)

// in main(), before the work starts:
go func() { _ = http.ListenAndServe("127.0.0.1:6060", nil) }()

// and a deliberate leak:
leak := make(chan struct{})
for i := 0; i < 50; i++ {
	go func() { <-leak }() // blocks forever; `leak` becomes unreachable when main returns scope
}
```

Add a long sleep at the end of `main` so the process stays up, then:

```bash
go run ./cmd/probe &
sleep 2
curl -s 'http://localhost:6060/debug/pprof/goroutine?debug=1' | head -20
go tool pprof -http=:8080 http://localhost:6060/debug/pprof/heap
```

**Look for:** 50 goroutines parked in `chan receive` in the goroutine profile.

If you are on Go 1.27, also try the new dedicated profile ([Go 1.27 notes](https://go.dev/doc/go1.27#goroutine-leak-profile)):

```bash
curl -s http://localhost:6060/debug/pprof/goroutineleak > leaks.pb.gz
go tool pprof -top leaks.pb.gz
```

This is the profile that turns "goroutines are slowly climbing" into a stack trace. Note the caveat from the release notes: leaks reachable through global variables or through locals of runnable goroutines are not detected.

### 9. Deterministic time with `synctest`

```go
//go:build go1.25

package pool

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"
	"time"
)

func TestRunDeadlineInstant(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
		defer cancel()

		_, err := Run(ctx, []Job{{ID: 1, Dur: time.Hour}}, 1)
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("err = %v, want DeadlineExceeded", err)
		}
	})
}
```

```bash
go test -run TestRunDeadlineInstant -v ./internal/pool
```

**Look for:** the test simulating a 30-minute timeout and passing in single-digit milliseconds. That is the fake clock jumping forward the moment every goroutine in the bubble is blocked ([Testing Time](https://go.dev/blog/synctest)).

### 10. Lint it

```bash
go vet ./...
go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest run ./...
gofmt -l .          # should print nothing
go fix ./...        # Go 1.26+: apply modernizers, then review the diff
```

---

## Production gotchas

Numbered, opinionated, each one something that has cost someone a night.

1. **`GOMAXPROCS` on Kubernetes is only auto-correct if your `go.mod` says `go 1.25.0`+ and your pods set CPU *limits*.** Container-aware `GOMAXPROCS` reads the cgroup CPU *bandwidth* limit, and the release notes are explicit that "the Go runtime does not consider the 'CPU requests' option" ([Go 1.25 notes](https://go.dev/doc/go1.25#container-aware-gomaxprocs)). Requests-only pods — an extremely common Kubernetes convention, precisely so workloads can burst into idle capacity — get the old whole-machine default. For those, set `GOMAXPROCS` explicitly from the request via the Downward API, or accept the CPU spikes. Also: setting the `GOMAXPROCS` env var at all disables the periodic re-read, so a `kubectl set resources` will no longer be picked up ([Go blog](https://go.dev/blog/container-aware-gomaxprocs)).

2. **A non-nil interface holding a nil pointer is not nil.** This is Go's most famous trap and it survives into modern code:

   ```go
   func find() *QuotaError { return nil }
   func check() error      { return find() }   // returns a NON-NIL error!
   if err := check(); err != nil { /* THIS RUNS */ }
   ```

   An interface value is a (type, value) pair; it is `nil` only when both halves are. This is the concrete reason behind "return structs, not interfaces" and behind "declare `var err error` and assign, rather than returning a typed nil" ([Go Code Review Comments](https://go.dev/wiki/CodeReviewComments#interfaces), [Go FAQ: nil error](https://go.dev/doc/faq#nil_error)).

3. **Not calling `cancel()` from `context.WithCancel`/`WithTimeout` leaks until the parent is cancelled.** In a server whose root is `context.Background()`, that is forever, and the leak is a growing linked list of children plus a timer per `WithTimeout`. `defer cancel()` immediately after the call, every time, including on the success path. `go vet`'s `lostcancel` catches the obvious shapes but not all of them ([`context` docs](https://pkg.go.dev/context)).

4. **`http.DefaultClient` and `http.DefaultTransport` have no request timeout and `MaxIdleConnsPerHost: 2`.** The first means one hung TLS handshake to a cloud API pins a goroutine indefinitely; the second means a client hitting one host hard opens and closes connections constantly. Construct your own `http.Client` with `Timeout` and a tuned `Transport`, and share one instance ([`net/http`](https://pkg.go.dev/net/http#Transport)).

5. **Not draining and closing `resp.Body` leaks the connection.** `defer resp.Body.Close()` is necessary but historically insufficient — an unread body could not be reused. Go 1.27 now auto-drains up to a conservative limit on `Close` ([Go 1.27 notes](https://go.dev/doc/go1.27#nethttp)), which helps, but on any older toolchain you still want `io.Copy(io.Discard, resp.Body)` before closing when you abandon a response early. `bodyclose` lints for the missing `Close`.

6. **A `nil` map panics on write but not on read.** `var m map[string]string; m["k"] = "v"` panics with `assignment to entry in nil map`. This bites hardest when a struct field of map type is left unset by a JSON unmarshal of a document that omitted the key, and the panic surfaces three layers away from the cause.

7. **The zero-value `http.Server` has no timeouts.** No `ReadHeaderTimeout` means a slowloris client can hold a connection and a goroutine forever. Always set `ReadHeaderTimeout` at minimum ([`net/http`](https://pkg.go.dev/net/http#Server)).

8. **Copying a struct that contains a `sync.Mutex` copies the lock.** Two goroutines then lock two different mutexes and both enter the critical section. `go vet`'s `copylocks` catches this and it is one of the analyzers that runs automatically during `go test` — do not silence it.

9. **`GOMEMLIMIT` is off by default, and `GOGC` is relative, so nothing stops the heap from growing past the container limit.** The kernel OOM-kills you and the pod restarts into the same burst. Set `GOMEMLIMIT` to ~85-90% of `limits.memory`, and remember it is soft and excludes cgo/mmap memory ([GC guide](https://go.dev/doc/gc-guide)). Also alert on GC CPU fraction: the runtime's 50% GC CPU cap converts the OOM into a slow-death instead, which is quieter and worse.

10. **`controller-runtime`'s default client reads from an informer cache, not the API server.** Create-then-immediately-read returns stale data or `NotFound`. This is not a bug; it is the entire point of the cache. Write reconcilers that tolerate it (level-triggered, idempotent), and reach for an uncached reader only for the specific read that must be authoritative ([Kubebuilder Book](https://book.kubebuilder.io/architecture)).

11. **Returning an error from `Reconcile` for an expected "not ready yet" condition poisons your metrics and your backoff.** `ctrl.Result{RequeueAfter: 15 * time.Second}, nil` is the correct expression of "poll again"; `ctrl.Result{}, err` means "something went wrong," triggers exponential backoff, and logs. Also note `Result.Requeue` is deprecated in favor of `RequeueAfter` or returning an error ([`reconcile` docs](https://pkg.go.dev/sigs.k8s.io/controller-runtime/pkg/reconcile)).

12. **Owner references cannot cross namespaces, and cross-cluster cleanup does not exist.** For cell teardown that spans namespaces, clusters, or cloud providers, you need a finalizer plus explicit deletion logic — and then you own the failure mode where a finalizer blocks deletion forever because the cloud API is down ([Kubernetes owners and dependents](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/)). Every finalizer needs a documented manual-removal escape hatch.

13. **Native goroutines, `time.Now`, `time.Sleep`, and `math/rand` inside a Temporal workflow are determinism bugs, not style issues.** Replay after a worker restart will diverge and the workflow will fail. Use `workflow.Go`, `workflow.Now`, `workflow.Sleep`, `workflow.SideEffect`, and run the `workflowcheck` analyzer in CI ([Temporal Go SDK multithreading](https://docs.temporal.io/develop/go/best-practices/multithreading)).

14. **`exec.CommandContext` kills the child, not the process group.** A cancelled `helm`/`terraform`/`kubectl` invocation can leave grandchildren running and holding locks. Set `SysProcAttr.Setpgid = true` and signal `-pid` if the tool you shell out to spawns children. Also set `cmd.WaitDelay` so a child that ignores SIGINT does not block `Wait()` forever ([`os/exec`](https://pkg.go.dev/os/exec#Cmd)).

15. **`time.After` inside a `select` in a loop allocates a timer per iteration that survives until it fires.** With a long duration and a hot loop, that is a real leak. Hoist a `time.Timer` and `Reset` it, or use a `Ticker`. Related: as of Go 1.27, `time` package channels are always unbuffered, and the `asynctimerchan` GODEBUG that restored older buffered behavior is gone permanently ([Go 1.27 notes](https://go.dev/doc/go1.27#runtime)).

16. **Bumping the `go` line in `go.mod` changes runtime behavior, not just syntax availability.** GODEBUG-gated changes activate based on that line ([GODEBUG history](https://go.dev/doc/godebug)). Toolchain bumps to Go 1.26 changed the default GC (Green Tea) and to Go 1.27 changed the JSON implementation backing `encoding/json` — the latter preserves marshal/unmarshal semantics but **error message text may differ**, which breaks tests that assert on error strings ([Go 1.27 notes](https://go.dev/doc/go1.27#json)). Bump the toolchain and the `go` line in separate PRs.

17. **`sync.Pool` is cleared at every GC cycle and gives no lifetime guarantees.** It reduces allocation pressure for short-lived, similarly-sized buffers. It is not an object pool, not a connection pool, and not a cache with a hit-rate you can reason about ([`sync`](https://pkg.go.dev/sync#Pool)).

18. **`CGO_ENABLED=1` (the default when a C compiler is present) plus importing `net` or `os/user` yields a dynamically linked binary.** Drop it into `FROM scratch` or an alpine (musl) image and it fails at exec with a confusing "not found." Pin `CGO_ENABLED=0` in CI, or use distroless-base rather than distroless-static ([`net` name resolution](https://pkg.go.dev/net#hdr-Name_Resolution)).

19. **Go 1.25 fixed a compiler bug (present since Go 1.21) that incorrectly delayed nil-pointer checks.** Code that used a function result before checking its error — `f, err := os.Open(...); name := f.Name(); if err != nil {...}` — silently worked on Go 1.21-1.24 and now correctly panics ([Go 1.25 notes](https://go.dev/doc/go1.25#compiler)). If a toolchain bump produced new nil-pointer panics in old code, this is why, and the code was always wrong.

20. **`unbuffered channel send with no cancellation case` is the most common goroutine leak in infra Go.** A producer blocked on `out <- v` after the consumer returned early never exits, and its whole capture set stays reachable. Always pair a channel send with `case <-ctx.Done()`. The Go 1.26 release notes use exactly this shape as the motivating example for the goroutine leak profile ([Go 1.26 notes](https://go.dev/doc/go1.26#goroutineleak-profiles)).

---

## How this shows up in cell lifecycle

Mapping the above onto cell-lifecycle work — provisioning, upgrading, and tearing down Kubernetes-based cells across AWS, GCP, and Azure, with networking often owned by a separate team.

**Provisioning is a distributed transaction with no rollback, so it is a Temporal workflow.** Allocate a VPC/VNet and subnets, create a cluster, install the base addons, register with the traffic layer, mark ready. Each step is an activity with its own retry policy and timeout; the workflow is the deterministic spine. The Go skills that matter: `context` propagation into each cloud SDK call so a cancelled provisioning actually stops burning API quota; disciplined error wrapping so `errors.As` at the top can distinguish a quota error (wait and retry) from a validation error (fail fast); and the workflow-determinism rules, which are a hard constraint rather than a style preference.

**The cloud provider abstraction is exactly the "accept interfaces, return structs" case.** You need one `NetworkProvisioner` interface with `AWSProvisioner`, `GCPProvisioner`, `AzureProvisioner` implementations. Define the interface in the consuming package (the workflow/activity layer), keep the implementations returning concrete structs, and keep the interface as small as the consumer needs. Build tags are the wrong tool here — you want all three compiled into one binary and selected at runtime, not three binaries.

**Networking is shared ownership, which means shared Go types and a shared vocabulary of errors.** "You can't bring up a cell without networking" translates directly into: the cell workflow calls into the networking team's API, and the interesting failures are ordering failures. The concrete Go implications are sentinel or typed errors at the boundary (`ErrCIDRExhausted`, `*PeeringPendingError`) so the caller can distinguish "retry in 30s" from "this cell can never be provisioned in this VPC," and gRPC deadline propagation so a cell-provisioning timeout actually cancels the in-flight subnet allocation instead of orphaning it. Orphaned network resources are the most expensive kind of leak in this domain — they cost money and they collide with future allocations.

**Upgrades are level-triggered reconciliation.** Whatever drives cell upgrades — a controller, a Temporal workflow, or both — the design is the same: desired version vs. observed version, converge, be idempotent, tolerate being interrupted at every step. If it is a controller, everything in the `controller-runtime` section applies directly: watch what matters, `RequeueAfter` for polling, finalizers for ordered teardown, and server-side apply with a stable `FieldOwner` so you do not fight other controllers over the same fields.

**Helm for templating only, no release state, is a Go decision with consequences.** You are rendering manifests (`helm template` or the Helm Go SDK) and applying them yourself. That means you own what Helm's release state would otherwise have given you: knowing what you applied last time, pruning what is no longer in the desired set, and ordering. In Go terms this is `os/exec` discipline (context-aware, output-captured, process-group-aware) or the Helm Go libraries plus `client.Apply` with a field owner, and it is where your "what does the cluster actually have" reads run into the informer cache staleness gotcha.

**Teardown is where the finalizer and goroutine-leak material earns its keep.** Deleting a cell means deleting cloud resources across namespace and cluster boundaries where owner references cannot reach. That is a finalizer plus explicit deletion, which means you must handle: the cloud API being down, the finalizer wedging deletion indefinitely, and partial teardown leaving orphans. `context.WithoutCancel` plus a fresh bounded timeout is the right shape for "the parent operation was cancelled but this cleanup still has to run and still has to finish."

**Reading Temporal server code is a real part of the job.** When a cell's Temporal deployment misbehaves, you will be in `go.temporal.io/server`. It is a large Go codebase using `fx` for dependency wiring (so constructors are registered, not called — trace `fx.Provide` to find what actually builds a component), `zap` for logging, `tally` for metrics, and gRPC everywhere. Being fluent in Go's interface and embedding idioms is the difference between navigating that in an hour and navigating it in a day.

**Operationally, the runtime settings are the ones that will page you.** `GOMEMLIMIT` derived from the pod memory limit, `GOMAXPROCS` correct for your limits-vs-requests convention, `pprof` bound to localhost on every binary, `runtime/metrics` scheduler metrics exported, and the goroutine leak profile available. All five are ten-line changes that pay for themselves the first time a cell's control plane misbehaves at 3am.

---

## Learning path

### Day 1 (about 4 hours)

- Take the [Tour of Go](https://go.dev/tour/), skipping the parts you already understand from other languages. Do not skip: methods and interfaces, and the concurrency section.
- Read [Effective Go](https://go.dev/doc/effective_go) end to end. It is long but it is the document that makes Go *click*; everything else assumes it.
- Read [Go Code Review Comments](https://go.dev/wiki/CodeReviewComments) — 20 minutes, and it is the list of comments you would otherwise receive on your first PR.
- Do steps 1-4 of the Hands-on lab: module, worker pool with `errgroup`, find a race with `-race`, table-driven tests.
- Read the [`context` package docs](https://pkg.go.dev/context) and the [Go blog post on context](https://go.dev/blog/context) carefully. This is the highest-leverage 30 minutes on this list.

### Week 1

- Finish the Hands-on lab (static binaries, containers, `GOMAXPROCS`, escape analysis, pprof, `synctest`, lint).
- Read [Working with Errors in Go 1.13](https://go.dev/blog/go1.13-errors) and then go find the error handling conventions in your repo. Do they use sentinels? Typed errors? Do they wrap?
- Read [Go Concurrency Patterns: Pipelines and cancellation](https://go.dev/blog/pipelines) and [Go Wiki: Use a sync.Mutex or a channel?](https://go.dev/wiki/MutexOrChannel).
- Read the [Kubebuilder Book](https://book.kubebuilder.io/) through the "Groups, Versions, Kinds" and controller chapters, then read one real operator end to end. [cert-manager](https://github.com/cert-manager/cert-manager) or [Karpenter](https://github.com/kubernetes-sigs/karpenter) are both good, and both are probably in your stack.
- Do the [Temporal Go SDK quickstart](https://docs.temporal.io/develop/go/set-up-your-local-go) and run one workflow with one activity locally. Then read [Temporal Go SDK multithreading](https://docs.temporal.io/develop/go/best-practices/multithreading) to internalize the determinism rules.
- Read the [GC guide](https://go.dev/doc/gc-guide) sections on `GOGC` and the memory limit, and the [container-aware GOMAXPROCS blog post](https://go.dev/blog/container-aware-gomaxprocs). Then go find out what your team's pods actually set.
- Pick a small real task in the cell lifecycle codebase and ship it. Reading Go is fast; the fluency comes from getting a PR reviewed.

### Month 1

- Read the Temporal server's `go.mod` and then trace one request path end to end through the frontend service. Understanding `fx` wiring will unlock the rest of the codebase.
- Read [Google's Go Style Decisions](https://google.github.io/styleguide/go/decisions) and the [Uber Go Style Guide](https://github.com/uber-go/guide/blob/master/style.md). By now you have opinions; these will sharpen or change them.
- Read [When to Use Generics](https://go.dev/blog/when-generics) and the [Go memory model](https://go.dev/ref/mem). The memory model is short, formal, and worth knowing precisely rather than approximately.
- Profile something real. Take a CPU profile and a heap profile of a control-plane component under load, and explain the top three entries in each. Use [Profiling Go Programs](https://go.dev/blog/pprof) and [Diagnostics](https://go.dev/doc/diagnostics) as the manual.
- Write one controller (or one non-trivial Temporal workflow) from scratch, including tests. Writing is where you discover which parts you only thought you understood.
- Skim the release notes for [Go 1.25](https://go.dev/doc/go1.25), [1.26](https://go.dev/doc/go1.26), and [1.27](https://go.dev/doc/go1.27). Knowing what changed recently is how you avoid confidently asserting things that stopped being true two releases ago.
- Set up your own `.golangci.yml` and run it against the codebase you work in. The disagreements between what it flags and what the team accepts will teach you the local conventions faster than asking.

---

## References

1. [Go 1.27 Release Notes — The Go Programming Language](https://go.dev/doc/go1.27) — Current release (August 2026): generic methods, `encoding/json` backed by v2, goroutine leak profile GA.
2. [Go 1.26 Release Notes — The Go Programming Language](https://go.dev/doc/go1.26) — Green Tea GC by default, revamped `go fix` modernizers, `errors.AsType`, new scheduler metrics.
3. [Go 1.25 Release Notes — The Go Programming Language](https://go.dev/doc/go1.25) — Container-aware `GOMAXPROCS`, `testing/synctest` GA, `sync.WaitGroup.Go`, trace flight recorder.
4. [Release History — The Go Programming Language](https://go.dev/doc/devel/release) — Authoritative list of every release and its date; check here before quoting a version.
5. [Effective Go — The Go Programming Language](https://go.dev/doc/effective_go) — The canonical "how to think in Go" document; read it once end to end.
6. [Go Code Review Comments — Go Wiki](https://go.dev/wiki/CodeReviewComments) — The checklist of comments Go reviewers actually leave; 20 minutes well spent.
7. [Google Go Style Guide](https://google.github.io/styleguide/go/) — Three-tier style guidance; the "Decisions" document is the practically useful one.
8. [Uber Go Style Guide](https://github.com/uber-go/guide/blob/master/style.md) — More opinionated than Google's; widely mirrored across the CNCF ecosystem.
9. [The Go Memory Model](https://go.dev/ref/mem) — Short and formal; the precise rules for when one goroutine's writes are visible to another.
10. [The Go Programming Language Specification](https://go.dev/ref/spec) — The language reference; surprisingly readable, and the tiebreaker in arguments.
11. [Go Modules Reference](https://go.dev/ref/mod) — MVS, `go.mod` directives, vendoring, the module proxy, `go.sum` semantics.
12. [Tutorial: Getting started with multi-module workspaces](https://go.dev/doc/tutorial/workspaces) — How `go.work` works and when to use it instead of `replace`.
13. [Command go — build constraints](https://pkg.go.dev/cmd/go#hdr-Build_constraints) — `//go:build` syntax and the implicit filename-suffix constraints.
14. [context package — Go Packages](https://pkg.go.dev/context) — The full API including `WithCancelCause`, `WithoutCancel`, `AfterFunc`, and `Cause`.
15. [Go Concurrency Patterns: Context — Go Blog](https://go.dev/blog/context) — The original motivation and design of `context`; still the clearest explanation.
16. [Go Concurrency Patterns: Pipelines and cancellation — Go Blog](https://go.dev/blog/pipelines) — Fan-in/fan-out, explicit cancellation, and how to avoid goroutine leaks in pipelines.
17. [Working with Errors in Go 1.13 — Go Blog](https://go.dev/blog/go1.13-errors) — Wrapping with `%w`, `errors.Is`, `errors.As`, and when to use each.
18. [errors package — Go Packages](https://pkg.go.dev/errors) — Including `errors.Join` and the Go 1.26 generic `errors.AsType`.
19. [Go Wiki: Use a sync.Mutex or a channel?](https://go.dev/wiki/MutexOrChannel) — The official answer to "when not to use channels," with a decision table.
20. [Scalable Go Scheduler Design Doc — Dmitry Vyukov](https://golang.org/s/go11sched) — The original G-M-P design document; the authoritative model for how goroutines get scheduled.
21. [runtime/proc.go — golang/go on GitHub](https://github.com/golang/go/blob/master/src/runtime/proc.go) — The scheduler source; the top-of-file comment block is the up-to-date design doc.
22. [Container-aware GOMAXPROCS — Go Blog](https://go.dev/blog/container-aware-gomaxprocs) — Why CFS throttling wrecks tail latency, and exactly what Go 1.25 changed (and did not).
23. [runtime package — Go Packages](https://pkg.go.dev/runtime) — `GOMAXPROCS`, `SetDefaultGOMAXPROCS`, `NumCPU`, and the full GODEBUG environment variable list.
24. [A Guide to the Go Garbage Collector](https://go.dev/doc/gc-guide) — `GOGC`, `GOMEMLIMIT`, the 50% GC CPU cap, and what the memory limit does and does not cover.
25. [Proposal: Soft memory limit — golang/proposal](https://github.com/golang/proposal/blob/master/design/48409-soft-memory-limit.md) — Design rationale for `GOMEMLIMIT`, including the death-spiral protection.
26. [runtime/debug — SetMemoryLimit](https://pkg.go.dev/runtime/debug#SetMemoryLimit) — Programmatic control of the soft memory limit.
27. [GODEBUG History — The Go Programming Language](https://go.dev/doc/godebug) — How the `go` line in `go.mod` gates runtime behavior changes; essential before any toolchain bump.
28. [Diagnostics — The Go Programming Language](https://go.dev/doc/diagnostics) — Index of profiling, tracing, debugging, and runtime-statistics tooling.
29. [Profiling Go Programs — Go Blog](https://go.dev/blog/pprof) — Worked example of taking a CPU profile and acting on it.
30. [net/http/pprof — Go Packages](https://pkg.go.dev/net/http/pprof) — The HTTP profiling endpoints, including the new `goroutineleak` endpoint.
31. [runtime/trace — Go Packages](https://pkg.go.dev/runtime/trace) — The execution tracer and the Go 1.25 `FlightRecorder` ring-buffer API.
32. [Data Race Detector — The Go Programming Language](https://go.dev/doc/articles/race_detector) — How `-race` works and its documented 5-10x memory / 2-20x time cost.
33. [Go Fuzzing — The Go Programming Language](https://go.dev/doc/security/fuzz/) — Native coverage-guided fuzzing; ideal for parsers and CIDR/selector logic.
34. [testing package — Go Packages](https://pkg.go.dev/testing) — `t.Run`, `t.Parallel`, `t.Cleanup`, `B.Loop`, and the Go 1.26 artifact directory.
35. [testing/synctest — Go Packages](https://pkg.go.dev/testing/synctest) — Virtualized time in a goroutine bubble; makes timeout and backoff tests instant and deterministic.
36. [Testing Time (and other asynchronicities) — Go Blog](https://go.dev/blog/synctest) — The motivation and usage patterns for `synctest`.
37. [log/slog — Go Packages](https://pkg.go.dev/log/slog) — Structured logging in the standard library, plus the Go 1.26 `NewMultiHandler`.
38. [Structured Logging with slog — Go Blog](https://go.dev/blog/slog) — Design of `slog`, handlers, and how to write a custom one.
39. [An Introduction to Generics — Go Blog](https://go.dev/blog/intro-generics) — Type parameters, constraints, and type sets, from the people who designed them.
40. [When to Use Generics — Go Blog](https://go.dev/blog/when-generics) — The official guidance on when *not* to reach for a type parameter.
41. [Type Parameters Proposal — golang/proposal](https://go.googlesource.com/proposal/+/refs/heads/master/design/43651-type-parameters.md) — Full design, including the reasoning behind the restrictions that still apply.
42. [golang.org/x/sync/errgroup — Go Packages](https://pkg.go.dev/golang.org/x/sync/errgroup) — Bounded concurrent groups with cancel-on-first-error; replaces most hand-rolled `WaitGroup` code.
43. [sigs.k8s.io/controller-runtime — Go Packages](https://pkg.go.dev/sigs.k8s.io/controller-runtime) — Manager, client, cache, builder; the library every modern operator is built on.
44. [reconcile package — controller-runtime](https://pkg.go.dev/sigs.k8s.io/controller-runtime/pkg/reconcile) — `Result` semantics, including the deprecation of `Result.Requeue`.
45. [The Kubebuilder Book](https://book.kubebuilder.io/) — The best prose explanation of controller architecture, caching, and CRD design.
46. [client-go under the hood — kubernetes/sample-controller](https://github.com/kubernetes/sample-controller/blob/master/docs/controller-client-go.md) — Reflector, DeltaFIFO, Informer, Indexer, workqueue, with the canonical diagram.
47. [k8s.io/client-go/util/workqueue — Go Packages](https://pkg.go.dev/k8s.io/client-go/util/workqueue) — Deduplicating, rate-limited work queues; why reconcile keys coalesce.
48. [Owners and Dependents — Kubernetes Documentation](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/) — Owner reference rules, including the cross-namespace restriction that forces finalizers.
49. [Managing Resources for Containers — Kubernetes Documentation](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#how-pods-with-resource-limits-are-run) — How CPU limits become cgroup bandwidth limits and cause throttling.
50. [Go SDK developer guide — Temporal Documentation](https://docs.temporal.io/develop/go) — Entry point for workflows, activities, workers, testing, and versioning in Go.
51. [Temporal Go SDK multithreading — Temporal Documentation](https://docs.temporal.io/develop/go/best-practices/multithreading) — Why `workflow.Go` exists, the deterministic runner, and the `workflowcheck` analyzer.
52. [temporalio/sdk-go — GitHub](https://github.com/temporalio/sdk-go) — The SDK source; `contrib/tools/workflowcheck` is worth wiring into CI.
53. [temporalio/temporal go.mod — GitHub](https://github.com/temporalio/temporal/blob/main/go.mod) — The server's Go version and dependency set (`fx`, `zap`, `tally`, `gocql`, `pgx`, gRPC); good orientation before reading the code.
54. [golangci-lint: Linters](https://golangci-lint.run/docs/linters/) — Full catalog; `bodyclose`, `contextcheck`, `containedctx`, `noctx`, and `errorlint` are the high-value ones for infra Go.
55. [golangci-lint: Configuration File](https://golangci-lint.run/docs/configuration/file/) — The v2 schema, including `linters.default` which replaced `enable-all`/`disable-all`.
56. [google/go-cmp — Go Packages](https://pkg.go.dev/github.com/google/go-cmp/cmp) — Struct diffing for tests; `cmp.Diff` output beats any assertion library's message.
57. [stretchr/testify — GitHub](https://github.com/stretchr/testify) — `assert`/`require`/`mock`; ubiquitous in CNCF and in Temporal server, so you will read it regardless of preference.
58. [go.uber.org/mock — GitHub](https://github.com/uber-go/mock) — The maintained gomock fork used by Temporal server for generated mocks.
59. [Go FAQ: Why is my nil error value not equal to nil?](https://go.dev/doc/faq#nil_error) — The canonical explanation of the typed-nil-in-an-interface trap.
60. [gRPC over HTTP/2 protocol specification — grpc/grpc](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md) — How `grpc-timeout` carries a Go context deadline across the wire.
