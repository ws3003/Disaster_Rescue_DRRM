"""E7 采样调度循环（V4.4 E7a / E7-07，A3）。

职责：
- 按配置周期采样、调用读取适配器；
- 区分三类结果：
  * 正常有效读数 → 存 readings + 交 HighTempAlarm 判定（并落 ONLINE 状态）；
  * 读取故障（设备断链/CRC/文件缺失/驱动异常）→ 设备级故障持久化 + 离线判定；
  * 数据无效（越界/NaN/Inf/类型非法）→ 不持久化 readings、不触发告警；
    记录为 degraded（数据质量问题，不视为设备断线、不判离线）。
- 支持从数据库恢复离线/降质状态（重启后），并记录恢复时间与故障历史。

时钟/等待可注入（sleep），保证 Windows 离线测试无需真实等待与硬件。
"""

from __future__ import annotations

import time
import warnings
from datetime import datetime
from typing import Callable

from src.database.db import Database
from src.sensor.alarm import HighTempAlarm, SensorPersistError
from src.sensor.config import SensorConfig
from src.sensor.reader import SensorReader, Reading
from src.sensor.sensor_errors import SensorReadError
from src.sensor.validate import is_valid_sample


def _now_iso() -> str:
    """带时区的 ISO 8601 时间（与 E9 数据层一致）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


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
        self.fault_count = 0          # 设备级连续读取失败（离线判定）
        self.quality_failures = 0     # 数据质量异常计数（不参与离线判定）
        self.offline = False          # 设备级离线（重启后经 restore_state 恢复）

    # ------------------------------------------------------------------
    # 状态持久化辅助（E7-07；故障写入失败时告警、绝不静默假装成功）
    # ------------------------------------------------------------------
    def _persist(self, fn: Callable[[], object], what: str) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"[SensorLoop] {what} 持久化失败({type(exc).__name__}: {exc})")

    def _set_offline(self, reason: str, ts: str) -> None:
        def _do() -> None:
            prev = self.db.get_sensor_status(self.cfg.sensor_id)
            first_at = prev.get("first_fault_at") if prev else None
            self.db.save_sensor_status(
                sensor_id=self.cfg.sensor_id,
                status="offline", fault_type="device", last_error=reason,
                first_fault_at=first_at or ts, last_fault_at=ts,
                consecutive_failures=self.fault_count,
                quality_failures=self.quality_failures,
                updated_at=ts,
            )
            self.db.record_sensor_fault_event(
                sensor_id=self.cfg.sensor_id, status="offline",
                fault_type="device", detail=reason, occurred_at=ts,
            )
        self._persist(_do, "设备故障状态")

    def _set_degraded(self, reason: str, ts: str) -> None:
        def _do() -> None:
            prev = self.db.get_sensor_status(self.cfg.sensor_id)
            first_at = prev.get("first_fault_at") if prev else None
            self.db.save_sensor_status(
                sensor_id=self.cfg.sensor_id,
                status="degraded", fault_type="data_quality", last_error=reason,
                first_fault_at=first_at or ts, last_fault_at=ts,
                consecutive_failures=self.fault_count,
                quality_failures=self.quality_failures,
                updated_at=ts,
            )
            self.db.record_sensor_fault_event(
                sensor_id=self.cfg.sensor_id, status="degraded",
                fault_type="data_quality", detail=reason, occurred_at=ts,
            )
        self._persist(_do, "数据质量状态")

    def _mark_recovered(self, ts: str) -> bool:
        """维护 online 状态；仅当从 degraded/offline 真实恢复才原子地记一条恢复事件并回填。

        - 已在 online / 首次：不写 recovered 事件、不刷新、不清历史 recovered_at（保留 prev）。
        - 真实转移：调用 db.apply_sensor_online（同一事务：状态更新 + 回填 + 恢复事件）。
        返回是否发生真实恢复转移（局部闭包容器，不新增实例状态；写异常时保守返回 False）。
        """
        box = [False]  # 闭包结果容器

        def _do() -> None:
            prev = self.db.get_sensor_status(self.cfg.sensor_id)
            was_bad = prev is not None and prev.get("status") in ("offline", "degraded")
            prev_rec = prev.get("recovered_at") if prev else None
            if was_bad:
                # 原子恢复：状态 online + 回填未恢复故障事件 + 一条 recovered 事件（同事务）
                self.db.apply_sensor_online(
                    sensor_id=self.cfg.sensor_id, status="online",
                    fault_type=None, last_error=None,
                    first_fault_at=None, last_fault_at=None, recovered_at=ts,
                    consecutive_failures=0, quality_failures=0, updated_at=ts,
                    rec_occurred_at=ts, backfill_event_id=None,  # 在事务内自动选未恢复故障
                )
            else:
                # 首次/已在 online：仅维护状态；保留历史 recovered_at，不记事件
                self.db.save_sensor_status(
                    sensor_id=self.cfg.sensor_id, status="online",
                    fault_type=None, last_error=None,
                    first_fault_at=None, last_fault_at=None, recovered_at=prev_rec,
                    consecutive_failures=0, quality_failures=0, updated_at=ts,
                )
            box[0] = was_bad

        self._persist(_do, "恢复状态")
        return box[0]

    # ------------------------------------------------------------------
    # 一次采样
    # ------------------------------------------------------------------
    def step(self, now: Callable[[], str] | None = None) -> dict:
        ts = (now or _now_iso)()
        try:
            reading: Reading = self.reader.read(now=lambda: ts)
        except SensorReadError as exc:
            # ---- 设备级读取故障：离线判定 + 故障持久化 ----
            self.fault_count += 1
            became_offline = self.fault_count >= self.cfg.offline_after_failures
            self.offline = self.offline or became_offline
            self.alarm.on_read_fault()
            self._set_offline(str(exc), ts)
            return {
                "status": "offline" if self.offline else "error",
                "offline": self.offline,
                "fault_count": self.fault_count,
                "event": None,
                "reason": str(exc),
            }

        # ---- 数据无效（越界/NaN/inf/类型）：不入 readings、不告警、不判离线 ----
        if not is_valid_sample(
            reading.temperature_c,
            reading.humidity_pct,
            temperature_min=self.cfg.temperature_min,
            temperature_max=self.cfg.temperature_max,
            humidity_min=self.cfg.humidity_min,
            humidity_max=self.cfg.humidity_max,
        ):
            self.quality_failures += 1
            self._set_degraded("样本超出允许量程", ts)
            return {
                "status": "invalid",
                "offline": self.offline,
                "fault_count": self.fault_count,
                "quality_failures": self.quality_failures,
                "event": None,
                "reason": "样本超出允许量程",
            }

        # ---- 正常有效读数：恢复在线 + 持久化 reading + 告警 ----
        self.fault_count = 0
        self.quality_failures = 0
        was_offline = self.offline
        self.offline = False
        transitioned = self._mark_recovered(ts)

        try:
            from src.sync import bridge as c4_bridge
            c4_bridge.create_sync_record(
                self.db,
                record_type="reading",
                business={
                    "sensor_id": self.cfg.sensor_id,
                    "temperature": c4_bridge.exact_number(reading.temperature_c),
                    "humidity": c4_bridge.exact_number(reading.humidity_pct),
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
            "quality_failures": 0,
            "event": event,
            "was_offline": was_offline,
            "recovered": transitioned,
        }

    def restore_state(self) -> dict:
        """从数据库恢复上次持久化的设备状态（重启后）。返回恢复摘要。

        幂等：已恢复(online/degraded)不会被错误还原为 offline；只有 last 状态为 offline
        才把本实例拉成离线/复位故障计数。
        """
        st = self.db.get_sensor_status(self.cfg.sensor_id)
        if not st:
            return {"restored": False, "status": None}
        self.fault_count = int(st.get("consecutive_failures") or 0)
        self.quality_failures = int(st.get("quality_failures") or 0)
        self.offline = st.get("status") == "offline"
        return {"restored": True, "status": st.get("status"), "offline": self.offline}

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