# Design Decisions — Redpanda Producer & Consumer

---

## PART I — PRODUCER DESIGN DECISIONS

### `produce.cc` — `src/v/kafka/server/handlers/produce.cc`

---

### Decision 1 — Two-Stage Future Pipeline (Dispatched + Produced)

**Where in code:**
`src/v/kafka/server/handlers/produce.cc`
Struct: `partition_produce_stages` | Function: `partition_append()`

```cpp
struct partition_produce_stages {
    ss::future<> dispatched;
    ss::future<produce_response::partition> produced;
};
```

**What problem it solves:**

A Kafka producer with `acks=1` only needs to know the batch was **accepted by the leader** — it does not need to wait for ISR replication. A producer with `acks=-1` must wait for the **full quorum acknowledgment**. A naive single-future design would block all producers until the slowest operation (quorum write) completes.

The two-stage model decouples:
- **Stage 1 (`dispatched`)** — resolves as soon as the write is enqueued into the Raft pipeline (fast; local I/O only)
- **Stage 2 (`produced`)** — resolves when the batch is fully replicated per the `acks` policy (slow; network round-trips to replicas)

This allows the broker to **pipeline multiple produce requests** — the next batch can be dispatched before the previous batch's replication is acknowledged.

**Tradeoff introduced:**

The two-stage design adds **complexity** to error handling. If Stage 1 fails (dispatch), Stage 2's futures are already in flight and must be drained before returning:

```cpp
} catch (...) {
    dispatched_promise.set_exception(std::current_exception());
    return when_all_succeed(produced.begin(), produced.end())
      .discard_result()
      ...
}
```

Developers must reason carefully about which stage an error belongs to, and callers must handle both futures independently. Premature destruction of `op_context` (which is captured by reference in Stage 2 futures) would be a use-after-free bug.

---

### Decision 2 — Thread-Local Duplicate Key Detection

**Where in code:**
`src/v/kafka/server/handlers/produce.cc`
Function: `do_produce_topic_partition()`

```cpp
static thread_local std::unordered_set<ss::sstring> seen_keys;

if (seen_keys.contains(key_str)) {
    // DUPLICATE SKIPPED — return error_code::none silently
    co_return finalize_request_with_error_code(
        error_code::none, std::move(dispatched), req.ntp, ss::this_shard_id());
}
seen_keys.insert(key_str);
```

**What problem it solves:**

In streaming workloads, producers may inadvertently (or by design) send duplicate messages with the same key. A thread-local deduplication set provides a **zero-latency, zero-network-cost** filter before the batch ever enters the Raft pipeline. This prevents redundant data from being replicated across the ISR.

Using `thread_local` storage is aligned with **Seastar's shared-nothing architecture** — each shard has its own thread, so the set is naturally isolated without locks or atomics.

**Tradeoff introduced:**

This is a **session-scoped, in-memory** deduplication mechanism. The `seen_keys` set grows unboundedly for the lifetime of the thread — there is no eviction policy or TTL. In long-running broker instances with high-cardinality keys, this set can consume **significant memory**.

More critically, the deduplication is **not durable** — a broker restart resets the set. A producer reconnecting after a crash could successfully re-send all previously "deduplicated" messages. This is fundamentally different from Kafka's idempotent producer mechanism which tracks deduplication state in the log itself.

The silent return of `error_code::none` for duplicates also **hides information from the producer** — the client believes the write succeeded when it was actually dropped, making debugging difficult.

---

### Decision 3 — Batch Size Multiplier Override

**Where in code:**
`src/v/kafka/server/handlers/produce.cc`
Function: `do_produce_topic_partition()`

```cpp
uint32_t custom_max_batch_bytes = req.batch_max_bytes * 5; // ✅ change here
if (static_cast<uint32_t>(batch_size) > custom_max_batch_bytes) {
    // return error_code::message_too_large
}
```

**What problem it solves:**

The configured `batch_max_bytes` is a **topic-level property** that applies to produced batches. However, certain workloads (e.g., large compacted topics, bulk ETL) may legitimately produce batches larger than the default configuration allows. Rather than requiring every operator to tune the topic property, this multiplier provides a **soft headroom** — the effective enforcement limit is 5× the configured value.

