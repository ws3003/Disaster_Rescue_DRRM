"""V4.4 B E7→E8 高频采样根因验证脚本（临时、独立，不修复业务代码）。

作用：在 Raspberry Pi 上对比两种采样周期下，E7→E8 全链路是否在限时窗口内完成
（尤其验证：原始测试 `_fast_sleep` 把 1s 采样压缩为 ~1ms 造成的高频写库负载，
是否导致首次告警/FakeRecorder 迟迟不完成）。

两种模式（均走与原始测试**相同**的边缘装配入口 `EdgeE7E8`）：
- low  : 正常 1s 采样周期（time.sleep）
- fast : 模拟 `_fast_sleep` 的 1ms 语义（time.sleep(min(x, 0.001))）

可选 `--camera`：额外启动一个与原始测试 `cam_service` 等价的真实 `CameraService`
采集线程，用于隔离验证"CameraService 采集线程是否影响 E7→E8 链路"（实验 A）。
不带 `--camera` 时行为与之前完全一致（使用 `_CAM_SENTINEL`，不启相机）。

用法：
    python scripts/diag_e7e8.py --mode low
    python scripts/diag_e7e8.py --mode fast
    python scripts/diag_e7e8.py --mode fast --camera

约束：
- 独立临时 SQLite；不触正式库 data/drrm.db。
- FakeReader + FakeRecorder（包装），uploader=None（无真实 OSS）。
- 不 monkey patch 生产类、不改业务执行顺序；`--camera` 仅按官方 CameraService
  start/close 接口启停采集线程（Picamera2 用与测试等价的 FakeCam mock，只注 sys.modules）。
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
import types
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
from src.camera_service import CameraService  # noqa: E402
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
# 不再用它（e7e8_integration.build_evidence recorder 优先）。不使用 --camera 时传哨兵
# 即可完整复现集成测试装配，又不启动真相机/采集线程。
_CAM_SENTINEL = object()


# ---------------------------------------------------------------------------
# 与原始测试 `_FakeCam`（tests/test_e7e8_edge_integration.py:57-87）等价的
# Picamera2 mock：方法逐一对照测试实现，另加 `capture_count` 类级计数器用于观测。
# 仅 --camera 时经 sys.modules["picamera2"] 注入。
# ---------------------------------------------------------------------------
class _DiagFakeCam:
    capture_count = 0

    def __init__(self):
        self.started = False
        self.closed = False
        self._i = 0
        self.main = None

    def create_video_configuration(self, main=None, controls=None):
        self.main = main
        return {"main": main}

    def configure(self, c):
        pass

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def capture_array(self):
        import numpy as np
        _DiagFakeCam.capture_count += 1
        self._i += 1
        h, w = self.main["size"][1], self.main["size"][0]
        return np.zeros((h, w, 3), dtype=np.uint8) + (self._i % 255)


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


def _start_camera(width, height, fps, queue_size):
    """按测试 cam_service fixture 等价方式启动 CameraService（Picamera2 用 mock）。

    返回 (camera_service, 采集线程句柄)。注入 sys.modules["picamera2"] 仅本进程内生效；
    启动 Service 会惰性 import picamera2，故先用 mock 模块取代，再 start。
    """
    _DiagFakeCam.capture_count = 0
    mod = types.ModuleType("picamera2")
    mod.Picamera2 = _DiagFakeCam
    sys.modules["picamera2"] = mod
    svc = CameraService(width=width, height=height, fps=fps, queue_size=queue_size)
    svc.start()
    return svc, getattr(svc, "_thread", None)


def run_one(mode: str, with_camera: bool = False) -> dict:
    """跑一种模式，返回结果字典（内部完成临时库/线程/相机清理）。"""
    out: dict = {}
    started_wall = time.time()
    t0 = time.monotonic()
    tmp_dir = tempfile.mkdtemp(prefix=f"e7e8_diag_{mode}_")
    db_path = os.path.join(tmp_dir, "e7e8_diag.db")
    db = Database(db_path=db_path)
    edge = None
    camera_service = None
    cam_thread = None
    excepthook_orig = threading.excepthook
    thread_exc: dict = {"tb": None, "name": None}
    dest_paths_cleanup = []
    cam_cleanup_err: str | None = None

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
            # --camera：与测试 cam_service 等价启动真实 CameraService 采集线程
            if with_camera:
                camera_service, cam_thread = _start_camera(8, 6, 30.0, 8)
                out["cam_started_ok"] = camera_service.state in ("RUNNING", "STOPPING")
                out["cam_thread_started"] = cam_thread is not None

            edge = integ.EdgeE7E8(
                cfg,
                E8Config(enabled=True, record_seconds=0, width=8, height=6,
                         fps=30.0, codec="mp4v", tmp_dir=tmp_dir),
                db,
                camera_service=(camera_service if with_camera else _CAM_SENTINEL),
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
            # finally 会关闭相机并清理临时库；此处直接返回，cleaned 由 finally 回填。
            # thread_tb 无线程异常，置 None 以免缺键。
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

        # 相机观测快照（观测结束、关闭前）
        if with_camera:
            out["cam_capture_frames"] = _DiagFakeCam.capture_count
            out["cam_state_at_end"] = camera_service.state
            out["cam_fault_at_end"] = camera_service.fault()
            out["cam_thread_alive_at_end"] = cam_thread is not None and cam_thread.is_alive()

        out.update(
            mode=mode,
            elapsed=elapsed_total,
            db_path=db_path,
            with_camera=with_camera,
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
        # 关停：先关 E7/E8（edge），再关 CameraService；各自独立记录异常，避免互相覆盖
        try:
            if edge is not None:
                edge.close(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            out["edge_close_err"] = f"{type(exc).__name__}: {exc}"
        if camera_service is not None:
            prev_state = camera_service.state
            try:
                camera_service.close(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                cam_cleanup_err = f"{type(exc).__name__}: {exc}"
            out["camera_close_ok"] = (camera_service.state == "CLOSED")
            out["camera_cleanup_err"] = cam_cleanup_err
            out["cam_state_before_close"] = prev_state
            out["cam_thread_alive_final"] = (cam_thread is not None and cam_thread.is_alive())

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
        alive_cam = cam_thread is not None and cam_thread.is_alive()
        out["final_alive_sample"] = alive_s
        out["final_alive_consumer"] = alive_c
        out["final_alive_camera"] = alive_cam

        if alive_s or alive_c or alive_cam:
            out["cleaned"] = False
            out["cleanup_note"] = (f"线程仍未退出(sample={alive_s},consumer={alive_c},"
                                   f"camera={alive_cam})；保留临时目录/数据库: {tmp_dir}")
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

    print("\n===== 诊断结果 " + f"[mode={mode}" + (" +camera" if out.get("with_camera") else "") + "] =====")
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

    if out.get("with_camera"):
        print("| 相机采集帧数 | {0} | 非E7采样次数 |".format(out.get("cam_capture_frames", "?")))
        print("| 相机启动 | started_ok={0} thread_started={1} | state={2} |".format(
            _ok(out.get("cam_started_ok", False)), _ok(out.get("cam_thread_started", False)),
            out.get("cam_state_at_end", "?")))
        print("| 相机关闭 | close_ok={0} 关闭后线程alive={1} | state_before={2} fault={3} |".format(
            _ok(out.get("camera_close_ok", False)), _ok(out.get("cam_thread_alive_final", True)),
            out.get("cam_state_before_close", "?"),
            out.get("cam_fault_at_end", "无") if not out.get("cam_fault_at_end") else out.get("cam_fault_at_end")))
        if out.get("camera_cleanup_err"):
            print(f"| 相机关闭异常 | {out['camera_cleanup_err']} | 单独记录，不覆盖原始诊断异常 |")

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
        labels.append("A(采样/读取异常)")
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
    cam_alive_str = "-" if not out.get("with_camera") else str(out.get("final_alive_camera"))
    print(f"采样线程存活={out['final_alive_sample']}  "
          f"消费线程存活={out['final_alive_consumer']}  "
          f"相机线程存活={cam_alive_str}")
    print(f"临时资源清理: cleaned={out['cleaned']} {out.get('cleanup_note','')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="E7→E8 高频采样根因验证（Pi）")
    ap.add_argument("--mode", choices=["low", "fast"], default="low",
                    help="low=1s采样周期; fast=~1ms采样周期(模拟测试 _fast_sleep)")
    ap.add_argument("--camera", action="store_true",
                    help="启动与测试 cam_service 等价的 CameraService 采集线程(实验A)")
    args = ap.parse_args()

    out = run_one(args.mode, with_camera=args.camera)
    if out.get("error"):
        print(f"\n[装配失败] mode={out.get('mode')} db={out.get('db_path')} 临时库清理={out.get('cleaned')}")
        print(out["error"])
        return 1
    _report(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())