#!/bin/bash
# Database management script for the vynmatrix platform
# Usage: ./scripts/db/manage_db.sh [command]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKER_DIR="$PROJECT_ROOT/docker"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Load environment variables
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Detect docker compose command (V2 vs V1)
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    DOCKER_COMPOSE=(docker compose)
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE=(docker-compose)
else
    log_error "Docker Compose not found. Please install Docker Desktop."
    exit 1
fi

# Default values
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-vm_trading}"
DB_USER="${DB_USER:-trader}"
DB_PASSWORD="${DB_PASSWORD:-vm_trading_secure_2024}"

# Start PostgreSQL container
start_postgres() {
    log_info "Starting PostgreSQL container..."
    pushd "$DOCKER_DIR" > /dev/null || return 1
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml up -d postgres

    log_info "Waiting for PostgreSQL to be ready..."
    sleep 5

    # Wait for health check
    for i in {1..30}; do
        if "${DOCKER_COMPOSE[@]}" -f docker-compose.yml exec -T postgres pg_isready -U "$DB_USER" -d "$DB_NAME" > /dev/null 2>&1; then
            log_info "PostgreSQL is ready!"
            popd > /dev/null
            return 0
        fi
        echo -n "."
        sleep 1
    done

    log_error "PostgreSQL failed to start within 30 seconds"
    popd > /dev/null
    return 1
}

# Stop PostgreSQL container
stop_postgres() {
    log_info "Stopping PostgreSQL container..."
    pushd "$DOCKER_DIR" > /dev/null || return 1
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml down
    popd > /dev/null
    log_info "PostgreSQL stopped"
}

# Start pgAdmin (optional admin UI)
start_pgadmin() {
    if [ -z "${PGADMIN_EMAIL:-}" ] || [ -z "${PGADMIN_PASSWORD:-}" ] || [ "${PGADMIN_PASSWORD}" = "CHANGE_ME_BEFORE_USE" ]; then
        log_error "Set PGADMIN_EMAIL and a non-placeholder PGADMIN_PASSWORD in $PROJECT_ROOT/.env"
        return 1
    fi
    log_info "Starting pgAdmin..."
    pushd "$DOCKER_DIR" > /dev/null || return 1
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml --profile admin up -d pgadmin
    popd > /dev/null
    log_info "pgAdmin started at http://localhost:${PGADMIN_PORT:-5050}"
    log_info "Login: ${PGADMIN_EMAIL}"
}

# Initialize database schema
init_schema() {
    log_info "Initializing database schema via Alembic..."
    cd "$PROJECT_ROOT"

    DB_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
    export DATABASE_URL="$DB_URL"

    alembic -c scripts/db/alembic.ini upgrade head
    log_info "Schema initialization complete"
}

# Reset database (drop and recreate)
reset_db() {
    log_warn "This will DROP ALL DATA. Are you sure? (y/N)"
    read -r confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "Cancelled"
        return 0
    fi

    log_info "Resetting database..."
    cd "$PROJECT_ROOT"

    if [ -d "build/venvs/lib_application" ]; then
        source "build/venvs/lib_application/bin/activate"
    fi

    DB_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
    export DATABASE_URL="$DB_URL"

    log_info "Dropping database schema..."
    if command -v dropdb > /dev/null 2>&1 && command -v createdb > /dev/null 2>&1; then
        PGPASSWORD="$DB_PASSWORD" dropdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" --if-exists "$DB_NAME"
        PGPASSWORD="$DB_PASSWORD" createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"
    else
        log_warn "dropdb/createdb not found; falling back to Docker or schema reset."
        if "${DOCKER_COMPOSE[@]}" -f "$DOCKER_DIR/docker-compose.yml" ps -q postgres > /dev/null 2>&1; then
            CONTAINER_ID=$("${DOCKER_COMPOSE[@]}" -f "$DOCKER_DIR/docker-compose.yml" ps -q postgres)
        else
            CONTAINER_ID=""
        fi

        if [ -z "$CONTAINER_ID" ] && { [ "$DB_HOST" = "localhost" ] || [ "$DB_HOST" = "127.0.0.1" ]; }; then
            log_warn "Postgres container not running; attempting to start it."
            start_postgres
            CONTAINER_ID=$("${DOCKER_COMPOSE[@]}" -f "$DOCKER_DIR/docker-compose.yml" ps -q postgres 2>/dev/null)
        fi

        if [ -n "$CONTAINER_ID" ]; then
            log_info "Resetting database inside Docker container..."
            "${DOCKER_COMPOSE[@]}" -f "$DOCKER_DIR/docker-compose.yml" exec -T postgres dropdb -U "$DB_USER" --if-exists "$DB_NAME"
            "${DOCKER_COMPOSE[@]}" -f "$DOCKER_DIR/docker-compose.yml" exec -T postgres createdb -U "$DB_USER" "$DB_NAME"
        elif command -v psql > /dev/null 2>&1; then
            log_warn "Database utilities unavailable; resetting schema via psql."
            PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
        else
            log_error "No dropdb/createdb, Docker container, or psql available. Install Postgres client tools or start the Docker container."
            exit 1
        fi
    fi

    alembic -c scripts/db/alembic.ini upgrade head
    # Bootstrap the source-controlled instrument catalogue.
    python scripts/db/bootstrap_scoring.py --db "$DB_URL" --instruments config/instruments.yaml
    log_info "Database reset complete"
}

