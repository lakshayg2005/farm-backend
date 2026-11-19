# Event retries and the dead-letter queue

How `farm_common.events.RabbitEventBus` handles a failing event handler, and how to inspect and
replay what ends up in a dead-letter queue (DLQ).

## What happens when a handler raises

Each consumer has a main queue `<service>.<name>` (usually `<service>.main`), for example
`order.main`. When its handler raises:

1. The exception is logged at WARNING with the traceback, the event type and `event_id`, and the
   attempt number (`retry 2/3 in 4.0s`).
2. The message is re-published, with `x-retries` + 1, to a delay queue
   `<queue>.retry.<ms>ms` (e.g. `order.main.retry.4000ms`). That queue has `x-message-ttl` and
   dead-letters back to the main queue, so the retry is delivered again after the delay and the
   consumer keeps processing other messages meanwhile.
3. Delays come from `RabbitEventBus(retry_delays=...)`, default `(1.0, 4.0, 15.0)` seconds; the
   last value is reused if `max_retries` is larger. A delay of `0` re-publishes straight to the main
   queue.
4. After `max_retries` (default 3) failed retries the message is rejected, logged at ERROR with the
   traceback, and RabbitMQ moves it to `<queue>.dlq`.

The delay is part of the queue name, so changing `retry_delays` declares new delay queues instead of
clashing with the old ones. Old, empty `*.retry.*ms` queues can be deleted from the management UI.

## Looking at the queues

```sh
docker compose exec rabbitmq rabbitmqctl list_queues name messages consumers
```

Anything non-zero in a `*.dlq` row needs a look. To see why, search the service log for
`sent to dead-letter queue` (or the `event_id`): each attempt logged its exception.

The management UI is at <http://localhost:15672> (user and password from `.env`):
**Queues and Streams** → `<queue>.dlq` → **Get messages** shows the event bodies and headers
(`x-retries`, `x-death`, `x-correlation-id`). Set *Ack mode* to "Nack message requeue true" to peek
without removing them.

## Replaying a DLQ

Fix the cause first (deploy the fix, or wait for the dependency to come back). Handlers are
idempotent (`mark_processed`), so replaying an event that was in fact processed is safe.

- **Management UI:** open `<queue>.dlq` → **Move messages**, destination `<queue>` (e.g.
  `order.main`). This needs the shovel plugins:
  `docker compose exec rabbitmq rabbitmq-plugins enable rabbitmq_shovel rabbitmq_shovel_management`.
- **One message by hand:** **Get messages** on the DLQ (ack mode "Reject requeue false" to take it
  off), copy the payload, then on the main queue use **Publish message** with the same payload and
  properties.

A moved message keeps its `x-retries` header, which is already at `max_retries`: if it fails again it
goes straight back to the DLQ without further retries. To give it fresh retries, publish it by hand
with `x-retries` set to `0` (or leave the header out).
