#!/usr/bin/env python3
"""
Windows test runner script.
Detects OS and runs tests, logging output to logs/test_run.log.
"""

import subprocess
import os
import sys
import platform
from pathlib import Path


def run_tests():
    """Run tests and capture output."""
    # Create logs directory
    log_dir = Path('logs')
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / 'test_run.log'
    
    # Print header
    print('=' * 70)
    print('SearchableMixin Test Suite')
    print('=' * 70)
    print(f'OS: {platform.system()}')
    print(f'Python: {sys.version}')
    print(f'Log file: {log_file}')
    print('=' * 70)
    print()
    
    # Open log file
    with open(log_file, 'w') as log:
        log.write('=' * 70 + '\n')
        log.write('SearchableMixin Test Suite\n')
        log.write('=' * 70 + '\n')
        log.write(f'OS: {platform.system()}\n')
        log.write(f'Python: {sys.version}\n')
        log.write('=' * 70 + '\n\n')
        
        # Run tests
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'unittest', 'test_search_events', '-v'],
                capture_output=False,
                text=True,
                timeout=60,
            )
            
            exit_code = result.returncode
            
        except subprocess.TimeoutExpired:
            print('ERROR: Tests timed out after 60 seconds')
            log.write('ERROR: Tests timed out after 60 seconds\n')
            exit_code = 1
        except Exception as e:
            print(f'ERROR: {e}')
            log.write(f'ERROR: {e}\n')
            exit_code = 1
    
    print(f'\nTest results logged to: {log_file}')
    return exit_code


if __name__ == '__main__':
    sys.exit(run_tests())
