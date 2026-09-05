# vynmatrix migration and publication readiness

This independent local repository was exported from the committed `legacy-platform`
snapshot `a739b499c66dfea3895dd37966b120c9e81ed22d`. It starts with a new root commit;
source history, branches, tags, author metadata, and local Git configuration were
not imported. The source checkout and branch were not edited.

## Scope and change map

All 939 committed source files were retained. No source runtime state, private
local datasets, credentials, ignored/untracked checkout files, virtual environments,
build caches, or Git object database were copied. The snapshot has no submodules,
Git LFS pointers, symlinks, binary assets, archives, notebooks, or embedded documents.
Its small frozen public-data test fixtures remain available for reproducible tests.

| Area | Result |
| --- | --- |
| Product and package identity | `vynmatrix` root metadata, service descriptions, messages, and `vynmatrix_indicator` strategy distribution |
| Runtime contracts | Consistent schema/provider namespaces and payload identities; frozen test digests regenerated after confirming financial values and raw candles are unchanged |
| Developer tooling | `vmdev` retained; image ownership labels, source-repository label, build prefixes, cleanup selectors, validation identifiers, and matching tests updated |
| Docker | `vynmatrix/*` images; independent Compose project name; project-scoped containers, volumes, and networks; service-DNS defaults preserved |
| Examples and fixtures | Personal identities replaced with demonstration users; example email addresses use reserved domains; ambiguous account identifiers made explicitly fictional |
| CI and ownership | Manual image-build workflow with publishing disabled by default; registry must be configured explicitly; CODEOWNERS routes default review to `@vynaptic` |
| Contributor documentation | Portable setup, architecture, configuration, testing, Docker, readiness, and agent instructions; no inherited deployment/certification claims |
| Git quality checks | Repository-local Git alias and hooks path; tracked pre-commit wrapper keeps quality checks active beside pre-push; real-commit regression tests enforce both isolation and rejection behavior |

Generic `VM_*` configuration keys, `vm_*` database roles, and `vmdev` are retained
because they remain valid identifiers and form existing functional contracts.
Docker resources no longer use globally fixed container names. Existing persisted
artifacts with former branded schema identifiers must be regenerated; no runtime
migration or compatibility with old attestations is claimed.

## Privacy and artifact review

The review covered every retained file and filename, hidden repository metadata,
configuration and SQL examples, documentation, public URLs, credential-shaped
strings, home paths, Git-boundary inputs, and built wheel members/metadata.
All source files are UTF-8 text. No retained personal names, personal contact
addresses, personal home paths, stored private keys, or private account records
were identified after sanitization. This is a scoped audit result, not a guarantee
of legal clearance or anonymity.

The deliberate exceptions and false positives are:

- At the time of sanitisation, [LICENSE](../LICENSE) was preserved byte-for-byte
  with its original copyright holder, business licensing contact, and proprietary
  terms. The 2026-09-05 publication decision below supersedes that historical
  license status while retaining copyright attribution.
- The key-format example in [BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md) contains
  only a header, ellipsis, and footer; it is not a usable private key.
- Dotted reference-library version numbers are not network addresses. Reserved
  example emails, local-development passwords, fictional tenant/account fixtures,
  public provider endpoints, and required third-party attribution are retained.

Generated test logs, caches, environments, images, and local validation connection
settings are not part of the Git tree. The seven built library/strategy wheels
were separately scanned for former branding and personal paths/identifiers.
At sanitisation, the destination remote was kept only in local Git configuration.
The current publication identifies the canonical `vynaptic/vynmatrix` repository
in documentation. New commits use the repository-local maintainer identity
configured for the release.

## Validation

