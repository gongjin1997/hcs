"""邮件标识只使用租户，不拼接部署环境 PROD。"""
import pytest

from app.core.config import get_settings
from app.models import AlarmEvent
from app.models.base import utcnow
from app.notify.render import build_subject, render_alarm_mail


@pytest.mark.parametrize("kind", ["FIRING", "UPGRADE", "REPEAT", "ESCALATE", "RESOLVED"])
def test_alarm_subject_uses_tenant_without_environment(monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "env_tag", "PROD")
    alarm = AlarmEvent(id=9101, tenant_id=1, resource_type="ecs_server", resource_name="测试资源",
                       service="ecs", severity="P2", title="状态异常", detail={"tenant": "测试租户"},
                       first_seen=utcnow(), last_seen=utcnow())
    subject = build_subject(alarm, kind)
    assert "【测试租户】" in subject and "【PROD" not in subject and "PROD·" not in subject
    assert "ECS" in subject and "测试资源" in subject


def test_global_alarm_does_not_manufacture_environment_or_empty_tenant_tag():
    alarm = AlarmEvent(id=9102, resource_type="ecs_server", resource_name="平台资源", service="ecs",
                       severity="P2", title="状态异常", detail={}, first_seen=utcnow(), last_seen=utcnow())
    assert "【PROD" not in build_subject(alarm, "FIRING") and "【】" not in build_subject(alarm, "FIRING")


def test_alarm_render_has_tenant_without_environment(mem_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "env_tag", "PROD")
    alarm = AlarmEvent(id=9103, rule_id="R-synthetic", resource_type="ecs_server", resource_name="测试资源",
                       check_item="status", service="ecs", severity="P2", status="FIRING", title="状态异常",
                       detail={"tenant": "测试租户"}, first_seen=utcnow(), last_seen=utcnow(), count=1)
    subject, html, text = render_alarm_mail(mem_db, alarm, "FIRING", action_links=False)
    assert "测试租户" in subject and "测试租户" in html and "测试租户" in text
    assert "PROD" not in subject + html + text


def test_existing_queued_subject_uses_current_tenant_format(mem_db, monkeypatch):
    from app.models import HcsAccount, Notification, User
    from app.notify import dispatcher

    person = User(username="mail-recipient", display_name="接收人", email="mail@example.invalid",
                  password_hash="!", login_enabled=False, notify_enabled=True)
    tenant = HcsAccount(code="mail-tenant", name="测试租户全名", short_name="租户简称",
                        iam_endpoint="https://example.invalid", domain_name="synthetic")
    mem_db.add_all([person, tenant])
    mem_db.flush()
    alarm = AlarmEvent(tenant_id=tenant.id, fingerprint="mail-format", rule_id="R-synthetic",
                       resource_type="ecs_server", resource_name="资源", check_item="status", service="ecs",
                       severity="P2", status="FIRING", title="状态异常", detail={},
                       first_seen=utcnow(), last_seen=utcnow(), count=1)
    mem_db.add(alarm)
    mem_db.flush()
    notice = Notification(alarm_id=alarm.id, kind="FIRING", recipients=[person.email],
                          subject="【PROD·旧名称】旧版标题")
    mem_db.add(notice)
    mem_db.commit()
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *_: None)
    sent = []
    assert dispatcher.dispatch_pending(mem_db, sender=sent.append) == 1
    assert len(sent) == 1 and "【租户简称】" in sent[0]["Subject"]
    assert "PROD" not in sent[0]["Subject"] and "PROD" not in notice.subject
