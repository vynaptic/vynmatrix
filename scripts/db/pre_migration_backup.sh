#!/usr/bin/env bash
set -euo pipefail

# Create a timestamped pre-migration backup of the Postgres database via pg_dump.
# Cloud-agnostic: works for an operator-selected PostgreSQL database and only
# needs DATABASE_URL + pg_dump.
#
# Required env:
#   DATABASE_URL          postgresql://user:pass@host:5432/db
# Optional env:
#   BACKUP_DIR            output directory (default: ./backups)
#   BACKUP_DESCRIPTION    label embedded in the filename (default: pre-migration)
#   SPACES_TARGET         if set (e.g. s3://vm-backups/pg), upload the dump there
#                         via s3cmd (DigitalOcean Spaces is S3-compatible)

DATABASE_URL="${DATABASE_URL:?DATABASE_URL is required}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DESCRIPTION="${BACKUP_DESCRIPTION:-pre-migration}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/${DESCRIPTION}-${STAMP}.sql.gz"

mkdir -p "${BACKUP_DIR}"
echo "Creating pre-migration backup ${OUT}"
pg_dump "${DATABASE_URL}" | gzip >"${OUT}"
echo "Backup written: ${OUT} ($(du -h "${OUT}" | cut -f1))"

if [[ -n "${SPACES_TARGET:-}" ]]; then
  echo "Uploading to ${SPACES_TARGET}"
  s3cmd put "${OUT}" "${SPACES_TARGET%/}/${DESCRIPTION}-${STAMP}.sql.gz"
fi

echo "Pre-migration backup complete: ${DESCRIPTION}-${STAMP}"
