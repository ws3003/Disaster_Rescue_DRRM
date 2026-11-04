"""E7-01 DS18B20Reader 单元测试（A2）。

用临时目录 + monkeypatch 将默认设备目录指向临时路径，模拟 w1_slave 内容；
不连接真实 Raspberry Pi / 不访问 /sys/bus/w1。
覆盖：正常 CRC+温度、CRC 错、缺目录、缺文件、缺字段、格式错、指定设备、自动发现、摄氏度换算。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.sensor.ds18b20_reader import DEFAULT_W1_DIR, DS18B20Reader
from src.sensor.sensor_errors import SensorReadError

TS = "2026-10-26T09:00:00+08:00"


def _write_slave(root, device_id, first, second):
    dev = root / device_id
    dev.mkdir()
    slave = dev / "w1_slave"
    slave.write_text(f"{first}\n{second}\n", encoding="utf-8")
    return root


def _reader(root, device_id=None):
    return DS18B20Reader(device_dir=str(root), device_id=device_id)


class TestParse:
    def test_normal_crc_and_temp(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-0224e33f1e4e",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=8b YES",
                     "6e 01 4b 46 7f ff 04 10 8b t=26750")
        r = _reader(root, device_id="28-0224e33f1e4e").read(now=lambda: TS)
        assert r.temperature_c == pytest.approx(26.75)
        assert r.humidity_pct is None
        assert r.captured_at == TS

    def test_negative_temperature(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-aaaa",
                     "ff ff ff ff ff ff ff ff ff : crc=ff YES",
                     "ff ff ff ff ff ff ff ff ff t=-5050")
        r = _reader(root, device_id="28-aaaa").read(now=lambda: TS)
        assert r.temperature_c == pytest.approx(-5.05)

    def test_crc_no_yes_rejected(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-bbbb",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=00 NO",
                     "6e 01 4b 46 7f ff 04 10 8b t=26750")
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-bbbb").read(now=lambda: TS)

    def test_missing_t_field_rejected(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-cccc",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=8b YES",
                     "6e 01 4b 46 7f ff 04 10 8b")
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-cccc").read(now=lambda: TS)

    def test_bad_t_value_rejected(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-dddd",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=8b YES",
                     "6e 01 4b 46 7f ff 04 10 8b t=abc")
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-dddd").read(now=lambda: TS)


class TestDeviceLocate:
    def test_directory_missing(self, tmp_path):
        missing = str(tmp_path / "nope")
        with pytest.raises(SensorReadError):
            DS18B20Reader(device_dir=missing).read(now=lambda: TS)

    def test_specific_device_file_missing(self, tmp_path):
        root = tmp_path
        dev = root / "28-eeee"
        dev.mkdir()  # 目录在但无 w1_slave
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-eeee").read(now=lambda: TS)

    def test_autodiscover_first_device(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-1111",
                     "xx : crc=AA YES", "xx t=20000")
        _write_slave(root, "28-2222",
                     "xx : crc=AA YES", "xx t=30000")
        r = _reader(root).read(now=lambda: TS)  # 无 device_id → 自动发现
        assert r.temperature_c == pytest.approx(20.0)

    def test_no_device_found(self, tmp_path):
        root = tmp_path
        (root / "some-other").mkdir()
        with pytest.raises(SensorReadError):
            _reader(root).read(now=lambda: TS)


# 合法双行 w1_slave 内容（校验通过，t=26.750℃）
_VALID = ("6e 01 4b 46 7f ff 04 10 8b : crc=8b YES\n"
          "6e 01 4b 46 7f ff 04 10 8b t=26750\n")


def _read_text_staged(*contents):
    """按序返回 read_text 结果；空表默认为最后一次；元素为异常则抛出。"""
    state = {"n": 0}
    def _fake(_path, *args, **kwargs):
        i = state["n"]
        state["n"] += 1
        val = contents[i] if i < len(contents) else contents[-1]
        if isinstance(val, BaseException):
            raise val
        return val
    return _fake, state


class TestRetry:
    """V4.4 有限重试：空/不完整内容、CRC、OSError 可瞬态重试；定位错误不重试。"""

    def _reader(self, root, state, *, retry_count=2, sleeps=None):
        if sleeps is None:
            sleeps = []
        return DS18B20Reader(
            device_dir=str(root), device_id="28-aaaa",
            retry_count=retry_count, sleep=sleeps.append,
        ), sleeps

    def test_empty_first_then_valid_succeeds(self, tmp_path, monkeypatch):
        root = tmp_path
        _write_slave(root, "28-aaaa", "x", "y")  # 文件存在即可, 内容由 staged 提供
        fake, state = _read_text_staged("", _VALID)
        monkeypatch.setattr(Path, "read_text", fake)
        r, sleeps = self._reader(root, state)
        rd = r.read(now=lambda: TS)
        assert rd.temperature_c == pytest.approx(26.75)
        assert state["n"] == 2      # 第一次空 + 第二次成功
        assert len(sleeps) == 1     # 成功前一次重试等待

    def test_persistent_empty_raises_bounded(self, tmp_path, monkeypatch):
        root = tmp_path
        _write_slave(root, "28-aaaa", "x", "y")
        fake, state = _read_text_staged("", "", "", "")
        monkeypatch.setattr(Path, "read_text", fake)
        r, sleeps = self._reader(root, state, retry_count=2)
        with pytest.raises(SensorReadError) as ei:
            r.read(now=lambda: TS)
        assert "行数不足" in str(ei.value)
        assert state["n"] == 3      # retry_count=2 → 最多 3 次
        assert len(sleeps) == 2     # 有界等待次数 = 重试次数

    def test_transient_oserror_then_valid(self, tmp_path, monkeypatch):
        root = tmp_path
        _write_slave(root, "28-aaaa", "x", "y")
        fake, state = _read_text_staged(OSError("EIO"), _VALID)
        monkeypatch.setattr(Path, "read_text", fake)
        r, sleeps = self._reader(root, state)
        rd = r.read(now=lambda: TS)
        assert rd.temperature_c == pytest.approx(26.75)
        assert state["n"] == 2
        assert len(sleeps) == 1

    def test_retry_zero_reads_once(self, tmp_path, monkeypatch):
        root = tmp_path
        _write_slave(root, "28-aaaa", "x", "y")
        fake, state = _read_text_staged("")
        monkeypatch.setattr(Path, "read_text", fake)
        r, sleeps = self._reader(root, state, retry_count=0)
        with pytest.raises(SensorReadError):
            r.read(now=lambda: TS)
        assert state["n"] == 1      # retry_count=0 → 只读一次
        assert len(sleeps) == 0

    def test_crc_failure_is_retried_then_succeeds(self, tmp_path, monkeypatch):
        root = tmp_path
        _write_slave(root, "28-aaaa", "x", "y")
        bad = "6e 01 4b 46 7f ff 04 10 8b : crc=00 NO\n6e 01 4b 46 7f ff 04 10 8b t=26750\n"
        fake, state = _read_text_staged(bad, _VALID)
        monkeypatch.setattr(Path, "read_text", fake)
        r, sleeps = self._reader(root, state)
        rd = r.read(now=lambda: TS)
        assert rd.temperature_c == pytest.approx(26.75)
        assert state["n"] == 2

    def test_device_file_missing_not_retried(self, tmp_path):
        # 定位错误（目录在但无 w1_slave）：立即失败, 不重试、不等待
        root = tmp_path
        dev = root / "28-eeee"
        dev.mkdir()
        sleeps = []
        r = DS18B20Reader(device_dir=str(root), device_id="28-eeee", sleep=sleeps.append)
        with pytest.raises(SensorReadError):
            r.read(now=lambda: TS)
        assert len(sleeps) == 0