| Check | Result |
| --- | --- |
| `vmdev test all` | 3,250 passed, 26 skipped; one upstream dependency deprecation warning |
| `vmdev audit --strict` | Passed across the staged source tree |
| All declared pre-commit hooks | Passed, including Ruff, formatting, mypy, syntax, private-key detection, and architecture checks |
| CI scoring/feedback mypy check | Passed: 40 source files |
| `vmdev build libs` and `vmdev build strategies` | Passed: six library wheels and one strategy wheel |
| Wheel privacy review | Seven wheels scanned; no former-brand/personal-path/identifier matches |
| `vmdev build docker --from-config --tag latest` | Passed: shared base plus all five declared service images |
| Both Compose configurations | Parsed successfully |
| Disposable PostgreSQL setup | Fresh migrations, runtime roles, initial seed, and repeated seed passed |
| Container module imports | All six service entry modules passed with initialized PostgreSQL on an internal network |
| PostgreSQL integration | Four passed; the same two failures reproduced on the unmodified snapshot |
| Alembic/ORM schema drift | Passed: zero differences |
| PowerShell | Static review only; interpreter unavailable |

The unmodified snapshot passed 3,247 non-integration tests with the pinned Python
3.11 dependency set (2 skips and 24 deselections). An initial run with host
packages and no Git index produced environment-related failures; those were
resolved in the validation harness before assessing migration regressions.

Renaming metadata changed a fixture digest, its transitive correctness-attestation
digest, and a model-definition digest. The original values were independently
reproduced, the new values were frozen in their consumers, and 167 affected tests
passed without weakening assertions. Raw candles and financial values were not
changed.

Two PostgreSQL integration failures were inherited in the recorded baseline:

- `test_public_strategy_pipeline_postgres_integration.py`: the paper account
  fixture uses a descriptive external account reference, while the paper broker
  reports the database account ID. The unchanged identity check rejects four
  CLOSE deliveries. The normal-path expiry rejection of four historical LONG
  signals is intentional and is not the failure.
- `test_service_roles_postgres_integration.py`: the expected denial of market-data
  SELECT access to the strategy catalogue conflicts with the explicit grant in
  migration `0086_equity_factor_evidence.py`. Both the expectation and migration
  are unchanged from the source snapshot.

A fresh baseline database reproduced exactly the same two failures (four other
integration tests passed). No trading gate or privilege was weakened to make
these inherited tests pass. They must be reconciled before claiming complete
pipeline or production readiness.

The Docker checks use only locally built declared images, a separate Compose
project, a verified non-default loopback database port, and disposable volumes.
Provider services are not started; import checks do not launch application loops.
The execution entry module restores pending-order state during import and therefore
requires the disposable database; this check used an empty migrated ledger.
All validation containers, networks, and volumes were removed afterward.
Only test identities are supplied. No live order gate, external broker account,
production database, publication workflow, or deployment is exercised.

## Single-owner implementation follow-up (2026-09-05)

The working tree adds the owner/control-plane/catalogue migrations `0099`–`0104`,
explicit bootstrap/onboarding and the consolidated platform runtime. The original
baseline results above remain historical. The two inherited tests now have targeted
fixture corrections: local-paper identity uses the database account ID, and market
catalogue SELECT expectations reflect `0086` while write denials remain asserted.
These changes do not weaken execution identity checks or widen runtime privileges.

The final `vmdev test all` run passed **3,625 tests**, skipped 121 opt-in or
environment-dependent cases, and reported four warnings. Separate PostgreSQL runs
passed the full fresh/repeated bootstrap case, 94 owner/catalogue/migration cases,
five existing feedback/scoring/notification/service-role cases, and the recorded-data
public pipeline case. Its companion SQLite fixture check also passed. Schema drift
against the migration-built PostgreSQL database was zero. Ruff and formatting passed
for all 110 changed Python files; focused type checks and `vmdev audit --strict`
also passed.

Real database verification exposed and resolved three implementation gaps: reference
patches now cast desired JSON to the installed column type; frozen scoring commands
retain the selected account's authority instead of the diagnostic default account;
and restore passes the archive from byte zero to `pg_restore` after checking its
header. The account fix also removes incompatible default-account financial caches
and credential fallback. Execution identity, freshness, outbox and ledger guards
remain unchanged. Exact local `paper`/`paper` onboarding creates no broker credential
or ciphertext; other broker accounts retain atomic encrypted-secret onboarding.

