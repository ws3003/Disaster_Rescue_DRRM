"""X4/E6 组合根（`src/x4/composition.py`）。

把 X4 云边推理接入既有感知链路：装配 `CloudYoloClient` / `X4DetectionService` /
`EdgePerception`，让 `EdgePerception.det` 使用 X4 检测后端；本地 Detector 仅在
`X4DetectionService` 云端失败回退时被调用（不额外旁路、不双跑检测）。

**职责边界**：
- 只做依赖装配（组合根），不重写检测/风险/状态/排序/Agent。
- 姿态/状态/风险/排序由 `EdgePerception` 生产默认（冻结模块）承担，云端只给检测。
- 云端失败回退语义全部由 `X4DetectionService` 既有 `_edge` 处理，**不新增第二套重试/回退状态机**。
- Token 仅经 env（`X4_API_TOKEN`）读取，不写进源码/日志/配置。
- 构造阶段不触发真实模型推理。

安全：`edge_detect` 为懒加载单例本地 Detector；云端成功时不会被调用。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from src.x4.config import X4Config, load_x4_config
from src.x4.http_transport import new_http_client
from src.x4.service import X4DetectionService


class X4CompositionError(RuntimeError):
    """组合根装配失败（配置/依赖不可用；不吞异常）。"""


def _build_detector():
    """懒加载单例本地 `Detector`，作为 X4 云端失败回退用的 edge 检测器。

    仅在被调用时才构造（回退时），不在此处提前加载模型。
    """
    try:
        from src.detector import Detector
    except Exception as exc:  # noqa: BLE001
        raise X4CompositionError(f"edge detector import failed: {type(exc).__name__}") from exc
    return Detector()


def make_edge_detector() -> Callable[[Any], List[Any]]:
    """返回 `edge_detect` 回调：懒加载本地 Detector，调用其 `detect_frame(frame)`。

    组合根只在此处注入；`X4DetectionService` 云端失败时经 `_edge` 调用一次，
    正常帧云端成功时不会被调用（不双跑）。
    """
    _det: Optional[Any] = None

    def _fallback(frame: Any) -> List[Any]:
        nonlocal _det
        if _det is None:
            _det = _build_detector()
        return _det.detect_frame(frame)

    return _fallback


def build_x4_perception(
    *,
    cfg: Optional[X4Config] = None,
    cloud: Optional[Any] = None,
    edge_detect: Optional[Callable[[Any], List[Any]]] = None,
    db: Optional[Any] = None,
    record: bool = True,
    det_only: bool = False,
) -> Any:
    """装配并返回可被 `edge_client` 直接使用的感知组件（`EdgePerception`）。

    参数（均可在测试中注入替换）：
    - `cfg`：X4 配置；默认 `load_x4_config()`。
    - `cloud`：`CloudYoloClient`；默认经 `new_http_client(cfg)` 构造（真实 HTTP transport）。
    - `edge_detect`：云端失败回退的本地检测回调；默认 `make_edge_detector()`（懒加载本地 Detector）。
    - `db`：可选，供 `X4DetectionService._record` 写 agent_logs。
    - `record`：是否记录推理日志（默认 True）。
    - `det_only`：True 时返回 `X4DetectionService` 本身（供单测/内部使用），False 时返回
      注入 X4 的 `EdgePerception`（供 edge_client 使用）。

    若 `cfg` 未开启云端（`enabled=False` 或 `cloud_endpoint` 为空），`X4DetectionService`
    自动进入 EDGE_ONLY，每帧走本地回退——与原 `EdgePerception()` 默认本地行为一致。
    """
    cfg = cfg if cfg is not None else load_x4_config()
    if cloud is None:
        # 仅当配置可用且 endpoint 非空时才建真实 client；否则保持 None（进入 edge-only）。
        if getattr(cfg, "enabled", False) and str(getattr(cfg, "cloud_endpoint", "") or "").strip():
            cloud = new_http_client(cfg)
        else:
            cloud = None
    edge = edge_detect if edge_detect is not None else make_edge_detector()

    svc = X4DetectionService(cfg=cfg, cloud=cloud, edge_detect=edge, db=db, record=record)

    if det_only:
        return svc

    from src.edge_perception import EdgePerception
    return EdgePerception(det=svc)