"""Production page reads over the same node deltas used by BI, without source scans."""
from collections import Counter, defaultdict

from fastapi import HTTPException
from sqlalchemy import func, select, tuple_

from .bi_models import BIIdentityMonth, BIInventory, BIProjectionState, BIUploadDay
from .config import get_settings
from .db import EmployeeOrgMonth, Workspace


def enabled():
    s = get_settings()
    return s.metadata_only and not s.demo_mode


def ready(db):
    from .metrics import primary_snapshot_id
    state = db.get(BIProjectionState, 1)
    if not state or not state.seed_complete or state.baseline_id != primary_snapshot_id(db):
        raise HTTPException(503, "增量统计尚未就绪，请稍后重试。")
    return state


def spaces(db):
    from .service import workspace_name_is_ignored
    s = get_settings()
    return {w.workspace_id: w for w in db.scalars(select(Workspace).where(Workspace.is_active.is_(True)))
            if not workspace_name_is_ignored(s, w.name)}


def collect(db):
    from .metrics import PERSON_DAY_BULK_MIN, robot_ids
    ready(db)
    ids = spaces(db)
    totals = dict(db.execute(select(BIInventory.workspace_id, BIInventory.file_count).where(BIInventory.workspace_id.in_(ids))).all())
    typed = [(r.workspace_id, r.identity_kind, r.source_key, r.day, r.file_count)
             for r in db.scalars(select(BIUploadDay).where(BIUploadDay.workspace_id.in_(ids)))]
    joint, persons, space_months = Counter(), Counter(), defaultdict(Counter)
    for ws, kind, who, day, count in typed:
        if day:
            joint[ws, who, day] += count
            persons[who, day] += count
            space_months[ws][day[:7]] += count
    monthly, bulk, robots = Counter(), Counter(), robot_ids()
    for (who, day), count in persons.items():
        monthly[day[:7]] += count
        if who in robots or bool(who) and count >= PERSON_DAY_BULK_MIN:
            bulk[day[:7]] += count
    return {"total_files": sum(totals.values()), "monthly": dict(monthly), "bulk_by_month": dict(bulk),
            "creator_day": dict(persons), "workspace_creator_day": dict(joint), "robots": robots,
            "space_totals": totals, "space_months": {k: dict(v) for k, v in space_months.items()}, "typed_rows": typed}


def counts(db):
    ready(db)
    return dict(db.execute(select(BIInventory.workspace_id, BIInventory.file_count)).all())


def person_lookup(db, rows=None):
    from .metrics import robot_ids
    if rows is None:
        from .metrics import collected
        rows = collected(db)['typed_rows']
    wanted = list({(day[:7], kind, source) for _, kind, source, day, _ in rows if day})
    identities, orgs = {}, {}
    for offset in range(0, len(wanted), 500):
        for r in db.execute(select(BIIdentityMonth.month, BIIdentityMonth.identity_kind, BIIdentityMonth.source_key,
                BIIdentityMonth.employee_key, BIIdentityMonth.resolve_status, BIIdentityMonth.include_official,
                BIIdentityMonth.directory_version, BIIdentityMonth.policy_version).where(
                tuple_(BIIdentityMonth.month, BIIdentityMonth.identity_kind, BIIdentityMonth.source_key).in_(wanted[offset:offset+500]))):
            identities[r.month, r.identity_kind, r.source_key] = r
    keys = list({(r.month, r.employee_key) for r in identities.values() if r.employee_key})
    for offset in range(0, len(keys), 500):
        for r in db.execute(select(EmployeeOrgMonth.month, EmployeeOrgMonth.employee_key, EmployeeOrgMonth.employee_name,
                EmployeeOrgMonth.department_name, EmployeeOrgMonth.biz_group_name, EmployeeOrgMonth.directory_version,
                EmployeeOrgMonth.policy_version, EmployeeOrgMonth.resolved_snapshot_month).where(tuple_(EmployeeOrgMonth.month, EmployeeOrgMonth.employee_key).in_(keys[offset:offset+500]))):
            orgs[r.month, r.employee_key] = r
    robots = robot_ids()
    def resolve(month, kind, source):
        identity = identities.get((month, kind, source))
        key = identity.employee_key if identity else ''
        org = orgs.get((month, key))
        robot = source in robots or bool(key) and key in robots
        official = bool(identity and identity.resolve_status == 'matched' and identity.include_official and key and org and not robot
                        and identity.directory_version == org.directory_version and identity.policy_version == org.policy_version)
        return {"user_id": key if official else f"unresolved:{kind}:{source}",
                "name": (org.employee_name or '未命名员工') if official else ('数字员工' if robot else '未映射'),
                "department_name": org.department_name if official else ('系统/机器人' if robot else '未映射'),
                "biz_group_name": org.biz_group_name if official else ('系统/机器人' if robot else '未映射'),
                "matched": official, "include_official": official, "is_robot": robot,
                "organization_month": month, "resolved_organization_month": org.resolved_snapshot_month if official else ''}
    return resolve


