# CNI and Host-Level Networking, From the Kernel Up

**Why this matters.** If you own cell lifecycle across AWS, GCP, and Azure, you cannot bring up a cell without networking — which is why this layer is often shared with a dedicated networking team. Every cell provision starts with an IP plan (pod CIDR, service CIDR, node subnet) that is effectively immutable once workloads land on it; every cell upgrade risks a datapath regression that manifests as "some pods can't reach some services"; every cell teardown leaks cloud network objects if you don't understand what the CNI created on your behalf. The three clouds disagree fundamentally about where a pod IP comes from, so "the CNI" is not one thing you can abstract behind Helm templating — it is three different IPAM models with three different exhaustion failure modes. This guide is meant to get you from "I know TCP/IP and routing" to "I can `nsenter` into a pod's namespace at 3am and tell you which of the six hops dropped the packet."

---

## The mental model

Forget Kubernetes for a second. A "container network" is three primitives glued together:

1. A **network namespace** — a private copy of the kernel's entire network stack: interfaces, routing tables, ARP/neighbour table, netfilter rules, conntrack table, and `/proc/sys/net`.
2. A **virtual wire** out of that namespace — almost always a **veth pair**, which is a two-ended virtual Ethernet cable where one end lives inside the pod and the other lives on the host.
3. A **decision on the host** about what to do with packets arriving on that wire — bridge them, route them, encapsulate them, or hand them to eBPF.

Everything else — CNI, kube-proxy, Cilium, the AWS VPC CNI — is automation and policy on top of those three things.

Now trace one packet, twice. Same scenario both times:

- Pod A `10.244.1.5` on node1 `192.168.0.11`
- Pod B `10.244.2.7` on node2 `192.168.0.12`
- Service `payments` with ClusterIP `10.96.0.42:8080`, backed by pod B on port 8080
- Pod A runs `curl http://payments:8080`

### Trace 1: overlay (VXLAN), kube-proxy in iptables mode

1. **DNS first.** `payments` has 0 dots, `ndots:5` is set, so glibc appends search domains: it queries `payments.default.svc.cluster.local` first. That query is itself a UDP packet to the ClusterIP of `kube-dns` (`10.96.0.10:53`) and goes through steps 2–9 below before the HTTP request even starts. Remember this — DNS is not a shortcut past the datapath, it is a full trip through it.
2. **Socket to wire.** The app calls `connect()`. Inside pod A's netns, the routing table says `default via 10.244.1.1 dev eth0`. `eth0` is one end of a veth pair. Source IP `10.244.1.5`, dest `10.96.0.42`.
3. **OUTPUT / netfilter inside the pod netns.** ClusterIP DNAT does *not* happen here — kube-proxy programs rules in the **host** netns. The packet leaves the pod unchanged, addressed to a ClusterIP that exists nowhere on any wire.
4. **Host end of veth.** The peer interface (`veth1a2b3c`, or `caliXXXX` for Calico, `lxcXXXX` for Cilium) receives the frame. Because the packet is not addressed to the host, it hits the host's forwarding path: `PREROUTING` in the `nat` and `mangle` tables, then the routing decision, then `FORWARD` in `filter`.
5. **DNAT.** In `nat/PREROUTING`, kube-proxy's `KUBE-SERVICES` chain matches `-d 10.96.0.42/32 -p tcp --dport 8080` and jumps to `KUBE-SVC-<hash>`. That chain picks a backend with `-m statistic --mode random --probability 0.5`-style rules and jumps to `KUBE-SEP-<hash>`, which does `DNAT --to-destination 10.244.2.7:8080`. **conntrack records this translation** so the reply can be un-DNAT'd. Destination is now a real pod IP.
6. **Routing decision, take two.** The host's main table has `10.244.2.0/24 via <vtep> dev flannel.1` (or `vxlan.calico`). The kernel routes the packet to a **VXLAN device**.
7. **Encapsulation.** The VXLAN driver looks up the remote VTEP for that pod CIDR (`bridge fdb show dev flannel.1`), wraps the original frame in `outer IP (192.168.0.11 → 192.168.0.12) + UDP (dport 8472 or 4789) + VXLAN header (VNI)`. This adds **50 bytes** for VXLAN over IPv4. The overlay device's MTU must therefore be 1450 if the underlay is 1500 — this is where MTU bugs are born.
8. **Underlay transit.** The outer packet leaves node1's real NIC, crosses the VPC/VNet as ordinary node-to-node traffic. The cloud fabric sees only node IPs; it has no idea pod IPs exist. That is the entire point of an overlay — and also why the cloud load balancer, VPC flow logs, and security groups cannot see pod identity.
9. **Decapsulation on node2.** The kernel matches the UDP dport to the VXLAN socket, strips the outer headers, and injects the inner frame as if it had arrived on `flannel.1`.
10. **To the pod.** Node2 routes `10.244.2.7` out the veth host-end for pod B (either directly via a `/32` route, or via the `cni0` bridge if the CNI uses one). Pod B's netns receives it on `eth0`.
11. **Reply.** Pod B replies to `10.244.1.5` (the *original* source — it never saw the ClusterIP). On node1, conntrack matches the flow and reverses the DNAT, so pod A's socket sees a reply from `10.96.0.42:8080`, which is what it expects.

Cost of the overlay: one extra encap/decap per packet, 50 bytes of MTU, invisible pod IPs in cloud tooling, and total independence from VPC IP space.

### Trace 2: native routing (AWS VPC CNI / GKE alias IPs), kube-proxy in nftables mode

Same pods, but now `10.244.x.x` addresses are **real VPC addresses** that the cloud fabric routes natively.

1. **DNS** — same as before.
2. **Socket to wire** — same. Pod A's `eth0` is a veth end; default route points at a link-local gateway (`169.254.1.1` on AWS, with a `/32` route and proxy ARP on the host end).
3. **Host end of veth.** Under the AWS VPC CNI, the host has a `/32` route for the pod plus an **`ip rule`** entry: `from 10.244.1.5 lookup <table N>`, where table N is the route table for the ENI that owns that IP. This is policy routing doing source-based selection so replies exit the same ENI they arrived on — otherwise the VPC's source/destination checks and the ENI's security groups misbehave.
4. **DNAT, nftables flavour.** Instead of walking a chain of thousands of iptables rules, kube-proxy's nftables backend does a **verdict map lookup**: `ip daddr . meta l4proto . th dport vmap @service-ips`. That is a single hash lookup regardless of how many Services exist. Result: dest rewritten to `10.244.2.7:8080`, conntrack entry created.
5. **Routing — no encapsulation.** The main table (or an ENI table) says the next hop for `10.244.2.7` is the VPC. The packet leaves node1's ENI with **source `10.244.1.5`, dest `10.244.2.7`** — both real VPC IPs. No outer header, no MTU tax, full 9001-byte jumbo frames available inside the VPC.
6. **Cloud fabric routes it.** On AWS the address is a secondary IP on an ENI attached to node2, so the VPC delivers it. On GKE, `10.244.2.0/24` is an **alias IP range** bound to node2's NIC, and the VPC has an implicit route. This is why native routing needs the cloud's cooperation: the fabric must already know that this /24 lives behind that instance.
7. **Node2 delivers** via the pod's `/32` route and veth. Reply path is symmetric, un-DNAT'd by conntrack on node1.

Cost of native routing: every pod burns a routable VPC IP, so **IP exhaustion becomes a cell-sizing constraint**, and pod density per node is capped by cloud limits (ENI slots on AWS, alias range size on GKE). Benefit: no encap, jumbo frames, pod IPs visible to security groups, flow logs, and cloud load balancers.

**The one-sentence version:** overlays trade CPU and MTU for IP-space freedom; native routing trades IP-space freedom for line-rate simplicity and cloud-native visibility. Which one a cell uses is an irreversible day-0 decision.

---

## Core concepts

### Network namespaces

