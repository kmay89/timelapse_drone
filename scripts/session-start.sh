#!/usr/bin/env bash
# Claude Code SessionStart hook (.claude/settings.json). Gets a checkout ready to run Vantage:
#   - Python deps:   uv sync --frozen (quiet; a no-op when nothing changed)
#   - ffmpeg:        reported; installed with apt in Claude Code cloud sessions when missing
#   - web tests:     npm ci when node_modules/ is missing
# Prints a short status block that becomes session context. Idempotent; never fails the session.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/..}" || exit 0
status=()

if command -v uv >/dev/null 2>&1; then
  if uv sync --frozen --quiet >/dev/null 2>&1; then status+=("python deps: ok (uv sync --frozen)")
  else status+=("python deps: uv sync --frozen FAILED; run it to see why"); fi
else
  status+=("python deps: uv not found (https://docs.astral.sh/uv/)")
fi

if ! command -v ffmpeg >/dev/null 2>&1 && [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] && [ "$(id -u)" = 0 ]; then
  timeout 240 bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg' >/dev/null 2>&1
fi
if command -v ffmpeg >/dev/null 2>&1; then
  status+=("ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1 | cut -d' ' -f3)")
else
  status+=("ffmpeg: MISSING (apt-get install ffmpeg / brew install ffmpeg); ingest, align, film and video need it")
fi

if [ -f package-lock.json ] && command -v npm >/dev/null 2>&1; then
  if [ ! -d node_modules/@playwright/test ]; then
    timeout 180 npm ci --no-audit --no-fund --silent >/dev/null 2>&1 || status+=("web tests: npm ci FAILED")
  fi
  [ -d node_modules/@playwright/test ] && status+=("web tests: ready (npm run test:web:local)")
fi

status+=("projects: ${VANTAGE_PROJECTS:-./projects (set VANTAGE_PROJECTS for private client projects)}")
printf 'Vantage session check\n'
printf '  %s\n' "${status[@]}"
exit 0
