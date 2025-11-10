import threading
import time
import logging

log = logging.getLogger(__name__)

class FakeRedis:
    def __init__(self):
        self._data = {}
        self._version = {}
        self._locks = {}
        self._lock = threading.Lock()

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def incr(self, key):
        with self._lock:
            v = int(self._data.get(key, 0)) + 1
            self._data[key] = v
            return v

    def decr(self, key):
        with self._lock:
            v = int(self._data.get(key, 0)) - 1
            self._data[key] = v
            return v

    def set_versioned(self, key, value, version):
        with self._lock:
            cur = self._version.get(key, 0)
            if version >= cur:
                self._data[key] = value
                self._version[key] = version
                log.debug('set_versioned: %s -> %s (v=%s)', key, value, version)
                return True
            else:
                log.debug('set_versioned skipped due to older version: %s %s %s', key, value, version)
                return False

    def get_version(self, key):
        return self._version.get(key, 0)

    def lock(self, key, timeout=5):
        # naive lock with dictionary
        with self._lock:
            if key in self._locks:
                return None
            self._locks[key] = time.time() + timeout
            return key

    def unlock(self, key):
        with self._lock:
            if key in self._locks:
                del self._locks[key]

redis_client = FakeRedis()


class VersionedCounter:
    def __init__(self, redis: FakeRedis, prefix='followers'):
        self.redis = redis
        self.prefix = prefix
        self.lock_timeout = 2

    def key(self, user_id, tenant_id):
        return f"{self.prefix}:{tenant_id}:{user_id}"

    def increment(self, user_id, tenant_id, version):
        k = self.key(user_id, tenant_id)
        lock_id = self.redis.lock(k)
        try:
            # read current value to apply increment
            cur = int(self.redis.get(k) or 0)
            cur += 1
            self.redis.set_versioned(k, cur, version)
            return True
        finally:
            if lock_id:
                self.redis.unlock(lock_id)

    def decrement(self, user_id, tenant_id, version):
        k = self.key(user_id, tenant_id)
        lock_id = self.redis.lock(k)
        try:
            cur = int(self.redis.get(k) or 0)
            cur = max(0, cur - 1)
            self.redis.set_versioned(k, cur, version)
            return True
        finally:
            if lock_id:
                self.redis.unlock(lock_id)

    def set_exact(self, user_id, tenant_id, value, version):
        k = self.key(user_id, tenant_id)
        lock_id = self.redis.lock(k)
        try:
            self.redis.set_versioned(k, value, version)
            return True
        finally:
            if lock_id:
                self.redis.unlock(lock_id)
