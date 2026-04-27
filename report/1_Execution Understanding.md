# Execution Understanding — Redpanda Producer-Consumer

## System Flow Overview

```
Producer → Kafka handler → Cluster → Partition → Raft → Storage → Consumer
```

**Complete Journey of Redpanda:**

1. Producer sends message
2. System processes it (handler, partition, etc.)
3. Raft replicates (reliability)
4. Storage saves data
5. Consumer reads data

---

# PART 1 — PRODUCER

**Source File:** `kafka/server/handlers/produce.cc`

---

## Step 1: Main Entry Function — Producer-Consumer Flow

```cpp
produce_handler::handle(request_context ctx, ...)
```

- A producer request has arrived → process it

---

## Step 2: Decode Request

```cpp
request.decode(ctx.reader(), ctx.header().version);
```

- Convert network data → internal object
- Now system understands:
  - topic
  - partition
  - records

---

## Step 3: Logging

```cpp
log_request(ctx.header(), request);
```

- Just debug/logging (not important for flow)

---

## Step 4: Basic Checks

**Check 1: Recovery Mode**

```cpp
if (ctx.recovery_mode_enabled())
```

- If system recovering → reject request

**Check 2: Disk Full**

```cpp
if (ctx.metadata_cache().should_reject_writes())
```

- If disk full → return error

---

## Step 5: Pre-Processing (Inside handle())

### Loop 1: Count Internal Topic Bytes

```cpp
for (const auto& topic : request.data.topics)
```

- Loop over all topics

```cpp
for (const auto& part : topic.partitions)
```

- Loop over all partitions
- If records exist → add size to `resp.internal_topic_bytes`
- Purpose: tracking usage (not core flow)

### Loop 2: Detect Transactional / Idempotent

```cpp
for (auto& topic : request.data.topics)
    for (auto& part : topic.partitions)
```

- Loop again over all topics + partitions
- Inside: Check batch header
- Set:
  - `request.has_transactional`
  - `request.has_idempotent`
- Purpose: detect message type

### IF Conditions (Validation)

**1. Transaction Check**

```cpp
if (request.has_transactional)
```

- Check: transactions enabled? authorized?
- If not → return error

**2. Idempotent Check**

```cpp
else if (request.has_idempotent)
```

- If idempotence disabled → error

**3. ACK Check**

```cpp
else if (request.data.acks < -1 || request.data.acks > 1)
```

- Invalid ack → error

---

## Step 6: Authorization Filter

**Partition topics into authorized / unauthorized**

```cpp
std::partition(...)
```

- Move authorized topics to front

**Fill error for unauthorized**

```cpp
fill_response_with_errors(...)
```

- Add error in response

**Remove unauthorized topics**

```cpp
request.data.topics.erase_to_end(...)
```

- Now only valid topics remain

---

## Step 7: Create Promise + Context

**Promise**

```cpp
ss::promise<> dispatched_promise;
```

- Used to track async completion

**Context**

```cpp
produce_ctx(...)
```

- Contains: request, response, metadata, connection info

---

## Step 8: Call produce_topics()

**Loop over topics**

```cpp
for (auto& topic : octx.request.data.topics)
```

- For each topic: `produce_topic(octx, topic)`

---

## Step 9: Function produce_topic()

**Get topic metadata**

```cpp
metadata_cache().get_topic_metadata_ref(...)
```

- If not found → return error

**Create config context**

- Stores: batch size, timestamp rules

**Create vectors**

- `partitions_produced`
- `partitions_dispatched`
- To store results

**Loop partitions**

```cpp
for (auto& part : topic.partitions)
```

Inside loop:

- Check 1: partition disabled → push error
- Check 2: records null → error
- Check 3: legacy error → error
- Check 4: CRC valid → if not → error
- Check 5: format valid → if not → error
- If all valid → `produce_topic_partition(...)`

**Collect futures**

```cpp
partitions_produced.push_back(...)
partitions_dispatched.push_back(...)
```

**Combine results**

```cpp
when_all_succeed(...)
```

- Wait for all partitions

**Build response**

```cpp
produce_response::topic
```

---

## Step 10: Function produce_topic_partition()

**Create ntp (namespace-topic-partition)**

```cpp
model::ntp(...)
```

**Create schema validator** (optional)

**Create batch**

```cpp
record_batch
```

**Create promise**

```cpp
dispatch
```

