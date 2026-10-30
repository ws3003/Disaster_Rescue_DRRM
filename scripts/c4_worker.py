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

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from types import FrameType
from typing import Callable, Optional

from src.database.db import Database
from src.sync.worker import OutboxWorker

_LOGGER = logging.getLogger("c4.worker")

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

    db = Database(db_path=cfg.db_path)
    worker = make_worker(cfg, db)
    _LOGGER.info("C4 worker 启动: base_url=%s db=%s batch=%d poll=%.1fs",
                 cfg.base_url, cfg.db_path, cfg.batch_size, cfg.poll_interval_seconds)
    try:
        run_loop(worker, poll_interval_seconds=cfg.poll_interval_seconds,
                 batch_size=cfg.batch_size, should_stop=stop_event.is_set)
    finally:
        worker_loop_close = getattr(worker, "close", None)
        if callable(worker_loop_close):
            worker_loop_close()
        _LOGGER.info("C4 worker 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())