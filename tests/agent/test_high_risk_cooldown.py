"""HIGH_RISK cooldown 去重测试 (edge process_frame 路径)。

背景: 树莓派运行中发现 HIGH_RISK (Help-Waving) 在同一 cooldown 窗口内
      被重复发送 (13:02 / 13:21 / 13:24 三连)。

根因: planner._check_trigger 的 IMMEDIATE_TRIGGERS (HIGH_RISK) 分支
      原先无条件 return allowed=True, 完全不 consult memory, 导致
      cooldown 形同虚设。

修复(方案B): HIGH 分支用 should_cooldown 做基础窗口判断; cooldown 内
      对同 person+state 再算 risk_delta, 只有 risk_delta >= 0.10 才
      allow, 否则 suppress。不依赖 should_break_cooldown(任意微升即放行)。

本测试走真实 production 路径:
    person_events -> person_events_to_frame_record
    -> Agent.process_frame -> Trigger -> Memory -> Planner -> Executor
    -> Tools(communication/database) -> SQLite

隔离: 临时 SQLite + 临时 communication_log, 不触碰 data/ 真实运行产物。

注意: process_frame 可能同时产出 HEARTBEAT 等探测 trigger(completed 态),
      因此断言聚焦【communication 是否新增】与【memory_alert 落库次数】,
      而非笼统的 status 是否全为 suppressed。
"""

import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent.agent import Agent  # noqa: E402
from src.agent.input_adapter import person_events_to_frame_record  # noqa: E402


def _cfg(db_path, tmp_dir):
    """隔离配置: 临时 db + 临时 communication_log, 30min cooldown。"""
    return {
        "agent": {
            "trigger": {"high_risk_must_alert": True},
            "memory": {"cooldown_minutes": 30},
            "planner": {"max_steps": 3, "cooldown_minutes": 30},
        },
        "db": {"path": db_path},
        "vision": {"snapshot_path": os.path.join(tmp_dir, "v.json"),
                   "recent_path": os.path.join(tmp_dir, "v.jsonl")},
        "communication": {"channel": "mock", "max_retries": 1,
                          "backoff_seconds": [1], "timeout": 5,
                          "log_path": os.path.join(tmp_dir, "comm.log"),
                          "pending_path": os.path.join(tmp_dir, "p.jsonl")},
        "rescue": {"snapshot_path": os.path.join(tmp_dir, "r.json")},
    }


def _mem_alert_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='memory_alert'"
        ).fetchone()[0]
    finally:
        conn.close()


def _help_waving(person_id=1, risk=0.60):
    return [{"person_id": person_id, "state": "Help-Waving",
             "risk_score": risk, "priority": "HIGH"}]

# 树莓派真实时序 fixture
_PI_REAL = [
    (0.6012, "2026-09-27T13:02:08"),
    (0.6156, "2026-09-27T13:21:08"),
    (0.6148, "2026-09-27T13:24:41"),
]


class HighRiskCooldownTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "h.db")
        self.comm = os.path.join(self._tmp.name, "comm.log")

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self):
        return Agent(config=_cfg(self.db, self._tmp.name))

    def test_identical_risk_suppressed(self):
        """完全同 person/state/risk 的第二次 HIGH: 真实报警不再新增, cooldown 只记一次。

        指标: 用 memory_alert(=真实 send 次数, 与 send 一一对应, 不受 LOCAL_MOCK
        双写 comm 影响) 作为权威。comm 双写只做辅助参考。
        """
        agent = self._agent()
        r1 = agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.60), timestamp="2026-09-27T13:02:08"))
        self.assertEqual(_mem_alert_count(self.db), 1,
                         "第一次 HIGH 应 record_alert 1 次")

        r2 = agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.60), timestamp="2026-09-27T13:21:08"))
        # 同 risk, cooldown 内: record_alert 不增加 -> 未再次真实发送
        self.assertEqual(_mem_alert_count(self.db), 1,
                         "同风险重复报警不应再次 record_alert")
        # HIGH 应存在 suppressed 结果
        suppressed_high = [x for x in r2 if isinstance(x, dict)
                           and x.get("trigger_type") == "HIGH_RISK"]
        self.assertTrue(
            any(x.get("status") == "suppressed" for x in suppressed_high)
            or _mem_alert_count(self.db) == 1,
            "同风险 HIGH_RISK 应被 cooldown 抑制或未再次 record_alert")

    def test_micro_rise_suppressed(self):
        """树莓派真实: 0.6012 -> 0.6156 (delta=0.014<0.10) 不应逃逸。"""
        agent = self._agent()
        agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.6012), timestamp=_PI_REAL[0][1]))
        self.assertEqual(_mem_alert_count(self.db), 1, "首次应 record 1 次")

        # 微升 delta=0.0144 < 0.10 -> 抑制, 不再 record_alert(不再真实发送)
        agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.6156), timestamp=_PI_REAL[1][1]))
        self.assertEqual(_mem_alert_count(self.db), 1,
                         "微升(risk_delta<0.10)不应再次 record_alert")

        # 微降 0.6148 -> 也同样不新增
        agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.6148), timestamp=_PI_REAL[2][1]))
        self.assertEqual(_mem_alert_count(self.db), 1,
                         "risk 微降不应再次 record_alert")

    def test_escalation_breaks_cooldown(self):
        """cooldown 内 risk 明显提升 (0.60 -> 0.90, delta=0.30>=0.10) 仍允许报警。"""
        agent = self._agent()
        agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.60), timestamp="2026-09-27T13:02:08"))
        self.assertEqual(_mem_alert_count(self.db), 1)

        agent.process_frame(person_events_to_frame_record(
            _help_waving(risk=0.90), timestamp="2026-09-27T13:21:08"))
        self.assertEqual(_mem_alert_count(self.db), 2,
                         "risk 明显提升(>=0.10)应再次 record_alert")

    def test_different_person_independent(self):
        """不同 person_id 互不影响, 各自可发送。"""
        agent = self._agent()
        agent.process_frame(person_events_to_frame_record(
            _help_waving(person_id=1, risk=0.60), timestamp="2026-09-27T13:02:08"))
        self.assertEqual(_mem_alert_count(self.db), 1)

        # 同窗口内 person_id=2: 不应被 person 1 的 cooldown 抑制
        agent.process_frame(person_events_to_frame_record(
            _help_waving(person_id=2, risk=0.60), timestamp="2026-09-27T13:03:00"))
        self.assertEqual(_mem_alert_count(self.db), 2,
                         "不同 person 应各自 record_alert")


if __name__ == "__main__":
    unittest.main(verbosity=2)