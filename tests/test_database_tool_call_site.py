"""V4.4 G8: database_tool save_event 调用点接入 bridge/C4 的兼容性测试（自 test_c4_integration 迁出）。

验证: `DatabaseTool.run(action="save_event", payload=任意)` 经 C4 bridge 分流(可哈希/verify),
保留原有断言与临时库隔离。不连真实库/网络/硬件。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db import Database


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "database_tool_call_site_test.db"))
    yield db


# ---- 14: 调用点契约 (database_tool) ----
class TestCallSiteCompat:
    def test_database_tool_save_event_contract(self, db, tmp_path):
        from src.tools.database_tool import DatabaseTool
        dt = DatabaseTool(db_path=os.path.join(tmp_path, "dt.db"))
        out = dt.run(action="save_event", type="t", severity="H", person_id=1,
                     payload={"foo": "bar"})  # LLM 任意 payload -> verify
        assert out["ok"] is True and out["action"] == "save_event" and out["event_id"]
        assert dt.db.get_event(out["event_id"]) is not None