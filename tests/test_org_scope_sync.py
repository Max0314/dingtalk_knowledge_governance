import asyncio


def test_sync_monthly_org_cache_commits_and_reports_counts(monkeypatch):
    from app.db import EmployeeOrgMonth, SessionLocal, init_db
    from scripts import sync_employee_org_month_cache as sync_module

    class FakeClient:
        def __init__(self, settings):
            pass

        async def employee_directory_month_page(self, month_key, limit=500, offset=0):
            return {
                "resolvedSnapshotMonth": "2033-06",
                "directoryVersion": "directory-test",
                "policyVersion": "policy-test",
                "total": 2,
                "items": [
                    {"employeeKey": "scope-sync-1", "dept": "研发一", "isActive": True,
                     "isRdSystem": True},
                    {"employeeKey": "scope-sync-2", "dept": "其他", "isActive": True,
                     "isRdSystem": False},
                ],
            }

    init_db()
    monkeypatch.setattr(sync_module, "BiCenterClient", FakeClient)
    try:
        result = asyncio.run(sync_module.sync(["2033-06"]))
        assert result == {
            "status": "ok",
            "report_months": 1,
            "resolved_snapshots": 1,
            "cached_rows": 2,
            "rd_scope_rows": 1,
        }
        with SessionLocal() as db:
            rows = db.query(EmployeeOrgMonth).filter(EmployeeOrgMonth.month == "2033-06").all()
            assert len(rows) == 2
            assert sum(1 for row in rows if row.is_rd_system) == 1
    finally:
        with SessionLocal() as db:
            db.query(EmployeeOrgMonth).filter(EmployeeOrgMonth.month == "2033-06").delete(
                synchronize_session=False
            )
            db.commit()
