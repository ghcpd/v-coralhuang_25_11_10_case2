import contextlib
import time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError, OperationalError

# Simple shard manager that picks DB URL based on tenant_id
SHARD_MAP = {
    # tenant_id: connection_string
    1: 'sqlite:///./shard1.db',
    2: 'sqlite:///./shard2.db',
}


class ShardSession:
    """Context manager for shard-aware sessions with retry on IntegrityError/deadlock.

    This is a simplified replacement for a production-grade sharding manager.
    It supports a retry loop and a naive two-phase commit coordinator for tests.
    """

    def __init__(self, tenant_id, max_retries=3, backoff=0.1):
        self.tenant_id = tenant_id
        self.max_retries = max_retries
        self.backoff = backoff
        self._engine = None
        self._Session = None
        self.session = None

    def __enter__(self):
        db_url = SHARD_MAP.get(self.tenant_id)
        if not db_url:
            raise RuntimeError(f'No shard for tenant {self.tenant_id}')
        self._engine = create_engine(db_url, connect_args={})
        self._Session = sessionmaker(bind=self._engine)
        self.session = self._Session()
        return self

    def commit(self):
        retries = 0
        while True:
            try:
                self.session.commit()
                return
            except (IntegrityError, OperationalError) as e:
                self.session.rollback()
                retries += 1
                if retries > self.max_retries:
                    raise
                time.sleep(self.backoff * retries)

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            try:
                self.session.rollback()
            except Exception:
                pass
        try:
            self.session.close()
        except Exception:
            pass


class TwoPhaseCoordinator:
    """Naive two-phase commit coordinator across multiple shard sessions.

    For testing purposes only: prepare/commit across SQLAlchemy sessions.
    """

    def __init__(self, sessions):
        self.sessions = sessions

    def commit(self):
        # phase 1: try to flush/prepare
        prepared = []
        try:
            for s in self.sessions:
                s.session.flush()
                prepared.append(s)
            # phase 2: commit each
            for s in self.sessions:
                s.commit()
        except Exception:
            for s in prepared:
                try:
                    s.session.rollback()
                except Exception:
                    pass
            raise
