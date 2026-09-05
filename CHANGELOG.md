# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This technical history was inherited with the source snapshot and sanitized for
vynmatrix. The new repository has fresh Git history; earlier commits, runtime
artifacts, external infrastructure, personal accounts, and certification evidence
are not included. Past test/deployment statements below describe prior work and
are not verification results for this migration. The project is not yet
open-source: [LICENSE](LICENSE) is unchanged and a license/rights decision is pending.

## [Unreleased]

- Prepared the independent vynmatrix codebase with neutral example identities,
  renamed package/image branding, a local contributor workflow, and explicit
  pending license/rights and publication boundaries. Historical operational
  claims do not confer authority on this migration.

- **Closed two US Quality Compounder market-data admission gaps.** Forward panel registration now
  requires 80% factor-complete coverage overall and 70% in every sector with at least 10 members.
  A generic hash-pinned equity catalogue command dry-runs or atomically provisions exact USD
  equity fields, reviewed positive whole-share lot sizes, existing-XNYS bindings, and positive
  IBKR `STK` conIds; conflicts fail closed and identical artifacts replay as no-ops.
  IBKR portfolio targets are floored to the catalogued lot without overspending and the adapter
  rejects non-multiples before submission. Quarter-end evidence production now requires the exact
  active model version but not an executable strategy row, removing the activation cycle while
  strategy config and bindings remain disabled.

- **Generalized the existing paper-promotion manifest to synchronized portfolios.** The single
  fail-closed gate now binds a data-driven strategy/version/config/image and exact
  user/binding/account/broker route. Portfolio authority additionally requires
  `paper_forward` scope, one pre-start model-configuration digest, and a reviewed hash-pinned
  instrument-id/canonical-symbol allowlist. Rebalance legs are an exact subset of that allowlist,
  bindings match its full symbol set, and zero-leg all-cash decisions remain valid. Rank and panel
  input hashes remain per-run audit lineage rather than circular promotion identity. Ordinary
  per-symbol dispatch cannot consume portfolio authority; all manifests continue to deny live.

- **Advanced the disabled US Quality Compounder candidate to v0.2 after sequential
  recorded-data coverage experiments.** Exact component diagnostics rejected a filing-drift-only
  patch, then accepted operating profitability in place of gross profitability and a simpler
  issuer-aware fundamental-growth sleeve in place of filing-event drift. The outer
  quality/growth/valuation/momentum weights remain 35/30/20/15. Factor-complete coverage improved
  from 3.6–30.9% in v0.1 to 61.7–64.4% in v0.2 without a material-sector coverage failure, while
  growth/momentum correlation stayed bounded. A labelled one-year comparison smoke test returned
  11.45% versus v0.1's 8.48% and SPY's 18.99%, with 9.93% maximum drawdown and 1.92x annual
  turnover. The result remains diagnostic and below the provisional 80% coverage gate, so config,
  catalogue, and bindings remain disabled. v0.1 lineage is deprecated but immutable; v0.2 has
  distinct factor, panel, validator, configuration, and strategy-version identities. Persisted
  factor contributions now use the same quantized weight and normalized values stored in their
  audit rows. The shared descriptive factor-risk model is versioned to 2.0 and consumes the same
  raw operating-profitability component, preventing the risk adapter from depending on the
  rejected gross-profitability evidence.

- **Rewrote `CLAUDE.md`/`AGENTS.md` as instruction files and relocated their reference
  material to the documents that own it.** The agent files went from 2 004 to 211 lines by
  moving, not discarding: the `# noqa` policy, the lint rules that were missing from the
  style table, and the "pick the right service base" rationale now live in
  `CONTRIBUTING.md`; the full 15-rule `vmdev audit` reference (with severities, thresholds,
  `MIGRATION_EXEMPTIONS` and `SESSION_DRIFT_ALLOWLIST` semantics) in
  `docs/REVIEWER_CHECKLIST.md`; a canonical symbol index, the internal event-contract table,
  the cross-layer impact matrix and the strategy add/remove lockstep checklist in
  `docs/USER_MANUAL.md`; the `SignalAction`-to-column mapping and the
  `mode_selection_policy` enum and ranking semantics in `docs/DATABASE.md`; the two-tier
  dependency version authority in `docs/CONFIGURATION.md`; and the Windows path-separator
  and script-parity caveats in `SETUP_WINDOWS.md`. Corrected defects that were actively
  misleading agents, including a deleted strategy presented as current, four unresolvable
  commit hashes, option-spread and library paths missing their inner package directory, a
  broken section anchor, code examples whose imports and call signatures did not match the
  real modules, and an invented `vmdev test strategy` subcommand (`vmdev test` has only
  `all`, `lib`, `team`; strategy campaigns use the separate `vmdev strategy` group).
  Absolute rules were made proportionate rather than deleted: deterministic fixtures are
  explicitly valid at unit boundaries while backtests and end-to-end proofs still require
  recorded real data; research and delegation are no longer mandatory gates for routine
  work; only surfaces the change genuinely reaches must be updated; focused `pytest` runs
  are expected while iterating; and platform-specific commands are given for the platform in
  use. `CONTRIBUTING.md`'s links into the relocated sections were repointed.

- **Added immutable synchronized-panel historical replay and ran the first recorded US Quality
  Compounder diagnostic.** Panel artifacts bind the exact snapshot and full strategy identity,
  verify canonical codec round-trips and official next-session execution, reject truncated
  portfolio-state windows, and preserve the verified manifest digest in results. A replacement
  diagnostic-only EODHD/SEC snapshot produced five 2024–2025 panels and a one-year smoke result:
  +8.48% versus SPY total return's +18.99%, 0.84x annual turnover, 2.73% maximum drawdown, and only
  seven closed trades. Required factor evidence collapsed from 156 complete members in June to 18
  in September, leaving the portfolio mostly cash; the strategy therefore fails the paper-promotion
  bar and remains disabled. The one-off compiler was deleted while immutable snapshot, panel, and
  result evidence was retained. Strict EODHD ingestion now omits syntactically valid rows outside
  the pinned official-session axis before requiring exact official coverage. The canonical signal
  supplies explicit zero target-weight drift, rank-model configs no longer claim calibrated
  expected-return/risk or stop-loss inputs, and an optional binding-level
  `entry_cash_buffer_bps` now reaches the settled-USD execution guard.

- **Discarded `LiquidityLeadersBasket` after it failed its acceptance bar over a full
  market cycle.** The strategy's own recorded failure mode — "the 2000-2010 analog
  (most-traded names of 2000 stagnating for a decade)", previously documented as "not
  testable with current vendor data" — was tested and confirmed. Over 2000-2025 the
  basket returned +6.55 %/yr against SPY total return's +8.03 %/yr, with an 86.0 %
  maximum drawdown and 19.9 continuous years underwater; the 2000-2011 leg returned
  −60.59 % against the index's +7.83 %, losing 31.0 pp of excess in the 2003-2006
  *recovery* rather than the bust. The 2012-2025 outperformance is beta 1.25, one
  mega-cap regime, and one name (NVDA supplied 49.7 % of the modern leg's P&L;
  substituting the benchmark for that slot makes the strategy underperform). Basket-size
  and rank-buffer variants were measured by paired stationary block bootstrap and every
  confidence interval contained zero, with the sign reversing in the clean window, so no
  replacement configuration was nominated. Per the repo's no-deprecation-cycle policy the
  strategy is deleted rather than disabled: strategy directory, pytest testpath,
  catalogue and version-lineage seed rows, and the e2e paper binding. A fail-closed
  migration and seed convergence disable any authority left in an existing database.
  The equity portfolio simulator's 26 rule-level tests are retained and now run against a
  test-owned `tests/fixtures/strategies/EquityBasketExerciser` core, following the
  precedent set when the scalpers moved their coverage to `PipelineExerciser`. Equity
  instrument catalogue rows are retained as shared reference data.

- **Reduced always-loaded agent context and removed duplicate skill discovery.** Replaced
  the 90 KB `AGENTS.md`/`CLAUDE.md` handbooks with a 7.6 KB production-safety core that
  routes detailed work to canonical documentation only when relevant. Routine work no
  longer requires web research, subagent delegation, or full Docker verification. Removed
  the byte-identical `.codex/skills/pr-description` copy; `.agents/skills/pr-description`
  remains the single repository skill.
- **Completed the OpenSpec uninstall: removed 30 further skill and command files.** The
  earlier removal covered `.agents/skills/` but missed identical copies under
  `.claude/skills/` + `.claude/commands/opsx/` and `.codex/skills/`. The verification sweep
  that reported them clean was itself at fault: `grep` is a shell function here that skips
  gitignored paths, and `.claude/` and `.codex/` are gitignored while still holding tracked
  files. `git grep`, which ignores `.gitignore` entirely, now reports zero references across
  all tracked files. Prefer `git grep` over `grep -r` when verifying repository-wide removal.
- **Consolidated the Git workflow to one home and renamed the equity strategy reference.**
  The branch-and-ship flow, conventional-commit list and `git mr submit` troubleshooting
  were documented in six places; `CONTRIBUTING.md § Workflow` is now canonical and
  `CLAUDE.md`, `AGENTS.md`, `SETUP_MAC_LINUX.md`, `SETUP_WINDOWS.md` and
  `docs/QUICK_REFERENCE.md` link to it. The two SETUP guides carried a 42-line block that
  was byte-identical apart from the code-fence language, since git commands do not differ
  by OS. Separately, `docs/EQUITY_SWING_STRATEGY.md` is renamed to
  `docs/SP500_QUALITY_MOMENTUM_ROTATION.md` to match its actual subject, and the three
  source files citing it (`lib_data/adjustments.py`,
  `market_data_ingestor/edgar_earnings.py`,
  `lib_infrastructure/tests/test_market_data_providers.py`) now cite real section titles —
  their previous `§4.5`/`§7.2`/`§8.1` pointers referred to a numbering scheme the document
  has not had since the EquitySwingPullback revision was deleted.
- **Uninstalled OpenSpec from the repository and the toolchain.** Deleted `openspec/`
  in full (61 tracked files): the project config, the `lean-strategy-validation-boundary`
  main spec, eight archived changes, and the `harden-cloud-paper-runtime` and
  `add-sp500-quality-momentum-rotation` changes. Deleted the twenty `openspec-*` and
  `source-command-opsx-*` agent skills under `.agents/skills/`, retaining
  `pr-description`. Dropped the now-dead `openspec/` entry from `.dockerignore` and
  uninstalled the global `@fission-ai/openspec` CLI. `CLAUDE.md` and `AGENTS.md` gain a
  `Change Management` subsection recording that this repository uses conventional
  commits and PRs only, with no spec-driven tooling, so the workflow is not
  reintroduced. Earlier changelog entries citing `openspec/changes/...` paths are left
  intact as historical record.
- **Collapsed four conflicting documentation indexes into one canonical table.**
  `README.md § Documentation` is now the single source of truth for what every repository
  document covers, and it lists all thirteen `docs/*.md` files for the first time —
  `RUNBOOK.md`, `BROKER_CREDENTIALS.md` and `REVIEWER_CHECKLIST.md` appeared in no index
  at all, and the four partial tables disagreed with each other on the rest. The duplicate
  tables in `CLAUDE.md`, `AGENTS.md` and `docs/USER_MANUAL.md` are replaced by links to it.
  `CLAUDE.md` and `AGENTS.md` also gain a mutual keep-in-sync directive: the two files were
  99.7% identical (13 differing lines in 4,113) but had silently drifted, with `AGENTS.md`
  carrying two graphify rules `CLAUDE.md` lacked. Those are now reconciled, and
  `diff CLAUDE.md AGENTS.md` is reduced to exactly three hunks, each one a place where a
  file necessarily names its own tool.
- **Snapshot acquisition defers an unsourced pre-membership prefix instead of
  aborting the run.** Every pre-membership warm-up segment still requires a
  dated sourced provider alias, but a prefix that precedes a reviewed
  non-stitch identity edge has none by construction: the successor is a new
  security, so no earlier bar belongs to it. Acquisition now records the
  requested history window on the disposition, leaves the interval `partial`,
  and fetches no `security_history_*` component, matching the
  `security_history_policy.pre_membership_identity` contract already written
  into the snapshot manifest ("not fetched and remains panel-ineligible until
  explicitly supported"). This can only lower a security's panel eligibility —
  it never fabricates history and never admits look-ahead. Previously one such
  security aborted the whole acquisition; against the frozen S&P 500 membership
  set that is 9 of 659 intervals, all genuine entity formations (DowDuPont to
  DuPont, Ingersoll-Rand to Trane, UTX+RTN to RTX, Mylan to Viatris, Discovery
  to WBD, Arconic to Howmet, Bemis to Amcor, Gardner Denver to Ingersoll Rand
  Inc, Paramount to Skydance).

- **Retired SP500QualityMomentumRotation and the layers that served only it.**
  The pre-registered 2020-2025 head-to-head found the strategy failed its KEEP
  bar on both primary conditions (net CAGR 1.71% vs SPY 13.17%; Sharpe 0.225 vs
  0.700), with an excess-return confidence interval excluding zero on the
  negative side and negative annualized alpha in all 44 registered trials
  across fifteen sensitivity axes. Per the repo's no-deprecation-cycle policy
  the strategy core, the SEC factor-panel and trial-ledger validation layers,
  the trial-bound equal-weight benchmark, and the SP500 paper-forward ingestor
  jobs are deleted rather than deprecated — about 35k lines whose only consumer
  was that strategy. The reusable platform is retained and still exercised by
  LiquidityLeadersBasket: point-in-time snapshot acquisition, membership and
  identity resolution, the portfolio simulator with its cost, corporate-action,
  and universe policies, and the official-session calendar. The equity
  validation CLI is now `fetch`, `run`, `compile-official-sessions`, and
  `acquire-membership-correction-evidence`. Verdict and weaknesses are recorded
  in `docs/STRATEGY_READINESS.md`; LiquidityLeadersBasket reproduces its
  2020-2025 result bit-identically after the removal.


- **Separated portfolio factor risk from S&P 500 rotation alpha.** Added a
  provider-neutral, model/version-pinned point-in-time beta/style exposure
  contract backed by the existing immutable equity-observation store, with
  exact dataset, timestamp, revision, content, entitlement, and permanent-ID
  lineage. Deterministic regime-scaled equal-slot beta and six-style caps now
  participate in selection, rebalance identity, panel authority, and signal
  audit metadata. Public-core `1.2.0` activates one definition-hashed internal
  descriptive model for 252-session SPY beta/residual volatility, liquidity,
  momentum, size, value, and issuer-type quality; historical diagnostics with
  incomplete authorized inputs record the cap as inactive, while paper/live
  use fails closed. Migration `0093` registers the separate observation kind
  without creating a parallel store, and the same pure calculator and
  persistence boundary serve historical and prospective panels.

- **Closed S&P 500 rotation trend and zero-exposure state gaps.** The current
  public-core revision is now `1.2.0`: immutable instrument evidence carries
  the already calculated current 252-session total-return trend, requires it
  to remain positive independently of skip-21 momentum, and versions the
  momentum/panel authority contract accordingly. The preregistered 21-session
  cost-aware continuation break-even screen is now explicit in the strategy
  specification. An exact-zero regime target emits no entry/hold targets,
  closes real incumbents, persists an all-cash model state, and cannot grant
  rank-buffer or minimum-hold privilege to a never-owned zero-weight name.
  Strategy tests now cover issuer, sector, industry, hold-band, current-trend,
  cost-screen, and flat-restart behavior.

- **Made official NYSE session evidence deterministically extensible.** The
  existing compiler can now reuse a prior content-addressed compiler output
  without a network request only after re-verifying the outer hash and
  filename, every embedded pinned ICE/NYSE PDF byte/hash and authoritative
  URL, and byte-for-byte reconstruction under the pinned XNYS calendar
  runtime. Extended coverage remains a canonical immutable artifact with the
  original source retrieval lineage; edited, partial, reordered, or
  differently versioned evidence fails closed.

- **Fenced synchronized panel execution by entitlement owner.** Paper panel
  workers now require an explicit `paper_forward` owner, environment, and UTC
  activation watermark; persist that identity in input lineage, runtime state,
  decisions, uniqueness keys, and outbox ordering; never poll historical or
  other-owner revisions; and immutably skip pre-activation or expire missed
  official execution windows without evaluating them. A later knowledge cutoff
  for an already prepared or completed official decision session is now retained
  as a terminal rejected correction before core evaluation or restart replay.

- **Added fail-closed current S&P 500 paper-forward panel registration.** The
  existing profile-gated equity market-data container can now, when explicitly
  enabled with canonical owner/provider and market-policy contracts, register
  the actionable month-end panel from immutable database evidence. It reuses
  the synchronized DB resolver and transactional revision service, enforces
  official-session timing, complete membership/identity/factor dispositions,
  a deterministic selected-evidence knowledge cutoff, DB-only market/SEC
  factor calculation and snapshot persistence, content-addressed replay,
  provider-failure-independent scheduling, separate due-cutoff readiness and
  bounded metrics, and never acquires data or grants strategy/live-order
  authority itself. The frozen v1 configuration now owns the 550-day
  fundamental/share limits and 20-session/63-session/126-day filing-event
  timing used by both historical validation and paper-forward calculation.

- **Completed the exact-owner prospective S&P 500 paper control plane.** The
  same declared equity-ingestor container can now acquire a current actionable
  month-end source graph from EODHD and SEC before invoking the independent
  DB-only registrar. It requires exact current-versus-historical membership
  agreement, permanent active-listing identity, pinned official sessions,
  adjusted market/action history, issuer-level SEC work units, internally
  derived market cap and factor-risk evidence, bounded provider retries, and
  personal-owner lineage; provider calls never hold database transactions,
  pre-listing warm-up exclusions are exactly reconstructable, post-listing gaps
  and same-date corrections fail closed, and no generic price/action mirror or
  live authority is created. Backend strategy, version, account, binding, and
  runtime controls now use the existing tenant/account boundaries through
  migration `0094`. Binding-owned `max_total_exposure_pct` and
  `max_open_positions` flow through scoring and execution so the 30-name model
  cannot be stopped by an unrelated fallback cap; migration `0095` adds the
  validated 50% default while the isolated S&P 500 paper binding explicitly
  owns its reviewed 100% gross target and remains paper-only. Convergent
  migration `0096` also repairs databases carrying the legacy two-column
  model-rebalance prior-leg reference to the exact
  rebalance/leg/instrument/factor identity without weakening fresh schemas.

- **Corrected EODHD historical volume and split-coordinate semantics.** Daily
  artifacts now retain EODHD `volume` as provider split-adjusted shares rather
  than relabelling it as raw tape volume. Liquidity and modeled daily-bar costs
  transform raw OHLC into the same split basis, fetch and hash split tails
  through a pinned retrieval date, apply one explicit reported-share rounding
  haircut, and reject mismatched factors. Complete deterministic reconstruction
  may support diagnostic research, while missing vendor adjustment-vintage,
  horizon, or rounding authority still blocks confirmatory/headline claims.

- **Hardened S&P 500 validation accounting and publication evidence.** The
  equity simulator now keeps primary terminal holdings marked rather than
  inventing an end-window sale, publishes a separately labelled non-actionable
  liquidation-cost sensitivity, persists daily gross/net exposure, and freezes
  two-way turnover as executed buy-plus-sell notional over average daily net
  NAV. Raw-price validation applies source-lineaged splits and records USD
  dividends as ex-date NAV receivables that cannot fund trades when the provider
  has no authoritative payment date; missing/non-USD currency fails closed.
  Strict JSON reports normalize non-finite metrics and must be written before a
  successful trial result can persist. Executable baseline trials now add a
  separately labelled, non-official point-in-time S&P 500 equal-weight
  reconstruction using the same raw corporate actions, institutional costs,
  terminal treatment, and immutable snapshot lineage, with explicit net-return
  dispositions against both 2× SPY and 2× reconstructed equal weight.
  Preregistered diagnostic portfolio trials may now reuse the exact
  scope-admission policy already enforced for factor materialization, while
  unregistered and confirmatory runs remain global-completeness gated.
  Factor-panel claim scope, diagnostic
  campaign admission, filing-drift timing, regime-volatility naming, and
  independent daily-candle versus delayed-quote polling were also made explicit
  and replay-safe.

- **Contained EODHD daily-quota exhaustion in the existing equity ingestor.**
  Classified credential-safe EODHD failures so HTTP 402 opens a bounded
  scheduler circuit through the provider's midnight-UTC reset (and a validated
  minimum cooldown), while HTTP 401 authentication and HTTP 403 entitlement
  failures remain distinct. The circuit suppresses quota-burning poll retries,
  marks readiness false, and publishes bounded-cardinality metrics without
  changing the fail-closed historical CLI or any execution authority.

- **Adopted graphify as a supported repository-discovery aid, with measured usage
  rules.** Replaced the "graphify (optional)" note in `CLAUDE.md`/`AGENTS.md` with
  guidance benchmarked against this repository's own graph rather than the tool's
  generic advice: `god-nodes`/`explain`/`affected` for orientation, symbol detail
  and blast radius; exact symbol names rather than prose (~26x hit rate at equal
  cost); never raising `--budget` to chase a missing answer, because ranking is
  stable and an untruncated result emits every edge unbudgeted (a 10k budget
  returned 32k tokens); and treating `graphify path` as unreliable for
  architecture claims — it defaults to directed and returns nothing on this
  undirected graph without `--undirected`. `[EXTRACTED]` edges are AST facts while
  `[INFERRED]` edges citing a `.md` are documentation claims. `graphify update .`
  is a code-only refresh: it re-extracts markdown structurally and replaces a
  doc's concept nodes with bare headings, so doc edits need a full `/graphify`
  re-extraction (`graphify check-update .` reports when one is pending).
  `graphify-out/` stays derived and gitignored, so a fresh clone or CI checkout
  has no graph until it is rebuilt. Agent wiring (`/graphify` skill,
  `.claude/settings.json` PreToolUse hook, `.codex/hooks.json`, and the
  post-commit/post-checkout rebuild hooks) is local and untracked; no build,
  deploy, or runtime surface changed.

- **Disabled S&P 500 quality-momentum rotation and synchronized portfolio
  contracts.** Added the fixed-weight, long-only
  `SP500QualityMomentumRotation` v1 core (40% intermediate momentum, 25%
  quality, 20% filing/growth/event, 15% valuation; typed, pluggable analyst/
  news/call/insider/crowding/macro sleeves disabled at zero for v1), point-in-time provider authority and
  complete-panel validation, immutable observation/factor/rank evidence, and
  migrations `0086`–`0091`; the account execution fence is isolated in the
  forward-only `0089` revision so the already-deployed `0088` contract remains
  immutable. The indicator runtime now atomically journals a
  registered completed panel, rank, state, audit, stable signals, and
  `signals.rebalances.submit`; scoring re-hashes that lineage and creates
  tenant/user/binding/account-scoped `paper_forward` plans with
  `execution.rebalance.commands`; execution generation-fences the durable plan
  and confirms exits/reductions before entries. Position caps value the whole
  post-order broker symbol exposure while aggregate exposure adds only the
  proposed delta; exact rejected incumbent-HOLD dispositions remain frozen
  coverage rather than disguising new exposure. Terminal failed plans now keep
  readiness red until an authenticated operator appends an immutable,
  idempotent acknowledgement/reconciliation/remediation record; later evidence
  extends that lineage without rewriting the plan. Missing/stale/corrected,
  entitlement-incompatible, or incomplete evidence fails closed, and
  historical-validation/live-forward batches cannot create paper plans. The
  strategy remains `enabled=false`, dev-only, uses
  `us_equity_live_unconfigured`, and has no paper/live authority or claimed
  performance/E2E result. The existing equity ingestor can now persist exact
  personal-owner EODHD extended delayed last-trade/BBO evidence for configured
  equities; ingestion binds the vendor response/source digest to a normalized
  owner-scoped delayed-BBO contract, while execution imports only the generic
  equity-lineage reader for that same owner in paper mode, applies a separate
  delayed-data freshness limit, never receives the vendor token, and never uses
  it as live authority. The validation-only EODHD path now uses the licensed,
  documented `HistoricalTickerComponents` feed as interval authority with inclusive
  starts and exclusive removals, uses the earliest v1.1 full-state component
  checkpoint only to prove open-left incumbents, and records every checkpoint
  presence/name disagreement in a typed content-addressed cross-check without
  silently creating, extending, or relabelling membership,
  installs the existing market-data application and indicator strategy wheel into
  the dedicated validation and market-data environments so the production factor
  implementation is import-verified rather than sourced through a developer path,
  retains active/delisted directories and every exact ID-mapping response, and
  materializes permanent-identity membership plus dated aliases without a
  manually reconstructed CSV. Source-backed symbol changes remain primary;
  otherwise an exact component code is retained only under an exact normalized
  issuer-name match, while a uniquely name-matched EODHD storage symbol (such
  as a delisted `_OLD` code) safely supersedes a reused active ticker. Ambiguous
  aliases, directory/mapping identity conflicts, and other malformed,
  contradictory, or unresolved identity evidence fail closed, and every source
  byte is pinned in the snapshot manifest without credentials. Each resolved
  provider symbol now also contributes one exact v1.1 Fundamentals `General`
  response to the content-addressed evidence set and membership revision.
  Nullable fields remain unavailable evidence, while malformed values, a
  mismatched `Code`, contradictory ISIN/CIK, an unbound IPO date, or any
  reconstructed interval beginning before a bound legal security's `IPODate`
  fail closed without truncation or silent exclusion. Typed membership listing
  evidence carries the vendor-reported date, normalized identity fields, and
  raw artifact identity into the interval digest for downstream exact-byte and
  official-session reconciliation; it is never official listing proof or an
  alpha input. Membership authority remains incomplete when a snapshot-only
  legal-history or identity claim lacks a permanent-ID, dated symbol-change, or
  corporate-action bridge; that state is propagated separately from permanent
  identity completeness and blocks confirmatory factor panels and performance
  claims. One-day constituent rows are retained for exactly one date under the
  half-open boundary, while an inclusive CSV end is intersected downstream
  with pinned official sessions. A two-stage reviewed-correction boundary
  acquires canonical HTTPS primary-source specifications into exact raw
  content-addressed artifacts and a frozen manifest; materialization accepts
  only that verified manifest, rechecks exact ticker-row preconditions, and
  binds every source hash into authority identity. One raw row may be replaced
  by zero to many explicit half-open intervals. Separately validity-dated
  identity-only edges now split an exact backcast raw row without changing
  membership coverage, require class-qualified endpoint identities, reject
  provider-symbol collisions, and permit aliases only for identical-security
  renames; successor/class edges never stitch prices. Missing historical
  publication timestamps remain explicit and keep EODHD membership
  offline-research-only for cutoff-safe promotion. No symbol exception is
  hardcoded. The existing market-data-ingestor image now owns a transactional,
  replay-safe import boundary for a future content-addressed, provider-neutral
  symbolic evidence bundle. It derives local DB identities rather than copying
  research instrument IDs or hashes, preserves ticker renames through
  validity-dated security symbols in forward-only migration `0091`, and fails
  before writes when membership publication availability or exact structural
  breadth evidence is absent; the current EODHD reconstruction therefore
  remains non-promotable. The factor materializer now emits the deterministic
  symbolic `database_evidence_bundle` itself, including explicit ineligible
  factor snapshots for every non-rankable effective member, a benchmark
  identity, mixed public/private lineage, and honest null membership
  availability; it does not fabricate a promotable artifact. Provider-authority
  v3 binds personal ownership per
  provider rule, so personal EODHD evidence can coexist with unowned public SEC
  and official-calendar evidence without mislabelling or cross-user access. This
  personal-research authority is not forward/live or multi-tenant authority;
  official sessions, observed
  costs, the registered confirmatory gates, and Docker paper E2E remain
  required. Historical
  headline trial prepare, running, and success transitions now reverify
  immutable inputs and reject self-asserted factor eligibility or lineage.
  The verifier-only boundary now authenticates exact-byte reconstruction with
  an Ed25519 approval under an out-of-band-fingerprint-pinned trust policy that
  allowlists the authority, reviewers, and reconstructor source identity.
  Observed quote/fill costs and honestly labelled daily-bar modeled costs have
  separate strict lineage contracts. Trial transitions
  reload the exact frozen campaign/specification/trial-ID association, blocking
  caller-side headline-ID substitution. No qualifying independent
  reconstruction, trusted external approval, or eligible historical
  transaction-cost dataset has been supplied. Regime breadth now retains every
  price-complete point-in-time member even when SEC/fundamental evidence makes
  that member rank-ineligible, missing member price history fails closed with
  an explicit disposition, and filing-event drift uses split/dividend-aware
  total-return prices. Selected price revisions now reject duplicate authority
  and validate correction chains/forks before their hashes can satisfy factor
  lineage. Configuration, reweight/ablation, universe, rebalance-timing,
  regime, and missing-data variants now use a source-hash-pinned deterministic
  harness; implementation or input drift rejects before entering RUNNING.
  The optional sleeves share a typed provider-neutral raw-observation,
  timestamp, correction, activation, value-schema, owner-bound entitlement,
  and complete model-transform identity contract. Forward-only `0090` adds
  their observation kinds and binds the frozen registry digest into factor and
  panel replay identity without modifying deployed `0086`/`0087` semantics;
  no provider, MCP, or AI runtime is enabled by that plug-in boundary.
  Paper drawdown admission
  accepts only fresh exact-account broker equity with durable metric provenance
  or configured `paper_initial_equity`; live, cached, unattributed, mismatched,
  or history-free baselines remain blocked. Ordinary, manual,
  historical-replay, rebalance, paper lifecycle, and reconciliation writers
  now share a crash-released user/account PostgreSQL advisory-lock generation.
  Rebalance targets freeze once; completion blocks on recoverable orders and
  seals an exact all-symbol FIFO reconciliation digest plus its account
  generation in the terminal transaction. The development test profile now
  declares the same mandatory JSON-schema validator as the indicator-runner
  runtime, preventing host-suite dependency drift. Migrated PostgreSQL and
  Docker paper E2E evidence remain promotion blockers.
  Portfolio hierarchy thresholds fail closed for target exposure until
  cutoff-bound sector/market snapshots can be sealed into plan lineage; exact
  asset scoring and independently authorized reductions remain available.
