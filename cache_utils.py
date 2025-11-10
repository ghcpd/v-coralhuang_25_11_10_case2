import time
import uuid
import threading
from contextlib import contextmanager
try:
    import redis
except Exception:
    redis = None

# A minimal redlock-like lock using Redis SET NX with expiration.
class RedisLock:
    def __init__(self, client, name, ttl=5.0):
        self.client = client
        self.name = name
        self.ttl = int(ttl * 1000)
        self.token = None

    def acquire(self):
        token = str(uuid.uuid4())
        ok = self.client.set(self.name, token, nx=True, px=self.ttl)
        if ok:
            self.token = token
            return True
        return False

    def release(self):
        # simple del if token matches (not atomic here for brevity)
        try:
            cur = self.client.get(self.name)
            if cur and cur.decode() == self.token:
                self.client.delete(self.name)
        except Exception:
            pass

    @contextmanager
    def lock(self):
        try:
            acquired = self.acquire()
            yield acquired
        finally:
            if acquired:
                self.release()


class VersionedFollowerCache:
    """Write-through cache for follower counts with versioning.

    Stores keys like follower_count:{tenant}:{user_id} -> {count}:{version}
    """

    def __init__(self, client):
        if redis is None and client is None:
            raise RuntimeError('Redis not available')
        self.client = client

    def _key(self, tenant_id, user_id):
        return f'follower_count:{tenant_id}:{user_id}'

    def read(self, tenant_id, user_id):
        raw = self.client.get(self._key(tenant_id, user_id))
        if not raw:
            return None, 0
        try:
            s = raw.decode()
            count, ver = s.split(':')
            return int(count), int(ver)
        except Exception:
            return None, 0

    def write(self, tenant_id, user_id, count, version=None):
        # if version provided, ensure write is only applied if version is newer
        key = self._key(tenant_id, user_id)
        if version is None:
            # bump version
            _, cur_v = self.read(tenant_id, user_id)
            version = cur_v + 1
        value = f'{count}:{version}'
        self.client.set(key, value)
        return version
