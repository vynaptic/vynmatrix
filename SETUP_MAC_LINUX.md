# vynmatrix setup: macOS and Linux

This page supplies platform-specific prerequisites. Continue with the shared
[SETUP.md](SETUP.md) workflow after step 3. For Windows, use
[SETUP_WINDOWS.md](SETUP_WINDOWS.md).

## 1. Install prerequisites

- Python 3.11
- Git
- Docker Desktop (macOS) or Docker Engine with the Compose and Buildx plugins
  (Linux)
- `make` and a POSIX shell

On macOS, install the Xcode command-line tools if they are not already
available:

```bash
xcode-select --install
```

Use your operating system's package manager for Python and Git.

On Linux, including a cloud VM, install Docker Engine from Docker's own package
repository with its `docker-buildx-plugin` and `docker-compose-plugin`
packages. The image build uses BuildKit (`--build-context` and `RUN --mount`),
which the legacy builder rejects, and a distribution's own `docker.io` package
may ship without Buildx. Follow Docker's Linux post-install steps so your user
can reach the daemon without `sudo`; run `vmdev` as that user, never as root.

Confirm the interpreter is 3.11 and that Docker answers before creating the
virtual environment:

```bash
python3.11 --version
git --version
docker compose version
docker buildx version
docker version --format '{{.Server.Version}}'
```

The last command prints the Docker Engine version only when the daemon is
running and your user may use it. `vmdev doctor` checks all three again.

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
