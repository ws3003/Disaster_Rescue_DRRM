"""E6-01 FrameSelector（云端选帧器）单元测试（注入时钟，不联网/不触模型）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.x4.config import X4Config, load_x4_config
from src.x4.frame_selector import FrameSelector


class _Clock:
    def __init__(self, start=1000.0):
        self.value = float(start)

    def advance(self, sec):
        self.value += float(sec)

    def __call__(self):
        return self.value


def _sel(enabled=True, interval=10.0, clock=None):
    clock = clock or _Clock()
    return FrameSelector(enabled=enabled, interval_seconds=interval, now=clock), clock


class TestFrameSelector:
    def test_windows_throttle_interval_seconds(self):
        sel, clock = _sel(interval=10.0)
        assert sel.should_send() is True     # 窗口起点：选中
        assert sel.should_send() is False    # 未过窗口：不选
        clock.advance(9.0)
        assert sel.should_send() is False    # 仍不足 10s
        clock.advance(1.0)                   # 累计 10s
        assert sel.should_send() is True     # 窗口翻：再选

    def test_reset_on_pass(self):
        sel, clock = _sel(interval=5.0)
        assert sel.should_send() is True
        clock.advance(5.0)
        assert sel.should_send() is True     # 距离上次选中达 interval → 双侧均 True

    def test_disabled_always_true(self):
        sel, _ = _sel(enabled=False)         # 默认关：不选帧（每帧云）
        for _ in range(5):
            assert sel.should_send() is True

    def test_invalid_interval_rejected(self):
        with pytest.raises(ValueError):
            _sel(interval=0.0)
        with pytest.raises(ValueError):
            _sel(interval=-1.0)
        with pytest.raises(ValueError):
            _sel(interval="x")

    def test_invalid_enabled_rejected(self):
        with pytest.raises(ValueError):
            FrameSelector(enabled="yes", interval_seconds=10.0,
                          now=_Clock())


class TestConfigParse:
    def test_defaults_disabled(self):
        # 缺省/缺失 frame_selection 段：enabled=False（不改变每帧云行为）
        c = X4Config()
        assert c.frame_selection_enabled is False
        assert c.frame_selection_interval_seconds == 10.0

    def test_load_enabled_parses(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x4.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x4:\n"
                        "  enabled: true\n"
                        "  cloud_endpoint: http://fake\n"
                        "  frame_selection:\n"
                        "    enabled: true\n"
                        "    interval_seconds: 15.0\n")
            c = load_x4_config(p)
            assert c.frame_selection_enabled is True
            assert c.frame_selection_interval_seconds == 15.0

    def test_invalid_interval_rejected_at_config(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x4.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x4:\n  enabled: true\n  frame_selection:\n"
                        "    enabled: true\n    interval_seconds: 0\n")
            with pytest.raises(ValueError):
                load_x4_config(p)