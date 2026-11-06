"""Versioned monthly organization cache populated by the existing BI publisher."""
from sqlalchemy import delete, select

from .db import EmployeeOrgMonth, utcnow
from .bi_snapshot import SnapshotUnavailable
from .bi_projection import enqueue
from .bi_models import BIDirtyScope
from .bi_push_contract import RD_PREFIX


async def prepare_month(sessions, month, version, remote, shared):
    with sessions() as db:
        row = db.scalar(select(EmployeeOrgMonth).where(EmployeeOrgMonth.month == month).limit(1))
        if row and row.directory_version == version['directoryVersion'] and row.policy_version == version['policyVersion'] and row.details_ready:
            return
    key = (version['resolvedSnapshotMonth'], version['directoryVersion'], version['policyVersion'])
    if key not in shared:
        with sessions() as db:
            existing = db.scalar(select(EmployeeOrgMonth.month).where(EmployeeOrgMonth.resolved_snapshot_month == key[0],
                EmployeeOrgMonth.directory_version == key[1], EmployeeOrgMonth.policy_version == key[2], EmployeeOrgMonth.details_ready.is_(True)).limit(1))
            if existing:
                shared[key] = {r.employee_key: {'employee_name': r.employee_name, 'department_name': r.department_name,
                    'biz_group_name': r.biz_group_name, 'is_rd_system': r.is_rd_system}
                    for r in db.scalars(select(EmployeeOrgMonth).where(EmployeeOrgMonth.month == existing))}
    if key not in shared:
        items, offset, expected_total = {}, 0, None
        while True:
            data = await remote.employee('/api/internal/v1/employee-directory/monthly', params={
                'month': month, 'limit': 500, 'offset': offset, 'includeInactive': 'true'})
            if (data.get('directoryVersion') or 'none') != version['directoryVersion'] or (data.get('policyVersion') or 'none') != version['policyVersion'] or data.get('resolvedSnapshotMonth') != version['resolvedSnapshotMonth']:
                raise SnapshotUnavailable('organization_versions_changed')
            rows, total = data.get('items'), data.get('total')
            if not isinstance(rows, list) or type(total) is not int or not 0 < total <= 20000 or offset+len(rows) > total:
                raise SnapshotUnavailable('organization_page_invalid')
            if expected_total is None:
                expected_total = total
            if total != expected_total or data.get('offset', offset) != offset:
                raise SnapshotUnavailable('organization_page_inconsistent')
            for r in rows:
                employee = r.get('employeeKey') or r.get('unionId')
                if not isinstance(employee, str) or not employee or employee in items:
                    raise SnapshotUnavailable('organization_page_duplicate')
                # These are the public monthly-directory fields. Never infer/reverse organization labels.
                items[employee] = {'employee_name': str(r.get('name') or r.get('employeeName') or ''),
                    'department_name': str(r.get('dept') or r.get('departmentName') or ''),
                    'biz_group_name': str(r.get('biz') or r.get('bizGroupName') or ''), 'is_rd_system': r.get('isRdSystem') is True}
            offset += len(rows)
            if offset == total:
                break
            if not rows:
                raise SnapshotUnavailable('organization_page_incomplete')
        shared[key] = items
    with sessions.begin() as db:
        db.execute(delete(EmployeeOrgMonth).where(EmployeeOrgMonth.month == month))
        db.add_all([EmployeeOrgMonth(month=month, employee_key=k, **v, details_ready=True, directory_version=version['directoryVersion'],
                    policy_version=version['policyVersion'], resolved_snapshot_month=version['resolvedSnapshotMonth'], synced_at=utcnow())
                    for k, v in shared[key].items()])
        enqueue(db, BIDirtyScope, [RD_PREFIX + month])
