#!/usr/bin/env bash
# Regenerate src/morning_star_review/_data/agent-requirements.txt from uv.lock.
# `msreview setup` installs the agent environment from this file with --require-hashes,
# so the agent's dependencies are exactly the locked, hash-checked versions.
set -euo pipefail
cd "$(dirname "$0")/.."
unset UV_INDEX_URL UV_DEFAULT_INDEX UV_EXTRA_INDEX_URL UV_INDEX_STRATEGY PIP_INDEX_URL
export UV_NO_CONFIG=1
uv export --frozen --no-dev --extra agent --no-emit-project --no-header \
  --format requirements.txt --quiet \
  --output-file src/morning_star_review/_data/agent-requirements.txt
