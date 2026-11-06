"""Scheduler frequency and enablement contract; no cloud calls."""
from app.models import InspectionTask, SysSetting
from app.schemas.collection_policy import CollectionPolicy
from app.services import collection_policy
from app.workers import scheduler


def test_ecs_policy_jobs_poll_each_minute_while_legacy_tasks_keep_their_schedule(mem_db, monkeypatch):
    monkeypatch.setattr(scheduler, "RUNNABLE", {*scheduler.RUNNABLE, "ecs_metrics"})
    for code, interval, enabled, cron in (("ecs_status", 120, True, None), ("ecs_metrics", 300, True, None),
            ("ledger_sync", 3600, True, None), ("dcs_status", 300, True, "*/5 * * * *")):
        mem_db.add(InspectionTask(code=code, name=code, service="ecs", interval_sec=interval, enabled=enabled, cron=cron))
    mem_db.commit()
    jobs = scheduler.desired_jobs(mem_db)
    assert jobs["ecs_status"]["sig"] == jobs["ecs_metrics"]["sig"] == ("interval", 60)
    assert jobs["ecs_inventory"]["sig"] == ("interval", 60)
    assert jobs["ledger_sync"]["sig"] == ("interval", 3600)
    assert jobs["dcs_status"]["sig"] == ("cron", "*/5 * * * *")


def test_disabled_tasks_never_gain_policy_jobs(mem_db, monkeypatch):
    monkeypatch.setattr(scheduler, "RUNNABLE", {*scheduler.RUNNABLE, "ecs_metrics"})
    for code in ("ecs_status", "ecs_metrics", "ledger_sync"):
        mem_db.add(InspectionTask(code=code, name=code, service="ecs", interval_sec=120, enabled=False))
    mem_db.commit()
    jobs = scheduler.desired_jobs(mem_db)
    assert not set(jobs) & {"ecs_status", "ecs_metrics", "ecs_inventory", "ledger_sync"}


def test_policy_reports_task_and_master_gates_honestly(mem_db):
    status = collection_policy.phase_status(mem_db, 11, collection_policy.effective(mem_db, 11))
    assert status["metrics"]["configured_enabled"]
    assert not status["metrics"]["task_enabled"]
    assert not status["metrics"]["effective_enabled"]
    assert not status["quota"]["effective_enabled"]


def test_legacy_cron_still_uses_polling_to_allow_tenant_interval_overrides(mem_db):
    mem_db.add(InspectionTask(code="ecs_status", name="status", service="ecs", interval_sec=600,
                              cron="0 3 * * *", enabled=True))
    mem_db.commit()
    assert scheduler.desired_jobs(mem_db)["ecs_status"]["sig"] == ("interval", 60)


def test_accepted_legacy_thirty_second_schedule_is_not_silently_slowed(mem_db):
    mem_db.add(InspectionTask(code="ecs_status", name="status", service="ecs", interval_sec=30, enabled=True))
    mem_db.commit()
    assert collection_policy.effective(mem_db, 11).inspection.interval_sec == 30
    assert scheduler.desired_jobs(mem_db)["ecs_status"]["sig"] == ("interval", 30)
    task = mem_db.get(InspectionTask, "ecs_status")
    task.interval_sec = 120
    explicit = CollectionPolicy(inspection={"interval_sec": 30})
    mem_db.add(SysSetting(key=collection_policy.policy_key(11), value=explicit.model_dump(mode="json")))
    mem_db.commit()
    assert scheduler.desired_jobs(mem_db)["ecs_status"]["sig"] == ("interval", 30)
