# Adverse-News Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the inert adverse-news gate, end to end except for the news
provider and the panel wiring:

- the gate source contracts;
- the TypeSafe question module and truth tables;
- the storage;
- the TypeSafe client;
- the classification and coverage writers;
- the database gate resolver.

**Architecture:**

- **Domain (`lib_strategy`).** Holds the wording, thresholds, truth tables,
  digests, and source contracts, with no I/O.
- **Application (`lib_application`).** Persists classifications and coverage as
  append-only `news_event` and `news_coverage` observations, and resolves a
  per-member gate result at a cutoff.
- **Infrastructure (`market_data_ingestor`).** Owns the HTTP client and the
  classification job.
- **Not wired.** Nothing calls the resolver from the panel build. The `0.2.0`
  configuration digest must not change.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, httpx, pytest. The tests run
on SQLite; the PostgreSQL migration test follows the existing pattern.

**Spec:** `docs/superpowers/specs/2026-10-24-typesafe-adverse-news-gate-design.md`

## Global Constraints

- **Model pin.** Model ID `jev-1.14.0`, released `2026-10-14`. Endpoint
  `https://api.typesafe.ai/v1/systemone`. A response whose `model` field
  differs is rejected.
- **Thresholds.** `YES = 0.70`, `NO = 0.30`. A Noul `p >= 0.70` is yes, and
  `p <= 0.30` is no. Severity is material when `P(2) + P(3) >= 0.70` and
  immaterial when that sum is `<= 0.30`.
- **Window.** `(T − 90 days, T]`. Coverage records complete after `T + 6 h` and
  no later than `T + 72 h`. Classifications must have `classified_at <= T + 72 h`.
- **Question set.** `ADVERSE_NEWS_QUESTION_SET_VERSION = "adverse-news-questions-v1"`.
  Question IDs, wording, and criteria are copied verbatim from spec section 4.2.
- **Article identity.** `source_record_identity` is
  `<provider article id>|<instrument id>|<question_set_version>|<model_version>`.
- **Hashing.** Every digest uses `lib_common.hashing.canonical_json_hash`. The
  API key never enters a payload, digest, log, or fixture.
- **Separate registry.** The factor registry is unchanged:
  `optional_factor_source_registry_sha256()` and
  `OPTIONAL_FACTOR_SOURCE_CONTRACTS` stay byte-identical. Gate contracts live in
  `OPTIONAL_GATE_SOURCE_CONTRACTS`.
- **Released strategy untouched.** `QualityCompounderEvidencePolicy`,
  `quality_compounder_configuration_sha256`, and
  `QUALITY_COMPOUNDER_STRATEGY_VERSION = "0.2.0"` are unchanged.
- **No live calls.** No test or task makes a live TypeSafe call. Client tests
  use `httpx.MockTransport`.
- **Paper only.** `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`
  are untouched.
- **Test runner.** Run tests from `.venv-dev`:
  `python -m pytest <path> -q`.

## Review Focus

1. **One article tagged with several instruments** should produce one
   independent classification per instrument. One company's verdict never leaks
   into another's. *Task 5:*
   `test_one_article_two_instruments_gets_two_records`.
2. **A provider correction after classification.** It writes a revision with the
   correction's availability. At a cutoff before the correction, the original
   is still admitted. *Task 7:*
   `test_provider_correction_after_cutoff_keeps_original`.
3. **An empty or headline-only article body** is skipped as `empty_body` before
   any request is made. *Task 2:* `test_skip_reason_empty_body`.
4. **A run interrupted by `429` or a timeout** classifies some articles and
   writes no coverage record for that instrument. *Task 6:*
   `test_partial_run_writes_no_coverage`.
5. **Provider timestamps with a non-UTC offset** are normalized to UTC before
   hashing and comparing, so the same instant always yields the same digest.
   *Task 2:* `test_state_normalizes_published_at_to_utc`.

---

### Task 1: Gate source contracts and validator entry point

**Files:**
- Modify: `libs/python/lib_strategy/lib_strategy/equity_optional_factors.py`
- Modify: `libs/python/lib_application/lib_application/services/equity_optional_factor_sources.py:53-62`
- Test: `libs/python/lib_strategy/tests/test_equity_optional_gate_contracts.py` (create)
- Test: `libs/python/lib_application/tests/test_equity_optional_factor_sources.py`

