"""稳定查询预算；避免依赖机器速度的脆弱断言。"""
from contextlib import contextmanager
from datetime import timedelta
from time import perf_counter

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select

from app.collectors.base import Observation, RunContext
from app.models import AlarmEvent, AlarmEventLog, Notification, Resource
from app.models.base import utcnow
from app.rules.loader import sync_default_rules
from app.services.pipeline import process_observations


@contextmanager
def query_count(engine):
    count = [0]

    def tick(*args):
        count[0] += 1

    event.listen(engine, 'before_cursor_execute', tick)
    try:
        yield count
    finally:
        event.remove(engine, 'before_cursor_execute', tick)


def test_two_hundred_resources_have_bounded_sql_budget(mem_db):
    sync_default_rules(mem_db)
    observations = [Observation('dcs_instance', f'perf-{i}', f'perf-{i}', 'instance_status',
                                {'status': 'RUNNING'}, status_raw='RUNNING') for i in range(200)]
    ctx = RunContext(client=None, account='perf', project_id='perf')
    process_observations(mem_db, 'dcs', observations, ctx, utcnow())
    with query_count(mem_db.bind) as counter:
        process_observations(mem_db, 'dcs', observations, ctx, utcnow())
    print(f'200 existing resources: {counter[0]} SQL statements; before optimization: 1403')
    assert counter[0] <= 25


def test_batched_pipeline_preserves_upgrade_repeat_recovery_and_reopen(mem_db):
    from .test_pipeline import _setup

    _setup(mem_db)
    ctx = RunContext(client=None, account='flow', project_id='flow')
    now = utcnow()

    def observe(value, seconds):
        process_observations(mem_db, 'dcs', [Observation('dcs_instance', 'flow', 'flow', 'memory',
                             {'memory_usage': value})], ctx, now + timedelta(seconds=seconds))

    for i in range(3):
        observe(85, i)
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == 'R-DCS-005'))
    original_id = alarm.id
    assert alarm.severity == 'P3'
    observe(92, 3)
    assert alarm.severity == 'P2'
    observe(99, 4)
    assert alarm.severity == 'P1'
    observe(85, 5)
    assert alarm.severity == 'P3'
    observe(78, 6)
    assert alarm.status == 'FIRING'
    observe(85, 7200)
    kinds = list(mem_db.scalars(select(Notification.kind).order_by(Notification.id)))
    assert kinds == ['FIRING', 'UPGRADE', 'UPGRADE', 'REPEAT']
    observe(70, 7201)
    assert alarm.status == 'RESOLVED'
    for i in range(3):
        observe(85, 7210 + i)
    assert alarm.id == original_id and alarm.status == 'FIRING' and alarm.reopen_count == 1
    actions = list(mem_db.scalars(select(AlarmEventLog.action).where(AlarmEventLog.alarm_id == alarm.id)))
    assert {'FIRE', 'UPGRADE', 'DOWNGRADE', 'RESOLVE', 'REOPEN'} <= set(actions)


def test_five_thousand_resource_list_uses_bounded_pagination(mem_db):
    from app.api.v1.resources import HEALTH_ORDER, router
    from app.core.db import get_db
    from app.core.deps import CurrentUser, get_current_user

    health = list(HEALTH_ORDER)
    rows = [{'service': 'ecs', 'resource_type': 'ecs_server', 'resource_id': f'bench-{i:05}',
             'name': f'bench-{i:05}', 'health': health[i % len(health)], 'critical': i % 3 == 0} for i in range(5000)]
    mem_db.execute(insert(Resource), rows)
    mem_db.commit()
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username='perf', display_name='perf', permissions=['ledger:view'])
    expected = sorted(rows, key=lambda row: (HEALTH_ORDER[row['health']], not row['critical'], row['name']))
    durations = []
    with TestClient(app) as client:
        for page in range(1, 21):
            with query_count(mem_db.bind) as count:
                start = perf_counter()
                response = client.get('/api/v1/resources', params={'page': page, 'page_size': 20, 'keyword': 'bench-'})
                durations.append((perf_counter() - start) * 1000)
            assert response.status_code == 200
            data = response.json()['data']
            assert data['total'] == 5000 and len(data['items']) == 20
            assert [item['name'] for item in data['items']] == [row['name'] for row in expected[(page - 1) * 20:page * 20]]
            assert count[0] <= 4
    print(f'5000 resource list, 20 HTTP requests: P95={sorted(durations)[18]:.2f}ms, max={max(durations):.2f}ms')
