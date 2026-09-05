# Build the production indicator strategy wheel, dependencies, managed
# environments, and shared runner image; per-strategy images are retired.

[CmdletBinding(PositionalBinding = $false)]
Param(
    [string]$Tag = "latest",
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Host "Usage: .\scripts\build_strategies.ps1 [-Tag TAG]"
    exit 0
}

vmdev build libs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

vmdev build strategies
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

vmdev build venvs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

vmdev build docker --from-config --tag $Tag
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker image inspect "vynmatrix/platform:$Tag" | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Built and verified vynmatrix/platform:$Tag" -ForegroundColor Green
