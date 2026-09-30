"""V4.1 Assistant 回归测试 (mock, 离线, 信息辅助)。

验证:
  1. scene_status 提问可回答
  2. risk_query 提问可回答(含风险提示)
  3. snapshot_query 返回图片信息
  4. assistant 不生成 task (SQLite 只读)
  5. assistant 不调用 communication_tool / rescue_tool
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant import classify  # noqa: E402
from src.assistant.context import AssistantContext  # noqa: E402
from src.assistant.responder import AssistantResponder  # noqa: E402


def _make_db(path, row=None):
    """建一个含 tasks 表的最小 db, 可预置一条任务。"""
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS tasks ("
                  "task_id TEXT PRIMARY KEY, trigger_event TEXT, plan_trace TEXT, "
                  "status TEXT, created TEXT, finished TEXT)")
        if row:
            c.execute("INSERT INTO tasks (task_id, trigger_event, plan_trace, status, "
                      "created, finished) VALUES (?,?,?,?,?,?)", row)


class AssistantTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "a.db")
        self.snap_dir = os.path.join(self._tmp.name, "snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)
        # 预置一条最近任务 + 一张快照, 模拟已有现场数据
        _make_db(
            self.db,
            row=("T-20260929-120000-abc",
                 "人员1 Fall 高风险", '[]', "completed",
                 "2026-09-29T12:00:00", "2026-09-29T12:01:00"),
        )
        Path(self.snap_dir, "20260929_120100_analysis.jpg").write_bytes(b"fake")

    def tearDown(self):
        self._tmp.cleanup()

    def _new_responder(self):
        # 隔离: 用临时空 vision 文件, 避免 AssistantResponder 读取真实 data/vision_persons.json
        vision_path = os.path.join(self._tmp.name, "vision_persons.json")
        Path(vision_path).write_text("{}", encoding="utf-8")
        ctx = AssistantContext(db_path=self.db, snapshot_dir=self.snap_dir,
                               vision_path=vision_path)
        return AssistantResponder(context=ctx)

    # ---- 1 / 2 / 3: 意图与回答 -------------------------------------------------
    def test_intent_classification(self):
        self.assertEqual(classify("现场怎么样"), "scene_status")
        self.assertEqual(classify("有没有危险"), "risk_query")
        self.assertEqual(classify("图片在哪里"), "snapshot_query")

    def test_scene_status_answers(self):
        r = self._new_responder()
        out = r.answer("现场怎么样")
        self.assertEqual(out["intent"], "scene_status")
        self.assertTrue(out["answer"])
        self.assertIn(out["sources"][0]["task_id"], "T-20260929-120000-abc")

    def test_risk_query_answers(self):
        r = self._new_responder()
        out = r.answer("有没有危险")
        self.assertEqual(out["intent"], "risk_query")
        self.assertTrue(any(k in out["answer"] for k in ("风险", "危险", "救援", "研判")))

    def test_snapshot_query_returns_image(self):
        r = self._new_responder()
        out = r.answer("图片在哪里")
        self.assertEqual(out["intent"], "snapshot_query")
        self.assertIn("20260929_120100_analysis.jpg", out["answer"])

    # ---- 4: 不生成 task ---------------------------------------------------------
    def test_assistant_does_not_generate_task(self):
        before = self._count_tasks()
        r = self._new_responder()
        for q in ("现场怎么样", "有没有危险", "图片在哪里"):
            r.answer(q)
        after = self._count_tasks()
        self.assertEqual(before, after, "assistant 不应新增任何任务行")

    # ---- 5: 不调用 communication / rescue 工具 ---------------------------------
    def test_assistant_does_not_call_tools(self):
        with patch("src.tools.communication_tool.CommunicationTool.run") as mcomm, \
             patch("src.tools.rescue_tool.RescueTool.run") as mrescue:
            r = self._new_responder()
            for q in ("现场怎么样", "有没有危险", "图片在哪里"):
                r.answer(q)
            mcomm.assert_not_called()
            mrescue.assert_not_called()

    def _count_tasks(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    # ---- V4.1 AMap route 语义: nearby 保持 / 附近医院怎么走 → route ----
    def test_nearby_intent(self):
        self.assertEqual(classify("附近有没有医院"), "nearby_query")

    def test_route_intent(self):
        self.assertEqual(classify("附近医院怎么走"), "route_query")
        self.assertEqual(classify("怎么去消防站"), "route_query")

    def test_dynamic_route_calls_query_route(self):
        fm = _FakeMap()
        vision_path = os.path.join(self._tmp.name, "vision_persons2.json")
        Path(vision_path).write_text("{}", encoding="utf-8")
        ctx = AssistantContext(db_path=self.db, snapshot_dir=self.snap_dir,
                               map_provider=fm, vision_path=vision_path)
        r = AssistantResponder(context=ctx)
        out = r.answer("附近医院怎么走")
        self.assertEqual(out["intent"], "route_query")
        self.assertIn(("现场", "医院"), fm.route_calls)  # query_route(现场→医院) 被调用
        self.assertIn("路线", out["answer"])
        self.assertIn("距离", out["answer"])

    def test_route_uses_nearby_poi_name(self):
        # 泛称"医院" → 先 query_nearby 取真实 POI 名 → query_route(现场, POI名)
        fm = _FakeMap(poi=[{"name": "南京市中心医院", "type": "医院", "distance": "800米"}])
        vision_path = os.path.join(self._tmp.name, "vision_persons3.json")
        Path(vision_path).write_text("{}", encoding="utf-8")
        ctx = AssistantContext(db_path=self.db, snapshot_dir=self.snap_dir,
                               map_provider=fm, vision_path=vision_path)
        r = AssistantResponder(context=ctx)
        out = r.answer("附近医院怎么走")
        self.assertEqual(out["intent"], "route_query")
        self.assertIn(("现场", "南京市中心医院"), fm.route_calls)
        self.assertIn("南京市中心医院", out["answer"])


class _FakeMap:
    """隔离用的 mock map provider(不依赖真实高德)。"""
    def __init__(self, poi=None):
        self.route_calls = []
        self._poi = poi or []

    def nearby(self, keyword=None, location=None):
        return {"ok": True, "poi": self._poi}

    def rescue_route(self, origin=None, destination=None):
        self.route_calls.append((origin, destination))
        return {"ok": True, "route": {"distance_km": 9.9, "duration_min": 30,
                                      "summary": "mock route"}}


if __name__ == "__main__":
    unittest.main(verbosity=2)