`vmdev build libs`, `vmdev build strategies`, `vmdev build venvs`, and
`vmdev build docker --from-config --tag latest` passed. The declared platform image
passed dependency checks, application/API imports and shipped-catalogue validation
in a removed maintenance job with network/database connections explicitly blocked.
The final working-tree platform image is
`sha256:797501b73138153349f4334431ab3824f14e272b85143bbb1d91410b01ce28aa`.

The isolated local Compose lifecycle also passed against a separate runtime test
database. Fresh and repeated `vmdev db bootstrap` preserved the sole owner ID,
edited profile, account key, reference counts and inactive/registered strategies.
Real `vmdev` profile/account and catalogue dry-run/apply/repeat operations passed;
source conflicts refused to overwrite an acknowledged metadata edit. Sampling the
repeat lifecycle observed at most three running project containers, with only
PostgreSQL and the bootstrap job running during maintenance. PostgreSQL and the
backend published only loopback ports.

Both three-container and two-container layouts passed process health checks.
The combined layout verified four separately scoped runtime database credentials,
paper/live gates and a bounded backend restart without restarting sibling processes.
Unselected strategy workers correctly reported unconfigured readiness. Anonymous and
caller-selected-owner API requests were rejected. Backup produced a mode-0600 custom
archive; transactional restore recovered the original profile after an acknowledged
test edit, left runtime stopped and preserved grants. `vmdev db migrate` then checked
the restored current-head database, and bootstrap restarted the split layout.
Graceful application/worker shutdown returned exit code zero.

The recorded-data PostgreSQL pipeline proves two-account historical execution,
fill/ledger accounting, duplicate/retry handling, normal historical freshness
rejection and least-privilege feedback using the existing June candle fixture and
component strategy cores. It is not a current-time Swing Docker economic-order
witness. Swing's strict explicit-forecast gate remains enabled; the current source's
price-ladder entries cannot supply that proof. No July witness, strategy certification,
live broker connectivity, cloud deployment or load-capacity result is inferred.

Verification used only the isolated `vynmatrix-single-owner-verify` Compose project,
loopback port 55432 and test identities/accounts. The owner explicitly authorized
resetting its unused test database; the failed replay snapshot was archived before
that exact reset. Successful database evidence and protected archives remain local.
The temporary verification stack was stopped with volumes retained; the unrelated
existing PostgreSQL service was left unchanged.
No cloud deployment, rights transfer, or strategy certification follows from the
code changes. Source publication is governed by the 2026-09-05 decision below;
see [SINGLE_OWNER.md](SINGLE_OWNER.md) for the reviewed design and
[DATABASE.md](DATABASE.md) for the current operating contract.

## Publication decision and remaining provenance work (2026-09-05)

1. **License and repository:** the repository is published at
   [`vynaptic/vynmatrix`](https://github.com/vynaptic/vynmatrix) under the
   [Vynmatrix Personal Noncommercial Reciprocity License 1.0](../LICENSE).
   It is publicly source-available for personal, noncommercial use and is not an
   OSI-approved open-source license. The license retains VisionMaverick copyright
   attribution and is not evidence of a separate copyright transfer.
2. **Fixture provenance and reuse records remain unresolved:** the S&P 500 membership file at
   `config/universe/sp500_membership_full.csv` identifies its source but lacks an
   exact revision/source and reuse record. Coinbase-related frozen fixtures under
   `tests/fixtures/market_data/` likewise have incomplete capture and
   redistribution records. Resolve those records or replace/exclude the affected
   fixtures deliberately while preserving valid test provenance. These are
   documentation gaps, not findings that reuse is prohibited. [NOTICE](../NOTICE)
   keeps this limitation visible.
3. **Public ownership and operations:** `@vynaptic` owns the canonical repository,
   receives default CODEOWNERS routing, and protects `main` through a pull-request
   policy. A private Code of Conduct reporting channel and any image registry
   destination remain unconfigured.

PowerShell scripts were reviewed but not executed because PowerShell was not
available in the validation environment. Credentialed broker and external-provider
behavior remains unverified. No deployment or live execution was performed.