def person_rows(db, month='', user_id=''):
    from .metrics import collected
    rows = [r for r in collected(db)['typed_rows'] if r[3] and (not month or r[3].startswith(month))]
    if user_id:
        if user_id.startswith('unresolved:'):
            _, kind, source = user_id.split(':', 2)
            rows = [r for r in rows if r[1:3] == (kind, source)]
        else:
            keys = set(db.execute(select(BIIdentityMonth.month, BIIdentityMonth.identity_kind, BIIdentityMonth.source_key)
                                 .where(BIIdentityMonth.employee_key == user_id)).all())
            rows = [r for r in rows if (r[3][:7], r[1], r[2]) in keys]
    resolve = person_lookup(db, rows)
    for ws, kind, source, day, count in rows:
        if day:
            yield ws, day[:7], day, int(count), resolve(day[:7], kind, source)


def uploader_months(db):
    from .metrics import collected
    d = collected(db)
    monthly = sorted(d['monthly'].items())
    years = Counter()
    for m, n in monthly:
        years[m[:4]] += n
    return {"snapshot_id": ready(db).baseline_id, "months": [{"month": m, "total": n} for m, n in monthly],
            "yearly": dict(years), "total_files": d['total_files'],
            "uploader_count": len({p['user_id'] for _, _, _, _, p in person_rows(db) if p['matched']}),
            "workspace_count": len(d['space_totals'])}


def org_rollup(db, year='', month=''):
    period, departments = month or year, {}
    fallbacks = set()
    for ws, m, day, n, p in person_rows(db):
        if (not period or m.startswith(period)) and p['resolved_organization_month'] and p['resolved_organization_month'] != m:
            fallbacks.add((m, p['resolved_organization_month']))
        dept = departments.setdefault(p['department_name'], {"department_name": p['department_name'], 'stock': 0, 'delta': 0,
                'is_robot': p['is_robot'], 'people': set(), 'groups': {}})
        group = dept['groups'].setdefault(p['biz_group_name'], {"biz_group_name": p['biz_group_name'], 'stock': 0, 'delta': 0, 'people': {}})
        person = group['people'].setdefault(p['user_id'], {**p, 'stock': 0, 'delta': 0})
        for bucket in (dept, group, person):
            bucket['stock'] += n
            bucket['delta'] += n if not period or m.startswith(period) else 0
        dept['people'].add(p['user_id'])
    items = []
    for d in departments.values():
        d['uploaders'] = len(d.pop('people'))
        for g in d['groups'].values():
            g['uploaders'] = len(g['people'])
            g['people'] = sorted(g['people'].values(), key=lambda p: (-p['delta'], -p['stock']))
        d['groups'] = sorted(d['groups'].values(), key=lambda g: (-g['delta'], -g['stock']))
        items.append(d)
    return {'snapshot_id': ready(db).baseline_id, 'year': year, 'month': month,
            'items': sorted(items, key=lambda d: (-d['delta'], -d['stock'])),
            'organization_basis': 'bi_center_fact_month_v1',
            'organization_fallbacks': [{'month': m, 'resolved_month': r} for m, r in sorted(fallbacks)]}


def uploader_breakdown(db, user_id, year='', month=''):
    months, days, workspaces = Counter(), Counter(), Counter()
    names = spaces(db)
    display = None
    display_month = ''
    for ws, m, day, n, p in person_rows(db, user_id=user_id):
        if p['user_id'] != user_id:
            continue
        if (not (month or year) or m.startswith(month or year)) and m >= display_month:
            display, display_month = p, m
        months[m] += n
        if not (month or year) or m.startswith(month or year):
            workspaces[names[ws].name] += n
        if month and m == month:
            days[day] += n
    return {**(display or {'user_id': user_id, 'name': '未映射', 'matched': False, 'department_name': '未映射', 'biz_group_name': '未映射'}),
            'year': year, 'month': month, 'organization_month': display_month, 'snapshot_id': ready(db).baseline_id,
            'months': [{'month': m, 'count': n} for m, n in sorted(months.items())],
            'days': [{'day': d, 'count': n} for d, n in sorted(days.items())],
            'workspaces': [{'name': ws, 'files': n} for ws, n in workspaces.most_common(30)],
            'period_total': sum(workspaces.values()), 'all_total': sum(months.values())}


