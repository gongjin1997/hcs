"""同名云资源必须按项目隔离，不得覆盖或搬迁其他租户数据。"""
import io

from openpyxl import Workbook
from sqlalchemy import select

from app.collectors.base import Observation, RunContext
from app.ledger import importer
from app.ledger.fetchers import LedgerRecord
from app.ledger.sync import apply_snapshot
from app.models import AlarmEvent, HcsAccount, Resource, User
from app.models.base import utcnow
from app.rules.loader import sync_default_rules
from app.services.pipeline import process_observations, upsert_resource
from app.services.settings_service import LedgerDict


def test_same_job_id_is_isolated_across_tenants_and_projects(mem_db):
    for turn in range(3):
        for tenant, project, status in [(101, 1001, 'RUNNING'), (202, 2002, 'STOPPED'), (101, 1002, 'EXCEPTION')]:
            ctx = RunContext(client=None, account=str(tenant), project_id=str(project),
                             tenant_id=tenant, project_ref=project)
            upsert_resource(mem_db, 'dataarts', [Observation('dgc_realtime_job', 'shared', f'job-{project}',
                            'job_status', {}, status_raw=status, metrics={'turn': turn})], utcnow(), ctx)
            mem_db.flush()
    rows = mem_db.scalars(select(Resource).order_by(Resource.project_ref)).all()
    assert [(r.tenant_id, r.project_ref, r.status_raw) for r in rows] == [
        (101, 1001, 'RUNNING'), (101, 1002, 'EXCEPTION'), (202, 2002, 'STOPPED')]
    assert all(r.metrics['turn'] == 2 for r in rows)


def test_ledger_does_not_move_other_projects_or_delete_their_resources(mem_db):
    for tenant, project in [(101, 1001), (202, 2002)]:
        apply_snapshot(mem_db, 'vpc', [LedgerRecord('shared', f'vpc-{project}')],
                       tenant_id=tenant, project_ref=project)
    rows = mem_db.scalars(select(Resource).order_by(Resource.project_ref)).all()
    assert len(rows) == 2
    rows[1].owner = 'tenant-b-owner'
    apply_snapshot(mem_db, 'vpc', [], tenant_id=101, project_ref=1001)
    mem_db.flush()
    assert rows[0].lifecycle == 'DELETED'
    assert rows[1].tenant_id == 202 and rows[1].project_ref == 2002
    assert rows[1].lifecycle == 'ACTIVE' and rows[1].owner == 'tenant-b-owner'


def test_legacy_record_owned_by_another_tenant_is_not_adopted(mem_db):
    legacy = Resource(service='vpc', resource_type='vpc', resource_id='shared', name='legacy',
                      tenant_id=202, project_ref=None, owner='original')
    mem_db.add(legacy)
    mem_db.flush()
    apply_snapshot(mem_db, 'vpc', [LedgerRecord('shared', 'new')], tenant_id=101, project_ref=1001)
    mem_db.flush()
    assert legacy.project_ref is None and legacy.tenant_id == 202 and legacy.owner == 'original'
    assert len(mem_db.scalars(select(Resource)).all()) == 2


def test_alarms_link_to_the_correct_project_resource(mem_db):
    sync_default_rules(mem_db)
    for tenant, project in [(101, 1001), (202, 2002)]:
        ctx = RunContext(client=None, account=str(tenant), project_id=str(project), tenant_id=tenant, project_ref=project)
        obs = [Observation('dcs_instance', 'same', f'redis-{tenant}', 'instance_status',
                           {'status': 'ERROR'}, status_raw='ERROR')]
        process_observations(mem_db, 'dcs', obs, ctx, utcnow())
    alarms = mem_db.scalars(select(AlarmEvent).where(AlarmEvent.rule_id == 'R-DCS-001')).all()
    assert len(alarms) == 2
    for alarm in alarms:
        resource = mem_db.get(Resource, alarm.resource_ref)
        assert resource.tenant_id == alarm.tenant_id
        assert resource.project_ref == (1001 if alarm.tenant_id == 101 else 2002)


def test_import_uses_tenant_column_and_rejects_ambiguous_resource_ids(mem_db):
    owner = User(username='new-owner', display_name='导入负责人', password_hash='!', login_enabled=False, tenant_ids=[202])
    mem_db.add(owner)
    for tenant, project in [(101, 1001), (202, 2002)]:
        mem_db.add(HcsAccount(id=tenant, code=f't{tenant}', name=f'Tenant {tenant}',
                             iam_endpoint='https://example.com', domain_name='test'))
        mem_db.add(Resource(service='vpc', resource_type='vpc', resource_id='same', name='vpc',
                            tenant_id=tenant, project_ref=project))
    mem_db.commit()

    def xlsx(tenant):
        wb = Workbook()
        wb.active.append(['租户', '资源类型', '资源 ID', '负责人'])
        wb.active.append([tenant, 'vpc', 'same', 'new-owner'])
        stream = io.BytesIO()
        wb.save(stream)
        wb.close()
        return stream.getvalue()

    ambiguous, todo = importer.parse(mem_db, xlsx(None), None, LedgerDict())
    assert len(ambiguous.errors) == 1 and not todo
    selected, todo = importer.parse(mem_db, xlsx('t202'), None, LedgerDict())
    assert not selected.errors and len(todo) == 1 and todo[0][0].tenant_id == 202
    assert todo[0][1] == {'owner': owner.display_name, 'owner_user_id': owner.id}
    assert todo[0][0].owner is None and todo[0][0].owner_user_id is None
    forbidden, todo = importer.parse(mem_db, xlsx('t202'), [101], LedgerDict())
    assert forbidden.errors and not todo


def test_missing_workload_cleanup_does_not_touch_another_project(world, mem_db, mock_http):
    from .test_pod_events import _collect

    assert _collect(mem_db, 'tenant-a', mock_http).status == 'SUCCESS'
    first = mem_db.scalar(select(Resource).where(Resource.resource_type == 'cce_workload'))
    cluster = first.resource_id.split('/')[0]
    outsider = Resource(service='cce', resource_type='cce_workload', resource_id=f'{cluster}/other/Deployment/other',
                        name='other project', project_ref=999999, tenant_id=world['tenant-b'].id)
    mem_db.add(outsider)
    mem_db.commit()
    assert _collect(mem_db, 'tenant-a', mock_http).status == 'SUCCESS'
    mem_db.refresh(outsider)
    assert outsider.lifecycle == 'ACTIVE'
