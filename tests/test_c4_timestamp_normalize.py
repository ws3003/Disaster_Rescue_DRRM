"""V4.4 C4-01 canonical 时间戳解析覆盖测试。

针对 canonical._norm_time 的时间契约：
- 带时区（Z / +08:00 / +00:00）→ 规范化为同一 UTC 字符串（Z 与 +00:00 同一瞬时同一表示）；
- +08:00 与 Z 同瞬时 → 同一规范化结果；
- 无时区 → 保留字面（不猜时区）；
- 非法日期 / 非法字符串 / 空 → unparsable（拒绝，不静默丢失）。

结论：时间戳解析已正确处理；本测试用于锁定该契约、防回归，不涉及数值契约改动。
"""

from __future__ import annotations

import pytest

from src.database import canonical


def _norm(value):
    return canonical._norm_time(value)


def _is_unp(v):
    return v == canonical._UNP or (isinstance(v, str) and v.startswith("unparsable"))


class TestTimeZones:
    def test_z_and_plus_offset_same_instant(self):
        a = _norm("2026-10-30T08:00:00Z")
        b = _norm("2026-10-30T16:00:00+08:00")
        c = _norm("2026-10-30T08:00:00+00:00")
        assert a == b == c  # 同一瞬时 → 同一 UTC 规范化
        assert a == "2026-10-30T08:00:00.000000Z"

    def test_no_tz_preserved_literal(self):
        assert _norm("2026-10-30T08:00:00") == "2026-10-30T08:00:00"

    def test_invalid_rejected(self):
        assert _is_unp(_norm("not-a-date"))
        assert _is_unp(_norm("2026-13-33T99:00:00Z"))
        assert _is_unp(_norm(""))  # 空时间戳

    def test_sub_micro_rejected(self):
        assert _is_unp(_norm("2026-10-30T08:00:00.123456789+08:00"))

    @pytest.mark.parametrize("suffix", ["Z", "+08:00", ""])
    @pytest.mark.parametrize("fraction", ["1234567", "1234560"])
    def test_long_fraction_rejected_before_datetime_truncation(self, suffix, fraction):
        assert _is_unp(_norm("2026-10-30T08:00:00." + fraction + suffix))

    def test_microsecond_precision_preserved(self):
        assert _norm("2026-10-30T08:00:00.123456+08:00") == "2026-10-30T00:00:00.123456Z"
        assert _norm("2026-10-30T08:00:00.123456") == "2026-10-30T08:00:00.123456"


class TestReadingTimestampEnqueue:
    def test_reading_z_literal_enqueues_clean(self):
        # 合法 Z reading（整数温度）→ reasons 为空（可入 Outbox）
        ch, reasons = canonical.classify(
            "reading",
            {"sensor_id": "s", "temperature": 30, "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"},
            version="v1")
        assert sorted(reasons) == []
        assert ch

    def test_reading_offset_same_hash(self):
        _, r1 = canonical.classify(
            "reading",
            {"sensor_id": "s", "temperature": 30, "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        _, r2 = canonical.classify(
            "reading",
            {"sensor_id": "s", "temperature": 30, "humidity": 40,
             "device_captured_at": "2026-10-28T18:00:00+08:00"}, version="v1")
        # 同一瞬时 → 同一 canonical hash（幂等不因时区写法破坏）
        ch1 = canonical.compute_content_hash(
            "reading",
            {"sensor_id": "s", "temperature": 30, "humidity": 40,
             "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        ch2 = canonical.compute_content_hash(
            "reading",
            {"sensor_id": "s", "temperature": 30, "humidity": 40,
             "device_captured_at": "2026-10-28T18:00:00+08:00"}, version="v1")
        assert ch1 == ch2
