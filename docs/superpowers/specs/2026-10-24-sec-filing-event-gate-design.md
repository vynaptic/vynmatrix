# SEC filing event-risk gate for USQualityCompounder

Status: draft for owner review (2026-10-24). Scope: a disabled-by-default
quarter-end hard gate that makes an S&P 500 member ineligible when its own SEC
filings, accepted before the panel cutoff, report a listed high-risk event. The
first layer is code only: it reads 8-K item codes and late-filing notices from
EDGAR. A second layer, designed here but built later, asks TypeSafe Jev narrow
questions about filing text where a reading judgment is needed. Code owns every
decision. No live TypeSafe call has been made while writing this design.

This gate is the second evidence source beside the inert adverse-news gate from
PR vynaptic/vynmatrix#20
(`docs/superpowers/specs/2026-10-24-typesafe-adverse-news-gate-design.md`). It
reuses that PR's gate registry, validator extension, and enabling path, so its
build starts after PR #20 merges.

## 1. Goal and non-goals

The strategy's existing gates
(`quality_compounder_market_ineligibility_reason` in
`libs/python/lib_strategy/lib_strategy/equity_quality_compounder.py`) see price,
liquidity, cost, corporate-action and data-quality evidence. They cannot see an
issuer telling the SEC that its past financial statements can no longer be
relied on, that it has filed for bankruptcy, or that it cannot file its periodic
report on time. A screen built on trailing XBRL facts can rank such a name
highly at exactly the wrong quarter, because the facts it ranks on are the ones
the issuer has just disowned.

| Scenario | Owner wants | Today | After |
| --- | --- | --- | --- |
| A | Avoid buying a name that disclosed a listed SEC event | Invisible | Excluded with a stable `sec_*` reason |
| B | Leave a held name when such an event lands | Held until rank decays | Exits through the existing hold-gate path |
| C | Know why a name was excluded | — | Each exclusion cites accession numbers, items and acceptance times |
| D | Trust that an EDGAR outage never looks like "no filings" | — | Missing or partial submissions coverage fails the member closed |
| E | Test the rules on history without lookahead | Not possible for news | Acceptance timestamps allow an honest historical diagnostic |

Non-goals:

- **Scoring.** No rank, weight or composite score changes. The gate only removes
  names.
- **Trading between rebalances.** The gate is read once, at the quarter-end
  panel build.
- **Scoring-engine `event_flags`.** Out of scope; this gate is strategy-local.
- **Other forms.** Proxy statements, Form 4, 13D/G, S-1 and comment letters are
  not read.
- **Live authority.** Paper mode and `EXECUTION_ENGINE_ALLOW_LIVE=false` are
  untouched. Enabling the gate is a strategy-version change through the normal
  validation and paper-release path.
- **Model decisions.** No question asks "should we exclude this stock?"

## 2. Evidence that shapes the design

- **The submissions are downloaded at the cutoff, then dropped.**
  `QualityCompounderProducer` in
  `apps/market_data_ingestor/market_data_ingestor/quality_compounder_producer.py`
  calls `acquire_quality_compounder_sec_graphs(..., cutoff=sec_cutoff)` after
  the decision close and before the panel cutoff. `_acquire_issuer_graph` in
  `quality_compounder_sec.py` downloads each issuer's submissions pages, keeps
  only 10-K/10-Q rows (`_ELIGIBLE_FORMS`), and discards the merged dataset and
  its page sources. The 8-K and NT rows are therefore fetched but not reachable
  from what the acquirer returns.
- **Item codes are already parsed.** `SecFilingRecord.items` in
  `sec_edgar.py` holds the comma-separated `items` column, and
  `acceptance_time` is the parsed `acceptanceDateTime`. `SecFilingHeader` does
  not parse the header's `ITEM INFORMATION` lines, which carry item titles, not
  codes.
- **Availability means local retrieval.** `_sec_fact_submission` in
  `apps/market_data_ingestor/market_data_ingestor/equity_evidence.py` sets
  `available_at` to the latest `retrieved_at` of the sources used, not to EDGAR
  acceptance. The panel admits only observations with `available_at <= cutoff`.
  This gate follows the same rule.
- **A `filing` observation kind exists and is unused.** It is already in
  `ck_equity_observation_kind`. No production writer uses it today. The check
  `ck_equity_sec_observation_filing_identity` requires non-empty
  `accession_number` and `filing_form` columns on every `filing` row.
