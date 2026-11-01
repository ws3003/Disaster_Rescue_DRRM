"""V4.4 B E7→E8 高频采样根因验证脚本（临时、独立，不修复业务代码）。

作用：在 Raspberry Pi 上对比两种采样周期下，E7→E8 全链路是否在限时窗口内完成
（尤其验证：原始测试 `_fast_sleep` 把 1s 采样压缩为 ~1ms 造成的高频写库负载，
是否导致首次告警/FakeRecorder 迟迟不完成）。

两种模式（均走与原始测试**相同**的边缘装配入口 `EdgeE7E8`）：
- low  : 正常 1s 采样周期（time.sleep）
- fast : 模拟 `_fast_sleep` 的 1ms 语义（time.sleep(min(x, 0.001))）

用法：
    python scripts/diag_e7e8.py --mode low
    python scripts/diag_e7e8.py --mode fast

约束：
- 独立临时 SQLite；不触正式库 data/drrm.db。
- FakeReader + FakeRecorder（包装），uploader=None（无真实 OSS）。
- 不 monkey patch 生产类、不改业务执行顺序、不触真实 GPIO / 相机 / DingTalk。
- 固定限时 DIAG_WINDOW_SECONDS；fast 即使首拍成功也续跑到窗口满，观察高频稳态。
- 退出前必须确认采样/消费线程退出；线程未退则**不删除**临时数据库，并明确报告。

异常捕获：EdgeE7E8 内部线程若 loop.step 抛 SensorPersistError（如 DB locked），
run() 无捕获会使线程未捕获异常退出 —— 本脚本用 `threading.excepthook` 记录完整
traceback（脚本内观测装置，不改生产类，结束时恢复）。
"""

from __future__ import annotations

import sys
import os
import argparse
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
from src import e7e8_integration as integ  # noqa: E402
from src.evidence.recorder import FakeRecorder as _BusinessFakeRecorder  # noqa: E402
from scripts.e7e8_runner import E8Config  # noqa: E402

WINDOWS = (os.name == "nt")

# 与 tests/test_e7e8_edge_integration.py::_SENSOR / _hot_reader 同语义
_SENSOR = {
    "sensor_id": "ds18b20-01",
    "temperature_threshold": 40.0, "level2_threshold": 50.0, "recovery_threshold": 36.0,
    "sample_period_seconds": 1.0, "confirm_high_count": 1, "confirm_recovery_count": 1,
    "cooldown_seconds": 1.0, "temperature_min": -40.0, "temperature_max": 85.0,
    "humidity_min": 0.0, "humidity_max": 100.0, "reader": "fake",
    "device_id": "28-0224e33f1e4e",
}

DIAG_WINDOW_SECONDS = 8.0    # 每模式限时上限
POLL_INTERVAL = 0.05

# SQLite 写失败类错误的关键子串（用于从线程 traceback 取证，不直接数 BUSY）
_SQLITE_LOCK_HINTS = ("is locked", "locked", "busy", "operationalerror", "sqlite")


def _hot_reader():
    return FakeReader(lambda: (55.0, 50.0))   # 稳定 55.0 > L2=50


def _fast_sleep(x: float) -> None:
    time.sleep(min(x, 0.001))                 # 与测试 _fast_sleep 语义一致


def _sleep_for(mode: str):
    return _fast_sleep if mode == "fast" else time.sleep


# EdgeE7E8 仅当 camera_service is None 才报错；recorder 非 None 时 build_evidence
# 不再用它（e7e8_integration.build_evidence recorder 优先）。传哨兵即可完整复现
# 集成测试装配，又不启动真相机/采集线程。
_CAM_SENTINEL = object()


