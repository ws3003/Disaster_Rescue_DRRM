"""E7 高温告警状态机（V4.4 E7a，纯软件）。

职责：
- 连续超温确认、活动告警管理、连续恢复确认、活动告警去重。
- 只对“有效样本”处理告警；无效/读取失败由 SampledSensorLoop 处理（on_read_fault 重置计数）。
- 温度触发告警；湿度只采集/记录、不参与告警判定。
- 写库失败不得推进依赖此写入的状态、不得报告成功；错误可诊断（抛 SensorPersistError）。
- 生成的 alert_id 及温度/阈值快照通过返回事件交给调用方（供未来 E8 使用，本模块不触发 E8）。

不修改 events / V4.3 融合链；不写录像/上传/重试。
"""

from __future__ import annotations

from datetime import datetime

from src.database.db import Database
from src.sensor.config import SensorConfig

# 传感器温度告警类型（独立于 events 融合，对应 E7-05）
ALERT_TYPE = "sensor_hightemp"

STATE_NORMAL = "normal"
STATE_CONFIRM_HIGH = "confirm_high"
STATE_ACTIVE = "active"
STATE_CONFIRM_RECOVERY = "confirm_recovery"


class SensorPersistError(ValueError):
    """传感器告警持久化失败（可诊断，不被静默吞掉）。"""


def _ts(iso: str) -> datetime:
    text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return datetime.fromisoformat(text)


