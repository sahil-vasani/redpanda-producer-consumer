# Execution Pipeline — Redpanda Producer & Consumer

---

## PART I — PRODUCER WRITE PATH

### `produce_handler::handle()` → `partition_append()` → Raft Replication

---

### Step 1 — Request Entry Point

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `produce_handler::handle(request_context ctx, ss::smp_service_group ssg)`

When a Kafka `PRODUCE` request arrives over the wire, the top-level handler is invoked. This function serves as the **gateway** for all producer traffic entering the Redpanda broker.

Key responsibilities at this stage:

- Decodes the raw `produce_request` from the wire using `request.decode(ctx.reader(), ctx.header().version)`
- Inspects the request for **transactional** and **idempotent** batch flags by scanning `hdr.attrs.is_transactional()` and `hdr.producer_id`
- Checks **recovery mode** — if the broker is recovering, it returns `error_code::policy_violation` immediately
- Checks **disk pressure** via `ctx.metadata_cache().should_reject_writes()` — if disk is full, returns `error_code::kafka_storage_error`
- Performs **ACL authorization** — unauthorized topics are partitioned out via `std::partition()` and filled with `error_code::topic_authorization_failed`
- Handles **linked/migrated topics** to prevent writes during migration windows

```
produce_handler::handle()
    ├── request.decode()
    ├── recovery_mode check → policy_violation
    ├── disk space check   → kafka_storage_error
    ├── ACL authorization  → topic_authorization_failed
    └── produce_topics()   → [continue pipeline]
```

---

### Step 2 — Topic and Partition Dispatch

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `produce_topics()` → `produce_topic()` → `produce_topic_partition()`

For each topic in the request, `produce_topics()` iterates and calls `produce_topic()`. Inside `produce_topic()`:

- Validates that the topic is **not a no-produce topic** (e.g., internal transform log topics)
- Fetches **topic metadata** from the metadata cache to obtain per-topic configuration
- Builds a `topic_configuration_context` containing:
  - `batch_max_bytes` — maximum allowed batch size
  - `timestamp_type` — whether to use `CreateTime` or `LogAppendTime`
  - `message_timestamp_before_max_ms` / `message_timestamp_after_max_ms` — timestamp validation windows

For each **partition** inside the topic, it calls `produce_topic_partition()` which constructs a `ntp_produce_request` (NTP = Namespace/Topic/Partition) and forwards it to `do_produce_topic_partition()`.

```
produce_topics()
    └── produce_topic()
            ├── topic metadata fetch
            ├── topic_configuration_context build
            └── produce_topic_partition() × N partitions
                    └── do_produce_topic_partition()
```

---

### Step 3 — Batch Validation

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `do_produce_topic_partition()`

Before any replication happens, the batch undergoes **multi-layer validation**:

- **Batch validation** via `validate_batch()` — checks CRC, magic version, timestamp bounds, and compression
- **Duplicate key detection** — a `thread_local std::unordered_set<ss::sstring> seen_keys` is maintained per shard. If a record key has been seen before within the same thread-local session, the request is **silently dropped** with `error_code::none`
- **Batch size enforcement** — computes `custom_max_batch_bytes = req.batch_max_bytes * 5` and rejects oversized batches with `error_code::message_too_large`
- **Schema ID validation** — if the topic has schema validation enabled, `pandaproxy::schema_registry::maybe_make_schema_id_validator` is invoked against the batch; failures return a schema-specific error code

```
do_produce_topic_partition()
    ├── validate_batch()           → timestamp, CRC, magic
    ├── duplicate key check        → thread_local seen_keys set
    ├── batch size check           → message_too_large
    └── schema_id_validator()      → schema registry validation
```

---

### Step 4 — Shard Routing and Cross-Shard Dispatch

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `do_produce_topic_partition()` → `octx.rctx.partition_manager().invoke_on()`

Redpanda's architecture assigns each partition to a specific **CPU shard** (via Seastar's shard model). The code resolves the responsible shard:

```cpp
auto shard = octx.rctx.shards().shard_for(req.ntp);
```

