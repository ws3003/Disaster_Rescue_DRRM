"""传感器不可变数据模型（阶段 A1）。

只含 A1 必需字段；不含告警/数据库/视频/OSS 状态。
时间使用带时区的 ISO 8601 字符串。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    """一次有效传感器读数。

    temperature_c: 温度（℃）必填。
    humidity_pct:  湿度（%RH）可空；None 表示本次未测得/未提供，
                   不代表真实采集链路可以长期缺少湿度（阶段 B 按 SRS 确认）。
    captured_at:   带时区的 ISO 8601 时间字符串。
    """

    temperature_c: float
    humidity_pct: float | None
    captured_at: str