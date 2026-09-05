# Database management script for the vynmatrix platform
# PowerShell equivalent of scripts/db/manage_db.sh
# Usage: .\scripts\db\manage_db.ps1 [command] [args]

Param(
    [Parameter(Position=0)]
    [ValidateSet('start', 'stop', 'pgadmin', 'init', 'reset', 'connect', 'backup', 'restore', 'status', 'help')]
    [string]$Command = 'help',

    [Parameter(Position=1)]
    [string]$BackupFile = ""
)

$ErrorActionPreference = "Stop"

# Get script paths
$ScriptDir = $PSScriptRoot
$ProjectRoot = Split-Path (Split-Path $ScriptDir -Parent) -Parent
$DockerDir = Join-Path $ProjectRoot "docker"

# Helper functions
function Write-Info($msg)  { Write-Host "[INFO] $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Error2($msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red }

# Load environment variables from .env file
function Parse-DotEnvLine {
    param([string]$Line)

    $clean = $Line.Trim()
    if (-not $clean -or $clean.StartsWith("#") -or ($clean -notmatch "=")) {
        return $null
    }

    $parts = $clean.Split("=", 2)
    $key = $parts[0].Trim()
    $value = $parts[1].Trim()
    if (-not $key) {
        return $null
    }

    if (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
        if ($value.Length -ge 2) {
            $value = $value.Substring(1, $value.Length - 2)
        } else {
            $value = ""
        }
    } else {
        # Strip inline comments for unquoted values (matches .env behavior on *nix)
        $value = $value -replace '\s+#.*$', ''
        $value = $value.Trim()
    }

    return @{ Key = $key; Value = $value }
}

if (Test-Path "$ProjectRoot\.env") {
    Get-Content "$ProjectRoot\.env" | ForEach-Object {
        $parsed = Parse-DotEnvLine $_
        if ($parsed) {
            Set-Item -Path "env:$($parsed.Key)" -Value $parsed.Value -ErrorAction SilentlyContinue
        }
    }
}

# Detect docker compose command (V2 vs V1)
$DockerCompose = $null
try {
    docker compose version 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $DockerCompose = "docker compose"
    }
} catch {}

if (!$DockerCompose) {
    try {
        docker-compose --version 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $DockerCompose = "docker-compose"
        }
    } catch {}
}

if (!$DockerCompose) {
    Write-Error2 "Docker Compose not found. Please install Docker Desktop."
    exit 1
}

# Default values with environment variable fallback
$DbHost = if ($env:DB_HOST) { $env:DB_HOST } else { "localhost" }
$DbPort = if ($env:DB_PORT) { $env:DB_PORT } else { "5432" }
$DbName = if ($env:DB_NAME) { $env:DB_NAME } else { "vm_trading" }
$DbUser = if ($env:DB_USER) { $env:DB_USER } else { "trader" }
$DbPassword = if ($env:DB_PASSWORD) { $env:DB_PASSWORD } else { "vm_trading_secure_2024" }

# Command functions

function Start-Postgres {
    Write-Info "Starting PostgreSQL container..."
    Push-Location $DockerDir
    try {
        if ($DockerCompose -eq "docker compose") {
            docker compose -f docker-compose.yml up -d postgres
        } else {
            docker-compose -f docker-compose.yml up -d postgres
        }

        Write-Info "Waiting for PostgreSQL to be ready..."
        Start-Sleep -Seconds 5

        # Wait for health check (30 attempts)
        for ($i = 1; $i -le 30; $i++) {
            try {
                if ($DockerCompose -eq "docker compose") {
                    $result = docker compose -f docker-compose.yml exec -T postgres pg_isready -U $DbUser -d $DbName 2>&1
                } else {
                    $result = docker-compose -f docker-compose.yml exec -T postgres pg_isready -U $DbUser -d $DbName 2>&1
                }

                if ($LASTEXITCODE -eq 0) {
                    Write-Info "PostgreSQL is ready!"
                    return
                }
            } catch {}

            Write-Host "." -NoNewline
            Start-Sleep -Seconds 1
        }

        Write-Error2 "PostgreSQL failed to start within 30 seconds"
        exit 1
    } finally {
        Pop-Location
    }
}

function Stop-Postgres {
    Write-Info "Stopping PostgreSQL container..."
    Push-Location $DockerDir
    try {
        if ($DockerCompose -eq "docker compose") {
            docker compose -f docker-compose.yml down
        } else {
            docker-compose -f docker-compose.yml down
        }
        Write-Info "PostgreSQL stopped"
    } finally {
        Pop-Location
    }
}

