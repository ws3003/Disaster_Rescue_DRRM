"""V4.4 B E7→E8 短时故障诊断脚本（临时、独立，不修复业务代码）。

目标：在可观测且不触正式资源的前提下，复现并定位 Raspberry Pi 5 上
``TestE7TriggersE8::test_hot_alert_consumed_and_fake_recorded`` 失败的断链阶段。

方法：
- 复用正式装配入口 ``src.sensor.runner.build_runner`` 走真实路径
  runner.run → sample → loop.step → enqueue → _consume → handle_event → recorder.record。
- 依赖全注入 / 替换：FakeReader（高温）、独立临时 SQLite、业务 FakeRecorder 的包装器、
  uploader=None（跳过 OSS）。不触真实 GPIO / 相机 / 正式库 / 网络 / DingTalk。
- 正常 1 秒采样周期，不用 _fast_sleep。
- 观测全部在正式类**外部**加包装/旁路计数，不 monkey patch，不改业务顺序。
- 采样线程在 target 外层 try/except 捕获并记录完整 traceback（run() 内部无此捕获）。

用法（Windows 验证 / 在 Pi 实机上运行同一脚本）：
    python scripts/diag_e7e8.py

注意：本脚本只在脚本自身挂载独立临时目录，测试结束后若线程可安全退出才删除，
否则保留路径并明确报告（见 finally）。
"""

from __future__ import annotations

import sys
import os
import tempfile
import shutil
import threading
import time
import traceback as _tb
from pathlib import Path

# repo 根入 sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.database.db import Database  # noqa: E402
from src.sensor.config import sensor_config_from_dict  # noqa: E402
from src.sensor.reader import FakeReader  # noqa: E402
from src.sensor.runner import build_runner  # noqa: E402
from src.evidence.recorder import FakeRecorder as _BusinessFakeRecorder  # noqa: E402

WINDOWS = (os.name == "nt")

# 与 tests/test_e7e8_edge_integration.py::_SENSOR / _hot_reader 同语义；
# 采样周期必须为正常 1s（不用 _fast_sleep）。
_SENSOR = {
    "sensor_id": "ds18b20-01",
    "temperature_threshold": 40.0, "level2_threshold": 50.0, "recovery_threshold": 36.0,
    "sample_period_seconds": 1.0, "confirm_high_count": 1, "confirm_recovery_count": 1,
    "cooldown_seconds": 1.0, "temperature_min": -40.0, "temperature_max": 85.0,
    "humidity_min": 0.0, "humidity_max": 100.0, "reader": "fake",
    "device_id": "28-0224e33f1e4e",
}

DIAG_WINDOW_SECONDS = 10.0    # 诊断上限；超时强制停止采样+消费线程
POLL_INTERVAL = 0.2


class WrapperReader:
    """采样读取包装器（内层 FakeReader，满足 read(now=None) 契约）。

    在 read 外层统计：采样尝试 / 成功 / 异常。仅观测，不改内层行为。
    """

    def __init__(self, inner: FakeReader):
        self._inner = inner
        self.attempts = 0          # read() 被调用次数（采样尝试）
        self.read_ok = 0           # read() 正常返回次数
        self.read_errors = 0       # read() 抛异常次数
        self.last_error_tb: str | None = None
        self.first_ok_at: float | None = None
        self._lock = threading.Lock()
        self._t0: float | None = None
        self.cum_read_sec = 0.0

    def set_t0(self, t0: float) -> None:
        self._t0 = t0

    def read(self, now=None):
        st = time.monotonic()
        with self._lock:
            self.attempts += 1
        try:
            r = self._inner.read(now=now)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.read_errors += 1
                self.last_error_tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
            raise
        el = time.monotonic() - st
        with self._lock:
            self.read_ok += 1
            self.cum_read_sec += el
            if self.first_ok_at is None and self._t0 is not None:
                self.first_ok_at = time.time() - self._t0
        return r

    def close(self):  # loop.close() 会调 reader.close；委托内层（FakeReader 无 close，安全）
        c = getattr(self._inner, "close", None)
        if callable(c):
            c()


