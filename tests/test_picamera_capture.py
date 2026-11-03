"""Picamera2 薄适配器测试（Fake picamera2，不连真实硬件）。"""

from __future__ import annotations

import sys

import numpy as np
import pytest

import src.evidence.picamera_capture as pcc
from src.evidence.picamera_capture import PicameraCaptureError, PicameraFrameSource


class _FakeCam:
    def __init__(self, fail_configure=False, fail_capture=False):
        self.fail_configure = fail_configure
        self.fail_capture = fail_capture
        self.stop_calls = 0
        self.close_calls = 0
        self.configure_calls = 0

    def create_video_configuration(self, main=None, controls=None):
        return {}

    def configure(self, cfg):
        self.configure_calls += 1
        if self.fail_configure:
            raise RuntimeError("configure boom")

    def start(self):
        if self.fail_configure:
            raise RuntimeError("start boom")

    def capture_array(self):
        if self.fail_capture:
            raise RuntimeError("capture boom")
        a = np.zeros((2, 2, 3), dtype=np.uint8)
        a[..., 0] = 10   # B：RGB888 实际数组直接供 OpenCV 使用
        a[..., 1] = 20   # G
        a[..., 2] = 30   # R
        a[0, 0] = [11, 22, 33]  # 不对称角点用于验证安装方向补偿
        return a

    def stop(self):
        self.stop_calls += 1

    def close(self):
        self.close_calls += 1


def _install_fake(monkeypatch, cam=None):
    cam = cam or _FakeCam()
    mod = type("FakePicamera2Module", (), {"Picamera2": staticmethod(lambda: cam)})
    monkeypatch.setitem(sys.modules, "picamera2", mod)
    return cam


class TestLazyImport:
    def test_module_import_does_not_open_camera(self, monkeypatch):
        # 模块顶层不应 import picamera2/cv2：本文件顶部已 `import pcc` 成功（无 picamera2 环境），
        # 且构造时才需要 picamera2（见 test_picamera_import_unavailable）。
        assert "picamera2" not in getattr(pcc, "__dict__", {})
        assert "cv2" not in getattr(pcc, "__dict__", {})
        monkeypatch.delitem(sys.modules, "picamera2", raising=False)
        assert "picamera2" not in sys.modules


class TestReadBGR:
    def test_success_read_and_bgr_order(self, monkeypatch):
        cam = _install_fake(monkeypatch)
        src = PicameraFrameSource(width=1280, height=720, fps=15.0)
        try:
            ok, frame = src.read()                 # 明确用 R/G/B 不同通道值校验
            assert ok is True
            assert frame.shape[:2] == (2, 2) and frame.shape[2] == 3
            expected = cam.capture_array()[::-1, ::-1]
            np.testing.assert_array_equal(frame, expected)
            assert list(frame[0, 0]) == [10, 20, 30]
            assert list(frame[1, 1]) == [11, 22, 33]
        finally:
            src.release()

    def test_capture_failure_raises(self, monkeypatch):
        cam = _install_fake(monkeypatch, cam=_FakeCam(fail_capture=True))
        src = PicameraFrameSource()
        try:
            with pytest.raises(PicameraCaptureError):
                src.read()
        finally:
            src.release()


class TestRelease:
    def test_release_idempotent_and_stops(self, monkeypatch):
        cam = _install_fake(monkeypatch)
        src = PicameraFrameSource()
        src.release()
        src.release()                                # 重复调用不报错
        assert cam.stop_calls >= 1
        assert cam.close_calls >= 1
        assert src.read() == (False, None)           # 已关闭不再读


class TestErrors:
    def test_picamera_import_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "picamera2", None)   # import 即抛 ImportError
        with pytest.raises(PicameraCaptureError):
            PicameraFrameSource()

    def test_init_failure_cleans_up(self, monkeypatch):
        cam = _install_fake(monkeypatch, cam=_FakeCam(fail_configure=True))
        with pytest.raises(PicameraCaptureError):
            PicameraFrameSource()
        assert cam.stop_calls + cam.close_calls >= 1   # 已清理已分配资源