**Interfaces:**
- Produces:
  - **Enum members:**
    - `OptionalFactorSleeve.ADVERSE_NEWS = "adverse_news"` and
      `OptionalFactorSleeve.ADVERSE_NEWS_COVERAGE = "adverse_news_coverage"`;
    - `OptionalFactorObservationKind.NEWS_COVERAGE = "news_coverage"`;
    - `OptionalFactorApplication.ELIGIBILITY_GATE = "eligibility_gate"`;
    - `OptionalFactorAggregation.CONFIRMED_MATERIAL_ADVERSE_EVENT` and
      `OptionalFactorAggregation.COVERAGE_ATTESTATION = "coverage_attestation"`;
    - `OptionalFactorNormalization.ABSOLUTE_THRESHOLD`.
  - **`ADVERSE_NEWS_GATE_CONTRACT`**, set exactly per spec section 6, including
    the required-values table.
  - **`NEWS_COVERAGE_GATE_CONTRACT`** with these settings:
    - sleeve `ADVERSE_NEWS_COVERAGE`, kind `NEWS_COVERAGE`, `requires_instrument=True`;
    - `event_at_semantic=MEASUREMENT_AS_OF`, `event_value_field="query_window_end"`;
    - required values `provider_query_identity` (text), `query_window_end`
      (timestamp), `query_window_start` (timestamp), `returned_article_count`
      (integer), `source_available_at` (timestamp);
    - activation `ELIGIBILITY_GATE`, `HIGHER_IS_BETTER`, `COVERAGE_ATTESTATION`,
      `ABSOLUTE_THRESHOLD`, `INELIGIBLE`;
    - `maximum_source_age_days=3`, `signal_half_life_days=3`,
      `expected_holding_sessions=(40, 63)`, `requires_model_transform=False`.
  - **Registry:**
    - `OPTIONAL_GATE_SOURCE_CONTRACTS`, the two contracts sorted by sleeve;
    - `optional_gate_source_contract(sleeve: str | OptionalFactorSleeve) -> OptionalFactorSourceContract`;
    - `optional_gate_source_registry_sha256() -> str`;
    - `OPTIONAL_GATE_REGISTRY_VERSION = "sp500-optional-gate-registry-v1"`.
  - **Validator:** `validate_optional_factor_evidence(session, *, sleeve_name: str | None = None, contract: OptionalFactorSourceContract | None = None, factor_version, observation_id, cutoff, provider_authority_policy, expected_instrument_id)`.
    Exactly one of `sleeve_name` and `contract` must be given; otherwise it raises
    `OptionalFactorEvidenceValidationError("exactly one of sleeve_name or contract is required")`.

- [ ] **Step 1: Write failing tests**

```python
def test_factor_registry_digest_is_unchanged():
    assert optional_factor_source_registry_sha256() == _REGISTRY_SHA256_BEFORE_GATES
    # capture the literal from main before editing; paste it as the constant

def test_gate_registry_holds_exactly_two_contracts():
    assert [c.sleeve.value for c in OPTIONAL_GATE_SOURCE_CONTRACTS] == [
        "adverse_news", "adverse_news_coverage"]
    assert ADVERSE_NEWS_GATE_CONTRACT.observation_kind.value == "news_event"
    assert ADVERSE_NEWS_GATE_CONTRACT.activation.maximum_source_age_days == 90
    assert ADVERSE_NEWS_GATE_CONTRACT.activation.requires_model_transform is True
    assert NEWS_COVERAGE_GATE_CONTRACT.activation.requires_model_transform is False
    assert len(optional_gate_source_registry_sha256()) == 64

def test_gate_sleeve_is_not_a_factor_sleeve():
    with pytest.raises(OptionalFactorContractError):
        optional_factor_source_contract("adverse_news")
    assert optional_gate_source_contract("adverse_news") is ADVERSE_NEWS_GATE_CONTRACT
```

In `test_equity_optional_factor_sources.py`, add
`test_validator_requires_exactly_one_of_sleeve_or_contract` (both given raises,
neither given raises). Also add
`test_validator_accepts_gate_contract_for_news_event`. It reuses `_seed_news`,
adding the new required values, and asserts `.sleeve_name == "adverse_news"`.

- [ ] **Step 2: Run, expect failures**

Run: `python -m pytest libs/python/lib_strategy/tests/test_equity_optional_gate_contracts.py libs/python/lib_application/tests/test_equity_optional_factor_sources.py -q`
Expected: FAIL with `ImportError` (`ADVERSE_NEWS_GATE_CONTRACT`).

