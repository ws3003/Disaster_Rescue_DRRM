"""B LLM→Agent 内部收口(方案 A) 单元测试。

覆盖: 正常 LLM task(record_alert) / cooldown 抑制第二次 send_alert /
通信失败不记录 / 非报警任务无 memory_alert / 多 action(含被 suppress) /
LLM 提案触发 rescue / 跨实例 cooldown 恢复(重启持久化)。
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


def _mem_alert_count(db_path):
    c = sqlite3.connect(db_path)
    n = c.execute("SELECT COUNT(*) FROM events WHERE type='memory_alert'").fetchone()[0]
    c.close()
    return n


def _cfg(dbpath, tmp, comm_channel="mock"):
    return {
        "agent": {"trigger": {"confirm_frames": 3, "high_risk_must_alert": True,
                              "heartbeat_minutes": 5},
                  "memory": {"cooldown_minutes": 30},
                  "planner": {"max_steps": 3, "cooldown_minutes": 30}},
        "db": {"path": dbpath},
        "vision": {"snapshot_path": os.path.join(tmp, "v.json"),
                   "recent_path": os.path.join(tmp, "v.jsonl"),
                   "scene_window_sec": 10},
        "communication": {"channel": comm_channel, "max_retries": 1,
                          "backoff_seconds": [1], "timeout": 5,
                          "log_path": os.path.join(tmp, "c.log"),
                          "pending_path": os.path.join(tmp, "p.jsonl")},
        "rescue": {"snapshot_path": os.path.join(tmp, "r.json")},
    }


def _alert_llm():
    return {
        "event_summary": "测试: person 1 HIGH Fall",
        "risk_explanations": [{"pid": 1, "reason": "Fall"}],
        "rescue_suggestions": ["优先救援"],
        "task_proposals": [
            {"action": "send_alert", "target": 1,
             "payload": {"severity": "HIGH", "priority": "HIGH",
                         "state": "Fall", "message": "高危倒地"}},
            {"action": "save_event", "target": 1, "payload": {"severity": "HIGH"}},
            {"action": "get_snapshot", "target": None, "payload": {}},
        ],
    }


class LlmEntryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "e2e.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_t1_normal_llm_task_records_alert(self):
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        res = agent.ingest_llm_output(_alert_llm())
        self.assertTrue(res[0]["ok"])
        self.assertEqual(res[0]["suppressed"], 0)
        self.assertTrue(any(r.get("tool") == "communication_tool" and r.get("ok")
                            for r in res[0]["execution"]))
        self.assertEqual(_mem_alert_count(self.db), 1)  # communication ok → record_alert

    def test_t2_cooldown_suppresses_second_send(self):
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        agent.ingest_llm_output(_alert_llm())
        res2 = agent.ingest_llm_output(_alert_llm())
        self.assertEqual(res2[0]["suppressed"], 1)
        self.assertEqual(_mem_alert_count(self.db), 1)  # 只记一次

    def test_t3_communication_failure_no_record(self):
        old = os.environ.get("WECOM_WEBHOOK_URL")
        os.environ["WECOM_WEBHOOK_URL"] = ""
        try:
            agent = Agent(config=_cfg(self.db, self._tmp.name, comm_channel="wecom"))
            res = agent.ingest_llm_output(_alert_llm())
        finally:
            if old is None:
                os.environ.pop("WECOM_WEBHOOK_URL", None)
            else:
                os.environ["WECOM_WEBHOOK_URL"] = old
        self.assertEqual(res[0]["suppressed"], 0)
        self.assertEqual(_mem_alert_count(self.db), 0)  # 通信失败 → 无 record

    def test_t4_non_alert_task_no_memory_alert(self):
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        llm = {"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
               "task_proposals": [
                   {"action": "get_snapshot", "target": None, "payload": {}},
                   {"action": "save_event", "target": 1, "payload": {"severity": "LOW"}},
               ]}
        res = agent.ingest_llm_output(llm)
        self.assertTrue(res[0]["ok"])
        self.assertEqual(_mem_alert_count(self.db), 0)

    def test_t5_multiaction_partial_suppress(self):
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        agent.ingest_llm_output(_alert_llm())
        res2 = agent.ingest_llm_output(_alert_llm())
        self.assertEqual(res2[0]["suppressed"], 1)
        executed_ok = {r.get("tool") for r in res2[0]["execution"] if r.get("ok")}
        self.assertIn("database_tool", executed_ok)  # save_event 仍执行
        self.assertIn("vision_tool", executed_ok)    # get_snapshot 仍执行

    def test_t6_llm_rescue_proposals(self):
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        llm = {"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
               "task_proposals": [
                   {"action": "get_ranking", "target": None, "payload": {}},
                   {"action": "assess_person", "target": 1, "payload": {}},
               ]}
        res = agent.ingest_llm_output(llm)
        tools = {r.get("tool") for r in res[0]["execution"]}
        self.assertIn("rescue_tool", tools)
        self.assertEqual(_mem_alert_count(self.db), 0)


class RestartPersistenceTest(unittest.TestCase):
    def test_cooldown_survives_agent_restart(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "e2e.db")
        try:
            a1 = Agent(config=_cfg(db, tmp.name))
            a1.ingest_llm_output(_alert_llm())
            self.assertEqual(_mem_alert_count(db), 1)

            a2 = Agent(config=_cfg(db, tmp.name))   # 同一 SQLite → 恢复 cooldown
            res2 = a2.ingest_llm_output(_alert_llm())
            self.assertEqual(res2[0]["suppressed"], 1)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)