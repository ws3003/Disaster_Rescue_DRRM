"""V4.4 E7→E8 本地联调 Runner（方案 A，最小接线）。

职责：
- 组装现有 SampledSensorLoop（采样）+ HighTempAlarm（告警）+ Database + EvidenceRecorder（取证）。
- 采样线程只做 `loop.step()` 并把**二级事件**投入有界队列；录像/上传由单一消费者线程处理，
  保证录像/上传不阻塞采样循环。
- 事件投递直接复用 `EvidenceRecorder.decide_from_event()` 判定（不复制告警规则），
  同 `alert_id` 的去重由 `handle_event()` 以 `media_evidence.ref_id` 幂等保证。
- 消费者线程：启动时先 `recover()` 一次，之后按 `recover_interval_seconds` 间隔调度（不在每个采样步骤触发）。
- 队列满：`put_nowait` 丢弃并记录诊断（含 alert_id/事件类型/原因），不假报已接受/已录像。
- 关闭：先停采样 → 消费者继续排空已接受事件 → join（带超时）→ `loop.close()`。避免无限等待。
- 依赖（Reader / Recorder适配器 / Uploader / Clock / Sleep）全部经构造或 `build_runner` 注入，
  测试使用 Fake，不触真实硬件/OSS/正式库。

已知限制（方案 A，不夸大）：单实例/单消费者；`processing` 崩溃遗留、多进程并发认领、
持久化退避、远端对象验证 仍需 Schema/DAO/真实 OSS-Pi 后续授权，本模块不声称已解决。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

from src.database.db import Database
from src.evidence.recorder import EvidenceRecorder, FakeRecorder
from src.sensor.alarm import HighTempAlarm
from src.sensor.config import SensorConfig, load_sensor_config
from src.sensor.loop import SampledSensorLoop
from src.sensor.sensor_errors import SensorReadError

_QUEUE_GET_TIMEOUT = 0.2


class SensorRunner:
    """单消费者采样+取证 Runner（依赖注入，可测）。"""

    def __init__(
        self,
        loop: SampledSensorLoop,
        recorder: EvidenceRecorder,
        *,
        queue_size: int = 1000,
        recover_interval_seconds: float = 60.0,
        max_recover_attempts: int = 10,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.loop = loop
        self.recorder = recorder
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._recover_interval = float(recover_interval_seconds)
        self._max_recover = int(max_recover_attempts)
        self._sleep = sleep
        self._stop = threading.Event()
        self._life_lock = threading.Lock()   # 生命周期状态/资源关闭保护
        self._lifecycle = "new"              # new / running / stopping / closed
        self._thread: Optional[threading.Thread] = None
        self._last_recover: Optional[float] = None
        self._dropped: list[dict] = []      # 队列满/消费者异常 诊断（有界）
        # 采样循环生命周期（采样线程由调用方经 run() 驱动；Runner 不自建采样线程）
        self._sampling_active = False
        self._sampling_done = threading.Event()
        self._sampling_done.set()
        self._reader_closed = False         # reader 只关闭一次（幂等）

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------
    def enqueue(self, event: Any) -> str:
        """仅把二级取证事件投入队列；一级/持续/恢复/None 忽略。返回 skip/enqueued/dropped。"""
        if not self.recorder.decide_from_event(event):
            return "skip"
        try:
            self._queue.put_nowait(event)
            return "enqueued"
        except queue.Full:
            self._note_drop(event, "queue_full")
            return "dropped"

    def _note_drop(self, event: Any, reason: str) -> None:
        diag = {
            "ts": time.time(),
            "alert_id": (event or {}).get("alert_id") if isinstance(event, dict) else None,
            "event_type": (event or {}).get("type") if isinstance(event, dict) else None,
            "reason": reason,
        }
        self._dropped.append(diag)
        if len(self._dropped) > 1000:
            self._dropped = self._dropped[-1000:]

    @property
    def dropped(self) -> list[dict]:
        return list(self._dropped)

    # ------------------------------------------------------------------
    # 采样（采样线程，非阻塞式投递）
    # ------------------------------------------------------------------
    def sample(self, steps: int = 1, now: Optional[Callable[[], str]] = None) -> list[dict]:
        results: list[dict] = []
        for _ in range(int(steps)):
            res = self.loop.step(now=now)
            results.append(res)
            if isinstance(res, dict) and res.get("event"):
                self.enqueue(res["event"])
        return results

    # ------------------------------------------------------------------
    # 恢复调度
    # ------------------------------------------------------------------
    def _recover_once(self) -> None:
        try:
            self.recorder.recover(max_tasks=self._max_recover)
        except Exception as exc:  # noqa: BLE001
            self._note_drop({"type": "recover"}, f"recover_exception:{exc}")
        self._last_recover = time.time()

    def _maybe_recover(self) -> None:
        if self._last_recover is None:
            self._recover_once()
            return
        if self._recover_interval > 0 and (time.time() - self._last_recover) >= self._recover_interval:
            self._recover_once()

    # ------------------------------------------------------------------
    # 消费者线程
    # ------------------------------------------------------------------
    def _consume(self) -> None:
        self._recover_once()                       # 启动时一次
        while True:
            try:
                ev = self._queue.get(timeout=_QUEUE_GET_TIMEOUT)
            except queue.Empty:
                if self._stop.is_set() and self._queue.empty():
                    break
                self._maybe_recover()
                continue
            try:
                self.recorder.handle_event(ev)
            except Exception as exc:               # noqa: BLE001——消费者异常不终止后续任务
                self._note_drop(ev, f"consumer_exception:{exc}")
            finally:
                self._queue.task_done()
            self._maybe_recover()

    def start(self) -> None:
        """启动单一消费者线程（首次/运行态可启动；已停止或已关闭则拒绝重启）。

        生命周期：``new → running → stopping → closed``。
        - 已 stopping/closed：直接返回（**不** clear 停止标志、不重启采样/消费者线程）。
        - 首次/运行态：才 clear 停止标志并启动消费者线程（保证正常首次启动仍可用）。
        """
        with self._life_lock:
            if self._lifecycle in ("stopping", "closed"):
                return                       # 已请求停止/已关闭：不得重启
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("runner consumer already started")
            self._stop.clear()               # 仅允许启动时才清除停止标志
            self._lifecycle = "running"
            th = threading.Thread(target=self._consume, daemon=True, name="sensor-runner-e8")
            self._thread = th
        th.start()

    # ------------------------------------------------------------------
    # 生产采样主循环 / 关闭
    # ------------------------------------------------------------------
    def run(self, steps_per_cycle: int = 1, now: Optional[Callable[[], str]] = None) -> None:
        """生产入口：持续采样直至 stop()。由调用方在独立线程调用（或主线程配合 stop 信号）。

        采样循环在**调用方线程**运行；退出（stop 信号）后置 `_sampling_active=False` 并 set
        `_sampling_done`，供 close() 确认采样线程已退出后再安全关闭 reader。
        """
        self.start()
        self._sampling_done.clear()
        with self._life_lock:
            # 仅当生命周期允许且未请求停止才真正采样；否则本线程只确认退出（不再触碰 reader）
            self._sampling_active = (self._lifecycle == "running" and not self._stop.is_set())
        try:
            while self._sampling_active and not self._stop.is_set():
                self.sample(steps_per_cycle, now=now)
                self._sleep(self.loop.cfg.sample_period_seconds)
        finally:
            self._sampling_done.set()
            self._sampling_active = False

    def stop(self, timeout: float = 5.0) -> bool:
        """停止新采样；等待消费者排空已接受事件并退出（有界）。返回消费者是否已干净退出。

        注意：本方法只等待**消费者线程**；`run()` 采样循环（调用方线程）由 `close()` 另行确认。
        """
        with self._life_lock:
            self._stop.set()
            if self._lifecycle != "closed":
                self._lifecycle = "stopping"
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=float(timeout))
            if self._thread.is_alive():
                self._note_drop({"type": "close"}, "close_timeout")
                return False
        return True

    def close(self, timeout: float = 5.0) -> bool:
        """停止 + 关闭 Reader。**仅当采样线程与消费者线程均已确认退出**才关 reader。

        生命周期：``new/running → stopping → closed``；closed 后不可再 start（start 会被拒）。
        返回 True=完全关闭；False=超时或仍有线程未退出（**不关 reader**、保留引用、可重试）。
        Reader 只关闭一次（幂等）；重复 close 安全。
        """
        # 幂等：已 closed → 直接返回成功
        with self._life_lock:
            if self._lifecycle == "closed":
                return True
        ok = self.stop(timeout=timeout)   # 置 stopping + set stop + 有界等待消费者线程
        # 采样线程：正常进入 run() 后无论 start 被拦还是采样后退出，都必须在 run() finally 置
        # sampling_done；从未启动 run() 时 sampling_done 为预设 set。有界等待其证实退出。
        sampled_done = self._sampling_done.wait(timeout=float(timeout))
        if not ok:
            self._note_drop({"type": "close"}, "consumer_still_running")
            return False
        if not sampled_done:
            self._note_drop({"type": "close"}, "sampling_still_running")
            return False
        # 采样与消费者均确认退出 → 安全关 reader（只关一次）
        if not self._reader_closed:
            try:
                self.loop.close()
            except Exception as exc:  # noqa: BLE001
                self._note_drop({"type": "close"}, f"reader_close_exception:{exc}")
                return False
            with self._life_lock:
                self._reader_closed = True
        with self._life_lock:
            self._lifecycle = "closed"
        return True


def build_sensor_reader(cfg: SensorConfig, *, provider: Any = None):
    """按配置装配传感器读取器（E7-01，A2）。

    - cfg.reader == 'ds18b20' → DS18B20Reader(device_dir, device_id)（真实 One-Wire 温度）。
    - 否则 → FakeReader(provider)（Windows 测试/离线联调）。provider 缺失时抛错，
      明确可诊断——不把 Fake 当作真实硬件已接入。

    构造过程不触碰 Linux 设备路径（DS18B20Reader 仅 read() 时访问），Windows 无副作用。
    """
    if cfg.reader == "ds18b20":
        from src.sensor.ds18b20_reader import DEFAULT_W1_DIR, DS18B20Reader
        device_dir = cfg.device_dir or str(DEFAULT_W1_DIR)
        return DS18B20Reader(device_dir=device_dir, device_id=cfg.device_id)
    if provider is None:
        raise SensorReadError(
            "reader=fake 需要显式 provider（测试/离线联调）；不要将 Fake 当作真实硬件已接入")
    from src.sensor.reader import FakeReader
    return FakeReader(provider)


def build_runner_from_config(
    sensor_config_path: Any,
    *,
    db: Database,
    reader_provider: Any = None,
    recorder_adapter: Any = None,
    uploader: Any = None,
    storage_config: str = "config/storage.yaml",
    sleep: Callable[[float], None] = time.sleep,
) -> "SensorRunner":
    """生产装配入口：从 sensor 配置 yaml 加载，按 reader 选择真实 DS18B20 或 Fake 读取器。

    适用 Raspberry Pi 生产（reader=ds18b20 + device_id）；Windows 离线联调传 reader_provider
    （Fake provider）即可。供启动脚本调用，不另建第二套 SensorRunner。
    """
    cfg = load_sensor_config(sensor_config_path)
    reader = build_sensor_reader(cfg, provider=reader_provider)
    return build_runner_with_storage(
        cfg, db=db, reader=reader, recorder_adapter=recorder_adapter,
        uploader=uploader, storage_config=storage_config, sleep=sleep,
    )


def build_runner(
    sensor_cfg: SensorConfig,
    *,
    db: Database,
    reader: Any,
    recorder_adapter: Any = None,
    uploader: Any = None,
    queue_size: int = 1000,
    recover_interval_seconds: float = 60.0,
    max_recover_attempts: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> SensorRunner:
    """组装现有 采样循环 + 告警状态机 + Recorder（依赖可注入），返回可用的 SensorRunner。

    recorder_adapter 缺省为 FakeRecorder（离线联调）；uploader 为 None 时 Recorder 跳过上传
    （保持 processing，不伪造成功）。生产接真实摄像头适配器 / create_storage() 的 provider。
    """
    alarm = HighTempAlarm(sensor_cfg, db)
    loop = SampledSensorLoop(sensor_cfg, reader, alarm, db, sleep=sleep)
    # E7-07 重启恢复：从 SQLite 恢复上次持久化的设备离线/故障状态
    loop.restore_state()
    recorder = EvidenceRecorder(
        db,
        recorder=recorder_adapter or FakeRecorder(),
        uploader=uploader,
    )
    return SensorRunner(
        loop, recorder,
        queue_size=queue_size,
        recover_interval_seconds=recover_interval_seconds,
        max_recover_attempts=max_recover_attempts,
        sleep=sleep,
    )


def build_runner_with_storage(
    sensor_cfg: SensorConfig,
    *,
    db: Database,
    reader: Any,
    recorder_adapter: Any = None,
    uploader: Any = None,
    storage_config: str = "config/storage.yaml",
    queue_size: int = 1000,
    recover_interval_seconds: float = 60.0,
    max_recover_attempts: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> SensorRunner:
    """生产构造入口：默认经 `create_storage(storage_config)` 注入 uploader。

    仅作依赖注入接线，不重写 storage。测试/离线可传 `uploader`（Fake/Local provider）以避开真实 OSS。
    注意：`create_storage` 按 config/storage.yaml 的 provider 选择实现；真实 OSS 上传需 env 凭据，
    未切真实 OSS 前不应预期真实上传。
    """
    final_uploader = uploader
    if final_uploader is None:
        from src.storage import create_storage
        final_uploader = create_storage(storage_config)
    return build_runner(
        sensor_cfg, db=db, reader=reader, recorder_adapter=recorder_adapter,
        uploader=final_uploader, queue_size=queue_size,
        recover_interval_seconds=recover_interval_seconds,
        max_recover_attempts=max_recover_attempts, sleep=sleep,
    )