- [ ] **Step 3: Implement the enums, the two contracts, the gate registry, and the validator argument**

Build both contracts with `_values(...)` and `_model_transform_values()`, so
canonical ordering holds. Export the new names in `__all__`.

- [ ] **Step 4: Run the tests again, expect PASS.** Also run
  `python -m pytest libs/python/lib_application/tests/test_equity_factor_snapshots.py tests/test_optional_equity_factor_contracts_migration.py -q`
  and expect PASS, which shows the factor registry is untouched.

- [ ] **Step 5: Commit** `feat(strategy): register adverse-news gate source contracts`

---

### Task 2: Question module, with state, request, answers, and digests

**Files:**
- Create: `libs/python/lib_strategy/lib_strategy/adverse_news_questions.py`
- Test: `libs/python/lib_strategy/tests/test_adverse_news_questions.py`

**Interfaces:**
- Produces:
  - **Constants:**
    - `ADVERSE_NEWS_QUESTION_SET_VERSION`;
    - `ADVERSE_NEWS_MODEL = "jev-1.14.0"`;
    - `ADVERSE_NEWS_MODEL_RELEASED_ON = date(2026, 10, 14)`;
    - `TYPESAFE_ENDPOINT`;
    - `YES_THRESHOLD = 0.70`, `NO_THRESHOLD = 0.30`;
    - `LOOKBACK_DAYS = 90`;
    - `MAX_BODY_CHARACTERS = 72_000`;
    - `ALLOWED_DOCUMENT_TYPES = frozenset({"news", "press_release", "regulatory_notice"})`.
  - **`@dataclass(frozen=True, slots=True) AdverseNewsCompany`**, with fields
    `name`, `ticker`, `sector`, `industry` (all `str`) and
    `other_names: tuple[str, ...] = ()`.
  - **`@dataclass(frozen=True, slots=True) AdverseNewsArticle`**, with fields
    `provider_article_id`, `headline`, `source`, `body`, `language`, and
    `document_type` (all `str`), plus `published_at` and `source_available_at`
    (`datetime`, tz-aware). It rejects naive datetimes.
  - **Functions:**
    - `article_skip_reason(article) -> str | None`, which returns
      `"language_not_en"`, `"document_type_not_allowed"`, `"empty_body"`, or
      `None`;
    - `build_adverse_news_state(company, article) -> tuple[dict[str, object], bool]`,
      which returns the state and `body_truncated`;
    - `ADVERSE_NEWS_QUESTIONS: Mapping[str, Mapping[str, object]]`, the exact
      `questions` JSON;
    - `adverse_news_request_body(state) -> dict[str, object]`, which returns
      `{"model", "state", "questions"}`.
  - **`AdverseNewsAnswers`**, a frozen dataclass with fields `about_company`,
    `price_recap`, `new_development`, `negative_development`, and
    `negative_confirmed` (all `float`), plus
    `negative_severity: tuple[float, float, float, float]`. It has:
    - `classmethod from_response(response: Mapping[str, object]) -> AdverseNewsAnswers`;
    - `classmethod from_values(values: Mapping[str, Sequence[tuple[int, Decimal]]]) -> AdverseNewsAnswers`,
      which reads the stored observation values.
  - **`AdverseNewsAnswerError(ValueError)`**.
  - **Digest functions:**
    - `prompt_schema_sha256() -> str`;
    - `output_schema_sha256() -> str`;
    - `inference_configuration_sha256() -> str`;
    - `model_artifact_sha256(model: str) -> str`.

- [ ] **Step 1: Write failing tests**