# ---------------------------------------------------------------------------
# 观测包装器（在正式类外部统计，不 monkey patch）
# ---------------------------------------------------------------------------
class WrapperReader:
    """采样读取包装器（内层 FakeReader，满足 read(now=None) 契约）。"""

    def __init__(self, inner: FakeReader):
        self._inner = inner
        self.attempts = 0
        self.read_ok = 0
        self.read_errors = 0
        self.last_error_tb: str | None = None
        self.first_ok_at: float | None = None
        self._lock = threading.Lock()
        self._t0: float | None = None
        self.cum_read_sec = 0.0
        self._prev_read_at: float | None = None
        self.max_read_gap: float = 0.0   # 相邻 read 间隔上限（≈ 一拍 loop.step + 采样sleep）
        self.read_gaps_sampled = 0

    def set_t0(self, t0: float) -> None:
        self._t0 = t0

    def read(self, now=None):
        st = time.monotonic()
        with self._lock:
            self.attempts += 1
            if self._prev_read_at is not None:
                gap = st - self._prev_read_at
                if gap > self.max_read_gap:
                    self.max_read_gap = gap
                self.read_gaps_sampled += 1
            self._prev_read_at = st
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

    def close(self):
        c = getattr(self._inner, "close", None)
        if callable(c):
            c()


class MetricRecorder:
    """录像后端包装器（内层业务 FakeRecorder，契约 record(seconds, dest_path) -> str）。"""

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
    try:
        with db._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0)
    except Exception:
        return -1


def _sec(at: float | None) -> str:
    return f"{at:.2f}s" if at is not None else "-"


def _ok(cond: bool) -> str:
    return "是" if cond else "否"


