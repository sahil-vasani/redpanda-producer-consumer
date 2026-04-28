from kafka import KafkaConsumer
import json

consumer = KafkaConsumer(
    "test-batch",
    bootstrap_servers="localhost:9092",
    auto_offset_reset="earliest",
    enable_auto_commit=False,
    group_id=None,
    consumer_timeout_ms=6000,
    value_deserializer=lambda v: json.loads(v.decode("utf-8"))
)

print("\n--- READING BACK ALL MESSAGES ---")
received = []
for msg in consumer:
    key = msg.key.decode()
    val = msg.value
    received.append(key)
    print(f"  offset={msg.offset}  key={key}  data={val['data']}")

consumer.close()

print(f"\n--- CONSUMER SUMMARY ---")
print(f"Expected : 20 messages")
print(f"Received : {len(received)} messages")

expected = {f"key-{i:03d}" for i in range(20)}
got = set(received)
missing = expected - got
if missing:
    print(f"MISSING  : {sorted(missing)}")
else:
    print("All 20 messages confirmed present")
