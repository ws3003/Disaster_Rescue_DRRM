"""V4.4 S1 E7/E8 生产装配入口测试（Mock，不连真实摄像头/OSS）。

覆盖：e8 配置解析与类型校验、每次录像新建帧源（capture_factory 产生独立实例）、
build_camera_recorder 透传 codec/fps、摄像头初始化失败时 _probe_picamera 返回可诊断错误。
"""

from __future__ import annotations

import os
import runpy
import tempfile
import types

import pytest


def _load():
    g = runpy.run_path(os.path.join("scripts", "e7e8_runner.py"), run_name="e7e8_runner")
    return types.SimpleNamespace(**g)


@pytest.fixture(scope="module")
def m():
    return _load()


def _write_yaml(tmp_path, e8):
    import yaml as _y
    p = tmp_path / "edge.yaml"
    p.write_text(_y.safe_dump({"e8": e8}), encoding="utf-8")
    return str(p)


class TestE8Config:
    def test_defaults_when_no_e8(self, m, tmp_path):
        p = tmp_path / "edge.yaml"
        p.write_text("sensor:\n  sensor_id: x\n", encoding="utf-8")
        cfg = m.load_e8_config(str(p))
        assert cfg.enabled is False
        assert cfg.record_seconds == 10 and cfg.width == 1280 and cfg.height == 720
        assert cfg.fps == 15.0 and cfg.codec == "mp4v" and cfg.tmp_dir is None

    def test_parse_e8(self, m, tmp_path):
        p = _write_yaml(tmp_path, {"enabled": True, "record_seconds": 10, "width": 1280,
                                   "height": 720, "fps": 15.0, "codec": "mp4v",
                                   "tmp_dir": "/tmp/ev"})
        cfg = m.load_e8_config(p)
        assert cfg.enabled is True and cfg.record_seconds == 10 and cfg.tmp_dir == "/tmp/ev"

    def test_bad_type_rejected(self, m, tmp_path):
        p = _write_yaml(tmp_path, {"record_seconds": "abc"})
        with pytest.raises(m.E8ConfigError):
            m.load_e8_config(p)


class TestCaptureFactory:
    def test_factory_creates_new_source_each_call(self, m, monkeypatch):
        from src.evidence import picamera_capture as pc

        made = []

        class Stub:
            def __init__(self, width, height, fps):
                made.append((width, height, fps))

            def release(self):
                pass

        monkeypatch.setattr(pc, "PicameraFrameSource", Stub)
        cfg = m.E8Config(enabled=True, width=640, height=480, fps=24.0)
        factory = m.make_capture_factory(cfg)
        a, b = factory(), factory()
        assert a is not b                     # 每次新建帧源（方案 A）
        assert made == [(640, 480, 24.0), (640, 480, 24.0)]

    def test_build_camera_recorder_factory_and_params(self, m, monkeypatch):
        from src.evidence import picamera_capture as pc
        from src.evidence import camera_recorder as cr

        made = []

        class StubSrc:
            def __init__(self, width, height, fps):
                made.append((width, height, fps))

            def release(self):
                pass

        monkeypatch.setattr(pc, "PicameraFrameSource", StubSrc)
        cfg = m.E8Config(enabled=True, codec="avc1", fps=10.0)
        rec = m.build_camera_recorder(cfg)
        assert isinstance(rec, cr.CameraRecorder)
        # 每次 record 会调用 capture_factory -> 新建独立帧源
        s1 = rec._capture_factory()
        s2 = rec._capture_factory()
        assert s1 is not s2 and made == [(1280, 720, 10.0), (1280, 720, 10.0)]


class TestProbe:
    def test_probe_ok_returns_none(self, m, monkeypatch):
        from src.evidence import picamera_capture as pc

        class Stub:
            def __init__(self, *a, **k):
                self.released = False

            def release(self):
                self.released = True

        monkeypatch.setattr(pc, "PicameraFrameSource", Stub)
        cfg = m.E8Config(enabled=True)
        assert m._probe_picamera(cfg) is None

    def test_probe_init_failure_returns_error(self, m, monkeypatch):
        from src.evidence import picamera_capture as pc

        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("camera busy")

        monkeypatch.setattr(pc, "PicameraFrameSource", Boom)
        err = m._probe_picamera(m.E8Config(enabled=True))
        assert err and "camera busy" in err


class TestSmoke:
    def test_edge_yaml_loads_e8(self, m):
        cfg = m.load_e8_config("config/edge.yaml")
        assert cfg.enabled is False                       # 生产接线前默认关闭
        assert cfg.record_seconds == 10