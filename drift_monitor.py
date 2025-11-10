import logging
from datetime import datetime, timedelta
from sqlalchemy import func

logger = logging.getLogger(__name__)

def reconcile_counts(session, redis_client, tenant_id, user_id, User, Follower, cache):
    # DB count excludes deleted
    db_count = session.query(func.count()).select_from(Follower).filter_by(tenant_id=tenant_id, followed_id=user_id, deleted=False).scalar()
    cache_reconciled = cache.reconcile_user(tenant_id, user_id, db_count)
    return {'tenant_id': tenant_id, 'user_id': user_id, 'db_count': db_count, 'reconciled': cache_reconciled}


def detect_cross_tenant(session, Follower, User):
    """Detect rows where user tenant doesn't match follower tenant."""
    # Join follower table to user table twice and compare tenant ids
    q = session.query(Follower).join(User, User.id == Follower.follower_id)
    # naive: this requires referencing followed User separately
    # We'll just scan
    anomalies = []
    for row in session.query(Follower).all():
        follower_user = session.query(User).get(row.follower_id)
        followed_user = session.query(User).get(row.followed_id)
        if not follower_user or not followed_user:
            anomalies.append({'follower_id': row.follower_id, 'followed_id': row.followed_id, 'reason': 'user missing'})
            continue
        if follower_user.tenant_id != row.tenant_id or followed_user.tenant_id != row.tenant_id:
            anomalies.append({'follower_id': row.follower_id, 'followed_id': row.followed_id, 'tenant_ids': (follower_user.tenant_id, followed_user.tenant_id), 'row_tenant_id': row.tenant_id})
    return anomalies


def cleanup_soft_deleted(session, Follower, older_than_days=30):
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    rows = session.query(Follower).filter(Follower.deleted == True, Follower.deleted_at <= cutoff).all()
    for r in rows:
        session.delete(r)
    session.commit()
    return len(rows)
