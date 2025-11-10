import time
import logging
import json
try:
    from redis import Redis
except Exception:
    Redis = None
try:
    from redlock import Redlock
except Exception:
    Redlock = None

logger = logging.getLogger(__name__)

class FollowerCache:
    def __init__(self, redis_client: Redis, redlock_instances=None, prefix='followers'):
        self.redis = redis_client
        # redlock expects list of dict{host,port} or redis instances; using redis instance(s)
        if redlock_instances is None:
            redlock_instances = [self.redis]
        # fallback to a naive lock if redlock isn't available
        if Redlock is None:
            class DummyLockManager:
                def lock(self, key, t):
                    return key
                def unlock(self, lock):
                    return True
            self.lock_manager = DummyLockManager()
        else:
            self.lock_manager = Redlock(redlock_instances)
        self.prefix = prefix

    def _key(self, tenant_id, user_id):
        return f'{self.prefix}:{tenant_id}:{user_id}'

    def get_count(self, tenant_id, user_id):
        k = self._key(tenant_id, user_id)
        v = self.redis.get(k)
        if not v:
            return None
        try:
            payload = json.loads(v)
            return payload.get('count'), payload.get('version')
        except Exception:
            return None

    def set_count(self, tenant_id, user_id, count, version):
        k = self._key(tenant_id, user_id)
        payload = {'count': count, 'version': version}
        self.redis.set(k, json.dumps(payload))

    def increment_count(self, tenant_id, user_id, delta=1):
        k = self._key(tenant_id, user_id)
        # Acquire lock per key
        lock = None
        try:
            lock = self.lock_manager.lock(k, 1000)
            v = self.get_count(tenant_id, user_id)
            if v is None:
                # seed with 0 version
                version = int(time.time() * 1000)
                count = delta
            else:
                count, version = v
                version = int(time.time() * 1000)
                count = count + delta
            self.set_count(tenant_id, user_id, count, version)
            return count
        except Exception as e:
            logger.exception('Failed to increment cache: %s', e)
            raise
        finally:
            if lock:
                try:
                    self.lock_manager.unlock(lock)
                except Exception:
                    pass

    def write_through(self, tenant_id, user_id, db_count):
        # Writes to redis with new version after writing to DB
        version = int(time.time() * 1000)
        self.set_count(tenant_id, user_id, db_count, version)

    def delete(self, tenant_id, user_id):
        k = self._key(tenant_id, user_id)
        self.redis.delete(k)

    def reconcile_user(self, tenant_id, user_id, db_count):
        existing = self.get_count(tenant_id, user_id)
        if existing is None or existing[0] != db_count:
            self.write_through(tenant_id, user_id, db_count)
            return True
        return False
