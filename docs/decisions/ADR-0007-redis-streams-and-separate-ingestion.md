# ADR-0007: Move events over Redis Streams, with ingestion as its own service

- **Date:** 2026-10-09
- **Status:** accepted
- **Phase:** 6 (discovery pipeline), completed in 8

## Context

Phase 6 shipped an `InMemoryEventBus` (an `asyncio.Queue`) behind an
`EventBus` Protocol, and ran ingestion (EDGAR poller, Alpaca news
websocket) inside the same process as the agent worker and the API. That
was the right v1, but it left three gaps against the architecture the
design doc draws (three processes meeting only at shared state):

- An event in the queue when the backend restarts is lost.
- Restarting the API restarts the news websocket, and Alpaca's free tier
  allows one connection — a fast restart races the old session (`406
  connection limit exceeded`).
- "Replay the stored event log" had no log to replay.

## Decision

`RedisStreamEventBus` (`app/discovery/bus.py`) implements the same Protocol
on a Redis stream with one consumer group. The worker ACKs an event only
after processing it; a consumer with a stable name reclaims its own pending
entries on restart (at-least-once). The stream is MAXLEN-capped (~2 months)
and doubles as the replay log. Ingestion runs as its own container
(`python -m app.discovery.ingest`), publishing to the stream and writing a
TTL'd heartbeat snapshot to Redis, which is how `/autonomous/status`
reports its health without the processes ever calling each other.
Because a worker that was down comes back to a backlog, a staleness gate
skips (visibly) events that waited longer than
`reactive_max_event_age_seconds` — the reactive path is only worth
anything when it's minutes-fresh. `EVENT_BUS=memory` + `INGESTION_MODE=embedded`
remain the defaults for tests and a plain `uvicorn` run.

## Alternatives considered

- **Keep the in-memory queue.** Fine for tests; loses events on restart and
  couples the websocket's lifetime to the API's.
- **Redis pub/sub.** No persistence or acknowledgement: a message published
  while the worker is down is gone.
- **Redis lists (LPUSH/BRPOP).** No consumer groups or pending-entry
  recovery without hand-rolling them.
- **Redpanda/Kafka.** ~800 events/day; unearned complexity (design doc §6).

## Consequences

- **Positive:** worker restarts no longer lose events or bounce the news
  websocket; the stream is the replay source; the deployed topology now
  matches the architecture diagram.
- **Negative:** at-least-once means a crash *after* a thesis is written but
  *before* the ACK can re-run that event (rare: the window is the few
  milliseconds between persist and ACK). Two gotchas found while wiring it:
  redis-py 8's default 5 s socket timeout equals the 5 s `XREADGROUP`
  block, so idle reads raced it ("Timeout reading from redis") until the
  bus's connection got a longer timeout; and a client that completes a
  blocking read without suspending (fakeredis) can spin the consume loop,
  so it yields explicitly on empty reads.
- **Reversibility:** High: `EVENT_BUS=memory`, `INGESTION_MODE=embedded`.

## References

- Related: [[ADR-0001]] (architecture overview).
- `app/discovery/bus.py`, `app/discovery/ingest.py`, `app/discovery/replay.py`.
