import threading
import time
import json
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, User, followers
from sharding_manager import ShardSession, TwoPhaseCoordinator
import cache_utils
import os


LOG_PATH = os.path.join('logs', 'test_run.log')
os.makedirs('logs', exist_ok=True)


def setup_shards():
    # create sqlite files and metadata
    from sharding_manager import SHARD_MAP
    for url in set(SHARD_MAP.values()):
        engine = create_engine(url)
        Base.metadata.create_all(engine)


def test_cross_tenant_follow():
    # attempt to create cross-tenant relationship - should be blocked
    # For this lightweight test we simulate by inserting into followers table directly
    from sharding_manager import SHARD_MAP
    engine1 = create_engine(SHARD_MAP[1])
    engine2 = create_engine(SHARD_MAP[2])
    Session1 = sessionmaker(bind=engine1)
    Session2 = sessionmaker(bind=engine2)
    s1 = Session1()
    s2 = Session2()
    try:
        u1 = User(tenant_id=1, username='u1')
        u2 = User(tenant_id=2, username='u2')
        s1.add(u1)
        s2.add(u2)
        s1.commit()
        s2.commit()
        # cross insert into shard1's followers referencing user in shard2
        conn = engine1.connect()
        conn.execute(followers.insert().values(tenant_id=1, follower_id=u1.id, followed_id=u2.id, deleted=False))
        conn.close()
        result = 'committed'
    except Exception as e:
        result = f'failed:{e}'
    finally:
        s1.close(); s2.close()
    with open(LOG_PATH, 'a') as f:
        f.write('test_cross_tenant_follow:' + result + '\n')
    return 'committed' not in result


def test_concurrent_follow():
    # simulate concurrent inserts to create duplicates without unique constraint
    from sharding_manager import SHARD_MAP
    engine = create_engine(SHARD_MAP[1])
    Session = sessionmaker(bind=engine)
    session = Session()
    a = User(tenant_id=1, username='concurrent_a_' + uuid.uuid4().hex[:8])
    b = User(tenant_id=1, username='concurrent_b_' + uuid.uuid4().hex[:8])
    session.add_all([a, b])
    session.commit()
    a_id, b_id = a.id, b.id
    session.close()

    def follow_once():
        s = Session()
        try:
            conn = s.connection()
            conn.execute(followers.insert().values(tenant_id=1, follower_id=a_id, followed_id=b_id, deleted=False))
            s.commit()
        except Exception as e:
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=follow_once) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    s = Session()
    from sqlalchemy import select, func
    cnt = s.execute(select(func.count()).select_from(followers).where(
        (followers.c.tenant_id == 1) & (followers.c.follower_id == a_id) & (followers.c.followed_id == b_id) & (followers.c.deleted == False)
    )).scalar()
    s.close()
    with open(LOG_PATH, 'a') as f:
        f.write(f'test_concurrent_follow:count={cnt}\n')
    return cnt == 1


def run_all():
    setup_shards()
    results = {}
    results['test_cross_tenant_follow'] = test_cross_tenant_follow()
    results['test_concurrent_follow'] = test_concurrent_follow()
    with open(LOG_PATH, 'a') as f:
        f.write(json.dumps(results) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    run_all()
