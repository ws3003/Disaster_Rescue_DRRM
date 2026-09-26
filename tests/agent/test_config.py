"""P0-20 Config 单元测试。

覆盖: 正常读取 / 映射到 Agent·Trigger·Memory·Planner / Vision 路径 /
Communication retry·backoff·timeout / db.path 统一 / 缺段·缺字段·null·文件缺失回退 /
显式参数优先于 config / secret 不入 YAML / 类型错误 warn+回退。
"""

import os
import sys
import tempfile
import unittest
import warnings

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent import config as bconfig  # noqa: E402
from src.agent.agent import Agent  # noqa: E402


def _write_yaml(tmp, text):
    p = os.path.join(tmp, "config.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


FULL = """
agent:
  trigger:
    confirm_frames: 5
    high_risk_must_alert: false
    heartbeat_minutes: 7
  memory:
    cooldown_minutes: 20
  planner:
    max_steps: 2
db:
  path: "DATA_DB"
vision:
  snapshot_path: "VSNAP"
  recent_path: "VRECENT"
communication:
  channel: "mock"
  max_retries: 5
  backoff_seconds: [2, 8]
  timeout: 15
  log_path: "CLOG"
  pending_path: "CPEND"
rescue:
  snapshot_path: "RSNAP"
"""


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, text):
        return bconfig.load_config(_write_yaml(self._tmp.name, text))

    def test_t1_normal(self):
        cfg = self._cfg(FULL)
        self.assertEqual(cfg["agent"]["trigger"]["confirm_frames"], 5)
        self.assertEqual(cfg["agent"]["memory"]["cooldown_minutes"], 20)
        self.assertEqual(cfg["db"]["path"], "DATA_DB")
        self.assertEqual(cfg["vision"]["recent_path"], "VRECENT")

    def test_t6_missing_section_field_null(self):
        cfg = self._cfg("db:\n  path: X\n")
        self.assertEqual(cfg["agent"]["trigger"]["confirm_frames"], 3)  # 缺段→默认
        self.assertEqual(cfg["db"]["path"], "X")

    def test_t6_missing_file(self):
        cfg = bconfig.load_config("/nonexistent/config.yaml")
        self.assertEqual(cfg["agent"]["trigger"]["confirm_frames"], 3)
        self.assertEqual(cfg["db"]["path"], "data/drrm.db")

    def test_t9_type_error_warns_and_fallback(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cfg = self._cfg("agent:\n  trigger:\n    confirm_frames: abc\n")
        self.assertEqual(cfg["agent"]["trigger"]["confirm_frames"], 3)  # 回退默认
        self.assertTrue(any("confirm_frames" in str(x.message) for x in w))

    def test_t8_secret_not_in_yaml(self):
        cfg = self._cfg(FULL)
        self.assertNotIn("webhook", cfg["communication"])  # schema 无 webhook 字段
        text = open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8").read().lower().replace(" ", "")
        # 允许文档注释提及 env 名，但绝不允许出现 secret 赋值或真实 webhook URL
        self.assertNotIn("wecom_webhook_url=", text)
        self.assertNotIn("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=", text)


class AgentInjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "e2e.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg_dict(self):
        return {
            "agent": {"trigger": {"confirm_frames": 5, "high_risk_must_alert": True,
                                  "heartbeat_minutes": 7},
                      "memory": {"cooldown_minutes": 20},
                      "planner": {"max_steps": 2, "cooldown_minutes": 30}},
            "db": {"path": self.db},
            "vision": {"snapshot_path": os.path.join(self._tmp.name, "v.json"),
                       "recent_path": os.path.join(self._tmp.name, "v.jsonl"),
                       "scene_window_sec": 10},
            "communication": {"channel": "mock", "max_retries": 5,
                              "backoff_seconds": [2, 8], "timeout": 15,
                              "log_path": os.path.join(self._tmp.name, "c.log"),
                              "pending_path": os.path.join(self._tmp.name, "p.jsonl")},
            "rescue": {"snapshot_path": os.path.join(self._tmp.name, "r.json")},
        }

    def test_t2_agent_trigger_memory_planner_mapping(self):
        agent = Agent(config=self._cfg_dict())
        self.assertEqual(agent.trigger_manager.confirm_frames, 5)
        self.assertTrue(agent.trigger_manager.high_risk_must_alert)
        self.assertEqual(agent.memory.cooldown_minutes, 20)
        self.assertEqual(agent.planner.max_steps, 2)

    def test_t3_vision_paths_from_config(self):
        agent = Agent(config=self._cfg_dict())
        vt = agent.executor.registry.get("vision_tool")
        self.assertEqual(str(vt.snapshot_path), os.path.join(self._tmp.name, "v.json"))
        self.assertEqual(str(vt.recent_path), os.path.join(self._tmp.name, "v.jsonl"))

    def test_t4_comm_retry_backoff_timeout(self):
        agent = Agent(config=self._cfg_dict())
        ct = agent.executor.registry.get("communication_tool")
        self.assertEqual(ct.max_retries, 5)
        self.assertEqual(ct.backoff, (2.0, 8.0))
        self.assertEqual(ct.timeout, 15)

    def test_t5_db_path_unified(self):
        agent = Agent(config=self._cfg_dict())
        self.assertEqual(str(agent.db_path), self.db)
        dbt = agent.executor.registry.get("database_tool")
        self.assertEqual(str(dbt.db.db_path), self.db)

    def test_t7_explicit_beats_config(self):
        explicit = os.path.join(self._tmp.name, "explicit.db")
        agent = Agent(db_path=explicit, config=self._cfg_dict())  # config db= self.db
        self.assertEqual(str(agent.db_path), explicit)
        dbt = agent.executor.registry.get("database_tool")
        self.assertEqual(str(dbt.db.db_path), explicit)


if __name__ == "__main__":
    unittest.main(verbosity=2)