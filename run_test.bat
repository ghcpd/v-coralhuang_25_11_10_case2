@echo off
REM Run test suite on Windows

echo ================================
echo Running Test Suite
echo ================================

REM Create logs directory if it doesn't exist
if not exist logs mkdir logs

REM Activate virtual environment if it exists
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

REM Run tests with coverage
echo Running tests with pytest...
python -m pytest test_follow_relationships.py -v --tb=short --log-cli-level=DEBUG > logs\test_run.log 2>&1

REM Run manual test script
echo.
echo Running manual test suite...
python test_follow_relationships.py >> logs\test_run.log 2>&1

echo.
echo ================================
echo Test run completed
echo ================================
echo Results saved to: logs\test_run.log

type logs\test_run.log
