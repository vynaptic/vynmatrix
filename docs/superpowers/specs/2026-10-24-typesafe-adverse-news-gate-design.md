# Adverse-news exclusion gate for USQualityCompounder, judged by TypeSafe

Status: draft for owner review (2026-10-24). Scope: a disabled-by-default
quarter-end hard gate that makes an S&P 500 member ineligible when the 90 days
before the cutoff contain confirmed, material, negative news about it. TypeSafe
Jev answers typed questions about each article; code owns every decision. No
live TypeSafe call has been made while writing this design.

## 1. Goal and non-goals

USQualityCompounder rebalances once per quarter on the final official XNYS
session. Its gates already block names whose evidence is unsafe
(`corporate_action_unresolved`, `market_data_quality_failed`, and others in
`quality_compounder_market_ineligibility_reason`). They cannot see events that
exist only in text: a restatement, a fraud allegation confirmed by a regulator,
a going-concern warning, a lost flagship contract, a product recall, a CEO
departure under investigation. A quality screen built on trailing filings can
hold or buy such a name at exactly the wrong quarter.

| Scenario | Owner wants | Today | After |
| --- | --- | --- | --- |
| A | Avoid buying a name hit by a confirmed material negative event | Invisible to every gate | Excluded with reason `market_ineligible:material_adverse_news` |
| B | Leave a held name when such an event lands | Held until rank or factors decay | Exits through the existing hold-gate path |
| C | Know why a name was excluded | — | Each exclusion cites the exact observation IDs, answers, and model identity |
| D | Trust that a news outage never looks like "no news" | — | A missing coverage record fails the member closed |

Non-goals, each deliberate:

- **A sentiment tilt.** Nothing here changes the rank, weights, or composite
  score. The registered `news_sentiment` rank sleeve stays registered and
  disabled; this gate does not activate it.
- **Positive news.** The gate only removes names. It never adds one.
- **Trading on news.** No signal is emitted between rebalances; the gate is read
  once, at the quarter-end panel build.
- **Crypto strategies.** They have no text inputs.
- **Live authority.** Paper mode and `EXECUTION_ENGINE_ALLOW_LIVE=false` are
  untouched. Enabling the gate is a strategy-version change that goes through
  the normal validation and paper-release path.
- **Model decisions.** No question asks "should we exclude this stock?"

## 2. Evidence that shapes the design

- `OptionalFactorSourceContract(NEWS_SENTIMENT)` in
  `libs/python/lib_strategy/lib_strategy/equity_optional_factors.py` already
  defines `news_event` observations with original-publication event time,
  first-entitled availability, append-only corrections, and the full
  model-transform identity (`_MODEL_TRANSFORM_DIGEST_FIELDS`). Its activation is
  `CROSS_SECTIONAL_RANK` with a 3-day half-life, which does not fit a quarterly
  book. This design keeps the source semantics and adds a separate gate sleeve.
- The contract requires hashes of model input and output but no field for the
  answers themselves. A gate cannot be recomputed from hashes, so the new
  contract adds answer fields (section 6).
- `validate_optional_factor_evidence` in
  `libs/python/lib_application/lib_application/services/equity_optional_factor_sources.py`
  already enforces cutoff safety, provider authority, pinned model identity, and
  lowercase SHA-256 digests. The gate reuses it per observation.
- `EquityFactorSnapshot` rejects any enabled optional factor whose application
  is not `CROSS_SECTIONAL_RANK`. The gate is therefore not a factor detail. It
  enters through the market-eligibility path instead:
  - `quality_compounder_market_ineligibility_reason` in
    `libs/python/lib_strategy/lib_strategy/equity_quality_compounder.py` returns
    a reason from pre-resolved inputs;
  - `build_quality_compounder_panel_input` in
    `apps/market_data_ingestor/market_data_ingestor/quality_compounder_panel.py`
    passes those inputs and stores the reason as `market_eligible=False`;
  - `_entry_failure` and `_hold_failure` then block both entries and holds.

  Both functions are pure and take no database session.
