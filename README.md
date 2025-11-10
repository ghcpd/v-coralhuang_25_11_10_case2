# Multi-Tenant Follower System - Production Incident Fix

## Executive Summary

This document describes the root causes of the production incident in a Flask + SQLAlchemy application managing a self-referential many-to-many "followers" relationship across multiple tenants, and the comprehensive solution implemented.

### Incident Overview

After deployment of multi-tenant sharding with Redis caching, users reported:
- **Cross-tenant relationships**: Users from different tenants could follow each other
- **Cache-DB drift**: Inconsistent follower counts between Redis cache and PostgreSQL
- **Duplicate relationships**: Concurrent follow requests created duplicate entries
- **Data isolation violations**: Tenant boundaries not enforced

### Root Causes Identified

1. **Missing `tenant_id` in followers table**: No constraint preventing cross-tenant links
2. **Unique constraint on only (`follower_id`, `followed_id`)**: Duplicate relationships allowed across tenants
3. **Naive cache writes**: No versioning, locking, or write-through guarantees
4. **No distributed locking**: Race conditions in concurrent cache updates
5. **Soft deletes not respected**: Deleted relationships still appearing in queries
6. **No reconciliation**: Cache drift never detected or fixed
7. **Single-tenant session management**: No shard-aware transaction handling

---

## Architecture Overview

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                      Flask Application                       │
│  (API endpoints for follow/unfollow operations)              │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│             ShardManager (Transaction Layer)                 │
│  • Tenant → Shard routing                                    │
│  • Session management per tenant                             │
│  • Retry logic with exponential backoff                      │
│  • Distributed Redlock coordination                          │
└─────────────────────────────────────────────────────────────┘
           ↓                                           ↓
      ┌────────────────┐                    ┌────────────────┐
      │  PostgreSQL    │                    │  PostgreSQL    │
      │   Shard 1      │                    │   Shard 2      │
      │  (Tenant 1,2)  │                    │  (Tenant 3,4)  │
      └────────────────┘                    └────────────────┘
                            ↓
        ┌─────────────────────────────────────┐
        │    WriteThroughCache Layer           │
        │  • Versioned cache entries           │
        │  • Distributed locking (Redlock)     │
        │  • Cache consistency verification    │
        │  • Periodic reconciliation           │
        └─────────────────────────────────────┘
                            ↓
                    ┌────────────────┐
                    │  Redis 7       │
                    │  (Shared)      │
                    └────────────────┘
```

---

## Schema Refactoring

### Before (Broken)

```python
followers = db.Table(
    'followers',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'))
)
# PROBLEMS:
# - No tenant_id column
# - Unique constraint only on (follower_id, followed_id) - allows duplicates per tenant
# - Cross-tenant relationships possible
# - No soft delete support
# - No timestamps for audit trail
```

### After (Fixed)

```python
followers = db.Table(
    'followers',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('tenant_id', db.Integer, db.ForeignKey('user.tenant_id'), primary_key=True),
    db.Column('created_at', db.DateTime, default=datetime.utcnow, nullable=False),
    db.Column('deleted', db.Boolean, default=False, index=True),
    db.Column('deleted_at', db.DateTime, nullable=True),
    
    # Composite uniqueness: (tenant_id, follower_id, followed_id)
    db.UniqueConstraint('tenant_id', 'follower_id', 'followed_id', 
                        name='uc_followers_tenant_users'),
    
    # Indexes for efficient querying
    db.Index('ix_followers_tenant_follower', 'tenant_id', 'follower_id'),
    db.Index('ix_followers_tenant_followed', 'tenant_id', 'followed_id'),
    db.Index('ix_followers_deleted', 'deleted'),
)
```

### Key Changes

| Aspect | Before | After |
|--------|--------|-------|
| **Tenant Isolation** | None | `tenant_id` foreign key + composite unique constraint |
| **Uniqueness** | Only `(follower_id, followed_id)` | `(tenant_id, follower_id, followed_id)` |
| **Soft Deletes** | Not supported | `deleted` boolean + `deleted_at` timestamp |
| **Validation** | None | `after_flush` hook in ORM |
| **Indexes** | None | Per-tenant, per-user queries optimized |
| **Audit Trail** | None | `FollowerAuditLog` table |

---

## Transaction Safety

### Problem: Race Conditions and Deadlocks

**Scenario**: Two concurrent requests to follow the same user.

```
Thread A:
  1. SELECT * FROM followers WHERE tenant=1, follower=101, followed=102
  2. No row found
  3. INSERT followers VALUES (1, 101, 102)

