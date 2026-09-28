"""A 侧 Authoritative Observation Contract (Phase 2-B) 测试。

覆盖: observation_id 每请求唯一 / timestamp ISO+tz / persons 保留 Edge FACT /
缺失字段 null / LLM fake facts 被剥离、persons 权威 / payload 消毒 / action 词表对齐 B /
target=observation-local person_id / 无效 target 被丢弃 / 多人员 / contract_version / A API Mock E2E。
A→B 真实跨分支 Mock 因分支隔离不在本步骤运行(见报告 note)。
"""

import json
import os
import sys
import unittest
from datetime import datetime

from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import api  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.vlm.client import VLMClient  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
FAKE_VLM = {"scene_type": "collapse", "overall_severity": "critical",
            "hazards": [], "environment_note": "mock env"}
PERSONS = [{"person_id": 1, "state": "Fall", "risk_score": 0.6077, "priority": "HIGH"}]


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeTransport:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"choices": [{"message": {"content": self.content}}]})


def _llm_raw(task_proposals=None):
    if task_proposals is None:
        task_proposals = [{"action": "send_alert", "target": 1,
                           "payload": {"message": "test", "state": "Standing",
                                       "risk_score": 0.1, "priority": "LOW"}}]
    return json.dumps({
        "event_summary": "mock", "risk_explanations": [{"pid": 1, "reason": "r"}],
        "rescue_suggestions": ["s"], "task_proposals": task_proposals}, ensure_ascii=False)


class ContractObservationTest(unittest.TestCase):
    def setUp(self):
        self._env = {}
        for k in ("VLM_API_KEY", "LLM_API_KEY"):
            self._env[k] = os.environ.get(k)
            os.environ[k] = "test-key"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        api.app.dependency_overrides.pop(api.get_clients, None)

    def _override(self, llm_content):
        vlm_tr = FakeTransport(json.dumps(FAKE_VLM, ensure_ascii=False))
        llm_tr = FakeTransport(llm_content)

        def _factory():
            return (VLMClient(provider="qwen", transport=vlm_tr),
                    LLMClient(provider="qwen", transport=llm_tr))

        api.app.dependency_overrides[api.get_clients] = _factory

    def _post(self, persons=PERSONS, llm_content=None):
        self._override(llm_content if llm_content is not None else _llm_raw())
        return TestClient(api.app).post(
            "/api/v1/analyze",
            files={"image": ("f.png", PNG, "application/octet-stream")},
            data={"person_events": json.dumps(persons, ensure_ascii=False)},
        )

    def _body(self, persons=PERSONS, llm_content=None):
        resp = self._post(persons=persons, llm_content=llm_content)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["llm_output"]

    def test_t1_observation_id_unique_per_request(self):
        o1 = self._body()["observation"]
        o2 = self._body()["observation"]
        self.assertNotEqual(o1["observation_id"], o2["observation_id"])
        self.assertTrue(o1["observation_id"].startswith("obs-"))

    def test_t2_timestamp_iso_with_tz(self):
        dt = datetime.fromisoformat(self._body()["observation"]["timestamp"])
        self.assertIsNotNone(dt)

    def test_t3_persons_retain_edge_facts(self):
        p = self._body()["persons"][0]
        self.assertEqual(p["person_id"], 1)
        self.assertEqual(p["state"], "Fall")
        self.assertEqual(p["risk_score"], 0.6077)
        self.assertEqual(p["priority"], "HIGH")

    def test_t4_missing_edge_fields_null(self):
        p = self._body()["persons"][0]
        self.assertIsNone(p["state_conf"])
        self.assertIsNone(p["bbox"])
        self.assertIsNone(p["frame_idx"])

    def test_t5_llm_fake_facts_lose_person_authority(self):
        body = self._body(llm_content=_llm_raw())  # payload 含 Standing/0.1/LOW
        self.assertEqual(body["persons"][0]["state"], "Fall")
        self.assertEqual(body["persons"][0]["risk_score"], 0.6077)
        self.assertEqual(body["persons"][0]["priority"], "HIGH")

    def test_t6_payload_sanitization(self):
        prop = self._body()["task_proposals"][0]
        self.assertNotIn("state", prop["payload"])
        self.assertNotIn("risk_score", prop["payload"])
        self.assertNotIn("priority", prop["payload"])
        self.assertEqual(prop["payload"].get("message"), "test")

    def test_t7_action_vocabulary_b_aligned(self):
        body = self._body()
        self.assertEqual(body["task_proposals"][0]["action"], "send_alert")  # alert→send_alert
        llm = json.dumps({"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
                          "task_proposals": [{"action": "hack_tool", "target": 1, "payload": {}},
                                             {"action": "log", "target": None, "payload": {}}]},
                         ensure_ascii=False)
        body2 = self._body(llm_content=llm)
        actions = {t["action"] for t in body2["task_proposals"]}
        self.assertEqual(actions, {"save_event"})  # log→save_event; hack 丢弃

    def test_t8_target_is_person_id_not_index(self):
        persons = [{"person_id": 7, "state": "Fall", "risk_score": 0.6, "priority": "HIGH"}]
        llm = json.dumps({"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
                          "task_proposals": [{"action": "send_alert", "target": 7,
                                              "payload": {"message": "m"}}]}, ensure_ascii=False)
        body = self._body(persons=persons, llm_content=llm)
        self.assertTrue(body["task_proposals"])
        self.assertEqual(body["task_proposals"][0]["target"], 7)

    def test_t9_invalid_target_dropped(self):
        persons = [{"person_id": 7, "state": "Fall", "risk_score": 0.6, "priority": "HIGH"}]
        llm = json.dumps({"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
                          "task_proposals": [{"action": "send_alert", "target": 1,
                                              "payload": {"message": "m"}}]}, ensure_ascii=False)
        body = self._body(persons=persons, llm_content=llm)
        self.assertEqual(body["task_proposals"], [])  # target=1 不在 observation → 丢弃

    def test_t10_multi_person(self):
        persons = [{"person_id": 7, "state": "Fall", "risk_score": 0.8, "priority": "HIGH"},
                   {"person_id": 12, "state": "Standing", "risk_score": 0.1, "priority": "LOW"}]
        llm = json.dumps({"event_summary": "x", "risk_explanations": [], "rescue_suggestions": [],
                          "task_proposals": [
                              {"action": "send_alert", "target": 7, "payload": {"message": "a"}},
                              {"action": "save_event", "target": 12, "payload": {"message": "b"}}]},
                         ensure_ascii=False)
        body = self._body(persons=persons, llm_content=llm)
        targets = {t["target"] for t in body["task_proposals"]}
        self.assertEqual(targets, {7, 12})

    def test_t11_contract_version(self):
        self.assertEqual(self._body()["contract_version"], "0.1-observation-v1")

    def test_t12_api_mock_e2e(self):
        body = self._body()
        self.assertEqual(body["contract_version"], "0.1-observation-v1")
        for key in ("observation", "persons", "event_summary", "task_proposals"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)