@echo off
REM Windows batch script to run tests
REM Auto-detects OS and runs test suite, logging to logs\test_run.log

setlocal enabledelayedexpansion

echo ======================================================================
echo SearchableMixin Test Suite (Windows)
echo ======================================================================
echo.

if not exist logs (
    mkdir logs
)

echo Running tests...
python -m unittest test_search_events -v 2>&1 | tee logs\test_run.log

if !errorlevel! equ 0 (
    echo.
    echo Tests PASSED
    exit /b 0
) else (
    echo.
    echo Tests FAILED
    exit /b 1
)
