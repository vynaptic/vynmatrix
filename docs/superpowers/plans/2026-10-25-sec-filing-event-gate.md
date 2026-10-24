# SEC Filing Event Gate (Layer 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the inert, code-only SEC filing event gate for
USQualityCompounder: rules, gate contracts, storage, acquisition, persistence,
the database resolver, inert producer collection, and the historical
diagnostic. Layer 2 (TypeSafe judgments) gets its own plan later.

**Architecture:**

- **Domain (`lib_strategy`).** `sec_filing_rules.py` holds the rule table,
  windows, item parsing, precedence, and the pure gate function. The two gate
  contracts join PR #20's `OPTIONAL_GATE_SOURCE_CONTRACTS`.
- **Application (`lib_application`).** Persists `filing` and `filing_coverage`
  observations and resolves a per-member result at a cutoff.
- **Infrastructure (`market_data_ingestor`).** A second submissions download per
  issuer, the inert producer hook, and the historical diagnostic.
- **Not wired.** The panel never calls the resolver. The `0.2.0` configuration
  digest must not change.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, httpx, pytest. Unit tests run
on SQLite; the PostgreSQL migration test follows the existing pattern.

**Spec:** `docs/superpowers/specs/2026-10-24-sec-filing-event-gate-design.md`

## Prerequisite

PR vynaptic/vynmatrix#20 is merged into `main`, and local `main` is pulled. This
plan uses its `OPTIONAL_GATE_SOURCE_CONTRACTS`,
`OptionalFactorApplication.ELIGIBILITY_GATE`,
`OptionalFactorAggregation.COVERAGE_ATTESTATION`,
`OptionalFactorNormalization.ABSOLUTE_THRESHOLD`, the `contract=` keyword of
`validate_optional_factor_evidence`, and migration `0111`. Stop if any is
missing. Branch and submit per `CONTRIBUTING.md#branch-and-submit`.

## Global Constraints

- **Rules (spec 3.1).** 8-K/8-K/A item `4.02`: 365 days, `sec_non_reliance`.
  8-K/8-K/A item `1.03`: 730 days, `sec_bankruptcy_or_receivership`.
  `NT 10-K`, `NT 10-Q`, `NT 10-K/A`, `NT 10-Q/A`: 180 days,
  `sec_late_periodic_report`. 8-K/8-K/A item `2.06`: 120 days,
  `sec_material_impairment`.
- **Window.** A filing fires when `T − window < acceptance_time <= T`.
- **Precedence.** `sec_filing_coverage_unavailable`, `sec_filing_evidence_invalid`,
  `sec_bankruptcy_or_receivership`, `sec_non_reliance`,
  `sec_late_periodic_report`, `sec_material_impairment`.
- **Item format.** `^\d\.\d{2}$` after stripping. An 8-K or 8-K/A in
  `(T − 730 days, T]` with empty or malformed items is
  `sec_filing_evidence_invalid`. NT forms fire on form alone.
- **Coverage.** `window_end <= T`, `window_end >= T − 6 hours`, and the oldest
  filing in kept pages is dated `<= T − 730 days` or the issuer has no older
  page. Fetch uses `since = T − 760 days`.
- **Identity.** `filing`: `<accession>|<instrument id>|<fetch cutoff ISO UTC>`,
  with `accession_number` and `filing_form` set. Each fetch writes a fresh set.
- **Availability.** `available_at` = submissions page `retrieved_at`;
  `event_at` = `acceptance_time`. `maximum_source_age_days = 1`.
- **Released strategy untouched.** `QualityCompounderEvidencePolicy`,
  `quality_compounder_configuration_sha256("0.2.0")`,
  `acquire_quality_compounder_sec_graphs` and its return type are unchanged.
  `optional_factor_source_registry_sha256()` is unchanged.
- **Inert failures.** Gate acquisition or persistence failure never fails the
  `0.2.0` panel. Catch only named exceptions; `vmdev audit` caps broad handlers.
- **Paper only.** `EXECUTION_MODE=paper`, `EXECUTION_ENGINE_ALLOW_LIVE=false`.
- **Test runner.** From `.venv-dev`: `python -m pytest <path> -q`.

## Review Focus