- **Event-loop offload for sync DB hot paths (owner amendment #6).** The
  scoring ingest endpoint now runs its ENTIRE unit of work (signal + scores +
  decision log + execution.commands outbox, one transaction) as a single
  callable on an `asyncio.to_thread` worker thread that owns the
  `_ACTIVE_SESSION` ContextVar session end-to-end; provider lookups are
  pre-resolved on the event loop via the dispatcher's new two-phase API
  (`resolve_provider_contexts` + sync `dispatch_resolved`; async `dispatch`
  is now a thin composition of the two). The scoring outbox relay offloads
  its self-contained `claim_outbox_batch`/`mark_outbox_failed`/
  `mark_outbox_published` store calls per call (each opens, commits, and
  closes its own session; the relay never runs inside a unit of work). The
  execution engine's dedup claim runs on a worker thread, with a new
  deduplicator memory lock replacing the within-process claim atomicity the
  event loop used to provide. Per-store-call offload inside the ingest unit
  of work remains forbidden (it would share one Session across threads).
  `BARE_EXCEPT_BASELINE` 42 → 43 for the dispatcher's in-worker provider
  boundary catch.
- **Phase 2 architecture & modernization (owner-approved plan).** Ruff gains
  the ASYNC and G rulesets (47 f-string logging sites → lazy args; the one
  justified shutdown drain poll documented); pre-3.11 `.replace("Z", ...)`
  dropped in 20 files; outbox relay waiters use `create_task` and are awaited
  on cancel; lib clock reads route through `now_utc`; `HttpSignalEmitter`
  pools one HTTP client; lib_common enums are `StrEnum`. The three legacy
  Query-API lib files (outbox, secrets, execution repo) are SQLAlchemy-2.0
  `select()`/fenced `update()` style, pinned by the outbox atomicity harness.
  PaperBroker's per-asset-class fill models moved to
  `paper_futures.py`/`paper_options.py` (stateless simulators over the single
  broker ledger; facade at 1,208 LOC). `_ResolvedExecutionFlow` consumes a
  typed `ExecutionServices` snapshot instead of 19 private engine reaches,
  and the engine's six `Any` constructor params are structural Protocols.
  ScoreEngine delegates binding evaluation/mode selection to the extracted
  `BindingEvaluator` (engine at 789 LOC) and `ingest_signal` reads as three
  named stages in one unit of work. SignalWorker is durable-only (nine
  test-only non-durable forks deleted) and reads prices through new
  session-aware `PriceIngestionService` methods (caller's transaction owns
  the revision fence) instead of per-fetch table reflection. The unwired
  `EquityEODBackfiller` folded into `HistoricalBackfiller` as the
  scheduled-daily policy (calendar-aware coverage, single-request daily
  fetch, structurally derived corporate-action loading) behind the one
  existing `backfill` command. `_template` ships a reviewed `core.py` +
  tests scaffold. The infra go-live position gate now derives per-strategy
  attribution from execution fill lineage (rebuildable temp projection) so
  swing strategies can hold non-live positions through deploys.
- **Phase 1 cleanup (owner-approved plan, 2026-07-29/30).** Deleted all
  verified dead code: `lib_indicators` RSI module + phantom `__all__` exports
  (ATR/SMA/Vortex retained with a documented rationale); the unwired
  `lib_common` profile/broker validation chain, hashing helpers,
  `app/protocols.py`, `parse_enum_env`, `parse_strategy_filter`, duplicate
  `HealthStatus` enum + `APIHealthStatus` alias; `lib_strategy`
  `format_signal_key`/`is_known_scoring_action`; `lib_application`
  `use_cases` shell + `PriceFrameProvider`; the scoring engine's unreachable
  canonical-signal mirror path (`_write_canonical_signal` + the entire
  `AliasResolver` threading) and `ScoringPipeline.process_batch`; the
  execution engine's `types.py` shim, five capability helpers, and
  `get_broker_factory`; the ingestor `api` subcommand; dev_cli dead helpers;
  the LEAN-era `algorithm-type-name` config field and schema surface.
- Dependency truth: every setup.py now declares its true direct imports
  (lib-common/sqlalchemy edges added; never-imported numpy/pandas/httpx/
  pyyaml/toml declarations removed); `config/dependencies.yaml` deleted —
  version authority is setup.py ranges + `docker/constraints.txt`, and the
  audit's interpreter gate reads `build.yaml global.python_version`. New
  `first-party-dependency-contract` audit rule keeps setup.py and build.yaml
  edges covering every direct `lib_*` import. Bare-except baseline 43 → 42.
- Duplication consolidations: single `EvaluationHorizon.duration` map
  (unknown horizons now fail closed, no silent 1-day default); single
  `StrategyRuntimeParams` resolution for indicator runtime defaults; shared
  `OutboxBacklogSnapshot` readiness predicate; shared
  `supervise_background_task` liveness contract; backend admin auth via the
  shared constant-time comparator with `BACKEND_ALLOW_ANON` refused in
  production; `vmdev db start/stop/status` delegate to the manage_db scripts;
  app requirements single-sourced (indicator profile moved to
  `docker/requirements-indicator-runner.txt`); dev compose postgres now
  `extends` the stack definition.
- Fail-closed hardening: unsupported execution modes produce structured
  blocked results; strategy-config schema validation is mandatory; credential
  resolution is strictly scoped to the routed `broker_account_id` (fixes the
  two-accounts-at-one-broker route blockage). `SwingHighLowPMO` is recorded
  as the e2e pipeline canary only, excluded from paper promotion.
- Table-drop step 1 (expand/contract): removed all seed references to the 13
  dropping tables (option templates/legs, feature flags incl. per-user
  seeds); models/schema stay until the post-soak drop release — see
  docs/DATABASE.md "Table lifecycle". `index_membership` is retained for the
  SP500 rotation change.
- Restored the orphaned `libs/python/lib_application/tests` and
  `libs/python/lib_infrastructure/tests` suites to
  `[tool.pytest.ini_options] testpaths` (132 tests — RLS tenant scoping,
  encrypted secrets, market-calendar/version-retirement fail-closed,
  ECB FX parsing, Coinbase order sizing). They had been silently excluded
  from CI and `vmdev test all` since `a2d195da` removed the then-empty
  path (2026-06-25); the suites landed afterwards without re-listing it.
  All 132 pass unchanged. Added `tests/test_testpaths_contract.py`, a
  repo-contract guard asserting every tracked `test_*.py` lives under a
  configured testpaths entry so a suite can never go dark this way again.
- Deleted the `MomentumRotation` benchmark fixture (owner-directed
  2026-07-27), completing the equity research cleanup: its monthly top-N
  12-1 selection layer showed no consistent edge over its own top-ADV
  pool, and that pool became `LiquidityLeadersBasket`. Strategy dir,
  pytest testpath, and the runtime inventory in `CLAUDE.md`/`AGENTS.md`
  are removed; the research narrative and evidence remain in the basket
  doc and git history. At that cleanup boundary,
  `strategies/indicator/` shipped exactly two configs: `SwingHighLowPMO` and
  `LiquidityLeadersBasket`.
- Deleted the retired `EmaCrossScalper` and `RsiBounceScalper` composite
  witnesses and the retired `EquitySwingPullback` research strategy (code,
  tests, configs, seeds, docs; owner-directed 2026-07-27), and removed
  their catalogue rows from the platform seeds, the production seed
  guard, and the infra `go-live-config.sql` (the scalpers join the
  retired-strategy deactivation list there). The PostgreSQL pipeline gate
  and the durable-runtime suite now run on a test-owned deterministic
  exerciser (`tests/fixtures/strategies/PipelineExerciser`) that replays
  a pre-registered LONG/CLOSE schedule against the same frozen real
  Coinbase fixture with the same emit contract, so every pipeline
  assertion keeps its meaning without shipping non-strategies in the
  indicator image. The equity backtest harness and its tests now drive
  the `LiquidityLeadersBasket` core (the forced universe-rotation close
  assertion got strictly stronger), and unit-test fixture ids renamed to
  neutral `test_strategy_*` names. `MomentumRotation` (created 2026-07-26
  at the owner's request as the top-N rotation experiment) is retained
  pending an owner decision.
- Recorded the first long-window performance evidence for
  `SwingHighLowPMO` (3 years of real Coinbase 15m candles, 2023-08 →
  2026-07, next-open fills, bracket exits): **no edge** — gross of all
  costs the long-only core returned +4.6% (BTC), +8.5% (ETH), +4.6%
  (SOL) over the whole window, and at the campaign's own 10 bps/side
  flat fee every symbol lost 38-41% with all years negative
  (artifacts: `reports/swing_crypto_backtest_20260727/`, gitignored).
  The config's `decision` reverts to `BENCHMARK_OR_COMPONENT_ONLY` with
  the evidence recorded in its role; any live promotion now requires an
  explicit owner decision against this result plus the formal
  validation campaign.
- Scoring can now serve mixed feeds in one deployment: the market-context
  provider routes per asset class
  (`SCORING_MARKET_CONTEXT_BY_ASSET_CLASS` JSON env; e.g. equity →
  `eodhd`/1d with a weekend-spanning max age while the default keeps
  serving crypto `coinbase_live`/1m). Every per-class entry must state its
  full source/cadence contract; unknown classes, partial entries, or an
  unresolvable instrument fail closed exactly like the single-feed path.
  This removes the verified July-26 blocker where equity signals could
  never become actionable next to the crypto feed.
- The market-data ingestor accepts `INGESTOR_SOURCE=eodhd` as a scheduled
  vendor source: equity selectors resolve against the instruments
  catalogue (no broker product mapping — EODHD US tickers are the
  canonical symbols; unknown or non-equity selectors fail startup),
  credentials come from `EODHD_API_TOKEN`, and the stack gains a
  profile-gated `market-data-ingestor-equity` instance (`--profile
  equity`) polling EODHD daily bars beside the crypto feed with a
  cadence-aware staleness threshold.
- Expanded the `SwingHighLowPMO` canary to the owner's top-3 crypto
  universe (`BTCUSDC,ETHUSDC,SOLUSDC`) as catalogue version `1.1.0`
  (`1.0.1` deprecated in place — version parameter snapshots are
  immutable), with the dev-seed binding widened to the three pairs
  (`max_open_positions` 3) and the e2e guide/scripts docs updated to the
  new canary contract. The recorded BTC-only replay witness remains a
  `1.0.1` artifact. Owner decision recorded: SwingHighLowPMO (long-only,
  Coinbase) and LiquidityLeadersBasket (EODHD backtest, IBKR live
  target) are the two production strategies.
- Repaired the long-red `python-ci` type-check step (predates the equity
  merge; red since at least `82eec50f`): `lib_common.metrics` now assigns
  its prometheus fallbacks through Any-typed module aliases so mypy passes
  both with prometheus-client installed (CI step env) and absent
  (pre-commit hook env), and the CI step installs `types-PyYAML` (pinned
  in `docker/constraints.txt`) for the `yaml` stubs. Runtime behavior is
  unchanged in both branches. The `vmdev strategy attest-*` commands now
  print attestation paths and digests with `soft_wrap=True` so narrow
  terminals (and the CI test runner at 80 columns) cannot split a path or
  sha256 mid-token.
- Completed the equity-swing runtime dependency closure: `lib_data`'s
  `exchange_calendars` requirement is now pinned in
  `docker/requirements-svc-base.txt` and `docker/constraints.txt`
  (`exchange_calendars==4.13.2` plus its `korean_lunar_calendar`,
  `pyluach`, `toolz`, and previously-unpinned `tzdata` transitives), so
  every service image and the CI test runtime satisfy the wheel metadata
  introduced by the equity session-calendar work.
- Selected `LiquidityLeadersBasket` as the forward equity candidate
  (the former equity-basket design document, not retained in this snapshot):
  equal-weight basket of the PIT top-25 dollar-ADV S&P 500 names,
  quarterly rotation only, near-zero parameters. Traded through the
  harness with costs it returned +521% (Sharpe 1.33) out-of-window
  2012-2019 and +441% (Sharpe 1.01) 2020-2026 vs S&P TR +199%/+151%,
  cost-insensitive — with the single-secular-regime risk and the
  2000-2010 failure-mode analog recorded as owner-accepted. Includes a
  bootstrap-phantom-position regression fix + test in the core.
  `EquitySwingPullback` and `MomentumRotation` demote to
  `BENCHMARK_OR_COMPONENT_ONLY` comparison fixtures (dip: edge validated
  but too small; rotation: no consistent selection edge over its own
  pool). Full survivorship-complete membership drives all universe
  construction.
- Added the owner-directed `MomentumRotation` candidate
  (`strategies/indicator/MomentumRotation/`): monthly top-N 12-1 momentum
  rotation over the highest-ADV S&P names with a rank-buffer exit and
  absolute-momentum cash fallback — the strategy family the Phase 1
  evidence favors for liquid mega-caps. Dev-only, pre-registered, 7
  rule-level tests. First run on the existing snapshot: +331% vs S&P TR
  +151% (Sharpe 0.85, beta 1.12, 30 trades, cost-insensitive) — reported
  WITH the candidate-pool selection-bias caveat: the number is upward-
  biased until re-run on a full survivorship-complete S&P constituent
  set, which is the required next step before any promotion decision.
- Ran the first pre-registered EquitySwingPullback backtest (2020-01-02→
  2026-07-24, real EODHD + EDGAR data) and the §10 validation suite
  (`equity_validation.py`: 12-variant ablation/sensitivity ledger, block
  bootstrap, deflated Sharpe, regime breakdown, capacity). Verdict recorded
  in §13 of the historical `docs/EQUITY_SWING_STRATEGY.md` revision: ITERATE —
  the residualized dip is
  validated (absolute-dip ablation collapses), the parameter surface is
  flat and the edge survives 2× costs, but the regime gate and earnings
  blackout were net drags in-window and absolute returns are far below
  buy-and-hold. The strategy core gained four pre-registered ablation-only
  flags defaulting to the locked rules.
- Added the N-symbol portfolio backtest layer for `EquitySwingPullback`
  (`tools/dev_cli/dev_cli/validation/backtest/equity_portfolio.py`): drives
  the production core over a point-in-time quarterly top-ADV universe with
  a shared cash account, next-open fills, the dated US cost model
  (VIX-conditioned stress multiplier + dated SEC Section 31 sell fee),
  per-symbol history bootstraps at rebalances, forced closes for universe
  rotation/delistings/end-of-window, and the §10 report metrics including
  monthly/quarterly/yearly return tables and benchmark alpha/beta. The
  strategy core gained per-symbol benchmark ring buffers plus a monotonic
  session guard on the shared trend gate so mid-run symbol bootstraps
  cannot corrupt regime state.
- Implemented the `EquitySwingPullback` strategy core
  (`strategies/indicator/EquitySwingPullback/`): the locked §4 rules of the
  historical revision formerly at `docs/EQUITY_SWING_STRATEGY.md` as a
  `PureSignalStrategy` — residualized 5-day dip z-score inside
  12-1-momentum/SMA200-qualified names, benchmark trend gate with 3-day
  confirmation + VIX-percentile kill-switch (fed via a documented
  four-key bar-metadata contract, fail-closed when keys are absent),
  earnings blackout/guard, and the full exit-precedence chain. Shared
  regime primitives (`TrendConfirmGate`, `RollingZScore`,
  `ExpandingPercentileRank`) live in `lib_indicators.regime_gates`.
  Dev-only (`environments: ["dev"]`); 12 rule-level tests including an
  end-to-end scale-invariance check. Phase 3 data plane landed alongside:
  equity reference tables + migration 0052, XNYS calendar wrapper, EODHD
  daily-bar/corporate-action client, calendar-aware equity backfiller,
  CRSP adjustment builder, EDGAR 8-K earnings loader, and the PIT S&P 500
  membership dataset (`config/universe/`).
- Added the pre-registered design spec for `EquitySwingPullback` (the historical
  revision formerly at `docs/EQUITY_SWING_STRATEGY.md`): a US
  large-cap long-only equity swing strategy — residualized pullback entries
  inside 12-1-momentum/SMA200-qualified S&P 500 names, index-trend regime
  gate + VIX kill-switch, earnings-window blackout, sell-into-strength/
  time-stop exits, inverse-ATR sizing, PIT universe from hand-audited index
  membership, a dated US cost model, and a pre-registered backtest protocol
  (2020→present, anchored OOS folds, ablations, deflated Sharpe/PBO).
  Parameters are locked ahead of the first backtest run. Scope amendment A7:
  v1 trades the US only; Europe/India research is retained as Appendix A and
  any future variant requires its own design amendment and validation.
- Added migrations `0075`–`0083` and the corresponding production paths for
  unattended paper trading. Indicator workers now atomically persist a
  versioned shared-model snapshot, source watermark, append-only bar decision,
  and stable signal envelope before a durable relay posts to scoring. The Swing,
  EMA, and RSI cores implement the same state restoration contract, including
  exact raw-candle provenance through consolidated bars.
- Ratcheted the application broad-exception audit baseline from 44 to 43 by
  consolidating reconciliation broker lookups behind one API boundary and
  making unexpected paper-lifecycle task death process-fatal.
- Added an explicit configurable 60-second Compose stop grace to supervised
  application services. This aligns Docker termination with the internal
  request, broker, database, lifecycle-worker, and strategy-child drain windows
  instead of allowing Docker's 10-second default to SIGKILL an otherwise
  progressing shutdown. The shared coordinator now preserves its 30-second
  pending-operation drain inside one monotonic 45-second total cleanup budget,
  gives each async handler only the remaining time, and records timed-out,
  cancelled, or skipped handlers instead of silently abandoning cleanup.
- Replaced process-memory local-paper protection with a durable,
  committed-real-candle lifecycle. Resting stop/limit orders retain stable
  client identity, source watermark, reduce-only/parent/OCO state and cumulative
  fills; conservative versioned OHLC/gap rules project one exact fill through
  canonical cash, position, P&L/NAV, event, and reconciliation state across
  duplicate delivery and restart. Each delayed partial or terminal fill now
  checkpoints its exact canonical execution until the idempotent
  `execution_metrics`, position, and NAV projections complete; restart drains
  that checkpoint before consuming another candle, and realized-P&L metadata
  attributes only the current exit execution.
- Hardened execution authority and recovery: entry and reduce-only exit
  authority are explicit; active binding values and the interim
  one-strategy-per-account/instrument rule are database-enforced; current user,
  binding, account, credential, environment, and route are revalidated before
  broker I/O. Ambiguous transport outcomes persist as `submission_unknown` for
  client-ID reconciliation, startup discovers reconciliation partitions from
  durable state, and execution-command dead letters have authenticated,
  generation-fenced, audited redrive without changing economic identity.
  Historical local-paper replay now requires the exact executable v1 decision
  and published command for the requested account, reuses its persisted
  economics/route, records the exact next-bar source timestamp/revision on the
  entry fill, and cannot weaken normal stale-signal rejection. Recovered
  protective fills also rebuild an explicit broker/account profile before
  refreshing position and NAV projections.
- Made feedback attribution and operations fail closed. Signal/horizon
  evaluation and scoped mode-performance writes use database-native uniqueness,
  while decisions carry exact canonical-signal/account lineage through order
  and fill attribution. Evaluation cycles are serialized per horizon,
  deterministic tracker progression rejects duplicate or late older work, and
  strategy retirement cannot overtake suggestion persistence. Every PostgreSQL
  engine uses the same bounded pool budget and pool-pressure metrics; scoring
  outbox age, indicator journal
  backlog/strategy lag, local-paper order progress, unknown submissions, and
  initial reconciliation now participate in readiness. The tracked
  DigitalOcean 8-GB topology has validated resource totals and bounded logs.
  PostgreSQL feedback-concurrency fixtures use unique strategy, instrument,
  signal, and run namespaces so parallel/repeated acceptance cannot collide
  with or delete seeded catalogue rows.
- Added an evidence-hashed, exact-scope paper promotion gate for
  SwingHighLowPMO `1.0.1` on real Coinbase BTC-USDC data and one dedicated
  local-paper account. Manifest schema v2 derives its passing status only after
  all nine versioned evidence documents share the exact run/scope and satisfy
  their semantic outcome contracts, including the pinned reviewed historical
  signal/price/trigger witness; indicator and scoring revalidate both content
  and hashes at startup. It cannot grant live authority, and production remains
  fail closed with no selected strategy or active binding until the real-data
  Docker/restart/soak evidence is complete. Broker source-IP/gateway requirements
  and the self-hosted sub-$100 deployment configuration were synchronized in the
  existing runbooks and infra validator.
- Added ownership labels and fail-closed local retention to the canonical
  `vmdev build docker --from-config` fleet build: only untagged images from the
  configured platform repositories with no running or stopped container
  references are eligible, while tagged rollback images and daemon-wide Docker
  state remain untouched. Repository guidance now also requires explicit
  approval, purpose, and lifetime before any ad-hoc Docker artifact is created.
- Kept the local backend's tenant configuration reads available with the
  read-only environment secrets provider while making credential onboarding and
  rotation return an actionable 503 before any database mutation. Local
  execution retains the account-scoped DB secret store, and a Compose contract
  test prevents the two service defaults from drifting.
- Corrected the user manual and repository guidance to describe the actual
  three-stage scoring pipeline, its binding/execution ownership boundary, the
  current source paths, and the real migration-before-seed dependency.
- Made canonical order persistence fail closed on route/intent broker
  disagreement and broker-fill side or routed-symbol disagreement. Local paper
  fills now enter the exact canonical ledger before process-local cash or
  positions mutate, making the submit-to-persistence crash window recoverable;
  pre-submit persistence failures retain but terminally reject their OMS audit
  rows instead of leaving false routed orders.
- Added account-scoped canonical order idempotency at the dispatch-to-OMS
  boundary. Each concrete order leg derives a stable identity from the
  execution decision, retries reuse and validate the original economics, and a
  durable fill is returned without a second broker submission. Restart
  reconciliation now treats the exact canonical paper ledger as authoritative,
  closes its pending projection as filled, and preserves the valuation fields
  required to rebuild the account book. Fill quantity, price, and signed fees
  are canonicalized to PostgreSQL `NUMERIC(20,8)` precision before both insert
  and replay comparison, preventing database rounding from masquerading as an
  economic mutation while retaining strict one-ledger-quantum immutability.
- Hardened migration rollback boundaries: fail-closed binding defaults remain
  inactive/manual, and downgrades now refuse to discard explicit paper capital,
  authoritative market sessions, typed broker identities, or canonical
  ETF/cash-index semantics. Safe market-session rollback also revokes the
  backend instrument-update grant introduced by that revision.
- Made the default `vmdev audit` include tracked and non-ignored untracked
  worktree files while preserving index-only `--staged` behavior, so new
  production modules cannot bypass architecture gates before their first
  `git add`. Team-scoped platform builds now include scoring, feedback, and
  market-data services, and the E2E guide asserts the actual Alembic head.
- Made feedback horizon provenance mandatory at both suggestion and tracker
  write boundaries. Migration 0072 now derives every existing suggestion from
  exactly one identity-matched tracker or aborts, and removes fallback
  defaults. Removed the obsolete feedback strategy mount, stale framework and
  dormant-library setup references, and local-stack commands that bypassed the
  required `.env` file.
- Made feedback suggestion generation recoverable under the least-privilege
  runtime role. Exact active strategy/version validation now uses a narrowly
  granted, fixed-search-path lock function installed by the linear 0073
  catalogue-gate migration; consecutive-wrong tracking and
  pending suggestions are horizon-scoped; orphaned reached trackers reconcile
  idempotently after a crash with exact version-checked links; per-signal
  suggestion failures aggregate into the run error count, degrade the durable
  heartbeat without being erased by a later no-work horizon, and make one-shot
  evaluation exit non-zero. The retained public-data strategy gate exercises
  both the threshold and recovery paths as `vm_feedback_login`.
  EmaCrossScalper and RsiBounceScalper now publish source-parity-checked,
  immutable `1.0.3` parameter snapshots rather than rewriting the historical
  `1.0.2` version.
- Grounded `mode_performance` in exact FIFO round-trip economics. Execution
  metrics carry immutable entry-fill → exit-fill → canonical-signal
  contributions with fee- and observed-FX-adjusted net P&L and entry capital;
  the feedback writer attributes each return to the opening signal's evaluated
  horizon and fails closed on malformed lineage. The real Coinbase-history
  PostgreSQL gate now proves two account-scoped mode rows through the full
  strategy → scoring → paper fill → P&L → feedback flow.
- Preserve the resolved broker account and instrument settlement currency on
  post-resolution no-order results, including policy and risk blocks, so
  account-scoped execution metrics remain complete without weakening the
  pre-trade guard. Close/flatten intents now preserve their observed reference
  price as well, giving exact fills the same slippage provenance as entries.
- Split execution dispatch and broker submission into small staged collaborators,
  eliminating the engine's complexity suppressions while preserving its public
  behavior, deduplication, circuit-breaker attribution, and paper/live safety
  gates. Notify-only decisions retain their configured route for audit without
  requiring an order-capable or exact-fill-certified venue, and malformed
  account IDs or paper price hints now fail closed at typed boundaries.
- Replaced cumulative order-status fill synthesis with a provider-neutral exact
  trade boundary. Canonical executions now require a stable venue trade ID,
  actual timezone-aware fill timestamp, exact quantity/price, fee amount, and
  fee currency; signed maker rebates are preserved. Complete order-scoped fill
  sets are checked against broker status and replay idempotently. Coinbase and
  Deribit implement strict trade retrieval, while IBKR, Saxo, Zerodha, and
  Delta fail certification explicitly where their current official integration
  cannot supply or safely scope the complete contract. Only Coinbase has the
  broker-specific live-certification workflow; Deribit remains live-blocked
  pending its own authenticated evidence.
  Application and decimal-preserving wire DTOs remain intentional layer types
  with parity-tested bridge conversions and one `supports_perpetual` name.
- Established one canonical persisted asset taxonomy (`crypto`, `equity`,
  `etf`, `index`, `futures`, `options`, `fx`, `commodities`) across strategy
  config, events, scoring, execution, backtests, broker capabilities, and
  database constraints. Legacy lexical aliases normalize only at write/input
  boundaries. ETFs remain distinct from equities, while cash indices are
  explicit reference-only instruments (`is_tradable=false`) and fail closed
  before any order construction; executable futures/options must use their own
  concrete catalogue identity. Fresh reference seeding now advances the legacy
  explicit sector-ID sequence before inserting the dynamic ETF/index hierarchy.
- Added broker-aware account onboarding and atomic full-document credential
  rotation. Delta region, exact IBKR account/TLS gateway, time-bounded Zerodha
  sessions, complete Saxo OAuth snapshots, and credential-free local paper
  accounts now have explicit mutually exclusive contracts; plaintext is never
  returned.
- Enforced one broker route per broker/environment/region, globally unique
  credential references, and at most one active credential version per linked
  account while retaining disabled history. The backend role now has only the
  tenant-scoped credential update policy needed for rotation, and canonical
  broker seeds resolve foreign keys by stable broker code instead of assuming
  sequence-generated IDs.
- Removed scoring's runtime instrument-catalogue write surface and its implicit
  strategy, sector, and instrument-sector creation paths. Signal ingestion now
  validates the pre-provisioned catalogue and fails before persistence when any
  identity or relationship is absent; alias-aware reads use the same canonical
  instrument resolver as writes.
- Added a required PostgreSQL integration acceptance for all six real runtime
  logins. CI now provisions the same one-role-per-service topology as local and
  production, verifies the positive and negative privilege matrix, and proves
  backend broker credentials and encrypted secrets are readable/updatable only
  inside the selected tenant.
- Unified basis-point and fractional execution-cost conversion in `lib_common`.
  Validation scenarios can now produce the exact commission/slippage rates
  consumed by the runtime paper broker, reject unrepresentable spread/impact
  dimensions, and prove identical price and commission for one reference fill.
- Added an authoritative pre-trade market-session boundary. Crypto instruments
  are explicitly continuous; every non-crypto instrument requires fresh,
  complete official broker/exchange coverage before new exposure. Calendar
  provenance, coverage, open intervals, and exact instrument assignments are
  replaced atomically through an admin-authenticated backend route. Missing,
  stale, future-dated, and out-of-coverage schedules fail closed, while
  reduce-only `CLOSE` signals remain eligible. Its revision identifier also
  fits Alembic's standard 32-character version column so a fresh PostgreSQL
  migration can advance beyond this boundary.
- Added independently supervised official schedule writers for IBKR Client
  Portal, Saxo OpenAPI live, and NSE current market state for Zerodha routes.
  Writers resolve exact conid/UIC/Kite identity from the canonical catalogue,
  validate all configured responses before publishing, and use the existing
  authenticated backend calendar transaction. IBKR persists regular
  `liquid_hours`, Saxo persists only `AutomatedTrading`, and NSE positive
  authorization is a bounded current-date lease; source outages never trigger
  weekday, holiday, timezone, or session-time inference.
- Ratcheted the application broad-exception audit baseline from 46 to 45 after
  removing the final dormant strategy-version compatibility boundary.
- Ratcheted the application broad-exception audit baseline from 45 to 44 after
  removing the market-data service's false-green scheduler crash boundary.
- Added the account-scoped Saxo OpenAPI adapter and canonical SIM/LIVE broker
  catalogue. Typed broker mappings keep the exact broker symbol, opaque venue
  ID, and venue type separate; execution resolves them through the selected
  linked account before broker access, and seeds only Saxo's documented
  EURUSD/UIC 21/FxSpot mapping. Orders require explicit AccountKey, ClientKey,
  UIC, AssetType and option open/close intent; pre-check disclaimers block
  automation, ambiguous submissions are never retried, and OAuth access
  expires closed unless an externally refreshed credential document—with
  Saxo's rotated refresh token—has been atomically persisted. Account balance,
  position, order-audit, capability, factory, seed, migrations, and contract
  tests now share that same environment-specific broker contract.
- Removed IBKR execution-time contract search and first-result inference.
  Orders now require selected-account catalogue conids before broker access,
  validate cached Client Portal sessions through `/tickle`, and fail closed on
  missing or reused contract identities. Delta spot routing now reflects the
  derivative-only adapter, every execution mode is capability-checked, and
  unsupported sequential multi-leg residue has been deleted. A `vmdev audit`
  gate keeps database broker capabilities aligned with the runtime matrix.
- Completed the non-Coinbase candle boundary with exact Delta regional,
  Zerodha, IBKR, and Saxo live/simulation provenance. Every feed now resolves
  canonical selectors through the shared broker instrument catalogue; typed
  IBKR conids, Zerodha instrument tokens, and Saxo UIC/AssetType pairs never
  leak into environment configuration or downstream notifications. Historical
  request ranges are timezone-safe, ambiguous venue search is removed, Saxo
  OAuth expiry and delayed/bid-ask-only live data fail closed, and simulation
  data cannot masquerade as live. Shared feeds use dedicated market-data
  credentials rather than any tenant's execution secret, and fatal provider
  contract failures now exit non-zero after cleanup instead of reporting a
  false-green shutdown.
- Made the isolated days-scale historical warmup provider-neutral. It now uses
  the live ingestor's exact source, shared broker catalogue identity, provider
  registry, and dedicated venue credentials for Coinbase, Deribit, Delta,
  IBKR, Zerodha, and Saxo. Every returned row must match the configured source,
  instrument, and timeframe; cross-venue fallback and provenance relabelling
  fail closed. Continuous and session-based feeds have separate total-coverage
  and recent-tail gates; clean scheduled-market gaps avoid repeated
  overnight/weekend requests while transport failures still retry. The
  backfill Compose process receives the same venue-specific credential surface
  as the live poller.
- Consolidated execution and scoring process controls into frozen, validated
  startup snapshots that are injected into long-lived collaborators. Removed
  import-time and per-decision environment parsing, made malformed numeric and
  boolean values fail startup, passed the scoring-relay API key explicitly,
  fixed Coinbase fill polling to its documented safety timeout, and extended
  `vmdev audit` to detect hand-parsed values hidden behind environment mappings
  or local aliases.
- Consolidated indicator supervisor and worker process controls into the same
  frozen startup-snapshot boundary. Strategy selection, schema path, start
  pacing, scoring endpoint, inter-service credential, retry bounds, database
  URL, notification channel, and catch-up controls no longer change during a
  running process; malformed operational values fail startup and health reports
  the selector that the supervisor actually loaded.
- Added a real PostgreSQL integration gate for the market-data notification
  boundary. A provenance-stamped public Coinbase candle now traverses the
  scheduler's committed upsert, post-commit `NOTIFY`, the production
  `PgNotifyListener`, SignalWorker catch-up, and its durable watermark in CI.
- Added a network-free PostgreSQL integration gate for the complete trading
  pipeline. Both retained strategy cores now replay the frozen public Coinbase
  dataset through scoring, account-scoped bindings, transactional outbox
  delivery, fail-closed historical execution, authorized exact paper fills,
  flat positions, realized P&L, and provenance-bearing feedback in CI.
- Removed the last PostgreSQL `create_all` path from the instrument-catalogue
  loader. `bootstrap_scoring.py` now requires an Alembic-managed schema and
  rejects missing or unknown asset classes instead of silently fabricating
  equity semantics.
- Made Coinbase sandbox certification mandatory for release-tag image builds:
  missing or unusable sandbox credentials now fail the release gate instead of
  allowing an all-skipped integration suite to report success. Pull-request and
  local runs remain credential-optional, and the gate never targets live
  trading endpoints.
- Added exact indicator-runtime observability: successful closed bars and
  accepted non-HOLD signals are counted with bounded strategy/timeframe/action
  labels, worker-process counters are aggregated through Prometheus
  multiprocess storage, and the parent health server exposes them at
  ``/metrics`` without making metric failures block trading.
- Removed four fabricated numeric tenants, their roles, subscriptions, feature
  assignments, sizing profiles, and risk mandate from the canonical database
  seed. Local and production bootstrap now share the same production-shaped
  catalogue; explicit account-owned paper/E2E seeds remain the only user setup
  path, and the production guard still rejects legacy numeric demo rows.
- Made pgAdmin startup consume and display the same repo-root `.env` values
  passed to Compose, reject missing or placeholder credentials, and removed the
  stale documented `admin123` fallback.
- Made `vmdev run app` resolve each shipped application’s real executable
  module, fail on missing runtime layouts or virtualenvs, and propagate child
  process failures instead of reporting false success.
- Removed stale 64/69-table and renamed-table documentation. Alembic is stated
  as schema authority, the ORM is described without a drift-prone hard-coded
  count, and current table names now match the model registry.
- Removed the process-global Coinbase quote-currency rewrite. Broker orders now
  preserve the exact USD, USDC, or other settlement instrument selected by the
  account-scoped binding and canonical catalogue, so one tenant cannot rewrite
  another tenant's Coinbase product.
- Made execution sizing and local-paper economics currency-safe. Account
  capital and risk budgets are converted with the latest eligible observed FX
  rate at the signal timestamp before settlement-denominated quantity is
  calculated; the same provenance is persisted with the canonical order.
  Paper cash, equity, exposure, and P&L remain in the linked account's base
  currency while broker commissions retain their exact settlement amount and
  currency. Coinbase candle closes become FX-eligible only at interval end, so
  historical execution cannot read the current candle's future close. Missing
  observations, mismatched products, and implicit USD/USDC parity now fail
  closed.
- Made local-paper restart recovery derive cash, realized P&L, open-position
  basis, and fill-origin marks from the account-scoped canonical execution
  ledger and each order's persisted FX provenance. A restart after the fill
  commits but before metrics/positions persistence now rebuilds and repairs the
  position projection instead of silently returning a full-balance flat
  account. Execution metrics remain observability snapshots and are never
  synthesized as accounting truth. Linear futures and perpetual orders now
  persist their exact contract multiplier, leverage, contract type, and
  fill-time currency provenance; paper cash moves only by variation P&L and
  fees, current margin is gross notional divided by leverage, and restart replay
  reconstructs the same contract position and account economics. Missing or
  inconsistent terms, inverse/quanto value models, and unsupported funding or
  liquidation assumptions fail closed.
- Removed the zero-consumer scoring enrichment framework and its
  `example_strategy_id` configuration. Canonical signal producers own metadata;
  a remote enrichment boundary will be introduced only with an owned,
  asynchronous, fail-closed production consumer.
- Made post-broker recovery crash-safe: both the submit path and stale-order
  reconciliation now append the idempotent canonical fill/state first and mark
  the pending recovery row terminal only afterward. A fill-ledger failure
  therefore leaves the order discoverable for retry instead of creating a
  terminal pending row with missing accounting evidence.
- Removed the zero-consumer strategy engine discriminator and MLflow, RL policy,
  and agent-graph metadata from the canonical strategy-version catalogue.
  Migration `0065` locks the catalogue and refuses non-`python_service` or
  metadata-bearing historical rows instead of inventing a conversion.
- Canonical paper replay now selects one exact persisted market-data source
  (`--source`, default `coinbase_live`) instead of a prefix. This removes
  nondeterministic fill selection when similarly named provenance streams
  coexist. Replay also preserves the canonical external signal identity for
  fill lineage while deriving its execution claim in a deterministic replay
  namespace, so a normal freshness rejection cannot suppress the authorized
  paper replay and repeated replay remains idempotent. Broker-route and
  execution-policy snapshots are now validated at dispatch entry, before any
  broker-visible side effect; replay keeps account currency in the owned
  account/profile snapshot instead of adding undeclared route fields.
- Rebuilt the Docker end-to-end acceptance guide around isolated, uniquely
  named resources, least-privilege service roles, public Coinbase candles,
  deterministic paper costs, and scoped routing/fill/P&L/feedback/RLS
  assertions. Its PostgreSQL checks use unambiguous loop-variable names so the
  documented run is executable without PL/pgSQL column-name conflicts.
- Removed the feedback service's placeholder `/stats` response; authenticated
  suggestion-review routes remain the operational API, while shared health and
  readiness endpoints own service-state reporting.

- Made source-installed application virtualenvs clean their generated
  ``build/``, ``dist/``, ``*.egg-info``, and bytecode state before packaging.
  Deleted modules can no longer be resurrected from stale setuptools output;
  wheel and venv builds share the same cleanup boundary.
- Made the pending-order recovery write a mandatory pre-submit durability gate.
  Pending-order and reconciliation mutations now return a committed local order
  identity or raise; SQLAlchemy, operating-system, missing-adapter, missing-row,
  and unacknowledged-write failures block every broker submission instead of
  degrading to best-effort logging.
- Made scoring startup require durable PostgreSQL configuration and made
  readiness reject in-memory stores. Runtime images no longer default scoring,
  feedback, or market-data processes to the schema-owner login, and component
  database URL assembly requires an explicit identity. Removed unused scoring
  API provider parameters, unused vmdev configuration helpers, and the
  nonfunctional future-RBAC ownership manifest; `config/build.yaml` owns build
  grouping while `.github/CODEOWNERS` remains the sole review-routing authority.
- Scoped strategy-binding precedence, inactive-binding suppression, execution
  configuration lookup, and execution deduplication to the exact linked broker
  account. One user can now route the same strategy signal independently to
  multiple selected accounts without wildcard reactivation or cross-account
  dedup collisions.
- Removed the empty post-LEAN `frameworks` build category and its no-op
  `vmdev`, wheel-validation, team-build, test-discovery, configuration, and
  documentation surfaces. Active wheel builds now cover only shared libraries
  and the production indicator strategy package.
- Replaced synthetic strategy-price waves and the in-process false-E2E suite
  with core contract replays over the provenance-stamped public Coinbase
  BTC-USD fixture; full readiness remains a PostgreSQL-backed Docker-pipeline
  assertion. Normalized shared Python dependency floors to the exact versions
  exercised by the production constraints lock, retained next-major caps, and
  removed the empty reconciliation tooling package. Retained strategy cores now
  carry the exact source candle provenance into canonical signal metadata, and
  historical execution replay uses an explicit half-open date window.
- Removed unreferenced scoring-domain records, conversions, and exports,
  retaining only the live scoring read models and execution-decision contract. Pruned
  six zero-consumer indicators while retaining every production strategy
  dependency and the Vortex/ATR/SMA real-data parity boundary.
- Made realized mode-performance attribution deterministic and account-safe.
  Execution metrics now correlate through the exact executed audit record's
  canonical signal, owner, account, strategy, mode, run, and runtime signal
  identity instead of joining on `run_id` alone. One trade contributes once per
  normalized horizon bucket, and the supported `15min` evaluation horizon now
  maps to `intraday`.
- Made canonical signal identity mandatory across persistence, scoring,
  transactional-outbox dispatch, and execution deduplication. HTTP producers
  derive one deterministic per-bar `external_signal_id` when callers omit it;
  internal event contracts, canonical signals, asset scores, decision logs, and
  execution commands then require that stable identity. Migration `0063`
  refuses unattributed historical rows instead of inventing identities.
- Completed signal-to-fill lineage enforcement. Canonical order intents now
  require their persisted strategy signal and selected instrument, every fill
  must match that signal's instrument, and exact broker fills require a nonblank
  stable venue trade identity. Accounting uses an inner relational signal
  join, and migration `0064` refuses unattributed historical rows. Scoring
  weight maps now reject malformed, duplicate, non-finite, negative, or unknown
  entries, and market context is partitioned by an explicitly resolved asset
  class instead of fabricating a crypto default. Feedback horizon configuration
  rejects empty, duplicate, and unknown values. Removed unenforced trading-hour
  configuration until exchange-calendar session gating is implemented.
- Removed the retired Cloud Run topology, its CI validator, Google event/secrets
  adapters, wheel extras, and configuration switches. DigitalOcean deployment
  remains owned by the `infra` repo; platform application secrets use the
  environment and broker credentials use the account-scoped DB/env/composite
  provider boundary.
- Made historical OHLCV repair safe for stateful indicator strategies. Price
  upserts now increment a content revision only for actual changes and
  atomically mark affected feed watermarks; supervisors rebuild every subscribed
  symbol in deterministic timestamp/symbol order inside a fresh process, suppress
  every replay emission, and clear the generation-fenced request only after a
  successful replay. A post-mutation checkpoint failure now retires the core
  instead of retrying a bar against already-mutated state. Indicator config also
  has one canonical top-level strategy id/version, copied into core parameters
  only at the runtime boundary.
- Removed the empty web/TypeScript/Turborepo, cross-language-contract, and
  experiments scaffolds plus the no-op TypeScript CI lane. Tooling and contract
  generation will be introduced with owned production consumers; research stays
  in the registered `vmdev strategy` evidence workflow.
- Renamed the active strategy insight wire model and helpers from retired
  provider-specific GCP terminology to the provider-neutral
  `InsightSignalPayload` contract, without changing `/api/v1/signals`.
- Removed permissive signal/config validation modes. Unknown actions and invalid
  scoring or execution configuration now fail closed unconditionally instead
  of being rewritten to HOLD or allowed through an environment escape hatch.
- Made strategy environment admission fail closed. Every strategy must declare a
  non-empty environment allowlist and the runner must know its current
  deployment environment; missing, malformed, empty, or unknown values can no
  longer activate a strategy through compatibility defaults.
- Made feedback horizon selection fail closed for signals without an explicit
  holding period, preventing identity-less historical rows from being graded at
  every horizon and contaminating consecutive-wrong or optimization evidence.
- Removed the obsolete duplicate `run_id` copies from execution-log JSON.
  Cross-container execution tracing now has one indexed source of truth in
  `execution_logs.run_id`.
- Made paper execution capital explicit and account-owned. Paper accounts now
  require persisted initial equity, cash, and base currency; resolver-owned
  paper brokers replay account-scoped canonical fills and repair the position
  projection, while missing fill/FX provenance blocks sizing instead of
  inventing a 100,000 USD account. Removed the
  broker bridge's parallel paper implementation, profile-based account
  inference, paper account auto-provisioning, replay defaults, and execution/P&L
  USD fallbacks. Daily NAV and risk/drawdown baselines are now account-native
  and unique per broker account/date through linear migrations 0060 and 0061.
- Removed the parallel `vmdev build all` orchestration, unused single-symbol
  strategy-state API, and stale execution-decision external-ID reconciliation
  fallback. CSCV now requires its pre-registered calendar explicitly, and every
  persisted backtest report is content-hashed and verified on idempotent resume.
- Removed fake GCP and Cloud SQL placeholders plus unused service/resource
  settings from the active deployment YAML. The strategy runner now resolves
  its scoring endpoint from a provider-neutral `endpoints.signal_api_url`
  contract, while DigitalOcean remains authoritative for runtime topology.
- Consolidated the strategy-emitter endpoint on `SIGNAL_API_URL`, removing the
  duplicate `SCORING_ENGINE_URL` environment name and unused
  `gcp_signal_api_url` strategy parameter.
- Removed the validation backtest's legacy fill/cost constructors and parallel
  simulator branch. All runs now use explicit bar-interval timestamps, the
  conservative `next_open_bracket_v2` policy, and the configured flat-fee cost
  model by default; benchmark registration and reports expose the same current
  contract.
- Replaced the production sizing kernel's compatibility-named quantity-rounding
  mode with the descriptive `price_tiered_decimals` policy. Removed duplicate
  pre-cap risk-reporting fields; execution now reports post-cap, post-rounding
  effective risk while preserving the resulting order quantity. The policy and
  sizing-evidence contracts advance to v2 and v3 respectively.
- Made strategy-binding onboarding fail closed: new API and database rows now
  default to inactive/manual without changing intentionally active existing
  bindings. Broker-account onboarding now requires an explicit canonical base
  currency instead of silently assuming USD. User and linked-account schemas no
  longer provide an implicit USD default, and the FX migration refuses
  unresolved historical currencies rather than inventing ownership economics.
- Made attributed canonical `executions` rows the only fill evidence accepted by
  soak certification. Exact fills must join through canonical order intent
  and order records with matching account, signal, strategy, and instrument
  provenance, carry a non-empty venue `trade_id`, actual timestamp, fee amount,
  and fee currency, and remain unique on `(order_id, trade_id)`; cumulative
  status-derived keys are not accepted. Execution logs and pending orders
  are diagnostic and reconciliation state only. The deployed execution service now refuses startup
  and reports not-ready when `DATABASE_URL` is absent, while database-free
  `ExecutionEngine` construction remains limited to isolated unit tests.
- Removed application test sources and repository-only agent/OpenSpec metadata
  from Docker build contexts while preserving non-application test assets,
  ratcheted the production broad-catch
  audit to its measured count, and excluded environment-owned Codex skill
  bundles from product pre-commit hooks. Corrected the backend documentation to
  describe its admin-key-only DigitalOcean configuration surface, removed the
  obsolete GCP deploy permission, stale common-types reference, and duplicate
  per-wheel test extras now owned by the root development environment. Aligned
  package metadata with the repository-wide Python 3.11 minimum and added
  next-major upper bounds matching the central constraints policy.
- Removed fabricated Layer-2 market defaults and made point-in-time persisted
  price history the unconditional production context source. Context derivation
  now selects an explicit source/timeframe, excludes future observations, and
  fails closed on missing, insufficient, malformed, or stale history. Explicit
  unit/backtest contexts remain injectable without fallbacks, and the remaining
  heuristic is truthfully named `DeterministicMetaScorer` and documented as an
  uncalibrated bounded scorer for paper validation; scoring now refuses to start
  it in live mode.
- Replaced the collision-prone polymorphic integer ownership in
  `risk_mandates` and `risk_breaches` with real `users.user_id` foreign keys;
  account-attributable breaches now carry a composite-validated linked broker
  account. Migration `0058` refuses unknown, conflicting, or ambiguous legacy
  ownership, and gives only the execution service explicit command-specific
  RLS policies for these tables.
- Made append-only `executions` the only accounting input for FIFO P&L and
  strategy metrics. Canonical order intents now carry relational strategy,
  signal, side, execution-mode, and broker-environment attribution; migration
  `0057` backfills it from the owned pending-order link and fails closed on
  ambiguous or unattributed lineage. Partial fills retain their independent
  venue trade identities and exact account, settlement-currency, timestamp, and
  commission economics; a venue trade cannot later mutate into a fee-only
  adjustment.
- Made feedback optimization resolve parameters exclusively from the signal's
  exact active `strategy_versions.default_params` snapshot and refuse missing,
  mismatched, malformed, empty, or no-op suggestions. Suggestions now identify
  their actual bounded-heuristic method instead of claiming a grid search.
  Extracted suggestion listing and approval/rejection into
  `SuggestionReviewService`; review remains audit-only and cannot mutate
  strategy files.
- Made the account position ledger preserve explicit asset, contract, or
  quote-notional quantity semantics, optional observed multipliers, exact gross
  notional, and valuation currency across process restarts. Migration `0056`
  refuses to guess when legacy position rows exist and retires the unused fixed
  two-leg `options_positions` tracker plus its simulated domain P&L and
  zero-consumer repository methods; canonical orders, fills, and positions are
  the only execution sources of truth.
- Removed the zero-consumer decision/consensus/opportunity schema and its
  `order_intents.opp_id` coupling. Migration `0055` fails before DDL if any
  dormant row remains, drops only the verified-empty tables, and leaves the
  active canonical-signal, scoring, outbox, account, order, fill, P&L, and
  feedback pipeline intact. `user_budget_buckets` remains available.
- Consolidated PostgreSQL integration and ORM/Alembic drift verification into
  the single required, path-aware `ci-gate`; integration now builds schema from
  Alembic head with scoring auto-creation disabled, and required CI runs the
  pre-commit private-key and Python AST checks. CI installs the exact indicator
  and vmdev dependency closures before collecting the full suite, and every
  GitHub Action (including DigitalOcean release authentication) is pinned to an
  immutable reviewed commit.
- Upgraded the production API and persistence closure to current stable
  FastAPI, Starlette, Pydantic, Uvicorn, SQLAlchemy, Alembic, and PostgreSQL
  driver releases under the shared lock, with full API, migration, wheel, and
  container validation required before rollout.
- Replaced the DB secrets backend's single Fernet key with a newest-first
  `SECRETS_MASTER_KEYS` MultiFernet ring, added account-scoped atomic ciphertext
  rotation with `last_rotated_at` tracking, and made malformed or ambiguous key
  and credential registrations fail closed. The obsolete singular key input was
  removed; deployments must provide the rotation-capable key ring.
- Removed zero-consumer mandatory PyArrow and TA-Lib dependencies and moved the
  lazy GCP clients behind explicit wheel extras, eliminating them from the
  DigitalOcean service closure. Rebuilt all five service images on a shared
  constraints-pinned multi-stage base with isolated per-service wheel stages,
  exact wheel freshness checks, build-tool-free runtime venvs, stdlib health
  probes, per-image BuildKit CI caches, and an audit gate preventing duplicated,
  unpinned, or optional-heavy runtime dependencies. Runtime stages also remove
  third-party NumPy, Pandas, Greenlet, JSON Schema, Referencing, and Alembic test
  suites after dependency validation instead of shipping non-runtime test
  sources in every service image.
- Made broker account and settlement-currency identity explicit from binding
  through canonical order, fill, P&L, and feedback persistence, with database
  ownership constraints that reject cross-user account references. Removed the
  scoring engine's parallel binding/instrument write endpoints and fabricated
  manual dispatch route; the backend and source-controlled instrument catalogue
  are now the write authorities, while scoring diagnostics require an admin key.
- Added an independently supervised observed-FX process that persists official
  ECB EUR reference rates and real Coinbase USDC-EUR hourly candles. Broker
  balances, positions, P&L, and NAV now normalize to the selected account/user
  base currency from fresh point-in-time direct, inverse, or single-EUR-cross
  observations and fail closed without them; no stablecoin parity is assumed.
- Made paper multi-leg options fills require the observed premium for every
  contract leg, retain exact contract identity and multipliers in the position
  book, and reject incomplete pricing without account mutation. Single-leg
  intents now carry their resolved premium explicitly. Strategy Sharpe and
  Sortino metrics annualize from observed trade cadence instead of an assumed
  fixed trading frequency.
- Replaced the shared all-table `vm_app` database grant with explicit NOLOGIN
  backend, scoring, execution, feedback, market-data, and indicator group
  roles. Runtime RLS policies are now command-specific, managed secrets and
  mode-performance rows are account-owned, and cross-tenant authorization is
  carried by service-role membership instead of a mutable session flag.
- Made feedback optimization suggestion-only (review approval no longer mutates
  runtime strategy files), made market-data provenance labels canonical and
  provider-owned, added explicit scoring/feedback mypy coverage, scoped
  `vmdev clean` Docker removal to `vynmatrix/*`, and required release tags
  to reference the current main tip with successful CI for the exact SHA.
  Repaired the supported component/app Makefile build targets and explicitly
  disabled scoring schema auto-creation in production topology references;
  alert-sink credentials are now injected only into the execution service.
- Removed the unregistered ML, RL, and agentic libraries, runners, strategy
  placeholders, signal-provider adapters, Docker scaffolding, and build/config
  inventory. Signal ingestion now accepts only the implemented indicator type;
  unused strategy-type scoring weights, common model/feature type shells, and
  the unconsumed prototype data-source configuration were removed. Also removed
  repository-static config, broker-policy, budget, and strategy
  port/repository/use-case chains plus zero-consumer compatibility aliases,
  including the parallel vertical-spread class hierarchy and `create_spread`
  factory in favor of the contract-aware canonical `build_spread` path,
  along with fabricated local database seed entry points and their dormant
  opportunity records. The production PriceFrameProvider, execution repository,
  signal-performance repository, canonical signal pipeline, broker adapters,
  source-controlled instrument bootstrap, and intentional Domain/DTO/ORM and
  OrderIntent layer boundaries remain.
- Retired VolatilityReversal as `DISCARD_IMPLEMENTATION_RISK` /
  `BLOCKED_INSUFFICIENT_EVIDENCE` / `NOT_SUITABLE`. The configured adjusted
  equity source has no production adapter or authoritative session/corporate-
  action lineage; the checked-in long-only core also diverges from the prior
  Backtrader evidence in fill, state, stop, execution-timing, scoring, and
  feedback semantics. The 15-trade SPY result cannot validate this core, and the
  BTC result is an `ASSET MISMATCH`. Removed only strategy-specific executable,
  config, test, seed, and deployment surfaces; shared equity, IBKR, indicator,
  execution, and feedback capabilities remain.
- Retired EnhancedDualMomentum as `DISCARD_IMPLEMENTATION_RISK` /
  `BLOCKED_INSUFFICIENT_EVIDENCE`. Prior results ran independent single-symbol
  strategy instances and do not measure the checked-in shared rotation
  portfolio; the default BTC/ETH/SOL universe also lacks six complete years
  before its 366-bar warm-up. Durable portfolio state, chronological restart
  recovery, close-fill acknowledgement, broker-stop reconciliation, explicit
  scoring inputs, expiry, and portfolio feedback attribution remain absent.
  Removed its executable, config, test, seed, and deployment surfaces rather
  than adding an unconsumed multi-asset research subsystem. No weak-premise,
  paper, live, shadow, or automatic-deployment claim is made.
- Administratively retired QuantileChannel after its frozen corrected baseline
  was negative after expected costs on both assets, benchmark-dominated, outside
  registered risk limits, and operationally underpowered. The formal campaign
  remains `BLOCKED_INSUFFICIENT_EVIDENCE` because checkpoint-stopped arms and
  lineage-failed trials leave required PBO/DSR/stability evidence incomplete;
  no formal economic disposition is claimed. Executable, dependency, config,
  and deployment surfaces are removed through fail-closed lineage preservation,
  with no trading authority granted.
- Retired VortexRSIProfitTarget after manifest
  `09aa7dfb72cd8210f46272fed40e6de846a21b0610cf0ba7cf575ceceffc0599`
  produced a negative expected-cost baseline on both assets and every OOS fold.
  Independent audit also found a one-bar-latency trade/bracket-lineage mismatch
  hidden by side-only reconciliation, so post-baseline arms remain audit-only.
  Removed its runtime, config, tests, retained-candidate docs, and deployment
  surfaces; migration `0046` now converges historical lineage retired and no
  shadow, paper, live, or automatic-deployment authority is granted.
- Made validation manifest freeze require an explicit data timeframe and,
  when a versioned operational-state contract is declared, exact binding,
  autopilot, strategy-config, stop-loss, and explicit-scoring guard states.
  Archived protocols without that contract remain auditable.
- Retired StatValidatedRegChannelBreakout by owner stop-work decision before
  the third formal Pass A executed. Two prior 862-trial attempts were blocked by
  shared harness failures and provide no economic verdict. Removed its runtime,
  config, bindings, selectors, focused tests, and retained-candidate rehearsal;
  migration `0045` preserves historical lineage and grants no shadow, paper,
  live, or automatic-deployment authority.
- Fixed the paper-soak verifier's source bootstrap and reconciled canonical
  signals to decision rows through the documented cross-container `run_id`,
  while retaining legacy external-ID matching. The verifier now distinguishes
  handled risk rejections from genuinely orphaned signals.
- Retired MaSlope after manifest
  `ac0f4f7276c7a594242b2cb6743e586195a9a689c1a36d55712df8702594f29a`
  returned `BLOCKED_INSUFFICIENT_EVIDENCE` and the independent disposition
  found no robust benchmark-relative edge worth repairing its protective-stop
  and private-model divergence. Removed strategy-specific
  code/config/deployment surfaces, preserved inactive lineage through migration
  `0044`, and granted no shadow, paper, live, or automatic-deployment authority.
- Store canonical frozen campaign protocols by content hash before publishing
  their referencing manifests, allowing retired-strategy evidence to remain
  independently auditable after temporary protocols and source are removed.
- Retired VortexTrendCapture after its frozen 1,187-trial campaign returned
  `RETIRE`; removed its executable/config/deployment surfaces while preserving
  inactive historical lineage. Manifest
  `85a55fade384bb4c157efc3f829b46a92c999ad6e47a7f9c0553b455214b7532`
  grants no paper, live, shadow, or automatic-deployment authority.
- Added content-addressed, fail-closed strategy validation for correctness,
  source data, execution costs, trial selection, walk-forward diagnostics,
  historical disposition, installed wheels, and Docker identity. Campaign-only
  orchestration and statistics are installed only in the validation CLI
  environment, not production service wheels.
- Recorded the frozen Vortex retirement-boundary build and pipeline evidence in
  deployment attestation
  `1e525a3fdeaf77a3d71c9178fd81679031c60762223f189dc838ff5f15aa6160`.
  It predates the validation-boundary cleanup and does not attest rebuilt
  artifacts; protective-order restart durability and complete realized-fill
  persistence remain paper-promotion blockers.
- Released EmaCrossScalper and RsiBounceScalper `1.0.2`; expired startup
  catch-up entries no longer emit or advance model exposure, while stale closes
  remain reduce-only eligible. Tracked bindings remain inactive and paper-only.
- Made feedback pricing no-look-ahead and provenance-bearing, and made feedback
  suggestions fail closed for inactive, retired, deprecated, mismatched, or
  invalid-state strategies. Automatic parameter deployment remains disabled.
- Added canonical signal/execution/instrument/run attribution, one shared
  fixed/risk position-sizing kernel, exact historical data ranges, reproducible
  wheel/image inputs, and fail-closed explicit strategy selection.
- Made repository-level pytest collection independent of an implicit working
  directory by installing one shared repo-root bootstrap and removing five
  copied strategy-test bootstraps.
- Made strategy-retirement migrations, both platform production seed paths, and
  DigitalOcean catalogue convergence abort before mutation when any non-zero
  position exists. Position rows lack strategy attribution, so treating paper
  holdings as warnings could orphan them when their implementation is removed.
  The catalogue seed defines the seventeen retired strategy IDs once in a
  transaction-local table and reuses it for every work, binding, config,
  version, and catalogue convergence check. Global-binding decisions are
  attributed through their immutable decision-context signal identity; only
  retired or genuinely unattributed non-terminal decisions block convergence.
- Normalized offset-aware signal expiry timestamps to UTC before persistence and
  classified mixed explicit/heuristic scoring inputs accurately; removed the
  unreachable generic heuristic state.
- Removed unsupported `allow_short` config fallbacks, copied boolean/universe
  parsers, and dead per-strategy Docker-builder entry points. The validated
  `trade_direction_mode` schema and config-driven service-image build are now the
  only supported controls.
- Aligned Bash, PowerShell, documentation, and `vmdev` on the config-driven
  service-image build. PowerShell rejects positional strategy names, the CLI no
  longer exposes per-strategy/category service builds, and tests run through the
  invoking interpreter rather than an undocumented `devtest` venv.
- Hardened the matching infra release path with a versioned, fail-closed staged
  deploy protocol and documented one-time root-owned bootstrap; converged the
  Block Storage `/etc/fstab` contract even when already mounted, and made the
  policy validator report unexpected services instead of raising `KeyError`.
  The workflow probe is POSIX-compatible and the validator now proves that the
  protocol check precedes the root-owned deploy call with its staged directory.
- Removed three invalid console commands advertised by non-deployable RL and
  agent scaffolds and by the module-launched execution engine. Venv builds now
  resolve every console entry point owned by the installed app distribution,
  so a missing module or target fails the build instead of shipping a broken
  command.
- Excluded nested setuptools `build`, `dist`, and egg-info state from direct
  app and strategy Docker copies. Rebuilt service images contain only current
  source/runtime payloads rather than ignored duplicate package-build trees.
- Declare `lib_indicators` as a direct indicator-runner build dependency so the
  generated application venv can wheel-install native indicator cores without
  relying on Docker-only installation or source-path injection.
- Clean each component's generated `build`, `dist`, egg-info, and Python cache
  state before wheel construction, preventing deleted modules or stale bytecode
  from being repackaged into newly built wheels and downstream Docker images.
- Build every component wheel in an empty private output directory and reject
  bytecode or Python payloads not backed by the current component source before
  atomically replacing prior versions in the shared wheel directory.

### Changed — Reconciled indicator fleet and promotion safety gates (2026-07-20)

- Reduced the default indicator-runner catalogue to three staged
  `KEEP_HIGH_PRIORITY` candidates: MaSlope, VortexTrendCapture, and
  StatValidatedRegChannelBreakout. All remain `READY_FOR_BACKTEST`; source
  loading in a production image and paper-mode configuration are not paper-soak
  or live approval.
- Kept VortexRSIProfitTarget, QuantileChannel, EnhancedDualMomentum, and
  VolatilityReversal disabled for improvement and fresh historical validation.
  SwingHighLowPMO, EmaCrossScalper, and RsiBounceScalper remain native
  operational strategies outside the reconciled catalogue.
- Retired ten rejected catalogue imports and their strategy-only indicator
  dependencies: AdaptiveKalmanFilter, BBKCSqueeze, Chaos, ConfirmedMSS,
  DonchianATRChannel, DualRegimeWeekly, MaRibbonPullback, OUMeanReversion,
  PSARVolumeAdaptiveAF, and VolMomWMAProfitTarget.
- Removed orphaned golden outputs and catalogue documentation for the legacy ML
  strategies that had already been deleted. The generic ML/RL/agentic runners,
  libraries, and build scaffolding remain explicitly dormant with zero
  registered or deployable strategies.
- Removed the remaining retired-runtime LEAN labels, CLI diagnostics, and dead
  export example from active indicator tooling, and corrected the developer
  guides to the ten-config source inventory and three-strategy default fleet.
- Consolidated the image/build path on `vynmatrix/indicator-runner`; the
  default Compose and reference Cloud Run strategy lists now name only the three
  staged candidates. Per-strategy image arguments are rejected by the retained
  build helpers.
- Made indicator-runner selection fail closed when neither `STRATEGY_NAME` nor
  `STRATEGY_LIST` is configured. Unfiltered source discovery now requires the
  explicit `INDICATOR_ALLOW_DEV_DISCOVERY=true` override and is rejected outside
  `ENVIRONMENT=dev`; configured names remain exact and case-sensitive.
- Hardened signal-worker catch-up with serialized processing, monotonic durable
  watermarks, bounded fail-visible HTTP delivery retries, stable external signal
  identity, and restart catch-up that does not acknowledge a failed bar during
  bootstrap. Strategy signals now inherit configured version, asset class, and
  runtime source when a core omits an attribution field.
- Enforced entry freshness and explicit expiry in paper as well as live routing,
  while retaining the reduce-only exemption for delayed closes. Signal expiry is
  now preserved through HTTP ingestion, canonical persistence, outbox events,
  replay, and execution.
- Labeled scoring-input provenance (`explicit`, price-ladder, stop/risk-reward,
  or base-volatility heuristic) and added a binding-level risk gate that blocks
  heuristic scores when calibrated expected return and risk are required.
- Raised consolidated daily-bar coverage to 95%, made signal-worker watermarks
  fail closed, bounded catch-up batches, normalized UTC timestamps, and carried
  the configured asset class and evaluation horizon through the runtime.
- Kept the disabled research candidates fail closed: EnhancedDualMomentum and
  VolatilityReversal now reject non-daily or unattributed sampling and honor the
  configured evaluation horizon, while QuantileChannel rejects malformed
  projected channels, labels deterministic fallbacks, and caps channel-stop
  distance.
- Added a database uniqueness constraint and migration for
  `(strategy_id, semver)`, preserved deprecated native `1.0.0` lineage, and
  registered the hardened SwingHighLowPMO, EmaCrossScalper, and
  RsiBounceScalper implementations as `1.0.1`. KEEP paper bindings are staged
  inactive/manual while their fail-closed safety policies remain active.
- Hardened the real-history warmup around Coinbase's public market-candles
  endpoint: partial credentials, malformed or partially rejected batches,
  non-retryable 4xx, forming candles, unsafe forward resume, and sparse legacy
  ranges now fail closed or repair idempotently. Empty request windows retry
  four times; only one unique window per symbol may defer to the mandatory
  aggregate gate, and a distinct second empty window aborts. Deployment warms
  150 days in a separate one-shot process and requires 95% aggregate coverage,
  a recently closed tail candle, and the stricter per-day database gate before
  strategy emission restarts. A stale suffix is repaired with one API-sized
  recent pass instead of being hidden by the long-window aggregate percentage.
- Updated the Coinbase certification and replay documentation to the current
  USDC instruments and complete evidence-backed marker arguments; the
  SwingHighLowPMO walkthrough is explicitly a disposable native pipeline
  fixture, not retained-strategy promotion evidence.
- Kept paper protective orders working-only until a complete durable design is
  implemented. Continuous market-bar triggering, full stop/OCO persistence,
  atomic delayed-fill accounting, and restoration of a versioned shared
  strategy-model state ledger remain explicit paper-promotion blockers; no
  partial process-memory trigger path is being represented as production-safe.
- Added [the strategy-readiness source of truth](docs/STRATEGY_READINESS.md),
  including exact inventory, asset boundaries, required optimizations,
  empirical promotion/rejection gates, unresolved P0s, and honestly scoped
  verification evidence.

## [1.3.1] - 2026-07-02

### Changed — backend ships inside the scoring-engine image (DOCR 5-repo cap)

The v1.3.0 release failed at the DOCR push: the registry's Basic tier caps at
5 repositories and the standalone `vynmatrix/backend` image would be the
6th. Rather than paying the Professional tier for a tiny FastAPI app, the
backend now ships INSIDE the scoring-engine image and runs as its own
container with a command override (`python -m apps.backend.backend.main`) —
the same pattern the db-migrate one-shot has always used. What this does NOT
change: container-level isolation (own env, non-superuser `vm_app` DB role,
own resource limits, independent restart/lifecycle) and the execution-engine's
own-image blast-radius rule. What it trades away: nothing today — the deploy
model already pins one `VM_IMAGE_TAG` for the whole stack, so per-service
images never provided independent release cadence. `docker/backend.Dockerfile`
deleted; `config/containers.yaml` documents the re-split trigger (DOCR tier
upgraded anyway, or backend dependencies diverge from scoring's). Verified
live locally: backend container serving from the scoring image, `/health` OK,
RLS-scoped bindings reads working.

## [1.3.0] - 2026-07-02

### Changed — Scalpers loadable for controlled DO paper validation

EmaCrossScalper and RsiBounceScalper (born as local e2e-verification
exercisers) now allow `environments: ["dev", "production"]` so a
production-shaped DigitalOcean deployment can validate them against real market
data in paper mode. They are not production-trading approved: metadata remains
`BENCHMARK_OR_COMPONENT_ONLY` / `READY_FOR_BACKTEST`, routine deployment leaves
bindings inactive, and every tracked cloud path keeps live execution disabled.
They run only where an explicit `STRATEGY_LIST` opts them in.

### Changed — Traded pairs switch to USDC quotes (BTC/ETH/SOL-USDC) (2026-07-02)

EUR-funded Coinbase accounts hold USDC (no USD balance; Coinbase EU lists
`-USDC` products), so the platform now trades USDC-quoted pairs natively
instead of aliasing USD→USDC inside the Coinbase adapter (the alias split the
ledger vs the broker book — `BTCUSD` FIFO vs `BTCUSDC` position — in
reconciliation). Legacy USD instruments (instr 1-3) remain for price history
and old positions; new ingestion, signals, binding filters, and orders use the
new USDC instruments (instr 11-13).

- `INGESTOR_SYMBOLS` default → `BTC-USDC,ETH-USDC,SOL-USDC` (local compose,
  droplet compose, `.env.example`s).
- Seeds: USDC instruments + `BTCUSDC/ETHUSDC/SOLUSDC` aliases + Coinbase
  `BTC-USDC/ETH-USDC/SOL-USDC` broker symbols; all binding
  `instruments_allowed` lists rewritten to `*/USDC`; `config/instruments.yaml`
  gains the USDC entries.
- No code changes: symbol flow is instrument/alias-driven end-to-end
  (normalization, scoring instrument resolution, Coinbase product mapping,
  feedback pricing are all quote-agnostic — verified by tracing each layer).
- Verified live: the ingestor's 180-min startup lookback backfilled 179 bars
  per USDC pair on first cycle, covering the scalpers' 120-bar warmup.

### Fixed — Local compose must be invoked with `--env-file .env` (2026-07-02)

With `-f docker/docker-compose.stack.yml`, compose resolves the default env
file against `docker/` (the compose file's directory), not the repo root — so
repo-root `.env` values (credentials included) were silently ignored and every
`${VAR:-default}` fell back to its default. This surfaced as a 401-dead market
data feed after the Jul-2 Coinbase credential rotation (the rotated key never
reached the container). All documented invocations (compose header, CLAUDE.md,
AGENTS.md, READMEs, docs/, setup guides, PowerShell script) now pass
`--env-file .env` explicitly.

### Fixed — DO-promotion readiness fixes from the end-to-end live audit (2026-07-02)

A full adversarially-verified audit of the platform + infra repos, the 24h+ local
Docker run, and the live Coinbase smoke surfaced the following; all fixed in this
change (deeper design items — per-binding environment column, explicit
`live_trading_enabled` opt-in flag, broker-breaker auth-error scoping, outbox
dead-letter requeue tooling — are deliberately deferred pending owner decisions):

- **Reconciliation terminal-syncs stale pending orders** (root cause of the
  ~232/cycle `missing_broker_open_order` warn-flood and the 90k-row
  `risk_breaches` bloat). When a tracked order is missing from the broker's open
  orders, the worker now queries `get_order_status`: a definitive terminal state
  (filled/cancelled/expired/rejected) or order-unknown response marks the
  `pending_orders` row terminal via the new
  `PendingOrderRepository.mark_terminal()`; paper-environment orders (memory-only
  book, lost on restart) are expired directly. Live orders are NEVER
  terminal-synced on a transient API error — the recurring warn stays, as that is
  a real signal. Findings now fire on the transition only. The 441 stranded
  `submitted` rows self-heal on the next reconciliation cycles — no manual DB
  surgery.
- **Naked-position windows closed in the live order lifecycle** (critical).
  `_await_market_fill` (Coinbase adapter) now polls to a TERMINAL status instead
  of returning on the first partial fill or timing out with the market order
  still live; on timeout it cancels the remainder and reconciles the raced-fill
  case (window ~30s, `COINBASE_MARKET_FILL_TIMEOUT_S`). `OrderExecutor` places
  protective exits sized to the ACTUAL filled quantity (full or partial) and only
  skips exits on a definitive zero-fill cancel. A CLOSE now cancels the symbol's
  tracked resting exit orders (OCO bracket legs) before the flatten — previously
  the resting GTC bracket reserved the spot balance (close rejected) or survived
  as an orphan that could fire against future balance.
- **Live-gate hardening: `EXECUTION_MODE=paper` is now a real paper guarantee.**
  A live-environment route snapshot arriving while the engine's own mode is paper
  is BLOCKED (fail-closed, names both modes) instead of executing live under
  `ALLOW_LIVE=true`; live submission now requires mode==live AND allow_live AND
  environment==live. The per-user `live_enabled` gate keys on
  environment=='live' (was mode) so a live route can never skip it. Startup
  warning when `EXECUTION_ENGINE_ALLOW_LIVE=true` while the default mode is paper.
- **Stale CLOSE exempt from the live signal-freshness gate** (mirrors the H2
  reduce-only-through-breaker principle): a delayed/retried CLOSE for an open
  live position now executes with a WARNING instead of being dropped; entries
  keep the 300s freshness rejection.
- **Per-strategy paper/live routing is now expressible**: when a binding's
  `allowed_brokers` contains exactly ONE broker code, the scoring strategy-config
  provider emits it as the config `broker` override (honored by the dispatcher
  route snapshot and the execution broker resolver). `allowed_brokers=["paper"]`
  routes that strategy to the paper broker even for a live-connected user;
  `["coinbase"]` pins it to Coinbase. Multi-broker/empty lists keep veto-only
  behavior.
- **Wildcard-fallback observability**: scoring logs a WARNING when a signal is
  served by a user's wildcard binding while an INACTIVE strategy-specific binding
  exists for the same strategy (this run: demo_user's scalper silently traded LIVE
  under the wildcard after its specific binding was deactivated). Semantic change
  (inactive = suppression) deferred to an owner decision.
- **Execution commands survive multi-hour execution-engine outages**:
  `execution.commands` are enqueued with `max_attempts=60` (~4.7h of relay
  backoff) instead of the library default 10 (~23 min to dead-letter, no requeue
  tooling).
- **Backend config API fails closed**: `create_app` refuses to start when
  `BACKEND_ADMIN_API_KEY` is unset (unless `BACKEND_ALLOW_ANON=true`, a dev-only
  escape hatch defaulting false in code; the LOCAL compose sets it true), and
  `_require_admin` 401s when no key is configured instead of allowing all.
  Previously an unset key granted full cross-tenant read/write + broker-key
  onboarding to anyone reaching port 8081.
- **`backend` is now deployable — from inside the scoring-engine image**: it
  was first registered as a standalone `vynmatrix/backend` image, but the
  v1.3.0 release surfaced that DOCR Basic caps the registry at 5 repositories
  (the 6th repo push was denied, blocking the whole release). Since backend's
  dependency closure is a strict subset of scoring's and the deploy model pins
  one `VM_IMAGE_TAG` for the whole stack anyway, backend now ships inside the
  scoring-engine image and runs as its OWN container via command override
  (`python -m apps.backend.backend.main`) — the established db-migrate
  pattern. Container-level isolation (own env, `vm_app` DB role, own resource
  limits, independent restart) is unchanged; `docker/backend.Dockerfile` is
  deleted. Re-split into its own image when the DOCR tier is upgraded anyway
  or backend's dependencies diverge (trigger documented in
  `config/containers.yaml`).
- **CI is green on main again**: repaired the two stale tests that failed on
  3d24374f/6d19a48d (coinbase adapter test now mocks `_get_product_info` +
  asserts the async-fill handoff; audit baseline guard updated), and raised the
  bare-except baseline 44 → 46 for the two new documented broker-boundary catches.
- **Ingestor healthcheck now detects a stalled feed**: the compose healthcheck
  probes the freshness-aware `/ready` (scheduler.is_fresh) instead of the static
  `/health` that stayed green through the Jun-30 16h feed stall. Mirrored in the
  infra droplet compose (which previously had no ingestor healthcheck at all).
- **Reconciliation is no longer blind after a restart**: contexts were
  registered only when an order executed, so a restarted engine never re-checked
  (or terminal-synced) stale pending orders until fresh traffic happened to
  re-register each context. `ReconciliationTracker` now rehydrates non-live
  contexts from the restored pending-order rows at construction (live contexts
  are deliberately NOT rehydrated — resolving a live broker without its
  credentials would fail every cycle and flip reconciliation unhealthy;
  live rows terminal-sync when real live traffic re-registers a working
  context). `PendingOrderRepository.load()` now includes `user_id` in each
  payload (required for per-user broker resolution — its absence silently
  dropped every row from rehydration in the first local verification).
  Verified live: the startup reconcile expired all 452 stranded `submitted`
  rows in one cycle with exactly one transition finding each.

### Added — Tenant config API (`apps/backend`) + account-scoped RLS (gap #3, H-8) (2026-07-02)

The tenant self-service surface (gap #3) — the first service that genuinely runs
**as `vm_app`**, so Postgres RLS is the hard backstop on untrusted multi-tenant
requests. Verified end-to-end over a real `vm_app` connection: each tenant's HTTP
responses are RLS-scoped to their own rows (demo_user saw only their 2 bindings,
demo_peer only their 3), and cross-tenant writes are blocked by the policy `WITH CHECK`.

- `apps/backend` — FastAPI config API. Endpoints: strategy-binding CRUD
  (thresholds, trading modes, risk caps, filters, autopilot) and broker-account
  onboarding. Every DB unit-of-work is wrapped in `tenant_scope(user_id=…)`.
- Broker-account onboarding stores API keys through the pluggable secret store
  (`DbSecretsProvider`, `SECRETS_BACKEND=db`); key material is never returned or
  logged, only a `secret_ref` pointer. Fails closed (400) on a non-writable backend.
- Admin-API-key gate (operator / web BFF); per-end-user JWT auth is a web follow-up.
- Migration `0040`: account-scoped RLS policies for the trade tables that key off
  `account_id`/`order_id` rather than `user_id` directly — `broker_credentials`,
  `orders`, `positions`, `executions`, `opp_sub_execution_bindings` — via a
  `linked_broker_accounts` subquery. 29 tenant policies total.
- `tenant_scope` now no-ops off Postgres (sqlite test fixtures have no RLS), so the
  per-user unit-of-work pattern is unit-testable without a live DB.

Staged (not in this change): flipping the **internal pipeline** services
(scoring/execution/feedback) from `trader` → `vm_app`. That needs store-layer
`tenant_scope` wiring (fan-out reads take `cross_tenant=True`) plus a full local
e2e first — flipping a live money pipeline blind would RLS-fail-closed and stall
trading. The pipeline stays on `trader` (RLS inert under the superuser) until then.

### Added — Multi-tenant RLS foundation: vm_app role + tenant-isolation policies (H-8, staged) (2026-07-02)

Lands the DB-level tenant-isolation seam from `docs/SCALING.md §H-8` — a backstop
behind the application-layer `WHERE user_id = …` filters, so a single missing
filter can no longer leak/cross-write another tenant's data. Migration `0039`:

- Creates the non-superuser **`vm_app`** role (LOGIN, NOSUPERUSER, **NOBYPASSRLS**)
  that services will connect as; migrations keep running as the owner. Password is
  set operationally at deploy (never in a migration).
- Enables RLS + a tenant-isolation policy on all 24 tables with a `user_id` column.
  Policy scopes rows to `current_setting('app.current_tenant')`, with a deliberate
  cross-tenant read escape (`app.cross_tenant = 'on'`) for the scoring fan-out; all
  writes are self-scoped (`WITH CHECK`).
- `lib_application.db.session.tenant_scope(session, user_id=…|cross_tenant=…)` sets
  those transaction-local GUCs (the GUC is `app.current_tenant`, not
  `app.current_user`, which collides with a reserved keyword under `SET`).

**Staged / non-disruptive:** RLS is enabled but INERT while services still connect
as the superuser `trader` (owner + BYPASSRLS bypasses every policy), so this
changes nothing for the running stack. Verified against real Postgres by connecting
**as `vm_app`**: fail-closed with no GUC (0 rows), per-tenant scoping (only that
user's rows), cross-tenant fan-out (all rows), and `WITH CHECK` blocks a
cross-tenant write. **Not yet flipped** — the remaining go-live steps (per-service
`tenant_scope` wiring incl. the scoring fan-out, account_id-scoped table policies,
the `DATABASE_URL`→`vm_app` connection flip + e2e, and the query audit-lint) are a
separate verified rollout; keep `EXECUTION_ENGINE_ALLOW_LIVE=false` for multi-tenant
until it lands.

### Added — Provider-agnostic secrets backend with a DB-encrypted default (multi-tenant, DigitalOcean) (2026-07-02)

Per-account broker credentials were resolvable only from `BROKER_CREDS_*` **env
vars** in practice — `MutableSecretsProvider` hardcoded `EnvSecretsProvider`, and
`GCPSecretsProvider` was never wired into the execution path — which does not scale
per-user (onboarding a tenant meant editing container env + redeploying) and has no
managed-secrets service on DigitalOcean to lean on. `create_secrets_provider()` was
also GCP-locked for staging/production. Now the backend is **config-selected and
provider-agnostic** via `SECRETS_BACKEND` (`env` | `db` | `gcp` | `composite`;
default `env` for dev backward-compat):

- New `DbSecretsProvider` + `managed_secrets` table (migration `0038`): per-account
  credential JSON stored **encrypted at rest** (Fernet) in Postgres, keyed by
  `secret_ref`, decrypted with a single `SECRETS_MASTER_KEY`. Scales per-user with a
  row insert — no redeploy — and keeps only the master key in the environment. This
  is the recommended prod backend for self-hosted / DigitalOcean.
- `create_secrets_provider()` is now backend-driven (no GCP assumption); the
  execution engine's `MutableSecretsProvider` defaults its fallback to it.
- `scripts/manage_broker_secret.py` onboards/rotates a user's encrypted keys
  (values read from env, never argv; `check` confirms decrypt without printing).
- Docs (`docs/BROKER_CREDENTIALS.md §4`, `.env.example`) updated to the pluggable
  model. Tests cover encryption-at-rest, fail-closed decrypt on key mismatch,
  upsert, and backend selection; verified end-to-end against real Postgres.

### Fixed — Live-configured broker no longer hijacked by an auto-provisioned paper account (2026-07-02)

A live-configured user could be silently downgraded to paper. The execution side
auto-provisions a paper `LinkedBrokerAccount` on a paper order
(`_ensure_paper_account`), and scoring's `_resolve_broker_profile` preferred paper
globally (`env_preference = "paper" if any connected paper account`) — so that
stray paper account shadowed the user's real live account: the profile resolved
the broker to `environment=paper` with no credential, and it self-sustained (paper
routing → paper orders → re-provision, recurring even after revoking the account).
Fixed in scoring: a connected **LIVE** account for a broker is an explicit live
opt-in and is now preferred over paper, both for the per-broker profile entry
(credential_ref) and the top-level default (`broker_environment`, which drives the
broker-route). Paper-first remains the fallback for brokers without a live account,
so legitimate paper auto-provisioning is unaffected. Regression test: a user with
both a live and an (auto-provisioned) paper account for the same broker resolves to
live with the live credential.

### Fixed — SignalWorker survives transient scoring outages (stall blocker) (2026-07-01)

The indicator SignalWorker stopped emitting entirely (silent stall) whenever the
scoring service restarted/redeployed. `_process_bar` fed each bar to the strategy
(which emits via HTTP to scoring) with no error handling, and neither the LISTEN
callback (`_on_notify`) nor the periodic `catchup_all` (which only caught
`SQLAlchemyError`/`OSError`) caught an `httpx` error — so one failed emit POST
(5xx/connect/timeout during a scoring restart) propagated and killed the LISTEN
thread / catch-up loop, permanently stalling signal emission. Signal delivery to
scoring is best-effort: `_process_bar` now catches `httpx.HTTPError`, drops that
one signal, logs, and keeps the worker alive; the strategy's indicator state still
advanced so the next bar re-evaluates. Regression test asserts a delivery failure
does not propagate out of `_process_bar`.

### Fixed — Decouple historical backfill from the live ingestor (feed-stall blocker) (2026-07-01)

The market-data ingestor silently stopped writing candles (price age grew 1:1
with the wall clock), starving every downstream strategy — no signals, no trades.
Root cause: the startup auto-warm backfill ran as an in-process daemon thread that
shared the poll loop's DB connection pool and Coinbase client/rate-limit; paging
months of history could starve the live poll until it stalled. The
"non-blocking" daemon-thread approach was the flaw. Fixed by decoupling: the
ingestor process now runs the poll loop ONLY, and the deficit-aware warmup runs as
a SEPARATE one-shot process (`python -m market_data_ingestor.main backfill`, wired
as the `market-data-backfill` compose service under `--profile backfill`) with its
own engine/pool + Coinbase client, so it can never contend with or stall the live
feed. Verified: cycles run every ~61s with a healthy sawtooth price age; two
regression tests assert the live ingestor does not backfill in-process and the
one-shot disposes its engine. `INGESTOR_BACKFILL_DAYS` is consumed by the backfill
service, not the ingestor.

### Fixed — Scope execution operational alerts to live-only (2026-07-01)

Reconciliation drift and circuit-breaker-open alerts were paging Telegram for
**paper** findings. The paper broker is in-memory and resets on restart, so after
any execution-engine restart the persisted FIFO ledger disagreed with the reset
broker and produced a drift/breaker storm — all of which paged, burying real
live-money alerts. Added a single central gate in `AlertPublisher.publish`: an
alert tagged with a non-paging `environment` (default set is `live` only, via
`EXECUTION_ALERT_ENVIRONMENTS`) is logged but not delivered to sinks. Alerts with
no environment tag (readiness, generic failures) always page. Covers every alert
path — reconciliation drift, reconciliation block, and `circuit_breaker_open` —
at one chokepoint. Also wired a `BROKER_CREDS_COINBASE_LIVE_MAIN` passthrough on
the execution-engine service (inert unless set) so a live-linked account's
per-account credential reaches the container.

### Fixed — Coinbase live order-id capture + sandbox smoke harness (live-trading blocker) (2026-06-30)

The Stage-A Coinbase sandbox smoke (run locally before enabling real-money
trading) caught a live-trading blocker in `CoinbaseAdapter._do_place_order`: it
parsed the created order from a top-level `"order"` key, but Coinbase Advanced
Trade returns it under `"success_response"` (only the GET historical-order
endpoint uses `"order"`). Every placement therefore returned
`broker_order_id=None`, which in live would break order tracking, status
polling, reconciliation, and cancellation. Now reads `success_response` (with an
`"order"` fallback); a unit regression test asserts the id is extracted from the
real response shape.

The smoke suite itself never caught this because it combined a module-scoped
async fixture with per-test `asyncio.run()`: the first test closed its event
loop, so every later authenticated call failed with `RuntimeError: Event loop is
closed` and the order-id assertions never ran cleanly. Routed all calls through
one persistent module loop. Stage A now passes 12/12 against the sandbox (auth,
balance, order place/cancel, status round-trip, invalid-cred rejection,
BrokerBridge delegation).

### Fixed — Market-data ingestor restart resilience (go-live hardening) (2026-06-30)

Bringing the live Coinbase feed online locally surfaced two restart/redeploy
hazards in `market_data_ingestor`, both of which would bite on the first cloud
redeploy once the `prices` table already held history:

- **Startup crash-loop on tz-naive comparison.** `prices.ts` is
  `timestamp without time zone`, so `PriceIngestionService.oldest_candle_ts`
  returned a tz-naive datetime; the deficit-aware auto-warm backfill
  (`HistoricalBackfiller.ensure_history`) compared it against a tz-aware window
  and raised `TypeError: can't compare offset-naive and offset-aware datetimes`,
  crash-looping the container on every restart that had stored history. Fixed at
  the data boundary (`oldest_candle_ts` now stamps UTC) and defensively at the
  comparison site. Regression test feeds a naive oldest-ts.
- **Blocking backfill blinded the live feed.** `_run_startup_backfill` ran
  synchronously before `scheduler.run_forever()`, so a 150-day auto-warm left the
  feed — and every strategy — with no fresh prices for the whole backfill on each
  restart. It now runs in a daemon thread; the live poll starts immediately
  (first cycle upserts within ~1s) while history warms concurrently. The backfill
  is deficit-aware and writes only the older window, so the idempotent upserts are
  safe alongside the live poll. Verified locally: price age recovered 46m → 2.2m
  in one cycle after restart.

### Fixed — Negative-qty (oversell) spot-position guard (2026-06-30)

The local pre-live acceptance check (`scripts/check_soak_acceptance.py`,
`positions_consistency`) flagged a `-24.40 ETH` position — a long-only-spot
oversell. Root trigger (the symbol-format reconciliation divergence → stale
account state → CLOSE qty exceeding the broker-held → `PaperBroker._update_position`
silently reversing into a short) was already fixed (0 recurrence post-fix), but
nothing *prevented* a spot position from going negative. Added two defense-in-depth
guards: (1) `PaperBroker` clamps an oversell to flat instead of opening a reverse
short (opt-in `allow_reversal=True` for margin/futures); (2)
`ExecutionPositionStore.sync_positions` refuses to persist a negative (short) qty
for the long-only-spot platform, flattening the row and logging loudly. Both relax
when short/non-spot modes ship.

### Fixed — Symbol-format reconciliation drift + live CLOSE safety (2026-06-30)

A second 24h verification soak (run on the fully-remediated images) ran cleanly
through the 00:00 UTC daily-cap reset that broke the prior run — execution kept
filling, no stuck positions (H2 reduce-only flattening confirmed), 0 dead-letter,
1:1 signals:scores, feedback healthy. But it surfaced a residual breaker flap
(~20/hr post-reset) and, via a deep execution-flow audit, a latent live-mode
position-flatten blocker. Both are the SAME bug class — symbol-format equality
across layers that use three different formats for one instrument: the ledger
`Instrument.canonical` = `ETH/USD` (slash), the signal/broker symbol = `ETHUSD`
(no separator), live Coinbase = `ETH-USD` (dash).

- **Reconciliation breaker flap (the residual symptom C1 unmasked).**
  `classify_reconciliation` keyed positions by the raw symbol string, so the SAME
  held position was double-flagged `missing_broker_position` (ledger `ETH/USD`)
  AND `phantom_broker_position` (broker `ETHUSD`) — a symmetric block pair that
  opened the circuit breaker every reconciliation cycle a position was held.
  (Pre-C1 the anonymous broker was empty so only the missing side fired; C1's
  per-user broker exposed the phantom side.) Fixed by keying every
  symbol-comparison dict (`_positions_by_symbol`, `pending_symbols`, the FIFO
  classifier) on the canonical `normalize_product_symbol`, keeping the original
  symbol in the value so finding context and `sync_positions` are untouched. Real
  quantity/side drift is still detected across formats.
- **LIVE-BLOCKER — CLOSE could not flatten in live.**
  `order_builder._build_close_order` matched the open position with an exact
  `pos.symbol == signal.symbol`. In live, Coinbase reports `ETH-USD` while the
  signal is `ETHUSD`, so the lookup would never match → the CLOSE silently no-ops
  → an open position can never be flattened (unbounded risk). Masked in paper.
  Fixed to compare on the canonical symbol.

Go-live follow-ups the audit flagged (not yet fixed — tracked): `unexpected_broker_open_order`
warn-flood from account-level broker open-orders vs strategy-scoped local pending
(M1 side-effect); per-(user,strategy,broker) breaker scope means one symbol's
drift halts all symbols (consider per-symbol keys / warn-with-escalation so a
reconciliation comparison can never halt trading); Coinbase `get_positions`
reports long-spot only (cannot represent shorts/perps); `_resolve_instr_id` quote
suffix allowlist; paper `_orders` unbounded resting-leg growth; a single canonical
symbol-compare helper + an audit rule forbidding raw-symbol broker-vs-ledger
equality.

### Fixed — Fresh verification-soak findings (2026-06-29)

Re-running the soak on freshly rebuilt images (Droplet/inline-relay topology) with
the remediation above immediately surfaced two follow-ups — both exposed by the M2
fix now routing scalpers to the sub-hour evaluation horizon:

- **`signal_performance` rejected the `15min` horizon.** The H5 sub-hour machinery
  added `EvaluationHorizon.MIN15='15min'` in code, but the `ck_signal_perf_horizon`
  CHECK constraint was never widened to include it. Latent until M2 made the
  scalpers declare a sub-hour horizon; the feedback engine then hit a
  `CheckViolation` on every `15min` insert (errors=100/cycle) and went unhealthy
  (the `1h` horizon persisted fine). Widened the model constraint + added Alembic
  migration `0037_signal_perf_15min_horizon`, with a regression test asserting the
  DB constraint accepts every `EvaluationHorizon`. Verified end-to-end: 100 `15min`
  rows persist, 0 violations.
- **Feedback healthcheck falsely reported unhealthy in scheduled mode.** The image
  baked a `curl localhost:8002/health` HEALTHCHECK valid only in daemon mode; in
  scheduled mode (the Droplet default) there is no HTTP server, so a healthy
  one-shot-timer engine reported `unhealthy` forever. Made the HEALTHCHECK
  mode-aware (scheduled mode defers liveness to the `service_heartbeats` row the
  acceptance gate reads; daemon mode keeps the HTTP probe).

### Fixed — Post-24h-soak forensic remediation (2026-06-29)

A forensic review of the completed 24h scalper paper-soak (run on pre-fix images)
found one critical execution-halting bug plus a cluster of correctness and
observability gaps. The soak's data plane (market-data → scoring → outbox) was
clean; the execution plane suffered a ~16h silent outage after the 00:00 UTC
daily-cap reset. Remediation, landed as atomic commits to `main`:

- **C1/N11 — reconciliation resolved an anonymous paper broker → breaker storm.**
  `ExecutionEngine.get_reconciliation_broker` dropped `user_id`, so the per-(user,
  type, env, cred) broker cache resolved an empty anonymous instance; every open
  position then looked missing-at-broker (`missing_broker_position` → block →
  circuit breaker). Breakers opened ~36/hr for ~16h and halted execution, leaving
  3 positions stuck open. Thread `context["user_id"]` through so reconciliation
  shares the same per-user PaperBroker the execution path fills. Added non-stubbed
  regression tests (the prior `_FakeEngine` stub hid the bug). This also resolves
  M3 (FIFO drift warns) and M4 (post-reset blocked-ratio) — both N11 byproducts.
- **H1 — circuit-breaker blocks were invisible in stdout.** A breaker block wrote
  only a DB risk-breach row + a Prometheus counter, so a full outage looked like a
  quiet market. Added a block-site WARNING with trace context + `block_reason` and
  a `vm_execution_circuit_breaker_blocks_total` counter.
- **H2 — breaker blocked de-risking orders.** An open strategy breaker also blocked
  CLOSE/flatten orders, trapping open positions. A reduce-only request
  (`signal.action == CLOSE`) now bypasses the strategy breaker; the broker-global
  breaker stays a hard stop.
- **M1 — paper resting orders were flagged missing every cycle.** `PaperBroker`
  accepted stop/take-profit orders as "working" but never tracked them and had no
  `get_open_orders`, so reconciliation flagged each one `missing_broker_open_order`
  (702 warns) forever. Track resting orders in `self._orders` and expose
  `get_open_orders(symbol)`.
- **M2 — scalpers were graded at the wrong feedback horizon.** With no declared
  horizon, scalper signals defaulted to 1.0-day (`horizon_seconds=86400`) and the
  feedback gating routed every scalp to the SWING {H4, D1} buckets — the MIN15/H1
  sub-hour machinery added for these scalpers evaluated 0 signals all soak. Both
  cores now emit `horizon="1H"` (→ 3600s → {MIN15, H1}).
- **L2 — idempotent re-enqueue inflated the dispatch log.** The "Queued execution
  command" INFO fired per event even on idempotent reuse; demoted to DEBUG with one
  INFO batch summary.
- **L3 — empty market-data fetches were silent.** A 200-but-no-closed-candles fetch
  was a bare `continue`; added a `vm_market_data_empty_fetches_total` counter +
  `empty_products` in the cycle summary (stall detection stays with the freshness
  gauge — an all-empty cycle is normal between 1m closes).
- **O1 — no alert sink (the only soak acceptance-gate FAIL).** A live execution
  engine now refuses to start without a usable alert path (`EXECUTION_ALERTS_ENABLED`
  + a sink); paper mode warns. Override with `EXECUTION_REQUIRE_ALERT_SINK`. Infra
  env wiring is a documented follow-up.
- **O2 — per-tenant config dumped at INFO 936×.** `handle_signal` logged the raw
  signal + full `user_strategy_config` (risk caps, allow-list, policy snapshot) at
  INFO; collapsed to a single safe DEBUG line.
- **O3 — httpx access logs were 56–75% of some service logs.** `setup_logging` now
  pins httpx/httpcore/urllib3 to WARNING (`HTTP_CLIENT_LOG_LEVEL` to override).
- **O4 — indicator-runner logged one line in 28h.** The per-strategy
  `signal_worker` subprocess never called `setup_logging`, so its root logger
  defaulted to WARNING and dropped every INFO breadcrumb. `main()` now configures
  logging at `LOG_LEVEL` (default INFO).

### Fixed — E2E soak-review remediation (2026-06-28)

A production-readiness review of the scalper paper-soak harness found the local
stack could not actually validate the pipeline (stale runtime + schema drift +
monitoring-correctness gaps). Remediation, landed as atomic commits:

- **N3 — signal_worker mislabeled every strategy_id.** `_build_worker` read
  `strategy_id` from `config["parameters"]` (always absent — it is declared
  top-level) and fell back to a hardcoded `swing_high_low_pmo_v1`, so ALL indicator
  strategies emitted under one id and never matched their own bindings (zero scalper
  executions). Now resolves top-level config first, else raises. Found by the live soak.
- **N5 — positions never persisted: no `paper` broker.** `docker/seed/02_seed_data.sql`
  now seeds a `paper` broker so the execution engine can auto-provision a per-user
  paper `linked_broker_accounts` (paper FILLS are simulated in-process — no real
  exchange credentials are used). Without it `positions` stayed empty and CLOSE
  orders logged `missing_broker_open_order`. The two paper tenants are `demo_user` +
  `demo_peer` (each gets an isolated paper account; multi-tenant isolation, not shared
  credentials). Found by the live soak.
- **N6 — risk guard blocked ~96% of entries at the cap boundary.** The position
  sizer caps a position's notional to *exactly* `max_position_pct` of equity, but the
  risk guard re-estimates the notional from `market_data`, so price granularity /
  rounding lands it a hair over (e.g. 0.1001 vs a 0.1000 cap) and the strict `>`
  check rejected nearly every entry. Added a small relative tolerance
  (`_CAP_TOLERANCE = 1.005`, 0.5%) to the `max_position_pct` and
  `max_total_exposure_pct` checks (pre-trade and post-trade) so sub-percent overshoot
  no longer blocks, without materially loosening the caps. Found by the live soak.
- **N7 — per-binding `max_position_pct` now reaches the position sizer.** The binding's
  position cap is carried in `risk_caps` (what the risk guard enforces), but the sizer
  reads the `sizing` block and defaulted to 0.10 — so a tenant with a tighter cap (e.g.
  demo_peer's 0.05 ema binding) was sized to 0.10 and then *blocked* by the risk guard
  instead of sized down, yielding zero executions for that binding. `OrderBuilder._parse_user_config`
  now reconciles the two: the sizer caps at `min(explicit sizing pref, risk_caps cap)`,
  so the position is sized down to the binding limit and passes. Found by the live soak.
- **N8 — ingest now dispatches the signal it just ingested, not the latest-for-symbol.**
  The `/signals` and `/api/v1/signals` handlers scored the inbound signal but then
  dispatched `_build_latest_signal(symbol)` (the most recent signal *for the symbol*).
  Under concurrent same-symbol ingestion — a multi-strategy instrument (e.g. ETH/USD)
  receiving signals from two strategy workers at different replay positions — the
  "latest" signal is frequently a *different* signal than the one just ingested, so
  `evaluate_bindings` ran against the wrong strategy/action and the just-ingested
  signal's decisions were dropped (ETH produced 90/226 decisions vs SOL's 198/198,
  where only one strategy emits). Both handlers now dispatch + emit events for the
  ingested `sig` (the `score` already corresponds to it; `sig` carries its own
  external_signal_id for dedup). Found by the live soak. The later account-routing
  consolidation removed the manual dispatch endpoint entirely.
- **N10 — scheduled feedback `evaluate` now covers all configured horizons.** The
  one-shot `evaluate` (run by the Droplet's 5-min timer) hardcoded `D1`, while the
  daemon loops `get_feedback_evaluation_horizons()` (default all, incl. the sub-hour
  MIN15/H1 added for the scalper loop) — so the live topology never evaluated sub-hour
  horizons. `evaluate` now iterates the same configured horizons as the daemon.
- **N9 — local stack now mirrors the live cloud topology (dev-prod parity).** The
  local `docker/docker-compose.stack.yml` ran the outbox relay as a *separate*
  `scoring-outbox-relay` container and feedback as a *continuous daemon* — which
  matches the Phase-2 App Platform shape (`infra/digitalocean/app.yaml`), not the
  **live Phase-1 Droplet** (`infra/digitalocean/docker-compose.droplet.yml`), where
  the relay runs **inline** in scoring-engine (`SCORING_OUTBOX_RELAY_INLINE=true`, no
  separate container) and feedback runs as a **scheduled** one-shot `evaluate` on a
  5-min systemd timer. Local now defaults to the Droplet shape: scoring-engine
  defaults `SCORING_OUTBOX_RELAY_INLINE=true`; the standalone `scoring-outbox-relay`
  service is behind the opt-in `standalone-relay` profile; feedback defaults to a
  scheduled one-shot `evaluate` loop (`FEEDBACK_RUN_MODE=scheduled`,
  `FEEDBACK_EVAL_INTERVAL_SEC=300`). The App-Platform shape is still reachable via
  `--profile standalone-relay` + `FEEDBACK_RUN_MODE=daemon`. Docs reconciled
  (`docs/DEPLOYMENT.md`, `docs/E2E_VERIFICATION_GUIDE.md`). Same images and code paths
  in every topology — only the process topology differs.

- **C1/L6 — alembic is now the single schema-of-record for the local stack.** Added
  a `db-migrate` one-shot to `docker/docker-compose.stack.yml` that runs
  `alembic upgrade head` (via `scripts/db/migrate_and_seed.sh`, `SEED=false`) before
  any service starts, reusing the scoring-engine image (which already bakes alembic
  + `scripts/db` + `docker/seed`). All services + `db-seed` now depend on it
  (`service_completed_successfully`). The schema now reaches head — including the
  `asset_scores.external_signal_id` column (SC-6) and the outbox NOTIFY trigger that
  `create_all` never installs. The scoring engine no longer relies on `create_all`
  in compose: `AppScoreStore` gates it behind `SCORING_SCHEMA_AUTOCREATE` (default
  `true`; always on for sqlite unit tests; set `false` in compose). NOTE: a
  pre-alembic volume (built by the old `create_all` path, no `alembic_version` row)
  needs a one-time `docker compose ... down -v` — see `docs/E2E_VERIFICATION_GUIDE.md`.
- **H1/H3 — runnable scalper soak.** The indicator runner now logs strategies that
  are on disk but excluded by `STRATEGY_LIST` (`"present on disk but excluded by
  STRATEGY_LIST: [...]"`), so the "set `ENVIRONMENT=dev` but my dev-only strategy
  never ran" footgun is answerable from logs (the env gate runs AFTER the
  `STRATEGY_LIST` filter — list membership is the primary selector). Runbook updated
  with the full five-service bring-up (incl. `market-data-ingestor` +
  `feedback-loop-engine`), the `STRATEGY_LIST` opt-in, and the pre-alembic volume
  reset (`down -v`).
- **H2/L5 — instrument de-duplication + normalization guards.** A separator/case
  variant could spawn a duplicate price-less instrument (`BTCUSD` id 11 beside
  `BTC/USD` id 1 + alias), fragmenting prices/scores across instr_ids and starving
  the price lookup. Fixes: `upsert_instrument` now resolves via canonical → alias →
  normalized-symbol scan before creating (no more duplicate-creation vector);
  `PriceIngestionService.load_instrument_map` is collision-safe (lowest instr_id
  wins deterministically + logs); `AliasResolver.resolve_symbol` matches on the
  normalized form (the default alias map's `BTCUSD → BTC-USD` never matched the
  stored canonical `BTC/USD`). New migration `0036_dedupe_instruments` merges
  existing normalized-duplicate instruments (dynamic FK repoint across all
  referencing tables, keep lowest id) and adds a `UNIQUE` expression index on the
  normalized canonical so a duplicate can never recur. NB: the binding
  `instruments_allowed=["BTC/USD"]` already matched a `BTCUSD` signal (the gate
  normalizes both sides) — that was a false-positive, not fixed/needed.
- **H4/M12/L3/H7/H8/M11/L7 — soak_report hardening + correctness.**
  `soak_report.build_soak_report` now: (H4) reports realized P&L as the LATEST
  cumulative snapshot per (user, strategy, symbol, mode) partition — `realized_pnl`
  is a running total, so the old `SUM` over-reported ~170× (−688k vs −4k on the live
  DB) — grouped per (user, strategy, mode) (M12), excluding `blocked` rows (L3);
  (H7) adds a `reconciliation` section that LEFT JOINs actionable (long/short)
  `canonical_signals` → `execution_logs` to flag signals that never reached
  execution (with an orphan-id sample); (H8) runs each stage in isolation so a
  query against a schema behind head degrades that stage to UNKNOWN
  (`report.degraded`) instead of crashing, plus a `schema` preflight that flags
  missing sentinel columns ("run `alembic upgrade head`"); (M11) window-scopes the
  outbox + dedup invariant scans so stale prior-run history can't poison a verdict;
  (L7) `require_feedback=False` makes a missing feedback heartbeat informational and
  an empty/negative window fails fast. `scripts/verify_pipeline_soak.py` gains
  `--no-require-feedback` and exits non-zero when the report is degraded.
- **H6/M9 — continuous soak monitor + missing checks.**
  `scripts/check_soak_acceptance.py` gains `--interval-s N`: a continuous monitor
  loop that re-runs the acceptance checks every N seconds and publishes a
  `critical` alert to the configured `ALERT_*` sinks on every failing iteration
  (default stays one-shot) — so a mid-soak stall/dead-letter/missing-execution is
  caught at hour 3, not only by an end-of-soak one-shot. New acceptance checks in
  `soak_acceptance`: `signal_activity` (newest `canonical_signals` row recent — a
  stall proxy that catches a crashed strategy worker even when market data is
  fresh, MON-4) and `positions_consistency` (no negative-qty position — spot is
  long-only, MON-5). P&L-mismatch (MON-6) is covered by the corrected
  latest-per-partition realized P&L above; service-log error-rate (MON-7) is a
  DB-only-monitor limitation addressed operationally by the loop's alerting + the
  signal-stall proxy.
- **M2 — true shared-signal multi-tenancy.** `docker/seed/05_e2e_scalpers.sql` now
  binds BOTH paper tenants to the SAME scalper (`demo_researcher` + `demo_user` →
  `ema_cross_scalper_v1`) with deliberately different risk caps (demo_researcher
  0.05/0.03/5 vs demo_user 0.10/0.05/20), so one EMA signal fans out to two users
  and the soak proves per-user isolation (separate decisions / executions /
  positions / P&L). Idempotent; validated against a fresh alembic-migrated DB.
- **H5/M8/L4 — sub-hour feedback horizon + per-strategy horizon gating.** The
  smallest `EvaluationHorizon` was `H1="1h"` (and `M1="1m"` confusingly means one
  MONTH), so a 1-minute scalp's `pnl_pct`/`is_correct` measured a 1h-forward return
  unrelated to the trade. Added `EvaluationHorizon.MIN15="15min"` (distinct key,
  not colliding with `M1`; L4) wired into `evaluator.HORIZON_TIMEDELTA` and
  `price_provider.HORIZON_TO_TIMEDELTA`. Added `eligible_horizons_for(horizon_seconds)`
  and gated `get_pending_signals_for_evaluation` by it (M8): a signal is only
  evaluated at horizons matching its declared holding period (intraday ≤1h →
  {MIN15, H1}; swing ≤1d → {H4, D1}; position → {W1, W2, M1}); `horizon_seconds=None`
  stays all-horizons (back-compat). This stops a scalp being scored at 1w/1m and
  inflating the consecutive-wrong tracker.
- **M1 — policy blocks are no longer recorded as `failed`.** `execution_log_status`
  now maps a non-success result whose `execution_mode == "blocked"` (a spot
  long-only SHORT rejection, a risk-cap breach, a freshness guard) to
  `execution_logs.status = "blocked"` instead of `"failed"`, so a deterministic
  platform decision is no longer conflated with a broker/infra failure (poisoning
  the error-rate signal). `execution_logs.status` is a free string — no migration.
  The E2E-guide zero-error query now excludes `no_op` + `blocked`. (The
  decision-log surface — `execution_decision_logs` — still maps non-retryable
  blocks to `failed`; tracked separately as it needs `DedupRecord` plumbing.)
- **M6 — atomic SC-6 idempotent score write.** The asset-score idempotency write
  was a non-atomic SELECT-then-INSERT under the unique `external_signal_id` index,
  so concurrent re-delivery from parallel strategy workers could race two INSERTs
  into an `IntegrityError` that failed the whole ingest. The Postgres path now uses
  `INSERT ... ON CONFLICT (external_signal_id) DO UPDATE` (single atomic statement,
  no race window); sqlite (single-process tests) keeps the SELECT-then-update path.
  Verified on scratch Postgres: a re-delivered `external_signal_id` updates the one
  row instead of raising.
- **M7 — normalized `execution_metrics` symbol.** `ExecutionMetricsStore.record`
  now normalizes the symbol (`normalize_product_symbol`) before the partition
  lookup + persist, so a raw `BTC-USD` on one fill and `BTCUSD` on another collapse
  to one `(user, strategy, symbol, mode)` partition instead of fragmenting the
  cumulative realized-P&L carry-forward.
- **M4/L2 — empty-decision observability + decision-log provenance.** (M4) The
  scoring API's `_dispatch_if_configured` returned silently when `evaluate_bindings`
  produced zero decisions, and `persist_decision_log` only writes when there ARE
  decisions — so a strategy whose signals never reached execution left no trace. It
  now logs (signal_id, strategy_id, symbol, active_bindings) when zero decisions are
  produced for a signal whose strategy has ≥1 active binding. (L2) Documented the two
  authoring paths of `execution_decision_logs` (the scoring dispatcher writes the
  gating-decision row with binding_id/should_execute; the execution-side dedup insert
  records the outcome with those fields intentionally NULL).
- **L1 — scalper regression-lock tests.** The public-Coinbase replay suite asserts
  external_signal_id determinism across re-runs + uniqueness within a run, strict
  LONG/CLOSE alternation (no double-fire), and the exit branches observed in the
  frozen provider window.
- **H9/H10 — risk/NAV monitoring + day-boundary re-baseline test.** (H9) A new
  `nav_recorded` soak-acceptance check flags when `daily_nav` is missing/stale — the
  RiskGuard daily-loss / drawdown caps re-baseline off `daily_nav`, so without it the
  caps are silently inert (`--nav-max-age-days`, default 2). (H10) `resolve_risk_baseline`
  now accepts an injectable `now` (UTC) so the UTC-midnight re-baseline — the one event
  a 24h soak is guaranteed to cross — is deterministically tested: crossing midnight
  flips `day_start_equity` to the prior day's NAV, and a no-fill day (no prior-day NAV)
  correctly leaves the cap inert (documented fail-open, now locked). Risk-block
  visibility is already covered by the distinct `blocked` execution status (M1).
- **N2 — decision-log policy blocks recorded as `rejected`.** The execution-dedup
  decision-log write mapped a non-retryable policy block to `failed` (same
  conflation M1 fixed on `execution_logs`, but on the constrained
  `execution_decision_logs` surface). `ExecutionRecord`/`mark_executed` gained a
  `blocked` flag (set at the engine call site from
  `execution_mode == "blocked"`), mapped to decision status `rejected` (allowed by
  the CHECK constraint) so the decision-log error-rate isn't polluted by platform
  decisions.
- **M5 — dedicated outbox relay (no SPOF).** The scoring→execution outbox relay ran
  inline in the scoring-engine process, so a scoring-engine restart stalled delivery.
  Added a dedicated `scoring-outbox-relay` compose service (the existing
  `main.py relay` standalone mode, mirroring the cloud `scoring-outbox-relay`
  workload) and flipped the scoring-engine inline relay OFF by default
  (`SCORING_OUTBOX_RELAY_INLINE=false`). The outbox claim is row-locked, so delivery
  stays exactly-once even if both run. Verified the relay process starts and serves
  `/health`.
- **M10 — restart/concurrency idempotency + tz convention.** `add_signal` was a
  non-atomic SELECT-then-INSERT under the unique `canonical_signals.external_signal_id`
  — a NOTIFY redelivery / worker restart / outbox retry re-POSTing the same signal
  concurrently could race two INSERTs into an `IntegrityError` that failed the
  ingest. The Postgres path now uses `INSERT ... ON CONFLICT (external_signal_id)
  DO UPDATE` (mirrors the M6 asset-score fix); sqlite keeps the SELECT-then path.
  With M6 + this, both ingest writes are concurrency-safe by construction (the
  14+ strategy subprocesses can re-deliver the same id without failing ingest).
  Added signal-idempotency tests + a TZ-independence test proving the daily-loss
  day boundary stays UTC regardless of host `TZ`. The stale-`executing`-claim
  reclaim already exists (operator-gated `EXECUTION_DEDUP_ALLOW_STALE_EXECUTING_RECLAIM`,
  both branches tested) so a crash between claim and fill is recoverable.

### Added — local-only e2e-verification scalpers (2026-06-28)

- Two committed-but-dev-only indicator strategies under `strategies/indicator/`:
  `EmaCrossScalper` (fast/slow EMA crossover) and `RsiBounceScalper` (RSI
  oversold-bounce / overbought take-profit). Both are long/flat, 1m, signal-only,
  and emit explicit CLOSE for take-profit/stop/flip exits (paper SL/TP legs don't
  auto-fill). Each `config.json` sets `environments: ["dev"]`, so they ship in the
  indicator-runner image but the env gate skips them in the cloud (and they are
  intentionally absent from the cloud STRATEGY_LIST).
- `docker/seed/05_e2e_scalpers.sql` (local db-seed only) provisions both strategies
  + per-tenant paper bindings (demo_user→EMA BTC/ETH/SOL, demo_researcher→RSI BTC/ETH),
  exercising multi-tenant isolation. Idempotent + FK-valid.
- Purpose: a repeatable local 24h paper soak that drives frequent entry /
  take-profit / exit signals through the full pipeline
  (market-data → indicator → scoring → outbox → execution → feedback) to validate
  reliability/concurrency/correctness after a major change. Core logic is
  replay-tested in `apps/indicator_runner/tests/test_scalper_public_replay.py`.

### Added — per-strategy environment gate in the indicator runner (2026-06-28)

- Strategy `config.json` may now declare an optional `environments` allowlist
  (e.g. `["dev"]`). `runner_utils.load_strategy_config` returns a new
  `env_excluded` status and the `IndicatorRunner` discovery skips any strategy not
  permitted in the current `ENVIRONMENT`/`ENV` (resolved at runner startup).
  Absent field = runs everywhere (back-compat — the existing 14-strategy fleet is
  unaffected). Env-excluded names are also subtracted from the
  STRATEGY_LIST-missing warning. This lets local-only e2e-verification strategies
  ship inside the indicator-runner image yet do nothing in the cloud. Schema +
  `runner_utils` unit tests added.

### Added — pipeline soak reconciliation verifier (2026-06-28)

- `lib_application.services.soak_report.build_soak_report(session, *, now, since)`
  — a pure, unit-testable function that walks every pipeline stage over a window
  (signals → scoring → decisions → executions → outbox → pending_orders →
  realized_pnl → feedback) and returns a `SoakReport` whose `passed` is the AND of
  the invariant sections: signals emitted, SC-6 score idempotency (no duplicate
  non-null `external_signal_id`), ≥1 real paper fill, zero outbox `dead_letter`,
  no duplicate `idempotency_key`, and a fresh `feedback_loop_engine` heartbeat.
  `render_markdown()` emits a compact digest. Handles the tz-naive
  (`canonical_signals`/`asset_scores`/`signal_performance`) vs tz-aware
  (`*_at`/`execution_decision_logs.ts`) column split.
- `scripts/verify_pipeline_soak.py` — thin CLI over it (mirrors
  `scripts/check_soak_acceptance.py`): reads `DATABASE_URL`, `--hours` (default
  24), `--json`/`--output`; exit `0` pass / `1` fail / `2` missing DB URL.
- `tests/test_soak_report.py` — sqlite-seeded invariant verdicts (clean pass,
  empty-window fail, dead-letter fail, distinct-score count, stale-heartbeat fail).
- Companion to the `check_soak_acceptance` go-live gate: this is the one-command
  reconciliation for the recurring local 24h scalper soak. Runbook:
  `docs/E2E_VERIFICATION_GUIDE.md` → "Recurring 24h Scalper Soak".

### Added — multi-factor asset scoring (Q3, 2026-06-28)

- Turns the single-alpha asset score into a multi-factor composite, default-OFF.
  New `scoring_engine.factors` (`FactorBlender` + `momentum` / `low_volatility` +
  `winsorize`) computes orthogonal per-asset factors from the `prices` history,
  standardizes each to a z-score over its own rolling window (reusing
  `RollingStats`), winsorizes, and blends them (equal-weight default,
  `SCORING_FACTOR_WEIGHTS` overrides) into a composite alpha.
- The blended contribution is added to `alpha_raw` in `compute_global_score`
  (`factor_alpha`, default 0.0) and the composite is standardized in a SEPARATE
  `mf:` rolling-stats namespace so the single-alpha window is never mutated. The
  per-factor breakdown rides in `asset_scores.weights_applied['_meta']['factors']`
  (additive JSON, no migration). Flag: `SCORING_MULTI_FACTOR_ENABLED` (default
  false) — when off, no blender is constructed, `factor_alpha` is 0.0, and the
  score/`_meta` are byte-identical, so the running 14-strategy paper soak is
  undisturbed. Cross-sectional ranking is intentionally omitted (the live crypto
  universe is too small for a meaningful cross-section); the factor set is a
  registry, extensible to IC-weighted/feedback-derived factors.
- **Flipping the flag re-scales the gating score** (like
  `SCORING_CROSS_STRATEGY_ENSEMBLE`): it is a deliberate flip gated on e2e
  validation. Price-derived factors and Layer-2 context both require populated,
  fresh `prices` history. Deploy:
  `docker-compose.stack.yml` + `config/cloudrun/scoring-engine.yaml` (default off);
  the infra repo env must mirror it (default off) at go-live.

### Added — shorting-eligibility gate: strategy asset-class dimension (Q2, 2026-06-28)

- Completes the 4-way SHORT capability matrix in `RiskGuard._evaluate_short_block`.
  The gate already had three dimensions (user eligibility × broker/mode support ×
  instrument shortability); this adds the 4th — **strategy asset class** — sourced
  from `signal.asset_class` against a config allowlist
  (`EXECUTION_SHORT_ELIGIBLE_ASSET_CLASSES`).
- Additive + default-OFF: an empty allowlist (the default) imposes no asset-class
  restriction, and an unknown/absent `signal.asset_class` is never blocked — so the
  running spot-only paper soak is byte-identical (spot already blocks at the
  broker/mode dimension, and the new branch is a no-op when the allowlist is empty).
  `rule_code`/message are unchanged; the new failing dimension surfaces as
  `context['short_block_dimension'] == 'asset_class_not_shortable'`. CLOSE-of-long is
  still never gated (the block only fires on `action == SHORT`). No migration.
  Deploy: `docker-compose.stack.yml` + `config/cloudrun/execution-engine.yaml`
  (default empty); the infra repo env must mirror it (default empty) for go-live.

### Added — FIFO-ledger-vs-broker position reconciliation (PL-2, 2026-06-27)

- The reconciliation worker compared the persisted `positions` table against the
  broker — but that table is overwritten with the broker snapshot every cycle, so
  it could not catch our own books diverging from the broker. It now also runs an
  INDEPENDENT check: `classify_fifo_position_drift` compares the position derived
  from the fill ledger (`PnLService.get_fifo_positions`, signed FIFO over filled
  `pending_orders`, no price needed) against the broker's reported position.
- Drift is `warn`-level — recorded to `risk_breaches` and alerted
  (`reconciliation_position_drift`) for a human, but does NOT open the circuit
  breaker. Captured before the broker-mirror overwrite; the FIFO ledger is scoped
  to the reconciled broker + environment (so another broker's or paper's fills
  don't pollute the comparison) and run once per (user, broker, environment) per
  cycle (account-level, matching the broker's account-level positions);
  best-effort so a P&L-compute error never aborts a cycle. Tolerance via
  `EXECUTION_RECON_POSITION_DRIFT_TOLERANCE`. Broker *realized* P&L is not
  reconciled — no live broker reports it (it would only be computed-vs-0). No
  migration (reuses `risk_breaches`). Opt-in: without a wired `pnl_service` the
  worker behaves exactly as before.

### Fixed — live realized P&L computed per (strategy, symbol) from the fill ledger (PL-1, 2026-06-27)

- In live mode no broker reports account realized P&L, so `broker_bridge` returned
  `realized_pnl=None` and `ExecutionMetricsStore` carried the first snapshot's value
  forward — every live metrics row froze at the same realized P&L, starving the
  feedback loop / `ModePerformance` mode ranking of real live returns.
- `ExecutionMetricsStore` partitions metrics by (user, strategy, symbol) and diffs
  the realized P&L it is handed against that partition's previous row. So
  `ExecutionPersistence` now feeds each partition its OWN cumulative FIFO realized
  total via `PnLService.partition_realized_pnl` (a synchronous realized-only sum
  over that partition's filled `pending_orders`, no current price). DB-gated:
  no-DB callers keep the account-state value, so golden snapshots are unchanged.
- This fixes the live-frozen value AND the pre-existing per-partition corruption an
  account-wide figure would cause (one symbol's P&L booked against another;
  first-snapshot inflation). No new table/migration; reuses `_calculate_fifo_pnl`.

### Fixed — retry transient live blocks instead of dedup-suppressing them (RG-2, 2026-06-27)

- Four live short-circuit sites (account-state unavailable/stale, market-data
  unavailable/stale, risk-baseline unavailable) emitted `execution_mode='blocked'`,
  which `is_retryable_failure` treated as terminal — so the dedup row went `failed`
  and the outbox relay silently dropped the retry for the whole TTL even though the
  dependency was only momentarily unavailable.
- `ExecutionResult` now carries an optional `block_reason` (None-omitted in
  `to_dict`, so serialized/golden shapes are unchanged), and `is_retryable_failure`
  retries a `blocked` result only when its reason is in `RETRYABLE_BLOCK_REASONS`
  (the transient infra reasons). Safe-by-default: any unrecognized/missing reason —
  every deterministic policy/gate rejection — stays terminal; `dedup` is always
  terminal. The freshness helpers now return `(block_reason, message)`. No schema
  change; the paper path is unaffected.

### Added — deterministic backtest harness: engine + walk-forward + optimization (Phase 4, 2026-06-27)

- New `lib_strategy.backtest`: a reproducible `BacktestEngine` that drives a
  `PureSignalStrategy` over fixed bars through the SAME production components (the
  real `BarConsolidator`, period-CLOSE stamping, + `strategy.run()`), a
  no-lookahead `FillSimulator` (signal at a bar close fills at the next bar open),
  and a numpy-free metrics module (return/CAGR/Sharpe/Sortino/maxDD/win-rate/PF).
  No DB, no clock, no RNG → byte-reproducible, lockable as golden.
- `run_walk_forward` (rolling-window robustness; per-window reports + aggregate)
  and `grid_search` / `monte_carlo_search` (seeded, reproducible) over a
  consolidate-once seam; objectives sharpe (default) / sortino / total_return /
  calmar. `BacktestResultStore` persists a report to the existing
  `backtest_results` table.
- Frozen REAL-data fixture (`tests/fixtures/market_data/`, 1501 Coinbase BTC-USD
  1m candles) anchors the golden tests, per the real-data-not-simulations rule.
- Fixed two replay-determinism bugs: the canonical-signal replay defaulted
  `source_prefix` to the retired `lean:coinbase` (→ `coinbase_live`), and
  `verify_pipeline_e2e` consolidated bars at the period START via a bespoke
  reimplementation (→ the production `BarConsolidator`, period CLOSE).

### Added — pluggable operational alert sinks: Email / Telegram / webhook (2026-06-27)

- New `lib_common.alerting`: `Alert` + `AlertSink` protocol + `WebhookSink` /
  `TelegramSink` / `EmailSink` + a best-effort, off-thread `MultiSinkAlertPublisher`
  and `build_sinks_from_env` (a sink activates only when its `ALERT_*` vars are
  set). The execution `AlertPublisher` delegates to it (interface preserved),
  adding Telegram + email alongside the legacy webhook. SMS/WhatsApp need a paid
  provider (Twilio) and are a single sink class away. Pipeline-wide Prometheus
  alert *rules* remain in the infra monitoring stack.

### Fixed — idempotent asset-score writes on signal identity (SC-6, 2026-06-27)

- `upsert_score` INSERTed a fresh `asset_scores` row on every computation, so an
  idempotent re-POST of the same signal (NOTIFY redelivery / worker restart) left
  duplicate rows for the same `(instrument, ts)` — table bloat, and the SC-4 boot
  warm (`recent_asset_alpha_history`) double-counted the duplicated `alpha_raw`,
  skewing the restored rolling mean/std.
- `AssetScore` now carries the originating `Signal.external_signal_id`
  (nullable, unique-indexed; migration `0035`, drift 0), and the asset write is
  idempotent on it (query-then-upsert, mirroring `add_signal`): a re-delivered
  signal updates its row; distinct same-bar signals from different strategies keep
  separate rows; an identity-less signal falls back to insert (NULLs stay distinct).

### Added — promotion acceptance-criteria go-live gate (Phase 5, 2026-06-27)

- `docs/DEPLOYMENT.md` consolidates the promotion gate: the automated CI gates
  (audit/lint/types/tests, schema-drift 0, e2e integration, sandbox smoke), the
  local Docker e2e, and the 14-day paper soak — with the concrete signals to
  watch (feedback heartbeat, market-data freshness, outbox backlog, execution
  success-vs-no_op, dup-submission 0, a live alert sink) before flipping
  `EXECUTION_ENGINE_ALLOW_LIVE`.
- The 14-day soak signals are now **programmatically enforced**, not eyeballed:
  new `lib_application.services.soak_acceptance` (`check_soak_acceptance` →
  per-signal `SoakReport`) queries the live DB (`service_heartbeats`, `prices`,
  `outbox_events`, `execution_logs`) + `ALERT_*` env into one pass/fail verdict,
  exposed by `scripts/check_soak_acceptance.py` (exit 0/1, `--json`). The
  duplicate-submission check is canonical signals executed >1x in `execution_logs`
  (the execution-grain SC-1/EX-3 risk; decision-grain dedup is already enforced by
  `execution_decision_logs.idempotency_key UNIQUE`). The alert check verifies
  alerts are actually *deliverable* — a sink is configured AND
  `EXECUTION_ALERTS_ENABLED=true` — so a configured-but-disabled sink can't pass
  the gate while breaker/staleness alerts silently go nowhere.
- `write_sandbox_certification_marker.py` now **requires** that passing report
  (`--acceptance-report`) for a `passed` marker and embeds it — certification is
  refused while any signal is red, closing the soak→go-live loop.

### Added — cross-strategy ensemble gating score, default-off (SC-asset, 2026-06-26)

- The gating asset score now optionally blends the asset's recent *one-per-strategy*
  signals through the existing layer-3 ensemble (a true cross-strategy alpha
  combination) instead of scoring only the lone incoming signal. The pipeline already
  aggregated across strategies; ingest just never fed it more than one signal.
- Gated behind `SCORING_CROSS_STRATEGY_ENSEMBLE` (**default false**). With it off, the
  gather short-circuits to `[incoming]` without even reading the store, so
  single-strategy gating is byte-identical to the prior path — verified by the
  SwingHighLowPMO e2e (24 tests) and the full scoring suite. Siblings are admitted by a
  two-sided freshness window (`SCORING_SIBLING_FRESHNESS_SECONDS`, default 3600s),
  deduped to the latest per other strategy, capped at `SCORING_MAX_SIBLINGS` (16), and
  neutral/CLOSE siblings are dropped so they cannot dilute the live signal's magnitude.
  Multi-strategy blends standardize against a separate `xstrat:<asset>` rolling-stats
  namespace so enabling the flag cannot poison the single-strategy window.
- `signal_from_record()` is now the single source of truth for record→Signal
  reconstruction (shared by the dispatch path and the gather); malformed/unknown rows
  reconstruct to `None` and are skipped, never aborting an ingest.
- NOTE: the flag must NOT be flipped on until the follow-up execution-dispatch hardening
  (direction-conflict handling when the net ensemble disagrees with the incoming entry)
  lands. Score-side only for now.

### Fixed — CI lint/type via pinned pre-commit hooks (2026-06-25)

- The consolidated ``ci.yml`` ran hand-rolled **unpinned** ``ruff check .`` + ``mypy``,
  which diverged from the pinned pre-commit hooks: a newer ruff flagged latent
  ``UP042``/``PLW0108``, ``ruff check .`` linted ``strategies/`` (pre-commit excludes
  it), and ``mypy frameworks/python/`` / ``mypy strategies/`` errored on empty/no-py
  dirs — failures the team's pre-commit (changed-files + excludes) never sees. CI now
  runs the **same pinned hooks** (``pre-commit run ruff ruff-format mypy --all-files``;
  ruff 0.14.4, mypy 1.18.2, identical excludes/stubs) so CI ≡ pre-commit by
  construction. Also added ``strategies`` to ``[tool.ruff] exclude`` so direct
  ``ruff check .`` matches pre-commit. (The former ``type-check.yml`` had the same
  unpinned ``ruff check .`` and was already red; consolidation made it the visible
  gate.) Verified: all three hooks pass ``--all-files`` locally.
- Once lint passed, the tests step surfaced a second latent issue: ``ci.yml`` +
  ``pyproject`` ``testpaths`` both listed ``libs/python/lib_application/tests``,
  which has **zero tracked test files** (absent from a fresh checkout) → pytest
  exit 4 (path not found). Removed the dangling path from both. Verified: the CI
  pytest command runs 507 passed, 53% coverage (≥35% gate). (lib_application
  having no unit tests is a separate, pre-existing coverage gap.)
- Closed an audit-coverage gap the consolidation introduced: folding ``vmdev
  audit`` into the path-gated ``python-ci`` meant docker-only / workflow-only PRs
  skipped it (the former standalone ``audit.yml`` ran unconditionally), so a
  Dockerfile python base bump could escape the ``docker-python-base-drift`` gate.
  Added ``docker/**`` + ``.github/**`` to the ``python`` paths-filter so the gate
  runs on those changes. Also added a Dependabot ``ignore`` for the docker
  ``python`` base image (major+minor) — the 3.11→3.14 bump has been merged+reverted
  twice (#1, #6) and recreated as #7; Python is bumped deliberately alongside
  ``dependencies.yaml`` + a wheel re-lock, not by an automated base-image bump.
- Third latent issue: the CI test job's hand-listed pip deps were missing
  ``psutil`` (process_manager), ``cryptography`` (Coinbase CDP-JWT auth), and
  ``google-cloud-secret-manager`` (the GCP secrets-provider tests import
  ``google.api_core`` at collection time AND ``google.cloud.secretmanager`` at
  runtime). Added them. Verified by running the **full** suite in a clean venv
  matching the exact CI dep set: 507 passed, 0 failed, 53% coverage (the
  ``--collect-only`` pass alone missed the runtime ``secretmanager`` usage).

## [1.2.0] - 2026-06-25

### Fixed — execution dedup tz-comparison 500 (surfaced by the Docker e2e)

- ``ExecutionDeduplicator._claim_database`` compared the tz-aware
  ``ExecutionDecisionLog.ts`` (the column became ``DateTime(timezone=True)`` in
  migration 0029) against a tz-naive ``stale_cutoff``, raising "can't compare
  offset-naive and offset-aware datetimes" and 500-ing ``/execute-command`` on
  Postgres. The in-process / SQLite e2e masked it (SQLite reads the column back
  naive); the full **container-on-Postgres** e2e surfaced it. Fix: normalize the
  DB-read ``ts`` to naive-UTC before the comparison (SQL filters already run under
  the UTC session; SQLite unaffected). Verified live: ``/execute-command`` now
  processes cleanly — the command is claimed, handled, and (for a stop-loss-less
  synthetic signal) correctly risk-blocked by RiskGuard rather than crashing.

### Added — scaling & hardening roadmap doc (Phase 3, 2026-06-25)

- New ``docs/SCALING.md``: the trigger-gated scale ladder (Managed Postgres/HA,
  indicator-runner sharding, App Platform, broker, market-making data plane), the
  Postgres **RLS + non-superuser app role** design (the deferred H-8 deploy-blocker —
  why it can't be validated under the current superuser connection, and the staged
  rollout to land before multi-tenant real money), and the scoped decisioning-
  evolution backlog (feedback→weight loop, abstain/quorum gates, calibrated
  classifier, async enrichment hooks, Greeks/slippage slots). Linked from the Key
  Documentation tables; DEPLOYMENT.md doc-table wording corrected GCP → DigitalOcean.

### Added — price-based market-context provider (Phase 2, 2026-06-25)

- New ``PriceBasedMarketContextProvider`` (scoring Layer 2) derives the real market
  regime (bull/bear × quiet/volatile, sideways, crisis) + realized volatility from
  recent ``prices`` history. It initially replaced the earlier constant-fed path.
  The later fail-closed hardening made this the unconditional production path;
  its window is configured with ``SCORING_MARKET_CONTEXT_WINDOW``. The computed
  regime/vol flow into both the meta scorer
  and the decision-context provenance snapshot. +5 tests. This activates the first
  of the wired-but-default-fed scoring seams (populate, not re-architect).

### Added — decision-context provenance (2026-06-25)

- New immutable ``decision_contexts`` table
  (``lib_application.db.models.provenance.DecisionContext``) + Alembic migration
  ``0030_decision_contexts``. A point-in-time snapshot of every scoring decision
  (market regime / volatility / liquidity / news slots, model + pipeline version,
  per-factor score contributions, stale-input flags, abstain outcome, and the full
  feature snapshot) written on the **same unit of work** as the signal + scores, so
  every decision is replayable and auditable against exactly the inputs that
  produced it — provenance that cannot be reconstructed after the fact. Persisted
  via ``ScoreStore.persist_decision_context`` (no-op on the in-memory store; gated
  by ``SCORING_PERSIST_DECISION_CONTEXT``, default on). Additive only — the scoring
  math and execution path are unchanged. Schema-drift gate held at 0.

### Changed — DOCR 2-version retention + deploy-doc truth-up (2026-06-25)

- DOCR retention tightened to **2 SemVer versions per image repo** (the running
  version + one prior, for single-step rollback) + a < 7-day deploy-safety window
  (was 5 versions + 30 days): ``infra:registry-cleanup.yml``. Deeper rollback =
  rebuild from the immutable git tag.
- ``build-and-push.yml`` no longer pushes the movable ``X.Y`` tag — the deploy
  pins the exact ``X.Y.Z`` and an extra floating pointer is just an unnecessary
  tag under 2-version retention; images now get ``X.Y.Z`` + ``sha-<commit>`` only.
- Doc truth-up: README "GCP Cloud Run for production" → DigitalOcean (Droplet now,
  App Platform at go-live; Cloud Run manifests are reference-only); DEPLOYMENT.md
  retention 5→2; RUNBOOK.md notes ``feedback_loop_engine`` is a scheduled one-shot;
  ``config/containers.yaml`` documents the deliberate per-service image strategy
  (independent scaling/release; not consolidating to one command-switched image).

### Changed — tag-first CI consolidation (2026-06-25)

- Folded the duplicate ``audit.yml`` + ``test-coverage.yml`` + ``type-check.yml``
  workflows into the single path-filtered, cancel-in-progress ``ci.yml`` gate,
  which now also runs on **push to main** (it was PR/merge_group-only, so it never
  ran for the direct-to-main workflow). ``python-ci`` now runs vmdev audit + ruff +
  mypy + tests/coverage in one job. Dropped ``integration.yml``'s nightly cron (the
  per-push run already covers it — the daily run was pure Actions-minutes +
  Coinbase-quota burn at pre-revenue). ``coinbase-sandbox-smoke.yml`` now runs only
  via ``workflow_call``/``workflow_dispatch`` (it was double-billing per PR:
  standalone + nested under build-and-push). Added ``cancel-in-progress`` to
  ``drift-check.yml``. Dependabot weekly → monthly. Image build/push stays
  SemVer-tag-gated. ``main`` is unprotected, so no required-check names are
  orphaned by the deletions.

### Changed — execution readiness gates on the database (2026-06-25)

- ``execution-engine`` ``/ready`` now runs a ``SELECT 1`` DB probe
  (``_execution_db_ready``) in addition to reconciliation health, matching the
  scoring/feedback readiness probes (G14): an instance whose DB pool is dead
  reports not-ready (503) instead of accepting ``/execute-command`` traffic it
  cannot dedup or persist (orders, dedup claims, decision logs all require the
  DB). +3 readiness tests. No-op when no session factory is configured (paper rig
  without a database).

## [1.1.2] - 2026-06-25

- Made Alembic import the installed `lib_application` model package instead of
  relying on a source-tree path, so schema migration uses the exact wheel
  shipped in the migration image.

## [1.1.1] - 2026-06-25

- Added the canonical migration and seed assets to the scoring image used by
  the `db-migrate` one-shot.

## [1.1.0] - 2026-06-25

- Installed `lib_data` in the execution, feedback, and market-data runtime
  images so their declared production imports are present.

### Changed — purge legacy vm-strategies identity + sync-hygiene instructions (2026-06-24)

- Renamed the residual legacy identity repo-wide: `vm-strategies` / `vm_strategies` /
  "VM Strategies" → `platform` / vynmatrix across setup.py, docstrings, scripts,
  docs, the Cloud Run `managed-by` labels, the docker-compose network
  (`vm-strategies` → `vm`), the plugins entry-point prefix (`vm_strategies.plugins`
  → `vynmatrix.plugins`), the GCP-project default, and the `~/.vm-strategies`
  ML cache dirs (`~/.vynmatrix`). Source tree is now free of the legacy name.
- CLAUDE.md / AGENTS.md: added Core Principles 9 (keep code/config/scripts/docs in
  sync on every change; sweep for stale references after refactors, log to CHANGELOG)
  and 10 (verify the end-to-end flow in the local Docker stack before promoting).
- Removed the dead `docs/tickets/` implementation backlog (historical; superseded).

### Changed — tag-gated DOCR builds + DigitalOcean Droplet deploy (2026-06-24)

Architecture-consolidation review follow-through (build/deploy land on
DigitalOcean; supersedes the interim per-merge `:$SHA` → GCR model below):

- **Tag-gated DOCR build.** `build-and-push.yml` now builds + pushes images ONLY
  on SemVer `v*.*.*` tags (PRs build to validate, no push), to the DigitalOcean
  Container Registry tagged `X.Y.Z` / `X.Y` / `sha-<commit>`. New `vmdev release`
  command normalizes the partner scheme (`V1_1p0` → `v1.1.0`) and tags `main`.
- **DigitalOcean deploy (infra repo).** Phase-1 single-Droplet `docker-compose`
  topology (`docker-compose.droplet.yml` + `DROPLET.md`): relay inline in
  scoring, `execution-engine` isolated in its own container, `feedback` as a
  systemd-timer job, per-service resource limits. `release.yml` (production-gated
  SSH deploy) + `registry-cleanup.yml` (keep latest 5 SemVer/repo + GC). App
  Platform (`app.yaml`) documented as the Phase-2 step.
- **Config reconcile.** `config/deployment/{production,staging}.yaml`:
  `secrets.source` → `env_vars` (DO injects env secrets); `live_mode` clarified
  as the indicator worker-restart flag (not the money switch, which is
  `EXECUTION_ENGINE_ALLOW_LIVE`); the six `config/cloudrun/*.yaml` banner as
  REFERENCE-ONLY (GCP retired).

### Added — optional tracing hook + dedup stale-skip metric (2026-06-24)

- `lib_common.observability.init_tracing()` — no-op OpenTelemetry bootstrap
  (active only when `OTEL_EXPORTER_OTLP_ENDPOINT` + the optional otel deps are
  present), wired into the scoring + execution entrypoints.
- `vm_execution_dedup_stale_executing_skipped_total` counter so a stale
  "executing" dedup claim (a possibly-dropped retry) is alertable, not only
  logged. Claim logic unchanged.

### Changed — split build/deploy CI; Phase 2-3 cleanup (2026-06-23)

Readiness-audit cleanup across Phases 0–4 plus the tag-gated release pipeline
(owner decisions: release on `v*.*.*` tags, deploy workflow lives in the `infra`
repo):

- **Build/deploy split.** `.github/workflows/build-and-deploy.yml` →
  `build-and-push.yml`: it now only builds + pushes immutable `:$SHA` images;
  the three Cloud Run deploy jobs moved to the **infra** repo
  (`release.yml` + reusable `_deploy.yml`), gated through `staging-paper` →
  `production` GitHub Environments with migrate-before-serve. Merging to `main`
  no longer ships to prod. `validate_cloudrun_contracts.py` now enforces that the
  platform workflow contains no `gcloud run` deploy step.
- **tz-aware outbox timestamps.** The 9 naive `DateTime` columns on
  `outbox_events` / `pending_orders` / `execution_decision_logs` →
  `DateTime(timezone=True)` + migration `0029` (drift check stays 0).
- **IBKR session hardening.** The market-data client now calls `POST /tickle`
  before each fetch (keepalive + auth check) so an expired Client Portal session
  fails loud instead of returning empty data; removed the dead
  `IBKR_TWS_PORT` / `IBKR_ACCOUNT_ID` env vars (the adapter uses the Client
  Portal gateway, not TWS).
- **Config/doc reconcile.** Corrected the (already-implemented) per-policy
  mode-selection docs; `production.yaml`/`staging.yaml` stale `signal-api` URL →
  scoring-engine, `replicas` 2→1, `ml.enabled` → false; dropped stale LEAN/ML
  strategy-count wording.

### Fixed — ml_runner startup crash + image PYTHONPATH; lib_data test collection (2026-06-22)

A completeness audit (independent multi-agent re-verification of the whole
review backlog) confirmed the remaining non-infra items:

- **`ml_runner` was unstartable.** `apps/ml_runner/ml_runner/process_manager.py`
  lacked `from __future__ import annotations`, so the dataclass field
  `command_queue: Queue | None` (where `multiprocessing.Queue` is a factory
  *method*, not a class) raised `TypeError` at import — crashing any import of
  `ml_runner`. Introduced by the ruff-UP (PEP 604) modernization. Added the
  future import. Also aligned the two ML **base** images' `PYTHONPATH` to
  `/app/ml_runner` (the package lives at `/app/ml_runner/ml_runner`, so
  `PYTHONPATH=/app` couldn't resolve `python -m ml_runner.main`) — matches the
  already-correct bundle image. Verified in-image: `ml_runner.main` resolves and
  imports cleanly. (`rl_runner`/`agent_runner` have no process_manager;
  `indicator_runner` uses `subprocess.Popen`, a real class — no bug.)
- **`lib_data` tests couldn't be collected locally.** `lib_data` is in pyproject
  `testpaths` but, alone among the libs, had no `tests/conftest.py` to put its
  source on `sys.path` — `pytest libs/python/lib_data` failed with
  `ModuleNotFoundError` unless PYTHONPATH was pre-set (CI sets it explicitly).
  Added the conftest; tests now collect with no PYTHONPATH.

The audit otherwise confirmed all C/H/M findings resolved; the only deferred
items are the three that require non-local validation (RLS role/staging, CI
branch-protection lockstep, buildx GHA cache).

### Fixed — TensorFlow image lib-gap + lazy google-cloud in lib_common (2026-06-21)

The TF base image installed only `lib_strategy`/`lib_ml` with `--no-deps`, so
`ml_runner.main` (its CMD) could not import `lib_common` — the image was
unstartable (M-23). `lib_common` could not simply be added because its
`google-cloud-*` deps force `protobuf>=4`, and `app/secrets.py` imported
google-cloud at module scope, so any `lib_common.app` import pulled it in and
collided with TensorFlow's `protobuf<5`.

- `lib_common/app/secrets.py` now lazy-imports google-cloud (consistent with
  `event_bus.py`) — `lib_common.app` imports cleanly with google-cloud absent.
  Verified in a clean container with no `google` package installed.
- The TF image installs `lib_common` + `lib_data` (+ existing libs) with
  `--no-deps` and lib_common's protobuf-neutral runtime deps explicitly
  (python-dotenv, pydantic, fastapi, httpx, prometheus-client, structlog),
  keeping `protobuf>=3.20.3,<5`. Built + verified: TF 2.16.1 + protobuf 4.25.9
  + `lib_common.app` + lib_strategy + lib_ml coexist; google-cloud stays absent.
- Updated 3 `test_secrets` mocks to patch the real google source (the lazy
  target). 82 lib_common tests + e2e green.
- Discovered (separate, pre-existing, chipped): `ml_runner` itself can't start
  in either ML image — `process_manager.py` has a `Queue | None` class
  annotation that evaluates at import, and the image's `PYTHONPATH=/app` doesn't
  match the package at `/app/ml_runner/ml_runner`.

### Changed — ml-base-core multistage build drops the toolchain (2026-06-21)

`ml-base-core` shipped its build toolchain (`build-essential`, `gfortran`,
`libopenblas-dev`, `liblapack-dev`) in the runtime image (M-22). numpy/scipy/
sklearn/statsmodels/hmmlearn all install from manylinux wheels (bundled
OpenBLAS), and the only source build is `psutil` on arches without a wheel
(e.g. aarch64). Split into a `builder` stage (with the toolchain) and a slim,
toolchain-free `runtime` stage that copies the built `/install` prefix.

- Measured: 6.03GB → 5.59GB (−440MB, ~7%); runtime imports verified
  (numpy 1.26.4 / scipy 1.12.0 ABI-consistent, libs import) and no compiler in
  the runtime image.
- Deps + platform wheels install in a single pip resolution so the pinned numpy
  stays consistent (separate `--prefix` installs re-resolved numpy via the
  wheels' transitive deps and broke scipy's ABI).
- Note: the image is still torch-heavy (a `lib_ml` dependency pulled into the
  non-deep-learning core image) — a separate dependency-graph concern.

### Changed — moved CandleRow + normalize_product_symbol to lib_data (2026-06-21)

Fixed a dependency-direction inversion (M-1): `lib_application`'s
`price_ingestion_service` imported `CandleRow` and `normalize_product_symbol`
from `lib_infrastructure.market_data` — an adapter layer — directly
contradicting its own `setup.py` ("NO infrastructure dependencies!"). Both
symbols are pure, dependency-free data helpers, so they moved to
`lib_data.market_data` (the neutral data layer that `lib_strategy`,
`lib_infrastructure`, and the apps already sit above).

- New `lib_data/market_data.py` holds `CandleRow` + `normalize_product_symbol`;
  removed from `lib_infrastructure/market_data/models.py` (and its package
  `__init__`). `IngestionSummary` + `GRANULARITY_TO_TIMEFRAME` stay (ingestion-specific).
- Updated all 9 consumers (scoring engine, feedback loop, market-data ingestor,
  the application price-ingestion service, and the infra Coinbase client) to
  import from `lib_data`; declared `lib-data` on `lib_application`
  (setup.py + build.yaml).
- Pure relocation: mypy + ruff clean, 104 scoring/feedback/ingestor + 120
  execution tests, audit and e2e green.

### Fixed — currency-aware daily NAV (2026-06-21)

`PnLService.persist_daily_nav` hard-coded `nav_ccy="USD"`, mislabeling NAV for
EUR/INR tenants (M-14). It now resolves the currency from `User.base_ccy`
(falling back to USD) or an explicit `nav_ccy` argument denoting the currency
the `equity` is actually in. Because no FX layer is wired yet, when the recorded
currency differs from the user's base currency it logs a guard warning rather
than silently producing a cross-currency NAV. Existing-row upserts now update
`nav_ccy` too. The e2e records its USD-quoted equity as `nav_ccy="USD"`. Added
`test_persist_daily_nav_uses_user_base_currency`.

### Fixed — instrument resolution maps symbol variants to one instrument; feedback skip vs error (2026-06-21)

Investigating the local e2e feedback gap (`signals_evaluated:0, errors:29`)
revealed the real cause was **not** a price-timeframe fallthrough but a
duplicate instrument: signals resolved a `BTCUSD` symbol to a second instrument
row while the prices (and the seed alias `BTCUSD`→`BTC/USD`) lived under the
canonical one, so every exit-price lookup missed (M-13).

- `AppScoreStore._resolve_instrument` / `resolve_instrument_id` now fall back to
  a normalized-symbol match (`normalize_product_symbol`: `BTC-USD` / `BTC/USD` /
  `BTCUSD` → one form) before creating a new instrument, so exchange/broker
  symbol-format variants resolve to the same instrument instead of spawning
  duplicates that fragment prices and scores across `instr_id`s — important for
  the multi-broker path where each venue sends its own symbol spelling.
- `FeedbackLoopEngine.run_evaluation_cycle` now counts a missing price as
  `skipped_no_price`, separate from `errors`, so a routine skip (exit horizon
  still in the future, or no bar covers the window) no longer masks a real
  fault. Demoted the per-signal log from `warning` to `debug`.
- Added `test_instrument_resolution.py` (variants resolve to one instrument;
  distinct pairs don't collide). With the local stale duplicate cleaned up, the
  e2e feedback stage now evaluates 29 signals (15 correct / 14 wrong, 0 errors).

### Added — short-TTL cache for `list_bindings` (2026-06-21)

`AppScoreStore.list_bindings` is read on every scored signal
(`evaluate_bindings`) but the binding set changes only on admin writes, so each
signal re-queried `user_strategy_bindings` and rebuilt the full DTO list
(instrument/sector/sizing maps included). Added a short-TTL in-process cache
(H-15):

- Default 5s TTL via `SCORING_BINDINGS_CACHE_TTL_SECONDS` (set 0 to disable).
- A store write (`add_binding`) invalidates the cache immediately via the new
  public `invalidate_bindings_cache()`; out-of-band writes (raw SQL, another
  replica) are reflected within the TTL — acceptable for rarely-changing config.
- Added `test_bindings_cache.py` (cache hit within TTL, write invalidation,
  TTL=0 disables). The e2e harness, which rewrites bindings via raw SQL between
  strategies, now calls `invalidate_bindings_cache()` after its seed (the
  documented contract for non-`add_binding` writes).

### Removed — redundant `OptionSpread.net_cost` alias (2026-06-21)

Deleted the backward-compat `net_cost` alias on the `OptionSpread` TypedDict
(M-7), which duplicated the canonical `net_debit`. The only consumer
(`options_builder._to_spread_result`) is fed exclusively by `build_spread`,
whose output always sets `net_debit`, so the `net_cost` read was a dead
fallback. Removed the field, the redundant `"net_cost": net_debit` write in
`_spread_result`, the dead fallback, and the alias assertion in the test.
`VerticalSpread.net_cost` (the primary, live field for that shape) is
unchanged. Per the repo no-deprecation-cycle policy: delete aliases, don't
annotate them.

### Fixed — dedup pending-orders fill query + batch N+1 mode-perf load (2026-06-21)

- **M-5:** `PnLService._get_trades` and `StrategyMetricsService._get_completed_trades`
  built the same "filled `pending_orders` for user (+optional strategy)" predicate
  by hand, differing only in optional symbol/mode/date narrowing. Extracted a
  shared `metrics/pending_orders.py::filled_orders_query` builder so the realized-fill
  predicate can't drift between the P&L and strategy-metrics consumers.
- **M-21:** `ScoreEngine.evaluate_bindings` called `select_best_mode` (→ a
  `list_mode_performance` DB query) once **per binding**, even though every binding
  in one evaluation shares the same `score.target` + horizon — an N+1. The
  mode-performance list is now loaded at most once per evaluation and ranked in
  memory per binding via a new pure `_rank_mode_performance` helper; `select_best_mode`
  keeps its public signature (delegates to the same helper).
- Behaviour-preserving: 73 scoring + 119 execution-engine tests, audit and e2e green.

### Changed — single-source broker spec registry (2026-06-21)

Collapsed the per-broker definitions into one `BROKER_SPECS` source of truth
(H-9). A broker was previously declared across three hand-maintained mappings
that silently drift: the `register_default_adapters` try/except list (factory
code → adapter class), the `BROKER_CAPABILITIES` alias dict (factory-code
aliases like `interactive_brokers`/`delta_exchange`), and the execution-engine
`_FACTORY_BROKER_CODES` bridge map (`BrokerType` → factory code).

- Added `lib_infrastructure/brokers/capabilities.py::BrokerSpec` +
  `BROKER_SPECS`, pairing each broker's canonical code (`BrokerType.value`),
  factory registration code, adapter module/class, and capability matrix.
- `register_default_adapters` now iterates `BROKER_SPECS` (one lazy `import_module`
  loop, still tolerating an absent optional SDK) instead of five copy-pasted
  try/except blocks; `BROKER_CAPABILITIES` and the bridge's
  `_FACTORY_BROKER_CODES` are both derived from it.
- Adding a broker is now one `BrokerSpec` entry (+ its capability matrix +
  adapter class + `BrokerType` member), not edits to three drift-prone maps.
- Pure centralization, zero behaviour change: the derived capability keys,
  factory registrations and bridge map are asserted byte-identical to the
  prior hand-written versions; mypy + ruff clean, 119 execution-engine tests +
  the broker-adapter suite pass, audit + e2e green.

### Changed — ExecutionEngine.handle_signal split into gate + execute phases (2026-06-21)

Continued the Sprint D `ExecutionEngine` decomposition (H-1). The
~620-line `handle_signal` god method now ends at the pre-trade gates; the
entire post-gating execution phase (broker connect → account/market fetch →
intent build + risk → order submit → aggregate/finalize/NAV snapshot) moved
verbatim to a sibling module `execution_engine/_execute.py` as
`execute_resolved_signal(engine, ResolvedDispatch)`, mirroring the existing
`_dispatch.build_dispatch_context` extraction.

- `engine.py` drops 1779 → 1510 LOC, restoring ~290 lines of headroom under
  the 1800 architecture LOC cap (clears M-19; the file was one feature away
  from breaching the audit gate).
- Behaviour-preserving: the resolved broker/mode/credential/circuit-breaker
  state is threaded through the frozen `ResolvedDispatch` dataclass; the
  `try/except` boundary and all short-circuit results are unchanged.
- Verified green: ruff + mypy clean, 119 execution-engine unit tests + 8
  golden snapshots pass identically, `vmdev audit` passes, and the e2e
  pipeline executes + fills both strategies end-to-end.

### Removed — OptionPremiumBreakdownShort (OPBS) strategy + NIFTY-options tooling (2026-06-15)

Retired the `OptionPremiumBreakdownShort` indicator strategy and its
self-contained NIFTY weekly-options backtest subsystem. OPBS was never wired
into `config/containers.yaml`, `config/build.yaml`, or the CI deploy matrix, so
removal is a no-op for the deployable fleet — the indicator inventory stays at
14 strategies across the `lean-tf-ma-crossover` and `lean-bt-must-have` bundles.

- Deleted `strategies/indicator/OptionPremiumBreakdownShort/`.
- Deleted the OPBS-only CSV backtest harness (`scripts/run_options_backtest.py`,
  `run_options_backtest_detailed.py`, `export_backtest_csv.py`) and the two
  NIFTY-options data fetchers (`fetch_truedata_options_data.py`,
  `fetch_nifty_options_data.py`).
- Deleted the now-orphaned TrueData market-data adapter
  (`lib_infrastructure/market_data/truedata_client.py`, with no remaining
  caller) and the dead option-symbol helper
  (`lib_strategy/canonical_symbols.py`); trimmed the `market_data` package
  re-exports accordingly.
- Deleted the associated tests (`tests/test_run_options_backtest.py`,
  `tests/test_truedata_options_fetch.py`, `tests/test_kite_options_fetch.py`)
  and neutralized the OPBS strategy_id used as a fixture in
  `test_signal_roundtrip.py` / `test_options_single_builder.py`.
- Removed the OPBS test path from `pyproject.toml`, the `TRUEDATA_*` block from
  `.env.example`, the NIFTY-options ignore rules from `.gitignore`, and the OPBS
  sections from `scripts/README.md`; refreshed the indicator inventory note in
  `CLAUDE.md` / `AGENTS.md`.

### Sprint G — Phase 4 contract tests + bare-except narrowing (2026-05-07+)

Phase 4 from the post-audit roadmap. Most of Phase 4 (outbox observability,
outbox failure-mode tests, vmdev CLI tests, ML E2E) was already done in
prior work — this commit closes the remaining items: RL/agentic contract
tests + a few more bare-except narrowing wins.

#### Added

- ``tests/test_rl_contract.py`` — 8 contract tests pinning the
  ``lib_rl`` public surface:
  - public-API export check (``InferenceStrategy``, ``PolicyManifest``,
    ``ActionMapping``, etc.)
  - ``ActionMapping`` discrete (0=HOLD, 1=LONG, 2=SHORT) + continuous
    threshold semantics
  - ``InferenceStrategy.predict`` produces a canonical
    ``Signal(strategy_type='rl')`` with policy metadata embedded
  - ``min_confidence`` gating coerces low-confidence predictions to HOLD
  - graceful HOLD return when the underlying policy raises
  - emitter wiring (the ``emit_signal=True`` path)
- ``tests/test_agentic_contract.py`` — 10 contract tests pinning the
  ``lib_agentic`` public surface:
  - public-API export check (``AgentPlanner``, ``GuardRails``,
    ``MarketContext``, ``PortfolioState``, ``AgentDecision``, etc.)
  - ``AgentDecision.to_signal`` carries LLM provenance
    (``model_id``, ``prompt_version``, ``token_usage``, ``latency_ms``,
    ``reasoning``, ``decision_type``)
  - ``GuardRails.check_pre_decision`` blocks symbols / max-positions /
    daily-loss limits
  - ``AgentPlanner.decide`` orchestrates prompt → LLM → parse and
    returns ``None`` when guard rails fire (instead of raising)
  - ``DEFAULT_TOOLS`` + ``get_tool_by_name`` exposes the
    ``EmitSignalTool``

  Both contract test files use stub policies / stub planners so they
  run without ``stable-baselines3`` / ``torch`` / ``onnxruntime`` /
  ``anthropic`` / ``openai`` installed.

#### Changed

- Bare-except narrowing (Plan §4.24): 3 more sites converted.
  - ``execution_engine/metrics/drawdown_service.py`` (×2 DB-update /
    DB-query wrappers) → ``except (SQLAlchemyError, OSError)``.
  - ``feedback_loop_engine/main.py`` price-provider construction
    fallback → ``except (ValueError, TypeError, OSError)``.
  - Audit baseline: 45 → 42.

#### Verified (no-ops in this sprint)

- Plan §4.23 (outbox observability — jitter, LISTEN/NOTIFY, metrics):
  already done. ``_failure_backoff_seconds`` does exponential backoff
  with ±20% jitter, ``_start_notify_listener`` is wired against
  ``outbox_events`` LISTEN, structured metric logs
  (``outbox.delivery_failed``, ``outbox.drain_summary``,
  ``outbox.listen_started``) emit ``attempts`` / ``max_attempts`` /
  ``will_dead_letter`` / ``dead_lettered`` for log-based metric
  backends.
- Plan §4.25 (ML E2E test): already exists at
  ``tests/test_e2e_ml_strategy_pipeline.py``. Sprint G adds RL +
  agentic contract tests to complete the trio.
- Plan §4.26 (outbox failure-mode tests): already exist at
  ``tests/test_outbox_failure_modes.py``,
  ``apps/scoring_engine/tests/test_outbox_relay_failure_modes.py``.
- Plan §4.27 (vmdev CLI tests): already exist at
  ``tools/dev_cli/tests/test_db.py``, ``test_user.py``,
  ``test_git_mr.py``.

---

### Sprint F — Phase 3 robustness aggregation + feedback ports (2026-05-07+)

Two slices of Phase 3:

1. The pure ``robustness_score`` aggregation that
   ``BacktestService.run_stress_test`` historically inlined now lives
   in ``StressTestRunner.summarize_results`` (a static method) so the
   math has its own tiny test surface.
2. Plan §6.9 (port introduction): a new
   ``ISignalPerformanceRepository`` port + SQLAlchemy adapter
   abstracts every ``StrategyConsecutiveWrongTracker`` /
   ``StrategyParameterFeedback`` access in
   ``feedback_loop_engine/engine.py``. The engine no longer reaches
   into ``lib_application.db.models`` for these tables and is testable
   against an in-memory stub.

#### Added

- ``StressTestRunner.summarize_results(scenario_results, baseline_metrics)``
  static method returning a ``RobustnessSummary`` dataclass
  (``scenarios_passed``, ``scenarios_failed``, ``worst_scenario``,
  ``robustness_score``). Pure function, no I/O.
- ``libs/lib_application/tests/test_stress_test_runner.py`` — 7
  regression tests covering empty input, no-baseline classifier,
  baseline-relative pass/fail rules, robustness-score penalty cap at
  30, floor at 0, and largest-absolute-impact tie-break for
  ``worst_scenario``.
- ``libs/lib_strategy/lib_strategy/ports/signal_performance_port.py``
  — new ``ISignalPerformanceRepository`` port with 6 methods
  (``update_consecutive_wrong_tracker``, ``has_pending_optimization``,
  ``link_feedback_to_tracker``, ``list_pending_suggestions``,
  ``approve_suggestion``, ``reject_suggestion``) + two value objects
  (``ConsecutiveWrongTracker``, ``PendingSuggestion``). Re-exported
  from ``lib_strategy.ports``.
- ``libs/lib_infrastructure/.../repositories/signal_performance_repo.py``
  — ``SQLAlchemySignalPerformanceRepository`` adapter. Constructor
  accepts either a session-factory callable or a bound ``Engine`` to
  match the feedback engine's existing wiring.
- ``apps/feedback_loop_engine/tests/test_signal_performance_port_integration.py``
  — 8 unit tests exercising the engine through an in-memory port stub
  (no DB connection required). Includes a hard regression that the
  engine's source no longer references ``StrategyConsecutiveWrongTracker``
  or ``StrategyParameterFeedback`` outside docstrings.

#### Changed

- ``BacktestService.run_stress_test``: inline 35-LOC robustness loop
  collapsed into a single ``StressTestRunner.summarize_results`` call.
  ``backtest_service.py`` LOC: 1176 → 1143 (−33).
- ``FeedbackLoopEngine.__init__``: optional
  ``signal_performance_repo`` constructor parameter (defaults to a
  lazily-constructed ``SQLAlchemySignalPerformanceRepository``).
- ``FeedbackLoopEngine`` 6 ORM-access methods now delegate to the
  port: ``_update_consecutive_tracker``,
  ``_has_pending_optimization``, ``_link_feedback_to_tracker``,
  ``get_pending_suggestions``, ``approve_suggestion``,
  ``reject_suggestion``. The remaining
  ``from lib_application.db.models import …`` lazy imports in the
  engine cover unrelated tables (``Instrument``, ``Strategy``); those
  are out of scope for Plan §6.9.

#### Decided

- Skipped ``IExecutionsRepository`` from the audit's recommendation —
  ``feedback_loop_engine`` has no caller that reads execution records
  directly. Adding a port without a real consumer would be
  dead-weight; the audit's own narrowing said "introduce ports
  selectively where coupling blocks tests/refactors."

---

### Sprint E — Phase 3 BacktestService decomposition (2026-05-07+)

Continuing Phase 3: extract the walk-forward orchestrator out of
``BacktestService``. The 165-LOC ``run_walk_forward`` god method was
the largest remaining responsibility on the service after Sprint D's
``handle_signal`` work; this commit lifts it into a dedicated
``WalkForwardRunner`` with its own focused test file.

#### Added

- ``libs/lib_application/lib_application/services/walk_forward_runner.py``
  — new ``WalkForwardRunner`` class. Owns window iteration (rolling
  vs anchored), best-params/best-metric tracking across windows,
  aggregate out-of-sample Sharpe + compounded-return calculation, and
  experiment lifecycle persistence. Heavy operations
  (``run_parameter_sweep`` / ``run_backtest`` / ``_resolve_signals``)
  are injected as callables so the runner has no compile-time
  dependency on ``BacktestService``. Ergonomic
  ``WalkForwardRunner.from_service(service)`` factory builds the
  runner from an existing service.
- ``libs/lib_application/tests/test_walk_forward_runner.py`` — 4
  regression tests pin the orchestration contract:
  - ``test_walk_forward_rolling_iterates_expected_windows`` — rolling
    mode advances train_start by step_days; loop terminates when
    test_end exceeds end_date.
  - ``test_walk_forward_anchored_grows_train_window`` — anchored mode
    keeps train_start = start_date and grows train_end by step_days.
  - ``test_walk_forward_aggregate_metrics_compound_returns`` —
    aggregate test return compounds per-window returns; Sharpe is
    averaged over windows with non-zero Sharpe.
  - ``test_walk_forward_persists_experiment_lifecycle`` —
    ``_create_experiment`` runs once with the right config dict;
    ``_finish_experiment`` runs once with the right summary keys.

#### Changed

- ``BacktestService.run_walk_forward`` reduced from 165 LOC to a
  9-line delegate that just constructs the runner and forwards. The
  ``# noqa: PLR0915`` (too-many-statements) suppression is no longer
  needed.
- ``backtest_service.py`` LOC: 1313 → 1176 (-137 LOC, -10.4%).

---

### Sprint D — Phase 3 ExecutionEngine decomposition (2026-05-07+)

First slice of Phase 3 from the post-audit roadmap: decompose
``ExecutionEngine.handle_signal`` (the orchestrator god method that
historically ran ~600 LOC of inline validation and short-circuit
logic) into named pre-execution gates. Each gate has a single
responsibility, a focused unit-test contract, and is locked in by
golden tests so future refactors cannot silently change observable
behaviour.

#### Added

- 4 new ``test_execution_engine_golden.py`` scenarios covering the
  short-circuit branches that lacked snapshot coverage:
  duplicate-signal dedup, ``HOLD`` no-op, ``NOTIFY_ONLY`` mode, and
  ``min_score`` threshold-block. Total golden coverage now: 8
  scenarios (was 4) — every short-circuit path in
  ``handle_signal`` has at least one snapshot.
- Three focused private helpers on ``ExecutionEngine``, all with the
  same ``Optional[str]`` (``None`` = pass, error message = block)
  contract so the orchestrator reads as a list of named stages:
  - ``_check_live_mode_gates`` — bundles the 5 live-mode preconditions
    (allow-live, per-user opt-in, risk-guard enabled, sandbox
    certification, reconciliation health).
  - ``_check_circuit_breakers`` — broker-global + strategy-scoped
    breaker checks with the existing recently-closed grace-period
    semantics.
  - ``_check_score_thresholds`` — ``min_score`` /
    ``min_sector_score`` user-configured gating.

#### Changed

- ``ExecutionEngine.handle_signal``: ~120 LOC of inline gate logic
  collapsed into 3 helper-method calls. Behaviour unchanged — pinned
  by all 8 golden tests + the 60-test ``execution_engine`` suite.

---

### Sprint C — Dead/deprecated code purge + Phase 3 prep (2026-05-07+)

The repo policy is "no deprecation cycles." Sprints A and B had staged
several pieces of code with ``DeprecationWarning`` re-exports for a
later removal cycle; Sprint C executes that removal in one pass and
removes the deprecation surface entirely. Going forward, anything that
would be marked "deprecated" is either deleted outright or kept and
documented as live.

#### Removed

- Entire legacy ``BaseStrategy`` chain:
  - ``libs/lib_strategy/lib_strategy/base.py``
    (``BaseStrategy``, ``StrategyMetadata``, ``StrategyType``, ``AssetClass``)
  - ``libs/lib_strategy/lib_strategy/full_strategy.py`` (``FullStrategy``)
  - ``libs/lib_strategy/lib_strategy/indicator_strategy.py`` (``IndicatorStrategy``)
  - ``libs/lib_strategy/lib_strategy/registry.py``
    (``StrategyRegistry``, ``RegistrableStrategy`` protocol)
  - ``lib_strategy/__init__.py`` ``_DEPRECATED_REEXPORTS`` map +
    ``__getattr__`` ``DeprecationWarning`` shim + ``TYPE_CHECKING`` block.
- ``libs/lib_application/lib_application/services/user_bindings.py``
  (the ``UserBindingsService`` / ``ModeSelector`` /
  ``BindingEvaluationResult`` legacy admin path). The live scoring path
  uses ``ScoreEngine.evaluate_bindings()`` directly. The canonical
  ``BindingEvaluationResult`` lives at
  ``lib_strategy.scoring.binding_evaluator``.
- ``apps/scoring_engine/scoring_engine/binding_adapters.py`` (DTO ↔
  domain ``UserBinding`` adapters; zero external callers).
- ``libs/lib_infrastructure/lib_infrastructure/persistence/sqlalchemy/session.py``
  (the ``create_session_factory`` / ``create_engine_only`` shim that
  delegated to ``lib_application.db.session``).
  ``apps/execution_engine/execution_engine/persistence.py`` migrated to
  ``lib_application.db.session.get_session_factory`` directly.
- ``lib_common.ExecutionMethod`` and ``lib_common.parse_execution_method``
  deprecated aliases (use ``ExecutionMode`` / ``parse_execution_mode``).
- ``lib_strategy.signals.payloads.GCPSignalPayload`` backward-compat
  alias (the live ``GCPSignalPayload`` Pydantic schema in
  ``apps/scoring_engine/scoring_engine/schemas.py`` is unchanged).
- ``ScoringUserBinding.threshold`` and ``ScoringUserBinding.min_sector_score``
  backward-compat property accessors.
- ``execution_engine_url`` deprecated parameter on
  ``LEANStrategyMixin.setup_signal_emitter`` (use ``signal_api_url``).
- ``libs/lib_common/tests/test_exports.py`` (entire file pinned the
  removed deprecated aliases).
- Misleading ``deprecated`` docstring on ``TradeSignal`` ORM —
  ``scripts/seed_sample_data.py`` actively writes to the table during
  local-DB seeding, so the docstring was wrong; rewritten to describe
  it as the legacy training-data table that the seed scripts use.

#### Changed

- ``AssetClass`` moved from ``lib_strategy.base`` (which is now
  deleted) to ``lib_strategy.types`` (its permanent home alongside
  ``ExecutionMode`` and friends). Re-exported from
  ``lib_strategy.__init__``.
- ``HttpSignalEmitter`` un-deprecated: it is the supported sync HTTP
  emitter for LEAN ``QCAlgorithm`` strategies and the RL inference
  wrapper. ``AsyncSignalClient`` covers async use cases but is not a
  drop-in replacement; the previous deprecation marker was aspirational
  rather than actionable. Updated ``tests/test_http_signal_emitter_capture.py``
  to drop the ``pytest.warns(DeprecationWarning)`` expectations.

#### Added

- 3 new ``test_sprint_b_consolidation.py`` cases pin the deletions:
  ``lib_strategy.base`` / ``full_strategy`` / ``indicator_strategy`` /
  ``registry`` are not importable; ``lib_application.services.user_bindings``
  is not importable; ``AssetClass`` is at its new home.

---

### Sprint B — Phase 2 consolidation (2026-05-07+)

Phase 2 from the post-audit roadmap: consolidate the deprecated paths the
codebase audit flagged so the next minor cycle can delete them in one
commit, and lock the consolidation in with new audit gates so future
regressions surface at commit time.

#### Added

- ``logger-canonical-drift`` audit rule (warning severity) — flags direct
  ``logging.getLogger`` calls outside ``libs/lib_common/lib_common/logging.py``.
  Allowlists: the canonical helper itself, ``vmdev`` CLI's rich-based
  logger, ``scripts/``, ``tools/reconciliation/``, and
  ``strategies/indicator/*/main.py`` (LEAN container entrypoints).
- ``tests/test_sprint_b_consolidation.py`` — 29 regression tests pinning
  the ``AssetClass`` relocation and the 27-site logger migration so a
  silent revert surfaces as a test failure.
- 2 new ``test_audit_command.py`` cases covering the new
  ``logger-canonical-drift`` rule and its scripts/tools allowlist.

#### Changed

- 27 modules across ``libs/`` + ``apps/`` migrated from
  ``logging.getLogger(__name__)`` to ``lib_common.logging.get_logger``
  (Plan §5.7, expanded scope). The canonical wrapper returns a
  ``_StructuredLoggerAdapter`` supporting keyword-style structured logs
  (``logger.info("msg", user_id=123)``); the migration gives every site
  the structured-logging surface for free.
  Migrated: ``lib_common.{retries,config_validation,shutdown,signal_client}``;
  ``lib_strategy.{config_loader,signals.emitter,signals.payloads}``;
  ``lib_ml.{promotion,validation,registry}``;
  ``lib_rl.{policy,inference}``; ``lib_agentic.{planner,tools}``;
  ``scoring_engine.{providers_db,pipeline,storage,alias_provider,
  services.meta_label_service,services.position_service,
  services.ensemble_service}``;
  ``execution_engine.{options_single_builder,futures_builder,
  position_sizer,order_builder,brokers.registry,brokers.paper}``;
  ``strategies/indicator/OptionPremiumBreakdownShort/core.py``.
- ``LogSignalEmitter`` (``lib_strategy.signals.emitter``) now returns the
  structured wrapper from the ``logger_name`` override path too, so the
  public API consistently produces structured loggers regardless of how
  the caller constructs the emitter.

#### Deprecated

- (Plan §6.1.4) ``AssetClass`` ripple-deleted from
  ``lib_common.config_validation`` + ``lib_common`` public API. Moved
  into ``lib_strategy.base`` (its only consumer, the deprecated
  ``StrategyMetadata.asset_classes`` field). The deprecated chain now
  owns its own types end-to-end so the next minor cycle can drop
  ``base.py`` / ``full_strategy.py`` / ``indicator_strategy.py`` in one
  commit. Historical aliases (``index_options`` → ``options``, etc.)
  preserved.

#### Verified (no-ops in this sprint)

- Plan items 10 (canonical session helper across 3 apps) and 12
  (process-manager loggers): already done in prior work — verified by
  inspection. ``execution_engine/main.py``, ``feedback_loop_engine/main.py``,
  and ``indicator_runner/signal_worker.py`` all use
  ``create_engine_for_env`` + ``get_session_factory`` + ``dispose_engine``.
- Plan item 11 (migrate 4 pipeline apps to ``ApplicationManager``):
  skipped per CLAUDE.md narrowing. The pipeline FastAPI services and
  ``market_data_ingestor``'s API surface keep ``uvicorn`` as primary
  process supervisor; only foreground worker loops use
  ``ApplicationManager``.
- Plan item 22 (shared ``lib_common.app.fastapi.create_service_app``):
  already exists and is consumed by all three FastAPI services —
  scoring, execution, feedback. No new code needed.

---

### Sprint A — production-readiness mechanical clean-up (2026-05-05+)

Delivered the items from the post-audit *Sprint A* checklist:
mechanical, high-signal cleanups that remove the last legacy
migration paths and bring repo metadata up to top-tier OSS-project
standards.

#### Added

- ``LICENSE`` (proprietary), ``CONTRIBUTING.md``, ``CODE_OF_CONDUCT.md``,
  ``CHANGELOG.md`` at the repo root.
- ``py.typed`` markers on every ``libs/lib_*`` so downstream type
  checkers see our type hints.
- ``--detailed`` flag on ``scripts/run_options_backtest.py`` (folded
  ``run_options_backtest_detailed.py`` in).
- pytest-cov coverage gate in CI: ``[tool.coverage.run]`` /
  ``[tool.coverage.report]`` config in ``pyproject.toml`` (branch
  coverage on ``libs/lib_*`` + ``tools/dev_cli``);
  ``.github/workflows/test-coverage.yml`` runs the cross-repo test
  surface and enforces ``--cov-fail-under=35`` on every PR. Current
  coverage: 41.50% — 6.5 ppt headroom for the next ratchet.

#### Changed

- ``scripts/run_options_backtest.py`` now reads ``config.json`` from
  the strategy directory directly (Plan §6.12 — closes the silent
  harness↔config drift bug). Also folded
  ``run_options_backtest_detailed.py`` into a ``--detailed`` flag on
  the same script; the old script is now a thin compatibility shim.
- Continued bare-except narrowing across ``apps/``: baseline lowered
  from 71 → 49 (target 50 hit). Narrowed DB query wrappers in
  ``execution_engine/metrics/strategy_metrics.py``,
  ``execution_engine/deduplication.py``,
  ``execution_engine/engine.py``, ``execution_engine/pending_orders.py``,
  and ``scoring_engine/providers_db.py`` from
  ``except Exception`` to ``except (SQLAlchemyError, OSError)``.
  Narrowed enum constructors in ``execution_engine/engine.py`` and
  ``execution_engine/broker_resolver.py`` to ``except ValueError``.
  Narrowed config loaders in ``execution_engine/main.py`` and
  ``scoring_engine/main.py`` to ``except (ValueError, ValidationError)``.
  Programming bugs (AttributeError, KeyError, TypeError) now surface
  in tests instead of being silently swallowed in production.

#### Deprecated

- ``lib_strategy.base.BaseStrategy`` and the legacy ``FullStrategy`` /
  ``IndicatorStrategy`` chain. The classes still re-export from
  ``lib_strategy.__init__`` with a ``DeprecationWarning``; planned
  removal in the next minor cycle. New strategies must subclass
  ``lib_strategy.signals.PureSignalStrategy`` (or, for ML,
  ``lib_ml.BaseMLStrategy``).
- ``StrategyRegistry`` no longer imports from ``lib_strategy.base``;
  it now keys off a small ``RegistrableStrategy`` ``Protocol`` (Plan
  §6.1 stage 1). The legacy ``BaseStrategy`` class still satisfies the
  protocol, so the registry's behavior is unchanged for current
  callers (none in production code as of Sprint A). The decoupling
  unblocks deletion of ``base.py`` / ``full_strategy.py`` /
  ``indicator_strategy.py`` in the next minor cycle.

#### Removed

- N/A in Sprint A.

#### Fixed

- Bare-except sites that were hiding typed exceptions across
  ``apps/`` modules.

---

## 2026-05-05 — Post-audit consolidation (the "9 PRs" + ML migration)

### Highlights

- **22 ML strategies migrated** to ``lib_ml.BaseMLStrategy``: 9 301 →
  7 532 LOC (~19% reduction). Behaviour pinned by per-strategy golden
  tests in ``tests/test_ml_strategy_golden_signals.py``.
- **Models split**: ``libs/lib_application/lib_application/db/models.py``
  (2 006 LOC, 69 tables) split into 12 per-domain submodules.
  ``models/__init__.py`` is now 253 LOC of pure re-exports.
- **Audit gates wired into pre-commit + CI**: ``vmdev audit`` now
  blocks god-class growth, duplicate canonical types, session drift,
  bare-except baseline regressions, indicator strategies that bypass
  the signal-only contract, and tracked build artefacts.
- **Lease-expiry production bug fixed in ``OutboxStore``**: zombie
  ``in_progress`` rows are now recoverable. Discovered by golden
  tests added in the same sprint.
- **3 production bugs in ``vmdev user``** fixed by added tests:
  missing ``user_id`` default, return-type mismatch, FK-name mismatch
  on broker accounts.
- **Bare-except baseline**: 114 → 71 sites across ``apps/`` (38%
  reduction).
- **315 tests, 13 skipped**, 0 audit errors, 0 audit warnings on every
  commit.

### Added

- ``lib_ml.BaseMLStrategy`` — shared base class for ML strategies
  (``_make_signal``, ``get_metadata``, position-tracking state,
  retraining cadence).
- ``lib_application.db.session.get_session_factory`` /
  ``create_engine_for_env`` / ``dispose_engine`` — single canonical
  SQLAlchemy session helper.
- ``lib_common.app.create_service_app`` — shared FastAPI factory used
  by scoring, execution, and feedback engines.
- ``vmdev audit`` CLI command + tests + pre-commit hook + GitHub
  Actions workflow.
- ``apps/scoring_engine/scoring_engine/_score_calculator.py`` —
  extracted from ``engine.py``.
- ``apps/scoring_engine/scoring_engine/schemas.py`` +
  ``_signal_validation.py`` — extracted from ``api.py``.
- ML strategy E2E pipeline test
  (``tests/test_e2e_ml_strategy_pipeline.py``).
- Outbox failure-mode tests, ``OutboxRelayWorker`` HTTP 5xx/4xx tests,
  ``SignalWorker`` cold-start / NOTIFY race tests, binding-evaluator
  magnitude unit tests, alembic-models round-trip test.
- ``vmdev user`` CLI tests.

### Changed

- ``apps/scoring_engine/scoring_engine/engine.py`` (789 → 687 LOC) —
  delegates score-axis aggregation to ``ScoreCalculator``.
- ``apps/scoring_engine/scoring_engine/api.py`` (720 → 571 LOC) —
  schemas + validation extracted.
- ``OutboxStore.claim_batch`` filter now allows reclaiming
  ``in_progress`` rows whose lease has expired (was: status filter
  permanently excluded them).
- All session-drift call sites either migrated to
  ``get_session_factory`` or explicitly allowlisted with a
  justification.

### Deprecated

- ``lib_infrastructure.persistence.sqlalchemy.session`` —
  ``create_session_factory`` / ``create_engine_only`` now delegate to
  ``lib_application.db.session``. The shim emits
  ``DeprecationWarning``.

### Fixed

- ``OutboxStore.claim_batch`` zombie-row recovery (lease-expiry).
- ``OutboxStore`` datetime tz-mismatch on SQLite.
- ``User.user_id`` missing ``default=generate_uuid``.
- ``vmdev user add`` ``LinkedBrokerAccount`` ``env_id`` → ``environment``.
- ``vmdev user add`` status="connected" (was illegal "pending_verification").

### Removed

- Stale ``MIGRATION_EXEMPTIONS`` entry for ``models/__init__.py``.
- Inline Pydantic schemas + signal-validation helpers from
  ``scoring_engine/api.py``.
- Inline score-aggregation methods from ``scoring_engine/engine.py``.
