#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker run --rm -e ISPC_REST_TEST_CONTAINER=1 -v "$PWD:/repo:ro" -w /repo python:3.12-slim-bookworm \
    sh -ec 'pip install --quiet --disable-pip-version-check --root-user-action=ignore "ansible-core>=2.17,<2.18"; python3 tests/rest-runtime-owner.py; python3 tests/rest-deployment.py'