```python
def test_questions_match_spec_ids_and_types():
    assert {k: v["type"] for k, v in ADVERSE_NEWS_QUESTIONS.items()} == {
        "about_company": "noul", "price_recap": "noul", "new_development": "noul",
        "negative_development": "noul", "negative_confirmed": "noul",
        "negative_severity": "score"}
    assert len(ADVERSE_NEWS_QUESTIONS["negative_severity"]["criteria"]) == 4

def test_request_body_pins_model():
    assert adverse_news_request_body({"x": 1})["model"] == "jev-1.14.0"

def test_state_omits_empty_other_names(): ...          # "other_names" not in state["company"]
def test_state_truncates_on_paragraph_boundary(): ...  # body ends at "\n\n" boundary, flag True
def test_state_normalizes_published_at_to_utc(): ...   # +02:00 input -> "...Z" string
def test_skip_reason_empty_body(): ...                 # body "  " -> "empty_body"
def test_skip_reason_language(): ...                   # "de" -> "language_not_en"

def test_from_response_rejects_wrong_model():
    with pytest.raises(AdverseNewsAnswerError, match="model"):
        AdverseNewsAnswers.from_response({**_RESPONSE, "model": "jev-1.13.0"})

def test_from_response_rejects_missing_answer(): ...   # drop "price_recap" -> error
def test_from_response_rejects_wrong_type(): ...       # noul answer typed "score" -> error
def test_from_response_rejects_severity_not_summing_to_one(): ...  # 0.5,0.5,0.5,0 -> error
def test_from_values_round_trips_from_response(): ...

def test_digests_are_stable_and_exclude_secrets():
    assert prompt_schema_sha256() == prompt_schema_sha256()
    assert "Bearer" not in json.dumps(adverse_news_request_body({}))
```

`_RESPONSE` is a fixture shaped as in spec section 2, with `model`, `answers`,
and `usage`. Severity probabilities come from keys `"0"` to `"3"`.

- [ ] **Step 2: Run, expect `ModuleNotFoundError`**

Run: `python -m pytest libs/python/lib_strategy/tests/test_adverse_news_questions.py -q`

- [ ] **Step 3: Implement the module**

- **Wording.** Copy the spec section 4.2 wording verbatim into
  `ADVERSE_NEWS_QUESTIONS`. Noul criteria are `{"true": ..., "false": ...}`,
  and the Score criteria are a 4-item list.
- **Truncation.** Cut at the last `"\n\n"` boundary at or before
  `MAX_BODY_CHARACTERS`. This is a conservative stand-in for 24k tokens, at
  about 3 characters per token.
- **Severity check.** Accept a probability sum within `1e-6` of 1.
- **`model_artifact_sha256`.** It hashes `{"provider": "typesafe", "model": model}`.
  The docstring must say it hashes the model ID, not the weights.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** `feat(strategy): add TypeSafe adverse-news question set`

---

### Task 3: Truth tables

**Files:**
- Modify: `libs/python/lib_strategy/lib_strategy/adverse_news_questions.py`
- Test: `libs/python/lib_strategy/tests/test_adverse_news_truth_table.py`

**Interfaces:**
- Consumes: `AdverseNewsAnswers` (Task 2).
- Produces:
  - `ArticleVerdict(StrEnum)`, with the ten values from spec section 5.1:
    `not_about_company`, `price_recap`, `not_new`, `not_negative`,
    `negative_unsettled`, `negative_unconfirmed`,
    `negative_confirmation_unsettled`, `negative_immaterial`,
    `negative_severity_unsettled`, `adverse_confirmed_material`;
  - `classify_article(answers: AdverseNewsAnswers) -> ArticleVerdict`;
  - `GateReason(StrEnum)`, with values `news_coverage_unavailable`,
    `news_evidence_invalid`, `material_adverse_news`;
  - `gate_reason(*, coverage_complete: bool, evidence_valid: bool, verdicts: Sequence[ArticleVerdict]) -> GateReason | None`;
  - `UNSETTLED_VERDICTS: frozenset[ArticleVerdict]`, holding rows 5, 7, and 9.

- [ ] **Step 1: Write failing tests**

Write one parametrized test, `test_each_truth_table_row`, with one case per row
1–10. Each case builds answers with clear values (0.9 for yes, 0.1 for no, 0.5
for unsettled) and asserts the verdict. Then add:

```python
@pytest.mark.parametrize("p,expected", [(0.70, "yes"), (0.6999, "unsettled"),
                                        (0.30, "no"), (0.3001, "unsettled")])
def test_threshold_edges_on_about_company(p, expected): ...

def test_severity_uses_upper_mass_not_expected_score():
    # P = (0.5, 0, 0, 0.5): expected score 1.5, upper mass 0.5 -> unsettled
    assert classify_article(_confirmed_negative(severity=(0.5, 0, 0, 0.5))) is \
        ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED

def test_gate_reason_order():
    assert gate_reason(coverage_complete=False, evidence_valid=False,
                       verdicts=[ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL]) \
        is GateReason.NEWS_COVERAGE_UNAVAILABLE
    assert gate_reason(coverage_complete=True, evidence_valid=False, verdicts=[]) \
        is GateReason.NEWS_EVIDENCE_INVALID
    assert gate_reason(coverage_complete=True, evidence_valid=True,
                       verdicts=[ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED]) is None
```

