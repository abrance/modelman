"""请求与响应模型。

业务响应结构属于对外契约：字段只能新增不能改名。

`values` 允许两种形状：`[1.0, 2.0, ...]`（单变量）与 `[[...], [...]]`
（多变量，每个内层列表是一个变量、长度相同）。TimesFM 3.0 原生支持多变量，
所以这里不额外加一层 `variate` 字段，而是让形状自己表达。

`quantiles` 用字符串键（`"0.1"`…`"0.9"`）而不是列表：JSON 对象的键本来就是
字符串，写 `{"0.1": [...]}` 比 `{"levels": [...], "values": [[...]]}` 少一层
对齐，调用方不用按下标去配。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 单条序列的数值形状。
Values = list[float] | list[list[float]]


class Series(BaseModel):
    # 可选标识。请求里给了就原样回填，便于批量调用按 id 对齐。
    id: str | None = None
    values: Values = Field(min_length=1)


class ForecastRequest(BaseModel):
    series: list[Series] = Field(min_length=1)
    horizon: int = Field(ge=1)
    # 只要这几个分位。缺省返回模型全部自带的分位（0.1…0.9）。
    quantiles: list[float] | None = None


class ForecastItem(BaseModel):
    id: str | None = None
    # 中位数（0.5 分位）点预测。
    point: Values
    # 分位预测：键是分位水平，值与 point 同形状。
    quantiles: dict[str, Values] = Field(default_factory=dict)


class ForecastResponse(BaseModel):
    success: bool = True
    model: str
    horizon: int
    quantiles: list[float]
    forecasts: list[ForecastItem]
    # 每条序列实际吃进去的上下文长度（按 MAX_CONTEXT 截断后）。
    context_lengths: list[int]
    usage: dict[str, Any] = Field(default_factory=dict)
    time_ms: float
    truncated: bool = False


class HealthResponse(BaseModel):
    status: str
    models_loaded: list[str]
    uptime_secs: float


class VersionResponse(BaseModel):
    name: str
    version: str
    git_commit: str
    build_time: str
    listen_addr: str
    models_dir: str
    tier: str
    device: str
    infer_threads: int
    preload: bool
    verify_weights: bool
    max_context: int
    max_horizon: int
    max_series: int
    max_bytes: int
    max_concurrency: int
    queue_timeout_secs: float
    limit_concurrency: int
    auth_required: bool


class ModelInfo(BaseModel):
    id: str
    label: str
    available: bool
    loaded: bool
    load_error: str | None = None
    params: dict[str, Any]
    state: dict[str, Any]
