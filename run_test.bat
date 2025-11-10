@echo off
python -m pytest -q --maxfail=1 tests > logs\test_run.log 2>&1
