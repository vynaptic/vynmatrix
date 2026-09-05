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
| CI and ownership | Manual image-build workflow with publishing disabled by default; registry must be configured explicitly; CODEOWNERS awaits verified maintainers |
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

- [LICENSE](../LICENSE) is preserved byte-for-byte. It retains the original
  copyright holder, business licensing contact, and proprietary terms.
- The key-format example in [BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md) contains
  only a header, ellipsis, and footer; it is not a usable private key.
- Dotted reference-library version numbers are not network addresses. Reserved
  example emails, local-development passwords, fictional tenant/account fixtures,
  public provider endpoints, and required third-party attribution are retained.

Generated test logs, caches, environments, images, and local validation connection
settings are not part of the Git tree. The seven built library/strategy wheels
were separately scanned for former branding and personal paths/identifiers.
The destination's pre-existing remote is kept in local Git configuration; its
account handle is not copied into tracked documentation. Repository ownership
will still identify the chosen GitHub account if the project is published.
New commits use a repository-local, nonpersonal maintainer identity.

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

Two PostgreSQL integration failures are inherited and remain explicit limitations:

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

## Decisions required before publication

1. **License and redistribution authority:** the current LICENSE is proprietary,
   so this repository is not yet open-source. The authorized rights holder must
   establish redistribution rights and select/authorize replacement terms.
   Required copyright and third-party notices must remain accurate.
2. **Fixture provenance and reuse records:** the S&P 500 membership file at
   `config/universe/sp500_membership_full.csv` identifies its source but lacks an
   exact revision/source and reuse record. Three frozen Coinbase candle fixtures
   under `tests/fixtures/market_data/` likewise lack recorded redistribution terms;
   the minute fixture also lacks a precise capture endpoint/time. Resolve those
   records or replace/exclude the affected fixtures deliberately while preserving
   valid test provenance. These are documentation gaps, not findings that reuse
   is prohibited.
3. **Public ownership and operations:** designate public maintainers, review
   routing, reporting/contact channels, and any publication registry. Hosting
   under a personal GitHub account makes that account's ownership visible.

PowerShell scripts were reviewed but not executed because PowerShell was not
available in the validation environment. Credentialed broker and external-provider
behavior remains unverified. No push, public release, visibility change, deployment,
or live execution was performed.
