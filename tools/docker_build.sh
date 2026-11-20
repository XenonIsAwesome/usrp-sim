#!/usr/bin/env bash
# Build the usrp-sim container image.   tools/docker_build.sh [--tag NAME[:TAG]] [extra docker build args]
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TAG=usrp-sim:latest
if [ "${1:-}" = "--tag" ]; then TAG="$2"; shift 2; fi
DOCKER=${DOCKER:-docker}     # DOCKER=podman works too
exec "$DOCKER" build -f "$HERE/tools/docker/Dockerfile" -t "$TAG" "$@" "$HERE"
