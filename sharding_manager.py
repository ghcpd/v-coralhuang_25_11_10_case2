import time
import logging
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError
from sqlalchemy.engine.url import URL

from sqlalchemy.orm import scoped_session

logger = logging.getLogger(__name__)

class ShardManager:
    """A simplified sharding manager that provides a session per tenant and
    supports 2PC-like coordinator for distributed commits across multiple shards.
    """
    def __init__(self, shard_map):
        # shard_map: tenant_id -> SQLALCHEMY_DATABASE_URI
        self.shard_map = shard_map
        self.engines = {t: create_engine(uri, future=True) for t, uri in shard_map.items()}
        self.sessions = {t: scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False)) for t, engine in self.engines.items()}

    def get_session(self, tenant_id):
        if tenant_id not in self.sessions:
            raise KeyError(f'No shard for tenant {tenant_id}')
        return self.sessions[tenant_id]()

    def close_session(self, tenant_id):
        if tenant_id in self.sessions:
            self.sessions[tenant_id].remove()

    @contextmanager
    def transaction(self, tenant_id, retries=3, backoff=0.1):
        session = self.get_session(tenant_id)
        try:
            attempt = 0
            while True:
                try:
                    yield session
                    session.commit()
                    break
                except (IntegrityError, OperationalError) as e:
                    session.rollback()
                    attempt += 1
                    if attempt >= retries:
                        logger.exception('Transaction failed after retries')
                        raise
                    logger.warning('Transient failure in transaction, retrying: %s', e)
                    time.sleep(backoff * attempt)
        finally:
            session.close()

    @contextmanager
    def distributed_transaction(self, tenant_sessions_and_tenants, retries=3, backoff=0.1):
        """Perform a distributed commit using SQLAlchemy two-phase transaction
        across sessions from multiple shards.
        tenant_sessions_and_tenants: list of tuples (tenant_id, session) or similar
        """
        # Begin 2PC
        transactions = []
        attempt = 0
        # Retry loop for transient errors
        try:
            while True:
                transactions = []
                try:
                    # Begin two-phase transactions
                    for tenant_id, session in tenant_sessions_and_tenants:
                        tx = session.begin_twophase()
                        transactions.append((tenant_id, session, tx))

                    yield [s for _, s in tenant_sessions_and_tenants]

                    # Prepare all transactions
                    for _, session, tx in transactions:
                        tx.prepare()

                    # Attempt commit all
                    for _, session, tx in transactions:
                        tx.commit()
                    # success
                    break
                except (IntegrityError, OperationalError) as e:
                    attempt += 1
                    logger.warning('Distributed transaction transient failure (attempt %s): %s', attempt, e)
                    # rollback prepared/started txs and retry
                    for _, session, tx in transactions:
                        try:
                            tx.rollback()
                        except Exception:
                            logger.exception('Failed rollback during retry')
                    transactions.clear()
                    if attempt >= retries:
                        raise
                    time.sleep(backoff * attempt)
        except Exception:
            # Rollback all if any exception bubbles up
            for _, session, tx in transactions:
                try:
                    tx.rollback()
                except Exception:
                    logger.exception('Failed to rollback during distributed transaction')
            raise
        finally:
            # Close sessions to release connections
            for tenant_id, session in tenant_sessions_and_tenants:
                try:
                    session.close()
                except Exception:
                    pass

    def dispose(self):
        for engine in self.engines.values():
            engine.dispose()
