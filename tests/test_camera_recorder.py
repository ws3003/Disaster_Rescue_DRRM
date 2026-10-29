"""X5 录像适配器 + OSS 注入测试（Fake 捕获源 / Fake uploader / 临时文件，不连真实硬件/OSS）。"""

from __future__ import annotations

import os

import numpy as np
import pytest

from src.database.db import Database
from src.evidence.camera_recorder import CameraRecorder, CameraRecorderError
from src.sensor.config import SensorConfig
from src.sensor.runner import build_runner_with_storage


@pytest.fixture()
def db() -> Database:
    import tempfile
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "cam_wire_test.db"))
    d._tmpdir = tmp
    return d


class OkUploader:
    def upload(self, path, prefix="evidence"):
        return {"ok": True, "url": f"http://x/{prefix}/{os.path.basename(path)}", "provider": "aliyun_oss"}


def _frame():
    return np.zeros((120, 160, 3), dtype=np.uint8)


class FakeCapture:
    def __init__(self, frames=30, fail_start=False, read_raise=False):
        self.frames = int(frames)
        self.i = 0
        self.fail_start = fail_start
        self.read_raise = read_raise
        self.released = False

    def read(self):
        if self.read_raise:
            raise RuntimeError("camera io error")
        if self.fail_start or self.i >= self.frames:
            return False, None
        self.i += 1
        return True, _frame()

    def release(self):
        self.released = True


def _recorder(cap):
    return CameraRecorder(capture_factory=lambda: cap)


class TestCameraRecorder:
    def test_record_produces_nonempty_file(self, tmp_path):
        cap = FakeCapture(frames=30)
        dest = str(tmp_path / "a.mp4")
        out = _recorder(cap).record(1.0, dest)
        assert out == dest
        assert os.path.exists(dest) and os.path.getsize(dest) > 0
        assert cap.released is True

    def test_seconds_zero_still_writes_first_frame(self, tmp_path):
        cap = FakeCapture(frames=5)
        dest = str(tmp_path / "b.mp4")
        out = _recorder(cap).record(0.0, dest)
        assert os.path.exists(out) and os.path.getsize(out) > 0

    def test_capture_read_error_raises_and_cleans(self, tmp_path):
        cap = FakeCapture(read_raise=True)
        dest = str(tmp_path / "c.mp4")
        with pytest.raises(CameraRecorderError):
            _recorder(cap).record(1.0, dest)
        assert not os.path.exists(dest)                 # 部分输出已清理
        assert cap.released is True                     # 资源释放

    def test_output_missing_not_fake_success(self, tmp_path):
        cap = FakeCapture(fail_start=True)
        with pytest.raises(CameraRecorderError):
            _recorder(cap).record(1.0, str(tmp_path / "d.mp4"))

    def test_no_backend_raises_not_fake(self, tmp_path):
        r = CameraRecorder()                            # 未注入捕获源
        with pytest.raises(CameraRecorderError):
            r.record(1.0, str(tmp_path / "e.mp4"))


class TestStorageWiring:
    def _cfg(self):
        return SensorConfig(sensor_id="s1", temperature_threshold=40.0, level2_threshold=50.0,
                            recovery_threshold=36.0, sample_period_seconds=1.0,
                            confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0)

    def test_uploader_injected_over_storage(self, db, tmp_path):
        fake = OkUploader()
        r = build_runner_with_storage(self._cfg(), db=db,
                                      reader=_NullReader(), uploader=fake)
        assert r.recorder.uploader is fake            # 注入优先, 不误用真实 OSS

    def test_uploader_default_via_create_storage(self, db, tmp_path, monkeypatch):
        # uploader 缺省经 create_storage 接线；不触发真实网络（仅实例化 provider）
        import src.storage as S
        called = {"n": 0}

        def fake_create(*a, **k):
            called["n"] += 1
            return OkUploader()

        monkeypatch.setattr(S, "create_storage", fake_create)
        r = build_runner_with_storage(self._cfg(), db=db, reader=_NullReader())
        assert called["n"] == 1 and r.recorder.uploader is not None


class _NullReader:
    # 仅用于接线测试：build_runner 不实际采样的 reader 占位
    def read(self, now=None):
        raise RuntimeError("not used")