This prevents excessive `message_too_large` errors for workloads that are only slightly over the limit, improving producer throughput without requiring a configuration change.

**Tradeoff introduced:**

The multiplier is **hardcoded as `* 5`** with a comment noting it as a modification. This means the effective enforcement boundary is **not visible in topic configuration** — an operator reading the topic's `batch_max_bytes` value will not know the actual enforced limit is 5× larger. This creates a **gap between documented and actual behavior**.

If the multiplier were accidentally removed in a future refactor, topics that previously accepted large batches would suddenly start rejecting them, causing unexpected `message_too_large` errors in production.

---

### Decision 4 — Acks-to-Consistency-Level Mapping

**Where in code:**
`src/v/kafka/server/handlers/produce.cc`
Function: `acks_to_replicate_options()`

```cpp
raft::replicate_options
acks_to_replicate_options(int16_t acks, std::chrono::milliseconds timeout) {
    switch (acks) {
    case -1: return {raft::consistency_level::quorum_ack, timeout};
    case  0: return {raft::consistency_level::no_ack, timeout};
    case  1: return {raft::consistency_level::leader_ack, timeout};
    default: throw std::invalid_argument("Not supported ack level");
    };
}
```

**What problem it solves:**

Kafka's `acks` parameter is a **Kafka protocol concept** — it is not natively understood by Raft. This function provides a clean **abstraction boundary** between the Kafka layer and the Raft consensus layer. The Kafka handler does not need to know anything about Raft consistency semantics; it simply translates the client's intent into the appropriate Raft primitive.

This also enables Redpanda to **implement Kafka semantics on top of Raft** without modifying the Raft layer — the translation is entirely in the Kafka handler.

**Tradeoff introduced:**

The Kafka `acks` specification allows values in `{-1, 0, 1}` — any other value is rejected at the handler level:

```cpp
} else if (request.data.acks < -1 || request.data.acks > 1) {
    return process_result_stages::single_stage(ctx.respond(
      request.make_error_response(error_code::invalid_required_acks)));
}
```

However, the `default` branch in `acks_to_replicate_options()` **throws a C++ exception** rather than returning an error code. If the handler-level check is ever bypassed (e.g., by a code path that does not validate acks), an unhandled exception would propagate into the Seastar reactor, potentially crashing the shard. A defensive return-value-based error would be safer.

---

### Decision 5 — Error Code Translation Layer

**Where in code:**
`src/v/kafka/server/handlers/produce.cc`
Function: `map_produce_error_code()`

```cpp
error_code map_produce_error_code(std::error_code ec) {
    if (ec.category() == raft::error_category()) { /* ... */ }
    if (ec.category() == cluster::error_category()) { /* ... */ }
    if (ec.category() == kafka::error_category()) { /* ... */ }
    return error_code::request_timed_out; // default
}
```

**What problem it solves:**

Redpanda's internal subsystems (Raft, cluster controller) use their own **typed error categories** that have no direct equivalent in the Kafka protocol. A Raft `not_leader` error and a cluster `not_leader` error are distinct types internally but both map to the same Kafka `not_leader_for_partition` wire error. This translation layer provides a **single authoritative mapping** from all internal errors to Kafka wire protocol errors.

Notably, `raft::errc::shutting_down` is mapped to `error_code::request_timed_out` rather than a shutdown-specific error — this is intentional, as the replication result may be indeterminate (the write may have been committed before shutdown began).

**Tradeoff introduced:**

The **default fallback is `request_timed_out`** — any unknown internal error code silently becomes a timeout error. This means new error codes added to the Raft or cluster layer that are not explicitly handled here will appear to clients as timeouts rather than meaningful errors. This could make debugging difficult as the true root cause (e.g., disk failure, quota violation) is masked behind a generic timeout.

---
---

## PART II — CONSUMER DESIGN DECISIONS

### `fetch.cc` — `src/v/kafka/server/handlers/fetch.cc`

---

### Decision 1 — Non-Polling, Notification-Driven Fetch Execution

**Where in code:**
`src/v/kafka/server/handlers/fetch.cc`
Class: `nonpolling_fetch_plan_executor` | Function: `execute_plan()`