- The optional-factor registry is content-addressed. Every
  `EquityFactorSnapshot` ID and content hash includes
  `source_contract_registry_sha256` (`equity_factor_snapshots.py:352-369`), and
  a test requires one contract per `observation_kind`. Adding a second
  `news_event` contract to that registry would break the test. It would also
  change the identity of every factor snapshot.
- `quality_compounder_configuration_sha256` hashes every field of
  `QualityCompounderEvidencePolicy`, and the code accepts exactly one strategy
  version (`0.2.0`, just released for paper). Adding a policy field changes
  that version's digest.
- `validate_equity_observation_authority` rejects an observation when a later
  revision of the same source record is available by the cutoff
  (`equity_lineage.py:574-590`).
- `news_event` is already an allowed `equity_observations.observation_kind`
  (migration `0090`). No `news_coverage` kind exists.
- TypeSafe facts from the live docs (read 2026-10-24):
  - endpoint `POST https://api.typesafe.ai/v1/systemone`, bearer auth;
  - question types `noul` (returns `noul`) and `score` (returns `score`,
    `confidence`, `legend`, `probabilities`);
  - versioned model IDs `jev-1.14.0` (released 2026-10-14) and `jev-1.13.0`;
    aliases `jev-latest` and `jev-preview` move;
  - the response `model` field names the versioned ID that answered;
  - no request ID, no weights digest, and no determinism guarantee are
    published;
  - limits are 64k tokens per request and 32k per question; text only;
  - price is $0.042 per million input tokens, output free.
- Jev's training cutoff is not published. Judging an article published before
  the model existed can leak later knowledge into a backtest.

## 3. How it flows

1. **Acquire** (provider adapter, not TypeSafe). For each catalogued S&P 500
   instrument, query the entitled news provider for articles tagged with that
   instrument. Persist each article as one immutable source lineage
   (`artifact_content_sha256` over the provider record) and record one
   coverage observation per (instrument, query window), including zero-result
   windows (section 7).
2. **Filter in code.** Skip articles whose provider language is not `en`, or whose
   provider document type is not in the allowed set
   (`news`, `press_release`, `regulatory_notice`). Count skips per reason in
   the job log. Code never asks TypeSafe about an article it has already
   skipped.
3. **Ask** (TypeSafe client). Send one request per (article, instrument) with the
   question set in section 4. The request pins `"model": "jev-1.14.0"`. Reject
   the response, and retry later, when its `model` differs from the pinned ID,
   when an answer is missing, or when an answer has the wrong type. A failed call
   is an error, never an answer.
4. **Persist.** Write one `news_event` observation per classification. Each
   observation holds the provider timestamps, the raw probabilities, the model
   identity, and the digests.
   - **Identity.** `source_record_identity` is
     `<provider article id>|<instrument id>|<question_set_version>|<model_version>`.
     A new question set or model therefore writes a new record, not a revision
     of an old one, so the later-revision check in
     `validate_equity_observation_authority` never hides an older valid answer
     during a past-cutoff replay.
   - **Write once.** A record is written once per identity. A rejected response
     writes nothing.
   - **Revisions.** A revision of the same identity is used only when the
     provider corrects the article. It carries the correction's own provider
     availability time and supersedes the old row explicitly.
5. **Gate at quarter end.** A new database-side resolver,
   `resolve_quality_compounder_adverse_news_gate`, runs in the panel resolver
   before the pure builder. For each observed member it:
   - loads the coverage records and the admissible observations in
     `(cutoff − 90 days, cutoff]`;
   - validates each one (section 8);
   - applies the truth tables in section 5.

   It returns a frozen per-member result: `reason: str | None` plus the
   `observation_ids` relied on, including coverage records. The pure builder
   passes `reason` to `quality_compounder_market_ineligibility_reason`, which
   returns it after its existing checks. The observation IDs join the panel
   manifest's `source_observation_id` ledger, so the database resolver
   re-verifies the gate on replay, as it does for every other panel input.

Daily acquisition and classification is the builder's choice. A single
quarter-end batch is also acceptable, as long as it meets the classification deadline in
section 8.

## 4. Question module

