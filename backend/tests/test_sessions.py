"""密码变更、退出登录与 SSE 必须遵守同一会话失效规则。"""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import Role, User

from .conftest import login


@pytest.fixture()
def session_user(client):
    name = f'session-{uuid4().hex[:12]}'
    with SessionLocal() as db:
        role = db.scalar(select(Role).where(Role.code == 'viewer'))
        user = User(username=name, display_name='会话回归', password_hash=hash_password('Before-1234'), role_id=role.id)
        db.add(user)
        db.commit()
        return name, user.id


def test_password_change_invalidates_existing_tokens(client, session_user):
    username, _ = session_user
    headers = login(client, username, 'Before-1234')
    result = client.post('/api/v1/auth/password', headers=headers,
                         json={'old_password': 'Before-1234', 'new_password': 'After-1234'})
    assert result.status_code == 200
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401
    assert client.get('/api/v1/auth/me', headers=login(client, username, 'After-1234')).status_code == 200


def test_logout_revokes_current_token_without_revoking_another_session(client, session_user):
    username, _ = session_user
    first = login(client, username, 'Before-1234')
    second = login(client, username, 'Before-1234')
    result = client.post('/api/v1/auth/logout', headers=first)
    assert result.status_code == 200
    assert client.get('/api/v1/auth/me', headers=first).status_code == 401
    assert client.get('/api/v1/auth/me', headers=second).status_code == 200


def test_admin_reset_invalidates_old_token(client, admin, session_user):
    username, uid = session_user
    headers = login(client, username, 'Before-1234')
    result = client.post(f'/api/v1/system/users/{uid}/reset-password', headers=admin, json={'password': 'Reset-1234'})
    assert result.status_code == 200
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401


def test_disable_then_enable_does_not_revive_old_token(client, admin, session_user):
    username, uid = session_user
    headers = login(client, username, 'Before-1234')
    for enabled in [False, True]:
        result = client.patch(f'/api/v1/system/users/{uid}', headers=admin, json={'enabled': enabled})
        assert result.status_code == 200
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401


def test_logout_invalidates_already_issued_sse_ticket(client, session_user):
    username, _ = session_user
    headers = login(client, username, 'Before-1234')
    ticket = client.post('/api/v1/stream/ticket', headers=headers).json()['data']['ticket']
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/v1/stream/alarms', params={'ticket': ticket}).status_code == 401
