# 📊 Big Data & Storage System Concepts Mapped to Redpanda Producer & Consumer

---

## 1. Partitioning & Distributed Storage

### Concept
Modern distributed systems partition data across nodes to achieve:
- **Horizontal scalability** — adding more brokers increases capacity
- **Parallel processing** — reads and writes happen simultaneously on different partitions
- **Fault isolation** — failures in one partition do not affect others

### Where It Is Used

**File:** `src/v/kafka/server/handlers/produce.cc`
**File:** `src/v/kafka/server/handlers/fetch.cc`

```cpp
// Producer: resolve which shard owns this NTP
auto shard = octx.rctx.shards().shard_for(req.ntp);

// Consumer: group partitions by shard in the fetch plan
plan.fetches_per_shard[*shard].push_back(std::move(ktp), fetch_config{...});
```

### How It Works in Redpanda

- Every topic is split into **N partitions**, each assigned to a **CPU shard** on a broker
- The `shard_table` maps each `model::ntp` (namespace/topic/partition) to its owning shard
- Both the producer handler and the fetch planner resolve the shard before dispatching
- Cross-shard work is dispatched via Seastar's `invoke_on()` to avoid data races

```
Topic: "orders"
    Partition 0  →  Shard 2  →  Broker Node A
    Partition 1  →  Shard 0  →  Broker Node A
    Partition 2  →  Shard 5  →  Broker Node B
```

### Why It Matters
- Enables **linear throughput scaling** with additional partitions
- Seastar's shared-nothing design means **no locks between shards** — zero contention overhead
- Fetch planner groups by shard to minimize cross-shard calls per fetch cycle

---

## 2. Replication & Fault Tolerance (Raft Consensus)

### Concept
Distributed storage systems replicate data to survive node failures:
- **Strong consistency** — all replicas agree before acknowledging
- **Leader-based replication** — one leader coordinates all writes
- **Quorum writes** — majority of replicas must confirm before ack

### Where It Is Used

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `acks_to_replicate_options()` → `partition.replicate()`

```cpp
raft::replicate_options
acks_to_replicate_options(int16_t acks, std::chrono::milliseconds timeout) {
    switch (acks) {
    case -1: return {raft::consistency_level::quorum_ack, timeout};
    case  0: return {raft::consistency_level::no_ack, timeout};
    case  1: return {raft::consistency_level::leader_ack, timeout};
    };
}
```

### How It Works in Redpanda

The producer's `acks` setting maps directly to Raft consistency levels:

| Kafka `acks` | Raft Level | Behavior |
|---|---|---|
| `-1` | `quorum_ack` | Majority of ISR must confirm — strongest durability |
| `1` | `leader_ack` | Leader persists locally — moderate durability |
| `0` | `no_ack` | Fire and forget — maximum throughput, no durability |

Replication is managed by the **Raft consensus layer** inside `partition.replicate()`. The fetch path enforces the **leader check** before serving reads:

```cpp
if (!partition || !partition->is_leader()) {
    return error_code::not_leader_for_partition;
}
```

### Why It Matters
- Provides **tunable durability** — operators choose the tradeoff between speed and safety
- Raft guarantees **no data loss** when `acks=-1`, even if the leader crashes after ack
- Fetch's leader check ensures consumers always read the most up-to-date committed data

---

## 3. Streaming & Ingestion (Event-Driven Pipeline)

### Concept
Streaming systems ingest continuous, unbounded data flows with:
- **Low-latency write paths** — data must be durably stored in milliseconds
- **Event-driven consumption** — consumers react to new data arrival rather than polling
- **Back-pressure mechanisms** — protect the broker from being overwhelmed

### Where It Is Used

**Producer — Two-Stage Ingestion Pipeline:**
**File:** `src/v/kafka/server/handlers/produce.cc`

```cpp
struct partition_produce_stages {
    ss::future<> dispatched;   // Stage 1: accepted into pipeline
    ss::future<produce_response::partition> produced;  // Stage 2: replicated
};
```

**Consumer — Non-Polling Offset Monitor:**
**File:** `src/v/kafka/server/handlers/fetch.cc`

```cpp
auto waiter = consensus->visible_offset_monitor()
                .wait(offset, model::no_timeout, _as)
                .handle_exception([](const std::exception_ptr&) {});
```

### How It Works in Redpanda

**Write (Ingestion) side:**
- Batches are validated, serialized, and injected into the Raft pipeline
- The two-stage future allows the broker to **pipeline multiple produce requests** — Stage 1 resolves on enqueue, Stage 2 resolves on replication completion

**Read (Streaming) side:**
- Consumers register **Raft visible-offset monitors** — these are futures that resolve *only when new data commits*
- No polling timer — the consumer worker sleeps until Raft wakes it
- Back-pressure is enforced via the **memory semaphore** in `fetch_memory_units_manager`

