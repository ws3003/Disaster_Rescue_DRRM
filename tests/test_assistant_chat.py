"""AssistantResponder 接入 LLMClient.ask() 的单元测试(V4.3 Chat 阶段2)。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.responder import AssistantResponder  # noqa: E402
from src.assistant.context import AssistantContext  # noqa: E402


class _FakeLLM:
    provider = "mock"

    def __init__(self, text="通用 LLM 回答", raise_err=False):
        self.text = text
        self.raise_err = raise_err
        self.calls = 0

    def ask(self, question, context=None):
        self.calls += 1
        if self.raise_err:
            raise RuntimeError("llm boom")
        return self.text


class AssistantChatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "c.db")
        self.snap = os.path.join(self._tmp.name, "s")
        os.makedirs(self.snap, exist_ok=True)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, "
                      "trigger_event TEXT, plan_trace TEXT, status TEXT, created TEXT, finished TEXT)")

    def tearDown(self):
        self._tmp.cleanup()

    def _ctx(self):
        # 空 vision 路径(不存在), 保证 scene 测试不依赖全局 data/vision_persons.json
        vision = os.path.join(self._tmp.name, "no_vision.json")
        return AssistantContext(db_path=self.db, snapshot_dir=self.snap, vision_path=vision)

    def test_general_prefers_llm(self):
        llm = _FakeLLM("LLM 给的通用回答")
        out = AssistantResponder(context=self._ctx(), llm=llm).answer("介绍一下你自己")
        self.assertEqual(llm.calls, 1)
        self.assertEqual(out["answer"], "LLM 给的通用回答")
        self.assertEqual(out["intent"], "general_query")
        self.assertEqual(out["sources"][0]["type"], "llm")

    def test_general_fallback_when_llm_raises(self):
        llm = _FakeLLM(raise_err=True)
        out = AssistantResponder(context=self._ctx(), llm=llm).answer("介绍一下你自己")
        self.assertEqual(llm.calls, 1)
        self.assertEqual(out["sources"], [])  # 回退到模板(信息辅助引导语)
        self.assertTrue(out["answer"])

    def test_structured_intent_not_hit_llm(self):
        llm = _FakeLLM()
        out = AssistantResponder(context=self._ctx(), llm=llm).answer("现场情况怎么样")
        self.assertEqual(llm.calls, 0)          # 结构化意图不走 LLM
        self.assertEqual(out["intent"], "scene_status")
        self.assertTrue(out["answer"])

    def test_default_no_llm_inject_uses_real_mock(self):
        # 未注入 llm → 默认 LLMClient(mock); general 也应返回(非空)
        out = AssistantResponder(context=self._ctx()).answer("介绍一下你自己")
        self.assertEqual(out["intent"], "general_query")
        self.assertTrue(out["answer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)