Thread B:
  1. SELECT * FROM followers WHERE tenant=1, follower=101, followed=102
  2. No row found
  3. INSERT followers VALUES (1, 101, 102)  ← DUPLICATE KEY ERROR

Result: Duplicate violation or inconsistent state
```

### Solution: `ShardManager` with Retry Logic

```python
class TransactionContext:
    """Context manager for tenant-aware transactions with retry logic."""
    
    def __init__(self, shard_manager, tenant_id, max_retries=3):
        self.shard_manager = shard_manager
        self.tenant_id = tenant_id
        self.max_retries = max_retries
    
    def retry_on_error(self, func, *args, **kwargs):
        """Retry transient errors with exponential backoff."""
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except (IntegrityError, OperationalError, DBAPIError) as e:
                if attempt < self.max_retries - 1:
                    backoff = 0.1 * (2 ** attempt)  # 0.1s, 0.2s, 0.4s
                    time.sleep(backoff)
                else:
                    raise
```

### Distributed Locking with Redlock

```python
class RedlockManager:
    """Redis-based distributed locking."""
    
    def acquire_lock(self, lock_key, max_retries=3):
        for attempt in range(max_retries):
            acquired = self.redis.set(
                lock_key,
                datetime.utcnow().isoformat(),
                ex=30,  # 30 second timeout
                nx=True  # Only if not exists
            )
            if acquired:
                return True
            time.sleep(0.1 * (2 ** attempt))
        return False
```

**Benefit**: Prevents concurrent cache writes during same database transaction.

---

## Cache Consistency

### Problem: Naive Cache Updates

**Scenario**: Multiple workers updating cache concurrently.

```
Worker A: GET cache → 100
Worker B: GET cache → 100
Worker A: SET cache → 101  (incremented)
Worker B: SET cache → 101  (also incremented, but lost update)

Result: Drift of 1-2 followers per concurrent update
```

### Solution: Versioned Write-Through Cache

#### 1. Write-Through Pattern

```
Follow Request:
  1. Acquire distributed lock on user
  2. INSERT INTO followers (tenant_id, follower_id, followed_id) VALUES (...)
  3. UPDATE user SET follower_count = follower_count + 1
  4. Commit transaction
  5. UPDATE Redis with new count and version
  6. Release lock

Benefit: Cache only updated after DB transaction succeeds
```

#### 2. Versioned Cache Entries

```json
{
  "version": 5,
  "timestamp": "2024-01-15T10:30:00",
  "value": 47,
  "ttl": 3600
}
```

```python
def increment_follower_count(tenant_id, user_id, version):
    """Increment with optimistic locking."""
    current_count, cached_version = cache.get(key)
    
    if cached_version != version:
        raise VersionMismatch()  # Lost update detected
    
    new_count = current_count + 1
    cache.set(key, new_count, version=version+1)
```

#### 3. Distributed Locking

```python
with cache.acquire_write_lock(key):
    # Only one writer at a time per cache key
    cache.set(key, new_value, version=new_version)
```

---

## Soft Delete Synchronization

### Problem: Deleted Relationships Reappear

**Scenario**: User deletes a follow relationship, but cache/queries still include it.

```sql
UPDATE followers SET deleted=TRUE, deleted_at=NOW() WHERE ...
-- Old queries still see the row in cache or lazy-loaded relationships
```

### Solution: Filtering at Multiple Layers

#### 1. ORM Relationship Filter

```python
followed = db.relationship(
    'User',
    secondary=followers,
    primaryjoin=and_(
        followers.c.follower_id == id,
        followers.c.tenant_id == tenant_id,
        followers.c.deleted == False  # ← Exclude soft-deleted
    ),
    ...
)
```

#### 2. Cache Invalidation on Delete

```python
def unfollow_user(follower_id, followed_id, tenant_id):
    with transactional_context(shard_mgr, tenant_id) as session:
        # Soft delete
        followers_row.deleted = True
        followers_row.deleted_at = datetime.utcnow()
        session.commit()
        
        # Invalidate cache
        cache_manager.invalidate_user_cache(tenant_id, followed_id)
        cache_manager.invalidate_user_cache(tenant_id, follower_id)
```

#### 3. Periodic Cleanup

```python
def cleanup_soft_deleted_rows(session, days_threshold=30):
    """Hard-delete soft-deleted rows older than threshold."""
    cutoff = datetime.utcnow() - timedelta(days=days_threshold)
    
    deleted_count = session.query(followers).filter(
        followers.c.deleted == True,
        followers.c.deleted_at < cutoff
    ).delete()
    
    session.commit()