def uploaders(db, month='', exclude_unmatched=True, limit=50, department=''):
    people, totals = {}, Counter()
    for ws, m, day, n, p in person_rows(db, month=month):
        totals['total_files'] += n
        totals['robot_files' if p['is_robot'] else 'human_files' if p['matched'] else 'unmatched_files'] += n
        if exclude_unmatched and not p['matched'] or department and p['department_name'] != department:
            continue
        row = people.setdefault(p['user_id'], {**p, 'files': 0, 'spaces': set()})
        row['files'] += n
        row['spaces'].add(ws)
    for row in people.values():
        row['workspaces'] = len(row.pop('spaces'))
    return {'snapshot_id': ready(db).baseline_id, 'month': month, 'exclude_unmatched': exclude_unmatched,
            'items': sorted(people.values(), key=lambda p: -p['files'])[:limit], 'uploader_count': len(people),
            **{k: totals[k] for k in ['total_files', 'human_files', 'robot_files', 'unmatched_files']},
            'note': '正式人员按 bi_center 对应月份身份与组织归属；未映射数据单独展示。'}


def uploader_detail(db, user_id):
    result = uploader_breakdown(db, user_id)
    return {**result, 'top_workspaces': result['workspaces'][:10]}


def department_rollup(db, month=''):
    d = org_rollup(db, month=month)
    return {'month': month, 'items': [{'department_name': r['department_name'], 'files': r['delta'], 'uploaders': r['uploaders']} for r in d['items']]}


def increments_tree(db, year='', month='', department='', biz_group='', person='', scope=''):
    import json
    from .metrics import collected, PERSON_DAY_BULK_MIN
    from .bi_models import BIFileFact
    from .db import ReviewInstance
    data, names = collected(db), spaces(db)
    selected_rows = [r for r in data['typed_rows'] if r[3] and (not month or r[3].startswith(month)) and (not year or r[3].startswith(year))]
    resolve = person_lookup(db, selected_rows) if any((department, biz_group, person)) else None
    rd = defaultdict(set)
    if scope == 'rd_system':
        for m, dept in db.execute(select(EmployeeOrgMonth.month, EmployeeOrgMonth.department_name).where(EmployeeOrgMonth.is_rd_system.is_(True)).distinct()):
            rd[m].add(dept)
    missing = set()
    def selected(ws, kind, source, day):
        if not day or month and not day.startswith(month) or year and not day.startswith(year) or ws not in names:
            return False
        if scope == 'rd_system':
            if day[:7] not in rd:
                missing.add(day[:7])
                return False
            if names[ws].owner_department_name not in rd[day[:7]]:
                return False
        if resolve:
            p = resolve(day[:7], kind, source)
            if department and department not in p['department_name'] or biz_group and biz_group not in p['biz_group_name'] or person and person not in p['name'] and person not in (source, p['user_id']):
                return False
        return True
    buckets, scores = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0.0, 0])
    length = 10 if month else 7 if year else 4
    for ws, kind, source, day, count in data['typed_rows']:
        if selected(ws, kind, source, day):
            b = buckets[day[:length]]
            b[0] += count
            if source in data['robots'] or bool(source) and data['creator_day'].get((source, day), 0) >= PERSON_DAY_BULK_MIN:
                b[1] += count
    # Only reviewed node primary keys, fetched in bounded batches. No document
    # inventory, attachment, body, or full file-fact scan is needed for daily scores.
    cursor = ''
    while True:
        ids = list(db.scalars(select(ReviewInstance.node_id).where(ReviewInstance.review_scope == 'full_content', ReviewInstance.node_id > cursor)
                              .distinct().order_by(ReviewInstance.node_id).limit(500)))
        if not ids:
            break
        for payload in db.scalars(select(BIFileFact.projection_json).where(BIFileFact.node_id.in_(ids))):
            p = json.loads(payload)
            q = p.get('quality') if p else None
            if q and selected(p['workspaceId'], p['kind'], p['creator'], p['day']):
                value = scores[p['day'][:length]]
                value[0] += float(q['scoreSum'])
                value[1] += q['scoredDocumentCount']
        cursor = ids[-1]
    if missing:
        raise HTTPException(409, {'code': 'organization_scope_cache_missing', 'message': '对应月份的研发组织缓存尚未就绪。', 'missing_months': sorted(missing)})
    return {'level': 'day' if month else 'month' if year else 'year', 'year': year, 'month': month,
            'filter_label': ' / '.join(x for x in [department, biz_group, person] if x), 'scope': scope,
            'scope_label': '研发体系七部门（知识库归属口径）' if scope == 'rd_system' else '全部部门',
            'person_day_bulk_min': PERSON_DAY_BULK_MIN,
            'rows': [{'key': k, 'total': n, 'bulk': b, 'routine': n-b,
                      'average_ai_score': round(scores[k][0]/scores[k][1], 1) if scores[k][1] else None,
                      'scored_documents': scores[k][1]} for k, (n, b) in sorted(buckets.items(), reverse=not (year or month))]}