Add one exhaustive test, `test_every_answer_combination_has_one_verdict`. It
covers the product of {0.1, 0.5, 0.9} for the five Nouls and three severity
shapes, and asserts `classify_article` returns an `ArticleVerdict` without
raising.

- [ ] **Step 2: Run, expect `ImportError`**

- [ ] **Step 3: Implement `classify_article` as the top-to-bottom rule list from spec section 5.1, and `gate_reason` per section 5.2**

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** `feat(strategy): add adverse-news truth tables`

---

### Task 4: Storage, with the `news_coverage` kind and inference records

**Files:**
- Create: `scripts/db/alembic/versions/0111_adverse_news_gate_storage.py`
  (`down_revision = "0110_asset_class_binding_scope"`; confirm the head with
  `alembic heads` first)
- Modify: `libs/python/lib_application/lib_application/db/models/equity_observations.py`.
  Change the `ck_equity_observation_kind` text and add `EquityModelInferenceRecord`.
- Modify: `libs/python/lib_application/lib_application/db/models/__init__.py`, to
  export the new model.
- Modify: `docs/DATABASE.md`, the equity evidence section, with one entry for the
  new table and kind.
- Test: `tests/test_adverse_news_gate_storage_migration.py`, following
  `tests/test_optional_equity_factor_contracts_migration.py`.

**Interfaces:**
- Produces: the ORM `EquityModelInferenceRecord`, table
  `equity_model_inference_records`, with columns:

  | Column | Type |
  | --- | --- |
  | `model_input_sha256` | `String(64)`, primary key |
  | `lineage_id` | `String(64)`, FK to `equity_source_lineages` |
  | `model_output_sha256` | `String(64)` |
  | `request_json` | `JSONType` |
  | `response_json` | `JSONType` |
  | `request_bytes` | `LargeBinary` |
  | `response_bytes` | `LargeBinary` |
  | `received_at` | `DateTime(timezone=True)` |
  | `entitlement_scope` | `String(100)` |
  | `entitlement_owner_user_id` | `String(50)`, FK to `users`, nullable |

  It also has length-64 `CheckConstraint`s on both digests. The model adds this
  table to the `_immutable_model` listener loop.

- [ ] **Step 1: Write failing tests**
  - `test_upgrade_allows_news_coverage_kind`;
  - `test_inference_record_is_append_only` (an ORM update raises `ValueError`);
  - `test_downgrade_refuses_when_news_coverage_rows_exist`;
  - `test_downgrade_refuses_when_inference_records_exist`;
  - `test_downgrade_restores_previous_kind_check`.

- [ ] **Step 2: Run, expect FAIL**

Run: `python -m pytest tests/test_adverse_news_gate_storage_migration.py -q`

- [ ] **Step 3: Implement the migration**
  - **Kind check.** Replace `ck_equity_observation_kind`, using `0090`'s
    `_OBSERVATION_KIND_CHECK` plus `'news_coverage'`.
  - **Table.** Create the table.
  - **PostgreSQL only.** Attach the trigger
    `trg_equity_model_inference_records_immutable` using the existing
    `public.vm_reject_immutable_equity_evidence_mutation()` (see `0086`, lines
    128–134). Also run
    `GRANT SELECT, INSERT ON TABLE public.equity_model_inference_records TO vm_market_data`.
  - **Downgrade.** Refuse with `RuntimeError("Cannot remove adverse-news gate storage after evidence exists")`
    when either kind of row exists.

- [ ] **Step 4: Run, expect PASS.** Also run
  `python -m pytest libs/python/lib_application/tests/test_equity_observation_writer.py -q`
  and expect PASS.

- [ ] **Step 5: Commit** `feat(db): store adverse-news coverage and model inference records`

---

### Task 5: Classification and coverage submissions

**Files:**
- Create: `libs/python/lib_application/lib_application/services/adverse_news_observations.py`
- Test: `libs/python/lib_application/tests/test_adverse_news_observations.py`