1. **A heavy filer whose `recent` block spans under 730 days.** Expect coverage
   to need the older page, and to fail closed when that page is absent. Test in
   Task 5.
2. **A multi-class issuer (two listed classes, one CIK).** Expect the same
   reason on both instruments. Test in Tasks 4 and 6.
3. **An 8-K accepted after the fetch cutoff but before `T`.** Expect it to be
   absent from that quarter's evidence, without invalidating the member. Test in
   Task 5.
4. **One issuer's EDGAR request fails.** Expect only that issuer's members to
   lack coverage; others persist and the `0.2.0` panel still builds. Test in
   Task 7.
5. **Evidence from an earlier quarter's fetch present in the database.** Expect
   the resolver to ignore it and use only the fetch matching the cutoff. Test in
   Task 6.

---

### Task 1: Pure rules module

**Files:**
- Create: `libs/python/lib_strategy/lib_strategy/sec_filing_rules.py`
- Test: `libs/python/lib_strategy/tests/test_sec_filing_rules.py`

**Interfaces:**
- Produces:
  - `SecFilingGateReason(StrEnum)` with the six reasons in precedence order.
  - `SecFilingRule` frozen dataclass: `forms: frozenset[str]`,
    `item: str | None`, `window: timedelta`, `reason: SecFilingGateReason`.
  - `SEC_FILING_RULES: tuple[SecFilingRule, ...]` (spec 3.1 values).
  - `SEC_FILING_EVIDENCE_SCOPE = timedelta(days=730)`,
    `SEC_FILING_COVERAGE_TOLERANCE = timedelta(hours=6)`,
    `SEC_FILING_FETCH_LOOKBACK = timedelta(days=760)`.
  - `GATE_RELEVANT_FORMS: frozenset[str]` = 8-K, 8-K/A and the four NT forms.
  - `parse_sec_items(raw: Sequence[str]) -> tuple[str, ...] | None` — `None`
    when empty or any item malformed.
  - `SecFilingEvent` frozen dataclass: `accession: str`, `form: str`,
    `items: tuple[str, ...]`, `acceptance_time: datetime`,
    `observation_id: str`.
  - `SecFilingCoverage` frozen dataclass: `window_end: datetime`,
    `oldest_listed_filing_date: date | None`, `has_older_page: bool`,
    `listed_accessions: frozenset[str]`, `observation_id: str`.
  - `SecFilingGateResult` frozen dataclass: `reason: SecFilingGateReason | None`,
    `fired: tuple[SecFilingGateReason, ...]`, `accessions: tuple[str, ...]`,
    `observation_ids: tuple[str, ...]`.
  - `sec_filing_gate_reason(*, events: Sequence[SecFilingEvent], coverage:
    SecFilingCoverage | None, cutoff: datetime) -> SecFilingGateResult`.

- [ ] **Step 1: Write failing tests**