function Start-PgAdmin {
    if (
        -not $env:PGADMIN_EMAIL -or
        -not $env:PGADMIN_PASSWORD -or
        $env:PGADMIN_PASSWORD -eq "CHANGE_ME_BEFORE_USE"
    ) {
        throw "Set PGADMIN_EMAIL and a non-placeholder PGADMIN_PASSWORD in $ProjectRoot\.env"
    }
    Write-Info "Starting pgAdmin..."
    Push-Location $DockerDir
    try {
        $pgAdminPort = if ($env:PGADMIN_PORT) { $env:PGADMIN_PORT } else { "5050" }

        if ($DockerCompose -eq "docker compose") {
            docker compose -f docker-compose.yml --profile admin up -d pgadmin
        } else {
            docker-compose -f docker-compose.yml --profile admin up -d pgadmin
        }

        Write-Info "pgAdmin started at http://localhost:$pgAdminPort"
        Write-Info "Login: $env:PGADMIN_EMAIL"
    } finally {
        Pop-Location
    }
}

function Initialize-Schema {
    Write-Info "Initializing database schema via Alembic..."
    Push-Location $ProjectRoot
    try {
        $DbUrl = "postgresql://${DbUser}:${DbPassword}@${DbHost}:${DbPort}/${DbName}"
        $env:DATABASE_URL = $DbUrl

        alembic -c scripts\db\alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) {
            Write-Error2 "Schema initialization failed"
            exit 1
        }
        Write-Info "Schema initialization complete"
    } finally {
        Pop-Location
    }
}

function Reset-Database {
    Write-Warn "This will DROP ALL DATA. Are you sure? (y/N)"
    $confirm = Read-Host
    if ($confirm -ne "y" -and $confirm -ne "Y") {
        Write-Info "Cancelled"
        return
    }

    Write-Info "Resetting database..."
    Push-Location $ProjectRoot
    try {
        # Activate venv if available
        if (Test-Path "build\venvs\lib_application\Scripts\Activate.ps1") {
            & "build\venvs\lib_application\Scripts\Activate.ps1"
        }

        $DbUrl = "postgresql://${DbUser}:${DbPassword}@${DbHost}:${DbPort}/${DbName}"
        $env:DATABASE_URL = $DbUrl
        $env:PGPASSWORD = $DbPassword

        Write-Info "Dropping database schema..."

        # Drop and recreate database using Docker
        Push-Location $DockerDir
        try {
            if ($DockerCompose -eq "docker compose") {
                docker compose -f docker-compose.yml exec -T postgres dropdb -h $DbHost -p $DbPort -U $DbUser --if-exists $DbName 2>&1 | Out-Null
                docker compose -f docker-compose.yml exec -T postgres createdb -h $DbHost -p $DbPort -U $DbUser $DbName
            } else {
                docker-compose -f docker-compose.yml exec -T postgres dropdb -h $DbHost -p $DbPort -U $DbUser --if-exists $DbName 2>&1 | Out-Null
                docker-compose -f docker-compose.yml exec -T postgres createdb -h $DbHost -p $DbPort -U $DbUser $DbName
            }
        } finally {
            Pop-Location
        }

        alembic -c scripts\db\alembic.ini upgrade head

        # Bootstrap the source-controlled instrument catalogue.
        if (Test-Path "scripts\db\bootstrap_scoring.py") {
            python scripts\db\bootstrap_scoring.py --db $DbUrl --instruments config\instruments.yaml
            if ($LASTEXITCODE -ne 0) {
                Write-Error2 "Instrument catalogue bootstrap failed"
                exit 1
            }
        }

        Write-Info "Database reset complete"
    } finally {
        Pop-Location
    }
}

function Connect-Database {
    Write-Info "Connecting to PostgreSQL..."
    Push-Location $DockerDir
    try {
        if ($DockerCompose -eq "docker compose") {
            docker compose -f docker-compose.yml exec postgres psql -U $DbUser -d $DbName
        } else {
            docker-compose -f docker-compose.yml exec postgres psql -U $DbUser -d $DbName
        }
    } finally {
        Pop-Location
    }
}

function Backup-Database {
    param([string]$File)

    if (!$File) {
        $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $File = Join-Path $ProjectRoot "backups\vm_trading_$timestamp.sql"
    }

    $backupDir = Split-Path $File -Parent
    if (!(Test-Path $backupDir)) {
        New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    }

    Write-Info "Backing up database to $File..."
    Push-Location $DockerDir
    try {
        if ($DockerCompose -eq "docker compose") {
            docker compose -f docker-compose.yml exec -T postgres pg_dump -U $DbUser -d $DbName | Out-File -FilePath $File -Encoding UTF8
        } else {
            docker-compose -f docker-compose.yml exec -T postgres pg_dump -U $DbUser -d $DbName | Out-File -FilePath $File -Encoding UTF8
        }
        Write-Info "Backup complete: $File"
    } finally {
        Pop-Location
    }
}

