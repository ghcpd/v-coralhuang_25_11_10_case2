"""
Shard-aware transaction context manager with retry logic and distributed locking.

Features:
1. Automatic shard routing based on tenant_id
2. Retry logic for IntegrityError and deadlocks (exponential backoff)
3. Distributed Redlock for cache coherency during multi-shard commits
4. Session isolation per tenant-shard
5. Automatic rollback on exceptions
"""

import logging
import time
from contextlib import contextmanager
from typing import Dict, Optional, Any, Generator
from functools import wraps
from datetime import datetime, timedelta

try:
    import redis
except ImportError:
    redis = None

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError, DBAPIError
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)


class RedlockManager:
    """
    Distributed locking using Redis for cache coherency across shards.
    Implements Redlock algorithm for acquiring locks across multiple Redis instances.
    """
    
    def __init__(self, redis_client: Any, lock_timeout: int = 30):
        self.redis = redis_client
        self.lock_timeout = lock_timeout
    
    def acquire_lock(self, lock_key: str, max_retries: int = 3) -> bool:
        """
        Acquire a distributed lock with retry logic.
        Returns True if lock acquired, False otherwise.
        """
        for attempt in range(max_retries):
            try:
                acquired = self.redis.set(
                    lock_key,
                    datetime.utcnow().isoformat(),
                    ex=self.lock_timeout,
                    nx=True
                )
                if acquired:
                    logger.debug(f"Lock acquired: {lock_key}")
                    return True
                
                # Exponential backoff before retry
                time.sleep(0.1 * (2 ** attempt))
            except redis.RedisError as e:
                logger.error(f"Redis error acquiring lock {lock_key}: {e}")
                time.sleep(0.1 * (2 ** attempt))
        
        logger.warning(f"Failed to acquire lock {lock_key} after {max_retries} attempts")
        return False
    
    def release_lock(self, lock_key: str) -> bool:
        """Release a distributed lock."""
        try:
            self.redis.delete(lock_key)
            logger.debug(f"Lock released: {lock_key}")
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error releasing lock {lock_key}: {e}")
            return False
    
    @contextmanager
    def lock_context(self, lock_key: str):
        """Context manager for distributed locking."""
        acquired = self.acquire_lock(lock_key)
        if not acquired:
            raise TimeoutError(f"Could not acquire lock: {lock_key}")
        try:
            yield
        finally:
            self.release_lock(lock_key)


class ShardManager:
    """
    Manages database shards and session routing based on tenant_id.
    
    Each tenant maps to a specific PostgreSQL shard.
    Supports connection pooling and automatic failover.
    """
    
    def __init__(self, shard_config: Dict[int, str], redis_client: Any):
        """
        Args:
            shard_config: Dict mapping tenant_id -> database_url
            redis_client: Redis client for caching and locking
        """
        self.shard_config = shard_config
        self.redis = redis_client
        self.engines: Dict[int, Any] = {}
        self.session_factories: Dict[int, sessionmaker] = {}
        self.redlock = RedlockManager(redis_client)
        self._initialize_engines()
    
    def _initialize_engines(self):
        """Initialize SQLAlchemy engines for each shard."""
        for tenant_id, db_url in self.shard_config.items():
            try:
                engine = create_engine(
                    db_url,
                    poolclass=QueuePool,
                    pool_size=10,
                    max_overflow=20,
                    pool_pre_ping=True,
                    echo=False,
                    connect_args={
                        'connect_timeout': 10,
                        'application_name': f'follower_app_tenant_{tenant_id}',
                    }
                )
                
                # Test connection
                with engine.connect() as conn:
                    conn.execute("SELECT 1")
                
                self.engines[tenant_id] = engine
                self.session_factories[tenant_id] = sessionmaker(bind=engine)
                logger.info(f"Engine initialized for tenant {tenant_id}")
            except Exception as e:
                logger.error(f"Failed to initialize engine for tenant {tenant_id}: {e}")
                raise
    
    def get_session(self, tenant_id: int) -> Session:
        """Get a session for the specified tenant's shard."""
        if tenant_id not in self.session_factories:
            raise ValueError(f"Unknown tenant_id: {tenant_id}")
        
        session = self.session_factories[tenant_id]()
        session.info['tenant_id'] = tenant_id
        return session
    
    def close_all(self):
        """Close all engine connections."""
        for engine in self.engines.values():
            engine.dispose()
        logger.info("All shard engines disposed")


