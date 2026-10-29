"""V4.4 C4-01 阶段 3-A: decimalize 辅助模块单元测试。

覆盖:
- 精确输入(int/Decimal/十进制串); bool 拒绝; 整数值 float;
- 非整 float 未确认精度 -> 明确失败 (不装作恢复精度);
- 显式 decimals 契约量化(明确声明精度场景);
- None vs 字段缺失; 空串/非法串; NaN/±Inf;
- 极大整数/极小非零 Decimal 不被转 float 或归零;
- 不注册 risk_score/temperature_c/humidity_pct 精度;
- 输出与 canonical 兼容 (精确 Decimal 可被 canonical._norm_number 接受)。
确定性输入, 不碰 DB/硬件/云。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.database import canonical
from src.database.decimalize import EXACT, EXACT_INT, NONE, UNPARSABLE, convert_exact, is_precision_registered


class TestExactInputs:
    def test_int(self):
        r = convert_exact(30)
        assert r.kind == EXACT and r.exact_value == Decimal("30")

    def test_decimal(self):
        r = convert_exact(Decimal("30.5"))
        assert r.kind == EXACT and r.exact_value == Decimal("30.5")

    def test_decimal_string(self):
        r = convert_exact("30.5")
        assert r.kind == EXACT and r.exact_value == Decimal("30.5")

    def test_integral_float(self):
        r = convert_exact(1.0)
        assert r.kind == EXACT_INT and r.exact_value == Decimal("1")
        # 1 与 1.0 一致语义在下层 canonical 保持
        assert r.exact_value == Decimal("1")

    def test_tiny_decimal_not_become_float(self):
        r = convert_exact(Decimal("1e-400"))
        assert r.kind == EXACT and r.exact_value == Decimal("1e-400")

    def test_big_int_exact(self):
        r = convert_exact(2 ** 53 + 1)
        assert r.kind == EXACT and r.exact_value == Decimal(2 ** 53 + 1)


class TestRejections:
    def test_bool_rejected(self):
        assert convert_exact(True).kind == UNPARSABLE
        assert convert_exact(False).kind == UNPARSABLE

    def test_none_distinct(self):
        r = convert_exact(None)
        assert r.kind == NONE and r.value is None

    def test_none_not_zero(self):
        assert convert_exact(None).value is None  # 不自动转 0

    def test_empty_string(self):
        assert convert_exact("  ").kind == UNPARSABLE

    def test_invalid_string(self):
        assert convert_exact("abc").kind == UNPARSABLE

    def test_nan_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")):
            assert convert_exact(bad).kind == UNPARSABLE

    def test_unsupported_type(self):
        assert convert_exact([1.0]).kind == UNPARSABLE


class TestNonIntegralFloatPrecision:
    def test_non_integral_float_no_precision_fails(self):
        # 0.1 非整 float, 无精度契约 -> 明确失败, 不宣称恢复
        r = convert_exact(0.1)
        assert r.kind == UNPARSABLE and r.reason == "precision_undetermined"

    def test_explicit_decimals_contract_quantizes(self):
        # 显式 decimals 契约(明确声明精度)才量化, 非默认
        r = convert_exact(0.92, decimals=2)
        assert r.kind == EXACT and r.exact_value == Decimal("0.92")

    def test_no_default_precision_registered(self):
        # 本轮不注册三个业务字段精度
        for f in ("risk_score", "temperature_c", "humidity_pct"):
            assert is_precision_registered(f) is False


class TestCanonicalCompat:
    def test_convert_ok_feeds_to_canonical(self):
        # 精确输出必须能被 canonical 规范化器接受 (与其输入契约兼容)
        from src.database.canonical import _norm_number
        r = convert_exact(Decimal("30.5"))
        assert r.kind == EXACT
        assert _norm_number(r.exact_value) == "30.5"

    def test_unparsable_not_merged(self):
        # 非整 float 未确认精度 -> unparsable; 未来应走 c4_verify, 不入正常队列
        r = convert_exact(25.5)
        assert r.kind == UNPARSABLE


class TestDecimalsValidity:
    # 任务 A: decimals 运行时校验 (任何格式化/scaleb/quantize 之前)
    def test_none_null_unchanged(self):
        assert convert_exact("30.5").kind == EXACT
        assert convert_exact(30.5).kind == UNPARSABLE  # 无精度契约 -> 仍失败

    def test_normal_non_neg_int_ok(self):
        assert convert_exact(0.92, decimals=2).kind == EXACT
        assert convert_exact("30", decimals=2).kind == EXACT

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            convert_exact(0.92, decimals=True)
        with pytest.raises(ValueError):
            convert_exact(0.92, decimals=False)

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            convert_exact(0.92, decimals=-1)

    def test_non_int_numeric_rejected(self):
        with pytest.raises(ValueError):
            convert_exact(0.92, decimals=2.5)

    def test_string_precision_rejected(self):
        with pytest.raises(ValueError):
            convert_exact(0.92, decimals="2")

    def test_above_tech_cap_rejected(self):
        from src.database.decimalize import _DECIMALS_MAX
        with pytest.raises(ValueError):
            convert_exact(0.92, decimals=_DECIMALS_MAX + 1)

    def test_validation_before_any_work(self):
        # 非法 decimals 不应触发格式化/scaleb (不会因 input 类型先抛别的错)
        with pytest.raises(ValueError):
            convert_exact(object(), decimals=-1)


class TestUniformStringQuantize:
    # 任务 C: 显式 decimals 下 "30" 与 "30.5" 统一量化, 同契约等价输入规范一致
    def test_string_30_and_30_5_same_precision_consistent(self):
        r30 = convert_exact("30", decimals=2)
        r30_5 = convert_exact("30.5", decimals=2)
        assert r30.kind == EXACT and r30_5.kind == EXACT
        assert r30.exact_value == Decimal("30.00")
        assert r30_5.exact_value == Decimal("30.50")
        # 30.0 与 30 等价数值 -> 契约下同 30.00
        assert convert_exact(30.0, decimals=2).exact_value == Decimal("30.00")

    def test_no_default_quantize_without_decimals(self):
        # 未传 decimals -> 不量化, "30" 保持精确 30 (不补 .00)
        r = convert_exact("30")
        assert r.kind == EXACT and r.exact_value == Decimal("30")