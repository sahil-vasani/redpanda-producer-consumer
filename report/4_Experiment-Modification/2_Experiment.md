# Experiments

## Experiment 1: Impact of Message Size Limits in Redpanda (Producer + Broker)

### Step 1: Build Redpanda

```bash
bazel build //src/v/redpanda:redpanda \
  --config=release \
  --jobs=2 \
  --local_resources=memory=4096 \
  --define=use_system_liburing=true \
  --action_env=LIBURING_USE_SYSTEM=1 \
  --spawn_strategy=standalone
```

**What it does:**
- Converts C++ source code → executable binary
- Applies your source code changes
- Produces final file: `bazel-bin/src/v/redpanda/redpanda`

---

### Step 2: Start the Redpanda Server

```bash
./bazel-bin/src/v/redpanda/redpanda \
  --redpanda-cfg redpanda.yaml \
  --smp=1 \
  --memory=1G \
  --reserve-memory=0M
```

> **NOTE:** After running, leave this terminal open.

**What it does:**
- Starts Redpanda server
- Opens Kafka API (port 9092)
- Ready to send/receive messages

---

### Step 3: Create Python Test File (Before Changes)

Create file: `nano experiment_before.py`

```python
from kafka import KafkaProducer, KafkaConsumer
import uuid, time, json

TOPIC = "batch-exp"
BROKER = "localhost:9092"
N = 20

def run_test(msg_size_kb):
    run_id = str(uuid.uuid4())
    print(f"\n=== TEST: {msg_size_kb} KB MESSAGE ===")

    payload = {
        "run_id": run_id,
        "data": "A" * (msg_size_kb * 1024)
    }

    # 🔴 STRICT PRODUCER (default limit)
    producer = KafkaProducer(
        bootstrap_servers=BROKER,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks=1,
        request_timeout_ms=20000
    )

    sent = 0
    for _ in range(N):
        producer.send(TOPIC, payload)
        sent += 1

    producer.flush()
    producer.close()

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BROKER,
        group_id=str(uuid.uuid4()),
        auto_offset_reset="earliest",
        value_deserializer=lambda v: v,
        consumer_timeout_ms=5000
    )

    received = 0
    for msg in consumer:
        try:
            data = json.loads(msg.value.decode())
            if data.get("run_id") == run_id:
                received += 1
        except:
            continue

    consumer.close()

    print(f"Sent: {sent}")
    print(f"Received: {received}")
    print(f"Missing: {sent - received}")


run_test(512)
time.sleep(2)
run_test(1100)
```

---

### Step 4: Run and Note Output

```bash
python experiment_before.py
```

> Note down the output, then press `Ctrl+C` in the Step 2 terminal.

---

### Step 5: Modify Source Code

**File:** `redpanda/src/v/kafka/server/handlers/produce.cc`

**Find:**
```cpp
auto batch_size = req.batch->size_bytes();
if (static_cast<uint32_t>(batch_size) > req.batch_max_bytes)
```

**Replace with:**
```cpp
auto batch_size = req.batch->size_bytes();

// 🔴 custom limit (experiment)
uint32_t custom_max_batch_bytes = req.batch_max_bytes * 5;

if (static_cast<uint32_t>(batch_size) > custom_max_batch_bytes) {
    auto msg = ssx::sformat(
      "batch size {} exceeds custom max {}",
      batch_size,
      custom_max_batch_bytes);

    thread_local static ss::logger::rate_limit rate(1s);
    vloglr(klog, ss::log_level::warn, rate, "{}", msg);

    co_return finalize_request_with_error_code(
      error_code::message_too_large,
      std::move(dispatched),
      req.ntp,
      ss::this_shard_id(),
      std::move(msg));
}
```

---

### Step 6: Rebuild Redpanda

```bash
bazel build //src/v/redpanda:redpanda \
  --config=release \
  --jobs=2 \
  --local_resources=memory=4096 \
  --define=use_system_liburing=true \
  --action_env=LIBURING_USE_SYSTEM=1 \
  --spawn_strategy=standalone
```

---

### Step 7: Restart the Server

```bash
./bazel-bin/src/v/redpanda/redpanda \
  --redpanda-cfg redpanda.yaml \
  --smp=1 \
  --memory=1G \
  --reserve-memory=0M
```

> **NOTE:** Leave this terminal open.

---

### Step 8: Create Python Test File (After Changes)

Create file: `nano experiment_after.py`