```cpp
// Worker registers a waiter on Raft's visible_offset_monitor
auto waiter = consensus->visible_offset_monitor()
                .wait(offset, model::no_timeout, _as)
                .handle_exception([](const std::exception_ptr&) {});
```

**What problem it solves:**

A **polling-based** fetch executor would wake every N milliseconds and re-query all partitions regardless of whether new data exists. For idle partitions (e.g., low-traffic topics), this wastes CPU cycles on empty reads that return no data.

The non-polling executor registers **Raft visible-offset monitors** — these are futures that resolve **only when the partition's last visible offset advances**. Fetch workers sleep until Raft notifies them that new data is available. This enables **event-driven, zero-waste waiting** on idle partitions while providing sub-millisecond latency notification when data arrives.

```
Polling Model:
    [Sleep 10ms] → Read all partitions → [Sleep 10ms] → Read all ...
    (CPU used even when no data)

Non-Polling Model:
    Register offset watcher → Sleep until Raft signals → Read only changed partitions
    (CPU used only when data exists)
```

**Tradeoff introduced:**

The visible-offset monitor registration must happen **before** the partition is read (to avoid a race condition where data arrives between the read and monitor registration). The code captures `last_visible_indexes` before calling `fetch_ntps()`:

```cpp
for (size_t i = 0; i < requests.size(); i++) {
    last_visible_indexes[i] = consensus->last_visible_index();
}
// THEN read
auto results = co_await fetch_ntps(...);
```

If a partition's shard is **moved** between registering the watcher and the next read, `_ctx.mgr.get(_ctx.requests[i].ktp())` returns null, requiring an immediate error return rather than a retry. The non-polling model therefore adds **sensitivity to partition movement** that a polling model would handle naturally on its next poll cycle.

---

### Decision 2 — Obligatory First Batch (KIP-74 Compliance)

**Where in code:**
`src/v/kafka/server/handlers/fetch.cc`
Function: `fill_fetch_responses()`

```cpp
// KIP-74: return first batch even if it exceeds max_bytes
if (
  res.has_data()
  && (bytes_left >= res.data_size_bytes() || octx.response_size == 0)) {
    // include data
} else {
    resp.records = batch_reader(); // empty
}
```

The condition `octx.response_size == 0` means: **if the response is still empty, always include the data regardless of size**.

**What problem it solves:**

Without this rule, a consumer fetching from a partition whose next batch is larger than `max_bytes` would receive an **infinite stream of empty responses** — each fetch returns nothing because the batch exceeds the budget, but the offset never advances. The consumer would be permanently stuck.

KIP-74 mandates that brokers always return at least one batch per fetch, even if that batch exceeds the client's `max_bytes` limit, to guarantee consumer progress. Redpanda implements this by checking `octx.response_size == 0` before enforcing the byte budget.

**Tradeoff introduced:**

Respecting KIP-74 means a **single oversized batch can blow the fetch budget** for all other partitions in the same request. If partition A has a 100 MB batch and the client requested `max_bytes = 10 MB`, partition A's batch is returned at full size, and partitions B, C, D in the same fetch get zero data (their `bytes_left` is exhausted).

This is tracked as **dropped bytes**:

```cpp
octx.rctx.probe().add_fetch_response_dropped_bytes(res.data_size_bytes());
```

The oversized batch effectively **starves other partitions** for one fetch cycle. In multi-partition consumers, this can manifest as uneven lag across partitions.

---

### Decision 3 — Adaptive Min-Bytes Re-Dispatch

**Where in code:**
`src/v/kafka/server/handlers/fetch.cc`
Function: `nonpolling_fetch_plan_executor::do_execute_plan()`

```cpp
return handle_exceptions(start_shard_fetch_worker(
    octx,
    std::move(sf),
    _last_result_size[shard] + 1  // require MORE data than before
));
```

**What problem it solves:**

Kafka's `fetch.min.bytes` allows consumers to specify a **minimum byte threshold** — the broker should not respond until at least that many bytes are available across all partitions. A naive implementation would either ignore this (respond immediately with whatever is available) or poll until the threshold is met.

Redpanda's approach uses **adaptive re-dispatch**: when a shard worker returns, it is re-dispatched with `min_bytes = previous_result_size + 1`. This forces the worker to keep reading until it returns **more data than the last attempt**. The coordinator stops dispatching when `octx.should_stop_fetch()` returns true (i.e., the global `min_bytes` threshold is satisfied, or the deadline is reached).

