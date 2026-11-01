"""V4.4 B 线 S1 —— 统一摄像头采集服务核心。

职责（本项目唯一 Picamera2 持有者）：
- 创建/配置/启动一个 Picamera2（RGB888），并启动唯一采集线程。
- 持续 capture_array → 生成独立副本，构造不可变 Packet(session_id, seq, monotonic_timestamp, frame)。
- 为多个订阅者(FrameReader)分发帧（每订阅者有界 FIFO，队列满丢**新帧** drop_newest）。
- 维护 latest 最新帧；管理订阅集合；维护 fault 状态；统一关闭。

服务只产出原始 RGB 帧；BGR 转换/180°旋转由各消费者(S2 EdgeClient / S3 E8)自行完成，本模块不做。

线程与资源：
- 构造不接触硬件；start() 才创建 Picamera2 + 采集线程。
- 只有该服务能 close()/release Picamera2；订阅者 release() 只解除自身订阅。
- session_id：每次成功 start() 递增，区分采集会话；帧 seq 对象级单调递增。
- 状态机：NEW → RUNNING → STOPPING → CLOSED；失败 → FAULT（可安全 stop+close 重建）；CLOSED 不再 start（重建需新对象）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, NamedTuple, Optional, Tuple

_LOGGER = logging.getLogger("camera_service")

_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
_DEFAULT_FPS = 15.0
_DEFAULT_QUEUE_SIZE = 24  # S1 初始值，非真机最终值
_CAPTURE_FORMAT = "RGB888"
_QUEUE_WAIT = 0.05  # 阻塞读的轮询间隔（兼顾唤醒与低 CPU）


class _State:
    NEW = "NEW"
    RUNNING = "RUNNING"
    FAULT = "FAULT"
    STOPPING = "STOPPING"
    CLOSED = "CLOSED"


class Packet(NamedTuple):
    """一次采样到的帧（不可变视图持有独立副本）。"""

    session_id: int
    seq: int
    monotonic_timestamp: float
    frame: Any  # ndarray, RGB888 独立副本


class CameraServiceError(RuntimeError):
    """摄像头服务本地错误。"""


class _SubRecord:
    """单订阅者的有界队列与统计。"""

    __slots__ = ("q", "dropped_frames", "max_queue_len_seen", "closed", "session_id")

    def __init__(self, maxsize: int, session_id: int):
        self.q: "queue.Queue[Packet]" = queue.Queue(maxsize=maxsize)
        self.dropped_frames: int = 0
        self.max_queue_len_seen: int = 0
        self.closed: bool = False
        self.session_id: int = session_id

    @property
    def full(self) -> bool:
        return self.q.full()

    def size(self) -> int:
        return self.q.qsize()


class CameraService:
    """统一摄像头采集服务（唯一 Picamera2 持有者）。"""

    def __init__(
        self,
        width: int = _DEFAULT_WIDTH,
        height: int = _DEFAULT_HEIGHT,
        fps: float = _DEFAULT_FPS,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._queue_size = max(1, int(queue_size))

        self._state = _State.NEW
        self._fault_reason: Optional[str] = None

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._picam2: Any = None
        self._session_id = 0

        self._subs: List[_SubRecord] = []
        self._latest: Optional[Packet] = None
        self._seq = 0

    # ------------------------------------------------------------------
    # 状态/查询
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def session_id(self) -> int:
        return self._session_id

    def fault(self) -> Optional[str]:
        """采集/初始化失败原因；None 表示正常。订阅者据此安全降级。"""
        return self._fault_reason

    def _set_fault(self, reason: str) -> None:
        self._fault_reason = reason
        if self._state in (_State.RUNNING, _State.FAULT):
            self._state = _State.FAULT
        else:
            self._state = _State.FAULT
        self._notify_subs()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """创建并启动唯一 Picamera2 + 采集线程。重复调用不创建第二个实例。"""
        with self._lock:
            if self._state in (_State.RUNNING, _State.FAULT):
                return  # 已有会话（RUNNING）或已失效（FAULT，可 stop+close 后重建对象）
            if self._state == _State.CLOSED:
                raise CameraServiceError("服务已 CLOSED，无法重启；请创建新 CameraService 实例")
            try:
                self._open_hardware()
            except Exception as exc:  # noqa: BLE001
                self._release_hardware()
                self._state = _State.FAULT
                self._fault_reason = f"初始化失败: {type(exc).__name__}: {exc}"
                _LOGGER.error("Picamera2 初始化失败: %s", self._fault_reason)
                return
            self._state = _State.RUNNING
            self._session_id += 1
            for sub in self._subs:
                sub.session_id = self._session_id
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                            name="camera-service-acquire")
            self._thread.start()

    def _open_hardware(self) -> None:
        import picamera2 as _p  # 惰性：仅 start() 才需真机

        cam = _p.Picamera2()
        cfg = cam.create_video_configuration(
            main={"size": (self._width, self._height), "format": _CAPTURE_FORMAT},
            controls={"FrameRate": self._fps},
        )
        cam.configure(cfg)
        cam.start()
        self._picam2 = cam

    def _release_hardware(self) -> None:
        cam, self._picam2 = self._picam2, None
        if cam is not None:
            for method in ("stop", "close"):
                fn = getattr(cam, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001 —— 尽力释放，不吞主异常
                        _LOGGER.debug("释放 Picamera2 时 %s 失败", method)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                raw = self._picam2.capture_array()
            except Exception as exc:  # noqa: BLE001
                self._set_fault(f"采集异常: {type(exc).__name__}: {exc}")
                _LOGGER.error("采集线程异常→服务失效（不自动重建第二个实例）: %s", self._fault_reason)
                return
            self._publish(raw)

    def _publish(self, raw: Any) -> None:
        """把一帧广播给所有订阅者（每订阅独立副本）+ 更新 latest。"""
        sid = self._session_id
        self._seq += 1
        seq = self._seq
        ts = time.monotonic()
        with self._lock:
            for sub in self._subs:
                if sub.closed or sub.session_id != sid:
                    continue
                copy = raw.copy()
                pkt = Packet(session_id=sid, seq=seq, monotonic_timestamp=ts, frame=copy)
                # drop_newest：队列满丢弃新帧，不覆盖消费者未读旧帧
                try:
                    sub.q.put_nowait(pkt)
                except queue.Full:
                    sub.dropped_frames += 1
                sz = sub.size()
                if sz > sub.max_queue_len_seen:
                    sub.max_queue_len_seen = sz
            # latest 独立副本
            self._latest = Packet(session_id=sid, seq=seq, monotonic_timestamp=ts,
                                  frame=raw.copy())

    # ------------------------------------------------------------------
    # 消费者接口
    # ------------------------------------------------------------------
    def latest(self) -> Optional[Packet]:
        return self._latest

    def subscribe(self) -> "FrameReader":
        """注册一个新订阅者，返回其排队读句柄。"""
        with self._lock:
            if self._state == _State.CLOSED:
                raise CameraServiceError("服务已关闭，无法订阅")
            rec = _SubRecord(self._queue_size, self._session_id)
            self._subs.append(rec)
            return FrameReader(self, rec, self._session_id)

    def _unsubscribe(self, rec: _SubRecord) -> None:
        with self._lock:
            try:
                self._subs.remove(rec)
            except ValueError:
                pass
            rec.closed = True
            rec.q = queue.Queue(maxsize=0)  # 置空队列：释放遗留帧

    def _notify_subs(self) -> None:
        """服务失效/关闭：唤醒并让阻塞 read 返回失败。"""
        for rec in self._subs:
            rec.closed = True

    def _drain(self) -> None:
        for rec in self._subs:
            try:
                while True:
                    rec.q.get_nowait()
            except queue.Empty:
                pass
            except Exception:  # noqa: BLE001
                break

    def stop(self) -> None:
        """停止采集线程（不关闭 Picamera2）。幂等。"""
        with self._lock:
            if self._state in (_State.CLOSED, _State.NEW):
                return
            self._stop_event.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)

    def close(self) -> None:
        """统一关闭：停采集→释放 Picamera2→清理订阅→CLOSED。幂等。"""
        with self._lock:
            if self._state == _State.CLOSED:
                return
            self._stop_event.set()
            self._state = _State.STOPPING
            self._notify_subs()  # 1) 通知订阅失效
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)  # 2) 停线程
        with self._lock:
            self._release_hardware()  # 3) 释放 Picamera2
            self._drain()  # 4) 清理订阅队列
            self._latest = None
            self._state = _State.CLOSED


class FrameReader:
    """单订阅者的有界 FIFO 读句柄；release() 只解除订阅，绝不关闭摄像头。"""

    def __init__(self, service: CameraService, rec: _SubRecord, session_id: int) -> None:
        self._service = service
        self._rec = rec
        self._session_id = session_id

    # ------------------------------------------------------------------
    def read(self, timeout: Optional[float] = None) -> Tuple[bool, Any]:
        """返回 (ok, frame)。frame 为 ndarray(RGB888 独立副本)。"""
        ok, pkt = self.read_packet(timeout=timeout)
        return (True, pkt.frame) if ok and pkt is not None else (False, None)

    def read_packet(self, timeout: Optional[float] = None) -> Tuple[bool, Optional[Packet]]:
        """返回 (ok, packet)；超时/失效返回 (False, None)；绝不重复返回同一帧。"""
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            rec = self._rec
            if rec.closed:
                return False, None
            remaining = (deadline - time.monotonic()) if deadline is not None else _QUEUE_WAIT
            if remaining <= 0:
                return False, None
            try:
                pkt = rec.q.get(timeout=remaining if deadline is not None else _QUEUE_WAIT)
            except queue.Empty:
                continue  # 空等后重试；deadline 由顶部 remaining 检查兜底
            if pkt.session_id != self._session_id:
                continue  # 旧会话残留帧，丢弃不读
            return True, pkt

    def release(self) -> None:
        """解除自身订阅；不关闭 CameraService / Picamera2。幂等。"""
        self._service._unsubscribe(self._rec)

    # ------------------------------------------------------------------
    @property
    def dropped_frames(self) -> int:
        return self._rec.dropped_frames

    @property
    def max_queue_len_seen(self) -> int:
        return self._rec.max_queue_len_seen

    @property
    def session_id(self) -> int:
        return self._session_id