# Connect to database via psql (using Docker)
connect() {
    log_info "Connecting to PostgreSQL..."
    pushd "$DOCKER_DIR" > /dev/null || return 1
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml exec postgres psql -U "$DB_USER" -d "$DB_NAME"
    popd > /dev/null
}

# Backup database (using Docker)
backup() {
    BACKUP_FILE="${1:-$PROJECT_ROOT/backups/vm_trading_$(date +%Y%m%d_%H%M%S).sql}"
    mkdir -p "$(dirname "$BACKUP_FILE")"

    log_info "Backing up database to $BACKUP_FILE..."
    pushd "$DOCKER_DIR" > /dev/null || return 1
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" > "$BACKUP_FILE"
    popd > /dev/null
    log_info "Backup complete: $BACKUP_FILE"
}

# Restore database (using Docker)
restore() {
    if [ -z "${1:-}" ]; then
        log_error "Usage: $0 restore <backup_file>"
        return 1
    fi

    if [ ! -f "$1" ]; then
        log_error "Backup file not found: $1"
        return 1
    fi

    log_warn "This will OVERWRITE the database. Are you sure? (y/N)"
    read -r confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "Cancelled"
        return 0
    fi

    log_info "Restoring database from $1..."
    pushd "$DOCKER_DIR" > /dev/null || return 1
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < "$1"
    popd > /dev/null
    log_info "Restore complete"
}

# Show database status
status() {
    log_info "Database Status:"
    echo ""

    # Resolve only this Compose project's PostgreSQL service.
    local postgres_container
    postgres_container=$("${DOCKER_COMPOSE[@]}" -f "$DOCKER_DIR/docker-compose.yml" ps -q postgres)
    if [ -n "$postgres_container" ] && [ "$(docker inspect --format '{{.State.Running}}' "$postgres_container")" = "true" ]; then
        echo -e "Container: ${GREEN}Running${NC}"
    else
        echo -e "Container: ${RED}Stopped${NC}"
        return 0
    fi

    # Check connection via the container's own psql — no host psql required.
    pushd "$DOCKER_DIR" > /dev/null || return 1
    if "${DOCKER_COMPOSE[@]}" -f docker-compose.yml exec -T postgres \
        psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" > /dev/null 2>&1; then
        echo -e "Connection: ${GREEN}OK${NC}"
    else
        echo -e "Connection: ${RED}Failed${NC}"
        popd > /dev/null
        return 0
    fi

    # Show table counts
    echo ""
    echo "Table Counts:"
    "${DOCKER_COMPOSE[@]}" -f docker-compose.yml exec -T postgres \
        psql -U "$DB_USER" -d "$DB_NAME" -t -c "
        SELECT schemaname || '.' || relname AS table, n_live_tup AS rows
        FROM pg_stat_user_tables
        WHERE n_live_tup > 0
        ORDER BY n_live_tup DESC
        LIMIT 10;
    "
    popd > /dev/null
}

# Show usage
usage() {
    echo "vynmatrix Database Management"
    echo ""
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  start       Start PostgreSQL container"
    echo "  stop        Stop PostgreSQL container"
    echo "  pgadmin     Start pgAdmin web UI"
    echo "  init        Initialize database schema"
    echo "  reset       Drop and recreate database"
    echo "  connect     Connect to database via psql"
    echo "  backup      Backup database to SQL file"
    echo "  restore     Restore database from SQL file"
    echo "  status      Show database status"
    echo "  help        Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 start                    # Start PostgreSQL"
    echo "  $0 init                     # Initialize schema"
    echo "  $0 backup ./my_backup.sql   # Backup to specific file"
    echo "  $0 restore ./my_backup.sql  # Restore from backup"
}

# Main
case "${1:-help}" in
    start)
        start_postgres
        ;;
    stop)
        stop_postgres
        ;;
    pgadmin)
        start_pgadmin
        ;;
    init)
        init_schema
        ;;
    reset)
        reset_db
        ;;
    connect)
        connect
        ;;
    backup)
        backup "${2:-}"
        ;;
    restore)
        restore "${2:-}"
        ;;
    status)
        status
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        log_error "Unknown command: $1"
        usage
        exit 1
        ;;
esac
