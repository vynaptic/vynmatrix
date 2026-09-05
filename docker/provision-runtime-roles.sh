#!/bin/sh
set -eu

# Credentials stay in the environment. The shared CLI validates every installed
# role and authenticates supplied passwords before any routine provisioning.
# Only an explicit --rotate changes existing passwords.
exec "${PYTHON:-python3}" -m dev_cli.main db roles "$@"
