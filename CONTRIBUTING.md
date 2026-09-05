# Contributing to vynmatrix

Read [AGENTS.md](AGENTS.md) before changing code. It defines the architecture,
safety invariants, documentation ownership, and validation expectations used by
this repository.

## Terms

vynmatrix is source-available under the
[Vynmatrix Personal Noncommercial Reciprocity License 1.1](LICENSE), not an
OSI-approved open-source license. For every Enhancement, including a private
one, the license requires complete corresponding source under the same terms
and a good-faith pull request to
[`vynaptic/vynmatrix`](https://github.com/vynaptic/vynmatrix) within 30 days.

By opening a pull request, you represent that you may contribute the change
under the project license. Preserve existing copyright, attribution, and notice
files, and add required notices for material you introduce.

## Prepare a checkout

Complete [SETUP.md](SETUP.md) and keep the prepared `.venv-dev` active. Use the
[quick reference](docs/QUICK_REFERENCE.md) for commands; do not install an
alternate dependency set or invoke an undocumented build path.

## Branch and submit

Keep changes focused and inspect staged paths for credentials, databases,
generated artifacts, and personal configuration before committing. The retained
helper uses a main-first workflow and creates the submission branch itself:

```text
git switch main
git pull --ff-only origin main
git add <paths>
git commit -m "<conventional commit>"
git mr submit
```

`git mr submit` requires a clean local `main` ahead of `origin/main`; it creates
and pushes a feature branch, opens the pull request, then resets local `main`.
For a branch created outside this helper, push that branch and open its pull
request through GitHub. The protected `main` branch accepts pull requests only:
satisfy the required `ci-gate` check and resolve conversations before merge. Do
not bypass hooks or push directly to `main`.

Use conventional commits with an imperative subject:

- `feat:` new behavior
- `fix:` defect correction
- `refactor:` behavior-preserving restructuring
- `test:` test-only change
- `docs:` documentation
- `chore:` maintenance

## Quality contract

Run the narrowest relevant check while iterating, then the checks the change
actually reaches. Typical code changes use:

```text
vmdev format
vmdev test lib --name=<library>
vmdev test team --team=<team>
vmdev audit --strict
```

For broad changes, run `vmdev test all`; for one focused test, use `python -m
pytest <existing-path-or-node-id>` from `.venv-dev`. Paper-pipeline claims need
the separate recorded-data procedure in
[docs/E2E_VERIFICATION_GUIDE.md](docs/E2E_VERIFICATION_GUIDE.md), never a
fabricated signal or market fixture.

New production code should have type hints, explicit errors, structured logs,
and tests appropriate to its boundary. Reuse the canonical type, service, or
adapter after searching first. Do not use `# noqa` where a real fix is possible;
when an external interface forces an exception, name the specific rule and its
reason.

## Boundaries that reviewers enforce

- Strategies emit canonical `Signal` records and never call broker order APIs.
- `lib_strategy` remains domain-only; application services own persistence;
  infrastructure implements ports; apps orchestrate.
- Preserve explicit owner, broker-account, environment, instrument,
  currency/FX, market-session, and ledger authority.
- Keep `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`.
- Update the one document that owns changed behavior. Keep `AGENTS.md` and
  `CLAUDE.md` synchronized when editing agent guidance.

Use [docs/REVIEWER_CHECKLIST.md](docs/REVIEWER_CHECKLIST.md) for the review
pass and [docs/USER_MANUAL.md](docs/USER_MANUAL.md) to locate canonical symbols
and boundaries.
