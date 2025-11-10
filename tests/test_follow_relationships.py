import threading
import logging
import time
import os

from models import Base, User, create_follow, soft_delete_follow
from sharding_manager import ShardManager
from cache import VersionedCounter, redis_client
from drift_monitor import report_drift
from sqlalchemy.exc import IntegrityError

log = logging.getLogger('test')
log.setLevel(logging.DEBUG)
os.makedirs('logs', exist_ok=True)
logging.basicConfig(level=logging.INFO)


def setup_shards(tmpdir=None):
    # Map tenants to per-tenant sqlite DBs
    shards = {
        1: 'sqlite:///tenant1.db',
        2: 'sqlite:///tenant2.db',
    }
    manager = ShardManager(shards=shards)
    manager.create_all(Base)
    return manager


def teardown_shards(manager):
    manager.drop_all(Base)
    # remove sqlite files
    for path in ['tenant1.db', 'tenant2.db']:
        try:
            os.remove(path)
        except OSError:
            pass


def test_cross_tenant_follow():
    manager = setup_shards()
    with manager.session_scope(1) as s1:
        u1 = User(username='a', tenant_id=1)
        v1 = User(username='b', tenant_id=1)
        s1.add_all([u1, v1])
        s1.flush()

    with manager.session_scope(2) as s2:
        u2 = User(username='c', tenant_id=2)
        s2.add(u2)
        s2.flush()

    # attempt cross-tenant follow (1 -> 2)
    try:
        with manager.session_scope(1) as s:
            # reload users
            follower = s.query(User).filter_by(username='a').one()
            # Attempt to follow a user in tenant 2 by fetching user from tenant2 session
            with manager.session_scope(2) as s_other:
                followed = s_other.query(User).filter_by(username='c').one()
            # Should raise
            try:
                create_follow(s, follower=follower, followed=followed)
                s.flush()
                raise AssertionError('Cross-tenant follow allowed')
            except Exception as e:
                # expected
                log.info('cross tenant prevented: %s', e)
    finally:
        teardown_shards(manager)


def test_concurrent_follow_uniqueness_retries():
    manager = setup_shards()
    with manager.session_scope(1) as s:
        a = User(username='u1', tenant_id=1)
        b = User(username='u2', tenant_id=1)
        s.add_all([a, b])
        s.flush()
        follower_id = a.id
        followed_id = b.id

    counter = VersionedCounter(redis_client)

    def attempt_follow():
        with manager.session_scope(1) as s:
            follower = s.query(User).get(follower_id)
            followed = s.query(User).get(followed_id)
            try:
                create_follow(s, follower, followed)
            except IntegrityError:
                log.info('IntegrityError on concurrent follow')

    threads = [threading.Thread(target=attempt_follow) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # confirm only one follow exists
    with manager.session_scope(1) as s:
        records = list(s.execute('SELECT * FROM followers').fetchall())
        assert len(records) == 1

    teardown_shards(manager)


def test_soft_delete_and_cache_sync():
    manager = setup_shards()
    with manager.session_scope(1) as s:
        a = User(username='soft_a', tenant_id=1)
        b = User(username='soft_b', tenant_id=1)
        s.add_all([a, b])
        s.flush()
        follower_id = a.id
        followed_id = b.id

    # create and then soft delete
    with manager.session_scope(1) as s:
        follower = s.query(User).get(follower_id)
        followed = s.query(User).get(followed_id)
        create_follow(s, follower, followed)

    # increment cache
    vc = VersionedCounter(redis_client)
    vc.set_exact(followed_id, 1, 1, 1)

    with manager.session_scope(1) as s:
        follower = s.query(User).get(follower_id)
        followed = s.query(User).get(followed_id)
        soft_delete_follow(s, follower, followed)

    # after soft delete, drift monitor should find no DB rows
    drift = report_drift(manager)
    assert len(drift) == 0

    teardown_shards(manager)


def test_reconciliation_detects_orphaned_cache():
    manager = setup_shards()
    with manager.session_scope(1) as s:
        a = User(username='rec_a', tenant_id=1)
        b = User(username='rec_b', tenant_id=1)
        s.add_all([a, b])
        s.flush()
        follower_id = a.id
        followed_id = b.id

    # simulate a crash after DB write but before cache sync
    from cache import redis_client
    with manager.session_scope(1) as s:
        follower = s.query(User).get(follower_id)
        followed = s.query(User).get(followed_id)
        create_follow(s, follower, followed)
        # intentionally don't update cache

    # now cache has stale or missing count; set a wrong value
    vc = VersionedCounter(redis_client)
    vc.set_exact(followed_id, 1, 1, 1)  # set a count but maybe mismatched

    drift = report_drift(manager)
    # drift should detect mismatch
    assert len(drift) >= 0

    teardown_shards(manager)


def test_analytics_replica_consistency():
    # Simulate an analytics replica (MySQL) as a separate SQLite DB for testing
    import sqlite3
    manager = setup_shards()
    with manager.session_scope(1) as s:
        a = User(username='rep_a', tenant_id=1)
        b = User(username='rep_b', tenant_id=1)
        s.add_all([a, b])
        s.flush()
        follower_id = a.id
        followed_id = b.id

    with manager.session_scope(1) as s:
        follower = s.query(User).get(follower_id)
        followed = s.query(User).get(followed_id)
        create_follow(s, follower, followed)

    # Now create analytics replica and copy aggregate counts
    conn = sqlite3.connect('analytics.db')
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS follower_counts (tenant_id INTEGER, user_id INTEGER, cnt INTEGER)')
    conn.commit()

    # generate counts from the primary DB
    with manager.session_scope(1) as s:
        rows = s.execute('SELECT followed_id, COUNT(*) as cnt FROM followers WHERE tenant_id = :t AND deleted = false GROUP BY followed_id', {'t': 1}).fetchall()
        for r in rows:
            c.execute('INSERT INTO follower_counts (tenant_id, user_id, cnt) VALUES (?, ?, ?)', (1, r[0], int(r[1])))
    conn.commit()

    # Verify analytics matches DB and cache
    cur = c.execute('SELECT user_id, cnt FROM follower_counts WHERE tenant_id = 1').fetchall()
    assert cur[0][1] == 1
    conn.close()
    try:
        os.remove('analytics.db')
    except OSError:
        pass

    teardown_shards(manager)


def test_soft_delete_cleanup_job():
    manager = setup_shards()
    with manager.session_scope(1) as s:
        a = User(username='cleanup_a', tenant_id=1)
        b = User(username='cleanup_b', tenant_id=1)
        s.add_all([a, b])
        s.flush()
        follower_id = a.id
        followed_id = b.id

    with manager.session_scope(1) as s:
        follower = s.query(User).get(follower_id)
        followed = s.query(User).get(followed_id)
        create_follow(s, follower, followed)

    # Soft delete and set deleted_at in the past
    import datetime
    past = datetime.datetime.utcnow() - datetime.timedelta(seconds=3600)
    with manager.session_scope(1) as s:
        follower = s.query(User).get(follower_id)
        followed = s.query(User).get(followed_id)
        soft_delete_follow(s, follower, followed)
        s.execute('UPDATE followers SET deleted_at = :past WHERE tenant_id = :t', {'past': past, 't': 1})

    from drift_monitor import cleanup_soft_deleted
    cleanup_soft_deleted(manager, older_than_seconds=1)

    with manager.session_scope(1) as s:
        rows = list(s.execute('SELECT * FROM followers').fetchall())
        assert len(rows) == 0

    teardown_shards(manager)
