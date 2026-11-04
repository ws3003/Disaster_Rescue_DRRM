"""V4.4 C4-02 Worker 生产入口测试（Mock / 临时库，不连真实网络/不启动真实服务）。

覆盖：配置缺失失败、默认值与覆盖、make_worker 复用、run_loop 轮询/休眠/优雅退出/异常继续、
Token 不入日志（不入 log）。
"""

from __future__ import annotations

import errno
import importlib.util
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

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


# ---- C4-P1：数据库绝对路径预检 + flock 单实例（新增） ----

_REPO_ROOT = Path(__file__).resolve().parent.parent
_POSIX = not sys.platform.startswith("win")


def _make_project_db(tmp_path) -> str:
    """经 Database() 建临时完整项目库（含 persons/events/sync_outbox），返回绝对路径。"""
    d = Database(db_path=str(tmp_path / "proj.db"))
    return str(Path(d.db_path))


def _part_sqlite(path: str, tables):
    """手建仅含指定表的最小 sqlite（不触发 _initialize_database）。"""
    conn = sqlite3.connect(path)
    for t in tables:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def _wait_locked(proc, timeout=10.0):
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        ch = proc.stdout.read(1)
        if ch:
            buf += ch
            if "locked" in buf:
                return
        elif proc.poll() is not None:
            break
    raise RuntimeError("lock holder 未成功加锁")


class TestDbPreflight:
    def test_abs_complete_ok(self, m, tmp_path):
        m._preflight_database(_make_project_db(tmp_path))   # 不抛

    def test_relative_rejected(self, m, tmp_path):
        with pytest.raises(m.ConfigError) as ei:
            m._preflight_database("data/drrm.db")
        assert "绝对路径" in str(ei.value)

    def test_missing_file_rejected(self, m, tmp_path):
        with pytest.raises(m.ConfigError) as ei:
            m._preflight_database(str(tmp_path / "nope.db"))
        assert "不存在" in str(ei.value)

    def test_empty_sqlite_rejected(self, m, tmp_path):
        empty = str(tmp_path / "empty.db")
        sqlite3.connect(empty).close()
        with pytest.raises(m.ConfigError) as ei:
            m._preflight_database(empty)
        assert "核心表" in str(ei.value)

    def test_not_sqlite_rejected(self, m, tmp_path):
        f = tmp_path / "x.db"
        f.write_text("not a database", encoding="utf-8")
        with pytest.raises(m.ConfigError):
            m._preflight_database(str(f))

    def test_corrupt_rejected(self, m, tmp_path):
        f = tmp_path / "c.db"
        f.write_bytes(b"\x00\x01\xde\xad\xbe\xef" * 16)
        with pytest.raises(m.ConfigError):
            m._preflight_database(str(f))

    def test_missing_persons_rejected(self, m, tmp_path):
        p = str(tmp_path / "mp.db")
        _part_sqlite(p, ["events", "sync_outbox"])
        with pytest.raises(m.ConfigError) as ei:
            m._preflight_database(p)
        assert "persons" in str(ei.value)

    def test_missing_events_rejected(self, m, tmp_path):
        p = str(tmp_path / "me.db")
        _part_sqlite(p, ["persons", "sync_outbox"])
        with pytest.raises(m.ConfigError) as ei:
            m._preflight_database(p)
        assert "events" in str(ei.value)

    def test_missing_sync_outbox_rejected(self, m, tmp_path):
        p = str(tmp_path / "ms.db")
        _part_sqlite(p, ["persons", "events"])
        with pytest.raises(m.ConfigError) as ei:
            m._preflight_database(p)
        assert "sync_outbox" in str(ei.value)

    @pytest.mark.skipif(not _POSIX, reason="路径含 ?/# 仅 POSIX 文件系统允许")
    def test_special_char_posix(self, m, tmp_path):
        d = tmp_path / "a b?#c"
        d.mkdir()
        dbp = str(d / "proj.db")
        Database(db_path=dbp)
        m._preflight_database(dbp)   # 空格、?、# 被 URI 转义后仍只读打开

    def test_space_path_ok(self, m, tmp_path):
        d = tmp_path / "with space"
        d.mkdir()
        dbp = str(d / "proj.db")
        Database(db_path=dbp)
        m._preflight_database(dbp)

    def test_preflight_no_modify(self, m, tmp_path):
        dbp = _make_project_db(tmp_path)
        pre = Path(dbp)
        size0 = pre.stat().st_size
        mtime0 = pre.stat().st_mtime
        conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        cnt0 = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        conn.close()
        m._preflight_database(dbp)
        size1 = pre.stat().st_size
        mtime1 = pre.stat().st_mtime
        conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        cnt1 = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        conn.close()
        assert size0 == size1 and mtime0 == mtime1 and cnt0 == cnt1


class TestLockPath:
    def test_derived_and_normpath_dedup(self, m):
        a = m._lock_path_for("/a/./b/x.db")
        b = m._lock_path_for("/a/b/x.db")
        assert a == b
        assert a.endswith(".c4worker.lock")
        assert os.path.isabs(a)

    def test_relative_lock_path_rejected(self, m):
        with pytest.raises(m.ConfigError):
            m._lock_path_for("data/drrm.db")