```
Producer Stream:
    Batch → validate → replicate → ack (sub-millisecond pipeline)

Consumer Stream:
    Register offset watcher → sleep → Raft signals → read new data
    (zero CPU waste on idle topics)
```

### Why It Matters
- Event-driven consumption matches the mental model of **Apache Kafka streams**
- Back-pressure via memory semaphore prevents OOM during ingest bursts
- Debounce delay (`fetch_reads_debounce_timeout`) batches small reads for efficiency

---

## 4. Reliability & Fault Tolerance (Error Code Translation)

### Concept
Reliable distributed systems must:
- **Detect failures** — distinguish transient errors from permanent ones
- **Translate errors** across abstraction boundaries cleanly
- **Guarantee progress** — consumers must never get permanently stuck

### Where It Is Used

**Error Translation — Producer:**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `map_produce_error_code()`

```cpp
error_code map_produce_error_code(std::error_code ec) {
    if (ec.category() == raft::error_category()) {
        switch (static_cast<raft::errc>(ec.value())) {
        case raft::errc::not_leader:          return error_code::not_leader_for_partition;
        case raft::errc::shutting_down:       return error_code::request_timed_out;
        case raft::errc::invalid_input_records: return error_code::invalid_record;
        default:                               return error_code::request_timed_out;
        }
    }
    // cluster, kafka error categories...
}
```

**KIP-74 Progress Guarantee — Consumer:**
**File:** `src/v/kafka/server/handlers/fetch.cc`

```cpp
// Always return first batch even if it exceeds max_bytes
if (res.has_data()
    && (bytes_left >= res.data_size_bytes() || octx.response_size == 0)) {
    resp.records = batch_reader(std::move(res).release_data());
}
```

### How It Works in Redpanda

**Producer reliability chain:**
1. Raft/cluster errors are caught in the `replicate_finished` continuation
2. `map_produce_error_code()` translates internal errors to Kafka wire codes
3. The client receives actionable errors (`not_leader_for_partition` → refresh metadata and retry)

**Consumer reliability guarantee (KIP-74):**
- If the response is still empty (`octx.response_size == 0`), the first batch is **always returned** regardless of byte limit
- This guarantees a consumer can **never get permanently stuck** behind an oversized batch

### Why It Matters
- Clean error translation enables **automatic client recovery** — clients know when to retry, redirect, or fail
- KIP-74 enforcement prevents the classic "stuck consumer" bug found in naive Kafka implementations
- `request_timed_out` as the default error code is a safe, retriable signal for clients

---

## 5. Execution Model — DAG-Based Async Execution (Seastar Futures)

### Concept
Modern data systems use **DAG-based execution** (Directed Acyclic Graph) to:
- Express complex multi-step workflows as composable stages
- Enable **non-blocking, cooperative execution** without threads per request
- Chain dependent operations without explicit callbacks (callback hell)

### Where It Is Used

**Producer DAG — `do_produce_topic_partition()`:**
**File:** `src/v/kafka/server/handlers/produce.cc`

```cpp
// Each co_await is a DAG edge — the next step only runs after the prior completes
co_await validate_batch(...)          // Node 1: validate
co_await schema_id_validator(...)     // Node 2: schema check
co_await partition_manager.invoke_on  // Node 3: cross-shard dispatch
    └── partition.replicate()         // Node 4: Raft replication
            └── replicate_finished    // Node 5: response assembly
```

**Consumer DAG — `fetch_worker::do_run()`:**
**File:** `src/v/kafka/server/handlers/fetch.cc`

```cpp
// Adaptive loop — each iteration is a DAG subgraph
co_await query_requests()             // Node 1: read partitions
    └── fetch_ntps()                  // Node 2: parallel shard reads
            └── do_read_from_ntp()   // Node 3: per-partition read
                    └── read_from_partition()  // Node 4: log reader
co_await _completed_waiter_count.wait() // Node 5: offset monitor sleep
```

### How It Works in Redpanda

Redpanda is built on **Seastar** — a C++ framework for non-blocking async execution. Every `co_await` represents an async boundary in the DAG. The Seastar reactor runs millions of lightweight coroutine tasks per second without OS thread context switches.

```
Producer DAG:
    [validate] → [schema] → [cross-shard] → [replicate] → [ack]
         ↑ Each arrow is a co_await — non-blocking, zero thread overhead

Consumer DAG:
    [plan] → [dispatch workers] → [read] → [fill responses] → [send]
                    ↑
              [offset watcher sleep] ← loops back when data arrives
```

### Why It Matters
- Seastar's reactor processes **one request per CPU core** with no locks — throughput scales linearly
- DAG structure makes the **execution order explicit** and auditable in code
- Cancellation propagates through the DAG via `ss::abort_source` — no orphaned futures

---

## 6. Storage Layout — Log-Structured Storage (LSM Inspiration)

