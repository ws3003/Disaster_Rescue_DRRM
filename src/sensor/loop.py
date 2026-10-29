"""E7 采样调度循环（V4.4 E7a）。

职责：
- 按配置周期采样、调用读取适配器；
- 区分 有效样本 / 读取异常(读取失败/无效)；
- 有效样本 → save_reading（仅有效读数持久化）并交 HighTempAlarm 判定；
- 读取异常 → 故障计数/离线判定，并通知告警模块(on_read_fault)重置计数（不作为恢复）；
- 结束时按约定关闭适配器资源（若有 close()）。

时钟/等待可注入（sleep），保证 Windows 离线测试无需真实等待与硬件。
"""

from __future__ import annotations

import time
from typing import Callable

from src.database.db import Database
from src.sensor.alarm import HighTempAlarm, SensorPersistError
from src.sensor.config import SensorConfig
from src.sensor.reader import SensorReader, Reading
from src.sensor.sensor_errors import SensorReadError


class SampledSensorLoop:
    def __init__(
        self,
        cfg: SensorConfig,
        reader: SensorReader,
        alarm: HighTempAlarm,
        db: Database,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.reader = reader
        self.alarm = alarm
        self.db = db
        self._sleep = sleep
        self.fault_count = 0
        self.offline = False

    def step(self, now: Callable[[], str] | None = None) -> dict:
        """执行一次采样。返回 {status, offline, fault_count, event, reason?}。"""
        try:
            reading: Reading = self.reader.read(now=now)
        except SensorReadError as exc:
            self.fault_count += 1
            self.offline = self.fault_count >= self.cfg.offline_after_failures
            self.alarm.on_read_fault()
            return {
                "status": "error",
                "offline": self.offline,
                "fault_count": self.fault_count,
                "event": None,
                "reason": str(exc),
            }

        self.fault_count = 0
        self.offline = False
        # 仅有效读数持久化；写库失败不推进告警(也不吞错)。经 C4 bridge 单一事务写入并分流。
        try:
            from src.sync import bridge as c4_bridge
            c4_bridge.create_sync_record(
                self.db,
                record_type="reading",
                business={
                    "sensor_id": self.cfg.sensor_id,
                    "temperature": reading.temperature_c,
                    "humidity": reading.humidity_pct,
                    "device_captured_at": reading.captured_at,
                },
            )
        except SensorPersistError:
            raise
        except Exception as exc:
            raise SensorPersistError(f"读数持久化失败: {exc!r}") from exc

        event = self.alarm.process(
            reading.temperature_c, reading.humidity_pct, reading.captured_at
        )
        return {
            "status": "valid",
            "offline": False,
            "fault_count": 0,
            "event": event,
        }

    def run(self, steps: int, now: Callable[[], str] | None = None) -> list[dict]:
        results: list[dict] = []
        for _ in range(int(steps)):
            results.append(self.step(now=now))
            self._sleep(self.cfg.sample_period_seconds)
        return results

    def close(self) -> None:
        """关闭读取器资源（若适配器提供 close）。"""
        close = getattr(self.reader, "close", None)
        if callable(close):
            close()