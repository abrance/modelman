"""pydantic 请求/响应模型。字段是对外契约：只能新增不能改名。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DayItem(BaseModel):
    date: str = Field(description="YYYY-MM-DD，完整日")
    values: list[float] = Field(description="24 个整点日前价（元/MWh）")
    power: list[float] | None = Field(
        default=None,
        description="24 个整点电量（万 MWh 口径与官网一致）；缺省时电量特征按缺失处理",
    )


class PriceForecastRequest(BaseModel):
    days: list[DayItem] = Field(description="历史完整日，升序，最后一天即锚点")
    target: str = Field(description="目标日 YYYY-MM-DD，须晚于锚点")


class HealthResponse(BaseModel):
    status: str
    models_loaded: list[str]
    uptime_secs: float


class ModelInfo(BaseModel):
    id: str
    label: str
    available: bool
    loaded: bool
    load_error: str | None
    params: dict
    state: dict


class VersionResponse(BaseModel):
    name: str
    version: str
    git_commit: str
    build_time: str
    listen_addr: str
    models_dir: str
    tier: str
    auth_required: bool


class PriceForecastResponse(BaseModel):
    success: bool = True
    model: str
    target: str
    horizon_days: int
    point: list[float]
    quantiles: dict[str, list[float]]
    time_ms: float
