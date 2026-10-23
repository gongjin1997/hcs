"""平台状态巡检（运维命令行）：一屏看清进程心跳、通知积压、任务健康、平台告警、数据库迁移版本。

用法（部署目录执行 make status，等价于）：
    docker compose exec api python -m app.tools.status
    docker compose exec api python -m app.tools.status --json          # 机器可读
容器健康检查（调度 / 通知进程没有 HTTP 端口，用心跳判断存活）：
    python -m app.tools.status --check scheduler      # 心跳超时（系统设置 → 平台自身告警 → N 分钟）退出码 1
退出码：0 正常；1 有异常（进程心跳超时 / 任务连续失败达到阈值 / 通知最终失败 / 迁移未到最新）。
"""
import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import AlarmEvent, HcsAccount, InspectionRun, InspectionTask, Notification, WorkerHeartbeat
from app.models.base import utcnow
from app.services import platform_watch, settings_service

ACTIVE = ("FIRING", "ACKED", "SUPPRESSED")


def _ago(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 90:
        return f"{sec} 秒前"
    if sec < 5400:
        return f"{sec // 60} 分钟前"
    if sec < 172800:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


def migration_state(db: Session) -> dict[str, Any]:
    """当前库的 alembic 版本 vs 代码里的最新版本（开发环境用 create_all 建表时没有版本表）。"""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        cfg = Config(str(ini))
        cfg.set_main_option("script_location", str(ini.parent / "migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head() if ini.exists() else None
    except Exception:  # noqa: BLE001
        head = None
    try:
        current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        db.rollback()
        current = None
    return {"current": current, "head": head, "ok": head is None or current is None or current == head}


def collect(db: Session) -> dict[str, Any]:
    s = get_settings()
    now = utcnow()
    cfg = settings_service.get_platform_alarm(db)
    workers: list[dict[str, Any]] = []
    for name, label in platform_watch.WORKERS.items():
        hb = db.get(WorkerHeartbeat, name)
        if hb is None:
            workers.append({"name": name, "label": label, "status": "NEVER"})
            continue
        age = (now - hb.beat_at).total_seconds()
        workers.append({"name": name, "label": label, "status": "OK" if age <= cfg.heartbeat_timeout_min * 60 else "LATE",
                        "age_sec": int(age), "host": hb.host, "version": hb.version})
    day = now - timedelta(hours=24)
    by_status = dict(db.execute(select(Notification.status, func.count()).where(Notification.created_at >= day)
                                .group_by(Notification.status)).all())
    backlog = dict(db.execute(select(Notification.status, func.count())
                              .where(Notification.status.in_(("PENDING", "RETRYING"))).group_by(Notification.status)).all())
    tenants = {a.id: a.short_name or a.name for a in db.scalars(select(HcsAccount))}
    tasks = []
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        runs = db.scalars(select(InspectionRun).where(InspectionRun.task_code == t.code, InspectionRun.status != "RUNNING")
                          .order_by(InspectionRun.started_at.desc()).limit(40)).all()
        per: dict[int | None, dict[str, Any]] = {}
        for r in runs:
            p = per.setdefault(r.tenant_id, {"tenant": tenants.get(r.tenant_id, "-") if r.tenant_id else "全局",
                                             "last_status": r.status, "age_sec": int((now - r.started_at).total_seconds()),
                                             "fails": 0, "_counting": True})
            if p["_counting"] and r.status == "FAILED":
                p["fails"] += 1
            elif r.status != "FAILED":
                p["_counting"] = False
        rows = [{k: v for k, v in p.items() if not k.startswith("_")} for p in per.values()]
        tasks.append({"code": t.code, "name": t.name, "enabled": t.enabled, "tenants": rows,
                      "status": "FAILED" if any(p["fails"] >= cfg.task_fail_threshold for p in rows)
                      else ("WARN" if any(p["fails"] for p in rows) else "OK")})
    alarms = db.scalars(select(AlarmEvent).where(AlarmEvent.service == platform_watch.PLATFORM,
                                                 AlarmEvent.status.in_(ACTIVE)).order_by(AlarmEvent.first_seen.desc())).all()
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "version": __version__, "env": s.env, "database": db.bind.dialect.name if db.bind is not None else None,
        "database_ok": db_ok, "migration": migration_state(db), "redis_configured": bool(s.redis_url),
        "workers": workers, "heartbeat_timeout_min": cfg.heartbeat_timeout_min,
        "outbox": {"sent_24h": by_status.get("SENT", 0), "failed_24h": by_status.get("FAILED", 0),
                   "pending": backlog.get("PENDING", 0), "retrying": backlog.get("RETRYING", 0)},
        "tasks": tasks, "task_fail_threshold": cfg.task_fail_threshold,
        "platform_alarms": [{"rule": a.rule_id, "severity": a.severity, "title": a.title, "resource": a.resource_name,
                             "status": a.status} for a in alarms],
    }


def problems(st: dict[str, Any]) -> list[str]:
    out = []
    if not st["database_ok"]:
        out.append("数据库不可用")
    if not st["migration"]["ok"]:
        out.append(f"数据库迁移未到最新（当前 {st['migration']['current']}，最新 {st['migration']['head']}）：make migrate")
    out += [f"{w['label']}心跳超时（{_ago(w['age_sec'])}）" for w in st["workers"] if w["status"] == "LATE"]
    out += [f"{w['label']}从未启动" for w in st["workers"] if w["status"] == "NEVER" and st["env"] == "prod"]
    out += [f"任务 {t['name']} 连续失败（{'、'.join(p['tenant'] for p in t['tenants'] if p['fails'])}）"
            for t in st["tasks"] if t["status"] == "FAILED"]
    if st["outbox"]["failed_24h"]:
        out.append(f"近 24 小时有 {st['outbox']['failed_24h']} 封通知最终发送失败（通知管理 → 发送记录）")
    return out


def render(st: dict[str, Any]) -> str:
    mark = {"OK": "✔", "WARN": "!", "LATE": "✘", "FAILED": "✘", "NEVER": "-"}
    mig = st["migration"]
    mig_text = mig["current"] or "无版本表（开发模式）"
    if not mig["ok"]:
        mig_text += f" → 最新 {mig['head']}（需 make migrate）"
    lines = [f"HCS-Inspector v{st['version']}  env={st['env']}  数据库={st['database']}"
             f"（{'可用' if st['database_ok'] else '不可用'}）  迁移版本={mig_text}", "", "后台进程："]
    for w in st["workers"]:
        if "age_sec" in w:
            extra = f"  最后心跳 {_ago(w['age_sec'])}  {w.get('host', '')}  v{w.get('version', '-')}"
        else:
            extra = "  未收到心跳"
        lines.append(f"  {mark.get(w['status'], '?')} {w['label']:<6} {w['status']:<6}{extra}")
    o = st["outbox"]
    lines += ["", f"通知投递（24h）：已发送 {o['sent_24h']}  最终失败 {o['failed_24h']}  "
                  f"待发送 {o['pending']}  重试中 {o['retrying']}",
              "", f"巡检任务（连续失败 ≥ {st['task_fail_threshold']} 次判定失败）："]
    for t in st["tasks"]:
        parts = []
        for p in t["tenants"]:
            fails = f"，连败 {p['fails']}" if p["fails"] else ""
            parts.append(f"{p['tenant']}:{p['last_status']}（{_ago(p['age_sec'])}{fails}）")
        per = "  ".join(parts) or "未运行"
        lines.append(f"  {mark.get(t['status'], '?')} {t['name'][:18]:<18} {'' if t['enabled'] else '[已停用] '}{per}")
    lines += ["", f"平台自身告警：{len(st['platform_alarms'])} 条活动"]
    lines += [f"  [{a['severity']}] {a['rule']} {a['resource'] or ''}：{a['title']}" for a in st["platform_alarms"]]
    bad = problems(st)
    lines += ["", "结论：" + ("正常" if not bad else f"有 {len(bad)} 项异常")] + [f"  - {b}" for b in bad]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HCS-Inspector 平台状态")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--check", choices=sorted(platform_watch.WORKERS), help="只检查某个进程的心跳（容器健康检查用）")
    args = ap.parse_args(argv)
    with SessionLocal() as db:
        if args.check:
            hb = db.get(WorkerHeartbeat, args.check)
            timeout = settings_service.get_platform_alarm(db).heartbeat_timeout_min * 60
            ok = hb is not None and (utcnow() - hb.beat_at).total_seconds() <= timeout
            print("ok" if ok else "heartbeat timeout")
            return 0 if ok else 1
        st = collect(db)
    print(json.dumps(st, ensure_ascii=False, indent=2, default=str) if args.json else render(st))
    return 1 if problems(st) else 0


if __name__ == "__main__":
    sys.exit(main())
