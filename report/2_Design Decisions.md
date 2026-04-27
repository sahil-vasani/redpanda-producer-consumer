# Design Decisions — Redpanda Producer-Consumer

---

## Design Decision 1: ACK Level → Raft Consistency Mapping

### Where is it implemented?

```cpp
// kafka/server/handlers/produce.cc
// Function: acks_to_replicate_options()

switch (acks) {
    case -1: return { raft::consistency_level::quorum_ack, timeout };
    case  0: return { raft::consistency_level::no_ack,     timeout };
    case  1: return { raft::consistency_level::leader_ack, timeout };
}
```

Called inside `do_produce_topic_partition()` → passed into `partition_append()` → passed into `raft::replicate()`

### What problem does it solve?

Different applications need different guarantees. A logging system does not care if one message is lost. A banking system cannot afford to lose any message. Redpanda solves this by letting the producer choose how deeply the system must confirm a write before responding.

- `acks=0` → Redpanda does not wait at all. Fire and forget.
- `acks=1` → Redpanda waits only until the **leader partition** writes it.
- `acks=-1` → Redpanda waits until a **quorum of replicas** confirm via Raft. Only then responds.

This single config controls the **entire reliability guarantee** of the system.

### What tradeoff does it introduce?

| ACK Level | Reliability | Latency | Risk |
|-----------|-------------|---------|------|
| `acks=0`  | Lowest      | Lowest  | Message can be lost silently |
| `acks=1`  | Medium      | Medium  | Lost if leader crashes before replication |
| `acks=-1` | Highest     | Highest | Slowest — must wait for Raft quorum |

> More reliability = more waiting. You cannot have both maximum speed and maximum safety at the same time.

---

## Design Decision 2: Shard-Based Partition Routing (CPU Affinity)

### Where is it implemented?

**Producer side** — inside `do_produce_topic_partition()`:

```cpp
// Step 4: Find shard
shard_for(ntp)
// Which CPU core handles this partition
```

Then:

```cpp
partition_manager().invoke_on(shard, ...)
// Work is sent to that specific CPU core
```

**Consumer side** — inside `create_plan()`:

```cpp
// Step 3: Find shard
shard_for(ktp)
// Which CPU core handles partition

plan.fetches_per_shard[shard].push_back(...)
// Fetch requests grouped by shard
```

### What problem does it solve?

Redpanda runs on a **multi-core machine**. If every request competed for the same memory and locks across all cores, performance would collapse under high load.

Redpanda solves this by assigning each partition to **one specific CPU core (shard)**. All reads and writes for that partition always go to the same core. This means:

- No shared locks between cores
- No cache thrashing
- Each core works independently — no waiting on other cores

This is based on the **Seastar framework** (which Redpanda is built on) — a shared-nothing architecture where each core owns its data.

### What tradeoff does it introduce?

- If one partition is extremely hot (all traffic going to it), its assigned core becomes a bottleneck — other cores sit idle
- You cannot rebalance a partition to a different core without stopping and reassigning it
- Works best when traffic is spread evenly across partitions — which is not always guaranteed

> Shard-per-partition gives maximum speed in normal cases, but creates a **hot partition problem** when load is uneven.

---

## Design Decision 3: Two-Phase Async Future Tracking (Dispatched + Produced)

### Where is it implemented?

**Producer side** — inside `produce_topic()` and `handle()`:

```cpp
// Two separate future vectors
partitions_dispatched.push_back(...)  // Phase 1: request sent to Raft
partitions_produced.push_back(...)    // Phase 2: replication confirmed

// Collected separately
when_all_succeed(dispatched)  // Wait: all requests dispatched?
when_all_succeed(produced)    // Wait: all writes confirmed?
```

Also the promise created in `handle()`:

```cpp
ss::promise<> dispatched_promise;
// Tracks async completion separately from write confirmation
```

**Consumer side** — same pattern inside `fetch_worker::run()`:

```cpp
for (;;)
// Worker keeps looping — does not block
// Only re-fetches partitions that changed
// Uses last_visible_index to detect changes
```

### What problem does it solve?

Writing to multiple partitions takes different amounts of time. If Redpanda waited **synchronously** for each partition one by one, the system would be extremely slow.

Instead, Redpanda splits the process into **two non-blocking phases:**

- **Phase 1 (dispatched):** All partition write requests are fired off to their shards simultaneously. System does not wait — just confirms the request was sent.
- **Phase 2 (produced):** System then waits for all replication confirmations to come back together.

This means many partitions are being written **in parallel**, not one after another. The response is only sent to the producer after **both phases complete**.

### What tradeoff does it introduce?

- **More complex error handling** — an error in Phase 2 (replication) must be traced back to which partition failed, even though all were launched together
- **Special case for `acks=0`** — since no confirmation is needed, Phase 2 is skipped entirely. But if an error occurs, the connection is closed instead of sending an error response
- **Memory cost** — holding open futures for all partitions simultaneously consumes more memory than sequential processing

> Parallel async execution gives high throughput, but makes debugging and error recovery significantly harder.

---

## Summary Table

| # | Design Decision | Where in Code | Problem Solved | Key Tradeoff |
|---|----------------|---------------|----------------|--------------|
| 1 | ACK → Raft Consistency Mapping | `acks_to_replicate_options()` in `produce.cc` | Flexible reliability per use case | Speed vs Safety |
| 2 | Shard-Based Partition Routing | `shard_for(ntp)` in both producer + consumer | Eliminate cross-core locking | Hot partition bottleneck |
| 3 | Two-Phase Async Future Tracking | `dispatched` + `produced` futures in `handle()` | Parallel writes across partitions | Complex error handling |