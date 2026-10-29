"""传感器读取错误（阶段 A1）。

统一表达"本次读取未产生有效读数"（驱动失败或数据校验失败）。
读取成功返回 Reading；读取失败抛出 SensorReadError。
原始驱动异常通过异常链（__cause__）保留。
"""

from __future__ import annotations


class SensorReadError(Exception):
    """传感器读取失败。"""