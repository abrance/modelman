"""响应与请求的协议结构。

字段名与 NAS 上正在跑的实现保持一致，只做加法：
`line` / `cluster_id` / `template` / `change_type` / `parameters` /
`matched` / `size` / `total_size` / `time_ms` / `success` 全部沿用。
新增两类字段：

- `error`：仓库的接口约定要求错误响应带可读的 `error`；
- `detail`：旧实现把错误放在 FastAPI 默认的 `detail` 里，保留一份
  是为了让可能的既有调用方不改代码（见 `docs/design.md` 目标 3）。

同理，`success` 在旧实现里只出现在成功路径（失败时抛 HTTPException），
这里失败也带上它，属于纯新增。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClusterLine(BaseModel):
    line: str = Field(..., description="原始日志行")
    cluster_id: int = Field(..., description="所属簇 ID")
    template: str = Field(..., description="挖出的日志模板")
    change_type: str = Field(
        ..., description="none / cluster_created / cluster_template_changed"
    )
    parameters: list[str] | None = Field(
        default=None, description="模板中的可变参数；无法提取时为 null"
    )


class ClusterRequest(BaseModel):
    lines: list[str] = Field(..., min_length=1, description="待聚类的日志行列表")


class ClusterResponse(BaseModel):
    success: bool
    results: list[ClusterLine] = Field(default_factory=list)
    cluster_count: int = 0
    time_ms: float = 0.0
    error: str | None = None
    detail: str | None = None


class MatchLine(BaseModel):
    line: str
    matched: bool
    cluster_id: int | None = None
    template: str | None = None


class MatchRequest(BaseModel):
    lines: list[str] = Field(..., min_length=1, description="待匹配的日志行列表")


class MatchResponse(BaseModel):
    success: bool
    results: list[MatchLine] = Field(default_factory=list)
    time_ms: float = 0.0
    error: str | None = None
    detail: str | None = None


class ClusterInfo(BaseModel):
    cluster_id: int
    template: str
    size: int


class ClustersResponse(BaseModel):
    success: bool
    clusters: list[ClusterInfo] = Field(default_factory=list)
    total_size: int = 0
    error: str | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    models_loaded: list[str] = Field(default_factory=list)
    uptime_secs: float
    cluster_count: int
    total_size: int


class ModelInfo(BaseModel):
    """与 OCR 服务的 /models 同形。

    本服务没有权重文件，"档位"就是那唯一一套聚类参数；
    `state` 里带上模板树的实际情况，`load_error` 保持字段存在以便
    运维工具用同一套逻辑读两个服务。
    """

    id: str
    label: str
    available: bool
    loaded: bool
    load_error: str | None = None
    params: dict
    state: dict = Field(default_factory=dict)


class VersionResponse(BaseModel):
    name: str
    version: str
    git_commit: str
    build_time: str
    listen_addr: str
    state_dir: str
    profile_id: str
    sim_th: float
    depth: int
    max_children: int
    mask_rules: list[str]
    parametrize_numeric: bool
    snapshot_interval_minutes: int
    max_concurrency: int
    queue_timeout_secs: float
    limit_concurrency: int
    max_lines: int
    max_line_chars: int
    max_bytes: int
    auth_required: bool
    state_loaded: bool
