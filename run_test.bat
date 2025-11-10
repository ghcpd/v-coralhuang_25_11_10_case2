@echo off
python -m pytest -q 2>&1 | tee logs\test_run.log
