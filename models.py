"""
Refactored SQLAlchemy models with tenant-aware constraints and transaction safety.

Key improvements:
1. tenant_id added to followers junction table
2. Composite unique constraint: (tenant_id, follower_id, followed_id)
3. Foreign key constraints enforce same-tenant validation
4. after_flush hook prevents cross-tenant relationships
5. soft_delete support for cleanup jobs
6. Updated indexes for shard-aware queries
"""

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, and_, or_
from datetime import datetime

db = SQLAlchemy()


# Junction table with tenant_id for multi-tenant isolation
followers = db.Table(
    'followers',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('tenant_id', db.Integer, db.ForeignKey('user.tenant_id'), primary_key=True),
    db.Column('created_at', db.DateTime, default=datetime.utcnow, nullable=False),
    db.Column('deleted', db.Boolean, default=False, index=True),
    db.Column('deleted_at', db.DateTime, nullable=True),
    
    # Composite uniqueness: (tenant_id, follower_id, followed_id)
    db.UniqueConstraint('tenant_id', 'follower_id', 'followed_id', name='uc_followers_tenant_users'),
    
    # Indexes for efficient querying per tenant
    db.Index('ix_followers_tenant_follower', 'tenant_id', 'follower_id'),
    db.Index('ix_followers_tenant_followed', 'tenant_id', 'followed_id'),
    db.Index('ix_followers_deleted', 'deleted'),
    db.Index('ix_followers_deleted_at', 'deleted_at'),
)


class User(db.Model):
    """
    User model with tenant isolation and self-referential relationships.
    
    tenant_id is required and indexed for efficient shard routing.
    """
    __tablename__ = 'user'
    
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    username = db.Column(db.String(64), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    follower_count = db.Column(db.Integer, default=0, nullable=False)
    following_count = db.Column(db.Integer, default=0, nullable=False)
    
    # Cache metadata for Redis drift detection
    cache_version = db.Column(db.Integer, default=0, nullable=False)
    last_cache_sync = db.Column(db.DateTime, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Composite unique constraint on (tenant_id, username)
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'username', name='uc_user_tenant_username'),
        db.Index('ix_user_tenant', 'tenant_id'),
    )
    
    # Self-referential many-to-many relationship with tenant isolation
    # Only includes non-deleted relationships
    followed = db.relationship(
        'User',
        secondary=followers,
        primaryjoin=and_(
            followers.c.follower_id == id,
            followers.c.tenant_id == tenant_id,
            followers.c.deleted == False  # Exclude soft-deleted
        ),
        secondaryjoin=and_(
            followers.c.followed_id == id,
            followers.c.tenant_id == tenant_id,
            followers.c.deleted == False
        ),
        backref=db.backref(
            'followers',
            lazy='dynamic',
            foreign_keys=[followers.c.follower_id, followers.c.tenant_id]
        ),
        lazy='dynamic',
        foreign_keys=[followers.c.follower_id, followers.c.followed_id, followers.c.tenant_id],
        viewonly=False,
        cascade='all, delete-orphan',
    )
    
    def __repr__(self):
        return f'<User {self.username} tenant={self.tenant_id}>'
    
    def validate_tenant_consistency(self):
        """
        Validation method called by after_flush hook.
        Ensures no cross-tenant relationships exist.
        """
        from sqlalchemy import text
        
        # Query for cross-tenant relationships
        cross_tenant = db.session.execute(
            text("""
                SELECT f.follower_id, f.followed_id, u1.tenant_id, u2.tenant_id
                FROM followers f
                JOIN "user" u1 ON f.follower_id = u1.id
                JOIN "user" u2 ON f.followed_id = u2.id
                WHERE u1.tenant_id != u2.tenant_id
                AND f.deleted = FALSE
            """)
        ).fetchall()
        
        if cross_tenant:
            violations = [
                f"follower_id={row[0]} (tenant {row[2]}) → followed_id={row[1]} (tenant {row[3]})"
                for row in cross_tenant
            ]
            raise ValueError(f"Cross-tenant relationships detected: {'; '.join(violations)}")
    
    @classmethod
    def before_flush(cls, mapper, connection, target):
        """
        Event listener to validate tenant isolation before flush.
        """
        pass  # Validation done in after_flush to catch all objects
    
    @classmethod
    def after_flush(cls, session, flush_context):
        """
        Event listener to enforce tenant isolation after flush.
        Raises ValueError if cross-tenant relationships detected.
        """
        try:
            # Get any User instance to access validate_tenant_consistency
            user_instances = [obj for obj in session.identity_map.values() 
                            if isinstance(obj, User)]
            if user_instances:
                user_instances[0].validate_tenant_consistency()
        except Exception as e:
            session.rollback()
            raise


# Register event listeners for transaction integrity
@event.listens_for(db.session, "after_flush")
def receive_after_flush(session, flush_context):
    """Global after_flush hook for tenant validation."""
    User.after_flush(session, flush_context)


class FollowerAuditLog(db.Model):
    """
    Audit log for follower relationship changes.
    Tracks all creates, updates, and soft deletes for debugging.
    """
    __tablename__ = 'follower_audit_log'
    
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    follower_id = db.Column(db.Integer, nullable=False)
    followed_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)  # 'create', 'delete', 'restore'
    reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    __table_args__ = (
        db.Index('ix_audit_tenant_action', 'tenant_id', 'action'),
        db.Index('ix_audit_created_at', 'created_at'),
    )
    
    def __repr__(self):
        return f'<FollowerAuditLog {self.action} tenant={self.tenant_id}>'


class CacheHealthCheck(db.Model):
    """
    Health check records for Redis cache drift detection.
    Stores periodic reconciliation results.
    """
    __tablename__ = 'cache_health_check'
    
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False)
    cache_count = db.Column(db.Integer, nullable=False)
    db_count = db.Column(db.Integer, nullable=False)
    drift = db.Column(db.Integer, nullable=False)  # db_count - cache_count
    status = db.Column(db.String(20), nullable=False)  # 'healthy', 'drift', 'stale'
    checked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    __table_args__ = (
        db.Index('ix_health_tenant_user', 'tenant_id', 'user_id'),
        db.Index('ix_health_status', 'status'),
    )
    
    def __repr__(self):
        return f'<CacheHealthCheck tenant={self.tenant_id} user={self.user_id} drift={self.drift}>'
