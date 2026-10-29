"""X4/E6 动态网络切换服务（第一阶段：离线可测，不接真实 HTTP）。

`X4DetectionService.detect_frame(frame) -> List[Detector.Detection]` 是实现 EdgePerception 的
`det` 契约（`det.detect_frame(frame)`）的外围适配层——**不改 detector/pose/state/risk/ranking/agent 内部**。

设计要点：
- 四状态机 `NetworkStateMachine`：EDGE_ONLY / CLOUD_PREFERRED / CLOUD_COOLDOWN / RECOVERY_PROBE。
- **非阻塞探测**：`detect_frame` 从不调用探测；探测通过独立的 `tick_probe(probe)` 在帧路径之外调度
  （组合根/定时在真实阶段接入）。此分离从结构上保证"正常帧不等待远端探测"。
- 云/边选择：`wants_cloud()` 仅当 CLOUD_PREFERRED；其余/冷却/恢复探测态直接走本地。
- 结果转换：云端 X4 Detection → 本地 `Detector.Detection`（`is_person = class_id ∈ {1,5}`）。
- 空检测=正常 `[]`（不记故障）；云端失败→本帧回退本地；云端与本地均失败→抛 `X4DetectionError`（不伪造）。
- 日志：复用 `record_inference`（agent_logs），写失败不抛、不影响结果。
- 单线程逐帧串行；`now`/probe 可注入，可离线确定测试。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, List, Optional, Tuple

from src.x4.cloud_yolo import CloudError, CloudYoloClient
from src.x4.config import X4Config
from src.x4.contracts import EDGE_MODEL_ID, InferenceResult, Detection
from src.x4.logging import record_inference

_log = logging.getLogger("x4.service")

# 人员类别（已核实：本地与云端同属 6 类 urban-disaster 体系；dataset data.yaml + config/config.yaml）
PERSON_CLASS_IDS = frozenset({1, 5})

EDGE_ONLY = "edge_only"
CLOUD_PREFERRED = "cloud_preferred"
CLOUD_COOLDOWN = "cloud_cooldown"
RECOVERY_PROBE = "recovery_probe"


def _pos_int(name: str, v: Any) -> int:
    """正整数（显式拒绝 bool，因 bool 是 int 子类）。"""
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{name} 必须为正整数, 得到 {v!r}")
    return int(v)


def _finite_pos(name: str, v: Any) -> float:
    """有限且严格大于零的数值（拒绝 bool/NaN/±Inf/字符串冒充）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} 必须为数值, 得到 {v!r}")
    f = float(v)
    if not math.isfinite(f) or f <= 0:
        raise ValueError(f"{name} 必须为有限正数值, 得到 {v!r}")
    return f


class X4DetectionError(Exception):
    """云端与本地检测均失败（明确传播，不伪造结果）。"""


