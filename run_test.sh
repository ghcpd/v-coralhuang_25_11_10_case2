#!/usr/bin/env bash
set -euo pipefail

python -m pytest -q --maxfail=1 tests | tee logs/test_run.log