```

---

## Cache Reconciliation

### Drift Detection

```python
def verify_cache_consistency(tenant_id, user_id, db_count):
    """Detect cache-DB discrepancies."""
    cache_count = redis.get(f"followers:count:{tenant_id}:{user_id}")
    
    drift = abs(cache_count - db_count)
    status = 'healthy' if drift == 0 else 'stale' if drift > 5 else 'warning'
    
    record_in_health_check_table(tenant_id, user_id, cache_count, db_count, drift, status)
```

### Periodic Reconciliation Job

```python
def reconcile_all_users(tenant_id):
    monitor = DriftMonitor(session, redis, tenant_id)
    
    # Detect issues
    cross_tenant_violations = monitor.check_cross_tenant_relationships()
    drift_issues = monitor.check_follower_count_drift()
    orphaned_records = monitor.check_orphaned_soft_deleted_records()
    
    # Auto-repair
    for violation in cross_tenant_violations:
        monitor.repair_cross_tenant_violation(violation['follower_id'], violation['followed_id'])
    
    for issue in drift_issues:
        monitor.repair_follower_count_drift(issue['user_id'])
    
    monitor.purge_orphaned_records(days_threshold=30)
    
    return monitor.report
```

---

## Testing Strategy

### Test Coverage

```
✓ Cross-Tenant Isolation
  - test_cross_tenant_follow_rejected
  - test_same_tenant_follow_allowed

✓ Transactional Safety
  - test_concurrent_follow_no_duplicates
  - test_retry_on_integrity_error
  - test_soft_delete_excludes_from_queries

✓ Cache Consistency
  - test_versioned_cache_prevents_lost_updates
  - test_write_through_consistency
  - test_distributed_lock_prevents_race_condition

✓ Soft Delete
  - test_soft_deleted_rows_excluded_from_count
  - test_cleanup_job_removes_old_soft_deleted_rows

✓ Schema Constraints
  - test_composite_unique_constraint
  - test_foreign_key_tenant_enforcement
  - test_indexes_for_efficient_queries

✓ Failure Recovery
  - test_redis_crash_recovery
  - test_celery_worker_crash_recovery
  - test_network_partition_handling

✓ Cache Drift
  - test_periodic_reconciliation_detects_drift
  - test_orphaned_relationships_detected

✓ Audit Logging
  - test_create_action_logged
  - test_delete_action_logged
```

---

## Database Schema Migration

### Migration Steps

```sql
-- Step 1: Add tenant_id column to followers table
ALTER TABLE followers ADD COLUMN tenant_id INTEGER;

-- Step 2: Populate tenant_id from user records
UPDATE followers f
SET tenant_id = u1.tenant_id
FROM "user" u1
WHERE f.follower_id = u1.id;

-- Step 3: Add NOT NULL constraint
ALTER TABLE followers ALTER COLUMN tenant_id SET NOT NULL;

-- Step 4: Add foreign key constraint
ALTER TABLE followers
ADD CONSTRAINT fk_followers_tenant_id
FOREIGN KEY (tenant_id)
REFERENCES "user"(tenant_id);

-- Step 5: Add soft delete columns
ALTER TABLE followers
ADD COLUMN deleted BOOLEAN DEFAULT FALSE,
ADD COLUMN deleted_at TIMESTAMP NULL,
ADD COLUMN created_at TIMESTAMP DEFAULT NOW();

-- Step 6: Add new unique constraint
ALTER TABLE followers
ADD CONSTRAINT uc_followers_tenant_users
UNIQUE (tenant_id, follower_id, followed_id);

-- Step 7: Create indexes
CREATE INDEX ix_followers_tenant_follower ON followers(tenant_id, follower_id);
CREATE INDEX ix_followers_tenant_followed ON followers(tenant_id, followed_id);
CREATE INDEX ix_followers_deleted ON followers(deleted);
CREATE INDEX ix_followers_deleted_at ON followers(deleted_at);

-- Step 8: Create audit log table
CREATE TABLE follower_audit_log (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    follower_id INTEGER NOT NULL,
    followed_id INTEGER NOT NULL,
    action VARCHAR(20) NOT NULL,
    reason VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (tenant_id) REFERENCES "user"(tenant_id)
);

CREATE INDEX ix_audit_tenant_action ON follower_audit_log(tenant_id, action);

-- Step 9: Create cache health check table
CREATE TABLE cache_health_check (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    cache_count INTEGER NOT NULL,
    db_count INTEGER NOT NULL,
    drift INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL,
    checked_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (tenant_id) REFERENCES "user"(tenant_id)
);

