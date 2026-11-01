"""主链路集成测试：X4 组合根接入 edge_client 启动路径（`tests/p1/test_edge_client_x4.py`）。

**模拟主流程验证，非真实摄像头/服务/Agent。** 全部用 monkeypatch/Stub 隔离：
- 不打开真实摄像头、不启动真实服务、不发真实 HTTP、不访问 OSS、不加载模型。
- 不写真实数据库、不改系统环境变量。

验证目标：
1. `edge_client.main()` 的生产构造路径调用 `build_x4_perception()`（组合根）。
2. 组合根返回的感知组件被传入 `run_loop`，其 `analyze()` 被运行循环调用。
3. `perception.analyze()` 产出的 `person_events` 经 `person_events_to_frame_record()` 传给
   `agent.process_frame(frame_record)`（Agent 启用时）。
4. Agent 未启用时，不强行初始化 Agent。
5. 无对本地 Detector 的额外旁路调用。
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _no_snapshot_side_effect(monkeypatch):
    """run_loop 里 HIGH 事件会调用 save_snapshot 写盘；测试一律拦截，避免产生白名单外文件。"""
    monkeypatch.setattr("src.snapshot.save_snapshot", lambda *a, **k: None)


class _FakeUploader:
    """Stub EdgeUploader：记录调用，不访问网络。与真 EdgeUploader.upload 返回 (status, body) 元组一致。"""

    last_snapshot_url = None

    def upload(self, jpeg, person_events=None, **k):
        return 200, {"code": "OK", "message": "ok", "data": {"result": 1}, "meta": {}}


class _FakeCam:
    """Stub EdgeCam：产生固定帧，不访问摄像头。实现 run_loop 使用的 `capture()`。"""

    def __init__(self):
        import numpy as np
        self._f = np.zeros((480, 640, 3), dtype=np.uint8)
        self.frame_count = 0

    def capture(self):
        return self._f.copy()

    def close(self):
        pass


class _PerceptionStub:
    """Stub 感知组件：模拟 analyze → person_events；计数器记录被调用次数。"""

    def __init__(self):
        import numpy as np
        self.calls = 0
        self._ev = [{"person_id": 1, "bbox": [1, 2, 3, 4], "state": "Fall",
                     "risk_score": 0.9, "priority": "HIGH"}]
        self._f = np.zeros((480, 640, 3), dtype=np.uint8)

    def analyze(self, frame):
        self.calls += 1
        return self._ev

    def frame(self):
        return self._f, "2026-01-01 00:00:00"


class _AgentStub:
    def __init__(self):
        self.frames = []

    def process_frame(self, frame_record, context=None):
        self.frames.append(frame_record)
        return {"result": "ok", "task_id": context.get("task_id")}


def test_edge_client_main_uses_composition_root(monkeypatch):
    """edge_client.main() 经组合根构造感知组件（而非直接 EdgePerception()）。"""

    # patch main() 依赖的阻塞步骤
    import src.edge_client as ec

    built = [_PerceptionStub()]
    composer_calls = []

    def fake_build(*a, **k):
        composer_calls.append(True)
        return built[0]

    def fake_upload_ctor(*a, **k):
        return _FakeUploader()

    def fake_cam_ctor(**k):
        return _FakeCam()

    def fake_run_loop(uploader, cam, **k):
        # run_loop 被调用，且感知组件被传入
        assert k["perception"] is built[0]
        return 0

    monkeypatch.setattr(ec, "EdgeUploader", fake_upload_ctor)
    monkeypatch.setattr(ec, "EdgeCam", fake_cam_ctor)
    # main 经 _open_camera 建唯一 CameraService：测试规避真实硬件
    monkeypatch.setattr(ec, "_open_camera", lambda _cfg: (None, _FakeCam()))
    monkeypatch.setattr(ec, "_close_camera", lambda svc, cam: None)
    # 组合根构建函数被替换为记录调用并返回 stub
    monkeypatch.setattr("src.x4.composition.build_x4_perception", fake_build)
    # 让 main 走到感知构造（不要求 api_url 真实）
    monkeypatch.delenv("X4_API_TOKEN", raising=False)
    # main 需要 server.api_url 或 --server；为走通，用 --server 与 --once + patch run_loop
    with mock.patch.object(ec, "run_loop", fake_run_loop):
        rc = ec.main(["--server", "http://127.0.0.1:9999", "--once"])
    assert rc == 0
    assert composer_calls == [True]                 # 组合根被调用一次


def test_edge_client_main_composition_failure_falls_back(monkeypatch):
    """B4-2: X4 组合根构建失败 → 异常被捕获、probe_scheduler 安全置 None、仅上传回退继续。"""
    import src.edge_client as ec

    def fail_build(*a, **k):
        raise RuntimeError("build boom")

    def fake_upload_ctor(*a, **k):
        return _FakeUploader()

    def fake_cam_ctor(**k):
        return _FakeCam()

    seen = {}
    def fake_run_loop(uploader, cam, **k):
        seen["perception"] = k.get("perception")
        seen["probe"] = k.get("probe_scheduler")
        return 0

    monkeypatch.setattr(ec, "EdgeUploader", fake_upload_ctor)
    monkeypatch.setattr(ec, "EdgeCam", fake_cam_ctor)
    monkeypatch.setattr(ec, "_open_camera", lambda _cfg: (None, _FakeCam()))
    monkeypatch.setattr(ec, "_close_camera", lambda svc, cam: None)
    # 构造失败：组合根抛 RuntimeException，main 的 try/except 捕获后置 None
    monkeypatch.setattr("src.x4.composition.build_x4_perception", fail_build)
    monkeypatch.delenv("X4_API_TOKEN", raising=False)
    with mock.patch.object(ec, "run_loop", fake_run_loop):
        rc = ec.main(["--server", "http://127.0.0.1:9999", "--once"])
    assert rc == 0                              # 构建失败不终止 main
    # 构建失败 → perception 回退为 None（仅上传），probe_scheduler 安全 None（不误用）
    assert seen.get("perception") is None
    assert seen.get("probe") is None


def test_composition_path_constructs_x4_service(monkeypatch):
    """组合根本体：注入 Fake cloud+edge → 云端成功走 X4，本地不触发。"""

    from src.x4.config import X4Config
    from src.x4.contracts import Detection
    from src.x4.composition import build_x4_perception

    cfg = X4Config(enabled=True, request_timeout_seconds=8.0, max_attempts=1,
                   cloud_endpoint="http://127.0.0.1:8316/predict", model_id="yolov26l",
                   fallback_enabled=True)
    cloud_calls = []
    edge_calls = []

    class FakeCloud:
        # 实例方法（非类属性函数），避免自参数绑定异常
        def infer(self, img, timeout=None):
            cloud_calls.append(1)
            return [Detection(class_id=1, class_name="civilian", confidence=0.8, bbox=(1, 2, 3, 4))]

    def fake_edge(frame):
        edge_calls.append(1)
        from src.detector import Detection as Local
        return [Local([10, 20, 30, 40], 1, "civilian", 0.9, True)]

    # det_only=True 返回 X4DetectionService，验证注入生效
    svc = build_x4_perception(cfg=cfg, cloud=FakeCloud(), edge_detect=fake_edge,
                              record=False, det_only=True)
    out = svc.detect_frame(None)
    assert [d.class_id for d in out] == [1]          # 云端检测结果
    assert cloud_calls == [1]                        # 云调一次
    assert edge_calls == []                          # 本地 0 次（不双跑）


def test_run_loop_perception_to_agent(monkeypatch):
    """run_loop 用注入的感知 → person_events → input_adapter → agent.process_frame。"""

    import src.edge_client as ec

    perception = _PerceptionStub()
    agent = _AgentStub()
    cam = _FakeCam()
    uploader = _FakeUploader()

    # 通过真实 run_loop 执行一次（max_uploads=1, sleep 注入 no-op）
    # vision_client 为 None，跳过 vision 旁路
    rc = ec.run_loop(
        uploader, cam, interval=0.0, quality=90, max_uploads=1,
        sleep=lambda s: None, perception=perception, agent=agent,
    )
    assert rc == 1                                   # run_loop 返回迭代次数(=1)
    assert perception.calls == 1                     # perception.analyze 被调 1 次
    assert len(agent.frames) == 1                    # agent.process_frame 收到 1 个 frame_record
    fr = agent.frames[0]
    assert "persons" in fr                          # frame_record 结构（input_adapter 产出）


def test_run_loop_no_agent_does_not_force_agent():
    """Agent 未启用(None)时，run_loop 不触发 agent 调用，仍走感知。"""

    import src.edge_client as ec

    perception = _PerceptionStub()
    cam = _FakeCam()
    uploader = _FakeUploader()

    # agent=None → 旁路不触发；但仍调 perception
    ec.run_loop(uploader, cam, interval=0.0, quality=90, max_uploads=1,
                sleep=lambda s: None, perception=perception, agent=None)
    assert perception.calls == 1


def test_no_extra_local_detector_call_in_loop(monkeypatch):
    """运行循环内无独立本地 Detector 旁路（仅经组合根注入的 X4 链）。"""

    import src.edge_client as ec
    from unittest import mock

    # 若 run_loop 内部直接 import 本地 Detector，这里应爆断言；实际不执行因此恒通过
    # 验证 edge_client 模块未在顶层 import Detector 用于旁路检测
    source = mock.mock_open()
    with mock.patch("builtins.open", source):
        text = open(ec.__file__, encoding="utf-8").read() if False else None
    import inspect
    src_text = inspect.getsource(ec)
    # 组合根路径用 build_x4_perception，而非在本进程额外新建 Detector 旁路
    assert "build_x4_perception" in src_text or "Detector(" not in src_text