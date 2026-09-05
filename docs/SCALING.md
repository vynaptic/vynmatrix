# Deferred capacity decisions

The supported single-owner runtime has at most three running containers,
including PostgreSQL. [DEPLOYMENT.md](DEPLOYMENT.md) owns the current split and
combined layouts. This document records decisions that need measured demand and
a separate design; it does not claim hosted capacity, recovery performance, or
broker certification.

| Trigger | First response | Guardrail |
| --- | --- | --- |
| Worker CPU or memory pressure | Measure selected feeds/strategies, reduce selection or resize the host | Keep one supervised worker group and explicit STRATEGY_LIST. |
| Database connection pressure | Measure per-process pools and checked-out/overflow metrics | Change bounded pool settings only after a host/database budget review. |
| Owner UI | Serve static assets through the backend application group | Do not add a frontend container by default. |
| IBKR gateway | Use the combined application group if an approved gateway container is unavoidable | The gateway consumes the third slot and remains an external operational dependency. |
| Recovery objective beyond one host | Evaluate backup restore time and a separately operated PostgreSQL service | No replica, failover, managed database, or hosted topology is implied today. |

PostgreSQL, the transactional outbox, and idempotent consumers remain the
durable handoff model. Per-strategy containers, Redis, extra brokers,
replication, and a second scheduler are outside the current design. A change
requires a measured bottleneck, owner approval, an updated topology, and
evidence that authority, ledger, outbox, and safety gates remain intact.