Place: `libs/python/lib_strategy/lib_strategy/adverse_news_questions.py`. The
module is pure domain code with no I/O. It is the single source of truth for:

- question wording and criteria;
- the question-set version;
- thresholds;
- both truth tables;
- `prompt_or_feature_schema_sha256` and `output_schema_sha256`.

The HTTP client lives in the infrastructure layer, in
`apps/market_data_ingestor/market_data_ingestor/typesafe_client.py`. It imports
the question set; it never defines wording.

`ADVERSE_NEWS_QUESTION_SET_VERSION = "adverse-news-questions-v1"`. Any change to
wording, criteria, thresholds, or question IDs bumps this version. That changes
the prompt digest, which makes earlier observations inadmissible for the new
version.

### 4.1 State

```json
{
  "company": {
    "name": "Example Corp",
    "ticker": "EXMP",
    "sector": "Health Care",
    "industry": "Medical Devices",
    "other_names": ["Example Medical", "ExampleScan"]
  },
  "article": {
    "headline": "…",
    "source": "Provider-reported outlet name",
    "published_at": "2026-11-03T13:05:00Z",
    "body": "…"
  }
}
```

Rules:

- `company` comes from the catalogued security identity, never from the
  article. `other_names` holds former names and well-known brand or subsidiary
  names from catalogue reference data. When none are catalogued, omit the key.
  Never send an empty list.
- `article.body` is the provider's text with markup removed. If body plus state
  would exceed 24k tokens, keep the headline and the first paragraphs that fit.
  Record `body_truncated = true`. Never cut mid-paragraph.
- Leave out every other provider field: tags, other tickers, provider
  sentiment, and URLs. None of them helps a reader judge the questions.

### 4.2 Questions

All are sent in one request, so the state is read and paid for once.
Questions 4 and 5 are asked speculatively: code reads them only when the truth
table needs them.

| ID | Type | Instructions | Criteria |
| --- | --- | --- | --- |
| `about_company` | noul | Is `article` mainly about `company.name`? | true: "The article's main subject is this company, its products, its people, or its results." false: "The company is mentioned in passing, in a list of several companies, as a competitor, or as a comparison." |
| `price_recap` | noul | Is `article` mainly a report that the share price of `company.name` moved? | true: "The article mainly describes a share-price move, trading volume, or analyst price-target changes, without reporting a new event at the company." false: "The article reports an event at the company, even if it also mentions the share price." |
| `negative_development` | noul | Does `article` report a development that harms `company.name`? | true: "The article reports an event that hurts the company, such as a loss of customers, a regulatory action, a lawsuit, an accounting problem, a recall, a guidance cut, or a leadership departure under pressure." false: "The article reports only good news, neutral news, or an event that has no adverse effect on the company." |
| `negative_confirmed` | noul | Has the harmful development in `article` happened or been officially announced? | true: "The company, a regulator, a court, or another named authority has announced it, or the article reports it as an established fact." false: "The development is a rumour, comes from unnamed sources, is an analyst's speculation, or is described as a possibility." |
| `negative_severity` | score | How badly could the harmful development in `article` hurt `company.name`? | 0: "A routine setback that is unlikely to change the company's earnings outlook." 1: "A setback that could hurt the company's results for about one quarter." 2: "A setback that could reduce the company's earnings for a year or more." 3: "A setback that could threaten the company's survival, independence, or core business." |
| `new_development` | noul | Does `article` report something about `company.name` that was not public before the article's `published_at`? | true: "The article is the first report of an event, an announcement, or new facts about an earlier event." false: "The article recaps earlier news, previews a scheduled event, or repeats facts that were already public." |

The reasons behind the wording:

- **Separate questions.** "Harmful", "confirmed", and "severe" are separate
  questions, so each can be audited and tuned on its own.
- **No `positive_development` question.** The gate never uses one.
- **`price_recap` is its own question.** Price-move stories otherwise read as
  negative. In practice they restate a market reaction, not a new event.
- **`negative_severity` describes harm to the business**, not to the stock price,
  because the strategy holds for a quarter.

## 5. Truth tables

