"""
src/edge_client.py — Raspberry Pi Edge Client (Stage 7-B)
==========================================================
独立负责: 摄像头采集关键帧 → JPEG 编码 → HTTP multipart 上传到
Windows FastAPI Gateway → 接收 VLM + LLM 分析结果并打印。

设计要点:
  - 与 src/main.py 及感知算法链**解耦**, 不 import 任何感知模块;
  - 不打开 cv2 窗口 (支持 headless);
  - 模块级纯函数/类便于用 FakeCamera + FakeTransport 离线测试。
    (运行不需要真实摄像头与服务器; 测试中注入替身)

硬件依赖: Python 3.11, picamera2(系统包) / opencv-python / requests。
配置: config/edge.yaml; 命令行 --server 可覆盖 API 地址。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading
import traceback
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

import cv2
import requests
import yaml

# 保证可从 `cd src && python edge_client.py` 运行 (顶层 import camera/edge_perception)
# 同时把 repo 根加入 sys.path, 使 V4.0-B Agent 用 b 分支真实约定 `from src.agent.agent`
# 导入 (src/ 无 __init__.py, 为 namespace package, 不可用 `from agent import`)。
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

# 灾害事件语音播报(可选组件; 导入/运行异常绝不影响视觉主循环)
try:
    from src.voice.voice_agent import VoiceAgent
except Exception:  # noqa: BLE001 — 无语音组件也允许运行视觉检测
    VoiceAgent = None
# 按钮语音交互开关(增量接入, 不改视觉逻辑):
#   VOICE_ENABLED=0  关闭按钮语音(默认)
#   VOICE_ENABLED=1  开启按钮语音(后台线程跑 VoiceAgent.run)

_VOICE = None  # 惰性缓存的播报器(VoiceAgent 构造较重, 复用实例)
# 播报去重: text_hash -> 上次播报时间戳(epoch); 同一文本 60s 内只播一次
_announced_alert_cache: Dict[int, float] = {}
_ANNOUNCE_TTL = 60.0
_HIGH_STATES = ("fall", "help-waving", "static lying", "lying")
_ALERT_RISK_THRESHOLD = 0.8
# event_summary 兜底允许播报的关键词(仅在包含以下词时才播)
_EVENT_KEYWORDS = ("跌倒", "倒地", "受伤", "被困", "危险", "Fall", "Help", "Lying", "救援")


def _is_high_risk(args: Any, message: str) -> bool:
    """send_alert 是否高危: 优先看 args 的 priority/state/risk_score, 再兜底匹配 message 文本。"""
    if isinstance(args, dict):
        if str(args.get("priority") or "").upper() == "HIGH":
            return True
        if str(args.get("state") or "").lower() in _HIGH_STATES:
            return True
        rs = args.get("risk_score")
        if isinstance(rs, (int, float)) and not isinstance(rs, bool) and rs >= _ALERT_RISK_THRESHOLD:
            return True
    low = message.lower()
    return ("priority: high" in low
            or any(f"state: {s}" in low for s in _HIGH_STATES)
            or f"risk: {_ALERT_RISK_THRESHOLD}" in low
            or f"risk: 0.9" in low)


def _announce_agent_result(result: Any) -> None:
    """从 Agent 返回结果中提取需播报的高风险灾害文本并语音播报。

    仅播报高危 send_alert(message 或 args 满足 Priority:HIGH / 高危 State / Risk>=0.8),
    或含高危关键词的 event_summary 兜底; 同一文本 60s 内去重。
    任何 TTS/解析失败仅跳过, 绝不影响视觉检测主流程。
    """
    global _VOICE
    try:
        entries = result if isinstance(result, list) else [result]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            task = entry.get("task")
            root = task if isinstance(task, dict) else entry
            if not isinstance(root, dict):
                continue
            # 1) 仅播报满足高危条件的 send_alert message
            text = ""
            actions = root.get("actions")
            for a in (actions if isinstance(actions, list) else []):
                if not isinstance(a, dict) or a.get("tool") != "communication_tool":
                    continue
                args = a.get("args") or {}
                if (isinstance(args, dict) and args.get("action") == "send_alert"
                        and args.get("message")):
                    msg = str(args["message"])
                    if _is_high_risk(args, msg):
                        text = msg
                    break
            # 2) 无高危 alert → event_summary 兜底(仅含高危关键词才播)
            if not text:
                summary = str(root.get("event_summary") or "")
                if any(k in summary for k in _EVENT_KEYWORDS):
                    text = summary
            if not text or not str(text).strip():
                continue

            # 3) 去重: 同一文本 _ANNOUNCE_TTL 秒内只播一次
            now = time.time()
            h = hash(text)
            if len(_announced_alert_cache) > 128:
                _announced_alert_cache = {
                    k: v for k, v in _announced_alert_cache.items()
                    if now - v < _ANNOUNCE_TTL
                }
            last = _announced_alert_cache.get(h)
            if last is not None and now - last < _ANNOUNCE_TTL:
                continue
            _announced_alert_cache[h] = now

            print("[VOICE] announce:", str(text).strip())
            if VoiceAgent is None:
                continue
            if _VOICE is None:
                _VOICE = VoiceAgent()
            _VOICE.broadcast(str(text).strip())
    except Exception:  # noqa: BLE001 — 播报失败不阻断视觉主循环
        pass


def _voice_enabled() -> bool:
    """VOICE_ENABLED=1 时开启按钮语音交互线程。"""
    return os.environ.get("VOICE_ENABLED", "").strip().lower() in ("1", "true", "yes")


def start_voice_thread() -> None:
    """后台启动按钮语音闭环 (VoiceAgent.run), 不阻塞视觉主循环。

    复用 _VOICE 单例(与视觉报警播报共用); 线程 daemon, 异常仅打印不影响视觉。
    """
    global _VOICE
    try:
        if VoiceAgent is None:
            print("[VOICE] thread failed: VoiceAgent 不可用")
            return
        if _VOICE is None:
            _VOICE = VoiceAgent()
        th = threading.Thread(target=_VOICE.run, name="voice-agent", daemon=True)
        th.start()
        print("[VOICE] Voice thread started")
    except Exception as exc:  # noqa: BLE001 — 语音线程失败不影响视觉
        print("[VOICE] thread failed:", exc)

# 默认配置文件 (repo 根/config/edge.yaml)
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "edge.yaml"
)


class EdgeEncodeError(Exception):
    """JPEG 编码失败。"""


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """读取 edge.yaml 为 dict; 缺失返回空 dict (由 main/调用方兜底)。"""
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg


# ----------------------------------------------------------------------
# 摄像头 (Picamera2)
# ----------------------------------------------------------------------
class EdgeCam:
    """兼容包装: 保证既有 EdgeClient 感知/上传的取帧接口（``iter_frames``/``capture``/``stop``）。

    帧源两种：
    - ``reader=`` 注入（测试/Fake）；
    - ``service=``（S1 CameraService）：经 EdgeCamAdapter 订阅取帧——图像处理与旧
      ``src/camera.py::CameraFrameReader`` 语义一致（独立副本 + 180° 旋转，不做 RGB→BGR）。
    仅订阅、不拥有摄像头生命周期；release 不影响其他订阅者。
    capture() 返回单帧 ndarray（保持旧 CameraFrameReader 的通道序与 180° 旋转）。
    """

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: float = 15.0,
        reader: Any = None,
        service: Any = None,
    ):
        """入参 width/height/fps 供真实 reader 初始化; reader 可注入(Fake 测试); service 可注入 S1 CameraService。

        - 不提供 reader 且不提供 service：懒加载 src/camera.py CameraFrameReader（既有行为）。
        - 提供 service（S1 CameraService）：经 EdgeCamAdapter 订阅取帧（BGR+180°+copy），
          仅订阅、不拥有摄像头生命周期；release 不影响其他订阅者。
        """
        if reader is None and service is not None:
            from src.edge_cam_adapter import EdgeCamAdapter
            reader = EdgeCamAdapter(service, width=int(width), height=int(height), fps=float(fps))
        if reader is None:
            # 懒加载: 仅真实实例化时引入 src/camera.py (脚本/测试可摆脱 Pi 依赖)
            from camera import CameraFrameReader
            reader = CameraFrameReader(
                {"camera": {"width": int(width), "height": int(height), "fps": float(fps)}}
            )
        self._reader = reader
        self._frames = iter(reader.iter_frames())

        self.width = int(getattr(reader, "width", width))
        self.height = int(getattr(reader, "height", height))

    def capture(self):
        """阻塞取最新一帧, 返回 BGR uint8 ndarray。"""
        _, _, frame_bgr = next(self._frames)
        return frame_bgr

    def close(self) -> None:
        try:
            self._reader.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 摄像头生命周期（V4.4 B：统一 CameraService 唯一硬件持有者）
# ----------------------------------------------------------------------
class CameraInitError(RuntimeError):
    """摄像头初始化失败。"""


def _camera_spec(camera_cfg: dict, default_fps: float = 15.0):
    """从 edge.yaml camera 段取宽/高/fps（缺省与旧 CameraFrameReader 默认一致）。"""
    w = int(camera_cfg.get("width", 1280))
    h = int(camera_cfg.get("height", 720))
    f = float(camera_cfg.get("fps", default_fps))
    return w, h, f


def _open_camera(camera_cfg: dict):
    """创建并启动唯一 CameraService，返回 (CameraService, EdgeCam)。

    - CameraService.start() 失败/故障 → 抛 CameraInitError（不伪造、不建第二个实例）。
    - EdgeCam(service=...) 订阅同一 CameraService（仅订阅，不拥有硬件）。
    - 任一步失败都 close 已创建的 CameraService，避免遗留摄像头资源。
    """
    from src.camera_service import CameraService
    w, h, f = _camera_spec(camera_cfg)
    svc = CameraService(width=w, height=h, fps=f)
    try:
        svc.start()
        if svc.fault():
            raise CameraInitError(f"摄像头初始化失败: {svc.fault()}")
        cam = EdgeCam(service=svc, width=w, height=h, fps=f)
    except Exception:
        try:
            svc.close()
        except Exception:
            pass
        raise
    return svc, cam


def _close_camera(svc, cam) -> None:
    """逆序幂等释放：先解 EdgeClient 订阅，再停采集，最后统一关闭 CameraService。"""
    try:
        if cam is not None:
            cam.close()          # EdgeCam.close → EdgeCamAdapter.release（只解自身订阅）
    except Exception:
        print("[EdgeClient] EdgeCam release failed (ignored, idempotent)")
    if svc is not None:
        try:
            svc.stop()
        except Exception:
            print("[EdgeClient] CameraService.stop failed (ignored)")
        try:
            svc.close()
        except Exception:
            print("[EdgeClient] CameraService.close failed (ignored)")


def _shutdown_edge(svc, cam, e7e8=None) -> bool:
    """EdgeClient 退出清理：先关闭 E7/E8（若存在），仅当其完全关闭才关闭共享 CameraService。

    返回 CameraService 是否已关闭。e7e8 未完全关闭时保持摄像头存活、保留引用，交由进程退出回收。
    """
    if e7e8 is not None:
        try:
            if not e7e8.close():
                print("[EdgeClient][E7/E8] 关闭未完成；保留共享 CameraService（不提前关闭），"
                      "交由进程退出回收")
                return False
        except Exception as exc:  # noqa: BLE001
            print(f"[EdgeClient][E7/E8] close failed: {exc}；保留 CameraService")
            return False
    _close_camera(svc, cam)
    return True


def _build_e7e8(cfg, camera_service, config_path):
    """按配置在 EdgeClient 进程内启动 E7(+可选 E8) 组合，共享传入的唯一 CameraService。

    - ``sensor.enabled=false`` → 不启用（EdgeClient 保持纯感知兼容）。
    - 复用 SensorRunner/EvidenceRecorder/E8CameraAdapter；E8 订阅与感知同一 camera_service。
    - 本轮不连接真实 OSS：uploader=None（EvidenceRecorder 不伪造成功；Pi 真机由部署接线）。
    - 返回已 start 的 EdgeE7E8，或 None（未启用）；初始化失败由调用方捕获降级为纯感知。
    """
    section = cfg.get("sensor")
    if not isinstance(section, dict) or not bool(section.get("enabled")):
        return None
    from src.e7e8_integration import EdgeE7E8
    from src.sensor.config import load_sensor_config
    from src.database.db import Database
    from scripts.e7e8_runner import load_e8_config   # 复用 e8 配置校验
    sensor_cfg = load_sensor_config(config_path)
    e8_cfg = load_e8_config(config_path)
    db = Database(db_path=str((cfg.get("db") or {}).get("path") or "data/drrm.db"))
    obj = EdgeE7E8(
        sensor_cfg, e8_cfg, db,
        camera_service=camera_service,
        uploader=None,
        sleep=time.sleep,
    )
    obj.start()
    return obj


# ----------------------------------------------------------------------
# 图片编码
# ----------------------------------------------------------------------
def encode_jpeg(frame, quality: int = 90) -> bytes:
    """BGR 帧 → JPEG bytes; 非法帧或编码失败抛 EdgeEncodeError。"""
    # 先校验帧类型, 避免 cv2.imencode 对非数组抛非预期异常
    if not hasattr(frame, "shape") or getattr(frame, "ndim", 0) not in (2, 3):
        raise EdgeEncodeError("JPEG encode failed: 非法帧类型")
    ok, buf = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not ok or buf is None:
        raise EdgeEncodeError("JPEG encode failed")
    return buf.tobytes()


# ----------------------------------------------------------------------
# HTTP 上传
# ----------------------------------------------------------------------

def _build_vision_client(cfg: Dict[str, Any]) -> Optional[Any]:
    """按 config.assistant.vision_snapshot_url 构建 VisionClient; 空则返回 None(关闭同步)。"""
    try:
        from src.vision_client import VisionClient
        url = str(((cfg or {}).get("assistant") or {}).get("vision_snapshot_url") or "").strip()
        if not url:
            return None
        return VisionClient(snapshot_url=url)
    except Exception:  # noqa: BLE001 — 构建失败仅关闭该旁路, 不影响主流程
        return None


class EdgeUploader:
    """单次把 JPEG + person_events 以 multipart 上传到 Gateway。

    transport 缺省 requests.post; 测试注入 FakeTransport。
    """

    def __init__(
        self,
        api_url: str,
        transport: Optional[Callable[..., Any]] = None,
        timeout: float = 30.0,
    ):
        self.api_url = api_url
        self._post = transport if transport is not None else requests.post
        self.timeout = timeout
        self.last_snapshot_url: Optional[str] = None  # 最近一次成功 upload 的 snapshot_url

    def upload(
        self, image_bytes: bytes, person_events: Optional[list] = None
    ) -> Tuple[int, Any]:
        """上传一帧, 返回 (HTTP status, 响应 JSON)。

        成功后把响应的 snapshot_url 缓存在 self.last_snapshot_url,
        供边缘 Agent 报警优先使用(server 已存 snapshot 时)。
        """
        files = {"image": ("frame.jpg", image_bytes, "image/jpeg")}
        data = {"person_events": json.dumps(person_events or [], ensure_ascii=False)}
        resp = self._post(self.api_url, files=files, data=data, timeout=self.timeout)
        try:
            body = resp.json()
        except Exception:
            body = {}
        if int(resp.status_code) == 200 and isinstance(body, dict):
            url = body.get("snapshot_url")
            if isinstance(url, str) and url:
                self.last_snapshot_url = url
        return int(resp.status_code), body


# ----------------------------------------------------------------------
# 结果解析/打印
# ----------------------------------------------------------------------
def extract_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    """从 Gateway 成功响应提取摘要 (Scene / Severity / Tasks)。"""
    vlm = body.get("vlm_output") or {}
    llm = body.get("llm_output") or {}
    tasks = [p.get("action") for p in (llm.get("task_proposals") or [])]
    return {
        "scene": vlm.get("scene_type", "unknown"),
        "severity": vlm.get("overall_severity", "unknown"),
        "tasks": tasks or [],
    }


def _print_summary(body: Dict[str, Any]) -> None:
    s = extract_summary(body)
    print("================================")
    print("Edge AI Response")
    print("Scene:")
    print(s["scene"])
    print("Severity:")
    print(s["severity"])
    print("Tasks:")
    print(", ".join(s["tasks"]) if s["tasks"] else "none")
    print("================================")


def _print_log(ts: str, w: int, h: int, status: int) -> None:
    print("[EdgeClient]")
    print(ts)
    print(f"uploaded: {w}x{h}")
    print(f"response: {status}")


# ----------------------------------------------------------------------
# 预览模式 (开发/调试用; opt-in, 默认关闭)
# ----------------------------------------------------------------------
def render_overlay(frame, person_events):
    """在帧上叠加 person_events 文字信息 (仅文本, 不画 bbox)。

    供 --preview 调试; 不改变 perception 输出 schema。
    """
    out = frame.copy()
    y = 24
    for i, ev in enumerate(person_events, start=1):
        _ = i  # person_id 优先取自事件
        lines = [
            f"Person {ev.get('person_id', '-')}",
            f"State: {ev.get('state', '-')}",
            f"Risk: {ev.get('risk_score', 0):.2f}",
            f"Priority: {ev.get('priority', '-')}",
        ]
        for line in lines:
            cv2.putText(out, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2, cv2.LINE_AA)
            y += 24
        y += 10
    return out


def _show_preview(frame, person_events) -> bool:
    """显示预览窗口; 返回 False 表示用户按 q 退出。"""
    cv2.imshow("DRRM Edge Preview", render_overlay(frame, person_events))
    return (cv2.waitKey(1) & 0xFF) != ord("q")


def run_preview_only(
    cam,
    max_frames: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    show: Optional[Callable[[Any, list], bool]] = None,
) -> int:
    """纯预览调试 (--preview-only): 连续采集并显示实时画面。

    - 不执行 YOLO/MediaPipe, 不 JPEG 编码, 不 HTTP 上传, 不依赖 Windows Gateway;
    - 复用 EdgeCam(内部 CameraFrameReader, 含 ov5647 颜色/旋转修复);
    - 窗口标题 "DRRM Edge Preview", 按 q 正常退出;
    - headless(无显示) 抛错时打印并结束, 不卡死。

    show 可注入(Fake 测试); max_frames 限制显示帧数(测试); sleep 每帧极短空转
    以便 waitKey 及时处理事件。返回实际显示帧数。
    """
    show = show if show is not None else _show_preview
    frames = 0
    while True:
        if max_frames is not None and frames >= max_frames:
            break
        try:
            frame = cam.capture()
        except Exception as exc:
            print(f"[PreviewOnly] capture failed: {exc}")
            sleep(0.5)
            continue
        try:
            if not show(frame, []):  # 按 q 退出
                break
        except Exception as exc:
            print(f"[PreviewOnly] display failed (headless?): {exc}")
            break
        frames += 1
        sleep(0.001)  # 让 waitKey(1) 获得事件处理机会
    return frames


# ----------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------
def run_loop(
    uploader: EdgeUploader,
    cam,
    interval: float = 10,
    quality: int = 90,
    max_uploads: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    perception: Optional[Any] = None,
    preview: bool = False,
    agent: Optional[Any] = None,
    observation: Optional[Dict[str, Any]] = None,
    flush_state: Optional[Dict[str, Any]] = None,
    vision_client: Optional[Any] = None,
    probe_scheduler: Optional[Callable[[Any], Any]] = None,
) -> int:
    """周期性 采集→(感知)→编码→上传→打印; preview 时叠加显示相机帧。

    任意单步失败(采集/编码/感知/网络/API错误)均打印并**继续**, 不退出。
    max_uploads 限制迭代次数(供测试/--once); sleep 可注入(测试用 no-op)。
    perception: 本地感知桥接(EdgePerception), 提供 analyze(frame)->person_events;
                为 None 时上传空 person_events。
    agent: V4.0-B Agent 决策辅助(旁路). 为 None 时完全不启用;
           异常只打印不阻断 upload. 必须由调用方在 main() 中初始化一次(跨帧复用,
           保证 memory cooldown 生效, 避免重复报警).
    observation: 观测配置 {enabled,status_every,flush_every}; 为 None 时不输出摘要。
    flush_state: pending 补偿状态 {caller, flush_every}; caller 为可调用对象,
                 每 flush_every 帧调一次(no-arg) 用于补发 communication pending。
    """
    obs_cfg = observation if isinstance(observation, dict) else {}
    obs_enabled = bool(obs_cfg.get("enabled", True))
    status_every = int(obs_cfg.get("status_every", 50))
    flush_every = int(
        (flush_state or {}).get("flush_every")
        if flush_state
        else obs_cfg.get("flush_every", 50)
    )
    flush_caller = (flush_state or {}).get("caller") if flush_state else None

    iterations = 0
    while True:
        if max_uploads is not None and iterations >= max_uploads:
            break
        iterations += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # 同一次迭代双路径(规则 process_frame + LLM ingest)复用同一 task_id,
        # 使 SQLite tasks 表同一次事件只产一行主要任务(executor 按 task_id upsert)。
        iter_task_id = "T-%s-%s" % (
            time.strftime("%Y%m%d%H%M%S"),
            uuid.uuid4().hex[:6],
        )

        try:
            frame = cam.capture()
            # 非图像帧(缺 shape)视作采集失败
            if not hasattr(frame, "shape"):
                raise TypeError("非法帧: 缺少 shape")
            w, h = frame.shape[1], frame.shape[0]
        except Exception:
            print("[EdgeClient]")
            print(ts)
            print("camera capture failed")
            sleep(interval)
            continue

        # ── X4 云端恢复探测：仅当有调度器时，按单调时钟/冷却恢复态节流调用。
        # 调度器内部负责单调时钟节流；run_loop 只把当前帧传给调度器(由调度器决定是否探测)。
        # 云端正常/禁用时调度器返回 None(无开销)；冷却结束恢复态才会发真实短超时探测。
        if probe_scheduler is not None:
            try:
                probe_scheduler(frame)
            except Exception:  # noqa: BLE001 — 探测调度失败不得阻断主帧上传
                print("[EdgeClient] x4 probe scheduler error (main loop continues)")

        try:
            jpeg = encode_jpeg(frame, quality)
        except EdgeEncodeError as exc:
            print("[EdgeClient]")
            print(ts)
            print(f"{exc}")
            sleep(interval)
            continue

        # 本地感知桥接: 生成 person_events (失败不阻塞, 仅上传 image)
        try:
            person_events = perception.analyze(frame) if perception else []
        except Exception:
            print("[EdgeClient]")
            print(ts)
            print("perception analyze failed")
            person_events = []

        # ── Vision Snapshot 旁路同步 (Phase7.3): 把最新 person_events 推送 server vision API。
        #    在此处: 数据最新/未经后续处理; 独立旁路, 失败绝不影响 camera/感知/Agent/上传/云端分析。
        if vision_client is not None:
            try:
                vision_client.upload(person_events)
            except Exception:  # noqa: BLE001 — 同步失败静默, 不阻断主 pipeline
                pass

        # 调试可观测性: 发送前打印本地感知结果 (不改变上传协议/数据结构)
        if perception is not None:
            print("[Perception]")
            print("person_events:")
            print(json.dumps(person_events, indent=2, ensure_ascii=False))

        # V4.0-B Agent 决策辅助: 旁路接入, 不修改 person_events, 不影响 upload
        # 失败只打印并继续, 绝不阻断上传链路
        if agent is not None and person_events:
            try:
                # ── Phase 2.5: HIGH 人员保存现场快照, 供 DingTalk 图片报警 ──
                # 在 frame_record 构建前, 把快照保存给 HIGH 人员, 并注入 snapshot_path。
                from src.snapshot import save_snapshot
                _events = list(person_events)  # 浅拷贝, 不修改原上传 payload
                for _ev in _events:
                    if not isinstance(_ev, dict):
                        continue
                    _prio = str(_ev.get("priority", "")).upper()
                    _risk = _ev.get("risk_score")
                    _high = (_prio == "HIGH") or (
                        isinstance(_risk, (int, float)) and _risk >= 0.60
                    )
                    if _high:
                        # 优先用 server 最近一次返回的 snapshot_url; 否则 fallback 本地快照
                        _server_url = getattr(uploader, "last_snapshot_url", None)
                        if _server_url:
                            _ev["image_path"] = _server_url
                            _ev["snapshot_url"] = _server_url
                            _ev.pop("snapshot_path", None)
                            print("[Snapshot] using remote snapshot_url: " + str(_server_url))
                        else:
                            _snap = save_snapshot(frame, _ev.get("person_id"))
                            if _snap:
                                _ev["snapshot_path"] = _snap
                                _ev["image_path"] = _snap
                                _ev.pop("snapshot_url", None)
                                print("[Snapshot] saved: " + str(_snap))

                from src.agent.input_adapter import person_events_to_frame_record
                frame_record = person_events_to_frame_record(
                    _events,
                    timestamp=ts,
                )
                _pa_result = agent.process_frame(
                    frame_record,
                    context={"task_id": iter_task_id},
                )
                _announce_agent_result(_pa_result)
            except Exception as exc:  # noqa: BLE001
                print("[EdgeClient]")
                print(ts)
                print(f"agent process failed (upload continues): {exc}")

        # ── 阶段1A: Agent 运行状态周期摘要(不阻断主循环) ──
        if obs_enabled and agent is not None and status_every > 0 \
                and iterations % status_every == 0:
            try:
                stats = getattr(agent, "stats", {}) or {}
                print("[AgentStatus]")
                print("frames:")
                print(iterations)
                print("triggers_received:")
                print(stats.get("triggers_received", 0))
                print("plans_created:")
                print(stats.get("plans_created", 0))
                print("tasks_completed:")
                print(stats.get("tasks_completed", 0))
                print("tasks_failed:")
                print(stats.get("tasks_failed", 0))
                print("tasks_suppressed:")
                print(stats.get("tasks_suppressed", 0))
            except Exception:  # noqa: BLE001 — 观测失败绝不阻断上传
                print("[EdgeClient] observation status output failed")

        # ── 阶段1B: communication pending 自动补偿(周期 flush, 不阻断) ──
        if flush_caller is not None and flush_every > 0 \
                and iterations % flush_every == 0:
            try:
                result = flush_caller()
                if isinstance(result, dict):
                    flushed = result.get("flushed")
                    remaining = result.get("remaining")
                    print("[EdgeClient] flush_pending result: "
                          f"flushed={flushed} remaining={remaining}")
            except Exception:  # noqa: BLE001
                print("[EdgeClient] flush_pending failed")

        # 预览模式: 叠加 person_events 文字显示相机帧; 按 q 退出
        # (headless/无显示环境抛错时自动关闭预览, 不影响后续上传)
        if preview:
            try:
                if not _show_preview(frame, person_events):
                    break
            except Exception:
                preview = False

        try:
            status, body = uploader.upload(jpeg, person_events=person_events)
        except requests.RequestException:
            print("[EdgeClient]")
            print(ts)
            print("API connection failed")
            sleep(interval)
            continue

        _print_log(ts, w, h, status)
        if status == 200:
            _print_summary(body if isinstance(body, dict) else {})
            # ── 路径B: 云端 AI (VLM+LLM) 决策接入 Agent ──
            # server 返回的 llm_output 正式喂给 Agent.ingest_llm_output,
            # 走 planner→executor→tools→DingTalk/SQLite。
            # 与路径A(边缘规则 process_frame)并存不互相覆盖;
            # 失败/缺 llm_output 时静默降级, 不影响原规则报警与上传。
            if agent is not None and isinstance(body, dict):
                llm_output = body.get("llm_output")
                if llm_output:
                    try:
                        print("[LLM->Agent] ingest_llm_output triggered")
                        result = agent.ingest_llm_output(
                            llm_output,
                            context={
                                "image_url":
                                    body.get("snapshot_url")
                                    or getattr(uploader, "last_snapshot_url", None),
                                "task_id": iter_task_id,
                                # V4.3 报警融合门: 环境危险等级(server 透传 VLM overall_severity)
                                "environment_severity":
                                    body.get("environment_severity") or "unknown",
                            },
                        )
                        print(
                            "[LLM->Agent] result:",
                            result,
                        )
                        _announce_agent_result(result)
                    except Exception as exc:  # noqa: BLE001
                        print("[EdgeClient]")
                        print(ts)
                        print(f"llm output ingest failed (rule alert unaffected): {exc}")
                        print(
                            "[LLM->Agent] failed:",
                            exc,
                        )
        else:
            err = body.get("error") if isinstance(body, dict) else ""
            print(f"server error: {status} {err}")

        sleep(interval)
    return iterations


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Disaster_Rescue_DRRM Edge Client")
    parser.add_argument(
        "--server", help="覆盖 Gateway API URL (默认读 config/edge.yaml)"
    )
    parser.add_argument("--interval", type=int, help="上传间隔秒数")
    parser.add_argument("--quality", type=int, help="JPEG 质量 0-100")
    parser.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    parser.add_argument(
        "--once", action="store_true", help="只上传一帧后退出 (调试/测试)"
    )
    parser.add_argument(
        "--preview", action="store_true", help="显示相机实时预览窗口 (仅叠加文字, 按 q 退出)"
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="纯预览调试: 连续显示摄像头实时画面, 不感知/不编码/不上传, 不需 Gateway (按 q 退出)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    server_cfg = cfg.get("server", {}) or {}
    cam_cfg = cfg.get("camera", {}) or {}
    up_cfg = cfg.get("upload", {}) or {}

    # 纯预览调试: 不需要 api_url / uploader / perception, 独立于上传链路提前返回
    if args.preview_only:
        svc, cam = _open_camera(cam_cfg)
        try:
            frames = run_preview_only(cam)
        finally:
            _close_camera(svc, cam)
        print(f"[PreviewOnly] preview ended, shown {frames} frames")
        return 0

    api_url = args.server or server_cfg.get("api_url")
    if not api_url:
        print("缺少 server.api_url 配置 (config/edge.yaml 或 --server)")
        return 1

    uploader = EdgeUploader(api_url)
    svc, cam = _open_camera(cam_cfg)

    # V4.4-B 第二批：E7/E8 组合（可选）共享同一 CameraService。
    # 配置/初始化失败只降级为纯感知，不拖垮检测主循环。
    e7e8 = None
    try:
        e7e8 = _build_e7e8(cfg, svc, args.config)
        if e7e8 is not None:
            print(f"[EdgeClient] E7/E8 enabled: mode={e7e8.mode}")
    except Exception as exc:  # noqa: BLE001
        print(f"[EdgeClient][E7/E8] init failed, continue detection only: {exc}")
        traceback.print_exc()
        e7e8 = None
    interval = args.interval if args.interval is not None else up_cfg.get("interval", 10)
    quality = args.quality if args.quality is not None else up_cfg.get("jpeg_quality", 90)

    # 本地感知桥接: 默认开启; 初始化失败回退为仅上传图片, 不阻塞
    # V4.4 X4 组合根接入: 经 src/x4.composition.build_x4_perception() 装配感知组件,
    # 使 X4DetectionService 成为检测后端(云端失败时其内部回退本地 Detector)。
    # 姿态/状态/风险/排序/事件生成与 Agent 输入格式不变。
    perception = None
    probe_scheduler = None
    perception_cfg = cfg.get("perception", {}) or {}
    if perception_cfg.get("enabled", True):
        # 阶段一：X4 日志专用 Database（独立于感知构建，不依赖 sensor.enabled）。
        # 初始化失败仅禁用 X4 日志，绝不影响云端检测/本地兜底。
        x4_db = None
        try:
            from src.database.db import Database
            x4_db = Database(db_path=str((cfg.get("db") or {}).get("path") or "data/drrm.db"))
        except Exception as exc:  # noqa: BLE001
            print(f"[EdgeClient] X4 DB init failed, X4 logging disabled: {exc}")
            x4_db = None
        # 阶段二：X4 感知组件构建（注入 db）；仅此阶段失败才走既有感知降级。
        try:
            from src.x4.composition import build_x4_perception, make_probe_scheduler
            perception = build_x4_perception(db=x4_db)
            probe_scheduler = make_probe_scheduler(perception)
        except Exception as exc:  # noqa: BLE001
            print(f"[EdgeClient] perception init failed, upload image only: {exc}")
            probe_scheduler = None

    # V4.0-B Agent 决策辅助: main() 中一次性初始化, 跨帧复用 (memory cooldown 生效)
    # agent.enabled=true 才初始化; 失败回退为仅上传(不阻塞 camera/perception/upload)。
    agent = None
    agent_ready = False
    agent_cfg = cfg.get("agent", {}) or {}
    if agent_cfg.get("enabled", False):
        try:
            from src.agent.agent import Agent
            agent = Agent(config=cfg)
            agent_ready = True
        except Exception:  # noqa: BLE001
            print("[EdgeClient][Agent] initialization failed, "
                  "fallback to upload only:")
            traceback.print_exc()
            agent = None
            agent_ready = False

    # ── 观测/补偿配置 (阶段1 A+B) ──
    obs_cfg = cfg.get("observation", {}) or {}
    obs_enabled = bool(obs_cfg.get("enabled", True))
    status_every = int(obs_cfg.get("status_every", 50))
    flush_every = int(obs_cfg.get("flush_every", 50))

    # ── 阶段1B: 独立 CommunicationTool 只用于 pending 补发 ──
    # 与 Agent 内部 communication 实例分隔; 共享同一 pending_path(读取同一队列)。
    # 通道由环境变量 COMMUNICATION_CHANNEL 决定(mock|wecom|dingtalk)。
    flush_caller = None
    if obs_enabled and flush_every > 0:
        try:
            from src.tools.communication_tool import CommunicationTool
            comm_cfg = cfg.get("communication", {}) or {}
            _flusher = CommunicationTool(
                log_path=comm_cfg.get("log_path", "data/communication_log.jsonl"),
                pending_path=comm_cfg.get("pending_path",
                                          "data/pending_communication.jsonl"),
                channel="auto",
            )
            flush_caller = _flusher.flush_pending
        except Exception:  # noqa: BLE001 — 补偿不可用不阻断主流程
            print("[EdgeClient] communication flusher init failed "
                  "(pending recovery disabled):")
            traceback.print_exc()
            flush_caller = None

    # ── V4.4: 按钮语音交互(可选, 后台线程, 视觉程序初始化完成后启动, 不阻塞视觉) ──
    if _voice_enabled():
        start_voice_thread()

    try:
        run_loop(
            uploader, cam, interval=interval, quality=quality,
            max_uploads=1 if args.once else None,
            perception=perception,
            preview=args.preview,
            agent=agent,
            observation={
                "enabled": obs_enabled,
                "status_every": status_every,
                "flush_every": flush_every,
            },
            flush_state={
                "flush_every": flush_every,
                "caller": flush_caller,
            },
            vision_client=_build_vision_client(cfg),
            probe_scheduler=probe_scheduler,
        )
    finally:
        _shutdown_edge(svc, cam, e7e8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())