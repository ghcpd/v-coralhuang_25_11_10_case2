#!/usr/bin/env bash
# Setup script: install dependencies and prepare environment

set -e

echo "Setting up SearchableMixin test environment..."
echo ""

# Detect OS
OS_TYPE=$(uname -s)
echo "Detected OS: $OS_TYPE"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Create logs directory
mkdir -p logs

echo ""
echo "Setup complete! To run tests, use:"
echo "  ./run_test.sh  (Linux/macOS)"
echo "  run_test.bat   (Windows)"
echo ""
echo "Or activate venv and run: python -m unittest test_search_events -v"