Thresholds: `YES = 0.70`, `NO = 0.30`. A Noul `p >= YES` is yes, `p <= NO` is
no, and anything between is unsettled. Severity reads the level probabilities,
not the expected score, because an expected score near the middle can hide a
two-peaked answer. Material means `P(level 2) + P(level 3) >= YES`. Immaterial
means that sum is `<= NO`. Anything between is unsettled.

### 5.1 Article verdict

Evaluate the rows top to bottom; the first match wins.

| # | `about_company` | `price_recap` | `new_development` | `negative_development` | `negative_confirmed` | severity | Verdict |
| - | - | - | - | - | - | - | - |
| 1 | no or unsettled | any | any | any | any | any | `not_about_company` |
| 2 | yes | yes | any | any | any | any | `price_recap` |
| 3 | yes | no or unsettled | no | any | any | any | `not_new` |
| 4 | yes | no or unsettled | yes or unsettled | no | any | any | `not_negative` |
| 5 | yes | no or unsettled | yes or unsettled | unsettled | any | any | `negative_unsettled` |
| 6 | yes | no or unsettled | yes or unsettled | yes | no | any | `negative_unconfirmed` |
| 7 | yes | no or unsettled | yes or unsettled | yes | unsettled | any | `negative_confirmation_unsettled` |
| 8 | yes | no or unsettled | yes or unsettled | yes | yes | immaterial | `negative_immaterial` |
| 9 | yes | no or unsettled | yes or unsettled | yes | yes | unsettled | `negative_severity_unsettled` |
| 10 | yes | no or unsettled | yes or unsettled | yes | yes | material | `adverse_confirmed_material` |

Every combination reaches exactly one row.

- **Row 3 (`not_new`).** This row is what keeps the 90-day window honest. A
  story that only recaps an event from before the window must not re-trigger
  the gate.
- **Unsettled `price_recap` or `new_development`.** These do not stop an
  article. A recap of an event that falls inside the window is harmless: one
  qualifying article is enough, so double-counting changes nothing.

### 5.2 Instrument gate at the cutoff

| Condition, checked in this order | Reason returned |
| --- | --- |
| Coverage record missing, stale, or not covering the full 90-day window | `news_coverage_unavailable` |
| Any admissible article in the window failed validation (section 8) | `news_evidence_invalid` |
| At least one `adverse_confirmed_material` article | `material_adverse_news` |
| Otherwise | none (gate clear) |

Verdicts 5, 7, and 9 are unsettled. They do not block a name, because the owner
chose to exclude only confirmed material news. Code counts them per instrument
and writes them to the panel log. That count becomes a validation metric
(section 9), so a high unsettled rate is visible instead of silent.

## 6. Gate source contract

The gate contract is **not** added to `OPTIONAL_FACTOR_SOURCE_CONTRACTS`. It
lives in a separate gate registry. That leaves three things unchanged:

- the factor-snapshot registry digest;
- every existing factor-snapshot identity;
- the one-contract-per-observation-kind invariant.

No registry migration is needed.

Changes in `equity_optional_factors.py`:

- Add enum values:
  - `OptionalFactorSleeve.ADVERSE_NEWS = "adverse_news"`
  - `OptionalFactorApplication.ELIGIBILITY_GATE = "eligibility_gate"`
  - `OptionalFactorAggregation.CONFIRMED_MATERIAL_ADVERSE_EVENT = "confirmed_material_adverse_event"`
  - `OptionalFactorNormalization.ABSOLUTE_THRESHOLD = "absolute_threshold"`

  Enum members alone do not enter any registry payload.
- Add `OPTIONAL_GATE_SOURCE_CONTRACTS = (ADVERSE_NEWS_GATE_CONTRACT,)` and
  `optional_gate_source_registry_sha256()`. The factor registry's
  `OPTIONAL_FACTOR_SLEEVES` drift check is unchanged.
- `optional_factor_source_contract(sleeve)` keeps resolving factor sleeves only.
  A new `optional_gate_source_contract(sleeve)` resolves gate sleeves.
  `validate_optional_factor_evidence` gains a keyword-only `contract` argument
  as an alternative to `sleeve_name`. Exactly one of the two must be given.

