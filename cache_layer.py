"""
Versioned, write-through cache layer with distributed locking.

Features:
1. Versioned cache entries to detect stale data
2. Write-through pattern: update DB first, then cache
3. Distributed Redlock to prevent race conditions
4. Periodic cache verification against PostgreSQL
5. Automatic cache invalidation on soft deletes
6. TTL-based cache expiry with refresh logic
"""

import logging
import time
import json
from typing import Optional, Any, Tuple
from datetime import datetime, timedelta

try:
    import redis
except ImportError:
    redis = None

logger = logging.getLogger(__name__)


class VersionedCacheEntry:
    """
    Versioned cache entry for coherency.
    
    Format: {
        'version': int,
        'timestamp': ISO datetime,
        'value': any,
        'ttl': int (seconds)
    }
    """
    
    def __init__(self, value: Any, version: int = 1, ttl: int = 3600):
        self.value = value
        self.version = version
        self.timestamp = datetime.utcnow()
        self.ttl = ttl
    
    def to_dict(self) -> dict:
        return {
            'version': self.version,
            'timestamp': self.timestamp.isoformat(),
            'value': self.value,
            'ttl': self.ttl,
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict())
    
    @staticmethod
    def from_json(json_str: str) -> 'VersionedCacheEntry':
        data = json.loads(json_str)
        entry = VersionedCacheEntry(data['value'], data['version'], data['ttl'])
        entry.timestamp = datetime.fromisoformat(data['timestamp'])
        return entry
    
    def is_expired(self) -> bool:
        age = (datetime.utcnow() - self.timestamp).total_seconds()
        return age > self.ttl


class WriteThroughCache:
    """
    Write-through cache layer with versioning and distributed locking.
    
    All writes go to cache AFTER successful DB writes.
    All reads check cache first, fallback to DB if missing/stale.
    """
    
    def __init__(
        self,
        redis_client: Any,
        lock_timeout: int = 30,
        cache_ttl: int = 3600,
    ):
        self.redis = redis_client
        self.lock_timeout = lock_timeout
        self.cache_ttl = cache_ttl
    
    def _get_lock_key(self, key: str) -> str:
        """Get distributed lock key for cache operation."""
        return f"lock:{key}"
    
    def acquire_write_lock(self, key: str, max_retries: int = 3) -> bool:
        """Acquire exclusive lock for cache write."""
        lock_key = self._get_lock_key(key)
        
        for attempt in range(max_retries):
            try:
                acquired = self.redis.set(
                    lock_key,
                    datetime.utcnow().isoformat(),
                    ex=self.lock_timeout,
                    nx=True
                )
                if acquired:
                    logger.debug(f"Write lock acquired: {key}")
                    return True
            except Exception as e:
                logger.error(f"Error acquiring write lock {key}: {e}")
            
            # Exponential backoff
            time.sleep(0.1 * (2 ** attempt))
        
        logger.warning(f"Failed to acquire write lock {key} after {max_retries} attempts")
        return False
    
    def release_write_lock(self, key: str):
        """Release exclusive lock."""
        lock_key = self._get_lock_key(key)
        try:
            self.redis.delete(lock_key)
            logger.debug(f"Write lock released: {key}")
        except Exception as e:
            logger.error(f"Error releasing write lock {key}: {e}")
    
    def set_with_lock(
        self,
        key: str,
        value: Any,
        version: int = 1,
        ttl: Optional[int] = None
    ) -> bool:
        """
        Set cache value with distributed locking.
        
        Returns True if successfully cached, False otherwise.
        """
        if not self.acquire_write_lock(key):
            return False
        
        try:
            ttl = ttl or self.cache_ttl
            entry = VersionedCacheEntry(value, version, ttl)
            self.redis.setex(key, ttl, entry.to_json())
            logger.debug(f"Cache set: {key} version={version}")
            return True
        except Exception as e:
            logger.error(f"Error setting cache {key}: {e}")
            return False
        finally:
            self.release_write_lock(key)
    
    def get(self, key: str) -> Tuple[Optional[Any], Optional[int]]:
        """
        Get cache value with version.
        
        Returns (value, version) or (None, None) if not found/expired.
        """
        try:
            cached = self.redis.get(key)
            if not cached:
                return None, None
            
            entry = VersionedCacheEntry.from_json(cached)
            
            if entry.is_expired():
                logger.debug(f"Cache expired: {key}")
                self.redis.delete(key)
                return None, None
            
            logger.debug(f"Cache hit: {key} version={entry.version}")
            return entry.value, entry.version
        except Exception as e:
            logger.error(f"Error getting cache {key}: {e}")
            return None, None
    
    def delete(self, key: str) -> bool:
        """Delete cache entry."""
        try:
            self.redis.delete(key)
            logger.debug(f"Cache deleted: {key}")
            return True
        except Exception as e:
            logger.error(f"Error deleting cache {key}: {e}")
            return False
    
    def invalidate_pattern(self, pattern: str) -> int:
        """
        Invalidate all keys matching pattern.
        Uses Redis SCAN for efficiency.
        """
        try:
            count = 0
            for key in self.redis.scan_iter(match=pattern):
                self.redis.delete(key)
                count += 1
            
            logger.debug(f"Invalidated {count} keys matching {pattern}")
            return count
        except Exception as e:
            logger.error(f"Error invalidating pattern {pattern}: {e}")
            return 0