- **PR #20 provides the gate plumbing.** `OPTIONAL_GATE_SOURCE_CONTRACTS`,
  `OptionalFactorApplication.ELIGIBILITY_GATE`, the `contract=` argument of
  `validate_optional_factor_evidence`, the TypeSafe client, the digest scheme,
  `equity_model_inference_records`, and migration `0111`. PR #20 is open, not
  merged, at the time of writing.
- **The released `0.2.0` identity is pinned.**
  `quality_compounder_configuration_sha256("0.2.0")` is asserted by a test on
  the PR #20 branch. Nothing inert may change it.

## 3. Layer 1: code-only rules

### 3.1 Rule table

A rule fires for an issuer when a filing of the listed form, carrying the listed
item, has `acceptance_time` in `(T − window, T]`, where `T` is the panel cutoff.
Amendments (`8-K/A`, `NT 10-K/A`, `NT 10-Q/A`) count the same as originals.

| Rule | Form | Item | Window | Reason |
| --- | --- | --- | --- | --- |
| Non-reliance on prior financial statements | 8-K, 8-K/A | 4.02 | 365 days | `sec_non_reliance` |
| Bankruptcy or receivership | 8-K, 8-K/A | 1.03 | 730 days | `sec_bankruptcy_or_receivership` |
| Late periodic report | NT 10-K, NT 10-Q (and /A) | — | 180 days | `sec_late_periodic_report` |
| Material impairment | 8-K, 8-K/A | 2.06 | 120 days | `sec_material_impairment` |

Each window is at least 100 days, so every event is seen at one rebalance at
least, whatever the quarter length.

Items recorded but not excluding in Layer 1, because the item code alone cannot
tell good from bad:

| Item | Why code alone is not enough | Layer 2 question |
| --- | --- | --- |
| 4.01 auditor change | Routine rotation and tenders are common; only resignation, declination, or a reported disagreement is a red flag | 4.2 |
| 3.01 listing notice | Covers voluntary exchange transfers, common among S&P 500 members, as well as deficiency notices | 4.2 |
| 5.02 officer change | Mostly planned retirements and successions | 4.2 |

All other 8-K items are stored (section 5) for diagnostics and coverage audit
but never read by a rule.

### 3.2 Precedence

When several rules fire, the reason is the first match in this order:
`sec_filing_coverage_unavailable`, `sec_filing_evidence_invalid`,
`sec_bankruptcy_or_receivership`, `sec_non_reliance`,
`sec_late_periodic_report`, `sec_material_impairment`. The resolver also logs
every rule that fired, not only the first.

### 3.3 Item parsing

- An item string must match `^\d\.\d{2}$` after stripping whitespace. 8-K rows
  accepted since 2004-08-23 use this format.
- An 8-K or 8-K/A accepted in `(T − 730 days, T]` with an empty `items` column,
  or any item that does not match, makes the member
  `sec_filing_evidence_invalid`. The scope is the widest rule window, because a
  malformed row could hide a 1.03. Code never guesses an item from the
  document. The historical diagnostic (section 8) reports how often this
  fires; a non-trivial rate returns to the owner.
- NT forms carry no items. They fire on the form alone.
- **No header cross-check.** The `items` column is EDGAR's structured metadata
  from the filer's own submission; the header's `ITEM INFORMATION` lines are
  titles derived from the same selection. A title-to-code mapping would add a
  fragile failure path without independent evidence.

### 3.4 Issuer to instrument

Filings are issuer-level (CIK). The rule result applies to every listed share
class of that issuer, through the same CIK-to-instrument binding the
fundamentals graph uses (`_resolve_catalogue_instruments`).

## 4. Layer 2: TypeSafe judgments (designed now, built later)

### 4.1 Reuse the adverse-news question set, or a separate one

Decision: a separate question set,
`SEC_FILING_QUESTION_SET_VERSION = "sec-filing-questions-v1"`, in a new pure
module `libs/python/lib_strategy/lib_strategy/sec_filing_questions.py`. It reuses
everything else from PR #20: the TypeSafe client, the pinned `jev-1.14.0`
model, the digest definitions, `equity_model_inference_records`, the
"failed call is an error, never an answer" rule, the YES/NO thresholds of 0.70
and 0.30, and the gate-registry validator.