`factor_version` for the gate is `question_set_version`.

The contract:

| Field | Value |
| --- | --- |
| `sleeve` | `adverse_news` |
| `observation_kind` | `news_event` |
| `event_at_semantic` / `event_value_field` | `ORIGINAL_PUBLICATION` / `original_published_at` |
| `requires_instrument` | true |
| `activation.application` | `ELIGIBILITY_GATE` |
| `activation.direction` | `LOWER_IS_BETTER` (more adverse evidence is worse) |
| `activation.aggregation` | `CONFIRMED_MATERIAL_ADVERSE_EVENT` |
| `activation.normalization` | `ABSOLUTE_THRESHOLD` |
| `activation.missing_policy` | `INELIGIBLE` |
| `activation.maximum_source_age_days` | 90 |
| `activation.signal_half_life_days` | 90 (no decay inside the window; the field must be positive) |
| `activation.expected_holding_sessions` | (40, 63) |
| `activation.minimum_evidence_count` | 1 |
| `activation.requires_model_transform` | true |

Required values, canonically sorted by the contract helper:

| Field | Type | Source |
| --- | --- | --- |
| `about_company_probability` | decimal | `answers.about_company.noul` |
| `body_truncated` | boolean | client |
| `classified_at` | timestamp | client clock at response receipt (UTC) |
| `document_type` | text | provider |
| `entity_relevance` | decimal | same value as `about_company_probability`; kept for parity with the news-event source semantics |
| `inference_configuration_sha256` | text | section 6.1 |
| `language` | text | provider |
| `model_artifact_sha256` | text | section 6.1 |
| `model_input_sha256` | text | section 6.1 |
| `model_name` | text | `jev` |
| `model_output_sha256` | text | section 6.1 |
| `model_provider` | text | `typesafe` |
| `model_version` | text | `1.14.0`, parsed from response `model` |
| `negative_confirmed_probability` | decimal | `answers.negative_confirmed.noul` |
| `negative_development_probability` | decimal | `answers.negative_development.noul` |
| `negative_severity_probability` | decimal, ordinals 0–3 | `answers.negative_severity.probabilities["0".."3"]` |
| `new_development_probability` | decimal | `answers.new_development.noul` |
| `original_published_at` | timestamp | provider |
| `output_schema_sha256` | text | section 6.1 |
| `price_recap_probability` | decimal | `answers.price_recap.noul` |
| `prompt_or_feature_schema_sha256` | text | section 6.1 |
| `question_set_version` | text | `ADVERSE_NEWS_QUESTION_SET_VERSION` |
| `source_available_at` | timestamp | provider first-entitled delivery |

`required_digest_fields` is `_MODEL_TRANSFORM_DIGEST_FIELDS`.
`negative_severity_probability` uses the existing `ordinal` column of
`EquityObservationValue`. The validator gains one check for it: exactly four
ordinal rows, 0–3, each in [0, 1], summing to 1 within 1e-6.

The verdict is not stored. It is recomputed from the stored probabilities at
every panel build, so the question module stays the one source of truth.

`EquityFactorSnapshot` keeps rejecting non-rank applications. The gate never
enters a factor snapshot. The panel manifest records
`optional_gate_source_registry_sha256()`, but only once the gate is enabled
(section 9).

### 6.1 Digest definitions

Every hash uses `lib_common.hashing.canonical_json_hash`.

| Field | Hashed material |
| --- | --- |
| `prompt_or_feature_schema_sha256` | `{"question_set_version", "questions"}`, where `questions` is the exact `questions` object sent, with no state |
| `output_schema_sha256` | `{"question_set_version", "answers": {id: type}}` |
| `inference_configuration_sha256` | `{"endpoint", "model_requested": "jev-1.14.0", "long_context": false}`; never the API key |
| `model_input_sha256` | the request body sent: `model`, `state`, and `questions` |
| `model_output_sha256` | the response body received, including `model`, `answers`, and `usage` |