class NetworkStateMachine:
    """E6 动态网络状态机（单线程帧循环串行；注入 now 以离线测试）。

    探测由帧路径之外的 `probe()` 显式驱动；本机不启动线程/后台任务。
    """

    def __init__(
        self,
        *,
        fail_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        probe_interval_seconds: float = 10.0,
        restore_success: int = 2,
        now: Callable[[], float] = time.monotonic,
    ):
        # 构造参数校验：非法值在构造阶段即抛 ValueError（不等到运行期）。
        self.fail_threshold = _pos_int("fail_threshold", fail_threshold)
        self.restore_success = _pos_int("restore_success", restore_success)
        self.cooldown_seconds = _finite_pos("cooldown_seconds", cooldown_seconds)
        self.probe_interval = _finite_pos("probe_interval_seconds", probe_interval_seconds)
        self._now = now
        self.state = EDGE_ONLY
        self.consecutive_failures = 0
        self.cooldown_until: Optional[float] = None
        self.probe_due_at: Optional[float] = None
        self.consecutive_probe_ok = 0
        self._probe_in_flight = False

    # ---- 基础 ----
    def enter_edge_only(self) -> None:
        """云端禁用/未配置：直接 EDGE_ONLY，不探测。"""
        self.state = EDGE_ONLY
        self.consecutive_failures = 0
        self.cooldown_until = None
        self.probe_due_at = None
        self.consecutive_probe_ok = 0

    def wants_cloud(self) -> bool:
        return self.state == CLOUD_PREFERRED

    # ---- 云端结果反馈 ----
    def on_cloud_success(self) -> None:
        if self.state == CLOUD_PREFERRED:
            self.consecutive_failures = 0

    def on_cloud_failure(self) -> bool:
        """云端失败计数；达阈值进入冷却。返回是否已进入冷却。"""
        if self.state != CLOUD_PREFERRED:
            return False
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.fail_threshold:
            self._enter_cooldown()
            return True
        return False

    def _enter_cooldown(self) -> None:
        self.state = CLOUD_COOLDOWN
        self.cooldown_until = self._now() + self.cooldown_seconds
        self.probe_due_at = None
        self.consecutive_probe_ok = 0

    # ---- 探测（帧路径之外显式驱动） ----
    def probe(self, probe_fn: Callable[[], bool]) -> Optional[str]:
        """推进冷却/恢复探测。返回状态字用于测试/诊断；不阻塞帧路径。"""
        if self.state == EDGE_ONLY:
            return None                       # 配置禁用：不探测
        if self.state not in (CLOUD_COOLDOWN, RECOVERY_PROBE):
            return None
        now = self._now()
        if self.state == CLOUD_COOLDOWN:
            if self.cooldown_until is not None and now < self.cooldown_until:
                return "in_cooldown"          # 冷却未结束
            self.state = RECOVERY_PROBE                      # 冷却结束 → 开始探测
            self.probe_due_at = now + self.probe_interval
            self.consecutive_probe_ok = 0
        if self.probe_due_at is not None and now < self.probe_due_at:
            return "deferred"                 # 未到探测间隔
        if self._probe_in_flight:
            return "in_probe"                 # 防止并发/重复探测
        self._probe_in_flight = True
        try:
            ok = bool(probe_fn())
        finally:
            self._probe_in_flight = False
        self.probe_due_at = self._now() + self.probe_interval
        if ok:
            self.consecutive_probe_ok += 1
            if self.consecutive_probe_ok >= self.restore_success:
                self.state = CLOUD_PREFERRED                # 达标恢复云端优先
                self.consecutive_failures = 0
                self.consecutive_probe_ok = 0
                return "restored"
            return "probe_ok"
        self.consecutive_probe_ok = 0
        return "probe_failed"


