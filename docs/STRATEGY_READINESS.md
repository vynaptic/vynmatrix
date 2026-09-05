# Strategy Readiness

This inventory describes the retained source configuration in vynmatrix. It is
not a record of a completed paper soak, broker certification, or deployment.
No historical result transfers authority to the independent migration.

| Strategy | Source configuration | Local role | Readiness boundary |
|---|---|---|---|
| [SwingHighLowPMO](../strategies/indicator/SwingHighLowPMO/README.md) | `runner_kind=signal_worker`, `enabled=true`, `environments=[dev]` | Explicit development canary | Requires exact `STRATEGY_LIST`, real historical warmup, fresh price bars, and normal runtime gates; no live approval |
| [USQualityCompounder](../strategies/indicator/USQualityCompounder/README.md) | `runner_kind=signal_worker`, `enabled=false`, `environments=[dev]` | Equity portfolio research and disabled runtime implementation | Remains disabled pending complete owner-scoped evidence, panels, catalogue/session coverage, account reconciliation, and exact paper authority |
| [_template](../strategies/indicator/_template/config.json) | `runner_kind=signal_worker`, `enabled=false`, `environments=[dev]` | Development scaffold | Not a candidate for execution or performance evaluation |

The configured indicator wheel includes source, but only an explicit selector
can start a worker. Runtime selection, strategy/version activity, per-user
bindings, instrument identity, market sessions, data entitlement, and account
execution authority are independent checks. Do not enable a strategy merely to
make an onboarding example emit signals.

Before claiming paper readiness, capture the exact source/wheel/image identity,
review strategy-specific evidence requirements, and complete the applicable
[paper E2E checks](E2E_VERIFICATION_GUIDE.md). A strategy-specific soak window must
be documented and approved for that exact candidate; this migration does not
supply one. Coinbase's inherited 14-day broker paper-soak reference in the
[runbook](RUNBOOK.md) is separate from strategy performance acceptance.

[USQualityCompounder's design](../strategies/indicator/USQualityCompounder/README.md)
contains inherited diagnostic results and implementation notes. Reproduce the
necessary recorded-data checks in the prepared validation environment before
using those notes as evidence. All local verification retains
`EXECUTION_ENGINE_ALLOW_LIVE=false`.
