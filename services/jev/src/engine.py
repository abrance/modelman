"""判定引擎：权重生命周期、并发上界、Laya 调用。

三条硬性要求（与 OCR、日志聚类同源）：

1. **推理放阻塞线程。** Laya 是同步的 CPU 计算，FastAPI 的同步 `def` 端点本来
   就跑在线程池里，所以不要写成 `async def` 直接算——那会占住事件循环。
2. **并发有上界。** 云主机 CPU 与邻居共享，一次判定要吃满几个核，请求堆积比快速
   失败更糟；用一个信号量把同时进入模型的请求数压在上界内，等不到就 503。
3. **加载失败不钉死进程。** 权重缺失、sha256 不符、目录结构不对时进程照常启动，
   把原因暴露在 `/models` 与 `/readyz` 上，业务端点一律 503；进程活着才有机会
   用 HTTP 去看原因。

锁只有一把，因为只有一个模型实例：锁粒度已经是"引擎级"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .metrics import Metrics

logger = logging.getLogger(__name__)

# 构建期写下的权重摘要清单，`models/<tier>/sha256.json`。
DIGEST_MANIFEST = "sha256.json"

# 上游对 choice 选项数量的硬上限，超出直接拒；这里保持一致。
MAX_CHOICE_OPTIONS = 100

QUESTION_TYPES = ("choice", "noul", "score")


class Overloaded(Exception):
    """在超时时间内没拿到并发槽位。"""


class ModelUnavailable(Exception):
    """权重不可用，业务操作一律拒绝。"""


class InvalidRequest(Exception):
    """请求不符合 Jev 的问题 schema。折算成 400。"""


@dataclass(frozen=True)
class DecisionOutcome:
    answers: dict
    usage: dict
    model: str
    elapsed_ms: float
    truncated: bool = False


@dataclass
class _ModelState:
    agent: object | None = None
    load_error: str | None = None
    load_seconds: float = 0.0
    digest_verified: bool = False
    digests: dict[str, str] = field(default_factory=dict)
    weights_bytes: int = 0
    revision: str | None = None


class DecisionEngine:
    """持有唯一那套权重，并把它暴露成一个判定操作。"""

    def __init__(self, config: Config, metrics: Metrics) -> None:
        self._config = config
        self._metrics = metrics
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(config.max_concurrency)
        self._state = _ModelState()
        self._load_lock = threading.Lock()

        self._configure_threads()
        self._read_manifest()

        if config.preload:
            self.load()
        else:
            logger.info("preload disabled; weights load on first request")

    # ── 权重生命周期 ────────────────────────────────────────────────────

    def _configure_threads(self) -> None:
        """限制 torch 的 intra-op 线程数。

        必须在加载权重之前设，且只在真正用到 Laya 时生效：`torch` 由 Laya
        带进来，这里不做顶层 import，免得 /livez 之类的端点也被 torch 拖慢。
        """
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - 依赖缺失由 /readyz 暴露
            self._state.load_error = f"torch 不可用：{exc}"
            return
        torch.set_num_threads(self._config.infer_threads)
        logger.info("torch intra-op threads=%d", torch.get_num_threads())

    def _read_manifest(self) -> None:
        """读构建期写下的摘要清单。清单缺失不是致命错误，但要在 /models 上说明。"""
        path = self._config.model_dir / DIGEST_MANIFEST
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("digest manifest not found at %s", path)
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("digest manifest unreadable: %s", exc)
            self._state.load_error = f"{DIGEST_MANIFEST} 不可读：{exc}"
            return
        if not isinstance(payload, dict):
            self._state.load_error = f"{DIGEST_MANIFEST} 不是 JSON 对象"
            return
        self._state.digests = {
            str(key): str(value) for key, value in payload.items()
        }
        self._state.revision = self._state.digests.get("revision")

    def load(self) -> bool:
        """加载权重。可重入：已就绪时是空操作，失败时保留原因不反复重试。"""
        with self._load_lock:
            if self._state.agent is not None:
                return True
            if self._state.load_error and self._state.digests and not self._dir_exists():
                # 目录不存在这种问题重试也不会变好，直接返回上一次的原因
                return False

            started = time.perf_counter()
            try:
                agent = self._build_agent()
            except Exception as exc:  # noqa: BLE001 - 加载期任何异常都降级，不要拖垮进程
                self._state.agent = None
                self._state.load_error = f"权重加载失败：{exc}"
                self._state.load_seconds = time.perf_counter() - started
                logger.exception("model load failed")
                self._metrics.set_model(False, self._state.load_seconds, self._state.load_error)
                return False

            self._state.agent = agent
            self._state.load_error = None
            self._state.load_seconds = time.perf_counter() - started
            self._state.weights_bytes = _dir_size(self._config.model_dir)
            self._metrics.set_model(True, self._state.load_seconds, None)
            logger.info(
                "model ready: tier=%s device=%s load_seconds=%.2f weights_bytes=%d digests=%d",
                self._config.model_tier,
                self._config.device,
                self._state.load_seconds,
                self._state.weights_bytes,
                len(self._state.digests),
            )
            return True

    def _dir_exists(self) -> bool:
        return self._config.model_dir.is_dir()

    def _build_agent(self):
        from laya import Agent

        expected = {
            name: digest
            for name, digest in self._state.digests.items()
            # revision 只是清单里的元数据，不是文件
            if name != "revision"
        }
        agent = Agent(
            model_id_or_path=str(self._config.model_dir),
            device=self._config.device,
            expected_sha256=expected or None,
        )
        self._state.digest_verified = bool(expected)
        return agent

    # ── 状态问答 ────────────────────────────────────────────────────────

    @property
    def model_tier(self) -> str:
        return self._config.model_tier

    @property
    def load_error(self) -> str | None:
        return self._state.load_error

    def is_ready(self) -> bool:
        """就绪 = 权重已常驻。未就绪时进程仍在跑，但 /readyz 返回 503。"""
        return self._state.agent is not None

    def model_metadata(self) -> dict:
        return {
            "tier": self._config.model_tier,
            "model_dir": str(self._config.model_dir),
            "device": self._config.device,
            "infer_threads": self._config.infer_threads,
            "weights_bytes": self._state.weights_bytes,
            "digest_verified": self._state.digest_verified,
            "digests": len(self._state.digests),
            "revision": self._state.revision,
            "load_seconds": round(self._state.load_seconds, 3),
            "max_len": self._config.max_len,
            "head_max_len": self._config.head_max_len,
        }

    # ── 业务操作 ────────────────────────────────────────────────────────

    def decide(self, state, questions: Mapping[str, dict]) -> DecisionOutcome:
        self._require_ready()
        with self._slot():
            with self._lock:
                started = time.perf_counter()
                raw = self._state.agent.predict(
                    state,
                    dict(questions),
                    max_len=self._config.max_len,
                    head_max_len=self._config.head_max_len,
                )
            elapsed_ms = (time.perf_counter() - started) * 1000.0

        usage = raw.get("usage") or {}
        truncated = bool(usage.get("truncated"))
        if truncated:
            self._metrics.record_truncated()
        return DecisionOutcome(
            answers=_normalize_answers(raw.get("answers") or {}),
            usage=usage,
            model=self._config.model_tier,
            elapsed_ms=elapsed_ms,
            truncated=truncated,
        )

    def _require_ready(self) -> None:
        if self._state.agent is None:
            self.load()
        if self._state.agent is None:
            raise ModelUnavailable(self._state.load_error or "权重未就绪")

    @contextmanager
    def _slot(self) -> Iterator[None]:
        if not self._slots.acquire(timeout=self._config.queue_timeout_secs):
            self._metrics.record_rejected("overloaded")
            raise Overloaded(
                f"服务忙：等待 {self._config.queue_timeout_secs:g} 秒仍没有空闲的推理槽位"
            )
        try:
            yield
        finally:
            self._slots.release()


# ─── 响应归一 ───────────────────────────────────────────────────────────────


def _normalize_answers(answers: dict) -> dict:
    """给 choice 答案补一个 `distribution` 键。

    Laya 把选项概率放在 `probabilities`，而 TypeSafe Jev 的客户端读的是
    `distribution`（pi-jev 就是按这个键取分布的）。只补不改：上游原有的键
    全部保留，谁读哪个都行。
    """
    normalized = {}
    for qid, answer in answers.items():
        if isinstance(answer, dict) and "distribution" not in answer:
            probabilities = answer.get("probabilities")
            if isinstance(probabilities, dict):
                merged = dict(answer)
                merged["distribution"] = probabilities
                normalized[qid] = merged
                continue
        normalized[qid] = answer
    return normalized


# ─── 请求校验 ───────────────────────────────────────────────────────────────


def validate_request(state, questions, config: Config) -> None:
    """校验 Jev 请求形状。与上游 serve 的语义保持一致，超限直接 400。"""
    if state is None or (isinstance(state, str) and not state.strip()):
        raise InvalidRequest("state 不能为空")

    if isinstance(state, str):
        if len(state) > config.max_state_chars:
            raise InvalidRequest(
                f"state 长度 {len(state)} 超过 MAX_STATE_CHARS={config.max_state_chars}"
            )
    elif _state_chars(state) > config.max_state_chars:
        raise InvalidRequest(
            f"state 序列化后超过 MAX_STATE_CHARS={config.max_state_chars}"
        )

    if not questions:
        raise InvalidRequest("questions 不能为空")
    if len(questions) > config.max_questions:
        raise InvalidRequest(
            f"问题数 {len(questions)} 超过 MAX_QUESTIONS={config.max_questions}"
        )

    for qid, question in questions.items():
        if not isinstance(question, dict):
            raise InvalidRequest(f"问题 {qid} 必须是对象")
        qtype = question.get("type")
        if qtype not in QUESTION_TYPES:
            raise InvalidRequest(
                f"问题 {qid} 的 type='{qtype}' 不是 "
                f"{'/'.join(QUESTION_TYPES)} 之一"
            )
        if not str(question.get("instructions", "")).strip():
            raise InvalidRequest(f"问题 {qid} 缺少 instructions")
        criteria = question.get("criteria")
        if qtype in ("choice", "score"):
            if criteria is None:
                raise InvalidRequest(f"问题 {qid} 缺少 criteria")
            option_count = len(criteria)
            if option_count < 2:
                raise InvalidRequest(f"问题 {qid} 至少需要 2 个选项")
            if option_count > min(config.max_options, MAX_CHOICE_OPTIONS):
                raise InvalidRequest(
                    f"问题 {qid} 的选项数 {option_count} 超过上限 "
                    f"{min(config.max_options, MAX_CHOICE_OPTIONS)}"
                )


def _state_chars(state) -> int:
    if isinstance(state, (dict, list)):
        try:
            return len(json.dumps(state, ensure_ascii=False))
        except (TypeError, ValueError):
            return 0
    return len(str(state))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_manifest(model_dir: Path, files: list[str], revision: str | None) -> dict:
    """构建期生成摘要清单：逐文件 sha256 + 权重版本。"""
    manifest = {name: sha256_file(model_dir / name) for name in files}
    if revision:
        manifest["revision"] = revision
    return manifest


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(Path(root) / name)
            except OSError:
                continue
    return total
