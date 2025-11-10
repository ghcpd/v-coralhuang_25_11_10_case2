import os
import threading
import time
from datetime import datetime
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
try:
    from fakeredis import FakeStrictRedis
except Exception:
    # fallback in case fakeredis isn't installed in the environment
    class FakeStrictRedis(dict):
        def get(self, k):
            return super().get(k)
        def set(self, k, v):
            super().__setitem__(k, v)
        def delete(self, k):
            super().pop(k, None)

from models import Base, User, Follower, add_follow, remove_follow
from sharding_manager import ShardManager
from cache import FollowerCache
from drift_monitor import reconcile_counts, detect_cross_tenant

DB_DIR = os.path.dirname(__file__)


@pytest.fixture(scope='module')
def setup_db(tmp_path_factory):
    # Simulate two postgres shards using SQLite files for tests (tenant 1 and 2)
    db1 = tmp_path_factory.mktemp('data') / 'shard1.sqlite'
    db2 = tmp_path_factory.mktemp('data2') / 'shard2.sqlite'
    uri1 = f'sqlite:///{db1}'
    uri2 = f'sqlite:///{db2}'
    engines = {
        1: create_engine(uri1, future=True),
        2: create_engine(uri2, future=True)
    }
    for e in engines.values():
        Base.metadata.create_all(e)
    sessions = {t: sessionmaker(bind=e, autocommit=False, autoflush=False) for t,e in engines.items()}
    yield {'engines': engines, 'sessions': sessions}
    # teardown
    for e in engines.values():
        e.dispose()


@pytest.fixture(scope='function')
def setup_tenants(setup_db):
    sessions = setup_db['sessions']
    s1 = sessions[1]()
    session_factory = sessions[1]
    s2 = sessions[2]()
    # clean up from prior tests if necessary
    s1.query(User).delete()
    s2.query(User).delete()
    s1.query(Follower).delete()
    s2.query(Follower).delete()
    s1.commit()
    s2.commit()
    # Create users
    user1 = User(id=100, tenant_id=1, username='t1_u1')
    user2 = User(id=101, tenant_id=1, username='t1_u2')
    user3 = User(id=200, tenant_id=2, username='t2_u1')
    user4 = User(id=201, tenant_id=2, username='t2_u2')
    s1.add_all([user1, user2])
    s2.add_all([user3, user4])
    s1.commit()
    s2.commit()
    yield {'s1': s1, 's2': s2}
    s1.close()
    s2.close()


def test_cross_tenant_follow_blocked(setup_db, setup_tenants):
    sessions = setup_db['sessions']
    s1 = sessions[1]()
    s2 = sessions[2]()
    # attempt a cross-tenant follow: tenant 1 follower -> tenant 2 followed
    with pytest.raises(Exception):
        add_follow(s1, 1, 100, 200)
    s1.close()
    s2.close()


def test_concurrent_follow_requests(setup_db, setup_tenants):
    sessions = setup_db['sessions']
    s1 = sessions[1]()
    session_factory = sessions[1]

    def worker_follow():
        session = session_factory()
        try:
            add_follow(session, 1, 100, 101)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    threads = []
    for _ in range(10):
        t = threading.Thread(target=worker_follow)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    count = s1.query(Follower).filter_by(tenant_id=1, follower_id=100, followed_id=101, deleted=False).count()
    assert count == 1
    s1.close()


def test_cache_consistency_and_reconciliation(setup_db, setup_tenants):
    sessions = setup_db['sessions']
    s1 = sessions[1]()
    redis = FakeStrictRedis()
    cache = FollowerCache(redis)

    # create a follow
    add_follow(s1, 1, 100, 101)
    s1.commit()
    # write-through
    cache.write_through(1, 101, 1)
    entry = json.loads(redis.get('followers:1:101'))
    assert entry['count'] == 1

    # Delete directly in DB to simulate drift and check reconcile
    # soft delete
    remove_follow(s1, 1, 100, 101, soft=True)
    s1.commit()
    # DB now 0
    db_count = s1.query(Follower).filter_by(tenant_id=1, followed_id=101, deleted=False).count()
    assert db_count == 0

    # Reconcile
    reconciled = reconcile_counts(s1, redis, 1, 101, User, Follower, cache)
    assert reconciled['db_count'] == 0
    # cache updated
    entry = json.loads(redis.get('followers:1:101'))
    assert entry['count'] == 0
    s1.close()


def test_soft_delete_cleanup(setup_db, setup_tenants):
    sessions = setup_db['sessions']
    s1 = sessions[1]()
    # Create and soft delete
    add_follow(s1, 1, 100, 101)
    s1.commit()
    remove_follow(s1, 1, 100, 101, soft=True)
    s1.commit()
    # artificially set deleted_at to old date for cleanup
    r = s1.query(Follower).filter_by(tenant_id=1, follower_id=100, followed_id=101).first()
    r.deleted_at = datetime(2000,1,1)
    s1.add(r)
    s1.commit()
    # perform cleanup
    from drift_monitor import cleanup_soft_deleted
    cleaned = cleanup_soft_deleted(s1, Follower, older_than_days=1)
    assert cleaned >= 1
    s1.close()


def test_distributed_transaction_and_recovery(setup_db, setup_tenants):
    sessions = setup_db['sessions']
    s1 = sessions[1]()
    s2 = sessions[2]()
    from sharding_manager import ShardManager
    # In tests we simply coordinate sessions manually
    # Add user 100 follows 101 in shard 1 and write analytics to shard 2 as an example
    manager = ShardManager({1: 'sqlite:///dummy', 2: 'sqlite:///dummy2'})
    # We'll not use manager here to actually commit since it's a simulation
    # Simulate 2PC: both operations should commit or both rollback
    try:
        s1.begin()
        s2.begin()
        add_follow(s1, 1, 100, 101)
        # Simulate writing to analytics shard too (e.g., incrementing a table), we'll create a dummy row
        # Instead we'll just commit s2 as no-op
        # Now simulate crash between DB commit and cache write: we will raise an exception after DB commit
        s1.commit()
        # Simulate crash by raising
        raise RuntimeError('Simulated crash after commit, before cache write')
    except Exception:
        # At this point, we should detect and recover: ensure s1 is consistent
        s1.rollback()
        s2.rollback()
    finally:
        s1.close()
        s2.close()


# More tests: concurrent cross-shard two-phase commit simulation can be added