```python
T = datetime(2026, 12, 31, 22, 0, tzinfo=UTC)

def test_rule_table_matches_spec():
    table = {(r.item, r.window.days, r.reason.value) for r in SEC_FILING_RULES}
    assert table == {
        ("4.02", 365, "sec_non_reliance"),
        ("1.03", 730, "sec_bankruptcy_or_receivership"),
        (None, 180, "sec_late_periodic_report"),
        ("2.06", 120, "sec_material_impairment"),
    }
    assert all(r.window >= timedelta(days=100) for r in SEC_FILING_RULES)

@pytest.mark.parametrize(("item", "days"), [("4.02", 365), ("1.03", 730), ("2.06", 120)])
def test_window_edges(item, days):
    inside = _event("8-K", (item,), T - timedelta(days=days) + timedelta(seconds=1))
    edge = _event("8-K", (item,), T - timedelta(days=days))
    assert sec_filing_gate_reason(events=[inside], coverage=_cov(), cutoff=T).reason is not None
    assert sec_filing_gate_reason(events=[edge], coverage=_cov(), cutoff=T).reason is None

def test_event_at_cutoff_fires(): ...            # acceptance_time == T fires
def test_amendment_counts(): ...                 # "8-K/A" with 4.02 fires
def test_nt_form_fires_without_items(): ...      # "NT 10-Q/A", items=() -> sec_late_periodic_report
def test_malformed_item_in_scope_is_invalid(): ...   # ("Item 4.02",) at T-700d -> sec_filing_evidence_invalid
def test_malformed_item_out_of_scope_ignored(): ...  # same at T-731d -> None
def test_empty_items_8k_is_invalid(): ...
def test_missing_coverage(): ...                 # coverage=None -> sec_filing_coverage_unavailable
def test_stale_coverage(): ...                   # window_end = T-6h-1s -> coverage_unavailable
def test_coverage_after_cutoff_rejected(): ...   # window_end = T+1s -> coverage_unavailable
def test_shallow_pages(): ...                    # oldest = (T-700d).date(), has_older_page=True -> coverage_unavailable
def test_shallow_pages_no_older_page_ok(): ...   # same, has_older_page=False -> None
def test_listed_accession_without_event(): ...   # -> sec_filing_evidence_invalid
def test_precedence_and_fired_list():
    events = [_event("8-K", ("2.06",), T - timedelta(days=10)),
              _event("8-K", ("1.03",), T - timedelta(days=20))]
    result = sec_filing_gate_reason(events=events, coverage=_cov(events), cutoff=T)
    assert result.reason is SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP
    assert result.fired == (SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP,
                            SecFilingGateReason.MATERIAL_IMPAIRMENT)
def test_naive_cutoff_rejected(): ...            # ValueError
```

`_cov(events=())` builds coverage with `window_end = T − 1 h`, oldest date
`(T − 800 days).date()`, `listed_accessions` from `events`.

- [ ] **Step 2: Run, expect import failure**

Run: `python -m pytest libs/python/lib_strategy/tests/test_sec_filing_rules.py -q`
Expected: FAIL, `ModuleNotFoundError: lib_strategy.sec_filing_rules`.

- [ ] **Step 3: Implement the module**

No I/O, no database imports. Coverage checks run first, then evidence
validity (listed accession without event, malformed items in scope), then
rules. `fired` is sorted by precedence and de-duplicated; `accessions` lists the
filings that fired, sorted. `observation_ids` includes the coverage ID and every
event ID considered, sorted.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest libs/python/lib_strategy/tests/test_sec_filing_rules.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add libs/python/lib_strategy/lib_strategy/sec_filing_rules.py libs/python/lib_strategy/tests/test_sec_filing_rules.py
git commit -m "feat(strategy): pure SEC filing event rules for the quality compounder gate"
```

### Task 2: Gate source contracts

**Files:**
- Modify: `libs/python/lib_strategy/lib_strategy/equity_optional_factors.py`
- Test: `libs/python/lib_strategy/tests/test_equity_optional_gate_contracts.py`

**Interfaces:**
- Produces: `OptionalFactorSleeve.SEC_FILING_EVENTS = "sec_filing_events"`,
  `OptionalFactorSleeve.SEC_FILING_COVERAGE = "sec_filing_coverage"`,
  `OptionalFactorObservationKind.FILING = "filing"`,
  `OptionalFactorObservationKind.FILING_COVERAGE = "filing_coverage"`,
  `OptionalFactorAggregation.SEC_FILING_EVENT_RULES = "sec_filing_event_rules"`,
  `OptionalFactorEventTimeSemantic.FILING_ACCEPTANCE = "filing_acceptance"`,
  `SEC_FILING_EVENT_GATE_CONTRACT`, `SEC_FILING_COVERAGE_GATE_CONTRACT`, both in
  `OPTIONAL_GATE_SOURCE_CONTRACTS` and `__all__`.

Contract values (mirror `NEWS_COVERAGE_GATE_CONTRACT` for the shared fields):

| Field | Event contract | Coverage contract |
| --- | --- | --- |
| `event_at_semantic` / `event_value_field` | `FILING_ACCEPTANCE` / `acceptance_time_raw` | `MEASUREMENT_AS_OF` / `window_end` |
| required values | `acceptance_time_raw` text, `cik` text, `filing_date` date, `item` text (zero or more ordinals) | `window_start` timestamp, `window_end` timestamp, `oldest_listed_filing_date` date, `has_older_page` boolean, `submission_page_sha256` text, `listed_accession` text |
| `aggregation` | `SEC_FILING_EVENT_RULES` | `COVERAGE_ATTESTATION` |
| `direction` | `LOWER_IS_BETTER` | `HIGHER_IS_BETTER` |
| `maximum_source_age_days` / `signal_half_life_days` | 1 / 1 | 1 / 1 |
| `requires_model_transform` | false | false |

If PR #20's `_value` helper cannot express "zero or more ordinals" for `item`,
or `oldest_listed_filing_date` that may be absent, add an optional-value flag to
the helper rather than encoding a sentinel; cover it in this task's tests.

- [ ] **Step 1: Write failing tests**

```python
def test_sec_gate_contracts_registered():
    kinds = {c.observation_kind.value for c in OPTIONAL_GATE_SOURCE_CONTRACTS}
    assert {"filing", "filing_coverage"} <= kinds
    assert optional_gate_source_contract("sec_filing_events") is SEC_FILING_EVENT_GATE_CONTRACT
    assert not SEC_FILING_EVENT_GATE_CONTRACT.activation.requires_model_transform
    assert SEC_FILING_EVENT_GATE_CONTRACT.activation.maximum_source_age_days == 1

