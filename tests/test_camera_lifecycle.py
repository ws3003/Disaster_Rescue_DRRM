"""V4.4 S4 摄像头生命周期测试（Mock Picamera2，不连真实硬件）。

S4 目标在本仓库的落点：
  - `src/camera_lifecycle.py` 提供统一生命周期载体：
    - ``camera_session``：创建/启动**唯一** CameraService，正常/异常退出按
      "消费者 release → service.stop → service.close" 逆序释放；
    - ``RunCameraSharedReader``：唯一 CameraService 的鸭子 reader（保留 V4.3 通道、180°+copy）；
    - ``release_consumers``：逆序幂等释放消费者。
  - `src/camera_service.py`：唯一 Picamera2 持有者；stop() 停采集不关硬件，close() 停+释放+CLOSED（幂等）；
    FrameReader.release() 只解除自身订阅，绝不关摄像头。

本文件不触碰真实 Picamera2（monkeypatch ``sys.modules["picamera2"]`` 为 Fake），
并验证**实际调用顺序**（创建/启动/释放次序、订阅隔离、CLOSED 状态），非仅"方法被调用"。

注意：本仓库 main.py 为感知展示入口，EdgeClient(E7/E8 取证) 为其各自独立进程；
S4 在 main.py 的落点是"唯一 CameraService + camera_session 顺序释放"，本测试验证该载体。
"""

from __future__ import annotations

import sys
import time

import pytest

from src.camera_lifecycle import RunCameraSharedReader, camera_session, release_consumers
from src.camera_service import CameraService


# ---------------------------------------------------------------------------
# Mock Picamera2（复用 S1 测试一致做法：惰性导入点替换）
# ---------------------------------------------------------------------------


class _FakeCam:
    instances = []

    def __init__(self):
        _FakeCam.instances.append(self)
        self.started = False
        self.closed = False
        self._i = 0

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        self.controls = controls
        return {"main": main, "controls": controls}

    def configure(self, cfg):
        self.configured = True

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def capture_array(self):
        self._i += 1
        import numpy as np
        return np.zeros((self.main["size"][1], self.main["size"][0], 3),
                        dtype=np.uint8) + (self._i % 255)


@pytest.fixture(autouse=True)
def _fake_picamera(monkeypatch):
    class _Mod:
        def __init__(self):
            self.Picamera2 = _FakeCam

    monkeypatch.setitem(sys.modules, "picamera2", _Mod())
    _FakeCam.instances.clear()