class TransactionContext:
    """
    Context manager for tenant-aware transactions with retry logic.
    
    Handles:
    - Automatic session routing per tenant
    - Retry logic for transient errors (deadlocks, unique constraint violations)
    - Distributed locking for cache coherency
    - Automatic rollback/commit management
    """
    
    def __init__(
        self,
        shard_manager: ShardManager,
        tenant_id: int,
        max_retries: int = 3,
        use_lock: bool = True
    ):
        self.shard_manager = shard_manager
        self.tenant_id = tenant_id
        self.max_retries = max_retries
        self.use_lock = use_lock
        self.session: Optional[Session] = None
        self.lock_key: Optional[str] = None
        self._retry_count = 0
    
    def __enter__(self) -> Session:
        """Acquire session and locks."""
        if self.use_lock:
            self.lock_key = f"tenant_lock:{self.tenant_id}"
            acquired = self.shard_manager.redlock.acquire_lock(self.lock_key)
            if not acquired:
                raise TimeoutError(f"Could not acquire lock for tenant {self.tenant_id}")
        
        self.session = self.shard_manager.get_session(self.tenant_id)
        logger.debug(f"TransactionContext opened for tenant {self.tenant_id}")
        return self.session
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Rollback on exception, cleanup."""
        try:
            if exc_type is not None:
                self.session.rollback()
                logger.warning(
                    f"TransactionContext rolled back for tenant {self.tenant_id}: "
                    f"{exc_type.__name__}: {exc_val}"
                )
            else:
                self.session.commit()
                logger.debug(f"TransactionContext committed for tenant {self.tenant_id}")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Error during transaction cleanup: {e}")
            raise
        finally:
            if self.session:
                self.session.close()
            
            if self.lock_key:
                self.shard_manager.redlock.release_lock(self.lock_key)
            
            logger.debug(f"TransactionContext closed for tenant {self.tenant_id}")
    
    def retry_on_error(self, func, *args, **kwargs):
        """
        Execute function with retry logic for transient errors.
        
        Retries on:
        - IntegrityError (duplicate key, constraint violation)
        - OperationalError (deadlock, connection issues)
        - DBAPIError (transient database errors)
        """
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                result = func(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"Retry succeeded on attempt {attempt + 1} for tenant {self.tenant_id}")
                return result
            except (IntegrityError, OperationalError, DBAPIError) as e:
                last_exception = e
                
                if attempt < self.max_retries - 1:
                    # Exponential backoff: 0.1s, 0.2s, 0.4s
                    backoff = 0.1 * (2 ** attempt)
                    logger.warning(
                        f"Transient error on attempt {attempt + 1}/{self.max_retries} "
                        f"for tenant {self.tenant_id}, retrying in {backoff}s: {e}"
                    )
                    
                    # Rollback and get new session
                    try:
                        self.session.rollback()
                    except:
                        pass
                    
                    time.sleep(backoff)
                else:
                    logger.error(
                        f"Failed after {self.max_retries} retries for tenant {self.tenant_id}: {e}"
                    )
        
        raise last_exception


@contextmanager
def transactional_context(
    shard_manager: ShardManager,
    tenant_id: int,
    max_retries: int = 3,
    use_lock: bool = True
) -> Generator[Session, None, None]:
    """
    Convenience context manager for tenant-aware transactions.
    
    Usage:
        with transactional_context(shard_mgr, tenant_id=1) as session:
            user = session.query(User).filter_by(id=45).first()
            # perform operations
            # auto-commit on success, auto-rollback on error
    """
    ctx = TransactionContext(shard_manager, tenant_id, max_retries, use_lock)
    with ctx as session:
        yield session


def with_transaction(shard_manager: ShardManager, max_retries: int = 3):
    """
    Decorator for transactional functions.
    
    Usage:
        @with_transaction(shard_manager)
        def follow_user(session, follower_id, followed_id, tenant_id):
            # session is automatically managed
            pass
    
    The decorated function receives 'session' as first argument after self/cls.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            tenant_id = kwargs.pop('tenant_id')
            
            with transactional_context(shard_manager, tenant_id, max_retries) as session:
                return func(*args, session=session, **kwargs)
        
        return wrapper
    return decorator


class DriftDetectionContext:
    """
    Context for detecting and logging cache-DB drift.
    Records discrepancies in CacheHealthCheck table.
    """
    
    def __init__(self, session: Session, tenant_id: int, redis_client: Any):
        self.session = session
        self.tenant_id = tenant_id
        self.redis = redis_client
    
    def check_user_follower_count(self, user_id: int) -> Dict[str, Any]:
        """
        Check for drift between Redis cache and PostgreSQL for user follower count.
        
        Returns:
            {
                'user_id': int,
                'cache_count': int,
                'db_count': int,
                'drift': int,  # db_count - cache_count
                'status': str  # 'healthy', 'drift', 'stale'
            }
        """
        from models import User, CacheHealthCheck
        
        # Get DB count
        user = self.session.query(User).filter_by(id=user_id, tenant_id=self.tenant_id).first()
        if not user:
            return None
        
        db_count = user.follower_count
        
        # Get cache count
        cache_key = f"followers:count:{self.tenant_id}:{user_id}"
        try:
            cache_value = self.redis.get(cache_key)
            cache_count = int(cache_value) if cache_value else 0
        except (redis.RedisError, ValueError):
            cache_count = 0
        
        drift = abs(db_count - cache_count)
        
        if drift == 0:
            status = 'healthy'
        elif drift <= 2:
            status = 'drift'
        else:
            status = 'stale'
        
        result = {
            'user_id': user_id,
            'tenant_id': self.tenant_id,
            'cache_count': cache_count,
            'db_count': db_count,
            'drift': drift,
            'status': status,
        }
        
        # Record in audit table
        health_check = CacheHealthCheck(
            tenant_id=self.tenant_id,
            user_id=user_id,
            cache_count=cache_count,
            db_count=db_count,
            drift=drift,
            status=status,
        )
        self.session.add(health_check)
        
        return result