class X4DetectionService:
    """外围适配器：状态机选路 + 云/边转换 + 日志（实现 EdgePerception 的 det 契约）。"""

    def __init__(
        self,
        *,
        cfg: X4Config,
        cloud: Optional[CloudYoloClient] = None,
        edge_detect: Optional[Callable[[Any], List[Any]]] = None,
        db: Optional[Any] = None,
        fail_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        probe_interval_seconds: float = 10.0,
        restore_success: int = 2,
        now: Callable[[], float] = time.monotonic,
        record: bool = True,
    ):
        self.cfg = cfg
        self.cloud = cloud
        self.edge_detect = edge_detect
        self.db = db
        self.record = record
        self._now = now
        self.machine = NetworkStateMachine(
            fail_threshold=fail_threshold, cooldown_seconds=cooldown_seconds,
            probe_interval_seconds=probe_interval_seconds, restore_success=restore_success, now=now,
        )
        self._cloud_viable = bool(
            getattr(cfg, "enabled", False) and str(getattr(cfg, "cloud_endpoint", "") or "").strip()
            and self.cloud is not None
        )
        if self._cloud_viable:
            self.machine.state = CLOUD_PREFERRED
        else:
            self.machine.enter_edge_only()

    # ---- 非阻塞探测（帧路径外显式调度） ----
    def tick_probe(self, probe_fn: Callable[[], bool]) -> Optional[str]:
        """同步探测的调度边界：捕获探测异常→当作探测失败（记录一次、清成功计数、定下次、返 "probe_failed"）。

        经标准 logging 记录（不写 agent_logs）；`_probe_in_flight` 由 machine.probe 的 finally 清理；
        不实现后台线程/异步调度器（phase-2 范围）。
        """
        try:
            return self.machine.probe(probe_fn)
        except Exception as exc:  # noqa: BLE001——探测异常不得穿透正常帧路径
            _log.warning("x4 probe failed: %s", exc)
            self.machine.consecutive_probe_ok = 0
            self.machine.probe_due_at = self._now() + self.machine.probe_interval
            return "probe_failed"

    # ---- 核心 ----
    def detect_frame(self, frame: Any) -> List[Any]:
        """返回当前帧下游可用的 `Detector.Detection` 列表。"""
        if self._cloud_viable and self.machine.wants_cloud():
            t0 = self._now()
            try:
                x4 = self.cloud.infer(frame, timeout=self.cfg.request_timeout_seconds)
                local = self._to_local_detections(x4)
                self.machine.on_cloud_success()
                self._record(result=self._result(provider="cloud", success=True, detections=x4,
                                                 latency=self._ms(t0), fallback_used=False,
                                                 cloud_error=""), state=self.machine.state)
                return local
            except CloudError as exc:
                self.machine.on_cloud_failure()
                cloud_error = exc.code
            except Exception:  # noqa: BLE001——云端异常不得吞成"空检测成功"
                self.machine.on_cloud_failure()
                cloud_error = "cloud_invalid_response"
            if not getattr(self.cfg, "fallback_enabled", True):
                # 禁回退：不调用本地推理，记录失败状态与日志，再抛既有异常（不伪造空列表）
                self._record(result=self._result(provider="cloud", success=False, detections=(),
                                                 latency=self._ms(t0), fallback_used=False,
                                                 cloud_error=cloud_error), state=self.machine.state)
                raise X4DetectionError(f"cloud failed and fallback disabled: {cloud_error}")
            # 云端失败 → 本帧回退本地
            local = self._edge(frame, fallback_used=True, cloud_error=cloud_error, t0=t0, attempted=True)
            return local
        if not self._cloud_viable:
            return self._edge(frame, fallback_used=False, cloud_error="", attempted=False)
        # 冷却/恢复探测态（云端可用但状态不允许）→ 直接本地
        return self._edge(frame, fallback_used=False, cloud_error="", attempted=False)

    def _edge(self, frame, *, fallback_used: bool, cloud_error: str,
              t0: Optional[float] = None, attempted: bool = False) -> List[Any]:
        t0 = t0 if t0 is not None else self._now()
        if self.edge_detect is None:
            raise X4DetectionError("edge detector not configured and cloud path unavailable/failed")
        try:
            local = list(self.edge_detect(frame))
        except Exception as exc:  # noqa: BLE001
            raise X4DetectionError(f"cloud and edge detection both failed: {exc}") from exc
        self._record(result=self._result(provider="edge", success=True, detections=tuple(local),
                                         latency=self._ms(t0), fallback_used=fallback_used,
                                         cloud_error=cloud_error), state=self.machine.state)
        return local

    def _result(self, *, provider: str, success: bool, detections: Tuple, latency: float,
                fallback_used: bool, cloud_error: str) -> InferenceResult:
        model_id = self.cfg.model_id if provider == "cloud" else EDGE_MODEL_ID
        return InferenceResult(
            success=success, provider=provider, model_id=model_id,
            detections=tuple(detections), latency_ms=latency,
            fallback_used=fallback_used, error_code=cloud_error,
        )

    def _record(self, *, result: InferenceResult, state: str) -> None:
        if not self.record or self.db is None:
            return
        record_inference(self.db, result, input_meta={"state": state})

    @staticmethod
    def _to_local_detections(x4_seq: Tuple[Detection, ...]) -> List[Any]:
        from src.detector import Detection as LocalDetection
        out: List[Any] = []
        for d in x4_seq:
            out.append(LocalDetection(
                bbox=list(d.bbox),
                class_id=int(d.class_id),
                class_name=str(d.class_name),
                confidence=float(d.confidence),
                is_person=bool(int(d.class_id) in PERSON_CLASS_IDS),
            ))
        return out

    def _ms(self, t0: float) -> float:
        return round((self._now() - t0) * 1000.0, 1)