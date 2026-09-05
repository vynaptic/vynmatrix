# Activate development virtual environment (Windows)
# Usage: .\activate-dev.ps1

if (Test-Path ".venv-dev\Scripts\Activate.ps1") {
    & ".venv-dev\Scripts\Activate.ps1"
    Write-Host "Development venv activated" -ForegroundColor Green
    Write-Host "Python: $(Get-Command python | Select-Object -ExpandProperty Source)"
    Write-Host ""
    Write-Host "To deactivate: deactivate"
} else {
    Write-Host "Error: .venv-dev not found" -ForegroundColor Red
    Write-Host "Run: .\scripts\venv\create_dev_venv.ps1"
    exit 1
}
