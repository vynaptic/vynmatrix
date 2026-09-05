# Strategy Readiness

This inventory describes the retained source configuration in vynmatrix. It is
not a record of a completed paper soak, broker certification, or deployment.
No historical result transfers authority to the independent migration.

| Strategy | Source configuration | Local role | Readiness boundary |
|---|---|---|---|
| [SwingHighLowPMO](../strategies/indicator/SwingHighLowPMO/README.md) | `runner_kind=signal_worker`, `enabled=true`, `environments=[dev]`, `E2E_PIPELINE_CANARY_ONLY` | Explicit development pipeline canary, version `1.1.0` | Requires narrow maintenance activation of the exact registered release, owner/account/binding authority, explicit selector, real warmup and fresh bars; permanently excluded from paper promotion and live trading |
| [USQualityCompounder](../strategies/indicator/USQualityCompounder/README.md) | `runner_kind=signal_worker`, `enabled=false`, `environments=[dev]` | Equity portfolio research and disabled runtime implementation | Remains disabled pending complete owner-scoped evidence, panels, catalogue/session coverage, account reconciliation, and exact paper authority |
| [_template](../strategies/indicator/_template/config.json) | `runner_kind=signal_worker`, `enabled=false`, `environments=[dev]` | Development scaffold | Not a candidate for execution or performance evaluation |

The configured indicator wheel ships in `vynmatrix/platform`, but only an explicit
selector starts its worker inside the existing process group. Runtime selection,
strategy/version activity, designated-owner configuration, account-scoped
bindings, instrument identity, market sessions, data entitlement, and account
execution authority are independent checks. Do not enable a strategy merely to
make an onboarding example emit signals.

[Database bootstrap and catalogue registration](DATABASE.md) establish the
explicit owner and inactive references; they create no broker account, binding
or execution selection. The narrow `vmdev db activate-canary` operation applies
only to an exact existing enabled dev-only E2E canary under maintenance authority,
with paper mode and live permission false. It does not establish general release
eligibility or activate account/binding flags. Follow the
[E2E procedure](E2E_VERIFICATION_GUIDE.md) for the exact Swing command and scope.

For an independently eligible candidate, before claiming paper readiness capture
the exact source/wheel/`vynmatrix/platform` image identity,
review strategy-specific evidence requirements, and complete the applicable
[paper E2E checks](E2E_VERIFICATION_GUIDE.md). A strategy-specific soak window must
be documented and approved for that exact candidate; this migration does not
supply one. Coinbase's inherited 14-day broker paper-soak reference in the
[runbook](RUNBOOK.md) is separate from strategy performance acceptance.
The logical `indicator-runner` attestation role still identifies that platform
image; an old indicator-image attestation or promotion manifest cannot be reused
by changing its image label. Successful Swing pipeline verification remains
development evidence only.

[USQualityCompounder's design](../strategies/indicator/USQualityCompounder/README.md)
contains inherited diagnostic results and implementation notes. Reproduce the
necessary recorded-data checks in the prepared validation environment before
using those notes as evidence. All local verification retains
`EXECUTION_ENGINE_ALLOW_LIVE=false`.
