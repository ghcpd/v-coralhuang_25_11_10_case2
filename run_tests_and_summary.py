import json
import subprocess
import pytest
import time
import os

LOG_FILE = os.path.join('logs', 'test_run.log')
OUTPUT_FILE = 'output.json'

REPORT = {
    'root_causes': [
        'followers table missing tenant_id leading to cross-tenant relationships',
        'No uniqueness constraint per tenant',
        'Cache inconsistencies due to race conditions and no versioning/locks',
        'Global commits without shard-aware transactions causing data drift'
    ],
    'refactor_changes': [
        'Added tenant_id column to followers',
        'Created Follower model with composite unique constraint',
        'Added after_flush validation on Follower creation',
        'Implemented shard-aware session manager with two-phase commit',
        'Implemented versioned write-through cache with redlock locking',
        'Implemented drift repair utilities and soft-delete cleanup'
    ],
    'test_outcomes': [],
    'execution_status': 'unknown',
}

# Run tests
try:
    # run pytest in-process so module paths resolve correctly
    import io, sys
    capture = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = capture
        rc = pytest.main(['-q', '--maxfail=1', 'tests'])
    finally:
        sys.stdout = old_stdout
    # Copy the pytest terminal output to the log file isn't straightforward, fetch the file created by pytest
    # For continuity we will read logs/test_run.log written separately by run_test.sh if present
    try:
        with open(LOG_FILE, 'r') as f:
            log_text = f.read()
    except Exception:
        log_text = ''
    if rc == 0:
        REPORT['execution_status'] = 'success'
    else:
        REPORT['execution_status'] = 'failure'
    # persist the capture into a log file
    try:
        log_text = capture.getvalue()
        with open(LOG_FILE, 'w') as f:
            f.write(log_text)
    except Exception:
        pass
    REPORT['test_outcomes'].append({'log_excerpt': log_text[:4000], 'return_code': rc})
except Exception as e:
    REPORT['execution_status'] = 'failure'
    REPORT['test_outcomes'].append({'error': str(e)})

with open(OUTPUT_FILE, 'w') as f:
    json.dump(REPORT, f, indent=2)

print('Wrote', OUTPUT_FILE)
