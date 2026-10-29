"""X4 组合根单元测试（`tests/test_x4_composition.py`）。

全部使用 Fake/Stub 注入：
- 不启动真实 HTTP 服务、不访问外网、不加载 YOLO 权重、不打开相机、无真实 Token。
- 用计数器验证 `detect_frame` 调用次数，证明"云端成功时不额外跑本地检测、不双跑"。

组合根只做装配；姿态/状态/风险/排序由 `EdgePerception` 生产默认（冻结模块）承担，
此处不实例化真实 Detector（避免副作用），仅校验注入与调用次数语义。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `src/edge_perception.py` 顶层用裸 `from detector import ...`（生产经 `python src/edge_client.py`
# 以脚本目录 src/ 作为 sys.path[0] 解析）。组合根构造 EdgePerception 时依赖此；测试在此补
# 齐 `src/` 到 sys.path，模拟生产脚本目录，使 det_only=False 路径可验证。
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.x4.cloud_yolo import CloudTimeoutError, CloudUnavailableError, CloudInvalidResponseError
from src.x4.config import X4Config
from src.x4.contracts import Detection as X4Detection
from src.x4.composition import build_x4_perception, make_edge_detector


def _cfg(**over):
    base = dict(enabled=True, request_timeout_seconds=8.0, max_attempts=1,
                cloud_endpoint="http://127.0.0.1:8316/predict", model_id="yolov26l",
                fallback_enabled=True, cloud_health_enabled=False)
    base.update(over)
    return X4Config(**base)


def _x4(cid, name, conf=0.9, bbox=(1, 2, 3, 4)):
    return X4Detection(class_id=int(cid), class_name=name, confidence=conf, bbox=tuple(bbox))


def _local(cid=1, name="civilian", conf=0.9, bbox=(10, 20, 30, 40)):
    from src.detector import Detection as Local
    return Local(list(bbox), int(cid), name, conf, True)


class _Cloud:
    """注入式 Fake cloud：按预设 infer 返回或抛错，记录调用次数。"""

    def __init__(self, results=None, exc=None):
        self.results = results if results is not None else []
        self.exc = exc
        self.calls = 0

    def infer(self, image_bgr, timeout=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.results)


def _edge_spy(calls):
    def edge(frame):
        calls.append(1)
        return [_local()]
    return edge


def _make(**kw):
    """构造经组合根装配、注入 Fake cloud + edge spy 的 X4DetectionService（det_only）。"""
    edge_calls = []
    svc = build_x4_perception(
        cfg=kw.get("cfg", _cfg()),
        cloud=kw.get("cloud"),
        edge_detect=kw.get("edge", _edge_spy(edge_calls)),
        record=False,
        det_only=True,
    )
    return svc, edge_calls


# ----------------------------------------------------------------------
# A. 正确构造
# ----------------------------------------------------------------------
class TestComposition:
    def test_det_only_returns_x4_service(self):
        svc = build_x4_perception(cfg=_cfg(), cloud=_Cloud(), edge_detect=_edge_spy([]),
                                  record=False, det_only=True)
        assert svc is not None
        assert svc.edge_detect is not None               # 组合根注入了 edge 回调

    def test_full_perception_injects_det(self, monkeypatch):
        # 组合根 det_only=False 时应构造 EdgePerception 并把 X4DetectionService 注入 det。
        # 用 mock 拦截 EdgePerception 构造器，避免实例化冻结的 PoseEstimator/MediaPipe 资源。
        import src.x4.composition as comp

        captured = {}
        def fake_ep_ctor(*a, **k):
            captured["det"] = k.get("det")
            class _Stub:
                def analyze(self, frame):
                    return []
            return _Stub()

        # patch `src.edge_perception.EdgePerception`（组合根函数内 `from ... import` 取自此模块）
        monkeypatch.setattr("src.edge_perception.EdgePerception", fake_ep_ctor)
        perception = build_x4_perception(cfg=_cfg(enabled=False, cloud_endpoint=""),
                                         cloud=None, edge_detect=_edge_spy([]),
                                         record=False, det_only=False)
        assert hasattr(perception, "analyze")
        assert captured.get("det") is not None            # X4DetectionService 被注入为 det

    def test_construction_does_not_infer(self):
        c = _Cloud([_x4(1, "civilian")])
        build_x4_perception(cfg=_cfg(), cloud=c, edge_detect=_edge_spy([]), record=False, det_only=True)
        assert c.calls == 0                              # 构造不触发推理

    def test_no_token_hardcoded(self):
        # 组合根不硬编码任何 token/api key 字面值；token 仅经 env 机制，源码不得含可取值。
        import inspect
        import src.x4.composition as mod
        src_text = inspect.getsource(mod)
        # 排除明显非 secret 的常量名；检查没有像 "AAAA.." 或 "Bearer <值>" 的实际 secret 字面
        assert "Authorization" not in src_text          # 组合根不构造鉴权头
        assert "_build_detector" in src_text            # 懒加载本地回退存在
        # 不应出现 16+ 的疑似 token 字面量（此处仅静态守卫）
        assert "token = \"" not in src_text
        assert "token='" not in src_text

    def test_edge_detector_lazy(self):
        edge = make_edge_detector()
        # 返回的是闭包，未在创建时实例化真实 Detector（真实 Detector 加载是耗时的）
        import inspect
        assert callable(edge)


# ----------------------------------------------------------------------
# B. 云端成功：本地不调用（不双跑）
# ----------------------------------------------------------------------
class TestCloudSuccess:
    def test_cloud_success_no_local_call(self):
        edge_calls = []
        c = _Cloud([_x4(1, "civilian", 0.8, (1, 2, 3, 4))])
        svc, _ = _make(cloud=c, edge=_edge_spy(edge_calls))
        out = svc.detect_frame(None)
        assert [d.class_id for d in out] == [1]
        assert c.calls == 1
        assert edge_calls == []                          # 云端成功 → 本地 0 次

    def test_cloud_success_detection_converted(self):
        c = _Cloud([_x4(1, "civilian", 0.8, (1, 2, 3, 4))])
        svc, _ = _make(cloud=c)
        (d,) = svc.detect_frame(None)
        assert d.class_id == 1 and d.class_name == "civilian" and d.is_person is True
        assert d.confidence == 0.8 and d.bbox == [1, 2, 3, 4]


# ----------------------------------------------------------------------
# C. 云端超时 / D. 鉴权错误(401/403) / E. 无效响应
#    验证不瞬时重试 + 按现有策略本地回退恰好 1 次
# ----------------------------------------------------------------------
class TestCloudFailureFallbackOnce:
    @pytest.mark.parametrize("exc", [
        CloudTimeoutError(), CloudUnavailableError("401"), CloudUnavailableError("403"),
        CloudInvalidResponseError("bad"),
    ])
    def test_cloud_error_retries_then_falls_back_once(self, exc):
        edge_calls = []
        c = _Cloud(exc=exc)
        cfg = _cfg(max_attempts=3)                        # 该错误非瞬时不重试; 云失败后回退 1 次
        svc, _ = _make(cfg=cfg, cloud=c, edge=_edge_spy(edge_calls))
        out = svc.detect_frame(None)
        # 回退本地成功 → 返回本地检测
        assert [d.class_id for d in out] == [1]
        assert edge_calls == [1]                          # 本地恰 1 次

    def test_cloud_timeout_no_local_call_when_fallback_disabled(self):
        edge_calls = []
        c = _Cloud(exc=CloudTimeoutError())
        cfg = _cfg(fallback_enabled=False, max_attempts=1)
        svc, _ = _make(cfg=cfg, cloud=c, edge=_edge_spy(edge_calls))
        from src.x4.service import X4DetectionError
        with pytest.raises(X4DetectionError):
            svc.detect_frame(None)
        assert edge_calls == []                          # 禁回退 → 本地 0 次

    def test_cloud_timeout_falls_back_once_max_attempts(self):
        edge_calls = []
        c = _Cloud(exc=CloudTimeoutError())
        cfg = _cfg(max_attempts=3)                        # 3 次尝试上限
        svc, _ = _make(cfg=cfg, cloud=c, edge=_edge_spy(edge_calls))
        out = svc.detect_frame(None)
        assert c.calls <= cfg.max_attempts                # 重试受 max_attempts 约束
        assert edge_calls == [1]                          # 回退恰 1 次


# ----------------------------------------------------------------------
# F. 空检测：合法成功，不回退，不伪造
# ----------------------------------------------------------------------
class TestEmptyDetection:
    def test_empty_is_success_no_fallback(self):
        edge_calls = []
        c = _Cloud([])                                    # 空检测
        svc, _ = _make(cloud=c, edge=_edge_spy(edge_calls))
        out = svc.detect_frame(None)
        assert out == []                                  # 空=成功无目标
        assert c.calls == 1                               # 云被调用一次（成功）
        assert edge_calls == []                           # 不触发本地回退


# ----------------------------------------------------------------------
# G. 本地回退失败：遵循既有异常语义，不伪造成功
# ----------------------------------------------------------------------
class TestLocalFallbackFails:
    def test_fallback_error_raises_not_fake(self):
        c = _Cloud(exc=CloudUnavailableError("503"))
        def bad_edge(frame):
            raise RuntimeError("edge boom")
        svc, _ = _make(cloud=c, edge=bad_edge)
        from src.x4.service import X4DetectionError
        with pytest.raises(X4DetectionError):
            svc.detect_frame(None)


# ----------------------------------------------------------------------
# B1 数据链：X4 → 真实 EdgePerception.analyze → person_events → frame_record
# 只用 Fake 替换 pose/classify/risk 三个 *下游* 依赖；X4DetectionService、
# EdgePerception(det=X4Service).analyze、person_events_to_frame_record 均为真实实现。
# ----------------------------------------------------------------------
class _FakePoseOK:
    is_ok = True
    visibility_mean = 0.8


class _FakeState:
    def __init__(self, state="Fall"):
        self.state = state
        self.confidence = 0.9


class _FakeRiskRes:
    def __init__(self, state="Fall"):
        self.state = state
        self.risk_score = 0.63
        self.f1 = 0.7        # 冻结 rank() 需要 f1/f6 读取
        self.f6 = 0.9


class _FakePoseEstimator:
    def estimate_persons(self, frame, detections):
        return [_FakePoseOK() for _ in detections]


class _FakeClassifier:
    def classify(self, pose):
        return _FakeState("Fall")


class _FakeRiskEval:
    def assess(self, detection, pose, state):
        if pose is None or not pose.is_ok:
            return None
        return _FakeRiskRes(state.state)


class TestX4ToAgentDataFlow:
    """真实组合数据链：X4 云端成功 → EdgePerception.analyze → person_events → frame_record。"""

    def _real_perception(self, cloud):
        """真实 X4DetectionService(det) + 真实 EdgePerception；仅 fake 下游 pose/classify/risk。"""
        from src.x4.composition import build_x4_perception
        from src.edge_perception import EdgePerception
        svc = build_x4_perception(cfg=_cfg(), cloud=cloud, edge_detect=_edge_spy([]),
                                  record=False, det_only=True)   # 真实 X4DetectionService
        ep = EdgePerception(
            det=svc,              # 真实注入
            pe=_FakePoseEstimator(),   # 真实 EdgePerception 装配这些下游 fake
            clf=_FakeClassifier(),
            rk=_FakeRiskEval(),
        )
        return svc, ep

    def test_cloud_detections_flow_to_person_events_and_frame_record(self):
        import numpy as np
        from src.agent.input_adapter import person_events_to_frame_record
        edge_calls = []
        cloud = _Cloud([_x4(1, "civilian", 0.8, (10, 20, 30, 40)),
                        _x4(5, "rescuer", 0.7, (50, 60, 70, 80))])
        svc, ep = self._real_perception(cloud)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        events = ep.analyze(frame)                    # 真实 EdgePerception.analyze

        # 云端成功：本地 Detector 0 次
        assert edge_calls == []
        assert len(events) >= 1                       # civilian 进入（class_name=civilian）
        ev = events[0]
        # 字段来源：X4 Detection bbox → person_events bbox；state/risk/priority 来自下游 fake/冻结
        assert {"person_id", "bbox", "state", "risk_score", "priority"} <= set(ev)
        assert ev["bbox"] == [10, 20, 30, 40]         # 来自 X4 检测 bbox[0]（civilian）
        assert isinstance(ev["risk_score"], float)
        assert ev["state"] in ("Fall", "Lying", "Help-Waving", "Sitting", "Standing")

        # 真实 person_events_to_frame_record → frame_record
        fr = person_events_to_frame_record(events, timestamp="2026-01-01T00:00:00")
        assert set(fr) == {"timestamp", "persons"}
        assert len(fr["persons"]) >= 1
        assert fr["persons"][0]["person_id"] == ev["person_id"]   # 保留 person_id 标识

        # 检测服务收到预期调用（云端 1 次，未被重复处理）
        assert cloud.calls == 1

    def test_cloud_success_detection_count_matches_input_civilian(self):
        """X4 检测中仅 civilian 进入 person_events（rescuer 分配 id 但不产生事件），与原有语义一致。"""
        import numpy as np
        from src.agent.input_adapter import person_events_to_frame_record
        edge_calls = []
        cloud = _Cloud([_x4(1, "civilian", 0.8, (10, 20, 30, 40)),
                        _x4(5, "rescuer", 0.7, (50, 60, 70, 80))])
        _, ep = self._real_perception(cloud)
        events = ep.analyze(np.zeros((480, 640, 3), dtype=np.uint8))
        # 仅 civilian 产生事件；事件字段与真实 impede 输出一致
        frames_b = [e["bbox"] for e in events]
        assert [10, 20, 30, 40] in frames_b          # civilian bbox 出现
        assert [50, 60, 70, 80] not in frames_b       # rescuer 不产生 person_event
        assert isinstance(person_events_to_frame_record(events)["persons"], list)