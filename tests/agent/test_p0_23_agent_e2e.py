"""P0-23 Production Agent E2E (受控 Edge Event Fixture)。

目标: 使用符合 Edge 契约的 person_events fixture，经真实 production 处理路径
      person_events → person_events_to_frame_record → Agent.process_frame
      → 真实 Trigger/Memory/Planner/Executor/ToolRegistry/Tools/SQLite。

分级: 本测试 = "Agent E2E PASS with real Edge event fixture"
      绝不是 "Live Camera E2E"（后者因既有 MediaPipe asset path 问题单独 BLOCKED）。

隔离:
  - 用临时 SQLite(db_path) 避免污染 data/drrm.db，前后快照(delta)判定证据;
  - Communication 复用现有 MockChannel(仅写 communication_log.jsonl，gitignored), 不发真实 WeCom;
  - 不触碰既有测试数据 / 历史 untracked 文件。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent.agent import Agent  # noqa: E402
from src.agent.input_adapter import person_events_to_frame_record  # noqa: E402

COMM_LOG = os.path.join(ROOT, "data", "communication_log.jsonl")

# 受控 Edge 契约 fixture (符合 feature/v4.0-edge person_events 结构)
EDGE_FIXTURE = [
    {"person_id": 1, "state": "Fall", "risk_score": 0.6077, "priority": "HIGH"},
    {"person_id": 2, "state": "Standing", "risk_score": 0.1, "priority": "LOW"},
]


def _db_counts(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return (
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM agent_logs").fetchone()[0],
        )
    finally:
        conn.close()


def _comm_lines(path):
    if not os.path.exists(path):
        return 0
    return sum(1 for line in open(path, encoding="utf-8") if line.strip())


class P0_23ProductionAgentE2E(unittest.TestCase):
    def test_fixture_full_chain(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "e2e.db")

        # 真实 Agent(先用默认 DB 初始化建表), 再取 before 快照
        agent = Agent(db_path=db_path)
        events0, tasks0, logs0 = _db_counts(db_path)
        comm0 = _comm_lines(COMM_LOG)

        # 真实输入路径: fixture -> input_adapter -> 真实 Agent.process_frame
        frame_record = person_events_to_frame_record(EDGE_FIXTURE)
        results = agent.process_frame(frame_record)

        events1, tasks1, logs1 = _db_counts(db_path)
        comm1 = _comm_lines(COMM_LOG)

        # --- Trigger 证据: 至少一次 HIGH_RISK 触发 ---
        self.assertIsInstance(results, list)
        high = [r for r in results if isinstance(r, dict)
                and r.get("trigger_type") == "HIGH_RISK"]
        self.assertGreaterEqual(len(high), 1, "fixture Fall/HIGH 未触发 HIGH_RISK")

        # --- Planner 证据: 每个触发结果含 plan, actions ≤3 且在 whitelist 内 ---
        for r in results:
            if not isinstance(r, dict):
                continue
            if "HIGH_RISK" != r.get("trigger_type"):
                continue
            plan = r.get("plan") or {}
            actions = plan.get("actions", [])
            self.assertLessEqual(len(actions), 3, "actions 超过 3")
            for a in actions:
                tool = a.get("tool")
                self.assertIn(tool, {"vision_tool", "rescue_tool",
                                     "communication_tool", "database_tool"},
                              f"action 指向未注册工具: {tool}")
            self.assertIn("plan_trace", plan)

        # --- Executor / Tool 证据: DB 增量 ---
        self.assertGreater(events1, events0, "events 无增量 (工具未执行落库)")
        self.assertGreater(tasks1, tasks0, "tasks 无增量 (任务未持久化)")
        self.assertGreater(logs1, logs0, "agent_logs 无增量 (工具调用未审计)")

        # --- Communication 证据: MockChannel 日志增量; 未触达真实 WeCom ---
        self.assertGreater(comm1, comm0, "MockChannel 日志无增量")


if __name__ == "__main__":
    unittest.main(verbosity=2)