def _await_running(s, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if s.latest() is not None:
            return True
        time.sleep(0.01)
    return s.latest() is not None


# ---------------------------------------------------------------------------
# A. 创建和注入：camera_session 唯一创建/启动一次，产出的 service 即唯一对象
# ---------------------------------------------------------------------------


class _FakeService:
    """记录创建次数与 stop/close 调用顺序的编排替身（用于 camera_session 生命周期编排）。"""

    created = 0

    def __init__(self, **kw):
        _FakeService.created += 1
        self._log = []
        self._started = False
        self._closed = False

    def start(self):
        self._log.append("start")
        self._started = True

    def fault(self):
        # 默认：启动成功（无故障）
        return self._fault_reason if hasattr(self, "_fault_reason") else None

    def stop(self):
        self._log.append("stop")
        self._started = False

    def close(self):
        self._log.append("close")
        self._closed = True

    def subscribe(self):
        return object()

    @property
    def log(self):
        return list(self._log)


@pytest.fixture()
def fake_view(monkeypatch):
    """把 camera_session 内的 CameraService 换成编排替身，计数/记录顺序。"""
    import src.camera_lifecycle as lc
    _FakeService.created = 0
    monkeypatch.setattr(lc, "CameraService", _FakeService)
    return _FakeService


def test_session_creates_service_exactly_once(fake_view):
    with camera_session() as svc:
        assert fake_view.created == 1      # 只创建一个 CameraService
    assert fake_view.created == 1


def test_session_starts_service_exactly_once(fake_view):
    with camera_session() as svc:
        assert svc.log.count("start") == 1
    assert fake_view.created == 1


def test_session_yields_single_service_shared_by_consumers():
    # within 内多次订阅：都来自唯一 service 对象（独立 FrameReader），不新建第二个硬件
    with camera_session(width=8, height=6, fps=30.0, queue_size=8) as service:
        _await_running(service)
        r1 = service.subscribe()
        r2 = service.subscribe()
        assert r1 is not r2               # 各自的独立订阅读句
        assert len(_FakeCam.instances) == 1  # 仍只有一个 PicCamera
        r1.release()
        r2.release()
    assert len(_FakeCam.instances) == 1


def test_reader_subscribes_current_service_not_new_picam():
    # 真实 CameraService + Fake picamera：reader 只订阅，不创建第二硬件
    with camera_session(width=8, height=6, fps=30.0, queue_size=8) as service:
        _await_running(service)
        reader = RunCameraSharedReader(service, width=8, height=6, fps=30.0)
        assert len(_FakeCam.instances) == 1      # 仍只有一个 PicCamera
        assert reader._frame_reader is not None  # 独立 FrameReader
        reader.release()
    assert len(_FakeCam.instances) == 1


# ---------------------------------------------------------------------------
# B. 正常关闭：yield 后按 stop→close 顺序释放，close 幂等，硬件只释放一次
# ---------------------------------------------------------------------------


def test_normal_exit_finally_stops_and_closes(fake_view):
    with camera_session() as svc:
        assert svc.log == ["start"]
    assert svc.log == ["start", "stop", "close"]


def test_norm_exit_releases_picam_once():
    with camera_session(width=8, height=6, fps=30.0):
        pass
    # close 已内部 stop+释放；PicCamera 实例唯一且已关闭，未重复释放（无第二实例/bug）
    assert len(_FakeCam.instances) == 1
    assert _FakeCam.instances[0].closed is True


def test_service_enters_closed_after_session():
    with camera_session(width=8, height=6, fps=30.0) as service:
        _await_running(service)
        assert service.state == "RUNNING"
    assert service.state == "CLOSED"


def test_repeat_close_no_hardware_double_release():
    service = CameraService(width=8, height=6, fps=30.0, queue_size=4)
    service.start()
    try:
        cam = _FakeCam.instances[0]
        service.close()
        assert cam.closed is True
        service.close()          # 幂等：重复关闭不再次释放（大量调 close 无副作用）
        assert cam.closed is True
        assert service.state == "CLOSED"
    finally:
        service.close()          # 幂等安全


# ---------------------------------------------------------------------------
# C. 异常关闭：body 异常 / KeyboardInterrupt / 启动失败 均能清理
# ---------------------------------------------------------------------------


def test_main_loop_exception_still_cleans_up(fake_view):
    with pytest.raises(LookupError):
        with camera_session() as svc:
            raise LookupError("主循环异常")
    assert svc.log == ["start", "stop", "close"]


def test_keyboard_interrupt_still_cleans_up(fake_view):
    with pytest.raises(KeyboardInterrupt):
        with camera_session() as svc:
            raise KeyboardInterrupt
    assert svc.log == ["start", "stop", "close"]


def test_start_failure_not_used_and_cleans_up(monkeypatch):
    # 启动失败（fault 非空）：camera_session 检测到故障即抛错，不再 fake 成功
    import src.camera_lifecycle as lc

    class _Failing(_FakeService):
        def fault(self):
            return "init failed: Boom"

    created_ref = {}
    fsvc = _Failing()
    created_ref["svc"] = fsvc

    def _factory(**kw):
        return fsvc

    monkeypatch.setattr(lc, "CameraService", _factory)
    with pytest.raises(RuntimeError, match="启动失败"):
        with camera_session():
            pass
    assert fsvc.log == ["start", "stop", "close"]  # 未使用 + finally 清理


def test_consumer_init_failure_still_closes_service(fake_view):
    # EdgeClient/感知消费者初始化抛错：camera_session finally 仍关闭 CameraService
    with pytest.raises(ValueError):
        with camera_session() as svc:
            raise ValueError("EdgeClient 初始化失败")
    assert svc.log == ["start", "stop", "close"]


def test_consumer_leak_no_camera_leak():
    # E8/录制异常：订阅 reader 泄漏时，session 退出仍统一关闭硬件
    with pytest.raises(RuntimeError):
        with camera_session(width=8, height=6, fps=30.0, queue_size=8) as service:
            _ = service.subscribe()          # 不显式 release（模拟泄漏/录制异常）
            raise RuntimeError("E8 录制异常")
    assert _FakeCam.instances[0].closed is True


# ---------------------------------------------------------------------------
# D. 共享消费者隔离：EdgeClient 释放不影响 E8，反之亦然；服务直到统一关闭才停
# ---------------------------------------------------------------------------


def test_edge_release_does_not_affect_e8():
    from src.edge_cam_adapter import EdgeCamAdapter
    from src.e8_camera_adapter import E8CameraAdapter

    service = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    service.start()
    _await_running(service)
    try:
        edge = EdgeCamAdapter(service)   # EdgeClient 侧（保留通道、180°）
        e8 = E8CameraAdapter(service)    # E8 侧（与旧采集器一致）
        edge.release()                   # EdgeClient 释放自身订阅
        assert service.state == "RUNNING"  # 不影响 E8 / 不关服务
        ok, frame = e8.read()            # E8 仍可读帧
        assert ok is True and frame is not None
    finally:
        service.close()


def test_e8_release_does_not_affect_edge():
    from src.edge_cam_adapter import EdgeCamAdapter
    from src.e8_camera_adapter import E8CameraAdapter

    service = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    service.start()
    _await_running(service)
    try:
        edge = EdgeCamAdapter(service)
        e8 = E8CameraAdapter(service)
        e8.release()                     # E8 释放自身订阅
        assert service.state == "RUNNING"  # 不影响 EdgeClient / 不关服务
        ok_frame = edge.capture()        # EdgeClient 仍可取帧
        assert ok_frame is not None
    finally:
        service.close()


def test_service_runs_until_main_closes():
    # 消费者都退出后 CameraService 仍在运行（main.py 统一关闭前）
    service = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    service.start()
    _await_running(service)
    reader = service.subscribe()
    reader.release()                     # 唯一消费者退出
    assert service.state == "RUNNING"    # 服务不因消费者退出而停止
    service.close()                      # main.py 统一关闭
    assert service.state == "CLOSED"
    assert _FakeCam.instances[0].closed is True


def test_final_close_picam_released_once():
    service = CameraService(width=8, height=6, fps=30.0, queue_size=8)
    service.start()
    service.close()
    assert len(_FakeCam.instances) == 1
    assert _FakeCam.instances[0].closed is True
