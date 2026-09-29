"""V4.0 全链路离线回归测试框架 (tests/v4_pipeline_test.py)

目标: 自动验证  Edge -> OSS -> VLM -> LLM -> Agent -> Executor -> SQLite  全链路。
   - 不依赖真实摄像头 (直接用已有测试图片 data/test_images/002_fall.jpg)。
   - 不依赖真实网络/凭据 (VLM/LLM 用 FakeTransport 走真实 api.py、OSS 打桩、通信 mock)。
   - 复用现有代码, 不修改 YOLO/MediaPipe/Agent/Edge 核心逻辑。

运行:
   python -m unittest tests.v4_pipeline_test -v
   python tests/v4_pipeline_test.py            # 输出逐步骤 PASS/FAIL 报告
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import requests
from fastapi.testclient import TestClient
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import api  # noqa: E402
from src.agent.agent import Agent  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.vlm.client import VLMClient  # noqa: E402

# 测试图片: 真实落图(不依赖摄像头)
TEST_IMAGE = ROOT / "data" / "test_images" / "002_fall.jpg"
PERSONS = [{"person_id": 1, "state": "Fall", "risk_score": 0.92, "priority": "HIGH"}]

# ---- Fake VLM/LLM 输出(离线, 走真实 api.py 编排) -------------------------------
FAKE_VLM = {
    "scene_type": "collapse",
    "overall_severity": "critical",
    "hazards": [{"type": "debris", "brief": "瓦砾"}],
    "environment_note": "v4 regression 假环境",
}
FAKE_LLM = {
    "event_summary": "v4 回归: 人员1 倒地高风险",
    "risk_explanations": [{"pid": 1, "reason": "Fall"}],
    "rescue_suggestions": ["优先处置人员 1"],
    "task_proposals": [
        {"action": "send_alert", "target": 1,
         "payload": {"severity": "HIGH", "priority": "HIGH", "state": "Fall",
                     "risk_score": 0.92, "message": "回归测试报警"}},
        {"action": "assess_person", "target": 1, "payload": {}},
        {"action": "get_snapshot", "target": 1, "payload": {}},
    ],
}

# 打桩 OSS 公网 URL(不触网)
FAKE_OSS_URL = "https://disaster-rescue-images.oss-cn-hangzhou.aliyuncs.com/snapshots/regression_analysis.jpg"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body, ensure_ascii=False)

    def json(self):
        return self._body


class FakeTransport:
    def __init__(self, content):
        self._content = content
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        body = {"choices": [{"message": {"content": self._content}}]}
        return FakeResponse(200, body)


def _override_fake_models():
    """用 FakeTransport 替换 api.py 的 get_clients(走真实 api 编排但不触网)。"""
    vlm_tr = FakeTransport(content=json.dumps(FAKE_VLM, ensure_ascii=False))
    llm_tr = FakeTransport(content=json.dumps(FAKE_LLM, ensure_ascii=False))

    def _factory():
        return (VLMClient(provider="qwen", transport=vlm_tr),
                LLMClient(provider="qwen", transport=llm_tr))

    api.app.dependency_overrides[api.get_clients] = _factory
    return vlm_tr, llm_tr


def _agent_config(db, tmp):
    return {
        "agent": {"trigger": {"confirm_frames": 3, "high_risk_must_alert": True,
                              "heartbeat_minutes": 5},
                  "memory": {"cooldown_minutes": 30},
                  "planner": {"max_steps": 3, "cooldown_minutes": 30}},
        "db": {"path": db},
        "vision": {"snapshot_path": os.path.join(tmp, "v.json"),
                   "recent_path": os.path.join(tmp, "v.jsonl"), "scene_window_sec": 10},
        "communication": {"channel": "mock", "max_retries": 1, "backoff_seconds": [1],
                          "timeout": 5, "log_path": os.path.join(tmp, "c.log"),
                          "pending_path": os.path.join(tmp, "p.jsonl")},
        "rescue": {"snapshot_path": os.path.join(tmp, "r.json")},
    }


def _count_tasks(db):
    with sqlite3.connect(db) as c:
        return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _count_memory_alert(db):
    with sqlite3.connect(db) as c:
        return c.execute("SELECT COUNT(*) FROM events WHERE type='memory_alert'").fetchone()[0]


class V4PipelineRegressionTest(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("VLM_API_KEY", "vlm-fake")
        os.environ.setdefault("LLM_API_KEY", "llm-fake")
        os.environ.pop("VLM_PROVIDER", None)
        os.environ.pop("LLM_PROVIDER", None)
        os.environ["COMMUNICATION_CHANNEL"] = "mock"
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "v4.db")

    def tearDown(self):
        api.app.dependency_overrides.pop(api.get_clients, None)
        self._tmp.cleanup()

    def _run_full_chain(self):
        """返回 (checks: dict[str, bool], result: list, body: dict, agent)。"""
        override = _override_fake_models()
        checks = {}

        with patch("server.api.upload_snapshot",
                   return_value={"ok": True, "url": FAKE_OSS_URL, "provider": "aliyun_oss"}):
            client = TestClient(api.app)
            image = TEST_IMAGE.read_bytes()
            resp = client.post(
                "/api/v1/analyze",
                files={"image": ("fall.jpg", image, "application/octet-stream")},
                data={"person_events": json.dumps(PERSONS, ensure_ascii=False)},
            )
            body = resp.json() if resp.status_code == 200 else {}
            checks["API status==200"] = resp.status_code == 200
            checks["VLM output 存在"] = (body.get("success")
                                          and "scene_type" in (body.get("vlm_output") or {}))
            checks["LLM output 存在"] = bool(body.get("llm_output", {}).get("task_proposals"))
            checks["OSS snapshot_url 存在"] = bool(body.get("snapshot_url"))

        # ---- Agent ingest(模拟 edge_client 路径B, 注入 image_url + task_id) ----
        tid = "T-REGRESSION-000001"
        agent = Agent(config=_agent_config(self.db, self._tmp.name))
        llm_output = body.get("llm_output")
        result = agent.ingest_llm_output(
            llm_output,
            context={"image_url": body.get("snapshot_url"), "task_id": tid},
        )
        root = result[0]
        task = root["task"]
        execs = root.get("execution") or []
        tools = {e.get("tool") for e in execs}
        vis = next((e for e in execs if e.get("tool") == "vision_tool"), {})
        comm = next((e for e in execs if e.get("tool") == "communication_tool"
                     and e.get("action") == "send_alert"), {})

        checks["trigger.source == llm"] = (task.get("trigger") or {}).get("source") == "llm"
        checks["task_id 复用(传入 tid)"] = task.get("task_id") == tid
        checks["persons 注入(rescue 评估成功)"] = any(
            e.get("tool") == "rescue_tool" and e.get("ok") for e in execs)
        checks["vision 返回 OSS URL"] = ((vis.get("snapshot") or {}).get("url") == FAKE_OSS_URL)
        checks["communication send_alert SENT"] = bool(comm.get("ok") and comm.get("status") == "SENT")
        checks["communication 用 OSS URL"] = comm.get("image_url") == FAKE_OSS_URL
        checks["executor 跑 rescue/vision/communication"] = {
            "rescue_tool", "vision_tool", "communication_tool"}.issubset(tools)
        checks["SQLite tasks == 1"] = _count_tasks(self.db) == 1
        checks["SQLite memory_alert == 1"] = _count_memory_alert(self.db) == 1

        return checks, result, body

    def test_full_pipeline(self):
        checks, _, _ = self._run_full_chain()
        _print_report(checks)
        for label, ok in checks.items():
            self.assertTrue(ok, f"步骤未通过: {label}")


# ---------------------------------------------------------------- 报告
def _print_report(checks: dict[str, bool]) -> int:
    print("\n" + "=" * 62)
    print("V4.0 全链路回归报告 (Edge->OSS->VLM->LLM->Agent->Executor->SQLite)")
    print("=" * 62)
    failed = 0
    for label, ok in checks.items():
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {label}")
    print("=" * 62)
    print(f"  总计: {len(checks)} 项, 通过 {len(checks) - failed}, 失败 {failed}")
    print("=" * 62)
    return failed


if __name__ == "__main__":
    # 独立运行: 输出逐步骤 PASS/FAIL 报告
    t = V4PipelineRegressionTest()
    t.setUp()
    try:
        checks, _, _ = t._run_full_chain()
        failed = _print_report(checks)
    finally:
        t.tearDown()
    sys.exit(1 if failed else 0)