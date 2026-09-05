# Diagnose local Python/vmdev environment state.
#
# Usage:
#   .\scripts\diagnose_environments.ps1

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }

Write-Host "=== Python Environments Diagnostic ==="
Write-Host ""

Write-Info "System Python:"
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if ($pythonCmd) {
    Write-Host $pythonCmd.Source
    & $pythonCmd.Source --version 2>&1
} else {
    Write-Warn "ERROR: python not found on PATH"
}
Write-Host ""

Write-Info "vmdev CLI:"
$vmdevCmd = Get-Command vmdev -ErrorAction SilentlyContinue
if ($vmdevCmd) {
    Write-Host $vmdevCmd.Source
    vmdev --version 2>&1
} else {
    Write-Warn "ERROR: vmdev not installed!"
}
Write-Host ""

Write-Info "Project venvs:"
if (Test-Path "build\venvs") {
    Get-ChildItem "build\venvs" -ErrorAction SilentlyContinue | Format-Table -AutoSize
    if (-not (Get-ChildItem "build\venvs" -ErrorAction SilentlyContinue)) {
        Write-Warn "ERROR: No venvs created yet. Run: vmdev build venvs"
    }
} else {
    Write-Warn "ERROR: No venvs created yet. Run: vmdev build venvs"
}
Write-Host ""

Write-Info "Custom libraries (wheels):"
if (Test-Path "build\wheels") {
    Get-ChildItem "build\wheels" -ErrorAction SilentlyContinue | Format-Table -AutoSize
    if (-not (Get-ChildItem "build\wheels" -ErrorAction SilentlyContinue)) {
        Write-Warn "ERROR: No wheels built yet. Run: vmdev build libs"
    }
} else {
    Write-Warn "ERROR: No wheels built yet. Run: vmdev build libs"
}
Write-Host ""