def test_factor_registry_unchanged_by_sec_contracts():
    assert optional_factor_source_registry_sha256() == FACTOR_REGISTRY_SHA256_BEFORE

def test_configuration_digest_unchanged_by_sec_gate():
    assert quality_compounder_configuration_sha256("0.2.0") == (
        "950fa8d190f9793224289c74c54e946eaf305d369bf4746194666d134f00ff74"
    )
```

Record `FACTOR_REGISTRY_SHA256_BEFORE` by running
`optional_factor_source_registry_sha256()` on `main` before editing.

- [ ] **Step 2: Run, expect failure** — `python -m pytest libs/python/lib_strategy/tests/test_equity_optional_gate_contracts.py -q`; Expected: FAIL on import of `SEC_FILING_EVENT_GATE_CONTRACT`.
- [ ] **Step 3: Add enum members and contracts.**
- [ ] **Step 4: Run, expect pass** — same command plus `python -m pytest libs/python/lib_strategy/tests -q -k "optional or quality_compounder"`.
- [ ] **Step 5: Commit** — `git commit -m "feat(strategy): register SEC filing gate source contracts"`.

### Task 3: Migration for `filing_coverage`

**Files:**
- Create: `scripts/db/alembic/versions/0112_sec_filing_gate_storage.py`
- Modify: `libs/python/lib_application/lib_application/db/models/equity_observations.py` (kind check constraint)
- Modify: `docs/DATABASE.md` (observation kind list)
- Test: `tests/test_sec_filing_gate_storage_migration.py`

**Interfaces:**
- Produces: revision `0112_sec_filing_gate_storage`, `down_revision =
  "0111_adverse_news_gate_storage"`. `filing_coverage` accepted by
  `ck_equity_observation_kind`.

- [ ] **Step 1: Write failing test** following `tests/test_adverse_news_gate_storage_migration.py`: upgrade accepts a `filing_coverage` row; downgrade with a `filing_coverage` row raises `RuntimeError("Cannot remove SEC filing gate storage after evidence exists")`; downgrade on an empty table restores the `0111` check.
- [ ] **Step 2: Run, expect failure** — `python -m pytest tests/test_sec_filing_gate_storage_migration.py -q`.
- [ ] **Step 3: Implement** by swapping the check, as `0111` does. Keep the ORM `CheckConstraint` text identical to the migration.
- [ ] **Step 4: Run, expect pass**, plus `python -m pytest tests/test_adverse_news_gate_storage_migration.py -q`.
- [ ] **Step 5: Commit** — `git commit -m "feat(db): accept filing_coverage equity observations"`.

### Task 4: Persistence of filing evidence

**Files:**
- Create: `libs/python/lib_application/lib_application/services/sec_filing_observations.py`
- Test: `libs/python/lib_application/tests/test_sec_filing_observations.py`

**Interfaces:**
- Consumes: `EquityObservationSubmission`, `EquityObservationValueInput` from
  `equity_observation_writer.py`; the write path used by PR #20's
  `adverse_news_observations._write`.
- Produces:
  - `SecSubmissionPage` frozen dataclass: `url: str`, `content_sha256: str`,
    `retrieved_at: datetime`.
  - `SecFilingFact` frozen dataclass: `cik: str`, `accession: str`, `form: str`,
    `items: tuple[str, ...]` (raw, unparsed), `acceptance_time: datetime`,
    `acceptance_time_raw: str`, `filing_date: date`, `page: SecSubmissionPage`.
  - `SecIssuerFilingFetch` frozen dataclass: `cik: str`,
    `instrument_ids: tuple[int, ...]`, `fetch_cutoff: datetime`,
    `pages: tuple[SecSubmissionPage, ...]`, `filings: tuple[SecFilingFact, ...]`,
    `oldest_listed_filing_date: date | None`, `has_older_page: bool`.
  - `persist_sec_filing_fetch(session: Session, fetch: SecIssuerFilingFetch, *,
    entitlement_owner_user_id: str | None) -> tuple[str, ...]` — returns new
    observation IDs; one `filing` row per (filing, instrument) and one
    `filing_coverage` row per instrument.
  - `SecFilingPersistenceError(RuntimeError)`.

Lineage: provider `sec`, product `edgar_submissions`, endpoint the page URL,
`artifact_content_sha256` the page SHA-256, entitlement scope `public_sec_edgar`
(match the value the existing SEC fact writer uses; read
`_sec_fact_submission` in `equity_evidence.py`). Raw items are stored even when
malformed, so the rule function, not the writer, decides validity.

- [ ] **Step 1: Write failing tests**

```python
def test_writes_one_filing_row_per_class_and_one_coverage_per_class(session): ...
    # fetch with instrument_ids=(11, 12) and 3 filings -> 6 filing + 2 coverage rows
