# vynmatrix setup: Windows

This page supplies PowerShell-specific prerequisites. Continue with the shared
[SETUP.md](SETUP.md) workflow after step 3. For macOS or Linux, use
[SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md).

## 1. Install prerequisites

- Python 3.11 on `PATH`
- Git for Windows
- Docker Desktop with the Compose plugin
- PowerShell

Confirm the tools before cloning:

```powershell
python --version
git --version
docker compose version
docker buildx version
docker version --format '{{.Server.Version}}'
```

Docker Desktop includes Compose and Buildx. The last command prints the Docker
Engine version only while Docker Desktop is running.

## 2. Clone the repository

```powershell
git clone https://github.com/vynaptic/vynmatrix.git
Set-Location vynmatrix
```

## 3. Create and activate the tooling environment

```powershell
python -m venv .venv-dev
.\.venv-dev\Scripts\Activate.ps1
python -m pip install --upgrade pip
.\scripts\setup_windows.ps1
```

If PowerShell blocks activation, use the execution-policy method approved for
your machine. Keep `.venv-dev` active when using `vmdev`, pre-commit, or
focused pytest checks.

The constrained install in [SETUP.md](SETUP.md) step 1 skips `uvloop`, which
has no Windows build; the Linux platform image still includes it. Windows has no owner-only file mode, so
`vmdev init` gives `.env` and `owner.local.yaml` an access-control list that
admits only you, SYSTEM and Administrators, and refuses to finish if it cannot.
