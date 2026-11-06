"""生产方言专项：仅在隔离的 PostgreSQL 测试 schema 内执行。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.models import AlarmEvent, Notification, Resource, User

pytestmark = pytest.mark.pg


def alarm(fp, **kwargs):
    return AlarmEvent(fingerprint=fp, rule_id='pg-test', resource_ref=1, service='ecs', resource_type='ecs_server',
                      resource_name='测试资源', severity='P2', status='FIRING', title='告警', check_item='status', **kwargs)


def test_pg_active_alarm_unique_index(mem_db):
    first = alarm('same-fingerprint')
    mem_db.add(first)
    mem_db.commit()
    with pytest.raises(IntegrityError), mem_db.begin_nested():
        mem_db.add(alarm('same-fingerprint'))
        mem_db.flush()
    first.status = 'RESOLVED'
    mem_db.commit()
    mem_db.add(alarm('same-fingerprint'))
    mem_db.commit()


def test_pg_outbox_skip_locked_does_not_claim_same_row(mem_db):
    mem_db.add_all([Notification(kind='FIRING', recipients=[]), Notification(kind='FIRING', recipients=[])])
    mem_db.commit()
    query = select(Notification).where(Notification.status == 'PENDING').order_by(Notification.id).limit(1)
    with Session(mem_db.bind) as first, Session(mem_db.bind) as second:
        a = first.scalar(query.with_for_update(skip_locked=True))
        b = second.scalar(query.with_for_update(skip_locked=True))
        assert a.id != b.id


def test_pg_jsonb_roundtrip(mem_db):
    item = Resource(service='ecs', resource_type='ecs_server', resource_id='json', name='中文',
                    labels={'team': '数据平台'}, attrs={'nested': {'values': [1, True, None]}})
    mem_db.add(item)
    mem_db.commit()
    mem_db.expire_all()
    loaded = mem_db.get(Resource, item.id)
    assert loaded.labels == {'team': '数据平台'}
    assert loaded.attrs['nested']['values'] == [1, True, None]


def test_pg_varchar_enforces_declared_length(mem_db):
    with pytest.raises(DataError), mem_db.begin_nested():
        mem_db.add(Resource(service='ecs', resource_type='ecs_server', resource_id='length', name='x' * 256))
        mem_db.flush()


def test_pg_timezone_and_unicode_ilike(mem_db):
    instant = datetime(2026, 10, 25, 12, 30, tzinfo=timezone(timedelta(hours=9)))
    item = Resource(service='ecs', resource_type='ecs_server', resource_id='time', name='生产数据库-ABC', last_seen=instant)
    mem_db.add(item)
    mem_db.commit()
    mem_db.expire_all()
    row = mem_db.scalar(select(Resource).where(Resource.name.ilike('%数据库-abc%')))
    assert row.last_seen == instant.astimezone(UTC)


def test_pg_project_identity_and_legacy_partial_indexes(mem_db):
    mem_db.add_all([Resource(service='ecs', resource_type='ecs_server', resource_id='same', name='same',
                             project_ref=project, tenant_id=project) for project in [1, 2, None]])
    mem_db.commit()
    with pytest.raises(IntegrityError), mem_db.begin_nested():
        mem_db.add(Resource(service='ecs', resource_type='ecs_server', resource_id='same', name='duplicate', project_ref=1))
        mem_db.flush()


def test_pg_dispatchers_do_not_send_the_same_outbox_row(mem_db, monkeypatch):
    from app.notify import dispatcher

    mem_db.add(User(username='pg-dispatch-reader', display_name='并发通知测试', email='test@example.com',
                    password_hash='!', login_enabled=False, enabled=True, notify_enabled=True, tenant_ids=None))
    for index in range(2):
        item = alarm(f'dispatch-{index}')
        mem_db.add(item)
        mem_db.flush()
        mem_db.add(Notification(kind='FIRING', alarm_id=item.id, recipients=['test@example.com']))
    mem_db.commit()
    monkeypatch.setattr(dispatcher, 'render_alarm_mail', lambda *_a, **_k: ('test', '<p>test</p>', 'test'))
    monkeypatch.setattr(dispatcher, '_platform_hooks', lambda *_a: None)
    entered, release = threading.Event(), threading.Event()
    sent = []

    def first_sender(message):
        sent.append(message['Message-ID'])
        entered.set()
        assert release.wait(5)

    def first_worker():
        with Session(mem_db.bind) as db:
            return dispatcher.dispatch_pending(db, sender=first_sender, batch=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_worker)
        try:
            assert entered.wait(5)
            with Session(mem_db.bind) as db:
                assert dispatcher.dispatch_pending(db, sender=lambda message: sent.append(message['Message-ID'])) == 1
        finally:
            release.set()
        assert first.result(timeout=5) == 1
    assert len(sent) == len(set(sent)) == 2
    assert set(mem_db.scalars(select(Notification.status))) == {'SENT'}
