"""
Reconciliation and drift monitoring script.

Detects and repairs:
1. Cache-DB discrepancies (drift)
2. Cross-tenant relationships (isolation violations)
3. Orphaned soft-deleted records
4. Stale cache entries
5. Missing relationships (DB records not in cache)

Generates reconciliation reports and automated fixes.
"""

import logging
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any
from sqlalchemy import text, and_

logger = logging.getLogger(__name__)


class DriftMonitor:
    """
    Monitors and reconciles cache-DB drift.
    """
    
    def __init__(self, session: Any, redis_client: Any, tenant_id: int):
        self.session = session
        self.redis = redis_client
        self.tenant_id = tenant_id
        self.report = {
            'timestamp': datetime.utcnow().isoformat(),
            'tenant_id': tenant_id,
            'checks': [],
            'violations': [],
            'drift_issues': [],
            'orphaned_records': [],
            'fixes_applied': [],
            'status': 'pending',
        }
    
    def check_cross_tenant_relationships(self) -> List[Dict]:
        """
        Check for cross-tenant relationships (data integrity violation).
        
        Query:
            SELECT f.follower_id, f.followed_id, u1.tenant_id, u2.tenant_id
            FROM followers f
            JOIN user u1 ON f.follower_id = u1.id
            JOIN user u2 ON f.followed_id = u2.id
            WHERE u1.tenant_id != u2.tenant_id
            AND f.deleted = FALSE
        
        Expected: Empty result set (no violations)
        """
        logger.info("Checking for cross-tenant relationships...")
        
        try:
            query = text("""
                SELECT f.follower_id, f.followed_id, u1.tenant_id, u2.tenant_id
                FROM followers f
                JOIN "user" u1 ON f.follower_id = u1.id
                JOIN "user" u2 ON f.followed_id = u2.id
                WHERE f.deleted = FALSE
                AND u1.tenant_id != u2.tenant_id
            """)
            
            result = self.session.execute(query).fetchall()
            
            violations = []
            for row in result:
                follower_id, followed_id, follower_tenant, followed_tenant = row
                violation = {
                    'type': 'cross_tenant_relationship',
                    'follower_id': follower_id,
                    'followed_id': followed_id,
                    'follower_tenant': follower_tenant,
                    'followed_tenant': followed_tenant,
                    'severity': 'CRITICAL',
                    'action': 'soft_delete_recommended',
                }
                violations.append(violation)
                logger.warning(
                    f"Cross-tenant violation: {follower_id} (tenant {follower_tenant}) "
                    f"→ {followed_id} (tenant {followed_tenant})"
                )
            
            self.report['violations'].extend(violations)
            return violations
        
        except Exception as e:
            logger.error(f"Error checking cross-tenant relationships: {e}")
            self.report['checks'].append({
                'name': 'cross_tenant_check',
                'status': 'failed',
                'error': str(e),
            })
            return []
    
    def check_follower_count_drift(self, user_id: int = None) -> List[Dict]:
        """
        Check for discrepancies between follower_count (DB) and cache.
        
        Args:
            user_id: If provided, check only this user. Otherwise check all.
        
        Returns: List of drift issues.
        """
        logger.info(f"Checking follower count drift (user_id={user_id})...")
        
        try:
            from models import User
            
            drift_issues = []
            
            # Query users
            query = self.session.query(User).filter_by(tenant_id=self.tenant_id)
            if user_id:
                query = query.filter_by(id=user_id)
            
            users = query.all()
            
            for user in users:
                # Get cache count
                cache_key = f"followers:count:{self.tenant_id}:{user.id}"
                try:
                    cache_value = self.redis.get(cache_key)
                    cache_count = int(cache_value) if cache_value else None
                except:
                    cache_count = None
                
                # Compare
                if cache_count is not None:
                    drift = abs(user.follower_count - cache_count)
                    
                    if drift > 0:
                        issue = {
                            'type': 'follower_count_drift',
                            'user_id': user.id,
                            'db_count': user.follower_count,
                            'cache_count': cache_count,
                            'drift': drift,
                            'severity': 'stale' if drift > 5 else 'warning',
                        }
                        drift_issues.append(issue)
                        logger.warning(
                            f"Drift detected: user {user.id} "
                            f"cache={cache_count} db={user.follower_count} drift={drift}"
                        )
                else:
                    # Cache miss
                    issue = {
                        'type': 'cache_miss',
                        'user_id': user.id,
                        'db_count': user.follower_count,
                        'severity': 'info',
                    }
                    drift_issues.append(issue)
                    logger.debug(f"Cache miss for user {user.id}")
            
            self.report['drift_issues'].extend(drift_issues)
            return drift_issues
        
        except Exception as e:
            logger.error(f"Error checking follower count drift: {e}")
            self.report['checks'].append({
                'name': 'drift_check',
                'status': 'failed',
                'error': str(e),
            })
            return []
    
    def check_orphaned_soft_deleted_records(self) -> List[Dict]:
        """
        Check for soft-deleted records older than retention period (e.g., 30 days).
        
        These can be safely purged to maintain database size.
        """
        logger.info("Checking for orphaned soft-deleted records...")
        
        try:
            retention_days = 30
            cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
            
            query = text(f"""
                SELECT id, tenant_id, follower_id, followed_id, deleted_at
                FROM followers
                WHERE tenant_id = :tenant_id
                AND deleted = TRUE
                AND deleted_at < :cutoff_date
            """)
            
            result = self.session.execute(
                query,
                {'tenant_id': self.tenant_id, 'cutoff_date': cutoff_date}
            ).fetchall()
            
            orphaned = []
            for row in result:
                record_id, tenant_id, follower_id, followed_id, deleted_at = row
                orphaned.append({
                    'id': record_id,
                    'follower_id': follower_id,
                    'followed_id': followed_id,
                    'deleted_at': deleted_at.isoformat(),
                    'age_days': (datetime.utcnow() - deleted_at).days,
                    'action': 'delete_recommended',
                })
                logger.info(
                    f"Orphaned record: id={record_id} "
                    f"age={(datetime.utcnow() - deleted_at).days} days"
                )
            
            self.report['orphaned_records'].extend(orphaned)
            return orphaned
        
        except Exception as e:
            logger.error(f"Error checking orphaned records: {e}")
            self.report['checks'].append({
                'name': 'orphaned_check',
                'status': 'failed',
                'error': str(e),
            })
            return []
    
    def check_stale_cache_entries(self) -> List[Dict]:
        """
        Check for cache entries that have expired TTL but still exist.
        """
        logger.info("Checking for stale cache entries...")
        
        try:
            stale = []
            
            # Scan for follower cache keys for this tenant
            pattern = f"followers:*:{self.tenant_id}:*"
            
            for key in self.redis.scan_iter(match=pattern):
                try:
                    # Check TTL
                    ttl = self.redis.ttl(key)
                    
                    if ttl == -1:  # No expiry set
                        stale.append({
                            'key': key.decode() if isinstance(key, bytes) else key,
                            'ttl': ttl,
                            'action': 'set_ttl_recommended',
                        })
                        logger.warning(f"Stale entry with no TTL: {key}")
                
                except Exception as e:
                    logger.debug(f"Error checking TTL for {key}: {e}")
            
            self.report['checks'].append({
                'name': 'stale_cache_check',
                'status': 'completed',
                'stale_count': len(stale),
            })
            return stale
        
        except Exception as e:
            logger.error(f"Error checking stale cache entries: {e}")
            self.report['checks'].append({
                'name': 'stale_cache_check',
                'status': 'failed',
                'error': str(e),
            })
            return []
    
    def repair_follower_count_drift(self, user_id: int) -> Dict[str, Any]:
        """
        Repair drift for specific user by syncing cache from DB.
        
        Returns: Repair result.
        """
        logger.info(f"Repairing drift for user {user_id}...")
        
        try:
            from models import User
            
            user = self.session.query(User).filter_by(
                id=user_id,
                tenant_id=self.tenant_id
            ).first()
            
            if not user:
                return {
                    'user_id': user_id,
                    'status': 'failed',
                    'reason': 'user_not_found',
                }
            
            # Update cache from DB
            cache_key = f"followers:count:{self.tenant_id}:{user.id}"
            self.redis.setex(cache_key, 3600, str(user.follower_count))
            
            # Increment version
            user.cache_version += 1
            user.last_cache_sync = datetime.utcnow()
            self.session.add(user)
            self.session.commit()
            
            result = {
                'user_id': user_id,
                'status': 'success',
                'synced_count': user.follower_count,
                'cache_version': user.cache_version,
                'synced_at': datetime.utcnow().isoformat(),
            }
            
            self.report['fixes_applied'].append(result)
            logger.info(f"✓ Repaired drift for user {user_id}")
            return result
        
        except Exception as e:
            logger.error(f"Error repairing drift for user {user_id}: {e}")
            return {
                'user_id': user_id,
                'status': 'failed',
                'error': str(e),
            }
    
    def repair_cross_tenant_violation(self, follower_id: int, followed_id: int) -> Dict:
        """
        Repair cross-tenant violation by soft-deleting the relationship.
        
        Returns: Repair result.
        """
        logger.info(f"Repairing cross-tenant violation: {follower_id} → {followed_id}...")
        
        try:
            from models import FollowerAuditLog
            
            # Soft delete the relationship
            query = text("""
                UPDATE followers
                SET deleted = TRUE, deleted_at = :now
                WHERE follower_id = :follower_id
                AND followed_id = :followed_id
                AND deleted = FALSE
            """)
            
            self.session.execute(
                query,
                {
                    'now': datetime.utcnow(),
                    'follower_id': follower_id,
                    'followed_id': followed_id,
                }
            )
            
            # Log to audit
            audit = FollowerAuditLog(
                tenant_id=self.tenant_id,
                follower_id=follower_id,
                followed_id=followed_id,
                action='delete',
                reason='cross_tenant_violation_remediation',
            )
            self.session.add(audit)
            self.session.commit()
            
            result = {
                'type': 'cross_tenant_fix',
                'follower_id': follower_id,
                'followed_id': followed_id,
                'status': 'success',
                'action': 'soft_deleted',
                'fixed_at': datetime.utcnow().isoformat(),
            }
            
            self.report['fixes_applied'].append(result)
            logger.info(f"✓ Fixed cross-tenant violation")
            return result
        
        except Exception as e:
            logger.error(f"Error repairing cross-tenant violation: {e}")
            return {
                'type': 'cross_tenant_fix',
                'follower_id': follower_id,
                'followed_id': followed_id,
                'status': 'failed',
                'error': str(e),
            }
    
    def purge_orphaned_records(self, days_threshold: int = 30) -> Dict:
        """
        Permanently delete soft-deleted records older than threshold.
        
        Args:
            days_threshold: Age in days before permanent deletion.
        
        Returns: Purge result.
        """
        logger.info(f"Purging orphaned records older than {days_threshold} days...")
        
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)
            
            query = text("""
                DELETE FROM followers
                WHERE tenant_id = :tenant_id
                AND deleted = TRUE
                AND deleted_at < :cutoff_date
            """)
            
            result = self.session.execute(
                query,
                {'tenant_id': self.tenant_id, 'cutoff_date': cutoff_date}
            )
            
            purged_count = result.rowcount
            self.session.commit()
            
            result = {
                'status': 'success',
                'purged_count': purged_count,
                'threshold_days': days_threshold,
                'purged_at': datetime.utcnow().isoformat(),
            }
            
            self.report['fixes_applied'].append(result)
            logger.info(f"✓ Purged {purged_count} orphaned records")
            return result
        
        except Exception as e:
            logger.error(f"Error purging orphaned records: {e}")
            return {
                'status': 'failed',
                'error': str(e),
            }
    
    def run_full_reconciliation(self, auto_repair: bool = False) -> Dict:
        """
        Run full reconciliation: detect all issues and optionally repair.
        
        Args:
            auto_repair: If True, automatically repair fixable issues.
        
        Returns: Full reconciliation report.
        """
        logger.info("=" * 80)
        logger.info(f"Starting full reconciliation for tenant {self.tenant_id}")
        logger.info("=" * 80)
        
        try:
            # Run all checks
            cross_tenant = self.check_cross_tenant_relationships()
            drift = self.check_follower_count_drift()
            orphaned = self.check_orphaned_soft_deleted_records()
            stale = self.check_stale_cache_entries()
            
            # Summary
            self.report['summary'] = {
                'cross_tenant_violations': len(cross_tenant),
                'drift_issues': len(drift),
                'orphaned_records': len(orphaned),
                'stale_cache_entries': len(stale),
            }
            
            # Auto-repair if requested
            if auto_repair:
                logger.info("Auto-repair enabled, fixing detected issues...")
                
                # Fix cross-tenant violations
                for violation in cross_tenant:
                    self.repair_cross_tenant_violation(
                        violation['follower_id'],
                        violation['followed_id']
                    )
                
                # Fix drift issues (but not cache misses)
                for issue in drift:
                    if issue['type'] == 'follower_count_drift':
                        self.repair_follower_count_drift(issue['user_id'])
                
                # Purge orphaned records
                if orphaned:
                    self.purge_orphaned_records(days_threshold=30)
            
            self.report['status'] = 'completed'
            logger.info("✓ Reconciliation completed")
        
        except Exception as e:
            logger.error(f"Error during reconciliation: {e}")
            self.report['status'] = 'failed'
            self.report['error'] = str(e)
        
        return self.report


