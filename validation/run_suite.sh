#!/usr/bin/env bash
# Linux / macOS runner for the h2s bot master validation suite.
# Usage:
#   ./validation/run_suite.sh                    (runs the full automated suite)
#   ./validation/run_suite.sh v1.2.3             (archives the report under that tag)
set -euo pipefail

TAG="${1:-dev-$(date +%Y%m%d-%H%M%S)}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-test-harness-token}"
export LOG_LEVEL="DEBUG"
export ALERT_PACING_SECONDS="0"

REPORT_DIR="$PROJECT_ROOT/validation/reports"
mkdir -p "$REPORT_DIR"
REPORT_FILE="$REPORT_DIR/pytest_report_${TAG}.txt"

echo "Running automated validation suite for release tag: $TAG"
echo "Report: $REPORT_FILE"
echo "---"

if pytest tests/ -v --tb=short 2>&1 | tee "$REPORT_FILE"; then
    echo "---"
    echo "All automated tests PASSED. Proceed with the manual sections of RELEASE_GATE.md."
    exit 0
else
    echo "---"
    echo "Automated suite FAILED. Release is held. See $REPORT_FILE."
    exit 1
fi