Why not feed filing text through the news questions as `regulatory_notice`:

- Four of the six news questions (`about_company`, `price_recap`,
  `new_development`, `negative_confirmed`) are settled by the form itself. An
  issuer's own 8-K is about the issuer, is not a price recap, is new at
  acceptance, and is official. Asking them adds cost and unsettled answers, and
  any "no" would be a model error that silently clears a real event.
- `negative_severity` measures business harm. The filing questions are about a
  specific fact: was the auditor dismissed for cause, is there going-concern
  doubt. A planned CFO retirement could still read as mildly harmful and an
  abrupt one could read as low-severity; severity is the wrong axis.
- A shared version would couple the two gates: rewording a filing question
  would force reclassifying every news article, and the reverse.
- The news truth table excludes only `adverse_confirmed_material`. The filing
  questions need their own small truth tables per item.

### 4.2 Questions and truth tables

Code extracts the text sent. Items are located by their `Item N.NN` headings in
the 8-K primary document. Going-concern passages are located by the phrase
"substantial doubt", which ASC 205-40 and the auditing standards require. Code
sends only the extracted section or paragraphs, never the whole filing. The
state names the company from the catalogue, as in the news design.

| Item | ID | Type | Question |
| --- | --- | --- | --- |
| 5.02 | `officer_leaves` | noul | Does the section report that the CEO, CFO, principal accounting officer, president or COO leaves that role? |
| 5.02 | `departure_planned` | noul | Is the departure a planned retirement or succession, announced with a transition period or successor? |
| 5.02 | `departure_abrupt` | noul | Is the departure a termination, effective immediately without a successor, or linked to an investigation, misconduct, policy violation, or disagreement? |
| 4.01 | `auditor_resigned` | noul | Did the auditor resign or decline to stand for re-appointment, rather than being replaced by the company's choice? |
| 4.01 | `disagreement_reported` | noul | Does the section report a disagreement with the auditor or a reportable event, such as a material weakness the auditor raised? |
| 3.01 | `listing_deficiency` | noul | Does the section report a notice that the company fails a listing standard, or a delisting decision, rather than a voluntary transfer of its listing? |
| 10-K, 10-Q | `going_concern_doubt` | noul | Does the passage state that substantial doubt exists about the company's ability to continue as a going concern, rather than saying such doubt was alleviated or describing it only as a possible risk? |

Truth tables. "Yes" means `p >= 0.70`, "no" means `p <= 0.30`, anything else is
unsettled. Unsettled answers never block; they are counted and logged, as in the
news gate.

| Item | Blocks when | Window | Reason |
| --- | --- | --- | --- |
| 5.02 | `officer_leaves` yes and `departure_abrupt` yes and `departure_planned` not yes | 120 days | `sec_abrupt_officer_departure` |
| 4.01 | `auditor_resigned` yes, or `disagreement_reported` yes | 365 days | `sec_adverse_auditor_change` |
| 3.01 | `listing_deficiency` yes | 180 days | `sec_listing_deficiency` |
| 10-K, 10-Q | any passage with `going_concern_doubt` yes | 365 days | `sec_going_concern_doubt` |

A 10-K or 10-Q with no "substantial doubt" phrase makes no model call and
clears that question. That is a keyword filter, so it can miss paraphrase; the
required wording of the standards makes that rare, and the Layer 2 validation
measures it.

Layer 2 judgments are stored as a new `filing_judgment` observation kind with
the PR #20 model-transform fields, keyed
`<accession>|<instrument id>|<question_set_version>|<model_version>`. Like the
news gate, a judgment of a filing accepted before the pinned model's release
date is a diagnostic only. Layer 2 has its own plan, written after PR #20 merges
and after the owner approves a live-probe budget.

## 5. Storage

Layer 1 needs one migration, after PR #20's `0111`:

- Extend `ck_equity_observation_kind` with `filing_coverage`.
- No new table.

**Fresh per fetch.** Every quarter-end fetch writes a complete new set of
`filing` and `filing_coverage` observations. A panel reads only the set from
its own fetch. Nothing is carried forward from an earlier quarter, so there is
no question of which coverage row an old filing belongs to.

**`filing` observations.** One per (accession, listed instrument, fetch), for
every 8-K, 8-K/A, NT 10-K, NT 10-Q and their amendments accepted in
`(T − 730 days, T]`.

