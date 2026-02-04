#!/bin/bash
# Setup script for multi-tenant follower system

set -e

echo "================================"
echo "Setting up development environment"
echo "================================"

# Create log directory
mkdir -p logs

# Create Python virtual environment
echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Create database directories for shards
echo "Creating database directories..."
mkdir -p data/shard_1
mkdir -p data/shard_2

echo ""
echo "================================"
echo "Setup completed successfully!"
echo "================================"
echo ""
echo "Next steps:"
echo "1. Activate venv: source venv/bin/activate"
echo "2. Configure database URLs in .env"
echo "3. Start Redis: redis-server"
echo "4. Run tests: ./run_test.sh"
