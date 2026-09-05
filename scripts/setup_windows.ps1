Param(
    [switch]$SkipVersionCheck
)

# Simple Windows setup script to mirror `make setup` for interns.
# Prereq: Python 3.11.13 on PATH, run from repo root (contains Makefile).

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host $msg -ForegroundColor Green }

# Ensure we are in repo root
if (!(Test-Path "Makefile")) {
    Write-Warn "Run this from the repo root (Makefile not found).";
    exit 1
}

# Check python version
if (-not $SkipVersionCheck) {
    try {
        $ver = (python --version).Split()[1]
    } catch {
        Write-Warn "Python not found on PATH. Install Python 3.11.13 first.";
        exit 1
    }
    if (-not ($ver.StartsWith("3.11"))) {
        Write-Warn "Expected Python 3.11.13; found $ver. Install/correct Python before continuing.";
        exit 1
    }
}

Write-Info "Creating build directories..."
New-Item -ItemType Directory -Force -Path build/wheels | Out-Null
New-Item -ItemType Directory -Force -Path build/venvs  | Out-Null
New-Item -ItemType Directory -Force -Path build/docker | Out-Null
New-Item -ItemType Directory -Force -Path data         | Out-Null

Write-Info "Installing developer CLI (editable)..."
Push-Location tools/dev_cli
python -m pip install -e .
Write-Info "Installing git MR workflow..."
python -m dev_cli.main git install
Pop-Location

Write-Ok "Setup complete!"
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1) Build libs:        vmdev build libs" -ForegroundColor Green
Write-Host "  2) Build strategies:  vmdev build strategies" -ForegroundColor Green
Write-Host "  3) Build venvs:       vmdev build venvs" -ForegroundColor Green
Write-Host "  4) Run the stack:     docker compose --env-file .env -f docker/docker-compose.stack.yml up -d" -ForegroundColor Green