**Interfaces:**
- Consumes:
  - from Task 2: `AdverseNewsArticle`, `AdverseNewsAnswers`, and the digest
    functions;
  - from Task 4: `EquityModelInferenceRecord`;
  - `EquityObservationSubmission`, `EquityObservationValueInput`, and
    `persist_equity_observation` from `equity_observation_writer.py`.
- Produces:
  - **`@dataclass(frozen=True, slots=True) ProviderLineage`**, with fields
    `provider`, `product`, `endpoint`, `dataset_version`, `tool_version`,
    `source_identity`, `source_revision`, `entitlement_scope`,
    `entitlement_owner_user_id`, `retrieved_at`, `timestamp_semantics`, and
    `artifact_content_sha256`. It carries the fields the provider adapter
    supplies.
  - **`@dataclass(frozen=True, slots=True) TypeSafeExchange`**, with fields
    `request_json: Mapping`, `response_json: Mapping`, `request_bytes: bytes`,
    `response_bytes: bytes`, `received_at: datetime`. Task 6's client returns it.
  - **`adverse_news_record_identity(*, provider_article_id: str, instrument_id: int, model_version: str) -> str`**.
  - **`persist_adverse_news_classification(session, *, lineage: ProviderLineage, instrument_id: int, article: AdverseNewsArticle, body_truncated: bool, exchange: TypeSafeExchange) -> str`**,
    which returns the `observation_id`. It parses answers with
    `AdverseNewsAnswers.from_response`, writes the observation, and writes the
    inference record idempotently. An existing key with an identical
    `model_output_sha256` is a no-op; a different digest raises.
  - **`persist_news_coverage(session, *, lineage: ProviderLineage, instrument_id: int, query_window_start: datetime, query_window_end: datetime, completed_at: datetime, provider_query_identity: str, returned_article_ids: Sequence[str], skipped_article_ids: Sequence[str]) -> str`**.
    It writes `returned_article_count`, one `returned_article_identity` text
    value per ordinal, and one `skipped_article_identity` text value per
    ordinal. Neither list is a contract-required value, because an empty list
    writes zero rows. It raises if `skipped_article_ids` is not a subset of
    `returned_article_ids`.
  - **`AdverseNewsPersistenceError(RuntimeError)`**.

- [ ] **Step 1: Write failing tests**
  - `test_classification_writes_every_contract_value`: validate the written
    observation with
    `validate_optional_factor_evidence(contract=ADVERSE_NEWS_GATE_CONTRACT, ...)`
    and expect it to pass.
  - `test_model_version_parsed_from_response_model`: `"jev-1.14.0"` gives
    `model_name="jev"` and `model_version="1.14.0"`.
  - `test_severity_written_as_four_ordinals`.
  - `test_entity_relevance_equals_about_company_probability`.
  - `test_replay_same_exchange_is_idempotent`.
  - `test_conflicting_output_for_same_input_raises`.
  - `test_one_article_two_instruments_gets_two_records` (Review Focus 1): two
    calls with different `instrument_id` values give two observation IDs and
    two record identities.
  - `test_coverage_zero_articles_is_valid`: validate with
    `contract=NEWS_COVERAGE_GATE_CONTRACT` and `cutoff=completed_at`, and
    expect it to pass.

- [ ] **Step 2: Run, expect `ModuleNotFoundError`**

- [ ] **Step 3: Implement both persist functions**
  - **`event_at`.** It is `published_at` for classifications and
    `query_window_end` for coverage.
  - **`available_at`.** It is `article.source_available_at` for
    classifications and `completed_at` for coverage.
  - **`classified_at`.** It is `exchange.received_at`.
  - **`normalized_content_sha256`.** It is the canonical hash of the value
    payloads.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** `feat(application): persist adverse-news classifications and coverage`

---

### Task 6: TypeSafe client and classification job

**Files:**
- Create: `apps/market_data_ingestor/market_data_ingestor/typesafe_client.py`
- Create: `apps/market_data_ingestor/market_data_ingestor/adverse_news_classification.py`
- Modify: `docs/CONFIGURATION.md`, adding `TYPESAFE_API_KEY` (secret, ingestor
  only, never logged)
- Test: `apps/market_data_ingestor/tests/test_typesafe_client.py`
- Test: `apps/market_data_ingestor/tests/test_adverse_news_classification.py`

**Interfaces:**
- Consumes: Task 2 (`adverse_news_request_body`, `build_adverse_news_state`,
  `article_skip_reason`); Task 5 (`TypeSafeExchange`,
  `persist_adverse_news_classification`, `persist_news_coverage`,
  `ProviderLineage`).