def test_filing_row_fields(session): ...
    # accession_number, filing_form set; event_at == acceptance_time;
    # available_at == page.retrieved_at; item ordinals 0..n-1 in order
def test_identity_includes_fetch_cutoff(session): ...
    # two fetches with different cutoffs both persist, no revision
def test_same_identity_different_content_rejected(session): ...
    # SecFilingPersistenceError
def test_filing_after_fetch_cutoff_rejected(session): ...
def test_malformed_items_still_stored(session): ...
def test_coverage_lists_every_accession(session): ...
```

- [ ] **Step 2: Run, expect failure** — `python -m pytest libs/python/lib_application/tests/test_sec_filing_observations.py -q`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(application): persist SEC filing gate evidence"`.

### Task 5: Acquisition

**Files:**
- Create: `apps/market_data_ingestor/market_data_ingestor/sec_filing_events.py`
- Test: `apps/market_data_ingestor/tests/test_sec_filing_events.py`
- Fixtures: recorded submissions pages under `apps/market_data_ingestor/tests/fixtures/sec_filing_events/` (real EDGAR JSON, trimmed to the needed rows; one root with a `files` page entry, one history page)

**Interfaces:**
- Consumes: `SecEdgarClient.fetch_submissions(cik, *, since)`,
  `SecFilingRecord`, `GATE_RELEVANT_FORMS`, `SEC_FILING_FETCH_LOOKBACK`,
  Task 4 dataclasses.
- Produces:
  - `SecFilingAcquisitionFailure` frozen dataclass: `cik: str`, `error: str`.
  - `acquire_sec_filing_events(client: SecEdgarClient, *, issuers:
    Mapping[str, tuple[int, ...]], fetch_cutoff: datetime) ->
    tuple[tuple[SecIssuerFilingFetch, ...], tuple[SecFilingAcquisitionFailure, ...]]`
    — `issuers` maps CIK to listed instrument IDs. A `SecEdgarError` for one
    issuer becomes a failure entry; other issuers continue.

