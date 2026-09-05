# Create development virtual environment with all libraries installed
# PowerShell equivalent of scripts/venv/create_dev_venv.sh
#
# This script creates a single development venv that includes all libraries
# for local development, testing, and IDE support. This venv is ONLY for
# development - production uses Docker containers.
#
# Usage:
#   .\scripts\venv\create_dev_venv.ps1

$ErrorActionPreference = "Stop"

# Helper functions
function Write-Info($msg)  { Write-Host $msg -ForegroundColor Cyan }
function Write-Warn($msg)  { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)    { Write-Host $msg -ForegroundColor Green }
function Write-Error2($msg) { Write-Host $msg -ForegroundColor Red }

Write-Ok "====================================="
Write-Ok "Creating Development Virtual Environment"
Write-Ok "====================================="

# Check if we're in the repository root
if (!(Test-Path "config/build.yaml")) {
    Write-Error2 "Error: Must run from repository root"
    exit 1
}

# Configuration
$VenvDir = ".venv-dev"
$PythonVersion = "3.11"

# Step 1: Check Python version
Write-Host ""
Write-Info "Step 1: Checking Python version..."

$PythonCmd = "python"
try {
    $versionOutput = & $PythonCmd --version 2>&1
    $version = $versionOutput.ToString()

    if ($version -notmatch "3\.11") {
        Write-Warn "Warning: Expected Python 3.11.x, found: $version"
        Write-Warn "Trying 'python3' command..."

        $PythonCmd = "python3"
        $versionOutput = & $PythonCmd --version 2>&1
        $version = $versionOutput.ToString()

        if ($version -notmatch "3\.11") {
            Write-Error2 "Error: Python 3.11 not found"
            Write-Host "Please install Python 3.11 first"
            exit 1
        }
    }

    Write-Host "Found: $version"
} catch {
    Write-Error2 "Error: Python not found on PATH"
    Write-Host "Please install Python 3.11 first"
    exit 1
}

# Step 2: Remove existing venv if present
if (Test-Path $VenvDir) {
    Write-Host ""
    Write-Info "Step 2: Removing existing venv..."
    Remove-Item -Recurse -Force $VenvDir
}

# Step 3: Create new venv
Write-Host ""
Write-Info "Step 3: Creating new virtual environment..."
& $PythonCmd -m venv $VenvDir
if ($LASTEXITCODE -ne 0) {
    Write-Error2 "Failed to create virtual environment"
    exit 1
}
Write-Host "Virtual environment created at: $VenvDir"

# Use the venv's python for all pip actions
$PythonExe = "$VenvDir\Scripts\python.exe"

# Step 4: Upgrade pip
Write-Host ""
Write-Info "Step 4: Upgrading pip..."
& $PythonExe -m pip install --quiet --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    Write-Error2 "Failed to upgrade pip"
    exit 1
}

# Step 5: Install library wheels
Write-Host ""
Write-Info "Step 5: Installing library wheels..."

if (!(Test-Path "build\wheels") -or (Get-ChildItem "build\wheels\*.whl" -ErrorAction SilentlyContinue).Count -eq 0) {
    Write-Error2 "Error: Library wheels not found"
    Write-Host "Run: vmdev build libs"
    exit 1
}

# Install in dependency order
$Libs = @("lib_common", "lib_data", "lib_indicators", "lib_strategy", "lib_application", "lib_infrastructure")
foreach ($lib in $Libs) {
    $wheelFile = Get-ChildItem "build\wheels\$lib-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($wheelFile) {
        Write-Host "Installing $lib..."
        & $PythonExe -m pip install --quiet $wheelFile.FullName
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Warning: Failed to install $lib"
        }
    } else {
        Write-Warn "Warning: $lib wheel not found, skipping"
    }
}

# Step 6: Install framework wheels
Write-Host ""
# No framework packages are configured.
Write-Info "Step 6: No framework packages configured (skipping)"

# Step 7: Install development dependencies
Write-Host ""
Write-Info "Step 7: Installing development dependencies..."
if (Test-Path "requirements-dev.txt") {
    & $PythonExe -m pip install --quiet -r requirements-dev.txt
} else {
    # Install common dev dependencies
    & $PythonExe -m pip install --quiet pytest pytest-cov pytest-asyncio mypy ruff black isort flake8
}

# Step 8: Verify installation
Write-Host ""
Write-Info "Step 8: Verifying installation..."
$verifyScript = @"
import sys
print('Python version:', sys.version)
import lib_common
print('lib_common:', lib_common.__version__)
import lib_indicators
print('lib_indicators: OK')
import lib_strategy
print('lib_strategy: OK')
import lib_application
print('lib_application: OK')
import lib_infrastructure
print('lib_infrastructure: OK')
print('All libraries installed successfully!')
"@

& $PythonExe -c $verifyScript
if ($LASTEXITCODE -ne 0) {
    Write-Error2 "Verification failed"
    exit 1
}

# Step 9: Create activation helper
Write-Host ""
Write-Info "Step 9: Creating activation helper..."
$activateContent = @"
# Activate development virtual environment
# PowerShell script for Windows
# Usage: .\activate-dev.ps1

if (Test-Path ".venv-dev\Scripts\Activate.ps1") {
    & ".venv-dev\Scripts\Activate.ps1"
    Write-Host "✅ Development venv activated" -ForegroundColor Green
    Write-Host "Python: `$(Get-Command python | Select-Object -ExpandProperty Source)"
    Write-Host ""
    Write-Host "To deactivate: deactivate"
} else {
    Write-Host "Error: .venv-dev not found" -ForegroundColor Red
    Write-Host "Run: .\scripts\venv\create_dev_venv.ps1"
    exit 1
}
"@

$activateContent | Out-File -FilePath "activate-dev.ps1" -Encoding UTF8

Write-Host ""
Write-Ok "====================================="
Write-Ok "✅ Development venv created successfully!"
Write-Ok "====================================="
Write-Host ""
Write-Host "Location: $VenvDir"
$pythonVer = & $PythonExe --version
Write-Host "Python: $pythonVer"
Write-Host ""
Write-Host "To activate:"
Write-Host "  .\activate-dev.ps1"
Write-Host "  # OR"
Write-Host "  & $VenvDir\Scripts\Activate.ps1"
Write-Host ""
Write-Host "To deactivate:"
Write-Host "  deactivate"
Write-Host ""
Write-Host "IDE Configuration:"
Write-Host "  VS Code: Select '$VenvDir\Scripts\python.exe' as interpreter"
Write-Host "  PyCharm: Add '$VenvDir' as project interpreter"
Write-Host ""
