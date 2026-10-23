"""运维命令 python -m app.tools.status：摘要输出、JSON、进程心跳健康检查。"""
import json

from app.core.db import SessionLocal
from app.models import WorkerHeartbeat
from app.models.base import utcnow
from app.services import platform_watch
from app.tools import status


def test_status_summary_and_json(client, capsys):
    with SessionLocal() as db:
        st = status.collect(db)
    assert st["database_ok"] and {w["name"] for w in st["workers"]} == set(platform_watch.WORKERS)
    text = status.render(st)
    assert "后台进程" in text and "巡检任务" in text and "结论" in text
    status.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["version"] and "outbox" in out


def test_status_check_heartbeat(client):
    with SessionLocal() as db:
        db.query(WorkerHeartbeat).filter_by(name="notifier").delete()
        db.commit()
    assert status.main(["--check", "notifier"]) == 1          # 从未上报心跳 → 不健康
    with SessionLocal() as db:
        platform_watch.beat(db, "notifier")
    assert status.main(["--check", "notifier"]) == 0
    with SessionLocal() as db:
        hb = db.get(WorkerHeartbeat, "notifier")
        hb.beat_at = utcnow().replace(year=2000)
        db.commit()
    assert status.main(["--check", "notifier"]) == 1          # 心跳超时
    with SessionLocal() as db:
        db.query(WorkerHeartbeat).filter_by(name="notifier").delete()
        db.commit()