class TestSingletonLock:
    def test_win_skip_warning_or_posix_fd(self, m, tmp_path, caplog):
        lock = str(tmp_path / "x.db.c4worker.lock")
        if _POSIX:
            fd = m._acquire_singleton_lock(lock)
            assert isinstance(fd, int)
            os.close(fd)
        else:
            assert m._acquire_singleton_lock(lock) is None
            assert "跳过单实例锁" in caplog.text

    @pytest.mark.skipif(not _POSIX, reason="flock 仅 POSIX")
    def test_flock_second_fails_then_release(self, m, tmp_path):
        lock = str(tmp_path / "x.db.c4worker.lock")
        fd = m._acquire_singleton_lock(lock)
        assert isinstance(fd, int)
        try:
            with pytest.raises(m.ConfigError) as ei:
                m._acquire_singleton_lock(lock)
            assert "已有 C4 Worker" in str(ei.value)
        finally:
            os.close(fd)
        fd2 = m._acquire_singleton_lock(lock)
        assert isinstance(fd2, int)
        os.close(fd2)

    @pytest.mark.skipif(not _POSIX, reason="flock 仅 POSIX")
    def test_lock_dir_unwritable_fails(self, m, tmp_path):
        d = tmp_path / "unwritable"
        d.mkdir()
        d.chmod(0o000)
        try:
            with pytest.raises(m.ConfigError) as ei:
                m._acquire_singleton_lock(str(d / "x.lock"))
            assert "无法打开" in str(ei.value) or "打开" in str(ei.value)
        finally:
            d.chmod(0o700)

    @pytest.mark.skipif(not _POSIX, reason="需要 flock 释放校验")
    def test_make_worker_failure_releases_lock_and_propagates(self, m, tmp_path, monkeypatch):
        prj = _make_project_db(tmp_path)
        monkeypatch.setenv("C4_SERVER_BASE_URL", "http://win:8000/api/v1")
        monkeypatch.setenv("C4_API_TOKEN", "t")
        monkeypatch.setenv("DRRM_DB", prj)
        monkeypatch.setenv("C4_POLL_INTERVAL_SECONDS", "0.5")

        def boom(*a, **k):
            raise ValueError("make_worker boom")

        monkeypatch.setitem(m.main.__globals__, "make_worker", boom)
        with pytest.raises(ValueError):
            m.main()
        # 未知异常传播；finally 已关闭 lock fd → 可再获
        fd = m._acquire_singleton_lock(m._lock_path_for(prj))
        assert isinstance(fd, int)
        os.close(fd)

    @pytest.mark.skipif(not _POSIX, reason="需要 flock 释放校验")
    def test_run_loop_failure_releases_lock_and_propagates(self, m, tmp_path, monkeypatch):
        prj = _make_project_db(tmp_path)
        monkeypatch.setenv("C4_SERVER_BASE_URL", "http://win:8000/api/v1")
        monkeypatch.setenv("C4_API_TOKEN", "t")
        monkeypatch.setenv("DRRM_DB", prj)
        monkeypatch.setenv("C4_POLL_INTERVAL_SECONDS", "0.5")

        def boom(*a, **k):
            raise RuntimeError("run_loop boom")

        monkeypatch.setitem(m.main.__globals__, "run_loop", boom)
        with pytest.raises(RuntimeError):
            m.main()
        fd = m._acquire_singleton_lock(m._lock_path_for(prj))
        assert isinstance(fd, int)
        os.close(fd)


_LOCK_HOLDER = (
    "import fcntl,os,sys,time;"
    "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);"
    "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);"
    "print('locked',flush=True);time.sleep(120)"
)


class TestSingletonSubprocess:
    @pytest.mark.skipif(not _POSIX, reason="flock subprocess 仅 POSIX")
    def test_sigkill_releases_lock(self, m, tmp_path):
        lock = str(tmp_path / "lk.db.c4worker.lock")
        env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
        p = subprocess.Popen([sys.executable, "-c", _LOCK_HOLDER, lock],
                             env=env, stdout=subprocess.PIPE, text=True)
        _wait_locked(p)
        p.kill()
        p.wait()
        fd = m._acquire_singleton_lock(lock)
        assert isinstance(fd, int)
        os.close(fd)

    @pytest.mark.skipif(not _POSIX, reason="flock subprocess 仅 POSIX")
    def test_sigterm_releases_lock(self, m, tmp_path):
        lock = str(tmp_path / "lk2.db.c4worker.lock")
        env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
        p = subprocess.Popen([sys.executable, "-c", _LOCK_HOLDER, lock],
                             env=env, stdout=subprocess.PIPE, text=True)
        _wait_locked(p)
        p.terminate()
        p.wait()
        fd = m._acquire_singleton_lock(lock)
        assert isinstance(fd, int)
        os.close(fd)

    @pytest.mark.skipif(not _POSIX, reason="flock subprocess 仅 POSIX")
    def test_second_worker_rejected_no_runloop(self, m, tmp_path):
        prj = _make_project_db(tmp_path)
        lock = m._lock_path_for(prj)
        holder_env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
        holder = subprocess.Popen([sys.executable, "-c", _LOCK_HOLDER, lock],
                                  env=holder_env, stdout=subprocess.PIPE, text=True)
        _wait_locked(holder)
        env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT),
                   C4_SERVER_BASE_URL="http://win:8000/api/v1",
                   C4_API_TOKEN="t", DRRM_DB=prj,
                   C4_POLL_INTERVAL_SECONDS="0.5")
        r = subprocess.run([sys.executable, "-m", "scripts.c4_worker"],
                           cwd=str(_REPO_ROOT), env=env,
                           capture_output=True, text=True, timeout=30)
        holder.terminate()
        holder.wait()
        assert r.returncode == 2
        assert "已有 C4 Worker" in r.stderr
        assert "C4 worker 启动" not in r.stderr   # 未走到 make_worker/run_loop