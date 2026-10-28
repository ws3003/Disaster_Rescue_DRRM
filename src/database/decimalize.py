"""V4.4 C4-01 阶段 3-A: 字段级精确数值转换辅助 (纯函数)。

设计目标:
- 独立、纯函数; 不访问 DB/文件/网络/硬件/全局业务状态。
- 只转换"可精确"的输入 (int / Decimal / 合法十进制字符串 / 整数值 float)。
- 不依赖未确认业务精度: 不默认小数位、不默认 quantize、不猜测舍入。
- 非整 float 在无显式精度契约(decimals=None)时明确失败, 不宣称恢复原始十进制精度。
- 不用 `Decimal(float_value)` 接受二进制浮点近似。
- None 与字段缺失/非法字符串/非有限值 用带 kind 的结果区分, 不以笼统 None 混淆。

输出兼容性: 本模块只产 Decimal/int-kind 的"精确"值或明确的 unparsable 失败;
调用方未来须把"精确"投给 canonical.classify, 把失败投给 c4_verify(待核验), 不得入正常同步队列。
本模块不调用 canonical/DAO/DB, 不接入生产。

结果语义 (重要):
- 本模块的 `kind` 是 decimalize 自己的结果分类; `reason` 是模块内部诊断信息。
- `kind`/`reason` 均不能未经映射就直接当作 C4 稳定原因码。
  特别地: 本地 `kind="unparsable"` 与 C4 原因码 `unparsable` 同名, 不代表语义相同或可直接互换。
- 未来接入方必须显式检查 `Result.kind`; 无法安全规范化的数据应进入待核验流程, 不得直接入正常同步队列。
- `None`(kind='none') 与"字段缺失"的区别由调用方通过键存在性判断。
- 只有存在明确、统一的精度契约时, 调用方才可显式传入 `decimals`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

# 可识别结果 kind (最小状态集合)
EXACT = "exact"          # 精确十进制值 (可无损转换)
EXACT_INT = "exact_int"  # 整数值 float 经 int 无损 (精度无损)
NONE = "none"            # 显式 None (与"字段缺失"由调用方用存在性区分)
UNPARSABLE = "unparsable"  # 无法安全转换 (需进入待核验)

# 未注册精度的字段占位 (本轮三个字段均不注册, 完整列表后续由业务确认填充)
#   risk_score / temperature_c / humidity_pct 均无正式精度 -> 不在本表
FIELD_PRECISION: dict[str, int] = {}

# 技术上限: decimals 允许的最大位数 (技术防护, 非业务精度规则)。
# 依据: Python decimal 模块默认上下文精度为 28, 超出会触发 InvalidOperation/资源风险;
# 该值仅为资源/健壮性上限, 不代表任何业务字段的小数位契约。
_DECIMALS_MAX = 28


def _validate_decimals(decimals) -> None:
    if decimals is None:
        return
    if (not isinstance(decimals, int)
            or isinstance(decimals, bool)
            or decimals < 0
            or decimals > _DECIMALS_MAX):
        raise ValueError(
            f"decimals 必须为 0..{_DECIMALS_MAX} 的整数(不含 bool), 得到: {decimals!r}"
        )


@dataclass(frozen=True)
class Result:
    kind: str
    value: Decimal | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind in (EXACT, EXACT_INT)

    @property
    def exact_value(self) -> Decimal:
        # 仅当 ok 时可用
        assert self.kind in (EXACT, EXACT_INT) and self.value is not None
        return self.value


def _finite_decimal(text: str) -> Decimal | None:
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None  # NaN/Inf 不静默
    return d


def convert_exact(value: Any, *, decimals: int | None = None) -> Result:
    """将业务数值精确转换为 Decimal; 不静默修复/截断/舍入。

    - decimals 仅当调用方显式持有该字段的精度契约时才提供; 未提供则不做 quantize。
    - 有 decimals 时按 ROUND_HALF_EVEN 显式量化 (仅用于明确契约场景)。
    - None -> kind='none'; 空串/非法串/非有限/bool/非整 float(无精度) -> unparsable。
    """
    if value is None:
        return Result(NONE)
    _validate_decimals(decimals)  # 校验在任何格式化/scaleb/quantize 之前
    if isinstance(value, bool):
        return Result(UNPARSABLE, reason="bool_not_numeric")
    if isinstance(value, int):
        return Result(EXACT, Decimal(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            return Result(UNPARSABLE, reason="non_finite")
        return Result(EXACT, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return Result(UNPARSABLE, reason="non_finite")
        if value.is_integer():
            # 整数值 float: 整数语义无损 (不走 Decimal(float))
            return Result(EXACT_INT, Decimal(int(value)))
        # 非整 float: 精度语义未确认 -> 不宣称恢复; 仅当显式 decimals 契约才量化
        if decimals is None:
            return Result(UNPARSABLE, reason="precision_undetermined")
        d = _finite_decimal(f"{value:.{decimals}f}")  # 显式精度契约下的十进制字符串
        if d is None:
            return Result(UNPARSABLE, reason="invalid")
        return Result(EXACT, d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_EVEN))
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return Result(UNPARSABLE, reason="empty_string")
        if decimals is not None:
            # 显式量化契约 (明确声明精度): 所有输入在契约下统一量化, 保证同精度等价输入一致
            d = _finite_decimal(s)
            if d is None:
                return Result(UNPARSABLE, reason="invalid")
            return Result(EXACT, d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_EVEN))
        d = _finite_decimal(s)
        if d is None:
            return Result(UNPARSABLE, reason="invalid")
        return Result(EXACT, d)
    return Result(UNPARSABLE, reason="unsupported_type")


def is_precision_registered(field: str) -> bool:
    """字段是否已有注册精度契约 (本轮三个字段均未注册 -> False)。"""
    return field in FIELD_PRECISION