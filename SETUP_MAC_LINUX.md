# vynmatrix setup: macOS and Linux

This page supplies platform-specific prerequisites. Continue with the shared
[SETUP.md](SETUP.md) workflow after step 3. For Windows, use
[SETUP_WINDOWS.md](SETUP_WINDOWS.md).

## 1. Install prerequisites

- Python 3.11
- Git
- Docker Desktop (macOS) or Docker Engine with the Compose plugin (Linux)
- `make` and a POSIX shell

On macOS, install the Xcode command-line tools if they are not already
available:

```bash
xcode-select --install
```

Use your operating system's package manager for Python and Git. Confirm the
interpreter is 3.11 before creating the virtual environment:

```bash
python3.11 --version
git --version
docker compose version
```

## 2. Clone the repository

```bash
git clone https://github.com/vynaptic/vynmatrix.git
cd vynmatrix
```

## 3. Create and activate the tooling environment

```bash
python3.11 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install --upgrade pip
make setup
```

Keep `.venv-dev` active when using `vmdev`, pre-commit, or focused pytest
checks. If `vmdev` is not found, reactivate the environment before reinstalling
anything.
