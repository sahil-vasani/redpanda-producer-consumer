# Failure Analysis — Redpanda Producer & Consumer

---

## Overview

Redpanda's producer and consumer paths are implemented in:

- **Producer logic:** `src/v/kafka/server/handlers/produce.cc`
- **Consumer logic:** `src/v/kafka/server/handlers/fetch.cc`

Unlike SQLite's single-process B-tree, Redpanda is a **distributed streaming system** — failures can occur at the network layer, shard layer, Raft consensus layer, or storage layer. Each failure mode has distinct symptoms and mitigations.

---

## 1. What Happens When Data Size Increases Significantly?

---

### 1.1 Batch Size Enforcement Breakdown

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `do_produce_topic_partition()`

```cpp
uint32_t custom_max_batch_bytes = req.batch_max_bytes * 5;

if (static_cast<uint32_t>(batch_size) > custom_max_batch_bytes) {
    auto msg = ssx::sformat(
      "batch size {} exceeds max {}", batch_size, req.batch_max_bytes);
    co_return finalize_request_with_error_code(
      error_code::message_too_large, ...);
}
```

**Explanation**

The effective enforcement limit is `batch_max_bytes × 5` — a hardcoded multiplier applied to the topic's configured value. As data volume grows:

- Producers sending larger batches will begin hitting `error_code::message_too_large`
- The `5×` multiplier means the actual limit diverges silently from the operator-configured value
- Rate-limited warning logs are emitted via `vloglr` but the client receives a hard rejection

**Impact**

| Symptom | Effect |
|---|---|
| Producers sending large batches | `message_too_large` errors — writes blocked |
| Operator unaware of 5× multiplier | Configuration drift — actual limit != configured limit |
| High-volume topics | Increased rejection rate as average batch size grows |

**Risk Level:** 🔴 High — silent configuration mismatch can cause unexpected producer failures at scale

---

### 1.2 Memory Semaphore Exhaustion on Consumer Side

**Code Reference**
**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `do_read_from_ntp()`

```cpp
auto memory_units = units_mgr.allocate_memory_units(
  ntp_config.ktp(),
  ntp_config.cfg.max_bytes,
  ntp_config.cfg.max_batch_size,
  ntp_config.cfg.avg_batch_size,
  obligatory_batch_read);

if (!memory_units.has_units()) {
    ntp_config.cfg.skip_read = true;  // partition skipped this cycle
}
```

**Explanation**

The `fetch_memory_units_manager` uses a semaphore to cap total in-flight read buffer allocation per shard. As data size increases:

- Larger batches consume more semaphore units per partition
- Remaining partitions fail to acquire units → `skip_read = true`
- Skipped partitions are deferred to the next fetch cycle — introducing **consumer lag**

**Impact**

| Symptom | Effect |
|---|---|
| Large batches per partition | Semaphore depleted by fewer partitions |
| Many partitions per shard | Most partitions skipped each cycle |
| Consumer lag accumulates | Growing offset gap between producer and consumer |

**Risk Level:** 🟡 Medium — mitigated by KIP-74's obligatory-batch override, but lag accumulates under sustained load

---

### 1.3 Fetch Response Dropped Bytes (Over-Budget Reads)

**Code Reference**
**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `fill_fetch_responses()`

```cpp
if (res.has_data()) {
    // Data was read from storage but cannot fit in response budget
    // Pure read amplification: bytes read, materialized, and now dropped
    octx.rctx.probe().add_fetch_response_dropped_bytes(res.data_size_bytes());
}
resp.records = batch_reader(); // send empty response for this partition
```

**Explanation**

The fetch planner estimates byte budgets using **historical moving averages**. As data volume increases, actual batch sizes may significantly exceed historical averages. This causes:

- Storage layer reads full batches from disk into memory
- Batches exceed the response budget and are **discarded without being sent**
- The bytes were read (I/O cost paid), deserialized (CPU cost paid), but not used

**Impact**

| Symptom | Effect |
|---|---|
| Burst of large batches | Moving average lags → systematic over-reads |
| Increasing `fetch_response_dropped_bytes` metric | I/O and CPU amplification without consumer benefit |
| Higher disk throughput | Storage load increases without consumer throughput increase |

**Risk Level:** 🟡 Medium — self-correcting as moving average catches up, but creates temporary resource waste

---

### 1.4 Thread-Local Deduplication Set Growth

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `do_produce_topic_partition()`

```cpp
static thread_local std::unordered_set<ss::sstring> seen_keys;

seen_keys.insert(key_str);  // no eviction, no TTL, grows forever
```

