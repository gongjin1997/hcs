"""作业报表的只读快照契约；空值表示当前采集链路未保存该字段。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.response import Page

JobPanel = Literal["dgc", "flink", "realtime", "batch", "mrs"]
JobResourceType = Literal["dgc_realtime_job", "dgc_batch_job", "mrs_job"]


class JobReportRow(BaseModel):
    id: int
    resource_id: str
    name: str
    service: str
    resource_type: JobResourceType
    job_type: str | None
    engine: str | None
    status_raw: str | None
    execution_state: str | None = Field(description="采集器保存的 job_state/job_status/instance_status；历史未保存时为空")
    execution_result: str | None = Field(description="MRS 已保存的 job_result，与执行状态独立；不从健康度推断")
    display_status: str = Field(description="已知状态归一化；未知非空状态保留原文；缺失状态为 NO_DATA")
    status_label: str
    status_known: bool
    health: str = Field(description="巡检告警健康度，与作业执行状态独立")
    lifecycle: str
    tenant_id: int | None
    tenant_name: str | None
    project_ref: int | None
    project_id: str | None
    project_name: str | None
    region: str | None
    updated_at: datetime = Field(description="Resource.last_seen：最近保存资源快照的时间，并非作业完成时间")
    status_since: datetime | None
    status_details: dict = Field(description="已保存的工作空间、节点、实例或集群等白名单属性；不返回完整 attrs")
    metrics: dict = Field(description="资源上已保存的观测指标；无指标时为空对象，不补零")
    active_alarms: int = Field(description="同租户同资源主键的 FIRING / ACKED / SUPPRESSED 告警事件数")


class JobReportSummary(BaseModel):
    total: int
    status_counts: dict[str, int] = Field(description="所有匹配行（分页前）的 display_status 分布")
    by_source: dict[str, int] = Field(description="分页前按资源 service 分布，例如 dataarts/mrs")
    by_type: dict[str, int] = Field(description="分页前按作业资源类型分布，区分 DGC 实时、DGC 批作业和 MRS 作业")
    by_engine: dict[str, int] = Field(description="分页前按展示引擎分布；没有引擎依据的键为 UNKNOWN")
    unknown_total: int = Field(description="未定义或尚未识别的非空状态数量，不包含缺失状态")
    no_data_total: int = Field(description="匹配资源中缺失状态的数量；不计入 unknown_total")
    active_alarms: int
    latest_update_at: datetime | None
    data_status: Literal["AVAILABLE", "NO_DATA"]


class JobReportOut(Page[JobReportRow]):
    panel: JobPanel
    summary: JobReportSummary
    data_notes: list[str]