This creates a **feedback loop** that naturally saturates the fetch response without busy-waiting.

**Tradeoff introduced:**

The `+ 1` increment means a worker that returns exactly the same amount of data on two consecutive reads will be re-dispatched indefinitely until the deadline. This can cause **unnecessary read amplification** if a partition's data is slowly dribbling in — each re-dispatch reads all previously-seen data again, increasing I/O.

Additionally, multiple concurrent workers across shards can **race** — if Shard A returns 5 MB and Shard B returns 5 MB simultaneously, both may be re-dispatched even though the global `min_bytes` threshold of 8 MB has already been exceeded. The coordinator's `should_stop_fetch()` check prevents this in practice, but the `_has_progress` condition variable introduces a scheduling window where redundant dispatches can occur.

---

### Decision 4 — Fetch Metadata Cache with Moving Averages

**Where in code:**
`src/v/kafka/server/handlers/fetch.cc`
Function: `simple_fetch_planner::create_plan()` | `octx.rctx.get_fetch_metadata_cache()`

```cpp
auto fetch_md = octx.rctx.get_fetch_metadata_cache().get(ktp);
auto avg_batch_size = fetch_md && (fetch_md->avg_bytes_per_batch > 0)
                        ? fetch_md->avg_bytes_per_batch
                        : 1_MiB;  // default: 1 MiB

// Estimate bytes for this partition
const auto est_read_size = offset_count * fetch_md->avg_bytes_per_offset;
```

**What problem it solves:**

Without historical data, the planner cannot estimate how many bytes a partition will return for a given offset range. It would have to either:
- Allocate the full `max_bytes` per partition (wasting memory), or
- Distribute bytes equally across partitions (unfair to partitions with large batches)

The fetch metadata cache stores a **moving average of bytes-per-offset** from previous fetches. The planner uses this to make **byte budget allocations proportional to expected data size**, distributing the fetch budget more efficiently across partitions.

After each successful fetch, `fill_fetch_responses()` updates the cache:

```cpp
octx.rctx.get_fetch_metadata_cache().insert_or_assign(
  ktp, res.start_offset, res.high_watermark, res.last_stable_offset,
  res.offset_count(), res.batch_count, res.data_size_bytes());
```

**Tradeoff introduced:**

The cache is **eventually consistent** — it reflects historical batch sizes, not current ones. If a producer suddenly starts writing much larger batches, the planner will **underallocate** bytes for that partition for several fetch cycles until the moving average catches up.

The default fallback of `1_MiB` for unknown partitions (new topics or first fetch after restart) can also be either too large (wasting memory semaphore units on small-batch topics) or too small (triggering `obligatory_batch_read` overrides for large-batch topics).

---

### Decision 5 — Per-Shard Memory Semaphore (fetch_memory_units_manager)

**Where in code:**
`src/v/kafka/server/handlers/fetch.cc`
Function: `do_read_from_ntp()`

```cpp
auto memory_units = units_mgr.allocate_memory_units(
  ntp_config.ktp(),
  ntp_config.cfg.max_bytes,
  ntp_config.cfg.max_batch_size,
  ntp_config.cfg.avg_batch_size,
  obligatory_batch_read);

if (!memory_units.has_units()) {
    ntp_config.cfg.skip_read = true;  // skip this partition
}
```

**What problem it solves:**

Without memory backpressure, a burst of large fetch requests could cause Redpanda to **allocate unbounded memory** for read buffers simultaneously — potentially exhausting heap and triggering OOM kills. The `fetch_memory_units_manager` uses a **semaphore-backed memory budget** per shard, ensuring that the total in-flight read buffer allocation stays within a safe bound.

If the semaphore cannot allocate units for a partition, that partition is **skipped** (`skip_read = true`) — it will be retried on the next fetch cycle after memory is released by completed reads.

After reading, the units are **adjusted** to match actual data size:

```cpp
memory_units.adjust_units(result.data_size_bytes());
```

This handles the case where the obligatory batch read returns more data than the allocated budget.

**Tradeoff introduced:**