function Restore-Database {
    param([string]$File)

    if (!$File) {
        Write-Error2 "Usage: .\scripts\db\manage_db.ps1 restore <backup_file>"
        exit 1
    }

    if (!(Test-Path $File)) {
        Write-Error2 "Backup file not found: $File"
        exit 1
    }

    Write-Warn "This will OVERWRITE the database. Are you sure? (y/N)"
    $confirm = Read-Host
    if ($confirm -ne "y" -and $confirm -ne "Y") {
        Write-Info "Cancelled"
        return
    }

    Write-Info "Restoring database from $File..."
    Push-Location $DockerDir
    try {
        if ($DockerCompose -eq "docker compose") {
            Get-Content $File | docker compose -f docker-compose.yml exec -T postgres psql -U $DbUser -d $DbName
        } else {
            Get-Content $File | docker-compose -f docker-compose.yml exec -T postgres psql -U $DbUser -d $DbName
        }
        Write-Info "Restore complete"
    } finally {
        Pop-Location
    }
}

function Get-DatabaseStatus {
    Write-Info "Database Status:"
    Write-Host ""

    # Resolve only this Compose project's PostgreSQL service.
    $composeFile = Join-Path $DockerDir "docker-compose.yml"
    if ($DockerCompose -eq "docker compose") {
        $containerId = docker compose -f $composeFile ps -q postgres
    } else {
        $containerId = docker-compose -f $composeFile ps -q postgres
    }
    $containerRunning = $containerId -and ((docker inspect --format '{{.State.Running}}' $containerId) -eq "true")
    if ($containerRunning) {
        Write-Host "Container: " -NoNewline
        Write-Host "Running" -ForegroundColor Green
    } else {
        Write-Host "Container: " -NoNewline
        Write-Host "Stopped" -ForegroundColor Red
        return
    }

    # Check connection
    $env:PGPASSWORD = $DbPassword
    Push-Location $DockerDir
    try {
        $testResult = $null
        if ($DockerCompose -eq "docker compose") {
            $testResult = docker compose -f docker-compose.yml exec -T postgres psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -c "SELECT 1" 2>&1
        } else {
            $testResult = docker-compose -f docker-compose.yml exec -T postgres psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -c "SELECT 1" 2>&1
        }

        if ($LASTEXITCODE -eq 0) {
            Write-Host "Connection: " -NoNewline
            Write-Host "OK" -ForegroundColor Green
        } else {
            Write-Host "Connection: " -NoNewline
            Write-Host "Failed" -ForegroundColor Red
            return
        }

        # Show table counts
        Write-Host ""
        Write-Host "Table Counts:"
        $query = @"
SELECT schemaname || '.' || relname AS table, n_live_tup AS rows
FROM pg_stat_user_tables
WHERE n_live_tup > 0
ORDER BY n_live_tup DESC
LIMIT 10;
"@

        if ($DockerCompose -eq "docker compose") {
            docker compose -f docker-compose.yml exec -T postgres psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -t -c $query
        } else {
            docker-compose -f docker-compose.yml exec -T postgres psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -t -c $query
        }
    } finally {
        Pop-Location
    }
}

function Show-Usage {
    Write-Host "vynmatrix Database Management"
    Write-Host ""
    Write-Host "Usage: .\scripts\db\manage_db.ps1 [command]"
    Write-Host ""
    Write-Host "Commands:"
    Write-Host "  start       Start PostgreSQL container"
    Write-Host "  stop        Stop PostgreSQL container"
    Write-Host "  pgadmin     Start pgAdmin web UI"
    Write-Host "  init        Initialize database schema"
    Write-Host "  reset       Drop and recreate database"
    Write-Host "  connect     Connect to database via psql"
    Write-Host "  backup      Backup database to SQL file"
    Write-Host "  restore     Restore database from SQL file"
    Write-Host "  status      Show database status"
    Write-Host "  help        Show this help message"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  .\scripts\db\manage_db.ps1 start                    # Start PostgreSQL"
    Write-Host "  .\scripts\db\manage_db.ps1 init                     # Initialize schema"
    Write-Host "  .\scripts\db\manage_db.ps1 backup .\my_backup.sql   # Backup to specific file"
    Write-Host "  .\scripts\db\manage_db.ps1 restore .\my_backup.sql  # Restore from backup"
}

# Main command routing
switch ($Command) {
    'start'   { Start-Postgres }
    'stop'    { Stop-Postgres }
    'pgadmin' { Start-PgAdmin }
    'init'    { Initialize-Schema }
    'reset'   { Reset-Database }
    'connect' { Connect-Database }
    'backup'  { Backup-Database -File $BackupFile }
    'restore' { Restore-Database -File $BackupFile }
    'status'  { Get-DatabaseStatus }
    'help'    { Show-Usage }
    default   {
        Write-Error2 "Unknown command: $Command"
        Show-Usage
        exit 1
    }
}