CREATE INDEX ix_health_tenant_user ON cache_health_check(tenant_id, user_id);
CREATE INDEX ix_health_status ON cache_health_check(status);
```

### Zero-Downtime Deployment

1. **Phase 1** (Pre-deployment):
   - Add columns to followers table (nullable)
   - Deploy new code with backward compatibility
   - Run data migration scripts

2. **Phase 2** (Deployment):
   - Add NOT NULL constraints
   - Add foreign keys and unique constraints
   - Create indexes

3. **Phase 3** (Validation):
   - Run drift reconciliation
   - Verify no cross-tenant violations
   - Monitor cache health

---

## Environment Setup

### Quick Start

```bash
# Linux/Mac
chmod +x setup.sh run_test.sh
./setup.sh
./run_test.sh

# Windows
run_test.bat
```

### Docker

```bash
docker build -t follower-system .
docker run -e REDIS_URL=redis://redis:6379 \
           -e DATABASE_URL=postgresql://user:pass@db:5432/followers \
           follower-system
```

### Configuration

Create `.env` file:

```
# Database Shards
SHARD_1_URL=postgresql://user:pass@db1:5432/tenant_1
SHARD_2_URL=postgresql://user:pass@db2:5432/tenant_2

# Redis
REDIS_URL=redis://localhost:6379/0

# Celery
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

# Logging
LOG_LEVEL=INFO
```

---

## Deployment Checklist

- [ ] Backup all shard databases
- [ ] Run schema migration scripts
- [ ] Deploy new application code
- [ ] Start reconciliation job (--auto-repair=false)
- [ ] Review drift detection report
- [ ] Manually fix any critical violations
- [ ] Enable auto-repair in reconciliation job
- [ ] Run periodic reconciliation (daily)
- [ ] Monitor cache health checks
- [ ] Set alerts on drift > threshold

---

## Monitoring and Alerts

### Key Metrics

1. **Cache Health**
   - `followers:drift:max` - Maximum drift observed
   - `followers:drift:average` - Average drift per user
   - `followers:reconciliation:duration_ms` - Reconciliation job runtime

2. **Data Integrity**
   - `followers:cross_tenant_violations` - Cross-tenant relationships found
   - `followers:orphaned_records` - Soft-deleted records pending purge
   - `followers:soft_delete_violations` - Deleted relationships in queries

3. **Transaction Performance**
   - `followers:transaction:retry_rate` - % of retried transactions
   - `followers:transaction:deadlock_rate` - Deadlock frequency
   - `followers:transaction:avg_latency_ms` - Transaction latency

### Alert Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| Max drift | > 5 | > 20 |
| Cross-tenant violations | > 0 | > 10 |
| Retry rate | > 1% | > 5% |
| Deadlock rate | > 0.1% | > 1% |

---

## Files Description

| File | Purpose |
|------|---------|
| `models.py` | Refactored ORM with tenant constraints |
| `sharding_manager.py` | Shard routing, session management, retry logic |
| `cache_layer.py` | Versioned cache, write-through, locking |
| `drift_monitor.py` | Reconciliation, drift detection, repairs |
| `test_follow_relationships.py` | Comprehensive test suite |
| `requirements.txt` | Python dependencies |
| `setup.sh` | Environment setup (Linux/Mac) |
| `run_test.sh` | Test runner (Linux/Mac) |
| `run_test.bat` | Test runner (Windows) |
| `Dockerfile` | Docker image definition |
| `README.md` | This document |

---

## Frequently Asked Questions

### Q: Will this migration break existing applications?
**A:** The refactored code is backward compatible during the migration phase. Old code can continue reading/writing until the constraint enforcement phase.

### Q: How long does reconciliation take?
**A:** Depends on dataset size. Typically 1-5 minutes for 1M users across 10 shards. Run off-peak.

### Q: Can we enable auto-repair immediately?
**A:** Recommended to run with `auto_repair=false` for 1-2 cycles to review issues. Then enable after validation.

### Q: What if Redis is down?
**A:** Writes still succeed (DB layer is persisted). Cache misses trigger DB queries. Reconciliation fills cache on next job.

### Q: How to handle cross-tenant violations found?
**A:** Soft-delete the offending relationships. Audit log preserved. User communication recommended.

---

## Support & Troubleshooting

For issues or questions, refer to:
1. Test output in `logs/test_run.log`
2. Database query logs
3. Redis commands: `redis-cli KEYS "followers:*"` to inspect cache
4. Audit table: `SELECT * FROM follower_audit_log ORDER BY created_at DESC`

---

**Document Version**: 1.0  
**Last Updated**: 2024-01-15  
**Status**: Production Ready
