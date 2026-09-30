"""请求与响应模型。

业务响应结构属于对外契约：字段只能新增不能改名。`/v1/systemone` 的形状对齐
TypeSafe Jev 的公开 API——`{model, answers, usage}`，其中 `answers[id]` 带
`choice` / `noul` / `score` 与 `confidence`。上游 SDK 与我们自己的调用方都依赖
这个名字，不要按本仓库习惯改写成下划线风格。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

QuestionType = Literal["choice", "noul", "score"]


class Question(BaseModel):
    type: QuestionType
    instructions: str
    # choice 用 {option_id: 说明}，score 用有序的说明列表；noul 可以不带
    criteria: dict[str, str] | list[str] | None = None
    labels: dict[str, str] | None = None


class SystemOneRequest(BaseModel):
    # state 可以是纯文本、JSON 对象或对话轮次列表，与上游一致
    state: str | dict[str, Any] | list[Any]
    questions: dict[str, Question]
    # 上游用它选 checkpoint；本服务只有一个档位，接受但忽略，便于直接替换
    model: str | None = None


class SystemOneResponse(BaseModel):
    success: bool = True
    model: str
    answers: dict[str, Any]
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
    max_len: int
    head_max_len: int
    max_state_chars: int
    max_questions: int
    max_options: int
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
