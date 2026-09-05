# Strategy readiness

This is the current source-configuration and authority inventory. It is not a
paper soak, broker certification, deployment, or performance record.

| Strategy | Source state | Role |
| --- | --- | --- |
| [SwingHighLowPMO](../strategies/indicator/SwingHighLowPMO/README.md) | enabled in dev, E2E pipeline canary, 1.1.0 | Development pipeline canary |
| [USQualityCompounder](../strategies/indicator/USQualityCompounder/README.md) | disabled in dev | Equity portfolio research |
| [_template](../strategies/indicator/_template/config.json) | disabled in dev | Development scaffold |

Current boundaries:

- Swing requires exact maintenance activation, owner/account/binding authority,
  an explicit selector, and fresh real warm-up data; it is permanently excluded
  from paper promotion and live trading.
- USQuality requires prospective owner-scoped data/panel/catalogue/session
  evidence, account reconciliation, and separate paper authority.
- The template is not an execution or performance candidate.

The shared platform image shipping a strategy is not authority. STRATEGY_LIST,
strategy/version state, owner, binding, exact account, instrument route, session
coverage, entitlement, and current execution checks are independent. Bootstrap
registers inactive references; it creates no broker account, binding, or route.

The maintenance canary action applies only to the exact eligible Swing release
with paper mode and the live gate false. It enables no account/binding and does
not generalize to another candidate. Use
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) for its evidence and
[DATABASE.md](DATABASE.md) for registration/activation boundaries.
