from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError, OperationalError
import threading
import time

class ShardManager:
    """Simple shard manager for tests.

    Maps tenant_id to a DB engine (SQLite file per tenant for simulation).
    Provides a session_scope that opens a session for the appropriate shard.
    Also supports a 'coordinated' commit across multiple shards.

    This includes a basic retry loop for IntegrityError and deadlock-like OperationalError.
    """

    def __init__(self, shards=None, echo=False):
        # shards: dict tenant_id -> connection string
        self.engines = {}
        self.Session = {}
        self.lock = threading.Lock()
        if shards:
            for tid, url in shards.items():
                self.register_shard(tid, url, echo=echo)

    def register_shard(self, tenant_id, url, echo=False):
        engine = create_engine(url, echo=echo, pool_pre_ping=True)
        self.engines[tenant_id] = engine
        self.Session[tenant_id] = sessionmaker(bind=engine)

    @contextmanager
    def session_scope(self, tenant_id, retries=3, backoff=0.1):
        Session = self.Session.get(tenant_id)
        if not Session:
            raise RuntimeError('unknown shard for tenant %s' % tenant_id)
        session = Session()
        try:
            yield session
            attempt = 0
            while True:
                try:
                    session.commit()
                    break
                except IntegrityError as e:
                    attempt += 1
                    session.rollback()
                    if attempt > retries:
                        raise
                    time.sleep(backoff * attempt)
                except OperationalError as e:
                    # simulate deadlock retry
                    attempt += 1
                    session.rollback()
                    if attempt > retries:
                        raise
                    time.sleep(backoff * attempt)
        finally:
            session.close()

    def create_all(self, Base):
        # create tables on all registered engines
        for e in self.engines.values():
            Base.metadata.create_all(e)

    def drop_all(self, Base):
        for e in self.engines.values():
            Base.metadata.drop_all(e)

    def coordinated_commit(self, sessions):
        """Attempt a two-phase commit across multiple SQLAlchemy sessions.

        For engines that don't support two-phase commit (sqlite), we simulate a coordinator
        by committing in a defined order and rolling back on error. This is only for unit tests.
        """
        # In real deployments, you would use two-phase commit or an external coordinator
        # such as XA/2PC or a distributed transaction manager. Here we simulate.
        committed = []
        try:
            for s in sessions:
                s.flush()
            for s in sessions:
                s.commit()
                committed.append(s)
        except Exception:
            # rollback all previously committed in reverse
            for s in reversed(committed):
                try:
                    s.rollback()
                except Exception:
                    pass
            raise