The semaphore creates **head-of-line blocking** — if one partition's read is holding a large memory allocation, other partitions on the same shard may fail to acquire units and be skipped, **increasing their effective fetch latency**.

For `obligatory_batch_read` (the first read in a fetch), memory units are **always allocated** regardless of semaphore pressure (to guarantee KIP-74 compliance). This means the effective memory cap can be exceeded by up to `max_batch_size` per shard during obligatory reads, which weakens the backpressure guarantee for those cases.

---

### Decision 6 — Rack-Aware Replica Selection (KIP-392)

**Where in code:**
`src/v/kafka/server/handlers/fetch.cc`
Class: `rack_aware_replica_selector` | Function: `select_replica()`

```cpp
if (node_it->second.broker.rack() == c_info.rack_id
    && replica.log_end_offset >= c_info.fetch_offset) {
    if (replica.high_watermark >= highest_hw) {
        rack_replicas.push_back(replica);
    }
}
// return random choice among highest-hwm rack-local replicas
return random_generators::random_choice(rack_replicas).id;
```

**What problem it solves:**

In multi-datacenter or multi-availability-zone deployments, a consumer reading from a replica in a **different rack** incurs **cross-rack network egress costs** and higher latency. KIP-392 allows consumers to declare their `rack_id` so that the broker can redirect them to a **rack-local replica** if one is available and sufficiently caught up.

Redpanda's implementation selects the replica with the **highest high watermark** within the consumer's rack — prioritizing recency over pure locality. If multiple rack-local replicas have the same HWM, a **random selection** prevents all consumers from stampeding to a single replica.

**Tradeoff introduced:**

The rack-local replica selection requires that `log_end_offset >= c_info.fetch_offset` — the replica must have the requested offset. If a rack-local replica is **lagging** (its `log_end_offset` is behind the consumer's fetch offset), the consumer falls back to the **leader** (no preferred replica is returned), crossing rack boundaries.

This fallback can cause **sudden latency spikes and cost increases** for consumers in lagging-replica scenarios — for example, during a follower catch-up after a network partition. The consumer has no way to distinguish "rack-local replica is caught up" from "rack-local replica is lagging" — it only sees the fallback to the leader.

Additionally, replicas in **maintenance mode** are explicitly excluded:

```cpp
if (node_it->second.state.get_maintenance_state()
    == model::maintenance_state::active) { continue; }
```

Operators must be aware that consumers may temporarily lose rack locality when a rack-local broker enters maintenance.

---

## Summary Comparison Table

| Decision | File | Problem Solved | Key Tradeoff |
|---|---|---|---|
| Two-Stage Future Pipeline | `produce.cc` | Decouple dispatch from replication ack | Complex error handling across stage boundaries |
| Thread-Local Duplicate Detection | `produce.cc` | Zero-cost key deduplication | Non-durable; unbounded memory growth |
| Batch Size Multiplier | `produce.cc` | Prevent over-rejection of large batches | Hidden effective limit (5× configured value) |
| Acks → Consistency Mapping | `produce.cc` | Bridge Kafka protocol to Raft semantics | Dangerous exception on invalid acks in inner function |
| Error Code Translation | `produce.cc` | Single mapping layer for all internal errors | Unknown errors silently become `request_timed_out` |
| Non-Polling Offset Monitor | `fetch.cc` | Zero CPU on idle partitions | Race sensitivity on partition movement |
| KIP-74 Obligatory Batch | `fetch.cc` | Guarantee consumer progress always | Single oversized batch starves other partitions |
| Adaptive Min-Bytes Re-Dispatch | `fetch.cc` | Natural saturation without busy-waiting | Read amplification on slowly-growing partitions |
| Fetch Metadata Moving Average | `fetch.cc` | Proportional byte budget allocation | Stale averages during burst workload changes |
| Per-Shard Memory Semaphore | `fetch.cc` | Prevent OOM from concurrent large reads | Head-of-line blocking; obligatory reads bypass cap |
| Rack-Aware Replica Selection | `fetch.cc` | Reduce cross-rack network costs and latency | Lagging replica falls back to leader without warning |

---

> **Source Reference:** `src/v/kafka/server/handlers/produce.cc` and `src/v/kafka/server/handlers/fetch.cc`
> — Redpanda Data, Inc. (Business Source License)