import logging
from cache import VersionedCounter, redis_client
from sharding_manager import ShardManager

log = logging.getLogger(__name__)


def report_drift(shard_manager: ShardManager):
    """Check follower counts in DB vs cache and report discrepancies.

    Returns list of dicts with tenant, user_id, db_count, cache_count.
    """
    results = []
    counter = VersionedCounter(redis_client)
    for tenant_id, engine in shard_manager.engines.items():
        # query DB for follower counts per user
        with shard_manager.session_scope(tenant_id) as s:
            # the mapping uses lowercase table names; we can execute SQL
            rows = s.execute('SELECT followed_id, COUNT(*) as cnt FROM followers WHERE tenant_id = :t AND deleted = false GROUP BY followed_id', {'t': tenant_id}).fetchall()
            for r in rows:
                followed_id = r[0]
                db_count = int(r[1])
                cache_key = counter.key(followed_id, tenant_id)
                cache_val = redis_client.get(cache_key)
                cache_count = int(cache_val or 0)
                if cache_count != db_count:
                    results.append({'tenant': tenant_id, 'user_id': followed_id, 'db_count': db_count, 'cache_count': cache_count})
    return results


def cleanup_soft_deleted(shard_manager: ShardManager, older_than_seconds=3600):
    """Remove soft-deleted rows older than a given age across all shards."""
    import datetime
    threshold = datetime.datetime.utcnow() - datetime.timedelta(seconds=older_than_seconds)
    for tenant_id, engine in shard_manager.engines.items():
        with shard_manager.session_scope(tenant_id) as s:
            s.execute("DELETE FROM followers WHERE deleted = true AND deleted_at < :t", {'t': threshold})