class HighTempAlarm:
    """温度高温告警状态机（内存态 + DAO 持久化）。"""

    def __init__(self, cfg: SensorConfig, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.state = STATE_NORMAL
        self.high_count = 0
        self.level2_count = 0
        self.recovery_count = 0
        self.active_id: str | None = None
        self.active_level: int | None = None  # 活动/升级级别 1|2（单级模式恒 1）
        self._last_resolved_ts: str | None = None

    # ---- 阈值辅助（单级兼容：无 level2/recovery 时退回到一级阈值） ----
    def _l1(self) -> float:
        return float(self.cfg.temperature_threshold)

    def _l2(self) -> float | None:
        return None if self.cfg.level2_threshold is None else float(self.cfg.level2_threshold)

    def _rv(self) -> float:
        if self.cfg.recovery_threshold is not None:
            return float(self.cfg.recovery_threshold)
        return self._l1()  # 单级语义: 低于一级阈值即进入恢复判定

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat()

    # ---- DAO 辅助 ----
    def _find_active(self) -> dict | None:
        rows = self.db.query_sensor_alerts(status="active", type=ALERT_TYPE, limit=1)
        return rows[0] if rows else None

    def _create_alert(self, temp: float, captured_at: str, threshold: float) -> str:
        # 经 C4 bridge 创建 sensor alert: 单事务内业务写 + Outbox/verify 分流。
        # occurred_at 在此唯一确定一次, 同一值同时进入 canonical(hash) 与持久化, 不被核心重新生成。
        from src.sync import bridge as c4_bridge
        occurred_at = datetime.now().astimezone().isoformat()
        res = c4_bridge.create_sync_record(
            self.db,
            record_type="sensor_alert",
            business={
                "type": ALERT_TYPE,
                "temperature": temp,
                "threshold": threshold,
                "device_captured_at": captured_at,
                "occurred_at": occurred_at,
            },
        )
        return res["source_record_id"]

    def _resolve_alert(self, alert_id: str, resolved_at: str) -> None:
        ok = self.db.update_sensor_alert_status(alert_id, "resolved", resolved_at=resolved_at)
        if not ok:
            raise SensorPersistError(f"告警 {alert_id!r} 不存在或状态未变更，恢复失败")

    # ---- 冷却 ----
    def _in_cooldown(self, captured_at: str) -> bool:
        if self._last_resolved_ts is None or self.cfg.cooldown_seconds <= 0:
            return False
        try:
            delta = (_ts(captured_at) - _ts(self._last_resolved_ts)).total_seconds()
        except ValueError:
            raise SensorPersistError(f"无法解析时间(冷却判定): {captured_at!r}")
        return delta < self.cfg.cooldown_seconds

    # ---- 供电给 loop 的状态推进 ----
    def process(self, temperature: float, humidity=None, captured_at: str = "") -> dict | None:
        """输入一个有效样本（loop 已保证有效并已持久化读数），推进告警状态。

        返回事件 dict（供 E7 loop 输出、E8 证据消费者消费）或 None。
        写库失败时抛 SensorPersistError；调用方(loop)负责记录，不推进内存态。

        事件语义（区分 首次/升级/持续/恢复）：
          - alert_created   首次告警创建。level∈{1,2}（二级模式下若首次已连达 L2，直接创建为 level 2）。
          - alert_upgraded  活动的一级告警升级到二级（同一 alert_id, 不新建/不关联断裂）。
          - alert_resolved  恢复正常。
          - 持续超温（已活动且未升级/未恢复）→ None（去重, 不重复通知）。
        """
        T = float(temperature)
        l1, l2, rv = self._l1(), self._l2(), self._rv()
        two_level = l2 is not None

        if self.state in (STATE_ACTIVE, STATE_CONFIRM_RECOVERY):
            # ---- 已存在活动告警：升级 / 持续 / 恢复 ----
            trigger = l2 if two_level else l1
            if T >= trigger:
                self.recovery_count = 0
                if two_level and T >= l2:
                    self.level2_count += 1
                    if self.active_level is not None and self.active_level < 2 \
                            and self.level2_count >= self.cfg.confirm_high_count:
                        self.active_level = 2  # 同一告警升级, 不新建
                        self.level2_count = 0
                        return {"type": "alert_upgraded", "alert_id": self.active_id,
                                "level": 2, "temperature": T,
                                "occurred_at": captured_at or self._now_iso()}
                    return None  # 已活动且达/超二级（或尚在二级确认累计）：无新事件
                self.level2_count = 0
                self.state = STATE_ACTIVE
                return None  # 单级已活动 / 两级·一级区间持续：去重
            if T >= rv:
                self.level2_count = 0
                return None  # 邻近阈值（≥恢复阈值但<触发）: 仍活动, 不推进也不恢复
            # T < rv: 恢复判定
            self.high_count = 0
            self.level2_count = 0
            self.recovery_count += 1
            self.state = STATE_CONFIRM_RECOVERY
            if self.recovery_count >= self.cfg.confirm_recovery_count:
                alert_id = self.active_id
                resolved_at = captured_at or self._now_iso()
                if alert_id is None:
                    row = self._find_active()
                    if row is not None:
                        alert_id = row["alert_id"]
                if alert_id is not None:
                    self._resolve_alert(alert_id, resolved_at)
                    self._last_resolved_ts = resolved_at
                self.state = STATE_NORMAL
                self.recovery_count = 0
                self.active_id = None
                self.active_level = None
                return {"type": "alert_resolved", "alert_id": alert_id,
                        "occurred_at": resolved_at}
            return None

        # ---- normal / confirm_high（无活动告警, 累计高温确认） ----
        if T >= l1:
            self.recovery_count = 0
            self.high_count += 1
            self.level2_count = (self.level2_count + 1) if (two_level and T >= l2) else 0
            if self.high_count >= self.cfg.confirm_high_count:
                existing = self._find_active()
                if existing is not None:
                    self.active_id = existing["alert_id"]
                    self.state = STATE_ACTIVE
                    self.active_level = 1
                    return None  # 已存在活动告警: 不重复
                if self._in_cooldown(captured_at):
                    return None  # 冷却期内不新建
                new_level = 2 if (two_level and self.level2_count >= self.cfg.confirm_high_count) else 1
                thr = l2 if new_level == 2 else l1
                alert_id = self._create_alert(T, captured_at, thr)
                self.active_id = alert_id
                self.state = STATE_ACTIVE
                self.active_level = new_level
                self.level2_count = 0
                return {"type": "alert_created", "alert_id": alert_id, "level": new_level,
                        "temperature": T, "threshold": thr,
                        "occurred_at": captured_at or self._now_iso(),
                        "device_captured_at": captured_at}
            self.state = STATE_CONFIRM_HIGH
            return None
        # T < l1（单级）或 T < l1 的任意区间（两级）: 未达触发, 打断累计
        self.high_count = 0
        self.level2_count = 0
        self.state = STATE_NORMAL
        return None

    def on_read_fault(self) -> None:
        """读取失败/断线：重置连续计数（不作为恢复），回到正常态。"""
        self.high_count = 0
        self.recovery_count = 0
        self.state = STATE_NORMAL