**Call:**

```cpp
do_produce_topic_partition(...)
```

---

## Step 11: Function do_produce_topic_partition()

**Step 1: Validate batch**

```cpp
validate_batch(...)
```

- If error → return error

**Step 2: Check batch size**

```cpp
if (batch_size > max)
```

- If too large → error

**Step 3: Schema validation**

```cpp
if (validator)
```

- If fail → error

**Step 4: Find shard**

```cpp
shard_for(ntp)
```

- Which CPU handles partition

**Step 5: Metrics recording**

```cpp
probe().record_batch(...)
```

- Just tracking

**Step 6: Timeout handling**

```cpp
if (timeout < 0)
```

- Set max timeout

**Step 7: Call partition manager**

```cpp
partition_manager().invoke_on(...)
```

---

## Step 12: Inside invoke_on()

**Get partition**

```cpp
make_partition_proxy(...)
```

**Check leader**

```cpp
if (!partition || !partition->is_leader())
```

- If not leader → error

**Main Call**

```cpp
partition_append(...)
```

---

## Step 13: Function partition_append()

**Call replicate**

```cpp
partition.replicate(...)
```

- Send to RAFT

**Create future**

- `dispatched` → request sent
- `produced` → replication result

**On success:**

- calculate offset
- set timestamp
- update metrics

**On error:**

- map error code

---

## Step 14: Back to handle()

**Collect all dispatched futures**

```cpp
when_all_succeed(dispatched)
```

**Then collect produced futures**

```cpp
when_all_succeed(produced)
```

**Build final response**

```cpp
octx.response.data.responses.push_back(...)
```

**Send response**

```cpp
ctx.respond(...)
```

**Special case: acks = 0**

- If no error → response dropped
- If error → connection closed

---

## Final Complete Producer Flow

```
handle()
  → loops (topics, partitions)
  → produce_topics()
  → produce_topic()
  → produce_topic_partition()
  → do_produce_topic_partition()
  → partition_manager.invoke_on()
  → partition_append()
  → replicate() (RAFT)
  → storage write
  → response back
```

---

# PART 2 — CONSUMER

**Entry Point:** `fetch_handler::handle(...)`

---

## Step 1: Handle Function

**Create context**

```cpp
op_context(...)
```

- Stores: request, response, metadata, limits (bytes)

**Decode request**

```cpp
request.decode(...)
```

- Convert network → internal format

**Check session error**

```cpp
if (octx.session_ctx.has_error())
```

- If error → return response

**Check recovery mode**

```cpp
if (octx.rctx.recovery_mode_enabled())
```

- If recovering → return error

**Set error_code = none**

- Normal start

**Call main function**

```cpp
do_fetch(octx)
```

---

## Step 2: do_fetch()

**Create planner**

```cpp
make_fetch_planner(...)
```

**Create plan**

```cpp
planner.create_plan(octx)
```

- This decides: which partitions to read, how much data

**Create executor**

```cpp
nonpolling_fetch_plan_executor
```

**Execute plan**

```cpp
executor.execute_plan(...)
```

---

## Step 3: Create Plan (Very Important)

**Loop topics**

```cpp
for_each_fetch_partition(...)
```

- Loop over topics

For each partition:

- Check 1: topic empty → error
- Check 2: authorization → error if not allowed
- Check 3: topic exists → if not → error
- Check 4: partition disabled → error

**Find shard**

```cpp
shard_for(ktp)
```

- Which CPU handles partition

**Create fetch config**

- Stores: offset, max_bytes, timeout, isolation level

**Add to plan**

```cpp
plan.fetches_per_shard[shard].push_back(...)
```

- Group by shard

---

## Step 4: Execute Plan

**Start workers**

```cpp
start_shard_fetch_worker(...)
```

- Each shard handles its partitions

**Loop until done**

```cpp
for (;;)
```

- Keep fetching until: enough data, timeout, or error

---

## Step 5: Fetch Worker

**Function**

```cpp
fetch_worker::run()
```

**Inside loop:**

```cpp
for (;;)
```

- Repeat fetch

**First run:**

```cpp
requests = _ctx.requests
```

- Take all partitions

**Next runs:**

- Only re-fetch changed partitions

**Call:**

```cpp
query_requests(...)
```

---

## Step 6: query_requests()

**Loop partitions**

```cpp
for (i = 0; i < requests.size(); i++)
```

