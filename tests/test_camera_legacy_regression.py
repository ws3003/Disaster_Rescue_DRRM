"""用同一不对称彩帧调用 V4.3 原采集器，对照四条 V4.4 消费路径。

使用模拟硬件验证像素、方向和副本，不作为真实相机或端到端验收证据。
可通过 unittest 执行，不要求 pytest 或树莓派硬件。
"""

import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from src.camera import CameraFrameReader
from src.camera_lifecycle import RunCameraSharedReader
from src.e8_camera_adapter import E8CameraAdapter
from src.edge_cam_adapter import EdgeCamAdapter
from src.evidence.picamera_capture import PicameraFrameSource


class _StaticPicamera:
    def __init__(self, frame):
        self.frame = frame
        self.config = None
        self.closed = False

    def create_video_configuration(self, main=None, controls=None):
        return {"main": main}

    def configure(self, config):
        self.config = config

    def camera_configuration(self):
        return self.config

    def start(self):
        pass

    def capture_array(self):
        return self.frame

    def stop(self):
        pass

    def close(self):
        self.closed = True


class _StaticReader:
    def __init__(self, frame):
        self.frame = frame
        self.closed = False

    def read(self):
        return (False, None) if self.closed else (True, self.frame)

    def release(self):
        self.closed = True


class _StaticService:
    def __init__(self, frame):
        self.frame = frame

    def subscribe(self):
        return _StaticReader(self.frame)


class TestCameraLegacyRegression(unittest.TestCase):
    def setUp(self):
        # 各像素和红蓝通道均不同，不能用灰帧掩盖交换或翻转错误。
        self.raw = np.array([
            [[5, 20, 240], [210, 40, 10], [60, 170, 90]],
            [[150, 30, 80], [15, 220, 65], [245, 55, 25]],
        ], dtype=np.uint8)
        self.original = self.raw.copy()
        self.service = _StaticService(self.raw)
        self.cam = _StaticPicamera(self.raw)
        module = types.SimpleNamespace(Picamera2=lambda: self.cam)
        self.hardware_patch = patch.dict(sys.modules, {"picamera2": module})
        self.hardware_patch.start()
        self.addCleanup(self.hardware_patch.stop)

        # 直接调用未修改的 V4.3 采集器作为行为基准，而不是重写其变换公式。
        legacy = CameraFrameReader({"camera": {"width": 3, "height": 2, "fps": 15}})
        try:
            _, _, self.expected = next(legacy.iter_frames())
        finally:
            legacy.close()

    def assert_matches_legacy(self, actual):
        np.testing.assert_array_equal(actual, self.expected)
        np.testing.assert_array_equal(self.raw, self.original)
        self.assertFalse(np.shares_memory(actual, self.raw))
        actual[:] = 0
        np.testing.assert_array_equal(self.raw, self.original)

    def test_oracle_preserves_channels_and_rotates(self):
        np.testing.assert_array_equal(self.expected, self.raw[::-1, ::-1])
        self.assertFalse(np.array_equal(self.expected, self.raw[::-1, ::-1, ::-1]))

    def test_edge_client_matches_v43(self):
        adapter = EdgeCamAdapter(self.service, width=3, height=2)
        self.addCleanup(adapter.release)
        self.assert_matches_legacy(adapter.capture())

    def test_main_shared_reader_matches_v43(self):
        reader = RunCameraSharedReader(self.service, width=3, height=2, fps=15)
        self.addCleanup(reader.release)
        _, _, frame = next(reader.iter_frames())
        self.assert_matches_legacy(frame)

    def test_shared_video_matches_v43(self):
        adapter = E8CameraAdapter(self.service)
        self.addCleanup(adapter.release)
        ok, frame = adapter.read()
        self.assertTrue(ok)
        self.assert_matches_legacy(frame)

    def test_standalone_video_matches_v43(self):
        source = PicameraFrameSource(width=3, height=2, fps=15)
        self.addCleanup(source.release)
        ok, frame = source.read()
        self.assertTrue(ok)
        self.assert_matches_legacy(frame)


if __name__ == "__main__":
    unittest.main()
