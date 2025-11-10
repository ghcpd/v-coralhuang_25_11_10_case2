# Multi-Tenant Followers System - Refactor

This repository contains a simplified Flask+SQLAlchemy example that demonstrates: schema hardening, shard-aware transactions, write-through caching with versioning and distributed lock, and drift monitoring.

Key changes:
- `models.py` defines the `Follower` association as a real model with `tenant_id`, `deleted` soft-delete and a UniqueConstraint on (tenant_id, follower_id, followed_id)
- `sharding_manager.py` implements shard-aware sessions and supports two-phase commit across multiple shards
- `cache.py` implements a write-through cache with versioning and distributed locking via redlock
- `drift_monitor.py` reconciles counts and detects cross-tenant violations
- `tests/test_follow_relationships.py` includes concurrency and reconciliation tests

Schema migration guide:
- Add `tenant_id`, `deleted` and `deleted_at` columns to the `followers` table
- Add `uq_tenant_follower_followed` unique constraint
- Migrate existing rows: for each row, set `tenant_id` based on follower user tenant; validate that follower and followed are same tenant

Testing setup:
- Use `pytest` to run tests
- The repository uses `fakeredis` for Redis testing and `sqlite` files for shard simulation

Execution:
- Run `run_test.sh` on Unix/macOS
- Run `run_test.bat` on Windows
- Logs are written to `logs/test_run.log`

Notes:
- This is a simplified simulation and doesn't implement full cross-shard distributed transaction readiness beyond SQLAlchemy 2PC. For real deployments, a proper transaction coordinator or external system should be used.
