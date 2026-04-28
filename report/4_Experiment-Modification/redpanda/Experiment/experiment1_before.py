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