```python
from kafka import KafkaProducer, KafkaConsumer
import uuid, time, json

TOPIC = "batch-exp"
BROKER = "localhost:9092"
N = 20

def run_test(msg_size_kb):
    run_id = str(uuid.uuid4())
    print(f"\n=== TEST: {msg_size_kb} KB MESSAGE ===")

    payload = {
        "run_id": run_id,
        "data": "A" * (msg_size_kb * 1024)
    }

    # 🟢 RELAXED PRODUCER (allow large messages)
    producer = KafkaProducer(
        bootstrap_servers=BROKER,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks=1,
        request_timeout_ms=20000,
        max_request_size=5 * 1024 * 1024,
        buffer_memory=10 * 1024 * 1024
    )

    sent = 0
    for _ in range(N):
        producer.send(TOPIC, payload)
        sent += 1

    producer.flush()
    producer.close()

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BROKER,
        group_id=str(uuid.uuid4()),
        auto_offset_reset="earliest",
        value_deserializer=lambda v: v,
        consumer_timeout_ms=5000
    )

    received = 0
    for msg in consumer:
        try:
            data = json.loads(msg.value.decode())
            if data.get("run_id") == run_id:
                received += 1
        except:
            continue

    consumer.close()

    print(f"Sent: {sent}")
    print(f"Received: {received}")
    print(f"Missing: {sent - received}")


run_test(512)
time.sleep(2)
run_test(1100)
```

---

### Step 9: Run and Compare

```bash
python experiment_after.py
```

> Note down the output and compare with the before results.

![Before vs After Output](Images/Experiment1.png)

---

## Experiment 2: Duplicate Message Detection in Redpanda

### Step 1: Start Redpanda Server

```bash
./bazel-bin/src/v/redpanda/redpanda \
  --redpanda-cfg redpanda.yaml \
  --smp=1 \
  --memory=1G \
  --reserve-memory=0M
```

> Leave this terminal open.

---

### Step 2: Create Consumer Script

Create file: `nano experiment2.py`

```python
from kafka import KafkaConsumer

consumer = KafkaConsumer(
    "test_duplicates",
    bootstrap_servers="localhost:9092",
    auto_offset_reset="earliest",
    group_id=None,
    consumer_timeout_ms=5000
)

print("\n--- CONSUMER OUTPUT ---")

seen = {}

for msg in consumer:
    key = msg.key.decode() if msg.key else "None"

    if key not in seen:
        seen[key] = 0
    seen[key] += 1

    print(f"Received: key={key}")

consumer.close()

print("\n--- COUNT ---")
for k, v in seen.items():
    print(f"{k} → {v} times")
```

---

### Step 3: Run and Note Output

```bash
python experiment2.py
```

> Note down the output and check logs from Step 1. Then press `Ctrl+C` in the Step 1 terminal.

---

### Step 4: Modify Source Code for Duplicate Detection

**File:** `redpanda/src/v/kafka/server/handlers/produce.cc`

Find this line:
```cpp
auto batch_size = req.batch->size_bytes();
```

Add the following block **below** that line:

```cpp
// ==================== DUPLICATE DETECTION START ===========
static thread_local std::unordered_set<ss::sstring> seen_keys;

// Extract key from batch records
for (const auto& record : req.batch->copy_records()) {
    if (record.has_key()) {
        auto key_view = record.key();
        ss::sstring key_str(key_view.data(), key_view.size());

        // Check if key already seen
        if (seen_keys.contains(key_str)) {
            vlog(klog.info,
                 "🚫 DUPLICATE SKIPPED: ntp={} key='{}' - message rejected",
                 req.ntp, key_str);

            co_return finalize_request_with_error_code(
                error_code::none,  // Return success to avoid client errors
                std::move(dispatched),
                req.ntp,
                ss::this_shard_id());
        }

        // Store new key
        seen_keys.insert(key_str);
        vlog(klog.info,
             "✅ NEW KEY ACCEPTED: ntp={} key='{}' - message will be stored",
             req.ntp, key_str);
    }
}
// ==================== DUPLICATE DETECTION END ================
```

---

### Step 5: Rebuild Redpanda

```bash
bazel build //src/v/redpanda:redpanda \
  --config=release \
  --jobs=2 \
  --local_resources=memory=4096 \
  --define=use_system_liburing=true \
  --action_env=LIBURING_USE_SYSTEM=1 \
  --spawn_strategy=standalone
```

---

### Step 6: Restart Server

```bash
./bazel-bin/src/v/redpanda/redpanda \
  --redpanda-cfg redpanda.yaml \
  --smp=1 \
  --memory=1G \
  --reserve-memory=0M
```

---

### Step 7: Rerun Consumer Script

```bash
python experiment2.py
```

---

### Step 8: Check Logs

- Check how many duplicates were detected
- Check which keys were flagged as duplicates

---

### Step 9: Results

![Before vs After Output](Images/Experiment2.png)
