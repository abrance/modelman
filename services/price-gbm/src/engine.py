"""预测引擎：加载三个分位 booster、校验摘要、出 24 点预测。

与 forecast 服务同源的三条硬性要求里，这里只有第一条适用（推理放阻塞线程——
FastAPI 的同步 def 端点本来就在线程池里）；GBM 单次推理毫秒级且不占满核，
并发上界（信号量）没有意义，请求体体积上限在 api 层兜住即可。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import lightgbm as lgb

from .config import Config
from .features import day_features

logger = logging.getLogger(__name__)

DIGEST_MANIFEST = "sha256.json"
BOOSTERS = (("0.1", "q10.txt"), ("0.5", "q50.txt"), ("0.9", "q90.txt"))
POINTS_PER_DAY = 24


class ModelUnavailable(Exception):
    """权重不可用，业务操作一律拒绝（503）。"""


class InvalidRequest(Exception):
    """请求不合法（天数、形状、日期），折算成 400。"""


@dataclass(frozen=True)
class PriceOutcome:
    target: str
    horizon_days: int
    point: np.ndarray
    quantiles: dict[str, np.ndarray]
    elapsed_ms: float


@dataclass
class _ModelState:
    boosters: dict[str, lgb.Booster] | None = None
    load_error: str | None = None
    load_seconds: float = 0.0
    digest_verified: bool = False
    revision: str | None = None


class PriceEngine:
    """持有唯一一组 booster，把它们暴露成一个预测操作。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._state = _ModelState()
        self._read_manifest()
        if config.verify_weights:
            # 校验失败不阻止进程启动：错误会出现在 /models 与 /readyz 上
            problem = self._verify_weights()
            if problem:
                self._state.load_error = problem
                logger.error("weight digest mismatch: %s", problem)
                return
        self.load()

    # ── 权重生命周期 ────────────────────────────────────────────────

    def _model_dir(self) -> Path:
        return self._config.models_dir / self._config.model_tier

    def _read_manifest(self) -> None:
        try:
            payload = json.loads((self._model_dir() / DIGEST_MANIFEST).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            self._state.load_error = f"{DIGEST_MANIFEST} 不可读：{exc}"
            return
        self._state.revision = payload.get("revision")

    def _verify_weights(self) -> str | None:
        try:
            payload = json.loads((self._model_dir() / DIGEST_MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"{DIGEST_MANIFEST} 不可读：{exc}"
        problems = []
        for _level, name in BOOSTERS:
            target = self._model_dir() / name
            if not target.is_file():
                problems.append(f"{name}: 缺失")
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            expected = str(payload.get(name, ""))
            if expected and actual.lower() != expected.lower():
                problems.append(f"{name}: 期望 {expected}，实际 {actual}")
        if problems:
            return "；".join(problems)
        self._state.digest_verified = True
        return None

    def load(self) -> bool:
        """可重入：已就绪时空操作，失败保留原因。"""
        with self._load_lock:
            if self._state.boosters is not None:
                return True
            started = time.perf_counter()
            try:
                if self._state.load_error:
                    raise RuntimeError(self._state.load_error)
                boosters = {}
                for level, name in BOOSTERS:
                    booster = lgb.Booster(model_file=str(self._model_dir() / name))
                    # 镜像里可能被环境线程数干扰；单线程推理延迟毫秒级，钉死即可
                    booster.params["num_threads"] = 1
                    boosters[level] = booster
            except Exception as exc:  # noqa: BLE001 - 加载失败降级，不拖垮进程
                self._state.boosters = None
                self._state.load_error = f"模型加载失败：{exc}"
                logger.exception("model load failed")
                return False
            self._state.boosters = boosters
            self._state.load_error = None
            self._state.load_seconds = time.perf_counter() - started
            logger.info(
                "model ready: tier=%s load_seconds=%.2f digest_verified=%s revision=%s",
                self._config.model_tier,
                self._state.load_seconds,
                self._state.digest_verified,
                self._state.revision,
            )
            return True

    # ── 状态问答 ────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        return self._state.boosters is not None

    @property
    def load_error(self) -> str | None:
        return self._state.load_error

    def model_metadata(self) -> dict:
        return {
            "tier": self._config.model_tier,
            "model_dir": str(self._model_dir()),
            "revision": self._state.revision,
            "digest_verified": self._state.digest_verified,
            "load_seconds": round(self._state.load_seconds, 3),
            "boosters": [name for _level, name in BOOSTERS],
        }

    # ── 业务操作 ────────────────────────────────────────────────────

    def forecast(self, days: list, target: str) -> PriceOutcome:
        """days：pydantic DayItem 列表（升序完整日）。锚点 = 最后一天。"""
        self._require_ready()

        if not days:
            raise InvalidRequest("days 不能为空")
        if len(days) > self._config.max_days:
            raise InvalidRequest(
                f"days 条数 {len(days)} 超过 MAX_DAYS={self._config.max_days}"
            )

        history: dict[str, dict[str, list[float]]] = {}
        previous: date | None = None
        for index, item in enumerate(days):
            try:
                day = date.fromisoformat(item.date)
            except ValueError:
                raise InvalidRequest(f"days[{index}].date 非法：{item.date!r}（应为 YYYY-MM-DD）") from None
            if len(item.values) != POINTS_PER_DAY:
                raise InvalidRequest(
                    f"days[{index}].values 有 {len(item.values)} 个点，需要完整 {POINTS_PER_DAY} 点"
                )
            if item.power is not None and len(item.power) != POINTS_PER_DAY:
                raise InvalidRequest(f"days[{index}].power 有 {len(item.power)} 个点，需要 {POINTS_PER_DAY} 点")
            if any(value != value for value in item.values):
                raise InvalidRequest(f"days[{index}].values 含 NaN")
            if previous is not None and day <= previous:
                raise InvalidRequest(
                    f"days 必须升序且无重复：days[{index}]={item.date} 不晚于 {previous}"
                )
            previous = day
            history[item.date] = {
                "prices": [float(v) for v in item.values],
                "power": [float(v) for v in item.power] if item.power else [],
            }

        try:
            target_day = date.fromisoformat(target)
        except ValueError:
            raise InvalidRequest(f"target 非法：{target!r}（应为 YYYY-MM-DD）") from None

        anchor = date.fromisoformat(days[-1].date)
        horizon_days = (target_day - anchor).days
        if horizon_days <= 0:
            raise InvalidRequest(f"target {target} 必须晚于锚点 {days[-1].date}")
        if horizon_days > self._config.max_horizon_days:
            raise InvalidRequest(
                f"目标日距锚点 {horizon_days} 天，超过 MAX_HORIZON_DAYS={self._config.max_horizon_days}"
            )

        matrix = []
        for hour in range(POINTS_PER_DAY):
            row = day_features(history, target_day, hour, horizon_days)
            if row is None:
                # 锚点一定在 days 里，这里 None 只可能是特征层的日期代数漏了
                raise InvalidRequest(f"小时 {hour} 的特征构造失败（锚点 {days[-1].date}）") from None
            matrix.append(row)
        matrix = np.asarray(matrix, dtype=float)

        started = time.perf_counter()
        quantiles = {}
        for level, _name in BOOSTERS:
            quantiles[level] = np.asarray(self._state.boosters[level].predict(matrix), dtype=float)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        return PriceOutcome(
            target=target,
            horizon_days=horizon_days,
            point=np.asarray(quantiles["0.5"]),
            quantiles=quantiles,
            elapsed_ms=elapsed_ms,
        )

    def _require_ready(self) -> None:
        if self._state.boosters is None:
            self.load()
        if self._state.boosters is None:
            raise ModelUnavailable(self._state.load_error or "模型未就绪")
