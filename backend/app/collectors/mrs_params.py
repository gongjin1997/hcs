"""MRS 参数与进度契约。任务 API 可复用有限字段校验，不改变公共 API/运行状态机。"""
import logging
import math
from copy import deepcopy

from app.hcs import HcsError
from app.services.catalog import MRS_TASK_PARAM_SCHEMAS

from .base import RunContext

log = logging.getLogger(__name__)
ACTIVE_JOB_STATES = ("NEW", "NEW_SAVING", "SUBMITTED", "ACCEPTED", "RUNNING")


def mrs_parameter_schema(task_code: str) -> dict:
    """返回独立副本，供任务页表单/API白名单使用；不修改保存中的任务参数。"""
    if task_code not in MRS_TASK_PARAM_SCHEMAS:
        raise ValueError("不是支持参数编辑的 MRS 任务")
    return deepcopy(MRS_TASK_PARAM_SCHEMAS[task_code])


def validate_mrs_params(task_code: str, params: dict, *, strict: bool = True) -> dict:
    """strict 用于 API；采集器 strict=False 保留公共内部参数及旧 window_hours 等兼容项。"""
    schema = mrs_parameter_schema(task_code)
    if not isinstance(params, dict):
        raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 参数须为对象")
    fields = schema["properties"]
    if strict and params.keys() - fields.keys():
        raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 参数包含未开放字段")
    result = {}
    for key, spec in fields.items():
        if key not in params and "default" not in spec:
            continue
        value = params.get(key, spec.get("default"))
        kind = spec["type"]
        if isinstance(kind, list):
            valid = value is None or isinstance(value, str)
        elif kind == "integer":
            valid = type(value) is int
        elif kind == "number":
            valid = type(value) is int or type(value) is float and math.isfinite(value)
        else:
            valid = isinstance(value, str)
        if not valid or "enum" in spec and value not in spec["enum"]:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", f"MRS {key} 类型或取值无效")
        if kind in ("integer", "number") and (
                "minimum" in spec and value < spec["minimum"]
                or "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]
                or "maximum" in spec and value > spec["maximum"]):
            raise HcsError(0, "LOCAL.INVALID_CONFIG", f"MRS {key} 超出允许范围")
        result[key] = value
    if task_code == "mrs_job_status":
        explicit = "submitted_time_begin" in params or "submitted_time_end" in params
        if explicit and ("submitted_time_begin" not in params or "submitted_window_hours" in params):
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 显式时间窗须提供起点且不能同时设置小时窗口")
        if result["query_mode"] in ("active_only", "all_history") and (
                explicit or "submitted_window_hours" in params):
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 当前查询模式不接受提交时间窗口")
        if result["query_mode"] in ("active_only", "all_history"):
            result.pop("submitted_window_hours")
        if explicit:
            result.pop("submitted_window_hours")
            if result.get("submitted_time_end", result["submitted_time_begin"]) < result["submitted_time_begin"]:
                raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 提交时间窗口终点早于起点")
    return result


def native_queries(config: dict, now_ms: int) -> list[tuple[str, dict]]:
    queries: list[tuple[str, dict]] = []
    if config["query_mode"] in ("active_and_recent", "active_only"):
        queries.extend((f"active_{state}", {"job_state": state}) for state in ACTIVE_JOB_STATES)
    if config["query_mode"] in ("active_and_recent", "recent_only"):
        begin = config.get("submitted_time_begin", int(now_ms - config.get("submitted_window_hours", 24) * 3_600_000))
        end = config.get("submitted_time_end", now_ms)
        if begin > end:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 提交时间窗口起点晚于本轮终点")
        queries.append(("recent", {"submitted_time_begin": begin, "submitted_time_end": end}))
    if config["query_mode"] == "all_history":
        queries.append(("all_history", {}))
    return queries


def mrs_progress(ctx: RunContext, phase: str, *, current: int | None = None, total: int | None = None,
                 unit: str | None = None, message: str | None = None, **details) -> None:
    # RunContext 已提供 progress 时，未托管调用可能是空操作；本地统计仍保存真实的最后进度。
    ctx.stats["mrs_progress"] = {"phase": phase, "current": current, "total": total,
                                 "unit": unit, "message": message, **details}
    progress = getattr(ctx, "progress", None)
    if callable(progress):
        progress(phase, current=current, total=total, unit=unit, message=message, **details)
    else:  # 主执行器接入前/未托管上下文：只有统计和日志，无持久队列/线程。
        log.info("mrs_collection_progress", extra={"event": "mrs_collection_progress", "phase": phase,
                 "current": current, "total": total, "unit": unit, **details})