`since` is `(fetch_cutoff − SEC_FILING_FETCH_LOOKBACK).date()`. Keep rows with
`form in GATE_RELEVANT_FORMS` and `acceptance_time <= fetch_cutoff`.
`oldest_listed_filing_date` is the minimum `filing_date` over all rows of all
kept pages (any form). `has_older_page` is true when the root lists a page that
`since` skipped.

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport` serving the fixtures:
  - `test_selects_gate_forms_only` — 10-K, 4, 8-K, NT 10-Q rows → only 8-K and NT kept.
  - `test_drops_filings_after_fetch_cutoff` (Review Focus 3).
  - `test_heavy_filer_fetches_history_page` — recent block shorter than 730 days → history page fetched, `oldest_listed_filing_date` from it (Review Focus 1).
  - `test_skipped_page_sets_has_older_page`.
  - `test_one_issuer_failure_isolated` — 500 for one CIK → one failure, other fetch returned.
  - `test_page_source_retained` — each `SecFilingFact.page.content_sha256` equals the served body's SHA-256.
- [ ] **Step 2: Run, expect failure** — `python -m pytest apps/market_data_ingestor/tests/test_sec_filing_events.py -q`.
- [ ] **Step 3: Implement.** No change to `quality_compounder_sec.py`.
- [ ] **Step 4: Run, expect pass**, plus `python -m pytest apps/market_data_ingestor/tests/test_quality_compounder_sec.py -q` unchanged.
- [ ] **Step 5: Commit** — `git commit -m "feat(ingestor): acquire SEC filing events for the quality compounder gate"`.

### Task 6: Database resolver

**Files:**
- Create: `libs/python/lib_application/lib_application/services/sec_filing_gate.py`
- Test: `libs/python/lib_application/tests/test_sec_filing_gate.py`

**Interfaces:**
- Consumes: Task 1 pure function and dataclasses; Task 2 contracts;
  `validate_optional_factor_evidence(session, *, contract=..., factor_version=None,
  observation_id=..., cutoff=..., provider_authority_policy=...,
  expected_instrument_id=...)`; `validate_equity_observation_authority`.
- Produces: `resolve_quality_compounder_sec_filing_gate(session: Session, *,
  cutoff: datetime, instrument_ids: Sequence[int], provider_authority_policy:
  ProviderAuthorityPolicy) -> Mapping[int, SecFilingGateResult]`.

Per instrument: select the latest `filing_coverage` row with
`available_at <= cutoff` and `window_end` in `[cutoff − 6 h, cutoff]`; load the
`filing` rows whose identity carries that coverage's fetch cutoff. Any
validation error on a selected row maps to
`sec_filing_evidence_invalid` for that instrument, never an exception out of
the resolver. Rows from other fetches are ignored.

- [ ] **Step 1: Write failing tests** (SQLite, rows written through Task 4):
  - `test_clear_member`, `test_non_reliance_member`;
  - `test_multi_class_same_reason` (Review Focus 2);
  - `test_older_fetch_ignored` — a fetch from the previous quarter with a 2.06 filing, current fetch clean → clear (Review Focus 5);
  - `test_no_coverage_fails_closed`;
  - `test_unauthorized_provider_is_invalid`;
  - `test_observation_ids_include_coverage_and_events`.
- [ ] **Step 2: Run, expect failure** — `python -m pytest libs/python/lib_application/tests/test_sec_filing_gate.py -q`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(application): resolve the SEC filing gate at a panel cutoff"`.

### Task 7: Inert producer collection

**Files:**
- Modify: `apps/market_data_ingestor/market_data_ingestor/quality_compounder_producer.py`
- Test: `apps/market_data_ingestor/tests/test_quality_compounder_producer.py` (or the existing producer test module)

**Interfaces:**
- Consumes: Task 5 `acquire_sec_filing_events`, Task 4 `persist_sec_filing_fetch`.
- Produces: no new public API. Structured log events
  `sec_filing_gate_acquired` (issuer and filing counts),
  `sec_filing_gate_issuer_failed` (CIK, error), and
  `sec_filing_gate_persist_failed`.

Order in `QualityCompounderProducer.run`: after
`acquire_quality_compounder_sec_graphs`, take
`filing_cutoff = self._now_before(deadline, field_name="SEC filing gate cutoff")`,
call `acquire_sec_filing_events`, then take the panel `cutoff` as today. After
`_persist_and_register` commits, persist each fetch in its own session and
transaction. Catch only `SecEdgarError`, `SecFilingPersistenceError`, and
`sqlalchemy.exc.SQLAlchemyError`; log and continue. The resolver is not called.

