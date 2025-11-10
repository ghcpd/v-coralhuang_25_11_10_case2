from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Table, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship, backref, validates
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import event
from sqlalchemy.orm import Session

Base = declarative_base()

# softer approach: association as model for additional fields
class Follower(Base):
    __tablename__ = 'followers'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=False)
    follower_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    followed_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('tenant_id', 'follower_id', 'followed_id', name='uq_tenant_follower_followed'),
    )

    def soft_delete(self):
        self.deleted = True
        self.deleted_at = datetime.utcnow()


class User(Base):
    __tablename__ = 'user'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=False)
    username = Column(String(64), unique=True)

    # relationships
    following = relationship(
        'User',
        secondary='followers',
        primaryjoin='and_(User.id==Follower.follower_id, Follower.deleted==False, User.tenant_id==Follower.tenant_id)',
        secondaryjoin='and_(User.id==Follower.followed_id, Follower.deleted==False, User.tenant_id==Follower.tenant_id)',
        backref=backref('followers', lazy='dynamic'),
        lazy='dynamic'
    )


# after_flush to ensure same-tenant follow
@event.listens_for(Follower, 'before_insert')
def ensure_same_tenant(mapper, connection, target):
    # if follower and followed loaded, ensure tenant ids match
    if target.tenant_id is None:
        raise ValueError('tenant_id required on follower relationship')


# Hooks for validation for relationships created via user.following association
# For robust validation, after_flush we will scan new objects
@event.listens_for(Session, 'after_flush')
def validate_followers(session, flush_context):
    # look through new/dirty Follower instances
    for instance in session.new:
        if isinstance(instance, Follower):
            # Ensure same tenant for follower and followed -- verify tenant ids match
            if instance.tenant_id is None:
                raise Exception('Follower.tenant_id cannot be None')
            # Check follower user tenant matches
            follower_user = session.query(User).get(instance.follower_id)
            followed_user = session.query(User).get(instance.followed_id)
            if not follower_user or not followed_user:
                raise Exception('User(s) referenced by Follower not found')
            if follower_user.tenant_id != instance.tenant_id or followed_user.tenant_id != instance.tenant_id:
                raise Exception('Cross-tenant following not allowed')
    # Prevent inserted duplicate if uniqueness mismatch would otherwise be missed; rely on uniq constraint


# helper to create a follower link
def add_follow(session, tenant_id, follower_id, followed_id):
    if follower_id == followed_id:
        raise ValueError('User cannot follow self')
    # Check users exist
    f = session.query(User).get(follower_id)
    t = session.query(User).get(followed_id)
    if not f or not t:
        raise ValueError('Follower or followed not found')
    if f.tenant_id != tenant_id or t.tenant_id != tenant_id:
        raise ValueError('Cross-tenant follow is not allowed')
    existing = session.query(Follower).filter_by(tenant_id=tenant_id, follower_id=follower_id, followed_id=followed_id, deleted=False).first()
    if existing:
        return existing
    rel = Follower(tenant_id=tenant_id, follower_id=follower_id, followed_id=followed_id)
    session.add(rel)
    session.flush()  # ensure validation
    return rel


def remove_follow(session, tenant_id, follower_id, followed_id, soft=True):
    rel = session.query(Follower).filter_by(tenant_id=tenant_id, follower_id=follower_id, followed_id=followed_id, deleted=False).first()
    if not rel:
        return None
    if soft:
        rel.soft_delete()
        session.add(rel)
    else:
        session.delete(rel)
    session.flush()
    return rel