**Get partition**

```cpp
mgr.get(...)
```

**Get raft consensus**

```cpp
part->raft()
```

- Used to track offsets

**Save last_visible_index**

- Used for change detection

**Call:**

```cpp
fetch_ntps(...)
```

---

## Step 7: fetch_ntps()

**Loop partitions (parallel)**

```cpp
max_concurrent_for_each(...)
```

For each partition:

- Check: max bytes reached → skip read
- Call: `do_read_from_ntp(...)`

---

## Step 8: do_read_from_ntp()

**Allocate memory**

```cpp
allocate_memory_units(...)
```

**Get partition**

```cpp
make_partition_proxy(...)
```

**Check leader**

```cpp
if (!is_leader)
```

- return error

**Validate offset**

```cpp
validate_fetch_offset(...)
```

**Get last stable offset**

```cpp
last_stable_offset()
```

**Adjust max_offset** (for transactions)

**Call core read**

```cpp
read_from_partition(...)
```

---

## Step 9: read_from_partition() — Core Read

**Get metadata**

- `high_watermark`
- `start_offset`

**Check if no data**

```cpp
if (hw < start_offset)
```

- return empty

**Create reader config**

- defines: offset range, max bytes

**Create reader**

```cpp
part.make_reader(...)
```

**Read data**

```cpp
reader.consume(...)
```

> THIS ACTUALLY READS DATA FROM STORAGE

**Store results:**

- data
- offsets
- batch_count

**Update metrics**

- `add_records_fetched`
- `add_bytes_fetched`

**Handle aborted transactions** (optional)

**Return result**

```cpp
read_result(...)
```

---

## Step 10: Back Flow

- Results collected: `results[cfg_idx] = ...`
- Total size updated
- Return to worker

---

## Step 11: Build Response

**Function**

```cpp
fill_fetch_responses(...)
```

**Loop results**

```cpp
for (idx)
```

For each partition:

- If error → set empty response
- If success:
  - set offsets
  - attach records
  - attach aborted transactions

**Check response size**

- If too large → drop data

---

## Step 12: Final Response

**Back to handle()**

```cpp
send_response()
```

**Loop topics + partitions**

- calculate total bytes

**Handle session:**

- sessionless → direct response
- session → update session cache

**Send response**

```cpp
rctx.respond(...)
```

---

## Final Complete Consumer Flow

```
fetch_handler::handle()
  → do_fetch()
  → create_plan()
  → execute_plan()
  → fetch_worker.run()
  → query_requests()
  → fetch_ntps()
  → do_read_from_ntp()
  → read_from_partition()
  → build response
  → send_response()
```

---

# Full Producer → Consumer Flow

```
Producer → write → raft → storage
Consumer → fetch → read → return
```

## Complete Diagram

```
               +---------------+
               |   Producer    |
               | (client send) |
               +------+--------+
                      |
                      v
        +----------------------------+
        | produce_handler::handle()  |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | produce_topics()           |
        | (loop topics)              |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | produce_topic()            |
        | (loop partitions)          |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | do_produce_topic_partition |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | partition_manager          |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | partition_append()         |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | raft::replicate()          |
        | (replication)              |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | Storage (disk write)       |
        +----------+-----------------+
                   |
                   v
        --------- DATA STORED ---------
                   |
                   v
        +----------------------------+
        | fetch_handler::handle()    |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | do_fetch()                 |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | fetch_plan (planner)       |
        | (select partitions)        |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | fetch_worker.run()         |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | do_read_from_ntp()         |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | read_from_partition()      |
        | (read from storage)        |
        +----------+-----------------+
                   |
                   v
        +----------------------------+
        | build response             |
        +----------+-----------------+
                   |
                   v
               +---------------+
               |   Consumer    |
               | (gets data)   |
               +---------------+
```

---

## Write Path (Producer Side)

```
Producer
  → produce_handler::handle()
  → produce_topic()
  → produce_topic_partition()
  → do_produce_topic_partition()
  → partition_manager
  → partition_append()
  → raft::replicate()
  → Storage (disk write)
```

## Read Path (Consumer Side)

```
Consumer
  → fetch_handler::handle()
  → do_fetch()
  → fetch_plan (planner)
  → fetch_worker.run()
  → do_read_from_ntp()
  → read_from_partition()
  → Storage (read)
  → Response to consumer
```