If no shard is found (partition was moved or doesn't exist), the code returns a **fake success** (`error_code::none`) with a log warning — this is a known anomaly in the source. Otherwise, it performs a **cross-shard invocation**:

```cpp
co_await octx.rctx.partition_manager().invoke_on(*shard, octx.ssg, [...])
```

This lambda runs on the **target shard**, where it:
- Calls `kafka::make_partition_proxy(ntp, mgr)` to get a local partition handle
- Verifies `partition->is_leader()` — returns `error_code::not_leader_for_partition` if not leader
- Calls `partition_append()` to begin the actual replication pipeline

---

### Step 5 — Raft Replication via `partition_append()`

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `partition_append()`

This function is the **replication entry point**. It:

- Determines the **log append timestamp** — if `LogAppendTime` is configured, uses the broker's local time; otherwise uses `model::timestamp::missing()`
- Converts the `acks` value to a Raft consistency level:
  - `acks = -1` → `raft::consistency_level::quorum_ack` (majority of ISR must confirm)
  - `acks = 0`  → `raft::consistency_level::no_ack` (fire and forget)
  - `acks = 1`  → `raft::consistency_level::leader_ack` (leader only)
- Calls `partition.replicate(bid, std::move(*batch), acks_to_replicate_options(...))` which enters the **Raft consensus layer**

The result is a two-stage future:
- **Stage 1 `dispatched`** — resolves when the write has been accepted into the Raft pipeline
- **Stage 2 `produced`** — resolves when the batch has been fully replicated per the `acks` policy

```
partition_append()
    ├── log_append_time_ms computation
    ├── acks_to_replicate_options()   → quorum_ack / no_ack / leader_ack
    └── partition.replicate()         → Raft consensus layer
            ├── Stage 1: request_enqueued (dispatched future)
            └── Stage 2: replicate_finished (produced future)
```

---

### Step 6 — Response Assembly and Metrics

**File:** `src/v/kafka/server/handlers/produce.cc`
**Function:** `partition_append()` → `.then_wrapped([...])` continuation

After `replicate_finished` resolves:

- On **success**: computes `base_offset = last_offset - (num_records - 1)` and returns `error_code::none`; probes are updated for records produced, bytes produced, and batches produced
- On **failure**: `map_produce_error_code()` translates Raft or cluster error codes to Kafka wire error codes
  - `raft::errc::not_leader` → `error_code::not_leader_for_partition`
  - `raft::errc::shutting_down` → `error_code::request_timed_out`
  - `cluster::errc::invalid_producer_epoch` → `error_code::invalid_producer_epoch`

Latency is recorded via `log_batch_stats()` which emits structured log lines with `ntp`, `bytes`, `records`, `latency_us`, and `ok` fields.

```
replicate_finished continuation
    ├── SUCCESS → base_offset, probe updates, log_batch_stats(ok=true)
    └── FAILURE → map_produce_error_code() → wire error code
```

---

### Producer Write Path — Full Sequence Diagram

```
CLIENT PRODUCE REQUEST
        │
        ▼
produce_handler::handle()
        │  ── decode, recovery check, disk check, ACL check
        ▼
produce_topics() → produce_topic() → produce_topic_partition()
        │  ── topic metadata, config context, per-partition dispatch
        ▼
do_produce_topic_partition()
        │  ── validate_batch(), duplicate key check, size check, schema check
        ▼
octx.rctx.partition_manager().invoke_on(*shard)
        │  ── cross-shard dispatch to partition's home core
        ▼
make_partition_proxy() + is_leader() check
        │
        ▼
partition_append()
        │  ── acks → consistency level, replicate()
        ▼
Raft Consensus Layer
        ├── Stage 1: request_enqueued  ──► dispatched future resolved
        └── Stage 2: replicate_finished ─► produced future resolved
                │
                ▼
        Response: base_offset / error_code
                │
                ▼
CLIENT PRODUCE RESPONSE
```

---
---

## PART II — CONSUMER FETCH PATH

### `fetch_handler::handle()` → `do_fetch()` → `read_from_partition()`

---

### Step 1 — Request Entry Point

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `fetch_handler::handle(request_context rctx, ss::smp_service_group ssg)`

When a Kafka `FETCH` request arrives, the top-level handler initializes the **operation context** (`op_context`):

- Decodes the `fetch_request` from the wire
- Initializes `bytes_left` as `min(fetch_max_bytes config, request.max_bytes)` — this is the global budget for the entire fetch response
- Resolves **topic names from topic IDs** (for Kafka API version ≥ 13) via `rctx.metadata_cache().get_name_by_id()`
- Establishes the **fetch session** via `rctx.fetch_sessions().maybe_get_session(request)` — supports sessionless, full-fetch, and incremental fetch session modes
- Creates **response placeholders** for all partitions, pre-populated with sentinel offsets (`-1`)
- Sets `deadline = now() + debounce_delay` and `fetch_deadline = now() + kafka_fetch_request_timeout_ms`

```
fetch_handler::handle()
    ├── op_context construction
    │       ├── decode request
    │       ├── bytes_left budget
    │       ├── topic ID → name resolution
    │       ├── session establishment
    │       └── response placeholder creation
    └── do_fetch(octx)
```

---

### Step 2 — Fetch Planning

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `simple_fetch_planner::create_plan()`

The planner **groups partitions by their home shard** to minimize cross-shard calls:

For each fetch partition (sessionless or session-based):
- Checks **authorization** — `acl_operation::read` required
- Checks **disabled partitions** via `metadata_cache.is_disabled()`
- Resolves the shard: `octx.rctx.shards().shard_for(ktp)`
- Estimates bytes needed using the **fetch metadata cache** — computes `est_read_size = offset_count * avg_bytes_per_offset` from historical averages
- Assigns each partition to `plan.fetches_per_shard[shard]` with a `fetch_config` containing start offset, max offset, byte budget, isolation level, rack awareness preferences, and abort source

```
simple_fetch_planner::create_plan()
    ├── for_each_fetch_partition()
    │       ├── ACL check
    │       ├── disabled partition check
    │       ├── shard resolution
    │       ├── byte budget estimation (avg_bytes_per_offset)
    │       └── assign to fetches_per_shard[shard]
    └── fetch_plan (grouped by shard)
```

---

### Step 3 — Non-Polling Plan Execution

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `nonpolling_fetch_plan_executor::execute_plan()`

Redpanda uses a **non-polling, notification-driven** fetch execution model. Instead of polling partitions on a timer, it registers **Raft visible-offset monitors** that wake the fetch worker only when new data arrives.

- Applies **debounce delay** if configured — `ss::sleep(fetch_reads_debounce_timeout)` before starting reads
- Applies **PID controller delay** if `non_polling_with_pid` strategy is active
- Spawns **one fetch worker per shard** via `start_shard_fetch_worker()` using `ssx::spawn_with_gate()`
- Arms a **deadline timer** — `_fetch_timeout.arm(octx.deadline.value())`
- Loops on `wait_for_progress()` — a condition variable that wakes when any worker completes or the timeout fires
- If `octx.should_stop_fetch()` is false and workers have returned, **re-dispatches** them with `min_fetch_bytes = last_result_size + 1` to force progress

```
nonpolling_fetch_plan_executor::execute_plan()
    ├── debounce sleep (optional)
    ├── PID delay (optional)
    ├── start_worker_aborts() — abort source per shard
    ├── spawn fetch worker per shard
    └── coordination loop
            ├── wait_for_progress()  ← condition variable
            ├── should_stop_fetch() check
            └── re-dispatch workers with higher min_bytes
```

---

### Step 4 — Shard Fetch Worker

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Class:** `fetch_worker`
**Function:** `fetch_worker::do_run()`

Each worker runs on its assigned shard and implements an **adaptive retry loop**:

**First run:** reads all assigned partitions via `query_requests()` → `fetch_ntps()`

**Subsequent runs:** only re-reads partitions that have **new data available** (signaled by Raft's `visible_offset_monitor`). The worker:
- Records `last_visible_indexes` **before** reading (to avoid a race condition where new data arrives between read and index capture)
- Calls `fetch_ntps()` with the filtered partition list
- Registers `visible_offset_monitor().wait()` waiters for partitions that returned no new data
- Returns when `total_size >= min_bytes`, an error occurs, or abort is requested

```
fetch_worker::do_run()
    ├── First run: read all partitions
    ├── register_waiters() for empty partitions
    │       └── visible_offset_monitor().wait() per partition
    ├── _completed_waiter_count.wait()  ← semaphore
    └── Subsequent runs: read only signaled partitions
```

---

### Step 5 — NTP-Level Read

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `do_read_from_ntp()` → `read_from_partition()`

For each partition in a shard's fetch batch:

- **Memory allocation** — `units_mgr.allocate_memory_units()` acquires a semaphore-backed memory budget; if insufficient, `skip_read = true`
- **Partition proxy** — `make_partition_proxy(ntp_config.ktp(), cluster_pm)` obtains a local handle
- **Leader check** — returns `not_leader_for_partition` if not leader (unless `read_from_follower` is enabled)
- **Leader epoch validation** (KIP-320) — ensures the client's cached epoch matches the current leader
- **Offset validation** — `kafka_partition->validate_fetch_offset()` checks the requested start offset against `start_offset` and `high_watermark`
- **LSO enforcement** (KIP-447) — for `read_committed` isolation, caps `max_offset = prev_offset(last_stable_offset)` to exclude uncommitted transactional data
- **Rack-aware replica selection** — if rack awareness is enabled and the consumer has a `rack_id`, `rack_aware_replica_selector::select_replica()` picks the highest-watermark replica in the same rack

```
do_read_from_ntp()
    ├── memory unit allocation
    ├── make_partition_proxy()
    ├── is_leader() / read_from_follower check
    ├── check_leader_epoch()      ← KIP-320
    ├── validate_fetch_offset()
    ├── last_stable_offset cap    ← KIP-447 (read_committed)
    ├── rack-aware replica select ← KIP-392
    └── read_from_partition()
```

---

### Step 6 — Log Read and Data Serialization

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `read_from_partition()`

This is the **innermost read function** that interacts directly with the storage layer:

- Fast-path return: if `high_watermark < config.start_offset` or `skip_read`, returns empty result immediately
- Builds a `log_reader_config` with the byte and offset bounds, abort source, and client address
- Calls `part.make_reader(reader_config)` to obtain a log segment reader
- Consumes the reader via `rdr.reader.consume(kafka_batch_serializer(), deadline)` — serializes batches directly into Kafka wire format
- Captures `delta_from_tip_ms` — the age of the oldest batch returned (used for lag monitoring)
- For `read_committed` isolation with transactional batches, fetches **aborted transaction ranges** via `part.aborted_transactions()` and performs a **truncation safety check**

```
read_from_partition()
    ├── fast-path: hw < start_offset → empty result
    ├── make_reader(log_reader_config)
    ├── consume(kafka_batch_serializer, deadline)
    │       └── serializes record batches → iobuf
    ├── delta_from_tip_ms computation
    ├── aborted_transactions() [read_committed only]
    └── read_result{data, offsets, hw, lso, batch_count}
```

---

### Step 7 — Response Assembly

**File:** `src/v/kafka/server/handlers/fetch.cc`
**Function:** `fill_fetch_responses()`

After all shard workers complete, results are assembled into the Kafka fetch response:

- Iterates `results[]` and `responses[]` in parallel (with a soft-assert on count mismatch)
- Updates the **fetch metadata cache** with latest `start_offset`, `high_watermark`, `lso`, `avg_bytes_per_offset`, and `batch_count` for future planning
- Enforces the **KIP-74 obligatory batch rule** — the first non-empty partition always returns its batch even if it exceeds `max_bytes`
- For subsequent partitions: includes data only if `bytes_left >= data_size`; data that can't fit is dropped and counted as `fetch_response_dropped_bytes`
- Transforms `aborted_transactions` into `fetch_response::aborted_transaction` structs with `producer_id` and `first_offset`
- Records `is_empty` in `g_empty_fetch_tracker` — a thread-local struct that logs empty fetch percentages every 100 fetches
- Records per-partition fetch latency via `octx.rctx.probe().record_fetch_latency()`

```
fill_fetch_responses()
    ├── fetch metadata cache update
    ├── KIP-74: obligatory first batch (bypass max_bytes)
    ├── bytes_left enforcement for subsequent partitions
    ├── aborted transactions assembly
    ├── g_empty_fetch_tracker.record(is_empty)
    └── fetch_latency probe recording
```

---

### Consumer Fetch Path — Full Sequence Diagram

```
CLIENT FETCH REQUEST
        │
        ▼
fetch_handler::handle()
        │  ── decode, op_context init, session resolution, placeholders
        ▼
simple_fetch_planner::create_plan()
        │  ── ACL check, shard resolution, byte budget estimation
        ▼
fetch_plan (partitions grouped by shard)
        │
        ▼
nonpolling_fetch_plan_executor::execute_plan()
        │  ── debounce, PID delay, per-shard worker spawn, deadline timer
        ▼
fetch_worker::do_run()  [runs on shard's CPU core]
        │  ── adaptive: read all (first), read signaled only (subsequent)
        ▼
do_read_from_ntp()
        │  ── memory alloc, leader check, epoch check, offset validate, LSO cap
        ▼
read_from_partition()
        │  ── log reader, kafka_batch_serializer, aborted tx, delta_from_tip
        ▼
read_result { iobuf data, offsets, hw, lso }
        │
        ▼
fill_fetch_responses()
        │  ── KIP-74 obligatory batch, bytes_left budget, metadata cache update
        ▼
op_context::send_response()
        │  ── session update, incremental fetch filtering (KIP-227)
        ▼
CLIENT FETCH RESPONSE
```

---

> **Source Reference:** `src/v/kafka/server/handlers/produce.cc` and `src/v/kafka/server/handlers/fetch.cc`
> — Redpanda Data, Inc. (Business Source License)