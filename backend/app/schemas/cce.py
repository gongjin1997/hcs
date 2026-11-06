from pydantic import BaseModel, ConfigDict, Field, StrictBool


class CceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    resolve_owners: StrictBool = False
    resource_metrics: StrictBool = False


class CceSettingsOut(BaseModel):
    tenant_id: int
    settings: CceSettings
    master_enabled: bool
    task_enabled: bool
    effective_enabled: bool
    interval_sec: int


class CceClusterOut(BaseModel):
    key: str
    tenant_id: int
    tenant_name: str
    project_ref: int
    project_name: str
    cluster_id: str
    cluster: dict
    last_seen: str
    freshness: str
    missing: bool
    sources: dict
    nodes: list[dict] = Field(default_factory=list)
    nodepools: list[dict] = Field(default_factory=list)
    native: dict = Field(default_factory=dict)


class CceClustersOut(BaseModel):
    items: list[CceClusterOut]
    total: int
    page: int
    page_size: int
