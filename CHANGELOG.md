# Changelog

This file records concise, user-visible changes in vynmatrix. Detailed design,
operational, and verification material belongs to the documents linked from
[README.md](README.md); historical source-snapshot entries remain available in
Git history.

## [Unreleased]

### Changed

- Consolidated repository documentation around one owner per topic: shared
  setup, architecture, configuration, database lifecycle, deployment, evidence,
  operations, and strategy readiness now link to one another instead of
  repeating contracts and commands.
- Updated the custom license to require source publication and an upstream pull
  request for every Enhancement, with a conditional redistribution grant.

## 2026-09-05

### Added

- Explicit single-owner bootstrap, inactive reference registration, guarded
  owner/account control-plane operations, and a three-container local runtime
  with a two-container combined alternative.
- The Vynmatrix Personal Noncommercial Reciprocity License and retained
  attribution/provenance notice for publication at `vynaptic/vynmatrix`.

### Changed

- Preserved the canonical signal → scoring → transactional outbox → execution
  → feedback path while keeping paper mode and the live-execution gate disabled.
- Recorded outstanding fixture provenance and independent-authority limits in
  [NOTICE](NOTICE) and [docs/MIGRATION.md](docs/MIGRATION.md).