class FollowerCacheManager:
    """
    Specialized cache manager for follower counts with drift detection.
    
    Maintains:
    - follower_count:{tenant_id}:{user_id} - current follower count
    - followers:{tenant_id}:{user_id}:set - set of follower IDs
    - version:{tenant_id}:{user_id} - cache version for optimistic locking
    """
    
    def __init__(self, write_through_cache: WriteThroughCache):
        self.cache = write_through_cache
    
    def get_follower_count_key(self, tenant_id: int, user_id: int) -> str:
        return f"followers:count:{tenant_id}:{user_id}"
    
    def get_followers_set_key(self, tenant_id: int, user_id: int) -> str:
        return f"followers:set:{tenant_id}:{user_id}"
    
    def get_version_key(self, tenant_id: int, user_id: int) -> str:
        return f"followers:version:{tenant_id}:{user_id}"
    
    def update_follower_count(
        self,
        tenant_id: int,
        user_id: int,
        count: int,
        version: int = 1,
    ) -> bool:
        """
        Update cached follower count with version.
        
        Args:
            tenant_id: Tenant ID
            user_id: User ID
            count: New follower count (from DB)
            version: Cache version (incremented on each update)
        
        Returns True if successfully cached.
        """
        key = self.get_follower_count_key(tenant_id, user_id)
        return self.cache.set_with_lock(key, count, version=version)
    
    def get_follower_count(self, tenant_id: int, user_id: int) -> Tuple[Optional[int], Optional[int]]:
        """
        Get cached follower count and version.
        
        Returns (count, version) or (None, None) if not cached.
        """
        key = self.get_follower_count_key(tenant_id, user_id)
        return self.cache.get(key)
    
    def increment_follower_count(
        self,
        tenant_id: int,
        user_id: int,
        version: int,
    ) -> bool:
        """
        Increment follower count in cache.
        Uses version to prevent lost updates.
        """
        key = self.get_follower_count_key(tenant_id, user_id)
        
        current_count, cached_version = self.get_follower_count(tenant_id, user_id)
        
        if current_count is None:
            logger.warning(f"Cannot increment: {key} not in cache")
            return False
        
        if cached_version != version:
            logger.warning(
                f"Version mismatch for {key}: "
                f"expected {version}, got {cached_version}"
            )
            return False
        
        new_count = current_count + 1
        new_version = version + 1
        
        return self.update_follower_count(tenant_id, user_id, new_count, new_version)
    
    def decrement_follower_count(
        self,
        tenant_id: int,
        user_id: int,
        version: int,
    ) -> bool:
        """
        Decrement follower count in cache.
        Uses version to prevent lost updates.
        """
        key = self.get_follower_count_key(tenant_id, user_id)
        
        current_count, cached_version = self.get_follower_count(tenant_id, user_id)
        
        if current_count is None:
            logger.warning(f"Cannot decrement: {key} not in cache")
            return False
        
        if cached_version != version:
            logger.warning(
                f"Version mismatch for {key}: "
                f"expected {version}, got {cached_version}"
            )
            return False
        
        new_count = max(0, current_count - 1)
        new_version = version + 1
        
        return self.update_follower_count(tenant_id, user_id, new_count, new_version)
    
    def invalidate_user_cache(self, tenant_id: int, user_id: int) -> int:
        """
        Invalidate all cache entries for a user.
        Called after soft delete or profile changes.
        """
        pattern = f"followers:*:{tenant_id}:{user_id}"
        return self.cache.invalidate_pattern(pattern)
    
    def verify_cache_consistency(
        self,
        tenant_id: int,
        user_id: int,
        db_count: int
    ) -> Tuple[bool, int]:
        """
        Verify cache count matches DB count.
        
        Returns (is_consistent, drift)
            is_consistent: True if cache equals DB (or cache miss)
            drift: absolute difference
        """
        cache_count, _ = self.get_follower_count(tenant_id, user_id)
        
        if cache_count is None:
            # Cache miss is acceptable - will be lazily filled
            return True, 0
        
        drift = abs(cache_count - db_count)
        is_consistent = drift == 0
        
        if not is_consistent:
            logger.warning(
                f"Cache inconsistency detected: "
                f"tenant={tenant_id} user={user_id} "
                f"cache={cache_count} db={db_count} drift={drift}"
            )
        
        return is_consistent, drift