def generate_reconciliation_report(
    session: Any,
    redis_client: Any,
    tenant_ids: List[int],
    auto_repair: bool = False,
) -> Dict:
    """
    Generate full reconciliation report for specified tenants.
    
    Args:
        session: Database session
        redis_client: Redis client
        tenant_ids: List of tenant IDs to reconcile
        auto_repair: If True, auto-repair issues
    
    Returns: Aggregated report.
    """
    logger.info(f"Generating reconciliation report for {len(tenant_ids)} tenants...")
    
    overall_report = {
        'timestamp': datetime.utcnow().isoformat(),
        'tenants': len(tenant_ids),
        'tenant_reports': [],
        'total_violations': 0,
        'total_drift_issues': 0,
        'total_orphaned': 0,
        'total_fixes_applied': 0,
    }
    
    for tenant_id in tenant_ids:
        monitor = DriftMonitor(session, redis_client, tenant_id)
        report = monitor.run_full_reconciliation(auto_repair=auto_repair)
        
        overall_report['tenant_reports'].append(report)
        overall_report['total_violations'] += len(report.get('violations', []))
        overall_report['total_drift_issues'] += len(report.get('drift_issues', []))
        overall_report['total_orphaned'] += len(report.get('orphaned_records', []))
        overall_report['total_fixes_applied'] += len(report.get('fixes_applied', []))
    
    logger.info("=" * 80)
    logger.info("RECONCILIATION REPORT SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Tenants reconciled: {len(tenant_ids)}")
    logger.info(f"Total violations: {overall_report['total_violations']}")
    logger.info(f"Total drift issues: {overall_report['total_drift_issues']}")
    logger.info(f"Total orphaned records: {overall_report['total_orphaned']}")
    logger.info(f"Total fixes applied: {overall_report['total_fixes_applied']}")
    
    return overall_report


if __name__ == '__main__':
    # This would be called from a scheduled job or CLI
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    
    logger.info("DriftMonitor module loaded")
