# Multi-tenant follower system refactor

This repository contains a simulated refactor to ensure tenant isolation and cache consistency for a followers system.

Key changes:
* Schema migration guide
	- Add `tenant_id` column to `followers` table (not nullable). If you have existing rows only in a single tenant, backfill tenant_id values using a safe migration.
	- Add `deleted` boolean and `deleted_at` timestamp. Existing rows set deleted=False.
	- Add unique constraint on `(tenant_id, follower_id, followed_id)`.
	- Add `Index` on `(tenant_id, followed_id)` for performance when counting followers.

How to run tests:

1. Install requirements: pip install -r requirements.txt
2. Run pytest: pytest -q

Test logs are written to `logs/test_run.log`.

Note: For real distributed transactions, use two-phase commit across Postgres shards or a robust coordinator.