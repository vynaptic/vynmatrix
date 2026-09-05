#!/bin/bash
# Build the production indicator strategy wheel, shared libraries, managed
# environments, and the single indicator-runner service image. Per-strategy
# Docker images are not supported.

set -euo pipefail

TAG="latest"

usage() {
    echo "Usage: $0 [--tag TAG]"
    echo "Builds library wheels, application venvs, and config-declared service images."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            TAG="${2:?--tag requires a value}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Per-strategy/category image builds were retired; unsupported argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

vmdev build libs
vmdev build strategies
vmdev build venvs
vmdev build docker --from-config --tag "$TAG"

docker image inspect "vynmatrix/indicator-runner:$TAG" >/dev/null
echo "Built and verified vynmatrix/indicator-runner:$TAG"