class MetricRecorder:
    """录像后端包装器（内层业务 FakeRecorder，契约 record(seconds, dest_path) -> str）。

    记录 record 调用次数、耗时、完成/失败。仅观测，不改内层录像行为。
    """

    def __init__(self):
        self._inner = _BusinessFakeRecorder()
        self.calls = 0
        self.completed = 0
        self.failed = 0
        self.dest_paths: list[str] = []
        self.done_first_at: float | None = None
        self.last_error_tb: str | None = None
        self.t0: float | None = None
        self._lock = threading.Lock()
        self.cum_record_sec = 0.0

    def set_t0(self, t0: float) -> None:
        self.t0 = t0

    def record(self, seconds, dest_path):
        st = time.monotonic()
        with self._lock:
            self.calls += 1
            self.dest_paths.append(str(dest_path))
        try:
            out = self._inner.record(seconds, dest_path)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.failed += 1
                self.last_error_tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
            raise
        el = time.monotonic() - st
        with self._lock:
            self.completed += 1
            self.cum_record_sec += el
            if self.done_first_at is None and self.t0 is not None:
                self.done_first_at = time.time() - self.t0
        return out


def _db_count(db, sql: str, params: tuple = ()) -> int:
    """只读观测用 COUNT（不动数据，不影响业务执行）。"""
    try:
        with db._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0)
    except Exception:
        return -1


def _ok(cond: bool) -> str:
    return "是" if cond else "否"


def _f(label: str, at: float | None) -> str:
    return f" {label}={at:.2f}s" if at is not None else ""


def _enqueue_note(dropped: list) -> str:
    qfull = [d for d in dropped if "queue_full" in str(d.get("reason", ""))]
    return "无" if not qfull else f"{len(qfull)} 个 queue_full: " + " ".join(str(d) for d in qfull[:3])


def _derive_enqueue(saw_alert: bool, record_calls: int, dropped: list) -> str:
    """入队成功与否的间接判定：事件能否从队列抵达消费端录像。

    说明：enqueue() 由 run() 内部 sample 调用，无法不改正式代码直接数返回分支，
    故用『告警已生成 && 有 record 调用 && 无 queue_full 丢弃』作为成功证据。
    """
    qfull = any("queue_full" in str(d.get("reason", "")) for d in dropped)
    if not saw_alert:
        return "否(未观测到告警)"
    if record_calls > 0 and not qfull:
        return "是(间接: 事件已到消费端录像)"
    if qfull:
        return "否(queue_full 丢弃)"
    return "待定(告警已生成但事件未达消费端录像)"


def _locate(reader, final, saw_alert, metric, diag_exc, consumer_alive) -> str:
    """按优先级给出定位字母 A–G；多阶段异常则并列。final=观测统计字典。"""
    labels = []
    if reader.read_errors > 0 or diag_exc["tb"] is not None:
        labels.append("A(采样/读取异常)")
    if final["readings"] == 0 and reader.read_ok > 0:
        labels.append("B(reading 持久化未推进)")
    if saw_alert and metric.calls == 0:
        labels.append("D(告警已生成但未达 E8 录像端)")
    elif not saw_alert and reader.read_ok > 0:
        labels.append("C(告警状态机/告警持久化未产生告警)")
    if saw_alert and metric.calls == 0 and not consumer_alive:
        labels.append("E(消费线程未存活)")
    if metric.calls > 0 and metric.completed == 0:
        labels.append("F(录像执行失败)")
    if not labels:
        if metric.completed > 0:
            labels.append("G(链路各阶段均可观测成功；需在 Pi 实机复现以确认，Windows 不能判根因)")
        else:
            labels.append("G(局部观测不足以定位，需测 loop.step 单步耗时/各写入耗时)")
    return "；".join(labels)


