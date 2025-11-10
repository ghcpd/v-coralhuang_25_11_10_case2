from sqlalchemy import (Column, Integer, String, Table, ForeignKey, Boolean,
                        UniqueConstraint, Index, event, DateTime)
from sqlalchemy.orm import relationship, backref, declarative_base, Session
import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

Base = declarative_base()

# New followers table includes tenant_id and deleted flag.
followers = Table(
    'followers', Base.metadata,
    Column('id', Integer, primary_key=True),
    Column('tenant_id', Integer, index=True, nullable=False),
    Column('follower_id', Integer, ForeignKey('users.id'), nullable=False),
    Column('followed_id', Integer, ForeignKey('users.id'), nullable=False),
    Column('deleted', Boolean, default=False, nullable=False),
    Column('deleted_at', DateTime, nullable=True),
    UniqueConstraint('tenant_id', 'follower_id', 'followed_id', name='uq_followers_tenant_follower_followed')
)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=False)
    username = Column(String(64), unique=True)

    followed = relationship(
        'User',
        secondary=followers,
        primaryjoin=(followers.c.follower_id == id) & (followers.c.deleted == False),
        secondaryjoin=(followers.c.followed_id == id) & (followers.c.deleted == False),
        backref=backref('followers', lazy='dynamic'),
        lazy='dynamic'
    )


# Validation hook: ensure follower and followed belong to same tenant
@event.listens_for(Session, 'after_flush')
def enforce_same_tenant(session, flush_context):
    # Validate insertions into followers table
    for obj in session.new:
        if hasattr(obj, '__tablename__') and obj.__tablename__ == 'followers':
            # Not expected for the table-based mapping; skip
            continue
    # Also look for insertions into association proxy or plain insert statements
    # We'll inspect Session._new objects that are simple tuples (sa.sql.expression.Insert)
    # Simpler approach: query session.info for queued 'follow_ops' (we add this in code that creates follows)
    ops = session.info.get('follow_ops', [])
    for op in ops:
        follower = op.get('follower')
        followed = op.get('followed')
        tenant = op.get('tenant')
        if follower.tenant_id != followed.tenant_id or follower.tenant_id != tenant:
            raise Exception('Cross-tenant relationship detected: follower=%s tenant=%s followed=%s tenant=%s' % (
                follower.id, follower.tenant_id, followed.id, followed.tenant_id))


# helper function to create a follow row while enforcing tenant and soft-delete
from sqlalchemy import insert

def create_follow(session, follower: User, followed: User):
    # Compose tenant id from follower
    tenant = follower.tenant_id
    if follower.tenant_id != followed.tenant_id:
        raise ValueError('Cross-tenant follow is not allowed')
    # Queue for hook validation (enforced in after_flush)
    session.info.setdefault('follow_ops', []).append({'follower': follower, 'followed': followed, 'tenant': tenant})

    # write to the followers table with tenant
    stmt = insert(followers).values(tenant_id=tenant, follower_id=follower.id, followed_id=followed.id, deleted=False)
    session.execute(stmt)


def soft_delete_follow(session, follower: User, followed: User):
    session.execute(
        followers.update().where(
            followers.c.tenant_id == follower.tenant_id,
        ).where(
            followers.c.follower_id == follower.id,
        ).where(
            followers.c.followed_id == followed.id,
        ).values(deleted=True, deleted_at=datetime.datetime.utcnow())
    )