A network namespace is a kernel object (`struct net`) that owns an independent copy of the networking stack. Per [`network_namespaces(7)`](https://man7.org/linux/man-pages/man7/network_namespaces.7.html), each namespace has its own network devices, IPv4/IPv6 stacks, routing tables, firewall rules, `/proc/net`, `/sys/class/net`, port number space, and the `UNIX domain abstract socket namespace`.

Key facts that matter operationally:

- A namespace exists as long as something references it: a process, a file descriptor, or a bind mount. `ip netns add foo` creates `/var/run/netns/foo` as a bind mount so the namespace outlives any process.
- A process's namespace is at `/proc/<pid>/ns/net`. Two processes in the same namespace have the same inode there — that is how you check whether a container is on the host network.
- A **pod is one network namespace** shared by all its containers. It is created and held by the *sandbox* (pause) container so the network survives individual container restarts. This is why `kubectl exec` into any container in a pod shows the same `ip addr`.
- `hostNetwork: true` means the pod's containers run in the **host's** namespace: they see node interfaces, bind node ports directly, and are exempt from most CNI plumbing. CNI daemonsets and kube-proxy run this way.
- `ip netns exec` and `nsenter -t <pid> -n` are the two ways in. `ip netns` only sees namespaces registered under `/var/run/netns`, which containerd does **not** do — so for pods you use `nsenter`, or you bind-mount the namespace yourself first.

Getting into a running pod's namespace from the node:

```bash
# Find the sandbox container for a pod
crictl pods --name my-pod -q
POD_ID=$(crictl pods --name my-pod -q | head -1)
PID=$(crictl inspectp "$POD_ID" | jq -r '.info.pid')

# Now run any host tool inside the pod's network namespace
nsenter -t "$PID" -n ip addr
nsenter -t "$PID" -n ip route
nsenter -t "$PID" -n ss -tnp
nsenter -t "$PID" -n tcpdump -ni eth0 -c 50

# Or register it for ip netns and use the friendlier syntax
mkdir -p /var/run/netns
ln -sf "/proc/$PID/ns/net" "/var/run/netns/mypod"
ip netns exec mypod ip route get 10.96.0.42
```

This is the single most valuable trick in the guide: it lets you use every host debugging tool inside a distroless pod that has no shell, no `curl`, and no `ip`.

### veth pairs, bridges, and how a container actually gets an interface

A **veth pair** ([`veth(4)`](https://man7.org/linux/man-pages/man4/veth.4.html)) is a virtual Ethernet cable: two devices, and a frame written to one comes out of the other. Create both in the host namespace, then move one end into the container's namespace and rename it `eth0`.

There are two dominant host-side topologies:

**Bridged (Flannel, kubenet, Docker's default, most simple CNIs).** All host-side veth ends are enslaved to a Linux bridge (`cni0`, `cbr0`, `docker0`). The bridge holds the pod subnet's gateway IP. Pod-to-pod on the same node is pure L2 switching inside the bridge; off-node traffic is routed by the host.

**Routed / point-to-point (Calico, Cilium, AWS VPC CNI).** No bridge. Each host-side veth end gets a `/32` (or `/128`) route pointing at it, and the pod's default gateway is a fake link-local address answered by **proxy ARP** on the host end. Every packet, even same-node pod-to-pod, is *routed* by the host kernel rather than switched.

Why routed wins in production: no bridge means no MAC learning table to overflow, no spanning-tree-ish behaviour, no bridge-level broadcast domain shared by hundreds of pods, and — critically — **every packet passes through the host's routing and netfilter/eBPF path**, so policy is enforceable uniformly. Bridged setups historically needed `net.bridge.bridge-nf-call-iptables=1` to force bridged frames through netfilter at all, which is a whole class of "my NetworkPolicy silently does nothing" bugs.

Here is the veth handoff a CNI plugin performs, in the order it happens:

```text
1. Runtime creates the pod netns (empty: only a down `lo`).
2. Runtime invokes the CNI plugin with CNI_NETNS=/proc/<pid>/ns/net, CNI_IFNAME=eth0.
3. Plugin creates veth pair in the HOST namespace: veth_h <-> veth_c
4. Plugin moves veth_c into CNI_NETNS and renames it to eth0
5. Inside the netns: `ip link set lo up`, `ip link set eth0 up`,
   `ip addr add <podIP>/32 dev eth0`,
   `ip route add <gw> dev eth0 scope link`,
   `ip route add default via <gw> dev eth0`
6. In the host namespace: `ip link set veth_h up`,
   `ip route add <podIP>/32 dev veth_h`,
   enable proxy_arp on veth_h
7. Plugin prints a CNI Result JSON on stdout and exits 0.
```

Note step 5's `/32` mask. The pod thinks it is alone on a /32 with a link-scope route to a gateway that does not really exist as a host — the host answers ARP for it via `proxy_arp`. This is deliberate: it forces every packet out of the namespace to the host, where policy lives.

### MACVLAN and IPVLAN

Alternatives to veth that skip the extra device hop:

| Property | veth + bridge | veth + routing | MACVLAN | IPVLAN (L2) | IPVLAN (L3) |
|---|---|---|---|---|---|
| Container MAC | own | own | own, unique | shared with parent | shared with parent |
| Needs upstream to learn many MACs | no (bridge is local) | no | **yes** | no | no |
| Host can reach the container | yes | yes | **no** (by default) | yes | yes |
| Broadcast/multicast to container | yes | via routing | yes | yes | no |
| Typical use | dev, Flannel | Calico, Cilium, AWS | bare metal, telco | dense L2, cloud with MAC limits | dense L3 |

The classic MACVLAN gotcha: a MACVLAN child and its parent interface **cannot talk to each other** — the frame never leaves the NIC to be switched back. That breaks kubelet health probes from the node to the pod, which is why MACVLAN is rare as a primary CNI in Kubernetes and common as a **Multus secondary interface** for dataplane-heavy workloads.

Clouds generally forbid MACVLAN for pods: AWS, Azure, and GCP all filter frames whose source MAC is not one they issued. IPVLAN, which reuses the parent's MAC, is viable in more places, and Azure has used IPVLAN-style modes historically. For your cells, assume veth.

### Routing tables and policy routing

Linux does not have "a" routing table; it has up to 2^32 of them, selected by a rule chain. `ip rule` ([`ip-rule(8)`](https://man7.org/linux/man-pages/man8/ip-rule.8.html)) is an ordered list of selectors evaluated by ascending priority; the first matching rule chooses a table, and if that table has no route, evaluation **continues** to the next rule.

Default rules on any Linux box:

```bash
$ ip rule show
0:      from all lookup local
32766:  from all lookup main
32767:  from all lookup default
```

Table `local` (255) holds routes for addresses the host itself owns. Table `main` (254) is what `ip route` shows by default.

Selectors you will meet: `from` (source), `to` (dest), `iif`/`oif`, `fwmark` (set by netfilter or eBPF), `ipproto`, `dport`/`sport`, `uidrange`, and `l3mdev` (VRF).

The AWS VPC CNI is the canonical policy-routing user. On a node with three ENIs you will see something like:

```bash
$ ip rule show
0:      from all lookup local
512:    from all to 10.0.1.23 lookup main       # to-pod: use main table
1024:   from all fwmark 0x80/0x80 lookup main   # marked (SNAT'd) traffic
1536:   from 10.0.1.23 lookup 2                 # from-pod: use ENI-2's table
1536:   from 10.0.1.47 lookup 3                 # from-pod: use ENI-3's table
32766:  from all lookup main

$ ip route show table 2
default via 10.0.0.1 dev eth1
10.0.0.1 dev eth1 scope link
```

Why: a pod whose IP is a secondary address on `eth1` **must** send from `eth1`, or the VPC drops the packet (source/dest check) and the wrong security group applies. Rule priority 512 exists so traffic *destined* to a local pod is delivered via `main` before the from-rules can misroute it.

Debug commands that answer "where would this packet actually go":

```bash
ip route get 10.244.2.7                        # simulate a lookup
ip route get 10.244.2.7 from 10.244.1.5        # with a source, exercises ip rules
ip rule show
ip route show table all | head -50
ip -d link show vxlan.calico                   # -d shows encap details, VNI, port
ip neigh show                                  # ARP/NDP cache
bridge fdb show dev flannel.1                  # VXLAN VTEP forwarding entries
```

### conntrack

Netfilter's connection tracker turns stateless packets into flows. It is mandatory for NAT: to un-DNAT a reply, the kernel must remember the original translation. Entries are keyed by a tuple (proto, src IP/port, dst IP/port) in both directions and stored in a per-namespace hash table.

What you need to know:

- **Table size.** `net.netfilter.nf_conntrack_max` caps entries; `net.netfilter.nf_conntrack_buckets` sizes the hash. Defaults are derived from RAM. **kube-proxy overrides them**: `--conntrack-max-per-core` (default 32768) multiplied by core count, floored at `--conntrack-min` (default 131072). See the [kube-proxy reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-proxy/).
- **Exhaustion is a hard failure.** When the table is full the kernel drops packets and logs `nf_conntrack: table full, dropping packet` to dmesg. Symptom: random connection failures under load, not a clean error.
- **Timeouts matter more than size.** `nf_conntrack_tcp_timeout_established` defaults to 432000 seconds (5 days) in the kernel; kube-proxy lowers it to 24h via `--conntrack-tcp-timeout-established`. UDP entries default to 30s (`nf_conntrack_udp_timeout`) — every DNS query creates one. All documented in [nf_conntrack-sysctl](https://docs.kernel.org/networking/nf_conntrack-sysctl.html).
- **conntrack is per network namespace.** A pod with `hostNetwork: false` has its own table, but the *host's* table is the one under pressure because that is where DNAT happens.

Inspection:

```bash
conntrack -S                       # per-CPU stats: insert_failed, drop, search_restart
conntrack -C                       # current entry count
sysctl net.netfilter.nf_conntrack_max
conntrack -L -p udp --dport 53 | wc -l          # how much of the table is DNS
conntrack -L -d 10.96.0.42                      # flows to a ClusterIP
conntrack -E -p tcp --dport 8080                # live event stream
conntrack -D -d 10.244.2.7                      # delete stale entries for a dead pod
```

`insert_failed` is the number you care about for the DNS race described later. `drop` non-zero plus `table full` in dmesg means you are out of entries.

### MTU and PMTU, and the encapsulation math

Every encapsulation steals bytes from the payload. Get this wrong and you get the worst failure mode in networking: small packets work, TCP handshakes succeed, and then large transfers hang forever. Both `curl` on a health endpoint and `ping` succeed; a 50KB response times out.

| Encapsulation | Overhead vs. underlay | Pod MTU on a 1500 underlay | Pod MTU on a 9001 underlay |
|---|---|---|---|
| None (native routing) | 0 | 1500 | 9001 |
| IPIP (IPv4) | 20 | 1480 | 8981 |
| VXLAN over IPv4 | 50 | 1450 | 8951 |
| VXLAN over IPv6 | 70 | 1430 | 8931 |
| Geneve over IPv4 (no options) | 50 | 1450 | 8951 |
| WireGuard | 80 | 1420 | 8921 |
| IPsec (ESP, varies by cipher) | ~50-80 | ~1420-1450 | ~8921-8951 |

VXLAN's 50 bytes = 20 (outer IPv4) + 8 (UDP) + 8 (VXLAN) + 14 (inner Ethernet). The inner Ethernet header is the part people forget.

**Path MTU Discovery** is the mechanism that is supposed to save you. A router that must forward an oversized packet with `DF` set replies with ICMPv4 `Destination Unreachable / Fragmentation Needed` (type 3, code 4) or ICMPv6 `Packet Too Big`, carrying the correct MTU ([RFC 1191](https://www.rfc-editor.org/rfc/rfc1191)). The sender caches it and shrinks. This breaks whenever a firewall or security group drops ICMP — the sender never learns, retransmits the same oversized packet forever, and you get a **PMTU black hole**.

Mitigations, in order of preference:

1. **Set the pod MTU correctly** at CNI config time so PMTUD is never needed inside the cluster. Cilium auto-detects; Calico has `veth_mtu` in its ConfigMap; the AWS VPC CNI uses `AWS_VPC_ENI_MTU`.
2. **MSS clamping** for traffic leaving the cluster, which rewrites the TCP MSS option in SYNs so both ends negotiate a safe segment size:

   ```bash
   iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN \
     -j TCPMSS --clamp-mss-to-pmtu
   ```

3. **Allow ICMP** type 3 code 4 and ICMPv6 type 2 in every security group and NetworkPolicy. Blocking "ping" almost always means blocking PMTUD too.
4. **PMTUD blackhole detection** (`net.ipv4.tcp_mtu_probing=1`) as a last-resort safety net; it makes TCP probe downward on repeated timeouts rather than hanging.

Cloud specifics worth writing down for your cells: AWS supports 9001-byte jumbo frames within a VPC on supported instance types but **1500 bytes for traffic traversing an internet gateway**, and different limits again for Transit Gateway and peering — verify against the current [AWS MTU documentation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/network_mtu.html) before assuming jumbo end to end. A cell that sets pod MTU to 8951 and then talks to an external API through an IGW will black-hole unless MSS clamping is in place.

### The CNI specification

CNI is deliberately, almost aggressively simple. A **CNI plugin is an executable file**. There is no daemon, no gRPC, no API server registration. The contract, defined in the [CNI specification](https://www.cni.dev/docs/spec/) ([source on GitHub](https://github.com/containernetworking/cni/blob/main/SPEC.md)), is:

- The plugin binary lives in a directory, conventionally `/opt/cni/bin`.
- The runtime sets **environment variables** to describe the operation.
- The runtime writes **network configuration JSON to the plugin's stdin**.
- The plugin writes a **Result JSON to stdout** and exits 0, or writes an Error JSON and exits non-zero.

Environment variables:

| Variable | Meaning |
|---|---|
| `CNI_COMMAND` | `ADD`, `DEL`, `CHECK`, `GC`, `STATUS`, or `VERSION` |
| `CNI_CONTAINERID` | Opaque runtime-assigned ID, must be unique and stable |
| `CNI_NETNS` | Path to the network namespace, e.g. `/proc/12345/ns/net` |
| `CNI_IFNAME` | Interface name to create inside the namespace, e.g. `eth0` |
| `CNI_ARGS` | Extra `KEY=VALUE;KEY=VALUE` pairs; Kubernetes passes pod name/namespace here |
| `CNI_PATH` | Colon-separated list of directories to search for plugins |

The operations, per the spec:

- **ADD** — create the interface in `CNI_NETNS`, or adjust an existing one. Must be idempotent enough that a retried ADD does not corrupt state.
- **DEL** — release everything ADD allocated. **Must succeed if the resource is already gone** — DEL is called on cleanup paths where the namespace may no longer exist, and a failing DEL leaks IPs forever.
- **CHECK** — verify the attachment is still healthy (added in spec v1.0.0).
- **GC** — the runtime supplies the set of known-good attachments and the plugin cleans up everything else. This is the fix for leaked IPAM reservations after a node crash. Added in **spec v1.1.0**.
- **STATUS** — ask the plugin whether it is ready to accept ADDs, so the runtime can avoid dooming pod creation during plugin startup. Also **v1.1.0**.
- **VERSION** — report supported spec versions.

v1.1.0 is the current spec version; the [spec upgrade notes](https://www.cni.dev/docs/spec-upgrades/) document what changed between versions and which `cniVersion` values a plugin must accept.

**Network configuration** lives in `/etc/cni/net.d/`. Files are read in lexical order and, for Kubernetes, **the first valid one wins** — which is why every CNI installer names its file `10-calico.conflist`, `05-cilium.conflist`, `10-aws.conflist` and why installing two CNIs produces a coin-flip. A `.conflist` describes a chain:

```json
{
  "cniVersion": "1.0.0",
  "name": "cell-net",
  "plugins": [
    {
      "type": "ptp",
      "ipMasq": false,
      "mtu": 1450,
      "ipam": {
        "type": "host-local",
        "ranges": [[{ "subnet": "10.244.1.0/24" }]],
        "routes": [{ "dst": "0.0.0.0/0" }]
      }
    },
    { "type": "portmap", "capabilities": { "portMappings": true } },
    { "type": "bandwidth", "capabilities": { "bandwidth": true } },
    { "type": "firewall", "backend": "iptables" }
  ]
}
```

**Chaining** works by passing each plugin's Result to the next as a `prevResult` field on stdin. The first plugin creates the interface; downstream plugins decorate it. The reference chained plugins in [containernetworking/plugins](https://github.com/containernetworking/plugins) are worth knowing by name: `portmap` (implements `hostPort`), `bandwidth` (implements the `kubernetes.io/ingress-bandwidth` annotations via tc), `firewall`, `tuning` (sets sysctls on the pod interface), `sbr` (source-based routing), and `bridge`/`ptp`/`macvlan`/`ipvlan`/`host-device` as main plugins.

**IPAM plugins** are invoked by the main plugin, not by the runtime, and follow the same exec contract. The reference implementations are `host-local` (allocations stored as files under `/var/lib/cni/networks/<network>/<ip>`, containing the container ID), `static`, and `dhcp`. Real CNIs ship their own: `calico-ipam` (allocates /26 blocks per node with affinity), the AWS VPC CNI's `ipamd`, Cilium's cluster-scope or CRD-backed IPAM. **`host-local` is node-local state on disk**, which is exactly why a node that loses `/var/lib/cni` reallocates IPs that are still in use, and why the GC verb exists.

### How kubelet actually invokes CNI

This trips people up because kubelet has not called CNI directly since dockershim was removed in v1.24. The real chain:

```text
kubelet
  └─ CRI RunPodSandbox  (gRPC over /run/containerd/containerd.sock)
       └─ containerd's CRI plugin
            └─ libcni (github.com/containernetworking/cni/libcni)
                 └─ fork/exec /opt/cni/bin/<type>  with stdin JSON + CNI_* env
```

containerd's relevant config, in `/etc/containerd/config.toml`:

```toml
[plugins."io.containerd.grpc.v1.cri".cni]
  bin_dir = "/opt/cni/bin"
  conf_dir = "/etc/cni/net.d"
  max_conf_num = 1          # only the first conflist is used
  conf_template = ""
```

Consequences you will hit during cell bootstrap:

- Until a valid conflist exists in `conf_dir`, the node reports `NotReady` with `container runtime network not ready: cni plugin not initialized`. This is **normal** for the window between node join and CNI daemonset rollout.
- The CNI daemonset must therefore run with `hostNetwork: true` and tolerate the `node.kubernetes.io/not-ready` taint — it is bootstrapping the very thing it needs. This chicken-and-egg is the number one cause of a cell that provisions but never converges.
- `max_conf_num = 1` means a stale `10-flannel.conflist` left behind by a previous install shadows your new CNI. Cell teardown/rebuild must clean `/etc/cni/net.d` and `/opt/cni/bin`.
- CNI plugin invocation is a **fork/exec per pod sandbox**. On a node churning thousands of pods, plugin process startup time is a real component of pod startup latency.

### The Kubernetes network model contract

The [cluster networking docs](https://kubernetes.io/docs/concepts/cluster-administration/networking/) state the requirements bluntly. Every implementation must satisfy:

1. Pods can communicate with all other pods on any node **without NAT**.
2. Agents on a node (kubelet, system daemons) can communicate with all pods on that node.
3. Pods in the host network of a node can communicate with all pods on all nodes without NAT.
4. Every pod has its own IP address, and that is the address other pods see.

Read requirement 1 again, because it is the constraint that shapes every design decision downstream:

- **No NAT pod-to-pod** means the source IP a receiver sees is the sender's real pod IP. That is what makes NetworkPolicy, mTLS identity, and audit logging tractable. It also means the CNI's IPAM must guarantee **cluster-wide uniqueness** — you cannot hand out overlapping per-node ranges.
- **Every pod routable** means either the underlay knows your pod CIDRs (native routing, alias IPs, BGP) or you build an overlay. There is no third option.
- **Node can reach all pods** kills designs where pods hide behind a per-node NAT — which is exactly why kubenet-style NAT models are legacy.
- Nothing in the contract mentions Services. **Services are not part of the network model**; they are a separate layer implemented by kube-proxy or a CNI's replacement for it. A cluster with a working CNI and a broken kube-proxy has perfect pod-to-pod connectivity and zero working ClusterIPs — a very common and very confusing failure signature.

Note also what the contract does *not* promise: no ordering guarantees, no bandwidth guarantees, no isolation by default (all pods can reach all pods until a NetworkPolicy says otherwise), and nothing about the node's own traffic.

### kube-proxy modes

kube-proxy watches Services and EndpointSlices and programs the node's datapath so that ClusterIPs work. It is a **control-plane component that programs the kernel** — it is not on the datapath itself, so killing kube-proxy does not immediately break traffic, it freezes the rules in place.

| Mode | Mechanism | Lookup cost | Rule update cost | Status (as of K8s v1.37) |
|---|---|---|---|---|
| `iptables` | netfilter chains in the `nat` table | O(services × endpoints) linear chain traversal | Full or partial `iptables-restore` | Still the **default**, but v1.37 warns when the mode is defaulted rather than explicit |
| `ipvs` | Kernel IPVS L4 load balancer + ipset | O(1) hash | Incremental | **Deprecated**; warning since v1.35 |
| `nftables` | nftables verdict maps | O(1) hash | Incremental, transactional | **GA since v1.33**, recommended for Linux |
| `winkernel` | Windows HNS/VFP | n/a | n/a | Windows nodes only |
| none | CNI replaces it (Cilium, Calico eBPF) | O(1) eBPF map | Incremental | Increasingly the default on managed platforms |

The transition timeline is unusually well documented, and you should plan cell upgrades against it. From [KEP-5343 (make nftables the default)](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/5343-nftables-to-default):

- **v1.37**: kube-proxy warns via logs and events when the mode is *defaulted* to `iptables` rather than explicitly set.
- **v1.39**: beta stage; kernel version expectations tightened.
- **v1.40**: the default flips to `nftables`, with an action-required release note.

From [KEP-5495 (deprecate IPVS mode)](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/5495-deprecate-ipvs-mode-in-kube-proxy/README.md):

- **v1.35**: docs updated, kube-proxy prints a warning in `ipvs` mode.
- **v1.37**: a `KubeProxyIPVS` feature gate is added, `Default: true`.
- **v1.40**: feature gate flips to `Default: false` — running `ipvs` without overriding the gate causes kube-proxy to exit with an error.
- **v1.43**: `pkg/proxy/ipvs` is removed entirely.
- **v1.46**: feature gate removed.

The single most important action item from those two KEPs: **set `mode` explicitly in your kube-proxy config today**, in every cell, on every cloud. If you do not, your cells silently change datapath on the v1.40 upgrade. The KEP is explicit that a node on a kernel too old for nftables (below 5.13) that has not pinned `mode: iptables` will have kube-proxy fail to start and the node will break.

nftables requires **kernel 5.13 or newer**. The KEP notes that 5.10 and 5.15 leave upstream LTS at the end of 2026, so by the time the default flips this is expected to be a non-issue for mainstream distros — but if your cells run on a pinned older AMI or a vendor kernel, verify.

### What the iptables chains actually look like

You need to be able to read these at 3am. The `nat` table is where Service translation happens.

```bash
iptables-save -t nat | less
# or, targeted:
iptables -t nat -L KUBE-SERVICES -n --line-numbers
```

The structure, top down:

```text
PREROUTING  -> KUBE-SERVICES        (traffic arriving at the node)
OUTPUT      -> KUBE-SERVICES        (traffic originating on the node)
POSTROUTING -> KUBE-POSTROUTING     (last stop before egress; does MASQUERADE)

KUBE-SERVICES
  -d 10.96.0.42/32 -p tcp --dport 8080 -j KUBE-SVC-<hash-of-service>
  -d 10.96.0.10/32 -p udp --dport 53   -j KUBE-SVC-<hash>
  ... one rule per (Service, port) ...
  -m addrtype --dst-type LOCAL -j KUBE-NODEPORTS      # always last

KUBE-SVC-<hash>                                      # the load balancer
  ! -s 10.244.0.0/16 -d 10.96.0.42/32 -j KUBE-MARK-MASQ   # off-cluster src -> SNAT
  -m statistic --mode random --probability 0.33333 -j KUBE-SEP-<ep1>
  -m statistic --mode random --probability 0.50000 -j KUBE-SEP-<ep2>
  -j KUBE-SEP-<ep3>

KUBE-SEP-<ep1>                                       # a single endpoint
  -s 10.244.2.7/32 -j KUBE-MARK-MASQ                 # hairpin: pod talking to itself
  -p tcp -m tcp -j DNAT --to-destination 10.244.2.7:8080

KUBE-MARK-MASQ
  -j MARK --or-mark 0x4000

KUBE-POSTROUTING
  -m mark ! --mark 0x4000/0x4000 -j RETURN
  -j MARK --xor-mark 0x4000
  -j MASQUERADE --random-fully
```

Three things to internalise from that listing:

1. **The probabilities are sequential, not absolute.** With three endpoints the rules are 1/3, then 1/2 of the remainder, then the fallthrough. That yields a uniform 1/3 each. If you see `0.25000` on the first rule you have four endpoints. This is how you count endpoints without touching the API server.
2. **`0x4000` is the masquerade mark.** Anything marked gets SNAT'd to the node IP in `POSTROUTING`. It is set for traffic entering from outside the pod CIDR and for hairpin traffic. This is why `externalTrafficPolicy: Cluster` loses the client source IP.
3. **`--random-fully`** on the MASQUERADE randomises source port selection fully rather than picking sequentially, which materially reduces the conntrack insert collisions behind the DNS 5-second timeout. kube-proxy sets this when the iptables binary supports it.

Additional chains you will see: `KUBE-NODEPORTS` (matches `--dport` on node-local addresses), `KUBE-EXT-<hash>` (external traffic entry for a Service, where `externalTrafficPolicy` is enforced), `KUBE-FW-<hash>` (LoadBalancer ingress IPs, `loadBalancerSourceRanges`), `KUBE-MARK-DROP`, and in the `filter` table `KUBE-FORWARD` and `KUBE-PROXY-FIREWALL`.

**The scaling problem.** Chain traversal is linear. Every packet to a ClusterIP walks `KUBE-SERVICES` rule by rule until it matches. With 5,000 Services and 5 endpoints each you have on the order of tens of thousands of rules, and — worse historically — kube-proxy rewrote large portions of the table on every change, so a single endpoint flap could take seconds of CPU and stall all rule updates behind it. [KEP-3866](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/3866-nftables-proxy/README.md) documents this motivation in detail and is the best primary source on why iptables mode does not scale.

The nftables backend replaces that structure with **verdict maps**:

```bash
nft list table ip kube-proxy | head -60
```

You will see maps like `service-ips` and `service-nodeports` keyed by `ip daddr . meta l4proto . th dport`, with a single `vmap` lookup replacing the whole linear scan. Updates are incremental and applied in a kernel transaction, so a partial failure does not leave a half-programmed datapath. In v1.37 kube-proxy also switched to reading nftables state via **netlink directly instead of shelling out to the `nft` binary** for list operations, and started truncating rule comments to the kernel's 128-byte limit to avoid sync failures on long Service names — both per the [v1.37 release notes](https://kubernetes.io/blog/2026/08/26/kubernetes-v1-37-release/).

IPVS mode, for completeness: it creates a dummy interface `kube-ipvs0` holding every ClusterIP, programs IPVS virtual servers via netlink, and still uses iptables plus `ipset` for masquerading and NodePort matching. Its schedulers (`rr`, `wrr`, `lc`, `sh`) are the reason many people chose it; KEP-5495 points out that these are mostly not useful in Kubernetes because kube-proxy has no visibility into backend load. Do not start new cells on it.

### Service types and datapath

| Type | What is allocated | Reachable from | Datapath note |
|---|---|---|---|
| `ClusterIP` | A VIP from the service CIDR | Inside the cluster | The IP exists **only** in kube-proxy rules; nothing ARPs for it, nothing owns it |
| `NodePort` | ClusterIP + a port on every node (default range 30000-32767) | Anywhere that can reach a node | Superset of ClusterIP; `KUBE-NODEPORTS` chain |
| `LoadBalancer` | NodePort + a cloud LB provisioned by the cloud controller manager | Internet or internal VIP | Cloud-specific: NLB/ALB, GCLB, Azure LB |
| `ExternalName` | Nothing | n/a | Pure CoreDNS CNAME; no proxying, no VIP |
| Headless (`clusterIP: None`) | Nothing | Inside the cluster | DNS returns one A/AAAA record per ready pod; client does its own LB |

Headless deserves emphasis because it is what stateful systems actually use. With `clusterIP: None`, CoreDNS returns the full set of ready pod IPs for the Service name, and StatefulSet pods additionally get stable per-pod names `<pod>.<svc>.<ns>.svc.cluster.local`. gRPC clients, database drivers, and anything doing client-side load balancing or leader discovery want this — a ClusterIP would pin every connection to one backend for the connection's lifetime, since L4 load balancing happens once at connect time.

**`externalTrafficPolicy`** applies to traffic entering via NodePort or LoadBalancer:

| | `Cluster` (default) | `Local` |
|---|---|---|
| Source IP seen by pod | **Node IP** (SNAT'd) | **Real client IP** |
| Extra network hop | Yes, can bounce to another node | No |
| Traffic spread | Even across all endpoints | Proportional to endpoints *per node* |
| Node with zero local endpoints | Forwards to another node | **Drops** the packet |
| LB health check | Any node passes | Uses `healthCheckNodePort`; nodes without endpoints fail the check |

The `Local` failure mode is subtle: the cloud LB stops sending traffic to endpoint-less nodes because the health check fails, which is correct — but if your DaemonSet-less deployment happens to schedule all replicas onto three of thirty nodes, those three nodes receive 100% of external traffic. Combine `Local` with a DaemonSet or topology spread constraints, or accept the source IP loss. Details and a walkthrough are in the [source IP tutorial](https://kubernetes.io/docs/tutorials/services/source-ip/).

**`internalTrafficPolicy`** is the same idea for *in-cluster* traffic: `Local` restricts a Service to endpoints on the same node as the client, and **drops** if there are none ([docs](https://kubernetes.io/docs/concepts/services-networking/service-traffic-policy/)). This is the correct way to build node-local agents — a logging or metrics DaemonSet fronted by a Service that never crosses the network.

**`trafficDistribution`** is the modern topology control, replacing the older `service.kubernetes.io/topology-mode: Auto` annotation. Per [KEP-3015](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/3015-prefer-same-node), the `PreferSameZone` and `PreferSameNode` values reached **GA in v1.35**, with the `PreferSameTrafficDistribution` feature gate enabled by default since v1.34. `PreferClose` still works as a backward-compatible alias for `PreferSameZone`.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: payments
spec:
  trafficDistribution: PreferSameZone   # or PreferSameNode
  selector:
    app: payments
  ports:
    - port: 8080
```

Unlike `internalTrafficPolicy: Local`, these are **preferences with fallback** — if no same-zone endpoint is available, traffic goes cluster-wide rather than dropping. For a multi-AZ cell this is the lever that cuts cross-AZ data transfer cost, which on AWS is a real line item at cell scale. The tradeoff is that a zone with few replicas can be overloaded by its own zone's traffic; the implementation includes safeguards, but verify against your replica-per-zone ratios.

### EndpointSlices, and why Endpoints had to die

The original `Endpoints` object held **every address for a Service in a single object**. That is fine for 5 pods and catastrophic for 5,000:

- One object, so any single pod becoming ready rewrites the whole thing, and **every kube-proxy on every node receives the entire list** over watch. A 5,000-endpoint Service produces a multi-megabyte object; a rolling update produces thousands of full-object updates. This is the classic "control plane melts during a deploy" incident.
- etcd's default value size limit (1.5 MiB) put a hard ceiling on endpoints per Service.
- No room for per-endpoint metadata: no topology, no dual-stack, no distinction between "not ready" and "terminating but still serving".

[EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/) fix all four:

- Endpoints are sharded across multiple objects, **100 per slice by default** (tunable via the controller's `--max-endpoints-per-slice`, capped at 1000). A single pod change rewrites one small slice.
- `addressType` is `IPv4`, `IPv6`, or `FQDN`, so dual-stack is a first-class concept.
- Per-endpoint `conditions`: `ready`, `serving`, and `terminating`. `serving` + `terminating` is how graceful shutdown works — a pod being deleted can keep receiving traffic for in-flight requests without being counted as ready.
- `hints` carries topology data for zone-aware routing.
- `nodeName` and `zone` per endpoint, which is what makes `trafficDistribution` implementable.

The `Endpoints` API was **deprecated in v1.33** and continues to function with warnings; new code and new controllers should read EndpointSlices. If you write any operator that reconciles on service backends — and cell lifecycle tooling often does — read slices, not Endpoints.

### DNS

**CoreDNS architecture.** A Deployment (typically 2 replicas plus an autoscaler on managed platforms), fronted by a ClusterIP Service named `kube-dns` for backward compatibility, usually at the tenth address of the service CIDR (`10.96.0.10`). Configuration is a `Corefile` in a ConfigMap:

```text
.:53 {
    errors
    health   { lameduck 5s }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf { max_concurrent 1000 }
    cache 30
    loop
    reload
    loadbalance
}
```

The `kubernetes` plugin watches Services and EndpointSlices from the API server and answers from memory — CoreDNS does not query etcd or the API server per request. `forward . /etc/resolv.conf` sends everything else to the **node's** resolver, which on a cloud VM is the cloud metadata resolver (169.254.169.253 on AWS, 169.254.169.254 on GCP/Azure). That upstream is rate-limited per ENI on AWS (1024 packets per second), which is a real cell-scale ceiling.

**The `ndots:5` problem.** kubelet writes this into every pod's `/etc/resolv.conf`:

```text
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local ec2.internal
options ndots:5
```

`ndots:5` tells the resolver: if the queried name has **fewer than 5 dots**, try it with each search domain appended *before* trying it as an absolute name. So resolving `api.stripe.com` (2 dots) issues:

```text
api.stripe.com.default.svc.cluster.local.   -> NXDOMAIN
api.stripe.com.svc.cluster.local.           -> NXDOMAIN
api.stripe.com.cluster.local.               -> NXDOMAIN
api.stripe.com.ec2.internal.                -> NXDOMAIN
api.stripe.com.                             -> answer
```

Five lookups, and glibc issues **A and AAAA in parallel**, so **ten DNS queries** for one external hostname. At cell scale that is the dominant load on CoreDNS and a direct latency tax on every outbound connection. Fixes, in order of blast radius:

1. Use a **trailing dot** in the hostname (`api.stripe.com.`) in application config — zero search expansion, and no cluster-wide change.
2. Set `ndots` per pod for egress-heavy workloads:

   ```yaml
   spec:
     dnsConfig:
       options:
         - name: ndots
           value: "2"
   ```

3. Trim the search list with `dnsConfig.searches` for pods that never resolve short cross-namespace names.
4. Enable connection pooling / keepalive so DNS happens once per pool, not once per request.

Do **not** globally set `ndots:1` without auditing: short in-cluster names like `payments` (0 dots) still work, but `payments.default` (1 dot) would stop resolving because it would no longer get search-expanded.

**The conntrack DNS race — the 5-second timeout.** This is the most famous Kubernetes networking bug and you will meet it. glibc's resolver sends the A and AAAA queries **from the same source socket, essentially simultaneously**. Both packets have the same 5-tuple until the kernel assigns the reply. They race through netfilter's DNAT for the kube-dns ClusterIP, and both try to insert a conntrack entry for the same tuple. One insert fails (`insert_failed` in `conntrack -S`), the kernel **drops** that packet, and the resolver — which has no idea — waits out its **5-second timeout** and retries. Symptom: p99 DNS latency of exactly 5,000 ms, at low but steady rates, under load. The definitive write-up is Weaveworks' [racy conntrack and DNS lookup timeouts](https://www.weave.works/blog/racy-conntrack-and-dns-lookup-timeouts) (secondary, vendor engineering blog, but it is the canonical analysis), and the tracking issue is [kubernetes#56903](https://github.com/kubernetes/kubernetes/issues/56903).

Mitigations, weakest to strongest:

1. **`--random-fully`** on MASQUERADE reduces source-port collisions. kube-proxy applies it where supported. Helps; does not eliminate.
2. **`single-request-reopen`** in `dnsConfig.options` makes glibc use a fresh socket for the AAAA query, breaking the tuple collision. Per-pod, glibc-only (musl/Alpine ignores it — Alpine has its own, worse, resolver behaviour).
3. **NodeLocal DNSCache** — the real fix.

**NodeLocal DNSCache** runs a CoreDNS instance as a DaemonSet on every node, listening on a link-local address (conventionally `169.254.20.10`) bound to a dummy interface. Pods query the local address, so:

- **No DNAT, therefore no conntrack entry, therefore no race.** The bug is structurally impossible.
- Cache hits never leave the node.
- Cache misses go upstream to CoreDNS **over TCP**, and TCP conntrack entries are cleaned up on close rather than waiting out the 30-second UDP timeout — which also relieves conntrack table pressure.

The [official docs](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/) cover the two deployment shapes. The operational catch: because pods resolve against a node-local address baked into their `resolv.conf` at creation, **restarting or upgrading the node-local-dns DaemonSet causes a DNS outage for that node** unless you use the dual-IP configuration that keeps the kube-dns ClusterIP path working as a fallback. Bake this into your cell upgrade runbook.

**Headless service DNS.** For `clusterIP: None`, the `kubernetes` plugin returns an A/AAAA record per ready endpoint for the Service name, plus per-pod names when the pod has `hostname` and `subdomain` set (which StatefulSets do automatically). SRV records `_<port>._<proto>.<svc>.<ns>.svc.cluster.local` return port and target for each endpoint. Full record semantics are in the [DNS spec for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/). Note the TTL: with `ttl 30` in the Corefile, a client that caches aggressively will keep hitting a dead pod for up to 30 seconds after it is removed — JVM clients with `networkaddress.cache.ttl=-1` will do it forever.

### Cloud CNI: AWS VPC CNI

The [amazon-vpc-cni-k8s](https://github.com/aws/amazon-vpc-cni-k8s) plugin gives every pod a **real VPC IP address**. Two components: the `aws-node` DaemonSet running `ipamd` (manages ENIs and IP pools, talks to the EC2 API) and the CNI binary that `ipamd` hands addresses to.

**How a pod gets its IP.** ENIs are attached to the instance; each ENI carries a primary IP plus secondary IPs. `ipamd` maintains a warm pool of unassigned secondary IPs and hands one to each new pod. Datapath is veth plus the policy routing described earlier — an `ip rule` per pod selects the route table for the ENI that owns that address.

**Density limits.** ENIs per instance and IPs per ENI are **instance-type properties**, published in the [EC2 ENI documentation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html) and baked into the EKS AMI's [`eni-max-pods.txt`](https://github.com/awslabs/amazon-eks-ami/blob/main/templates/shared/runtime/eni-max-pods.txt). In secondary-IP mode:

```text
maxPods = (maxENIs * (IPsPerENI - 1)) + 2
```

The `-1` is the ENI's primary IP (not usable for pods) and the `+2` accounts for the host-network pods (`aws-node`, `kube-proxy`) that do not consume an IP. A `t3.small` with 3 ENIs × 4 IPs gives `3 × 3 + 2 = 11` pods. On a small instance this is brutally low, and it is why "my node has 8 GB free but won't schedule pods" is an AWS-specific question.

**Prefix delegation** is the fix. With `ENABLE_PREFIX_DELEGATION=true`, on Nitro instances each ENI slot holds a **/28 IPv4 prefix (16 addresses)** instead of a single IP ([AWS announcement](https://aws.amazon.com/blogs/containers/amazon-vpc-cni-increases-pods-per-node-limits/), [plugin docs](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/docs/prefix-and-ip-target.md)):

```text
maxPods = (maxENIs * ((IPsPerENI - 1) * 16)) + 2
```

capped by the EKS max-pods calculator at **110** for instances with 30 or fewer vCPUs and **250** above that, per Kubernetes scalability guidance. The catch: a /28 prefix must be **contiguous and unfragmented** in the subnet. A long-lived subnet that has been churning single IPs fragments, and prefix allocation starts failing with `InsufficientCidrBlocks` even though the subnet reports free addresses. Allocate prefix-delegation subnets fresh and generously.

**Warm pool tuning.** These four environment variables are the whole game:

| Variable | Default | Meaning |
|---|---|---|
| `WARM_ENI_TARGET` | 1 | Keep this many **fully unused ENIs** attached and ready |
| `WARM_IP_TARGET` | unset | Keep this many **free IPs** available, regardless of ENI boundaries |
| `MINIMUM_IP_TARGET` | unset | Floor on total allocated IPs; pairs with `WARM_IP_TARGET` |
| `WARM_PREFIX_TARGET` | 1 (in the shipped manifest) | Keep this many spare /28 prefixes, prefix mode only |

Setting `WARM_IP_TARGET`/`MINIMUM_IP_TARGET` **overrides** `WARM_PREFIX_TARGET`. The tradeoff is a three-way one and you will tune it per cell shape: a large `WARM_ENI_TARGET` wastes VPC IPs but makes pod start instant; a tight `WARM_IP_TARGET` conserves IPs but makes `ipamd` call the EC2 API on nearly every pod launch, and **EC2 API throttling then shows up as slow or failed pod starts**. Note that setting either target to zero is unsupported with prefix delegation.

**IP exhaustion** is the defining AWS failure mode, because pods consume subnet addresses 1:1. A `/24` node subnet holds 251 usable addresses — roughly eight `m5.large` nodes' worth of pods. Mitigations:

- **Bigger subnets** — the boring, correct answer for a greenfield cell. Plan pod IPs, not node IPs.
- **Custom networking** (`AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true` plus `ENIConfig` CRDs): pods get addresses from a *different* subnet than the node, commonly from the RFC 6598 carrier-grade NAT space `100.64.0.0/10` added as a secondary VPC CIDR. See the [EKS custom networking docs](https://docs.aws.amazon.com/eks/latest/userguide/cni-custom-network.html). The gotcha: with custom networking the **primary ENI is no longer used for pods**, so your max-pods drops by one ENI's worth and must be recomputed. Silently keeping the old `--max-pods` overcommits the node and pods get stuck in `ContainerCreating`.
- **Prefix delegation**, which does not reduce IP consumption but makes allocation coarser and faster.
- **IPv6 mode**, which sidesteps exhaustion entirely.

**Security groups for pods.** The VPC Resource Controller attaches a **trunk ENI** (`aws-k8s-trunk-eni`) to the node and creates **branch ENIs** (`aws-k8s-branch-eni`) per pod, each with its own security groups selected by a `SecurityGroupPolicy` CRD ([best practices](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html)). Nitro-only, and branch ENIs are a scarcer resource than secondary IPs, so **pod density drops sharply** for pods using this feature. Useful when a pod must reach an RDS instance whose security group you cannot widen to the whole node.

**NetworkPolicy.** The VPC CNI gained native Kubernetes NetworkPolicy enforcement using **eBPF** ([announcement](https://aws.amazon.com/blogs/containers/amazon-vpc-cni-now-supports-kubernetes-network-policies/)). It is off by default and must be enabled on the add-on (`enableNetworkPolicy` / `--enable-network-policy`). **A default EKS cluster silently ignores NetworkPolicy objects.** Verify this on every cell you provision — see gotcha 9.

### Cloud CNI: GKE

GKE clusters are **VPC-native**, using **alias IP ranges**. The node's subnet gets a *secondary range* for pods; each node is assigned a slice of that range as an alias IP range bound to its NIC, and the VPC routes it natively — no overlay, no per-node routes to manage ([GKE network overview](https://cloud.google.com/kubernetes-engine/docs/concepts/network-overview)).

**The sizing rule you must memorise.** GKE assigns each node **the smallest CIDR that holds twice the max-pods-per-node setting**. That factor of two exists to avoid immediate IP reuse when pods churn.

| `--max-pods-per-node` | 2× | Per-node alias range | Nodes per /16 pod range |
|---|---|---|---|
| 110 (default) | 220 | **/24** | 256 |
| 64 | 128 | /25 | 512 |
| 32 | 64 | /26 | 1024 |
| 16 | 32 | /27 | 2048 |
| 8 | 16 | /28 | 4096 |

The default of 110 pods per node burning a /24 is why a /16 pod range caps a cluster at 256 nodes and why `IP_SPACE_EXHAUSTED` is the most common GKE scaling wall. **Setting `--max-pods-per-node` at node pool creation is the single highest-leverage GKE cell decision**, and it is immutable for that node pool. If your cells run ~30 pods per node, setting 32 quadruples the node ceiling for the same pod range. When you outgrow a range anyway, GKE supports [additional pod IPv4 ranges per node pool](https://cloud.google.com/kubernetes-engine/docs/how-to/multi-pod-cidr).

**GKE Dataplane V2** is Google's eBPF dataplane, built on Cilium, running as the GKE-managed `anetd` DaemonSet. It **replaces kube-proxy** for Service handling and provides NetworkPolicy plus network policy logging natively ([docs](https://cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2)). It is the **default for new Autopilot clusters** and Google's recommendation for Standard.

The operationally important constraint: **Dataplane V2 can only be enabled at cluster creation; existing clusters cannot be migrated.** For your cells that means the dataplane choice is part of the cell template, and changing it is a cell replacement, not a cell upgrade. Google's [history of the GKE network interface](https://cloud.google.com/blog/products/networking/gke-network-interface-from-kubenet-to-ebpfcilium-to-dranet) is a good read on the trajectory from kubenet through eBPF/Cilium to DRANET for high-performance NICs.

Legacy GKE clusters used the older dataplane with `calico-node` bolted on for NetworkPolicy; if you inherit one, expect different policy semantics and no policy logging.

### Cloud CNI: Azure

Azure has the most options and the most confusing naming. Four models, and you need to know which one a cell is on.

**kubenet (legacy, retiring).** Pods get IPs from a cluster-internal CIDR that is *not* in the VNet. The AKS control plane programs a **User-Defined Route per node** in the node subnet's route table so nodes can reach each other's pods, and pod egress is NAT'd to the node IP. Two hard limits: Azure route tables cap at 400 routes, which caps the cluster's node count, and the NAT breaks the "no NAT pod-to-pod" spirit for anything outside the cluster. Microsoft has announced that **kubenet for AKS retires on 31 March 2028**, with migration to Azure CNI Overlay required ([AKS legacy CNI docs](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni)).

**Azure CNI Node Subnet (legacy).** Pods get IPs directly from the **node's VNet subnet**. Every node pre-reserves `maxPods` addresses at join time, so a 100-node cluster with `maxPods=30` consumes 3,000 VNet addresses plus node addresses **whether or not the pods exist**. This is the fastest way to exhaust a VNet and the reason many Azure estates ran out of address space.

**Azure CNI Pod Subnet (dynamic IP allocation).** Pods draw from a **separate delegated subnet**, allocated in batches rather than fully pre-reserved. Better density and cleaner separation of node and pod address planning, and pod IPs remain real VNet addresses (visible to NSGs, peered networks, Private Link).

**Azure CNI Overlay.** Pods get IPs from a private overlay CIDR managed by Kubernetes; **only nodes consume VNet addresses**. Each node receives a **/24** from the overlay CIDR ([Overlay docs](https://learn.microsoft.com/en-us/azure/aks/concepts-network-azure-cni-overlay)). This is Microsoft's recommended default and the migration target for kubenet. The tradeoff is the usual overlay one: pod IPs are invisible outside the cluster, so a pod cannot be the direct target of a resource in a peered VNet.

**Azure CNI Powered by Cilium** is an eBPF dataplane layered on either Overlay or Pod Subnet IPAM, giving Cilium's Service handling and NetworkPolicy enforcement in a managed form ([docs](https://learn.microsoft.com/en-us/azure/aks/azure-cni-powered-by-cilium)). Note that the managed offering does not expose the full upstream `CiliumNetworkPolicy` surface — check the current documentation for which L7 and FQDN features are supported before designing policy around them.

Two Azure deprecations to track: **Azure NPM** (the older NetworkPolicy implementation) is unsupported on Windows nodes as of 30 September 2026, with Linux support ending 30 September 2028. And AKS has had **nftables kube-proxy mode in preview** since November 2025 ([AKS engineering blog](https://blog.aks.azure.com/2025/11/19/nftables-in-kube-proxy)) — confirm current GA status before pinning a mode in an Azure cell template.

### Cloud CNI comparison

| | AWS VPC CNI | AWS VPC CNI + custom networking | GKE (alias IP) | GKE Dataplane V2 | Azure CNI Node Subnet | Azure CNI Pod Subnet | Azure CNI Overlay | Azure kubenet |
|---|---|---|---|---|---|---|---|---|
| Pod IP source | Node's VPC subnet | Separate VPC subnet (often `100.64/10`) | Subnet secondary range | Same as GKE | Node's VNet subnet | Delegated VNet subnet | Cluster-internal overlay CIDR | Cluster-internal CIDR |
| Consumes cloud IPs | 1 per pod | 1 per pod, from a dedicated range | 1 per pod | 1 per pod | 1 per pod, **pre-reserved** | 1 per pod, batched | **Nodes only** | Nodes only |
| Encapsulation | None | None | None | None | None | None | Overlay | None (UDR routing) |
| Exhaustion risk | **High** | Medium | Medium-high (per-node /24 default) | Same | **Highest** | Medium | **Low** | Low |
| Max pods/node driver | ENI slots; 110/250 cap with prefixes | Same, minus one ENI | 2× max-pods rounded to a CIDR | Same | `maxPods` setting | `maxPods` setting | `maxPods` setting | `maxPods`; 400-route cluster cap |
| NetworkPolicy | eBPF, **opt-in** | Same | Requires Calico add-on | **Built in** (+ policy logging) | Azure NPM (deprecating) or Cilium | Same | Same | Azure NPM |
| Pod IP visible to cloud (NSG/SG, flow logs, peering) | **Yes** | Yes | **Yes** | Yes | **Yes** | Yes | No | No |
| Jumbo frames | Yes (9001) | Yes | Yes | Yes | Yes | Yes | Reduced by encap | Yes |
| Switchable after creation | Config-tunable | Needs node recycle | n/a | **No — create-time only** | Migration path exists | Migration path exists | Migration target | **Retires 2028-03-31** |

The cross-cloud lesson for cell templating: **pod IP addressing is the least portable part of Kubernetes**. You can template Helm charts identically across all three clouds and still need three different capacity models, three different exhaustion alarms, and three different max-pods calculations. Encode those as explicit per-cloud cell parameters rather than trying to unify them.

*See also: [networking per cloud](04-managed-kubernetes-eks-gke-aks.md#networking-per-cloud) for the managed-service routing table over this — which mode each provider defaults to, and which choices are immutable after cluster creation.*

### Overlay and policy CNIs

**Cilium** is eBPF-native and has become the default answer for new clusters that are not using a cloud CNI. Its distinguishing properties:

- **Identity-based policy.** Cilium assigns a numeric *security identity* to each set of pod labels and enforces policy on identity, not IP. Policy state is therefore O(identities), not O(pods) — a deployment scaling from 10 to 10,000 replicas adds zero policy rules. This is the architectural reason Cilium scales where IP-based policy engines do not.
- **kube-proxy replacement** via eBPF ([docs](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/)), including **Maglev consistent hashing** so that adding or removing a node changes backend selection for at most ~1% of flows without cross-node state synchronisation, plus **DSR** (Direct Server Return) and **XDP acceleration** for NodePort traffic.
- **Routing modes**: native routing, VXLAN, or Geneve, switchable by config.
- **Encryption**: WireGuard or IPsec transparently between nodes. Cilium 1.19 added **strict modes** for both, which drop unencrypted inter-node traffic instead of falling back — turning encryption from best-effort into an enforced invariant ([InfoQ coverage of 1.19](https://www.infoq.com/news/2026/02/cilium-119/), secondary).
- **Hubble** for flow-level observability: `hubble observe` gives you per-flow verdicts including *why* a packet was dropped, which is the single biggest debugging upgrade over iptables.
- **Cluster Mesh** for multi-cluster, **Egress Gateway** for stable egress source IPs, **BGP control plane**, **LB IPAM**, and a Gateway API implementation.

Cilium 1.19 shipped in early 2026 and **1.20 is the current stable line** as of this writing, with 1.21 in development; check the [releases page](https://github.com/cilium/cilium/releases) for the current patch level rather than trusting this sentence. A behaviour change worth flagging from 1.19: network policy selectors that do not explicitly name a cluster now default to **allowing only the local cluster**, which tightens Cluster Mesh deployments and can break policies written against older semantics.

**Calico** is the most deployment-flexible option and the one you are most likely to meet on bare metal or in a bring-your-own-dataplane cell.

- **Components**: `Felix` (per-node agent programming the dataplane), `BIRD` (BGP daemon), `confd`, and `Typha` — a watch multiplexer that sits between Felix instances and the API server. **Typha is mandatory above roughly 50 nodes**; without it, every Felix watches the API server directly and the control plane buckles.
- **Dataplanes**: iptables (classic), **nftables (GA in v3.31)**, **eBPF**, and Windows HNS ([nftables data plane guide](https://docs.tigera.io/calico/latest/getting-started/kubernetes/nftables), [eBPF mode](https://docs.tigera.io/calico/latest/operations/ebpf/)).
- **Networking modes**: pure **BGP** with no encapsulation (peer with the ToR or use route reflectors — the best option on a controlled L3 fabric), **IPIP**, or **VXLAN**, each available in `Always` or `CrossSubnet` mode. `CrossSubnet` is the pragmatic default: no encapsulation between nodes in the same subnet, encapsulation only when crossing a boundary the fabric will not route.
- **IPAM**: `calico-ipam` allocates **/26 blocks per node** with affinity, which is more IP-efficient than a fixed per-node /24 but means block exhaustion is a distinct failure from pool exhaustion.
- **Policy**: `NetworkPolicy` plus `GlobalNetworkPolicy` (cluster-scoped, with `order`, explicit `Deny`, `Log` and `Pass` actions), host endpoint policy for protecting the node itself, and **staged policies** for dry-running a rule before enforcing it ([v3.31 overview](https://www.tigera.io/blog/whats-new-in-calico-v3-31-ebpf-nftables-and-more/), vendor blog).

**Flannel** is the minimal option: a VXLAN or `host-gw` overlay with `host-local` IPAM and **no NetworkPolicy support at all**. It is a fine choice for kind and CI clusters and a poor one for anything carrying tenant traffic. The historical "Canal" pattern paired Flannel for networking with Calico for policy; today you would just run Calico or Cilium.

**Choosing.** For your cells, the decision tree is short:

- On EKS/GKE/AKS with no special requirements, **use the cloud CNI** — it is the supported path, integrates with cloud IAM and security groups, and keeps pod IPs visible to cloud tooling. Budget the IP plan carefully.
- If you need **identity-based policy at scale, L7 or FQDN policy, transparent encryption, multi-cluster, or deep flow observability**, run **Cilium**, either self-managed or as the cloud's managed variant (GKE Dataplane V2, Azure CNI Powered by Cilium).
- If you need **BGP integration with a physical fabric or a non-cloud environment**, run **Calico**.
- Use **Flannel** only where correctness of policy does not matter.

The consideration specific to a multi-cloud fleet: Cilium is the only one of these that gives you a **single dataplane and a single policy language on all three clouds**, at the cost of running it yourself on top of (or instead of) each cloud's CNI. That is a real, defensible platform decision, and it is the one most multi-cloud platform teams eventually make.

### eBPF, enough to be dangerous

An **eBPF program** is bytecode for a restricted in-kernel virtual machine. You load it with the `bpf()` syscall, the kernel **verifies** it, JIT-compiles it to native code, and attaches it to a hook. It then runs on every event at that hook, at native speed, with no context switch and no kernel module ([ebpf.io overview](https://ebpf.io/what-is-ebpf/), [kernel BPF docs](https://docs.kernel.org/bpf/)).

**Hook points that matter for networking**, earliest to latest in the receive path:

| Hook | Where | Sees | Can do | Used by |
|---|---|---|---|---|
| **XDP** | NIC driver, before `sk_buff` allocation | Raw frame | `DROP`, `PASS`, `TX`, `REDIRECT` | DDoS filtering, Cilium NodePort acceleration |
| **TC** (`clsact` ingress/egress) | Traffic control layer | `sk_buff`, full metadata | Rewrite, redirect, drop | Cilium's and Calico's main datapath |
| **cgroup** (`connect4`, `bind4`, `sendmsg4`) | Socket syscalls | Socket + address | Rewrite destination **at connect time** | Cilium's socket-level load balancing |
| **sockops / sk_msg** | TCP state changes, socket data | Socket | Splice sockets, bypass the stack | Cilium's local-process fast path, sidecar acceleration |
| **kprobes / tracepoints** | Anywhere in the kernel | Function args | Observe (and with LSM, enforce) | `bpftrace`, profiling, Hubble enrichment |

**Maps** are the shared memory between programs and userspace: hash, array, LRU hash (self-evicting — used for conntrack), **LPM trie** (longest-prefix match, used for CIDR policy rules), per-CPU variants, ring buffers for events, and sockmaps. Maps can be **pinned** into `/sys/fs/bpf` so they survive the agent restarting — which is why `cilium` pods can be upgraded without dropping traffic.

**The verifier** is why eBPF is safe to run in the kernel. It walks the program's control flow graph and rejects anything that could crash or hang: unbounded loops, out-of-bounds memory access, uninitialised reads, invalid pointer arithmetic, or calls to helpers not permitted at that hook. There is a complexity ceiling (on modern kernels, on the order of a million verified instructions), which is why complex eBPF programs use **tail calls** to chain smaller programs. LWN's [verifier coverage](https://lwn.net/Articles/794934/) is the best plain-language explanation of how it reasons about register bounds.

**Why Cilium's kube-proxy replacement is faster** — three distinct reasons, and it is worth being precise because design reviews and incident reviews both probe this:

1. **Map lookup instead of chain traversal.** Service resolution is a hash lookup in a BPF map: O(1), independent of Service count. iptables walks rules linearly.
2. **Socket-level load balancing.** At the `cgroup/connect4` hook, Cilium rewrites the destination **inside `connect()`**, before a packet exists. The socket connects directly to the backend pod IP. There is then **no per-packet DNAT and no conntrack entry** for that flow's Service translation — the cost is paid once at connect time rather than on every packet.
3. **Bypassing the netfilter stack.** For NodePort with XDP, packets are redirected at the driver before `sk_buff` allocation. For pod-to-pod on the same node, Cilium can redirect straight from one veth to another at the TC layer, skipping the routing and netfilter path entirely.

**Debugging eBPF datapaths:**

```bash
# Generic
bpftool prog show                          # loaded programs, ids, types, run stats
bpftool prog dump xlated id 42             # verified/translated bytecode
bpftool map show                           # maps and their sizes
bpftool map dump id 17                     # contents (conntrack, LB backends, policy)
bpftool net show                           # which programs are attached to which devices
bpftool cgroup tree                        # cgroup-attached programs

# Cilium (run inside a cilium agent pod)
cilium-dbg status --verbose                # datapath mode, kube-proxy replacement state
cilium-dbg service list                    # every Service and its backends, from BPF maps
cilium-dbg bpf lb list                     # raw load balancer map
cilium-dbg endpoint list                   # pods, their identities, and policy enforcement state
cilium-dbg bpf policy get <endpoint-id>    # the actual allow list for one pod
cilium-dbg monitor -t drop                 # live drop events with reasons
cilium-dbg monitor --related-to <ep-id> -v # everything touching one endpoint

# Hubble
hubble observe --namespace prod --verdict DROPPED --last 100
hubble observe --from-pod prod/api --to-service prod/payments -f
```

`cilium-dbg monitor -t drop` is the closest thing to a superpower in this whole guide: it tells you the *reason* a packet was dropped (`Policy denied`, `Stale or unroutable IP`, `CT: Map insertion failed`, `Invalid source ip`) rather than leaving you to infer it from a missing packet on the far side of a `tcpdump`.

Note: the binary was renamed from `cilium` to `cilium-dbg` inside agent pods in recent versions to distinguish it from the `cilium` CLI you run against the cluster. Older documentation and runbooks say `cilium monitor`; both may work depending on version.

### NetworkPolicy

The [standard API](https://kubernetes.io/docs/concepts/services-networking/network-policies/) is namespaced and additive. Its semantics have two rules that account for most confusion:

1. **Selecting a pod makes it default-deny for the selected direction.** A pod with no policy selecting it allows everything. The moment *any* policy selects it with `policyTypes: [Ingress]`, all ingress not explicitly allowed is denied.
2. **Policies are purely additive allow-lists.** There is no deny rule, no priority, no ordering. The union of all matching policies is the allow set.

The canonical default-deny baseline, which every cell namespace should probably start from:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: prod
spec:
  podSelector: {}          # every pod in the namespace
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: prod
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: kube-system }
          podSelector:
            matchLabels: { k8s-app: kube-dns }
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
```

Forgetting that second policy is the number one NetworkPolicy outage: you apply default-deny egress, DNS breaks, and **every** application in the namespace fails simultaneously in a way that looks like a DNS outage rather than a policy problem.

**Limitations of the standard API**, all of which you will run into:

- **No FQDN egress rules.** You cannot say "allow to `api.stripe.com`" — only to IP CIDRs, which for a CDN-fronted SaaS is unbounded and unstable.
- **No L7 awareness.** No "allow GET /health but not POST /admin".
- **No logging.** A denied packet vanishes silently. There is no standard way to see what a policy blocked, which makes rollout terrifying.
- **No explicit deny, no priority, no ordering.** You cannot express "deny this one thing" without restructuring the allow set.
- **No cluster-scoped policy.** Nothing enforces a baseline across all namespaces; a namespace with no policy is wide open.
- **`ipBlock` matches the packet's source as the CNI sees it**, which after SNAT may be a node IP rather than the original client — so `ipBlock` rules on ingress from a LoadBalancer often match nothing useful unless `externalTrafficPolicy: Local` preserves the client IP.
- **Enforcement is entirely the CNI's job.** The API server accepts and stores NetworkPolicy objects regardless of whether anything implements them. A cluster with Flannel, or an EKS cluster without the VPC CNI network policy feature enabled, **silently ignores every policy you apply** while `kubectl get netpol` shows them present and healthy-looking. This is a security-critical failure that produces no error anywhere.

The last point deserves a hard rule: **every cell provision must include a positive test that policy is enforced** — apply a deny-all in a scratch namespace, curl from a pod, assert it fails. Anything less is assuming.

The cluster-scoped gap is being addressed upstream by **AdminNetworkPolicy** and **BaselineAdminNetworkPolicy** from SIG-Network's [network-policy-api](https://github.com/kubernetes-sigs/network-policy-api) subproject, which add cluster-scoped, ordered rules with explicit `Deny` and `Pass` actions. Check that repository for the current maturity level before depending on it.

**CNI-specific extensions** fill the gaps today:

| Capability | Standard `NetworkPolicy` | `CiliumNetworkPolicy` | Calico `GlobalNetworkPolicy` |
|---|---|---|---|
| Scope | Namespaced | Namespaced + clusterwide variant | **Cluster-scoped** |
| Explicit deny | No | Via `Deny` policies | **Yes** (`action: Deny`) |
| Rule ordering | No | Deny takes precedence | **Yes** (`order` field) |
| FQDN egress | No | **Yes** (`toFQDNs`) | Yes (with DNS policy) |
| L7 rules | No | **Yes** (HTTP method/path, Kafka, DNS) | Limited |
| Logging | No | Via Hubble | **Yes** (`action: Log`) |
| Dry run | No | No | **Yes** (staged policies) |
| Protect the node itself | No | Host policies | **Yes** (host endpoints) |
| Portable across CNIs | **Yes** | No | No |

The tradeoff is stark and worth stating explicitly to your team: the standard API is portable and nearly useless for real egress control; the extensions are powerful and lock the cell to one CNI. For a multi-cloud fleet, standardising on Cilium's policy language is what buys you one policy model everywhere — and is a strong argument for running Cilium yourself rather than three different cloud CNIs.

*See also: [rule types with real examples](07-kyverno.md#rule-types-with-real-examples) for how a default-deny NetworkPolicy gets into every new namespace automatically — a `generate` rule is the mechanism, and "half my namespaces have one" is the failure it prevents.*

### Ingress and egress at the cell edge

In many organizations a networking team owns most of this. You need to speak the language, and you own the parts that a cell cannot boot without.

**Ingress vs Gateway API.** The `Ingress` API is still GA and supported, but it is **feature-frozen** — all upstream development moved to the [Gateway API](https://gateway-api.sigs.k8s.io/). Ingress's design flaw was that anything beyond "host and path to a Service" required controller-specific annotations, so an Ingress manifest was never actually portable.

Gateway API replaces it with a role-separated resource model:

| Resource | Owned by | Purpose |
|---|---|---|
| `GatewayClass` | Infrastructure provider | The implementation (Envoy Gateway, Cilium, Istio, a cloud LB) |
| `Gateway` | Cluster operator (**you**) | A listener: addresses, ports, TLS, and which routes may attach |
| `HTTPRoute` / `GRPCRoute` / `TCPRoute` / `TLSRoute` / `UDPRoute` | Application team | Matching and forwarding rules |
| `ReferenceGrant` | Namespace owner | Explicit consent for a cross-namespace reference |
| `BackendTLSPolicy` | Either | TLS from the gateway to the backend |

That split matters for a platform team: you own the `Gateway` (and therefore TLS, ports, and which namespaces may attach routes), while application teams own `HTTPRoute` without needing permission to touch shared infrastructure. Cross-namespace references require an explicit `ReferenceGrant`, closing the "any team can hijack a host" hole that Ingress had.

Status as of this writing: **Gateway API v1.5 shipped in February 2026**, focused on graduating experimental features to Standard ([release blog](https://kubernetes.io/blog/2026/04/21/gateway-api-v1-5/)), with v1.6 in circulation — check the [implementations page](https://gateway-api.sigs.k8s.io/implementations/) for which controllers support which channel. Two extensions worth knowing: **GAMMA**, which applies the same route types to east-west service mesh traffic, and the **Inference Extension**, which adds LLM-aware routing (model-aware, load-aware backend selection) — relevant if any cell ever fronts inference workloads.

**The urgent operational fact**: the community-maintained `kubernetes/ingress-nginx` controller reached **end of life in March 2026**. The `Ingress` *API* is unaffected, but the most widely deployed controller implementing it is no longer receiving security fixes. Any cell still running it needs a migration plan; SIG-Network shipped [ingress2gateway 1.0](https://kubernetes.io/blog/2026/03/20/ingress2gateway-1-0-release) in March 2026, which translates Ingress objects and 30+ common annotations into Gateway and HTTPRoute. Verify what your cells run.

**Envoy-based ingress** is the mainstream implementation shape — Envoy Gateway, Istio, Contour, Cilium's Gateway API, and kgateway all run Envoy as the dataplane with different control planes. Practical implications: config is pushed via xDS (so config propagation is eventually consistent and has its own latency and failure modes), and Envoy's access logs and `/stats` endpoint are usually your best L7 debugging surface.

**Source IP preservation** at the edge, in order of preference:

1. **`externalTrafficPolicy: Local`** — works for any Service type, costs you the traffic-spread properties discussed earlier.
2. **Cloud LB in IP target mode with client IP preservation** — an AWS NLB with `target-type: ip` and `preserve_client_ip` enabled hands the pod the real client IP with no SNAT.
3. **PROXY protocol** — the LB prepends a small header carrying the original source and destination before the first byte of the stream ([HAProxy PROXY protocol spec](https://www.haproxy.org/download/2.8/doc/proxy-protocol.txt)). v1 is human-readable text, v2 is binary with TLV extensions. **Both ends must agree.** If the LB sends it and the backend does not parse it, the backend sees `PROXY TCP4 1.2.3.4 ...` as the first line of what it thinks is an HTTP request and returns a 400; if the backend expects it and the LB does not send it, every connection fails. This asymmetry is the classic PROXY protocol outage, and it bites during rollouts where LB and backend change independently.
4. **`X-Forwarded-For`** — L7 only, and only trustworthy if every hop that could forge it is under your control.

**Egress.** Pods reaching the internet go through cloud NAT, and the constraint that surprises people is **SNAT port exhaustion**. An AWS NAT Gateway supports roughly 55,000 simultaneous connections **to each unique destination** (address, port, protocol); exceeding it produces the `ErrorPortAllocation` CloudWatch metric and connection failures that look like the remote service is down. A cell where thousands of pods hammer one third-party API endpoint hits this. Mitigations: multiple NAT gateways, connection pooling and keepalive in applications, VPC endpoints/Private Link for AWS services so the traffic never touches NAT at all. GCP Cloud NAT has the analogous constraint via minimum ports per VM (default 64) — enable Dynamic Port Allocation. Azure NAT Gateway has configurable SNAT port allocation with the same failure shape.

Also budget for NAT gateway **data processing cost**, which at cell scale is frequently larger than the compute it serves.

**Stable egress identity.** When a partner allowlists your source IPs, you need pod traffic to leave from a known address. Options: a dedicated NAT gateway per cell with an Elastic IP, or **Cilium Egress Gateway**, which routes selected pods' egress through designated gateway nodes with fixed source IPs — more granular, and it works identically on all three clouds, which is exactly the kind of portability your team benefits from.

*See also: [ingress and Gateway API](11-cert-manager-and-pki.md#ingress-and-gateway-api) for how the `Gateway` you own gets its certificate, and why the ingress-shim's implicit lifecycle is the wrong coupling for a cell edge.*

### Multi-cluster and multi-cloud connectivity

Cells are isolated units, but the moment you need cross-cell traffic, you are in multi-cluster networking. Survey:

| Approach | Layer | Requires same CNI | Cross-cluster service discovery | Encryption | Overlapping pod CIDRs | Operational cost |
|---|---|---|---|---|---|---|
| **Cilium Cluster Mesh** | L3/L4 | **Yes** (Cilium everywhere) | Global Services (same name in both clusters) | WireGuard/IPsec | Not supported | Low once Cilium is standard |
| **Submariner** | L3 | No (CNI-agnostic) | Lighthouse (`*.clusterset.local`) | IPsec tunnels | **Yes**, via Globalnet | Medium; gateway nodes are a bottleneck |
| **Istio multicluster** | L7 | No | Full mesh service registry | mTLS | **Yes** (east-west gateway terminates) | High; a mesh is a system |
| **Linkerd multicluster** | L7 | No | Mirrored services | mTLS | Yes (gateway-based) | Medium |
| **VPC peering / Transit Gateway / VNet peering** | L3 (cloud) | No | None — bring your own | Cloud-internal | **No** — hard requirement | Low, but no service abstraction |
| **Site-to-site VPN** | L3 | No | None | IPsec | No | Low; bandwidth-limited |

The rules of thumb:

- **Non-overlapping pod and service CIDRs across every cell is the cheapest decision you will ever make and the most expensive one to retrofit.** Allocate from a global plan on day one, even for cells that will "never" need to talk. Overlapping CIDRs force you into gateway-based (L7 or Globalnet) solutions permanently.
- **Cilium Cluster Mesh** is the lowest-friction option *if* you already run Cilium everywhere. It requires unique cluster IDs and names, non-overlapping pod CIDRs, and node-to-node reachability between clusters (peering or VPN underneath). Global Services let a Service name resolve to backends in multiple clusters with automatic failover.
- **Submariner** is the choice when clusters run different CNIs — which is exactly the multi-cloud case where each cell uses its cloud's native CNI. Its Globalnet feature handles CIDR overlap by NATing through a global CIDR, at the cost of losing real source IPs. Note that Submariner routes traffic through designated gateway nodes, so those nodes are both a bandwidth bottleneck and a failure domain; size and monitor them.
- **Istio (or any mesh) multicluster** solves the problem at L7 with east-west gateways, which is the only approach that genuinely does not care about IP overlap. You pay for it with mesh operational complexity, and you should only choose it if you already want a mesh for other reasons.
- **Plain cloud peering** gives you connectivity with zero service abstraction. For cell-to-cell control plane traffic between a small number of known endpoints, this is often the right answer and everything above is over-engineering.

For a multi-cloud fleet specifically: cross-cloud connectivity means either a cloud interconnect product (Direct Connect, Cloud Interconnect, ExpressRoute) or encrypted tunnels over the internet. Latency and cost both jump; design so that cross-cloud traffic is control-plane-scale, not data-plane-scale.

### IPv6 and dual-stack, briefly

Dual-stack has been GA since Kubernetes v1.23 ([docs](https://kubernetes.io/docs/concepts/services-networking/dual-stack/)). The model:

- Nodes get both an IPv4 and an IPv6 pod CIDR; the cluster has two service CIDRs.
- Services declare `ipFamilyPolicy`: `SingleStack` (default), `PreferDualStack`, or `RequireDualStack`, plus an `ipFamilies` list controlling ordering. The **first family listed is the primary**, and that is what `.spec.clusterIP` reports for backward compatibility.
- EndpointSlices are per-family (`addressType: IPv4` or `IPv6`), which is why the Endpoints API could not support dual-stack.

Why you would care: **IPv6 permanently solves the AWS and Azure IP exhaustion problem**. EKS supports IPv6-only clusters where pods get globally unique IPv6 addresses from the VPC and reach IPv4-only destinations through a host-local NAT64 path provided by the CNI. If a cell family is going to be very large, this is worth evaluating early — it is another create-time decision.

Gotchas that show up in practice:

- **Application readiness.** Anything binding `0.0.0.0` explicitly (rather than `::` with `v6only=0`, or dual sockets) will not accept IPv6 connections. Many older libraries and health-check scripts assume IPv4 string formats.
- **AAAA doubling.** Every DNS lookup already issues A and AAAA; on a dual-stack cluster both now return answers, and Happy Eyeballs behaviour in clients determines which is tried first. Latency characteristics change.
- **Node hardening scripts** that set `net.ipv6.conf.all.disable_ipv6=1` — a common CIS-benchmark artifact — break dual-stack silently at the node level.
- **Policy coverage.** A NetworkPolicy `ipBlock` written for IPv4 does nothing for the IPv6 path. Default-deny plus IPv4-only allow rules will block v6; IPv4-only allow rules without default-deny leave v6 wide open. Audit both families.

### Host-level tuning that matters

These are node properties, not pod properties, and they are set at node bootstrap. Getting them into the AMI/image or the bootstrap script is cell lifecycle work that you own.

| Sysctl | Default (typical) | Why it matters | Suggested for busy nodes |
|---|---|---|---|
| `net.core.somaxconn` | 4096 on modern kernels | Caps the accept queue; a full queue drops SYNs silently. The app's `listen()` backlog is capped by this | 32768 |
| `net.ipv4.tcp_max_syn_backlog` | 1024-4096 | Half-open connection queue; overflow drops SYNs | 8192+ |
| `net.core.netdev_max_backlog` | 1000 | Packets queued between NIC and stack; overflow shows in `/proc/net/softnet_stat` | 16384 |
| `net.ipv4.ip_local_port_range` | `32768 60999` (~28k ports) | Ephemeral ports for outbound connections; a busy egress node or NAT-heavy workload exhausts this | `1024 65535` (or `10240 65535` to stay clear of well-known ports) |
| `net.ipv4.tcp_tw_reuse` | 2 (loopback only) | Allows reusing TIME_WAIT sockets for new outbound connections | 1 for egress-heavy nodes |
| `net.ipv4.tcp_fin_timeout` | 60 | How long a socket sits in FIN_WAIT_2 | 30 |
| `net.netfilter.nf_conntrack_max` | RAM-derived; **kube-proxy overrides** | Table full = silent packet drops | Size to peak flows × 2 |
| `net.netfilter.nf_conntrack_buckets` | `max/4` | Hash sizing; too small means long chains | `max/4` or `max/2` |
| `net.netfilter.nf_conntrack_tcp_timeout_established` | 432000 (5 days) kernel / 86400 via kube-proxy | Stale entries occupying the table | 3600-86400 |
| `net.netfilter.nf_conntrack_udp_timeout` | 30 | Every DNS query holds an entry this long | leave, or use NodeLocal DNSCache |
| `net.ipv4.tcp_congestion_control` | `cubic` | `bbr` materially improves throughput on lossy or high-BDP paths | `bbr` (needs `fq` qdisc) |
| `net.ipv4.tcp_rmem` / `tcp_wmem` | `4096 131072 6291456` | Autotuning bounds; raise the max for high-BDP cross-region links | raise max to 16-32 MB |
| `net.ipv4.conf.all.rp_filter` | distro-dependent (0, 1, or 2) | **Strict mode (1) drops asymmetrically routed packets.** Several CNIs require 0 or 2 | 0 or 2 per CNI docs |
| `net.ipv4.ip_forward` | 0 | **Must be 1** or the node cannot route pod traffic at all | 1 |
| `net.bridge.bridge-nf-call-iptables` | 0 unless set | Bridged CNIs need 1 or netfilter (and therefore NetworkPolicy and Services) never sees the traffic | 1 for bridge-based CNIs |
| `fs.inotify.max_user_instances` / `max_user_watches` | 128 / 8192 | Not networking, but exhausts on dense nodes and breaks kubelet and CNI agents | 8192 / 524288 |

Sysctl values are documented in the kernel's [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html) and [nf_conntrack-sysctl](https://docs.kernel.org/networking/nf_conntrack-sysctl.html) references; defaults vary by kernel version and distro, so read the running value rather than trusting a table.

**Sysctls inside pods.** Kubernetes splits sysctls into *safe* (namespaced, and setting them cannot affect other pods or the node) and *unsafe*. Only safe ones can be set in a pod spec by default; unsafe ones require the node's kubelet to allow them explicitly via `--allowed-unsafe-sysctls`. The safe list includes `kernel.shm_rmid_forced`, `net.ipv4.ip_local_port_range`, `net.ipv4.ip_unprivileged_port_start`, `net.ipv4.tcp_syncookies`, `net.ipv4.ping_group_range`, and several `net.ipv4.tcp_keepalive_*` entries — but **the list grows between releases**, so check the [sysctl documentation](https://kubernetes.io/docs/tasks/administer-cluster/sysctl-cluster/) for your version rather than this paragraph.

```yaml
spec:
  securityContext:
    sysctls:
      - name: net.ipv4.tcp_keepalive_time
        value: "60"
      - name: net.ipv4.ip_local_port_range
        value: "10240 65535"
```

Critically, **`net.core.somaxconn` and everything under `net.netfilter.*` are not pod-settable** — conntrack is per-namespace in the kernel but its sysctls are treated as node-level, and `somaxconn` is on neither safe list historically. The escape hatches are:

1. **Node bootstrap** — cloud-init, Ignition, a custom AMI, or the managed node group's user data / AKS `linuxOSConfig` / GKE node system config. This is the right place for node-wide tuning and where your cell provisioning should set it.
2. **A privileged init container** that writes to `/proc/sys` in the host namespace — works, but it is a per-workload hack that quietly requires privilege escalation.
3. **A tuning DaemonSet** that applies a sysctl profile to every node. Common, and preferable to hand-editing images, but it means node configuration is eventually-consistent rather than a property of the node at join time — a race during scale-up.

For cells, prefer option 1: **the node should be correctly tuned before it ever passes a readiness check.** A node that joins with default conntrack limits and gets tuned thirty seconds later will drop traffic during those thirty seconds under load.

---

## Hands-on

**Prerequisite for labs 1, 2, and 5:** these manipulate Linux network namespaces, so they need a real Linux kernel. On macOS use a Linux VM — `lima` (`limactl start template://default && limactl shell default`), `multipass`, or `docker run --rm -it --privileged --network host ubuntu:24.04` (privileged plus host networking gives you namespace and iptables access). Labs 3 and 4 run through `kind`, which works on macOS via Docker Desktop.

Install the basics inside the Linux environment:

```bash
apt-get update && apt-get install -y \
  iproute2 iptables nftables conntrack tcpdump iputils-ping \
  netcat-openbsd curl jq ethtool bridge-utils
```

### Lab 1: build a two-namespace network by hand

This is the single best exercise in the guide. Everything a CNI plugin does, you are about to do manually.

**Part A: two namespaces connected directly by a veth pair.**

```bash
# Create two network namespaces
ip netns add ns1
ip netns add ns2

# They start empty: only a DOWN loopback
ip netns exec ns1 ip link show

# Create a veth pair (both ends currently in the host namespace)
ip link add veth1 type veth peer name veth2

# Move one end into each namespace
ip link set veth1 netns ns1
ip link set veth2 netns ns2

# Configure ns1
ip netns exec ns1 ip addr add 10.10.0.1/24 dev veth1
ip netns exec ns1 ip link set veth1 up
ip netns exec ns1 ip link set lo up

# Configure ns2
ip netns exec ns2 ip addr add 10.10.0.2/24 dev veth2
ip netns exec ns2 ip link set veth2 up
ip netns exec ns2 ip link set lo up

# Test
ip netns exec ns1 ping -c 3 10.10.0.2
```

That works because both ends are in the same /24 — pure L2 over a virtual cable. Watch it happen:

```bash
# In one shell
ip netns exec ns2 tcpdump -ni veth2
# In another
ip netns exec ns1 ping -c 3 10.10.0.2
```

You will see ARP for `10.10.0.2` followed by ICMP. This is a two-host Ethernet segment with no switch.

**Part B: add a bridge, which is what a bridged CNI does.**

Direct veth pairs do not scale — N namespaces would need N² cables. Real CNIs put one end of every pair on a bridge.

```bash
# Clean up part A
ip netns del ns1; ip netns del ns2

# Create the bridge (this is `cni0` / `cbr0` in a real cluster)
ip link add br0 type bridge
ip addr add 10.10.0.1/24 dev br0     # the bridge is the pods' gateway
ip link set br0 up

# Namespace 1
ip netns add ns1
ip link add veth1 type veth peer name br-veth1
ip link set veth1 netns ns1
ip link set br-veth1 master br0          # enslave host end to the bridge
ip link set br-veth1 up
ip netns exec ns1 ip addr add 10.10.0.11/24 dev veth1
ip netns exec ns1 ip link set veth1 up
ip netns exec ns1 ip link set lo up
ip netns exec ns1 ip route add default via 10.10.0.1

# Namespace 2
ip netns add ns2
ip link add veth2 type veth peer name br-veth2
ip link set veth2 netns ns2
ip link set br-veth2 master br0
ip link set br-veth2 up
ip netns exec ns2 ip addr add 10.10.0.12/24 dev veth2
ip netns exec ns2 ip link set veth2 up
ip netns exec ns2 ip link set lo up
ip netns exec ns2 ip route add default via 10.10.0.1

# ns1 -> ns2 (switched by the bridge)
ip netns exec ns1 ping -c 2 10.10.0.12
# ns1 -> host (the bridge IP is a host address)
ip netns exec ns1 ping -c 2 10.10.0.1

# Inspect the bridge's learned MAC table
bridge link show
bridge fdb show br br0
```

You have now built exactly what Flannel or kubenet builds on a single node.

**Part C: give the namespaces internet access — this is `ipMasq`.**

```bash
# Enable forwarding on the host
sysctl -w net.ipv4.ip_forward=1

# NAT traffic from the pod subnet out the host's real interface
UPLINK=$(ip route show default | awk '{print $5; exit}')
iptables -t nat -A POSTROUTING -s 10.10.0.0/24 ! -o br0 -j MASQUERADE
iptables -A FORWARD -i br0 -o "$UPLINK" -j ACCEPT
iptables -A FORWARD -i "$UPLINK" -o br0 -m state --state RELATED,ESTABLISHED -j ACCEPT
# Pod-to-pod across the bridge. Needed in part D, and mandatory if Docker is
# installed, because Docker sets the FORWARD policy to DROP.
iptables -A FORWARD -i br0 -o br0 -j ACCEPT

ip netns exec ns1 ping -c 3 1.1.1.1

# Watch conntrack record the NAT translation
conntrack -L | grep 10.10.0.11
```

That `conntrack -L` output is the whole story of NAT in one line: the original tuple and the reply tuple, with the source rewritten to the host IP.

**Part D: simulate the ClusterIP datapath.**

```bash
# Start a "backend" in ns2
ip netns exec ns2 nc -l -p 8080 &

# Create a fake ClusterIP and DNAT it, exactly like kube-proxy does
iptables -t nat -A PREROUTING -d 10.96.0.42/32 -p tcp --dport 80 \
  -j DNAT --to-destination 10.10.0.12:8080
iptables -t nat -A OUTPUT -d 10.96.0.42/32 -p tcp --dport 80 \
  -j DNAT --to-destination 10.10.0.12:8080

# From ns1, connect to an IP that exists nowhere
ip netns exec ns1 sh -c 'echo hello | nc -w2 10.96.0.42 80'

conntrack -L -d 10.10.0.12
```

Stop and appreciate what just happened: `10.96.0.42` is assigned to no interface anywhere, ARPs for nothing, and yet it works. That is exactly what a ClusterIP is.

**Part E: policy routing, the AWS VPC CNI pattern.**

```bash
# Give ns1's address its own routing table
echo "200 pod-table" >> /etc/iproute2/rt_tables
ip route add default via 10.10.0.1 dev br0 table 200
ip rule add from 10.10.0.11 lookup 200 priority 1536

ip rule show
# `iif` is required: 10.10.0.11 is not a local address on the host, so without it
# the kernel does an output lookup and answers "Invalid argument".
ip route get 8.8.8.8 from 10.10.0.11 iif br0   # note which table is consulted
```

**Cleanup:**

```bash
ip netns del ns1; ip netns del ns2
ip link del br0
iptables -t nat -F; iptables -F
ip rule del from 10.10.0.11 lookup 200 2>/dev/null
```

### Lab 2: write a CNI plugin as a shell script

Now automate lab 1 behind the CNI contract. Roughly 40 lines gives you a working plugin.

```bash
mkdir -p /opt/cni/bin
cat > /opt/cni/bin/toy-cni <<'EOF'
#!/bin/bash
set -e

# CNI passes network config on stdin and the operation in $CNI_COMMAND
CONFIG=$(cat)
NAME=$(echo "$CONFIG"      | jq -r '.name')
SUBNET=$(echo "$CONFIG"    | jq -r '.subnet')          # e.g. 10.22.0.0/24
GATEWAY=$(echo "$CONFIG"   | jq -r '.gateway')         # e.g. 10.22.0.1
BRIDGE=$(echo "$CONFIG"    | jq -r '.bridge // "toybr0"')
STATE_DIR="/var/lib/cni/toy/$NAME"
mkdir -p "$STATE_DIR"

log() { echo "toy-cni: $*" >> /var/log/toy-cni.log; }
log "cmd=$CNI_COMMAND id=$CNI_CONTAINERID netns=$CNI_NETNS ifname=$CNI_IFNAME"

ensure_bridge() {
  ip link show "$BRIDGE" >/dev/null 2>&1 && return
  ip link add "$BRIDGE" type bridge
  ip addr add "$GATEWAY/24" dev "$BRIDGE"
  ip link set "$BRIDGE" up
}

# Trivial IPAM: first free address in .10-.250, recorded as a file per IP
alloc_ip() {
  local base=${SUBNET%.*}
  for i in $(seq 10 250); do
    if [ ! -f "$STATE_DIR/$base.$i" ]; then
      echo "$CNI_CONTAINERID" > "$STATE_DIR/$base.$i"
      echo "$base.$i"; return 0
    fi
  done
  echo '{"cniVersion":"1.0.0","code":100,"msg":"no IPs left"}'; exit 1
}

free_ip() { grep -l "$CNI_CONTAINERID" "$STATE_DIR"/* 2>/dev/null | xargs -r rm -f; }

case "$CNI_COMMAND" in
ADD)
  ensure_bridge
  IP=$(alloc_ip)
  HOST_IF="toy$(echo "$CNI_CONTAINERID" | cut -c1-11)"

  ip link add "$HOST_IF" type veth peer name "$CNI_IFNAME-tmp"
  ip link set "$CNI_IFNAME-tmp" netns "$CNI_NETNS"
  ip link set "$HOST_IF" master "$BRIDGE"
  ip link set "$HOST_IF" up

  # CNI_NETNS is a PATH (/var/run/netns/xxx, or /proc/<pid>/ns/net from
  # containerd). `ip netns exec` only accepts a bare name -- it always prepends
  # /var/run/netns -- so enter the namespace with nsenter instead.
  nsenter --net="$CNI_NETNS" ip link set "$CNI_IFNAME-tmp" name "$CNI_IFNAME"
  nsenter --net="$CNI_NETNS" ip link set lo up
  nsenter --net="$CNI_NETNS" ip link set "$CNI_IFNAME" up
  nsenter --net="$CNI_NETNS" ip addr add "$IP/24" dev "$CNI_IFNAME"
  nsenter --net="$CNI_NETNS" ip route add default via "$GATEWAY"

  MAC=$(nsenter --net="$CNI_NETNS" cat "/sys/class/net/$CNI_IFNAME/address")
  cat <<JSON
{
  "cniVersion": "1.0.0",
  "interfaces": [{"name":"$CNI_IFNAME","mac":"$MAC","sandbox":"$CNI_NETNS"}],
  "ips": [{"address":"$IP/24","gateway":"$GATEWAY","interface":0}],
  "routes": [{"dst":"0.0.0.0/0","gw":"$GATEWAY"}]
}
JSON
  ;;
DEL)
  # DEL must succeed even if everything is already gone
  free_ip
  HOST_IF="toy$(echo "$CNI_CONTAINERID" | cut -c1-11)"
  ip link del "$HOST_IF" 2>/dev/null || true
  echo '{"cniVersion":"1.0.0"}'
  ;;
CHECK)  echo '{"cniVersion":"1.0.0"}' ;;
STATUS) echo '{"cniVersion":"1.0.0"}' ;;
VERSION)
  echo '{"cniVersion":"1.0.0","supportedVersions":["0.4.0","1.0.0","1.1.0"]}'
  ;;
*)
  echo "{\"cniVersion\":\"1.0.0\",\"code\":4,\"msg\":\"unknown command $CNI_COMMAND\"}"
  exit 4
  ;;
esac
EOF
chmod +x /opt/cni/bin/toy-cni
```

Test it by invoking it exactly the way containerd would:

```bash
# Create a namespace to act as the "pod sandbox"
ip netns add fakepod
NETNS_PATH=/var/run/netns/fakepod

CONFIG='{"cniVersion":"1.0.0","name":"toynet","type":"toy-cni",
         "subnet":"10.22.0.0/24","gateway":"10.22.0.1"}'

# ADD
echo "$CONFIG" | env \
  CNI_COMMAND=ADD \
  CNI_CONTAINERID=abcdef0123456789 \
  CNI_NETNS="$NETNS_PATH" \
  CNI_IFNAME=eth0 \
  CNI_PATH=/opt/cni/bin \
  /opt/cni/bin/toy-cni | jq .

ip netns exec fakepod ip addr
ip netns exec fakepod ip route
ip netns exec fakepod ping -c2 10.22.0.1

# DEL
echo "$CONFIG" | env \
  CNI_COMMAND=DEL \
  CNI_CONTAINERID=abcdef0123456789 \
  CNI_NETNS="$NETNS_PATH" \
  CNI_IFNAME=eth0 \
  CNI_PATH=/opt/cni/bin \
  /opt/cni/bin/toy-cni

ip netns del fakepod
```

Things to notice, because each is a real production bug class:

- **IPAM state is a directory on this node's disk.** Wipe it and you double-allocate. Real IPAM is either a cluster-wide CRD (Cilium, Calico) or a cloud API (AWS `ipamd`) precisely to avoid this. The CNI `GC` verb exists to reconcile leaks.
- **DEL must be idempotent.** The `|| true` on the link delete is not laziness; a DEL that fails leaves the pod stuck terminating forever.
- **Nothing here is atomic.** If the process is killed between allocating the IP and creating the veth, the IP is leaked. Real plugins order operations so the recoverable state is always consistent.
- **The plugin runs as a short-lived process per pod.** At high pod churn, exec cost is real.

To actually run it under containerd, drop a conflist and restart the runtime:

```bash
mkdir -p /etc/cni/net.d
cat > /etc/cni/net.d/10-toynet.conflist <<'EOF'
{
  "cniVersion": "1.0.0",
  "name": "toynet",
  "plugins": [
    { "type": "toy-cni", "subnet": "10.22.0.0/24", "gateway": "10.22.0.1" }
  ]
}
EOF
```

### Lab 3: kind cluster, read the iptables rules for a Service

```bash
# Install kind and kubectl first (https://kind.sigs.k8s.io/)
cat > /tmp/kind-3node.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
kind create cluster --name netlab --config /tmp/kind-3node.yaml

kubectl create deployment web --image=nginx --replicas=3
kubectl expose deployment web --port=80 --target-port=80
kubectl get svc web -o wide
kubectl get endpointslices -l kubernetes.io/service-name=web -o yaml
```

Now go inside a node and read the datapath:

```bash
docker exec -it netlab-worker bash

# What mode is kube-proxy in?
iptables-save -t nat | grep -c KUBE            # non-zero => iptables mode
nft list tables 2>/dev/null | grep kube-proxy  # present => nftables mode

# Find the Service's chain (substitute the ClusterIP from `kubectl get svc web`)
CLUSTER_IP=10.96.x.y
iptables-save -t nat | grep "$CLUSTER_IP"

# Follow the chain: KUBE-SERVICES -> KUBE-SVC-xxx
iptables -t nat -L KUBE-SERVICES -n | grep "$CLUSTER_IP"
iptables -t nat -L KUBE-SVC-<HASH> -n -v

# And the endpoints
iptables -t nat -L KUBE-SEP-<HASH> -n -v

# Watch the rules change as you scale
```

From a second terminal:

```bash
kubectl scale deployment web --replicas=6
```

Then re-read `KUBE-SVC-<HASH>`. The `--probability` values shift from `0.33333 / 0.50000 / (fallthrough)` to `0.16666 / 0.20000 / 0.25000 / 0.33333 / 0.50000 / (fallthrough)`. Confirm for yourself that those compose to a uniform 1/6 each — this is the mechanism, and being able to reconstruct it from a rule dump is the skill.

Count the total rule cost:

```bash
iptables-save -t nat | wc -l
# then create 200 services and count again
for i in $(seq 1 200); do
  kubectl create service clusterip svc-$i --tcp=80:80 >/dev/null
done
iptables-save -t nat | wc -l
```

Extrapolate to 5,000 Services and you have the KEP-3866 motivation in your own terminal.

Also inspect a pod's namespace from the node:

```bash
docker exec -it netlab-worker bash
PID=$(crictl inspectp "$(crictl pods --name web -q | head -1)" | jq -r .info.pid)
nsenter -t "$PID" -n ip addr
nsenter -t "$PID" -n ip route
nsenter -t "$PID" -n cat /etc/resolv.conf     # see ndots:5 and the search list
```

Clean up with `kind delete cluster --name netlab`.

### Lab 4: Cilium on kind with kube-proxy replacement, plus Hubble

```bash
cat > /tmp/kind-cilium.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
networking:
  disableDefaultCNI: true      # no kindnet; Cilium will provide the CNI
  kubeProxyMode: none          # no kube-proxy at all
EOF
kind create cluster --name cilium-lab --config /tmp/kind-cilium.yaml

# Nodes stay NotReady until a CNI lands - this is the chicken-and-egg from earlier
kubectl get nodes

# Install the Cilium CLI, then:
cilium install \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost=cilium-lab-control-plane \
  --set k8sServicePort=6443 \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true

cilium status --wait
cilium connectivity test        # takes several minutes; exercises the whole datapath
```

Inspect the eBPF datapath:

```bash
CILIUM_POD=$(kubectl -n kube-system get pods -l k8s-app=cilium -o name | head -1)

kubectl -n kube-system exec -it "$CILIUM_POD" -- cilium-dbg status --verbose
kubectl -n kube-system exec -it "$CILIUM_POD" -- cilium-dbg service list
kubectl -n kube-system exec -it "$CILIUM_POD" -- cilium-dbg bpf lb list
kubectl -n kube-system exec -it "$CILIUM_POD" -- cilium-dbg endpoint list

# The eBPF programs themselves
kubectl -n kube-system exec -it "$CILIUM_POD" -- bpftool prog show | head -30
kubectl -n kube-system exec -it "$CILIUM_POD" -- bpftool net show
```

Prove there are no Service iptables rules:

```bash
docker exec -it cilium-lab-worker bash -c 'iptables-save -t nat | grep -c KUBE-SVC || echo "no kube-proxy service rules"'
```

Now watch flows and enforce policy:

```bash
cilium hubble port-forward &
hubble status
hubble observe -f &                                # live flow feed; blocks, so
                                                   # background it or use a second
                                                   # terminal for the rest

kubectl create deployment web --image=nginx --replicas=2
kubectl expose deployment web --port=80
kubectl run client --image=nicolaka/netshoot -it --rm -- curl -s -o /dev/null -w '%{http_code}\n' http://web

# Apply default-deny and watch the drops appear with a reason
kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: deny-all }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
EOF

# Re-run the client AFTER the policy exists -- the drops you want to see are
# generated by new traffic, not retroactively by the flows above.
kubectl run client2 --image=nicolaka/netshoot -it --rm -- curl -m 5 -s http://web

hubble observe --verdict DROPPED --last 20
```

That last command is the payoff: a dropped packet with the policy verdict attached, instead of a silent black hole. Compare the debugging experience with lab 3.

```bash
kind delete cluster --name cilium-lab
```

### Lab 5: break MTU on purpose and watch the black hole

The most instructive failure in networking, reproduced in a minute.

```bash
# Rebuild the lab 1 Part B/C topology (bridge, ns1, ns2, NAT), then:

# Baseline: large transfers work
ip netns exec ns1 ping -c 2 -s 1400 -M do 10.10.0.12

# Now create the mismatch: the pod thinks it can send 1500,
# but the path only carries 1400.
ip netns exec ns1 ip link set veth1 mtu 1500
ip link set br-veth1 mtu 1400
ip link set br0 mtu 1400
ip link set br-veth2 mtu 1400
ip netns exec ns2 ip link set veth2 mtu 1400

# Small packets: fine.
ip netns exec ns1 ping -c 2 -s 100 -M do 10.10.0.12

# Large packets with DF set: fail.
ip netns exec ns1 ping -c 2 -s 1450 -M do 10.10.0.12
```

You get `Frag needed and DF set (mtu = 1400)` — PMTUD working correctly, because ICMP is allowed. Now break PMTUD, which is what a real firewall does:

```bash
# Drop the ICMP that tells the sender to shrink
ip netns exec ns1 iptables -A INPUT -p icmp --icmp-type fragmentation-needed -j DROP

# Now the failure is silent: no error, just a hang
ip netns exec ns1 ping -c 3 -W 2 -s 1450 -M do 10.10.0.12
```

Reproduce it over TCP, which is what actually bites you in production:

```bash
# Serve a large payload from ns2
ip netns exec ns2 sh -c 'head -c 5000000 /dev/urandom > /tmp/big.bin; \
  python3 -m http.server 8080 --directory /tmp' &

# Small request, large response: the handshake succeeds and the transfer stalls
ip netns exec ns1 curl -m 10 -o /dev/null http://10.10.0.12:8080/big.bin
```

The connection establishes (SYN packets are tiny), headers may arrive, and then the transfer hangs until the timeout. **This is why "the health check is green but the app is broken" is an MTU symptom.** Watch it:

```bash
ip netns exec ns1 tcpdump -ni veth1 'tcp port 8080'
# You will see the same segment retransmitted repeatedly with no ACK.
```

Then fix it two ways and confirm each works:

```bash
# Fix 1: correct the MTU (the right fix)
ip netns exec ns1 ip link set veth1 mtu 1400

# Fix 2: MSS clamping (the fix for traffic you don't control the far end of)
ip netns exec ns1 ip link set veth1 mtu 1500       # break it again

# ns1 and ns2 are on the same /24, so their traffic is BRIDGED, not routed, and
# never reaches the iptables FORWARD chain by default. This is the same
# net.bridge.bridge-nf-call-iptables=1 that kubeadm makes you set.
modprobe br_netfilter
sysctl -w net.bridge.bridge-nf-call-iptables=1

iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN \
  -j TCPMSS --clamp-mss-to-pmtu
ip netns exec ns1 curl -m 10 -o /dev/null http://10.10.0.12:8080/big.bin
```

Cleanup as in lab 1.

---

## Debugging playbook

Ordered runbooks. Work top to bottom; each step either isolates the fault or eliminates a layer.

### Pod cannot reach a Service

```bash
# 0. Establish the facts
kubectl get pod <pod> -o wide                       # node, pod IP, phase
kubectl get svc <svc> -o wide                       # ClusterIP, ports, selector
kubectl get endpointslices -l kubernetes.io/service-name=<svc> -o yaml

# 1. Are there ANY ready endpoints?  This is the answer 50% of the time.
#    Empty endpoints => not a network problem. It is a selector mismatch,
#    a failing readiness probe, or a port name mismatch.
kubectl get endpointslices -l kubernetes.io/service-name=<svc> \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses}{" ready="}{.conditions.ready}{"\n"}{end}'

# 2. Does the Service's targetPort match the container's containerPort NAME?
kubectl get svc <svc> -o jsonpath='{.spec.ports[*].targetPort}{"\n"}'

# 3. Test from inside the pod, bypassing layers one at a time
kubectl exec -it <pod> -- sh
  #   a) direct to a pod IP:port     -> works? then CNI is fine, Service layer is broken
  #   b) to the ClusterIP:port       -> works? then DNS is broken
  #   c) to the Service name         -> the full path
```

If (a) works and (b) fails, the fault is kube-proxy or its replacement:

```bash
# On the pod's node
kubectl -n kube-system logs -l k8s-app=kube-proxy --tail=100 | grep -iE 'error|fail|sync'

# Are the rules actually programmed?
iptables-save -t nat | grep <CLUSTER_IP>            # iptables mode
nft list table ip kube-proxy | grep <CLUSTER_IP>    # nftables mode
ipvsadm -Ln | grep -A5 <CLUSTER_IP>                 # ipvs mode

# Cilium / no kube-proxy
kubectl -n kube-system exec -it <cilium-pod> -- cilium-dbg service list | grep <CLUSTER_IP>
kubectl -n kube-system exec -it <cilium-pod> -- cilium-dbg monitor -t drop
```

If (a) also fails, the fault is the CNI or NetworkPolicy:

```bash
kubectl get netpol -A                               # is anything selecting this pod?
# On the node, enter the pod netns with the host's tools:
PID=$(crictl inspectp $(crictl pods --name <pod> -q | head -1) | jq -r .info.pid)
nsenter -t $PID -n ip route
nsenter -t $PID -n ip route get <target-pod-ip>
nsenter -t $PID -n tcpdump -ni eth0 host <target-pod-ip>
# ...and on the target's node, tcpdump the receiving veth. If packets leave
# and never arrive, it is the underlay/overlay. If they arrive and get no
# reply, it is policy or the app.
```

Fast Cilium equivalent of all of the above:

```bash
hubble observe --from-pod <ns>/<pod> --to-service <ns>/<svc> --last 50
hubble observe --verdict DROPPED --from-pod <ns>/<pod> --last 50
```

### Pod cannot reach the internet

```bash
# 1. Split DNS from connectivity immediately
kubectl exec -it <pod> -- getent hosts example.com     # DNS
kubectl exec -it <pod> -- curl -sS -m5 https://1.1.1.1 # raw IP connectivity

# 2. If raw IP fails, check egress policy first (cheapest)
kubectl get netpol -A -o yaml | grep -A20 Egress

# 3. Is the traffic being SNAT'd at all?
#    On the node:
iptables -t nat -L POSTROUTING -n -v | grep -i masq
conntrack -L -s <pod-ip> | head

# 4. Node-level: can the NODE reach the internet?
#    If not, it is a route table / NAT gateway / firewall problem, not Kubernetes.
curl -sS -m5 -o /dev/null -w '%{http_code}\n' https://1.1.1.1

# 5. Cloud layer
#    AWS:   route table has 0.0.0.0/0 -> nat-xxxx? SG egress? NACL?
#           NAT GW ErrorPortAllocation metric?
#    GCP:   Cloud NAT configured for this subnet? min ports per VM?
#    Azure: outbound rules / NAT gateway on the subnet? SNAT port exhaustion?

# 6. MTU (see below) if it "connects but hangs"
```

### Intermittent DNS failures

Signature: a small percentage of requests fail, p99 latency shows a hard **5000 ms** plateau, errors are `Temporary failure in name resolution` or connection timeouts to hostnames that usually work.

```bash
# 1. Confirm the conntrack race rather than a CoreDNS problem
conntrack -S | tr ' ' '\n' | grep -E 'insert_failed|drop'
#    Non-zero and climbing insert_failed => the UDP source-port race.

# 2. Is the table simply full?
dmesg -T | grep -i 'nf_conntrack: table full'
echo "$(conntrack -C) / $(sysctl -n net.netfilter.nf_conntrack_max)"

# 3. How much of the table is DNS?
conntrack -L -p udp --dport 53 2>/dev/null | wc -l

# 4. Is CoreDNS itself unhealthy or throttled?
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=200 | grep -iE 'error|timeout|SERVFAIL|i/o'
kubectl -n kube-system top pod -l k8s-app=kube-dns
#    CoreDNS metrics: coredns_dns_request_duration_seconds,
#    coredns_dns_responses_total{rcode="SERVFAIL"}, coredns_forward_healthcheck_failures_total

# 5. Quantify the ndots tax
kubectl exec -it <pod> -- cat /etc/resolv.conf
kubectl exec -it <pod> -- sh -c 'time getent hosts api.example.com'
#    Compare with the trailing-dot form:
kubectl exec -it <pod> -- sh -c 'time getent hosts api.example.com.'

# 6. Watch the actual queries
PID=$(crictl inspectp $(crictl pods --name <pod> -q | head -1) | jq -r .info.pid)
nsenter -t $PID -n tcpdump -ni eth0 -s0 port 53
```

Fixes in order: deploy **NodeLocal DNSCache** (removes the race structurally), reduce `ndots` or use FQDNs for external hostnames, raise `nf_conntrack_max`, add `single-request-reopen`, scale CoreDNS and enable its autoscaler.

### MTU black holes

Signature: TCP connects, small responses work, large responses or uploads hang and time out. Health checks pass. Often appears only for *some* destinations (those beyond a lower-MTU hop) or only after enabling encryption/encapsulation.

```bash
# 1. Find the actual working MTU along the path
ping -M do -s 1472 <dest>     # 1472 + 28 = 1500
ping -M do -s 1422 <dest>     # 1422 + 28 = 1450 (VXLAN)
ping -M do -s 1372 <dest>     # 1372 + 28 = 1400
#    Binary-search the largest size that succeeds; add 28 for the IP+ICMP headers.

# 2. Compare configured MTUs at every hop
ip link show                                    # node interfaces
ip -d link show <vxlan-or-tunnel-dev>           # encap device
PID=$(crictl inspectp $(crictl pods --name <pod> -q | head -1) | jq -r .info.pid)
nsenter -t $PID -n ip link show eth0            # pod MTU
#    Pod MTU must be <= (node MTU - encapsulation overhead).

# 3. Is PMTUD being blocked?
tcpdump -ni any 'icmp[icmptype] == 3 and icmp[icmpcode] == 4'
#    Silence here while large packets fail == ICMP is being dropped somewhere.

# 4. Cached path MTU the kernel already learned
ip route get <dest>                             # look for `mtu NNNN` in the output
ip route flush cache

# 5. Confirm at the TCP layer
tcpdump -ni any "tcp[tcpflags] & tcp-syn != 0" -vv | grep -o 'mss [0-9]*'
```

Fixes: correct the CNI's MTU setting and recycle nodes; add MSS clamping for external traffic; allow ICMP type 3 code 4 and ICMPv6 type 2 in security groups and NetworkPolicies; as a stopgap, `sysctl -w net.ipv4.tcp_mtu_probing=1`.

### conntrack exhaustion

```bash
# 1. Confirm
dmesg -T | grep -i 'nf_conntrack: table full, dropping packet'
conntrack -C
sysctl -n net.netfilter.nf_conntrack_max
conntrack -S      # look at drop, early_drop, insert_failed per CPU

# 2. Find what is filling it
conntrack -L 2>/dev/null | awk '{print $1, $3}' | sort | uniq -c | sort -rn | head
conntrack -L -p udp 2>/dev/null | wc -l
conntrack -L -p tcp --state TIME_WAIT 2>/dev/null | wc -l

# 3. Which pod?
conntrack -L 2>/dev/null | grep -oP 'src=\K[0-9.]+' | sort | uniq -c | sort -rn | head

# 4. Immediate relief
sysctl -w net.netfilter.nf_conntrack_max=1048576
sysctl -w net.netfilter.nf_conntrack_buckets=262144
sysctl -w net.netfilter.nf_conntrack_tcp_timeout_established=3600
sysctl -w net.netfilter.nf_conntrack_tcp_timeout_time_wait=30
```

Then fix it properly: kube-proxy's `--conntrack-max-per-core` and `--conntrack-min` in the cell's kube-proxy config, node bootstrap sysctls, and application-level connection pooling. Note that conntrack pressure is a symptom of connection churn — an app opening a new connection per request is the root cause, and NodeLocal DNSCache removes the largest single contributor (UDP DNS entries).

### General-purpose commands worth memorising

```bash
ss -tanp state established | wc -l        # connection count by state
ss -s                                      # socket summary, TIME_WAIT counts
ss -tlnp                                   # what is listening
ss -tin                                    # per-socket TCP info: rtt, cwnd, retrans
ethtool -S eth0 | grep -iE 'drop|err|discard|miss'   # NIC-level drops
ethtool -g eth0                            # ring buffer sizes
cat /proc/net/softnet_stat                 # column 2 non-zero => netdev backlog drops
nstat -az | grep -iE 'TcpRetrans|ListenDrops|ListenOverflows'
netstat -s | grep -iE 'listen|overflow|prune|collapse'
ip -s link show eth0                       # interface counters
tc -s qdisc show dev eth0                  # queueing discipline drops
```

`TcpExtListenOverflows` and `TcpExtListenDrops` climbing means `somaxconn`/backlog is too small. Column 2 of `/proc/net/softnet_stat` climbing means `netdev_max_backlog` is too small.

---

## Production gotchas

1. **Your kube-proxy mode is about to change under you.** [KEP-5343](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/5343-nftables-to-default) makes `nftables` the default in **v1.40**; v1.37 already warns when the mode is defaulted. If a cell's kube-proxy config omits `mode`, that upgrade silently swaps the datapath — and on a node with a kernel older than **5.13**, kube-proxy will fail to start and the node breaks. Pin `mode: iptables` (or migrate to `nftables`) explicitly in every cell today.

2. **IPVS is on a removal schedule, and the dates are published.** Per [KEP-5495](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/5495-deprecate-ipvs-mode-in-kube-proxy/README.md): warnings from v1.35, a `KubeProxyIPVS` gate in v1.37, gate defaults to false in **v1.40** (kube-proxy exits with an error), code removed in **v1.43**. Any cell on IPVS needs a migration plan now, not in 2028.

3. **A cluster can accept NetworkPolicy objects and enforce none of them.** The API server stores them regardless of whether the CNI implements them. Flannel ignores them entirely; an **EKS cluster ignores them until network policy is explicitly enabled on the VPC CNI add-on** ([AWS](https://aws.amazon.com/blogs/containers/amazon-vpc-cni-now-supports-kubernetes-network-policies/)); legacy GKE needs the Calico add-on. There is no error, no event, no warning. Make "apply deny-all, assert curl fails" a mandatory post-provision conformance test for every cell.

4. **Default-deny egress without a DNS allow rule takes down the entire namespace at once.** Every application fails simultaneously with what looks like a DNS outage. Ship the `allow-dns-egress` policy in the same commit as the default-deny, never after it.

5. **`ndots:5` turns one external hostname lookup into ten DNS queries.** Five search-domain expansions × A and AAAA. This is the dominant load on CoreDNS at scale and a latency tax on every outbound connection ([DNS docs](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)). Fix with trailing dots or per-pod `dnsConfig`, and do not globally set `ndots:1` without auditing for single-dot names like `svc.namespace`.

6. **The 5-second DNS timeout is a kernel conntrack race, not a CoreDNS problem.** Parallel A/AAAA queries collide on conntrack insert and one packet is dropped silently ([Weaveworks analysis](https://www.weave.works/blog/racy-conntrack-and-dns-lookup-timeouts), secondary; [kubernetes#56903](https://github.com/kubernetes/kubernetes/issues/56903)). Scaling CoreDNS does nothing. [NodeLocal DNSCache](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/) eliminates it by removing the DNAT entirely — but note that restarting the node-local-dns DaemonSet causes a node-wide DNS outage unless you use the dual-IP configuration. Put that in the upgrade runbook.

7. **AWS pod density is an ENI property, not a memory property.** `maxPods = maxENIs × (IPsPerENI − 1) + 2`. Enabling **custom networking** removes the primary ENI from the pod pool and lowers max-pods, and forgetting to recompute `--max-pods` leaves pods stuck in `ContainerCreating` with no obvious cause ([EKS custom networking](https://docs.aws.amazon.com/eks/latest/userguide/cni-custom-network.html)). Prefix delegation raises the ceiling to 110 (≤30 vCPU) or 250 (>30 vCPU) but requires **contiguous /28 blocks** — a fragmented long-lived subnet fails allocation while reporting free IPs ([prefix docs](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/docs/prefix-and-ip-target.md)).

8. **Tightening `WARM_IP_TARGET` to save IPs trades one outage for another.** A small warm pool means `ipamd` calls the EC2 API on nearly every pod launch, and EC2 API throttling then manifests as slow or failed pod starts across the whole cell. Tune warm-pool settings against pod churn rate, not just IP budget, and alarm on `ipamd` API errors.

9. **GKE's default of 110 pods per node burns a /24 per node.** GKE allocates the smallest CIDR fitting **2×** max-pods, so a /16 pod range caps the cluster at 256 nodes and then fails with `IP_SPACE_EXHAUSTED`. `--max-pods-per-node` is set at node pool creation and is **immutable**; setting it to 32 (a /26 per node) quadruples the node ceiling for the same range ([multi-pod-CIDR docs](https://cloud.google.com/kubernetes-engine/docs/how-to/multi-pod-cidr)).

10. **GKE Dataplane V2 cannot be enabled on an existing cluster.** It is create-time only ([docs](https://cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2)). For cell lifecycle that means the dataplane is a property of the cell template and changing it is a cell *replacement* with workload migration, not an upgrade. Plan it as such.

11. **Azure kubenet retires 31 March 2028, and Azure CNI Node Subnet pre-reserves pod IPs.** kubenet's 400-route-table limit also caps cluster size. Node Subnet mode reserves `maxPods` addresses per node **whether the pods exist or not**, which exhausts VNets fast. Azure CNI Overlay is the migration target ([AKS legacy CNI docs](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni)). Also track Azure NPM: unsupported on Windows since 30 September 2026, Linux support ending 30 September 2028.

12. **Pod CIDR and Service CIDR are effectively immutable.** Changing either requires rebuilding the cluster. Allocate them from a **global, non-overlapping plan across every cell in every cloud on day one**, even for cells that will "never" need to talk to each other. Retrofitting non-overlapping CIDRs across a live fleet is the most expensive networking remediation there is, and overlapping CIDRs permanently rule out Cluster Mesh and plain VPC peering.

13. **MTU misconfiguration produces green health checks and broken applications.** Small packets succeed, large transfers hang. VXLAN costs 50 bytes, WireGuard 80. Enabling encryption on a working cluster silently reduces the usable MTU and creates the black hole days later. Blocking ICMP — which almost every hardened security group does — disables PMTUD and turns a clean error into a hang ([RFC 1191](https://www.rfc-editor.org/rfc/rfc1191)). Allow ICMP type 3 code 4 and ICMPv6 type 2 everywhere.

14. **`externalTrafficPolicy: Local` preserves the source IP and can create severe traffic imbalance.** Nodes with no local endpoint fail the `healthCheckNodePort` and receive nothing, so if replicas land on 3 of 30 nodes, those 3 take all external traffic ([source IP tutorial](https://kubernetes.io/docs/tutorials/services/source-ip/)). Pair it with topology spread constraints or a DaemonSet.

15. **`internalTrafficPolicy: Local` and `trafficDistribution` are not the same thing.** `internalTrafficPolicy: Local` **drops** traffic when no local endpoint exists; `trafficDistribution: PreferSameZone`/`PreferSameNode` is a **preference with cluster-wide fallback** ([KEP-3015](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/3015-prefer-same-node), GA in v1.35). Reaching for the former when you wanted the latter causes intermittent hard failures during rollouts.

16. **The CNI daemonset must bootstrap the network it depends on.** Nodes report `NotReady` with `cni plugin not initialized` until a conflist appears in `/etc/cni/net.d`. The CNI pods therefore need `hostNetwork: true` and tolerations for the not-ready taint. Get this wrong and the cell provisions but never converges — the most common brand-new-cell failure.

17. **`max_conf_num = 1` in containerd means a stale conflist shadows your CNI.** A leftover `10-flannel.conflist` from a previous install wins lexically and quietly. Cell teardown must clean `/etc/cni/net.d` and `/opt/cni/bin`, and cell rebuild on recycled hosts must not assume a clean disk.

18. **`host-local` IPAM state lives on the node's disk.** Lose `/var/lib/cni` (disk replacement, node reimage with a reattached volume) and the plugin reallocates addresses that are still in use, producing duplicate pod IPs and traffic going to the wrong pod. The CNI spec's **GC verb** (v1.1.0) exists precisely to reconcile leaked reservations; check whether your CNI implements it ([CNI spec](https://www.cni.dev/docs/spec/)).

19. **A CNI `DEL` that fails leaks an IP forever and hangs pod deletion.** DEL must succeed when the resource is already gone. If you ever write or patch a plugin, this is the invariant to protect.

20. **PROXY protocol must be enabled on both the load balancer and the backend, atomically.** Enabled on the LB only, the backend parses `PROXY TCP4 ...` as an HTTP request line and returns 400. Enabled on the backend only, every connection fails. Rollouts where LB and backend versions change independently are the danger window ([PROXY protocol spec](https://www.haproxy.org/download/2.8/doc/proxy-protocol.txt)).

21. **NAT gateway SNAT port exhaustion looks like the remote service is down.** Roughly 55,000 concurrent connections per unique destination endpoint on an AWS NAT Gateway; exceeding it surfaces as `ErrorPortAllocation` and connection failures. A cell where thousands of pods poll one third-party API will hit it. Use VPC endpoints, connection pooling, and multiple NAT gateways; alarm on the metric.

22. **`ingress-nginx` reached end of life in March 2026.** The `Ingress` API is fine; the most-deployed controller implementing it is not receiving security fixes. Audit what your cells run and plan a Gateway API migration with [ingress2gateway](https://kubernetes.io/blog/2026/03/20/ingress2gateway-1-0-release).

23. **`Endpoints` was deprecated in v1.33 — stop writing controllers against it.** Any cell lifecycle tooling that reconciles on service backends should watch EndpointSlices, which are sharded (100 per slice by default), dual-stack aware, and carry the `serving`/`terminating` conditions that make graceful shutdown work ([docs](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/)).

24. **Node sysctl tuning applied by a DaemonSet is a race during scale-up.** A node that joins with default `nf_conntrack_max` and gets tuned thirty seconds later drops traffic for thirty seconds under load. Tune at node bootstrap — AMI, cloud-init, Ignition, managed node group user data, AKS `linuxOSConfig`, or GKE node system config — so the node is correct before it passes readiness.

25. **A CIS-hardening script that sets `net.ipv6.conf.all.disable_ipv6=1` silently breaks dual-stack**, and `net.ipv4.conf.all.rp_filter=1` (strict reverse-path filtering) silently breaks CNIs that rely on asymmetric routing. Both are common in baseline node images and both produce failures far from their cause.

---

## How this shows up in cell lifecycle

Mapping the above onto the thing you actually own.

**Cell provisioning — the IP plan is the cell's most permanent decision.** Before any Helm template renders, a cell needs a pod CIDR, a service CIDR, node subnets, and a max-pods-per-node value. Pod and service CIDRs are effectively immutable; max-pods-per-node is immutable per GKE node pool and drives AWS ENI math and Azure address reservation. These belong in a **fleet-wide IPAM registry**, not in per-cell defaults, because the moment two cells need to talk — Cluster Mesh, Submariner, or plain peering — overlapping CIDRs make it impossible. Treat CIDR allocation as a global scheduler problem with a durable record, and make cell creation fail loudly if it cannot get a non-overlapping block.

**Cell provisioning — the bootstrap ordering is a real dependency graph.** A cell converges in this order: nodes join and report `NotReady` with `cni plugin not initialized` → the CNI DaemonSet schedules (only possible because it is `hostNetwork: true` and tolerates the not-ready taint) → conflists land in `/etc/cni/net.d` → nodes go `Ready` → CoreDNS schedules (it needs pod networking) → everything else. Any cell health check that runs before this settles will report a false failure; any CNI manifest that forgets the toleration produces a cell that provisions cleanly and never converges. Encode the expected convergence sequence as explicit gates rather than a fixed sleep.

**Helm as templating only changes your migration story.** Without Helm release state, you have no `helm upgrade` to diff against and no rollback of record — which is fine for stateless manifests and dangerous for CNIs specifically, because CNI upgrades involve CRDs, DaemonSet rollouts that touch the datapath, and sometimes eBPF map format changes. Two consequences: own CRD lifecycle explicitly (Helm does not upgrade CRDs anyway, so you were going to own this regardless), and make CNI version transitions an explicit, ordered procedure with its own validation rather than "apply the rendered manifests."

**Cell upgrade — the datapath is the risky part.** Three specific landmines from this guide, all with dates: kube-proxy's default flips to `nftables` in **v1.40** unless every cell pins `mode` explicitly; IPVS mode stops working in **v1.40** and disappears in **v1.43**; and nftables mode needs kernel **5.13+**, so the node image is part of the upgrade's compatibility matrix. Add a pre-upgrade check that asserts, per cell: kube-proxy mode is explicitly set, the node kernel supports the mode, and the CNI version is compatible with the target Kubernetes version.

**Cell upgrade — node recycling has a conntrack and endpoint story.** Draining a node moves pods; the Service's EndpointSlices update; kube-proxy on every *other* node reprograms; and in-flight connections to the drained pods rely on the `terminating`/`serving` conditions plus `terminationGracePeriodSeconds` to finish cleanly. Stale conntrack entries pointing at a dead pod IP are a real source of post-upgrade errors — some CNIs flush them, some do not. If your upgrade produces a burst of connection resets that clears after a minute, that is what you are looking at.

**Cell teardown leaks cloud resources unless you enumerate them.** Deleting the Kubernetes objects does not delete what the cloud controller manager created on their behalf: load balancers, target groups, security groups, Azure route table entries, GCP forwarding rules, and — on AWS specifically — **ENIs left attached or detached-but-not-deleted** when nodes go away abruptly. Cell teardown should reconcile against the cloud API by tag and assert zero remaining network objects, not just assert the cluster is gone. Budget for the fact that leaked ENIs consume subnet IPs that the *next* cell in that VPC needs.

**The multi-cloud abstraction has a real boundary, and you should draw it explicitly.** What genuinely portable: the Kubernetes network model contract, Service semantics, EndpointSlices, DNS behaviour, NetworkPolicy's *API* (not its enforcement), Gateway API, and every sysctl in this guide. What is irreducibly per-cloud: IPAM model, max-pods math, IP exhaustion failure mode and its alarms, load balancer provisioning and annotations, egress NAT and its port limits, and MTU. Do not try to template your way past the second list — surface those as named, per-cloud cell parameters with per-cloud validation, so a reviewer can see that an AWS cell and a GKE cell are configured differently *on purpose*.

**The one platform decision worth making deliberately:** running Cilium on all three clouds gives you one dataplane, one policy language, one observability tool (Hubble), one multi-cluster mechanism (Cluster Mesh), and one encryption story — at the cost of owning the CNI yourself instead of consuming the cloud's supported one. Managed variants (GKE Dataplane V2, Azure CNI Powered by Cilium) split the difference on two of three clouds. Given a fleet spanning AWS, GCP, and Azure with shared ownership of networking, this is the highest-leverage architectural question your team will answer, and it is worth having a real opinion on before it is asked.

---

## Learning path

**Day 1 — build the primitives with your own hands.**

Do **Lab 1** end to end, all five parts, in a Linux VM. Do not skim it; type the commands and read every `ip route`, `ip rule`, and `conntrack -L` output. By the end you should be able to explain, without notes, what a veth pair is, why the pod gets a `/32` and a fake gateway, what a bridge does that routing does not, and why a ClusterIP works despite existing on no interface. Then do **Lab 5** and watch the MTU black hole with your own `tcpdump`. These two labs are 80% of the intuition.

Read: [`network_namespaces(7)`](https://man7.org/linux/man-pages/man7/network_namespaces.7.html), [`veth(4)`](https://man7.org/linux/man-pages/man4/veth.4.html), and the [Kubernetes cluster networking](https://kubernetes.io/docs/concepts/cluster-administration/networking/) page — specifically the four-point contract.

**Week 1 — connect the primitives to Kubernetes.**

- Do **Lab 2** (write the toy CNI) and then read the [CNI spec](https://www.cni.dev/docs/spec/) top to bottom. It is short. You will find you already know most of it.
- Do **Lab 3** and trace a real Service through the iptables chains. Scale the deployment and watch the probabilities change. Create 200 Services and watch the rule count.
- Do **Lab 4** and compare the debugging experience: `hubble observe --verdict DROPPED` versus reading `iptables-save`.
- Read [Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/) and the [nftables mode blog](https://kubernetes.io/blog/2025/02/28/nftables-kube-proxy/).
- Skim [KEP-5343](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/5343-nftables-to-default) and [KEP-5495](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/5495-deprecate-ipvs-mode-in-kube-proxy/README.md). These are short, readable, and contain dates that affect your cells.
- Then do one concrete piece of work: **audit every cell's kube-proxy config for an explicit `mode`**, and audit whether NetworkPolicy is actually enforced on each. Both are gotchas 1 and 3, both are quick, and both are real findings.

**Month 1 — go deep where your cells actually live.**

- Read your primary cloud's CNI documentation properly, then the other two. For AWS, the [plugin README](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/README.md) and [prefix delegation doc](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/docs/prefix-and-ip-target.md); for GKE, [Dataplane V2](https://cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2) and the alias IP sizing rules; for Azure, [CNI Overlay](https://learn.microsoft.com/en-us/azure/aks/concepts-network-azure-cni-overlay) and the [kubenet retirement notice](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni).
- Compute, for each of your real cell shapes on each cloud, the max-pods number and the IP consumption at full scale. Write it down. This exercise finds latent exhaustion problems reliably.
- Work through the [Cilium eBPF datapath reference](https://docs.cilium.io/en/stable/reference-guides/bpf/) and get comfortable with `bpftool` and `cilium-dbg monitor`.
- Run the **debugging playbook** against a deliberately broken cluster: delete a NetworkPolicy's DNS rule, set a wrong MTU, drop `nf_conntrack_max` to something tiny. Practising the runbook when it is not 3am is the whole point of having one.
- Finally, form and write down an opinion on the Cilium-everywhere question, with the tradeoffs from the cell lifecycle section. That is the senior-level deliverable here — not knowing the commands, but being the person who can argue the fleet-wide dataplane decision from first principles and cite the constraints.

---

## References

### CNI and the container runtime

1. [Container Network Interface Specification — cni.dev](https://www.cni.dev/docs/spec/) — the authoritative plugin contract; short enough to read in one sitting.
2. [CNI SPEC.md — containernetworking/cni on GitHub](https://github.com/containernetworking/cni/blob/main/SPEC.md) — the source of truth, including the v1.1.0 GC and STATUS verbs.
3. [containerd CRI configuration — GitHub](https://github.com/containerd/containerd/blob/main/docs/cri/config.md) — `bin_dir`, `conf_dir`, and `max_conf_num`, i.e. why a stale conflist wins.

### The Kubernetes network model, Services, and kube-proxy

4. [Cluster Networking — kubernetes.io](https://kubernetes.io/docs/concepts/cluster-administration/networking/) — the four-point contract every CNI must satisfy.
5. [Virtual IPs and Service Proxies — kubernetes.io](https://kubernetes.io/docs/reference/networking/virtual-ips/) — how each kube-proxy mode implements Services, plus the iptables-to-nftables migration notes.
6. [kube-proxy command-line reference — kubernetes.io](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-proxy/) — the conntrack flags and their defaults.
7. [NFTables mode for kube-proxy — kubernetes.io blog](https://kubernetes.io/blog/2025/02/28/nftables-kube-proxy/) — why nftables replaces iptables and what the verdict maps look like.
8. [KEP-3866: nftables kube-proxy backend — GitHub](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/3866-nftables-proxy/README.md) — the best primary explanation of why iptables mode does not scale.
9. [KEP-5343: make nftables the default kube-proxy backend — GitHub](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/5343-nftables-to-default) — the v1.37 warning, v1.39 beta, v1.40 default-flip timeline and the kernel 5.13 requirement.
10. [KEP-5495: deprecate IPVS mode in kube-proxy — GitHub](https://github.com/kubernetes/enhancements/blob/master/keps/sig-network/5495-deprecate-ipvs-mode-in-kube-proxy/README.md) — the exact removal schedule: warn 1.35, gate 1.37, disable 1.40, remove 1.43.
11. [Kubernetes v1.37: Garhwal — kubernetes.io blog](https://kubernetes.io/blog/2026/08/26/kubernetes-v1-37-release/) — current release; nftables netlink listing, comment truncation, localhost NodePort work.
12. [EndpointSlices — kubernetes.io](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/) — sharding, conditions, hints, and why Endpoints was deprecated.
13. [KEP-3015: PreferSameZone and PreferSameNode traffic distribution — GitHub](https://github.com/kubernetes/enhancements/tree/master/keps/sig-network/3015-prefer-same-node) — GA in v1.35 and the rename from `PreferClose`.
14. [Service Internal Traffic Policy — kubernetes.io](https://kubernetes.io/docs/concepts/services-networking/service-traffic-policy/) — `internalTrafficPolicy: Local` and its drop-on-no-local-endpoint behaviour.
15. [Using Source IP — kubernetes.io](https://kubernetes.io/docs/tutorials/services/source-ip/) — the definitive walkthrough of `externalTrafficPolicy` and SNAT.

### DNS

16. [DNS for Services and Pods — kubernetes.io](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) — record formats, headless service semantics, `dnsConfig` and `ndots`.
17. [NodeLocal DNSCache — kubernetes.io](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/) — the architecture and both deployment shapes.
18. [kubernetes#56903: DNS intermittent delays of 5s — GitHub](https://github.com/kubernetes/kubernetes/issues/56903) — the canonical tracking issue for the conntrack race.
19. [Racy conntrack and DNS lookup timeouts — Weaveworks](https://www.weave.works/blog/racy-conntrack-and-dns-lookup-timeouts) — *secondary, vendor engineering blog*, but the definitive kernel-level analysis of the 5-second timeout.

### NetworkPolicy, ingress, and egress

20. [Network Policies — kubernetes.io](https://kubernetes.io/docs/concepts/services-networking/network-policies/) — the standard API, its semantics, and an explicit list of what it cannot express.
21. [network-policy-api (AdminNetworkPolicy) — kubernetes-sigs on GitHub](https://github.com/kubernetes-sigs/network-policy-api) — cluster-scoped, ordered policy with explicit deny; check current maturity here.
22. [Gateway API — sigs.k8s.io](https://gateway-api.sigs.k8s.io/) — the resource model, conformance, and the implementations list.
23. [Gateway API v1.5: Moving features to Stable — kubernetes.io blog](https://kubernetes.io/blog/2026/04/21/gateway-api-v1-5/) — February 2026 release; what graduated to Standard.
24. [Announcing ingress2gateway 1.0 — kubernetes.io blog](https://kubernetes.io/blog/2026/03/20/ingress2gateway-1-0-release) — automated Ingress-to-Gateway migration, 30+ annotations supported.
25. [The PROXY protocol specification — HAProxy](https://www.haproxy.org/download/2.8/doc/proxy-protocol.txt) — v1 and v2 wire formats; read before enabling it anywhere.

### Linux kernel primitives

26. [network_namespaces(7) — man7.org](https://man7.org/linux/man-pages/man7/network_namespaces.7.html) — exactly what a network namespace isolates.
27. [veth(4) — man7.org](https://man7.org/linux/man-pages/man4/veth.4.html) — the virtual Ethernet pair.
28. [ip-rule(8) — man7.org](https://man7.org/linux/man-pages/man8/ip-rule.8.html) — policy routing selectors and evaluation order.
29. [IP sysctl reference — kernel.org](https://docs.kernel.org/networking/ip-sysctl.html) — authoritative defaults and semantics for `net.ipv4.*`.
30. [nf_conntrack sysctl reference — kernel.org](https://docs.kernel.org/networking/nf_conntrack-sysctl.html) — table sizing and every timeout.
31. [RFC 1191: Path MTU Discovery — RFC Editor](https://www.rfc-editor.org/rfc/rfc1191) — the mechanism that a blocked-ICMP firewall silently disables.
32. [Sysctls for a Kubernetes cluster — kubernetes.io](https://kubernetes.io/docs/tasks/administer-cluster/sysctl-cluster/) — the safe/unsafe split and the current safe list.

### eBPF

33. [What is eBPF? — ebpf.io](https://ebpf.io/what-is-ebpf/) — the best conceptual introduction to programs, maps, hooks, and the verifier.
34. [Bounded loops in BPF — LWN](https://lwn.net/Articles/794934/) — how the verifier reasons about control flow and why the complexity limit exists.
35. [Cilium eBPF datapath reference guide — docs.cilium.io](https://docs.cilium.io/en/stable/reference-guides/bpf/) — the most concrete description of a production eBPF datapath in existence.

### AWS

36. [amazon-vpc-cni-k8s README — GitHub](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/README.md) — every environment variable, including the warm-pool targets.
37. [Prefix and IP target configuration — amazon-vpc-cni-k8s docs](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/docs/prefix-and-ip-target.md) — how `WARM_*` and `MINIMUM_IP_TARGET` interact and override each other.
38. [Amazon VPC CNI increases pods per node limits — AWS blog](https://aws.amazon.com/blogs/containers/amazon-vpc-cni-increases-pods-per-node-limits/) — prefix delegation, the /28 model, and the 110/250 caps.
39. [Security groups per pod — Amazon EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html) — trunk and branch ENIs, and the density cost.
40. [CNI custom networking — Amazon EKS](https://docs.aws.amazon.com/eks/latest/userguide/cni-custom-network.html) — `ENIConfig`, secondary CIDRs, and the max-pods recalculation trap.
41. [Amazon VPC CNI now supports Kubernetes Network Policies — AWS blog](https://aws.amazon.com/blogs/containers/amazon-vpc-cni-now-supports-kubernetes-network-policies/) — the eBPF policy agent and the fact that it is opt-in.
42. [Elastic network interfaces — Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html) — per-instance-type ENI and IP limits, the input to the max-pods formula.
43. [Network MTU for EC2 instances — AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/network_mtu.html) — where 9001 applies and where 1500 does, per path type.

### Google Cloud

44. [GKE Dataplane V2 — Google Cloud](https://cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2) — the eBPF/Cilium dataplane, its policy logging, and the create-time-only constraint.
45. [GKE network overview — Google Cloud](https://cloud.google.com/kubernetes-engine/docs/concepts/network-overview) — VPC-native clusters, alias IPs, and the per-node range sizing rule.
46. [Adding Pod IPv4 address ranges — Google Cloud](https://cloud.google.com/kubernetes-engine/docs/how-to/multi-pod-cidr) — what to do when a pod range runs out.

### Azure

47. [AKS legacy container networking interfaces — Microsoft Learn](https://learn.microsoft.com/en-us/azure/aks/concepts-network-legacy-cni) — kubenet and Node Subnet, and the 31 March 2028 kubenet retirement date.
48. [Azure CNI Overlay overview — Microsoft Learn](https://learn.microsoft.com/en-us/azure/aks/concepts-network-azure-cni-overlay) — the /24-per-node overlay model and why it is the migration target.
49. [Azure CNI Powered by Cilium — Microsoft Learn](https://learn.microsoft.com/en-us/azure/aks/azure-cni-powered-by-cilium) — the managed eBPF option and its documented limitations.
50. [nftables support for kube-proxy in AKS — AKS engineering blog](https://blog.aks.azure.com/2025/11/19/nftables-in-kube-proxy) — *vendor blog*; preview announcement, verify current GA status.

### Cilium and Calico

51. [Kubernetes without kube-proxy — docs.cilium.io](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/) — socket-level LB, Maglev, DSR, and XDP acceleration.
52. [Cluster Mesh — docs.cilium.io](https://docs.cilium.io/en/stable/network/clustermesh/clustermesh/) — requirements (unique cluster IDs, non-overlapping CIDRs) and Global Services.
53. [Hubble observability — docs.cilium.io](https://docs.cilium.io/en/stable/observability/hubble/) — flow-level visibility with drop reasons.
54. [Cilium releases — GitHub](https://github.com/cilium/cilium/releases) — check here for the current stable line rather than trusting any document.
55. [Cilium at Ten Years: 1.19 — InfoQ](https://www.infoq.com/news/2026/02/cilium-119/) — *secondary*; encryption strict modes and the local-cluster policy default change.
56. [Calico eBPF data plane — docs.tigera.io](https://docs.tigera.io/calico/latest/operations/ebpf/) — source IP preservation, DSR, and kube-proxy replacement.
57. [Calico nftables data plane — docs.tigera.io](https://docs.tigera.io/calico/latest/getting-started/kubernetes/nftables) — the GA nftables backend and when to choose it.
58. [What's new in Calico v3.31 — Tigera](https://www.tigera.io/blog/whats-new-in-calico-v3-31-ebpf-nftables-and-more/) — *vendor blog*; staged policies, Whisker UI, nftables GA.

### Multi-cluster and tooling

59. [Submariner — submariner.io](https://submariner.io/) — CNI-agnostic L3 multi-cluster, Lighthouse discovery, and Globalnet for overlapping CIDRs.
60. [kind — kind.sigs.k8s.io](https://kind.sigs.k8s.io/) — local multi-node clusters; `disableDefaultCNI` and `kubeProxyMode` are the flags used in the labs.
