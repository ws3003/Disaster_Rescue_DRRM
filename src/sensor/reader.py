"""传感器读取器接口与 Fake（阶段 A1，纯软件）。

- 不在模块顶层导入 adafruit/board/GPIO 等硬件依赖（Windows 可无副作用导入）。
- 成功返回 Reading；失败抛 SensorReadError（驱动异常经 __cause__ 保留）。
- 本阶段不实现真实 DHT11 GPIO 适配器；FakeReader 仅用于测试/Mock。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Protocol, Tuple

from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError
from src.sensor.validate import build_reading

# 原始样本提供者: 返回 (temperature, humidity)；可抛任意异常。
SampleProvider = Callable[[], Tuple[Any, Any]]


def _now_iso() -> str:
    """生成带时区的 ISO 8601 时间（与 E9 数据层 _now_iso 一致）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SensorReader(Protocol):
    """读取一次传感器读数（成功返回 Reading；失败抛 SensorReadError）。

    now: 可选时钟注入（返回带时区 ISO 8601），便于测试断言 captured_at。
    """

    def read(self, now: Callable[[], str] | None = None) -> Reading: ...


def read_sample(
    provider: SampleProvider,
    now: Callable[[], str] | None = None,
) -> Reading:
    """从 provider 取原始样本、校验并构造 Reading。

    成功返回 Reading；provider 抛驱动异常 → 包装为 SensorReadError(保留 __cause__)；
    校验不过 → SensorReadError。绝不返回虚假有效 Reading。
    """
    ts = (now or _now_iso)()
    try:
        temperature, humidity = provider()
    except SensorReadError:
        raise
    except Exception as exc:  # 驱动/传输异常统一转为读取失败
        raise SensorReadError(
            f"传感器读取失败: {type(exc).__name__}: {exc}"
        ) from exc
    return build_reading(temperature, humidity, ts)


class FakeReader:
    """仅供软件测试/Windows Mock 的读取器；不代表真实 DHT11。"""

    def __init__(self, provider: SampleProvider) -> None:
        self._provider = provider

    def read(self, now: Callable[[], str] | None = None) -> Reading:
        return read_sample(self._provider, now)