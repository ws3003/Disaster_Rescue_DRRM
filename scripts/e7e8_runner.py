#!/usr/bin/env python3
"""V4.4 B 线 E7+E8 生产装配入口（S1，方案 A）。

- E7：真实 DS18B20 采样 → readings/sensor_alerts → C4 Outbox（同一事务，先落库）。
- E8：仅当 edge.yaml e8.enabled=true 且摄像头可初始化时，用
  CameraRecorder(capture_factory=lambda: PicameraFrameSource(...)) 装配真实录像；
  每次告警触发录像新建一个 PicameraFrameSource，录毕由 CameraRecorder release（方案 A）。
- e8.enabled=false 或摄像头不可用时：**不**用 FakeRecorder 伪装成功，而是回退为 E7-only
  （SampledSensorLoop 直连，不创建录像器/OSS），告警与 C4 照常。
- C4 Outbox Worker 应独立进程运行（scripts/c4_worker.py），本进程不占摄像头之外共享资源。
- 信号：SIGINT/SIGTERM 优雅退出并关闭采样循环与摄像头资源。

注意到不使用 build_runner() 便捷构造：其无法把 record_seconds/tmp_dir 透传给 EvidenceRecorder，
为尊重 e8 配置而在此用既有类直接装配（不改 runner.py 核心）。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field as _field
from pathlib import Path
from typing import Optional

# 使 src 可导入（cwd=项目根或部署目录）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402

from src.database.db import Database  # noqa: E402
from src.sensor.alarm import HighTempAlarm  # noqa: E402
from src.sensor.config import load_sensor_config  # noqa: E402
from src.sensor.loop import SampledSensorLoop  # noqa: E402
from src.sensor.runner import SensorRunner, build_sensor_reader  # noqa: E402

_LOGGER = logging.getLogger("e7e8.runner")


class E8ConfigError(ValueError):
    """edge.yaml e8 段缺字段/类型非法。"""


@dataclass
class E8Config:
    enabled: bool = False
    record_seconds: int = 10
    width: int = 1280
    height: int = 720
    fps: float = 15.0
    codec: str = "mp4v"
    tmp_dir: Optional[str] = None


def _require_bool(v, key):
    if isinstance(v, bool):
        return v
    raise E8ConfigError(f"e8.{key} 须为 bool, got {v!r}")


def _require_int(v, key, lo):
    if isinstance(v, int) and not isinstance(v, bool) and v >= lo:
        return v
    raise E8ConfigError(f"e8.{key} 须为 int>= {lo}, got {v!r}")


def _require_float(v, key, lo):
    if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) >= lo:
        return float(v)
    raise E8ConfigError(f"e8.{key} 须为数值>= {lo}, got {v!r}")


def load_e8_config(edge_yaml: str) -> E8Config:
    """读取 edge.yaml 的 e8 段；缺失字段用默认值；类型非法抛 E8ConfigError。"""
    try:
        raw = yaml.safe_load(Path(edge_yaml).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise E8ConfigError(f"读取 {edge_yaml} 失败: {exc}") from exc
    sec = raw.get("e8") if isinstance(raw, dict) else {}
    if sec is None:
        sec = {}
    if not isinstance(sec, dict):
        raise E8ConfigError("e8 段须为映射(dict)")
    cfg = E8Config()
    cfg.enabled = _require_bool(sec.get("enabled", cfg.enabled), "enabled")
    cfg.record_seconds = _require_int(sec.get("record_seconds", cfg.record_seconds), "record_seconds", 1)
    cfg.width = _require_int(sec.get("width", cfg.width), "width", 1)
    cfg.height = _require_int(sec.get("height", cfg.height), "height", 1)
    cfg.fps = _require_float(sec.get("fps", cfg.fps), "fps", 1.0)
    cfg.codec = str(sec.get("codec", cfg.codec)).strip() or cfg.codec
    tmp = sec.get("tmp_dir")
    cfg.tmp_dir = str(tmp).strip() if isinstance(tmp, str) and tmp.strip() else None
    return cfg


def make_capture_factory(e8: E8Config):
    """返回一个每次调用都创建新 PicameraFrameSource 的工厂（方案 A：每录新建，录毕 release）。"""
    from src.evidence.picamera_capture import PicameraFrameSource

    def _factory():
        return PicameraFrameSource(width=e8.width, height=e8.height, fps=e8.fps)

    return _factory


def build_camera_recorder(e8: E8Config):
    """构造真实 CameraRecorder（capture_factory 每录新建帧源）。"""
    from src.evidence.camera_recorder import CameraRecorder

    return CameraRecorder(capture_factory=make_capture_factory(e8), codec=e8.codec, fps=e8.fps)


def build_uploader(storage_config: str = "config/storage.yaml"):
    """构造 OSS/存储 uploader（遵循 config/storage.yaml provider）。"""
    from src.storage import create_storage

    return create_storage(storage_config)


def _e8_lock_path(db_path: str) -> str:
    """按规范化数据库绝对路径确定锁文件路径（不删除文件）。"""
    return os.path.realpath(db_path) + ".e7e8.lock"


def _acquire_e8_singleton_lock(db_path: str) -> Optional[int]:
    """E8 单实例锁（POSIX flock，沿用 scripts/c4_worker 模式，不修改 c4_worker）。

    返回保持打开的 raw int fd（进程生命周期持有）；Windows 开发环境无 POSIX flock，
    明确 WARNING 后返回 None（未验证；真实 Pi(POSIX) 必须启用）。
    POSIX 上无法获取（另一实例存续）时抛 RuntimeError，由调用方拒绝启动。
    绝不 unlink 锁文件；不使用 O_TRUNC；此锁不构成上传成功的证明。
    """
    lock_path = _e8_lock_path(db_path)
    plat = sys.platform
    if plat.startswith("win"):
        _LOGGER.warning("POSIX flock 不可用（开发环境），跳过 E8 单实例锁；真实 Pi(POSIX) 必须启用")
        return None
    if not (plat.startswith("linux") or plat in ("darwin", "freebsd", "openbsd", "netbsd", "solaris")):
        raise RuntimeError(f"不支持的平台 {plat!r}，无法保证 E8 单实例保护")
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError(f"fcntl 不可用（POSIX 平台必需）: {exc}") from exc
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)  # 不 O_TRUNC、不 unlink
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise RuntimeError(f"获取 E8 单实例锁失败(errno={exc.errno}): {exc}") from exc


def _close_lock_fd(lock_fd: Optional[int]) -> None:
    """只在确认全部线程干净退出后调用；异常/停机超时路径不调用（由进程退出自动释放）。"""
    if lock_fd is None:
        return
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _probe_picamera(e8: E8Config) -> Optional[str]:
    """启动探针：尝试建一次帧源并释放；失败返回错误原因（摄像头被占/初始化失败），成功 None。"""
    try:
        src = make_capture_factory(e8)()
    except Exception as exc:  # noqa: BLE001
        return f"picamera 初始化失败: {type(exc).__name__}: {exc}"
    try:
        src.release()
    except Exception:  # noqa: BLE001
        pass
    return None


def run_e7_only(cfg, reader, db, stop: threading.Event, sleep=time.sleep) -> None:
    """e8 禁用/摄像头不可用时回退：仅 E7 采样（不创建录器/OSS，不伪造录像）。"""
    loop = SampledSensorLoop(cfg, reader, HighTempAlarm(cfg, db), db, sleep=sleep)
    loop.restore_state()
    try:
        while not stop.is_set():
            loop.step()
            sleep(cfg.sample_period_seconds)
    finally:
        loop.close()


def build_shared_camera_recorder(service, e8: E8Config):
    """共享模式（Mode B）：用已启动的 CameraService 构建 CameraRecorder。

    每次录像新建一个 E8CameraAdapter（订阅该 service 的独立 FrameReader），
    录毕由 CameraRecorder release（只解订阅，不关摄像头服务）。
    """
    from src.e8_camera_adapter import E8CameraAdapter
    from src.evidence.camera_recorder import CameraRecorder

    return CameraRecorder(
        capture_factory=lambda: E8CameraAdapter(service),
        codec=e8.codec, fps=e8.fps,
    )


def run_e7e8(cfg, reader, db, e8: E8Config, stop: threading.Event,
             sleep=time.sleep, uploader=None, camera_service=None,
             lock_fd: Optional[int] = None,
             shutdown_timeout: Optional[float] = None) -> int:
    """E7(+可选 E8) 运行直至 stop；严格顺序停机后返回退出码。

    返回 0=采样/消费者均干净退出；1=停机未排空（线程可能仍在，DB 行可保持 processing，
    由下次持锁启动恢复补传）。
    """
    from src.evidence.recorder import EvidenceRecorder

    if camera_service is not None:
        # Mode B：共享 CameraService，不再自建第二个 Picamera2
        cam = build_shared_camera_recorder(camera_service, e8)
    else:
        # Mode A：独立模式（每次录像新建 PicameraFrameSource，兼容既有）
        cam = build_camera_recorder(e8)
    recorder = EvidenceRecorder(
        db, recorder=cam, uploader=uploader,
        record_seconds=e8.record_seconds, tmp_dir=e8.tmp_dir,
    )
    # 诊断：记录真正传入 EvidenceRecorder 的 uploader 类型（非 argparse 参数）
    _LOGGER.info("E8 EvidenceRecorder uploader=%s",
                 type(uploader).__name__ if uploader is not None else None)
    # 持单实例锁（且已接 OSS）才补传遗留 processing 录像；不重录。获取锁失败绝不执行此恢复。
    if lock_fd is not None and uploader is not None:
        try:
            recorder.recover(allow_processing=True)
        except Exception as exc:  # noqa: BLE001 - 恢复失败不阻断启动采样
            _LOGGER.warning("processing 启动恢复异常（继续启动采样）: %s", exc)
    loop = SampledSensorLoop(cfg, reader, HighTempAlarm(cfg, db), db, sleep=sleep)
    loop.restore_state()
    runner = SensorRunner(loop, recorder, sleep=sleep)
    th = threading.Thread(target=runner.run, kwargs={"steps_per_cycle": 1}, daemon=True, name="e7-e8-run")
    th.start()
    try:
        while not stop.is_set():
            sleep(0.5)
    finally:
        # 严格顺序停机：stop() 先停生产者(采样)再排空消费者；OSS 上传无已证明上界，
        # 故此处只设**安全上限**(关闭等待预算)，不声称该时间保证上传完成；超时不强制终止，
        # 交给进程退出兜底 + 下次持锁启动恢复。
        budget = float(shutdown_timeout if shutdown_timeout is not None
                       else (e8.record_seconds + 30.0))
        clean = False
        try:
            clean = runner.close(timeout=budget)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("停机 close 异常: %s", exc)
        if not clean:
            _LOGGER.error(
                "停机未干净排空：采样/消费线程未随 close 退出，media_evidence 可保持 processing，"
                "请由下次持锁启动恢复补传；退出码非 0（不强制终止在途上传）")
        th.join(timeout=budget)
        return 0 if clean else 1


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="V4.4 E7+E8 生产装配入口")
    ap.add_argument("--config", default="config/edge.yaml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--storage", default="config/storage.yaml")
    ap.add_argument("--uploader", default="oss", choices=["oss", "none"])
    a = ap.parse_args(argv)

    try:
        e8 = load_e8_config(a.config)
        cfg = load_sensor_config(a.config)
    except ValueError as exc:
        _LOGGER.error("配置错误: %s", exc)
        return 2

    db_path = a.db or os.environ.get("DRRM_DB", "data/drrm.db")
    try:
        lock_fd = _acquire_e8_singleton_lock(db_path)
    except RuntimeError as exc:
        _LOGGER.error("E8 单实例锁获取失败，不启动: %s", exc)
        return 3
    db = Database(db_path=db_path)
    reader = build_sensor_reader(cfg)

    stop = threading.Event()

    def _h(_s, _f):
        _LOGGER.info("收到信号 %s，优雅退出", _s)
        stop.set()

    signal.signal(signal.SIGINT, _h)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _h)

    if not e8.enabled:
        _LOGGER.info("e8.enabled=false：仅运行 E7 采样（不创建录像器）")
        run_e7_only(cfg, reader, db, stop)
        _close_lock_fd(lock_fd)  # E7-only 无消费者线程，可立即释放
        return 0

    probe_err = _probe_picamera(e8)
    if probe_err:
        _LOGGER.error("摄像头不可用（%s）→ 回退 E7-only，不伪造录像；告警/C4 照常", probe_err)
        run_e7_only(cfg, reader, db, stop)
        _close_lock_fd(lock_fd)
        return 1

    uploader = build_uploader(a.storage) if a.uploader != "none" else None
    _LOGGER.info("E8 启动：record=%ss %dx%d@%s codec=%s uploader=%s",
                 e8.record_seconds, e8.width, e8.height, e8.fps, e8.codec,
                 type(uploader).__name__ if uploader is not None else None)
    exit_code = run_e7e8(cfg, reader, db, e8, stop, uploader=uploader, lock_fd=lock_fd)
    # 锁释放：仅当 run_e7e8 确认全部线程干净退出才 close fd；否则留给进程退出自动释放
    # （停机超时不得提前释放仍可能存续的消费者所持锁）。
    if exit_code == 0:
        _close_lock_fd(lock_fd)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())