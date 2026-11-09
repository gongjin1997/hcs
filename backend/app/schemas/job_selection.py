"""DataArts 精确监控名单；指定为空不授权采集，未配置保留原采集范围。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator, model_validator

JobResourceType = Literal["dgc_realtime_job", "dgc_batch_job"]
JobName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class JobSelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ref: Annotated[StrictInt, Field(ge=1)]
    workspace: str = Field(max_length=128)
    mode: Literal["selected", "all"]
    jobs: list[JobName] = Field(max_length=10000)

    @field_validator("workspace")
    @classmethod
    def workspace_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("工作空间须填写准确的 ID，不含首尾空白或控制字符")
        return value

    @field_validator("jobs")
    @classmethod
    def exact_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() or name != name.strip() or any(ord(c) < 32 for c in name) for name in value):
            raise ValueError("作业名须为非空精确名称，不含首尾空白或控制字符")
        return sorted(set(value))

    @model_validator(mode="after")
    def explicit_all(self):
        if self.mode == "all" and self.jobs:
            raise ValueError("全部作业模式的 jobs 须为空；切换模式须显式确认")
        return self
