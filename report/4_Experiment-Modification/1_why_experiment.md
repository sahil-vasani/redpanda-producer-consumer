# Why These Experiments?

## Experiment 1: Impact of Message Size Limits in Redpanda (Producer + Broker)

In real-world systems like banking, trading, or e-commerce, messages can become large (e.g., transaction data, images, logs).

By default, streaming systems like Redpanda/Kafka have size limits. If a message exceeds this limit, it is rejected.

This experiment is done to understand:
1. Where large messages are blocked (producer or broker)
2. How system behavior changes when limits are increased
3. How to safely support larger messages in production systems

### Real-Life Example

Imagine a courier system:

**Truck (Producer)** → carries packages  
**Warehouse (Broker)** → accepts packages

**Before:**
- Truck capacity = 1 ton
- Warehouse capacity = 1 ton
- If package = 1.1 ton → rejected ❌

**After:**
- Truck capacity = 5 ton
- Warehouse capacity = 5 ton
- Same package → delivered successfully ✅

---

## Experiment 2: Duplicate Message Detection in Redpanda

In distributed streaming systems like Redpanda/Kafka, duplicate messages can occur due to retries, network issues, or producer behavior.

By default, the broker does not prevent duplicate messages, which can lead to incorrect data processing in downstream systems.

The goal of this experiment is to modify the Redpanda broker (C++ source code) to:
1. Detect duplicate messages using message keys
2. Log duplicate occurrences

This helps us understand how duplicate handling can be implemented at the broker level and its impact on system behavior.