### Concept
Log-structured storage systems (like **LSM trees** in Cassandra/RocksDB) optimize for:
- **Sequential append writes** — avoid random I/O
- **Immutable segments** — old data is not modified in place
- **Compaction** — merge segments periodically to reclaim space

### Where It Is Used

**Producer — Log Append:**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `partition_append()`

```cpp
// LogAppendTime: broker stamps the time at ingestion
auto log_append_time_ms = batch->header().attrs.timestamp_type()
    == model::timestamp_type::create_time
  ? model::timestamp::missing()
  : batch->header().max_timestamp;

auto stages = partition.replicate(bid, std::move(*batch), ...);
```

**Consumer — Sequential Read:**
**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `read_from_partition()`

```cpp
kafka::log_reader_config reader_config(
  model::offset_cast(config.start_offset),
  model::offset_cast(config.max_offset),
  0,
  config.max_bytes, ...);

auto rdr = co_await part.make_reader(reader_config);
auto result = co_await rdr.reader.consume(kafka_batch_serializer(), deadline);
```

### How It Works in Redpanda

Redpanda's storage layer is **log-structured** at the partition level:
- Each partition is an **append-only log** — producers always write to the tail
- The `base_offset` of each batch is computed as `last_offset - (num_records - 1)` — offsets are monotonically increasing
- Consumers read sequentially from `start_offset` to `max_offset` using a log segment reader
- `LogAppendTime` timestamps are assigned at **broker ingestion time** — consistent with the log ordering

```
Partition Log:
    [Batch 0: offsets 0-99] [Batch 1: offsets 100-199] [Batch 2: offsets 200-299] →
    ↑ Immutable                ↑ Immutable                ↑ Consumer reads here
    Producer always appends to tail →→→→→→→→→→→→→→→→→→→→→→→
```

### Why It Matters
- Sequential appends achieve **disk bandwidth saturation** — no random I/O seek overhead
- Immutable log segments enable **safe concurrent reads** without locks
- `last_stable_offset` enforcement (`KIP-447`) ensures consumers never read uncommitted transactional data

---

## 7. Data Skew Handling — Rack-Aware Replica Selection

### Concept
Data skew occurs when:
- Some partitions receive **disproportionately more traffic** than others
- Some replicas are **geographically closer** to consumers than others
- Without balancing, certain nodes become **hotspots** while others are underutilized

### Where It Is Used

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Class:** `rack_aware_replica_selector`

```cpp
if (node_it->second.broker.rack() == c_info.rack_id
    && replica.log_end_offset >= c_info.fetch_offset) {
    if (replica.high_watermark >= highest_hw) {
        rack_replicas.push_back(replica);
    }
}
// Break ties randomly to distribute load
return random_generators::random_choice(rack_replicas).id;
```

### How It Works in Redpanda

- Each broker is tagged with a **rack ID** (maps to availability zone or datacenter)
- When a consumer declares `rack_id`, the broker selects a **rack-local replica** with the highest high watermark
- Among equal-HWM replicas, **random selection** distributes load to prevent all consumers from stampeding one replica
- Replicas in **maintenance mode** are excluded from selection

```
Consumer in Rack "us-east-1a":
    Replica A (Rack us-east-1a, HWM=1000) ← selected
    Replica B (Rack us-east-1b, HWM=1000) ← skipped (wrong rack)
    Replica C (Rack us-east-1a, HWM= 950) ← skipped (lower HWM)
```

### Why It Matters
- Reduces **cross-rack egress cost** in cloud deployments — significant cost savings at scale
- Random tie-breaking prevents **read hotspots** on a single rack-local replica
- Aligns with **KIP-392** — the official Kafka specification for rack-aware consumers

---

## Summary Table

| # | Concept | Where in Code | Key Benefit |
|---|---|---|---|
| 1 | Partitioning & Distributed Storage | `shard_for()`, `fetches_per_shard[]` | Linear scalability, zero-contention shards |
| 2 | Replication & Fault Tolerance | `acks_to_replicate_options()`, Raft layer | Tunable durability, crash safety |
| 3 | Streaming & Ingestion | Two-stage futures, offset monitor | Low-latency writes, event-driven reads |
| 4 | Reliability & Fault Tolerance | `map_produce_error_code()`, KIP-74 | Client recovery, consumer progress guarantee |
| 5 | DAG-Based Async Execution | `co_await` chains, Seastar reactor | Non-blocking, CPU-efficient pipeline |
| 6 | Log-Structured Storage | `partition.replicate()`, `make_reader()` | Sequential I/O, immutable segments |
| 7 | Data Skew Handling | `rack_aware_replica_selector` | Cross-rack cost reduction, load balancing |

---

> **Source Reference:** `src/v/kafka/server/handlers/produce.cc` and `src/v/kafka/server/handlers/fetch.cc`
> — Redpanda Data, Inc. (Business Source License)