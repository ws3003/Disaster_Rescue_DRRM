"""V4.4 S3 —— E8 runner 共享 CameraService 模式测试（Mock，不连真机）。

覆盖：build_shared_camera_recorder 返回 CameraRecorder；其 capture_factory 生成 E8CameraAdapter（订阅传入 service）；
不创建第二个 Picamera2、不调用 CameraService.close()；E8 适配器 release 只解订阅；共享/独立两模式并存。
"""

from __future__ import annotations

import os
import runpy
import sys
import types

import pytest


@pytest.fixture(scope="module")
def runner_mod():
    g = runpy.run_path(os.path.join("scripts", "e7e8_runner.py"), run_name="e7e8_runner")
    return types.SimpleNamespace(**g)


class _Pic:
    instances = []

    def __init__(self):
        _Pic.instances.append(self)
        self.closed = False

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        return {}

    def configure(self, c):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def capture_array(self):
        import numpy as np
        h, w = self.main["size"][1], self.main["size"][0]
        return np.zeros((h, w, 3), dtype=np.uint8) + 1


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    class _M:
        def __init__(self):
            self.Picamera2 = _Pic

    monkeypatch.setitem(sys.modules, "picamera2", _M())
    _Pic.instances.clear()


def _e8_config_shared(runner_mod):
    return runner_mod.E8Config(enabled=True, record_seconds=1, width=8, height=6,
                               fps=15.0, codec="mp4v", tmp_dir=None)


class TestSharedRecorderBuilder:
    def test_build_shared_uses_service_no_new_picam(self, runner_mod):
        from src.camera_service import CameraService
        service = CameraService(width=8, height=6, queue_size=4)
        service.start()
        try:
            e8 = _e8_config_shared(runner_mod)
            rec = runner_mod.build_shared_camera_recorder(service, e8)
            # capture_factory 生成 E8CameraAdapter（订阅传入 service），不新建 PicCamera
            s1 = rec._capture_factory()
            s2 = rec._capture_factory()
            assert s1 is not s2                    # 每次录像独立订阅
            assert len(_Pic.instances) == 1        # 共享：仍只有 service 那一个 PicCamera
            s1.release(); s2.release()
            assert service.session_id >= 1
        finally:
            service.close()

    def test_shared_recorder_does_not_close_service(self, runner_mod):
        from src.camera_service import CameraService
        service = CameraService(width=8, height=6, queue_size=4)
        service.start()
        try:
            rec = runner_mod.build_shared_camera_recorder(service, _e8_config_shared(runner_mod))
            src = rec._capture_factory()
            src.release()   # E8 适配器 release 只解订阅
            assert service.state == "RUNNING"      # 服务未关
            assert _Pic.instances[0].closed is False
        finally:
            service.close()


class TestRunnerModes:
    def test_shared_and_independent_modes_both_exist(self, runner_mod):
        # runner 暴露两种构建：build_camera_recorder(独立) 与 build_shared_camera_recorder(共享)
        assert callable(getattr(runner_mod, "build_camera_recorder", None))
        assert callable(getattr(runner_mod, "build_shared_camera_recorder", None))
        assert "camera_service" in _sig(runner_mod.run_e7e8)

    def test_independent_mode_unchanged(self, runner_mod):
        e8 = _e8_config_shared(runner_mod)
        rec = runner_mod.build_camera_recorder(e8)   # 独立：用 PicameraFrameSource 工厂
        s = rec._capture_factory()
        assert s.__class__.__name__ == "PicameraFrameSource"
        # 独立模式每录新建一个自己的 PicCamera（方案 A）→ _open 触发 1 个实例
        assert len(_Pic.instances) == 1
        s.release()
        assert _Pic.instances[0].closed is True


def _sig(fn):
    import inspect
    try:
        return str(inspect.signature(fn))
    except Exception:  # noqa: BLE001
        return ""