- Produces:
  - **`class TypeSafeClient`**:
    - `__init__(self, *, api_key: str, http_client: httpx.Client | None = None, clock: Callable[[], datetime] = ...)`;
    - `ask(self, request_json: Mapping[str, object]) -> TypeSafeExchange`.
  - **`TypeSafeRequestError(RuntimeError)`**.
  - **`@dataclass(frozen=True, slots=True) InstrumentNewsBatch`**, with fields
    `instrument_id: int`, `company: AdverseNewsCompany`,
    `articles: tuple[AdverseNewsArticle, ...]`, `lineage: ProviderLineage`,
    `query_window_start: datetime`, `query_window_end: datetime`,
    `provider_query_identity: str`, `completed_at: datetime`.
  - **`@dataclass(frozen=True, slots=True) ClassificationRunReport`**, with
    fields `classified: int`, `skipped: Mapping[str, int]`,
    `failed_instruments: tuple[int, ...]`, `covered_instruments: tuple[int, ...]`.
  - **`classify_adverse_news(session_factory, *, client: TypeSafeClient, batches: Sequence[InstrumentNewsBatch]) -> ClassificationRunReport`**.

- [ ] **Step 1: Write failing client tests with `httpx.MockTransport`**
  - `test_ask_sends_bearer_and_exact_body`: the transport asserts the header
    value and the decoded body.
  - `test_ask_returns_exact_bytes`.
  - `test_ask_retries_429_then_succeeds`, using `retry_sync` from
    `lib_common.retries` with `retry_on=(TypeSafeRequestError,)`, retrying only
    when `is_retryable_status` is true.
  - `test_ask_raises_on_400_without_retry`.
  - `test_api_key_absent_from_error_messages_and_logs`, using `caplog`.

- [ ] **Step 2: Write failing job tests** with a fake client and SQLite sessions:
  - `test_skipped_articles_never_reach_client`;
  - `test_failed_answer_writes_nothing_for_that_article`;
  - `test_partial_run_writes_no_coverage` (Review Focus 4): the second article
    raises `TypeSafeRequestError`, so that instrument has no coverage
    observation and appears in `failed_instruments`;
  - `test_complete_batch_writes_coverage_with_all_article_ids`: coverage lists
    every returned article in `returned_article_identity`, and lists skipped
    articles again in `skipped_article_identity`.

- [ ] **Step 3: Implement**
  - One database transaction per article classification.
  - Coverage is written in its own transaction, only after every article in the
    batch is classified or skipped.
  - Log with structured fields: `instrument_id`, `provider_article_id`,
    `skip_reason`, and `error_class`.

- [ ] **Step 4: Run both test files, expect PASS**

- [ ] **Step 5: Commit** `feat(ingestor): classify adverse news with TypeSafe`

---

### Task 7: Gate resolver (inert)

**Files:**
- Create: `libs/python/lib_application/lib_application/services/adverse_news_gate.py`
- Test: `libs/python/lib_application/tests/test_adverse_news_gate.py`
- Test: `libs/python/lib_strategy/tests/test_equity_quality_compounder.py`, adding
  one regression test

**Interfaces:**
- Consumes: Tasks 1, 3, and 5.
- Produces:
  - **`@dataclass(frozen=True, slots=True) AdverseNewsGatePolicy`**, with fields
    `question_set_version: str = ADVERSE_NEWS_QUESTION_SET_VERSION`,
    `model_version: str = "1.14.0"`,
    `model_released_on: date = ADVERSE_NEWS_MODEL_RELEASED_ON`,
    `lookback_days: int = 90`, `coverage_min_delay: timedelta = timedelta(hours=6)`,
    `deadline: timedelta = timedelta(hours=72)`.
  - **`@dataclass(frozen=True, slots=True) AdverseNewsGateResult`**, with fields
    `reason: GateReason | None`, `observation_ids: tuple[str, ...]` (sorted, and
    including coverage IDs), `unsettled_count: int`.
  - **`resolve_quality_compounder_adverse_news_gate(session, *, cutoff: datetime, instrument_ids: Sequence[int], policy: AdverseNewsGatePolicy, provider_authority_policy: ProviderAuthorityPolicy) -> Mapping[int, AdverseNewsGateResult]`**.

