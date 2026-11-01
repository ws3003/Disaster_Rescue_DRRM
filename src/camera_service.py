"""V4.4 B 线 S1 —— 统一摄像头采集服务核心。

职责（本项目唯一 Picamera2 持有者）：
- 创建/配置/启动一个 Picamera2（RGB888），并启动唯一采集线程。
- 持续 capture_array → 生成独立副本，构造不可变 Packet(session_id, seq, monotonic_timestamp, frame)。
- 为多个订阅者(FrameReader)分发帧（每订阅者有界 FIFO，队列满丢**新帧** drop_newest）。
- 维护 latest 最新帧；管理订阅集合；维护 fault 状态；统一关闭。

服务只产出原始 RGB 帧；BGR 转换/180° 旋转由各消费者(S2 EdgeClient / S3 E8)自行完成，本模块不做。

线程与资源（关闭并发安全模型——部署前定向审计第三/四轮定稿）：
- **构造不接触硬件**；`start()` 才创建 Picamera2 + 采集线程。
- **硬件唯一所有权**：`self._picam2` 是唯一所有权标记（非 None=由一个持有者占有；None=已释放）。
  - `start()` 初始化期间由 `start()` 负责硬件清理；采集线程**成功启动后**释放责任移交采集线程。
  - 唯一 `cam.close()` 调用点 = 采集线程 `_run_loop()` 的 `finally`（持 `_hw_lock` stop+close 后置 None）。
  - `close()` **只**发起关闭请求 + 有界等待 + 判定，**不直接释放 Picamera2**。
- 所有对 Picamera2 的硬件管理操作（open/stop/close，即 helper.stop 与 finally.stop+close）经 `_hw_lock` 串行。
  **注意**：`capture_array()` 不持 `_hw_lock`，stop-helper 的 `cam.stop()` 与 `capture_array()` 仍可能跨线程交叠——
  这是 Picamera2 API 生命周期行为，在 Raspberry Pi 上需真机验证，`_hw_lock` 只串行"两个硬件管理操作"。
- stop-helper：独立线程隔离可能阻塞的 `cam.stop()`；不在已释放的 Picamera2 上再次 stop；同实例至多一个。
- `close()` 有界：置停止请求 →（等 `_start_done`）→ 必要时启动 stop-helper → 有界 join 采集线程 → 判定。
  采集线程仍存活/硬件未释放时不置 CLOSED，保持 STOPPING 并记录挂起，留待采集线程 finally 回收。

状态机：NEW → RUNNING/FAULT →（stop/close）→ STOPPING → 采集线程退出并释放后 → CLOSED。
- 阻塞关闭保持 STOPPING、保留硬件引用、不释放仍被使用的硬件、记录挂起、不伪装 CLOSED。
- 真实采集异常（非主动关闭）保留 FAULT。
- `_session_id`：每次成功 start() 递增，区分采集会话；帧 seq 对象级单调递增。
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
_DEFAULT_JOIN_TIMEOUT = 2.0  # close()/stop() 有界等待采集线程退出上限


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

        # 状态/订阅/data 锁：_
        #   - _lock: 状态、订阅列表、latest/seq、fault、所有权标记(session 相关)。
        #   - _hw_lock: Picamera2 硬件管理(open/stop/close) 串行化。两锁不得嵌套(避免死锁)。
        self._lock = threading.Lock()
        self._hw_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._picam2: Any = None
        self._session_id = 0

        # 关闭并发协调:
        self._shutting_down = False
        self._capture_exited = threading.Event()   # 采集线程 finally 完成(硬件已释放)后置位
        self._start_done = threading.Event()       # start() 结束(成功移交或失败清理)后置位
        self._stop_helper_started = threading.Event()  # 同实例至多一个 stop-helper

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
        """置 FAULT 并通知订阅（调用方须持 _lock，或在本方法内锁）。"""
        with self._lock:
            self._fault_reason = reason
            if self._state not in (_State.CLOSED,):
                self._state = _State.FAULT
            self._notify_subs()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """创建并启动唯一 CameraService + 采集线程。重复调用不创建第二个实例。

        所有权移交：
        - 初始化（Picamera2 open）期间：清理责任归本 start()（失败路径持 _hw_lock 释放）。
        - 采集线程成功启动后：硬件释放责任移交采集线程（其 finally 为唯一 close 点）。
        - 若 close() 已请求（stop_event 已置位）：不进入常驻 RUNNING，直接以 STOPPING 移交，
          采集线程启动后立即走关闭路径。
        """
        with self._lock:
            if self._state in (_State.RUNNING, _State.FAULT):
                return  # 已有会话（RUNNING）或已失效（FAULT，可 stop+close 后重建对象）
            if self._state == _State.CLOSED:
                raise CameraServiceError("服务已 CLOSED，无法重启；请创建新 CameraService 实例")

        # --- 初始化（start 拥有硬件清理责任）---
        try:
            with self._hw_lock:
                self._open_hardware()
        except Exception as exc:  # noqa: BLE001
            # create/configure/start(stream) 任一失败：start 负责清理
            with self._hw_lock:
                self._release_hardware_owned()
            with self._lock:
                self._state = _State.FAULT
                self._fault_reason = f"初始化失败: {type(exc).__name__}: {exc}"
                self._notify_subs()
            self._start_done.set()
            _LOGGER.error("Picamera2 初始化失败: %s", self._fault_reason)
            return

        # --- 移交采集线程 ---
        if self._stop_event.is_set():
            # close() 已请求：不进入常驻 RUNNING，启动后立即关闭
            with self._lock:
                self._state = _State.STOPPING
                th = threading.Thread(target=self._run_loop, daemon=True,
                                      name="camera-service-acquire")
                self._thread = th
        else:
            with self._lock:
                self._state = _State.RUNNING
                self._session_id += 1
                for sub in self._subs:
                    sub.session_id = self._session_id
                self._stop_event.clear()
                th = threading.Thread(target=self._run_loop, daemon=True,
                                      name="camera-service-acquire")
                self._thread = th
        try:
            th.start()
        except Exception as exc:  # noqa: BLE001
            # 采集线程启动失败：未移交，硬件清理归 start
            with self._hw_lock:
                self._release_hardware_owned()
            with self._lock:
                if self._state not in (_State.CLOSED,):
                    self._state = _State.FAULT
                self._fault_reason = f"采集线程启动失败: {type(exc).__name__}: {exc}"
                self._notify_subs()
            self._start_done.set()
            _LOGGER.error("采集线程启动失败: %s", self._fault_reason)
            return
        # 移交完成：此后硬件由采集线程 finally 负责释放
        self._start_done.set()

    def _open_hardware(self) -> None:
        import picamera2 as _p  # 惰性：仅 start() 才需真机（调用方持 _hw_lock）

        cam = _p.Picamera2()
        try:
            cfg = cam.create_video_configuration(
                main={"size": (self._width, self._height), "format": _CAPTURE_FORMAT},
                controls={"FrameRate": self._fps},
            )
            cam.configure(cfg)
            cam.start()
        except BaseException:
            # 局部失败立即释放本对象，避免残留半初始化硬件
            try:
                cam.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._picam2 = cam

    def _release_hardware_owned(self) -> None:
        """释放 Picamera2 并置所有权 None。**必须在持 _hw_lock 时调用**。幂等。

        stop 与 close 在此串行完成；采集线程 finally 与 start 失败路径均经此到达，
        由"线程/硬件所有权"保证二者互斥，不会重复释放。
        """
        cam, self._picam2 = self._picam2, None
        if cam is not None:
            # 先 stop(stream) 再 close 释放资源
            for method in ("stop", "close"):
                fn = getattr(cam, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001 —— 尽力释放，不吞主异常
                        _LOGGER.debug("释放 Picamera2 时 %s 失败", method)

    def _run_loop(self) -> None:
        """采集线程主体：持续 capture_array 并广播；退出时是唯一 cam.close() 点。"""
        try:
            while not self._stop_event.is_set():
                try:
                    raw = self._picam2.capture_array()
                except Exception as exc:  # noqa: BLE001
                    self._set_fault(f"采集异常: {type(exc).__name__}: {exc}")
                    _LOGGER.error("采集线程异常→服务失效（不自动重建第二个实例）: %s",
                                  self._fault_reason)
                    break
                self._publish(raw)
        finally:
            # ---- 唯一硬件 close 点（已移交所有权）----
            with self._hw_lock:
                self._release_hardware_owned()
            with self._lock:
                # 主动关闭(stop_event 已置位) → CLOSED；未请求关闭的真实异常 → FAULT
                if self._stop_event.is_set():
                    self._state = _State.CLOSED
                else:
                    self._state = _State.FAULT
                self._notify_subs()
            self._capture_exited.set()

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
        """服务失效/关闭：唤醒并让阻塞 read 返回失败。**调用方须持 _lock**。"""
        for rec in self._subs:
            rec.closed = True

    def _drain(self) -> None:
        with self._lock:
            for rec in self._subs:
                try:
                    while True:
                        rec.q.get_nowait()
                except queue.Empty:
                    pass
                except Exception:  # noqa: BLE001
                    break

    # ------------------------------------------------------------------
    # 硬件停止辅助：隔离可能阻塞的 cam.stop() 以便 close() 有界返回
    # ------------------------------------------------------------------
    def _maybe_start_stop_helper(self) -> None:
        """若采集线程仍存活且硬件仍被持有，启动一个（至多一个）stop-helper 线程尝试唤醒。

        stop-helper 只调 cam.stop()（持 _hw_lock），**不执行 cam.close()**；
        获锁后二次校验硬件有效性(自 _picam2 非 None)与采集线程存活，防止对已释放对象再 stop。
        """
        with self._lock:
            if self._stop_helper_started.is_set():
                return
            if self._picam2 is None:
                return
            if self._thread is None or not self._thread.is_alive():
                return
            self._stop_helper_started.set()

        def _helper() -> None:
            with self._hw_lock:
                # 二次校验：等待锁期间采集线程 possibly 已完成 finally 释放
                if self._picam2 is None:
                    return
                if self._thread is None or not self._thread.is_alive():
                    return
                cam = self._picam2
                try:
                    cam.stop()   # 尝试唤醒阻塞中的 capture_array（可能阻塞，隔离在 helper 线程）
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("stop-helper 唤醒失败（忽略，采集线程最终自行退出）: %s", exc)

        threading.Thread(target=_helper, daemon=True, name="camera-stop-helper").start()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """停止采集（置停止请求 + 有界等待采集线程退出），**不释放硬件**。幂等。"""
        with self._lock:
            if self._state in (_State.CLOSED, _State.NEW):
                return
            self._stop_event.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=_DEFAULT_JOIN_TIMEOUT)

    def close(self, timeout: float = _DEFAULT_JOIN_TIMEOUT) -> None:
        """统一关闭：发起停止请求 + 有界等待 + 判定，**不直接释放 Picamera2**。幂等。

        - 采集线程已成功启动：硬件由其 finally（唯一 close 点）释放；close 在采集线程
          ``_capture_exited`` 后判定 CLOSED。
        - 采集线程仍存活（阻塞）：不释放硬件、不置 CLOSED、保留引用并记录挂起。
        - start() 尚未落定：有界等待 `_start_done`，避免对未初始化/半初始化硬件做状态预判。

        再次调用 close 幂等：仅继续附加等待/判定，不重复启动 stop-helper 或 release。
        """
        # 1) 幂等 + 并发 close 协调：仅首个请求者发起停止请求/通知
        with self._lock:
            if self._state == _State.CLOSED:
                return
            if not self._shutting_down:
                self._shutting_down = True
                self._stop_event.set()
                if self._state not in (_State.CLOSED,):
                    self._state = _State.STOPPING
                    self._notify_subs()

        # 2) 若 start() 尚未落定：有界等待其完成（成功后移交采集线程，或失败已释放）
        if not self._start_done.is_set():
            self._start_done.wait(timeout=timeout)

        # 3) 若采集线程仍存活且硬件被持有：启动 stop-helper 尝试唤醒阻塞 capture
        self._maybe_start_stop_helper()

        # 4) 有界等待采集线程退出（thread 不存在=start 失败，无需等）
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=timeout)

        # 5) 判定：仅当"采集线程已退出并释放硬件"或"start 失败/从未创建线程"才置 CLOSED
        with self._lock:
            finished = self._capture_exited.is_set() or (
                self._thread is None and self._start_done.is_set()
            )
            if finished and self._state != _State.CLOSED:
                self._state = _State.CLOSED
                self._notify_subs()
            elif not finished:
                _LOGGER.warning(
                    "摄像头关闭挂起: 采集线程未退出或硬件仍被持有，状态保持 %s，"
                    "硬件由采集线程退出时回收（不伪装 CLOSED）", self._state)


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