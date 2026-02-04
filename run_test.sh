#!/bin/bash
# Run test suite on Linux/Mac

set -e

echo "================================"
echo "Running Test Suite"
echo "================================"

# Ensure logs directory exists
mkdir -p logs

# Activate virtual environment if it exists
if [ -d venv ]; then
    source venv/bin/activate
fi

# Run tests with coverage
echo "Running tests with pytest..."
python -m pytest test_follow_relationships.py -v --tb=short --log-cli-level=DEBUG 2>&1 | tee logs/test_run.log

# Run manual test script
echo ""
echo "Running manual test suite..."
python test_follow_relationships.py 2>&1 | tee -a logs/test_run.log

echo ""
echo "================================"
echo "Test run completed"
echo "================================"
echo "Results saved to: logs/test_run.log"
