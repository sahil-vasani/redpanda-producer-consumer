from kafka import KafkaProducer
import json
import time

producer = KafkaProducer(
    bootstrap_servers=['localhost:9092'],
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

topic = 'test_duplicates'

print("Sending messages with duplicate keys...")
messages = [
    ('id_1', {'msg': 'First message with id_1', 'timestamp': 1}),
    ('id_2', {'msg': 'First message with id_2', 'timestamp': 2}),
    ('id_1', {'msg': 'DUPLICATE id_1', 'timestamp': 3}),
    ('id_3', {'msg': 'First message with id_3', 'timestamp': 4}),
    ('id_2', {'msg': 'DUPLICATE id_2', 'timestamp': 5}),
    ('id_1', {'msg': 'DUPLICATE id_1 again', 'timestamp': 6}),
    ('id_4', {'msg': 'First message with id_4', 'timestamp': 7}),
]

for key, value in messages:
    future = producer.send(topic, key=key.encode('utf-8'), value=value)
    result = future.get(timeout=10)
    print(f"Sent: key={key}, value={value['msg']}")
    time.sleep(0.1)

producer.close()
print("\n✅ All messages sent")
