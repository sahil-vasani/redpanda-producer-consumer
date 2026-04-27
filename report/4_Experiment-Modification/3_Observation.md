# Observations

## Experiment 1: Impact of Message Size Limits in Redpanda

### Before Changes

| Message Size | Received | Status     |
|--------------|----------|------------|
| 512 KB       | 20 / 20  | ✅ Success |
| 1100 KB      | 0 / 20   | ❌ Rejected |

**Observation:** Large messages are rejected due to default system limits (~1MB).

---

### After Changes

| Message Size | Received | Status     |
|--------------|----------|------------|
| 512 KB       | 20 / 20  | ✅ Success |
| 1100 KB      | 20 / 20  | ✅ Success |

**Observation:** After increasing limits in both producer and broker, large messages are successfully processed.

---

### Key Insight

> Message size limits exist at **multiple layers** in distributed systems.

- Even if the broker allows large messages, the **producer must also allow sending them**.
- Both layers must be configured correctly to support large data.

---

## Experiment 2: Duplicate Message Detection in Redpanda

### Results

1. **New messages are accepted:**
   ```
   ✅ NEW KEY ACCEPTED: key='id_1'
   ✅ NEW KEY ACCEPTED: key='id_2'
   ```

2. **Duplicate messages are detected and skipped:**
   ```
   🚫 DUPLICATE SKIPPED: key='id_1'
   🚫 DUPLICATE SKIPPED: key='id_2'
   ```

3. System correctly distinguishes between:
   - First occurrence → **stored**
   - Repeated occurrence → **rejected**

4. Logs confirm that duplicate detection logic is working at the broker level.

---

### Key Insight

> This experiment shows that:

- Kafka/Redpanda does **not** prevent duplicates by default
- Duplicate handling requires additional logic (like idempotency or custom filtering)
- Broker-level modification can control **data quality before storage**
- Message keys can be used as a simple identifier for duplicate detection