- `source_record_identity`: `<accession>|<instrument id>|<fetch cutoff, ISO UTC>`.
- `accession_number` and `filing_form` columns filled, as
  `ck_equity_sec_observation_filing_identity` requires.
- `event_at`: EDGAR `acceptance_time`.
- `available_at`: the submissions page's `retrieved_at`, per the repository
  rule.
- Values: `cik`, `filing_date`, `acceptance_time_raw`, and `item` as text rows
  with ordinals `0..n-1` in filing order.
- Lineage: the exact submissions page (`SecSourceDocument`, with its content
  SHA-256).
- An identity is written once. A second write of the same identity with
  different content is an error, not a correction: EDGAR filings are immutable
  and an amendment is a new accession.

**`filing_coverage` observations.** One per (issuer CIK, listed instrument, fetch).

- Values: `window_start`, `window_end` (= the fetch cutoff),
  `oldest_listed_filing_date`, `submission_page` (one text row per page used,
  with its SHA-256), and `listed_accession` (one text row per gate-relevant
  accession in the window).
- Coverage holds at `T` when `window_end <= T`, `window_end >= T − 6 hours`, and
  the oldest filing in the pages kept is dated on or before `T − 730 days`, or
  the issuer has no older page. That last check matters: the `recent` block
  holds the latest 1,000 filings, which for a heavy filer of prospectus
  supplements can span less than a year.
- Every `listed_accession` must have a valid `filing` observation from the same
  fetch; otherwise `sec_filing_evidence_invalid`.

Both kinds get gate-registry contracts, `SEC_FILING_EVENT_GATE_CONTRACT` and
`SEC_FILING_COVERAGE_GATE_CONTRACT`, added to PR #20's
`OPTIONAL_GATE_SOURCE_CONTRACTS`. They are not added to the factor registry, so
factor-snapshot identities do not change. Contract fields:

- new enum members: `OptionalFactorSleeve.SEC_FILING_EVENTS`,
  `OptionalFactorObservationKind.FILING` and `.FILING_COVERAGE`,
  `OptionalFactorAggregation.SEC_FILING_EVENT_RULES`; coverage reuses
  `COVERAGE_ATTESTATION`;
- `application = ELIGIBILITY_GATE`, `normalization = ABSOLUTE_THRESHOLD`,
  `missing_policy = INELIGIBLE`, `requires_model_transform = false`;
- `event_at_semantic`: acceptance time for `filing`, fetch completion for
  coverage;
- `maximum_source_age_days = 1`. Freshness applies to `available_at`, which is
  the fetch time, not to acceptance time. Rule windows are applied by the pure
  rule function on `event_at`.

## 6. Flow

1. **Acquire.** `acquire_quality_compounder_sec_graphs` and its return type stay
   unchanged, so the `0.2.0` panel path is untouched. A new sibling,
   `acquire_sec_filing_events` in
   `apps/market_data_ingestor/market_data_ingestor/sec_filing_events.py`,
   downloads each issuer's submissions a second time with
   `SecEdgarClient.fetch_submissions(cik, since=T − 760 days)`, keeps the page
   sources, and selects the gate-relevant forms with `acceptance_time <= cutoff`.
   The second download is about 500 issuers of small JSON pages under the
   client's existing throttle, a few minutes per quarter. It runs after the
   fundamentals acquisition and before the panel cutoff.
2. **Persist.** In its own transaction, after the panel's SEC persistence.
   While the gate is inert, any acquisition or persistence failure is logged and
   leaves coverage missing; it never fails the `0.2.0` panel.
3. **Rule evaluation, pure.** `sec_filing_gate_reason(filings, coverage,
   cutoff, policy)` in `libs/python/lib_strategy/lib_strategy/sec_filing_rules.py`
   applies section 3 and returns a frozen result: `reason`, all rules fired, and
   the accession numbers relied on. No I/O.
4. **Resolve at the cutoff.** `resolve_quality_compounder_sec_filing_gate` in
   `libs/python/lib_application/lib_application/services/sec_filing_gate.py`
   loads coverage and filings per member, validates each with
   `validate_optional_factor_evidence(contract=...)` and
   `validate_equity_observation_authority`, and calls the pure function. The
   observation IDs join the panel manifest's `source_observation_id` ledger, so
   replay re-verifies them.