`canonical_json_hash` re-serializes parsed JSON, so these two digests cover the
parsed content, not the raw bytes on the wire. The raw bytes are kept separately
(section 6.2).
| `model_artifact_sha256` | `{"provider": "typesafe", "model": <response model>}` |

`model_artifact_sha256` is an identity digest of the versioned model ID. It is
not a digest of model weights, because TypeSafe publishes none. This doc, the
module docstring, and the evidence report must say so in those words.

### 6.2 Raw request and response retention

The observation stores the answers. The exact request and response bodies are
also kept, so any result can be audited or re-sent by hand. They go in a new
append-only table `equity_model_inference_records`, keyed by
`model_input_sha256`, with columns:

- `request_json`
- `response_json`
- `request_bytes` and `response_bytes`, the exact bytes sent and received
- `model_output_sha256`
- `received_at`
- `lineage_id`

Writes are idempotent on the key. Request bodies contain article text, so the
table inherits the article lineage's `entitlement_scope` and owner. It is never
exported. Replays read stored observations and never call TypeSafe again.

## 7. Coverage: "no news" versus "no feed"

The `INELIGIBLE` missing policy would drop every name that had no qualifying
news in 90 days. That is most of the index, so "no articles" must be provable.
Proposal: a new observation kind, `news_coverage`. The migration extends
`ck_equity_observation_kind`. Each record holds one completed provider query:

- `instrument_id`
- `query_window_start` and `query_window_end`
- `returned_article_count`
- `provider_query_identity`
- `source_available_at`, the time the query completed

A member is covered at cutoff `T` when coverage records, without gaps, span
`(T − 90 days, T]`. A failed or partial query writes no coverage record, so the
gap fails the member closed with `news_coverage_unavailable`.

Timing:

- **The final query runs after `T`.** A query that completes at or before `T`
  cannot see articles delivered close to `T`. So the last record covering `T`
  must complete after `T + 6 hours` and before the panel build deadline.
- **What it can count.** Only articles with provider `source_available_at <= T`
  count toward `returned_article_count`. Every such article must already be
  classified.
- **No lookahead.** A coverage record therefore has `source_available_at > T`
  by design. It reports facts about data available by `T`; it does not use
  later information. The gate resolver exempts coverage records from the
  `available_at <= T` check. It still applies provider authority and lineage
  validation.
- **Stale.** A coverage record is stale when its query window does not reach
  `T`, or when it completed more than 72 hours after `T`.
- **Late articles.** An article that the provider first makes available after
  `T`, even with an earlier publication time, is outside the cutoff, as the
  contract requires.

`news_coverage` gets its own gate-registry contract (no model transform).
Validation uses the same validator, with the coverage exemption above.

## 8. Admissibility at a cutoff

An observation counts at cutoff `T` only when all of these hold:

Selection first. In the window, the resolver picks the observations whose:

- `question_set_version` and `model_version` equal the frozen gate policy;
- `source_available_at <= T`;
- `original_published_at > T − 90 days`.

Records from other versions, and articles outside the window, are skipped. That
is normal and is not an error.

Each selected observation must then pass:

1. `validate_optional_factor_evidence(contract=ADVERSE_NEWS_GATE_CONTRACT, …,
   cutoff=T)`. That covers authority, pinned identities, digests, timestamps,
   staleness, and the later-correction rule.
2. **Model existed at the cutoff.** The pinned model's release date is on or
   before `T`. The release date is recorded in the gate policy, for example
   2026-10-14 for `jev-1.14.0`.
3. **Classified before the build.** `classified_at` is before the panel build
   and no later than `T + 72 hours`, the same deadline as coverage.

Classifying after `T` with a model that was released before `T` adds no later
information. The pinned model is the same artifact before and after `T`, and the
article was available by `T`.

If any selected observation fails 1–3, the member is blocked with
`news_evidence_invalid`. If coverage counts an article that has no selected
observation, the member is also blocked with `news_evidence_invalid`.

## 9. Validation before the gate is enabled

The gate ships inert, and the inert code does not touch the released `0.2.0`
identity:

- `QualityCompounderEvidencePolicy` is unchanged.
- `quality_compounder_configuration_sha256` is unchanged.
- The panel resolver does not call the gate resolver.