- [ ] **Step 1: Write failing tests**
  - `test_gate_collection_persists_after_panel`;
  - `test_gate_issuer_failure_does_not_fail_panel` (Review Focus 4);
  - `test_gate_persist_failure_does_not_fail_panel`;
  - `test_filing_cutoff_precedes_panel_cutoff`.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect pass**, plus the full producer test module and Task 2's digest test.
- [ ] **Step 5: Commit** — `git commit -m "feat(ingestor): collect SEC filing gate evidence beside the 0.2.0 panel"`.

### Task 8: Historical diagnostic

**Files:**
- Create: `apps/market_data_ingestor/market_data_ingestor/sec_filing_event_history.py`
- Test: `apps/market_data_ingestor/tests/test_sec_filing_event_history.py`

**Interfaces:**
- Consumes: Task 1 `sec_filing_gate_reason`; recorded submissions persisted as
  source lineage; membership and price observations already in the evidence
  store.
- Produces:
  - `SecFilingDiagnosticQuarter` frozen dataclass: `cutoff: datetime`,
    `members: int`, `removed_by_reason: Mapping[str, int]`,
    `removed_by_sector: Mapping[str, int]`, `invalid_items: int`.
  - `SecFilingDiagnosticReport` frozen dataclass: `quarters`,
    `mean_removal_rate: float`, `per_rule_flags: Mapping[str, int]`,
    `per_rule_excess_return_63: Mapping[str, float | None]`,
    `per_rule_left_tail_share: Mapping[str, float | None]`,
    `unflagged_left_tail_share: float | None`, `survivorship_biased: bool`,
    `label: Literal["acceptance-time diagnostic"]`.
  - `replay_sec_filing_rules(*, filings_by_cik, members_by_cutoff, sector_by_instrument,
    returns_63 , cutoffs) -> SecFilingDiagnosticReport` — pure; acceptance time
    stands in for availability; synthetic coverage marked complete.
  - `main(argv: Sequence[str] | None = None) -> int` for
    `python -m market_data_ingestor.sec_filing_event_history`, reading inputs
    from the database and printing the report as JSON to stdout. No report file
    is committed.

`survivorship_biased` is true when point-in-time membership is unavailable for
any requested cutoff. Excess return is the 63-session total return minus the
sector median at the same cutoff; left tail is the share below −0.20. A rule
with fewer than 30 flags reports its numbers and is labelled a safety rule.

- [ ] **Step 1: Write failing tests** on small deterministic fixtures at the function boundary (allowed for unit tests per `CLAUDE.md`): removal counts per reason, sector split, excess-return arithmetic, `invalid_items` counting, survivorship flag, and that the report never sets coverage-unavailable.
- [ ] **Step 2: Run, expect failure** — `python -m pytest apps/market_data_ingestor/tests/test_sec_filing_event_history.py -q`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(ingestor): acceptance-time diagnostic for SEC filing gate rules"`.

### Task 9: Documentation and verification

**Files:**
- Modify: `CHANGELOG.md` (Unreleased: inert SEC filing gate evidence collection)
- Modify: `strategies/indicator/USQualityCompounder/README.md` (one line beside the adverse-news gate note: inert, enabled only by a later strategy version)

- [ ] **Step 1: Edit both files.**
- [ ] **Step 2: Run affected suites**

```bash
python -m pytest libs/python/lib_strategy/tests libs/python/lib_application/tests/test_sec_filing_observations.py libs/python/lib_application/tests/test_sec_filing_gate.py apps/market_data_ingestor/tests -q
vmdev audit --strict
```

Expected: all pass; audit reports no new broad handlers or oversized files.

- [ ] **Step 3: Commit and submit** per `CONTRIBUTING.md#branch-and-submit` — `git commit -m "docs: note the inert SEC filing gate"`.

## Not in this plan

- **Layer 2** (spec section 4): own plan after PR #20 merges and the owner
  approves a TypeSafe live-probe budget.
- **Enabling** (spec section 7): the `QualityCompounderSecFilingGatePolicy`, the
  `event_risk_reason` keyword, panel wiring, and the version bump. Needs the
  diagnostic report against the frozen bar and one matching live forward
  quarter first.
- **Running the diagnostic on real data**: an operational step after Task 8,
  not a code task.