## 7. Enabling

Inert first, exactly as the news gate:

- `QualityCompounderEvidencePolicy` and the `0.2.0` configuration digest stay
  unchanged.
- The panel resolver does not call the gate resolver.
- Acquisition and persistence run, so forward evidence accumulates.

The enabling change adds a frozen `QualityCompounderSecFilingGatePolicy` (rule
table, windows, coverage tolerance) to the configuration digest, wires the
resolver into the panel, and bumps the strategy version. If the news gate is
ready at the same time, both go into one `0.3.0` and the reason passed into
`quality_compounder_market_ineligibility_reason` is the SEC reason first, then
the news reason. Otherwise the first ready gate takes `0.3.0` and the other the
next version.

`quality_compounder_market_ineligibility_reason` gains one keyword,
`event_risk_reason: str | None = None`, returned after the existing checks. That
keyword lands only with the enabling change.

## 8. Validation before enabling

Unlike news, Layer 1 can be checked on history without lookahead, because EDGAR
accepted the filing at a recorded instant and filings are immutable.

**Historical diagnostic.** Download the submissions JSON once for every issuer
that was an S&P 500 member at any quarter-end in the study period, and record it
as source lineage. For each quarter-end `T` from 2010 to the latest complete
quarter, replay the rules with `acceptance_time <= T` standing in for
`available_at <= T`. This proxy is a diagnostic, labelled as such; it never
enters a panel. Report, per rule and in total:

- members removed per quarter, and by sector;
- for flagged names, the next 63-session total return against the sector
  median, and the share below −20%, compared with unflagged members.

The study needs point-in-time membership and recorded prices. If the repository
holds only current membership, the report is labelled survivorship-biased, and
it can only argue for dropping a rule, never for adding one.

**Bar, frozen before results are seen:**

1. Mean removal across quarters ≤ 3% of the universe. A higher rate needs owner
   review.
2. A rule whose flagged names show a higher mean 63-session excess return and a
   smaller left tail than unflagged names is dropped. Rules with fewer than 30
   flags in the study are kept on prior grounds as safety rules, and the report
   says so.
3. Forward check: at least one live quarter-end where the inert gate's result
   matches the acceptance-time replay for the same `T`, member for member.

Unit tests use fixtures at the module boundary, no network:

- every rule at its window edges (`T − window` excluded, `T` included);
- `8-K/A` and `NT 10-Q/A` counted;
- malformed or empty items, inside and outside the 730-day scope;
- NT forms firing without items;
- coverage missing, stale (older than 6 hours), and not reaching back 730 days;
- a listed accession without a `filing` observation;
- multi-class issuer fan-out;
- precedence when several rules fire;
- `0.2.0` configuration digest unchanged.

Client and parser tests use recorded EDGAR submissions pages.

## 9. Decided here, open to owner change

- **Rules that exclude in Layer 1:** 4.02, 1.03, NT 10-K/NT 10-Q, 2.06.
- **Rules that do not:** 4.01, 3.01, 5.02 wait for Layer 2.
- **Windows:** 365 / 730 / 180 / 120 days; every window at least 100 days.
- **NT forms added.** Not in the original idea; a late-filing notice from an
  S&P 500 issuer is a strong, code-readable accounting signal.
- **Amendments count.**
- **No header cross-check;** the `items` column is trusted as EDGAR metadata.
- **Malformed items fail closed** across the 730-day scope.
- **Second submissions download** rather than changing the `0.2.0` acquirer.
- **Fresh observations per fetch;** nothing carried across quarters.
- **Separate question set** for Layer 2, reusing the PR #20 machinery.
- **Layer 2 deferred** to its own plan after PR #20 merges and a live-probe
  budget is approved.
- **`filing` kind reused,** one new `filing_coverage` kind.
- **Availability = retrieval,** as for the existing SEC facts; acceptance time
  is used only by the labelled historical diagnostic.
- **Coverage tolerance:** the submissions fetch must complete within 6 hours
  before `T`.
- **Holds are gated** through the existing hold path.
- **Precedence:** SEC reason before news reason.

Left to the builder:

- Error taxonomy and structured log fields.
- Where the historical diagnostic lives within `vmdev strategy` tooling, and its
  report format (not committed).
- Whether `filing_coverage` stores page digests as values or lineage links, as
  long as replay can re-verify them.
