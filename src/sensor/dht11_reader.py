"""真实 DHT11 适配器（阶段 A2）。

- 实现 A1 的 SensorReader 协议：read(now=None) -> Reading。
- 复用 src.sensor 的 model / validate / reader 契约，不复制数据模型/校验。
- 真实硬件驱动（adafruit_dht）采用延迟导入：导入本模块不初始化 GPIO、不创建设备、不读传感器。
- 支持依赖注入（driver_factory）以便 Windows 用 Mock 测试真实驱动路径。
- 缺少驱动依赖时不静默回退到 FakeReader，而是抛可定位的 SensorReadError。
- 不设未经核实的 GPIO 默认值；pin 须由确认后的硬件映射注入。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.sensor import reader as _reader
from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError


class DHT11Reader:
    """真实 DHT11 读取器（延迟加载 adafruit_dht）。"""

    def __init__(
        self,
        pin: Optional[int] = None,
        driver_factory: Optional[Callable[[], Any]] = None,
        retry_count: int = 0,
    ) -> None:
        # pin 无默认值；须由确认后的扩展板/GPIO 映射注入。
        self._pin = pin
        # driver_factory: 注入真实或 Mock 驱动, 便于 Windows 单测。
        self._factory = driver_factory
        # read_retry_count：读取失败时的有限重试次数（0=仅尝试一次，2=最多三次）。
        self._retry_count = int(retry_count) if retry_count else 0
        self._driver = None

    def _ensure_driver(self) -> Any:
        """返回驱动实例（工厂注入优先；否则延迟 import adafruit_dht）。"""
        if self._driver is not None:
            return self._driver
        if self._factory is not None:
            self._driver = self._factory()
            return self._driver
        try:
            import adafruit_dht  # 延迟导入; 仅在 read() 时触发
        except ImportError as exc:
            raise SensorReadError(
                "DHT11 驱动依赖缺失: 需在树莓派安装 adafruit-circuitpython-dht"
            ) from exc
        if self._pin is None:
            raise SensorReadError(
                "未指定 DHT GPIO 引脚（需硬件映射确认后注入）"
            )
        self._driver = adafruit_dht.DHT11(self._pin)
        return self._driver

    def read(self, now: Callable[[], str] | None = None) -> Reading:
        """读取一次读数；成功返回 Reading，失败抛 SensorReadError。

        校验与异常转换委托给 A1 reader.read_sample / build_reading。
        仅对读取失败进行有限重试（retry_count 次重试，0=单次）：驱动初始化/依赖缺失/
        未确认 pin 属配置错误，不重试；最终失败保留最后一次 SensorReadError 上抛，
        绝不吞异常、不伪造数据、不回退 FakeReader。
        """
        driver = self._ensure_driver()  # 配置/依赖错误只暴露一次，不进入重试
        attempts = int(self._retry_count) + 1  # retry_count=N → 共尝试 N+1 次
        last: SensorReadError | None = None
        for _ in range(attempts):
            try:
                return _reader.read_sample(
                    lambda: (driver.temperature, driver.humidity),
                    now=now,
                )
            except SensorReadError as exc:
                last = exc
        assert last is not None
        raise last

    def close(self) -> None:
        """释放驱动资源（若驱动对象提供 exit()）。关闭后再 read() 会惰性重建。

        属可选资源清理接口；不触碰 GPIO、不读取传感器。
        """
        if self._driver is not None:
            exit_ = getattr(self._driver, "exit", None)
            if callable(exit_):
                try:
                    exit_()
                except Exception:  # noqa: BLE001 - 清理失败不应阻断关闭
                    pass
            self._driver = None