- [ ] **Step 1: Write failing tests**

Seed the data through the Task 5 persist functions:
  - `test_clear_when_covered_and_no_adverse_articles`;
  - `test_material_adverse_news_blocks`;
  - `test_unsettled_counts_but_does_not_block`;
  - `test_coverage_gap_blocks_with_coverage_unavailable`;
  - `test_coverage_completed_before_t_plus_6h_is_rejected`;
  - `test_coverage_after_deadline_is_stale`;
  - `test_classified_after_deadline_is_invalid`;
  - `test_model_released_after_cutoff_is_invalid`;
  - `test_counted_article_without_classification_is_invalid`;
  - `test_skipped_article_is_not_missing`: a returned ID that is listed in
    `skipped_article_identity` needs no classification;
  - `test_other_question_set_version_is_skipped_not_invalid`;
  - `test_reclassification_under_new_version_does_not_hide_original_on_replay`;
  - `test_provider_correction_after_cutoff_keeps_original` (Review Focus 2);
  - `test_article_outside_window_is_skipped`.

In `test_equity_quality_compounder.py`, add
`test_configuration_digest_unchanged_by_gate`. It asserts
`quality_compounder_configuration_sha256("0.2.0")` equals the literal captured
from `main` before Task 1.

- [ ] **Step 2: Run, expect `ModuleNotFoundError`**

- [ ] **Step 3: Implement**
  - **Coverage.** Validate each coverage record with
    `contract=NEWS_COVERAGE_GATE_CONTRACT` and `cutoff=T + policy.deadline`, then
    require `available_at >= T + coverage_min_delay`.
  - **Selection.** Select classifications per spec section 8, then validate each
    with `contract=ADVERSE_NEWS_GATE_CONTRACT` and
    `cutoff=T + policy.deadline`.
  - **Availability.** Separately require `source_available_at <= T`.
  - **Verdicts.** Build answers with `AdverseNewsAnswers.from_values` and call
    `classify_article`, then `gate_reason`.
  - **Errors.** A validation error becomes `news_evidence_invalid` for that
    instrument. It never escapes as an exception for one member, and it never
    becomes "clear".

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit** `feat(application): resolve the adverse-news gate at a cutoff`

---

### Task 8: Changelog, README note, and full verification

**Files:**
- Modify: `CHANGELOG.md`, adding an `Unreleased` entry: "Adverse-news gate
  storage and classification for USQualityCompounder (inert; migration 0111)".
- Modify: `strategies/indicator/USQualityCompounder/README.md`, adding one line
  under the activation blockers that links the spec and states that the gate is
  inert until 0.3.0.

- [ ] **Step 1:** Run
  `python -m pytest libs/python/lib_strategy/tests libs/python/lib_application/tests apps/market_data_ingestor/tests tests/test_adverse_news_gate_storage_migration.py tests/test_optional_equity_factor_contracts_migration.py -q`.
  Expected: all pass.
- [ ] **Step 2:** Run `vmdev format`, then `vmdev audit --strict`. Expected: no
  failures and no new broad-exception baseline entries.
- [ ] **Step 3:** Run
  `rg -n "jev-latest|TYPESAFE_API_KEY=" --glob '!docs/**'`. Expected: no hits.
- [ ] **Step 4: Commit** `docs: note the inert adverse-news gate`

---

## Out of scope (separate plans)

- **News provider adapter.** Choose an entitled provider with first-entitled
  availability timestamps, then produce `InstrumentNewsBatch` and `ProviderLineage`.
- **Scheduling.** Register the classification job with the ingestor scheduler,
  off by default.
- **Labelled evaluation.** Build two owner-labelled sets of at least 200
  articles each, and measure precision, recall, and the unsettled rate against
  the spec section 9 bar.
- **Enabling the gate (`0.3.0`).** Add `AdverseNewsGatePolicy` to the
  configuration digest, call the resolver from
  `DatabaseQualityCompounderPanelResolver`, pass `reason` into
  `quality_compounder_market_ineligibility_reason`, add the observation IDs to
  the manifest ledger, and go through release.
- **Provider-correction writer.** Spec section 3 step 4 revisions have no
  production path yet; this is a prerequisite for `0.3.0`.
- **Migration 0111 integration run.** Run
  `tests/test_deployment_lifecycle_postgres_integration.py` (the 0111 trigger and
  grants) before `0.3.0`.