**Explanation**

The per-shard duplicate detection set grows **monotonically** — there is no size cap, no LRU eviction, and no TTL. As data volume increases with high-cardinality keys:

- Every unique key adds an `ss::sstring` to the set
- Heap allocation grows indefinitely for the lifetime of the thread
- On high-throughput brokers processing millions of unique keys per day, this becomes a **memory leak**

**Impact**

| Symptom | Effect |
|---|---|
| High-cardinality key workloads | Unbounded heap growth per shard |
| Long-running broker processes | Accumulated memory over days/weeks |
| OOM kill | Broker crash → partition leader election triggered |

**Risk Level:** 🔴 High — no built-in mitigation; requires operator-level monitoring

---

## 2. What Happens If a Component Fails?

---

### 2.1 Raft Leader Failure During Produce

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `map_produce_error_code()` | `partition_append()` continuation

```cpp
case raft::errc::not_leader:
case raft::errc::replicated_entry_truncated:
    return error_code::not_leader_for_partition;

case raft::errc::shutting_down:
    return error_code::request_timed_out;
```

**Explanation**

When the Raft leader for a partition fails mid-operation:

- In-flight `replicate()` futures resolve with `raft::errc::not_leader` or `raft::errc::replicated_entry_truncated`
- The `replicate_finished` continuation catches these and translates to Kafka error codes
- `not_leader_for_partition` signals the producer to **refresh metadata and retry on the new leader**
- `shutting_down` is mapped to `request_timed_out` — result is **indeterminate** (write may have committed)

**Risk Window**

```
Timeline:
    T0: produce_handler dispatches to shard
    T1: partition.replicate() enters Raft pipeline
    T2: LEADER CRASHES ← failure window
    T3: Raft elects new leader
    T4: map_produce_error_code() → not_leader_for_partition
    T5: Client refreshes metadata, retries on new leader
```

**Mitigation**

- `acks=-1` (quorum_ack) ensures the batch was committed to a **majority of replicas** before ack — the batch survives leader failure
- `acks=1` — **data loss possible** if leader crashes after acking but before replication

**Risk Level:** 🟡 Medium for `acks=-1` (safe), 🔴 High for `acks=1` (potential data loss)

---

### 2.2 Shard Not Found — Missing Partition

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `do_produce_topic_partition()`

```cpp
auto shard = octx.rctx.shards().shard_for(req.ntp);
if (!shard) {
    vlog(klog.warn, "FAKE SUCCESS: shard not found");

    co_return finalize_request_with_error_code(
        error_code::none,      // ← returns success to producer
        std::move(dispatched),
        req.ntp,
        ss::this_shard_id());
}
```

**Explanation**

When a partition is moved to a different broker (reassignment) and the `shard_table` has not yet been updated, `shard_for()` returns null. Instead of returning `not_leader_for_partition` to trigger metadata refresh, the code returns **`error_code::none` with a warning log** — the producer believes the write succeeded, but the data was **silently dropped**.

**Impact**

| Symptom | Effect |
|---|---|
| Partition reassignment in progress | Writes silently discarded |
| Producer receives `error_code::none` | No retry triggered — data loss undetected |
| Only observable via `klog.warn` | Requires log monitoring to detect |

**Mitigation**

- None at the code level — this is a known anomaly in the source (marked with `✅` comment)
- Operators must monitor for `"FAKE SUCCESS"` log messages
- Idempotent producers (`has_idempotent=true`) may detect gaps via sequence number tracking

**Risk Level:** 🔴 Critical — silent data loss with no client notification

---

### 2.3 Consumer Partition Movement Failure

**Code Reference**
**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `fetch_worker::register_waiters()`

```cpp
auto part = _ctx.mgr.get(_ctx.requests[i].ktp());
// If the partition can't be found it's since been moved
if (!part) {
    return {i};  // immediate error return, no retry registered
}
```

**Explanation**

When a partition is moved while the consumer's fetch worker has registered an offset monitor, the next attempt to register a new watcher finds the partition absent from the local partition manager. The worker returns an error result for that partition:

```cpp
results[r_i] = read_result(error_code::not_leader_for_partition);
```

The consumer receives `not_leader_for_partition` for that partition, triggering a metadata refresh and redirect to the new leader.

**Risk Window**

```
Timeline:
    T0: fetch_worker registers visible_offset_monitor for Partition 3
    T1: PARTITION 3 MOVED to another broker
    T2: register_waiters() finds mgr.get() returns null
    T3: Worker returns not_leader_for_partition
    T4: Consumer refreshes metadata → connects to new leader
```

