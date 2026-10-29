"""V4.4 X4 隔离端到端测试（`tests/p1/test_edge_client_x4_e2e.py`）。

**端到端接收测试，但不触发生产副作用**：
- 使用真实 `run_loop`、真实 `EdgePerception`（det=真实 X4DetectionService + Fake cloud）、
  真实 `person_events_to_frame_record`，验证生产组件组合方式。
- 用 FakeCam / Stub Agent / Fake cloud / Fake edge 隔离外部副作用：
  不开相机、不向生产 API 上传、不写生产 DB、不落生产快照/JSONL、不实例化真实 Agent。
- 不启动真实 X4 服务、不加载模型、不使用 GPU —— 云端行为全部由 Fake cloud 模拟。

依据：G4-A 设计。本测试区分"Fake 端到端"与"真实 GPU HTTP"（后者属 G4-B2）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 `src/edge_perception.py` 的裸 `from detector import` 可解析（同 test_x4_composition）。
# 本文件位于 `<root>/tests/p1/`：parents[0]=tests/p1, [1]=tests, [2]=repo 根。
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402

from src.agent.input_adapter import person_events_to_frame_record  # noqa: E402  (真实适配器)
from src.x4.cloud_yolo import (  # noqa: E402
    CloudTimeoutError,
    CloudUnavailableError,
)
from src.x4.config import X4Config  # noqa: E402
from src.x4.contracts import Detection as X4Detection  # noqa: E402
from src.x4.composition import build_x4_perception  # noqa: E402


# ----------------------------------------------------------------------
# 隔离替身（Stub / Fake）
# ----------------------------------------------------------------------
class FakeCam:
    """Stub 相机：产出固定测试帧，实现 run_loop 使用的 `capture()`。"""

    def __init__(self):
        self._f = np.zeros((480, 640, 3), dtype=np.uint8)

    def capture(self):
        import numpy as np
        return self._f.copy()

    def close(self):
        pass


class StubUploader:
    """Stub 上传器：不访问网络，仅记录调用。返回 (status, body)。"""

    def __init__(self):
        self.calls = 0
        self.last_snapshot_url = None

    def upload(self, jpeg, person_events=None, **k):
        self.calls += 1
        return 200, {"code": "OK", "message": "ok",
                     "data": {"result": 1}, "meta": {}}


class StubAgent:
    """Stub Agent：只记录 process_frame 输入，不实例化真实 Planner/Executor。"""

    def __init__(self):
        self.frames = []

    def process_frame(self, frame_record, context=None):
        self.frames.append(frame_record)
        return {"result": "ok", "task_id": context.get("task_id") if context else None}


class FakeCloud:
    """Fake 云端：按预设 infer 返回检测或抛错，记录调用次数。"""

    def __init__(self, detections=None, exc=None):
        self.detections = detections if detections is not None else []
        self.exc = exc
        self.calls = 0

    def infer(self, image_bgr, timeout=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.detections)


def _edge_spy(calls):
    """Fake 本地 Detector：云端失败回退时调用，计数。复现 8B 系列回退计数断言。"""
    from src.detector import Detection as Local

    def edge(frame):
        calls.append(1)
        return [Local([10, 20, 30, 40], 1, "civilian", 0.9, True)]
    return edge


# 姿态/状态/风险 fake（避免 MediaPipe；供真实 EdgePerception 装配为下游）
class _PoseOK:
    is_ok = True
    visibility_mean = 0.8


class _State:
    def __init__(self, state="Fall"):
        self.state = state
        self.confidence = 0.9


class _RiskRes:
    def __init__(self, state="Fall"):
        self.state = state
        self.risk_score = 0.63
        self.f1 = 0.7
        self.f6 = 0.9


class _FakePoseEstimator:
    def estimate_persons(self, frame, detections):
        return [_PoseOK() for _ in detections]


class _FakeClassifier:
    def classify(self, pose):
        return _State("Fall")


class _FakeRiskEval:
    def assess(self, detection, pose, state):
        if pose is None or not pose.is_ok:
            return None
        return _RiskRes(state.state)


def _build_perception(cfg, cloud, edge_calls):
    """真实 X4DetectionService + 真实 EdgePerception；仅 fake 下游 pose/classify/risk。"""
    from src.edge_perception import EdgePerception
    svc = build_x4_perception(cfg=cfg, cloud=cloud, edge_detect=_edge_spy(edge_calls),
                              record=False, det_only=True)          # 真实 X4DetectionService
    return EdgePerception(det=svc, pe=_FakePoseEstimator(), clf=_FakeClassifier(),
                          rk=_FakeRiskEval())


def _cfg(**over):
    base = dict(enabled=True, request_timeout_seconds=8.0, max_attempts=1,
                cloud_endpoint="http://127.0.0.1:8316/predict", model_id="yolov26l",
                fallback_enabled=True, cloud_health_enabled=False)
    base.update(over)
    return X4Config(**base)


@pytest.fixture(autouse=True)
def _no_snapshot_side_effect(monkeypatch):
    """阻断 run_loop 内 HIGH 事件写盘（不产生生产快照）。"""
    monkeypatch.setattr("src.snapshot.save_snapshot", lambda *a, **k: None)


def _run_once(perception, agent):
    """经真实 run_loop 跑一次（max_uploads=1, sleep no-op, 注入 stub 上传器）。"""
    import src.edge_client as ec
    uploader = StubUploader()
    cam = FakeCam()
    rc = ec.run_loop(uploader, cam, interval=0.0, quality=90, max_uploads=1,
                     sleep=lambda s: None, perception=perception, agent=agent)
    return rc, uploader


# ----------------------------------------------------------------------
# 端到端场景
# ----------------------------------------------------------------------
class TestX4E2E:
    def test_a_cloud_success_detections_reach_agent(self):
        """A+F：云端成功 → X4 检测进 person_events → frame_record → Stub Agent；本地不被调用。"""
        edge_calls = []
        cloud = FakeCloud([X4Detection(1, "civilian", 0.8, (10, 20, 30, 40))])
        perception = _build_perception(_cfg(), cloud, edge_calls)
        agent = StubAgent()
        rc, uploader = _run_once(perception, agent)
        assert rc == 1
        assert cloud.calls == 1                    # 云端被调一次
        assert edge_calls == []                    # 云成功 → 本地 0 次
        assert len(agent.frames) == 1              # Agent 收到 1 个 frame_record
        fr = agent.frames[0]
        assert set(fr) == {"timestamp", "persons"}
        assert len(fr["persons"]) >= 1             # X4 检测进入 person_events
        assert fr["persons"][0]["bbox"] == [10, 20, 30, 40]  # X4 检测 bbox 保留
        assert uploader.calls == 1                 # 上传被调（Stub，不触网）

    def test_b_cloud_empty_detections_is_success(self):
        """B：云端空检测 = 成功，不触发本地回退。"""
        edge_calls = []
        cloud = FakeCloud([])                       # 云端空检测
        perception = _build_perception(_cfg(), cloud, edge_calls)
        agent = StubAgent()
        rc, _ = _run_once(perception, agent)
        assert rc == 1
        assert cloud.calls == 1                     # 云被调（视为成功）
        assert edge_calls == []                     # 空检测不触发本地回退
        for fr in agent.frames:
            assert fr["persons"] == []              # 无 civilian → 空 persons

    def test_c_cloud_timeout_falls_back_once(self):
        """C：云端失败(超时) → 本地 Detector 回退恰一次，回退结果进感知链路。"""
        edge_calls = []
        cloud = FakeCloud(exc=CloudTimeoutError())
        perception = _build_perception(_cfg(), cloud, edge_calls)
        agent = StubAgent()
        rc, _ = _run_once(perception, agent)
        assert rc == 1
        assert edge_calls == [1]                    # 本地回退恰一次
        assert len(agent.frames) == 1               # 回退检测进入 person_events
        assert agent.frames[0]["persons"]           # 非空（本地 civilian）

    def test_d_auth_failure_falls_back(self):
        """D：鉴权失败(403/401 语义映射到 CloudUnavailableError) → 本地回退恰一次。"""
        edge_calls = []
        cloud = FakeCloud(exc=CloudUnavailableError("401"))
        perception = _build_perception(_cfg(), cloud, edge_calls)
        agent = StubAgent()
        rc, _ = _run_once(perception, agent)
        assert edge_calls == [1]                    # 鉴权失败 → 本地回退恰一次
        assert rc == 1

    def test_e_uses_stub_uploader_and_no_vision(self, monkeypatch):
        """E：隔离副作用 —— 用 Stub 上传器、禁用 vision 同步、无真实相机/Agent。"""
        monkeypatch.setattr("src.edge_client._build_vision_client", lambda cfg: None)
        edge_calls = []
        cloud = FakeCloud([X4Detection(1, "civilian", 0.8, (10, 20, 30, 40))])
        perception = _build_perception(_cfg(), cloud, edge_calls)
        agent = StubAgent()
        rc, uploader = _run_once(perception, agent)
        assert rc == 1
        assert isinstance(uploader, StubUploader)   # 未用生产 EdgeUploader（不触网）
        assert agent.frames                          # Stub Agent 收到结果
        # 断言 input_adapter(frame_record) 结构被真实实现复用
        fr = person_events_to_frame_record(agent.frames[0]["persons"])
        assert set(fr) == {"timestamp", "persons"}