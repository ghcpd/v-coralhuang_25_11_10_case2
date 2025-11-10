import json
import time
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from models import User, followers, Base
import cache_utils
try:
    import redis
except Exception:
    redis = None


def reconcile(shard_db_url, redis_client, tenant_id, max_rows=100):
    engine = create_engine(shard_db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    results = []
    for user in session.query(User).filter(User.tenant_id == tenant_id).limit(max_rows):
        # count followers in DB excluding soft-deleted
        db_count = session.query(func.count()).select_from(followers).filter(
            followers.c.followed_id == user.id,
            followers.c.tenant_id == tenant_id,
            followers.c.deleted == False,
        ).scalar()
        if redis_client:
            cache = cache_utils.VersionedFollowerCache(redis_client)
            cache_count, _ = cache.read(tenant_id, user.id)
        else:
            cache_count = None
        if cache_count != db_count:
            results.append({'user_id': user.id, 'tenant_id': tenant_id, 'db': db_count, 'cache': cache_count})
    session.close()
    return results


if __name__ == '__main__':
    # simple runner reading a local config
    cfg = {'shard_map': {1: 'sqlite:///./shard1.db'}}
    r = None
    out = reconcile(cfg['shard_map'][1], r, 1)
    print(json.dumps(out, indent=2))