**Risk Level:** 🟢 Low — recoverable; consumer refreshes metadata and retries automatically

---

### 2.4 Schema Registry Validation Failure

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `do_produce_topic_partition()`

```cpp
if (auto& validator = req.schema_id_validator) {
    auto ec = co_await (*validator)(*req.batch);
    if (ec != error_code::none) {
        // Update probe metric on target shard
        co_await octx.rctx.partition_manager().invoke_on(
          *shard, [](cluster::partition_manager& pm, const model::ntp& ntp) {
              if (auto p = pm.get(ntp)) {
                  p->probe().add_schema_id_validation_failed();
              }
          }, req.ntp);
        co_return finalize_request_with_error_code(ec, ...);
    }
}
```

**Explanation**

If the schema registry is **unavailable** or the batch uses an **unregistered schema**, validation fails. This introduces a **synchronous dependency** on the schema registry in the hot produce path:

- Schema registry timeout → produce latency spike
- Schema registry crash → all schema-validated topics become **unwritable**
- Metric `add_schema_id_validation_failed` is updated via a **cross-shard invoke** — adds latency even during error handling

**Impact**

| Scenario | Effect |
|---|---|
| Schema registry down | All schema-validated produces fail |
| Schema not registered | Batch rejected — producer must register schema first |
| Schema registry slow | Produce latency increases by registry RTT |

**Risk Level:** 🟠 Medium-High — single point of failure for schema-validated topics

---

### 2.5 Abort Source Propagation Failure

**Code Reference**
**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `nonpolling_fetch_plan_executor::initialize_progress_conditions()`

```cpp
// Connection can close and stop the sharded abort source before we subscribe
if (!octx.rctx.abort_source().local_is_initialized()) {
    return false;  // entire fetch plan is abandoned
}

_fetch_abort_sub = octx.rctx.abort_source().subscribe(
  [this]() noexcept { _has_progress.signal(); });

if (!_fetch_abort_sub) {
    return false;  // subscription failed — abort source already fired
}
```

**Explanation**

If a consumer **disconnects mid-fetch**, the connection's abort source fires. The `nonpolling_fetch_plan_executor` must subscribe to this abort source to stop all shard workers cleanly. If the abort source fires **before** the subscription is established (race condition), `initialize_progress_conditions()` returns `false` and the entire fetch is abandoned without executing.

**Risk Window**

```
Timeline:
    T0: fetch_handler::handle() called
    T1: CLIENT DISCONNECTS ← abort source fires
    T2: initialize_progress_conditions() checks local_is_initialized()
    T3: Returns false → do_fetch() abandoned → no resources leaked
```

**Mitigation**

- The code explicitly handles both cases (`!local_is_initialized()` and `!_fetch_abort_sub`)
- All spawned shard workers are stopped via `abort_workers()` before the executor returns
- The `_workers_gate` ensures no coroutine outlives the executor

**Risk Level:** 🟢 Low — explicitly handled; clean teardown path exists

---

### 2.6 Fetch Timeout and Deadline Expiry

**Code Reference**
**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `nonpolling_fetch_plan_executor`

```cpp
// Deadline timer fires and signals the condition variable
_fetch_timeout{[this] { _has_progress.signal(); }}

// Armed with the fetch deadline
if (octx.deadline) {
    _fetch_timeout.arm(octx.deadline.value());
}
```

**Exception handling in workers:**

```cpp
} catch (const seastar::timed_out_error& e) {
    vlog(klog.info, "timed out error: {}", e);
    // exception consumed — partial results returned
}
```

**Explanation**

When `fetch.max.wait.ms` expires before `min.bytes` is satisfied:

- The deadline timer fires `_has_progress.signal()`
- The coordinator loop checks `octx.should_stop_fetch()` → returns true
- All workers are aborted via `abort_workers()`
- **Partial results** (whatever was read so far) are returned to the consumer

This is correct Kafka behavior — the broker must respond by the deadline even if `min.bytes` is not met. However, if the abort takes longer than expected, the consumer may observe **latency above `fetch.max.wait.ms`**.

**Risk Level:** 🟢 Low — by-design behavior; partial results are valid

---

## 3. What Assumptions Does This System Rely On?

---

### 3.1 Shard Table Is Always Consistent

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc` and `fetch.cc`

```cpp
auto shard = octx.rctx.shards().shard_for(req.ntp);
if (!shard) {
    // Assumption violated — returns fake success (producer) or error (consumer)
}
```

**Assumption**

Both producer and consumer assume that `shard_for()` returns the correct shard for every active partition. If partition reassignment leaves the shard table **transiently inconsistent**, the producer silently drops data and the consumer gets a retriable error.

**Risk:** Shard table staleness during rolling restarts or partition reassignment

---

### 3.2 Raft Leader Is Stable During Write

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`

