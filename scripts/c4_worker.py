"""C4 Worker 生产常驻入口（Pi → Windows 单向同步 OutboxWorker 启动器）。

仅作为启动/编排外壳：复用 `src.sync.worker.OutboxWorker` 的全部业务逻辑，
不复制、不重实现 Outbox 状态机。

循环行为：
- 有可领取记录 → process_batch 处理；
- 无待处理记录 → 按 C4_POLL_INTERVAL_SECONDS 休眠；
- 网络/服务端异常 → 由 OutboxWorker 内部重试/退避机制处理，本入口仅记录日志；
- 非预期异常 → 记录堆栈并继续（不静默退出、不误标 success）；
- SIGINT / SIGTERM → 优雅停止（结束当前 batch 后退出，不把未成功记录标 success）。

配置（环境变量，Token 绝不写入日志/不硬编码）：
  C4_SERVER_BASE_URL        必填  服务端接收地址（如 http://192.168.x.x:8000/api/v1）
  C4_API_TOKEN              必填  同步令牌
  DRRM_DB                   可选  本地 SQLite 路径（默认 data/drrm.db）
  C4_POLL_INTERVAL_SECONDS  可选  无待处理时的轮询间隔（默认 10.0）
  C4_BATCH_SIZE             可选  每轮最大处理条数（默认 10）
  C4_HTTP_TIMEOUT_SECONDS   可选  HTTP 超时（默认 10.0）
缺失必填配置 → 明确报错并退出非零（不静默回退到不安全默认）。
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from types import FrameType
from typing import Callable, Optional

from src.database.db import Database
from src.sync.worker import OutboxWorker

_LOGGER = logging.getLogger("c4.worker")

# Worker 连接前必须存在的项目核心表（V4.3 persons/events + C4 sync_outbox）。
_CORE_TABLES = frozenset({"persons", "events", "sync_outbox"})

# 默认值（安全、可配置；不写死业务阈值）
_DEFAULTS = {
    "db_path": "data/drrm.db",
    "poll_interval_seconds": 10.0,
    "batch_size": 10,
    "http_timeout_seconds": 10.0,
    "retry_base_ms": 5000,
    "retry_max_ms": 300000,
}


class ConfigError(Exception):
    """C4 Worker 配置缺失/非法。"""


@dataclass
class WorkerConfig:
    db_path: str
    base_url: str
    token: str
    poll_interval_seconds: float = _DEFAULTS["poll_interval_seconds"]
    batch_size: int = _DEFAULTS["batch_size"]
    http_timeout_seconds: float = _DEFAULTS["http_timeout_seconds"]
    retry_base_ms: int = _DEFAULTS["retry_base_ms"]
    retry_max_ms: int = _DEFAULTS["retry_max_ms"]


def parse_config(env: Optional[dict] = None) -> WorkerConfig:
    """从环境变量构造配置；缺失必填项抛 ConfigError（Token 一律不参与日志/回显）。"""
    env = dict(os.environ if env is None else env)
    base_url = (env.get("C4_SERVER_BASE_URL") or "").strip().rstrip("/")
    token = (env.get("C4_API_TOKEN") or "").strip()
    if not base_url:
        raise ConfigError("C4_SERVER_BASE_URL 未设置（必填）")
    if not token:
        raise ConfigError("C4_API_TOKEN 未设置（必填）")

    def _float(key, default):
        if not env.get(key):
            return default
        try:
            return float(env[key])
        except ValueError:
            raise ConfigError(f"{key} 须为数值: {env[key]!r}")

    def _int(key, default):
        if not env.get(key):
            return default
        try:
            v = int(env[key])
        except ValueError:
            raise ConfigError(f"{key} 须为整数: {env[key]!r}")
        if v < 1:
            raise ConfigError(f"{key} 须为正整数: {v!r}")
        return v

    return WorkerConfig(
        db_path=(env.get("DRRM_DB") or _DEFAULTS["db_path"]).strip(),
        base_url=base_url,
        token=token,
        poll_interval_seconds=_float("C4_POLL_INTERVAL_SECONDS",
                                     _DEFAULTS["poll_interval_seconds"]),
        batch_size=_int("C4_BATCH_SIZE", _DEFAULTS["batch_size"]),
        http_timeout_seconds=_float("C4_HTTP_TIMEOUT_SECONDS",
                                    _DEFAULTS["http_timeout_seconds"]),
        retry_base_ms=_int("C4_RETRY_BASE_MS", _DEFAULTS["retry_base_ms"]),
        retry_max_ms=_int("C4_RETRY_MAX_MS", _DEFAULTS["retry_max_ms"]),
    )


def make_worker(cfg: WorkerConfig, db: Database) -> OutboxWorker:
    """复用现有 OutboxWorker（含 CAS 领取/退避/dead_letter/verify）。"""
    return OutboxWorker(
        db,
        cfg.base_url,
        cfg.token,
        worker_id=f"pi-worker-{os.getpid()}",
        retry_base_ms=cfg.retry_base_ms,
        retry_max_ms=cfg.retry_max_ms,
        request_timeout=cfg.http_timeout_seconds,
    )


def _preflight_database(db_path: str) -> None:
    """Worker 连接前校验数据库：绝对路径 + 文件存在 + SQLite 只读核心表。

    - 不调用 Database()/_initialize_database()，绝不建表/迁移/写库。
    - 失败抛 ConfigError（由 main 统一返回退出码 2）。
    """
    if not os.path.isabs(db_path):
        raise ConfigError("DRRM_DB 必须是绝对路径，不得使用相对路径静默解析")
    norm = os.path.normpath(db_path)
    if not os.path.isfile(norm):
        raise ConfigError(f"数据库文件不存在: {norm}（不自动创建）")
    # 只读 URI：转义空格 / ? / #；保留 '/'(POSIX 与 C:)；不替换 Linux 正斜杠。
    norm_fwd = norm.replace("\\", "/") if os.name == "nt" else norm
    uri = f"file:{urllib.parse.quote(norm_fwd, safe='/:')}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        # sqlite3.connect 是惰性的：非 SQLite / 损坏的 DatabaseError 在首次查询才抛出，
        # 因此连接与读取都须在同一 try/except sqlite3.Error 内捕获。
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    except sqlite3.Error as exc:
        # 非 SQLite / 损坏 / 只读不可打开 → 明确失败（不伪装成功）
        raise ConfigError(f"数据库只读打开/读取失败（非 SQLite/损坏/权限）: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    have = {r[0] for r in rows}
    missing = _CORE_TABLES - have
    if missing:
        raise ConfigError(f"数据库缺少核心表: {sorted(missing)}")


def _lock_path_for(db_path: str) -> str:
    """锁文件固定派生自规范化绝对数据库路径；同库同锁，无自定义覆盖。"""
    if not os.path.isabs(db_path):
        raise ConfigError("DRRM_DB 必须是绝对路径")
    return f"{os.path.normpath(db_path)}.c4worker.lock"


def _acquire_singleton_lock(lock_path: str) -> Optional[int]:
    """POSIX flock 单实例锁；成功返回保持打开的 fd（进程生命周期持有）。

    - Linux/POSIX：必须导入 fcntl 并成功 flock(LOCK_EX|LOCK_NB)，否则拒绝启动。
    - Windows 开发：显式 WARNING 后返回 None（跳过锁，未验证）。
    - 其他未知平台：拒绝启动（不得静默跳过）。
    """
    plat = sys.platform
    if plat.startswith("win"):
        _LOGGER.warning("POSIX flock 不可用，开发环境跳过单实例锁（未验证）")
        return None
    posix = (plat.startswith("linux")
             or plat in ("darwin", "freebsd", "openbsd", "netbsd", "solaris"))
    if not posix:
        raise ConfigError(f"不支持平台 {plat!r}，无法保证单实例保护")
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX 系统通常自带
        raise ConfigError("fcntl 不可用（POSIX 平台必需）") from exc
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # 打开失败 / 锁目录不可写 / 权限：输出真实原因，不误报"已有实例"
        raise ConfigError(f"锁文件无法打开: {lock_path} ({exc})") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise ConfigError("已有 C4 Worker 实例在运行") from exc
        raise ConfigError(f"获取进程锁失败(errno={exc.errno}): {exc}") from exc
    return fd


def run_loop(
    worker: OutboxWorker,
    *,
    poll_interval_seconds: float,
    batch_size: int,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """持续轮询 OutboxWorker。stop 回调为真时结束循环（优雅退出）。

    - 网络/服务端异常已由 OutboxWorker 内部置 retry_wait/dead_letter，不在此捕获为退出；
    - 非预期异常：记录堆栈并继续下一轮（不静默、不误标 success）。
    """
    log = log or (lambda m: _LOGGER.info(m))
    should_stop = should_stop or (lambda: False)
    while True:
        if should_stop():
            log("收到停止信号，优雅退出")
            return
        try:
            processed = worker.process_batch(max_records=batch_size)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("C4 process_batch 出现未预期异常（不退出，下一轮继续）")
            processed = []
        if should_stop():
            return
        if not processed:
            log(f"无待处理记录，休眠 {poll_interval_seconds:.1f}s")
            sleep(poll_interval_seconds)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = parse_config()
    except ConfigError as exc:
        _LOGGER.error("配置错误: %s（Token 不会打印）", exc)
        return 2

    stop_event = threading.Event()

    def _stop(_sig, _fr):
        _LOGGER.info("收到信号 %s", _sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    db = None
    worker = None
    lock_fd = None
    try:
        # 连接前预检 + 单实例锁：ConfigError → 退出码 2，不构造 worker、不进 run_loop
        _preflight_database(cfg.db_path)
        lock_fd = _acquire_singleton_lock(_lock_path_for(cfg.db_path))
        db = Database(db_path=cfg.db_path)
        worker = make_worker(cfg, db)
        _LOGGER.info("C4 worker 启动: base_url=%s db=%s batch=%d poll=%.1fs",
                     cfg.base_url, cfg.db_path, cfg.batch_size, cfg.poll_interval_seconds)
        run_loop(worker, poll_interval_seconds=cfg.poll_interval_seconds,
                 batch_size=cfg.batch_size, should_stop=stop_event.is_set)
    except ConfigError as exc:
        _LOGGER.error("C4 worker 启动失败: %s", exc)
        return 2
    finally:
        # 1) Worker close 优先；2) 无论成败最后关闭锁 fd；清理异常只记录，不覆盖原始异常。
        if worker is not None:
            worker_loop_close = getattr(worker, "close", None)
            if callable(worker_loop_close):
                try:
                    worker_loop_close()
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("C4 worker close 异常", exc_info=True)
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:  # noqa: BLE001
                _LOGGER.warning("C4 worker 锁 fd 关闭异常", exc_info=True)
        _LOGGER.info("C4 worker 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())