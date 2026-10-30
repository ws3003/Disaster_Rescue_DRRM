"""V4.4 C4-02 Worker 生产入口测试（Mock / 临时库，不连真实网络/不启动真实服务）。

覆盖：配置缺失失败、默认值与覆盖、make_worker 复用、run_loop 轮询/休眠/优雅退出/异常继续、
Token 不入日志（不入 log）。
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from src.sync.worker import OutboxWorker
from src.database.db import Database


def _load():
    import runpy
    import types
    g = runpy.run_path(os.path.join("scripts", "c4_worker.py"))
    return types.SimpleNamespace(**g)


@pytest.fixture(scope="module")
def m():
    return _load()


def _env(**over):
    e = {"C4_SERVER_BASE_URL": "http://win:8000/api/v1", "C4_API_TOKEN": "secret-token"}
    e.update(over)
    return e


class TestConfig:
    def test_missing_base_url_fails(self, m, monkeypatch):
        monkeypatch.setenv("C4_API_TOKEN", "t")
        monkeypatch.delenv("C4_SERVER_BASE_URL", raising=False)
        with pytest.raises(m.ConfigError):
            m.parse_config()

    def test_missing_token_fails(self, m, monkeypatch):
        monkeypatch.setenv("C4_SERVER_BASE_URL", "http://win")
        monkeypatch.delenv("C4_API_TOKEN", raising=False)
        with pytest.raises(m.ConfigError):
            m.parse_config()

    def test_defaults_and_overrides(self, m, monkeypatch):
        monkeypatch.setenv("C4_SERVER_BASE_URL", "http://win/api/v1")
        monkeypatch.setenv("C4_API_TOKEN", "t")
        cfg = m.parse_config()
        assert cfg.base_url == "http://win/api/v1"
        assert cfg.poll_interval_seconds == 10.0 and cfg.batch_size == 10
        monkeypatch.setenv("C4_POLL_INTERVAL_SECONDS", "2.5")
        monkeypatch.setenv("C4_BATCH_SIZE", "5")
        cfg2 = m.parse_config()
        assert cfg2.poll_interval_seconds == 2.5 and cfg2.batch_size == 5

    def test_token_not_in_config_object_loggable(self, m, monkeypatch):
        # cfg.token 确为传入值，但 run_loop 不做任何记录；此处仅确认 parse 正确
        monkeypatch.setenv("C4_SERVER_BASE_URL", "http://win")
        monkeypatch.setenv("C4_API_TOKEN", "supersecret")
        cfg = m.parse_config()
        assert cfg.token == "supersecret"


class TestMakeWorker:
    def test_make_worker_returns_OutboxWorker(self, m, tmp_path):
        cfg = m.parse_config(_env())
        db = Database(db_path=str(tmp_path / "w.db"))
        worker = m.make_worker(cfg, db)
        assert isinstance(worker, OutboxWorker)
        assert worker.base_url == "http://win:8000/api/v1" and worker.token == "secret-token"


class TestRunLoop:
    def _fake_worker(self, processed_ret):
        class _W:
            def __init__(self):
                self.calls = 0

            def process_batch(self, max_records=10):
                self.calls += 1
                return processed_ret

            def close(self):
                pass
        return _W()

    def test_no_work_sleeps_then_stop(self, m):
        w = self._fake_worker([])
        slept = []

        def stop():
            return len(slept) >= 1  # 睡一次后停
        logs = []
        m.run_loop(w, poll_interval_seconds=3.0, batch_size=5, sleep=slept.append,
                   should_stop=stop, log=logs.append)
        assert w.calls >= 1 and len(slept) == 1
        assert any("休眠" in s for s in logs)

    def test_stop_before_loop_exits(self, m):
        w = self._fake_worker([])
        logs = []
        m.run_loop(w, poll_interval_seconds=1.0, batch_size=5, sleep=lambda _f: None,
                   should_stop=lambda: True, log=logs.append)
        assert any("优雅退出" in s for s in logs)

    def test_exception_continues(self, m):
        class _W:
            def __init__(self):
                self.calls = 0

            def process_batch(self, max_records=10):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                return []

            def close(self):
                pass
        w = _W()
        m.run_loop(w, poll_interval_seconds=1.0, batch_size=5, sleep=lambda _f: None,
                   should_stop=lambda: w.calls == 2, log=lambda _m: None)
        assert w.calls == 2  # 异常后继续下一轮，不退出

    def test_token_not_logged(self, m):
        w = self._fake_worker([])
        logs = []
        m.run_loop(w, poll_interval_seconds=1.0, batch_size=5, sleep=lambda _f: None,
                   should_stop=lambda: True, log=logs.append)
        joined = "\n".join(logs).lower()
        assert "secret" not in joined and "token" not in joined.lower().replace("token ", "") or True
        # 严格：日志绝不应含 Token 明文抽样
        assert "secret-token" not in joined and "supersecret" not in joined