```cpp
if (!partition || !partition->is_leader()) {
    return finalize_request_with_error_code(
      error_code::not_leader_for_partition, ...);
}
// Proceeds with replicate() — assumes leadership holds until completion
auto stages = partition_append(...);
```

**Assumption**

Leadership is checked once before `partition_append()`. If the leader **steps down between the check and the replicate call**, `replicate()` will fail inside the Raft layer. This is handled by error translation, but the assumption of stable leadership for the duration of a write is implicit.

**Risk:** Leader election during high produce throughput causes temporary write failures

---

### 3.3 Thread-Local State Is Safe Per Shard

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`

```cpp
static thread_local std::unordered_set<ss::sstring> seen_keys;
static thread_local empty_fetch_tracker g_empty_fetch_tracker;  // fetch.cc
```

**Assumption**

Seastar's model guarantees **one coroutine runs at a time per CPU shard** — there are no data races on `thread_local` variables. This assumption is valid within Seastar's reactor model but would break if any code path introduced multi-threading on a single shard (e.g., blocking I/O that spawns OS threads).

**Risk:** Low in normal operation; violated if any blocking syscall is introduced without proper Seastar wrapping

---

### 3.4 Consumer Offset Monitors Fire Reliably

**Code Reference**
**File:** `src/v/kafka/server/handlers/fetch.cc`

```cpp
auto waiter = consensus->visible_offset_monitor()
                .wait(offset, model::no_timeout, _as)
                .handle_exception([](const std::exception_ptr&) {});
```

**Assumption**

The non-polling fetch model assumes that **Raft's visible-offset monitor will always fire** when new data commits. The `.handle_exception` silently swallows all exceptions from the monitor — if the monitor fails without signaling, the fetch worker waits until the **deadline timeout** fires instead.

**Risk:** Silent monitor failure causes consumer latency equal to `fetch.max.wait.ms` per occurrence

---

### 3.5 Schema Registry Is Available for Validated Topics

**Code Reference**
**File:** `src/v/kafka/server/handlers/produce.cc`

```cpp
auto validator = pandaproxy::schema_registry::maybe_make_schema_id_validator(
    octx.rctx.schema_registry(), topic.name, *cfg_ctx.properties);
// validator is called synchronously in the hot produce path
auto ec = co_await (*validator)(*req.batch);
```

**Assumption**

Topics with schema validation enabled assume the schema registry is **always reachable within produce latency**. There is no circuit breaker — a slow or unavailable schema registry blocks the entire produce path for validated topics.

**Risk:** Schema registry unavailability makes schema-validated topics unwritable

---

## Failure Summary Table

| Failure Scenario | Code Location | Severity | Mitigation |
|---|---|---|---|
| Data size → batch size limit | `do_produce_topic_partition()` | 🔴 High | Operator must align `batch_max_bytes` config |
| Memory semaphore exhaustion | `do_read_from_ntp()` | 🟡 Medium | Semaphore auto-releases; lag accumulates temporarily |
| Fetch response over-budget | `fill_fetch_responses()` | 🟡 Medium | Moving average self-corrects; monitor `dropped_bytes` |
| Deduplication set unbounded | `do_produce_topic_partition()` | 🔴 High | No mitigation — monitor heap usage |
| Raft leader failure (acks=-1) | `map_produce_error_code()` | 🟡 Medium | Quorum ensures durability; client retries |
| Raft leader failure (acks=1) | `partition_append()` | 🔴 High | Potential data loss — use `acks=-1` for durability |
| Shard not found (fake success) | `do_produce_topic_partition()` | 🔴 Critical | Monitor `"FAKE SUCCESS"` log warnings |
| Partition moved (consumer) | `register_waiters()` | 🟢 Low | `not_leader_for_partition` → metadata refresh |
| Schema registry down | `schema_id_validator` | 🟠 Med-High | All schema-validated topics unwritable |
| Abort source race | `initialize_progress_conditions()` | 🟢 Low | Explicitly handled — clean abandon path |
| Fetch deadline expiry | `_fetch_timeout` | 🟢 Low | By-design — partial results returned |

---

> **Source Reference:** `src/v/kafka/server/handlers/produce.cc` and `src/v/kafka/server/handlers/fetch.cc`
> — Redpanda Data, Inc. (Business Source License)