#!/usr/bin/env bash
# Start the sample target (port 9000) and the tool (port 8000) for local development.
# Requires JMeter on PATH, or set JMETER_BIN=/full/path/to/bin/jmeter
set -euo pipefail
cd "$(dirname "$0")/../backend"

export ALLOW_PRIVATE_TARGETS="${ALLOW_PRIVATE_TARGETS:-true}"   # local testing only
export PERF_DATA_DIR="${PERF_DATA_DIR:-../data}"

python -m uvicorn tests.sample_app:app --port 9000 --log-level warning &
SAMPLE_PID=$!
trap 'kill $SAMPLE_PID 2>/dev/null || true' EXIT

echo "Sample target: http://127.0.0.1:9000   Dashboard: http://127.0.0.1:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
