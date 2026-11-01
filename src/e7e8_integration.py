"""V4.4 B 第二批：E7/E8 与 EdgeClient 感知共享 CameraService 的组合根。

在 EdgeClient 进程内，把已存在的 E7 采样 + E8 取证接到**同一** CameraService：

- 全部复用既有模块：`SampledSensorLoop`/`HighTempAlarm`/`build_sensor_reader`（E7）、
  `SensorRunner`（采样线程 + 独立消费线程）、`E8CameraAdapter` + `CameraRecorder`（E8 共享订阅）、
  `EvidenceRecorder`（证据登记/上传/重试）。
- 本模块只做「进程内装配 + 生命周期」，不重写任何已有能力。
- e8 disabled 时仅跑 E7 采样告警（不建 E8 订阅、不录像、不触摄像头）。
- 生命周期幂等：`close()` 先停 E7 采样并释放 E8 订阅（经 `SensorRunner`），
  再由调用方（EdgeClient）统一 `CameraService.stop/close`。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from src.sensor.loop import SampledSensorLoop
from src.sensor.alarm import HighTempAlarm
from src.sensor.runner import SensorRunner, build_sensor_reader

_LOGGER = logging.getLogger("e7e8.integration")


def build_shared_recorder(camera_service: Any, e8: Any) -> Any:
    """用 E8CameraAdapter 作 CameraRecorder capture_factory（与 e7e8_runner Mode B 相同）。

    - `E8CameraAdapter(camera_service)` 只在每次录像时新建，订阅同一 CameraService；
    - 录毕 release 只解 E8 自身订阅，不关 CameraService。
    """
    from src.e8_camera_adapter import E8CameraAdapter
    from src.evidence.camera_recorder import CameraRecorder
    return CameraRecorder(
        capture_factory=lambda: E8CameraAdapter(camera_service),
        codec=e8.codec, fps=e8.fps,
    )


def build_evidence(db, camera_service: Any, e8: Any, uploader: Any = None,
                   recorder: Any = None) -> Any:
    """组装 E8 证据链（复用 EvidenceRecorder，不重写上传/重试逻辑）。

    recorder 缺省为共享 CameraService 的 E8CameraAdapter 录像器；测试可注入 Fake/慢 recorder。
    """
    from src.evidence.recorder import EvidenceRecorder
    return EvidenceRecorder(
        db, recorder=recorder if recorder is not None else build_shared_recorder(camera_service, e8),
        uploader=uploader, record_seconds=e8.record_seconds, tmp_dir=e8.tmp_dir,
    )


class EdgeE7E8:
    """E7(+可选 E8) 组合。E7 采样告警在采样线程；E8 录像/上传在 SensorRunner 消费线程。"""

    def __init__(
        self,
        sensor_cfg: Any,
        e8_cfg: Any,
        db: Any,
        *,
        camera_service: Any = None,
        uploader: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        reader: Any = None,
        recorder: Any = None,
        wait: Optional[Callable[[float], None]] = None,
    ):
        self._e8_cfg = e8_cfg
        self._sleep = sleep
        self._stop = threading.Event()
        # 可中断等待：E7-only 采样间隔等待用（默认 Event.wait，可被 stop 唤醒）；测试可注入
        self._wait = wait if wait is not None else self._stop.wait
        self._thread: Optional[threading.Thread] = None
        self._loop_closed = False   # E7-only reader 只关一次（幂等）

        # E7 采样循环（二级告警经 SensorRunner 入队触发 E8 录像）
        loop = SampledSensorLoop(
            sensor_cfg,
            reader if reader is not None else build_sensor_reader(sensor_cfg),
            HighTempAlarm(sensor_cfg, db), db, sleep=sleep,
        )
        loop.restore_state()
        self._loop = loop

        e8_on = bool(getattr(e8_cfg, "enabled", False))
        if e8_on:
            if camera_service is None:
                raise ValueError("E8 enabled 但未提供 camera_service：无法共享同一摄像头")
            self._runner = SensorRunner(
                loop, build_evidence(db, camera_service, e8_cfg, uploader, recorder), sleep=sleep,
            )
            self._mode = "e7+e8"
        else:
            self._runner = None
            self._mode = "e7"

    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        """返回 'e7'（仅采样）或 'e7+e8'（采样+取证共享摄像头）。"""
        return self._mode

    @property
    def runner(self) -> Optional[SensorRunner]:
        return self._runner

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        if self._runner is not None:
            self._thread = threading.Thread(
                target=self._runner.run, kwargs={"steps_per_cycle": 1},
                daemon=True, name="e7e8-sensor-runner")
        else:
            self._thread = threading.Thread(target=self._run_e7_only, daemon=True,
                                            name="e7-sampler")
        self._thread.start()

    def _run_e7_only(self) -> None:
        while not self._stop.is_set():
            try:
                self._loop.step()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("E7 采样异常（继续下一拍）")
            # 可中断等待：默认 Event.wait(采样周期)，被 stop 唤醒后立即退出
            self._wait(self._loop.cfg.sample_period_seconds)

    def close(self, timeout: float = 5.0) -> bool:
        """关闭 E7/E8，返回是否**完全关闭**。未完成时保留线程/硬件引用、不关 reader、可重试。幂等。

        - E7+E8：传播 SensorRunner.close() 的布尔结果；未完成(False)时不提前关 reader，返回 False。
        - E7-only：先等待采样线程退出，**确认退出后才关 reader**；超时则不关 reader、保留引用、返回 False。
        - 返回 True 仅当所有工作线程已退出且依赖资源已安全关闭。
        """
        self._stop.set()
        th = self._thread

        if self._runner is not None:
            # ---- E7+E8：复用 SensorRunner 的布尔语义 ----
            ok = False
            try:
                ok = self._runner.close(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("runner.close 异常（按未完全关闭处理）: %s", exc)
                return False
            if not ok:
                if th is not None:
                    th.join(timeout=timeout)   # 尽力多等采样线程退出
                if th is not None and th.is_alive():
                    _LOGGER.warning("E7+E8 关闭未完成：采样线程仍存活；保留引用，稍后可重试 close")
                else:
                    _LOGGER.warning("E7+E8 runner.close 返回 False（消费/采样未干净退出）；保留引用可重试")
                # 不置 self._thread=None：保留引用，供后续 close() 重试
                return False
            self._thread = None
            return True

        # ---- E7-only：先停线程，确认退出后再关 reader ----
        if th is None:
            # 从未启动（或已关闭）：无线程竞争，直接安全关 reader（幂等）
            try:
                if not self._loop_closed:
                    self._loop.close()
                    self._loop_closed = True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("loop close 异常: %s", exc)
            return True
        th.join(timeout=timeout)
        if th.is_alive():
            _LOGGER.warning("E7-only 采样线程未退出；不关闭 reader、保留引用，可重试 close")
            return False
        try:
            if not self._loop_closed:
                self._loop.close()
                self._loop_closed = True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("loop close 异常: %s", exc)
            return False
        self._thread = None
        return True