def run_one(mode: str) -> dict:
    """跑一种模式，返回结果字典（内部完成临时库/线程清理）。"""
    out: dict = {}
    started_wall = time.time()
    t0 = time.monotonic()
    tmp_dir = tempfile.mkdtemp(prefix=f"e7e8_diag_{mode}_")
    db_path = os.path.join(tmp_dir, "e7e8_diag.db")
    db = Database(db_path=db_path)
    edge = None
    excepthook_orig = threading.excepthook
    thread_exc: dict = {"tb": None, "name": None}
    dest_paths_cleanup = []

    def _hook(args):
        try:
            thread_exc["tb"] = "".join(
                _tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            thread_exc["name"] = args.thread.name
        except Exception:  # noqa: BLE001
            pass

    try:
        reader = WrapperReader(_hot_reader())
        reader.set_t0(t0)
        metric = MetricRecorder()
        metric.set_t0(t0)
        dest_paths_cleanup = metric.dest_paths

        cfg = sensor_config_from_dict(_SENSOR)
        try:
            edge = integ.EdgeE7E8(
                cfg,
                E8Config(enabled=True, record_seconds=0, width=8, height=6,
                         fps=30.0, codec="mp4v", tmp_dir=tmp_dir),
                db,
                camera_service=_CAM_SENTINEL,          # 占位：recorder 注入时不建共享录制器
                reader=reader,
                recorder=metric,                        # E8 录像后端 → 包装 FakeRecorder
                uploader=None,                          # 无真实 OSS
                sleep=_sleep_for(mode),                # low=time.sleep, fast=_fast_sleep
            )
            assert edge.mode == "e7+e8", "EdgeE7E8 未处于 e7+e8 模式"
        except Exception as exc:  # noqa: BLE001 — 装配失败：输出清晰信息而非 KeyError
            out["error"] = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
            out["mode"] = mode
            out["db_path"] = db_path
            out["failed"] = True
            # 装配失败：except 只包住**构造语句**，外层 try 主体内它之后的 start/观测
            # 仍会执行（edge=None 会崩溃）——因此这里必须 return。
            # Python 保证 return 前先执行 finally：finally 对 edge=None 判 alive 全 False，
            # 走 else 分支 rmtree 临时库并回填 cleaned=True / cleanup_note，故此处直接返回
            # 也能把清理结果正确带回。thread_tb 无线程异常，置 None 以免缺键。
            out.setdefault("thread_tb", None)
            return out

        threading.excepthook = _hook
        edge.start()
        sampler = edge._thread
        consumer = edge.runner._thread if (edge.runner is not None) else None

        stage: dict[str, float | None] = {"reading": None, "alert": None, "record": None}
        lock_hits = 0
        lock_tb: list[str] = []

        deadline = time.monotonic() + DIAG_WINDOW_SECONDS
        while time.monotonic() < deadline:
            rd = _db_count(db, "SELECT COUNT(*) FROM readings")
            al = _db_count(db, "SELECT COUNT(*) FROM sensor_alerts")
            elapsed = time.time() - started_wall
            if rd > 0 and stage["reading"] is None:
                stage["reading"] = elapsed
            if al > 0 and stage["alert"] is None:
                stage["alert"] = elapsed
            if metric.calls > 0 and stage["record"] is None:
                stage["record"] = elapsed

            # 从线程未捕获异常中取证 SQLite 锁（写失败多 locked/busy；直接数 BUSY 不可靠）。
            # 仅对**不同**锁相关 traceback 去重计数，不重复统计同一 tb。
            if thread_exc["tb"] and any(k in thread_exc["tb"].lower() for k in _SQLITE_LOCK_HINTS):
                if thread_exc["tb"] not in lock_tb:
                    lock_tb.append(thread_exc["tb"])
            # 采样线程已因异常终止 → 提前停止观测
            if sampler is not None and not sampler.is_alive() and thread_exc["tb"] is not None:
                break
            time.sleep(POLL_INTERVAL)

        # 去重后不同锁相关 traceback 数（≠ SQLite 锁异常真实发生次数）
        lock_hits = len(lock_tb)
        elapsed_total = time.time() - started_wall

        dropped = list(edge.runner.dropped) if edge.runner is not None else []
        cons_exc = [d for d in dropped if "consumer_exception" in str(d.get("reason", ""))]
        try:
            qsize = edge.runner._queue.qsize() if edge.runner is not None else -1
        except Exception:
            qsize = -1

        out.update(
            mode=mode,
            elapsed=elapsed_total,
            db_path=db_path,
            reader=reader, metric=metric,
            stage=stage,
            dropped=dropped, cons_exc=cons_exc, qsize=qsize,
            lock_hits=lock_hits, lock_tb=lock_tb,
            final_high = {
                "readings": _db_count(db, "SELECT COUNT(*) FROM readings"),
                "alerts": _db_count(db, "SELECT COUNT(*) FROM sensor_alerts"),
                "outbox": _db_count(db, "SELECT COUNT(*) FROM sync_outbox"),
                "verify": _db_count(db, "SELECT COUNT(*) FROM c4_verify"),
                "media": _db_count(db, "SELECT COUNT(*) FROM media_evidence"),
            },
        )
        out["sampler_alive_after_run"] = (sampler is not None and sampler.is_alive())
        out["consumer_alive_after_run"] = (consumer is not None and consumer.is_alive())
    finally:
        threading.excepthook = excepthook_orig
        try:
            if edge is not None:
                edge.close(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        # 关停后判定线程最终状态，并据此决定是否删除临时资源
        try:
            sampler = edge._thread if edge is not None else None
        except Exception:
            sampler = None
        try:
            consumer = edge.runner._thread if (edge is not None and edge.runner is not None) else None
        except Exception:
            consumer = None
        alive_s = sampler is not None and sampler.is_alive()
        alive_c = consumer is not None and consumer.is_alive()
        out["final_alive_sample"] = alive_s
        out["final_alive_consumer"] = alive_c

        if alive_s or alive_c:
            out["cleaned"] = False
            out["cleanup_note"] = (f"线程仍未退出(sample={alive_s},consumer={alive_c})；"
                                   f"保留临时目录/数据库: {tmp_dir}")
        else:
            removed = False
            try:
                for p in list(dest_paths_cleanup):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
                shutil.rmtree(tmp_dir, ignore_errors=True)
                removed = os.path.exists(tmp_dir) is False
            except Exception:
                removed = False
            out["cleaned"] = removed
            out["cleanup_note"] = "" if removed else (f"临时目录删除失败: {tmp_dir}")
    out["thread_tb"] = thread_exc["tb"]
    out["thread_name"] = thread_exc["name"]
    return out


def _report(out: dict) -> None:
    rd = out["reader"]
    m = out["metric"]
    st = out["stage"]
    f = out["final_high"]
    thread_tb = out.get("thread_tb")
    mode = out["mode"]

    print("\n===== 诊断结果 " + f"[mode={mode}] =====")
    print(f"采样周期模式    : {'fast(~1ms)' if mode == 'fast' else 'low(1s)'}  "
          f"限时={DIAG_WINDOW_SECONDS}s 实际={out['elapsed']:.2f}s")
    print(f"运行时平台      : {'Windows' if WINDOWS else 'Pi/其他'}")
    print(f"临时数据库文件  : {out['db_path']}")
    print("---- 指标表 ----")
    print("| 指标 | 值 | 说明 |")
    print("|---|---|---|")
    print(f"| E7 采样 | 尝试={rd.attempts} 成功={rd.read_ok} 异常={rd.read_errors} | "
          f"累计读取耗时={rd.cum_read_sec:.4f}s |")
    print(f"| 采样间隔(相邻采样) | 上限={rd.max_read_gap:.4f}s(相邻read入口间隔, 采样{rd.read_gaps_sampled}次) | "
          f"low:含采样sleep(≈1s)+step执行; fast:更接近step执行耗时,非纯SQLite写耗时(单拍无法直接观测) |")
    print(f"| reading 持久化 | readings={f['readings']} 首次={_sec(st['reading'])} | 首现观测 |")
    print(f"| 告警生成 | sensor_alerts={f['alerts']} 首次={_sec(st['alert'])} | 首现观测 |")
    print(f"| 入队/消费 | record调用={m.calls} 消费异常={len(out['cons_exc'])} | "
          f"sync_outbox={f['outbox']} c4_verify={f['verify']} dropped={len(out['dropped'])} {out['dropped'][:3]} |")
    print(f"| FakeRecorder | 完成={m.completed} 失败={m.failed} 累计耗时={m.cum_record_sec:.4f}s | "
          f"media_evidence={f['media']} qsize={out['qsize']} |")
    print(f"| SQLITE 锁证据(可捕获, 去重) | 不同锁相关traceback数={out['lock_hits']} | "
          f"非SQLite锁异常真实发生次数；观测盲区见下方，不能据此排除所有锁竞争 |")

    print("---- 观测盲区说明 ----")
    print("- loop._persist 会把部分 sensor_status 等写入异常转为 warning（src/sensor/loop.py _persist），"
          "这些 warning 不会进入线程 excepthook。")
    print("- 因此 lock_hits 只能依据可捕获的 bridge 异常(reading/告警 bridge 抛出的 SensorPersistError)统计，"
          "不能据此排除所有 SQLite 锁竞争。")

    if out["lock_tb"]:
        print("---- 含 locked/busy 的线程 traceback ----")
        print(out["lock_tb"][0])
    if thread_tb is not None:
        print("---- 线程未捕获异常完整 traceback ----")
        print(thread_tb)

    # FakeRecorder 是否在限时窗口内完成
    done = m.completed > 0
    first_ok = f["alerts"] > 0

    labels = []
    if rd.read_errors > 0 or (thread_tb is not None):
        labels.append("A(采样/读取异常或采样线程终止)")
    if f["readings"] == 0 and rd.read_ok > 0:
        labels.append("B(reading 持久化未推进)")
    if f["alerts"] == 0 and rd.read_ok > 0:
        labels.append("C(告警状态机/告警持久化未产生告警)")
    if first_ok and m.calls == 0:
        labels.append("D(告警已生成但未达 E8 录像端)")
    if first_ok and m.calls == 0 and out["final_alive_consumer"] is False:
        labels.append("E(消费线程未存活)")
    if m.calls > 0 and m.completed == 0:
        labels.append("F(录像执行失败)")
    if out["lock_hits"]:
        labels.append("LOCK(高频写触数据库锁)")
    if not labels:
        labels.append("G(链路各阶段可观测成功)" if done else "G(局部观测不足以定位，需更细粒度)")
    print(f"\n---- 定位建议 ----\n{'；'.join(labels)}")

    print("\n---- 线程退出状态 ----")
    print(f"采样线程存活(最后判定并清理后)={out['final_alive_sample']}  "
          f"消费线程存活={out['final_alive_consumer']}")
    print(f"临时资源清理: cleaned={out['cleaned']}");


def main() -> int:
    ap = argparse.ArgumentParser(description="E7→E8 高频采样根因验证（Pi）")
    ap.add_argument("--mode", choices=["low", "fast"], default="low",
                    help="low=1s采样周期; fast=~1ms采样周期(模拟测试 _fast_sleep)")
    args = ap.parse_args()

    out = run_one(args.mode)
    if out.get("error"):
        print(f"\n[装配失败] mode={out.get('mode')} db={out.get('db_path')} 临时库清理={out.get('cleaned')}")
        print(out["error"])
        return 1
    _report(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())