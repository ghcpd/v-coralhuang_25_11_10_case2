#!/usr/bin/env bash
# Linux/macOS test runner script
# Auto-detects OS and runs tests, logging output to logs/test_run.log

set -e

echo "======================================================================"
echo "SearchableMixin Test Suite ($(uname -s))"
echo "======================================================================"
echo ""

mkdir -p logs

echo "Running tests..."
python3 -m unittest test_search_events -v 2>&1 | tee logs/test_run.log

if [ ${PIPESTATUS[0]} -eq 0 ]; then
    echo ""
    echo "Tests PASSED"
    exit 0
else
    echo ""
    echo "Tests FAILED"
    exit 1
fi