The acquisition, classification, and coverage jobs can therefore run and build
forward evidence while `0.2.0` keeps trading paper exactly as released.

Enabling the gate is one later change. That change:

- adds a frozen `QualityCompounderAdverseNewsGatePolicy`, holding
  `question_set_version`, `model_version`, and the window, to the configuration
  digest;
- bumps `QUALITY_COMPOUNDER_STRATEGY_VERSION` to `0.3.0`, retiring `0.2.0` to
  immutable lineage the way `0.1.0` was retired;
- wires the resolver into the panel;
- goes through the normal registration, validation, and paper-release path.

The bar for that change, frozen before any result is seen:

1. **Labeled review set.** The owner hand-labels at least 200 real S&P 500
   articles, available after 2026-10-14, with the article verdict:
   - at least 60 labeled `adverse_confirmed_material`;
   - at least 60 that are negative but unconfirmed, immaterial, or price
     recaps.

   Report precision and recall of `adverse_confirmed_material` and the
   unsettled rate. Proposed pass bar: precision ≥ 0.85, recall ≥ 0.70,
   unsettled ≤ 15%. If the bar fails, fix the state or split a question. Do not
   tune thresholds against this set.
2. **Held-out check.** Rerun on a second, untouched labeled set of the same shape.
3. **Forward event study.** For each flagged name, compare the next 20- and
   63-session total return against its sector median, on recorded prices, from
   2026-10-14 onward. This is diagnostic evidence, not a promotion gate, until
   at least 30 independent flags exist.
4. **Panel impact.** On real quarter-end panels, report how many members the
   gate removes, and from which sectors. A removal rate above 5% of the
   universe needs owner review before release.

Backtests on articles published before the model's release date are allowed
only as labeled diagnostics. They must never be offered as promotion evidence,
because Jev's training cutoff is not published.

Unit tests use fixture answers at the module boundary and need no network:

- every truth-table row;
- the threshold edges at 0.30 and 0.70 exactly;
- severity probabilities that do not sum to 1;
- a response `model` that differs from the pin;
- a missing answer;
- a `classified_at` after `T + 72 hours`;
- a model released after `T`;
- a counted article with no matching classification;
- a reclassification under a new question-set version during a past-cutoff
  replay;
- a coverage gap.

Client parsing tests use recorded TypeSafe response bodies captured with owner
permission. Live probing comes later and needs owner approval, a budget, and an
agreed sample of articles. Cost estimate for a full quarter: about 50,000
article-instrument pairs at about 3,000 tokens each, roughly 150M input tokens,
about $6.30 at current pricing.

## 10. Decided here, and left to the builder

My calls, open to owner change:

- **Window.** 90-day lookback, no decay inside it.
- **Unsettled answers.** Unsettled confirmation or severity never blocks.
- **Holds.** The gate applies to held names too, through the existing hold
  path.
- **Hashes.** `model_artifact_sha256` hashes the model ID (section 6.1).
- **Coverage.** A new `news_coverage` kind, rather than overloading
  `news_event`.
- **Raw records.** A new table for raw request and response retention.
- **Separate registry.** The gate contract sits in its own registry, so the
  factor registry, its digest, and existing snapshot identities are untouched.
- **Classification identity.** It includes the question-set and model versions,
  so a reclassification is a new record, not a revision.
- **Coverage timing.** The coverage query completes between `T + 6 h` and
  `T + 72 h`, and counts only articles available by `T`.
- **Inert first.** The gate ships inert. Enabling it is the `0.3.0` change, so
  the released `0.2.0` digest is untouched.
- **Replay.** Gate and coverage observation IDs join the panel manifest ledger,
  so replay re-verifies them.

Left to the builder:

- **Provider.** The choice of news provider adapter. It must supply
  first-entitled availability and must not use retrieval time; EODHD news is
  the obvious candidate if the entitlement covers it.
- **Schedule.** Daily or quarter-end classification.
- **Retries.** Batching and rate limits; the docs return `429`, and the SDK
  retries with backoff.
- **Errors.** Error taxonomy and structured log fields.
