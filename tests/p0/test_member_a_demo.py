"""Demo 链路测试 (Stage1 收尾): 复用 demo.run_pipeline(), 只测数据链路。

覆盖:
  1. VLM Mock 可运行; 2. raw 可被现有 parser 解析; 3. vlm_output 过正式 schema;
  4. person_event.json 可正常读取; 5-7. LLM 侧同上;
  8. task_proposals 非空且动作在词表内; 9. 不需要 API Key; 10. 不产生网络请求。
"""

import os
import socket
import unittest

import jsonschema

import _setup
import demo.p0_member_a_demo as demo
from src.llm import parser as llm_parser
from src.llm.prompts import TASK_PROPOSAL_VOCAB
from src.vlm import parser as vlm_parser

# 常见 API Key 环境变量 (用于验证 Demo 不依赖任何 Key)
_API_KEY_VARS = (
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
)


class DemoPipelineTest(unittest.TestCase):
    """直接跑 demo.run_pipeline(), 验证整条 Mock 数据链路。"""

    @classmethod
    def setUpClass(cls):
        cls.raw_vlm, cls.vlm_output, cls.person_events, cls.raw_llm, cls.llm_output = (
            demo.run_pipeline()
        )

    def test_vlm_mock_runs(self):
        self.assertTrue(self.raw_vlm)

    def test_vlm_raw_parseable_by_existing_parser(self):
        # 用现有 parser 解析 raw (不经过 demo 的产物, 独立验证)
        frame = vlm_parser.parse(self.raw_vlm).to_dict()
        for field in ("scene_type", "overall_severity", "hazards", "environment_note"):
            self.assertIn(field, frame)

    def test_vlm_output_passes_schema(self):
        jsonschema.validate(self.vlm_output, _setup.load_schema("vlm_output"))

    def test_person_event_file_loads(self):
        self.assertIsInstance(self.person_events, list)
        self.assertTrue(self.person_events)
        self.assertIn("person_id", self.person_events[0])

    def test_llm_mock_runs(self):
        self.assertTrue(self.raw_llm)

    def test_llm_raw_parseable_by_existing_parser(self):
        frame = llm_parser.parse(self.raw_llm).to_dict()
        for field in ("event_summary", "risk_explanations", "rescue_suggestions", "task_proposals"):
            self.assertIn(field, frame)

    def test_llm_output_passes_schema(self):
        jsonschema.validate(self.llm_output, _setup.load_schema("llm_output"))

    def test_task_proposals_non_empty_and_in_vocab(self):
        # mock 逻辑: 有人员(风险 0.82)→send_alert; 必有 save_event 留痕
        proposals = self.llm_output["task_proposals"]
        self.assertTrue(proposals)
        actions = {p["action"] for p in proposals}
        self.assertTrue(actions)
        self.assertTrue(actions.issubset(set(TASK_PROPOSAL_VOCAB)))
        self.assertIn("send_alert", actions)  # person_event.json 中风险最高者
        self.assertIn("save_event", actions)

    def test_risk_explanations_cover_person_event(self):
        pids = {item["pid"] for item in self.llm_output["risk_explanations"]}
        self.assertIn(self.person_events[0]["person_id"], pids)


class DemoNoDependencyTest(unittest.TestCase):
    """验证 Mock 链路不需要 API Key、不产生网络请求。"""

    def test_pipeline_runs_without_api_keys(self):
        saved = {}
        try:
            for var in _API_KEY_VARS:
                if var in os.environ:
                    saved[var] = os.environ.pop(var)
            # 清空 Key 环境变量后仍能完整跑通
            results = demo.run_pipeline()
            self.assertEqual(len(results), 5)
        finally:
            os.environ.update(saved)

    def test_pipeline_makes_no_network_requests(self):
        real_socket = socket.socket

        def _guard(*args, **kwargs):
            raise AssertionError("Demo 尝试建立网络连接 (Mock 链路不应有任何网络请求)")

        socket.socket = _guard
        try:
            demo.run_pipeline()  # 若有任何网络调用将直接失败
        finally:
            socket.socket = real_socket


if __name__ == "__main__":
    unittest.main(verbosity=2)