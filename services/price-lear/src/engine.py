"""预测引擎：加载 LEAR 线性 predictor 状态、校验摘要、出 24 点预测。

LEAR = LASSO Estimated AutoRegression（Lago et al. 2021）。训练期用 sklearn 的
``SimpleImputer + StandardScaler + LassoCV`` 拟合，导出成一组纯数值（填充中位数、
标准化均值/尺度、LASSO 系数/截距、残差 q10/q90 偏移）随镜像交付。

推理期**不依赖 sklearn**：特征 → 中位数填充 → 标准化 → 点积 + 截距 → 加偏移，
全是 numpy，毫秒级。这样与训练期数值一致（同一套变换），又让运行镜像依赖最小。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from .config import Config
from .features import day_features

logger = logging.getLogger(__name__)

DIGEST_MANIFEST = "sha256.json"
STATE_FILE = "state.json"
POINTS_PER_DAY = 24
# 分位带来源：训练残差的经验分位数固定偏移（非模型原生分位，已在 README 标注）
LEVELS = ("0.1", "0.5", "0.9")


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
class _Predictor:
    """纯数值 predictor，等价于 sklearn 管线的一次 predict。"""

    impute_medians: np.ndarray
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray
    coef: np.ndarray
    intercept: float
    offset_q10: float
    offset_q90: float

    def predict(self, matrix: np.ndarray) -> np.ndarray:
        # 1) 中位数填充（仅对 NaN 列）
        x = np.array(matrix, dtype=float, copy=True)
        nan_mask = np.isnan(x)
        if nan_mask.any():
            cols = np.where(nan_mask.any(axis=0))[0]
            for col in cols:
                x[nan_mask[:, col], col] = self.impute_medians[col]
        # 2) 标准化（scale=0 的列 sklearn StandardScaler 置 1，训练期已导出）
        x = (x - self.scaler_mean) / self.scaler_scale
        # 3) 线性点积 + 截距
        return x @ self.coef + self.intercept


@dataclass
class _ModelState:
    predictor: _Predictor | None = None
    load_error: str | None = None
    load_seconds: float = 0.0
    digest_verified: bool = False
    revision: str | None = None
    feature_names: list[str] | None = None


class PriceEngine:
    """持有唯一一组 LEAR predictor，把它暴露成一个预测操作。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._load_lock = threading.Lock()
        self._state = _ModelState()
        self._read_manifest()
        if config.verify_weights:
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
        target = self._model_dir() / STATE_FILE
        if not target.is_file():
            return f"{STATE_FILE}: 缺失"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        expected = str(payload.get(STATE_FILE, ""))
        if expected and actual.lower() != expected.lower():
            return f"{STATE_FILE}: 期望 {expected}，实际 {actual}"
        self._state.digest_verified = True
        return None

    def load(self) -> bool:
        """可重入：已就绪时空操作，失败保留原因。"""
        with self._load_lock:
            if self._state.predictor is not None:
                return True
            started = time.perf_counter()
            try:
                if self._state.load_error:
                    raise RuntimeError(self._state.load_error)
                state = json.loads((self._model_dir() / STATE_FILE).read_text(encoding="utf-8"))
                predictor = _Predictor(
                    impute_medians=np.asarray(state["impute_medians"], dtype=float),
                    scaler_mean=np.asarray(state["scaler_mean"], dtype=float),
                    scaler_scale=np.asarray(state["scaler_scale"], dtype=float),
                    coef=np.asarray(state["coef"], dtype=float),
                    intercept=float(state["intercept"]),
                    offset_q10=float(state["offset_q10"]),
                    offset_q90=float(state["offset_q90"]),
                )
                # 形状自检：所有向量必须等长，且与训练特征维度一致
                n = predictor.coef.size
                for name, arr in (
                    ("impute_medians", predictor.impute_medians),
                    ("scaler_mean", predictor.scaler_mean),
                    ("scaler_scale", predictor.scaler_scale),
                ):
                    if arr.size != n:
                        raise ValueError(f"{name} 维度 {arr.size} != coef {n}")
                self._state.feature_names = state.get("feature_names")
            except Exception as exc:  # noqa: BLE001 - 加载失败降级，不拖垮进程
                self._state.predictor = None
                self._state.load_error = f"模型加载失败：{exc}"
                logger.exception("model load failed")
                return False
            self._state.predictor = predictor
            self._state.load_error = None
            self._state.load_seconds = time.perf_counter() - started
            logger.info(
                "model ready: tier=%s load_seconds=%.3f digest_verified=%s revision=%s coef_dim=%d",
                self._config.model_tier,
                self._state.load_seconds,
                self._state.digest_verified,
                self._state.revision,
                n,
            )
            return True

    # ── 状态问答 ────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        return self._state.predictor is not None

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
            "state_file": STATE_FILE,
            "coef_dim": self._state.predictor.coef.size if self._state.predictor else None,
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
                raise InvalidRequest(f"小时 {hour} 的特征构造失败（锚点 {days[-1].date}）") from None
            matrix.append(row)
        matrix = np.asarray(matrix, dtype=float)

        started = time.perf_counter()
        point = self._state.predictor.predict(matrix)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        quantiles = {
            "0.1": point + self._state.predictor.offset_q10,
            "0.5": point,
            "0.9": point + self._state.predictor.offset_q90,
        }
        return PriceOutcome(
            target=target,
            horizon_days=horizon_days,
            point=np.asarray(quantiles["0.5"]),
            quantiles=quantiles,
            elapsed_ms=elapsed_ms,
        )

    def _require_ready(self) -> None:
        if self._state.predictor is None:
            self.load()
        if self._state.predictor is None:
            raise ModelUnavailable(self._state.load_error or "模型未就绪")