class CacheReconciliationTask:
    """
    Background task for periodic cache reconciliation.
    Detects and repairs cache-DB drift.
    """
    
    def __init__(
        self,
        session: Any,
        cache_manager: FollowerCacheManager,
        tenant_id: int,
        batch_size: int = 100,
    ):
        self.session = session
        self.cache_manager = cache_manager
        self.tenant_id = tenant_id
        self.batch_size = batch_size
    
    def reconcile_all_users(self) -> dict:
        """
        Reconcile all users in tenant.
        
        Returns:
            {
                'total_users': int,
                'inconsistent': int,
                'fixed': int,
                'drift_summary': {
                    'total_drift': int,
                    'max_drift': int,
                    'avg_drift': float,
                }
            }
        """
        from models import User
        
        total_users = 0
        inconsistent_count = 0
        fixed_count = 0
        total_drift = 0
        max_drift = 0
        
        # Query users in batches
        offset = 0
        while True:
            users = self.session.query(User).filter_by(
                tenant_id=self.tenant_id
            ).offset(offset).limit(self.batch_size).all()
            
            if not users:
                break
            
            for user in users:
                total_users += 1
                is_consistent, drift = self.cache_manager.verify_cache_consistency(
                    self.tenant_id,
                    user.id,
                    user.follower_count
                )
                
                total_drift += drift
                max_drift = max(max_drift, drift)
                
                if not is_consistent:
                    inconsistent_count += 1
                    
                    # Fix: update cache with DB value
                    success = self.cache_manager.update_follower_count(
                        self.tenant_id,
                        user.id,
                        user.follower_count,
                        version=user.cache_version
                    )
                    
                    if success:
                        fixed_count += 1
                        logger.info(f"Fixed cache for user {user.id}: drift was {drift}")
            
            offset += self.batch_size
        
        avg_drift = total_drift / total_users if total_users > 0 else 0
        
        return {
            'total_users': total_users,
            'inconsistent': inconsistent_count,
            'fixed': fixed_count,
            'drift_summary': {
                'total_drift': total_drift,
                'max_drift': max_drift,
                'avg_drift': avg_drift,
            },
        }