def main() -> int:
    started_wall = time.time()
    t0 = time.monotonic()
    samphd: threading.Thread | None = None
    runner = None
    metric: MetricRecorder | None = None
    tmp_dir: str | None = None
    db_path: str | None = None
    cleaned_up = False
    cleanup_note = ""

    # ---- 独立临时 SQLite（绝不触正式库 data/drrm.db）----
    tmp_dir = tempfile.mkdtemp(prefix="e7e8_diag_")
    db_path = os.path.join(tmp_dir, "e7e8_diag.db")
    db = Database(db_path=db_path)
    db_file_location = str(db_path)

    try:
        reader = WrapperReader(FakeReader(lambda: (55.0, 50.0)))   # 稳定 55.0 > L2=50
        reader.set_t0(t0)
        metric = MetricRecorder()
        metric.set_t0(t0)

        cfg = sensor_config_from_dict(_SENSOR)
        runner = build_runner(
            cfg,
            db=db,
            reader=reader,
            recorder_adapter=metric,      # E8 录像后端 → 包装的 FakeRecorder
            uploader=None,                # 跳过真实 OSS
            sleep=time.sleep,             # 正常 1s 采样周期，不用 _fast_sleep
            recover_interval_seconds=60.0,
        )

        # ---- 采样线程 target 外层捕获完整 traceback（run() 内部无此捕获）----
        diag_exc: dict = {"tb": None}

        def _sample_target():
            try:
                runner.run(steps_per_cycle=1)   # run() 内部会 start() 消费线程
            except BaseException as exc:  # noqa: BLE001
                diag_exc["tb"] = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))

        samphd = threading.Thread(target=_sample_target, name="diag-sampler", daemon=True)
        samphd.start()

        # ---- 观测阶段 ----
        stage_first_at: dict[str, float | None] = {
            "reading_persist": None, "alert_gen": None, "consume": None, "fake_recorder": None,
        }
        saw_alert = False
        last_prints = {}

        deadline = time.monotonic() + DIAG_WINDOW_SECONDS
        while time.monotonic() < deadline:
            elapsed = time.time() - started_wall
            rd = _db_count(db, "SELECT COUNT(*) FROM readings")
            al = _db_count(db, "SELECT COUNT(*) FROM sensor_alerts")

            if rd > 0 and stage_first_at["reading_persist"] is None:
                stage_first_at["reading_persist"] = elapsed
            if al > 0 and stage_first_at["alert_gen"] is None:
                stage_first_at["alert_gen"] = elapsed
                saw_alert = True
            if metric.calls > 0 and stage_first_at["consume"] is None:
                # record 被调 = 二级事件抵达消费端并进入录像
                stage_first_at["consume"] = elapsed
            if metric.completed > 0 and stage_first_at["fake_recorder"] is None:
                stage_first_at["fake_recorder"] = elapsed

            last_prints.update(
                attempts=reader.attempts, readings=rd, alerts=al,
                record_calls=metric.calls, record_done=metric.completed,
            )

            sample_exited = samphd is not None and not samphd.is_alive()
            if metric.completed >= 1:
                break   # FakeRecorder 已完成 → 链路到过端，提前结束
            if sample_exited and diag_exc["tb"] is not None:
                break   # 采样异常退出 → 立即停止观测
            time.sleep(POLL_INTERVAL)

        elapsed_total = time.time() - started_wall

        # ---- 消费线程引用（run() 内 start 后才有）----
        consumer = getattr(runner, "_thread", None)
        sample_alive = samphd is not None and samphd.is_alive()
        consumer_alive = consumer is not None and consumer.is_alive()

        dropped = list(runner.dropped)
        consumer_exc = [d for d in dropped if "consumer_exception" in str(d.get("reason", ""))]
        try:
            qsize = runner._queue.qsize()
        except Exception:
            qsize = -1
        final = {
            "readings": _db_count(db, "SELECT COUNT(*) FROM readings"),
            "alerts": _db_count(db, "SELECT COUNT(*) FROM sensor_alerts"),
            "outbox": _db_count(db, "SELECT COUNT(*) FROM sync_outbox"),
            "verify": _db_count(db, "SELECT COUNT(*) FROM c4_verify"),
            "media": _db_count(db, "SELECT COUNT(*) FROM media_evidence"),
        }

        # ---- 阶段表 ----
        rows = [
            ("E7 采样", _ok(reader.attempts > 0),
             f"采样尝试={reader.attempts} 成功={reader.read_ok} 异常={reader.read_errors} "
             f"累计读取耗时={reader.cum_read_sec:.4f}s",
             "无" if reader.last_error_tb is None else "见下方 traceback"),
            ("reading 持久化", _ok(final["readings"] > 0),
             f"readings={final['readings']}" + _f(" 首次", stage_first_at["reading_persist"]),
             "无" if diag_exc["tb"] is None else "loop.step 处异常（见下方 traceback）"),
            ("告警生成", _ok(saw_alert),
             f"sensor_alerts={final['alerts']}" + _f(" 首次", stage_first_at["alert_gen"]),
             "无" if saw_alert else "未观测到告警行使"),
            ("E8 入队", _derive_enqueue(saw_alert, metric.calls, dropped),
             f"(间接观测) sync_outbox={final['outbox']} media_evidence={final['media']}",
             _enqueue_note(dropped)),
            ("E8 消费", _ok(metric.calls > 0 and not consumer_exc),
             f"到达录像端(record调用)={metric.calls} 消费异常={len(consumer_exc)}"
             + _f(" 首次", stage_first_at["consume"]),
             "无" if not consumer_exc else f"{len(consumer_exc)} 个 consumer_exception"),
            ("FakeRecorder", _ok(metric.completed > 0),
             f"record完成={metric.completed} record失败={metric.failed} "
             f"累计录像耗时={metric.cum_record_sec:.4f}s" + _f(" 首次", stage_first_at["fake_recorder"]),
             "无" if metric.last_error_tb is None else "见下方 traceback"),
        ]

        print("\n===== E7→E8 诊断结果 =====")
        print(f"运行平台        : {'Windows' if WINDOWS else 'Linux/其他'}")
        print(f"总运行时长      : {elapsed_total:.2f}s (上限 {DIAG_WINDOW_SECONDS}s)")
        print(f"临时数据库文件  : {db_file_location}")
        print(f"采样线程存活    : {sample_alive}   消费线程存活: {consumer_alive}")
        print(f"采样循环意外退出: {diag_exc['tb'] is not None}")
        if diag_exc["tb"]:
            print("---- 采样线程完整 traceback ----")
            print(diag_exc["tb"])
        print("---- 阶段表 ----")
        print("| 阶段 | 是否成功 | 次数/耗时 | 异常信息 |")
        print("|---|---|---|---|")
        for name, ok, detail, err in rows:
            print(f"| {name} | {ok} | {detail} | {err.replace(chr(10), ' / ') if err else ''} |")
        print(f"| 队列/媒体 | - | qsize={qsize} dropped={len(dropped)} media_evidence={final['media']} "
              f"sync_outbox={final['outbox']} c4_verify={final['verify']} | "
              + (" ".join(str(d) for d in dropped[:5]) or "无") + " |")

        print("\n---- 定位建议 ----")
        print(_locate(reader, final, saw_alert, metric, diag_exc, consumer_alive))

    finally:
        # ---- 停止采样+消费线程并安全退出后，才删临时资源 ----
        try:
            if runner is not None:
                runner.close(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            cleanup_note += f" runner.close 异常: {exc!r}; "

        sample_alive2 = samphd is not None and samphd.is_alive()
        consumer2 = getattr(runner, "_thread", None) if runner is not None else None
        consumer_alive2 = consumer2 is not None and consumer2.is_alive()

        if sample_alive2 or consumer_alive2:
            # 线程仍可能使用临时资源：不删除，明确报告
            cleanup_note += (f" 线程仍存活(sample={sample_alive2},consumer={consumer_alive2})，"
                             f"临时目录/数据库未删除: {tmp_dir}")
            cleaned_up = False
        else:
            # 线程已全部退出，可安全清理
            if metric is not None:
                for p in list(metric.dest_paths):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
            if tmp_dir:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    cleaned_up = True
                except Exception:
                    cleaned_up = False
                    cleanup_note = f" 临时目录删除失败: {tmp_dir}"
        print(f"\n[清理] cleaned={cleaned_up} note={cleanup_note or ''}")

    return 0


if __name__ == "__main__":
    sys.exit(main())