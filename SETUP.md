# Local setup

This document owns the installation sequence. Everything else links here rather
than repeating it. Complete the platform prerequisites in
[SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md) or [SETUP_WINDOWS.md](SETUP_WINDOWS.md)
first; they install Docker, Python 3.11 and the `.venv-dev` tooling environment.

The result is a local paper deployment on your own machine. It provisions no
cloud host, no broker authority and no trading permission.

## 1. Prepare repository tooling

From the activated `.venv-dev` at the repository root, install the same
constrained tooling CI uses:

```text
python -m pip install --constraint docker/constraints.txt --requirement docker/requirements-svc-base.txt --requirement docker/requirements-platform.txt --editable tools/dev_cli pytest pytest-cov pre-commit mypy ruff types-PyYAML build psutil jsonschema
pre-commit install-hooks
```

The constrained install is the dependency authority for local checks. The
platform guide's `vmdev git install` keeps the repository's pre-commit and
pre-push hooks under `.githooks`; `pre-commit install-hooks` prepares their
environments. [CONTRIBUTING.md](CONTRIBUTING.md) has the pull-request workflow.

## 2. Write your private configuration

```text
vmdev init
```

It asks for the four things a machine cannot know — your email, accounting
currency, timezone and starting paper equity — and writes two untracked,
owner-only files:

| File | What it holds |
| --- | --- |
| `.env` | Every setting, with all fifteen secrets generated and distinct |
| `owner.local.yaml` | The profile that designates you as the deployment owner |

Both are private. Keep them out of commits, logs and command lines; never paste
a value from them into a terminal you are sharing. `vmdev init` never overwrites
an existing file. Pass `--email`, `--base-currency`, `--timezone` and
`--paper-equity` to run it without prompts.

Nothing needs editing by hand to get started. The optional provider keys,
strategy selectors and tuning settings are documented in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md), which remains the authority on
what every key means.

## 3. Deploy

```text
vmdev deploy
```

One command for a first install and for every upgrade after it. On an empty
machine it builds the wheels and the platform image, creates the database,
migrates it, provisions the six runtime roles, registers the reference
catalogue, creates you as the owner, creates a local paper account with the
starting equity you chose, and starts the stack.

It finishes by printing the address of your dashboard. No strategy is released
or bound, so installing grants no trading authority: the Strategies page shows
the catalogue as registered and not released, which is the honest state.

Useful flags:

```text
vmdev deploy --plan          # print what would happen, change nothing
vmdev deploy --start-only    # bring a stopped stack back up, unchanged
vmdev doctor                 # validate configuration and state, change nothing
```

Run `vmdev doctor` whenever something looks wrong. It reports missing
configuration keys, an unreachable Docker, a schema that does not match this
checkout, and a running image that is not the one that was deployed.

## 4. Open the dashboard

Open <http://127.0.0.1:8081/> in a browser on the same computer. The page asks
once for the admin key: paste the value of `BACKEND_ADMIN_API_KEY` from your
`.env`. The view is read-only — it shows your paper account, the strategies and
their bindings, and profit and loss — and the address is reachable from this
computer only. The footer names the build that is running.

## 5. Keep it up to date

After pulling new work:

```text
git pull
vmdev init --update          # adds .env keys that appeared upstream
vmdev deploy --plan          # see what the change will do
vmdev deploy
```

`vmdev init --update` generates values for new secrets and leaves every existing
value untouched. `vmdev deploy` takes a snapshot of your database before it
changes anything and restores it if a stage after the runtime stops fails. The
upgrade and rollback contract is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## 6. Contributor extras

These are for changing the code, not for running the platform, and the deploy
path does not use them:

```text
vmdev build venvs            # host virtualenvs for strategy validation
vmdev test lib --name=lib_common
vmdev audit --strict
```

`vmdev build venvs` builds the strategy-validation environment, which installs
[TA-Lib](https://ta-lib.org/) 0.6.8. Its Python package needs the TA-Lib C
library present first — `brew install ta-lib` on macOS, the distribution's
`ta-lib` development package on Linux. Skip this step unless you are running
strategy validation.

## 7. Work locally

Use [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) for supported commands,
[docs/STRATEGY_READINESS.md](docs/STRATEGY_READINESS.md) before selecting a
strategy, and [docs/E2E_VERIFICATION_GUIDE.md](docs/E2E_VERIFICATION_GUIDE.md)
for a recorded-data paper proof. A green unit test or running container is not
broker, strategy, or live-trading evidence.
