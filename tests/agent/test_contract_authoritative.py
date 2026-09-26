"""A→B Contract(Phase1) 权威性测试。

验证: observation/persons 为 Edge FACT 唯一权威，忽略 LLM payload 伪造的
state/risk/priority；target 缺失/不在 observation → 拒绝；缺 observation_id/结构错 → contract 错误；
duplicate person_id → 后者覆盖；多人员；authoritative cooldown；重启恢复；legacy 兼容。
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent.agent import Agent  # noqa: E402


def _cfg(db, tmp):
    return {
        "agent": {"trigger": {"confirm_frames": 3, "high_risk_must_alert": True,
                              "heartbeat_minutes": 5},
                  "memory": {"cooldown_minutes": 30},
                  "planner": {"max_steps": 3, "cooldown_minutes": 30}},
        "db": {"path": db},
        "vision": {"snapshot_path": os.path.join(tmp, "v.json"),
                   "recent_path": os.path.join(tmp, "v.jsonl")},
        "communication": {"channel": "mock", "max_retries": 1, "backoff_seconds": [1],
                          "timeout": 5, "log_path": os.path.join(tmp, "c.log"),
                          "pending_path": os.path.join(tmp, "p.jsonl")},
        "rescue": {"snapshot_path": os.path.join(tmp, "r.json")},
    }


def obs_llm(persons, proposals, observation_id="obs-1"):
    return {
        "contract_version": "0.1-observation-v1",
        "observation": {"observation_id": observation_id,
                        "timestamp": "2026-09-26T20:00:00+08:00", "frame_idx": None},
        "persons": persons,
        "event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
        "task_proposals": proposals,
    }


def _mem_alerts(db):
    c = sqlite3.connect(db)
    rows = c.execute(
        "SELECT person_id, payload FROM events WHERE type='memory_alert'").fetchall()
    c.close()
    out = []
    for pid, payload in rows:
        d = json.loads(payload)
        d["person_id"] = pid  # person_id 存于 events 列, 合并入视图
        out.append(d)
    return out


def _mem_alert_count(db):
    return len(_mem_alerts(db))


class ContractAuthorityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "e2e.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_edge_facts_win_over_llm_payload(self):
        # Edge: Fall/0.6077/HIGH；LLM payload 伪造 Standing/0.1/LOW → B 必须用 Edge
        llm = obs_llm(
            [{"person_id": 1, "state": "Fall", "risk_score": 0.6077, "priority": "HIGH"}],
            [{"action": "send_alert", "target": 1,
              "payload": {"message": "x", "state": "Standing",
                          "risk_score": 0.1, "priority": "LOW"}}])
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertEqual(r[0]["rejected"], 0)
        self.assertEqual(_mem_alert_count(self.db), 1)
        m = _mem_alerts(self.db)[0]
        self.assertEqual(m["state"], "Fall")          # Edge, 非 Standing
        self.assertEqual(m["risk_score"], 0.6077)      # Edge, 非 0.1
        self.assertEqual(m["priority"], "HIGH")        # Edge, 非 LOW
        # cooldown 只对权威状态生效
        self.assertTrue(agent.memory.should_cooldown(1, "Fall"))
        self.assertFalse(agent.memory.should_cooldown(1, "Standing"))

    def test_target_not_in_observation_rejected(self):
        llm = obs_llm([{"person_id": 1, "state": "Fall", "risk_score": 0.6, "priority": "HIGH"}],
                      [{"action": "send_alert", "target": 99, "payload": {"message": "x"}}])
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertEqual(r[0]["rejected"], 1)
        self.assertEqual(_mem_alert_count(self.db), 0)  # 未执行、未 record

    def test_target_null_rejected(self):
        llm = obs_llm([{"person_id": 1, "state": "Fall"}],
                      [{"action": "send_alert", "target": None, "payload": {"message": "x"}}])
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertEqual(r[0]["rejected"], 1)
        self.assertEqual(_mem_alert_count(self.db), 0)

    def test_missing_observation_id_contract_error(self):
        llm = obs_llm([{"person_id": 1, "state": "Fall"}],
                      [{"action": "send_alert", "target": 1, "payload": {}}],
                      observation_id=None)
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertFalse(r[0]["ok"])
        self.assertEqual(r[0]["stage"], "contract")

    def test_observation_without_persons_contract_error(self):
        llm = obs_llm([], [])
        llm["persons"] = "not-a-list"
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertFalse(r[0]["ok"])
        self.assertEqual(r[0]["stage"], "contract")

    def test_duplicate_person_id_last_wins(self):
        llm = obs_llm(
            [{"person_id": 1, "state": "Fall"}, {"person_id": 1, "state": "Standing"}],
            [{"action": "send_alert", "target": 1, "payload": {"message": "x"}}])
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertEqual(r[0]["rejected"], 0)
        self.assertEqual(_mem_alert_count(self.db), 1)
        self.assertEqual(_mem_alerts(self.db)[0]["state"], "Standing")  # 后者覆盖
        self.assertTrue(agent.memory.should_cooldown(1, "Standing"))

    def test_multi_person_separate_alerts(self):
        llm = obs_llm(
            [{"person_id": 1, "state": "Fall", "risk_score": 0.6, "priority": "HIGH"},
             {"person_id": 2, "state": "Sitting", "risk_score": 0.3, "priority": "LOW"}],
            [{"action": "send_alert", "target": 1, "payload": {}},
             {"action": "send_alert", "target": 2, "payload": {}}])
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(llm)
        self.assertEqual(r[0]["rejected"], 0)
        self.assertEqual(_mem_alert_count(self.db), 2)
        states = {m["person_id"]: m["state"] for m in _mem_alerts(self.db)}
        self.assertEqual(states[1], "Fall")
        self.assertEqual(states[2], "Sitting")

    def test_authoritative_cooldown_suppresses_second(self):
        llm = obs_llm([{"person_id": 1, "state": "Fall"}],
                      [{"action": "send_alert", "target": 1, "payload": {}}])
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        agent.ingest_llm_output(llm)
        self.assertEqual(_mem_alert_count(self.db), 1)
        r2 = agent.ingest_llm_output(llm)
        self.assertEqual(r2[0]["suppressed"], 1)

    def test_restart_persists_authoritative_cooldown(self):
        llm = obs_llm([{"person_id": 1, "state": "Fall"}],
                      [{"action": "send_alert", "target": 1, "payload": {}}])
        a1 = Agent(config=_cfg(self.db, self._tmp.name))
        a1.ingest_llm_output(llm)
        a2 = Agent(config=_cfg(self.db, self._tmp.name))
        r = a2.ingest_llm_output(llm)
        self.assertEqual(r[0]["suppressed"], 1)  # 同一 SQLite 恢复

    def test_legacy_contract_still_works(self):
        legacy = {"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
                  "task_proposals": [
                      {"action": "send_alert", "target": 1,
                       "payload": {"severity": "HIGH", "priority": "HIGH",
                                   "state": "Fall", "message": "m"}}]}
        agent = Agent(config=_cfg(self.db, self._tmp.name))
        r = agent.ingest_llm_output(legacy)
        self.assertTrue(r[0]["ok"])
        self.assertEqual(_mem_alert_count(self.db), 1)  # legacy 仍 record


if __name__ == "__main__":
    unittest.main(verbosity=2)